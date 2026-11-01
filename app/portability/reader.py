"""Opening a `.rumbos` archive without trusting it.

Nothing here extracts anything: entries are read in memory, in bounded chunks,
and never named on disk after what the archive says. Everything that can be
checked from the ZIP's directory is checked BEFORE a byte is decompressed:

- the first bytes are a ZIP local header (no prefixed or self-extracting data);
- at most `max_entries` entries, each named exactly like one of the allowed
  paths (this alone rules out absolute and `..` paths, directories, nested
  archives and any name that is not plain ASCII), none twice, none whose name
  hides a NUL;
- no encryption, no symlinks or other special files, only the STORED and
  DEFLATED methods;
- every declared size within its limit, their sum within the total, and no
  implausible compression ratio.

Then each entry is read with a running total that stops at its limit (a header
that lies about its size cannot make us read more), hashed, and compared with
`manifest.json`, which must list exactly the other entries.
"""

import hashlib
import json
import re
import stat
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import ValidationError

from app.exceptions import (
    ArchiveInvalidError,
    ArchiveTooLargeError,
    ArchiveVersionUnsupportedError,
)
from app.portability.format import (
    ASSETS_DIR,
    ENTRY_PATTERN,
    FORMAT_VERSION,
    MANIFEST_PATH,
    MAPS_PATH,
    MIN_READER_VERSION,
    Manifest,
    parse_model,
)
from app.portability.problems import from_validation_error, problem

_LOCAL_HEADER = b"PK\x03\x04"
_CHUNK = 64 * 1024
_UNSAFE_FLAGS = 0x1 | 0x40 | 0x2000  # encrypted, strongly encrypted, masked headers
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_MiB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReaderLimits:
    max_bytes: int
    max_uncompressed_bytes: int
    max_entries: int
    # The largest single asset (the biggest per-kind upload limit).
    max_asset_bytes: int
    max_manifest_bytes: int = 1 * _MiB
    max_json_bytes: int = 24 * _MiB
    max_json_depth: int = 32
    # Uncompressed / compressed, for entries big enough for it to matter.
    max_ratio: int = 100
    ratio_from_bytes: int = 1 * _MiB


_CORRUPT = (
    zipfile.BadZipFile,
    EOFError,
    zlib.error,
    OSError,
    RuntimeError,
    NotImplementedError,
    ValueError,
)

_STRUCTURE = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"|[\[{]|[\]}]')


def _invalid(code: str, message: str, path: str = "") -> ArchiveInvalidError:
    return ArchiveInvalidError([problem(path, code, message)])


def json_depth_within(data: bytes, limit: int) -> bool:
    """Whether the JSON text nests no deeper than `limit`. A scan, not a parse:
    the real parser would hit Python's recursion limit on a hostile input."""
    depth = 0
    for match in _STRUCTURE.finditer(data):
        first = match.group()[0]
        if first == 0x22:  # a string
            continue
        if first in b"[{":
            depth += 1
            if depth > limit:
                return False
        else:
            depth -= 1
    return True


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON")


def parse_json(data: bytes, limits: ReaderLimits, path: str) -> Any:
    """JSON from untrusted bytes: strict UTF-8, bounded depth, no duplicate keys,
    no NaN."""
    if not json_depth_within(data, limits.max_json_depth):
        raise _invalid("json_too_deep", "The JSON is nested too deeply", path)
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constant,
        )
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise _invalid("json_invalid", "The file is not valid JSON", path) from None


class OpenedArchive:
    """An archive whose directory has been checked. Use as a context manager."""

    def __init__(
        self,
        zf: zipfile.ZipFile,
        infos: dict[str, zipfile.ZipInfo],
        limits: ReaderLimits,
    ) -> None:
        self._zf = zf
        self._infos = infos
        self._limits = limits
        self.manifest: Manifest | None = None
        self._declared: dict[str, tuple[str, int]] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._zf.close()

    @property
    def asset_paths(self) -> list[str]:
        return sorted(p for p in self._infos if p.startswith(ASSETS_DIR))

    def _cap_of(self, path: str) -> int:
        if path == MANIFEST_PATH:
            return self._limits.max_manifest_bytes
        if path == MAPS_PATH:
            return self._limits.max_json_bytes
        return self._limits.max_asset_bytes

    def _chunks(self, path: str) -> Iterator[bytes]:
        """The entry's bytes, stopping the moment it passes its limit or its own
        declared size."""
        info = self._infos[path]
        cap = min(self._cap_of(path), info.file_size)
        total = 0
        try:
            with self._zf.open(info) as handle:
                while chunk := handle.read(_CHUNK):
                    total += len(chunk)
                    if total > cap:
                        raise _invalid("entry_too_large", "An entry is too big", path)
                    yield chunk
        except _CORRUPT:
            raise _invalid("entry_corrupt", "An entry cannot be read", path) from None
        if total != info.file_size:
            raise _invalid(
                "size_mismatch", "An entry is not the size it declares", path
            )

    def read(self, path: str) -> bytes:
        return b"".join(self._chunks(path))

    def read_asset(self, path: str) -> bytes:
        """An asset's bytes, checked once more against the manifest."""
        data = self.read(path)
        expected = self._declared.get(path)
        if expected is None or hashlib.sha256(data).hexdigest() != expected[0]:
            raise _invalid(
                "hash_mismatch", "A file is not the one the manifest lists", path
            )
        return data

    def check_manifest(self) -> Manifest:
        """Reads and verifies `manifest.json` and every file it lists."""
        raw = parse_json(self.read(MANIFEST_PATH), self._limits, MANIFEST_PATH)
        check_versions(raw)
        try:
            manifest = parse_model(Manifest, raw)
        except ValidationError as exc:
            raise ArchiveInvalidError(
                from_validation_error(exc, prefix=MANIFEST_PATH)
            ) from None
        listed = {f.path: f for f in manifest.files}
        if set(listed) != set(self._infos) - {MANIFEST_PATH}:
            raise _invalid(
                "manifest_mismatch",
                "The manifest does not list exactly the files of the archive",
                MANIFEST_PATH,
            )
        for path, entry in listed.items():
            digest = hashlib.sha256()
            for chunk in self._chunks(path):
                digest.update(chunk)
            if self._infos[path].file_size != entry.size:
                raise _invalid("size_mismatch", "A file is not the size listed", path)
            if digest.hexdigest() != entry.sha256:
                raise _invalid(
                    "hash_mismatch", "A file is not the one the manifest lists", path
                )
            self._declared[path] = (entry.sha256, entry.size)
        self.manifest = manifest
        return manifest

    def read_maps(self) -> Any:
        return parse_json(self.read(MAPS_PATH), self._limits, MAPS_PATH)


def check_versions(raw: object) -> None:
    """`ArchiveVersionUnsupportedError` unless this reader can read the archive.

    Looked at before anything else of the manifest, so an archive from a future
    format is told apart from a broken one. A reader refuses a `minReaderVersion`
    above its own, and a `formatVersion` it does not know."""
    if not isinstance(raw, dict):
        raise _invalid("invalid_type", "The manifest is not an object", MANIFEST_PATH)
    format_version, min_reader = raw.get("formatVersion"), raw.get("minReaderVersion")
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or isinstance(min_reader, bool)
        or not isinstance(min_reader, int)
    ):
        raise _invalid("invalid_type", "The versions are not integers", MANIFEST_PATH)
    if min_reader > MIN_READER_VERSION or format_version != FORMAT_VERSION:
        raise ArchiveVersionUnsupportedError(FORMAT_VERSION)


def _inspect(zf: zipfile.ZipFile, limits: ReaderLimits) -> dict[str, zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > limits.max_entries:
        raise _invalid("too_many_entries", "The archive has too many entries")
    names = Counter(i.filename for i in infos)
    problems: list[dict[str, str]] = []
    total = 0
    for index, info in enumerate(infos):
        where = f"entries[{index}]"
        # `orig_filename` still has what `filename` loses after a NUL byte.
        if info.orig_filename != info.filename or not ENTRY_PATTERN.fullmatch(
            info.filename
        ):
            problems.append(
                problem(where, "entry_name", "An entry has a name that is not allowed")
            )
            continue
        if names[info.filename] > 1:
            problems.append(problem(where, "entry_duplicate", "An entry appears twice"))
        if info.flag_bits & _UNSAFE_FLAGS:
            problems.append(
                problem(where, "entry_encrypted", "Encrypted entries are not accepted")
            )
        mode = info.external_attr >> 16
        if (
            info.is_dir()
            or info.external_attr & 0x10
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
        ):
            problems.append(
                problem(where, "entry_special", "Only regular files are accepted")
            )
        if info.compress_type not in _METHODS:
            problems.append(problem(where, "entry_method", "Unsupported compression"))
        cap = {
            MANIFEST_PATH: limits.max_manifest_bytes,
            MAPS_PATH: limits.max_json_bytes,
        }.get(info.filename, limits.max_asset_bytes)
        if info.file_size > cap:
            problems.append(problem(where, "entry_too_large", "An entry is too big"))
        total += info.file_size
        if (
            info.file_size > 0
            and info.compress_size == 0
            or (
                info.file_size >= limits.ratio_from_bytes
                and info.file_size > limits.max_ratio * info.compress_size
            )
        ):
            problems.append(
                problem(where, "compression_ratio", "Implausible compression")
            )
    if total > limits.max_uncompressed_bytes:
        problems.append(
            problem(
                "", "too_large_uncompressed", "The archive is too big once unpacked"
            )
        )
    for required in (MANIFEST_PATH, MAPS_PATH):
        if required not in names:
            problems.append(
                problem(required, "entry_missing", "A required file is missing")
            )
    if problems:
        raise ArchiveInvalidError(problems[:50])
    return {i.filename: i for i in infos}


def open_archive(path: Path, limits: ReaderLimits) -> OpenedArchive:
    """The archive at `path`, its directory checked and `manifest.json` verified
    against every file. Raises `ArchiveTooLargeError`, `ArchiveInvalidError` or
    `ArchiveVersionUnsupportedError`; never leaves the file open on failure."""
    if path.stat().st_size > limits.max_bytes:
        raise ArchiveTooLargeError(limits.max_bytes)
    with path.open("rb") as head:
        if head.read(4) != _LOCAL_HEADER:
            raise _invalid("not_a_zip", "The file is not a .rumbos archive")
    try:
        zf = zipfile.ZipFile(path)
    except (*_CORRUPT, UnicodeDecodeError):
        raise _invalid("not_a_zip", "The file is not a .rumbos archive") from None
    try:
        opened = OpenedArchive(zf, _inspect(zf, limits), limits)
        opened.check_manifest()
    except BaseException:
        zf.close()
        raise
    return opened

"""The hardened reader against hostile archives.

Every test hands the reader a file somebody could really upload and checks that
it is refused with a fixed code, before or while it is read, and that nothing is
ever written to disk.
"""

import io
import warnings
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.exceptions import (
    ArchiveInvalidError,
    ArchiveTooLargeError,
    ArchiveVersionUnsupportedError,
)
from app.portability.reader import (
    ReaderLimits,
    json_depth_within,
    open_archive,
    parse_json,
)
from tests import archive_builders as ab

LIMITS = ReaderLimits(
    max_bytes=10_000_000,
    max_uncompressed_bytes=50_000_000,
    max_entries=50,
    max_asset_bytes=1_000_000,
)


def write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "in.rumbos"
    path.write_bytes(data)
    return path


def codes(exc: pytest.ExceptionInfo[ArchiveInvalidError]) -> set[str]:
    return {p["code"] for p in exc.value.problems}


def refused(tmp_path: Path, data: bytes, limits: ReaderLimits = LIMITS) -> set[str]:
    with pytest.raises(ArchiveInvalidError) as caught:
        open_archive(write(tmp_path, data), limits).close()
    return codes(caught)


def zip_with(
    entries: list[tuple[zipfile.ZipInfo, bytes]],
    compress: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    out = io.BytesIO()
    with (
        zipfile.ZipFile(out, "w", compression=compress) as zf,
        warnings.catch_warnings(),
    ):
        warnings.simplefilter("ignore")  # duplicate names warn on write
        for info, data in entries:
            zf.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue()


def info(
    name: str, mode: int = 0o600, method: int = zipfile.ZIP_DEFLATED
) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, date_time=ab.FIXED_TIME)
    entry.external_attr = mode << 16
    entry.compress_type = method
    return entry


def set_flag(data: bytes, name: str, flag: int) -> bytes:
    """`flag` set in the general-purpose bits of `name`, in its local header and
    its directory record (`writestr` resets them, so they are patched in)."""
    raw = bytearray(data)
    encoded = name.encode("ascii")
    # (signature, flags at, name length at, name at)
    layouts = (
        (b"PK\x03\x04", 6, 26, 30),
        (b"PK\x01\x02", 8, 28, 46),
    )
    for signature, flags_at, length_at, name_at in layouts:
        start = 0
        while (at := bytes(raw).find(signature, start)) != -1:
            start = at + 4
            length = int.from_bytes(raw[at + length_at : at + length_at + 2], "little")
            if raw[at + name_at : at + name_at + length] == encoded:
                raw[at + flags_at] |= flag & 0xFF
                raw[at + flags_at + 1] |= flag >> 8
    return bytes(raw)


def with_extra_entry(name: str, data: bytes = b"x", **kw: Any) -> bytes:
    """A consistent archive plus one more entry the manifest does not list."""
    base = zipfile.ZipFile(io.BytesIO(ab.archive()))
    entries = [(info(i.filename), base.read(i)) for i in base.infolist()]
    entries.append((info(name, **kw), data))
    return zip_with(entries)


# --- the good case ----------------------------------------------------------------


def test_a_good_archive_is_opened_and_verified(tmp_path: Path) -> None:
    png = ab.good_png()
    maps = ab.maps_file(assets=[ab.asset_entry("a1", png)])
    maps["maps"][0]["background"] = {"assetRef": "a1"}
    data = ab.archive(maps, {"assets/a1.png": png})
    with open_archive(write(tmp_path, data), LIMITS) as opened:
        assert opened.manifest is not None
        assert opened.manifest.counts.assets == 1
        assert opened.asset_paths == ["assets/a1.png"]
        assert opened.read_asset("assets/a1.png") == png
        assert opened.read_maps()["maps"][0]["ref"] == "m1"


def test_opening_never_writes_anything(tmp_path: Path) -> None:
    path = write(tmp_path, ab.archive())
    before = sorted(p.name for p in tmp_path.iterdir())
    open_archive(path, LIMITS).close()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# --- not an archive ---------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"", b"hello", b"PK\x03\x04", b"%PDF-1.7 " + b"x" * 100, b"\x89PNG\r\n\x1a\n"],
    ids=["empty", "text", "header-only", "pdf", "png"],
)
def test_a_file_that_is_not_a_zip_is_refused(tmp_path: Path, data: bytes) -> None:
    assert refused(tmp_path, data) == {"not_a_zip"}


def test_a_truncated_archive_is_refused(tmp_path: Path) -> None:
    data = ab.archive()
    assert refused(tmp_path, data[: len(data) // 2]) == {"not_a_zip"}


def test_data_in_front_of_the_archive_is_refused(tmp_path: Path) -> None:
    """A self-extracting or polyglot file starts with something else."""
    assert refused(tmp_path, b"MZ" + b"\x00" * 60 + ab.archive()) == {"not_a_zip"}


def test_an_archive_over_the_size_limit_is_refused(tmp_path: Path) -> None:
    small = ReaderLimits(100, 10_000, 50, 1000)
    with pytest.raises(ArchiveTooLargeError):
        open_archive(write(tmp_path, ab.archive()), small)


# --- entry names ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../evil.png",
        "assets/../../evil.png",
        "assets/../maps.json",
        "/etc/passwd",
        "/assets/a1.png",
        "C:/Windows/evil.png",
        "assets\\..\\evil.png",
        "assets/",
        "assets",
        "assets/a1.zip",
        "assets/a1.png.exe",
        "assets/A1.png",
        "assets/a1.PNG",
        "assets/a1.svg",
        "assets/a1.gif",
        "assets/aaa1.png",
        "assets/a1.png/",
        "assets//a1.png",
        "./maps.json",
        "maps.json ",
        "other.json",
        "assets/\u00e1.png",
        "assets/a1.png\n",
        "assets/a1\u202e.png",
    ],
)
def test_names_outside_the_allowed_ones_are_refused(tmp_path: Path, name: str) -> None:
    assert "entry_name" in refused(tmp_path, with_extra_entry(name))


def test_a_name_that_hides_a_nul_is_refused(tmp_path: Path) -> None:
    """zipfile cuts a name at a NUL byte, so `assets/a1.png\\0.php` would read as
    `assets/a1.png`. The reader looks at the name as it was written."""
    data = with_extra_entry("assets/a1.pngXphp")
    assert data.count(b"assets/a1.pngXphp") == 2  # local header and directory
    patched = data.replace(b"assets/a1.pngXphp", b"assets/a1.png\x00php")
    assert "entry_name" in refused(tmp_path, patched)


def test_a_name_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    data = set_flag(with_extra_entry("assets/a1.pngEE"), "assets/a1.pngEE", 0x800)
    patched = data.replace(b"assets/a1.pngEE", b"assets/a1.png\xff\xfe")
    assert refused(tmp_path, patched) == {"not_a_zip"}


def test_a_name_that_appears_twice_is_refused(tmp_path: Path) -> None:
    base = zipfile.ZipFile(io.BytesIO(ab.archive()))
    entries = [(info(i.filename), base.read(i)) for i in base.infolist()]
    entries.append((info("maps.json"), b"{}"))
    assert "entry_duplicate" in refused(tmp_path, zip_with(entries))


def test_the_required_files_must_be_there(tmp_path: Path) -> None:
    only_maps = zip_with([(info("maps.json"), ab.dumps(ab.maps_file()))])
    assert "entry_missing" in refused(tmp_path, only_maps)
    only_manifest = zip_with([(info("manifest.json"), b"{}")])
    assert "entry_missing" in refused(tmp_path, only_manifest)


# --- kinds of entry ---------------------------------------------------------------


def test_a_symlink_is_refused(tmp_path: Path) -> None:
    data = with_extra_entry("assets/a1.png", b"/etc/passwd", mode=0o120777)
    assert "entry_special" in refused(tmp_path, data)


@pytest.mark.parametrize(
    "mode",
    [0o020666, 0o060666, 0o010666, 0o140666, 0o040755],
    ids=["chr", "blk", "fifo", "sock", "dir"],
)
def test_special_files_are_refused(tmp_path: Path, mode: int) -> None:
    data = with_extra_entry("assets/a1.png", b"x", mode=mode)
    assert "entry_special" in refused(tmp_path, data)


def test_an_encrypted_entry_is_refused(tmp_path: Path) -> None:
    data = set_flag(with_extra_entry("assets/a1.png"), "assets/a1.png", 0x1)
    assert "entry_encrypted" in refused(tmp_path, data)


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_other_compression_methods_are_refused(tmp_path: Path, method: int) -> None:
    base = zipfile.ZipFile(io.BytesIO(ab.archive()))
    entries = [(info(i.filename), base.read(i)) for i in base.infolist()]
    entries.append((info("assets/a1.png", method=method), b"x" * 100))
    assert "entry_method" in refused(tmp_path, zip_with(entries))


# --- sizes and bombs --------------------------------------------------------------


def test_too_many_entries_are_refused(tmp_path: Path) -> None:
    assets = {f"assets/a{i}.png": b"x" for i in range(1, 60)}
    data = ab.archive(ab.maps_file(), assets)
    assert refused(tmp_path, data) == {"too_many_entries"}


def test_an_entry_over_its_limit_is_refused(tmp_path: Path) -> None:
    big = {"assets/a1.png": b"x" * 1_000_001}
    assert "entry_too_large" in refused(tmp_path, ab.archive(ab.maps_file(), big))


def test_the_total_unpacked_size_is_limited(tmp_path: Path) -> None:
    limits = ReaderLimits(10_000_000, 1_500_000, 50, 1_000_000)
    assets = {f"assets/a{i}.png": bytes([i]) * 700_000 for i in (1, 2, 3)}
    assert "too_large_uncompressed" in refused(
        tmp_path, ab.archive(ab.maps_file(), assets), limits
    )


def test_a_compression_bomb_is_refused_without_inflating_it(tmp_path: Path) -> None:
    """60 MB of zeros deflate to about 60 KB: a ratio no real file has."""
    limits = ReaderLimits(10_000_000, 200_000_000, 50, 100_000_000)
    bomb = {"assets/a1.png": b"\x00" * 60_000_000}
    data = ab.archive(ab.maps_file(), bomb)
    assert len(data) < 200_000
    assert "compression_ratio" in refused(tmp_path, data, limits)


def test_a_header_that_lies_about_the_size_cannot_make_us_read_more(
    tmp_path: Path,
) -> None:
    """The declared size is patched down in both headers: the entry then fails
    its CRC instead of being read past what it declared."""
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps), "assets/a1.png": b"A" * 1000}
    entries = {"manifest.json": ab.dumps(ab.manifest_for(maps, files)), **files}
    data = ab.zip_of(entries, compress=zipfile.ZIP_STORED)
    size = (1000).to_bytes(4, "little")
    assert data.count(size * 2) == 2  # compressed and uncompressed, both headers
    patched = data.replace(size * 2, (10).to_bytes(4, "little") * 2)
    assert refused(tmp_path, patched) <= {"entry_corrupt", "size_mismatch"}


def test_a_local_header_that_disagrees_with_the_directory_is_refused(
    tmp_path: Path,
) -> None:
    data = ab.archive()
    patched = data.replace(b"manifest.json", b"manifest.jsoX", 1)  # the local header
    assert refused(tmp_path, patched) == {"entry_corrupt"}


# --- json -------------------------------------------------------------------------


def archive_with_maps(raw: bytes) -> bytes:
    maps = ab.maps_file()
    files = {"maps.json": raw}
    manifest = ab.manifest_for(maps, files)
    return ab.zip_of({"manifest.json": ab.dumps(manifest), **files})


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{", "json_invalid"),
        (b"", "json_invalid"),
        (b"\xff\xfe{}", "json_invalid"),
        (b'{"a":1,"a":2}', "json_invalid"),
        (b'{"a":NaN}', "json_invalid"),
        (b'{"a":Infinity}', "json_invalid"),
        (b"[" * 40 + b"]" * 40, "json_too_deep"),
        (b"[" * 200_000 + b"]" * 200_000, "json_too_deep"),
        (b'{"a":' * 33 + b"1" + b"}" * 33, "json_too_deep"),
    ],
    ids=[
        "open",
        "empty",
        "utf8",
        "dup",
        "nan",
        "inf",
        "deep",
        "huge-deep",
        "deep-objects",
    ],
)
def test_malformed_and_deeply_nested_json_is_refused(
    tmp_path: Path, raw: bytes, code: str
) -> None:
    path = write(tmp_path, archive_with_maps(raw))
    with (
        open_archive(path, LIMITS) as opened,
        pytest.raises(ArchiveInvalidError) as caught,
    ):
        opened.read_maps()
    assert codes(caught) == {code}


def test_a_huge_integer_is_refused() -> None:
    with pytest.raises(ArchiveInvalidError):
        parse_json(b'{"a":' + b"9" * 5000 + b"}", LIMITS, "maps.json")


def test_brackets_inside_strings_do_not_count_as_nesting() -> None:
    assert json_depth_within(b'{"a":"' + b"[" * 500 + b'"}', 32)
    assert json_depth_within(b'{"a":"\\"[[[[[[["}', 2)
    assert not json_depth_within(b"[[[]]]", 2)
    assert json_depth_within(b"[[[]]]", 3)


# --- the manifest -----------------------------------------------------------------


def test_a_manifest_that_lies_about_a_hash_is_refused(tmp_path: Path) -> None:
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps)}
    manifest = ab.manifest_for(maps, files)
    manifest["files"][0]["sha256"] = "0" * 64
    data = ab.zip_of({"manifest.json": ab.dumps(manifest), **files})
    assert refused(tmp_path, data) == {"hash_mismatch"}


def test_a_manifest_that_lies_about_a_size_is_refused(tmp_path: Path) -> None:
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps)}
    manifest = ab.manifest_for(maps, files)
    manifest["files"][0]["bytes"] += 1
    data = ab.zip_of({"manifest.json": ab.dumps(manifest), **files})
    assert refused(tmp_path, data) == {"size_mismatch"}


def test_a_file_the_manifest_does_not_list_is_refused(tmp_path: Path) -> None:
    assert refused(tmp_path, with_extra_entry("assets/a9.png")) == {"manifest_mismatch"}


def test_a_listed_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    maps = ab.maps_file()
    files = {"maps.json": ab.dumps(maps), "assets/a1.png": b"abc"}
    manifest = ab.manifest_for(maps, files)
    data = ab.zip_of(
        {"manifest.json": ab.dumps(manifest), "maps.json": files["maps.json"]}
    )
    assert refused(tmp_path, data) == {"manifest_mismatch"}


def test_a_changed_asset_is_caught_when_it_is_read_again(tmp_path: Path) -> None:
    png = ab.good_png()
    maps = ab.maps_file(assets=[ab.asset_entry("a1", png)])
    path = write(tmp_path, ab.archive(maps, {"assets/a1.png": png}))
    with open_archive(path, LIMITS) as opened:
        opened._declared["assets/a1.png"] = ("0" * 64, len(png))  # as if swapped
        with pytest.raises(ArchiveInvalidError) as caught:
            opened.read_asset("assets/a1.png")
    assert codes(caught) == {"hash_mismatch"}


@pytest.mark.parametrize(
    "over", [{"formatVersion": 2}, {"minReaderVersion": 2}, {"formatVersion": 0}]
)
def test_an_unsupported_version_is_told_apart_from_a_broken_file(
    tmp_path: Path, over: dict[str, Any]
) -> None:
    data = ab.archive(manifest_over=over)
    with pytest.raises(ArchiveVersionUnsupportedError) as caught:
        open_archive(write(tmp_path, data), LIMITS)
    assert caught.value.supported == 1


def test_a_future_archive_with_new_fields_is_unsupported_not_invalid(
    tmp_path: Path,
) -> None:
    data = ab.archive(
        manifest_over={"formatVersion": 2, "minReaderVersion": 2, "new": 1}
    )
    with pytest.raises(ArchiveVersionUnsupportedError):
        open_archive(write(tmp_path, data), LIMITS)


def test_versions_that_are_not_integers_are_invalid(tmp_path: Path) -> None:
    for value in ("1", 1.0, True, None):
        data = ab.archive(manifest_over={"formatVersion": value})
        assert "invalid_type" in refused(tmp_path, data), value


def test_an_unknown_manifest_field_is_invalid(tmp_path: Path) -> None:
    assert "unknown_field" in refused(tmp_path, ab.archive(manifest_over={"extra": 1}))


def test_a_nested_archive_is_just_bytes_here(tmp_path: Path) -> None:
    """The reader does not open it; validation refuses it as an image."""
    inner = ab.archive()
    maps = ab.maps_file(assets=[ab.asset_entry("a1", inner)])
    path = write(tmp_path, ab.archive(maps, {"assets/a1.png": inner}))
    with open_archive(path, LIMITS) as opened:
        assert opened.read_asset("assets/a1.png") == inner

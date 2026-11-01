"""Builders for `.rumbos` archives in tests, including hostile ones.

Archives are plain dicts and bytes on purpose: attack tests need to make files
the real writer never would.
"""

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from tests import images

EXPORTED_AT = "2026-10-31T12:00:00Z"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def activity_bubble(ref: str = "b1", **over: Any) -> dict[str, Any]:
    bubble: dict[str, Any] = {
        "ref": ref,
        "kind": "activity",
        "x": 0.25,
        "y": 0.5,
        "icon": "star",
        "skin": None,
        "sequence": 0,
        "activity": {
            "modname": "quiz",
            "name": "Final quiz",
            "sectionNumber": 2,
            "sectionName": "Unit 2",
            "positionInSection": 1,
            "originalCmid": 41,
        },
    }
    bubble.update(over)
    return bubble


def blocks_content(**over: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "type": "blocks",
        "title": "Read me",
        "autoOpen": "never",
        "body": {
            "schemaVersion": 1,
            "blocks": [
                {"type": "heading", "text": "Hello", "level": 2},
                {"type": "paragraph", "text": "A <b>plain</b> text."},
                {"type": "link", "text": "Docs", "url": "https://example.com/docs"},
            ],
        },
    }
    content.update(over)
    return content


def widget_content(**over: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "type": "widget",
        "title": "Toy",
        "autoOpen": "never",
        "altText": "A small toy",
        "html": "<p>hi</p>",
    }
    content.update(over)
    return content


def content_bubble(
    ref: str = "b2", content: dict[str, Any] | None = None, **over: Any
) -> dict[str, Any]:
    bubble: dict[str, Any] = {
        "ref": ref,
        "kind": "content",
        "x": 0.75,
        "y": 0.5,
        "icon": None,
        "skin": None,
        "sequence": 1,
        "content": content if content is not None else blocks_content(),
    }
    bubble.update(over)
    return bubble


def archive_map(ref: str = "m1", **over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "ref": ref,
        "title": "Unit 2",
        "position": 0,
        "settings": {},
        "background": {"imageUrl": "https://example.com/map.png"},
        "defaultSkin": None,
        "skinRules": [],
        "bubbles": [activity_bubble(), content_bubble()],
        "view3d": None,
    }
    value.update(over)
    return value


def asset_entry(ref: str, data: bytes, **over: Any) -> dict[str, Any]:
    """The `assets` entry of a PNG background built from `data`."""
    entry: dict[str, Any] = {
        "ref": ref,
        "kind": "background",
        "file": f"assets/{ref}.png",
        "mime": "image/png",
        "sha256": sha(data),
        "bytes": len(data),
        "width": 32,
        "height": 24,
        "title": "Background",
    }
    entry.update(over)
    return entry


def maps_file(**over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"maps": [archive_map()], "skins": [], "assets": []}
    value.update(over)
    return value


def count_bubbles(maps: Mapping[str, Any]) -> int:
    return sum(len(m["bubbles"]) for m in maps["maps"])


def manifest_for(
    maps: Mapping[str, Any], files: Mapping[str, bytes], **over: Any
) -> dict[str, Any]:
    """A manifest that tells the truth about `files` (maps.json included)."""
    listed = [
        {"path": path, "sha256": sha(data), "bytes": len(data)}
        for path, data in sorted(files.items())
    ]
    value: dict[str, Any] = {
        "formatVersion": 1,
        "minReaderVersion": 1,
        "exportedAt": EXPORTED_AT,
        "generator": "rumbos",
        "files": listed,
        "counts": {
            "maps": len(maps["maps"]),
            "bubbles": count_bubbles(maps),
            "assets": sum(p.startswith("assets/") for p in files),
        },
    }
    value.update(over)
    return value


def zip_of(
    entries: Mapping[str, bytes],
    *,
    compress: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """A ZIP with exactly these entries, in this order (names are not checked)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compress) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.external_attr = 0o600 << 16
            zf.writestr(info, data, compress_type=compress)
    return out.getvalue()


def archive(
    maps: Mapping[str, Any] | None = None,
    assets: Mapping[str, bytes] | None = None,
    *,
    manifest_over: Mapping[str, Any] | None = None,
    extra: Mapping[str, bytes] | None = None,
) -> bytes:
    """A consistent archive: the manifest lists, correctly, `maps.json` and
    `assets` (a dict of `assets/<ref>.<ext>` -> bytes)."""
    maps = deepcopy(dict(maps)) if maps is not None else maps_file()
    files = {"maps.json": dumps(maps), **(assets or {})}
    manifest = manifest_for(maps, files, **(manifest_over or {}))
    entries = {"manifest.json": dumps(manifest), **files, **(extra or {})}
    return zip_of(entries)


def good_png(color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    return images.png((32, 24), color=color)

"""Audio of the trainer and the exam, served from the content packs.

Only audio that belongs to something the caller's course may use is served
(the exam or the practice activities, drafts only when the server allows
them). Files are immutable per `content_version`, so the URL carries it
(`?v=`) and the response is cacheable for a year; the browser still needs
`Range` for seeking, so single byte ranges are honoured (206).
"""

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import FileResponse

from app.assessment.access import CourseSettingsDep, rate_limited
from app.auth.dependencies import student_route
from app.core.config import Settings, get_settings
from app.exceptions import TrainerNotFoundError
from app.trainer.catalog import Catalog
from app.trainer.dependencies import CatalogDep
from app.trainer.pack import Pack

router = APIRouter(
    prefix="/trainer",
    tags=["trainer"],
    dependencies=[Depends(student_route), Depends(rate_limited)],
)

TYPES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _usable(catalog: Catalog, audio_id: str, drafts: bool) -> tuple[Pack, Path] | None:
    for pack in catalog.packs.values():
        entry = pack.audio.get(audio_id)
        if entry is None:
            continue
        published = pack.meta.status == "published"
        if not (published or drafts):
            continue
        path = (pack.root / entry.file).resolve()
        if not path.is_relative_to(pack.root.resolve()) or not path.is_file():
            return None
        return pack, path
    return None


def _slice(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """The byte range asked for: (start, end) inclusive, None for no Range
    header, False for one that cannot be satisfied."""
    if not header:
        return None
    m = _RANGE.match(header.strip())
    if not m or (m[1] == "" and m[2] == ""):
        return None  # not a single byte range: serve the whole file
    if m[1] == "":
        length = int(m[2])
        if length == 0:
            return False
        return max(size - length, 0), size - 1
    start = int(m[1])
    end = min(int(m[2]), size - 1) if m[2] else size - 1
    if start >= size or end < start:
        return False
    return start, end


@router.get("/audio/{audio_id}")
async def get_audio(
    audio_id: str,
    catalog: CatalogDep,
    course: CourseSettingsDep,
    app_settings: Annotated[Settings, Depends(get_settings)],
    range_: Annotated[str | None, Header(alias="Range")] = None,
    v: str | None = None,
) -> Response:
    if not (course.assessments.enabled or course.trainer.enabled):
        raise TrainerNotFoundError
    found = _usable(catalog, audio_id, app_settings.trainer_serve_drafts)
    if found is None:
        raise TrainerNotFoundError
    pack, path = found
    media = TYPES.get(path.suffix.lower(), "application/octet-stream")
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{pack.content_version}-{audio_id}"',
        "Cache-Control": (
            "private, max-age=31536000, immutable"
            if v == pack.content_version
            else "private, no-cache"
        ),
    }
    size = path.stat().st_size
    wanted = _slice(range_, size)
    if wanted is None:
        return FileResponse(path, media_type=media, headers=headers)
    if wanted is False:
        return Response(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )
    assert isinstance(wanted, tuple)
    start, end = wanted
    with path.open("rb") as f:
        f.seek(start)
        body = f.read(end - start + 1)
    return Response(
        body,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"},
    )

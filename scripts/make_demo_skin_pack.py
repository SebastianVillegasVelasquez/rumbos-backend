"""Generates an ORIGINAL demo skin pack: six 256x256 transparent PNGs.

Test-fixture material for exercising the upload and image-skin pipeline
(`POST /assets` with kind=bubble, then `POST /skins` with an image config).
It is NOT production art; real skin art is a separate deliverable.

All six states are drawn on the same artboard with the token in the same place
and size, because an image skin requires identical dimensions and different
sizes would make the states "jump" when they switch.

Usage:  uv run python scripts/make_demo_skin_pack.py [OUT_DIR]
        (default OUT_DIR: tmp/demo-skin-pack)

Only Pillow is needed.
"""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
SUPERSAMPLE = 4  # drawn at 4x and shrunk, for smooth edges
CENTER = SIZE // 2
RADIUS = 84  # the token; the rest of the artboard is room for glows

Color = tuple[int, int, int]

PALETTE: dict[str, tuple[Color, Color]] = {
    # name: (fill, rim)
    "available": ((255, 183, 3), (224, 142, 0)),
    "locked": ((138, 148, 166), (91, 101, 119)),
    "next": ((255, 183, 3), (14, 154, 167)),
    "inProgress": ((14, 154, 167), (8, 118, 128)),
    "complete": ((47, 191, 113), (30, 142, 82)),
    "hover": ((255, 205, 70), (224, 142, 0)),
}
WHITE: Color = (255, 255, 255)


def _scaled(value: float) -> float:
    return value * SUPERSAMPLE


def _box(radius: float) -> tuple[float, float, float, float]:
    c = _scaled(CENTER)
    r = _scaled(radius)
    return (c - r, c - r, c + r, c + r)


def _blank() -> Image.Image:
    return Image.new("RGBA", (SIZE * SUPERSAMPLE,) * 2, (0, 0, 0, 0))


def _glow(color: Color, radius: float, blur: float, alpha: int) -> Image.Image:
    layer = _blank()
    ImageDraw.Draw(layer).ellipse(_box(radius), fill=(*color, alpha))
    return layer.filter(ImageFilter.GaussianBlur(_scaled(blur)))


def _token(fill: Color, rim: Color) -> Image.Image:
    """The disc: a soft shadow, a rim, the body and a glossy highlight."""
    layer = _blank()
    draw = ImageDraw.Draw(layer)
    shadow = _blank()
    c = _scaled(CENTER)
    r, drop = _scaled(RADIUS), _scaled(8)
    ImageDraw.Draw(shadow).ellipse(
        (c - r, c - r + drop, c + r, c + r + drop), fill=(0, 0, 0, 70)
    )
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(_scaled(6))))
    draw.ellipse(_box(RADIUS), fill=(*rim, 255))
    draw.ellipse(_box(RADIUS - 8), fill=(*fill, 255))
    shine = _blank()
    ImageDraw.Draw(shine).ellipse(
        (c - _scaled(50), c - _scaled(66), c + _scaled(30), c - _scaled(14)),
        fill=(255, 255, 255, 90),
    )
    layer.alpha_composite(shine.filter(ImageFilter.GaussianBlur(_scaled(3))))
    return layer


def _line(
    draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], width: float
) -> None:
    scaled = [(_scaled(x), _scaled(y)) for x, y in points]
    draw.line(scaled, fill=(*WHITE, 255), width=round(_scaled(width)), joint="curve")
    for x, y in scaled:  # round caps
        r = _scaled(width) / 2
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*WHITE, 255))


def _padlock(layer: Image.Image) -> None:
    draw = ImageDraw.Draw(layer)
    c = CENTER
    body = (c - 26, c - 2, c + 26, c + 40)
    draw.rounded_rectangle(
        [_scaled(v) for v in body], radius=_scaled(7), fill=(*WHITE, 255)
    )
    shackle = (c - 17, c - 36, c + 17, c + 2)
    draw.arc(
        [_scaled(v) for v in shackle],
        180,
        360,
        fill=(*WHITE, 255),
        width=round(_scaled(8)),
    )
    draw.line(
        [(_scaled(c - 17), _scaled(c - 17)), (_scaled(c - 17), _scaled(c + 2))],
        fill=(*WHITE, 255),
        width=round(_scaled(8)),
    )
    draw.line(
        [(_scaled(c + 17), _scaled(c - 17)), (_scaled(c + 17), _scaled(c + 2))],
        fill=(*WHITE, 255),
        width=round(_scaled(8)),
    )
    keyhole = (c - 5, c + 8, c + 5, c + 18)
    draw.ellipse([_scaled(v) for v in keyhole], fill=(91, 101, 119, 255))
    draw.rectangle(
        [_scaled(c - 2.5), _scaled(c + 14), _scaled(c + 2.5), _scaled(c + 28)],
        fill=(91, 101, 119, 255),
    )


def _check(layer: Image.Image) -> None:
    c = CENTER
    _line(
        ImageDraw.Draw(layer), [(c - 30, c + 2), (c - 8, c + 24), (c + 32, c - 24)], 14
    )


def _play(layer: Image.Image) -> None:
    c = CENTER
    triangle = [(c - 16, c - 28), (c - 16, c + 28), (c + 30, c)]
    ImageDraw.Draw(layer).polygon(
        [(_scaled(x), _scaled(y)) for x, y in triangle], fill=(*WHITE, 255)
    )


def _star(layer: Image.Image) -> None:
    c = CENTER
    points = []
    for i in range(10):
        radius = 34 if i % 2 == 0 else 14
        angle = math.radians(-90 + i * 36)
        points.append(
            (
                _scaled(c + radius * math.cos(angle)),
                _scaled(c + radius * math.sin(angle) + 3),
            )
        )
    ImageDraw.Draw(layer).polygon(points, fill=(*WHITE, 255))


def _progress_ring(layer: Image.Image, fraction: float) -> None:
    draw = ImageDraw.Draw(layer)
    outer = RADIUS + 14
    draw.ellipse(_box(outer), outline=(255, 255, 255, 70), width=round(_scaled(8)))
    draw.arc(
        _box(outer),
        -90,
        -90 + 360 * fraction,
        fill=(94, 211, 221, 255),
        width=round(_scaled(8)),
    )


def render(state: str) -> Image.Image:
    fill, rim = PALETTE[state]
    image = _blank()
    if state == "next":
        # "This is where to go next": a bright halo around an available token.
        image.alpha_composite(_glow((94, 211, 221), RADIUS + 26, 8, 230))
    if state == "hover":
        image.alpha_composite(_glow((255, 216, 102), RADIUS + 24, 9, 210))
    image.alpha_composite(_token(fill, rim))
    if state == "inProgress":
        _progress_ring(image, 0.62)
    glyphs = {
        "available": _star,
        "hover": _star,
        "next": _play,
        "locked": _padlock,
        "complete": _check,
    }
    if state in glyphs:
        glyphs[state](image)
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for state in PALETTE:
        path = out_dir / f"{state}.png"
        render(state).save(path, "PNG", optimize=True)
        print(f"wrote {path}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tmp/demo-skin-pack"))

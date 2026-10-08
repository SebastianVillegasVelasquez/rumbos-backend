"""Valid skin configs (camelCase, as on the wire) for tests to copy and break."""

import copy
import uuid
from typing import Any

PALETTE: dict[str, dict[str, str]] = {
    "locked": {"fill": "#8A94A6", "accent": "#5B6577", "glow": "#B4BCCB"},
    "available": {"fill": "#FFB703", "accent": "#E08E00", "glow": "#FFD866"},
    "inProgress": {"fill": "#0E9AA7", "accent": "#087680", "glow": "#5ED3DD"},
    "complete": {"fill": "#2FBF71", "accent": "#1E8E52", "glow": "#7DE3A8"},
}


def procedural(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "procedural",
        "shape": "circle",
        "size": 56,
        "palette": copy.deepcopy(PALETTE),
        "icon": {"mode": "auto", "preset": None, "color": "auto"},
        "label": {"mode": "hover"},
        "effects": {
            "idle": "breathe",
            "ring": True,
            "glow": True,
            "completion": "burst",
        },
    }
    config.update(overrides)
    return config


def image(
    available: uuid.UUID | str, **states: uuid.UUID | str | None
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "kind": "image",
        "size": 96,
        "anchor": "bottom",
        "states": {
            "available": str(available),
            **{k: (str(v) if v else None) for k, v in states.items()},
        },
        "label": {"mode": "always"},
        "effects": {"idle": "float", "completion": "ripple"},
    }

"""Writes, or checks, the exported OpenAPI document of the API.

    python -m app.scripts.export_openapi            # rewrite openapi.json
    python -m app.scripts.export_openapi --check    # exit 1 if it is stale

`openapi.json` is what the frontend builds its client from, so it must say what
the code says. CI runs `--check`: a route or schema changed without exporting
fails the build. The document is built by the application itself, with no
database or Moodle involved.
"""

import json
import sys
from pathlib import Path

from app.main import create_app

DEFAULT_PATH = Path("openapi.json")


def render() -> str:
    """The document exactly as it is kept in the repository."""
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check = "--check" in args
    paths = [a for a in args if not a.startswith("--")]
    target = Path(paths[0]) if paths else DEFAULT_PATH
    fresh = render()
    if check:
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == fresh:
            print(f"{target} is up to date")
            return 0
        print(
            f"{target} differs from the API. Run: python -m app.scripts.export_openapi"
        )
        return 1
    target.write_text(fresh, encoding="utf-8", newline="\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

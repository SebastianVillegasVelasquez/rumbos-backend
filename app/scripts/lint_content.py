"""Lints the content packs.

    python -m app.scripts.lint_content [content/trainer]

Exits 1 when a pack fails to load or has errors (CI runs it). It reads only
the content: no database or Moodle settings are needed, so the directory comes
from the argument, then `TRAINER_CONTENT_DIR`, then `content/trainer`.
"""

import os
import sys
from pathlib import Path

from app.trainer.catalog import load_catalog
from app.trainer.lint import lint
from app.trainer.pack import PackError


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = Path(
        args[0] if args else os.environ.get("TRAINER_CONTENT_DIR", "content/trainer")
    )
    try:
        catalog = load_catalog(base)
    except PackError as exc:
        print("The pack does not load:")
        for problem in exc.problems:
            print(f"  {problem}")
        return 1
    failed = False
    for (language, level), pack in sorted(catalog.packs.items()):
        report = lint(pack)
        print(f"== {language}/{level} (version {pack.content_version}) ==")
        print(report.render())
        failed |= not report.ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

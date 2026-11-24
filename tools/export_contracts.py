"""Rewrites (or with --check, verifies) the files of contracts/ that are built
from the code: the JSON Schemas, the example payloads and the measured sizes.

    python tools/export_contracts.py
    python tools/export_contracts.py --check
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import contract_examples

ROOT = Path(__file__).resolve().parent.parent / "contracts"


def wanted() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for name, schema in contract_examples.schemas().items():
        files[ROOT / "schemas" / f"{name}.schema.json"] = json.dumps(
            schema, indent=2, ensure_ascii=False
        )
    for name, payload in contract_examples.examples().items():
        files[ROOT / "examples" / f"{name}.json"] = json.dumps(
            payload, indent=2, ensure_ascii=False
        )
    files[ROOT / "sizes.json"] = json.dumps(contract_examples.sizes(), indent=2)
    return {path: text + "\n" for path, text in files.items()}


def main() -> int:
    files = wanted()
    if "--check" in sys.argv:
        stale = [
            p.name
            for p, text in files.items()
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        if stale:
            print("out of date:", ", ".join(stale))
            return 1
        print("contracts are up to date")
        return 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(files)} files under {ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

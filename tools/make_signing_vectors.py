"""Rewrites (or with --check, verifies) contracts/signing-vectors/.

python tools/make_signing_vectors.py
python tools/make_signing_vectors.py --check
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import signing_vectors


def main() -> int:
    if "--check" in sys.argv:
        for name, payload in signing_vectors.claims().items():
            kept = json.loads(
                (signing_vectors.DIRECTORY / f"{name}.json").read_text(encoding="utf-8")
            )["claims"]
            if kept != json.loads(json.dumps(payload)):
                print(f"{name}: the claims drifted; rerun without --check")
                return 1
        print("signing vectors are up to date")
        return 0
    signing_vectors.write()
    print(f"wrote {signing_vectors.DIRECTORY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

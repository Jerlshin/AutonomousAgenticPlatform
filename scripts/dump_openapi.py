#!/usr/bin/env python3
"""Write `backend/openapi.json` without starting a server.

`openapi-typescript` can read a URL, but requiring a live API turns type generation into a
task that only works on a fully composed stack — it cannot run in CI, in a clean clone, or
in a pre-commit hook. `app.openapi()` is the same document the server would serve.

`make openapi` writes the file; `make openapi-check` fails when it has drifted, which is
what turns a renamed response field into a red build rather than an `undefined` rendered
into a table cell (docs/FRONTEND.md §4.2, §4.4).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.main import app  # noqa: E402

TARGET = REPO_ROOT / "backend" / "openapi.json"


def main() -> int:
    # `sort_keys` because the dict order FastAPI builds the document in is not stable
    # across Python versions or route registration order, and an unstable document turns
    # the drift gate into noise that people learn to ignore.
    document = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != document:
            print("backend/openapi.json is stale — run `make openapi`", file=sys.stderr)
            return 1
        print(f"{TARGET.relative_to(REPO_ROOT)} is up to date.")
        return 0
    TARGET.write_text(document, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

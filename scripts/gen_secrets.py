#!/usr/bin/env python3
"""Fill an .env file with freshly generated secrets.

Rewrites the value of every key in SECRET_KEYS, appending the key if it is not
already present. Used by `make init-secrets`; safe to re-run against a fresh
copy of .env.example, never against a populated .env (the Makefile guards that).
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

SECRET_KEYS = (
    "PLATFORM_API_TOKEN",
    "SECRET_KEY",
    "POSTGRES_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
)


def main(path: Path) -> int:
    if not path.exists():
        print(f"error: {path} does not exist", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    for key in SECRET_KEYS:
        value = secrets.token_urlsafe(32)
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, f"{key}={value}", text, flags=re.MULTILINE)
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += f"{key}={value}\n"

    path.write_text(text, encoding="utf-8")
    print(f"generated {len(SECRET_KEYS)} secrets in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".env")))

#!/usr/bin/env python3
"""Fail if a generated secret has leaked into a tracked file (ARCHITECTURE.md §13.3).

`.env` is git-ignored and holds the API token, the Postgres password, the signing key and
the Grafana admin password. This greps every *tracked* file for those exact values, which
catches the realistic accident: a token pasted into a README while debugging, a curl
example committed with a working `Authorization` header, a fixture built from the live
`.env`.

**It is a leak detector, not a secret scanner.** It looks only for the values this
machine's `.env` currently holds — it cannot see a secret from someone else's box, or one
already rotated. Its value is that it runs before the commit, on the machine where the
secret is live, which is where the mistake is actually made.

Exit codes: 0 clean (or no `.env` to compare against), 1 a leak was found.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The keys `scripts/gen_secrets.py` generates. Anything else in .env — a hostname, a model
# name, a port — is configuration, not a secret, and searching for it would produce
# nothing but false positives.
SECRET_KEYS = (
    "PLATFORM_API_TOKEN",
    "SECRET_KEY",
    "POSTGRES_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
)

# Values below this length, or on this list, are placeholders that appear all over the
# tree by design — `.env.example` ships them, the compose file defaults to them, and the
# tests assert on them. Flagging those would make the check noise and get it switched off.
MIN_SECRET_LENGTH = 16
PLACEHOLDERS = frozenset(
    {
        "dev_secret_key_change_in_production",
        "postgres_password_dev",
        "changeme",
        "admin",
    }
)


def load_secrets(env_path: Path) -> dict[str, str]:
    """The current values of the generated keys, from an un-tracked `.env`."""
    if not env_path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if (
            key in SECRET_KEYS
            and len(value) >= MIN_SECRET_LENGTH
            and value not in PLACEHOLDERS
        ):
            values[key] = value
    return values


def tracked_files() -> list[Path]:
    """Every file git knows about. Untracked scratch is not a leak — committing is."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"error: could not list tracked files: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    return [REPO_ROOT / name for name in out.split("\0") if name]


def scan(paths: list[Path], secrets: dict[str, str]) -> list[tuple[Path, int, str]]:
    """Every (file, line number, key) where a live secret appears."""
    findings: list[tuple[Path, int, str]] = []
    for path in paths:
        try:
            # Binary files (images, the odd fixture) decode to nothing useful; a secret
            # cannot be "pasted into" one by the accident this check is aimed at.
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for key, value in secrets.items():
                if value in line:
                    findings.append((path, number, key))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env",
        type=Path,
        default=REPO_ROOT / ".env",
        help="the .env holding the live secrets (default: the repository root's)",
    )
    args = parser.parse_args()

    secrets = load_secrets(args.env)
    if not secrets:
        # A clean clone or a CI runner has no .env, so there is nothing to leak. Reported
        # rather than silently passing: "0 findings" and "nothing was searched for" are
        # different results and only one of them is reassuring.
        print(
            f"no generated secrets found in {args.env.name}; nothing to check "
            "(run `make init-secrets` on a machine that should have them)"
        )
        return 0

    findings = scan(tracked_files(), secrets)
    if findings:
        print("error: live secrets found in tracked files:\n", file=sys.stderr)
        for path, number, key in findings:
            location = path.relative_to(REPO_ROOT)
            print(f"  {location}:{number}: value of {key}", file=sys.stderr)
        print(
            "\nRemove the value, then rotate it — a secret that reached the working tree "
            "must be assumed compromised. `rm .env && make init-secrets` regenerates all "
            "four.",
            file=sys.stderr,
        )
        return 1

    print(
        f"no leaks: {len(secrets)} secret(s) checked against "
        f"{len(tracked_files())} tracked files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

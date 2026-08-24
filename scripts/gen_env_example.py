#!/usr/bin/env python3
"""Generate .env.example from backend/app/core/config.py.

`Settings` is the single source of truth for configuration; this script renders it as a
commented template so `.env.example` cannot drift from the fields the application
actually reads (defect D-012). `make gen-env-example` writes the file, and
`make check-env-example` (also run in CI) fails if the checked-in copy differs.

Values written are the field defaults, which are the host-development values. Where a
service inside `platform_net` needs a different value, the field records it as
`in_network` metadata and it is emitted as a comment above the variable.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import GROUPS, Settings  # noqa: E402

HEADER = """\
# ==============================================================================
#  Pluton R&D Engine — environment template
#
#  GENERATED FILE — do not edit by hand.
#  Regenerate with `make gen-env-example` after changing backend/app/core/config.py.
#
#  Copy to .env with `make init-secrets`, which also fills the secret values.
#  Every variable below is a field on `Settings`; anything not listed here is not
#  read by the application. Reference: docs/ARCHITECTURE.md §14.
#
#  Values are the host-development defaults — `make migrate` and `make dev` run
#  natively on the host. Lines marked "in-network" show the value used by services
#  running inside the compose network, which docker-compose.yml injects itself.
# ==============================================================================
"""


def render_value(value: Any) -> str:
    """Render a field default the way python-dotenv will read it back."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def wrap(text: str, width: int = 76) -> list[str]:
    """Wrap a doc string into comment-width lines."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def render() -> str:
    out: list[str] = [HEADER]

    for group in GROUPS:
        fields = [
            (name, field)
            for name, field in Settings.model_fields.items()
            if (field.json_schema_extra or {}).get("group") == group
        ]
        if not fields:
            continue

        out.append(f"# ------------------------------------------------------------------")
        out.append(f"#  {group}")
        out.append(f"# ------------------------------------------------------------------")

        for name, field in fields:
            extra: dict[str, Any] = dict(field.json_schema_extra or {})
            for line in wrap(str(extra.get("doc", "")).strip()):
                out.append(f"# {line}")
            if "in_network" in extra:
                out.append(f"# in-network (docker compose): {extra['in_network']}")
            if extra.get("secret"):
                out.append("# secret — `make init-secrets` replaces this value")
            out.append(f"{name}={render_value(field.get_default())}")
            out.append("")

    # Not a Settings field: consumed by the observability profile's Grafana container,
    # but generated as a secret by scripts/gen_secrets.py, so it belongs in the template.
    out.append("# ------------------------------------------------------------------")
    out.append("#  Observability profile (not read by the backend)")
    out.append("# ------------------------------------------------------------------")
    out.append("# Grafana admin password, used only by `make up PROFILE=observability`.")
    out.append("# secret — `make init-secrets` replaces this value")
    out.append("GRAFANA_ADMIN_PASSWORD=")
    out.append("")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the checked-in .env.example differs from the generated one",
    )
    parser.add_argument("--path", type=Path, default=REPO_ROOT / ".env.example")
    args = parser.parse_args()

    generated = render()

    if args.check:
        current = args.path.read_text(encoding="utf-8") if args.path.exists() else ""
        if current != generated:
            diff = difflib.unified_diff(
                current.splitlines(),
                generated.splitlines(),
                fromfile=f"{args.path.name} (checked in)",
                tofile=f"{args.path.name} (generated)",
                lineterm="",
            )
            print("\n".join(diff))
            print(
                f"\nerror: {args.path.name} is out of date. Run `make gen-env-example`.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.path.name} is in sync with Settings")
        return 0

    args.path.write_text(generated, encoding="utf-8")
    field_count = sum(
        1
        for field in Settings.model_fields.values()
        if (field.json_schema_extra or {}).get("group")
    )
    print(f"wrote {args.path} ({field_count} settings fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

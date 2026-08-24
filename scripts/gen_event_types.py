#!/usr/bin/env python3
"""Generate the frontend's WebSocket event types from `app/schemas/events.py`.

ARCHITECTURE.md §18.5: "WebSocket event types are generated from the same Pydantic event
models via a JSON Schema export, so a backend event field rename is a frontend compile
error rather than a runtime `undefined`."

Generating from the Python enum rather than from a running server is what makes this
runnable in CI and in a clean clone: `openapi-typescript` (`make fe-types`) needs a live
`/openapi.json`, and the WebSocket protocol does not appear in OpenAPI at all.

`make gen-event-types` writes `frontend/src/lib/events.generated.ts`;
`make check-event-types` fails if the checked-in copy has drifted, which is what turns a
renamed event into a red build instead of a silent mismatch.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.schemas.events import (  # noqa: E402
    CONTROL_EVENTS,
    PROTOCOL,
    PROTOCOL_VERSION,
    TERMINAL_EVENTS,
    ClientMessageType,
    CloseCode,
    EventType,
)

TARGET = REPO_ROOT / "frontend" / "src" / "lib" / "events.generated.ts"

HEADER = f"""\
// GENERATED FILE — DO NOT EDIT.
//
// Source: backend/app/schemas/events.py
// Regenerate: make gen-event-types
//
// The `pluton.v1` wire contract (docs/ARCHITECTURE.md §9). Every name below is the
// backend's own, so renaming an event there and forgetting the frontend is a TypeScript
// error here rather than an `undefined` at runtime (§18.5).

export const PROTOCOL = "{PROTOCOL}" as const;
export const PROTOCOL_VERSION = {PROTOCOL_VERSION} as const;
"""


def _union(name: str, values: list[str], doc: str) -> str:
    members = "\n".join(f'  | "{value}"' for value in values)
    return f"\n/** {doc} */\nexport type {name} =\n{members};\n"


def _const_array(name: str, values: list[str], doc: str, element: str) -> str:
    members = "\n".join(f'  "{value}",' for value in values)
    return (
        f"\n/** {doc} */\nexport const {name}: readonly {element}[] = [\n{members}\n] as const;\n"
    )


def render() -> str:
    event_values = [member.value for member in EventType]
    control = sorted(member.value for member in CONTROL_EVENTS)
    terminal = sorted(member.value for member in TERMINAL_EVENTS)
    client = [member.value for member in ClientMessageType]

    parts = [
        HEADER,
        _union("RunEventType", event_values, "Every server→client event type (§9.4)."),
        _union("ClientMessageType", client, "Every client→server message type (§9.5)."),
        _const_array(
            "CONTROL_EVENTS",
            control,
            "Frames describing the connection rather than the run. They carry `seq: 0` and are never replayed.",
            "RunEventType",
        ),
        _const_array(
            "TERMINAL_EVENTS",
            terminal,
            "After one of these the run is over and the reconnect loop stops (§9.8).",
            "RunEventType",
        ),
        _close_codes(),
        ENVELOPE,
    ]
    return "".join(parts)


def _close_codes() -> str:
    rows = "\n".join(
        f"  {name}: {value},"
        for name, value in vars(CloseCode).items()
        if not name.startswith("_") and isinstance(value, int)
    )
    return (
        "\n/** WebSocket close codes (§9.7). The client's reconnect policy keys off these. */\n"
        f"export const CloseCode = {{\n{rows}\n}} as const;\n"
    )


ENVELOPE = """
/** The §9.2 envelope every server→client message travels in. */
export interface RunEvent<P = Record<string, unknown>> {
  v: typeof PROTOCOL_VERSION;
  /** Gapless and strictly increasing per run, from 1. The resume cursor. Control frames carry 0. */
  seq: number;
  run_id: string;
  /** RFC 3339 UTC, millisecond precision. */
  ts: string;
  type: RunEventType;
  payload: P;
}

/** Sent to the server; see §9.5. */
export interface ClientMessage<P = Record<string, unknown>> {
  type: ClientMessageType;
  payload?: P;
}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the checked-in file differs from what would be generated.",
    )
    args = parser.parse_args()

    generated = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.is_file() else ""
        if current == generated:
            print(f"{TARGET.relative_to(REPO_ROOT)} is up to date.")
            return 0
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                generated.splitlines(keepends=True),
                fromfile="checked in",
                tofile="generated",
            )
        )
        print(
            f"\n{TARGET.relative_to(REPO_ROOT)} has drifted from "
            "backend/app/schemas/events.py. Run `make gen-event-types`.",
            file=sys.stderr,
        )
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(generated, encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

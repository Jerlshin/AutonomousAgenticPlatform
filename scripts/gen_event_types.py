#!/usr/bin/env python3
"""Generate the frontend's WebSocket event types from `app/schemas/events.py`.

ARCHITECTURE.md §18.5: "WebSocket event types are generated from the same Pydantic event
models via a JSON Schema export, so a backend event field rename is a frontend compile
error rather than a runtime `undefined`."

Generating from the Python models rather than from a running server is what makes this
runnable in CI and in a clean clone: `openapi-typescript` (`make fe-types`) needs an
OpenAPI document, and the WebSocket protocol does not appear in OpenAPI at all.

Two halves are emitted (docs/FRONTEND.md §4.3):

* the **enums** — event names, client message names, control and terminal sets, close
  codes — read straight off the `StrEnum`s;
* the **payloads** — one TypeScript interface per model in `PAYLOADS`, plus the
  discriminated `RunEvent` union that binds each event name to its payload. That union is
  what turns the store's fold into an exhaustive `switch` in which `event.payload.node` is
  a `string`, and a field renamed in `events.py` into a compile error at the case that
  reads it rather than an `undefined` rendered into a pane.

`run.snapshot` is the one payload not generated from a model: it carries the full
`GET /runs/{id}` body, whose authority is `RunRead` in the OpenAPI document, so the
generated file aliases the type `rest.ts` already unpacks instead of re-declaring
thirty fields that would then have two sources of truth.

`make gen-event-types` writes `frontend/src/lib/events.generated.ts` and
`backend/app/schemas/events.schema.json`; `make check-event-types` fails if either has
drifted, which is what turns a renamed event into a red build instead of a silent
mismatch.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from pydantic import BaseModel  # noqa: E402

from app.schemas.events import (  # noqa: E402
    CONTROL_EVENTS,
    PAYLOADS,
    PROTOCOL,
    PROTOCOL_VERSION,
    TERMINAL_EVENTS,
    ClientMessageType,
    CloseCode,
    EventEnvelope,
    EventType,
)

TARGET = REPO_ROOT / "frontend" / "src" / "lib" / "events.generated.ts"
SCHEMA_TARGET = REPO_ROOT / "backend" / "app" / "schemas" / "events.schema.json"

#: `run.snapshot` aliases the REST body rather than declaring its own interface. See the
#: module docstring; the value is the name `rest.ts` exports.
REST_ALIASES: dict[str, str] = {"RunSnapshotPayload": "RunRead"}

HEADER = f"""\
// GENERATED FILE — DO NOT EDIT.
//
// Source: backend/app/schemas/events.py
// Regenerate: make gen-event-types
//
// The `pluton.v1` wire contract (docs/ARCHITECTURE.md §9, docs/FRONTEND.md §4.3). Every
// name below is the backend's own, so renaming an event or a payload field there and
// forgetting the frontend is a TypeScript error here rather than an `undefined` at
// runtime (§18.5).

import type {{ RunRead }} from "./rest";

export const PROTOCOL = "{PROTOCOL}" as const;
export const PROTOCOL_VERSION = {PROTOCOL_VERSION} as const;
"""


# ------------------------------------------------------------------------------------
#  JSON Schema → TypeScript
# ------------------------------------------------------------------------------------


def ts_type(schema: dict[str, Any]) -> str:
    """One JSON Schema node as a TypeScript type expression.

    Deliberately narrow: it handles what Pydantic emits for the models in `PAYLOADS` and
    raises on anything else. A generator that silently degrades an unrecognised shape to
    `unknown` would hide exactly the drift this pipeline exists to catch.
    """
    if not schema:
        return "unknown"

    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]

    if "anyOf" in schema:
        members = [ts_type(member) for member in schema["anyOf"]]
        # De-duplicate while keeping order: `str | None` and `Optional[str]` both produce
        # `string | null`, and a union that repeats a member reads like a mistake.
        seen: list[str] = []
        for member in members:
            if member not in seen:
                seen.append(member)
        return " | ".join(seen)

    if "const" in schema:
        return json.dumps(schema["const"])

    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])

    kind = schema.get("type")
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        item = ts_type(schema.get("items") or {})
        # `A | B[]` parses as `A | (B[])`, so a union item needs parentheses.
        return f"({item})[]" if " | " in item else f"{item}[]"
    if kind == "object":
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {ts_type(extra)}>"
        return "Record<string, unknown>"

    raise ValueError(f"unsupported JSON Schema node: {schema!r}")


def doc_comment(schema: dict[str, Any], indent: str) -> str:
    """A TSDoc line for a field or interface, or nothing when there is no description."""
    text = (schema.get("description") or "").strip().replace("\n", " ")
    if not text:
        return ""
    return f"{indent}/** {text} */\n"


def render_interface(name: str, schema: dict[str, Any]) -> str:
    """One `export interface`, fields in declaration order.

    Every field is emitted required. A field with a Python default still travels on the
    wire when the emitter constructs the model, and marking defaults optional would put
    `| undefined` on almost every field in the file — which reintroduces the defensive
    `?? ""` coercions §4.3 exists to delete.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    body = "".join(
        f"{doc_comment(field, '  ')}  {key}: {ts_type(field)};\n"
        for key, field in properties.items()
    )
    if not body:
        # An empty payload — `ping`. `Record<string, never>` is the honest type: the frame
        # carries an object, and reading a field off it is a mistake worth a compile error.
        return (
            f"{doc_comment(schema, '')}"
            f"export type {name} = Record<string, never>;\n"
        )
    return f"{doc_comment(schema, '')}export interface {name} {{\n{body}}}\n"


def collect(models: list[type[BaseModel]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Every model's own schema plus the shared `$defs` they reference.

    Returns `(roots, defs)`. A `$def` reached from two payloads must be identical in both,
    or the two models are describing the same name differently — a defect worth failing on
    rather than resolving by picking whichever was generated last.
    """
    roots: dict[str, Any] = {}
    defs: dict[str, Any] = {}
    for model in models:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        for name, definition in (schema.pop("$defs", {}) or {}).items():
            if name in defs and defs[name] != definition:
                raise ValueError(f"two different definitions of ${name} in $defs")
            defs[name] = definition
        roots[model.__name__] = schema
    return roots, defs


# ------------------------------------------------------------------------------------
#  Emitters
# ------------------------------------------------------------------------------------


def _union(name: str, values: list[str], doc: str) -> str:
    members = "\n".join(f'  | "{value}"' for value in values)
    return f"\n/** {doc} */\nexport type {name} =\n{members};\n"


def _const_array(name: str, values: list[str], doc: str, element: str) -> str:
    members = "\n".join(f'  "{value}",' for value in values)
    return (
        f"\n/** {doc} */\nexport const {name}: readonly {element}[] = [\n{members}\n] as const;\n"
    )


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


def _payloads() -> str:
    """Shared objects, then one payload type per model, then the event map and union."""
    ordered: list[type[BaseModel]] = []
    for event in EventType:
        model = PAYLOADS[event]
        if model not in ordered:
            ordered.append(model)

    roots, defs = collect(ordered)

    parts = [
        "\n// ── Shared payload objects ─────────────────────────────────────────────────\n"
    ]
    for name in sorted(defs):
        parts.append("\n" + render_interface(name, defs[name]))

    parts.append(
        "\n// ── Per-event payloads ─────────────────────────────────────────────────────\n"
    )
    for model in ordered:
        name = model.__name__
        alias = REST_ALIASES.get(name)
        if alias is not None:
            parts.append(
                f"\n{doc_comment(roots[name], '')}export type {name} = {alias};\n"
            )
            continue
        parts.append("\n" + render_interface(name, roots[name]))

    rows = "\n".join(
        f'  "{event.value}": {PAYLOADS[event].__name__};' for event in EventType
    )
    parts.append(
        "\n/** Every event type bound to the payload it carries (§9.4). */\n"
        f"export interface RunEventMap {{\n{rows}\n}}\n"
    )
    return "".join(parts)


ENVELOPE = """
/**
 * The §9.2 envelope, discriminated on `type`.
 *
 * Narrowing on `event.type` narrows `event.payload` with it, which is what makes the
 * store's fold an exhaustive `switch` rather than a pile of `String(p.x ?? "")` (§4.3).
 */
export type RunEvent = {
  [K in keyof RunEventMap]: {
    v: typeof PROTOCOL_VERSION;
    /** Gapless and strictly increasing per run, from 1. The resume cursor. Control frames carry 0. */
    seq: number;
    run_id: string;
    /** RFC 3339 UTC, millisecond precision. */
    ts: string;
    type: K;
    payload: RunEventMap[K];
  };
}[keyof RunEventMap];

/** One member of the union, by event name: `RunEventOf<"node.started">`. */
export type RunEventOf<K extends RunEventType> = Extract<RunEvent, { type: K }>;

/** Sent to the server; see §9.5. */
export interface ClientMessage<P = Record<string, unknown>> {
  type: ClientMessageType;
  payload?: P;
}
"""


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
        _payloads(),
        ENVELOPE,
    ]
    return "".join(parts)


def render_schema() -> str:
    """A `$defs` bundle of every payload plus the envelope.

    Not on the frontend's critical path (§4.3): it documents the protocol for consumers
    that are not TypeScript, and gives tests something to validate recorded fixtures
    against rather than trusting a hand-written sample.
    """
    ordered: list[type[BaseModel]] = []
    for event in EventType:
        if PAYLOADS[event] not in ordered:
            ordered.append(PAYLOADS[event])
    roots, defs = collect([*ordered, EventEnvelope])

    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pluton.local/schemas/events.schema.json",
        "title": f"{PROTOCOL} event payloads",
        "description": (
            "Generated from backend/app/schemas/events.py by scripts/gen_event_types.py. "
            "`payloads` maps each event type to the $def describing its payload."
        ),
        "payloads": {event.value: PAYLOADS[event].__name__ for event in EventType},
        "$defs": {**defs, **roots},
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _diff(current: str, generated: str, target: Path) -> None:
    sys.stdout.writelines(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=f"checked in: {target.relative_to(REPO_ROOT)}",
            tofile="generated",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if a checked-in file differs from what would be generated.",
    )
    args = parser.parse_args()

    outputs = [(TARGET, render()), (SCHEMA_TARGET, render_schema())]

    if args.check:
        stale = False
        for target, generated in outputs:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != generated:
                stale = True
                _diff(current, generated, target)
        if stale:
            print(
                "\nGenerated protocol types have drifted from "
                "backend/app/schemas/events.py. Run `make gen-event-types`.",
                file=sys.stderr,
            )
            return 1
        print("Generated protocol types are up to date.")
        return 0

    for target, generated in outputs:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated, encoding="utf-8")
        print(f"Wrote {target.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

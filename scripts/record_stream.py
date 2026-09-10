#!/usr/bin/env python3
"""Record a run's durable event log as a frontend test fixture.

docs/FRONTEND.md §13: the end-to-end suite is driven by a recorded stream rather than a
live backend, so it runs in CI in seconds and is deterministic. `GET /runs/{id}/events`
returns exactly what the WebSocket replays, from exactly the same source, which is what
makes a fixture recorded here a faithful stand-in for the socket.

    make record-stream RUN=b41e7c2a-… NAME=debug-loop

Fixtures MUST cover at least one run with a sandbox failure and a debug loop, one with a
HITL gate, and one that hits `replay.gap` — the three paths the UI is most likely to get
wrong and least likely to exercise by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "frontend" / "tests" / "fixtures"


def fetch(base: str, run_id: str, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/v1/runs/{run_id}/events?after_seq=0&limit=10000"
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - local API
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="The run to record.")
    parser.add_argument("--name", help="Fixture name. Defaults to the run id.")
    parser.add_argument(
        "--base",
        default=os.environ.get("PLUTON_API_BASE", "http://localhost:8000"),
        help="API base URL.",
    )
    args = parser.parse_args()

    token = os.environ.get("PLATFORM_API_TOKEN") or os.environ.get("API_TOKEN") or ""
    try:
        body = fetch(args.base, args.run_id, token)
    except urllib.error.URLError as exc:
        print(f"Could not read {args.run_id} from {args.base}: {exc}", file=sys.stderr)
        return 1

    events = body.get("events") or []
    if not isinstance(events, list) or not events:
        print(f"Run {args.run_id} has no retained events to record.", file=sys.stderr)
        return 1

    FIXTURES.mkdir(parents=True, exist_ok=True)
    target = FIXTURES / f"{args.name or args.run_id}.jsonl"
    # JSONL rather than one JSON array: a fixture is read a frame at a time by the mock
    # socket, and a line-oriented file is also greppable and diffable per event.
    target.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    print(
        f"Wrote {target.relative_to(REPO_ROOT)} — {len(events)} events, "
        f"gap={body.get('gap')}, oldest_available={body.get('oldest_available')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

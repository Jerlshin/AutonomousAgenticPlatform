# FRONTEND — Dashboard Design & Implementation Specification

> | | |
> |---|---|
> | **Status** | Normative. Supersedes the previous implementation-plan draft of this file. |
> | **Applies to** | Everything under `frontend/`. |
> | **Companion docs** | [`ARCHITECTURE.md §18`](./ARCHITECTURE.md#18-frontend-architecture) (stack and route map, summarised) · [`ARCHITECTURE.md §8`](./ARCHITECTURE.md#8-rest-api-contract) (REST contract) · [`ARCHITECTURE.md §9`](./ARCHITECTURE.md#9-websocket-protocol) (the wire protocol this UI consumes) · [`AGENTS.md §3`](./AGENTS.md#3-state-schema) / [`§4`](./AGENTS.md#4-graph-topology) / [`§7`](./AGENTS.md#7-agent-specifications) (what the UI renders) · [`MLOPS.md §3`](./MLOPS.md#3-the-metricsjson-contract) / [`§4`](./MLOPS.md#4-run-hierarchy-and-naming) (metrics and MLflow links) |
> | **Precedence** | Where this document and `ARCHITECTURE.md §18` disagree, the disagreement is a defect. Every known one is listed in [§16](#16-deviations-and-open-questions) with a proposed resolution; nothing else may drift silently. |

The platform's product is not a task queue with a log tail attached. It is the claim that an
autonomous run can be **watched, understood, and steered while it happens** — that a person can
see the graph move, see the criteria go from pending to met, see the sandbox fail and the debugger
reason about it, and intervene before a bad plan burns thirty minutes of GPU time. Every decision
below serves that claim.

This specification is written to be executable: an engineer should be able to build the dashboard
from it without re-deriving a contract from the backend source, and a reviewer should be able to
reject a pull request by pointing at a clause.

---

## Table of contents

1. [Purpose, status, and normative language](#1-purpose-status-and-normative-language)
2. [Requirements and system alignment](#2-requirements-and-system-alignment)
3. [Transport and protocol compliance](#3-transport-and-protocol-compliance)
4. [Type invariants and code generation](#4-type-invariants-and-code-generation)
5. [Directory tree](#5-directory-tree)
6. [State management contract](#6-state-management-contract)
7. [The `useRunStream` contract](#7-the-userunstream-contract)
8. [UI architecture and view hierarchy](#8-ui-architecture-and-view-hierarchy)
9. [Performance and virtualization controls](#9-performance-and-virtualization-controls)
10. [Design system](#10-design-system)
11. [Accessibility and interaction standards](#11-accessibility-and-interaction-standards)
12. [Setup, dependencies, and commands](#12-setup-dependencies-and-commands)
13. [Testing and quality gates](#13-testing-and-quality-gates)
14. [Build order](#14-build-order)
15. [Backend work this plan requires](#15-backend-work-this-plan-requires)
16. [Deviations and open questions](#16-deviations-and-open-questions)

---

## 1. Purpose, status, and normative language

### 1.1 What this document is

Three layers, and this document owns the third:

| Layer | Owner | Question it answers |
|---|---|---|
| Wire contract | [`ARCHITECTURE.md §8`](./ARCHITECTURE.md#8-rest-api-contract), [`§9`](./ARCHITECTURE.md#9-websocket-protocol) | What does the backend send? |
| Domain semantics | [`AGENTS.md`](./AGENTS.md), [`MLOPS.md`](./MLOPS.md) | What do those bytes *mean*? |
| **Presentation contract** | **this file** | What must the client do with them, exactly? |

It does not re-litigate the stack decisions in [`ARCHITECTURE.md §18.1`](./ARCHITECTURE.md#181-stack)
— those are settled and already reflected in `frontend/package.json`. It does supersede the
loose parts of §18.3–§18.5, which sketch the live run view; this file is the buildable version.

### 1.2 Requirement keywords

**MUST** / **MUST NOT** are conformance requirements: a violation is a defect, and several of them
are the difference between a dashboard that survives a twenty-minute run and one that locks the
tab. **SHOULD** is a strong default that may be traded away with a comment in the code stating
why. **MAY** is genuinely optional.

Every MUST in this document exists because the alternative was tried, reasoned through, or is a
known failure mode of the protocol in [`ARCHITECTURE.md §9`](./ARCHITECTURE.md#9-websocket-protocol).
Where the reason is not obvious, it is stated next to the rule rather than left for the reader to
reconstruct.

### 1.3 Implementation status

Phases 0–11 of [§14](#14-build-order) are built. The transport and state layers came first and
are unchanged in shape; the views were built on top of them in the order §14 sets out.

| Layer | Status | Where |
|---|---|---|
| WebSocket hook — ticket auth, reconnect, replay cursor, control floor, rAF batcher | Built, amended per [§7](#7-the-userunstream-contract) | `src/hooks/useRunStream.ts` |
| Zustand run store — ring buffers, exhaustive event folding | Built, extended per [§6.3](#63-the-zustand-run-store-schema) | `src/stores/runStore.ts` |
| Generated WS protocol types — enums **and** per-event payloads | Built ([§4.3](#43-websocket-types-from-pydantic-payload-models)) | `src/lib/events.generated.ts` |
| Generated REST types, and the single module that unpacks them | Built ([§4.2](#42-rest-types-from-openapi)) | `src/lib/api.d.ts`, `src/lib/rest.ts` |
| REST client | Built, covers every endpoint implemented in [§2.3](#23-backend-surface-implemented-versus-specified) | `src/lib/api.ts` |
| App shell, dashboard, task list, task detail, submission form | Built ([§8.1](#81-app-shell-and-route-map)–[§8.4](#84-task-creation-and-configuration)) | `src/app/`, `src/components/` |
| The four-pane run deck — graph, timeline, console, artifacts | Built ([§8.5](#85-the-live-run-control-deck)) | `src/components/run/` |
| HITL gate console, revision diff viewer, report viewer | Built ([§8.6](#86-human-in-the-loop-gate-console)–[§8.8](#88-report-viewer)) | `src/components/run/` |
| Corpus inspector and benchmark scorecards | Built ([§8.9](#89-corpus-inspector), [§8.10](#810-benchmark-scorecards)) | `src/components/corpus/`, `src/components/benchmarks/` |
| Hardening — load test, E2E against a recorded stream, a11y sweep | **Phase 12, outstanding** | [§13](#13-testing-and-quality-gates) |

Every view degrades against the backend surface that does not exist yet: the gates are in
`src/lib/capabilities.ts` and nowhere else ([§2.4](#24-degradation-rules-for-unimplemented-endpoints)),
and the backend work each one waits on is listed in [§15](#15-backend-work-this-plan-requires).

When a component needs a slice of run state the store does not expose, the fix is to extend the
store's fold (`applyEvent`), never to re-derive it by scanning the event buffer inside a component
— that scan is the performance bug this architecture is shaped to avoid.

---

## 2. Requirements and system alignment

### 2.1 Stack (normative)

| Concern | Choice | Constraint |
|---|---|---|
| Framework | Next.js 15, App Router | React 19. Server Components by default; `"use client"` only where [§2.2](#22-server-and-client-component-split) allows. |
| Language | TypeScript, `strict: true` **and** `noUncheckedIndexedAccess` | Already set in `tsconfig.json`. `any` is an ESLint error; narrow from `unknown` ([`ARCHITECTURE.md §20.2`](./ARCHITECTURE.md#202-typescript)). |
| Styling | Tailwind CSS v4 | Tokens live in `globals.css` under `@theme` — there is no `tailwind.config.js` in v4 ([§10](#10-design-system)). |
| Components | shadcn/ui, **vendored** into `src/components/ui/` | Copied in per component as a page needs it. MUST NOT add a blanket dependency on a component package. |
| Client run state | Zustand | One store per mounted run stream ([§6.3](#63-the-zustand-run-store-schema)). |
| Server state | TanStack Query v5 | REST reads, caching, invalidation ([§6.2](#62-tanstack-query-contract)). |
| Transport | Native `WebSocket` | No socket.io, no wrapper library. The protocol is already specified end to end. |
| Virtualization | TanStack Virtual | Mandatory for the console and any list that can exceed 200 rows ([§9.2](#92-virtualization-rules)). |
| Charts | Recharts | Metric curves from `metric.logged` and `metric_series` ([`MLOPS.md §3`](./MLOPS.md#3-the-metricsjson-contract)). |
| Code display | Shiki, **server-side only** | Highlighting happens in a Route Handler or Server Component; grammars MUST NOT ship to the browser ([§8.7](#87-code-revision-diff-viewer)). |
| Diagrams | See [§8.5.3](#853-pane-1--agent-graph-visualizer) | The live graph is a fixed-layout SVG, not a runtime Mermaid render. This deviates from §18.1 and is recorded in [§16](#16-deviations-and-open-questions). |

Nothing else may be added to `dependencies` without a line in the pull request explaining what it
replaces. The bundle budget in [§9.4](#94-bundle-budget) is the enforcement mechanism.

### 2.2 Server and Client component split

The split is not stylistic. A Server Component cannot hold a socket, and a Client Component that
wraps the whole page turns every `token.delta` into a full-tree reconciliation.

| Rule | |
|---|---|
| **MUST** be Server Components | `layout.tsx`, page shells, static chrome, anything that only renders props |
| **MUST** be Client Components | Anything calling `useRunStream`, any Zustand or TanStack Query consumer, any pane with local UI state (filters, scroll, search) |
| **MUST NOT** | Put `"use client"` on a route's `page.tsx` when only one child needs it. Push the boundary down to the smallest subtree that needs interactivity. |
| **MUST NOT** | Fetch authenticated REST data in a Server Component using `NEXT_PUBLIC_API_TOKEN`. The token is a browser-visible single shared secret ([`ARCHITECTURE.md §13.2`](./ARCHITECTURE.md#132-authentication-and-cors)); server-side reads MUST use a server-only variable ([§12.3](#123-environment-variables)). |
| **SHOULD** | Render the page frame, navigation, and empty pane skeletons on the server so the first paint is not a spinner, then let the client subtree fill them. |

The live run view is the canonical shape: `app/runs/[runId]/page.tsx` is a Server Component that
renders the four-pane grid and mounts one Client Component, `<RunDeck runId=… />`, which owns the
stream. Each pane is its own Client Component subscribing to its own store slice.

### 2.3 Backend surface: implemented versus specified

[`ARCHITECTURE.md §8.2`](./ARCHITECTURE.md#82-endpoint-index) is the target contract. Part of it
is not built yet. A frontend written against the target as though it were live fails at runtime in
exactly the views that matter most, so the split is stated here and every view in [§8](#8-ui-architecture-and-view-hierarchy)
declares which side of it it depends on.

**Implemented today** (verified against `backend/app/api/v1/`):

| Method | Path | Response model |
|---|---|---|
| `GET` | `/health`, `/health/deep` | `HealthCheckResponse`, `DeepHealthResponse` |
| `POST` `GET` `PATCH` `DELETE` | `/tasks`, `/tasks/{task_id}` | `TaskRead`, `TaskListResponse` |
| `POST` | `/tasks/{task_id}/runs` | `RunAccepted` — honours `Idempotency-Key` |
| `GET` | `/tasks/{task_id}/runs` | `RunListResponse` |
| `GET` | `/runs/{run_id}` | `RunRead` |
| `GET` | `/runs/{run_id}/events?after_seq=` | `RunEventsResponse` — carries `gap`, `oldest_available` |
| `POST` | `/runs/{run_id}/cancel`, `/resume`, `/approve` | `StandardResponse` |
| `POST` `GET` `DELETE` | `/corpus/documents`, `/corpus/documents/{doc_id}` | `CorpusDocumentRead`, `CorpusDocumentListResponse` |
| `POST` | `/corpus/search` | `CorpusSearchResponse` |
| `GET` | `/benchmarks`, `/benchmarks/{suite}/results` | `BenchmarkSuiteListResponse`, `BenchmarkResultsResponse` |
| `POST` | `/benchmarks/{suite}/run` | `BenchmarkRunAccepted` |
| `POST` | `/ws/tickets` | `WsTicketResponse` |
| `WS` | `/ws/runs/{run_id}` | `pluton.v1` |
| `GET` | `/metrics` (unversioned, unauthenticated, Prometheus text) | — |

**Specified but not implemented.** Depending on any of these without a fallback is a defect:

`GET /runs/{id}/steps` · `GET /runs/{id}/artifacts` · `GET /runs/{id}/bundle` ·
`GET /runs/{id}/report` · `GET /runs/{id}/evaluation` · `GET /artifacts/{id}` ·
`GET /artifacts/{id}/download` · `GET /agents` · `POST /agents/{name}/invoke` ·
aggregate statistics for the dashboard.

Two further mismatches with §8.3 that the UI MUST accommodate:

1. **Run creation takes no configuration body.** `POST /tasks/{task_id}/runs` currently accepts
   only an `Idempotency-Key` header. `budgets`, `model_overrides`, `hitl_gates`, and
   `sandbox_profile` from §8.3 are not read. `POST /tasks` accepts only `title` and `prompt` —
   no `task_kind`, no `tags`. [§8.4](#84-task-creation-and-configuration) specifies the form
   against the target contract and how it behaves until the backend catches up.
2. **A task *is* a run.** `run_id == task_id` today, one run per task, and `RunRead` is projected
   from a `tasks` row merged with the Redis summary hash. `attempt`, `plan[]`, `progress`,
   `counters`, `evaluation`, `mlflow`, and `deliverables[]` from the §8.3 body are **not**
   fields on `RunRead`. For a *terminal* run, the equivalent data is inside `RunRead.result`,
   which stores the `run.completed` payload verbatim: `{status, deliverables[], bundle_url,
   evaluation, mlflow, usage}`. For a *live* run it exists only as WebSocket events.

That second point is the single most important thing to internalise before building the run view:
**live truth is the event stream, terminal truth is `result`, and REST alone shows neither.**

### 2.4 Degradation rules for unimplemented endpoints

| Rule | |
|---|---|
| A view **MUST NOT** call a non-existent endpoint speculatively | A 404 storm in the console makes real failures invisible |
| Every capability that depends on missing backend work **MUST** be gated behind a single flag in `src/lib/capabilities.ts` | One place to flip when the endpoint lands, one place to grep in review |
| A gated-off feature **MUST** render an explicit disabled affordance with a reason | A greyed **Download bundle** button with the tooltip *"`GET /runs/{id}/bundle` is not implemented yet"* is honest; a button that 404s is not, and a silently missing button hides scope |
| Fallback data paths **MUST** be labelled in the UI when they are lossy | e.g. the artifact deck for a run that finished before this tab opened shows a *"reconstructed from the run record"* marker |

```ts
// src/lib/capabilities.ts — the flag set, and nothing else.
export const CAPABILITIES = {
  runSteps: false,        // GET /runs/{id}/steps
  runArtifacts: false,    // GET /runs/{id}/artifacts
  runBundle: false,       // GET /runs/{id}/bundle
  runReport: false,       // GET /runs/{id}/report
  artifactDownload: false,// GET /artifacts/{id}/download
  agentRegistry: false,   // GET /agents
  runConfig: false,       // POST /tasks/{id}/runs with a body
  stats: false,           // aggregate dashboard statistics
} as const;
```

---

## 3. Transport and protocol compliance

The client half of [`ARCHITECTURE.md §9`](./ARCHITECTURE.md#9-websocket-protocol). All of it is
implemented in exactly one module, `src/lib/useRunStream.ts`; no other file in the app may
construct a `WebSocket`.

### 3.1 Connection lifecycle

```
mount
  │
  ├─ GET /api/v1/runs/{id}            ← seed the header before the first frame
  ├─ POST /api/v1/ws/tickets          ← single-use, 60 s, run-scoped
  ├─ WS   /api/v1/ws/runs/{id}?ticket=…&after_seq={cursor}
  │       subprotocol: pluton.v1
  │
  ├─ ← hello            {protocol, run, last_seq, heartbeat_s}
  ├─ → subscribe        (only if a filter was requested — see §3.5)
  ├─ ← replay …         events with seq > cursor
  ├─ ← replay.complete  {through_seq}
  ├─ ← live tail …
  ├─ ← ping  → pong     every heartbeat_s (default 20 s)
  │
  └─ unmount → close(1000, "component unmounted")
```

The REST read before the socket is not redundant with `hello`. A `QUEUED` run has nothing in its
stream but `run.queued`, and the header would otherwise render empty until a worker picks the job
up — which on a busy box is minutes.

### 3.2 Ticket authentication

| Rule | |
|---|---|
| The client **MUST** mint a fresh ticket on **every** connect attempt, including every reconnect | The server consumes the ticket with `GETDEL` at accept time. A cached ticket succeeds once and then fails every retry in the backoff loop — a failure that looks exactly like a server outage |
| The client **MUST NOT** put the platform bearer token in the WebSocket URL | URLs reach proxy logs and browser history. The ticket exists precisely so the token does not travel |
| Ticket minting failure **MUST NOT** abort the connection attempt | A loopback development box runs without a token; failing hard there makes the dashboard useless where it is used most. Connect without a ticket and let the server decide |
| First-frame auth (§9.3 mechanism 2) **MAY** be implemented as a fallback | Only if a `4401` close follows a ticketless connect. It is not the primary path |
| Three consecutive `4401` closes **MUST** stop the loop and surface an auth error | Retrying a rejected credential forever is a busy loop with a user-visible symptom of "nothing happens" |

### 3.3 Sequence cursor and gapless replay

| Rule | |
|---|---|
| The cursor **MUST** advance only when `event.seq > 0` | Control frames (`hello`, `ping`, `replay.*`, `run.snapshot`, `error`) carry `seq: 0`. Treating a `ping` as progress would skip real history on the next reconnect |
| The cursor **MUST** be monotonic: `cursor = max(cursor, seq)` | Defensive against an out-of-order forward; `seq` is authoritative, arrival order is not |
| The cursor **MUST** live in a ref, not in React state | The reconnect closure must read the current value, not the value captured at the render that created it |
| The client **MUST** reconnect with `?after_seq={cursor}` | This is the whole resume mechanism |
| A client that sends a `subscribe` filter **MUST** accept that its cursor trails the stream | Filtered events never arrive, so their seqs are never seen and a reconnect replays them again. This is the documented tradeoff of a server-side filter, and it is harmless as long as the client is idempotent per `seq` |
| Every buffer entry **MUST** be keyed by `seq` (console lines by a monotonic id) | A replayed duplicate must be identifiable |
| `event.v !== 1` **MUST** close with `4400` and stop | §9.2. Guessing at an unknown protocol version is worse than failing |

### 3.4 Gap recovery

When the requested `after_seq` is older than the retained backlog, the server sends
`replay.gap {requested_after, oldest_available}` and then a full `run.snapshot`.

Normative recovery sequence:

1. Set `historyComplete = false` and record `oldestAvailable`.
2. Reset the cursor to `0` so the connection replays everything still retained.
3. On the following `run.snapshot`, replace `run` wholesale. Derived slices that the snapshot
   authoritatively covers (`status`, `phase`, `current_node`, counters, `last_seq`) **MUST** be
   taken from it.
4. Derived slices the snapshot does **not** cover — the timeline, console, artifact and criteria
   buffers — **MUST NOT** be silently trusted afterwards. The affected panes MUST render a
   truncation banner: *"Earlier history was dropped from the server's retention window (events
   before `seq {oldest_available}`)."*
5. The client **MAY** backfill with `GET /runs/{id}/events?after_seq=0`, which returns the same
   retained window plus its own `gap` / `oldest_available` markers. It **MUST NOT** treat a
   successful backfill as making history complete when `gap` is `true`.

The same banner mechanism covers a client-side drop: when the console ring buffer evicts, the pane
reports how many lines are no longer held. A log viewer that quietly loses lines is worse than one
that says it did.

### 3.5 Heartbeats and the subscribe control floor

This clause is load-bearing and the current hook violates it.

The server applies a connection's `subscribe` filter to **every** outbound frame, control frames
included (`RunConnection.send_event` → `matches_filter`). The heartbeat increments `missed_pongs`
whether or not the `ping` was actually written. So a client that subscribes to, say,
`["node.", "run."]`:

* never receives a `ping`,
* therefore never sends a `pong`,
* accumulates two missed pongs in ~40 s,
* and is closed with `1001`, reconnects, and repeats — forever.

Therefore:

| Rule | |
|---|---|
| Any `subscribe` message **MUST** union the caller's requested types with the control floor | Non-negotiable |
| The control floor is `["ping", "hello", "error", "replay.", "run.snapshot"]` | Prefix entries end in `.` or `*`; the server matches both |
| `pong` **MUST** be sent synchronously on receipt of `ping` | It costs one frame; deferring it into a batched update risks crossing the 40 s window on a loaded tab |
| The hook **MUST** expose the effective filter it sent | Debuggability: "why am I not seeing artifacts" is otherwise unanswerable from the UI |
| A pane **MUST NOT** send its own `subscribe` | One socket per run, one filter, owned by the hook. Two panes negotiating one filter is a race |

The four-pane deck consumes nearly every event type, so the deck **SHOULD** send no filter at all.
The filter exists for narrow consumers — a dashboard card tailing many runs wants `run.` only, and
skipping several hundred `token.delta` frames per run is the difference between a smooth list and
a stuttering one.

### 3.6 Reconnection backoff and close-code policy

Verbatim from [`ARCHITECTURE.md §9.8`](./ARCHITECTURE.md#98-client-reconnection-algorithm-normative),
with the close-code branch expanded to the codes in [§9.7](./ARCHITECTURE.md#97-close-codes):

```
last_seq ← 0 ; backoff ← 500 ms
loop:
    ticket ← POST /api/v1/ws/tickets {run_id}
    ws     ← connect(/api/v1/ws/runs/{run_id}?ticket=…&after_seq=last_seq)
    on open:      backoff ← 500 ms
    on message m: if m.seq > 0 then last_seq ← m.seq
                  if m.type = "replay.gap" then last_seq ← 0 ; await run.snapshot
    on close c:   dispatch on c per the table below
                  sleep(backoff + jitter(0, 250 ms)) ; backoff ← min(backoff × 2, 30 s)
```

| Close code | Meaning | Client action |
|---|---|---|
| `1000` | Normal | Stop **iff** the run is terminal; otherwise reconnect. A `1000` on a live run means the server closed for its own reasons |
| `1001` | Going away / heartbeat failure | Reconnect with backoff |
| `1011` | Internal error | Reconnect with backoff; surface a non-blocking error toast |
| `4400` | Protocol error | **Stop.** Surface a hard error. Retrying a malformed client cannot help |
| `4401` | Unauthenticated / ticket expired | Re-mint and reconnect immediately (bypassing backoff) for the first two attempts; on the third consecutive `4401`, stop with an auth error |
| `4403` | Forbidden for this run | **Stop.** |
| `4404` | Run not found | **Stop.** |
| `4429` | Connection quota exceeded (8/run, 64/server) | Reconnect, but with a **5 s** backoff floor. The tab is competing with other tabs; hammering at 500 ms guarantees it keeps losing |
| any other | — | Treat as `1011` |

Additional requirements:

* Backoff **MUST** reset to 500 ms on a successful `open`, not on a successful `hello`. A socket
  that opens and is immediately closed by quota still counts as progress toward the ceiling.
* Jitter **MUST** be applied (0–250 ms). Four panes' worth of tabs reconnecting in lockstep after
  an API restart is a self-inflicted thundering herd.
* The reconnect timer **MUST** be cleared on unmount, and the socket closed with `1000`. React 19
  Strict Mode double-mounts in development; a leaked socket per mount exhausts the 8-per-run
  quota after four remounts and then looks like a server bug.
* The hook **SHOULD** pause reconnection while `document.visibilityState === "hidden"` **and** the
  run is terminal. It **MUST NOT** pause for a live run — a backgrounded tab watching a run is the
  normal case, and that is exactly what replay is for.

### 3.7 Memory bounds on the client

The server bounds its side: token deltas are coalesced at 80 ms / 64 chars, sandbox lines are
truncated at 4 KiB per frame and capped at 2 MiB per stream, and the Redis stream is `XADD`-capped.
None of that bounds a React array.

| Buffer | Cap | Eviction | Rationale |
|---|---|---|---|
| `events` | 2000 | Oldest first | Raw envelopes are kept only for the debug drawer and diagnostics; the panes read folded slices |
| `consoleLines` | 5000 | Oldest first | ~500 KB at typical line length. A `train` run emits tens of thousands |
| `timeline` | 500 entries | Oldest first, never evicting a `running` entry | A 60-node-visit budget means 500 is unreachable in practice; the cap is a guard against a pathological loop |
| `codeRevisions` | 12 (`max_sandbox_executions`) | Oldest first, content dropped before metadata | Full source per revision is the largest single payload the UI holds |
| `retrievalHits` | 200 | Oldest first | Debug detail, not primary product |
| `metricPoints` | 4000 | Oldest first | Matches `metric_series` `maxItems` of 2000 with headroom for two series |

Caps **MUST** be module constants with the justification comment attached, exported so tests can
assert against them rather than hard-coding a number in two places.

### 3.8 Protocol conformance checklist

A pull request touching the transport layer **MUST** be able to answer yes to all of these. They
map one-to-one onto the test cases in [§13](#13-testing-and-quality-gates).

- [ ] Subprotocol `pluton.v1` is requested on connect.
- [ ] `v !== 1` closes with `4400` and does not retry.
- [ ] Cursor advances only on `seq > 0`, monotonically.
- [ ] A fresh ticket is minted per attempt; ticket failure does not abort the attempt.
- [ ] `ping` is answered with `pong` synchronously.
- [ ] Any `subscribe` includes the control floor.
- [ ] `replay.gap` resets the cursor, awaits `run.snapshot`, and marks history incomplete.
- [ ] Backoff is 500 ms → ×2 → 30 s with 0–250 ms jitter, reset on `open`.
- [ ] `4400` / `4403` / `4404` stop; `4429` backs off from a 5 s floor; `4401` re-mints then stops after three.
- [ ] `1000` stops only when the run is terminal.
- [ ] Unmount clears timers and closes the socket.
- [ ] Every buffer is capped and eviction is user-visible.
- [ ] Binary frames are never sent; every client message is a JSON object with a `type`.

---

## 4. Type invariants and code generation

### 4.1 The rule

> **No type describing a backend payload may be written by hand.**

[`ARCHITECTURE.md §18.5`](./ARCHITECTURE.md#185-typed-contracts) states the intent; this section
is the pipeline. The failure it prevents is specific and recurring: a backend field is renamed,
the frontend keeps its stale interface, TypeScript stays green, and the bug surfaces in production
as `undefined` rendered into a table cell. Generated types turn that into a red build.

Two independent generators, because the two halves of the contract have different sources of
truth. REST lives in OpenAPI. **The WebSocket protocol does not appear in OpenAPI at all** — it is
Pydantic models emitted through Redis — so it needs its own export.

```
backend/app/main.py ──► scripts/dump_openapi.py ──► backend/openapi.json
                                                          │
                                                          ▼
                                              openapi-typescript
                                                          │
                                                          ▼
                                          frontend/src/lib/api.d.ts        (REST)

backend/app/schemas/events.py ──► scripts/gen_event_types.py ──► frontend/src/lib/events.generated.ts  (WS)
```

Both generated files **MUST** be committed. CI regenerates and diffs; a difference fails the
build ([§4.4](#44-drift-gates-in-ci)). Committing them keeps a clean clone type-checkable without
a running backend, which is what makes the gate cheap enough to actually run.

### 4.2 REST types from OpenAPI

`openapi-typescript` is already in `devDependencies`. What is missing is a source document that
does not require a live server.

**`scripts/dump_openapi.py`** (new) imports the FastAPI app and writes its schema:

```python
#!/usr/bin/env python3
"""Write `backend/openapi.json` without starting a server.

`openapi-typescript` can read a URL, but requiring a live API turns type generation into a
task that only works on a fully composed stack — it cannot run in CI, in a clean clone, or
in a pre-commit hook. `app.openapi()` is the same document the server would serve.
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
    document = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        current = TARGET.read_text() if TARGET.exists() else ""
        if current != document:
            print("backend/openapi.json is stale — run `make openapi`", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(document)
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Generation, and how the result is consumed:

```bash
python scripts/dump_openapi.py
npx openapi-typescript backend/openapi.json -o frontend/src/lib/api.d.ts
```

```ts
// src/lib/rest.ts — the ONLY place generated OpenAPI types are unpacked.
import type { components, paths } from "./api.d";

type Schemas = components["schemas"];

export type RunRead        = Schemas["RunRead"];
export type RunAccepted    = Schemas["RunAccepted"];
export type RunListResponse= Schemas["RunListResponse"];
export type RunEventsResponse = Schemas["RunEventsResponse"];
export type TaskRead       = Schemas["TaskRead"];
export type TaskListResponse = Schemas["TaskListResponse"];
export type CorpusSearchRequest  = Schemas["CorpusSearchRequest"];
export type CorpusSearchResponse = Schemas["CorpusSearchResponse"];
export type CorpusDocumentRead   = Schemas["CorpusDocumentRead"];
export type BenchmarkResultsResponse = Schemas["BenchmarkResultsResponse"];
export type BenchmarkSuiteListResponse = Schemas["BenchmarkSuiteListResponse"];
export type WsTicketResponse = Schemas["WsTicketResponse"];
export type StandardResponse = Schemas["StandardResponse"];

/** Request/response of one operation, for the fetch wrapper's generics. */
export type Body<P extends keyof paths, M extends keyof paths[P]> = /* … */ unknown;
```

| Rule | |
|---|---|
| `api.d.ts` **MUST NOT** be edited | It carries a `GENERATED` banner |
| Modules **MUST** import from `rest.ts`, never from `api.d` directly | One indirection means a backend schema rename is one edit, not forty |
| A hand-written interface duplicating a `components["schemas"]` entry **MUST** be deleted on sight | Today `src/lib/types.ts` hand-writes `RunDetail`, `TaskSummary`, `TaskListResponse`, `RunAccepted`, `WsTicket`. All five are replaced by aliases in `rest.ts` in the first build phase ([§14](#14-build-order)) |
| `types.ts` **MAY** keep only view models with no backend counterpart | `ConsoleLine`, `TimelineEntry`, `CriterionRow`, `ArtifactRow`, `MetricPoint`, `GraphNodeState` |

### 4.3 WebSocket types from Pydantic payload models

`scripts/gen_event_types.py` exists and generates the *enums* — event names, client message
names, close codes, control and terminal sets. That is half the contract. Every `payload` is
still `Record<string, unknown>`, which is why `runStore.ts` is full of `String(p.node ?? "")`
coercions. Those coercions are the hand-written duplicate typing this section abolishes.

**Backend change (prerequisite).** `backend/app/schemas/events.py` gains one Pydantic model per
event type and a registry binding them:

```python
class NodeStartedPayload(BaseModel):
    node: str
    agent: str
    phase: str
    model: str | None = None
    plan_step_id: str | None = None
    step_seq: int | None = None


class NodeCompletedPayload(BaseModel):
    node: str
    duration_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    degraded: bool = False
    summary: str = ""


# … one per EventType …

PAYLOADS: dict[EventType, type[BaseModel]] = {
    EventType.NODE_STARTED: NodeStartedPayload,
    EventType.NODE_COMPLETED: NodeCompletedPayload,
    # …
}
```

Emitters **SHOULD** be migrated to construct these models rather than dicts, which makes the
schema the enforced shape rather than documentation of one. Until that migration completes, the
registry alone is enough to generate the frontend types, and a unit test asserting that every
`EventType` has a `PAYLOADS` entry keeps the two from drifting.

**Generator change.** `scripts/gen_event_types.py` additionally emits, from
`model_json_schema()` of each registered model, a payload interface per event and a discriminated
union:

```ts
// GENERATED FILE — DO NOT EDIT.  Source: backend/app/schemas/events.py

export interface NodeStartedPayload {
  node: string;
  agent: string;
  phase: string;
  model: string | null;
  plan_step_id: string | null;
  step_seq: number | null;
}

export interface RunEventMap {
  "node.started": NodeStartedPayload;
  "node.completed": NodeCompletedPayload;
  // … every EventType …
}

/** The §9.2 envelope, discriminated on `type`. */
export type RunEvent = {
  [K in keyof RunEventMap]: {
    v: 1;
    seq: number;
    run_id: string;
    ts: string;
    type: K;
    payload: RunEventMap[K];
  };
}[keyof RunEventMap];

export type RunEventOf<K extends RunEventType> = Extract<RunEvent, { type: K }>;
```

The payoff is direct: the store's fold becomes an exhaustive `switch` in which
`event.payload.node` is a `string` and a backend field rename is a compile error at the case that
reads it. The `String(p.x ?? "")` defensive coercions **MUST** be removed as part of the same
change — leaving them keeps the runtime tolerant of a shape the compiler now guarantees, which is
how a silent mismatch gets a second life.

**JSON Schema for the envelope.** The generator **SHOULD** also emit
`backend/app/schemas/events.schema.json` (a `$defs` bundle of every payload plus the envelope),
so the protocol is documented for non-TypeScript consumers and can be used to validate fixtures
in tests. It is not on the frontend's critical path.

### 4.4 Drift gates in CI

Four gates, all runnable offline:

| Command | Fails when |
|---|---|
| `make openapi-check` | `backend/openapi.json` is stale relative to the FastAPI app |
| `make check-fe-types` | `frontend/src/lib/api.d.ts` differs from a regeneration of the committed `openapi.json` |
| `make check-event-types` | `frontend/src/lib/events.generated.ts` differs from a regeneration from `events.py` |
| `make fe-typecheck` | `tsc --noEmit` fails — which it will, loudly, the first time a generated rename reaches a consumer |

These join the aggregate `make check` target ([§12.2](#122-makefile-targets-to-add)). A backend
pull request that renames an event field and does not regenerate is red before review.

### 4.5 What may still be hand-written

| Category | Example | Why it is exempt |
|---|---|---|
| View models | `ConsoleLine`, `TimelineEntry` | They are the client's own folding of many events; no backend model corresponds |
| Static domain tables | `GRAPH_TOPOLOGY`, `PHASE_ORDER`, `GATE_METADATA` | Transcribed from [`AGENTS.md §4.1`](./AGENTS.md#41-full-graph) and [`§9`](./AGENTS.md#9-human-in-the-loop-gates). They mirror graph *structure*, not a payload, and MUST carry a comment naming the section they came from |
| UI-only state | filter selections, scroll anchors | Never crosses the wire |

Static domain tables are the sharpest remaining edge: a node added to the graph in `AGENTS.md`
will not fail a build here. [§13](#13-testing-and-quality-gates) requires a test that asserts the
frontend topology table matches `backend/app/engine/graph.py`'s registered nodes, exported as a
fixture, so that edge is covered by data rather than by vigilance.

---

## 5. Directory tree

```
frontend/
├── src/
│   ├── app/                                  # Next.js 15 App Router — Server Components by default
│   │   ├── layout.tsx                        # ✅ shell: nav, Providers
│   │   ├── globals.css                       # ✅ Tailwind v4 @theme tokens (§10)
│   │   ├── error.tsx                         # ✅ route error boundary
│   │   ├── not-found.tsx                     # ✅
│   │   ├── page.tsx                          # ✅ §8.2  Global dashboard
│   │   ├── tasks/
│   │   │   ├── page.tsx                      # ✅ §8.3  Task list
│   │   │   ├── new/page.tsx                  # ✅ §8.4  Submission + configuration
│   │   │   └── [taskId]/page.tsx             # ✅ §8.3  Task detail, run history
│   │   ├── runs/[runId]/
│   │   │   ├── page.tsx                      # ✅ §8.5  THE LIVE RUN CONTROL DECK
│   │   │   ├── loading.tsx                   # ✅ pane skeletons
│   │   │   ├── code/page.tsx                 # ✅ §8.7  Revision diff viewer
│   │   │   └── report/page.tsx               # ✅ §8.8  Rendered REPORT.md
│   │   ├── corpus/page.tsx                   # ✅ §8.9  Documents + retrieval playground
│   │   ├── benchmarks/page.tsx               # ✅ §8.10 Suite scorecards, KPI trends
│   │   └── api/
│   │       ├── artifact/[artifactId]/route.ts# ✅ §8.5.6 authenticated download proxy
│   │       └── highlight/route.ts            # ✅ §8.7  server-side Shiki
│   ├── components/
│   │   ├── providers.tsx                     # ✅ TanStack Query client
│   │   ├── dashboard/
│   │   │   ├── DashboardClient.tsx         # ✅ §8.2  client root: tiles, live rows
│   │   │   ├── LiveRunRow.tsx              # ✅ §8.2  one active run, its own stream
│   │   │   └── DependencyStrip.tsx         # ✅ §8.2  /health/deep
│   │   ├── shell/
│   │   │   ├── NavBar.tsx                    # ✅ extracted from layout when nav grows
│   │   │   └── ConnectionBadge.tsx           # ✅ socket status, global
│   │   ├── run/
│   │   │   ├── RunDeck.tsx                   # ✅ §8.5.1 client root: owns useRunStream
│   │   │   ├── RunHeader.tsx                 # ✅ §8.5.2 status, phase, budgets
│   │   │   ├── OperatorControls.tsx          # ✅ §8.5.2 cancel · resync · resume
│   │   │   ├── GraphPane.tsx                 # ✅ §8.5.3
│   │   │   ├── GraphNode.tsx                 # ✅ §8.5.3
│   │   │   ├── TimelinePane.tsx              # ✅ §8.5.4
│   │   │   ├── TimelineRow.tsx               # ✅ §8.5.4
│   │   │   ├── CriteriaLedger.tsx            # ✅ §8.5.4
│   │   │   ├── ConsolePane.tsx               # ✅ §8.5.5
│   │   │   ├── ConsoleToolbar.tsx            # ✅ §8.5.5
│   │   │   ├── ArtifactDeck.tsx              # ✅ §8.5.6
│   │   │   ├── MetricSparkline.tsx           # ✅ §8.5.6
│   │   │   ├── MetricsTable.tsx              # ✅ §8.5.6 metrics.json
│   │   │   ├── DeckFocus.tsx                # ✅ §8.5.1 cross-pane selection
│   │   │   ├── GateConsole.tsx               # ✅ §8.6  HITL approvals
│   │   │   ├── DiffViewer.tsx                # ✅ §8.7
│   │   │   ├── CodeBrowser.tsx               # ✅ §8.7  revision rail + diagnosis
│   │   │   ├── ReportViewer.tsx              # ✅ §8.8  server-rendered, TOC, cross-check
│   │   │   └── ReportActions.tsx             # ✅ §8.8  copy · download · print (client)
│   │   ├── tasks/
│   │   │   ├── TaskTable.tsx                 # ✅ §8.3
│   │   │   ├── TaskDetail.tsx                # ✅ §8.3
│   │   │   ├── TaskForm.tsx                  # ✅ §8.4
│   │   │   ├── BudgetFields.tsx              # ✅ §8.4
│   │   │   ├── GateSelector.tsx              # ✅ §8.4
│   │   │   └── RunHistoryList.tsx            # ✅ §8.3
│   │   ├── corpus/
│   │   │   ├── CorpusClient.tsx              # ✅ §8.9  the two halves
│   │   │   ├── SearchPlayground.tsx          # ✅ §8.9
│   │   │   └── DocumentTable.tsx             # ✅ §8.9  table + ingest + delete
│   │   ├── benchmarks/
│   │   │   ├── BenchmarksClient.tsx          # ✅ §8.10 suite selection, run + poll
│   │   │   ├── KpiScorecard.tsx              # ✅ §8.10
│   │   │   └── SuiteResultsTable.tsx         # ✅ §8.10
│   │   ├── charts/
│   │   │   └── OutcomeTrend.tsx              # ✅ §8.2 (Recharts; see the dataviz rules in §10)
│   │   └── ui/                               # vendored shadcn/ui — one component per file
│   │       ├── primitives.tsx                # ✅ Panel, Dot, Chip, Button, Empty, formatters
│   │       ├── table.tsx  · tabs.tsx  · select.tsx
│   │       ├── dialog.tsx · tooltip.tsx           # ✅ Badge ships inside primitives.tsx
│   │       └── virtual-list.tsx              # ✅ TanStack Virtual wrapper (§9.2)
│   ├── hooks/
│   │   ├── useRunStream.ts                   # ✅ §7  the only WebSocket in the app
│   │   ├── useRunSlice.ts                    # ✅ §6.5 selector helper over the run store
│   │   ├── useFollowTail.ts                  # ✅ §8.5.5 scroll-anchoring for the console
│   │   ├── useDebouncedValue.ts              # ✅ search inputs
│   │   └── useTicker.ts                      # ✅ §9.3 one shared 1 Hz interval
│   ├── stores/
│   │   ├── runStore.ts                       # ✅ §6.3
│   │   ├── RunStoreProvider.tsx              # ✅ §6.5 puts one store in scope
│   │   └── uiStore.ts                        # ✅ §6.1 persisted pane sizes, filters
│   └── lib/
│       ├── api.ts                            # ✅ REST client (§2.3) — browser token
│       ├── serverApi.ts                      # ✅ §2.2 server-only token reads
│       ├── api.d.ts                          # ✅ GENERATED — openapi-typescript (§4.2)
│       ├── rest.ts                           # ✅ the only unpacking of api.d.ts (§4.2)
│       ├── events.generated.ts               # ✅ GENERATED — WS protocol (§4.3)
│       ├── types.ts                          # ✅ view models only (§4.5)
│       ├── queryKeys.ts                      # ✅ §6.2 the key registry
│       ├── capabilities.ts                   # ✅ §2.4 feature flags
│       ├── graphTopology.ts                  # ✅ §8.5.3 static node/edge table
│       ├── domain.ts                         # ✅ enums mirrored from the docs
│       ├── status.ts  · outcome.ts           # ✅ status/outcome tone mapping
│       ├── terminalResult.ts                 # ✅ §2.3 the only reader of RunRead.result
│       ├── report.ts                         # ✅ §8.8 sections + criteria cross-check
│       ├── benchmarks.ts                     # ✅ §8.10 KPI targets, comparison
│       ├── log.ts                            # ✅ client-side logging
│       ├── gates.ts                          # ✅ §8.6 gate metadata from AGENTS.md §9
│       ├── diff.ts                           # ✅ §8.7 line diff, dependency-free
│       ├── format.ts                         # ✅ durations, bytes, tokens, metric values
│       └── ansi.ts                           # ✅ §8.5.5 escape-sequence stripping
├── tests/
│   ├── protocol/                             # §13 — the §3.8 checklist, one test per line
│   ├── store/                                # fold correctness, ring eviction
│   ├── lib/                                  # pure helpers: ansi, diff, report, benchmarks
│   ├── contract/                             # graph topology, the KPI mirror (§13)
│   ├── fixtures/                             # generated + hand-authored contract fixtures
│   └── e2e/                                  # ⬜ Playwright, against a recorded stream
├── public/
├── .env.example                              # ✅ §12.3
├── next.config.ts                            # ✅ no rewrites — see its header comment
├── tsconfig.json                             # ✅ strict + noUncheckedIndexedAccess, `@/*` → ./src/*
├── package.json                              # ✅
└── postcss.config.mjs                        # ✅
```

Two structural moves from the current tree, both deliberate:

* `useRunStream.ts` and `runStore.ts` move out of `lib/` into `hooks/` and `stores/`. `lib/` is
  for things with no React dependency — a rule that keeps the socket, the store, and pure helpers
  from congealing into one directory where nobody can tell what is testable in isolation.
* `components/run/` gains a `RunDeck.tsx` root. Exactly one component in the app calls
  `useRunStream`; everything else reads slices ([§6.5](#65-selector-discipline)).

---

## 6. State management contract

### 6.1 Ownership split

Three state systems, and a piece of state belongs to exactly one:

| System | Owns | Lifetime | Examples |
|---|---|---|---|
| **TanStack Query** | Everything readable over REST | Cache, survives navigation | task list, task detail, run history, corpus documents, benchmark results, terminal run bodies |
| **Zustand (`runStore`)** | The live event stream and its folded projections | One mounted run view | timeline, console, criteria, artifacts, graph state, budgets, pending gate |
| **Zustand (`uiStore`)** | Operator preferences | `localStorage` | pane split sizes, console filters, "follow tail" toggle, theme |
| React local state | Nothing that outlives a component | — | input values, open/closed disclosure |

The boundary rule, stated once so it can be cited in review:

> **REST is the resting state of a run. The WebSocket is its motion.** A view MUST read a
> resting value from Query and a moving value from the store, and MUST NOT poll REST for
> something the stream already delivers.

React Context is not on the list. A `token.delta` arrives several times a second; a Context value
changing at that rate re-renders every consumer beneath the provider, which is the whole reason
[`ARCHITECTURE.md §18.1`](./ARCHITECTURE.md#181-stack) chose Zustand.

### 6.2 TanStack Query contract

**Key registry.** All keys **MUST** come from `src/lib/queryKeys.ts`. Ad-hoc key arrays scattered
through components make invalidation unreviewable.

```ts
export const qk = {
  health:                          ["health"] as const,
  tasks:      (p: { skip: number; limit: number }) => ["tasks", p] as const,
  task:       (id: string)        => ["task", id] as const,
  taskRuns:   (id: string)        => ["task", id, "runs"] as const,
  run:        (id: string)        => ["run", id] as const,
  runEvents:  (id: string, after: number) => ["run", id, "events", after] as const,
  corpusDocs: (p: { skip: number; limit: number }) => ["corpus", "documents", p] as const,
  corpusSearch: (body: unknown)   => ["corpus", "search", body] as const,
  benchmarks:                      ["benchmarks"] as const,
  benchmarkResults: (suite: string) => ["benchmarks", suite, "results"] as const,
} as const;
```

**Cache policy.** Defaults are set once in `providers.tsx`; per-query overrides carry a comment.

| Query | `staleTime` | `refetchInterval` | Note |
|---|---|---|---|
| `task`, `tasks` | 10 s | 15 s **only while a listed run is active** | Polling a list of finished tasks is waste |
| `run` (live) | 0 | **none** | The socket is the update channel. Polling here is the anti-pattern this architecture exists to avoid |
| `run` (terminal) | `Infinity` | none | A terminal run never changes |
| `taskRuns` | 30 s | none | Invalidated by `run.completed` |
| `corpusDocs` | 60 s | none | Invalidated by ingest/delete mutations |
| `corpusSearch` | 5 min | none | A retrieval debug query is a pure function of its request |
| `benchmarkResults` | 60 s | 30 s while a suite run is in flight | |
| `health` | 15 s | 30 s | Feeds the dashboard's dependency strip |

**Global defaults**: `retry: 1` for reads; `retry: 0` for mutations (a retried `POST /runs` that
the `Idempotency-Key` does not cover is a second run); `refetchOnWindowFocus: false` on the run
view (a focus refetch mid-stream produces a visible flicker as REST state briefly overwrites
fresher stream state).

**Mutations.** `cancel`, `approve`, `resume`, task create, run start, corpus ingest/delete, and
benchmark run are mutations. Each **MUST** declare its invalidations up front:

| Mutation | Invalidates |
|---|---|
| `createTask` | `tasks` |
| `startRun` | `task(id)`, `taskRuns(id)`, `tasks` |
| `cancelRun` / `approveGate` / `resumeRun` | `run(id)` — the authoritative update still arrives over the socket; this is belt-and-braces for a socket that is mid-reconnect |
| `ingestDocument` / `deleteDocument` | `corpusDocs` |
| `runBenchmarkSuite` | `benchmarkResults(suite)` |

### 6.3 The Zustand run store schema

The store is the folded projection of one run's event stream. Every field below is written by
exactly one place — `applyEvent` — and read by panes through selectors.

```ts
export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export interface RunStreamState {
  // ── identity & connection ───────────────────────────────────────────────
  runId: string;
  status: StreamStatus;
  lastSeq: number;
  replayed: boolean;            // replay.complete seen — history vs live
  historyComplete: boolean;     // false after replay.gap or a ring eviction (§3.4)
  oldestAvailable: number | null;
  droppedEvents: number;        // evicted from the ring, for the truncation banner
  droppedConsoleLines: number;
  effectiveFilter: string[] | null;  // what was actually sent to `subscribe` (§3.5)
  error: StreamError | null;

  // ── run identity (seeded from REST, refreshed by run.snapshot) ──────────
  run: RunRead | null;
  phase: RunPhase | null;
  phaseHistory: { phase: RunPhase; ts: string }[];
  outcome: RunOutcome | null;
  terminal: boolean;
  workerId: string | null;
  modelRouting: Record<string, string>;   // role → model, from run.started
  queuePosition: number | null;           // from run.queued

  // ── graph (§8.5.3) ──────────────────────────────────────────────────────
  activeNode: string | null;
  nodeState: Record<string, {
    status: "pending" | "running" | "succeeded" | "failed" | "degraded";
    visits: number;
    lastDurationMs?: number;
    lastError?: string;
  }>;
  edgeTraversals: Record<string, number>; // "coder→sandbox_exec" → count
  lastEdge: string | null;

  // ── timeline (§8.5.4) ───────────────────────────────────────────────────
  timeline: TimelineEntry[];              // capped, one entry per node visit
  planSteps: PlanStepRow[];               // from plan.created / plan.revised
  planRevision: number;
  criteria: CriterionRow[];               // merged plan criteria + evaluation results
  verdict: VerdictRow | null;             // from evaluation.completed
  usage: { tokensIn: number; tokensOut: number; llmCalls: number; nodeVisits: number };
  budgets: Record<string, { used: number; limit: number; percent: number }>;

  // ── console (§8.5.5) ────────────────────────────────────────────────────
  consoleLines: ConsoleLine[];            // capped ring
  executions: SandboxExecutionRow[];      // sandbox.started/exit, one per revision
  truncations: { executionId: string; stream: string; bytesDropped: number }[];

  // ── artifacts & metrics (§8.5.6) ────────────────────────────────────────
  artifacts: ArtifactRow[];
  metrics: MetricPoint[];
  mlflow: MlflowRef | null;               // from run.completed / metric.logged
  bundleUrl: string | null;

  // ── code & retrieval (§8.7) ─────────────────────────────────────────────
  codeRevisions: CodeRevisionRow[];       // from code.revision (diff included)
  retrievalHits: RetrievalHitRow[];       // from retrieval.results
  diagnoses: DiagnosisRow[];              // derived from node.completed(debugger).summary

  // ── operator attention (§8.6) ───────────────────────────────────────────
  pendingGate: PendingGate | null;
  gateHistory: GateDecisionRow[];

  // ── raw, for the debug drawer only ──────────────────────────────────────
  events: RunEvent[];                     // capped ring; panes MUST NOT scan this
}
```

Actions are deliberately few — a large action surface is how a store starts being written from
five places:

```ts
interface RunStreamActions {
  reset(runId: string): void;
  setStatus(status: StreamStatus): void;
  setError(error: StreamError | null): void;
  setRun(run: RunRead): void;
  setEffectiveFilter(types: string[] | null): void;
  ingest(events: RunEvent[]): void;   // a BATCH — see §6.4
}
```

**Fold rules (normative):**

| Rule | |
|---|---|
| Events **MUST** be folded on arrival, never on render | Deriving a timeline by scanning 2000 events every frame is the same bug as an unbounded array, arriving later |
| `applyEvent` **MUST** be an exhaustive `switch` over `RunEvent["type"]` | With the discriminated union from [§4.3](#43-websocket-types-from-pydantic-payload-models), a new backend event becomes a compile error listing the file that must handle it |
| A fold **MUST** return only the slices it changed | Zustand's shallow comparison is what keeps a `token.delta` from re-rendering the artifact deck |
| Folds **MUST** be pure and idempotent per `seq` | Replay after reconnect re-delivers events. `node.completed` closing "the most recent open entry for this node" is idempotent; a blind `push` is not |
| Terminal events **MUST** set `terminal` and clear `activeNode` | A graph left with a node pulsing after the run ended is a lie about the system's state |
| `run.snapshot` **MUST** replace `run` wholesale, never merge | It is the authoritative body; a merge preserves stale fields that the gap invalidated |

Two folds are subtler than they look and are specified explicitly:

* **`node.completed` matching.** Loop 1 visits `coder` several times per run
  ([`AGENTS.md §6.1`](./AGENTS.md#61-loop-1--correctness-reflection)). The completion belongs to
  the visit that is still open, so the search **MUST** run from the end of the timeline backwards,
  never from the front.
* **Criteria merge.** `plan.created` supplies the contract (metric, comparator, threshold,
  required, weight); `evaluation.completed` supplies observations. They **MUST** be merged by
  `criterion_id`, and a criterion whose `observed` is `null` **MUST** render as **failed**, not
  as pending, once a verdict exists — absence is not success
  ([`AGENTS.md §7.6`](./AGENTS.md#76-evaluator-agent)).

### 6.4 Ring buffers and the frame batcher

The existing `ring()` helper copies the array on every append. At 5000 console lines that is a
5000-element copy per line, and a `train` run produces thousands of lines in bursts. Two changes,
both required before the console pane ships:

**1. Batch by animation frame.** The hook **MUST NOT** call `ingest` per message. It appends to a
module-level queue and flushes once per frame:

```ts
// hooks/useRunStream.ts — the queue is per-hook-instance, the schedule is per-frame.
const pending: RunEvent[] = [];
let frame: number | null = null;

function enqueue(event: RunEvent, flush: (batch: RunEvent[]) => void): void {
  pending.push(event);
  if (frame !== null) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    const batch = pending.splice(0, pending.length);
    if (batch.length) flush(batch);
  });
}
```

Store updates are then bounded at ~60/s regardless of stream rate, and one array copy serves a
whole burst. `ping`/`pong` and `interrupt.requested` **MUST** bypass the batcher — a heartbeat
answered a frame late is fine, but a gate notification is an operator-attention event and takes
the immediate path.

**2. Cap-aware append.** Within a batch, appends **MUST** compute the final slice once:

```ts
function ringPush<T>(buffer: T[], items: T[], limit: number): { next: T[]; dropped: number } {
  if (items.length === 0) return { next: buffer, dropped: 0 };
  const merged = buffer.length + items.length <= limit
    ? buffer.concat(items)
    : buffer.concat(items).slice(-limit);
  return { next: merged, dropped: Math.max(0, buffer.length + items.length - limit) };
}
```

`dropped` **MUST** accumulate into `droppedConsoleLines` / `droppedEvents` so the panes can say
how much history they no longer hold ([§3.4](#34-gap-recovery)).

When `document.visibilityState === "hidden"`, the batcher **SHOULD** fall back to a 250 ms timer
— browsers throttle `requestAnimationFrame` in background tabs to roughly 1 Hz, and a run that
finishes while the tab is backgrounded should not need a foreground repaint to become correct.

### 6.5 Selector discipline

| Rule | |
|---|---|
| A pane **MUST** subscribe via a selector, never `useRunStore()` bare | The bare call subscribes to the whole store; every event then re-renders every pane, which is precisely the failure Zustand was chosen to avoid. The current `useRunStream` does exactly this and **MUST** be amended |
| Multi-field selections **MUST** use `useShallow` | `useShallow((s) => ({ a: s.a, b: s.b }))`; without it the new object identity re-renders on every set |
| Selectors returning derived arrays **MUST** be memoized outside the component | A selector that `.filter()`s inside the component body returns a new array each call and defeats the comparison |
| Actions **MUST** be read from `useRunStore.getState()` in effects, not from a subscription | Actions are stable; subscribing to them re-runs the connect effect |
| Console filtering and search **MUST** happen in the pane, not the store | Filter state is per-viewer UI state; storing it would re-fold the buffer on every keystroke |

```ts
// hooks/useRunSlice.ts
export function useRunSlice<T>(selector: (state: RunStreamState) => T): T {
  return useRunStore(selector);
}
export const selectGraph = (s: RunStreamState) => ({
  activeNode: s.activeNode, nodeState: s.nodeState, edgeTraversals: s.edgeTraversals,
});
// usage: const graph = useRunStore(useShallow(selectGraph));
```

### 6.6 Crossing the boundary: events that invalidate queries

The two systems meet in exactly one place — a small effect inside `RunDeck` — and the mapping is
fixed:

| Event | Query invalidation | Why |
|---|---|---|
| `run.completed` / `run.failed` / `run.cancelled` | `qk.run(runId)`, `qk.taskRuns(taskId)`, `qk.tasks(*)` | The terminal body (`result`) now exists in Postgres, and the task list's status column changed |
| `artifact.created` | `qk.run(runId)` when `CAPABILITIES.runArtifacts` | Until then the store is the only source and no refetch helps |
| `evaluation.completed` | none | The verdict arrives complete in the payload; a refetch would fetch less |
| `interrupt.requested` | `qk.run(runId)` | `status` becomes `AWAITING_INPUT`, which the header reads from REST |

Invalidations **MUST** be debounced (250 ms) and **MUST NOT** fire during replay
(`replayed === false`) — a reconnect that replays 300 events would otherwise issue 300 refetches
of a run that has not changed since the first one.

---

## 7. The `useRunStream` contract

The complete interface. Panes are written against this and nothing else.

### 7.1 Signature

```ts
export function useRunStream(
  runId: string,
  options?: UseRunStreamOptions,
): RunStream;
```

Exactly one component per page may call it (`RunDeck`). Two live sockets for one run in one tab
consume two of the eight per-run connection slots and duplicate every fold.

### 7.2 Options

```ts
export interface UseRunStreamOptions {
  /**
   * Server-side `subscribe` filter (§9.5). Exact types, or a family prefix ending in
   * "." or "*". The hook ALWAYS unions this with the control floor (§3.5) — passing a
   * filter without "ping" would otherwise get the connection heartbeat-killed in ~40 s.
   * MUST be a stable reference (module constant); it is an effect dependency.
   */
  types?: readonly string[];

  /** false leaves the socket closed; REST-only views (the report page) pass false. */
  enabled?: boolean;

  /** Ring-buffer overrides. Default to the §3.7 caps; tests use small values. */
  eventLimit?: number;
  consoleLimit?: number;

  /** Called for every ingested event, after the fold. MUST be stable (useCallback). */
  onEvent?: (event: RunEvent) => void;

  /** Called once when the run reaches a terminal state, with the terminal payload. */
  onTerminal?: (outcome: RunOutcome, payload: TerminalPayload) => void;

  /** Called when a HITL gate opens. Used to raise the gate console (§8.6). */
  onGate?: (gate: PendingGate) => void;
}
```

### 7.3 Return value

```ts
export interface RunStream {
  // ── connection ──────────────────────────────────────────────────────────
  status: StreamStatus;              // "idle" | "connecting" | "open" | "reconnecting" | "closed"
  connected: boolean;                // status === "open"
  lastSeq: number;
  replayed: boolean;                 // replay.complete received
  historyComplete: boolean;          // false after a gap or an eviction
  reconnectAttempts: number;
  nextRetryInMs: number | null;      // for "reconnecting in 4s…" in the header
  effectiveFilter: string[] | null;
  error: StreamError | null;

  // ── run state (folded projections; identical to the store slices) ────────
  run: RunRead | null;
  phase: RunPhase | null;
  terminal: boolean;
  outcome: RunOutcome | null;
  activeNode: string | null;
  nodeState: RunStreamState["nodeState"];
  edgeTraversals: Record<string, number>;
  timeline: TimelineEntry[];
  planSteps: PlanStepRow[];
  criteria: CriterionRow[];
  verdict: VerdictRow | null;
  usage: RunStreamState["usage"];
  budgets: RunStreamState["budgets"];
  consoleLines: ConsoleLine[];
  executions: SandboxExecutionRow[];
  artifacts: ArtifactRow[];
  metrics: MetricPoint[];
  mlflow: MlflowRef | null;
  codeRevisions: CodeRevisionRow[];
  retrievalHits: RetrievalHitRow[];
  pendingGate: PendingGate | null;
  droppedEvents: number;
  droppedConsoleLines: number;

  // ── commands (§7.5) ─────────────────────────────────────────────────────
  cancel(reason?: string): Promise<CommandResult>;
  approve(gate: string, decision: GateDecision, notes?: string): Promise<CommandResult>;
  resync(): void;
  reconnectNow(): void;              // operator-initiated; resets backoff
}

export type GateDecision = "approve" | "reject";

export interface CommandResult {
  ok: boolean;
  via: "socket" | "rest";
  error?: string;
}
```

Returning the folded slices from the hook is a convenience for `RunDeck`, which needs a handful of
them for the header. Panes **MUST NOT** take the whole `RunStream` as a prop and destructure — that
re-renders the pane on every field. They subscribe to their own slice
([§6.5](#65-selector-discipline)).

### 7.4 Invariants

| # | Invariant |
|---|---|
| I1 | Exactly one `WebSocket` exists per mounted hook instance, and zero after unmount |
| I2 | `lastSeq` is monotonically non-decreasing for the lifetime of a `runId` |
| I3 | Every message with `seq > 0` is folded exactly once per delivery; folding is idempotent per `seq` |
| I4 | `status === "open"` implies the socket's `readyState === OPEN` |
| I5 | `terminal === true` implies no further reconnect attempt is scheduled |
| I6 | A `runId` change resets every buffer and the cursor before the first frame of the new run is folded |
| I7 | The hook never throws; every failure surfaces as `error` |
| I8 | Any `subscribe` sent includes the control floor |
| I9 | The store is written only through the hook's batcher; no other module calls `ingest` |

### 7.5 Command semantics and REST fallback

`cancel` and `approve` travel over the socket, because the API publishes them onto the run's Redis
control channel that the worker is already subscribed to — no round trip through Postgres. But the
socket is not always open, and this is the exact moment it matters least to be elegant: an operator
rejecting a `before_sandbox_exec` gate while the connection is mid-reconnect must not have the
click silently dropped, which is what today's fire-and-forget `send()` does.

Normative behaviour for both commands:

1. If `status === "open"`, send the frame and resolve `{ok: true, via: "socket"}`.
2. Otherwise **MUST** fall back to REST — `POST /runs/{id}/cancel` or `POST /runs/{id}/approve` —
   and resolve with `via: "rest"`.
3. On a REST `409` (run terminal, or not `AWAITING_INPUT`), resolve `{ok: false}` with the
   problem detail. The UI **MUST** show it; a gate that expired needs to say so.
4. The caller **MUST** treat the resolved promise as *delivery*, not *effect*. The authoritative
   consequence arrives as `run.cancelled` or the run leaving `AWAITING_INPUT`. Optimistic UI is
   limited to disabling the control and showing a pending state.

`resync` is socket-only (it has no REST equivalent); when closed it **MUST** be a no-op that
triggers `reconnectNow()` instead, since a reconnect performs a superset of a resync.

### 7.6 Error taxonomy

```ts
export interface StreamError {
  kind: "auth" | "forbidden" | "not_found" | "protocol" | "quota" | "network" | "server";
  message: string;                 // operator-facing, no stack traces
  code?: number;                   // WebSocket close code, when there was one
  retryable: boolean;
}
```

| Close / cause | `kind` | `retryable` | UI treatment |
|---|---|---|---|
| `4401` ×3 | `auth` | false | Full-pane error: token or ticket rejected |
| `4403` | `forbidden` | false | Full-pane error |
| `4404` | `not_found` | false | Route-level not-found |
| `4400` | `protocol` | false | Full-pane error, "report this" |
| `4429` | `quota` | true | Header banner: "too many open views of this run" |
| `1001` / `1011` / transport failure | `network` / `server` | true | Header badge only — reconnection is normal and MUST NOT blank the panes |

The last row is a product decision worth stating: **a reconnect must never clear the panes.** The
buffers hold what was already received; replay refills the gap. A view that empties on every
network blip is unusable on a laptop.

### 7.7 What the hook does not do

It does not fetch artifacts, reports, code, or task metadata; it does not poll REST; it does not
own filter or scroll state; it does not decide what a pane renders when a slice is empty; and it
does not retry commands. Each of those belongs to a pane or to Query, and putting any of them here
is how a 240-line hook becomes a 900-line one that nothing else can be tested without.

---

## 8. UI architecture and view hierarchy

### 8.1 App shell and route map

```
┌ header (h-12, Server Component) ───────────────────────────────────────────────┐
│ Pluton R&D Engine   Dashboard · Tasks · Corpus · Benchmarks      ● API ok  ⟳ ws │
├────────────────────────────────────────────────────────────────────────────────┤
│ main (flex-1, min-h-0)                                                         │
└────────────────────────────────────────────────────────────────────────────────┘
```

`min-h-0` on `main` is not decoration: without it a flex child with an internal scroll area grows
to its content height and the four-pane grid scrolls the page instead of the pane.

| Route | Component kind | Data sources | Section |
|---|---|---|---|
| `/` | Server shell + client cards | Query: `tasks`, `health`; WS: optional `run.` tail | [§8.2](#82-global-dashboard) |
| `/tasks` | Server shell + client table | Query: `tasks` | [§8.3](#83-task-list-and-task-detail) |
| `/tasks/new` | Client form | Mutations: `createTask`, `startRun` | [§8.4](#84-task-creation-and-configuration) |
| `/tasks/[taskId]` | Server shell + client list | Query: `task`, `taskRuns` | [§8.3](#83-task-list-and-task-detail) |
| `/runs/[runId]` | Server shell + `<RunDeck>` | **WS primary**, Query: `run` | [§8.5](#85-the-live-run-control-deck) |
| `/runs/[runId]/code` | Server shell + client viewer | Store (live) or Query `run.result` (terminal) | [§8.7](#87-code-revision-diff-viewer) |
| `/runs/[runId]/report` | Server-rendered Markdown | Query: `run` → `result`; `GET /runs/{id}/report` when available | [§8.8](#88-report-viewer) |
| `/corpus` | Client playground | Query: `corpusDocs`, `corpusSearch` | [§8.9](#89-corpus-inspector) |
| `/benchmarks` | Server shell + client tables | Query: `benchmarks`, `benchmarkResults` | [§8.10](#810-benchmark-scorecards) |

Cross-cutting requirements for every route:

* A `loading.tsx` that renders the *shape* of the page (pane frames, table headers), never a
  centred spinner. The run view's skeleton is the four-pane grid with empty panes.
* An `error.tsx` that reports the failure and offers a retry, and for a `404` from the API a
  `not-found` render rather than a generic error.
* Empty states carry the next action ("No tasks yet — create one"), not just an absence.
* Every route is usable at 1280×800. Below `lg`, the four-pane deck stacks
  ([§8.5.1](#851-layout-geometry)).

### 8.2 Global dashboard

**Purpose.** Answer, in one screen and without scrolling: is the platform healthy, what is running
right now, what happened recently, and is anything stuck.

```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│ ACTIVE RUNS  │ QUEUE DEPTH  │ SUCCESS RATE │ MEDIAN TIME  │
│      2       │      1       │  71% (7/10)  │    6m 41s    │
└──────────────┴──────────────┴──────────────┴──────────────┘
┌────────────────────────────────────┬───────────────────────┐
│ ACTIVE & QUEUED                    │ DEPENDENCIES          │
│ ● breast-cancer …  EXECUTE   62%   │ ● postgres  ● redis   │
│ ● churn-model   …  PLANNING   8%   │ ● qdrant    ● ollama  │
│ ○ queued: sentiment-baseline       │ ● mlflow              │
├────────────────────────────────────┴───────────────────────┤
│ OUTCOME TREND (last 30 runs)                               │
│ ▂▃▅▂▇▅▆▇▅▇  SUCCEEDED / PARTIAL / FAILED stacked by day     │
├────────────────────────────────────────────────────────────┤
│ RECENT RUNS  title · outcome · duration · criteria · when   │
└────────────────────────────────────────────────────────────┘
```

| Element | Source | Degradation |
|---|---|---|
| Stat tiles | Derived client-side from `GET /tasks?limit=100` | `CAPABILITIES.stats` false: derived counts are labelled *"from the most recent 100 tasks"*. When `/api/v1/stats` lands ([§15](#15-backend-work-this-plan-requires)) the tiles read it directly |
| Queue depth | `run.queued` payload `position` on any open stream, else the stats endpoint | Hidden entirely when neither is available — a wrong queue depth is worse than none |
| Active runs list | `tasks` filtered to `RUNNING`/`PENDING` | — |
| Live phase/percent per active run | One `useRunStream(runId, {types: RUN_ONLY})` per active row, capped at **4 concurrent** | Above 4, rows poll `qk.run(id)` at 10 s instead. The per-run WS quota is 8 and the deck itself uses one |
| Dependency strip | `GET /health/deep` every 30 s | `503` renders each failing service in `fail` tone with its message |
| Outcome trend | `tasks` grouped by day and status | Recharts stacked bars; follows the palette in [§10](#10-design-system) |
| Recent runs table | `tasks` sorted by `updated_at` | Row click → `/runs/{id}` |

`RUN_ONLY` is a module constant — `["run.", "node.started"]` unioned with the control floor — so
the dashboard's four background streams cost a handful of frames per minute rather than the full
token firehose. This is the one place the `subscribe` filter earns its complexity.

### 8.3 Task list and task detail

**`/tasks`** — a virtualized table (`total` can grow unbounded; `?skip=&limit=` paginates at 20).

| Column | Notes |
|---|---|
| Title | Link to `/tasks/{id}` |
| Status | `Dot` + label. `PENDING`/`RUNNING` pulse |
| Phase | Only for active rows, from the run store when a stream is open |
| Created / Updated | Relative ("4m ago") with an absolute `title` attribute |
| Actions | **Open run** when a run exists; **Start run** otherwise |

Filters (client-side over the fetched page, plus server params when they exist): status, free-text
title. `task_kind` and `tags` filters are specified but **MUST** stay hidden until `TaskCreate`
carries those fields ([§15](#15-backend-work-this-plan-requires)).

**`/tasks/[taskId]`** — the prompt in full, the task's metadata, and `GET /tasks/{id}/runs`
rendered as a run history: attempt, outcome, duration, criteria score, links to the run view, the
report, and the code browser. A **Start run** button posts with a fresh `Idempotency-Key` (a
UUIDv4 generated at click time, held for the mutation's lifetime so a React retry cannot double
it), and on `202` navigates straight to `/runs/{run_id}` — the operator's next question is always
"what is it doing", and making them find the link is friction with no upside.

A `409` from that POST means a run is already active: the UI **MUST** surface the message and
offer to open the existing run.

### 8.4 Task creation and configuration

**`/tasks/new`** — one form, submitted as two calls (`POST /tasks`, then `POST /tasks/{id}/runs`),
because that is the shape of the API. A failure between them leaves a created task with no run;
the UI **MUST** say so and offer **Start run** rather than silently retrying the pair.

```
┌ WHAT ─────────────────────────────────────────────────────────────┐
│ Title            [ Breast cancer classifier beating 95% accuracy ] │
│ Task kind        [ tabular-classification ▾ ]        (§MLOPS 3.2)  │
│ Prompt           [ multiline, 8 rows, monospace                  ] │
│                  Guidance: state the dataset, the target metric,   │
│                  and the deliverables. The Planner turns this into │
│                  machine-checkable success criteria.               │
│ Tags             [ sklearn ×] [ portfolio ×] [ + ]                 │
├ HOW ──────────────────────────────────────────────────────────────┤
│ Sandbox profile  ( ) exec   (•) train   ( ) train-tracked          │
│                  train: 4 CPU · 6 GiB · 900 s · network none       │
│ Model overrides  planner   [ default: qwen2.5:14b-instruct   ▾ ]   │
│                  coder     [ default: qwen2.5-coder:7b       ▾ ]   │
│                  (researcher, debugger, evaluator, reporter …)     │
├ LIMITS ───────────────────────────────────────────────────────────┤
│ Debug iterations [ 4 ]  Replans [ 2 ]  Node visits    [ 60 ]       │
│ Sandbox execs    [ 12 ] Wallclock [ 1800 s ]  Max tokens [250000]  │
├ SUPERVISION ──────────────────────────────────────────────────────┤
│ [ ] after_plan               Pause after planning, before any work │
│ [x] before_sandbox_exec      Review code before it runs            │
│ [ ] before_model_registration Approve registry writes              │
│ [ ] on_replan                Approve a change of approach          │
│ Gates expire after 30 min and expiry is treated as rejection.      │
├───────────────────────────────────────────────────────────────────┤
│                                   [ Create only ]  [ Create & run ]│
└───────────────────────────────────────────────────────────────────┘
```

| Field | Type | Default | Validation | Source of truth |
|---|---|---|---|---|
| `title` | string | — | 3–255 chars | `TaskCreate` |
| `prompt` | text | — | ≥ 5 chars; warn under 40 with "a thin prompt produces a thin plan" | `TaskCreate` |
| `task_kind` | enum | `tabular-classification` | one of the eight kinds | [`MLOPS.md §3.2`](./MLOPS.md#32-json-schema) |
| `tags` | string[] | `[]` | ≤ 10, ≤ 32 chars each | §8.3 target contract |
| `sandbox_profile` | enum | `train` | `exec` \| `train` \| `train-tracked` | [`ARCHITECTURE.md §10.3`](./ARCHITECTURE.md#103-execution-profiles) |
| `model_overrides` | record | `{}` | role ∈ the agent roster; model string free-form | [`ARCHITECTURE.md §11.1`](./ARCHITECTURE.md#111-role-to-model-table) |
| `budgets.*` | int | §14.5 defaults | positive; each field's max is 4× the default, with an inline warning above 2× | [`AGENTS.md §3.2`](./AGENTS.md#32-value-objects) `Budgets` |
| `hitl_gates` | string[] | `[]` | subset of the four gates | [`AGENTS.md §9`](./AGENTS.md#9-human-in-the-loop-gates) |

Normative form behaviour:

* Every non-default value **MUST** be visually marked as overridden, and a **Reset to defaults**
  control **MUST** restore all of them. Silent divergence from platform defaults is how a run's
  behaviour becomes unexplainable a week later.
* Budget fields **MUST** show the default inline as placeholder text, not as a pre-filled value —
  a pre-filled field submits a "custom" budget identical to the default and obscures intent.
* Gate checkboxes **MUST** state the consequence, including the 30-minute timeout and that timeout
  equals rejection ([`AGENTS.md §9`](./AGENTS.md#9-human-in-the-loop-gates)). An operator who
  ticks a gate and walks away has cancelled their own run.
* **Until `CAPABILITIES.runConfig` is true**, the HOW / LIMITS / SUPERVISION sections **MUST**
  render read-only with a single banner: *"Run configuration is not yet accepted by
  `POST /tasks/{id}/runs`; these are the platform defaults this run will use."* Showing editable
  controls whose values are discarded is worse than showing none.
* Draft persistence: the form **SHOULD** persist to `uiStore`/`localStorage` per keystroke-debounce
  so a refresh does not lose a long prompt.

### 8.5 The live run control deck

The product. Everything else in the platform exists to feed this screen.

#### 8.5.1 Layout geometry

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ HEADER  ● RUNNING · EXECUTE · 04:12 · 62%          [Cancel] [Resync] [⋮]        │
│ breast-cancer-classifier   debug 1/4 · replans 0/2 · tokens 41k/250k · ws ●     │
├──────────────────────────────┬─────────────────────────────────────────────────┤
│ ① AGENT GRAPH                │ ② TIMELINE & CRITERIA                           │
│    fixed-layout SVG          │    virtualized node trace                       │
│    live node + edge state    │    + criteria ledger (pinned footer)            │
│    38% width · 46% height    │    62% width · 46% height                       │
├──────────────────────────────┴─────────────────────────────────────────────────┤
│ ③ CONSOLE                              [all|stdout|stderr|tokens] [search] [⇩] │
│    virtualized, follow-tail                                        54% height  │
├────────────────────────────────────────────────────────────────────────────────┤
│ ④ ARTIFACTS  main.py · metrics.json · confusion_matrix.png · MLflow ↗          │
└────────────────────────────────────────────────────────────────────────────────┘
```

| Rule | |
|---|---|
| The deck **MUST** fill the viewport exactly: `h-[calc(100vh-3rem)]`, no page scroll | Panes scroll internally. A page-level scrollbar next to a live log is a usability failure |
| Grid: `grid-rows-[auto_minmax(0,46fr)_minmax(0,54fr)_auto]` | `minmax(0, …)` is required or a grid row refuses to shrink below content height and the console pushes the artifact bar off-screen |
| Panes 1 and 2 share a row; panes 3 and 4 are full width | Graph and timeline are read together; the console needs width for long tracebacks |
| Splits **MUST** be draggable and persisted in `uiStore` | Watching a training log and watching a graph are different sessions |
| Below `lg` (1024 px) panes stack vertically in order ①②③④, each with a fixed height and its own scroll | A four-pane grid on a laptop half-screen is four unusable panes |
| Each pane **MUST** be independently collapsible | An operator watching a long `train` execution wants the console at full height |
| Pane headers **MUST** carry a live count | "TIMELINE · 11 steps", "CONSOLE · 2,431 lines (312 dropped)" |

`RunDeck` is the only component holding the stream. It renders the header and the four panes, wires
`onGate` to the gate console ([§8.6](#86-human-in-the-loop-gate-console)), and passes **no stream
data as props** — panes subscribe to slices themselves.

#### 8.5.2 Header and operator controls

| Element | Source | Behaviour |
|---|---|---|
| Status dot + label | `run.status`, `status_detail`, `phase` | `QUEUED` idle · `RUNNING` pulsing · `AWAITING_INPUT` warn, pulsing · terminal tones per outcome |
| Phase chip | `phase` + `phaseHistory` | Hover shows the phase sequence with durations |
| Elapsed | `run.created_at` → now, frozen at terminal | Ticks at 1 Hz via a single interval, not per-render |
| Progress | `percent` from the summary; else `planSteps` completed ÷ total | Never both — mixing two progress definitions produces a bar that goes backwards |
| Budget chips | `usage` + `budgets` | Turn `warn` at 80% (the threshold `budget.warning` fires at) and `fail` at 100% |
| Connection badge | `status`, `nextRetryInMs`, `historyComplete` | "reconnecting in 4s…"; a distinct marker when history is incomplete |
| **Cancel** | `stream.cancel(reason)` | Confirmation dialog; cooperative — the label after click is *"Cancel requested — the run stops at the next node boundary"*, because that is what the engine actually does |
| **Resync** | `stream.resync()` | Requests `run.snapshot`; also the recovery for a suspected fold bug |
| **Resume** | `POST /runs/{id}/resume` | Shown only for `INTERRUPTED` / `AWAITING_INPUT`; `409` surfaces the problem detail |
| Overflow ⋮ | — | Copy run id · Open MLflow · Download bundle (gated) · Open report · Open code · Toggle debug drawer |

The debug drawer is a developer affordance and **MUST** stay behind the overflow menu: raw
envelopes from the `events` ring with type/seq/timestamp filtering, and a copy-as-JSONL action.
When a pane disagrees with reality, this is the only tool that says which side is wrong.

#### 8.5.3 Pane 1 — Agent graph visualizer

**What it must show.** The graph from [`AGENTS.md §4.1`](./AGENTS.md#41-full-graph) with live
state: which node is executing now, which have completed, which failed, how many times each was
visited (the reflection loops in [`AGENTS.md §6`](./AGENTS.md#6-reflection-loops-and-termination)
revisit `coder` and `sandbox_exec` repeatedly), and which edge was traversed last.

**Rendering decision.** The topology is *static and known at build time* — twelve nodes, a fixed
edge set, registered in `backend/app/engine/graph.py`. Therefore:

| Rule | |
|---|---|
| The live graph **MUST** be a hand-laid-out SVG driven by `lib/graphTopology.ts` | Fixed coordinates, no layout engine |
| It **MUST NOT** re-run a Mermaid render on state change | Mermaid re-parses and replaces the DOM subtree per render (tens to >100 ms); at one `node.started` every few seconds that is a visible stutter and it destroys any CSS transition |
| State changes **MUST** be expressed as class/attribute changes on existing nodes | So a node lights up with a transition instead of the graph flickering |
| Mermaid **MAY** still render the *static* topology in documentation views | Its strength is documents, not live state |

This deviates from [`ARCHITECTURE.md §18.1`](./ARCHITECTURE.md#181-stack), which lists Mermaid for
diagrams. The deviation is recorded in [§16](#16-deviations-and-open-questions) with a proposed
amendment.

```ts
// lib/graphTopology.ts — transcribed from AGENTS.md §4.1 / engine/graph.py.
// A test asserts this table matches the backend's registered nodes (§13).
export interface GraphNodeDef {
  id: string;                       // "coder"
  label: string;                    // "Coder"
  phase: RunPhase;                  // AGENTS.md §4.3 mapping
  kind: "llm" | "deterministic" | "gate";
  model?: string;                   // display default; overridden by run.started routing
  x: number; y: number;             // fixed layout coordinates
}
export interface GraphEdgeDef {
  from: string; to: string;
  kind: "normal" | "loop" | "failure" | "gate";
  label?: string;                   // routing predicate, AGENTS.md §5
}
```

| Signal | Event | Visual |
|---|---|---|
| Active node | `node.started` | Accent border, pulsing halo, node scaled 1.04 |
| Completed | `node.completed` | `ok` tone; duration and token badge |
| Degraded | `node.completed` with `degraded: true` | `warn` tone + a "fell back" marker — the deterministic fallback path ran, which is materially different from success |
| Failed | `node.failed` | `fail` tone; error kind on hover; retry counter from `node.retrying` |
| Visits | count of `node.started` per node | Small "×3" badge; only shown above 1 |
| Edge traversal | consecutive `node.started` pairs | Edge tinted by traversal count; the most recent edge animates a dash offset once |
| Pending | never started | `idle` tone at 40% opacity |
| Gate | `interrupt.requested` | `hitl_gate` node pulses `warn`; clicking it opens the gate console |

Interactions: hovering a node shows a tooltip with role, model in use (from `run.started`'s
`model_routing`), visit count, cumulative duration, and last summary. Clicking scrolls the
timeline pane to that node's most recent entry and filters the console to it. The pane
**MUST** render correctly with zero events — the topology is known before the run starts, and an
empty graph is a better first paint than a spinner.

Accessibility: the SVG carries `role="img"` with an `aria-label` naming the active node and phase,
plus an off-screen `<ol>` listing node states so the pane is not opaque to a screen reader.

#### 8.5.4 Pane 2 — Execution timeline and criteria ledger

Two stacked regions in one pane: a scrolling trace on top, the criteria ledger pinned to the
bottom at its natural height (it is never long — a plan carries a handful of criteria — and it is
the answer to "is this working", so it must not scroll out of view).

**Timeline.**

```
 ✓ planner          6.1s    412 tok   4-step plan · 2 required criteria
 ✓ researcher      12.4s   1.2k tok   sufficient · 6 chunks
 ✓ coder rev 1     38.9s   1.9k tok   142 lines
 ✗ sandbox_exec     4.2s              ValueError: could not convert string to float
 ✓ debugger        11.0s    780 tok   confidence 0.87 · shape mismatch on column 'diagnosis'
 ✓ coder rev 2     31.2s   1.7k tok   +6 −3 lines
 ▶ sandbox_exec    running…           exec e2 · train profile
```

| Column | Source |
|---|---|
| Status glyph | `node.started` / `completed` / `failed` / `retrying`, `degraded` flag |
| Node + revision | `node`, plus `code.revision` correlation for `coder` |
| Duration | `duration_ms`, live-ticking while running |
| Tokens | `tokens_in` + `tokens_out` from `node.completed` |
| Summary | `summary` from `node.completed`, or `error.message` from `node.failed` |

Requirements: entries **MUST** be grouped under their plan step when `plan_step_id` is present,
with the step title as a sticky sub-header and its `StepStatus`; a running entry **MUST** show a
live elapsed counter driven by one shared 1 Hz interval, not one timer per row; `node.retrying`
**MUST** annotate the existing entry rather than adding a row; the list **MUST** virtualize above
200 entries and auto-scroll only when already at the bottom
([§9.2](#92-virtualization-rules)); clicking an entry filters the console to that node's window,
and clicking a `sandbox_exec` entry filters to its `execution_id`.

**Criteria ledger.** The machine-checkable scorecard. It is the platform's objectivity claim made
visible, so its rules are strict.

```
── CRITERIA ────────────────────────────  score 0.97 · ACCEPT ──
 ✅ accuracy    ≥ 0.95    0.9737   required   w 1.0
 ✅ f1_macro    ≥ 0.94    0.9712   required   w 1.0
 ⚠️ roc_auc     ≥ 0.99    0.9948   optional   w 0.5   stretch goal
 ⏳ auc_pr      ≥ 0.90       —      required   awaiting metrics.json
```

| Rule | |
|---|---|
| Required and optional criteria **MUST** be visually distinct and required ones sorted first | `required=false` criteria are aspirational ([`AGENTS.md §3.2`](./AGENTS.md#32-value-objects)) and a failed optional criterion is not a failed run |
| The comparator **MUST** be rendered literally (`≥ 0.95`, `≈ 0.5 ±0.01`) | "Target 0.95" hides whether the bound is inclusive, and `approx` carries a tolerance |
| `observed === null` after a verdict **MUST** render as **failed**, annotated *"metric absent from metrics.json"* | Absence is not success ([`AGENTS.md §7.6`](./AGENTS.md#76-evaluator-agent)) |
| Before any verdict, a criterion with no observation is **pending**, not failed | During `PLANNING` nothing has been measured yet |
| The score **MUST** be labelled as the weighted criteria satisfaction, and the decision badge (`ACCEPT`/`REFINE`/`REPLAN`/`ABORT`) shown next to it | The two are different quantities and operators conflate them |
| The advisory rubric **MUST** be presented as advisory, in a collapsed disclosure | The LLM rubric never influences `passed` ([`AGENTS.md §7.6`](./AGENTS.md#76-evaluator-agent)); rendering it beside the hard result implies otherwise |
| A `plan.revised` **MUST** visibly reset the ledger with a "criteria revised (revision N)" marker | Comparing against a silently changed contract is how a REPLAN looks like a regression |

#### 8.5.5 Pane 3 — Live terminal and event stream

The high-throughput pane, and the one that decides whether the tab survives a long run.

| Concern | Rule |
|---|---|
| Virtualization | TanStack Virtual, **always** — not "above N lines". Row height is fixed (`leading-5`, 20 px) so measurement never runs |
| Sources | `sandbox.stdout`, `sandbox.stderr`, `token.delta`, plus synthetic markers for `sandbox.started` / `sandbox.exit` / `sandbox.truncated` |
| Filters | Segmented control: **all · stdout · stderr · tokens**; plus node and `execution_id` filters set by the graph and timeline panes |
| Search | Debounced 150 ms, case-insensitive substring; match count, next/previous, matches highlighted in place. Regex behind a toggle, compiled in a `try/catch` — an invalid partial regex must not throw during typing |
| Follow tail | On by default; **MUST** auto-disable when the user scrolls up and re-enable on scroll to bottom. A **Jump to latest (N new)** pill appears while detached |
| Truncation | `sandbox.truncated` renders an inline `warn` divider: *"— 1.2 MB of stdout dropped: the output cap was reached —"*. Client-side eviction renders a sticky top banner: *"N earlier lines are no longer held"* |
| Token deltas | Rendered as a single soft-wrapped growing block per node, not one row per delta. A delta appended to the last token line **MUST** mutate that row, not push a new one, or a 4000-token plan produces 4000 rows |
| ANSI | Stripped by default via `lib/ansi.ts`; a toggle renders SGR colours. Raw escapes in a virtualized row break alignment |
| Long lines | Truncated to 4 KiB by the server; the client soft-wraps and offers per-line **expand** |
| Copy / download | Copy visible, copy all held, download as `.log`. Download is a client-side `Blob`, so it needs no backend endpoint |
| Timestamps | Off by default, toggleable, `HH:MM:SS.mmm` from the event `ts` |
| Keys | `data-seq` on every row; React keys are the line id, never the array index |
| Empty state | *"No output yet — the sandbox has not started."* Not a spinner |

Colour: `stdout` is default foreground, `stderr` is `fail`-tinted, tokens are `muted` italic,
markers are `warn`. Anything that is not program output **MUST** be visually distinguishable from
program output; a UI that renders its own notices in the same style as a traceback teaches
operators to distrust both.

#### 8.5.6 Pane 4 — Artifact and deliverable deck

```
ARTIFACTS · 6                                    MLflow: run-b41e7c2a ↗  [bundle ⇩]
┌────────────┬────────────┬────────────┬────────────┐
│ main.py    │metrics.json│ confusion  │ model/     │
│ code 2.9KB │ metrics    │ _matrix.png│ .joblib    │
│ rev 2      │ 7 metrics  │  [preview] │ sklearn    │
└────────────┴────────────┴────────────┴────────────┘
accuracy 0.9737 ▁▂▄▇  ·  f1_macro 0.9712  ·  train 1.83s  ·  peak RSS 312 MB
```

| Source | Feeds |
|---|---|
| `artifact.created` | Live tiles: `artifact_id`, `name`, `type`, `size_bytes`, `sha256`, `download_url` |
| `metric.logged` | Metric strip and sparklines (`key`, `value`, `step`) |
| `run.completed` payload | `deliverables[]`, `bundle_url`, `mlflow`, `evaluation` — merged with live tiles by `name`, de-duplicated |
| `RunRead.result` (terminal, via Query) | The same body for a run that finished before this tab opened |

Requirements:

* Tiles group by the `Deliverable.artifact_type` vocabulary — `code`, `model`, `plot`, `report`,
  `metrics`, `log`, `bundle` ([`AGENTS.md §3.2`](./AGENTS.md#32-value-objects)) — in that order,
  because that is the order a person looks for them.
* Plots **MUST** preview inline. Since an `<img>` tag cannot carry an `Authorization` header, the
  preview source **MUST** be the Next Route Handler `app/api/artifact/[artifactId]/route.ts`,
  which attaches the bearer token server-side and streams the bytes. Putting the token in a query
  string instead would place a credential in browser history and proxy logs.
* `metrics.json` **MUST** render as a parsed table — dataset identity (`id`, `sha256`, `n_samples`,
  split, seed), params, metrics, runtime, and baseline comparison — following
  [`MLOPS.md §3.3`](./MLOPS.md#33-reference-instance). A raw JSON blob in a pane this small helps
  nobody; a **view raw** toggle covers the rest.
* Metric sparklines come from `metric_series` when present, else from the sequence of
  `metric.logged` events for that key. A single-point series **MUST** render as a value, not a
  degenerate one-pixel chart.
* MLflow deep links use `MLflowRef.ui_url` when the payload carries it, else compose
  `${NEXT_PUBLIC_MLFLOW_BASE}/#/experiments/{experiment_id}/runs/{run_id}`. Both parent
  (`run-{run_id[:8]}`) and the winning child (`attempt-{n:03d}`) **MUST** be linked separately —
  the hierarchy in [`MLOPS.md §4.1`](./MLOPS.md#41-three-level-hierarchy) is the whole point of the
  nesting, and only linking the parent hides the per-attempt comparison.
* A registered model (`registered_model`, `model_version`) **MUST** show its name, version, and
  alias, linking to the registry page ([`MLOPS.md §7`](./MLOPS.md#7-model-registry)).
* **Download bundle** and per-artifact download are gated on `CAPABILITIES.runBundle` /
  `artifactDownload` and render disabled-with-reason until those endpoints exist.
* Every artifact tile shows the first 12 characters of its `sha256` with copy-on-click. Artifact
  identity is what makes a claim reproducible.

### 8.6 Human-in-the-loop gate console

A gate is the one moment the platform is *waiting on a person*, so it is the one thing the UI may
interrupt for. It renders as a dialog over the deck plus a persistent header banner, and the
`hitl_gate` node pulses in the graph until it is resolved.

| Gate | Payload rendered | Decisions |
|---|---|---|
| `after_plan` | Plan steps with kind, dependencies, dataset bindings, assumptions, and the criteria table | approve · reject · *edit_criteria* |
| `before_sandbox_exec` | Full source (highlighted, with a diff against the previous revision when one exists), the `ValidationReport` (rejections, warnings, imports seen, `writes_metrics_json`), the profile and its limits | approve · reject · *approve_once* |
| `before_model_registration` | Metrics, model size, and the comparison to the current champion | approve · *skip_registration* |
| `on_replan` | Failure history and the proposed new direction, with a diff of old vs new plan | approve · *abort* |

Normative behaviour:

* The dialog **MUST** show the countdown to `expires_at` and state that **expiry is treated as
  rejection** and terminates the run `CANCELLED`
  ([`AGENTS.md §9`](./AGENTS.md#9-human-in-the-loop-gates)). A silent countdown that ends a run is
  indefensible.
* A **notes** field (≤ 2000 chars) is submitted with the decision and appears in the report's
  narrative.
* Submission goes through `stream.approve()` — socket first, REST fallback
  ([§7.5](#75-command-semantics-and-rest-fallback)). Controls disable while pending; the dialog
  closes only when the run actually leaves `AWAITING_INPUT`, not on the response.
* Decisions in *italics* above are in the target contract but **not accepted by
  `RunApproveRequest`, which validates `^(approve|reject)$`**. They **MUST** be hidden (not
  shown-and-broken) until the backend accepts them ([§15](#15-backend-work-this-plan-requires)).
* The dialog **MUST** be dismissible without deciding — an operator needs to read the console
  before approving. Dismissal leaves the header banner and the graph pulse; the gate is not
  resolved by closing a window.
* Resolved gates append to `gateHistory` and render in the timeline as a first-class entry
  (`⏸ gate after_plan — approved by operator, 14s`).

### 8.7 Code revision diff viewer

`/runs/[runId]/code`, and embedded in the `before_sandbox_exec` gate.

| Concern | Rule |
|---|---|
| Data | `code.revision` events (`revision`, `path`, `sha256`, `lines_changed`, `diff`) held in the store; for a terminal run, from `RunRead.result.deliverables` plus the artifact proxy |
| Selection | A revision rail (rev 1 · rev 2 · rev 3 ★) with left/right pickers; default compares the last two |
| Diff | Side-by-side by default, unified toggle. Computed with `lib/diff.ts` (a ~120-line LCS over lines) — no dependency for something this small, and the server already sends a `diff` for the adjacent-revision case |
| Highlighting | Shiki, **server-side**, through `app/api/highlight/route.ts`, keyed and cached by `sha256`. Grammars must not enter the client bundle ([`ARCHITECTURE.md §18.1`](./ARCHITECTURE.md#181-stack)) |
| Context | Each revision shows its `rationale` and the `addresses_error` fingerprint it was written against, and links to the debugger's `Diagnosis` (root cause, evidence, fix strategy, confidence) from the same iteration |
| Correlation | The failing region from `ErrorRecord.offending_source` (± 5 lines) **MUST** be highlighted in the *previous* revision, so "what went wrong" and "what changed" are on the same screen |
| Fallback | When no `code.revision` events were captured (run predates the emitter, or history was evicted), show the final `main.py` from artifacts with a note that intermediate revisions are unavailable |

This view is the clearest evidence the platform self-corrects, so the rationale and diagnosis are
not decoration — a diff without them is just a patch.

### 8.8 Report viewer

`/runs/[runId]/report` renders `REPORT.md` — the primary human deliverable
([`AGENTS.md §7.8`](./AGENTS.md#78-reporter-agent)).

| Concern | Rule |
|---|---|
| Source | `GET /runs/{id}/report` when `CAPABILITIES.runReport`; otherwise the `report`-typed deliverable fetched through the artifact proxy; otherwise the deterministic fallback rendering below |
| Rendering | Server Component, Markdown → HTML at request time; GFM tables required (the criteria table is a table). Sanitised: the document contains model-generated text and **MUST NOT** be rendered as raw HTML |
| Structure | The eight mandated sections are rendered with a sticky table of contents. A missing section **MUST** be shown as missing rather than silently omitted — section 4 (what went wrong) is mandatory, and its absence is a reporter defect worth seeing |
| Criteria table | Cross-checked against the run's `evaluation`. A mismatch between the narrative table and the machine verdict **MUST** surface a warning banner; the numbers are generated from state and must match |
| Artifacts | Relative image paths resolved through the artifact proxy so plots render inline |
| Actions | Copy Markdown · download `.md` · print stylesheet (A4, light) · link to the MLflow run |
| No report | For a `FAILED` run with no report, render the failure dossier from `run.failed` (`error`, `last_node`, `dossier_url`) rather than an empty page |

### 8.9 Corpus inspector

`/corpus` — two halves, backed by endpoints that exist today.

**Documents.** `GET /corpus/documents` in a table: title, collection, `chunk_count`, `source_uri`,
`ingested_at`, metadata. Ingest via `POST /corpus/documents` (title, text, `source_uri`,
collection, tags, metadata) with a `409` on duplicate `sha256` surfaced as *"this document is
already ingested"*, not as an error. Delete with confirmation stating that the document's Qdrant
points go with it. Ingestion targets `rd_corpus` or `code_exemplars` only — `run_memory` is written
exclusively by the Reporter after a **successful** run and the collection selector **MUST** reflect
that asymmetry rather than offering an option the API rejects.

**Retrieval playground.** `POST /corpus/search`, mirroring exactly what the Researcher sees.

| Control | Field |
|---|---|
| Query | `query` |
| Collection | `rd_corpus` · `code_exemplars` · `run_memory` |
| Top-K | `top_k`, 1–50, default 6 |
| Task kind | `task_kind` filter |

Results render as ranked cards: score, `trust_level` badge (`curated` / `verified` / `untrusted`),
`source_uri`, `section`, and the chunk text with query terms highlighted. `run_memory` results are
the `run_memory` fix inspector the platform needs: each hit shows the error fingerprint, the fix
summary, and — when the metadata carries a `run_id` — a link back to that run. Queries and their
results are kept in a session history strip so two phrasings can be compared side by side; that
comparison is the entire purpose of a retrieval playground.

Hybrid-retrieval explain fields (`dense_rank`, `sparse_rank`, `rrf_score`, `took_ms`) from
[`ARCHITECTURE.md §8.3`](./ARCHITECTURE.md#83-key-payloads) are **not** returned by
`CorpusSearchResponse` today. The UI **MUST NOT** invent them; the fusion breakdown column is
gated and listed in [§15](#15-backend-work-this-plan-requires).

### 8.10 Benchmark scorecards

`/benchmarks` — suites from `GET /benchmarks`, results from `GET /benchmarks/{suite}/results`,
execution via `POST /benchmarks/{suite}/run` (optionally a case subset).

**KPI scorecard.** `BenchmarkKpis` rendered against the platform targets in
[`AGENTS.md §13.1`](./AGENTS.md#131-platform-kpis) and
[`ARCHITECTURE.md §17`](./ARCHITECTURE.md#17-performance-targets):

| KPI | Target | Rendering |
|---|---|---|
| `task_success_rate` | ≥ 0.70 | Gauge with the threshold marked; tone by pass/fail |
| `judgement_score` | ≥ 2/3 trap cases | Trap cases listed individually — a trap case is a deliberate impossibility, and an aggregate hides which one was fumbled |
| `mean_debug_iterations` | ≤ 1.5 | Value + distribution |
| `first_pass_rate` | — | Value + trend |
| `replan_rate` | ≤ 0.25 | Value + trend |
| `expectations_met` / `cases_scored` | — | Ratio |

**Results table.** Per case: `case_id`, pass/fail, outcome, duration, metrics, and the `checks`
array expanded to show which expectation failed. Rows link to `/runs/{run_id}` when `run_id` is
set — the value of a benchmark table is being one click from the run that produced the number.

**Historical comparison.** Results are de-duplicated to the latest per case by the API; the view
**MUST** offer a run-over-run comparison of the same suite across `created_at`, showing per-case
transitions (pass→fail is the row that matters) and the KPI deltas. Triggering a suite disables
the control while in flight and polls `benchmarkResults` at 30 s until the count stabilises.

---

## 9. Performance and virtualization controls

A twenty-minute run at 40 tok/s with a chatty training log is not a stress test; it is the median
case. These controls are what keep it from freezing the tab.

### 9.1 Frame budget

| Budget | Value | Enforced by |
|---|---|---|
| Store commits | ≤ 60/s regardless of stream rate | The rAF batcher ([§6.4](#64-ring-buffers-and-the-frame-batcher)) |
| Main-thread work per commit | < 8 ms at 2000 console lines held | Profile in the React DevTools Profiler; a regression fails review |
| Re-rendering components per `token.delta` | **1** (the console's virtual window) | Slice selectors ([§6.5](#65-selector-discipline)) |
| Re-rendering components per `node.started` | ≤ 3 (graph, timeline, header) | idem |
| Time to first meaningful paint on `/runs/[runId]` | < 400 ms on a warm cache | Server-rendered shell; panes render their empty state immediately |
| Sustained event rate handled without dropped frames | ≥ 200 events/s | The load test in [§13](#13-testing-and-quality-gates) |

### 9.2 Virtualization rules

| Rule | |
|---|---|
| Console: virtualized **always**, fixed 20 px rows, `overscan: 20` | Fixed height avoids per-row measurement, which is the expensive part of a virtualizer |
| Timeline: virtualized above 200 entries | Below that, measurement costs more than it saves |
| Task, corpus, and benchmark tables: virtualized above 200 rows | idem |
| Auto-scroll **MUST** be conditional on being at the bottom (within 8 px) | Auto-scrolling away from what someone is reading is the classic log-viewer bug |
| Virtualized rows **MUST** be pure components with stable keys and `React.memo` | An unmemoized row re-renders the whole visible window per commit |
| A virtualized container **MUST** have an explicit height from the grid | `height: 100%` inside an `auto` row collapses to zero and renders nothing — with no error |

### 9.3 Render-cost rules

* Derived values (filtered console lines, grouped timeline entries, criteria sort order) **MUST**
  be memoized on their inputs. Filtering 5000 lines per keystroke without memoization is a visible
  freeze.
* Formatting helpers (`formatDuration`, `formatBytes`, `formatMetric`) **MUST** be pure and
  allocation-light; they run per visible row per frame.
* The graph **MUST NOT** re-mount on state change — attribute updates only
  ([§8.5.3](#853-pane-1--agent-graph-visualizer)).
* Live counters (elapsed, "running for") **MUST** share one 1 Hz interval published through the
  store or a context, never one `setInterval` per row.
* Charts **MUST** receive pre-aggregated data. Recharts re-computing scales over 4000 points every
  frame is the same bug wearing a different hat; downsample to ≤ 300 points for display.
* Images in the artifact deck **MUST** be lazy (`loading="lazy"`) and thumbnailed by CSS, not by
  loading full-resolution plots into a 96 px tile.
* `console.log` in a hot path is a performance bug. Debug output goes through a `DEBUG`-gated
  logger in `lib/log.ts`.

### 9.4 Bundle budget

| Budget | Value |
|---|---|
| First-load JS for `/` | ≤ 160 KB gzipped |
| First-load JS for `/runs/[runId]` | ≤ 260 KB gzipped |
| Any single client chunk | ≤ 120 KB gzipped |

Shiki (server-only), Recharts (dynamically imported by the chart components), and the report
Markdown renderer (server-only) stay out of the run view's bundle. `next build` prints per-route
first-load JS; exceeding a budget requires either a dynamic import or a justification in the pull
request.

### 9.5 Measuring it

[`ARCHITECTURE.md §17`](./ARCHITECTURE.md#17-performance-targets) sets one target the frontend
alone can measure: **WebSocket event end-to-end < 150 ms p95** (worker `XADD` → client receipt).
The hook **SHOULD** compute `Date.now() - Date.parse(event.ts)` per event, keep a reservoir of the
last 200 samples, and expose p50/p95 in the debug drawer. Clock skew makes the absolute number
soft; the distribution's shape and its changes are what matter, and the alternative — no client
instrumentation at all — leaves that row of §17 unmeasurable.

The debug drawer **SHOULD** also show: events/s, commits/s, held buffer sizes, dropped counts,
reconnect count, and the effective subscribe filter. Every one of those has been the answer to a
"the UI is stuck" report at some point in this kind of system.

---

## 10. Design system

Dark by default. This dashboard is watched for twenty minutes at a stretch while a run executes,
and a bright background is the wrong thing to do to someone doing that.

**Tokens** live in `globals.css` under Tailwind v4's `@theme` (there is no `tailwind.config.js` in
v4). The palette is already established and **MUST NOT** be extended casually:

| Token | Value | Meaning |
|---|---|---|
| `--color-ink` | `#0b0d11` | Page ground |
| `--color-surface` | `#12151c` | Pane ground |
| `--color-raised` | `#1a1f28` | Cards, dialogs, hovered rows |
| `--color-line` | `#262c38` | Borders, dividers |
| `--color-fg` | `#e6e9ef` | Primary text |
| `--color-muted` | `#8b93a4` | Secondary text, labels |
| `--color-running` | `#4c9aff` | In progress |
| `--color-ok` | `#3ecf8e` | Succeeded, criterion met |
| `--color-warn` | `#f2c94c` | Degraded, optional miss, budget ≥ 80%, gate waiting |
| `--color-fail` | `#f2695c` | Failed, criterion missed, stderr |
| `--color-idle` | `#5a6376` | Pending, never visited |

| Rule | |
|---|---|
| State **MUST NOT** be encoded by colour alone | Every status carries a glyph or label. `✓ ✗ ▶ ⏸ ⚠` plus text; the five status colours include a red/green pair, and a red/green-only scorecard is unreadable for a meaningful fraction of engineers |
| Numbers **MUST** be tabular | `font-variant-numeric: tabular-nums` on every counter, or the timeline jitters each tick |
| Monospace for machine text | Code, logs, hashes, metric values, ids |
| Metric precision **MUST** be 4 significant decimals, never rounded for display | The report is generated from state and must match; a UI that shows `0.97` next to a report that says `0.9737` looks like a discrepancy |
| Density is a feature | 12–13 px base in panes, `leading-5` rows. This is an instrument panel, not a marketing page |
| Motion **MUST** be ≤ 200 ms and disabled under `prefers-reduced-motion` | The only continuous animation permitted is the active-node pulse and the connection badge |
| Charts follow the categorical/sequential rules in the repo's data-visualisation guidance | Outcome categories map to `ok`/`warn`/`fail`; metric curves use a single-hue sequential ramp |

Vendored shadcn/ui components are restyled to these tokens on the way in — a component still
carrying default shadcn greys is a review comment, not a merge.

---

## 11. Accessibility and interaction standards

| Requirement | |
|---|---|
| Keyboard | Every control reachable and operable by keyboard. The deck defines: `g` graph · `t` timeline · `c` console · `a` artifacts · `/` console search · `f` toggle follow-tail · `Esc` close dialog. Shortcuts **MUST NOT** fire while a text input has focus |
| Focus | Visible focus rings on the dark ground (2 px, `running` tone). Dialogs trap focus and restore it on close |
| Live regions | The gate dialog is `role="alertdialog"`. Run status changes announce through a polite live region. The console **MUST NOT** be a live region — announcing thousands of lines is hostile |
| Contrast | Body text ≥ 4.5:1, large/secondary ≥ 3:1 against its own surface. `muted` on `surface` **MUST** be verified, not assumed |
| Semantics | Tables are `<table>`; the timeline is an ordered list; the graph is `role="img"` with a labelled off-screen state list |
| Reduced motion | Pulses become static tone changes |
| Zoom | Usable at 200% browser zoom: the deck collapses to the stacked layout rather than clipping |
| Copy | Every id, hash, and path is copy-on-click with a confirmation toast |

---

## 12. Setup, dependencies, and commands

### 12.1 Dependencies to add

Current `package.json` already carries `next`, `react`, `@tanstack/react-query`,
`@tanstack/react-virtual`, `zustand`, `recharts`, `mermaid`, `clsx`, `tailwindcss`,
`openapi-typescript`, `eslint`, and `typescript`.

| Package | Scope | For |
|---|---|---|
| `shiki` | dependency (server-only import) | Code highlighting in the diff viewer and gate ([§8.7](#87-code-revision-diff-viewer)) |
| `react-markdown` + `remark-gfm` | dependency (Server Component) | Report rendering with GFM tables ([§8.8](#88-report-viewer)) |
| `zod` | dependency | Form validation in `/tasks/new`, and runtime validation of untrusted `metrics.json` before it is rendered as a table |
| `date-fns` | dependency | Relative timestamps. `Intl.RelativeTimeFormat` **MAY** be used instead to avoid the dependency |
| `vitest` + `@testing-library/react` + `jsdom` | dev | Unit and store tests ([§13](#13-testing-and-quality-gates)) |
| `@playwright/test` | dev | End-to-end against a recorded stream |
| `msw` | dev | REST and WebSocket mocking in tests |
| `@typescript-eslint/*`, `eslint-plugin-react-hooks` | dev | `no-explicit-any` as an error; exhaustive-deps as an error |

`mermaid` stays only if a static topology diagram is rendered somewhere; if
[§8.5.3](#853-pane-1--agent-graph-visualizer)'s fixed-layout SVG is the only graph in the app, it
**SHOULD** be removed — an unused 500 KB dependency is a bundle risk waiting for someone to import
it casually.

### 12.2 Makefile targets to add

`fe-install` and `fe-dev` exist. The rest of the frontend lifecycle has no targets, and
`scripts/gen_event_types.py` documents `make gen-event-types` / `make check-event-types` targets
that do not exist — a documentation defect this section closes.

```make
.PHONY: openapi
openapi: ## Dump backend/openapi.json from the FastAPI app (no server needed)
	$(PY) scripts/dump_openapi.py

.PHONY: openapi-check
openapi-check: ## Fail if backend/openapi.json is stale
	$(PY) scripts/dump_openapi.py --check

.PHONY: fe-types
fe-types: openapi ## Generate frontend REST types from the OpenAPI document
	cd frontend && npx openapi-typescript ../backend/openapi.json -o src/lib/api.d.ts

.PHONY: check-fe-types
check-fe-types: ## Fail if the committed REST types have drifted
	cd frontend && npx openapi-typescript ../backend/openapi.json -o /tmp/api.d.ts \
	  && diff -u src/lib/api.d.ts /tmp/api.d.ts

.PHONY: gen-event-types
gen-event-types: ## Generate frontend WebSocket protocol types from events.py
	$(PY) scripts/gen_event_types.py

.PHONY: check-event-types
check-event-types: ## Fail if the committed WS types have drifted
	$(PY) scripts/gen_event_types.py --check

.PHONY: fe-lint
fe-lint: ## Lint the frontend
	cd frontend && npm run lint

.PHONY: fe-typecheck
fe-typecheck: ## tsc --noEmit
	cd frontend && npm run typecheck

.PHONY: fe-test
fe-test: ## Frontend unit and store tests
	cd frontend && npm run test

.PHONY: fe-e2e
fe-e2e: ## Playwright end-to-end against recorded stream fixtures
	cd frontend && npm run test:e2e

.PHONY: fe-build
fe-build: ## Production build
	cd frontend && npm run build

.PHONY: fe-check
fe-check: check-fe-types check-event-types fe-lint fe-typecheck fe-test fe-build ## Everything CI runs for the frontend
```

`fe-check` and `openapi-check` **MUST** be added to the repository's aggregate `check` target so a
backend rename that breaks the dashboard fails the backend's own pipeline.

### 12.3 Environment variables

| Variable | Scope | Default | Note |
|---|---|---|---|
| `NEXT_PUBLIC_API_BASE` | browser | `http://localhost:8000` | Absolute, because the WebSocket cannot traverse a Next.js rewrite (see `next.config.ts`) |
| `NEXT_PUBLIC_API_TOKEN` | browser | empty | The shared platform token. Browser-visible by construction; acceptable only under the single-user model in [`ARCHITECTURE.md §13.2`](./ARCHITECTURE.md#132-authentication-and-cors) |
| `PLATFORM_API_TOKEN` | **server only** | — | Used by Route Handlers (artifact proxy, highlighter). **MUST NOT** carry the `NEXT_PUBLIC_` prefix |
| `NEXT_PUBLIC_MLFLOW_BASE` | browser | `http://localhost:5001` | For composing deep links when a payload lacks `ui_url` |
| `NEXT_PUBLIC_DEBUG_STREAM` | browser | `0` | Enables the debug drawer's verbose logging |

`CORS_ORIGINS` on the backend **MUST** include the dashboard's origin (`http://localhost:3000`);
a wildcard with credentials is invalid per the CORS spec and is tracked as backend defect D-007.

### 12.4 Build verification

```bash
# From the repository root
make fe-install                 # npm install
make fe-types gen-event-types   # regenerate both halves of the typed contract
make fe-check                   # drift gates + lint + typecheck + unit tests + build

# From frontend/
npm run dev                     # http://localhost:3000, turbopack
npm run build && npm start      # production build, then serve on :3000
npm run typecheck               # tsc --noEmit
npm run lint
npm run test                    # vitest
npm run test:e2e                # playwright
```

`package.json` gains `"test": "vitest run"`, `"test:watch": "vitest"`, and
`"test:e2e": "playwright test"`.

A build is verified when: `next build` succeeds with zero TypeScript errors and zero ESLint
errors; every route in [§8.1](#81-app-shell-and-route-map) renders; per-route first-load JS is
within [§9.4](#94-bundle-budget); and `make fe-check` is green from a clean clone with no backend
running — which is exactly why the OpenAPI document is committed rather than fetched.

---

## 13. Testing and quality gates

| Layer | Tool | Must cover |
|---|---|---|
| Protocol | Vitest + a mock `WebSocket` | Every line of the [§3.8](#38-protocol-conformance-checklist) checklist, one test each. This is the highest-value suite in the app: reconnection bugs are invisible in development and constant in use |
| Store folds | Vitest | Every `applyEvent` case against a recorded event fixture; idempotency under replay (fold a sequence twice, assert identical state); ring eviction counts; the `node.completed` backward-match with three `coder` visits; criteria merge with a missing metric |
| Hook | Vitest + Testing Library | Mount/unmount socket lifecycle; Strict Mode double-mount leaks no socket; `runId` change resets buffers; command REST fallback when closed |
| Components | Testing Library | Each pane renders empty, loading, populated, and error states; the criteria ledger's absent-metric case; console filter and search |
| Contract | Vitest | `graphTopology.ts` matches a fixture exported from `backend/app/engine/graph.py`; every `EventType` has a store case; every `EventType` has a `PAYLOADS` entry (backend test) |
| E2E | Playwright | Submit a task → watch a run → approve a gate → read the report, driven by a **recorded stream fixture** (a JSONL of real events) rather than a live backend, so it runs in CI in seconds and is deterministic |
| Load | Vitest/bench | 200 events/s for 60 s: assert commits/s ≤ 60, held buffers at their caps, and no unbounded growth in retained memory |
| A11y | `@axe-core/playwright` | Zero critical violations on every route |

Recording fixtures: `make record-stream RUN=<run_id>` dumps `GET /runs/{id}/events?after_seq=0`
to `frontend/tests/fixtures/{name}.jsonl`. Fixtures **MUST** include at least one run with a
sandbox failure and a debug loop, one with a HITL gate, and one that hits `replay.gap` — the three
paths where the UI is most likely to be wrong and least likely to be exercised by hand.

Definition of done for any pane: renders all four states; virtualized where required; subscribes
to a slice, not the store; has a test for its empty and error states; and adds no unbudgeted
dependency.

---

## 14. Build order

Ordered so that each phase leaves the app runnable and each one unblocks the next. Estimates
assume one engineer.

| Phase | Work | Unblocks | Done when |
|---|---|---|---|
| **0. Typed contract** (0.5 d) | `dump_openapi.py`, `api.d.ts`, `rest.ts`, extend `events.py` payload models + generator, delete hand-written duplicates from `types.ts`, Makefile targets | Everything | `make fe-check` green; `tsc` clean with no `Record<string, unknown>` payload access left in the store |
| **1. Transport amendments** (1 d) | Control floor, rAF batcher, slice selectors, close-code policy, REST command fallback, `historyComplete` / dropped counters, error taxonomy | Every pane | The [§3.8](#38-protocol-conformance-checklist) suite passes |
| **2. Store extension** (1 d) | The full [§6.3](#63-the-zustand-run-store-schema) schema, exhaustive fold, capped rings | Panes 1–4 | Fold tests pass against fixtures, including replay idempotency |
| **3. Shell + dashboard** (1.5 d) | `app/page.tsx`, task list, task detail, nav, health strip, primitives (`Table`, `Tabs`, `Select`, `Dialog`, `Tooltip`) | Navigation exists | `next build` renders every route; a run can be started and opened |
| **4. Run deck skeleton** (1 d) | `RunDeck`, grid geometry, header + operator controls, connection badge, collapsible panes | Panes | Deck fills the viewport, no page scroll, cancel and resync work |
| **5. Console** (1.5 d) | Virtualized viewer, filters, search, follow-tail, truncation banners, ANSI, copy/download | The highest-volume pane | 200 events/s with no dropped frames |
| **6. Timeline + criteria ledger** (1.5 d) | Trace, step grouping, live counters, criteria scorecard, verdict, rubric disclosure | Interpretability | All ledger rules in [§8.5.4](#854-pane-2--execution-timeline-and-criteria-ledger) hold, including absent-metric |
| **7. Graph** (1.5 d) | `graphTopology.ts`, fixed-layout SVG, node/edge state, visit badges, tooltips, cross-pane selection | The signature view | Topology test passes; no re-mount on state change |
| **8. Artifacts + MLflow** (1 d) | Tiles, artifact proxy Route Handler, `metrics.json` table, sparklines, MLflow parent/child links | Deliverables | Plots preview; links resolve |
| **9. HITL console** (1 d) | Gate dialog per gate kind, countdown, notes, socket/REST submission, gate history | Steering | Approving and rejecting a gate both work end to end against a fixture |
| **10. Code + report** (1.5 d) | Diff viewer, server highlighter, revision rail, diagnosis context; report viewer with TOC and criteria cross-check | Deliverables | A run with three revisions is legible; report cross-check warns on mismatch |
| **11. Corpus + benchmarks** (1.5 d) | Playground, document table, KPI scorecard, results table, comparison | Full route map | Every listed endpoint exercised |
| **12. Hardening** (1 d) | A11y pass, bundle budgets, load test, E2E fixtures, empty/error states audit | Ship | [§13](#13-testing-and-quality-gates) fully green |

Phases 0–2 are prerequisites for everything and **MUST NOT** be reordered behind visible work; the
temptation to build a page first and fix the data layer later is exactly how a component ends up
re-deriving state that the store should own.

---

## 15. Backend work this plan requires

Frontend features blocked on backend surface, with the flag each one is gated behind. Ordered by
how much of this document they unblock.

| # | Backend work | Unblocks | Flag |
|---|---|---|---|
| B1 | Per-event Pydantic `PAYLOADS` registry in `schemas/events.py`, emitters migrated to construct them | The entire typed contract in [§4.3](#43-websocket-types-from-pydantic-payload-models) | — |
| B2 | Emit the domain events already specified in §9.4 but not yet produced: `run.phase`, `plan.created`, `plan.revised`, `code.revision`, `retrieval.results`, `artifact.created`, `metric.logged`, `evaluation.completed`, `interrupt.requested`, `budget.warning`, `tool.*`, `node.progress` | Criteria ledger, artifact deck, diff viewer, gate console, budget chips — i.e. most of [§8.5](#85-the-live-run-control-deck) | — |
| B3 | `GET /runs/{id}/artifacts`, `GET /artifacts/{id}`, `GET /artifacts/{id}/download` | Artifact deck for runs not watched live; plot previews | `runArtifacts`, `artifactDownload` |
| B4 | `GET /runs/{id}/report` | Report viewer without the artifact fallback | `runReport` |
| B5 | Accept the §8.3 body on `POST /tasks/{id}/runs` (`budgets`, `model_overrides`, `hitl_gates`, `sandbox_profile`) | The HOW / LIMITS / SUPERVISION sections of [§8.4](#84-task-creation-and-configuration) | `runConfig` |
| B6 | `task_kind` and `tags` on `TaskCreate` / `TaskRead` | Task kind and tag filters; experiment resolution shown pre-run | `runConfig` |
| B7 | `GET /runs/{id}/steps`, `GET /runs/{id}/evaluation` | Timeline and ledger for a historical run | `runSteps` |
| B8 | `GET /runs/{id}/bundle` | Download bundle | `runBundle` |
| B9 | Aggregate statistics endpoint — active runs, queue depth, success rate, median duration, outcome histogram by day | Dashboard tiles and trend without a 100-task client-side derivation | `stats` |
| B10 | Widen `RunApproveRequest.decision` beyond `approve\|reject` to include `edit_criteria`, `approve_once`, `skip_registration`, `abort`, per [`AGENTS.md §9`](./AGENTS.md#9-human-in-the-loop-gates) | The per-gate decisions in [§8.6](#86-human-in-the-loop-gate-console) | — |
| B11 | Return the hybrid-retrieval explain fields (`dense_rank`, `sparse_rank`, `rrf_score`, `took_ms`) from `POST /corpus/search` | Fusion breakdown in the playground | — |
| B12 | `GET /agents` (role, model, tools, prompt version) | An agent registry view, and accurate per-node model labels before `run.started` | `agentRegistry` |

None of these blocks starting: phases 0–7 of [§14](#14-build-order) depend only on B1 and B2, and
every pane has a specified degradation for the rest.

---

## 16. Deviations and open questions

**Deviations from `ARCHITECTURE.md §18`.** Each needs either an amendment there or a change here;
leaving them unresolved is how two documents start lying to different readers.

| # | Deviation | Rationale | Proposed resolution |
|---|---|---|---|
| D1 | §18.1 lists **Mermaid** for diagrams; [§8.5.3](#853-pane-1--agent-graph-visualizer) specifies a fixed-layout SVG for the live graph | Mermaid re-parses and replaces the DOM per render; at one transition every few seconds that stutters and forbids transitions. The topology is static and known | Amend §18.1 to "Mermaid for static topology documentation; hand-laid-out SVG for the live graph" |
| D2 | §18.4 gives `useRunStream` a four-field return; [§7.3](#73-return-value) returns the full folded projection plus commands | The four-pane deck needs graph, plan, criteria, artifact and gate state; re-deriving them per pane is the performance bug the store exists to prevent | Amend §18.4 to reference this section as the authoritative signature |
| D3 | §18.5 names `frontend/lib/api.d.ts`; the tree is `frontend/src/lib/api.d.ts` | The app uses `src/` | Correct the path in §18.5 |
| D4 | §8.3's `GET /runs/{id}` body (with `plan`, `progress`, `counters`, `evaluation`, `mlflow`, `deliverables`) is not what `RunRead` returns | Implementation drift; documented in [§2.3](#23-backend-surface-implemented-versus-specified) | Either implement the fuller body or correct §8.3. The frontend is written to survive either |
| D5 | §18.3's pane-to-event table omits `interrupt.requested`, `budget.warning`, `code.revision`, `retrieval.results` | Those events have panes in this document | Fold [§8.5](#85-the-live-run-control-deck)'s tables into §18.3 by reference |

**Open questions**, each with the default this document assumes until decided:

| # | Question | Assumed default |
|---|---|---|
| Q1 | Does a task get multiple runs (`attempt`), or does `run_id == task_id` stay? | The UI is built for many runs per task and degrades to one; the route map already separates `/tasks/[taskId]` from `/runs/[runId]` |
| Q2 | Should the dashboard open a WebSocket per active run, or should a multiplexed `/ws/runs` stream exist? | Per-run, capped at 4 concurrent ([§8.2](#82-global-dashboard)). A multiplexed endpoint would be cleaner if the platform ever runs more than a handful of concurrent runs |
| Q3 | Is a run's `metrics.json` reachable without an artifact endpoint? | No; the deck renders metrics from `metric.logged` and the terminal `result` until B3 lands |
| Q4 | Light theme? | Not in v1, except the report's print stylesheet |
| Q5 | Should the run view show the LangGraph checkpoint time-travel history ([`AGENTS.md §10`](./AGENTS.md#10-checkpointing-and-resume))? | Out of scope for v1; it needs a backend endpoint over `aget_state_history` and is a debugging tool, not an operator tool |

---

*This specification is normative for `frontend/`. Changes to it are changes to the contract: a
pull request that alters a MUST clause states what it replaces and why, the same way a prompt
change bumps its version.*

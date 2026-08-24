# Frontend Implementation Plan — Pluton R&D Engine Dashboard

> | | |
> |---|---|
> | **Status** | Living document. Transport and state layer built; pages not started. |
> | **Companion docs** | [`ARCHITECTURE.md §18`](./ARCHITECTURE.md#18-frontend-architecture) (stack choice and route map, summarised) · [`ARCHITECTURE.md §9`](./ARCHITECTURE.md#9-websocket-protocol) (the wire protocol this UI consumes) · [`notes.md`](../notes.md#phase-6--real-time-frontend-week-89) (Phase 6 status) |
> | **Scope** | The remaining implementation work under `frontend/`: pages, the four-pane live run view, and the type-generation wiring. It does not re-litigate the stack decisions in `ARCHITECTURE.md §18.1` — those are settled and already reflected in `package.json`. |

This is the plan for finishing the piece the rest of the platform has been built to feed: a
dashboard where watching a run live is more informative than reading the logs. The backend side
of that contract — the WebSocket protocol, the event schema, the REST endpoints — is complete and
tested. The frontend's data-consuming layer is also complete. What is missing is everything the
user actually looks at.

---

## Table of contents

1. [Scope and current state](#1-scope-and-current-state)
2. [Project structure](#2-project-structure)
3. [State management architecture](#3-state-management-architecture)
4. [The `useRunStream` hook](#4-the-userunstream-hook)
5. [Route map and page-by-page plan](#5-route-map-and-page-by-page-plan)
6. [The four-pane live run view](#6-the-four-pane-live-run-view)
7. [Design system](#7-design-system)
8. [Type generation workflow](#8-type-generation-workflow)
9. [Build checklist, in order](#9-build-checklist-in-order)
10. [Setup and build commands](#10-setup-and-build-commands)
11. [Testing and quality gates](#11-testing-and-quality-gates)
12. [Known gaps and follow-ups](#12-known-gaps-and-follow-ups)

---

## 1. Scope and current state

Reading the wrong starting assumption into this document is the single easiest way to waste the
first day of frontend work, so it states plainly: **this is not a greenfield app.** The data layer
— the part that is hardest to get right and most expensive to redo — is built, tested by
construction against the real wire protocol, and should be treated as stable API surface by
everything described below.

| Layer | Status | Where |
|---|---|---|
| WebSocket hook (reconnect, ticket auth, replay) | ✅ Built | `src/lib/useRunStream.ts` |
| Client store (ring buffers, event folding) | ✅ Built | `src/lib/runStore.ts` |
| Generated WS event types | ✅ Built | `src/lib/events.generated.ts` (regenerate via `scripts/gen_event_types.py`; Makefile target not yet wired — [§12](#12-known-gaps-and-follow-ups)) |
| REST client | ✅ Built | `src/lib/api.ts` |
| Hand-written view types | ✅ Built | `src/lib/types.ts` |
| TanStack Query provider | ✅ Built | `src/components/providers.tsx` |
| App shell (nav, root layout) | ✅ Built | `src/app/layout.tsx` |
| Base UI primitives (`Panel`, `Dot`, `Chip`, `Button`, `Empty`, formatters) | ✅ Built | `src/components/ui/primitives.tsx` |
| Run header (status, phase, budgets, cancel/resync) | ✅ Built | `src/components/run/RunHeader.tsx` |
| **Every page route** | ⬜ Not started | no `page.tsx` exists anywhere under `src/app` |
| Agent graph visualizer | ⬜ Not started | §6.2 |
| Timeline & criteria checkcard | ⬜ Not started | §6.3 |
| Streaming console | ⬜ Not started | §6.4 |
| Artifact / MLflow browser | ⬜ Not started | §6.5 |
| Generated REST types (`make fe-types`) | ⬜ Not started | §8 |

The work from here is almost entirely **pages and panes consuming an API that already exists.**
Resist the urge to touch `useRunStream.ts` or `runStore.ts` to make a component fit; if a
component needs a slice of state the store doesn't expose, extend the store's `applyEvent` switch
— it already has a case per event type — rather than re-deriving state in the component.

---

## 2. Project structure

```
frontend/
├── src/
│   ├── app/                          # Next.js 15 App Router
│   │   ├── layout.tsx                # ✅ root shell — nav, Providers
│   │   ├── globals.css               # ✅ Tailwind v4 theme tokens (§7)
│   │   ├── page.tsx                  # ⬜ Dashboard (§5)
│   │   ├── tasks/
│   │   │   ├── page.tsx              # ⬜ Task list
│   │   │   ├── new/page.tsx          # ⬜ Submission form
│   │   │   └── [taskId]/page.tsx     # ⬜ Task detail + run history
│   │   ├── runs/[runId]/
│   │   │   ├── page.tsx              # ⬜ THE LIVE RUN VIEW (§6)
│   │   │   ├── report/page.tsx       # ⬜ Rendered REPORT.md
│   │   │   └── code/page.tsx         # ⬜ Revision diff browser
│   │   ├── corpus/page.tsx           # ⬜ Ingested docs + retrieval playground
│   │   └── benchmarks/page.tsx       # ⬜ Suite results, KPI trends
│   ├── components/
│   │   ├── providers.tsx             # ✅ TanStack Query client
│   │   ├── run/
│   │   │   ├── RunHeader.tsx         # ✅ status strip
│   │   │   ├── GraphPane.tsx         # ⬜ §6.2
│   │   │   ├── TimelinePane.tsx      # ⬜ §6.3
│   │   │   ├── CriteriaChecklist.tsx # ⬜ §6.3
│   │   │   ├── ConsolePane.tsx       # ⬜ §6.4
│   │   │   └── ArtifactPane.tsx      # ⬜ §6.5
│   │   ├── tasks/
│   │   │   ├── TaskTable.tsx         # ⬜ §5
│   │   │   ├── TaskForm.tsx          # ⬜ §5
│   │   │   └── RunHistoryList.tsx    # ⬜ §5
│   │   └── ui/
│   │       └── primitives.tsx        # ✅ Panel, Dot, Chip, Button, Empty, formatters
│   │                                 #    ⬜ extend as pages need more (Table, Tabs, Select,
│   │                                 #    Dialog) — vendor each in when a page needs it, per
│   │                                 #    ARCHITECTURE.md §18.1; do not pull in the full shadcn
│   │                                 #    kit speculatively
│   └── lib/
│       ├── api.ts                    # ✅ REST client — extend per new endpoint used
│       ├── api.d.ts                  # ⬜ generated by `make fe-types` (§8)
│       ├── types.ts                  # ✅ hand-written view models
│       ├── events.generated.ts       # ✅ generated WS protocol types (§8)
│       ├── runStore.ts               # ✅ Zustand store
│       └── useRunStream.ts           # ✅ the WebSocket hook
├── public/
├── .env.example                      # ✅ NEXT_PUBLIC_API_BASE, NEXT_PUBLIC_API_TOKEN
├── next.config.ts                    # ✅ no rewrites — see its own header comment
├── tsconfig.json                     # ✅ strict, `@/*` → `./src/*`
├── package.json                      # ✅
└── postcss.config.mjs                # ✅
```

Every `⬜` above is this document's actual scope. Nothing under `app/` other than `layout.tsx` and
`globals.css` exists yet — that includes `page.tsx` at the root, so `next build` currently has no
route to render.

---

## 3. State management architecture

Two state systems, cleanly separated by what kind of state they own — this split is already built
and should not be blurred by a page reaching into the wrong one.

### 3.1 Server state — TanStack Query

Owns anything that comes from a REST call and does not change under the user's cursor: task lists,
task detail, benchmark results, corpus documents. `Providers` (`src/components/providers.tsx`) is
already mounted in the root layout with `staleTime: 15_000` and `refetchOnWindowFocus: false` —
deliberately generous, because the live run view is fed by the socket, not by refetching, and a
refetch storm on every alt-tab would re-request bodies the socket already superseded.

Pages that only read REST (task list, benchmarks, corpus) use `useQuery` directly against
`src/lib/api.ts`'s functions. There is no repository/hook-per-endpoint abstraction to build first —
`api.ts` already is that abstraction; wrap each call site in `useQuery({ queryKey, queryFn: () =>
api.listTasks(...) })` as pages are built.

### 3.2 Client state — the Zustand run store

Owns everything that arrives over the WebSocket for one run: the event ring buffer, the console
ring buffer, the derived timeline, the derived criteria table, the active/visited/failed node
sets, budgets, the pending HITL gate. This is `src/lib/runStore.ts`, and it is complete —
`EVENT_BUFFER_LIMIT = 2000`, `CONSOLE_BUFFER_LIMIT = 5000`, one `applyEvent` case per WS event
type, folding on arrival rather than on render (see the file's own header comment for why: scanning
2,000 events per frame to derive the timeline is the same performance bug as an unbounded array).

**Every pane reads a slice of this store, not the raw `events` array.** `TimelinePane` reads
`timeline`, `ConsolePane` reads `consoleLines`, `GraphPane` reads `activeNode`/`visitedNodes`/
`failedNodes`. None of them need to know an event stream exists — that is the entire point of
folding at ingest time, and it is why a `token.delta` arriving several times a second re-renders
only `ConsolePane`, not the other three panes.

### 3.3 Why not React Context

Already decided, stated here so it isn't relitigated per-page: a `token.delta` frame arrives
multiple times a second during code generation. A Context value changing that often re-renders
every consumer in the tree on every frame. Zustand's per-slice subscription means a component that
selects `state.consoleLines` re-renders on console updates and nothing else.

---

## 4. The `useRunStream` hook

**Already implemented** (`src/lib/useRunStream.ts`, 241 lines) to the exact spec `ARCHITECTURE.md
§18.4` and `§9.8` describe. This section documents the contract as built, so pages consume it
correctly rather than re-implementing pieces of it.

```typescript
export interface RunStream {
  run: RunDetail | null;
  events: RunEvent[];
  consoleLines: ConsoleLine[];
  timeline: TimelineEntry[];
  criteria: CriterionRow[];
  artifacts: ArtifactRow[];
  status: "connecting" | "open" | "reconnecting" | "closed";
  lastSeq: number;
  activeNode: string | null;
  visitedNodes: string[];
  failedNodes: string[];
  terminal: boolean;
  error: string | null;
  cancel: (reason?: string) => void;
  approve: (gate: string, decision: "approve" | "reject", notes?: string) => void;
  resync: () => void;
}

function useRunStream(
  runId: string,
  options?: { types?: string[]; enabled?: boolean },
): RunStream;
```

One call, at the top of `runs/[runId]/page.tsx`, feeds every pane:

```tsx
const stream = useRunStream(runId);
// <RunHeader stream={stream} />
// <GraphPane activeNode={stream.activeNode} visited={stream.visitedNodes} failed={stream.failedNodes} />
// <TimelinePane entries={stream.timeline} criteria={stream.criteria} />
// <ConsolePane lines={stream.consoleLines} />
// <ArtifactPane artifacts={stream.artifacts} />
```

### 4.1 Reconnection algorithm (implemented, verbatim from §9.8)

```
last_seq ← 0 ; backoff ← 500 ms
loop:
    ticket ← POST /api/v1/ws/tickets {run_id}
    ws     ← connect(/api/v1/ws/runs/{run_id}?ticket=…&after_seq=last_seq)
    on open:      backoff ← 500 ms
    on message m: if m.seq > 0 then last_seq ← m.seq
    on close c:   if c ∈ {1000} and run is terminal then exit
                  if c ∈ {4400, 4403, 4404} then exit with error
                  sleep(backoff + jitter) ; backoff ← min(backoff × 2, 30 s)
```

Two details the implementation gets right that are easy to get wrong: the cursor advances only on
`seq > 0` (control frames carry `seq: 0`; treating a `ping` as progress would skip real history on
reconnect), and the ticket is re-minted on every attempt (it is single-use — the server consumes it
at accept time, so a cached ticket works once and then fails every retry in the backoff loop,
which looks exactly like a server that is down).

### 4.2 Ticket auth

`api.wsTicket(runId)` calls `POST /api/v1/ws/tickets` before every connection attempt (initial and
every reconnect). On a token-less development box this call still succeeds — ticket minting does
not require `NEXT_PUBLIC_API_TOKEN` to be set — and if it fails for any other reason the hook
proceeds without a ticket rather than blocking the dashboard entirely, since a WS endpoint that
requires no token in development should not be made unreachable by a minting hiccup.

### 4.3 Replay and gap recovery

`after_seq` on the connection URL asks the server to replay everything since the client's last
known sequence number. `replay.complete` marks the boundary between history and live traffic (the
store's `replayed` flag). If the server can no longer satisfy a replay — the client has been
offline long enough that Redis Streams trimmed the backlog — it sends `replay.gap` followed by a
fresh `run.snapshot`; the hook resets its cursor to 0 and the store rebuilds from the snapshot
rather than from history it can no longer be given. No page-level code needs to handle this case
specially — it is inside the hook.

---

## 5. Route map and page-by-page plan

| Route | Status | Renders | Data |
|---|---|---|---|
| `/` | ⬜ | Dashboard: active runs, recent outcomes, success-rate sparkline, queue depth | `useQuery` over `GET /tasks` filtered client-side to recent runs, or a dedicated summary once one exists — start with the task list and derive |
| `/tasks` | ⬜ | Task list, filterable by status | `api.listTasks` |
| `/tasks/new` | ⬜ | Submission form: title, prompt, kind | `api.createTask`, then `api.startRun` on submit |
| `/tasks/[taskId]` | ⬜ | Task detail + its run history | `api.getTask` + `GET /tasks/{id}/runs` (add to `api.ts`) |
| `/runs/[runId]` | ⬜ | **The live run view** — §6 | `useRunStream(runId)` |
| `/runs/[runId]/report` | ⬜ | Rendered `REPORT.md`, criteria table, artifact links | REST-only; pass `{ enabled: false }` to `useRunStream` since this view has no need for the live socket |
| `/runs/[runId]/code` | ⬜ | Revision browser, diffs between attempts | `code.revision` events already land in `stream.events`; a first cut can filter those from a REST event backlog (`GET /runs/{id}/events`) rather than requiring a live connection |
| `/corpus` | ⬜ | Ingested documents, retrieval playground | `api.ts` needs `listDocuments`/`searchCorpus` added, backed by `GET /corpus/documents` and `POST /corpus/search` |
| `/benchmarks` | ⬜ | Suite results, KPI trends, per-case history | `api.ts` needs `listSuites`/`runSuite`/`getSuiteResults` added, backed by `GET /benchmarks`, `POST /benchmarks/{suite}/run`, `GET /benchmarks/{suite}/results` |

Build `/runs/[runId]` first. It is the product; every other route is bookkeeping around it, and
building it first means the graph/timeline/console/artifact components exist before the pages that
merely list runs need to link to something real.

`nav` in `layout.tsx` already links to `/`, `/tasks`, `/corpus`, `/benchmarks` — `/tasks/new` and
the `/runs/[runId]/*` routes are reached from within those pages, not the top nav.

---

## 6. The four-pane live run view

### 6.1 Layout

```
┌───────────────────────────────────────────────────────────────────────┐
│  Breast cancer classifier          ● RUNNING · EXECUTE · 04:12 · 62%  │   ← RunHeader (built)
│  debug 1/4 · replans 0/2 · tokens 41k                    [Cancel]     │
├──────────────────────────┬────────────────────────────────────────────┤
│  AGENT GRAPH              │  TIMELINE                                 │
│  (Mermaid, live)          │  ✓ planner        6.1s   412 tok          │
│                           │  ✓ researcher    12.4s   1.2k tok         │
│   planner ──▶ researcher  │  ✓ coder         38.9s   1.9k tok         │
│      │           │        │  ✗ sandbox_exec   4.2s   ValueError       │
│      ▼           ▼        │  ✓ debugger      11.0s   confidence 0.87  │
│   coder ◀──── debugger    │  ✓ coder (rev 2) 31.2s                    │
│      │  ▲                 │  ▶ sandbox_exec  running…                 │
│      ▼  │                 │                                           │
│  ◉ sandbox_exec           │  ── criteria ─────────────────────        │
│      │                    │  accuracy  ≥ 0.95    pending              │
│      ▼                    │  f1_macro  ≥ 0.94    pending              │
│   mlops ──▶ evaluator     │                                           │
├──────────────────────────┴────────────────────────────────────────────┤
│  CONSOLE                                          [stdout|stderr|all] │
│  Fitting 5 folds for each of 12 candidates, totalling 60 fits         │
│  [CV] C=0.01 ...................... accuracy 0.9451                   │
│  ▌                                                                    │
├───────────────────────────────────────────────────────────────────────┤
│  ARTIFACTS   main.py (2.9 KB) · metrics.json · confusion_matrix.png   │
└───────────────────────────────────────────────────────────────────────┘
```

`runs/[runId]/page.tsx` is a CSS grid: `RunHeader` full-width on top, a two-column row (graph |
timeline), then console, then artifacts — each pane is a `<Panel title="…">` from
`components/ui/primitives.tsx`, which already provides the bordered, scrollable, title-barred
shell every pane below sits inside. Each pane subscribes to a different slice of `useRunStream`'s
return value, which is why the protocol's `subscribe` filter exists: the artifact pane has no use
for 400 `token.delta` frames, so a future optimisation can pass `{ types: [...] }` scoped per
mounted pane — not required for a first build, where all panes share one connection.

### 6.2 Pane 1 — Agent graph visualizer

**Component:** `components/run/GraphPane.tsx`. **Library:** Mermaid (already a `package.json`
dependency; do not add React Flow unless a later requirement needs node-level interactivity beyond
highlighting — see the note below).

The graph is static structure (the topology never changes within a run) with dynamic styling
(which nodes are active/visited/failed changes every few seconds). Render it as:

```tsx
const definition = `
graph LR
  init --> planner --> researcher --> coder --> sandbox_exec
  sandbox_exec -->|failure| debugger --> coder
  sandbox_exec -->|clean| mlops --> evaluator
  evaluator -->|REFINE| coder
  evaluator -->|REPLAN| planner
  evaluator -->|ACCEPT| reporter --> finalizer
`;
```

with a `classDef` per status injected from `activeNode`/`visitedNodes`/`failedNodes` before each
re-render (Mermaid re-parses the whole definition on each call to `mermaid.render`, so build the
string with the current highlight classes rather than trying to mutate a rendered SVG in place).
Node ids in the definition must match the backend's own node names exactly — `init`, `planner`,
`researcher`, `coder`, `sandbox_exec`, `debugger`, `mlops`, `evaluator`, `reporter`, `finalizer` —
because those are the literal strings `node.started`/`node.completed`/`node.failed` events carry
in their `node` field, and `activeNode`/`visitedNodes`/`failedNodes` are built directly from them.

Debounce re-renders to the animation frame rather than firing one per event: `node.started` and
`node.completed` are the only events this pane cares about, and even those can arrive faster than
Mermaid's SVG re-layout is comfortable with during a fast-completing node.

**If interactivity is added later** (click a node to jump the timeline to its entry, drag to
rearrange, zoom on a large graph) migrate this one pane to React Flow — it is a
per-pane decision, not a project-wide one, since nothing else in the app renders a graph.

### 6.3 Pane 2 — Execution timeline & criteria checkcard

**Components:** `components/run/TimelinePane.tsx` (reads `stream.timeline`) and
`components/run/CriteriaChecklist.tsx` (reads `stream.criteria`), composed inside one `<Panel
title="Timeline">`.

`TimelineEntry` (already defined in `types.ts`) has everything a row needs: `node`, `status`
(`running | succeeded | failed | degraded`), `startedAt`, `durationMs`, `tokensOut`, `summary`,
`error`. Render as a simple list — a status glyph (`Dot` from primitives, toned by `status`), the
node name, `formatDuration(durationMs)`, `formatTokens(tokensOut)` — newest last, auto-scrolled to
bottom while `status === "running"` entries are present. `coder` can appear more than once in one
run (the correctness loop revisits it); render every entry, not a per-node dedup — the repeat *is*
the information (it tells the operator how many revisions this run took).

`CriterionRow` (`id`, `metric`, `comparator`, `threshold`, `required`, `observed?`, `passed?`)
starts populated from `plan.created`/`plan.revised` with `observed`/`passed` undefined ("pending"),
and fills in from `evaluation.completed`. Render as a compact table: metric name, comparator +
threshold (`≥ 0.95`), and a status cell that reads "pending" until `observed` is set, then shows
the observed value toned green/red by `passed`. Required criteria get visual weight (bold or a
marker) over optional ones — a run can `ACCEPT` with an optional criterion unmet, and the checkcard
should not read as failed when that happens.

### 6.4 Pane 3 — Live streaming console

**Component:** `components/run/ConsolePane.tsx`. **Library:** `@tanstack/react-virtual` (already a
dependency).

Reads `stream.consoleLines` (`ConsoleLine[]`, capped at 5,000 by the store — §3.2). **This pane
must use `useVirtualizer`, not a mapped list.** The store's own header comment states the
constraint plainly: a run emitting thousands of console lines must not grow an unbounded rendered
DOM list, or the tab freezes long before the run finishes. Virtualize on `consoleLines.length` as
the item count, a fixed or measured row height per line, and auto-scroll to the bottom on new
lines *unless* the user has manually scrolled up to read history — track that with a ref checking
whether the scroll container is within a few pixels of its bottom before each auto-scroll, and
suspend auto-scroll if not.

A `stream` toggle (`stdout | stderr | all`, per the layout mock) filters client-side on
`ConsoleLine.stream` — no server-side re-subscription needed, since all three stream kinds already
arrive over the one connection. Tone `stderr` lines and `token` lines (LLM output, distinguishable
by `stream === "token"`) differently — they are semantically different (agent reasoning vs.
subprocess output) even though both render as console text.

### 6.5 Pane 4 — Artifact & MLflow deliverable browser

**Component:** `components/run/ArtifactPane.tsx`. Reads `stream.artifacts` (`ArtifactRow[]`:
`name`, `type`, `size_bytes?`, `sha256?`, `download_url?`), populated incrementally from
`artifact.created` events and backfilled from `run.completed`'s deliverable list (see
`runStore.ts`'s `mergeDeliverables`, which already de-duplicates by name across both sources).

Render as a compact list — name, `formatBytes(size_bytes)`, a type badge — grouped or sorted with
`main.py` / `metrics.json` first and plots/model artifacts after, matching the "What you get back"
ordering in the root README. **`download_url` is not currently populated by the backend for most
artifacts** — there is no route serving the run bundle yet (D-024 in `notes.md`). Build this pane
against the `ArtifactRow` shape now (name, size, type render regardless), and wire the actual
download links once that route exists; do not block the pane on it. The MLflow side of "MLflow Run
Deliverable" is a single external link — `MLFLOW_PUBLIC_URL` (from `NEXT_PUBLIC_*` config, add
alongside the existing two env vars) plus the run's MLflow run ID, which `RunDetail` does not yet
expose — add `mlflow_run_id` to the backend's run-detail payload and `RunDetail` in `types.ts`
together, rather than guessing an MLflow URL client-side.

---

## 7. Design system

Tailwind v4 keeps tokens in CSS (`@theme` in `globals.css`), already defined — use these, don't
introduce new colors ad hoc:

| Token | Value | Use |
|---|---|---|
| `--color-ink` | `#0b0d11` | page background |
| `--color-surface` | `#12151c` | panel background |
| `--color-raised` | `#1a1f28` | hover/raised surfaces |
| `--color-line` | `#262c38` | borders |
| `--color-fg` | `#e6e9ef` | primary text |
| `--color-muted` | `#8b93a4` | secondary text |
| `--color-running` | `#4c9aff` | active/in-progress |
| `--color-ok` | `#3ecf8e` | success |
| `--color-warn` | `#f2c94c` | warning/degraded |
| `--color-fail` | `#f2695c` | failure |
| `--color-idle` | `#5a6376` | not-yet-reached |
| `--font-mono` | system mono stack | console, code, metric values |

Dark-only by design — see the file's own comment: this dashboard is watched for the length of a
run, and a bright background is the wrong choice for that. Do not add a light theme without a
product decision to do so.

`components/ui/primitives.tsx` already provides `Panel`, `Dot`, `Chip`, `Button`, `Empty`, and the
`formatDuration`/`formatBytes`/`formatTokens` helpers. Per `ARCHITECTURE.md §18.1`'s vendoring
approach, add to this file (or a sibling in `ui/`) only the primitive a page actually needs when it
needs it — `Table` for the task list, `Tabs` for the console's stream filter, `Select` for the
task-kind field on the submission form, `Dialog` for the cancel-confirmation prompt. Pulling in a
full shadcn component kit speculatively is exactly the premature abstraction this vendoring
approach exists to avoid.

---

## 8. Type generation workflow

Two independent generators, one built, one not yet wired to a Make target:

**WebSocket events — built.** `scripts/gen_event_types.py` reads the Pydantic event models in
`backend/app/schemas/events.py` and writes `frontend/src/lib/events.generated.ts`. Its header
already instructs `make gen-event-types`, but that target does not exist in the Makefile yet
(D-025 in `notes.md`) — add it as a thin wrapper (`python scripts/gen_event_types.py`), matching
the existing `gen-env-example`/`gen-dashboards` pattern.

**REST contract — not built.** `ARCHITECTURE.md §18.5` specifies `make fe-types` running
`openapi-typescript` against a live `/api/v1/openapi.json` into `frontend/src/lib/api.d.ts`.
`openapi-typescript` is already a `package.json` devDependency; nothing consumes it yet. Add:

```makefile
.PHONY: fe-types
fe-types: ## Regenerate frontend types from the live OpenAPI schema
	cd frontend && npx openapi-typescript http://localhost:8000/api/v1/openapi.json -o src/lib/api.d.ts
```

This needs the API running (`make dev` or `make up`) — it cannot be part of `make check`, which
runs without the stack up. Once generated, `api.d.ts` becomes the authority for response shapes
the hand-written `types.ts` currently guesses at; keep `types.ts` for the *derived* view models
(`ConsoleLine`, `TimelineEntry`, `CriterionRow` — folded client-side, no backend equivalent) and
let `api.d.ts` own anything that is literally a REST response body.

A backend field rename becomes a frontend compile error under this scheme rather than a runtime
`undefined` — the entire reason §18.5 specifies generation over hand-maintenance. Re-run both
generators after any change to `schemas/events.py` or a route's response model, before trusting a
page against them.

---

## 9. Build checklist, in order

Each step produces something runnable, so progress is visible rather than a pile of components
with nothing wired together until the end:

1. **`app/page.tsx`** — even a static placeholder. Without this, `next build` has no root route.
2. **`fe-types` Make target** (§8) — generate `api.d.ts` once, before writing pages that guess at
   response shapes.
3. **`app/tasks/new/page.tsx`** — the simplest possible page: a form, `api.createTask` +
   `api.startRun` on submit, redirect to `/runs/[runId]`. This is what makes every page after it
   testable against a real run instead of a mock.
4. **`components/run/GraphPane.tsx`, `TimelinePane.tsx`, `CriteriaChecklist.tsx`,
   `ConsolePane.tsx`, `ArtifactPane.tsx`** — the five panes, buildable and testable independently
   against a running task from step 3.
5. **`app/runs/[runId]/page.tsx`** — compose `RunHeader` (built) with the five panes into the grid
   in §6.1. This is the milestone: watching a real run live in the browser.
6. **`app/tasks/page.tsx`, `app/tasks/[taskId]/page.tsx`, `app/page.tsx`** (real dashboard, not the
   step-1 placeholder) — the list/detail views that link into the run view built in step 5.
7. **`app/runs/[runId]/report/page.tsx`, `app/runs/[runId]/code/page.tsx`** — the two secondary
   run views, REST-only.
8. **`app/corpus/page.tsx`, `app/benchmarks/page.tsx`** — the two standalone tools, lowest
   priority since neither blocks the run-watching experience that is the product's core claim.
9. **`gen-event-types` Make target** (§8) — wire it now if it wasn't needed earlier; needed the
   moment `schemas/events.py` changes again.
10. **The demo recording** — Phase 6's exit criterion in `notes.md`: a 60-second screen recording
    of a full run, committed as `docs/assets/demo.gif` and linked from the README. This is the
    last step for a reason — it should show the finished view, not a placeholder.

---

## 10. Setup and build commands

```bash
# From frontend/
npm install                 # installs Next 15, React 19, Zustand, TanStack Query/Virtual, Mermaid, Recharts

cp .env.example .env.local  # NEXT_PUBLIC_API_BASE defaults to http://localhost:8000
                             # NEXT_PUBLIC_API_TOKEN only if the backend has PLATFORM_API_TOKEN set

npm run dev                 # next dev --turbopack --port 3000 — needs the backend reachable
                             # at NEXT_PUBLIC_API_BASE; `make up-infra && make dev` in another
                             # terminal is the minimum backend to develop against

npm run typecheck           # tsc --noEmit — strict mode, run before every commit
npm run lint                # eslint .
npm run build                # next build — the CI gate; catches anything typecheck alone misses
npm run start                # next start --port 3000 — serves the production build
```

Or via the root `Makefile`, once used from the repository root:

```bash
make fe-install              # cd frontend && npm install
make fe-dev                  # cd frontend && npm run dev
```

The dashboard talks to the API over an **absolute URL** (`NEXT_PUBLIC_API_BASE`), not a Next.js
rewrite — `next.config.ts`'s own header comment explains why: the WebSocket upgrade at
`/api/v1/ws/runs/{id}` does not survive a dev-server rewrite, and keeping REST and the socket on
the same absolute origin means they cannot disagree about where the backend is.

---

## 11. Testing and quality gates

`npm run typecheck` and `npm run lint` are already real scripts — wire them into `make check`
alongside the backend's `lint`/`typecheck`/`test` once the frontend has enough surface area to be
worth gating on (there is little value in a CI job for a tree with one `page.tsx`). `npm run build`
is the strongest single signal before that point: TypeScript strict mode plus Next's own build-time
checks catch most of what a dedicated test suite would.

No component or integration test framework is chosen yet. When one is needed (once panes exist),
prefer testing `runStore.ts`'s `applyEvent` reducer in isolation over testing components — it is
pure, it is where the actual protocol-to-UI-state logic lives, and it needs no DOM. A store test
that feeds it a fixture sequence of real WS events (captured from a backend integration test) and
asserts on the resulting `timeline`/`criteria`/`consoleLines` catches more real bugs than a
component render test would, for less effort.

---

## 12. Known gaps and follow-ups

Tracked in `notes.md`'s defect catalogue, repeated here because they block or shape frontend work
specifically:

- **D-024 — no route serves the deliverable bundle.** `finalizer.py` writes `bundle.zip`; nothing
  in `api/v1/` serves it. `ArtifactPane`'s download links (§6.5) have nothing to point at until
  this route exists. Not a frontend-side fix.
- **D-025 — `fe-types`/`gen-event-types` Make targets don't exist yet**, despite the generators
  and the devDependency both existing. §8 and §9 (steps 2 and 9) are where this document schedules
  adding them.
- **`mlflow_run_id` is not on `RunDetail`.** Needed for the MLflow link in §6.5; add to the
  backend's run-detail response and to `types.ts` together.
- **No `GET /tasks/{id}/runs` call in `api.ts` yet** — the route exists (`api/v1/tasks.py`), the
  client wrapper does not. Needed for `/tasks/[taskId]` (§5, route map).

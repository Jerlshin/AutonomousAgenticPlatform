# Technical Decisions Log — Pluton R&D Engine

> Architecture Decision Records, rejected alternatives, known defects in the current tree, the risk
> register, and the revised roadmap.
>
> | | |
> |---|---|
> | **Status** | Living document. Append; do not rewrite history. |
> | **Last updated** | 2026-08-24 |
> | **Specs** | [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) · [`docs/AGENTS.md`](./docs/AGENTS.md) · [`docs/MLOPS.md`](./docs/MLOPS.md) |

---

## How to read this

Each ADR states the decision, the forces that produced it, what was given up, and what would
invalidate it. An ADR is superseded, never edited — if a decision changes, a new ADR supersedes the
old one and the old one gets a `Superseded by` line.

**Status values:** `Accepted` · `Superseded` (by a later ADR) · `Provisional` (revisit after benchmarking) · `Deferred`. Each ADR carries its status on the line below its heading.

---

## Table of contents

- [Architecture Decision Records](#architecture-decision-records)
- [Deviations from the original proposal](#deviations-from-the-original-proposal)
- [Rejected alternatives](#rejected-alternatives)
- [Known defects in the current tree](#known-defects-in-the-current-tree)
- [Risk register](#risk-register)
- [Revised roadmap](#revised-roadmap)
- [Open questions](#open-questions)

---

## Architecture Decision Records

### ADR-001 — LangGraph as the orchestration framework

**Status:** Accepted

**Decision.** Build the agent system on LangGraph's `StateGraph` with a Postgres checkpointer,
rather than CrewAI, AutoGen, or a hand-rolled orchestrator.

**Forces.** The workload is inherently cyclic — code → execute → fail → diagnose → code. It needs
durable state across a 20-minute run, resumption after a crash, and inspectable control flow.

**Why LangGraph.** Explicit graphs with typed state channels make control flow *readable and
testable*. Routers are pure functions over state, which means the entire control flow can be
unit-tested without a model — see [`AGENTS.md §12`](./docs/AGENTS.md#12-testing-strategy).
Checkpointing is first-class rather than bolted on. Human-in-the-loop interrupts are built in.

**Given up.** More boilerplate than CrewAI's role-based abstraction. LangGraph's API is still
moving; version pinning matters.

**Invalidated if.** LangGraph's checkpointing proves unreliable at scale, or the API churns badly
enough that pinning blocks security updates.

---

### ADR-002 — Dispatch/execute split with `arq`, not Celery, not in-request

**Status:** Accepted

**Decision.** The API enqueues; a separate worker pool executes the graph. The queue is `arq`, not
Celery, superseding the proposal's stack table.

**Forces.** Runs take 2–20 minutes. Running them in a request handler holds an HTTP connection
open, loses all progress on redeploy, couples API availability to inference latency, and makes
cancellation impossible.

**Why `arq` over Celery.** The entire stack is asyncio-native: SQLAlchemy `AsyncSession`,
`AsyncQdrantClient`, `httpx.AsyncClient`, LangGraph's `ainvoke`. Celery's async story is a
sync-bridging thread pool or an experimental async worker; either means either blocking the event
loop or maintaining two concurrency models in one codebase. `arq` is ~1.5k LOC, Redis-backed,
asyncio-first, and supports cancellation, deferred retries, and cron — the complete requirement.
Redis is already a dependency, so `arq` adds no infrastructure.

**Given up.** Celery's ecosystem (Flower, beat, huge operational corpus) and its résumé value. `arq`
has a smaller community and fewer monitoring integrations.

**Migration path.** `worker/jobs.py` exposes plain `async def job(ctx, run_id)` functions. Swapping
to Celery means changing the decorator and the enqueue call — the graph execution code is
untouched.

**Invalidated if.** Multi-node worker fan-out is needed with routing, priorities, and rate limits
per queue, where Celery's maturity would start to pay.

---

### ADR-003 — LangGraph `AsyncPostgresSaver` for checkpointing

**Status:** Accepted

**Decision.** Checkpoint graph state to the same PostgreSQL instance after every node.

**Forces.** A worker crash 15 minutes into a training run must not discard the work. HITL gates
require state to persist across process boundaries. Debugging a bad run requires replay.

**Consequences.** Resume replays at most one node. `aget_state_history()` gives time-travel
debugging for free. Checkpoint tables live in the app database and must be excluded from Alembic
autogeneration — see defect **D-006**, which would otherwise silently `DROP TABLE checkpoints`.

**Given up.** Write amplification: every node boundary is a database round trip. Mitigated by blob
offloading ([`AGENTS.md §10`](./docs/AGENTS.md#10-checkpointing-and-resume)) — without it a run
with several revisions checkpoints megabytes per node and the checkpoint table dominates the
database.

---

### ADR-004 — Docker-out-of-Docker for the sandbox, with the risk stated plainly

**Status:** Accepted

**Decision.** The worker mounts `/var/run/docker.sock` and launches sibling containers. Not
Docker-in-Docker, not a VM, not a WASM runtime.

**Forces.** Agent-generated code must be executed with strong isolation, on a single developer
machine, with a one-command setup, on both macOS and Linux.

**The risk, stated without euphemism.** Access to the Docker socket is equivalent to root on the
host. If the worker container is compromised, the host is compromised.

**Why it is acceptable here.** The worker never executes agent-generated code — that is the entire
security property the design rests on. The worker launches containers; the containers run the code.
The socket is mounted into the worker only, never the API, never a sandbox. The driver never passes
model-controlled strings into container configuration: image, command, mounts, and limits come from
a fixed profile table.

**Hardening available.** The `hardened` compose profile puts `docker-socket-proxy` in front of
dockerd with `CONTAINERS=1 POST=1 IMAGES=1 EXEC=0 NETWORKS=0 VOLUMES=0`, so a compromised worker
cannot create privileged containers or mount arbitrary host paths. `SANDBOX_RUNTIME=runsc` swaps in
gVisor where available.

**Rejected alternatives.** See [Rejected alternatives](#rejected-alternatives): DinD, rootless
Docker, Firecracker, Podman, Pyodide/WASM.

**Invalidated if.** The platform becomes multi-user, or is exposed beyond localhost. Either changes
the threat model completely and forces gVisor or microVMs.

---

### ADR-005 — Network-isolated sandbox with file-handoff to MLflow

**Status:** Accepted

**Decision.** Sandboxes run `--network none`. Training scripts write `/artifacts/metrics.json`; the
host-side `mlops` node reads it and logs to MLflow. Scripts do not call MLflow directly.

**Forces.** The proposal's flow — "MLOps Agent executes the script and logs metrics to MLflow" —
implicitly requires the sandbox to reach the network. Any network path is an exfiltration path and
an uncontrolled dependency.

**Why this is better than in-sandbox logging, not merely safer.**

1. MLflow credentials never enter agent-controlled code.
2. Metrics are JSON-Schema-validated before reaching the tracking store, so an agent cannot pollute
   it with hallucinated metric names or a string where a float belongs.
3. Tracking survives a sandbox crash — the file is on a bind mount, written incrementally.
4. MLflow being down cannot fail a run: the file is the durable record and backfill replays it.
5. The same file drives criteria evaluation and the report, so all four consumers read one
   validated source.

**Given up.** No live metric streaming during a long training run; metrics appear at the end.
`mlflow.autolog()` is unavailable. Both are acceptable — see
[`MLOPS.md §12.3`](./docs/MLOPS.md#123-what-deliberately-is-not-planned).

**Escape hatch.** The opt-in `train-tracked` profile attaches the sandbox to an `internal: true`
network whose only member is MLflow. No route to the internet, host, or other services.

---

### ADR-006 — Redis Streams for the event log, not pub/sub

**Status:** Accepted

**Decision.** Run events go to a Redis Stream per run (`run:{id}:events`, `MAXLEN ~ 10000`, 24 h
TTL) with an application-allocated gapless `seq`. WebSocket clients replay from `after_seq` and
then tail.

**Forces.** A browser that sleeps for two minutes must not lose events. `PUBLISH` is fire-and-forget
and drops everything while nobody is listening.

**Why an application-allocated `seq` rather than the stream ID.** Stream IDs are timestamps and
change meaning when the stream is trimmed. A monotonic `INCR` counter gives clients a stable
cursor, and a `replay.gap` event tells a client its cursor has aged out so it can resynchronise
from `run.snapshot` rather than silently missing history.

**Given up.** Memory: 10 000 entries × ~500 B ≈ 5 MiB per active run. Bounded by `MAXLEN` and TTL.

---

### ADR-007 — Hybrid retrieval, plus an episodic `run_memory` collection

**Status:** Accepted

**Decision.** Every Qdrant collection carries a dense vector and a BM25 sparse vector, fused with
RRF. A third collection, `run_memory`, stores error→fix pairs from completed runs.

**Forces on hybrid.** Pure dense retrieval systematically fails on exact identifiers — API names,
error codes, parameter names — which is precisely what a coding agent searches for. `GridSearchCV`
is a lexical match problem, not a semantic one.

**Forces on `run_memory`.** Without it, the platform makes the same mistake on run 1 and run 100.
The Debugger queries by normalised error fingerprint before reasoning from scratch, so a fix
discovered once is reused. This is the only component whose value compounds with usage.

**Given up.** Ingestion writes two vectors per chunk. `run_memory` can accumulate bad advice if
written from failed runs — mitigated by writing only from `SUCCEEDED` runs.

**Provisional element 🔬.** The `0.82` score threshold for injecting prior art is a guess.
Calibrate against `core-10` once the corpus has ≥ 200 memory points; too low poisons the Debugger
prompt with irrelevant fixes, too high makes the collection inert.

---

### ADR-008 — Success criteria as a machine-checkable contract

**Status:** Accepted

**Decision.** The Planner emits `success_criteria` — metric name, comparator, threshold, required
flag, weight. The Evaluator computes pass/fail arithmetically from `metrics.json`. The LLM's rubric
is advisory and cannot change `passed`.

**Forces.** "Eval Agent checks MLflow for performance. If the model fails benchmarks…" leaves
*benchmark* undefined. An LLM asked "is 0.91 good enough?" answers differently on consecutive calls,
which makes routing nondeterministic and the whole system unevaluable.

**Consequences.** Evaluation is reproducible, auditable, and cheap. The final report has a real
pass/fail table. The Planner is forced to think about measurement *before* implementation, which
measurably improves plan quality. A run cannot declare success on a metric it never computed —
absence is failure, not success.

**The asymmetry that matters.** The LLM may downgrade `REFINE → REPLAN` (it can see that an
approach is structurally wrong even when the numeric gap is small) but may never upgrade
`REPLAN → REFINE`, and can never produce `ACCEPT` when a required criterion failed. Enforced in
code after parsing, not by prompt instruction.

**Given up.** Goals that resist quantification ("write an interesting analysis") fit awkwardly. The
`analysis` task kind exists for these and leans on the rubric plus artifact-existence criteria.

---

### ADR-009 — A Reporter agent, because telemetry is not a deliverable

**Status:** Accepted

**Decision.** Add a `reporter` node that runs on every terminal path — success, partial, and
failure — and produces `REPORT.md`.

**Forces.** The proposal's graph ends at the Eval Agent, whose output is a number. That is
telemetry. A human still has to open MLflow, find the run, read the code, and reconstruct what
happened. The brief for this design explicitly required **tangible output**.

**Consequences.** Every run yields a document a person can read: objective, result against
criteria, approach, *what went wrong and how it was fixed*, detailed results, reproduction
instructions, limitations, artifact manifest. Failed runs produce a diagnosis rather than a log
file. The debugging narrative in section 4 is frequently the most valuable part — it is a record of
the system reasoning about its own failures.

**Given up.** One more LLM call (~30–60 s) per run.

**Cannot fail.** If the LLM fails every repair stage, a Jinja2 template renders the same eight
sections from state. This is what makes the "every run produces a deliverable" invariant provable
rather than aspirational — see the corollary in
[`AGENTS.md §6.4`](./docs/AGENTS.md#64-termination-proof).

---

### ADR-010 — An offline dataset registry, not runtime downloads

**Status:** Accepted

**Decision.** `/datasets` is a read-only volume seeded by `make seed-datasets`, described by
`manifest.json`. Every `train` plan step must bind to a manifest entry. `describe_dataset`
precomputes schema, dtypes, class balance, and missing-value counts at seed time.

**Forces.** With `--network none`, `fetch_openml` and `read_csv("https://…")` cannot work. Without
a registry, the Planner invents plausible-sounding datasets and the Coder writes
`pd.read_csv("data.csv")` for a file that does not exist — the single most common failure in
naive agentic ML systems.

**Consequences.** Plans are grounded in data that provably exists. Runs are reproducible because
the data is hash-pinned. Validation rejects a plan referencing an unknown `dataset_id` *before* any
compute is spent, and re-prompts the Planner with the valid ids. Precomputed schema removes the
"guess the column name" class of failure without spending a sandbox execution on `df.head()`.

**Given up.** The platform can only work on curated data. Adding a dataset is a deliberate
operator action.

---

### ADR-011 — Structured output with an escalating repair ladder

**Status:** Accepted

**Decision.** Every LLM node returns Pydantic-validated structured output through a five-stage
ladder: constrained decoding → error-feedback reprompt → deterministic salvage → field-wise
extraction → fail.

**Forces.** 7–8B local models emit malformed JSON far more often than frontier models. A single
retry is not enough; unbounded retries burn the budget.

**Consequences.** Node failure rates drop sharply. Ladder depth is exported as
`pluton_structured_output_attempts`, which is a leading indicator: a sudden rise in stage-3+
resolutions means a prompt regression or a model-tier change.

**The important detail.** Stage 2 feeds the Pydantic `ValidationError` back verbatim. Models fix
their own schema errors reliably when shown the specific error, and much less reliably when told
"that was invalid, try again."

---

### ADR-012 — Ollama runs natively on the host, not in a container (macOS)

**Status:** Accepted

**Decision.** Ollama is a host process reached at `http://host.docker.internal:11434`. A
containerised Ollama is available only in the `linux-gpu` profile.

**Forces.** Docker Desktop for macOS cannot pass through the Metal GPU. A containerised Ollama runs
CPU-only — a 5–15× slowdown that makes the platform unusable on the primary development target.

**Given up.** `docker compose up` is not literally one command; Ollama must be installed and
models pulled. `make setup` handles this and `make doctor` verifies it.

**Consequence to watch.** The `host.docker.internal` address does not resolve from the *host*, so
running the API natively via `make dev` with a container-oriented `.env` breaks Ollama access. This
is live defect **D-014**; the fix is environment-specific `.env` files rather than one shared file.

---

### ADR-013 — MLflow on Postgres with proxied artifacts, not SQLite

**Status:** Accepted

**Decision.** `--backend-store-uri postgresql://…/mlflow --artifacts-destination /mlflow/artifacts
--serve-artifacts`.

**Forces.** Two workers plus nested child runs means concurrent writers, which produces
`database is locked` on SQLite. The Model Registry requires a database-backed store and is
impossible on a file store.

**Why proxied artifacts specifically.** Clients get `mlflow-artifacts:/…` URIs and upload over
HTTP. Without it, every client needs the artifact volume mounted at the identical path or URIs
break silently. It is also what makes the MinIO migration a configuration change rather than a
rewrite.

**Given up.** One more database, and the MLflow server becomes a data-path bottleneck for large
artifact uploads.

---

### ADR-014 — One ASGI entrypoint: `app.main:app`

**Status:** Accepted

**Decision.** `backend/main.py` is deleted. `backend/app/main.py` is the sole entrypoint.

**Forces.** Two entrypoints exist today with divergent behaviour, and `backend/main.py` imports
`api_router` from `app.api.router`, which exports `api_v1_router`. It raises `ImportError` on
import — it has never run. Defect **D-010**.

**Consequence.** Every launcher — Makefile, Dockerfile, compose, CI, tests — targets
`app.main:app`.

---

### ADR-015 — Port allocation, and why MLflow is on 5001

**Status:** Accepted

**Decision.** MLflow maps host `5001` → container `5000`. Grafana maps to `3001`. cAdvisor to
`8081`.

**Forces.** macOS Monterey and later bind port 5000 to the AirPlay Receiver by default; a service
on host 5000 either fails to bind or is shadowed intermittently, producing a maddening
"works sometimes" failure. Grafana's default 3000 collides with Next.js. cAdvisor's 8080 collides
with roughly every other dev server.

**Consequence — and the trap.** Two URIs for MLflow. In-network callers use `http://mlflow:5000`;
host callers use `http://localhost:5001`. Conflating them is defect **D-003**. The `Settings` split
into `MLFLOW_TRACKING_URI` and `MLFLOW_PUBLIC_URL` exists specifically so this cannot be got wrong
by accident.

---

### ADR-016 — Single-user auth; multi-tenancy deferred

**Status:** Deferred

**Decision.** One shared bearer token. API binds to `127.0.0.1` by default. WebSocket auth via
single-use 60-second tickets.

**Forces.** The proposal targets a solo developer's local machine. Real multi-tenancy means users,
sessions, per-resource authorisation, quotas, and audit — weeks of work that demonstrates nothing
the rest of the system does not.

**Given up.** Cannot be exposed to a network as-is.

**Not deferred:** the *hooks*. Every table has a `run_id` scope; the auth dependency is a single
injectable function; the WebSocket already checks per-run authorisation. Adding real users later
means implementing that dependency, not restructuring the schema.

**The one guard shipped now.** Startup refuses to proceed if `HOST=0.0.0.0` is combined with the
default development token. The failure mode this prevents — a demo machine on a conference Wi-Fi
running arbitrary code for anyone who finds port 8000 — is bad enough to be worth the check.

---

### ADR-017 — Web search removed from the default configuration

**Status:** Accepted

**Decision.** The Researcher retrieves only from local Qdrant collections. `web_search` exists
behind `ENABLE_WEB_SEARCH=0` (SearXNG, opt-in compose profile).

**Forces.** The proposal's "searches the local vector database (RAG) or simulated web environments"
conflicts with the offline-first goal, breaks reproducibility (the same query returns different
results next week, so runs stop being replayable), and turns retrieval into an open-internet
untrusted-input channel — escalating prompt injection (threat T6) from a curated-corpus risk to an
unbounded one. "Simulated web environments" is also not an implementable specification.

**When enabled**, results are ingested with `trust_level: "untrusted"` and excluded from
`code_exemplars` entirely — untrusted text must never become a code exemplar the Coder trusts.

---

### ADR-018 — Deterministic routing; models propose, the graph disposes

**Status:** Accepted

**Decision.** No edge predicate reads free-form model text. Routers read enum fields from validated
structured output and integer counters.

**Forces.** Routing on model prose — "if the response contains 'error', go to the debugger" — fails
nondeterministically and is untestable.

**Consequences.** Every router is a pure function unit-tested to 100% branch coverage without a
model. Failure classification comes from `exit_code`, `OOMKilled`, and schema validity — never from
an LLM reading stderr. Cycle bounds are enforced by counters no model can influence.

---

### ADR-019 — Split diagnosis from synthesis: the Debugger writes no code

**Status:** Accepted

**Decision.** The Debugger emits a `Diagnosis` — root cause, evidence, fix strategy, targeted
changes. The Coder is the only node that produces a `CodeRevision`.

**Forces.** A single agent that both diagnoses and rewrites tends to rewrite everything,
reintroducing bugs in code that already worked. Two narrow prompts also outperform one wide one on
7B models.

**Consequences.** Only one node can produce code, which keeps `code_revisions` coherent. Diagnoses
are independently inspectable in the UI and the report. The Debugger prompt can be tuned for
analysis without regressing code quality.

**The guard that makes it work.** When the last three errors share a fingerprint, the graph
escalates to `planner` rather than looping. Without that rule, the most common multi-agent failure
mode is a Coder–Debugger pair producing cosmetically different code that fails identically until
the budget expires.

---

### ADR-020 — Repository layout: `backend/app/` package, superseding the proposal

**Status:** Accepted

**Decision.** Keep `backend/app/{api,core,db,engine,schemas,services,worker}` rather than the
proposal's `backend/{agents,api,core,models,tools}`.

**Forces.** The proposal layout is not an installable package: it forces `sys.path` manipulation,
makes `pytest` collection fragile, and makes Docker layer caching worse. `backend/app/` is the
standard FastAPI convention and gives clean `app.*` imports.

**Mapping.** `agents/` → `app/engine/nodes/` · `api/` → `app/api/v1/` · `core/` → `app/core/` ·
`models/` → `app/db/models/` + `app/schemas/` · `tools/` → `app/engine/tools/` · `mlops/` →
`app/services/mlflow_client.py` + `infrastructure/docker/sandbox/`.

---

### ADR-021 — The sandbox driver is a `Protocol`, for the k3s path

**Status:** Deferred

**Decision.** `SandboxDriver` is defined as a `typing.Protocol`. `DockerSandboxDriver` is the only
implementation in v1.

**Forces.** `docker run` from a worker pod does not port to Kubernetes. The sandbox is the *only*
component that genuinely blocks a k3s migration; everything else is a manifest translation.

**Consequence.** A future `KubernetesSandboxDriver` creates a `Job` with `restartPolicy: Never`, a
deny-all `NetworkPolicy`, a hardened `securityContext`, resource limits, and an `emptyDir`
workspace — with no changes to any graph node. Writing the Protocol now costs nothing; retrofitting
it later would touch every node.

---

## Deviations from the original proposal

| Proposal | This design | Why |
|---|---|---|
| 6 agents | 7 agents + 3 deterministic nodes | Reporter added for tangible output (ADR-009); `sandbox_exec` and `finalizer` split out for deterministic routing (ADR-018) |
| MLOps Agent is an LLM agent | `mlops` has no LLM | Mapping validated JSON to MLflow calls is mechanical; an LLM only adds transcription errors ([`AGENTS.md §1.3`](./docs/AGENTS.md#13-why-mlops-has-no-llm)) |
| Eval Agent "checks benchmarks" | Machine-checkable `success_criteria` contract | *Benchmark* was undefined; LLM-judged success is nondeterministic (ADR-008) |
| Celery | `arq` | asyncio-native stack (ADR-002) |
| Research Agent searches "simulated web environments" | Local corpus only; web search opt-in | Offline-first, reproducible, injection-resistant (ADR-017) |
| Graph runs in the API | Separate worker pool | Long runs, redeploy safety, cancellation (ADR-002) |
| MLflow on SQLite | MLflow on Postgres, proxied artifacts | Concurrent writers; registry requires a DB store (ADR-013) |
| MLflow on port 5000 | Host 5001 | macOS AirPlay Receiver owns 5000 (ADR-015) |
| `backend/{agents,api,core,models,tools}` | `backend/app/…` | Installable package, standard convention (ADR-020) |
| Script logs to MLflow directly | Script writes `metrics.json`; host logs it | Preserves `--network none` and validates before ingestion (ADR-005) |
| Agents fetch datasets | Curated read-only registry | No network in the sandbox; grounds plans in data that exists (ADR-010) |
| "RAG Precision: subjective relevance scoring by a local model" | Precision@5 against a 50-query labelled set | LLM-judged relevance drifts with the judge and cannot detect regressions ([`AGENTS.md §13.1`](./docs/AGENTS.md#131-platform-kpis)) |
| k3s manifests "optional, for later" | Deferred, with the driver Protocol shipped now | The sandbox is the only real blocker; the seam costs nothing today (ADR-021) |

**Kept from the proposal without change:** LangGraph, Ollama with local models, FastAPI, Postgres,
Redis, Qdrant, MLflow, Docker Compose, GitHub Actions, Next.js + TypeScript + Tailwind + shadcn/ui,
Prometheus + Grafana, WebSocket streaming, the six original agent roles, the monorepo shape, and
the documentation strategy.

---

## Rejected alternatives

### Orchestration

| Option | Rejected because |
|---|---|
| **CrewAI** | Role-and-task abstraction hides control flow. Cyclic self-correction is awkward; there is no first-class checkpointing, so a 15-minute run cannot survive a restart. |
| **AutoGen** | Conversational multi-agent chat is a poor fit for a deterministic pipeline with hard budget bounds. Group-chat termination is heuristic; ours must be provable. |
| **Hand-rolled state machine** | Would need to reimplement checkpointing, interrupts, streaming, and state merging. That is LangGraph, written worse. |
| **Temporal / Prefect** | Genuinely excellent durable execution, but a heavyweight external dependency for a single-node local platform, and neither understands LLM streaming. |

### Sandbox isolation

| Option | Rejected because |
|---|---|
| **Docker-in-Docker (`--privileged`)** | Strictly worse than socket mounting: `--privileged` on the *outer* container is a larger attack surface than the socket, plus nested storage-driver pain. |
| **Rootless Docker** | Materially better security, but Docker Desktop for macOS does not support it, and the primary development target is macOS. Documented as a Linux hardening option. |
| **Firecracker microVMs** | The right answer for hostile multi-tenant code. Linux/KVM only, ~10× the operational complexity, and ~125 ms boot vs ~50 ms container start. Disproportionate for a single-user local tool. |
| **Podman** | Rootless by default and daemonless — attractive. Rejected on macOS friction (podman machine adds a VM layer that must also mount the artifacts bind) and a smaller Python SDK. Revisit if the platform goes Linux-only. |
| **Pyodide / WASM** | True capability-based isolation, no container needed. Rejected because PyTorch, LightGBM, and XGBoost do not run under it; the platform would be limited to pure-Python ML. |
| **`RestrictedPython` / `exec` in-process** | Not a security boundary. Every sandbox-escape CVE in the Python ecosystem argues against it. |

### Vector store

| Option | Rejected because |
|---|---|
| **ChromaDB** (proposal offered it as an alternative) | Simpler, but weaker filtering, no native sparse/hybrid fusion, and less predictable at 100k+ points. Hybrid retrieval is load-bearing for a coding agent (ADR-007). |
| **pgvector** | One less container, and genuinely tempting. Rejected on hybrid search: implementing RRF over `tsvector` + `vector` by hand is real work Qdrant does natively, and HNSW tuning is coarser. |
| **FAISS** | A library, not a service. No payload filtering, no persistence story, no concurrent writes. |

### Real-time transport

| Option | Rejected because |
|---|---|
| **Server-Sent Events** | Simpler and auto-reconnecting, but unidirectional. Cancel and HITL approval need a client→server channel. |
| **Polling** | Would work; wastes the streaming demonstration that is half the point of the frontend. |
| **socket.io** | Rooms and fallbacks are nice, but it adds a protocol layer over a problem native WebSocket plus Redis Streams already solves, and the Python server support is weaker than the JS side. |

### Evaluation

| Option | Rejected because |
|---|---|
| **LLM-as-judge for pass/fail** | Nondeterministic control flow, unauditable, and self-serving — the same model family judging its own work. Retained *advisory only* as the rubric (ADR-008). |
| **Fixed thresholds per task kind** | Cannot express "the user asked for 95%". The Planner emitting criteria per task is strictly more expressive. |
| **No evaluation; report metrics and stop** | Removes the replan loop, which is the most interesting behaviour in the system. |

---

## Known defects in the current tree

Catalogued from a full read of the repository at commit `0109a1a`. Ordered by severity. These are
defects in code that exists — not missing features, which are tracked in
[`ARCHITECTURE.md §21`](./docs/ARCHITECTURE.md#21-implementation-status).

**Phase 0 closed D-001, D-002, D-003, D-006, D-007, D-008, D-010, D-011, D-012, D-013 and
D-014; Phase 1 closed D-004.** Each is kept below with a `**Fixed:**` line recording what was
actually done, because the reasoning is worth more than the checkbox. Still open: D-005, D-009,
and the Low band.

### Blockers (critical) — the application cannot currently run

**D-001 · `DATABASE_URL` uses a sync driver and wrong credentials**
`.env:6` sets `DATABASE_URL=postgresql://ai_user:ai_password@localhost:5432/platform_db`.
`app/core/config.py:async_database_url` returns `self.DATABASE_URL` verbatim when set, so
SQLAlchemy's async engine receives the sync `postgresql://` scheme and raises
`InvalidRequestError: The asyncio extension requires an async driver`. Independently, the
credentials and database name do not match `infrastructure/docker-compose.yml`, which creates
`postgres` / `postgres_password_dev` / `agent_platform`.
**Fix:** use `postgresql+asyncpg://postgres:${POSTGRES_PASSWORD}@localhost:5432/agent_platform`,
and add a `field_validator` on `DATABASE_URL` that rejects any non-`+asyncpg` scheme at startup
with a message naming the correct form.
**Fixed (Phase 0).** `DATABASE_URL` has a `field_validator` that rejects any scheme other than
`postgresql+asyncpg://` and names the correct form in the error; an empty value falls back to the
DSN composed from `POSTGRES_*`. `.env` no longer carries `ai_user`/`platform_db`: the defaults are
`postgres` / `agent_platform`, matching compose, and compose reads the same repository-root `.env`
(`--env-file`) so a rotated `POSTGRES_PASSWORD` cannot desynchronise the two.

**D-010 · `backend/main.py` imports a name that does not exist**
`backend/main.py:8` does `from app.api.router import api_router`; `app/api/router.py:5` exports
`api_v1_router`. This module raises `ImportError` on import and has never run. It also duplicates
`app/main.py` with divergent behaviour.
**Fix:** delete `backend/main.py` (ADR-014). Point every launcher at `app.main:app`.
**Fixed (Phase 0).** `backend/main.py` is deleted. `backend/app/main.py` is the sole entrypoint;
`make dev` already targeted `app.main:app`.

**D-014 · `OLLAMA_BASE_URL` is container-only but `make dev` runs on the host**
`.env:19` sets `http://host.docker.internal:11434`, which does not resolve from a macOS host
process. `make dev` runs uvicorn natively, so every LLM call fails with a DNS error.
**Fix:** `.env` for host-side development uses `http://localhost:11434`; compose injects
`OLLAMA_BASE_URL=http://host.docker.internal:11434` as a service-level environment override.
Document both in `.env.example`.
**Fixed (Phase 0).** `OLLAMA_BASE_URL` defaults to `http://localhost:11434`. This generalised into
a convention: every default is the host-development value, and `docker-compose.yml` injects the
in-network form per service. `Settings` records that form as field metadata, and the generated
`.env.example` prints it as a comment beside each affected variable.

### High — silent data or behaviour corruption

**D-006 · Alembic autogeneration will drop the LangGraph checkpoint tables**
`AsyncPostgresSaver.setup()` creates `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations` in the same database. They are absent from `Base.metadata`, so the next
`alembic revision --autogenerate` emits `DROP TABLE` for all four — destroying every resumable run.
**Fix:** add the `include_object` filter shown in
[`ARCHITECTURE.md §7.1`](./docs/ARCHITECTURE.md#71-postgresql-schema) to `alembic/env.py`.
**Fixed (Phase 0).** `alembic/env.py` defines `LANGGRAPH_TABLES` and an `include_object` filter,
passed to `context.configure()` in both offline and online modes (along with `compare_type=True`).
Verified by creating the four tables in a live database and confirming `alembic check` ignores
them while still detecting an unrelated stray table.

**D-002 · Per-role model settings are silently discarded**
`.env.example:29-33` defines `PLANNER_MODEL`, `RESEARCHER_MODEL`, `CODER_MODEL`, `DEBUGGER_MODEL`,
`EVALUATOR_MODEL`. `Settings` declares only `DEFAULT_MODEL`, and `model_config` sets
`extra="ignore"` — so Pydantic drops them without warning. An operator who changes `CODER_MODEL`
sees no effect and no error. The same applies to `USE_DOCKER_SANDBOX` and `MAX_AGENT_RETRIES`.
**Fix:** declare every documented variable in `Settings`
([`ARCHITECTURE.md §14`](./docs/ARCHITECTURE.md#14-configuration-reference)), and add a startup
check that warns on any `PLUTON_`/known-prefix env var not consumed by a field.
**Fixed (Phase 0).** All 66 documented variables are declared fields, per-role models included;
`settings.model_for_role("coder")` resolves one with a `DEFAULT_MODEL` fallback, and
`PLUTON_MODEL_TIER=small` swaps every role still at its standard default for the 3B ladder. Startup
calls `warn_unconsumed_env()`, which logs a warning for any variable carrying a platform prefix that
no field consumes.

**D-003 · `MLFLOW_TRACKING_URI` default is wrong in both contexts**
`config.py` defaults to `http://localhost:5000`. Compose maps MLflow to host `5001`, so host
callers 404 (and on macOS may hit AirPlay Receiver instead); in-network callers should use
`http://mlflow:5000`.
**Fix:** default `MLFLOW_TRACKING_URI=http://mlflow:5000`, add `MLFLOW_PUBLIC_URL=http://localhost:5001`
(ADR-015).
**Fixed (Phase 0).** `MLFLOW_TRACKING_URI` and `MLFLOW_PUBLIC_URL` are separate fields. Following
the host-default convention (see D-014), tracking defaults to `http://localhost:5001` and compose
injects `http://mlflow:5000` for in-network services; the public URL is always the host form.

**D-005 · Qdrant service: deprecated API and a blocked event loop**
`app/services/vector_store.py` has two problems. `await self.client.search(...)` is deprecated
since `qdrant-client` 1.10 in favour of `query_points`, and `search()` cannot express hybrid
fusion. More seriously, `self.embeddings.embed_documents(texts)` and `embed_query(query)` are
**synchronous** calls inside `async def` — each blocks the entire event loop for the duration of an
Ollama embedding round trip (tens to hundreds of ms per batch), stalling every concurrent request
in the process.
**Fix:** migrate to `query_points` with dense+sparse prefetch and RRF; use
`aembed_documents`/`aembed_query`, or wrap in `anyio.to_thread.run_sync`. Add payload indexes —
without them, every filtered query is a full scan.

**D-013 · Deep health check reports unhealthy dependencies as healthy, and never returns 503**
`app/api/v1/health.py:69-73`: when MLflow returns a non-200 status, the handler still records
`status="healthy"` with message `"Server reachable"`, and does not set `overall_healthy = False`.
The endpoint always returns HTTP 200 regardless of `overall_healthy`, so container orchestration
and monitoring cannot distinguish healthy from degraded.
**Fix:** mark non-200 as unhealthy; return 503 when any hard dependency (postgres, redis, qdrant)
is down. Classify MLflow and Ollama as soft dependencies that degrade rather than fail.
**Fixed (Phase 0).** Probes run concurrently and are classified: postgres, redis and qdrant are
hard (any failure returns 503 with `status="unhealthy"`), mlflow and ollama are soft (`degraded`,
still 200). A non-200 from MLflow is now recorded as unhealthy rather than "Server reachable".
Verified by stopping Redis: HTTP 503, `redis.status="unhealthy"`, everything else still truthful.

### Medium

**D-004 · Deprecated LangChain imports**
`app/engine/llm.py` imports `ChatOllama` and `OllamaEmbeddings` from `langchain_community`. Both
are deprecated in favour of the `langchain-ollama` package and emit `LangChainDeprecationWarning`;
the community versions also lack `keep_alive` and the `format` parameter needed for constrained
JSON decoding (ADR-011).
**Fix:** `pip install langchain-ollama`; import from `langchain_ollama`.
**Fixed (Phase 1).** `app/engine/llm.py` imports `ChatOllama` and `OllamaEmbeddings` from
`langchain_ollama`, passes `keep_alive` and the per-request timeout, and routes model, temperature
and `num_ctx` per role from a `ROLE_PROFILES` table. The import is deferred into the function
bodies: the graph is assembled, the state schema validated and the whole test suite run without
the Ollama stack installed, and a missing package raises a `RuntimeError` naming the install
command rather than an `ImportError` three frames down. `format` is bound per call by
`engine/structured.py`, which is where constrained decoding belongs.

**D-007 · CORS wildcard with credentials**
`app/main.py:44-50` sets `allow_origins=["*"]` together with `allow_credentials=True`. That
combination is invalid per the CORS specification — browsers reject it — and is unsafe if it ever
worked.
**Fix:** an explicit origin allowlist from `settings.CORS_ORIGINS`
([`ARCHITECTURE.md §13.2`](./docs/ARCHITECTURE.md#132-authentication-and-cors)).
**Fixed (Phase 0).** `app/main.py` uses `allow_origins=settings.CORS_ORIGINS` with an explicit
method and header list and `max_age=600`. Verified: an allowed origin gets its own origin echoed
back with `allow-credentials: true`; an unknown origin's preflight gets 400 and no
`access-control-allow-origin` header.

**D-008 · Missing compose health checks and dependency ordering**
`infrastructure/docker-compose.yml` defines health checks for postgres and redis but not qdrant or
mlflow, and has no `depends_on` at all. MLflow starts before Postgres is ready and crash-loops on
first boot.
**Fix:** health checks per
[`ARCHITECTURE.md §15.2`](./docs/ARCHITECTURE.md#152-startup-ordering), plus
`depends_on: {condition: service_healthy}`.
**Fixed (Phase 0).** Qdrant and MLflow have health checks, and MLflow waits on
`postgres: {condition: service_healthy}` — a real dependency now that its backend store is a
database on that server (ADR-013) rather than a SQLite file. Neither image ships `curl` or `wget`,
so the probes use bash's `/dev/tcp` and `python -c "…urlopen…"` respectively.

**D-009 · `researcher.py` is a copy of `qdrant_tool.py`**
`app/engine/nodes/researcher.py` contains a byte-identical duplicate of
`app/engine/tools/qdrant_tool.py` — the `search_knowledge_base` tool, not a node. Two `@tool`
functions with the same name in one process is a latent registry collision.
**Fix:** the tool stays in `tools/qdrant_tool.py`; `nodes/researcher.py` implements the node per
[`AGENTS.md §7.2`](./docs/AGENTS.md#72-researcher-agent).

**D-011 · Three near-duplicate Alembic revisions**
`b9cff7b47159` → `8e4ce31ef43e` → `eb1aa4f709e4` all carry the message "Initial schema migration
for tasks logs and artifacts". Only the head appears to have been applied. A fresh
`alembic upgrade head` replays all three; if the earlier two contain the same `CREATE TABLE`
statements, it fails on the second.
**Fix:** verify against a clean database. If the first two are dead, squash to a single baseline
revision before any real deployment — this is the last moment when squashing is free.
**Fixed (Phase 0).** Verified against a clean database: the first two revisions were empty
`pass` bodies and only the head carried DDL. Squashed to a single `0001_baseline`, regenerated by
autogenerate so it matches the ORM exactly, with the enum type dropped in `downgrade()` so the
round trip is repeatable. A database still stamped with an old revision needs
`alembic stamp 0001_baseline`.

**D-012 · `.env` and `.env.example` have diverged**
`.env.example` documents `USE_DOCKER_SANDBOX`, `SANDBOX_IMAGE`, `MAX_AGENT_RETRIES`, and the five
model variables; `.env` omits all of them. Neither matches the fields `Settings` actually declares.
Three sources of truth, no agreement.
**Fix:** `Settings` is the single source of truth; `.env.example` is generated from it by
`make gen-env-example`, and CI fails if the checked-in file differs.
**Fixed (Phase 0).** `Settings` is the single source of truth. `scripts/gen_env_example.py`
renders `.env.example` from its fields — defaults, docstrings, in-network forms and all —
via `make gen-env-example`; `make check-env-example` fails on any drift and is wired into
`make check` and CI.

### Low

- **D-015** · `app/services/vector_store.py` constructs a new `AsyncQdrantClient` per
  `VectorStoreService()` instantiation, and `qdrant_tool.py` instantiates one per tool call. Each
  call opens a fresh connection pool. Use a module-level singleton or FastAPI lifespan-managed
  client.
- **D-016** · `ensure_collection_exists()` runs on every `add_documents` and `search_similar` call
  — a `get_collections` round trip per query. Cache the result after first success.
- **D-017** · `vector_size` is hardcoded to `768` in `ensure_collection_exists` while the embedding
  model is a parameter. Changing `EMBEDDING_MODEL` to a 1024-dim model silently creates a mismatched
  collection. Derive the dimension from a probe embedding at startup and assert it matches.
- **D-018** · `Settings.SECRET_KEY` defaults to `"dev_secret_key_change_in_production"` with no
  guard against that value being used outside `development`.
- **D-019** · `.github/workflows/docker-build.yml` is an empty file — a workflow that silently does
  nothing. Either implement it or delete it.
- **D-020** · Empty placeholder files that are not obviously intentional: `backend/Dockerfile`,
  `backend/requirements.txt`, `backend/pyproject.toml` (the root `pyproject.toml` is the real one),
  `frontend/package.json`, `frontend/tsconfig.json`. Empty `package.json`/`tsconfig.json` break
  `npm install` and `tsc` with confusing parse errors rather than "file not found".
- **D-021** · `.DS_Store` files are committed at the repo root, `backend/`, and `backend/app/`
  despite being in `.gitignore` — they were added before the ignore rule. `git rm --cached` them.
- **D-022** · **The sandbox never received the program it was supposed to run.** `/workspace` is a
  tmpfs and the only mounts were `/datasets` and `/artifacts`, so `main.py` — written by the driver
  to `/runs/{id}/rev-N/main.py`, one level above the bind-mounted `artifacts/` — was not visible
  inside the container. Every real execution exited 2 (`can't open file '/workspace/main.py'`).
  Present in [§10.4](./docs/ARCHITECTURE.md#104-exact-launch-configuration)'s launch block as well
  as in the code, which is why no reading of one against the other caught it. **Fixed**: a
  read-only *file* bind of `main.py` at `/workspace/main.py`, layered over the tmpfs. The program
  additionally cannot rewrite itself mid-execution, and `/artifacts` stays the only writable mount.
  Found by `tests/integration/test_sandbox_security.py`, which was the first thing to run a real
  container end to end.
- **D-023** · **No sandbox output was ever captured.** `_pump_logs` called
  `container.logs(..., demux=True)`, but docker-py accepts `demux` on the *attach* endpoint only;
  `logs()` raised `TypeError`, which the surrounding `except` turned into one debug line. Both
  `stdout_tail` and `stderr_tail` came back empty for every real container, so every failure looked
  like a silent crash and the Debugger had no traceback to work from. **Fixed**: `container.attach(
  demux=True)`, established *before* the container starts — an attach issued after a container has
  already exited never returns, and a program that prints and exits in 50 ms is the common case.
  The capture runs on a dedicated daemon thread rather than `asyncio.to_thread` so a wedged daemon
  connection cannot consume a shared executor worker for the life of the process.

---

## Risk register

| # | Risk | Likelihood | Impact | Mitigation | Owner signal |
|---|---|---|---|---|---|
| R1 | **Local 7–8B models are not good enough** for reliable code generation, and the success rate sits below 50% | **High** | High | Model routing per role; the repair ladder; the static validator catching errors before execution; `run_memory` compounding fixes; an honest published KPI table | `make bench` success rate |
| R2 | Sandbox escape via a container vulnerability | Low | Critical | Defence in depth (ADR-004); `hardened` profile; gVisor option; the worker never runs agent code | `benchmarks/suites/sandbox-escape.yaml` |
| R3 | Prompt injection through an ingested corpus document | Medium | High | Delimited untrusted blocks; the static validator as the real enforcement; container as the boundary | Validation rejection rate by reason |
| R4 | Run cost (time) makes iteration painful — 20-minute feedback loops | Medium | Medium | `exec` profile for quick checks; embedding and LLM caches; model warm-up with `keep_alive`; `WORKER_MAX_JOBS` tuned to the GPU | `pluton_run_duration_seconds` p50 |
| R5 | Checkpoint table growth degrades Postgres | Medium | Medium | Blob offloading; `checkpoint_gc` cron; monitored table size | Postgres table-size panel |
| R6 | LangGraph API churn breaks the graph on upgrade | Medium | Medium | Pin exact versions; the 12 scripted integration scenarios catch breakage on any bump | CI on dependency updates |
| R7 | Scope creep — the spec is large and the tree is ~30% built | **High** | High | Phase gates below; each phase ends with something demonstrable | Phase completion |
| R8 | Disk exhaustion from artifacts and run scratch | Medium | Medium | Retention policy ([`MLOPS.md §8`](./docs/MLOPS.md#8-artifact-lifecycle-and-retention)); prune crons; Grafana alert at 80% | Disk usage panel |
| R9 | The agents produce *plausible but wrong* results — leakage, wrong metric, training-set evaluation | **High** | **High** | Criteria contract forbidding `train_`-prefixed metrics; baseline comparison; `leakage-trap` and `imbalance-trap` benchmark cases; the rubric's `metric_validity` dimension | Judgement Score on the 3 trap cases |
| R10 | Ollama model downloads (~20 GB for the standard tier) block first-run setup | Medium | Low | `make setup` pulls with progress; `PLUTON_MODEL_TIER=small` needs ~6 GB; documented up front | Setup completion time |

**R9 deserves the most attention.** An autonomous ML system that reports 99.8% accuracy because it
trained on the test set is worse than useless — it is confidently wrong, and a portfolio piece that
does this in a demo is actively damaging. Three separate controls address it, and the trap
benchmark cases exist to prove they work.

---

## Revised roadmap

The proposal's 10-week, 5-phase plan is preserved in spirit, resequenced so that **every phase ends
with something demonstrable end to end** rather than with a layer that cannot be shown to anyone.

### Phase 0 — Stabilise (3 days)

> **Status: complete.**

Fix D-001, D-010, D-014, D-006, D-002, D-003. Squash the Alembic baseline (D-011). Delete
`backend/main.py`. Generate `.env.example` from `Settings`.
**Exit:** `make up && make migrate && make dev` works from a clean clone; `/health/deep` reports
every dependency truthfully and returns 503 when one is down.

**Delivered.** All of the above, plus D-007, D-008, D-012 and D-013. Verified end to end: the
stack comes up with every service healthy and MLflow gated on Postgres; `alembic upgrade head`
applies `0001_baseline` to an empty database and `alembic check` reports no drift; `/health/deep`
returns 200/`degraded` with Ollama stopped and 503/`unhealthy` with Redis stopped; a task
round-trips through `POST`/`GET`/`DELETE /api/v1/tasks`. 40 tests in `backend/tests/` pin the
configuration invariants and the LangGraph autogenerate filter. Still open from the catalogue:
D-004, D-005, D-009 and the Low band.

### Phase 1 — Vertical slice (week 1–2)

The narrowest path that produces a real deliverable: `init → planner → coder → sandbox_exec →
finalizer`. Real Docker sandbox, real `metrics.json`, real artifact rows. No research, no debug
loop, no MLflow, no frontend. `curl` the API and read the JSON.
**Exit:** a task prompt produces a `metrics.json` and a downloadable `bundle.zip`. *This is the
single most important milestone — after it, everything else is improvement rather than
construction.*

### Phase 2 — Self-correction (week 3–4)

> **Status: complete.**

Add `debugger`, the `ErrorRecord` pipeline, traceback parsing, the static validator, and the
correctness loop. Add `reporter` with the deterministic template fallback.
**Exit:** a deliberately broken prompt produces a run that fails, diagnoses, fixes itself, and
reports what happened.

**Delivered.** The cycle `coder → sandbox_exec → debugger → coder` with three independent
bounds — `max_debug_iterations`, `max_sandbox_executions`, and the stagnation rule (three
consecutive failures sharing one error fingerprint escalate to a replan rather than spending
the rest of the budget proving the same thing). `reporter` is now `finalizer`'s sole
predecessor, so the deliverable guarantee holds structurally on every terminal path including
cancellation and budget exhaustion.

Three decisions worth recording:

1. **The `@node` decorator gained a declared `fallback`.** `DEGRADE` and
   `SYNTHESISE_FALLBACK` previously meant "the node writes nothing", which for the Debugger
   would have left `debug_iterations` unmoved and spun the loop against the global visit
   budget instead of its own. The fallback is now part of the node's declaration rather than
   a `try` inside its body: a body that handles its own failure can forget to, a declared
   fallback cannot.
2. **The Reporter is not trusted with numbers.** §7.8 asks the model not to invent or round a
   metric; asking is not a control. Sections 5, 6 and 8 and the criteria table are tabulated
   from state and spliced in after generation, and the model is left the job it is good at —
   explaining what happened. A report whose accuracy column disagrees with `metrics.json`
   would be worse than no report.
3. **`determine_outcome` moved from `finalizer` to `criteria`.** The Reporter has to state
   the outcome in its first paragraph and the Finalizer has to write it to `runs.final_state`.
   Two implementations of "did this run succeed" would eventually disagree, and the report
   contradicting the API is the worst possible way to discover it.

Still open from the catalogue: D-005, D-009 and the Low band.

### Phase 3 — Retrieval (week 5)

Ingestion pipeline, hybrid Qdrant collections, payload indexes, `researcher`, `run_memory`,
`/corpus/*` endpoints.
**Exit:** `make bench-rag` reports precision@5 ≥ 0.60; the Debugger demonstrably reuses a prior fix.

### Phase 4 — MLOps (week 6)

MLflow on Postgres, `mlops` node, the full tag taxonomy, flavor-aware model logging, registry with
alias promotion, backfill and GC crons.
**Exit:** `make reproduce RUN_ID=…` reproduces a logged run's metrics exactly.

### Phase 5 — Evaluation and replanning (week 7)

`evaluator` with deterministic criteria checking plus the advisory rubric, the `REFINE`/`REPLAN`
loops, the stagnation guard, `benchmark_results`, the `core-10` suite.
**Exit:** `make bench` produces a scorecard; the three trap cases behave correctly.

### Phase 6 — Real-time frontend (week 8–9)

WebSocket endpoint with the full protocol, Redis Streams, ticket auth, resume; the Next.js
dashboard — run list, live graph view, streaming console, artifact browser, report viewer.
**Exit:** watching a run live in the browser is more informative than reading the logs; a 60-second
screen recording of a full run is committed as `docs/assets/demo.gif` and linked from the README.

### Phase 7 — Observability and hardening (week 10)

Prometheus, Grafana dashboards, structlog, bearer auth, the `hardened` compose profile, the
sandbox-escape suite, full CI (lint, test, build, compose smoke).
**Exit:** `make up PROFILE=observability` gives five working dashboards; CI is green on all five
stages.

### Post-MVP

Ordered by value, not by effort: multi-user auth (ADR-016) · k3s manifests and
`KubernetesSandboxDriver` (ADR-021) · MinIO artifacts ([`MLOPS.md §12.1`](./docs/MLOPS.md#121-to-minio--s3-artifacts))
· plan-level parallelism for independent steps · a plugin interface for custom task kinds ·
fine-tuning a coder model on this platform's own successful runs, which the `run_memory` and
artifact corpus makes directly feasible.

---

## Open questions

Genuinely unresolved. Each names what evidence would settle it.

| # | Question | Settled by |
|---|---|---|
| Q1 | Is `qwen2.5:14b-instruct` worth ~9 GB of VRAM for the Planner over `llama3.1:8b`? | A/B on `core-10`, measuring plan-validation retry rate and first-pass success |
| Q2 | Is `max_debug_iterations = 4` right? | Histogram of iterations-to-success. If ≥ 90% of successes land within 2, lower it and save the budget for replans. |
| Q3 | Does `run_memory` actually reduce debug iterations, or does injecting prior art mislead the Debugger toward a superficially similar but wrong fix? | The Run Memory Lift KPI: `core-10` with memory on vs. off, ≥ 200 memory points |
| Q4 | Should the Coder see the full traceback or only the parsed `ErrorRecord`? Full tracebacks eat ~2k tokens of a 16k context. | A/B on debug success rate at fixed context |
| Q5 | Is 900 s enough for `train`? PyTorch on CPU is slow; a small CNN on MNIST-10k may exceed it. | Timeout rate on `digits-cnn` and `mnist-subset` |
| Q6 | Does the advisory rubric change any routing decision, or does the deterministic table decide everything anyway? | Log the pre- and post-LLM decision; if they never differ, drop the rubric call and save 15 s per run |
| Q7 | Should `sandbox_exec` retry a `TIMEOUT` once with a doubled limit before invoking the Debugger? | Fraction of timeouts that succeed on a longer limit vs. those that are genuine runaway loops |
| Q8 | Is one `run_memory` point per debug cycle the right granularity, or should it be one per *run* summarising the whole debugging arc? | Retrieval precision on the memory collection at ≥ 200 points |

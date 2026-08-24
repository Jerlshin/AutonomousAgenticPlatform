"""Prometheus instrumentation — every metric in `docs/ARCHITECTURE.md` §12.1, in one place.

Two processes expose metrics and they do it differently. The API is ASGI, so `/metrics` is
just another route. The worker is `arq`, which has no HTTP surface at all, so it starts a
dedicated `prometheus_client` server on `WORKER_HEALTH_PORT` (§12.1). Both scrape the same
default registry — they are separate processes, so there is nothing to reconcile.

**Why the definitions live here rather than next to their call sites.** A metric declared
in the module that increments it is a metric nobody can find, and two modules that both
want `pluton_runs_total` get `Duplicated timeseries in CollectorRegistry` at import time.
Declaring the whole surface in one module makes the §12.1 table checkable against the code
by reading, and makes the recording helpers the only thing call sites import.

**Label cardinality is a correctness property, not a style preference.** Prometheus keeps
one time series per label combination, forever, in memory. `task_kind` comes out of a
*model* — a Planner that answers "tabular classification (binary)" instead of
"tabular-classification" would mint a new series on every run until the process dies. So
every free-form value is clamped to a closed vocabulary here, at the boundary, and
anything outside it becomes `other`. The same reasoning is why HTTP routes are labelled
with the *template* (`/api/v1/runs/{run_id}`) and never the path: run ids are UUIDs.

**Instrumentation never fails a request.** Every helper in this module swallows its own
errors — a metrics bug must not be able to take down a run, and a `ValueError` from a
label that turned out to be `None` is a monitoring defect, not an outage.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "PrometheusMiddleware",
    "classify_rejection",
    "node_label",
    "observe_criteria_satisfaction",
    "observe_debug_iterations",
    "observe_node",
    "observe_retrieval",
    "record_cache_lookup",
    "record_http_request",
    "record_llm_call",
    "record_replan",
    "record_run",
    "record_run_memory_lookup",
    "record_sandbox_execution",
    "record_structured_output",
    "record_validation_rejection",
    "record_ws_event",
    "record_ws_replay_gap",
    "render",
    "run_in_flight",
    "sample_queue_depth",
    "set_queue_depth",
    "start_worker_metrics_server",
    "task_kind_label",
    "track_ws_connection",
]


# ------------------------------------------------------------------------------------
#  Registration
# ------------------------------------------------------------------------------------


def _metric(
    factory: Callable[..., Any],
    name: str,
    documentation: str,
    labelnames: Iterable[str] = (),
    **kwargs: Any,
) -> Any:
    """Define a collector, returning the existing one if the name is already registered.

    Module-level metrics are defined exactly once per process in normal operation. Under
    `pytest --cov` and any test that reloads a module, the same definitions run twice
    against a registry that outlives the reload, and `prometheus_client` answers that with
    a `ValueError`. Reusing the registered collector is the behaviour that makes a reload
    a no-op rather than an import error — the alternative is a private registry that no
    exposition endpoint would then be able to see.
    """
    try:
        return factory(name, documentation, labelnames=list(labelnames), **kwargs)
    except ValueError:
        # `_names_to_collectors` is keyed by every *sample* name a collector exposes, so a
        # counter declared as `pluton_runs_total` is reachable under both that name and
        # the `_total`-stripped base the client stores it under.
        registered = getattr(REGISTRY, "_names_to_collectors", {})
        for candidate in (name, name.removesuffix("_total"), f"{name}_total"):
            existing = registered.get(candidate)
            if existing is not None:
                return existing
        raise


# ------------------------------------------------------------------------------------
#  Label vocabularies
# ------------------------------------------------------------------------------------

# The taxonomy the Planner is instructed to classify into (`engine/prompts/planner.md`).
# A model that answers with anything else is a prompt-adherence problem; it must not also
# become an unbounded metric.
TASK_KINDS: frozenset[str] = frozenset(
    {
        "tabular-classification",
        "tabular-regression",
        "image-classification",
        "text-classification",
        "timeseries-forecasting",
        "clustering",
        "dimensionality-reduction",
        "analysis",
    }
)

# The graph's node set (`engine/graph.py`). Doubles as the `role` vocabulary for the LLM
# metrics: the ambient node name is what identifies the caller at the point a model is
# invoked, and for every LLM-driven node it is the role name.
NODE_NAMES: frozenset[str] = frozenset(
    {
        "init",
        "planner",
        "researcher",
        "coder",
        "sandbox_exec",
        "debugger",
        "mlops",
        "evaluator",
        "reporter",
        "finalizer",
    }
)

OTHER = "other"


def _clamp(value: Any, vocabulary: frozenset[str], *, default: str = OTHER) -> str:
    """Map a value onto a closed vocabulary. See the module docstring on cardinality."""
    if value is None:
        return default
    text = str(value).strip()
    return text if text in vocabulary else default


def task_kind_label(value: Any) -> str:
    """`task_kind` clamped to the Planner's taxonomy."""
    return _clamp(value, TASK_KINDS)


def node_label(value: Any) -> str:
    """A node or role name clamped to the graph's node set."""
    return _clamp(value, NODE_NAMES)


# Ordered longest-match-first: `network_import` and `forbidden_import` both mention
# `import`, and the network rule is the more specific finding.
_REJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("the sandbox has no network", "network_import"),
    ("is not permitted in the sandbox", "forbidden_import"),
    ("is not installed in the sandbox", "not_allowlisted"),
    ("relative import", "relative_import"),
    ("os.", "os_attribute"),
    ("defeats the import allowlist", "dynamic_import"),
    ("on a non-literal argument", "dynamic_eval"),
    ("stdin is closed", "stdin"),
    ("/datasets is mounted", "datasets_write"),
    ("the rootfs is", "readonly_write"),
    ("never references /artifacts/metrics.json", "no_metrics_contract"),
    ("over the", "source_too_large"),
    ("SyntaxError", "syntax_error"),
    ("IndentationError", "syntax_error"),
)


def classify_rejection(message: str) -> str:
    """Map a static-validator rejection to a bounded `reason` label.

    The validator's messages are prose written for the Coder to act on — they carry line
    numbers, module names and the full allowlist — so they are exactly the wrong thing to
    use as a label value. This collapses them to the rule that fired, which is the
    question the "validation rejections by reason" panel actually asks.
    """
    text = message or ""
    for needle, reason in _REJECTION_PATTERNS:
        if needle in text:
            return reason
    return OTHER


# ------------------------------------------------------------------------------------
#  Platform metrics  (§12.1)
# ------------------------------------------------------------------------------------

# Runs are measured in minutes, not milliseconds: the default histogram buckets top out at
# 10 s, which would put every run in `+Inf` and make the p95 panel useless.
_RUN_DURATION_BUCKETS = (5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, float("inf"))
_NODE_DURATION_BUCKETS = (
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    30,
    60,
    120,
    300,
    600,
    float("inf"),
)

RUNS_TOTAL = _metric(
    Counter,
    "pluton_runs_total",
    "Terminal run outcomes.",
    ("status", "task_kind"),
)
RUN_DURATION = _metric(
    Histogram,
    "pluton_run_duration_seconds",
    "End-to-end run wall clock.",
    ("status", "task_kind"),
    buckets=_RUN_DURATION_BUCKETS,
)
NODE_DURATION = _metric(
    Histogram,
    "pluton_node_duration_seconds",
    "Per-node latency.",
    ("node", "outcome"),
    buckets=_NODE_DURATION_BUCKETS,
)
NODE_VISITS = _metric(
    Counter,
    "pluton_node_visits_total",
    "Node entries. The cycle-detection signal; see the termination proof in §6.4.",
    ("node",),
)
DEBUG_ITERATIONS = _metric(
    Histogram,
    "pluton_debug_iterations",
    "Self-correction depth per run — a core benchmark KPI.",
    ("task_kind",),
    # MAX_DEBUG_ITERATIONS defaults to 4, so a bucket per iteration is both affordable and
    # the resolution the KPI is stated at: "how many runs needed 0, 1, 2 … fixes".
    buckets=(0, 1, 2, 3, 4, 5, 6, float("inf")),
)
REPLANS_TOTAL = _metric(
    Counter,
    "pluton_replans_total",
    "Evaluator-triggered replans.",
    ("task_kind", "reason"),
)
CRITERIA_SATISFACTION = _metric(
    Histogram,
    "pluton_criteria_satisfaction_ratio",
    "Fraction of required success criteria met at the end of a run.",
    ("task_kind",),
    buckets=(0.0, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0),
)
ACTIVE_RUNS = _metric(
    Gauge,
    "pluton_active_runs",
    "Runs currently executing on this worker.",
    ("worker_id",),
)
QUEUE_DEPTH = _metric(
    Gauge,
    "pluton_queue_depth",
    "Pending arq jobs.",
)
STRUCTURED_OUTPUT_ATTEMPTS = _metric(
    Histogram,
    "pluton_structured_output_attempts",
    "Repair-ladder depth for one structured-output call.",
    ("role", "stage"),
    buckets=(1, 2, 3, 4, float("inf")),
)

# ------------------------------------------------------------------------------------
#  LLM metrics
# ------------------------------------------------------------------------------------

LLM_REQUESTS = _metric(
    Counter,
    "pluton_llm_requests_total",
    "Model invocations by outcome.",
    ("model", "role", "outcome"),
)
LLM_TTFT = _metric(
    Histogram,
    "pluton_llm_ttft_seconds",
    "Time to first token. Only observable on the streaming path.",
    ("model", "role"),
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 20, 60, float("inf")),
)
LLM_TOKENS = _metric(
    Counter,
    "pluton_llm_tokens_total",
    "Tokens consumed and produced.",
    ("model", "role", "direction"),
)
LLM_TOKENS_PER_SECOND = _metric(
    Histogram,
    "pluton_llm_tokens_per_second",
    (
        "Output throughput. On macOS this is the GPU-utilisation proxy (§12.1): a "
        "sustained drop means the model spilled to CPU."
    ),
    ("model",),
    buckets=(1, 5, 10, 15, 20, 30, 50, 80, 120, 200, float("inf")),
)
LLM_CACHE_HITS = _metric(
    Counter,
    "pluton_llm_cache_hits_total",
    "Cache hits, by cache kind.",
    ("kind",),
)

# ------------------------------------------------------------------------------------
#  Sandbox metrics
# ------------------------------------------------------------------------------------

SANDBOX_EXECUTIONS = _metric(
    Counter,
    "pluton_sandbox_executions_total",
    "Sandbox executions by result classification (§10.9).",
    ("profile", "classification"),
)
SANDBOX_DURATION = _metric(
    Histogram,
    "pluton_sandbox_duration_seconds",
    "Sandbox execution wall clock.",
    ("profile",),
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 900, float("inf")),
)
SANDBOX_MAX_RSS = _metric(
    Histogram,
    "pluton_sandbox_max_rss_bytes",
    "Peak container RSS, sampled while the container ran.",
    ("profile",),
    buckets=(
        64 * 2**20,
        128 * 2**20,
        256 * 2**20,
        512 * 2**20,
        2**30,
        2 * 2**30,
        4 * 2**30,
        6 * 2**30,
        8 * 2**30,
        float("inf"),
    ),
)
SANDBOX_TIMEOUTS = _metric(
    Counter,
    "pluton_sandbox_timeouts_total",
    "Executions killed at the wall clock.",
    ("profile",),
)
SANDBOX_OOMS = _metric(
    Counter,
    "pluton_sandbox_oom_total",
    "Executions killed by the cgroup memory limit.",
    ("profile",),
)
SANDBOX_VALIDATION_REJECTIONS = _metric(
    Counter,
    "pluton_sandbox_validation_rejections_total",
    "Static-gate rejections by rule.",
    ("reason",),
)

# ------------------------------------------------------------------------------------
#  Retrieval metrics
# ------------------------------------------------------------------------------------

RETRIEVAL_LATENCY = _metric(
    Histogram,
    "pluton_retrieval_latency_seconds",
    "Vector search latency, embedding included.",
    ("collection",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, float("inf")),
)
RETRIEVAL_HITS = _metric(
    Histogram,
    "pluton_retrieval_hits",
    "Chunks returned per query, after the score floor.",
    ("collection",),
    buckets=(0, 1, 2, 3, 4, 5, 8, 10, 20, float("inf")),
)
RETRIEVAL_TOP_SCORE = _metric(
    Histogram,
    "pluton_retrieval_top_score",
    "Score of the best-ranked hit; 0 when a query returned nothing.",
    ("collection",),
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
RUN_MEMORY_HITS = _metric(
    Counter,
    "pluton_run_memory_hits_total",
    "Episodic-memory lookups that found a prior fix.",
    ("outcome",),
)

# ------------------------------------------------------------------------------------
#  API and WebSocket metrics
# ------------------------------------------------------------------------------------

HTTP_REQUESTS = _metric(
    Counter,
    "pluton_http_requests_total",
    "HTTP requests by templated route.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = _metric(
    Histogram,
    "pluton_http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, float("inf")),
)
WS_CONNECTIONS_ACTIVE = _metric(
    Gauge,
    "pluton_ws_connections_active",
    "Open run-stream WebSocket connections on this process.",
)
WS_EVENTS_SENT = _metric(
    Counter,
    "pluton_ws_events_sent_total",
    "Frames written to run-stream sockets.",
    ("type",),
)
WS_REPLAY_GAPS = _metric(
    Counter,
    "pluton_ws_replay_gaps_total",
    "Reconnects whose cursor had fallen off the retained end of the stream.",
)


# ------------------------------------------------------------------------------------
#  Recording helpers
# ------------------------------------------------------------------------------------
#
# Every one of these is `try/except`-wrapped at the boundary by `_safe`. See the module
# docstring: a monitoring defect must never surface as a failed run.


def _safe(fn: Callable[..., None]) -> Callable[..., None]:
    """Swallow and log anything an instrumentation call raises."""

    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - instrumentation is never load-bearing
            logger.debug("Metric %s failed: %s", getattr(fn, "__name__", "?"), exc)

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_safe
def record_run(status: str, task_kind: Any, duration_s: float | None = None) -> None:
    """One terminal run outcome, with its wall clock when the run was timed."""
    kind = task_kind_label(task_kind)
    RUNS_TOTAL.labels(status=str(status), task_kind=kind).inc()
    if duration_s is not None:
        RUN_DURATION.labels(status=str(status), task_kind=kind).observe(
            max(duration_s, 0.0)
        )


@_safe
def observe_node(node: str, outcome: str, duration_s: float) -> None:
    """One node execution: its latency and its entry, from the `@node` envelope."""
    name = node_label(node)
    NODE_DURATION.labels(node=name, outcome=str(outcome)).observe(max(duration_s, 0.0))
    NODE_VISITS.labels(node=name).inc()


@_safe
def observe_debug_iterations(task_kind: Any, iterations: int) -> None:
    """How deep the self-correction loop went on one run."""
    DEBUG_ITERATIONS.labels(task_kind=task_kind_label(task_kind)).observe(
        max(int(iterations), 0)
    )


@_safe
def record_replan(task_kind: Any, reason: str) -> None:
    """One Evaluator-triggered replan. `reason` is a decision name, never a directive."""
    REPLANS_TOTAL.labels(task_kind=task_kind_label(task_kind), reason=str(reason)).inc()


@_safe
def observe_criteria_satisfaction(task_kind: Any, ratio: float) -> None:
    """The fraction of required criteria a finished run met, clamped to [0, 1]."""
    CRITERIA_SATISFACTION.labels(task_kind=task_kind_label(task_kind)).observe(
        min(max(float(ratio), 0.0), 1.0)
    )


@contextmanager
def run_in_flight(worker_id: str) -> Any:
    """Hold `pluton_active_runs` up for the duration of one run.

    A context manager rather than paired inc/dec calls because the decrement has to
    survive every exit path a run has — cancellation, worker shutdown, an exception out of
    the graph — and a gauge that leaks on the failure paths reads as permanent saturation.
    """
    label = str(worker_id or "unknown")
    try:
        ACTIVE_RUNS.labels(worker_id=label).inc()
    except Exception as exc:  # noqa: BLE001
        logger.debug("active-runs gauge failed to increment: %s", exc)
    try:
        yield
    finally:
        try:
            ACTIVE_RUNS.labels(worker_id=label).dec()
        except Exception as exc:  # noqa: BLE001
            logger.debug("active-runs gauge failed to decrement: %s", exc)


@_safe
def set_queue_depth(depth: int) -> None:
    """Publish the pending-job count sampled from the arq queue."""
    QUEUE_DEPTH.set(max(int(depth), 0))


async def sample_queue_depth(client: Any, queue_name: str = "arq:queue") -> int | None:
    """Read the arq queue depth from Redis and publish it. Returns the depth, or None.

    arq's queue is a sorted set scored by "run me at or after this time", so `ZCARD` is
    the pending count including delayed jobs. Sampled rather than collected at scrape time
    because the Redis client is async and a `prometheus_client` collector is called from
    the exposition thread, which has no event loop to drive it.
    """
    try:
        depth = int(await client.zcard(queue_name))
    except Exception as exc:  # noqa: BLE001 - a Redis blip is not a worker failure
        logger.debug("Could not sample queue depth: %s", exc)
        return None
    set_queue_depth(depth)
    return depth


@_safe
def record_structured_output(role: Any, stage: str, attempts: int) -> None:
    """The repair-ladder depth of one structured call, and which stage ended it.

    `stage` is where the ladder stopped, not where it started: `constrained` means the
    first response validated, `repair` means at least one repair round was needed, and
    `exhausted` means it never validated and the caller got a `StructuredOutputError`.
    Splitting the depth histogram this way is what separates "the model needs one nudge"
    from "the model cannot produce this schema at all", which are different problems.
    """
    STRUCTURED_OUTPUT_ATTEMPTS.labels(role=node_label(role), stage=str(stage)).observe(
        max(int(attempts), 1)
    )


@_safe
def record_llm_call(
    *,
    model: str,
    role: Any,
    outcome: str,
    duration_s: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    ttft_s: float | None = None,
) -> None:
    """One model invocation, with everything §12.1's LLM table asks for.

    `ttft_s` is `None` on the non-streaming path and nothing is observed for it there.
    Recording the full round trip as a time-to-first-token would make the TTFT panel read
    as a latency panel for exactly the calls that are slowest, which is worse than a gap.
    """
    model_name = str(model or "unknown")
    role_name = node_label(role)

    LLM_REQUESTS.labels(model=model_name, role=role_name, outcome=str(outcome)).inc()
    if tokens_in:
        LLM_TOKENS.labels(model=model_name, role=role_name, direction="in").inc(
            tokens_in
        )
    if tokens_out:
        LLM_TOKENS.labels(model=model_name, role=role_name, direction="out").inc(
            tokens_out
        )
    if ttft_s is not None:
        LLM_TTFT.labels(model=model_name, role=role_name).observe(max(ttft_s, 0.0))
    if tokens_out and duration_s > 0:
        LLM_TOKENS_PER_SECOND.labels(model=model_name).observe(tokens_out / duration_s)


@_safe
def record_cache_lookup(kind: str, hit: bool) -> None:
    """A hit on the `llm` or `embed` cache. Misses are not counted — §12.1 asks for hits."""
    if hit:
        LLM_CACHE_HITS.labels(kind=str(kind)).inc()


@_safe
def record_sandbox_execution(
    *,
    profile: str,
    classification: str,
    duration_s: float | None = None,
    max_rss_bytes: int | None = None,
    timed_out: bool = False,
    oom_killed: bool = False,
) -> None:
    """One sandbox execution, whatever it did.

    Timeout and OOM get their own counters *as well as* a classification label because
    they are the two failures an operator alerts on, and an alert expression should not
    have to know that `classification="TIMEOUT"` is the same event.
    """
    name = str(profile)
    SANDBOX_EXECUTIONS.labels(profile=name, classification=str(classification)).inc()
    if duration_s is not None:
        SANDBOX_DURATION.labels(profile=name).observe(max(duration_s, 0.0))
    if max_rss_bytes:
        SANDBOX_MAX_RSS.labels(profile=name).observe(max_rss_bytes)
    if timed_out:
        SANDBOX_TIMEOUTS.labels(profile=name).inc()
    if oom_killed:
        SANDBOX_OOMS.labels(profile=name).inc()


@_safe
def record_validation_rejection(rejections: Iterable[str]) -> None:
    """Every rejection from one static-gate report, bucketed by the rule that fired."""
    for message in rejections:
        SANDBOX_VALIDATION_REJECTIONS.labels(reason=classify_rejection(message)).inc()


@_safe
def observe_retrieval(
    collection: str, *, duration_s: float, scores: list[float]
) -> None:
    """One vector search: its latency, how much it returned, and how good the best hit was.

    A query that returns nothing still observes a top score of 0 rather than skipping the
    histogram. "No hits" is the retrieval failure mode worth seeing, and omitting those
    queries would make the score distribution describe only the queries that worked.
    """
    name = str(collection)
    RETRIEVAL_LATENCY.labels(collection=name).observe(max(duration_s, 0.0))
    RETRIEVAL_HITS.labels(collection=name).observe(len(scores))
    RETRIEVAL_TOP_SCORE.labels(collection=name).observe(max(scores) if scores else 0.0)


@_safe
def record_run_memory_lookup(hits: int) -> None:
    """Whether episodic memory had a prior fix for this error."""
    RUN_MEMORY_HITS.labels(outcome="hit" if hits else "miss").inc()


@_safe
def record_http_request(
    method: str, route: str, status: int, duration_s: float
) -> None:
    """One HTTP request. `route` must already be a template — see the module docstring."""
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(
        max(duration_s, 0.0)
    )


@_safe
def record_ws_event(event_type: str) -> None:
    """One frame written to a run-stream socket."""
    WS_EVENTS_SENT.labels(type=str(event_type)).inc()


@_safe
def record_ws_replay_gap() -> None:
    """A reconnect whose cursor had fallen off the retained end of the stream."""
    WS_REPLAY_GAPS.inc()


@contextmanager
def track_ws_connection() -> Any:
    """Hold `pluton_ws_connections_active` up for the life of one socket."""
    try:
        WS_CONNECTIONS_ACTIVE.inc()
    except Exception as exc:  # noqa: BLE001
        logger.debug("ws-connections gauge failed to increment: %s", exc)
    try:
        yield
    finally:
        try:
            WS_CONNECTIONS_ACTIVE.dec()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ws-connections gauge failed to decrement: %s", exc)


# ------------------------------------------------------------------------------------
#  Exposition
# ------------------------------------------------------------------------------------


def render() -> tuple[bytes, str]:
    """The scrape body and its content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# Routes whose own latency is noise: `/metrics` measured by the scrape it is answering,
# and the health probe every orchestrator hits on a five-second interval. Both would
# dominate the request-rate panel while telling nobody anything.
_UNMEASURED_ROUTES = frozenset({"/metrics", "/api/v1/health"})


class PrometheusMiddleware:
    """Pure-ASGI HTTP instrumentation.

    Pure ASGI rather than `BaseHTTPMiddleware` for two reasons. `BaseHTTPMiddleware` runs
    the downstream app in a task group and buffers the response through a memory stream,
    which is measurable overhead on the streaming endpoints — and, more importantly, it
    breaks the trick this class depends on: Starlette's router writes the matched `route`
    into the *same* scope dict, so after the downstream call returns, `scope["route"]`
    names the template. Reading it afterwards is what keeps run ids out of the labels
    without this middleware having to duplicate the routing table.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_holder = {"code": 500}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The exception still propagates; recording first means a handler that blew up
            # appears in the 500 rate rather than vanishing from the request count.
            self._record(scope, 500, time.perf_counter() - started)
            raise
        self._record(scope, status_holder["code"], time.perf_counter() - started)

    @staticmethod
    def _record(scope: Any, status: int, duration_s: float) -> None:
        route = _route_label(scope)
        if route in _UNMEASURED_ROUTES:
            return
        record_http_request(
            method=str(scope.get("method", "GET")),
            route=route,
            status=status,
            duration_s=duration_s,
        )


def _route_label(scope: Any) -> str:
    """The matched route's full template, or `unmatched` for a request that hit no route.

    Never the raw path: `/api/v1/runs/{uuid}` would mint a series per run and the process
    would grow without bound (§12.1's cardinality note).

    Built by substituting `path_params` back out of the request path rather than by reading
    `scope["route"].path`. Under FastAPI's lazy router inclusion, the route object in the
    scope is the one declared on the *sub-router*, so its `.path` is prefix-less —
    `/{task_id}` rather than `/api/v1/tasks/{task_id}` — and two routers that both declare
    a single path parameter would share one series. Substitution uses only public scope
    keys and reconstructs the full template.
    """
    route = scope.get("route")
    if route is None:
        return "unmatched"

    path = str(scope.get("path") or "/")
    params = scope.get("path_params") or {}
    if not params:
        return path

    # Whole *segments* only. Replacing the substring anywhere in the path would rewrite a
    # static segment that happened to equal a parameter value.
    replacements = {
        str(value): "{" + name + "}" for name, value in params.items() if str(value)
    }
    label = "/".join(replacements.get(segment, segment) for segment in path.split("/"))

    if any(raw in label for raw in replacements):
        # A converter whose value spans a slash (`{p:path}`) defeats segment matching, and
        # a leftover raw value is exactly the unbounded-cardinality case this function
        # exists to prevent. The sub-router's own template is prefix-less but bounded,
        # which is the right way to be wrong here.
        return str(getattr(route, "path", None) or "unmatched")
    return label


_worker_server_started = False


def start_worker_metrics_server(port: int) -> bool:
    """Expose `/metrics` on `port` from a background thread. Returns whether it started.

    arq has no ASGI surface, so this is the worker's only exposition path (§12.1).
    `start_http_server` spawns a daemon thread, which is safe here precisely because
    `prometheus_client`'s collectors are thread-safe and the worker never mutates a metric
    from the exposition thread — it only reads.

    Idempotent, and non-fatal on failure: a worker that cannot bind its metrics port has a
    monitoring problem, not an execution problem, and refusing to start runs over it would
    be the wrong trade on a single-box platform.
    """
    global _worker_server_started
    if _worker_server_started:
        return True
    try:
        from prometheus_client import start_http_server

        start_http_server(port)
    except OSError as exc:
        logger.warning(
            "Could not bind the worker metrics server on port %s: %s. Metrics from this "
            "worker will not be scrapeable.",
            port,
            exc,
        )
        return False
    _worker_server_started = True
    logger.info("Worker metrics server listening on :%s/metrics", port)
    return True

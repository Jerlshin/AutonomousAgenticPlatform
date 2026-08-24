"""LangGraph channel definitions for the Pluton agent graph.

Normative specification: `docs/AGENTS.md` §3. LangGraph requires a `TypedDict` for the
channel definition, so `AgentState` is a flat TypedDict whose non-trivial fields are
Pydantic models — they validate on construction and serialise cleanly through the
Postgres checkpointer.

Two rules govern everything in this module:

* **Every channel has exactly one owning node** (`AGENTS.md` §3.5). Two nodes writing one
  channel is the source of nearly every state-corruption bug in a multi-agent graph.
* **History channels accumulate, they never overwrite.** `errors` and `code_revisions` use
  `append` because the debug loop's escape hatch — "this is the third failure with the same
  fingerprint, the approach is wrong" — reads exactly the history that last-write-wins
  would destroy.

Every section is live as of phase 5. The channels were declared up front, before the nodes
that write them existed, because the schema is normative and a checkpoint written by an
earlier phase has to stay loadable by a later one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator

# ------------------------------------------------------------------------------------
#  Enumerations  (AGENTS.md §3.1)
# ------------------------------------------------------------------------------------


class RunPhase(StrEnum):
    """Coarse progress signal for the UI, set by every node on entry."""

    INIT = "INIT"
    PLANNING = "PLANNING"
    RESEARCH = "RESEARCH"
    IMPLEMENT = "IMPLEMENT"
    EXECUTE = "EXECUTE"
    DEBUG = "DEBUG"
    TRACK = "TRACK"
    EVALUATE = "EVALUATE"
    REPORT = "REPORT"
    COMPLETE = "COMPLETE"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class StepKind(StrEnum):
    RESEARCH = "research"
    IMPLEMENT = "implement"
    TRAIN = "train"
    EVALUATE = "evaluate"
    REPORT = "report"


class ErrorKind(StrEnum):
    SYNTAX = "syntax"
    IMPORT = "import"
    NAME = "name"
    TYPE = "type"
    VALUE = "value"
    SHAPE = "shape"  # numpy/torch dimension mismatches — the most common ML bug
    DATA = "data"  # missing file, wrong column, NaN
    RUNTIME = "runtime"
    ASSERTION = "assertion"
    TIMEOUT = "timeout"
    OOM = "oom"
    CONTRACT_VIOLATION = (
        "contract_violation"  # ran fine but produced no valid metrics.json
    )
    VALIDATION_REJECTED = "validation_rejected"  # static gate refused to launch
    UNKNOWN = "unknown"


class EvalDecision(StrEnum):
    ACCEPT = "ACCEPT"  # criteria met → reporter
    REFINE = "REFINE"  # close; same plan, better code → coder
    REPLAN = "REPLAN"  # approach is wrong → planner
    ABORT = "ABORT"  # unrecoverable or budget spent → reporter with PARTIAL/FAILED


class RunOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# The classification vocabulary `sandbox_exec` routes on (ARCHITECTURE.md §10.9). It is a
# Literal rather than an Enum because it is also a JSON wire value in the API and the
# WebSocket envelope, and the routers compare it as a string.
Classification = Literal[
    "CLEAN",
    "RUNTIME_ERROR",
    "TIMEOUT",
    "OOM",
    "CONTRACT_VIOLATION",
    "VALIDATION_REJECTED",
    "UNKNOWN_FAILURE",
]

SandboxProfileName = Literal["exec", "train", "train-tracked"]

# The API-facing artifact vocabulary. Narrower than the MLflow artifact types in
# MLOPS.md §6, which `ArtifactRef` carries.
DeliverableType = Literal["code", "model", "plot", "report", "metrics", "log", "bundle"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------
#  Value objects  (AGENTS.md §3.2)
# ------------------------------------------------------------------------------------


class SuccessCriterion(BaseModel):
    """A machine-checkable acceptance condition emitted by the Planner.

    This is the contract that makes evaluation objective. Criteria are checked
    arithmetically from `metrics.json` by `engine.criteria.check_criteria`; no model ever
    decides whether a criterion was met.
    """

    id: str
    metric: str  # must match a key in metrics.json.metrics
    comparator: Literal["gte", "lte", "gt", "lt", "eq", "approx"]
    threshold: float
    tolerance: float = 0.0  # only meaningful for "approx"
    required: bool = True  # required=False criteria are aspirational
    weight: float = Field(default=1.0, ge=0.0)
    rationale: str = ""

    @field_validator("metric")
    @classmethod
    def not_a_training_metric(cls, v: str) -> str:
        """Reject `train_`-prefixed metrics (MLOPS.md §5.1).

        The most embarrassing possible outcome for an autonomous system is declaring
        victory on training accuracy, so the prefix is refused at the schema boundary
        rather than caught later by review.
        """
        if v.startswith("train_"):
            raise ValueError(
                f"success criterion targets '{v}', a training-set metric. Criteria must "
                "be held-out metrics; train_* names exist only for overfitting diagnosis."
            )
        return v


class CriterionResult(BaseModel):
    criterion_id: str
    metric: str
    comparator: str
    threshold: float
    observed: float | None  # None when the metric was never produced
    passed: bool
    required: bool
    weight: float
    note: str = ""


class DatasetBinding(BaseModel):
    """Binds a plan step to a concrete entry in /datasets/manifest.json."""

    dataset_id: str
    path: str
    sha256: str
    task_kind: str
    n_samples: int | None = None
    target_column: str | None = None


class PlanStep(BaseModel):
    id: str  # "s1", "s2", …
    index: int
    title: str = Field(max_length=120)
    description: str
    kind: StepKind
    depends_on: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)  # prose checks for the Reporter
    dataset: DatasetBinding | None = None  # REQUIRED when kind == TRAIN
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    notes: str = ""

    @field_validator("depends_on")
    @classmethod
    def no_self_dependency(cls, v: list[str], info: Any) -> list[str]:
        if info.data.get("id") in v:
            raise ValueError("a step cannot depend on itself")
        return v


class Plan(BaseModel):
    steps: list[PlanStep]
    success_criteria: list[SuccessCriterion]
    task_kind: str
    primary_metric: str  # the headline number for the report and MLflow
    assumptions: list[str] = Field(default_factory=list)
    revision: int = 1

    @field_validator("steps")
    @classmethod
    def acyclic_and_ordered(cls, steps: list[PlanStep]) -> list[PlanStep]:
        ids = {s.id for s in steps}
        seen: set[str] = set()
        for s in steps:
            missing = set(s.depends_on) - ids
            if missing:
                raise ValueError(
                    f"step {s.id} depends on unknown steps: {sorted(missing)}"
                )
            if not set(s.depends_on) <= seen:
                raise ValueError(f"step {s.id} depends on a step that comes after it")
            seen.add(s.id)
        return steps

    def step(self, step_id: str | None) -> PlanStep | None:
        """The step with `step_id`, or None. Convenience for nodes and routers."""
        if step_id is None:
            return None
        return next((s for s in self.steps if s.id == step_id), None)


class RetrievedChunk(BaseModel):
    point_id: str
    collection: Literal["rd_corpus", "code_exemplars", "run_memory"]
    score: float
    source_uri: str
    title: str = ""
    section: str = ""
    text: str
    trust_level: Literal["curated", "verified", "untrusted"] = "curated"


class ContextPack(BaseModel):
    """The Researcher's output. Extractive and cited — never a free-form summary."""

    query_plan: list[str] = Field(default_factory=list)
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    key_facts: list[str] = Field(default_factory=list)
    api_signatures: list[str] = Field(default_factory=list)  # verbatim, never generated
    citations: dict[str, list[str]] = Field(default_factory=dict)
    sufficiency: Literal["sufficient", "partial", "insufficient"] = "insufficient"
    gaps: list[str] = Field(default_factory=list)


class CodeRevision(BaseModel):
    revision: int
    path: str = "main.py"
    language: Literal["python"] = "python"
    content: str
    requirements: list[str] = Field(default_factory=list)
    sha256: str
    rationale: str = ""  # what changed and why, vs the previous revision
    addresses_error: str | None = None  # ErrorRecord.fingerprint being fixed
    created_at: datetime = Field(default_factory=_utcnow)


class ValidationReport(BaseModel):
    passed: bool
    rejections: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    imports_seen: list[str] = Field(default_factory=list)
    writes_metrics_json: bool = False


class ErrorRecord(BaseModel):
    kind: ErrorKind
    fingerprint: str  # stable across incidental detail; ARCHITECTURE.md §7.3.3
    exception_type: str = ""
    message: str
    traceback: str = ""
    file: str | None = None
    line: int | None = None
    offending_source: str | None = None  # ±5 lines around the failure
    revision: int
    occurred_at: datetime = Field(default_factory=_utcnow)


class SandboxOutcome(BaseModel):
    execution_id: uuid.UUID
    profile: SandboxProfileName
    classification: Classification
    exit_code: int | None
    duration_ms: int
    max_rss_bytes: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_ref: str = ""
    stderr_ref: str = ""
    metrics: dict[str, Any] | None = None  # validated metrics.json payload
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    validation: ValidationReport
    revision: int


class Diagnosis(BaseModel):
    """The Debugger's output. A directive for the Coder, not a patch."""

    error_fingerprint: str
    root_cause: str
    evidence: list[str] = Field(default_factory=list)
    fix_strategy: str
    targeted_changes: list[str] = Field(default_factory=list)
    prior_art: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_replan: bool = False
    requires_research: bool = False


class MLflowRef(BaseModel):
    experiment_id: str
    experiment_name: str
    run_id: str
    parent_run_id: str | None = None
    artifact_uri: str
    ui_url: str
    logged_metrics: dict[str, float] = Field(default_factory=dict)
    logged_params: dict[str, str] = Field(default_factory=dict)
    registered_model: str | None = None
    model_version: str | None = None


class RubricScore(BaseModel):
    dimension: Literal[
        "methodology",
        "code_quality",
        "metric_validity",
        "reproducibility",
        "goal_alignment",
    ]
    score: int = Field(ge=1, le=5)
    justification: str


class Verdict(BaseModel):
    decision: EvalDecision
    passed: bool  # all required criteria satisfied
    score: float = Field(ge=0.0, le=1.0)  # weighted criteria satisfaction
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    rubric: list[RubricScore] = Field(default_factory=list)
    rubric_mean: float | None = None
    replan_directive: str | None = None  # required when decision == REPLAN
    refine_directive: str | None = None  # required when decision == REFINE
    summary: str = ""


class Deliverable(BaseModel):
    artifact_id: uuid.UUID | None = None
    name: str
    artifact_type: Literal[
        "code", "model", "plot", "report", "metrics", "log", "bundle"
    ]
    path: str
    sha256: str
    size_bytes: int
    mime_type: str


class Budgets(BaseModel):
    max_debug_iterations: int = 4
    max_replans: int = 2
    max_node_visits: int = 60
    max_sandbox_executions: int = 12
    wallclock_seconds: int = 1800
    max_tokens: int = 250_000


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    node_visits: int = 0
    sandbox_executions: int = 0
    started_at: datetime | None = None

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (
            datetime.now(tz=self.started_at.tzinfo) - self.started_at
        ).total_seconds()

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out


# ------------------------------------------------------------------------------------
#  Reducers  (AGENTS.md §3.3)
# ------------------------------------------------------------------------------------


def last_write_wins(_current: Any, new: Any) -> Any:
    """Default: the latest node to write the channel owns it."""
    return new


def append(current: list | None, new: list | Any) -> list:
    """Accumulate history. Used for revisions, errors, sandbox outcomes, verdicts."""
    base = list(current or [])
    return base + (list(new) if isinstance(new, list) else [new])


def merge_usage(current: Usage | None, new: Usage) -> Usage:
    """Additively accumulate counters; `started_at` is set once and never moved."""
    if current is None:
        return new
    return Usage(
        tokens_in=current.tokens_in + new.tokens_in,
        tokens_out=current.tokens_out + new.tokens_out,
        llm_calls=current.llm_calls + new.llm_calls,
        node_visits=current.node_visits + new.node_visits,
        sandbox_executions=current.sandbox_executions + new.sandbox_executions,
        started_at=current.started_at or new.started_at,
    )


def merge_step_status(
    current: dict[str, StepStatus] | None, new: dict[str, StepStatus]
) -> dict[str, StepStatus]:
    return {**(current or {}), **new}


# ------------------------------------------------------------------------------------
#  AgentState  (AGENTS.md §3.4)
# ------------------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    """Central channel definition for the Pluton agent graph.

    Every field is a LangGraph channel. Fields without an `Annotated` reducer use
    last-write-wins. Fields carrying history use `append`.
    """

    # ---- Identity (write-once at graph entry) --------------------------------
    run_id: str
    task_id: str
    thread_id: str
    prompt: str
    task_kind: str

    # ---- Conversation -------------------------------------------------------
    messages: Annotated[list[BaseMessage], add_messages]

    # ---- Planning -----------------------------------------------------------
    plan: Plan | None
    plan_history: Annotated[list[Plan], append]
    current_step_id: str | None
    step_status: Annotated[dict[str, StepStatus], merge_step_status]

    # ---- Research -----------------------------------------------------------
    context_pack: ContextPack | None
    context_history: Annotated[list[ContextPack], append]

    # ---- Code ---------------------------------------------------------------
    current_revision: CodeRevision | None
    code_revisions: Annotated[list[CodeRevision], append]

    # ---- Execution ----------------------------------------------------------
    last_outcome: SandboxOutcome | None
    outcomes: Annotated[list[SandboxOutcome], append]

    # ---- Debugging ----------------------------------------------------------
    last_error: ErrorRecord | None
    errors: Annotated[list[ErrorRecord], append]
    last_diagnosis: Diagnosis | None
    diagnoses: Annotated[list[Diagnosis], append]

    # ---- MLOps --------------------------------------------------------------
    mlflow: MLflowRef | None
    mlflow_history: Annotated[list[MLflowRef], append]

    # ---- Evaluation ---------------------------------------------------------
    verdict: Verdict | None
    verdicts: Annotated[list[Verdict], append]

    # ---- Output -------------------------------------------------------------
    report_markdown: str | None
    deliverables: Annotated[list[Deliverable], append]
    outcome: RunOutcome | None

    # ---- Control ------------------------------------------------------------
    phase: RunPhase
    debug_iterations: int
    replan_count: int
    budgets: Budgets
    usage: Annotated[Usage, merge_usage]
    cancel_requested: bool
    hitl_gates: list[str]
    pending_gate: str | None

    # ---- Diagnostics --------------------------------------------------------
    model_routing: dict[str, str]
    metadata: dict[str, Any]


# A dependency is satisfied when it succeeded or when it was deliberately skipped.
# SKIPPED must not block: it means "this step was not run on purpose" — a research step
# in a phase with no Researcher, say — and treating it as unsatisfied would stall the
# plan on a step nobody intends to execute.
_SATISFIED = frozenset({StepStatus.SUCCEEDED, StepStatus.SKIPPED})


def next_pending_step(state: AgentState) -> PlanStep | None:
    """The first PENDING step whose dependencies are all satisfied (AGENTS.md §5.1).

    Lives here rather than in the routers because both `planner` (choosing the first step)
    and the routers (choosing the next) need exactly this definition of "next step", and
    two definitions would eventually disagree.
    """
    plan = state.get("plan")
    if plan is None:
        return None
    status = state.get("step_status") or {}

    def status_of(step_id: str) -> StepStatus:
        declared = plan.step(step_id)
        return status.get(step_id, declared.status if declared else StepStatus.PENDING)

    for step in plan.steps:
        if status_of(step.id) is not StepStatus.PENDING:
            continue
        if all(status_of(dep) in _SATISFIED for dep in step.depends_on):
            return step
    return None


def refine_cycles(verdicts: list[Verdict] | None) -> int:
    """How many `REFINE` verdicts a run has recorded (AGENTS.md §6.2).

    Loop 2 shares loop 1's `max_debug_iterations` bound, but `debug_iterations` is the
    Debugger's channel to write (§3.5) — the Evaluator cannot increment it. It counts its
    own cycles out of the verdict history instead, and this is the one definition of that
    count, shared by the node that spends the budget and the router that enforces it.

    Callers must be explicit about which side of the current verdict they are on: inside
    `evaluator_node` the verdict being formed is not in `verdicts` yet, so every REFINE
    here has already been acted on; inside `route_after_eval` the newest one has not, so
    the router discounts it.
    """
    return sum(1 for v in (verdicts or []) if v.decision is EvalDecision.REFINE)

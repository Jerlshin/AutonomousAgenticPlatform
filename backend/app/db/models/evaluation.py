"""Evaluation ORM model — one Evaluator verdict, durably recorded (ARCHITECTURE.md §7.1).

One row per `Verdict` the `evaluator` node emits, which is one row per pass through the
quality loop rather than one per run: a run that refines twice before it is accepted leaves
three rows, and the sequence of them is the record of *how* the run converged. That is what
`GET /runs/{run_id}/evaluation` reads, and what makes the "Criteria Satisfaction" KPI
(`AGENTS.md` §13.1) a queryable trend rather than a number recomputed from checkpoints.

**`passed` and `score` are arithmetic, not judgement.** Both come from
`engine.criteria.check_criteria` over `metrics.json` (`AGENTS.md` §7.6 stage 1). The LLM
rubric is stored beside them in `rubric_scores` and is explicitly advisory — it informs the
routing decision and the report's narrative, and it can never move `passed`. Storing them
in one row makes that separation auditable after the fact: a reviewer can see the rubric
that argued for a refinement and the arithmetic that refused to call it a pass.

**Deviations from the normative schema.** Two, both documented on
`app/db/models/experiment.py` as well:

* ARCHITECTURE.md §7.1 keys `evaluations` off `runs.id`, but this codebase has not yet
  built the `runs` table — today a Task is a Run. `task_id` is the foreign key, matching
  every other table in this package, and `run_id` is carried alongside as a plain string so
  a future `runs` table can absorb the key without a data migration.
* The normative table has `replan_directive` only. `refine_directive` is added beside it:
  the quality loop (§6.2) is driven entirely by that field, and a table that records why a
  run was replanned but not why it was refined loses half the history the loop produces.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.task import Task

# The `eval_decision_enum` of ARCHITECTURE.md §7.1, spelled as literal values rather than
# by importing `engine.state.EvalDecision`: the persistence layer does not depend on the
# engine in this package, and the two are kept in step by `test_evaluation_decisions_match_the_enum`.
EVAL_DECISIONS: tuple[str, ...] = ("ACCEPT", "REFINE", "REPLAN", "ABORT")


class Evaluation(Base):
    """One Evaluator verdict: the arithmetic, the advisory rubric, and the decision."""

    __tablename__ = "evaluations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the evaluation row.",
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Foreign key referencing the owning Task (stands in for the not-yet-built Run).",
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="The engine's run identity (AgentState.run_id).",
    )
    revision: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        comment="The code revision this verdict judged; pairs the row with experiments.revision.",
    )
    decision: Mapped[str] = mapped_column(
        SQLEnum(*EVAL_DECISIONS, name="eval_decision_enum"),
        nullable=False,
        comment="ACCEPT, REFINE, REPLAN or ABORT (AGENTS.md §7.6 decision table).",
    )
    passed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="Whether every required criterion was met. Arithmetic from metrics.json — "
        "never model output.",
    )
    score: Mapped[Decimal] = mapped_column(
        Numeric(6, 4),
        nullable=False,
        comment="Fraction of total criterion weight earned (0.0000–1.0000).",
    )
    criteria_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="Per-criterion outcome: metric, comparator, threshold, observed, passed.",
    )
    rubric_scores: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="The advisory 5-dimension LLM rubric. NULL when the rubric call failed and "
        "the deterministic criteria alone decided (failure policy DEGRADE_DETERMINISTIC).",
    )
    replan_directive: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="What the next plan must do differently. Set when decision == REPLAN.",
    )
    refine_directive: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="The quantitative gap and the change that should close it. Set when "
        "decision == REFINE (additive to the normative schema; see the module docstring).",
    )
    summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="One-line statement of the verdict, as it appears in the report.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="UTC timestamp this verdict was recorded.",
    )

    task: Mapped["Task"] = relationship("Task")

    def __repr__(self) -> str:
        return (
            f"<Evaluation run_id={self.run_id} decision={self.decision} "
            f"passed={self.passed} score={self.score}>"
        )

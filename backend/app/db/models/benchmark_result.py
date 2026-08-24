"""BenchmarkResult ORM model — one scored benchmark case (AGENTS.md §13, ARCHITECTURE.md §7.1).

One row per case per suite execution. The KPI table in `AGENTS.md` §13.1 is queries over
this table: Task Success Rate is `SUCCEEDED / total` on `core-10`, and the Judgement Score
is the pass count over the three trap cases — which is only answerable per case, so the row
is keyed by `case_id` rather than aggregated at write time.

`passed` is the conjunction of every expectation the suite declared for the case, and
`checks` records them individually. That split matters: a case that reached `SUCCEEDED` but
missed its accuracy floor and a case that crashed both have `passed = false`, and a
scorecard that cannot tell them apart cannot tell a regression from an outage.

**Deviations from the normative schema**, both additive:

* `outcome` — the `RunOutcome` the run actually reached. §13.1's Task Success Rate is
  defined over outcomes, not over expectation matches, and deriving one from the other is
  not possible for a case whose expectation *is* `PARTIAL` (`impossible-target`).
* `checks` — per-expectation results, so a failed case is diagnosable from the row rather
  than only from the run it came from, which the 7-day run retention will eventually sweep.

`task_id` is nullable with `ON DELETE SET NULL`, matching the normative `run_id` column:
benchmark history outlives the runs it was measured from, and a swept run must leave the
score behind rather than taking it with it.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.task import Task


class BenchmarkResult(Base):
    """One case of one benchmark suite execution, scored against its expectations."""

    __tablename__ = "benchmark_results"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique identifier for the benchmark result row.",
    )
    suite: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Suite name, e.g. 'core-10' (benchmarks/suites/{suite}.yaml).",
    )
    case_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Case id within the suite, e.g. 'bc-logreg'.",
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="The Task this case ran as, or NULL once that run has been swept.",
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        comment="The engine's run identity for this case's run.",
    )
    outcome: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="RunOutcome the run reached: SUCCEEDED, PARTIAL, FAILED or CANCELLED.",
    )
    passed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="Whether every declared expectation for this case held.",
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="Observed metrics.json metrics plus platform counters for this case.",
    )
    checks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="Per-expectation results: name, passed, detail (additive to the normative "
        "schema; see the module docstring).",
    )
    duration_seconds: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Wall-clock time this case took, for the Median Run Duration KPI.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        comment="UTC timestamp this case was scored.",
    )

    task: Mapped["Task | None"] = relationship("Task")

    def __repr__(self) -> str:
        return (
            f"<BenchmarkResult suite={self.suite} case={self.case_id} "
            f"passed={self.passed} outcome={self.outcome}>"
        )

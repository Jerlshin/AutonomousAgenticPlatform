"""Evaluation and replanning: evaluations, benchmark_results.

Revision ID: 0004_evaluation
Revises: 0003_mlops
Create Date: 2026-08-24 19:05:00.000000+00:00

The two Postgres tables Phase 5 adds (ARCHITECTURE.md §7.1, AGENTS.md §7.6 and §13).

`evaluations` holds one row per `Verdict` — one per pass through the quality loop, not one
per run — so the sequence of rows is the record of how a run converged. `passed` and
`score` come from `engine.criteria.check_criteria` over `metrics.json`; `rubric_scores`
holds the advisory LLM rubric beside them and is nullable, because the Evaluator's failure
policy (`DEGRADE_DETERMINISTIC`) is that the hard criteria decide alone when the rubric call
fails.

`benchmark_results` holds one row per case per suite execution, keyed by `case_id` because
§13.1's Judgement Score is defined over three specific cases and is not recoverable from an
aggregate. Its `task_id` is `ON DELETE SET NULL`: benchmark history outlives the runs it was
measured from.

Both are keyed off `task_id` rather than the normative `run_id -> runs.id`, matching
`0003_mlops` — this codebase has not yet built the `runs` table, and today a Task is a Run.
`run_id` is carried alongside as a plain string so a future `runs` table can absorb the
foreign key without a data migration.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0004_evaluation'
down_revision: str | None = '0003_mlops'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors app/db/models/evaluation.py EVAL_DECISIONS and engine.state.EvalDecision.
EVAL_DECISION_ENUM = sa.Enum(
    'ACCEPT', 'REFINE', 'REPLAN', 'ABORT', name='eval_decision_enum'
)


def upgrade() -> None:
    op.create_table(
        'evaluations',
        sa.Column('id', sa.Uuid(), nullable=False, comment='Unique identifier for the evaluation row.'),
        sa.Column('task_id', sa.Uuid(), nullable=False, comment='Foreign key referencing the owning Task (stands in for the not-yet-built Run).'),
        sa.Column('run_id', sa.String(length=64), nullable=False, comment="The engine's run identity (AgentState.run_id)."),
        sa.Column('revision', sa.Integer(), nullable=False, comment='The code revision this verdict judged; pairs the row with experiments.revision.'),
        sa.Column('decision', EVAL_DECISION_ENUM, nullable=False, comment='ACCEPT, REFINE, REPLAN or ABORT (AGENTS.md §7.6 decision table).'),
        sa.Column('passed', sa.Boolean(), nullable=False, comment='Whether every required criterion was met. Arithmetic from metrics.json — never model output.'),
        sa.Column('score', sa.Numeric(precision=6, scale=4), nullable=False, comment='Fraction of total criterion weight earned (0.0000–1.0000).'),
        sa.Column('criteria_results', sa.JSON(), nullable=False, comment='Per-criterion outcome: metric, comparator, threshold, observed, passed.'),
        sa.Column('rubric_scores', sa.JSON(), nullable=True, comment='The advisory 5-dimension LLM rubric. NULL when the rubric call failed and the deterministic criteria alone decided (failure policy DEGRADE_DETERMINISTIC).'),
        sa.Column('replan_directive', sa.Text(), nullable=True, comment='What the next plan must do differently. Set when decision == REPLAN.'),
        sa.Column('refine_directive', sa.Text(), nullable=True, comment='The quantitative gap and the change that should close it. Set when decision == REFINE.'),
        sa.Column('summary', sa.Text(), nullable=False, comment='One-line statement of the verdict, as it appears in the report.'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, comment='UTC timestamp this verdict was recorded.'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_evaluations_task_id'), 'evaluations', ['task_id'], unique=False)
    op.create_index(op.f('ix_evaluations_run_id'), 'evaluations', ['run_id'], unique=False)
    # §7.1's `ix_evaluations_run`: the newest verdict for a run is what
    # `GET /runs/{run_id}/evaluation` answers with.
    op.create_index(
        'ix_evaluations_run_created', 'evaluations', ['run_id', sa.text('created_at DESC')], unique=False
    )

    op.create_table(
        'benchmark_results',
        sa.Column('id', sa.Uuid(), nullable=False, comment='Unique identifier for the benchmark result row.'),
        sa.Column('suite', sa.String(length=64), nullable=False, comment="Suite name, e.g. 'core-10' (benchmarks/suites/{suite}.yaml)."),
        sa.Column('case_id', sa.String(length=64), nullable=False, comment="Case id within the suite, e.g. 'bc-logreg'."),
        sa.Column('task_id', sa.Uuid(), nullable=True, comment='The Task this case ran as, or NULL once that run has been swept.'),
        sa.Column('run_id', sa.String(length=64), nullable=True, comment="The engine's run identity for this case's run."),
        sa.Column('outcome', sa.String(length=16), nullable=True, comment='RunOutcome the run reached: SUCCEEDED, PARTIAL, FAILED or CANCELLED.'),
        sa.Column('passed', sa.Boolean(), nullable=False, comment='Whether every declared expectation for this case held.'),
        sa.Column('metrics', sa.JSON(), nullable=False, comment='Observed metrics.json metrics plus platform counters for this case.'),
        sa.Column('checks', sa.JSON(), nullable=False, comment='Per-expectation results: name, passed, detail.'),
        sa.Column('duration_seconds', sa.Integer(), nullable=True, comment='Wall-clock time this case took, for the Median Run Duration KPI.'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, comment='UTC timestamp this case was scored.'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_benchmark_results_task_id'), 'benchmark_results', ['task_id'], unique=False)
    op.create_index(op.f('ix_benchmark_results_run_id'), 'benchmark_results', ['run_id'], unique=False)
    op.create_index(op.f('ix_benchmark_results_case_id'), 'benchmark_results', ['case_id'], unique=False)
    # §7.1's `ix_benchmark_suite`: every KPI query is "this suite, most recent first".
    op.create_index(
        'ix_benchmark_suite', 'benchmark_results', ['suite', sa.text('created_at DESC')], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_benchmark_suite', table_name='benchmark_results')
    op.drop_index(op.f('ix_benchmark_results_case_id'), table_name='benchmark_results')
    op.drop_index(op.f('ix_benchmark_results_run_id'), table_name='benchmark_results')
    op.drop_index(op.f('ix_benchmark_results_task_id'), table_name='benchmark_results')
    op.drop_table('benchmark_results')

    op.drop_index('ix_evaluations_run_created', table_name='evaluations')
    op.drop_index(op.f('ix_evaluations_run_id'), table_name='evaluations')
    op.drop_index(op.f('ix_evaluations_task_id'), table_name='evaluations')
    op.drop_table('evaluations')
    # The enum type is not dropped with the table it is used by.
    EVAL_DECISION_ENUM.drop(op.get_bind(), checkfirst=True)

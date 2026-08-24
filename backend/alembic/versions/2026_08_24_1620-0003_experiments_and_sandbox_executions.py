"""MLOps integration: experiments, sandbox_executions.

Revision ID: 0003_mlops
Revises: 0002_corpus
Create Date: 2026-08-24 16:20:00.000000+00:00

Postgres tables Phase 4 adds (ARCHITECTURE.md §7.1, MLOPS.md §4 and §11). Both are keyed
off `task_id` rather than the normative schema's `run_id -> runs.id`: this codebase has not
yet built the `runs` table (ARCHITECTURE.md §6 marks it `⬜`), and today a Task is a Run.
`run_id` is carried alongside as a plain string — the engine's own identity — so a future
`runs` table can absorb the foreign key without a data migration. See the docstrings on
`app/db/models/experiment.py` and `app/db/models/sandbox_execution.py`.

`experiments.mlflow_run_id` is nullable and unique: NULL means the attempt was logged here
before MLflow could be reached, and `mlflow_backfill` (`app/worker/cron.py`) retries every
row in that state (MLOPS.md §11). MLflow being unreachable must never fail a run, so this
row is always written, independently of whether the MLflow call that ordinarily accompanies
it succeeded.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_mlops'
down_revision: str | None = '0002_corpus'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'experiments',
        sa.Column('id', sa.Uuid(), nullable=False, comment='Unique identifier for the experiment row.'),
        sa.Column('task_id', sa.Uuid(), nullable=False, comment='Foreign key referencing the owning Task (stands in for the not-yet-built Run).'),
        sa.Column('run_id', sa.String(length=64), nullable=False, comment="The engine's run identity (AgentState.run_id); the join key MLflow tags use."),
        sa.Column('revision', sa.Integer(), nullable=False, comment='The code revision this attempt logs — MLflow child run `attempt-{revision:03d}`.'),
        sa.Column('task_kind', sa.String(length=64), nullable=False, comment="The Plan's task_kind — resolves the MLflow experiment `pluton/{task_kind}` on backfill, when mlflow_experiment_id is not yet known."),
        sa.Column('mlflow_experiment_id', sa.String(length=64), nullable=True, comment='MLflow experiment id, once resolved.'),
        sa.Column('mlflow_run_id', sa.String(length=64), nullable=True, comment='MLflow child run id. NULL means MLflow was unreachable when this attempt ran; mlflow_backfill retries rows where this is NULL (MLOPS.md §11).'),
        sa.Column('mlflow_parent_run_id', sa.String(length=64), nullable=True, comment='MLflow parent run id (`run-{run_id[:8]}`).'),
        sa.Column('artifact_uri', sa.Text(), nullable=True, comment="The child run's MLflow artifact URI, once logged."),
        sa.Column('params', sa.JSON(), nullable=False, comment='Flattened, string-valued params as logged to MLflow (MLOPS.md §5.3).'),
        sa.Column('metrics', sa.JSON(), nullable=False, comment='Scalar metrics as logged to MLflow, from metrics.json plus platform metrics.'),
        sa.Column('tags', sa.JSON(), nullable=False, comment='The fixed-allowlist tag set actually written (MLOPS.md §4.3).'),
        sa.Column('registered_model_name', sa.String(length=128), nullable=True, comment='`pluton-{task_kind}`, when this attempt\'s model was registered (MLOPS.md §7.2).'),
        sa.Column('registered_model_version', sa.String(length=16), nullable=True, comment="The registry version created for this attempt's model, if any."),
        sa.Column('unrecoverable', sa.Boolean(), nullable=False, comment='Set by mlflow_backfill when the run volume was pruned before it could heal.'),
        sa.Column('metadata_json', sa.JSON(), nullable=True, comment='Backfill/failure bookkeeping: mlflow_error, artifact_upload_failed, reason.'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, comment='UTC timestamp this attempt was logged.'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mlflow_run_id'),
    )
    op.create_index(op.f('ix_experiments_task_id'), 'experiments', ['task_id'], unique=False)
    op.create_index(op.f('ix_experiments_run_id'), 'experiments', ['run_id'], unique=False)
    op.create_index(op.f('ix_experiments_mlflow_run_id'), 'experiments', ['mlflow_run_id'], unique=False)

    op.create_table(
        'sandbox_executions',
        sa.Column('id', sa.Uuid(), nullable=False, comment='Unique identifier for the execution row.'),
        sa.Column('task_id', sa.Uuid(), nullable=False, comment='Foreign key referencing the owning Task (stands in for the not-yet-built Run).'),
        sa.Column('run_id', sa.String(length=64), nullable=False, comment="The engine's run identity (AgentState.run_id)."),
        sa.Column('step_id', sa.String(length=16), nullable=True, comment="The Plan step's string id this execution served (PlanStep.id, e.g. 's2')."),
        sa.Column('revision', sa.Integer(), nullable=False, comment='The code revision that was executed.'),
        sa.Column('profile', sa.String(length=32), nullable=False, comment='Sandbox profile: exec, train, or train-tracked.'),
        sa.Column('image_digest', sa.String(length=128), nullable=True, comment='The resolved (pinned, if digests.json exists) image reference actually run.'),
        sa.Column('container_id', sa.String(length=64), nullable=True, comment='Docker container id, for correlation with daemon logs.'),
        sa.Column('exit_code', sa.Integer(), nullable=True, comment='Process exit code; NULL only when the container never started (rejected).'),
        sa.Column('timed_out', sa.Boolean(), nullable=False, comment='Killed for exceeding its wall clock.'),
        sa.Column('oom_killed', sa.Boolean(), nullable=False, comment='Killed for exceeding its memory limit.'),
        sa.Column('duration_ms', sa.Integer(), nullable=True, comment='Wall-clock execution time in milliseconds.'),
        sa.Column('max_rss_bytes', sa.BigInteger(), nullable=True, comment='Peak resident memory sampled during execution.'),
        sa.Column('cpu_ms', sa.Integer(), nullable=True, comment='CPU time consumed, in milliseconds.'),
        sa.Column('stdout_ref', sa.Text(), nullable=True, comment='Path to the captured stdout log on the run volume.'),
        sa.Column('stderr_ref', sa.Text(), nullable=True, comment='Path to the captured stderr log on the run volume.'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, comment='UTC timestamp this execution was recorded.'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_sandbox_executions_task_id'), 'sandbox_executions', ['task_id'], unique=False)
    op.create_index(op.f('ix_sandbox_executions_run_id'), 'sandbox_executions', ['run_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sandbox_executions_run_id'), table_name='sandbox_executions')
    op.drop_index(op.f('ix_sandbox_executions_task_id'), table_name='sandbox_executions')
    op.drop_table('sandbox_executions')
    op.drop_index(op.f('ix_experiments_mlflow_run_id'), table_name='experiments')
    op.drop_index(op.f('ix_experiments_run_id'), table_name='experiments')
    op.drop_index(op.f('ix_experiments_task_id'), table_name='experiments')
    op.drop_table('experiments')

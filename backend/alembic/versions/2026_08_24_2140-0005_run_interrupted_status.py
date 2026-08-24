"""Add INTERRUPTED to task_status_enum.

Revision ID: 0005_interrupted
Revises: 0004_evaluation
Create Date: 2026-08-24 21:40:00.000000+00:00

ARCHITECTURE.md §5.3 makes `INTERRUPTED` a real state and not a flavour of failure: it is
the only non-terminal state a run can be found in after its worker died, and it is the one
state `POST /runs/{id}/resume` accepts. `reap_interrupted_runs` (`app/worker/cron.py`)
writes it whenever a run is `RUNNING` with no `lock:run:{id}` in Redis.

Recording it as `FAILED` instead would have been cheaper and wrong twice over: a resumable
run would be presented to the operator as a dead one, and the failure-rate KPI in §13.1
would count infrastructure restarts as agent failures.

`ALTER TYPE ... ADD VALUE` is not reversible in PostgreSQL — an enum label cannot be
dropped — so `downgrade()` migrates the affected rows back to a label that still exists
and leaves the type alone. That is the honest downgrade: the alternative is recreating the
type and every column that uses it, which is a far larger operation than the upgrade it
undoes.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005_interrupted'
down_revision: str | None = '0004_evaluation'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS keeps this idempotent against a database where an earlier partial
    # apply already added the label. PostgreSQL 12+ permits ADD VALUE inside a
    # transaction as long as the new label is not used in the same transaction, which
    # is why nothing below writes it.
    op.execute("ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'INTERRUPTED'")


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET status = 'FAILED', "
        "error = COALESCE(error, 'run was interrupted before 0005 was rolled back') "
        "WHERE status = 'INTERRUPTED'"
    )

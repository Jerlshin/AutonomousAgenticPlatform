"""Initial schema migration for tasks logs and artifacts

Revision ID: b9cff7b47159
Revises: None
Create Date: 2026-08-11 06:46:58.077591+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9cff7b47159'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

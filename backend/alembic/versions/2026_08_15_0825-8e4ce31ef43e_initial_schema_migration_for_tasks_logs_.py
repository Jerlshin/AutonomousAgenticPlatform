"""Initial schema migration for tasks logs and artifacts

Revision ID: 8e4ce31ef43e
Revises: 'b9cff7b47159'
Create Date: 2026-08-15 08:25:00.340375+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8e4ce31ef43e'
down_revision: Union[str, None] = 'b9cff7b47159'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

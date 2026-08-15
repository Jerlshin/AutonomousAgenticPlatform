# Alembic (the database migration tool for SQLAlchemy), this file serves as the blueprint/template that generates every few migration script inside your /versions/

# up_revision - unique has identifying this specific migration
# down_revision - the unique hash of the previous migration

"""${message}

Revision ID: ${up_revision}
Revises: ${repr(down_revision)}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}

"""add institution_id to institution_papers

Revision ID: a0d66c11908a
Revises: 78beaabec600
Create Date: 2026-09-25 20:57:49.010640

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a0d66c11908a'
down_revision: Union[str, Sequence[str], None] = '78beaabec600'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # INSPIRE institution record id the papers were fetched by. NULL means the
    # row was fetched by exact-phrase name match (legacy rows, or free-text
    # postings with no linked institution) - discovery treats a NULL here, when
    # an id is now known, as needing a re-fetch.
    op.add_column("institution_papers", sa.Column("institution_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("institution_papers", "institution_id")

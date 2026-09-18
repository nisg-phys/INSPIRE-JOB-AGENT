"""add institution_papers table

Revision ID: 78beaabec600
Revises: 7bc4a784848c
Create Date: 2026-09-19 01:33:30.530947

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '78beaabec600'
down_revision: Union[str, Sequence[str], None] = '7bc4a784848c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "institution_papers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("institution", sa.Text(), nullable=False, unique=True),
        sa.Column("papers", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("institution_papers")

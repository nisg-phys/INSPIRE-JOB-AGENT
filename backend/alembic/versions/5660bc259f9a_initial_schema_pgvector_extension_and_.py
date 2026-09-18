"""initial schema: pgvector extension and jobs_raw

Revision ID: 5660bc259f9a
Revises: 
Create Date: 2026-09-18 15:43:44.889512

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


# revision identifiers, used by Alembic.
revision: str = '5660bc259f9a'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "jobs_raw",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("record_id", sa.Text(), nullable=False, unique=True),
        sa.Column("position", sa.Text(), nullable=False),
        sa.Column("institutions", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_jobs_raw_institutions", "jobs_raw", ["institutions"], postgresql_using="gin"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_jobs_raw_institutions", table_name="jobs_raw")
    op.drop_table("jobs_raw")
    # Extension is left in place on downgrade — other tables may depend on it.

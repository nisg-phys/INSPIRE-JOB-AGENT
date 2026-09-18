"""add query_cache table

Revision ID: 7bc4a784848c
Revises: 5660bc259f9a
Create Date: 2026-09-18 23:27:28.087676

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '7bc4a784848c'
down_revision: Union[str, Sequence[str], None] = '5660bc259f9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# text-embedding-3-small (app/embeddings.py) produces 1536-dim vectors.
EMBEDDING_DIM = 1536


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "query_cache",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        "CREATE INDEX ix_query_cache_embedding ON query_cache "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_query_cache_embedding", table_name="query_cache")
    op.drop_table("query_cache")

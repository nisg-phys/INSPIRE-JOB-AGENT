"""add institution_aliases and unresolved_institutions

Revision ID: c41e8a7d2f90
Revises: a0d66c11908a
Create Date: 2026-10-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c41e8a7d2f90'
down_revision: Union[str, Sequence[str], None] = 'a0d66c11908a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Normalization and resolution live in SQL, not Python, because the
    # backend and the worker are separate packages that both resolve names:
    # one definition here can't drift between two copies.
    #
    # Deliberately shallow: case, punctuation and spacing only, so
    # "Kentucky U." and "kentucky u" agree. Word order is NOT normalized -
    # "U. Kentucky" vs "Kentucky U." is what aliases are for, and guessing
    # there is how the wrong university gets matched.
    op.execute(
        r"""
        CREATE FUNCTION normalize_institution_name(text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
            SELECT btrim(regexp_replace(
                lower(regexp_replace($1, '[.,;:()]', ' ', 'g')), '\s+', ' ', 'g'
            ))
        $$
        """
    )

    # One row per (spelling, institution record, where we learned it). The
    # same spelling may map to several ids; resolve_institution then refuses
    # to pick one unless a curated row settles it.
    op.create_table(
        "institution_aliases",
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("institution_id", sa.Text(), nullable=False),
        # The spelling as written, for a human reading the table.
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("alias", "institution_id", "source"),
        sa.CheckConstraint(
            "source IN ('posting', 'record', 'curated')", name="ck_institution_aliases_source"
        ),
    )

    # Free-text institution names whose papers couldn't be found by name -
    # the worklist for curating aliases, most frequently seen first.
    op.create_table(
        "unresolved_institutions",
        sa.Column("alias", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # A curated alias wins outright. Otherwise a spelling resolves only if
    # every source agrees on one id: a name two postings linked to different
    # records is ambiguous, and showing another institution's papers is
    # worse than showing none.
    #
    # The argument is read as $1, not by name: inside the body a bare `name`
    # means institution_aliases.name, so every row would match itself.
    op.execute(
        """
        CREATE FUNCTION resolve_institution(text) RETURNS text
        LANGUAGE sql STABLE AS $$
            WITH candidates AS (
                SELECT institution_id, source
                FROM institution_aliases
                WHERE alias = normalize_institution_name($1)
            ),
            curated AS (
                SELECT DISTINCT institution_id FROM candidates WHERE source = 'curated'
            )
            SELECT CASE
                WHEN (SELECT count(*) FROM curated) = 1
                    THEN (SELECT institution_id FROM curated)
                WHEN (SELECT count(*) FROM curated) > 1
                    THEN NULL
                WHEN (SELECT count(DISTINCT institution_id) FROM candidates) = 1
                    THEN (SELECT min(institution_id) FROM candidates)
            END
        $$
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP FUNCTION resolve_institution(text)")
    op.drop_table("unresolved_institutions")
    op.drop_table("institution_aliases")
    op.execute("DROP FUNCTION normalize_institution_name(text)")

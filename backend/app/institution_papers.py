"""Read-only lookup of institution_papers (populated by the worker, Phase 4).

The query path only ever reads this table - it never calls Inspire's
literature API live. Institutions the worker hasn't enriched yet simply
aren't in the table; callers should treat that as "no papers yet", not
an error.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine


class Paper(BaseModel):
    record_id: str
    title: str
    citation_count: int = 0


def get_recent_papers(institutions: list[str]) -> dict[str, list[Paper]]:
    """Look up recent papers for a batch of institution names.

    Institutions not yet enriched are simply absent from the returned
    dict - callers should treat a missing key as "not yet available"
    rather than an error.
    """
    if not institutions:
        return {}

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT institution, papers
                FROM institution_papers
                WHERE institution = ANY(:institutions)
                """
            ),
            {"institutions": institutions},
        ).mappings()
        return {row["institution"]: [Paper.model_validate(p) for p in row["papers"]] for row in rows}

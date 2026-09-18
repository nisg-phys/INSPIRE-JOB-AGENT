"""Upserts recent-papers data into institution_papers for a list of institutions."""

from __future__ import annotations

import sys

from sqlalchemy import text

from worker.db import get_engine
from worker.inspire_literature_client import InspireAPIError, recent_papers


def enrich_institution(institution: str) -> None:
    papers = recent_papers(institution)
    payload = "[" + ",".join(paper.model_dump_json() for paper in papers) + "]"

    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO institution_papers (institution, papers, last_updated)
                VALUES (:institution, CAST(:papers AS jsonb), now())
                ON CONFLICT (institution) DO UPDATE
                SET papers = EXCLUDED.papers, last_updated = EXCLUDED.last_updated
                """
            ),
            {"institution": institution, "papers": payload},
        )


def enrich(institutions: list[str]) -> None:
    for institution in institutions:
        try:
            enrich_institution(institution)
            print(f"worker: enriched {institution!r}")
        except InspireAPIError as exc:
            print(f"worker: failed to enrich {institution!r}: {exc}", file=sys.stderr)

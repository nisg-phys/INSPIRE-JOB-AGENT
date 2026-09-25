"""Upserts recent-papers data into institution_papers for a list of institutions."""

from __future__ import annotations

import sys

from sqlalchemy import text

from worker.db import get_engine
from worker.discovery import InstitutionRef
from worker.inspire_literature_client import InspireAPIError, recent_papers


def enrich_institution(ref: InstitutionRef) -> None:
    papers = recent_papers(ref.name, ref.institution_id)
    payload = "[" + ",".join(paper.model_dump_json() for paper in papers) + "]"

    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO institution_papers (institution, institution_id, papers, last_updated)
                VALUES (:institution, :institution_id, CAST(:papers AS jsonb), now())
                ON CONFLICT (institution) DO UPDATE
                SET papers = EXCLUDED.papers,
                    institution_id = EXCLUDED.institution_id,
                    last_updated = EXCLUDED.last_updated
                """
            ),
            {"institution": ref.name, "institution_id": ref.institution_id, "papers": payload},
        )


def enrich(institutions: list[InstitutionRef]) -> None:
    for ref in institutions:
        try:
            enrich_institution(ref)
            how = f"by id {ref.institution_id}" if ref.institution_id else "by name"
            print(f"worker: enriched {ref.name!r} ({how})")
        except InspireAPIError as exc:
            print(f"worker: failed to enrich {ref.name!r}: {exc}", file=sys.stderr)

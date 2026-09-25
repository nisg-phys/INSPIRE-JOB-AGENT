"""Lookup of institution_papers, primarily populated by the worker (Phase 4).

The query path normally only reads this table - it doesn't call Inspire's
literature API live. The one exception is fetch_and_store, an on-demand
fallback main.py uses for institutions still missing after a search, so a
never-before-seen institution doesn't have to wait for the next worker run.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.inspire_literature_client import InspireAPIError, recent_papers

# How many institutions fetch_and_store_many fetches at once. Bounded (not
# "one thread per institution") to stay polite to INSPIRE's rate limit, which
# a burst from a shared Cloud Run egress IP could otherwise trip.
MAX_PARALLEL_FETCHES = 5


class Paper(BaseModel):
    record_id: str
    title: str
    citation_count: int = 0
    # INSPIRE's normalized category terms (e.g. "Theory-HEP"). Missing on
    # rows enriched before this field existed - defaults to empty, callers
    # already treat "no category match" as "fall back to most recent".
    categories: list[str] = []


DEFAULT_PAPER_LIMIT = 5


def select_relevant_papers(
    papers: list[Paper], categories: list[str], limit: int = DEFAULT_PAPER_LIMIT
) -> list[Paper]:
    """Prefer papers matching the query's subfield categories; fall back to
    most recent (the stored order) if none match, rather than showing an
    institution's unrelated output just because it's recent.
    """
    if categories:
        matches = [paper for paper in papers if set(paper.categories) & set(categories)]
        if matches:
            return matches[:limit]
    return papers[:limit]


def fetch_and_store(institution: str, institution_id: str | None = None) -> list[Paper]:
    """On-demand fallback for an institution the worker hasn't enriched yet.

    The worker's daily cron (worker/worker/enrichment.py) is still the
    primary path - it's cheap and keeps this table warm without ever
    touching the query path's latency. This exists only to close the gap
    on an institution's *first* appearance, so its very first search
    doesn't show "Not yet available" for a whole day. Same upsert shape
    as the worker's enrich_institution, so either one can freshen a row.

    Raises app.inspire_literature_client.InspireAPIError on failure -
    callers should decide how to degrade (main.py treats it the same as
    "no papers yet" rather than failing the whole search).
    """
    # recent_papers returns inspire_literature_client.Paper - structurally
    # identical to this module's Paper, but re-validate into the local type
    # so callers get one consistent Paper class regardless of source.
    papers = [
        Paper.model_validate(p.model_dump()) for p in recent_papers(institution, institution_id)
    ]
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
            {"institution": institution, "institution_id": institution_id, "papers": payload},
        )
    return papers


def fetch_and_store_many(
    institutions: dict[str, str | None],
) -> tuple[dict[str, list[Paper]], dict[str, str]]:
    """fetch_and_store for several institutions at once (name -> id or None).

    The fetches are independent network calls, so running them concurrently
    makes a search with N never-seen institutions cost roughly one fetch
    (~2-3s) instead of N in a row. Returns (papers by institution, error
    message by institution): an INSPIRE failure for one institution lands in
    the second dict rather than aborting the others. Any other exception
    (e.g. a database error) still propagates, as it would sequentially.
    """
    if not institutions:
        return {}, {}

    def fetch_one(name: str, institution_id: str | None) -> list[Paper] | str:
        try:
            return fetch_and_store(name, institution_id)
        except InspireAPIError as exc:
            return str(exc)

    papers: dict[str, list[Paper]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_FETCHES, len(institutions))) as pool:
        # Each task runs in a copy of this request's context, so anything it
        # logs still carries the request_id (worker threads don't inherit
        # ContextVars on their own).
        futures = {
            name: pool.submit(contextvars.copy_context().run, fetch_one, name, institution_id)
            for name, institution_id in institutions.items()
        }
        for name, future in futures.items():
            result = future.result()
            if isinstance(result, str):
                errors[name] = result
            else:
                papers[name] = result
    return papers, errors


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

"""Logs raw Inspire job results into jobs_raw.

This doubles as the institution-discovery log the worker's T4.3 scans:
jobs_raw.institutions is checked there to find institutions needing
enrichment, without hardcoding a list.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import get_engine
from app.inspire_client import RawJob


def known_institutions(names: list[str]) -> dict[str, str | None]:
    """Resolve institution names we've actually seen, to their Inspire ids.

    Doubles as an allowlist. The deferred-papers endpoint takes institution
    names from the client, and those names end up inside an Inspire
    literature query (`aff "<name>"`), so an arbitrary string must never
    reach it. Only names already logged from a real Inspire posting come
    back; anything else is silently dropped. The id may be None - not every
    posting links an institution record.
    """
    if not names:
        return {}

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT n AS institution,
                       max(payload->'institution_ids'->>n) AS institution_id
                FROM jobs_raw, unnest(institutions) AS n
                WHERE n = ANY(:names)
                GROUP BY n
                """
            ),
            {"names": names},
        ).mappings()
        return {row["institution"]: row["institution_id"] for row in rows}


def log_jobs(jobs: list[RawJob]) -> None:
    if not jobs:
        return

    with get_engine().begin() as conn:
        for job in jobs:
            conn.execute(
                text(
                    """
                    INSERT INTO jobs_raw (record_id, position, institutions, payload, fetched_at)
                    VALUES (:record_id, :position, :institutions, CAST(:payload AS jsonb), now())
                    ON CONFLICT (record_id) DO UPDATE
                    SET position = EXCLUDED.position,
                        institutions = EXCLUDED.institutions,
                        payload = EXCLUDED.payload,
                        fetched_at = EXCLUDED.fetched_at
                    """
                ),
                {
                    "record_id": job.record_id,
                    "position": job.position,
                    "institutions": job.institutions,
                    "payload": job.model_dump_json(),
                },
            )

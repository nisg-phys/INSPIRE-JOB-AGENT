"""Logs raw Inspire job results into jobs_raw.

This doubles as the institution-discovery log the worker's T4.3 scans:
jobs_raw.institutions is checked there to find institutions needing
enrichment, without hardcoding a list.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import get_engine
from app.inspire_client import RawJob


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

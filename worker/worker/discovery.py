"""Figures out which institutions need enrichment, without a hardcoded list.

Scans jobs_raw (populated by the backend's search path - see
backend/app/jobs_log.py) for institution names that are missing from
institution_papers, or whose entry there has gone stale.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text

from worker.db import get_engine

# Paper lists don't need to refresh as often as job postings/deadlines do.
DEFAULT_STALE_AFTER = timedelta(days=7)


def discover_institutions(stale_after: timedelta = DEFAULT_STALE_AFTER) -> list[str]:
    """Institutions seen in jobs_raw that are missing or stale in institution_papers."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                WITH seen_institutions AS (
                    SELECT DISTINCT unnest(institutions) AS institution
                    FROM jobs_raw
                )
                SELECT si.institution
                FROM seen_institutions si
                LEFT JOIN institution_papers ip ON ip.institution = si.institution
                WHERE si.institution <> ''
                  AND (
                      ip.institution IS NULL
                      OR ip.last_updated < now() - CAST(:stale_after_seconds || ' seconds' AS interval)
                  )
                ORDER BY si.institution
                """
            ),
            {"stale_after_seconds": stale_after.total_seconds()},
        )
        return [row[0] for row in rows]

"""Figures out which institutions need enrichment, without a hardcoded list.

Scans jobs_raw (populated by the backend's search path - see
backend/app/jobs_log.py) for institution names that are missing from
institution_papers, or whose entry there has gone stale.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NamedTuple

from sqlalchemy import text

from worker.db import get_engine

# Paper lists don't need to refresh as often as job postings/deadlines do.
DEFAULT_STALE_AFTER = timedelta(days=7)


class InstitutionRef(NamedTuple):
    name: str
    # INSPIRE institution record id, when the job posting linked one. Papers
    # are fetched by this when present; name is only the fallback search key.
    institution_id: str | None = None


def discover_institutions(stale_after: timedelta = DEFAULT_STALE_AFTER) -> list[InstitutionRef]:
    """Institutions seen in jobs_raw that are missing or stale in institution_papers.

    Also re-selects rows that were fetched by exact-phrase name match
    (institution_id IS NULL) once a posting has since supplied an id - those
    are the rows most likely to be wrongly empty, since a name spelling that
    differs from the papers' affiliation string matches nothing.
    """
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                WITH seen_institutions AS (
                    SELECT n AS institution,
                           max(payload->'institution_ids'->>n) AS institution_id
                    FROM jobs_raw, unnest(institutions) AS n
                    WHERE n <> ''
                    GROUP BY n
                )
                SELECT si.institution, si.institution_id
                FROM seen_institutions si
                LEFT JOIN institution_papers ip ON ip.institution = si.institution
                WHERE ip.institution IS NULL
                   OR ip.last_updated < now() - CAST(:stale_after_seconds || ' seconds' AS interval)
                   OR (ip.institution_id IS NULL AND si.institution_id IS NOT NULL)
                ORDER BY si.institution
                """
            ),
            {"stale_after_seconds": stale_after.total_seconds()},
        )
        return [InstitutionRef(row[0], row[1]) for row in rows]

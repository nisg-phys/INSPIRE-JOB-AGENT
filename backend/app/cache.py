"""Semantic query cache (query_cache table).

Stores past query text + embedding + the structured params and result
payload used to answer it, so a paraphrased query can be served without
re-running the LLM rewrite + Inspire search pipeline (wired into the
search endpoint in T3.3).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.embeddings import embed
from app.formatter import FormattedJob
from app.inspire_client import JobQueryParams
from app.query_rewriter import career_stages

logger = logging.getLogger("app.cache")

DEFAULT_SIMILARITY_THRESHOLD = 0.8

# Job postings and deadlines don't change minute-to-minute, but new postings
# do appear, so a cached result shouldn't be served indefinitely.
DEFAULT_TTL = timedelta(hours=24)

# How many nearest entries to consider. The nearest one can be a different
# career stage ("postdoc in X" while asking for "phd in X"), so a few more
# are checked before giving up - an exact-stage entry for the same query
# may sit just behind it.
CANDIDATE_COUNT = 5


class CacheEntry(BaseModel):
    id: int
    query_text: str
    params: JobQueryParams
    result: list[FormattedJob]
    total_matches: int | None = None
    created_at: datetime
    similarity: float


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in vector) + "]"


def lookup(
    query_text: str,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_age: timedelta = DEFAULT_TTL,
) -> CacheEntry | None:
    """Find the nearest non-stale cached entry for a query, if one is close enough.

    An entry also has to name the same career stages as the query
    (see query_rewriter.career_stages): the embedding alone can't be trusted
    to keep "phd in X" and "postdoc in X" apart.
    """
    vector = _vector_literal(embed(query_text))
    stages = career_stages(query_text)

    with get_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, query_text, params, result, created_at,
                       1 - (embedding <=> CAST(:vector AS vector)) AS similarity
                FROM query_cache
                WHERE created_at > now() - CAST(:max_age_seconds || ' seconds' AS interval)
                ORDER BY embedding <=> CAST(:vector AS vector)
                LIMIT :candidate_count
                """
            ),
            {
                "vector": vector,
                "max_age_seconds": max_age.total_seconds(),
                "candidate_count": CANDIDATE_COUNT,
            },
        ).mappings().all()

    close = [row for row in rows if row["similarity"] >= threshold]
    row = next((row for row in close if career_stages(row["query_text"]) == stages), None)
    if row is None:
        if close:
            # Logged because this is exactly the wrong answer the stage check
            # exists to prevent - how often it fires says how much it matters.
            logger.info(
                "cache_stage_mismatch",
                extra={
                    "similarity": close[0]["similarity"],
                    "query_stages": sorted(stages),
                    "cached_stages": sorted(career_stages(close[0]["query_text"])),
                },
            )
        return None

    # Entries used to be stored as a bare JSON array of jobs. The envelope
    # below adds the match count beside them, so accept both shapes rather
    # than invalidating every entry written before that.
    payload = row["result"]
    if isinstance(payload, list):
        items, total = payload, None
    else:
        items, total = payload.get("jobs", []), payload.get("total")

    return CacheEntry(
        id=row["id"],
        query_text=row["query_text"],
        params=JobQueryParams.model_validate(row["params"]),
        result=[FormattedJob.model_validate(item) for item in items],
        total_matches=total,
        created_at=row["created_at"],
        similarity=row["similarity"],
    )


def store(
    query_text: str,
    params: JobQueryParams,
    result: list[FormattedJob],
    total_matches: int | None = None,
) -> None:
    """Insert a new cache entry."""
    vector = _vector_literal(embed(query_text))
    payload = json.dumps(
        {"jobs": [json.loads(item.model_dump_json()) for item in result], "total": total_matches}
    )

    with get_engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO query_cache (query_text, embedding, params, result)
                VALUES (
                    :query_text,
                    CAST(:vector AS vector),
                    CAST(:params AS jsonb),
                    CAST(:result AS jsonb)
                )
                """
            ),
            {
                "query_text": query_text,
                "vector": vector,
                "params": params.model_dump_json(),
                "result": payload,
            },
        )

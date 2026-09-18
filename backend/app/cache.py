"""Semantic query cache (query_cache table).

Stores past query text + embedding + the structured params and result
payload used to answer it, so a paraphrased query can be served without
re-running the LLM rewrite + Inspire search pipeline. Wiring this into the
search endpoint happens in T3.3 - this module is just the storage and
nearest-neighbor lookup.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_engine
from app.embeddings import embed
from app.formatter import FormattedJob
from app.inspire_client import JobQueryParams

DEFAULT_SIMILARITY_THRESHOLD = 0.8


class CacheEntry(BaseModel):
    id: int
    query_text: str
    params: JobQueryParams
    result: list[FormattedJob]
    created_at: datetime
    similarity: float


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in vector) + "]"


def lookup(query_text: str, threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> CacheEntry | None:
    """Find the nearest cached entry for a query, if one is close enough."""
    vector = _vector_literal(embed(query_text))

    with get_engine().connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, query_text, params, result, created_at,
                       1 - (embedding <=> CAST(:vector AS vector)) AS similarity
                FROM query_cache
                ORDER BY embedding <=> CAST(:vector AS vector)
                LIMIT 1
                """
            ),
            {"vector": vector},
        ).mappings().first()

    if row is None or row["similarity"] < threshold:
        return None

    return CacheEntry(
        id=row["id"],
        query_text=row["query_text"],
        params=JobQueryParams.model_validate(row["params"]),
        result=[FormattedJob.model_validate(item) for item in row["result"]],
        created_at=row["created_at"],
        similarity=row["similarity"],
    )


def store(query_text: str, params: JobQueryParams, result: list[FormattedJob]) -> None:
    """Insert a new cache entry."""
    vector = _vector_literal(embed(query_text))

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
                "result": "[" + ",".join(item.model_dump_json() for item in result) + "]",
            },
        )

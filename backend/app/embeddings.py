"""Query embeddings for the semantic cache (Phase 3), via OpenAI's embedding API."""

from __future__ import annotations

from functools import lru_cache

import openai

from app.config import get_settings

MODEL = "text-embedding-3-small"


class EmbeddingError(RuntimeError):
    """Raised when the embedding API call fails."""


@lru_cache
def _client() -> openai.OpenAI:
    return openai.OpenAI(api_key=get_settings().openai_api_key)


def embed(text: str) -> list[float]:
    """Embed a query string as a vector for cosine-similarity search."""
    try:
        response = _client().embeddings.create(model=MODEL, input=text)
    except openai.APIError as exc:
        raise EmbeddingError(f"OpenAI embedding request failed: {exc}") from exc
    return response.data[0].embedding

"""Natural-language query rewriting via a single LLM provider (Groq).

Turns a free-text search query into structured JobQueryParams. The provider
is hardcoded for now; T5.1 pulls this behind a router interface once a
second provider is wired up.
"""

from __future__ import annotations

import json

import groq
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.inspire_client import JobQueryParams

MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """You extract structured search parameters from a natural-language query for physics/astronomy academic job postings.

Respond with JSON only, matching this shape:
{"subfield": string or null, "seniority": string or null, "location": string or null, "keywords": [string, ...]}

- subfield: the physics/research subfield mentioned (e.g. "string theory", "cosmology", "condensed matter"). null if none.
- seniority: the career stage/rank mentioned (e.g. "postdoc", "faculty", "phd student", "research scientist"). null if none.
- location: a country, region, or institution mentioned (e.g. "UK", "Europe", "Germany"). null if none.
- keywords: any other meaningful search terms not already captured above (e.g. specific techniques, grant names). Empty list if none.

Do not invent information that isn't in the query."""


class QueryRewriteError(RuntimeError):
    """Raised when the LLM extraction call fails or returns unusable output."""


class ParsedQuery(BaseModel):
    subfield: str | None = None
    seniority: str | None = None
    location: str | None = None
    keywords: list[str] = []


def _extract(text: str) -> ParsedQuery:
    settings = get_settings()
    client = groq.Groq(api_key=settings.groq_api_key)
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
    except groq.APIError as exc:
        raise QueryRewriteError(f"Groq request failed: {exc}") from exc

    raw = response.choices[0].message.content
    try:
        return ParsedQuery.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise QueryRewriteError(f"Could not parse LLM output as ParsedQuery: {raw!r}") from exc


def rewrite_query(text: str) -> JobQueryParams:
    """Turn a natural-language query into structured Inspire search params."""
    parsed = _extract(text)
    terms = [parsed.seniority, parsed.subfield, parsed.location, *parsed.keywords]
    keywords = " ".join(dict.fromkeys(term.strip() for term in terms if term and term.strip()))
    return JobQueryParams(keywords=keywords)

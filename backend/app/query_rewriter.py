"""Natural-language query rewriting via a pluggable LLM provider.

The provider implements LLMProvider (app/llm/base.py); this module owns
the prompt and JSON parsing, which stays the same regardless of which
provider (or fallback chain of providers, see app/llm/router.py)
answers it.
"""

from __future__ import annotations

import json
from functools import lru_cache

from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.inspire_client import JobQueryParams
from app.llm.base import LLMProvider, LLMProviderError
from app.llm.gemini_provider import GeminiProvider
from app.llm.groq_provider import GroqProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.router import LLMRouter

SYSTEM_PROMPT = """You extract structured search parameters from a natural-language query for physics/astronomy academic job postings.

Respond with JSON only, matching this shape:
{"subfield": string or null, "seniority": string or null, "location": string or null, "keywords": [string, ...], "ambiguous": bool, "clarification_question": string or null}

- subfield: the physics/research subfield mentioned (e.g. "string theory", "cosmology", "condensed matter"). null if none.
- seniority: the career stage/rank mentioned (e.g. "postdoc", "faculty", "phd student", "research scientist"). null if none.
- location: a country, region, or institution mentioned (e.g. "UK", "Europe", "Germany"). null if none.
- keywords: any other meaningful search terms not already captured above (e.g. specific techniques, grant names). Empty list if none.
- ambiguous: true only if the query is so vague that a search would likely return an unhelpfully broad or unclear set of results, AND asking one clarifying question would meaningfully narrow it down. A query that is deliberately broad (e.g. "any physics jobs", "show me everything open") is NOT ambiguous - the user has made a clear choice to see everything. A query naming just a subfield, or just a seniority level, is usually specific enough on its own and NOT ambiguous. Reserve true for queries with essentially no usable signal (e.g. a single vague word, or a request that could mean many unrelated things).
- clarification_question: if ambiguous is true, one short, specific question to ask the user to narrow the search. null if ambiguous is false.

Do not invent information that isn't in the query."""


class QueryRewriteError(RuntimeError):
    """Raised when the LLM extraction call fails or returns unusable output."""


class ParsedQuery(BaseModel):
    subfield: str | None = None
    seniority: str | None = None
    location: str | None = None
    keywords: list[str] = []
    ambiguous: bool = False
    clarification_question: str | None = None


@lru_cache
def _default_provider() -> LLMProvider:
    settings = get_settings()
    if settings.llm_provider == "openai":
        return OpenAIProvider(api_key=settings.openai_api_key)
    if settings.llm_provider == "gemini":
        return GeminiProvider(api_key=settings.gemini_api_key)
    if settings.llm_provider == "groq":
        return GroqProvider(api_key=settings.groq_api_key)

    # "auto" (default): fall back through free providers first, paid last.
    return LLMRouter(
        [
            ("groq", GroqProvider(api_key=settings.groq_api_key)),
            ("gemini", GeminiProvider(api_key=settings.gemini_api_key)),
            ("openai", OpenAIProvider(api_key=settings.openai_api_key)),
        ]
    )


def analyze_query(text: str, provider: LLMProvider | None = None) -> ParsedQuery:
    """Extract structured fields from a natural-language query, including
    whether it's too ambiguous to search well (see ParsedQuery.ambiguous).
    """
    chosen_provider = provider or _default_provider()
    try:
        raw = chosen_provider.complete_json(SYSTEM_PROMPT, text)
    except LLMProviderError as exc:
        raise QueryRewriteError(str(exc)) from exc

    try:
        return ParsedQuery.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise QueryRewriteError(f"Could not parse LLM output as ParsedQuery: {raw!r}") from exc


def to_job_query_params(parsed: ParsedQuery) -> JobQueryParams:
    """Fold an already-extracted ParsedQuery into Inspire search params.

    Split out from rewrite_query so callers that already have a ParsedQuery
    (e.g. main.py, after calling analyze_query to check ambiguity) don't
    need a second LLM call just to get JobQueryParams.
    """
    terms = [parsed.seniority, parsed.subfield, parsed.location, *parsed.keywords]
    keywords = " ".join(dict.fromkeys(term.strip() for term in terms if term and term.strip()))
    return JobQueryParams(keywords=keywords)


def rewrite_query(text: str, provider: LLMProvider | None = None) -> JobQueryParams:
    """Turn a natural-language query into structured Inspire search params."""
    return to_job_query_params(analyze_query(text, provider))

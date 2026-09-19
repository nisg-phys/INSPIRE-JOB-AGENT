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
from app.inspire_client import RANKS, JobQueryParams
from app.llm.base import LLMProvider, LLMProviderError
from app.llm.gemini_provider import GeminiProvider
from app.llm.groq_provider import GroqProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.router import LLMRouter

# INSPIRE's normalized category taxonomy (inspire_categories.term on
# literature records) - used to match a job's subfield against an
# institution's recent papers (institution_papers.select_relevant_papers),
# not sent to Inspire's API directly.
INSPIRE_CATEGORIES = [
    "Accelerators", "Astrophysics", "Computing", "Condensed Matter",
    "Data Analysis and Statistics", "Experiment-HEP", "Experiment-Nucl",
    "General Physics", "Gravitation and Cosmology", "Instrumentation",
    "Lattice", "Math and Math Physics", "Theory-HEP", "Theory-Nucl",
    "Phenomenology-HEP", "Quantum Physics", "Other",
]

SYSTEM_PROMPT = f"""You extract structured search parameters from a natural-language query for physics/astronomy academic job postings.

Respond with JSON only, matching this shape:
{{"subfield": string or null, "seniority": string or null, "location": string or null, "keywords": [string, ...], "ranks": [string, ...], "categories": [string, ...], "ambiguous": bool, "clarification_question": string or null}}

- subfield: the physics/research subfield mentioned (e.g. "string theory", "cosmology", "condensed matter"). null if none.
- seniority: a short human-readable description of the career stage/rank mentioned (e.g. "postdoc", "faculty", "phd student"). null if none. This is just for display - ranks (below) is what actually filters the search.
- location: a country, region, or institution mentioned (e.g. "UK", "Europe", "Germany"). null if none.
- keywords: any other meaningful search terms not already captured above (e.g. specific techniques, grant names). Empty list if none.
- ranks: the INSPIRE job-rank codes implied by the query, chosen ONLY from this exact set: {", ".join(RANKS)}. Map generously but precisely:
  * "faculty" / "professor" / "tenure-track" with no further detail -> ["JUNIOR", "SENIOR"] (covers assistant through full professor)
  * "assistant professor" specifically -> ["JUNIOR"]
  * "associate professor" / "full professor" / "tenured" -> ["SENIOR"]
  * "postdoc" / "postdoctoral" -> ["POSTDOC"]
  * "phd student" / "doctoral" / "graduate student" -> ["PHD"]
  * "master's student" / "msc" -> ["MASTER"]
  * "undergraduate" / "summer student" -> ["UNDERGRADUATE"]
  * "staff scientist" / "research scientist" / "permanent position" -> ["STAFF"]
  * "visiting researcher" / "sabbatical" -> ["VISITOR"]
  * no seniority mentioned at all -> [] (do not guess)
- categories: the INSPIRE research-area categories implied by the query's subfield, chosen ONLY from this exact set: {", ".join(INSPIRE_CATEGORIES)}. Used to find relevant recent papers from an institution, not to filter jobs. Map by physics content, e.g. "string theory"/"quantum gravity" -> ["Theory-HEP"], "cosmology"/"dark energy" -> ["Gravitation and Cosmology", "Astrophysics"], "particle phenomenology" -> ["Phenomenology-HEP"], "condensed matter"/"materials" -> ["Condensed Matter"], "detector"/"instrumentation" -> ["Instrumentation"], "lattice QCD" -> ["Lattice"], "quantum computing"/"quantum information" -> ["Quantum Physics"]. A query can imply more than one category. Empty list if no subfield is mentioned at all - do not guess a category just because a rank or location was given.
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
    ranks: list[str] = []
    categories: list[str] = []
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
    # seniority is NOT included here - it goes through the structured ranks
    # filter below instead. Inspire's `q` is relevance-scored free text, not
    # an exact-match filter, so a term like "faculty" in keywords doesn't
    # exclude Master's/PhD postings that just happen to score well on the
    # other terms - the ranks filter does.
    terms = [parsed.subfield, parsed.location, *parsed.keywords]
    keywords = " ".join(dict.fromkeys(term.strip() for term in terms if term and term.strip()))
    ranks = [rank for rank in dict.fromkeys(parsed.ranks) if rank in RANKS]
    return JobQueryParams(keywords=keywords, ranks=ranks)


def rewrite_query(text: str, provider: LLMProvider | None = None) -> JobQueryParams:
    """Turn a natural-language query into structured Inspire search params."""
    return to_job_query_params(analyze_query(text, provider))

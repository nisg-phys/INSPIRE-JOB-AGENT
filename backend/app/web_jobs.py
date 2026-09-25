"""Web-search fallback for queries Inspire has no postings for.

Inspire is a curated board many groups never post to, so a narrow query
("postdoc in celestial field theory") can legitimately return nothing
while the position exists on a university page or a job board. This
module searches the web via Tavily, has the LLM extract structured
fields from the hits, and returns them as FormattedJobs badged
source="web" so the frontend can mark them as unverified.

It is strictly a fallback: main.py only calls it when Inspire returned
zero jobs, and every failure path degrades to "no web results" rather
than failing the search (see fallback_results).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from urllib.parse import urlparse

import opik
from pydantic import BaseModel, ValidationError
import requests

from app.config import get_settings
from app.formatter import KNOWN_APPLICATION_PLATFORMS, FormattedJob, _apply_via
from app.inspire_client import RANKS
from app.llm.base import LLMProvider, LLMProviderError
from app.query_rewriter import ParsedQuery, _default_provider

logger = logging.getLogger("app.web_jobs")

TAVILY_URL = "https://api.tavily.com/search"

# Deliberately tighter than the 15s the Inspire clients use: this sits on
# the request's critical path, and a slow fallback is worth less to the
# user than a fast empty table.
REQUEST_TIMEOUT = 10

UNKNOWN_INSTITUTION = "Unknown institution"

MAX_HITS = 10  # asked of Tavily
MAX_WEB_JOBS = 6  # kept after filtering - small enough that a bad row can't dominate
SNIPPET_CHARS = 600  # per-hit truncation, to bound extraction prompt size

# Only fall back when Inspire found nothing at all. Above zero we'd have to
# dedupe a posting that appears both on Inspire and on the institution's own
# page (different URLs, different titles, no shared key) and decide how
# unverified rows interleave with verified ones - neither problem exists at
# zero.
WEB_FALLBACK_MAX_INSPIRE_RESULTS = 0

# Boards that actually carry academic postings are boosted, not required:
# the postings this fallback exists to find are often on a department's own
# site, which an allowlist would exclude.
PREFERRED_DOMAINS = sorted(KNOWN_APPLICATION_PLATFORMS)

# General job aggregators - they carry no academic research postings but
# rank well, so they crowd out real hits.
EXCLUDE_DOMAINS = [
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "jooble.org",
    "linkedin.com",
    "simplyhired.com",
]

# Inspire's rank codes are not web-search terms - "POSTDOC" matches nothing.
_RANK_QUERY_TERMS = {
    "POSTDOC": "postdoctoral position",
    "PHD": "PhD position",
    "JUNIOR": "assistant professor position",
    "SENIOR": "professor position",
    "STAFF": "research scientist position",
    "VISITOR": "visiting researcher position",
    "MASTER": "master's student position",
    "UNDERGRADUATE": "undergraduate research position",
    "OTHER": "position",
}


class WebSearchError(RuntimeError):
    """Raised when the Tavily web search request fails."""


class WebHit(BaseModel):
    """One raw Tavily search result, before any LLM interpretation."""

    title: str = ""
    url: str
    content: str = ""
    score: float = 0.0


class _Extraction(BaseModel):
    """The LLM's reading of a single hit. Untrusted - filtered below."""

    index: int
    is_job_posting: bool = False
    # "direct", "related" or "unrelated" - see _extraction_prompt. Graded
    # rather than boolean because niche subfields are almost never named in
    # an ad: a celestial-holography position is advertised as "theoretical
    # high energy physics", so strict matching returns nothing at all.
    relevance: str = "unrelated"
    title: str | None = None
    institution: str | None = None
    location: str | None = None
    deadline: str | None = None
    rank: str | None = None
    confidence: str = "low"


def should_fall_back(inspire_count: int) -> bool:
    """Whether a search returning `inspire_count` jobs should try the web."""
    return inspire_count <= WEB_FALLBACK_MAX_INSPIRE_RESULTS


def is_enabled() -> bool:
    """False when no Tavily key is configured - the fallback is optional."""
    return bool(get_settings().tavily_api_key)


def build_query(parsed: ParsedQuery) -> str:
    """Turn a ParsedQuery into natural-language text for a web search.

    Deliberately not the Inspire query: Inspire takes rank codes and
    field-scoped syntax, a web search wants the words a posting would
    actually use. No year is baked in (the search's recency window
    handles that) so the query doesn't rot.
    """
    # Left as None when the query names no career stage at all, so a parse
    # with no signal whatsoever yields "" rather than a web search for the
    # bare word "position".
    if parsed.ranks:
        rank_phrase = _RANK_QUERY_TERMS.get(parsed.ranks[0], "position")
    else:
        rank_phrase = parsed.seniority

    terms = [parsed.subfield, rank_phrase, parsed.location, *parsed.keywords]
    return " ".join(dict.fromkeys(term.strip() for term in terms if term and term.strip()))


def _request(payload: dict) -> dict:
    api_key = get_settings().tavily_api_key
    try:
        response = requests.post(
            TAVILY_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            raise WebSearchError("Tavily rate limit exceeded.") from exc
        # 432/433 are Tavily-specific: the plan's credits are exhausted.
        # Distinct from 429 because retrying won't help - it needs a human.
        if status in (432, 433):
            raise WebSearchError("Tavily credit limit reached.") from exc
        if status == 401:
            raise WebSearchError("Tavily API key rejected.") from exc
        raise WebSearchError(f"HTTP error from Tavily: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise WebSearchError("Request to Tavily timed out.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise WebSearchError("Could not connect to Tavily.") from exc
    except ValueError as exc:  # non-JSON body
        raise WebSearchError(f"Unreadable response from Tavily: {exc}") from exc


@opik.track(name="web_jobs.search")
def search_raw(query: str) -> list[WebHit]:
    """Run the web search. Raises WebSearchError on any failure."""
    data = _request(
        {
            "query": query,
            "max_results": MAX_HITS,
            "search_depth": "basic",
            "topic": "general",
            "include_domains": PREFERRED_DOMAINS,
            # "prefer" boosts those domains in ranking without excluding
            # everything else, unlike "restrict".
            "include_domains_mode": "prefer",
            "exclude_domains": EXCLUDE_DOMAINS,
        }
    )

    hits = []
    for raw in data.get("results", []):
        try:
            hits.append(WebHit.model_validate(raw))
        except ValidationError:
            # A hit without a URL is unusable; skip it rather than failing
            # the batch.
            continue
    return hits


def _extraction_prompt(parsed: ParsedQuery, today: date) -> str:
    wanted_ranks = ", ".join(parsed.ranks) if parsed.ranks else "any"
    return f"""You are reading web search results to decide which are real academic job postings matching a user's search, and to extract structured fields from them.

Today's date is {today.isoformat()}. The user is looking for: subfield={parsed.subfield or "any"}, career stage={parsed.seniority or "any"} (Inspire rank codes: {wanted_ranks}), location={parsed.location or "any"}.

Respond with JSON only, in this exact shape:
{{"jobs": [{{"index": int, "is_job_posting": bool, "relevance": "direct" or "related" or "unrelated", "title": string or null, "institution": string or null, "location": string or null, "deadline": string or null, "rank": string or null, "confidence": "high" or "medium" or "low"}}]}}

- index: the number of the search result you are describing, from the list below.
- is_job_posting: true ONLY for a page advertising ONE specific open position. False for anything that lists or indexes several positions - a job board's category, tag, field or search-results page is NOT a posting even when its title reads like one, because such pages show the title of the first job they list. Treat a URL whose last part is a research topic or field name rather than a specific job or numeric id as a listing page - for example /tags/quantum-error-correction, /jobs/position/post-doc/field/cosmology, /ajo/physics/Quantum%20Optics, or /ajo/HEP. Also false for news articles, research group homepages, conference or seminar pages, CVs, and general department or "vacancies" pages.
- relevance: how well the position matches the user's subfield above. Narrow research topics are rarely named in a job ad, so judge by whether someone working on the user's topic could plausibly apply:
  * "direct" - the ad names the subfield, a synonym, or an obviously equivalent topic.
  * "related" - the ad is in a broader parent field that normally includes the subfield, so a researcher in that topic would be a credible applicant. For example, a "theoretical high energy physics" or "quantum gravity" position for a search about celestial holography, or an "observational cosmology" position for a search about dark energy surveys.
  * "unrelated" - a different area of physics entirely, or the career stage is clearly not the one asked for.
  Judge the subfield and the career stage separately: a wrong career stage is always "unrelated", however well the topic matches.
- title: the position's title as advertised, e.g. "Postdoctoral Research Associate in Theoretical Physics". null if you cannot tell.
- institution: the hiring university, laboratory or institute. The page is very often hosted by a job board rather than the employer (quantiki.org, academicjobsonline.org, jobs.ac.uk, hyperspace.uni-frankfurt.de and similar), and a board's own name is never the institution. Never read the institution off the domain: use null unless the employer is named in the title or snippet.
- location: city and/or country, if stated. null otherwise.
- deadline: the application deadline as strict YYYY-MM-DD. Use today's date above to resolve a date written without a year. null if no deadline is stated - never invent one.
- rank: the career stage, chosen ONLY from this exact set: {", ".join(RANKS)}. null if you cannot tell.
- confidence: how sure you are that this is a genuine, currently-open posting matching the search.

Include one entry for every result below, in order. Do not invent results."""


def _hits_as_prompt(hits: list[WebHit]) -> str:
    blocks = []
    for index, hit in enumerate(hits):
        domain = urlparse(hit.url).netloc
        snippet = hit.content[:SNIPPET_CHARS]
        blocks.append(f"[{index}] title: {hit.title}\n    domain: {domain}\n    snippet: {snippet}")
    return "\n\n".join(blocks)


def _is_http_url(url: str) -> bool:
    return urlparse(url).scheme in ("http", "https")


# Path segments that mark a board's browse-by-topic page rather than one
# job. These are filtered in code, not by the prompt: such a page shows the
# title of the first job it lists, so the model sees what looks exactly
# like a posting and waves it through on some runs but not others.
_LISTING_PATH_MARKERS = ("/tags/", "/field/", "/jobs/position/", "/category/", "/search")


def _is_listing_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(marker in path for marker in _LISTING_PATH_MARKERS):
        return True
    # AcademicJobsOnline addresses a specific posting by numeric id
    # (/ajo/<institution>/<dept>/12345); anything else under /ajo/ is a
    # browse page, e.g. /ajo/HEP or /ajo/physics/Quantum%20Optics.
    if parsed.netloc.lower().removeprefix("www.") == "academicjobsonline.org":
        return not any(segment.isdigit() for segment in path.split("/"))
    return False


def _dedupe(jobs: list[FormattedJob]) -> list[FormattedJob]:
    """Drop repeats of one position advertised at several URLs.

    A posting is routinely mirrored - the group's own page, a job board and
    Inspire can all carry it - and the URL hash can't see that they're the
    same job. Keyed on title plus institution, which is what a reader would
    compare. First occurrence wins, so search rank decides.

    Only applied when the employer is known: titles like "Postdoctoral
    Research Associate" are common enough that merging two of them on title
    alone would silently drop a different job.
    """
    seen: set[tuple[str, str]] = set()
    unique = []
    for job in jobs:
        institution = job.institution.strip().casefold()
        key = (job.title.strip().casefold(), institution)
        if institution != UNKNOWN_INSTITUTION.casefold() and key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def _to_formatted_job(hit: WebHit, extraction: _Extraction) -> FormattedJob:
    # Namespaced so a web row can never collide with an Inspire record id
    # (which are bare control numbers) anywhere they share a key space.
    record_id = "web:" + hashlib.sha256(hit.url.encode("utf-8")).hexdigest()[:16]

    # A web hit on inspirehep.net is an Inspire posting our own jobs-API
    # query missed (usually too narrow a rank or keyword filter). Labelling
    # it "Web result - not verified against Inspire" would contradict
    # itself, so it keeps Inspire provenance; the link proves it.
    on_inspire = urlparse(hit.url).netloc.lower().removeprefix("www.") == "inspirehep.net"

    return FormattedJob(
        record_id=record_id,
        title=extraction.title or hit.title or "Untitled posting",
        institution=extraction.institution or UNKNOWN_INSTITUTION,
        deadline=extraction.deadline,
        location=extraction.location,
        link=hit.url,
        # Reuses Inspire rows' logic, so a hit on a known board gets the
        # same "AcademicJobsOnline" pill either source it arrived by.
        apply_via=_apply_via(hit.url, []),
        tags=[extraction.rank] if extraction.rank else [],
        source="inspire" if on_inspire else "web",
        # Says so on the row when the posting is in a broader parent field
        # rather than the subfield asked for, so a general HEP-theory ad
        # doesn't read as a hit for a narrow topic.
        match_note="broader field" if extraction.relevance == "related" else None,
        recent_papers=[],
    )


def _keep(extraction: _Extraction, parsed: ParsedQuery, today: date) -> bool:
    """Filter the LLM's readings in Python, not in the prompt."""
    if not extraction.is_job_posting or extraction.relevance not in ("direct", "related"):
        return False
    if extraction.confidence == "low":
        return False

    # A stage the user didn't ask for. `rank is None` is kept: search
    # snippets often don't state one, and dropping those loses good hits.
    if parsed.ranks and extraction.rank and extraction.rank not in parsed.ranks:
        return False

    # Cheap expiry signal. Only applies when a deadline was extracted at
    # all, so it catches some closed postings, not all of them.
    if extraction.deadline:
        try:
            if date.fromisoformat(extraction.deadline) < today:
                return False
        except ValueError:
            # Not a real ISO date - drop the field's claim, keep the row.
            extraction.deadline = None

    return True


@opik.track(name="web_jobs.extract")
def extract_jobs(
    hits: list[WebHit],
    parsed: ParsedQuery,
    provider: LLMProvider | None = None,
    today: date | None = None,
) -> list[FormattedJob]:
    """Turn raw search hits into job rows, dropping anything that isn't one.

    One batched LLM call rather than one per hit: it's a single round trip
    instead of N, stays well inside the free tiers' per-minute limits, and
    lets the model see the whole result set at once. Each returned element
    is validated on its own, so one malformed entry costs that hit rather
    than the batch.

    Raises LLMProviderError (or its subclasses) if the call fails outright,
    and ValueError if the response isn't usable JSON - fallback_results
    turns both into "no web results".
    """
    # Dropped before the model sees them, so they neither cost prompt
    # tokens nor occupy one of its result slots.
    hits = [hit for hit in hits if _is_http_url(hit.url) and not _is_listing_url(hit.url)]
    if not hits:
        return []

    chosen_provider = provider or _default_provider()
    today = today or date.today()

    raw = chosen_provider.complete_json(_extraction_prompt(parsed, today), _hits_as_prompt(hits))
    payload = json.loads(raw)
    entries = payload.get("jobs", []) if isinstance(payload, dict) else []

    jobs: list[FormattedJob] = []
    for entry in entries:
        try:
            extraction = _Extraction.model_validate(entry)
        except ValidationError:
            continue
        if not 0 <= extraction.index < len(hits):
            continue

        hit = hits[extraction.index]
        if not _is_http_url(hit.url):
            continue
        if not _keep(extraction, parsed, today):
            continue

        jobs.append(_to_formatted_job(hit, extraction))

    return _dedupe(jobs)[:MAX_WEB_JOBS]


def fallback_results(
    parsed: ParsedQuery, provider: LLMProvider | None = None
) -> tuple[list[FormattedJob], str | None]:
    """Search the web for jobs Inspire didn't have. Never raises.

    Returns (jobs, error message or None) - the same collect-don't-raise
    shape as institution_papers.fetch_and_store_many, for the same reason:
    this is an optional enhancement to a search that has already succeeded,
    so a failure here must not turn a working (if empty) response into an
    error.
    """
    query = build_query(parsed)
    if not query:
        return [], None

    try:
        hits = search_raw(query)
    except WebSearchError as exc:
        return [], str(exc)

    try:
        return extract_jobs(hits, parsed, provider), None
    except LLMProviderError as exc:
        # No LLM means no verification, and an unchecked aggregator page
        # badged as a result is worse than an empty table - so drop the
        # hits entirely rather than showing them unfiltered.
        return [], f"Could not read web results: {exc}"
    except ValueError as exc:  # malformed JSON from the model
        return [], f"Unreadable web result data: {exc}"
    except Exception as exc:  # noqa: BLE001 - see docstring
        # Deliberately broad. main.py's catch-all turns anything unhandled
        # into a 503, which would make this optional feature able to break
        # a search that already worked.
        logger.exception("web_fallback_unexpected_error")
        return [], f"Web search failed unexpectedly: {exc}"

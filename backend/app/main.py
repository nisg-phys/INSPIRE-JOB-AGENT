import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import cache
from app.config import get_settings
from app.formatter import FormattedJob, format_jobs
from app.inspire_client import InspireAPIError, search_jobs
from app.institution_papers import (
    MAX_PARALLEL_FETCHES,
    Paper,
    fetch_and_store_many,
    get_recent_papers,
    select_relevant_papers,
)
from app.jobs_log import known_institutions, log_jobs
from app.logging_config import request_id_var, setup_logging
from app.query_rewriter import (
    INSPIRE_CATEGORIES,
    QueryRewriteError,
    analyze_query,
    mentions_career_stage,
    to_job_query_params,
)
from app.tracing import setup_tracing
from app.web_jobs import fallback_results, is_enabled, should_fall_back

# Fail loudly at import time (i.e. before uvicorn starts serving) if required
# config is missing, rather than failing on the first request.
get_settings()

setup_tracing()
setup_logging()
logger = logging.getLogger("app.request")

# Bounds how many never-before-seen institutions a single search request
# will fetch live from Inspire's literature API (see the on-demand fallback
# in the search endpoint below). Kept equal to institution_papers'
# MAX_PARALLEL_FETCHES so they run as a single wave: a 25-job page spans
# ~20 institutions, and at 10 the second wave was adding ~8s to a first
# visit. Institutions past the cap get their papers from the worker's next
# daily run, and from later searches, which each enrich a few more.
MAX_ON_DEMAND_ENRICH = MAX_PARALLEL_FETCHES

# Upper bound on how many institution names one deferred-papers request may
# name. A 25-job page spans ~20 institutions; this leaves headroom without
# letting a caller hand us an unbounded list.
MAX_DEFERRED_INSTITUTIONS = 40

OFF_TOPIC_NOTICE = (
    "Pulsar only searches academic research job postings in physics, astronomy and "
    "related fields. Try naming a subfield and a career stage, for example "
    "\"postdoc in cosmology\"."
)

app = FastAPI(title="Inspire Jobs Agent")

# The deployed frontend (T8.4) is served from the same Firebase Hosting
# domain as this API (via a rewrite to Cloud Run), so its requests are
# same-origin and don't need CORS at all. This only matters for local dev,
# where the frontend runs on a separate static server (see frontend's
# isLocalDev check) and for direct API testing/tooling.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "https://pulsar-jobs-agent.web.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id_var.set(request_id)
    start = time.monotonic()
    logger.info("request_started", extra={"method": request.method, "path": request.url.path})
    try:
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        response.headers["x-request-id"] = request_id
        return response
    except Exception:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        logger.exception(
            "request_failed",
            extra={"method": request.method, "path": request.url.path, "duration_ms": duration_ms},
        )
        raise
    finally:
        request_id_var.reset(token)


class SearchRequest(BaseModel):
    query: str
    # Set on the second call of a two-turn clarification exchange: the
    # user's answer to a previous response's clarification_question.
    # Passing prior turns back in the request is the "minimal session
    # state" the plan calls for - no server-side conversation storage.
    clarification_answer: str | None = None


class SearchResponse(BaseModel):
    cached: bool = False
    results: list[FormattedJob] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    # True when Inspire had nothing and the web fallback ran, so the UI can
    # say "we looked on the web too" rather than just "no results".
    web_searched: bool = False
    # Set when the query isn't a search for academic research positions.
    # Distinct from "no results": nothing was searched at all.
    off_topic: bool = False
    notice: str | None = None
    # How many jobs matched on Inspire, which is usually more than the page
    # returned. None when unknown (a web-fallback result, or a cache entry
    # written before this was recorded).
    total_matches: int | None = None
    # The query's subfield categories, echoed back so the deferred papers
    # request can rank papers by the same relevance the inline path uses.
    paper_categories: list[str] = Field(default_factory=list)


@app.post("/jobs/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    # Last-resort safety net: an unexpected failure anywhere in _search
    # (e.g. a dropped DB connection - see db.py's pool_pre_ping for the
    # actual fix, this is the backstop for whatever that doesn't cover)
    # would otherwise bubble up as a raw, unhandled 500 with a traceback in
    # the response. HTTPExceptions are deliberate (already have a clean
    # status/detail from the try/excepts below) and pass through untouched.
    try:
        return _search(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("search_failed_unexpectedly", extra={"error": str(exc)})
        raise HTTPException(
            status_code=503, detail="Search is temporarily unavailable. Please try again."
        ) from exc


def _search(request: SearchRequest) -> SearchResponse:
    effective_query = request.query
    if request.clarification_answer:
        effective_query = f"{request.query} ({request.clarification_answer})"

    # A query that hasn't said which career stage it wants must not be
    # answered from cache. "string theory" and "postdoc in string theory"
    # embed very close together - they differ by one short phrase - so the
    # cache would serve the postdoc search's results and the clarification
    # question would never be asked. Skipping the lookup costs nothing
    # here: such a query needs the LLM anyway to produce its question.
    may_use_cache = mentions_career_stage(effective_query)

    cached = None
    if may_use_cache:
        cache_start = time.monotonic()
        cached = cache.lookup(effective_query)
        logger.info(
            "cache_lookup",
            extra={
                "hit": cached is not None,
                "similarity": cached.similarity if cached else None,
                "duration_ms": round((time.monotonic() - cache_start) * 1000, 1),
            },
        )
    else:
        logger.info("cache_lookup", extra={"hit": False, "skipped": "no_career_stage"})

    if cached is not None:
        return SearchResponse(
            cached=True, results=cached.result, total_matches=cached.total_matches
        )

    try:
        rewrite_start = time.monotonic()
        if request.clarification_answer:
            # Second turn: the user already resolved the ambiguity, so run
            # the search directly rather than re-checking ambiguous.
            parsed = analyze_query(effective_query)
        else:
            parsed = analyze_query(request.query)
        # Timed because this is the one step big enough to dominate a slow
        # response, and without it the gap between the logged steps and the
        # request total has to be guessed at. A provider failing over to the
        # next in the chain shows up here as several seconds.
        logger.info(
            "query_rewrite",
            extra={"duration_ms": round((time.monotonic() - rewrite_start) * 1000, 1)},
        )

        # Checked before anything is searched, and on both turns: Pulsar
        # answers job searches, and the web fallback below would otherwise
        # turn any other question into a general web search. Not cached -
        # a rejection isn't a result set, and storing it as one would serve
        # a bare "0 results" to the next matching query.
        if not parsed.on_topic:
            logger.info("query_rejected_off_topic")
            return SearchResponse(off_topic=True, notice=OFF_TOPIC_NOTICE)

        if not request.clarification_answer:
            logger.info("ambiguity_check", extra={"ambiguous": parsed.ambiguous})
            if parsed.ambiguous:
                return SearchResponse(
                    needs_clarification=True,
                    clarification_question=parsed.clarification_question,
                )
        params = to_job_query_params(parsed)
    except QueryRewriteError as exc:
        logger.error("query_rewrite_failed", extra={"error": str(exc)})
        raise HTTPException(status_code=502, detail=f"Query rewriting failed: {exc}") from exc

    try:
        inspire_start = time.monotonic()
        search_result = search_jobs(params)
        jobs = search_result.jobs
        total_matches = search_result.total
        logger.info(
            "inspire_search",
            extra={
                "keywords": params.keywords,
                "job_count": len(jobs),
                "total_matches": total_matches,
                "duration_ms": round((time.monotonic() - inspire_start) * 1000, 1),
            },
        )
    except InspireAPIError as exc:
        logger.error("inspire_search_failed", extra={"error": str(exc)})
        raise HTTPException(status_code=502, detail=f"Inspire search failed: {exc}") from exc

    log_jobs(jobs)
    result = format_jobs(jobs)

    # Read-only join against institution_papers (Phase 4's enrichment worker
    # output). Institutions not yet enriched are simply absent from the map,
    # so their jobs keep the default empty recent_papers list.
    institutions = sorted({name for job in jobs for name in job.institutions})
    papers_by_institution = get_recent_papers(institutions)

    # Institutions with nothing stored yet are deliberately NOT fetched here.
    # That live Inspire literature call was 6-16s of a cold response, dwarfing
    # the ~1s search itself, and it blocked jobs the user could already have
    # been reading. The frontend asks for those separately (see
    # /institutions/papers) and fills the cells in when they arrive.
    logger.info(
        "institution_enrichment",
        extra={
            "institutions_seen": len(institutions),
            "institutions_from_store": len(papers_by_institution),
            "institutions_deferred": len(
                [name for name in institutions if name not in papers_by_institution]
            ),
        },
    )
    for raw_job, formatted_job in zip(jobs, result):
        candidate_papers = []
        seen_record_ids: set[str] = set()
        for institution in raw_job.institutions:
            for paper in papers_by_institution.get(institution, []):
                if paper.record_id not in seen_record_ids:
                    seen_record_ids.add(paper.record_id)
                    candidate_papers.append(paper)
        # Prefer papers matching the query's subfield category over just
        # showing the institution's most recent output overall.
        formatted_job.recent_papers = select_relevant_papers(candidate_papers, parsed.categories)

    # Web fallback: Inspire is curated and many groups never post to it, so
    # a narrow query can come back empty while the position exists on a
    # university page. Runs only when Inspire found nothing - see
    # web_jobs.should_fall_back for why not on thin-but-nonempty results.
    # Placed after the loop above: it extends `result`, which zip() pairs
    # positionally against `jobs`.
    fallback_error = None
    web_searched = False
    if should_fall_back(len(jobs)) and is_enabled():
        web_searched = True
        web_start = time.monotonic()
        web_jobs, fallback_error = fallback_results(parsed)
        result.extend(web_jobs)
        logger.info(
            "web_fallback",
            extra={
                "kept_count": len(web_jobs),
                "duration_ms": round((time.monotonic() - web_start) * 1000, 1),
            },
        )
        if fallback_error:
            logger.warning("web_fallback_failed", extra={"error": fallback_error})

    # Web rows aren't drawn from a counted result set, so the Inspire total
    # would misdescribe the page actually shown.
    reported_total = None if web_searched else total_matches

    # An empty result is the least stable thing to remember, so it's only
    # cached when the web fallback ran cleanly and found nothing too - that
    # is a real "nothing anywhere", and repeating it would spend a Tavily
    # credit each time. Any other empty result is skipped:
    #  - the fallback errored: a transient Tavily or LLM outage would be
    #    frozen in as "no results" for the full 24h TTL;
    #  - the fallback didn't run (no key, or it wasn't deployed yet): the
    #    empty answer is only true of *this* build. A query answered empty
    #    two minutes before the fallback shipped was served that empty
    #    answer for a day, and never reached the code that would have
    #    found the posting.
    cacheable = bool(result) or (web_searched and not fallback_error)
    if cacheable:
        cache.store(effective_query, params, result, reported_total)
    return SearchResponse(
        cached=False,
        results=result,
        web_searched=web_searched,
        total_matches=reported_total,
        paper_categories=parsed.categories,
    )


class PapersRequest(BaseModel):
    institutions: list[str] = Field(default_factory=list)
    # The search's categories, so deferred papers are ranked by the same
    # subfield relevance as the ones that came back with the jobs.
    categories: list[str] = Field(default_factory=list)


class PapersResponse(BaseModel):
    papers: dict[str, list[Paper]] = Field(default_factory=dict)


@app.post("/institutions/papers", response_model=PapersResponse)
def institution_papers(request: PapersRequest) -> PapersResponse:
    """Papers for institutions a search didn't already have them for.

    Split out of the search path because it's the slow half: reading stored
    papers is a few milliseconds, but fetching a never-seen institution's
    from Inspire took 6-16s and held up jobs the user could already read.
    Failure is not an error here - the cells simply stay as they were.
    """
    names = [name.strip() for name in request.institutions if name and name.strip()]
    if not names:
        return PapersResponse()

    # The names arrive from the client and end up inside an Inspire query,
    # so they're resolved against institutions actually logged from a real
    # posting. Anything else is dropped rather than searched for.
    resolved = known_institutions(sorted(set(names))[:MAX_DEFERRED_INSTITUTIONS])
    if not resolved:
        logger.info("deferred_papers", extra={"requested": len(names), "recognised": 0})
        return PapersResponse()

    stored = get_recent_papers(list(resolved))
    missing = {name: rid for name, rid in resolved.items() if name not in stored}

    fetch_start = time.monotonic()
    fetched, failures = fetch_and_store_many(dict(list(missing.items())[:MAX_ON_DEMAND_ENRICH]))
    for institution, error in failures.items():
        logger.warning(
            "deferred_enrichment_failed", extra={"institution": institution, "error": error}
        )

    logger.info(
        "deferred_papers",
        extra={
            "requested": len(names),
            "recognised": len(resolved),
            "from_store": len(stored),
            "fetched": len(fetched),
            "failed": len(failures),
            "duration_ms": round((time.monotonic() - fetch_start) * 1000, 1),
        },
    )

    categories = [c for c in request.categories if c in INSPIRE_CATEGORIES]
    combined = {**stored, **fetched}
    return PapersResponse(
        papers={
            name: select_relevant_papers(papers, categories) for name, papers in combined.items()
        }
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

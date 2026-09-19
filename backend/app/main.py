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
from app.inspire_literature_client import InspireAPIError as LiteratureAPIError
from app.institution_papers import fetch_and_store, get_recent_papers, select_relevant_papers
from app.jobs_log import log_jobs
from app.logging_config import request_id_var, setup_logging
from app.query_rewriter import QueryRewriteError, analyze_query, to_job_query_params
from app.tracing import setup_tracing

# Fail loudly at import time (i.e. before uvicorn starts serving) if required
# config is missing, rather than failing on the first request.
get_settings()

setup_tracing()
setup_logging()
logger = logging.getLogger("app.request")

# Bounds how many never-before-seen institutions a single search request
# will fetch live from Inspire's literature API (see the on-demand fallback
# in the search endpoint below) - each is an extra ~1s+ HTTP call, so this
# caps the worst case added latency rather than letting one broad query
# block on an unbounded number of them.
MAX_ON_DEMAND_ENRICH = 5

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
    if cached is not None:
        return SearchResponse(cached=True, results=cached.result)

    try:
        if request.clarification_answer:
            # Second turn: the user already resolved the ambiguity, so run
            # the search directly rather than re-checking ambiguous.
            parsed = analyze_query(effective_query)
        else:
            parsed = analyze_query(request.query)
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
        jobs = search_jobs(params)
        logger.info(
            "inspire_search",
            extra={
                "keywords": params.keywords,
                "job_count": len(jobs),
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

    # On-demand fallback: fetch live for institutions still missing, rather
    # than making a never-before-seen institution wait for the next worker
    # run. Capped so one broad query naming many new institutions can't
    # balloon this request's latency - anything past the cap just stays
    # "not yet available" until the worker's next pass.
    missing = [name for name in institutions if name not in papers_by_institution]
    on_demand_fetched = 0
    on_demand_failed = 0
    for institution in missing[:MAX_ON_DEMAND_ENRICH]:
        try:
            papers_by_institution[institution] = fetch_and_store(institution)
            on_demand_fetched += 1
        except LiteratureAPIError as exc:
            on_demand_failed += 1
            logger.warning(
                "on_demand_enrichment_failed", extra={"institution": institution, "error": str(exc)}
            )

    logger.info(
        "institution_enrichment",
        extra={
            "institutions_seen": len(institutions),
            "institutions_enriched": len(papers_by_institution),
            "on_demand_fetched": on_demand_fetched,
            "on_demand_failed": on_demand_failed,
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

    cache.store(effective_query, params, result)
    return SearchResponse(cached=False, results=result)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

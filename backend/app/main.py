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
from app.institution_papers import get_recent_papers, select_relevant_papers
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

app = FastAPI(title="Inspire Jobs Agent")

# Permissive for now (no auth, local dev + free-tier deploy target) - the
# frontend (T7.x) calls this API cross-origin from a separate static host.
# Worth tightening to specific origins once there's a real deployed frontend
# URL (T8.4).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    logger.info(
        "institution_enrichment",
        extra={
            "institutions_seen": len(institutions),
            "institutions_enriched": len(papers_by_institution),
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

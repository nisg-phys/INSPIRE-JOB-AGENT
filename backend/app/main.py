from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import cache
from app.config import get_settings
from app.formatter import FormattedJob, format_jobs
from app.inspire_client import InspireAPIError, search_jobs
from app.institution_papers import get_recent_papers
from app.jobs_log import log_jobs
from app.query_rewriter import QueryRewriteError, analyze_query, to_job_query_params

# Fail loudly at import time (i.e. before uvicorn starts serving) if required
# config is missing, rather than failing on the first request.
get_settings()

app = FastAPI(title="Inspire Jobs Agent")


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

    cached = cache.lookup(effective_query)
    if cached is not None:
        return SearchResponse(cached=True, results=cached.result)

    try:
        if request.clarification_answer:
            # Second turn: the user already resolved the ambiguity, so run
            # the search directly rather than re-checking ambiguous.
            params = to_job_query_params(analyze_query(effective_query))
        else:
            parsed = analyze_query(request.query)
            if parsed.ambiguous:
                return SearchResponse(
                    needs_clarification=True,
                    clarification_question=parsed.clarification_question,
                )
            params = to_job_query_params(parsed)
    except QueryRewriteError as exc:
        raise HTTPException(status_code=502, detail=f"Query rewriting failed: {exc}") from exc

    try:
        jobs = search_jobs(params)
    except InspireAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Inspire search failed: {exc}") from exc

    log_jobs(jobs)
    result = format_jobs(jobs)

    # Read-only join against institution_papers (Phase 4's enrichment worker
    # output). Institutions not yet enriched are simply absent from the map,
    # so their jobs keep the default empty recent_papers list.
    papers_by_institution = get_recent_papers(
        sorted({name for job in jobs for name in job.institutions})
    )
    for raw_job, formatted_job in zip(jobs, result):
        seen_record_ids: set[str] = set()
        for institution in raw_job.institutions:
            for paper in papers_by_institution.get(institution, []):
                if paper.record_id not in seen_record_ids:
                    seen_record_ids.add(paper.record_id)
                    formatted_job.recent_papers.append(paper)

    cache.store(effective_query, params, result)
    return SearchResponse(cached=False, results=result)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

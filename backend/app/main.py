from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import cache
from app.config import get_settings
from app.formatter import FormattedJob, format_jobs
from app.inspire_client import InspireAPIError, search_jobs
from app.institution_papers import get_recent_papers
from app.jobs_log import log_jobs
from app.query_rewriter import QueryRewriteError, rewrite_query

# Fail loudly at import time (i.e. before uvicorn starts serving) if required
# config is missing, rather than failing on the first request.
get_settings()

app = FastAPI(title="Inspire Jobs Agent")


class SearchRequest(BaseModel):
    query: str


class SearchResponse(BaseModel):
    cached: bool
    results: list[FormattedJob]


@app.post("/jobs/search", response_model=SearchResponse)
def search(request: SearchRequest) -> SearchResponse:
    cached = cache.lookup(request.query)
    if cached is not None:
        return SearchResponse(cached=True, results=cached.result)

    try:
        params = rewrite_query(request.query)
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

    cache.store(request.query, params, result)
    return SearchResponse(cached=False, results=result)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import cache
from app.config import get_settings
from app.formatter import FormattedJob, format_jobs
from app.inspire_client import InspireAPIError, search_jobs
from app.query_rewriter import QueryRewriteError, rewrite_query

# Fail loudly at import time (i.e. before uvicorn starts serving) if required
# config is missing, rather than failing on the first request.
get_settings()

app = FastAPI(title="Inspire Jobs Agent")


class SearchRequest(BaseModel):
    query: str


@app.post("/jobs/search", response_model=list[FormattedJob])
def search(request: SearchRequest) -> list[FormattedJob]:
    cached = cache.lookup(request.query)
    if cached is not None:
        return cached.result

    try:
        params = rewrite_query(request.query)
    except QueryRewriteError as exc:
        raise HTTPException(status_code=502, detail=f"Query rewriting failed: {exc}") from exc

    try:
        jobs = search_jobs(params)
    except InspireAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Inspire search failed: {exc}") from exc

    result = format_jobs(jobs)
    cache.store(request.query, params, result)
    return result


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

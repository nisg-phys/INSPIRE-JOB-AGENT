from fastapi import FastAPI, HTTPException

from app.config import get_settings
from app.inspire_client import InspireAPIError, JobQueryParams, RawJob, search_jobs

# Fail loudly at import time (i.e. before uvicorn starts serving) if required
# config is missing, rather than failing on the first request.
get_settings()

app = FastAPI(title="Inspire Jobs Agent")


@app.post("/jobs/search", response_model=list[RawJob])
def search(params: JobQueryParams) -> list[RawJob]:
    try:
        return search_jobs(params)
    except InspireAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

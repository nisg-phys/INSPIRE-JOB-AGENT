"""Client for INSPIRE-HEP's literature API, used to fetch recent papers per institution."""

from __future__ import annotations

import httpx
from pydantic import BaseModel

INSPIRE_BASE_URL = "https://inspirehep.net/api"


class InspireAPIError(RuntimeError):
    """Raised when the INSPIRE-HEP API request fails."""


class Paper(BaseModel):
    record_id: str
    title: str
    citation_count: int = 0


def _request(endpoint: str, params: dict) -> dict:
    url = f"{INSPIRE_BASE_URL}/{endpoint}"
    try:
        response = httpx.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise InspireAPIError("Rate limit exceeded. Please wait and retry.") from exc
        raise InspireAPIError(f"HTTP error from INSPIRE: {exc}") from exc
    except httpx.TimeoutException as exc:
        raise InspireAPIError("Request to INSPIRE timed out.") from exc
    except httpx.ConnectError as exc:
        raise InspireAPIError("Could not connect to INSPIRE.") from exc


def recent_papers(institution: str, size: int = 5) -> list[Paper]:
    """Fetch the most recent literature records affiliated with an institution."""
    params = {"q": f'aff "{institution}"', "size": size, "sort": "mostrecent"}
    data = _request("literature", params)
    hits = data["hits"]["hits"]

    papers = []
    for hit in hits:
        metadata = hit["metadata"]
        papers.append(
            Paper(
                record_id=str(metadata.get("control_number", "")),
                title=metadata["titles"][0]["title"],
                citation_count=metadata.get("citation_count", 0),
            )
        )
    return papers

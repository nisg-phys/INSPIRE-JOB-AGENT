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
    # INSPIRE's own normalized category terms (e.g. "Gravitation and
    # Cosmology", "Theory-HEP") - present on every record, not just arXiv
    # ones. Lets the backend prefer papers relevant to a job's subfield
    # instead of just showing an institution's most recent output overall.
    categories: list[str] = []


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


def _affiliation_query(institution: str, institution_id: str | None) -> str:
    # Prefer the institution's record id: it matches every spelling of the
    # affiliation ("Kentucky U." and "U. Kentucky" alike). The exact-phrase
    # name search is only the fallback for free-text postings with no linked
    # institution record, and misses whenever the posting's spelling differs
    # from the papers' (e.g. `aff "U. Kentucky"` -> 0 hits vs 2,000+ by id).
    if institution_id:
        return f"affid {institution_id}"
    return f'aff "{institution}"'


def recent_papers(institution: str, institution_id: str | None = None, size: int = 20) -> list[Paper]:
    """Fetch the most recent literature records affiliated with an institution.

    Fetches more than a job listing would ever show (default 20, was 5) so
    the backend has enough of an institution's recent output to filter down
    to whatever's actually relevant to a given posting's subfield.
    """
    params = {"q": _affiliation_query(institution, institution_id), "size": size, "sort": "mostrecent"}
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
                categories=[
                    c["term"] for c in metadata.get("inspire_categories", []) if c.get("term")
                ],
            )
        )
    return papers

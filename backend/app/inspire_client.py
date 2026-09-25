"""Client for the INSPIRE-HEP jobs API.

Isolates the rest of the app from Inspire's actual response shape. Ported
from the jobs-search logic in the langchain-inspire-hep package (a thin
requests+pydantic wrapper with no langchain dependency), adapted to this
app's naming.
"""

from __future__ import annotations

import html
import re
from typing import Literal, Optional

import requests
from pydantic import BaseModel, Field

INSPIRE_BASE_URL = "https://inspirehep.net/api"

# INSPIRE's controlled vocabulary for job rank (schemas/records/jobs.json).
RANKS = ["STAFF", "SENIOR", "JUNIOR", "VISITOR", "POSTDOC", "PHD", "MASTER", "UNDERGRADUATE", "OTHER"]
Rank = Literal["STAFF", "SENIOR", "JUNIOR", "VISITOR", "POSTDOC", "PHD", "MASTER", "UNDERGRADUATE", "OTHER"]


class InspireAPIError(RuntimeError):
    """Raised when the INSPIRE-HEP API request fails."""


def _strip_html(value: str) -> str:
    """Strip HTML tags from an INSPIRE free-text field and collapse whitespace."""
    text = re.sub(r"</(p|div|li|ul|ol|br)\s*>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


class ContactDetail(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None


class RawJob(BaseModel):
    """A single job posting as returned by INSPIRE, lightly parsed."""

    record_id: str
    position: str
    institutions: list[str] = Field(default_factory=list)
    # Institution name -> INSPIRE institution record id, for the entries that
    # are linked to an institution record (~95% of open postings). The name
    # string alone is unreliable as a search key: the same institution shows
    # up as "Kentucky U." on one posting and "U. Kentucky" on another, and
    # only one of those matches a paper's affiliation string.
    institution_ids: dict[str, str] = Field(default_factory=dict)
    ranks: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    deadline: Optional[str] = Field(default=None, description="ISO date, if listed.")
    status: Optional[str] = None
    description: Optional[str] = Field(
        default=None, description="Full posting text, HTML tags stripped."
    )
    urls: list[str] = Field(default_factory=list)
    contact_details: list[ContactDetail] = Field(default_factory=list)


class JobQueryParams(BaseModel):
    """Structured search params accepted by the Inspire jobs client."""

    keywords: str = Field(default="", description="Free-text query, e.g. 'string theory'.")
    status: Literal["open", "closed"] = "open"
    sort: Literal["mostrecent", "deadline"] = "mostrecent"
    size: int = Field(default=10, gt=0, le=1000)
    # Hard filter on INSPIRE's rank facet, NOT free text - Inspire's `q` is a
    # relevance-scored search, so e.g. keywords="faculty cosmology" happily
    # surfaces Master's/PhD postings that just mention "cosmology" strongly.
    # Inspire's rank filter only accepts one value per request (no OR), so
    # multiple ranks here mean multiple requests, merged - see search_jobs.
    ranks: list[Rank] = Field(default_factory=list)


def _request(endpoint: str, params: dict) -> dict:
    url = f"{INSPIRE_BASE_URL}/{endpoint}"
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as exc:
        if response.status_code == 429:
            raise InspireAPIError("Rate limit exceeded. Please wait and retry.") from exc
        raise InspireAPIError(f"HTTP error from INSPIRE: {exc}") from exc
    except requests.exceptions.Timeout as exc:
        raise InspireAPIError("Request to INSPIRE timed out.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise InspireAPIError("Could not connect to INSPIRE.") from exc


def _institution_ids(institutions: list[dict]) -> dict[str, str]:
    """Map institution name -> record id, from each entry's record.$ref URL
    (e.g. ".../api/institutions/904048"). Free-text entries with no linked
    record are simply absent from the result.
    """
    ids: dict[str, str] = {}
    for inst in institutions:
        name = inst.get("value", "")
        match = re.search(r"/institutions/(\d+)", inst.get("record", {}).get("$ref", ""))
        if name and match:
            ids[name] = match.group(1)
    return ids


def _search_jobs_single(params: JobQueryParams, rank: Rank | None) -> list[RawJob]:
    query_params = {
        "q": params.keywords,
        "size": params.size,
        "sort": params.sort,
        "status": params.status,
    }
    if rank:
        query_params["rank"] = rank

    data = _request("jobs", query_params)
    hits = data["hits"]["hits"]

    jobs = []
    for hit in hits:
        metadata = hit["metadata"]
        description = metadata.get("description")
        contacts = [
            ContactDetail(name=contact.get("name"), email=contact.get("email"))
            for contact in metadata.get("contact_details", [])
        ]
        jobs.append(
            RawJob(
                record_id=str(metadata.get("control_number", "")),
                position=metadata.get("position", "Unknown position"),
                institutions=[inst.get("value", "") for inst in metadata.get("institutions", [])],
                institution_ids=_institution_ids(metadata.get("institutions", [])),
                ranks=metadata.get("ranks", []),
                regions=metadata.get("regions", []),
                deadline=metadata.get("deadline_date"),
                status=metadata.get("status", params.status),
                description=_strip_html(description) if description else None,
                urls=[u.get("value", "") for u in metadata.get("urls", [])],
                contact_details=contacts,
            )
        )
    return jobs


def search_jobs(params: JobQueryParams) -> list[RawJob]:
    """Search INSPIRE job postings.

    Raises:
        InspireAPIError: If the underlying request fails.
    """
    if not params.ranks:
        return _search_jobs_single(params, rank=None)

    # INSPIRE's rank filter takes one value per request; fetch each
    # requested rank separately and merge, deduped by record_id.
    seen_ids: set[str] = set()
    jobs: list[RawJob] = []
    for rank in params.ranks:
        for job in _search_jobs_single(params, rank=rank):
            if job.record_id not in seen_ids:
                seen_ids.add(job.record_id)
                jobs.append(job)
    return jobs[: params.size]

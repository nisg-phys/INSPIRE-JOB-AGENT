"""Client for the INSPIRE-HEP jobs API.

Isolates the rest of the app from Inspire's actual response shape. Ported
from the jobs-search logic in the langchain-inspire-hep package (a thin
requests+pydantic wrapper with no langchain dependency), adapted to this
app's naming.
"""

from __future__ import annotations

import contextvars
import html
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

import requests
from pydantic import BaseModel, Field

INSPIRE_BASE_URL = "https://inspirehep.net/api"

# Upper bound for the ids-only counting request: Inspire has ~207 open jobs
# in total, so this covers any single rank with room to spare.
MAX_COUNT_RECORDS = 500
MAX_PARALLEL_RANK_FETCHES = 4

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


class JobSearchResult(BaseModel):
    """A page of jobs plus how many matched, so the UI can say what's hidden."""

    jobs: list[RawJob] = Field(default_factory=list)
    total: int = 0


class JobQueryParams(BaseModel):
    """Structured search params accepted by the Inspire jobs client."""

    keywords: str = Field(default="", description="Free-text query, e.g. 'string theory'.")
    status: Literal["open", "closed"] = "open"
    # Soonest deadline first: someone searching for a job needs to know what
    # closes next, and there's no pagination to reach anything further down.
    sort: Literal["mostrecent", "deadline"] = "deadline"
    # Deadlines cluster hard - 24 of the ~207 currently-open jobs share one
    # date, and a broad postdoc search has 15 on one. A page of 10 would cut
    # through the middle of such a group with no way to see the rest.
    size: int = Field(default=25, gt=0, le=1000)
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


def _search_jobs_single(params: JobQueryParams, rank: Rank | None) -> tuple[list[RawJob], int]:
    """One Inspire request. Returns its jobs and how many matched in total."""
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
    total = data["hits"].get("total", len(hits))

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
    return jobs, total


def _matching_record_ids(params: JobQueryParams, rank: Rank) -> set[str]:
    """Every matching record id for one rank, ids only.

    Used to count a multi-rank search exactly. Summing the per-rank totals
    would overcount badly - 22 of the 55 open JUNIOR-or-SENIOR jobs are
    listed under both ranks, so a "faculty" search would claim 77 - and
    asking for just the ids is cheap (~26KB for all 120 open postdocs,
    against ~120KB for a single page of 25 full records).
    """
    data = _request(
        "jobs",
        {
            "q": params.keywords,
            "size": MAX_COUNT_RECORDS,
            "status": params.status,
            "rank": rank,
            "fields": "control_number",
        },
    )
    return {
        str(hit["metadata"]["control_number"])
        for hit in data["hits"]["hits"]
        if hit.get("metadata", {}).get("control_number")
    }


def _deadline_key(job: RawJob) -> tuple[int, str]:
    # Undated postings sort last rather than first: an absent deadline isn't
    # an urgent one. Every currently-open Inspire job has a date, so this
    # only guards the edge case.
    return (1, "") if not job.deadline else (0, job.deadline)


def search_jobs(params: JobQueryParams) -> JobSearchResult:
    """Search INSPIRE job postings.

    Raises:
        InspireAPIError: If the underlying request fails.
    """
    if not params.ranks:
        jobs, total = _search_jobs_single(params, rank=None)
        return JobSearchResult(jobs=jobs[: params.size], total=total)

    if len(params.ranks) == 1:
        jobs, total = _search_jobs_single(params, rank=params.ranks[0])
        return JobSearchResult(jobs=jobs[: params.size], total=total)

    # Inspire's rank filter takes one value per request, so a multi-rank
    # query fans out. Requests run together: a "faculty" search is four
    # round trips (two of jobs, two of ids) and sequentially they'd stack up
    # on the user's critical path.
    def fetch(rank: Rank) -> tuple[list[RawJob], set[str]]:
        jobs, _ = _search_jobs_single(params, rank=rank)
        return jobs, _matching_record_ids(params, rank)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_RANK_FETCHES, len(params.ranks))) as pool:
        # A fresh context copy per task: one Context can't be entered by two
        # threads at once. Copying carries this request's id into the worker
        # threads so their logs stay attributable.
        futures = [
            pool.submit(contextvars.copy_context().run, fetch, rank) for rank in params.ranks
        ]
        fetched = [future.result() for future in futures]

    seen_ids: set[str] = set()
    jobs: list[RawJob] = []
    all_ids: set[str] = set()
    for rank_jobs, rank_ids in fetched:
        all_ids |= rank_ids
        for job in rank_jobs:
            if job.record_id not in seen_ids:
                seen_ids.add(job.record_id)
                jobs.append(job)

    # Re-sort before truncating. Each rank's page arrives in its own order,
    # so concatenating them and cutting at `size` handed every slot to the
    # first rank: a JUNIOR+SENIOR search filled all 10 rows with JUNIOR and
    # hid six SENIOR jobs closing sooner, one of them within the week.
    # Taking `size` from each rank first guarantees the true earliest
    # `size` are among the merged candidates.
    if params.sort == "deadline":
        jobs.sort(key=_deadline_key)

    return JobSearchResult(jobs=jobs[: params.size], total=len(all_ids))

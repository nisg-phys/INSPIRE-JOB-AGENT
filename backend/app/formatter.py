"""Normalizes RawJob objects into the app's stable display schema."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field

from app.inspire_client import ContactDetail, RawJob
from app.institution_papers import Paper

# Known third-party application platforms, matched by domain. Anything not
# in here is assumed to be the hiring institution's own site.
KNOWN_APPLICATION_PLATFORMS = {
    "academicjobsonline.org": "AcademicJobsOnline",
    "interfolio.com": "Interfolio",
    "academicpositions.com": "Academic Positions",
    "jobs.ac.uk": "jobs.ac.uk",
    "higheredjobs.com": "HigherEdJobs",
}


class FormattedJob(BaseModel):
    record_id: str
    title: str
    institution: str
    deadline: str | None = None
    location: str | None = None
    link: str | None = None
    apply_via: str = "See posting"
    tags: list[str] = Field(default_factory=list)
    # Populated after formatting, from institution_papers (T4.5) - empty
    # until the worker has enriched at least one of this job's institutions.
    recent_papers: list[Paper] = Field(default_factory=list)


def _apply_via(link: str | None, contacts: list[ContactDetail]) -> str:
    if link:
        domain = urlparse(link).netloc.lower().removeprefix("www.")
        for platform_domain, label in KNOWN_APPLICATION_PLATFORMS.items():
            if domain == platform_domain or domain.endswith(f".{platform_domain}"):
                return label
        if domain:
            return f"Institute website ({domain})"

    emails = [contact.email for contact in contacts if contact.email]
    if emails:
        return f"Email: {emails[0]}"

    return "See posting on INSPIRE"


def _format_job(job: RawJob) -> FormattedJob:
    institutions = [name for name in job.institutions if name]
    regions = [region for region in job.regions if region]
    external_links = [url for url in job.urls if url]
    external_link = external_links[0] if external_links else None

    # Every posting has an INSPIRE record, so there's always somewhere to
    # send someone even when there's no external application URL (e.g. an
    # email-only or unlisted-application posting).
    inspire_url = f"https://inspirehep.net/jobs/{job.record_id}"

    return FormattedJob(
        record_id=job.record_id,
        title=job.position or "Unknown position",
        institution="; ".join(institutions) if institutions else "Unknown institution",
        deadline=job.deadline,
        location=", ".join(regions) if regions else None,
        link=external_link or inspire_url,
        apply_via=_apply_via(external_link, job.contact_details),
        tags=[rank for rank in job.ranks if rank],
    )


def format_jobs(jobs: list[RawJob]) -> list[FormattedJob]:
    """Normalize raw Inspire jobs into a consistent, display-ready table."""
    return [_format_job(job) for job in jobs]

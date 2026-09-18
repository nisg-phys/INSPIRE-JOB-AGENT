"""Normalizes RawJob objects into the app's stable display schema."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.inspire_client import RawJob


class FormattedJob(BaseModel):
    record_id: str
    title: str
    institution: str
    deadline: str | None = None
    location: str | None = None
    link: str | None = None
    tags: list[str] = Field(default_factory=list)


def _format_job(job: RawJob) -> FormattedJob:
    institutions = [name for name in job.institutions if name]
    regions = [region for region in job.regions if region]
    links = [url for url in job.urls if url]

    return FormattedJob(
        record_id=job.record_id,
        title=job.position or "Unknown position",
        institution="; ".join(institutions) if institutions else "Unknown institution",
        deadline=job.deadline,
        location=", ".join(regions) if regions else None,
        link=links[0] if links else None,
        tags=[rank for rank in job.ranks if rank],
    )


def format_jobs(jobs: list[RawJob]) -> list[FormattedJob]:
    """Normalize raw Inspire jobs into a consistent, display-ready table."""
    return [_format_job(job) for job in jobs]

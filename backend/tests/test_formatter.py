from app.formatter import FormattedJob, _apply_via, _format_job
from app.inspire_client import ContactDetail, RawJob


def test_apply_via_detects_known_platform():
    assert _apply_via("https://academicjobsonline.org/ajo/jobs/1234", []) == "AcademicJobsOnline"


def test_apply_via_detects_known_platform_with_www_prefix():
    assert _apply_via("https://www.academicjobsonline.org/ajo/jobs/1234", []) == "AcademicJobsOnline"


def test_apply_via_falls_back_to_institute_website():
    assert _apply_via("https://physics.harvard.edu/jobs/postdoc", []) == "Institute website (physics.harvard.edu)"


def test_apply_via_falls_back_to_email_when_no_link():
    contacts = [ContactDetail(name="Jane Doe", email="jane@example.edu")]
    assert _apply_via(None, contacts) == "Email: jane@example.edu"


def test_apply_via_uses_first_email_when_several():
    contacts = [
        ContactDetail(name="A", email="a@example.edu"),
        ContactDetail(name="B", email="b@example.edu"),
    ]
    assert _apply_via(None, contacts) == "Email: a@example.edu"


def test_apply_via_final_fallback_when_no_link_and_no_email():
    assert _apply_via(None, []) == "See posting on INSPIRE"


def _raw_job(**overrides) -> RawJob:
    defaults = dict(record_id="123", position="Postdoc", urls=[], contact_details=[])
    defaults.update(overrides)
    return RawJob(**defaults)


def test_format_job_falls_back_to_inspire_link_when_no_external_url():
    """Regression test: a posting with no external application URL must
    still get a clickable link, not a dead/empty one - the INSPIRE record
    page itself, where it was originally advertised.
    """
    job = _raw_job(urls=[])
    formatted = _format_job(job)

    assert formatted.link == "https://inspirehep.net/jobs/123"


def test_format_job_prefers_external_url_when_present():
    job = _raw_job(urls=["https://academicjobsonline.org/ajo/jobs/1234"])
    formatted = _format_job(job)

    assert formatted.link == "https://academicjobsonline.org/ajo/jobs/1234"


def test_format_job_defaults_missing_position_and_institution():
    job = _raw_job(position="", institutions=[])
    formatted = _format_job(job)

    assert formatted.title == "Unknown position"
    assert formatted.institution == "Unknown institution"


def test_inspire_jobs_are_labelled_as_such():
    job = _format_job(RawJob(record_id="1", position="Postdoc"))

    assert job.source == "inspire"


def test_a_cached_row_without_a_source_still_validates():
    """cache.py round-trips FormattedJob through JSONB, so rows written
    before `source` existed must still load - i.e. it must stay optional.
    """
    job = FormattedJob.model_validate(
        {"record_id": "1", "title": "Postdoc", "institution": "CERN"}
    )

    assert job.source == "inspire"

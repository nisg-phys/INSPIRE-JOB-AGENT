import json
from datetime import date, timedelta

import pytest

from app.llm.base import LLMProviderError
from app.query_rewriter import ParsedQuery
from app.web_jobs import (
    MAX_WEB_JOBS,
    WebHit,
    WebSearchError,
    build_query,
    extract_jobs,
    fallback_results,
    search_raw,
    should_fall_back,
)

TODAY = date(2026, 9, 25)


class FakeProvider:
    """Stands in for an LLMProvider. Mirrors tests/test_llm_router.py."""

    def __init__(self, response: str = "", error: str | None = None):
        self.response = response
        self.error = error
        self.calls = 0
        self.last_user_prompt = ""

    def complete_json(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_user_prompt = user
        if self.error:
            raise LLMProviderError(self.error)
        return self.response


def provider_returning(*entries: dict) -> FakeProvider:
    return FakeProvider(response=json.dumps({"jobs": list(entries)}))


def entry(index: int = 0, **overrides) -> dict:
    """A well-formed extraction the filters accept, unless overridden."""
    base = {
        "index": index,
        "is_job_posting": True,
        "relevance": "direct",
        "title": "Postdoctoral Researcher",
        "institution": "Example University",
        "location": "Germany",
        "deadline": None,
        "rank": "POSTDOC",
        "confidence": "high",
    }
    return {**base, **overrides}


def hit(url: str = "https://example.edu/jobs/1", **overrides) -> WebHit:
    return WebHit(url=url, **{"title": "Postdoc opening", "content": "...", **overrides})


# --- trigger predicate ------------------------------------------------


def test_falls_back_only_when_inspire_found_nothing():
    assert should_fall_back(0) is True
    assert should_fall_back(1) is False
    assert should_fall_back(10) is False


# --- query building ---------------------------------------------------


def test_build_query_uses_a_searchable_rank_phrase_not_the_rank_code():
    """Inspire's rank codes are filter values, not words that appear on a
    job ad - searching the web for "POSTDOC" matches nothing.
    """
    query = build_query(ParsedQuery(subfield="string theory", ranks=["POSTDOC"]))

    assert "POSTDOC" not in query
    assert "postdoctoral position" in query
    assert "string theory" in query


def test_build_query_includes_location_and_extra_keywords():
    query = build_query(
        ParsedQuery(subfield="cosmology", location="Germany", keywords=["DFG"], ranks=["PHD"])
    )

    assert "cosmology" in query
    assert "Germany" in query
    assert "DFG" in query


def test_build_query_falls_back_to_seniority_text_when_no_rank_code():
    query = build_query(ParsedQuery(subfield="lattice QCD", seniority="fellowship"))

    assert "lattice QCD" in query
    assert "fellowship" in query


def test_build_query_is_empty_for_an_empty_parse():
    assert build_query(ParsedQuery()) == ""


# --- transport --------------------------------------------------------


def test_search_raw_sends_the_query_and_parses_hits(monkeypatch):
    seen = {}

    def fake_request(payload):
        seen.update(payload)
        return {
            "results": [
                {"title": "Postdoc", "url": "https://example.edu/1", "content": "x", "score": 0.9}
            ]
        }

    monkeypatch.setattr("app.web_jobs._request", fake_request)
    hits = search_raw("string theory postdoctoral position")

    assert seen["query"] == "string theory postdoctoral position"
    assert seen["include_domains_mode"] == "prefer"
    assert "indeed.com" in seen["exclude_domains"]
    assert [h.url for h in hits] == ["https://example.edu/1"]


def test_search_raw_skips_unusable_hits(monkeypatch):
    """A result with no URL can't be linked to, but shouldn't lose the batch."""
    monkeypatch.setattr(
        "app.web_jobs._request",
        lambda payload: {"results": [{"title": "No link"}, {"url": "https://example.edu/2"}]},
    )

    assert [h.url for h in search_raw("q")] == ["https://example.edu/2"]


# --- extraction and filtering -----------------------------------------


def test_extract_maps_a_hit_to_a_web_badged_job():
    jobs = extract_jobs(
        [hit(url="https://example.edu/jobs/1")],
        ParsedQuery(subfield="string theory", ranks=["POSTDOC"]),
        provider=provider_returning(entry()),
        today=TODAY,
    )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "web"
    assert job.record_id.startswith("web:")
    assert job.title == "Postdoctoral Researcher"
    assert job.institution == "Example University"
    assert job.link == "https://example.edu/jobs/1"
    assert job.recent_papers == []


def test_known_platform_and_university_links_get_the_right_apply_label():
    jobs = extract_jobs(
        [hit(url="https://academicjobsonline.org/ajo/jobs/1"), hit(url="https://phys.example.edu/j")],
        ParsedQuery(ranks=["POSTDOC"]),
        provider=provider_returning(entry(0, title="Postdoc A"), entry(1, title="Postdoc B")),
        today=TODAY,
    )

    assert jobs[0].apply_via == "AcademicJobsOnline"
    assert jobs[1].apply_via == "Institute website (phys.example.edu)"


def test_pages_that_are_not_job_postings_are_dropped():
    jobs = extract_jobs(
        [hit()],
        ParsedQuery(),
        provider=provider_returning(entry(is_job_posting=False)),
        today=TODAY,
    )

    assert jobs == []


def test_unrelated_and_low_confidence_results_are_dropped():
    jobs = extract_jobs(
        [hit(), hit(url="https://example.edu/2")],
        ParsedQuery(),
        provider=provider_returning(entry(0, relevance="unrelated"), entry(1, confidence="low")),
        today=TODAY,
    )

    assert jobs == []


def test_a_broader_field_match_is_kept_but_labelled():
    """A niche subfield is rarely named in an ad - a parent-field posting
    is a real result, provided the row says that's what it is.
    """
    jobs = extract_jobs(
        [hit()],
        ParsedQuery(subfield="celestial holography", ranks=["POSTDOC"]),
        provider=provider_returning(entry(relevance="related")),
        today=TODAY,
    )

    assert len(jobs) == 1
    assert jobs[0].match_note == "broader field"


def test_a_direct_match_carries_no_qualifier():
    jobs = extract_jobs(
        [hit()], ParsedQuery(), provider=provider_returning(entry(relevance="direct")), today=TODAY
    )

    assert jobs[0].match_note is None


def test_an_unrecognised_relevance_value_is_not_trusted():
    """The model's output is untrusted: anything outside the known set
    must not sneak through as a result.
    """
    jobs = extract_jobs(
        [hit()], ParsedQuery(), provider=provider_returning(entry(relevance="maybe")), today=TODAY
    )

    assert jobs == []


def test_a_rank_the_user_did_not_ask_for_is_dropped():
    jobs = extract_jobs(
        [hit()],
        ParsedQuery(ranks=["POSTDOC"]),
        provider=provider_returning(entry(rank="PHD")),
        today=TODAY,
    )

    assert jobs == []


def test_an_unknown_rank_is_kept_because_snippets_often_omit_it():
    jobs = extract_jobs(
        [hit()],
        ParsedQuery(ranks=["POSTDOC"]),
        provider=provider_returning(entry(rank=None)),
        today=TODAY,
    )

    assert len(jobs) == 1
    assert jobs[0].tags == []


def test_a_posting_whose_deadline_has_passed_is_dropped():
    passed = (TODAY - timedelta(days=1)).isoformat()
    jobs = extract_jobs(
        [hit()], ParsedQuery(), provider=provider_returning(entry(deadline=passed)), today=TODAY
    )

    assert jobs == []


def test_a_future_deadline_is_kept_and_shown():
    upcoming = (TODAY + timedelta(days=30)).isoformat()
    jobs = extract_jobs(
        [hit()], ParsedQuery(), provider=provider_returning(entry(deadline=upcoming)), today=TODAY
    )

    assert jobs[0].deadline == upcoming


def test_an_unparseable_deadline_is_dropped_but_the_job_is_kept():
    jobs = extract_jobs(
        [hit()], ParsedQuery(), provider=provider_returning(entry(deadline="rolling")), today=TODAY
    )

    assert len(jobs) == 1
    assert jobs[0].deadline is None


def test_a_malformed_entry_does_not_lose_the_rest_of_the_batch():
    """The batched call's main risk: one bad element costs that hit only."""
    provider = FakeProvider(
        response=json.dumps({"jobs": [{"index": "not an int"}, entry(1)]})
    )
    jobs = extract_jobs(
        [hit(), hit(url="https://example.edu/2")], ParsedQuery(), provider=provider, today=TODAY
    )

    assert len(jobs) == 1
    assert jobs[0].link == "https://example.edu/2"


def test_an_out_of_range_index_is_ignored():
    jobs = extract_jobs(
        [hit()], ParsedQuery(), provider=provider_returning(entry(index=7)), today=TODAY
    )

    assert jobs == []


def test_non_http_links_are_dropped():
    jobs = extract_jobs(
        [WebHit(url="javascript:alert(1)", title="x")],
        ParsedQuery(),
        provider=provider_returning(entry()),
        today=TODAY,
    )

    assert jobs == []


def test_results_are_capped():
    hits = [hit(url=f"https://example.edu/{i}") for i in range(MAX_WEB_JOBS + 3)]
    provider = provider_returning(
        *(entry(i, title=f"Postdoc {i}") for i in range(len(hits)))
    )

    assert len(extract_jobs(hits, ParsedQuery(), provider=provider, today=TODAY)) == MAX_WEB_JOBS


def test_one_position_mirrored_at_two_urls_is_shown_once():
    """The same job is routinely carried by the group page, a job board and
    Inspire at once - the URL hash can't see they're the same posting.
    """
    jobs = extract_jobs(
        [hit(url="https://group.example.edu/jobs"), hit(url="https://jobs.ac.uk/job/X")],
        ParsedQuery(),
        provider=provider_returning(
            entry(0, title="Postdoc in Holography", institution="Edinburgh"),
            entry(1, title="postdoc in holography  ", institution="edinburgh"),
        ),
        today=TODAY,
    )

    assert len(jobs) == 1
    assert jobs[0].link == "https://group.example.edu/jobs"


def test_same_generic_title_at_unknown_employers_is_not_merged():
    """Two different jobs can share a title like "Postdoctoral Researcher";
    without a known employer they must not be collapsed into one.
    """
    jobs = extract_jobs(
        [hit(url="https://a.example.edu/1"), hit(url="https://b.example.edu/2")],
        ParsedQuery(),
        provider=provider_returning(
            entry(0, title="Postdoctoral Researcher", institution=None),
            entry(1, title="Postdoctoral Researcher", institution=None),
        ),
        today=TODAY,
    )

    assert len(jobs) == 2


def test_a_posting_found_on_inspire_keeps_inspire_provenance():
    """Badging an inspirehep.net link "not verified against Inspire" would
    contradict itself - it's an Inspire posting our jobs-API query missed.
    """
    jobs = extract_jobs(
        [hit(url="https://inspirehep.net/jobs/2844516")],
        ParsedQuery(),
        provider=provider_returning(entry()),
        today=TODAY,
    )

    assert jobs[0].source == "inspire"


def test_no_hits_means_no_llm_call():
    provider = FakeProvider()
    assert extract_jobs([], ParsedQuery(), provider=provider, today=TODAY) == []
    assert provider.calls == 0


# --- orchestration: every failure degrades to "no web results" ---------


def test_fallback_reports_a_search_failure_without_raising(monkeypatch):
    def boom(payload):
        raise WebSearchError("Tavily rate limit exceeded.")

    monkeypatch.setattr("app.web_jobs._request", boom)
    jobs, error = fallback_results(ParsedQuery(subfield="string theory"))

    assert jobs == []
    assert "rate limit" in error


def test_fallback_reports_an_llm_failure_without_raising(monkeypatch):
    monkeypatch.setattr(
        "app.web_jobs._request",
        lambda payload: {"results": [{"url": "https://example.edu/1", "title": "Postdoc"}]},
    )
    jobs, error = fallback_results(
        ParsedQuery(subfield="string theory"), provider=FakeProvider(error="all providers down")
    )

    assert jobs == []
    assert "Could not read web results" in error


def test_fallback_survives_unparseable_model_output(monkeypatch):
    monkeypatch.setattr(
        "app.web_jobs._request",
        lambda payload: {"results": [{"url": "https://example.edu/1", "title": "Postdoc"}]},
    )
    jobs, error = fallback_results(
        ParsedQuery(subfield="string theory"), provider=FakeProvider(response="not json")
    )

    assert jobs == []
    assert error is not None


def test_fallback_skips_the_search_entirely_for_an_empty_query(monkeypatch):
    def fail(payload):
        raise AssertionError("should not search with no query terms")

    monkeypatch.setattr("app.web_jobs._request", fail)

    assert fallback_results(ParsedQuery()) == ([], None)


def test_fallback_returns_jobs_and_no_error_on_the_happy_path(monkeypatch):
    monkeypatch.setattr(
        "app.web_jobs._request",
        lambda payload: {"results": [{"url": "https://example.edu/1", "title": "Postdoc"}]},
    )
    jobs, error = fallback_results(
        ParsedQuery(subfield="string theory", ranks=["POSTDOC"]),
        provider=provider_returning(entry()),
    )

    assert error is None
    assert [job.source for job in jobs] == ["web"]


# --- listing pages are rejected without asking the model ---------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.quantiki.org/tags/quantum-error-correction",
        "https://academicpositions.com/jobs/position/post-doc/field/cosmology",
        "https://academicjobsonline.org/ajo/HEP",
        "https://academicjobsonline.org/ajo/physics/Quantum%20Optics",
    ],
)
def test_browse_pages_are_dropped_before_the_model_sees_them(url):
    """A board's topic page shows its first job's title, so the model waves
    it through on some runs. Filtering in code makes it deterministic.
    """
    provider = FakeProvider()
    assert extract_jobs([hit(url=url)], ParsedQuery(), provider=provider, today=TODAY) == []
    assert provider.calls == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://academicjobsonline.org/ajo/Harvard/Physics/28110",
        "https://www.jobs.ac.uk/job/DDD188/postdoctoral-research-associate",
        "https://www.icehap.chiba-u.jp/en/about/recruit.html",
        "https://inspirehep.net/jobs/2844516",
    ],
)
def test_real_postings_are_not_mistaken_for_browse_pages(url):
    jobs = extract_jobs(
        [hit(url=url)], ParsedQuery(), provider=provider_returning(entry()), today=TODAY
    )

    assert len(jobs) == 1

"""When _search is allowed to cache an empty result.

An empty result is the least stable thing to remember. A query answered
empty two minutes before the web fallback shipped was served that empty
answer for 24 hours, so it never reached the code that would have found
the posting. These tests pin down exactly when an empty result may be
stored.
"""

import app.main as main
from app.formatter import FormattedJob
from app.inspire_client import JobSearchResult, RawJob
from app.query_rewriter import ParsedQuery
from fastapi.testclient import TestClient

client = TestClient(main.app)

QUERY = {"query": "postdoc in celestial field theory"}


def web_row() -> FormattedJob:
    return FormattedJob(
        record_id="web:1", title="Postdoc", institution="Somewhere", source="web"
    )


def run(monkeypatch, *, inspire_jobs=(), fallback_enabled=False, fallback=None):
    """Drive one uncached search and report whether anything was cached."""
    stored = []

    monkeypatch.setattr(main.cache, "lookup", lambda query: None)
    monkeypatch.setattr(
        main.cache, "store", lambda *args, **kwargs: stored.append(args)
    )
    monkeypatch.setattr(
        main,
        "analyze_query",
        lambda text: ParsedQuery(on_topic=True, subfield="celestial field theory", ranks=["POSTDOC"]),
    )
    monkeypatch.setattr(
        main,
        "search_jobs",
        lambda params: JobSearchResult(jobs=list(inspire_jobs), total=len(inspire_jobs)),
    )
    monkeypatch.setattr(main, "log_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "get_recent_papers", lambda names: {})
    monkeypatch.setattr(main, "is_enabled", lambda: fallback_enabled)
    monkeypatch.setattr(main, "fallback_results", fallback or (lambda parsed: ([], None)))

    response = client.post("/jobs/search", json=QUERY)
    assert response.status_code == 200
    return bool(stored), response.json()


def test_empty_result_is_not_cached_when_the_fallback_is_off(monkeypatch):
    """The case that bit us: no fallback yet, so "nothing" is only true of
    this build and mustn't outlive it.
    """
    cached, body = run(monkeypatch, fallback_enabled=False)

    assert body["results"] == []
    assert cached is False


def test_empty_result_is_not_cached_when_the_fallback_failed(monkeypatch):
    """A transient Tavily or LLM outage must not be frozen in for a day."""
    cached, _ = run(
        monkeypatch,
        fallback_enabled=True,
        fallback=lambda parsed: ([], "Tavily rate limit exceeded."),
    )

    assert cached is False


def test_empty_result_is_cached_when_the_fallback_ran_and_found_nothing(monkeypatch):
    """A real "nothing anywhere" is worth remembering: repeating it would
    spend a Tavily credit on every identical query.
    """
    cached, body = run(
        monkeypatch, fallback_enabled=True, fallback=lambda parsed: ([], None)
    )

    assert body["web_searched"] is True
    assert cached is True


def test_a_result_with_rows_is_cached(monkeypatch):
    cached, body = run(
        monkeypatch,
        inspire_jobs=[RawJob(record_id="1", position="Postdoc")],
    )

    assert len(body["results"]) == 1
    assert cached is True


def test_web_rows_are_cached(monkeypatch):
    cached, body = run(
        monkeypatch,
        fallback_enabled=True,
        fallback=lambda parsed: ([web_row()], None),
    )

    assert [row["source"] for row in body["results"]] == ["web"]
    assert cached is True


# --- Cached entries holding jobs that have since expired ---------------------

from datetime import datetime, timezone  # noqa: E402

from app.cache import CacheEntry  # noqa: E402
from app.inspire_client import JobQueryParams  # noqa: E402


def cached_entry(*deadlines, total=None):
    return CacheEntry(
        id=1,
        query_text="postdoc in celestial field theory",
        params=JobQueryParams(),
        result=[
            FormattedJob(record_id=str(i), title=f"Job {i}", institution="U", deadline=d)
            for i, d in enumerate(deadlines)
        ],
        total_matches=total,
        created_at=datetime.now(timezone.utc),
        similarity=0.95,
    )


def serve_from_cache(monkeypatch, entry):
    """Search with the cache returning `entry`; report whether it went live."""
    went_live = []
    monkeypatch.setattr(main.cache, "lookup", lambda query: entry)
    monkeypatch.setattr(main.cache, "store", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "is_expired", lambda deadline: deadline == "2000-01-01")

    def live(text):
        went_live.append(text)
        return ParsedQuery(on_topic=True, ranks=["POSTDOC"])

    monkeypatch.setattr(main, "analyze_query", live)
    monkeypatch.setattr(main, "search_jobs", lambda params: JobSearchResult(jobs=[], total=0))
    monkeypatch.setattr(main, "log_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "get_recent_papers", lambda names: {})
    monkeypatch.setattr(main, "is_enabled", lambda: False)
    body = client.post("/jobs/search", json=QUERY).json()
    return body, bool(went_live)


def test_a_cached_job_whose_deadline_has_since_passed_is_not_served(monkeypatch):
    body, went_live = serve_from_cache(
        monkeypatch, cached_entry("2000-01-01", "2099-01-01", None, total=40)
    )

    assert went_live is False
    assert body["cached"] is True
    assert [job["title"] for job in body["results"]] == ["Job 1", "Job 2"]
    # The stored total counted the expired job too.
    assert body["total_matches"] == 39


def test_a_cache_entry_whose_jobs_have_all_expired_searches_live(monkeypatch):
    """Its "0 results" would be the cache talking, not Inspire."""
    body, went_live = serve_from_cache(monkeypatch, cached_entry("2000-01-01", "2000-01-01"))

    assert went_live is True
    assert body["cached"] is False

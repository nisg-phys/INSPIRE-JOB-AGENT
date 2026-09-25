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

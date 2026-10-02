"""Per-client rate limiting on /jobs/search.

The limiter is driven with explicit timestamps so the windows can be
tested without sleeping.
"""

import logging

import app.main as main
from app.query_rewriter import ParsedQuery
from app.rate_limit import RateLimiter, search_limiter
from fastapi.testclient import TestClient

client = TestClient(main.app)


def test_requests_under_the_limit_are_allowed():
    limiter = RateLimiter([(3, 60.0)])

    assert [limiter.check("a", now=t) for t in (0, 1, 2)] == [None, None, None]


def test_the_request_over_the_limit_is_refused_until_the_oldest_ages_out():
    limiter = RateLimiter([(3, 60.0)])
    for t in (0, 10, 20):
        limiter.check("a", now=t)

    assert limiter.check("a", now=30) == 30.0
    assert limiter.check("a", now=60) is None


def test_clients_are_counted_separately():
    limiter = RateLimiter([(1, 60.0)])
    limiter.check("a", now=0)

    assert limiter.check("a", now=1) is not None
    assert limiter.check("b", now=1) is None


def test_the_longer_window_still_applies_after_the_short_one_clears():
    limiter = RateLimiter([(2, 60.0), (3, 3600.0)])
    for t in (0, 1, 100):
        assert limiter.check("a", now=t) is None

    # The minute window is clear again, but this would be the 4th this hour.
    assert limiter.check("a", now=200) == 3400.0


def test_a_refused_request_does_not_push_the_retry_further_out():
    limiter = RateLimiter([(1, 60.0)])
    limiter.check("a", now=0)
    for t in range(1, 50):
        limiter.check("a", now=t)

    assert limiter.check("a", now=60) is None


def test_a_zero_limit_turns_the_window_off():
    limiter = RateLimiter([(0, 60.0)])

    assert all(limiter.check("a", now=0) is None for _ in range(100))


def test_quiet_clients_are_dropped_from_the_table():
    limiter = RateLimiter([(5, 60.0)])
    limiter.check("gone", now=0)
    limiter._sweep(now=61)

    assert "gone" not in limiter._hits


def stub_search(monkeypatch):
    monkeypatch.setattr(main.cache, "lookup", lambda query: None)
    monkeypatch.setattr(main.cache, "store", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main, "analyze_query", lambda text: ParsedQuery(on_topic=False)
    )


def test_the_endpoint_answers_429_with_retry_after(monkeypatch):
    stub_search(monkeypatch)
    monkeypatch.setattr(search_limiter, "windows", [(2, 60.0)])
    headers = {"x-forwarded-for": "203.0.113.7"}

    statuses = [
        client.post("/jobs/search", json={"query": "postdoc in cosmology"}, headers=headers).status_code
        for _ in range(3)
    ]
    refused = client.post("/jobs/search", json={"query": "postdoc in cosmology"}, headers=headers)

    assert statuses == [200, 200, 429]
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0
    assert "Too many searches" in refused.json()["detail"]


def test_the_endpoint_limits_by_the_forwarded_client_not_the_proxy(monkeypatch):
    """Behind Firebase Hosting every request has the same TCP peer, so
    keying on it would make one user's limit everyone's.
    """
    stub_search(monkeypatch)
    monkeypatch.setattr(search_limiter, "windows", [(1, 60.0)])

    first = client.post(
        "/jobs/search", json={"query": "postdoc"}, headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    )
    other = client.post(
        "/jobs/search", json={"query": "postdoc"}, headers={"x-forwarded-for": "198.51.100.2, 10.0.0.1"}
    )

    assert (first.status_code, other.status_code) == (200, 200)


def test_request_completed_records_whether_search_was_cached(monkeypatch, caplog):
    """Search latency is bimodal (cache hit vs miss), so the request log
    line has to say which, or its percentiles can't be split.
    """
    stub_search(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.request"):
        client.post("/jobs/search", json={"query": "postdoc in cosmology"})

    completed = [r for r in caplog.records if r.getMessage() == "request_completed"]
    assert [r.cached for r in completed] == [False]

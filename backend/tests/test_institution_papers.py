from app.institution_papers import Paper, select_relevant_papers


def _paper(record_id: str, categories: list[str]) -> Paper:
    return Paper(record_id=record_id, title=f"Paper {record_id}", categories=categories)


def test_prefers_papers_matching_query_category():
    papers = [
        _paper("1", ["Quantum Physics"]),
        _paper("2", ["Gravitation and Cosmology"]),
        _paper("3", ["Theory-HEP"]),
    ]

    result = select_relevant_papers(papers, categories=["Gravitation and Cosmology"])

    assert [p.record_id for p in result] == ["2"]


def test_falls_back_to_most_recent_when_no_category_matches():
    """Regression test: before this, recent_papers showed an institution's
    most recent output regardless of subfield - e.g. condensed matter
    papers for a job in an unrelated field. Now it only falls back to
    "most recent" (the stored order) when nothing actually matches.
    """
    papers = [_paper("1", ["Condensed Matter"]), _paper("2", ["Instrumentation"])]

    result = select_relevant_papers(papers, categories=["Theory-HEP"])

    assert [p.record_id for p in result] == ["1", "2"]


def test_no_categories_requested_returns_most_recent():
    papers = [_paper("1", ["Theory-HEP"]), _paper("2", ["Astrophysics"])]

    result = select_relevant_papers(papers, categories=[])

    assert [p.record_id for p in result] == ["1", "2"]


def test_respects_limit():
    papers = [_paper(str(i), ["Theory-HEP"]) for i in range(10)]

    result = select_relevant_papers(papers, categories=["Theory-HEP"], limit=3)

    assert len(result) == 3


def test_paper_matching_any_of_several_query_categories_counts():
    papers = [_paper("1", ["Astrophysics"])]

    result = select_relevant_papers(papers, categories=["Gravitation and Cosmology", "Astrophysics"])

    assert [p.record_id for p in result] == ["1"]


# --- fetch_and_store_many ---------------------------------------------------

import threading

import pytest

from app.inspire_literature_client import InspireAPIError
from app.institution_papers import fetch_and_store_many


def test_fetches_run_concurrently(monkeypatch):
    """Each fake fetch blocks on a barrier that only releases once ALL of them
    are running at the same time. A sequential loop would never get past the
    first one, so the barrier times out and the test fails - a deterministic
    check for concurrency, unlike asserting on wall-clock time.
    """
    names = ["A", "B", "C", "D", "E"]
    barrier = threading.Barrier(len(names), timeout=5)

    def fake_fetch(name, institution_id):
        barrier.wait()
        return [_paper(name, [])]

    monkeypatch.setattr("app.institution_papers.fetch_and_store", fake_fetch)

    papers, errors = fetch_and_store_many({n: None for n in names})

    assert errors == {}
    assert set(papers) == set(names)


def test_passes_each_institution_its_own_id(monkeypatch):
    seen = {}

    def fake_fetch(name, institution_id):
        seen[name] = institution_id
        return []

    monkeypatch.setattr("app.institution_papers.fetch_and_store", fake_fetch)

    fetch_and_store_many({"Kentucky U.": "904048", "Wolfram": None})

    assert seen == {"Kentucky U.": "904048", "Wolfram": None}


def test_one_inspire_failure_does_not_abort_the_others(monkeypatch):
    def fake_fetch(name, institution_id):
        if name == "Bad":
            raise InspireAPIError("Rate limit exceeded.")
        return [_paper(name, [])]

    monkeypatch.setattr("app.institution_papers.fetch_and_store", fake_fetch)

    papers, errors = fetch_and_store_many({"Good": None, "Bad": None, "AlsoGood": None})

    assert set(papers) == {"Good", "AlsoGood"}
    assert errors == {"Bad": "Rate limit exceeded."}


def test_non_inspire_errors_still_propagate(monkeypatch):
    """Only INSPIRE failures are degraded gracefully; anything else (e.g. a
    database error) must still surface to the endpoint's 503 handler rather
    than being silently swallowed."""

    def fake_fetch(name, institution_id):
        raise RuntimeError("db is down")

    monkeypatch.setattr("app.institution_papers.fetch_and_store", fake_fetch)

    with pytest.raises(RuntimeError, match="db is down"):
        fetch_and_store_many({"A": None})


def test_empty_input_returns_empty():
    assert fetch_and_store_many({}) == ({}, {})

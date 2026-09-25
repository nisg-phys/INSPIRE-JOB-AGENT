"""Tests for /institutions/papers, the deferred half of the search path.

Jobs are returned as soon as Inspire answers; papers for institutions
we've never enriched are fetched by this endpoint afterwards, because
that call took 6-16s and used to block the jobs behind it.
"""

import app.main as main
from app.institution_papers import Paper
from fastapi.testclient import TestClient

client = TestClient(main.app)


def paper(record_id: str, categories: list[str] | None = None) -> Paper:
    return Paper(record_id=record_id, title=f"Paper {record_id}", categories=categories or [])


def post(**body):
    return client.post("/institutions/papers", json=body).json()["papers"]


def test_unknown_institution_names_are_never_searched_for(monkeypatch):
    """The names arrive from the client and end up inside an Inspire query
    (aff "<name>"), so only ones logged from a real posting may be used.
    """
    fetched = {}

    monkeypatch.setattr(main, "known_institutions", lambda names: {"CERN": "902725"})
    monkeypatch.setattr(main, "get_recent_papers", lambda names: {})
    monkeypatch.setattr(
        main, "fetch_and_store_many", lambda wanted: (fetched.update(wanted) or ({}, {}))
    )

    post(institutions=["CERN", 'Evil" OR 1=1', "Nowhere U."])

    assert list(fetched) == ["CERN"]


def test_stored_papers_are_returned_without_a_live_fetch(monkeypatch):
    """The common case: the worker already enriched the institution."""
    calls = []

    monkeypatch.setattr(main, "known_institutions", lambda names: {"CERN": "902725"})
    monkeypatch.setattr(main, "get_recent_papers", lambda names: {"CERN": [paper("1")]})
    monkeypatch.setattr(
        main, "fetch_and_store_many", lambda wanted: (calls.append(wanted) or ({}, {}))
    )

    result = post(institutions=["CERN"])

    assert [p["record_id"] for p in result["CERN"]] == ["1"]
    assert calls == [{}]  # nothing missing, so nothing fetched


def test_only_institutions_without_stored_papers_are_fetched(monkeypatch):
    fetched = {}

    monkeypatch.setattr(
        main, "known_institutions", lambda names: {"CERN": "902725", "DESY": None}
    )
    monkeypatch.setattr(main, "get_recent_papers", lambda names: {"CERN": [paper("1")]})
    monkeypatch.setattr(
        main,
        "fetch_and_store_many",
        lambda wanted: (fetched.update(wanted) or ({"DESY": [paper("2")]}, {})),
    )

    result = post(institutions=["CERN", "DESY"])

    assert list(fetched) == ["DESY"]
    assert set(result) == {"CERN", "DESY"}


def test_papers_are_ranked_by_the_query_categories(monkeypatch):
    """Deferred papers get the same subfield relevance as inline ones."""
    monkeypatch.setattr(main, "known_institutions", lambda names: {"CERN": None})
    monkeypatch.setattr(
        main,
        "get_recent_papers",
        lambda names: {"CERN": [paper("off", ["Astrophysics"]), paper("on", ["Theory-HEP"])]},
    )
    monkeypatch.setattr(main, "fetch_and_store_many", lambda wanted: ({}, {}))

    result = post(institutions=["CERN"], categories=["Theory-HEP"])

    assert [p["record_id"] for p in result["CERN"]] == ["on"]


def test_an_invented_category_is_ignored(monkeypatch):
    """Categories come from the client too, so they're checked against
    Inspire's actual taxonomy rather than used as given.
    """
    monkeypatch.setattr(main, "known_institutions", lambda names: {"CERN": None})
    monkeypatch.setattr(
        main, "get_recent_papers", lambda names: {"CERN": [paper("a"), paper("b")]}
    )
    monkeypatch.setattr(main, "fetch_and_store_many", lambda wanted: ({}, {}))

    result = post(institutions=["CERN"], categories=["Not A Real Category"])

    assert [p["record_id"] for p in result["CERN"]] == ["a", "b"]


def test_a_failed_fetch_returns_what_it_has_rather_than_an_error(monkeypatch):
    """Papers are supplementary - a dead Inspire mustn't turn a rendered
    table into an error.
    """
    monkeypatch.setattr(
        main, "known_institutions", lambda names: {"CERN": None, "DESY": None}
    )
    monkeypatch.setattr(main, "get_recent_papers", lambda names: {"CERN": [paper("1")]})
    monkeypatch.setattr(
        main, "fetch_and_store_many", lambda wanted: ({}, {"DESY": "Inspire timed out."})
    )

    response = client.post("/institutions/papers", json={"institutions": ["CERN", "DESY"]})

    assert response.status_code == 200
    assert set(response.json()["papers"]) == {"CERN"}


def test_an_empty_request_does_no_work(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("should not touch the database for an empty request")

    monkeypatch.setattr(main, "known_institutions", fail)

    assert post(institutions=[]) == {}


def test_the_number_of_institutions_is_capped(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        main, "known_institutions", lambda names: (seen.update({"n": len(names)}) or {})
    )

    post(institutions=[f"Institution {i}" for i in range(200)])

    assert seen["n"] == main.MAX_DEFERRED_INSTITUTIONS

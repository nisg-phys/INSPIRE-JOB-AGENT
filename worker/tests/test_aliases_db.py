"""Institution-name resolution, against a real Postgres.

Resolution is SQL (normalize_institution_name / resolve_institution, from
the backend's add_institution_aliases migration), so it is only worth
testing against a database. Skipped unless TEST_DATABASE_URL points at a
disposable database that has had `alembic upgrade head` run against it -
every test truncates the tables it uses.

    cd backend && DATABASE_URL=$TEST_DATABASE_URL alembic upgrade head
    cd worker && TEST_DATABASE_URL=... pytest
"""

import json
import os

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from sqlalchemy import text  # noqa: E402

from worker import aliases  # noqa: E402
from worker.db import get_engine  # noqa: E402
from worker.discovery import discover_institutions  # noqa: E402
from worker.inspire_literature_client import InstitutionRecord  # noqa: E402


@pytest.fixture(autouse=True)
def clean_tables():
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE jobs_raw, institution_aliases, unresolved_institutions, "
                "institution_papers"
            )
        )
    yield


def log_posting(record_id, institutions):
    """A jobs_raw row: institutions maps name -> linked id, or None for free text."""
    payload = {
        "record_id": record_id,
        "position": "Postdoc",
        "institutions": list(institutions),
        "institution_ids": {n: i for n, i in institutions.items() if i},
    }
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs_raw (record_id, position, institutions, payload) "
                "VALUES (:r, 'Postdoc', :names, CAST(:payload AS jsonb))"
            ),
            {"r": record_id, "names": list(institutions), "payload": json.dumps(payload)},
        )


def resolve(name):
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT resolve_institution(:n)"), {"n": name}).scalar()


def unresolved():
    with get_engine().connect() as conn:
        return dict(conn.execute(text("SELECT name, seen_count FROM unresolved_institutions")).all())


def test_a_free_text_spelling_resolves_through_another_posting_that_linked_it():
    """The original gap: one posting links "Kentucky U." to its record,
    another writes the same name as free text with no link.
    """
    log_posting("1", {"Kentucky U.": "904048"})
    log_posting("2", {"Kentucky U": None})
    aliases.sync_posting_aliases()

    assert resolve("Kentucky U") == "904048"
    assert resolve("  KENTUCKY   U. ") == "904048"


def test_word_order_is_not_guessed():
    log_posting("1", {"Kentucky U.": "904048"})
    aliases.sync_posting_aliases()

    assert resolve("U. Kentucky") is None


def test_a_spelling_linked_to_two_records_is_ambiguous():
    log_posting("1", {"Imperial": "1"})
    log_posting("2", {"Imperial": "2"})
    aliases.sync_posting_aliases()

    assert resolve("Imperial") is None


def test_a_curated_alias_settles_an_ambiguous_spelling(monkeypatch):
    log_posting("1", {"Imperial": "1"})
    log_posting("2", {"Imperial": "2"})
    aliases.sync_posting_aliases()
    monkeypatch.setattr(
        aliases, "institution_record", lambda i: InstitutionRecord(institution_id=i, legacy_icn="Imperial Coll., London")
    )
    aliases._add_curated("Imperial", "2")

    assert resolve("Imperial") == "2"


def test_record_spellings_are_added_once_per_institution(monkeypatch):
    calls = []

    def fake_record(institution_id):
        calls.append(institution_id)
        return InstitutionRecord(
            institution_id=institution_id, legacy_icn="CERN",
            name_variants=["Centre Européen de Recherches Nucléaires"],
            own_names=["European Organization for Nuclear Research"],
        )

    monkeypatch.setattr(aliases, "institution_record", fake_record)
    log_posting("1", {"CERN": "902725"})
    aliases.sync_posting_aliases()

    aliases.sync_record_aliases()
    aliases.sync_record_aliases()

    assert calls == ["902725"]
    assert resolve("European Organization for Nuclear Research") == "902725"
    assert resolve("centre européen de recherches nucléaires") == "902725"


def test_discovery_fetches_a_resolved_free_text_name_by_id():
    log_posting("1", {"Kentucky U.": "904048"})
    log_posting("2", {"Kentucky U": None})
    aliases.sync_posting_aliases()

    found = {ref.name: ref.institution_id for ref in discover_institutions()}

    assert found == {"Kentucky U.": "904048", "Kentucky U": "904048"}


def test_discovery_refetches_a_name_once_an_alias_resolves_it(monkeypatch):
    """A name fetched by phrase (and found nothing) is re-selected as soon
    as a curated alias gives it an id - adding the alias is all it takes.
    """
    log_posting("1", {"U. Kentucky": None})
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO institution_papers (institution, institution_id, papers, last_updated) "
                "VALUES ('U. Kentucky', NULL, '[]', now())"
            )
        )
    assert discover_institutions() == []

    monkeypatch.setattr(
        aliases, "institution_record", lambda i: InstitutionRecord(institution_id=i, legacy_icn="Kentucky U.")
    )
    aliases._add_curated("U. Kentucky", "904048")

    assert [(r.name, r.institution_id) for r in discover_institutions()] == [("U. Kentucky", "904048")]


def test_a_free_text_name_that_found_nothing_goes_on_the_worklist():
    aliases.record_enrichment_outcome("U. Kentucky", None, 0)
    aliases.record_enrichment_outcome("U. Kentucky", None, 0)

    assert unresolved() == {"U. Kentucky": 2}


def test_a_name_comes_off_the_worklist_once_it_resolves_or_finds_papers():
    aliases.record_enrichment_outcome("U. Kentucky", None, 0)
    aliases.record_enrichment_outcome("Somewhere U.", None, 0)

    aliases.record_enrichment_outcome("U. Kentucky", "904048", 0)
    aliases.record_enrichment_outcome("Somewhere U.", None, 5)

    assert unresolved() == {}

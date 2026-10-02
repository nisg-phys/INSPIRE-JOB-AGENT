"""known_institutions against a real Postgres: the papers allowlist.

Skipped unless TEST_DATABASE_URL points at a disposable, migrated database
(see worker/tests/test_aliases_db.py). Uses its own engine for that URL
rather than the app's, so a DATABASE_URL in the environment can never be
the database these truncate.
"""

import json
import os

import pytest
from sqlalchemy import create_engine, text

import app.jobs_log as jobs_log

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine(TEST_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE jobs_raw, institution_aliases"))
    monkeypatch.setattr(jobs_log, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


def insert(engine, sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def log_posting(engine, record_id, institutions):
    payload = {"institution_ids": {n: i for n, i in institutions.items() if i}}
    insert(
        engine,
        "INSERT INTO jobs_raw (record_id, position, institutions, payload) "
        "VALUES (:r, 'Postdoc', :names, CAST(:payload AS jsonb))",
        r=record_id, names=list(institutions), payload=json.dumps(payload),
    )


def test_a_free_text_name_gets_the_id_its_alias_resolves_to(engine):
    log_posting(engine, "1", {"U. Kentucky": None})
    insert(
        engine,
        "INSERT INTO institution_aliases (alias, institution_id, name, source) "
        "VALUES (normalize_institution_name('U. Kentucky'), '904048', 'U. Kentucky', 'curated')",
    )

    assert jobs_log.known_institutions(["U. Kentucky"]) == {"U. Kentucky": "904048"}


def test_a_linked_id_wins_over_an_alias(engine):
    log_posting(engine, "1", {"Kentucky U.": "904048"})
    insert(
        engine,
        "INSERT INTO institution_aliases (alias, institution_id, name, source) "
        "VALUES (normalize_institution_name('Kentucky U.'), '1', 'Kentucky U.', 'curated')",
    )

    assert jobs_log.known_institutions(["Kentucky U."]) == {"Kentucky U.": "904048"}


def test_an_alias_does_not_admit_a_name_no_posting_used(engine):
    """Still an allowlist: resolving ids must not let an arbitrary string
    through to an Inspire query just because an alias exists for it.
    """
    insert(
        engine,
        "INSERT INTO institution_aliases (alias, institution_id, name, source) "
        "VALUES (normalize_institution_name('U. Kentucky'), '904048', 'U. Kentucky', 'curated')",
    )

    assert jobs_log.known_institutions(["U. Kentucky"]) == {}

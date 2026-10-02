"""cache.lookup only serves an entry that names the same career stages.

Embeddings barely register the stage - "phd in string theory" scores 0.77
against a cached "postdoc in string theory" - so similarity alone can't be
trusted to keep them apart. The database is faked: these tests are about
which of the nearest rows lookup accepts, not about pgvector.
"""

from datetime import datetime, timezone

import app.cache as cache


def row(query_text, similarity, title="Job"):
    return {
        "id": hash(query_text),
        "query_text": query_text,
        "params": {},
        "result": {"jobs": [{"record_id": "1", "title": title, "institution": "U"}], "total": 1},
        "created_at": datetime.now(timezone.utc),
        "similarity": similarity,
    }


class FakeEngine:
    """Returns the given rows, nearest first, as the real query would."""

    def __init__(self, rows):
        self.rows = rows

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params):
        self.limit = params["candidate_count"]
        return self

    def mappings(self):
        return self

    def all(self):
        return self.rows[: self.limit]


def lookup(monkeypatch, query, rows):
    monkeypatch.setattr(cache, "embed", lambda text: [0.0])
    monkeypatch.setattr(cache, "get_engine", lambda: FakeEngine(rows))
    return cache.lookup(query)


def test_a_close_entry_for_another_career_stage_is_not_served(monkeypatch):
    """Above the threshold on purpose: nothing guarantees a pair like this
    stays below it, which is the gap this check closes.
    """
    hit = lookup(monkeypatch, "phd in string theory", [row("postdoc in string theory", 0.85)])

    assert hit is None


def test_a_matching_stage_behind_a_mismatched_nearest_entry_is_served(monkeypatch):
    hit = lookup(
        monkeypatch,
        "phd in string theory",
        [
            row("postdoc in string theory", 0.9, title="Postdoc"),
            row("PhD positions in string theory", 0.85, title="PhD"),
        ],
    )

    assert hit is not None
    assert [job.title for job in hit.result] == ["PhD"]


def test_a_paraphrase_of_the_same_stage_is_served(monkeypatch):
    hit = lookup(monkeypatch, "post-docs in string thoery", [row("postdoc in string theory", 0.9)])

    assert hit is not None
    assert hit.query_text == "postdoc in string theory"


def test_a_matching_stage_below_the_threshold_is_not_served(monkeypatch):
    hit = lookup(monkeypatch, "postdoc in string theory", [row("postdoc in cosmology", 0.7)])

    assert hit is None

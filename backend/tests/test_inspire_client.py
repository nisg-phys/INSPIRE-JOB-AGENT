from app.inspire_client import JobQueryParams, RawJob, search_jobs


def _job(record_id: str, deadline: str | None = None) -> RawJob:
    return RawJob(record_id=record_id, position="Postdoc", deadline=deadline)


def _ids(params, rank):
    """Stand-in for the ids-only counting request."""
    return {f"{rank}-1", f"{rank}-2"}


def test_no_ranks_makes_a_single_unfiltered_request(monkeypatch):
    calls = []

    def fake_search(params, rank):
        calls.append(rank)
        return [_job("1")], 7

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)

    result = search_jobs(JobQueryParams(keywords="cosmology"))

    assert calls == [None]
    assert [j.record_id for j in result.jobs] == ["1"]
    assert result.total == 7


def test_multiple_ranks_make_one_request_per_rank_and_merge(monkeypatch):
    """Regression test: Inspire's rank filter only accepts one value per
    request (confirmed empirically - repeated params and comma-separated
    values both failed), so multi-rank queries (e.g. "faculty" ->
    JUNIOR+SENIOR) must fan out into separate requests and merge results.
    """
    calls = []

    def fake_search(params, rank):
        calls.append(rank)
        return [_job(f"{rank}-1"), _job(f"{rank}-2")], 2

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", _ids)

    result = search_jobs(JobQueryParams(keywords="cosmology", ranks=["JUNIOR", "SENIOR"]))

    assert sorted(calls) == ["JUNIOR", "SENIOR"]
    assert sorted(j.record_id for j in result.jobs) == [
        "JUNIOR-1", "JUNIOR-2", "SENIOR-1", "SENIOR-2"
    ]


def test_multi_rank_results_are_deduped_by_record_id(monkeypatch):
    """The same posting can genuinely satisfy more than one rank filter
    (e.g. a JUNIOR/SENIOR-listed opening) - it must only appear once."""

    def fake_search(params, rank):
        return [_job("shared"), _job(f"{rank}-only")], 2

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", _ids)

    result = search_jobs(JobQueryParams(keywords="cosmology", ranks=["JUNIOR", "SENIOR"]))

    ids = [j.record_id for j in result.jobs]
    assert ids.count("shared") == 1
    assert sorted(ids) == ["JUNIOR-only", "SENIOR-only", "shared"]


def test_multi_rank_results_respect_size_cap(monkeypatch):
    def fake_search(params, rank):
        return [_job(f"{rank}-{i}") for i in range(5)], 5

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", _ids)

    result = search_jobs(JobQueryParams(keywords="x", ranks=["JUNIOR", "SENIOR"], size=3))

    assert len(result.jobs) == 3


def _hit(institutions: list[dict]) -> dict:
    return {
        "metadata": {
            "control_number": 1,
            "position": "Postdoc",
            "institutions": institutions,
        }
    }


def test_institution_ids_are_extracted_from_record_refs(monkeypatch):
    """Regression test for the U. Kentucky zero-papers bug: the same
    institution is spelled differently across postings, so the linked
    record id (not the name) is what paper enrichment needs to search by.
    """
    payload = {
        "hits": {
            "hits": [
                _hit(
                    [
                        {
                            "value": "Kentucky U.",
                            "record": {"$ref": "https://labs.inspirehep.net/api/institutions/904048"},
                        },
                        {
                            "value": "Fermilab",
                            "record": {"$ref": "https://labs.inspirehep.net/api/institutions/902796"},
                        },
                    ]
                )
            ]
        }
    }
    monkeypatch.setattr("app.inspire_client._request", lambda endpoint, params: payload)

    [job] = search_jobs(JobQueryParams(keywords="x")).jobs

    assert job.institution_ids == {"Kentucky U.": "904048", "Fermilab": "902796"}


def test_unlinked_institution_has_no_id(monkeypatch):
    """Free-text institutions (no record ref) must simply be absent from
    institution_ids, so enrichment falls back to name search for them."""
    payload = {
        "hits": {
            "hits": [
                _hit(
                    [
                        {"value": "Wolfram Institute, Champaign"},
                        {
                            "value": "Kentucky U.",
                            "record": {"$ref": "https://labs.inspirehep.net/api/institutions/904048"},
                        },
                    ]
                )
            ]
        }
    }
    monkeypatch.setattr("app.inspire_client._request", lambda endpoint, params: payload)

    [job] = search_jobs(JobQueryParams(keywords="x")).jobs

    assert job.institutions == ["Wolfram Institute, Champaign", "Kentucky U."]
    assert job.institution_ids == {"Kentucky U.": "904048"}


def test_multi_rank_merge_is_resorted_by_deadline_before_truncating(monkeypatch):
    """Regression test for jobs being hidden by the fan-out.

    Each rank comes back sorted on its own, so concatenating and cutting at
    `size` gave every slot to the first rank. Live, a JUNIOR+SENIOR search
    filled all ten rows with JUNIOR and hid six SENIOR jobs closing sooner -
    one of them within the week.
    """

    def fake_search(params, rank):
        if rank == "JUNIOR":
            return [_job("j1", "2026-10-01"), _job("j2", "2026-10-15")], 2
        return [_job("s1", "2026-09-28"), _job("s2", "2026-09-30")], 2

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", _ids)

    result = search_jobs(
        JobQueryParams(keywords="faculty", ranks=["JUNIOR", "SENIOR"], size=2)
    )

    # The two soonest overall, not the first rank's two.
    assert [j.record_id for j in result.jobs] == ["s1", "s2"]


def test_undated_postings_sort_last(monkeypatch):
    """An absent deadline isn't an imminent one."""

    def fake_search(params, rank):
        return [_job("undated", None), _job("dated", "2026-12-31")], 2

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", _ids)

    result = search_jobs(JobQueryParams(ranks=["JUNIOR", "SENIOR"]))

    assert [j.record_id for j in result.jobs][:2] == ["dated", "undated"]


def test_multi_rank_total_counts_each_job_once(monkeypatch):
    """Summing per-rank totals overcounts: 22 of the 55 open JUNIOR-or-
    SENIOR jobs carry both ranks, so summing would claim 77.
    """

    def fake_search(params, rank):
        return [_job(f"{rank}-1")], 40

    def overlapping_ids(params, rank):
        return {"shared-a", "shared-b", f"{rank}-only"}

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", overlapping_ids)

    result = search_jobs(JobQueryParams(ranks=["JUNIOR", "SENIOR"]))

    assert result.total == 4  # shared-a, shared-b, JUNIOR-only, SENIOR-only


def test_rank_fetches_actually_run_concurrently(monkeypatch):
    """Proven with a barrier rather than timing: it can only be passed if
    both rank fetches are in flight at once. Also guards the context bug -
    one shared Context object can't be entered by two threads, which only
    shows up when the tasks genuinely overlap.
    """
    import threading

    barrier = threading.Barrier(2, timeout=5)

    def fake_search(params, rank):
        barrier.wait()
        return [_job(f"{rank}-1", "2026-10-01")], 1

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)
    monkeypatch.setattr("app.inspire_client._matching_record_ids", _ids)

    result = search_jobs(JobQueryParams(ranks=["JUNIOR", "SENIOR"]))

    assert len(result.jobs) == 2


# --- Postings whose deadline has passed -------------------------------------

from datetime import date, datetime, timezone  # noqa: E402

import pytest  # noqa: E402

import app.inspire_client as inspire_client  # noqa: E402


@pytest.mark.parametrize(
    ("now", "cutoff"),
    [
        # 10:00 UTC on 2 Oct is still 1 Oct in UTC-12, so a 1 Oct deadline
        # hasn't passed everywhere yet.
        (datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc), date(2026, 10, 1)),
        # From noon UTC it is 2 Oct everywhere, and 1 Oct has ended.
        (datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc), date(2026, 10, 2)),
    ],
)
def test_a_deadline_stays_open_until_its_date_has_ended_everywhere(now, cutoff):
    assert inspire_client.deadline_cutoff(now) == cutoff


@pytest.mark.parametrize(
    ("deadline", "expired"),
    [("2026-10-01", True), ("2026-10-02", False), ("2026-11-30", False),
     (None, False), ("", False), ("not a date", False)],
)
def test_only_a_deadline_before_the_cutoff_counts_as_expired(deadline, expired):
    assert inspire_client.is_expired(deadline, cutoff=date(2026, 10, 2)) is expired


def sent_queries(monkeypatch, params):
    """Run a search and return the `q` of every request it sent to Inspire."""
    sent = []

    def fake_request(endpoint, query_params):
        sent.append(query_params["q"])
        return {"hits": {"hits": [], "total": 0}}

    monkeypatch.setattr(inspire_client, "_request", fake_request)
    monkeypatch.setattr(inspire_client, "deadline_cutoff", lambda now=None: date(2026, 10, 2))
    search_jobs(params)
    return sent


def test_open_searches_ask_inspire_to_drop_passed_deadlines(monkeypatch):
    """Inspire keeps a posting "open" for a while after its deadline, and
    sorted by soonest deadline those would top the results.
    """
    assert sent_queries(monkeypatch, JobQueryParams(keywords="string theory")) == [
        "(string theory) and deadline_date:>=2026-10-02"
    ]


def test_a_search_with_no_keywords_is_still_filtered(monkeypatch):
    assert sent_queries(monkeypatch, JobQueryParams(keywords="")) == ["deadline_date:>=2026-10-02"]


def test_the_counting_requests_of_a_multi_rank_search_are_filtered_too(monkeypatch):
    """Otherwise "Showing 25 of N" would count jobs that are never shown."""
    queries = sent_queries(monkeypatch, JobQueryParams(keywords="cosmology", ranks=["JUNIOR", "SENIOR"]))

    assert len(queries) == 4
    assert all(q == "(cosmology) and deadline_date:>=2026-10-02" for q in queries)


def test_keywords_cannot_close_the_group_and_escape_the_filter(monkeypatch):
    queries = sent_queries(monkeypatch, JobQueryParams(keywords="x) or (y"))

    assert queries == ["(x or y) and deadline_date:>=2026-10-02"]


def test_a_closed_search_is_not_date_filtered(monkeypatch):
    assert sent_queries(monkeypatch, JobQueryParams(keywords="x", status="closed")) == ["x"]

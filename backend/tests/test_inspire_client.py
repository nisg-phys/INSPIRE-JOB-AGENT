from app.inspire_client import JobQueryParams, RawJob, search_jobs


def _job(record_id: str) -> RawJob:
    return RawJob(record_id=record_id, position="Postdoc")


def test_no_ranks_makes_a_single_unfiltered_request(monkeypatch):
    calls = []

    def fake_search(params, rank):
        calls.append(rank)
        return [_job("1")]

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)

    result = search_jobs(JobQueryParams(keywords="cosmology"))

    assert calls == [None]
    assert [j.record_id for j in result] == ["1"]


def test_multiple_ranks_make_one_request_per_rank_and_merge(monkeypatch):
    """Regression test: Inspire's rank filter only accepts one value per
    request (confirmed empirically - repeated params and comma-separated
    values both failed), so multi-rank queries (e.g. "faculty" ->
    JUNIOR+SENIOR) must fan out into separate requests and merge results.
    """
    calls = []

    def fake_search(params, rank):
        calls.append(rank)
        return [_job(f"{rank}-1"), _job(f"{rank}-2")]

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)

    result = search_jobs(JobQueryParams(keywords="cosmology", ranks=["JUNIOR", "SENIOR"]))

    assert calls == ["JUNIOR", "SENIOR"]
    assert [j.record_id for j in result] == ["JUNIOR-1", "JUNIOR-2", "SENIOR-1", "SENIOR-2"]


def test_multi_rank_results_are_deduped_by_record_id(monkeypatch):
    """The same posting can genuinely satisfy more than one rank filter
    (e.g. a JUNIOR/SENIOR-listed opening) - it must only appear once."""

    def fake_search(params, rank):
        return [_job("shared"), _job(f"{rank}-only")]

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)

    result = search_jobs(JobQueryParams(keywords="cosmology", ranks=["JUNIOR", "SENIOR"]))

    ids = [j.record_id for j in result]
    assert ids.count("shared") == 1
    assert ids == ["shared", "JUNIOR-only", "SENIOR-only"]


def test_multi_rank_results_respect_size_cap(monkeypatch):
    def fake_search(params, rank):
        return [_job(f"{rank}-{i}") for i in range(5)]

    monkeypatch.setattr("app.inspire_client._search_jobs_single", fake_search)

    result = search_jobs(JobQueryParams(keywords="x", ranks=["JUNIOR", "SENIOR"], size=3))

    assert len(result) == 3


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

    [job] = search_jobs(JobQueryParams(keywords="x"))

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

    [job] = search_jobs(JobQueryParams(keywords="x"))

    assert job.institutions == ["Wolfram Institute, Champaign", "Kentucky U."]
    assert job.institution_ids == {"Kentucky U.": "904048"}

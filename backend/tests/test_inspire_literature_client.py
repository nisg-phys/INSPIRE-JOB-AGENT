from app.inspire_literature_client import _affiliation_query, recent_papers


def test_query_uses_institution_id_when_known():
    assert _affiliation_query("U. Kentucky", "904048") == "affid 904048"


def test_query_falls_back_to_exact_phrase_name_without_an_id():
    assert _affiliation_query("Wolfram Institute, Champaign", None) == 'aff "Wolfram Institute, Champaign"'


def test_recent_papers_sends_the_id_query_to_inspire(monkeypatch):
    seen = {}

    def fake_request(endpoint, params):
        seen.update(endpoint=endpoint, params=params)
        return {"hits": {"hits": []}}

    monkeypatch.setattr("app.inspire_literature_client._request", fake_request)

    recent_papers("U. Kentucky", "904048")

    assert seen["endpoint"] == "literature"
    assert seen["params"]["q"] == "affid 904048"

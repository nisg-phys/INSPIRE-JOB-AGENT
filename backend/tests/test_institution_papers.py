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

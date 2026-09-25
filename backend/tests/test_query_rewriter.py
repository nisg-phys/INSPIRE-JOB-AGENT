from app.query_rewriter import ParsedQuery, to_job_query_params


def test_ranks_pass_through_as_structured_filter_not_keywords():
    """Regression test for the bug where "faculty" leaking into free-text
    keywords let Master's/PhD postings outscore actual faculty postings -
    ranks must go through JobQueryParams.ranks, never into keywords.
    """
    parsed = ParsedQuery(subfield="cosmology", ranks=["JUNIOR", "SENIOR"])
    params = to_job_query_params(parsed)

    assert params.ranks == ["JUNIOR", "SENIOR"]
    assert "junior" not in params.keywords.lower()
    assert "senior" not in params.keywords.lower()
    assert "faculty" not in params.keywords.lower()


def test_unknown_rank_from_llm_is_dropped():
    """The LLM is asked to only emit ranks from inspire_client.RANKS, but
    treat its output as untrusted - an invented rank should never reach
    Inspire's API as a filter value.
    """
    parsed = ParsedQuery(ranks=["PROFESSOR"])
    params = to_job_query_params(parsed)

    assert params.ranks == []


def test_duplicate_ranks_are_deduped():
    parsed = ParsedQuery(ranks=["POSTDOC", "POSTDOC"])
    params = to_job_query_params(parsed)

    assert params.ranks == ["POSTDOC"]


def test_keywords_combine_subfield_location_and_extra_keywords():
    parsed = ParsedQuery(
        subfield="string theory",
        location="Germany",
        keywords=["DFG grant"],
    )
    params = to_job_query_params(parsed)

    assert params.keywords == "string theory Germany DFG grant"


def test_seniority_field_never_leaks_into_keywords():
    """seniority is a display-only field (see ParsedQuery docstring) - only
    ranks should influence the actual Inspire search.
    """
    parsed = ParsedQuery(subfield="cosmology", seniority="faculty", ranks=["SENIOR"])
    params = to_job_query_params(parsed)

    assert "faculty" not in params.keywords.lower()


def test_empty_query_produces_empty_params():
    params = to_job_query_params(ParsedQuery())

    assert params.keywords == ""
    assert params.ranks == []


def test_off_topic_is_the_default_so_unreadable_output_is_not_searched():
    """Fails closed: a response missing the flag means output we couldn't
    read, and answering anyway is what the flag exists to prevent.
    """
    assert ParsedQuery().on_topic is False


def test_runaway_keywords_are_capped():
    """The keyword string is model output shaped by user text. A real query
    is a few words; a huge one means a bad parse or leaked injected text.
    """
    from app.query_rewriter import MAX_KEYWORD_CHARS

    params = to_job_query_params(ParsedQuery(subfield="x" * 500, keywords=["y" * 500]))

    assert len(params.keywords) <= MAX_KEYWORD_CHARS


def test_keywords_are_flattened_to_a_single_line():
    params = to_job_query_params(ParsedQuery(subfield="string\n\ntheory", location="  UK  "))

    assert params.keywords == "string theory UK"

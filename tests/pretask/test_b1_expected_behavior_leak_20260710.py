"""B-1 (leak law): the issue's Expected-Behavior snippet must be leak-screened before
it enters the brief. The other-session audit reproduced a live symbol-anchored leak of
`test_verify_token` + `assert` through raw Expected-Behavior copying (v1r_brief.py:2321-2336).

TTD: these assert the DESIRED screened behavior. Before the screen was added, the
assertion/test-name/F2P cases surfaced verbatim (RED); after, they are dropped
(correct-or-quiet). `test_clean_*` guards that the fix is render-neutral for safe text.

Mutation proof (manual, restore after): comment the `_ASSERT_LEAK_RE` clause ->
test_assertion_snippet_dropped bites; comment the `_obligation_is_leaky` clause ->
test_test_name_snippet_dropped + test_f2p_snippet_dropped bite.
"""
from groundtruth.pretask.v1r_brief import _expected_behavior_spec


def test_clean_expected_behavior_passes():
    # render-neutral: a leak-free spec still reaches the brief
    issue = (
        "Title\n\n### Expected Behavior\n"
        "The parser should return an empty list on end of file.\n"
        "### Steps\n1. run it\n"
    )
    assert _expected_behavior_spec(issue) == "The parser should return an empty list on end of file."


def test_clean_bold_form_passes():
    issue = "**Expected behavior**: returns an empty list on end of file rather than raising"
    assert _expected_behavior_spec(issue) == "returns an empty list on end of file rather than raising"


def test_bare_assert_statement_released():
    # Fable-LIPI round-2 brief Finding-2 (2026-07-11): the BARE `assert` verb/statement is NOT a
    # test identity — it is the issue's own (grader-INDEPENDENT) requirement grammar, and the
    # Expected-Behavior spec is extracted from the already-agent-visible issue text, so
    # re-surfacing it adds NO hidden info. Only assertion CALL/macro forms (assertEqual /
    # assert_eq! / assert()) — the shapes copied verbatim from a test framework — still drop (see
    # test_unittest_assertion_snippet_dropped). The round-1 optional-tail regex over-dropped it.
    issue = "### Expected Behavior\nassert parse(data) == [] after the fix lands\n"
    spec = _expected_behavior_spec(issue)
    assert spec is not None and "parse(data)" in spec, spec


def test_unittest_assertion_snippet_dropped():
    issue = "### Expected Behavior\nself.assertEqual(parse(data), []) once the guard is added\n"
    assert _expected_behavior_spec(issue) is None


def test_test_name_snippet_dropped():
    issue = "### Expected Behavior\ntest_verify_token should return the decoded value here\n"
    assert _expected_behavior_spec(issue) is None


def test_f2p_snippet_dropped():
    issue = "### Expected Result\nThe FAIL_TO_PASS case now returns cleanly instead of raising\n"
    assert _expected_behavior_spec(issue) is None


def test_absent_or_empty_is_none():
    assert _expected_behavior_spec("just a plain issue with no expected section") is None
    assert _expected_behavior_spec("") is None
    assert _expected_behavior_spec("### Expected Behavior\nshort\n") is None  # < 10 chars


def test_english_asserts_is_not_treated_as_leak():
    # the ENGLISH verb "asserts"/"assertion" must NOT trip the keyword screen
    issue = "### Expected Behavior\nthe function asserts nothing and returns the value directly\n"
    assert _expected_behavior_spec(issue) == "the function asserts nothing and returns the value directly"

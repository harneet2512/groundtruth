"""Tests for orientation composite scoring & dynamic tiering.

Verifies that the module satisfies all three mandatory properties:
- Dynamic: tier boundaries adapt to per-task score distribution
- Hybrid: 5 distinct signals composited
- Confidence-gated: explicit [VERIFIED]/[WARNING]/[INFO] tiering + honest note
"""
from groundtruth.orientation.composite import (
    composite_score,
    dynamic_tiers,
    render_orientation,
    _direct_name_match,
    _part_overlap,
    _path_overlap,
    _inverse_hub_score,
    _property_evidence_match,
)


# ---------------------------------------------------------------------------
# Individual signal tests
# ---------------------------------------------------------------------------

def test_direct_match_positive():
    assert _direct_name_match("parse_query", "fix the parse_query bug") == 1.0


def test_direct_match_negative():
    assert _direct_name_match("parse_query", "fix something else") == 0.0


def test_direct_match_case_insensitive():
    assert _direct_name_match("ParseQuery", "fix the parsequery bug") == 1.0


def test_direct_match_empty_inputs():
    assert _direct_name_match("", "issue") == 0.0
    assert _direct_name_match("foo", "") == 0.0


def test_part_overlap_snake_case():
    # parse_query → {parse, query}; issue has "query" → 0.5
    assert _part_overlap("parse_query", {"query"}) == 0.5


def test_part_overlap_camel_case():
    # ParseQuery → {parse, query}; issue has "parse, query" → 1.0
    assert _part_overlap("ParseQuery", {"parse", "query"}) == 1.0


def test_part_overlap_no_match():
    assert _part_overlap("parse_query", {"unrelated"}) == 0.0


def test_part_overlap_common_filtered():
    # get_user → 'get' is common, 'user' remains → 1 part, matches "user" → 1.0
    assert _part_overlap("get_user", {"user"}) == 1.0


def test_path_overlap_positive():
    # src/auth/login.py → {auth, login}; issue keywords {auth}
    score = _path_overlap("src/auth/login.py", {"auth"})
    assert score > 0


def test_path_overlap_negative():
    assert _path_overlap("src/foo/bar.py", {"unrelated"}) == 0.0


def test_inverse_hub_leaf():
    assert _inverse_hub_score(0) == 1.0


def test_inverse_hub_decay():
    s1 = _inverse_hub_score(1)
    s10 = _inverse_hub_score(10)
    s100 = _inverse_hub_score(100)
    assert s1 > s10 > s100
    assert 0 < s100 < 0.5


def test_property_match_with_keyword():
    props = [{"value": "if user is None: raise ValueError('user required')"}]
    assert _property_evidence_match(props, "fix user validation", {"user"}) == 1.0


def test_property_match_no_keywords():
    assert _property_evidence_match([], "issue", {"foo"}) == 0.0


def test_property_match_short_kw_ignored():
    # 'in' is too short (< 4 chars)
    props = [{"value": "if x in foo"}]
    assert _property_evidence_match(props, "issue", {"in"}) == 0.0


# ---------------------------------------------------------------------------
# Composite score (Hybrid property)
# ---------------------------------------------------------------------------

def test_composite_is_hybrid():
    """Composite uses 5 signals, not 1."""
    score, signals = composite_score(
        name="parse_query",
        label="Function",
        file_path="src/api/queries.py",
        caller_count=2,
        properties=[{"value": "if not q: raise ValueError"}],
        issue_text="fix parse_query when q is None",
        issue_kws={"parse", "query", "fix"},
    )
    # All 5 signals should be present in breakdown
    assert set(signals.keys()) == {"direct", "part", "path", "inverse_hub", "prop"}
    # Strong-match case: score should be high
    assert score > 0.5


def test_composite_zero_signal_low_score():
    score, _ = composite_score(
        name="unrelated_helper",
        label="Function",
        file_path="src/totally/different.py",
        caller_count=50,
        properties=None,
        issue_text="parse_query bug",
        issue_kws={"parse", "query"},
    )
    # No direct match, no part overlap, no path overlap, high hub → low
    assert score < 0.2


def test_composite_class_demotion():
    """Class mentioned in issue text gets demoted (usually context, not target)."""
    f_score, _ = composite_score(
        name="QueryParser",
        label="Function",
        file_path="src/parser.py",
        caller_count=1,
        properties=None,
        issue_text="QueryParser bug",
        issue_kws={"queryparser"},
    )
    c_score, _ = composite_score(
        name="QueryParser",
        label="Class",
        file_path="src/parser.py",
        caller_count=1,
        properties=None,
        issue_text="QueryParser bug",
        issue_kws={"queryparser"},
    )
    assert c_score < f_score


def test_composite_inverse_hub_penalizes_high_callers():
    """Same name match but 1 caller vs 100 callers should score differently."""
    s1, _ = composite_score(
        name="run",
        label="Function",
        file_path="src/main.py",
        caller_count=1,
        properties=None,
        issue_text="run bug",
        issue_kws={"run"},
    )
    s100, _ = composite_score(
        name="run",
        label="Function",
        file_path="src/main.py",
        caller_count=100,
        properties=None,
        issue_text="run bug",
        issue_kws={"run"},
    )
    assert s1 > s100


# ---------------------------------------------------------------------------
# Dynamic tier boundaries (Dynamic property)
# ---------------------------------------------------------------------------

def test_tiers_clear_winner():
    """Top score >= 0.5 and gap > 0.3 → VERIFIED at top."""
    scores = [0.85, 0.3, 0.2, 0.1]
    tiers = dynamic_tiers(scores)
    assert tiers[0] == "[VERIFIED]"
    assert "[INFO]" in tiers


def test_tiers_flat_distribution_no_verified():
    """Flat distribution → no VERIFIED, just WARNING and INFO."""
    scores = [0.40, 0.38, 0.35, 0.30]
    tiers = dynamic_tiers(scores)
    assert "[VERIFIED]" not in tiers


def test_tiers_all_weak_all_info():
    """All scores < 0.3 → all INFO."""
    scores = [0.2, 0.15, 0.1]
    tiers = dynamic_tiers(scores)
    assert all(t == "[INFO]" for t in tiers)


def test_tiers_empty():
    assert dynamic_tiers([]) == []


def test_tiers_single_score_high():
    scores = [0.8]
    tiers = dynamic_tiers(scores)
    # Single score: median == score, gap == 0, falls to top >= 0.3 branch
    assert tiers[0] in ("[VERIFIED]", "[WARNING]")


def test_tiers_dynamic_with_different_top_scores():
    """Same shape, different absolute levels → tiering still works."""
    high_scores = [0.9, 0.5, 0.3]
    low_scores = [0.4, 0.2, 0.15]
    high_tiers = dynamic_tiers(high_scores)
    low_tiers = dynamic_tiers(low_scores)
    # High distribution should produce VERIFIED, low should not
    assert "[VERIFIED]" in high_tiers
    assert "[VERIFIED]" not in low_tiers


# ---------------------------------------------------------------------------
# Confidence-gated rendering
# ---------------------------------------------------------------------------

def test_render_verified_becomes_issue_references():
    candidates = [
        {"func": "parse_query", "file": "src/api.py", "callers": 2},
        {"func": "other", "file": "src/other.py", "callers": 1},
    ]
    tiers = ["[VERIFIED]", "[WARNING]"]
    lines, counts = render_orientation(candidates, tiers)
    assert any("Issue references" in line for line in lines)
    assert any("Related (by graph)" in line for line in lines)
    assert counts["verified"] == 1
    assert counts["warning"] == 1


def test_render_info_suppressed():
    candidates = [
        {"func": "foo", "file": "src/a.py", "callers": 5},
        {"func": "bar", "file": "src/b.py", "callers": 3},
    ]
    tiers = ["[INFO]", "[INFO]"]
    lines, counts = render_orientation(candidates, tiers)
    # No "Issue references" or "Related" — INFO entries are suppressed
    assert not any("Issue references" in line for line in lines)
    assert not any("Related (by graph)" in line for line in lines)
    # Should have honest fallback note
    assert any("could not match" in line.lower() for line in lines)
    assert counts["info_suppressed"] == 2


def test_render_warning_only():
    candidates = [{"func": "f1", "file": "a.py", "callers": 2}]
    tiers = ["[WARNING]"]
    lines, _ = render_orientation(candidates, tiers)
    assert any("Related (by graph)" in line for line in lines)
    assert not any("Issue references" in line for line in lines)


def test_render_max_per_section():
    candidates = [{"func": f"f{i}", "file": "a.py", "callers": 1} for i in range(10)]
    tiers = ["[VERIFIED]"] * 10
    lines, _ = render_orientation(candidates, tiers, max_per_section=3)
    # Should have header + 3 candidates = 4 lines max for the verified section
    verified_section_lines = [l for l in lines if l.startswith("  f")]
    assert len(verified_section_lines) == 3


# ---------------------------------------------------------------------------
# End-to-end: composite + tiering + render (integration sanity)
# ---------------------------------------------------------------------------

def test_e2e_strong_match_gets_verified_treatment():
    """End-to-end: a candidate that directly matches issue + has props
    + is a leaf function should land in Issue references section."""
    candidates_raw = [
        {
            "name": "parse_query",
            "label": "Function",
            "file_path": "src/parser.py",
            "caller_count": 2,
            "properties": [{"value": "if not q: raise ValueError('q required')"}],
        },
        {
            "name": "unrelated",
            "label": "Function",
            "file_path": "src/other.py",
            "caller_count": 50,
            "properties": None,
        },
    ]
    issue = "parse_query crashes when q is None"
    kws = {"parse", "query", "crash"}

    scores = []
    candidates = []
    for c in candidates_raw:
        s, _ = composite_score(
            name=c["name"], label=c["label"], file_path=c["file_path"],
            caller_count=c["caller_count"], properties=c["properties"],
            issue_text=issue, issue_kws=kws,
        )
        scores.append(s)
        candidates.append({
            "func": c["name"], "file": c["file_path"], "callers": c["caller_count"]
        })

    tiers = dynamic_tiers(scores)
    lines, counts = render_orientation(candidates, tiers)
    assert any("Issue references" in line for line in lines)
    assert counts["verified"] >= 1


def test_e2e_all_weak_emits_honest_note():
    candidates_raw = [
        {
            "name": "unrelated",
            "label": "Function",
            "file_path": "src/totally/different.py",
            "caller_count": 100,
            "properties": None,
        },
    ]
    issue = "Some specific bug"
    kws = {"specific", "bug"}

    s, _ = composite_score(
        name=candidates_raw[0]["name"], label=candidates_raw[0]["label"],
        file_path=candidates_raw[0]["file_path"],
        caller_count=candidates_raw[0]["caller_count"],
        properties=candidates_raw[0]["properties"],
        issue_text=issue, issue_kws=kws,
    )
    tiers = dynamic_tiers([s])
    candidates = [{"func": "unrelated", "file": "src/totally/different.py", "callers": 100}]
    lines, counts = render_orientation(candidates, tiers)
    assert counts["verified"] == 0
    assert counts["warning"] == 0
    assert any("could not match" in line.lower() for line in lines)

"""Tests for orientation composite scoring & signal-decomposition tiering.

Verifies that the module satisfies all three mandatory properties:
- Dynamic: tier comes from WHICH signals fired per candidate (not numeric
  composite-total thresholds)
- Hybrid: 5 distinct signals composited
- Confidence-gated: explicit [VERIFIED]/[WARNING]/[INFO] tiering + honest note
"""
from groundtruth.orientation.composite import (
    composite_score,
    signal_decomposition_tiers,
    dynamic_tiers,  # backward-compat shim
    render_orientation,
    _direct_name_match,
    _part_overlap,
    _path_overlap,
    _inverse_hub_score,
    _property_evidence_match,
    _tier_from_signals,
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
    assert _part_overlap("parse_query", {"query"}) == 0.5


def test_part_overlap_camel_case():
    assert _part_overlap("ParseQuery", {"parse", "query"}) == 1.0


def test_part_overlap_no_match():
    assert _part_overlap("parse_query", {"unrelated"}) == 0.0


def test_part_overlap_common_filtered():
    assert _part_overlap("get_user", {"user"}) == 1.0


def test_path_overlap_positive():
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
    assert set(signals.keys()) == {"direct", "part", "path", "inverse_hub", "prop"}
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
    assert score < 0.2


def test_composite_class_demotion():
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


def test_composite_class_no_direct_not_demoted():
    """Class with no direct match should NOT be demoted."""
    s_func, _ = composite_score(
        name="Helper",
        label="Function",
        file_path="src/helpers.py",
        caller_count=2,
        properties=None,
        issue_text="unrelated bug",
        issue_kws={"unrelated"},
    )
    s_class, _ = composite_score(
        name="Helper",
        label="Class",
        file_path="src/helpers.py",
        caller_count=2,
        properties=None,
        issue_text="unrelated bug",
        issue_kws={"unrelated"},
    )
    assert s_class == s_func


def test_negative_caller_count_clamped():
    s_neg, _ = composite_score(
        name="foo", label="Function", file_path="bar.py",
        caller_count=-5, properties=None,
        issue_text="", issue_kws=set(),
    )
    s_zero, _ = composite_score(
        name="foo", label="Function", file_path="bar.py",
        caller_count=0, properties=None,
        issue_text="", issue_kws=set(),
    )
    assert s_neg == s_zero


def test_class_label_generalized_across_languages():
    for cls_label in ("Trait", "Enum", "Type", "Protocol", "Module", "Mixin"):
        s_fn, _ = composite_score(
            name="QueryParser", label="Function", file_path="src/p.py",
            caller_count=1, properties=None,
            issue_text="QueryParser bug", issue_kws={"queryparser"},
        )
        s_cls, _ = composite_score(
            name="QueryParser", label=cls_label, file_path="src/p.py",
            caller_count=1, properties=None,
            issue_text="QueryParser bug", issue_kws={"queryparser"},
        )
        assert s_cls < s_fn, f"label={cls_label} did not demote"


# ---------------------------------------------------------------------------
# Signal-decomposition tiering (Option B — Cursor-style categorical)
# ---------------------------------------------------------------------------

def test_tier_verified_on_direct_match():
    """direct == 1.0 -> [VERIFIED] regardless of other signals."""
    signals = {"direct": 1.0, "part": 0.0, "path": 0.0, "inverse_hub": 0.3, "prop": 0.0}
    assert _tier_from_signals(signals) == "[VERIFIED]"


def test_tier_verified_on_majority_part_overlap():
    """part >= 0.5 -> [VERIFIED]."""
    signals = {"direct": 0.0, "part": 0.5, "path": 0.0, "inverse_hub": 0.5, "prop": 0.0}
    assert _tier_from_signals(signals) == "[VERIFIED]"


def test_tier_verified_on_property_match():
    """prop == 1.0 -> [VERIFIED]."""
    signals = {"direct": 0.0, "part": 0.0, "path": 0.0, "inverse_hub": 0.5, "prop": 1.0}
    assert _tier_from_signals(signals) == "[VERIFIED]"


def test_tier_warning_on_partial_part_overlap():
    """part > 0 but < 0.5 -> [WARNING]."""
    signals = {"direct": 0.0, "part": 0.3, "path": 0.0, "inverse_hub": 0.5, "prop": 0.0}
    assert _tier_from_signals(signals) == "[WARNING]"


def test_tier_warning_on_path_only():
    """Only path overlap fired -> [WARNING]."""
    signals = {"direct": 0.0, "part": 0.0, "path": 0.4, "inverse_hub": 0.5, "prop": 0.0}
    assert _tier_from_signals(signals) == "[WARNING]"


def test_tier_info_when_only_inverse_hub():
    """inverse_hub is universal — alone it doesn't make a tier."""
    signals = {"direct": 0.0, "part": 0.0, "path": 0.0, "inverse_hub": 1.0, "prop": 0.0}
    assert _tier_from_signals(signals) == "[INFO]"


def test_tier_info_on_empty_signals():
    """No signals at all -> [INFO]."""
    assert _tier_from_signals({}) == "[INFO]"


def test_tier_no_composite_total_used():
    """Tier is determined by signal categories, not composite total.
    A candidate with high direct + zero everything else and a candidate
    with high direct + many other signals both get [VERIFIED]."""
    minimal = {"direct": 1.0, "part": 0.0, "path": 0.0, "inverse_hub": 0.2, "prop": 0.0}
    full = {"direct": 1.0, "part": 1.0, "path": 1.0, "inverse_hub": 1.0, "prop": 1.0}
    assert _tier_from_signals(minimal) == "[VERIFIED]"
    assert _tier_from_signals(full) == "[VERIFIED]"


def test_signal_decomposition_tiers_list():
    signals_list = [
        {"direct": 1.0, "part": 0, "path": 0, "inverse_hub": 0.5, "prop": 0},
        {"direct": 0.0, "part": 0.2, "path": 0, "inverse_hub": 0.5, "prop": 0},
        {"direct": 0.0, "part": 0, "path": 0, "inverse_hub": 1.0, "prop": 0},
    ]
    tiers = signal_decomposition_tiers(signals_list)
    assert tiers == ["[VERIFIED]", "[WARNING]", "[INFO]"]


def test_signal_decomposition_empty_list():
    assert signal_decomposition_tiers([]) == []


def test_backward_compat_dynamic_tiers_with_dicts():
    """dynamic_tiers (shim) accepts signal dicts and routes to decomposition."""
    signals_list = [{"direct": 1.0, "part": 0, "path": 0, "inverse_hub": 0.5, "prop": 0}]
    assert dynamic_tiers(signals_list) == ["[VERIFIED]"]


def test_backward_compat_dynamic_tiers_with_floats_returns_info():
    """Legacy float scores degrade to [INFO] — caller must migrate."""
    assert dynamic_tiers([0.9, 0.5, 0.3]) == ["[INFO]", "[INFO]", "[INFO]"]


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
    assert not any("Issue references" in line for line in lines)
    assert not any("Related (by graph)" in line for line in lines)
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
    verified_section_lines = [l for l in lines if l.startswith("  f")]
    assert len(verified_section_lines) == 3


def test_render_empty_inputs_returns_fallback():
    lines, counts = render_orientation([], [])
    assert any("could not match" in line.lower() for line in lines)
    assert counts["verified"] == 0
    assert counts["warning"] == 0


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

def test_e2e_strong_match_lands_in_issue_references():
    """End-to-end with composite score → signal decomposition → render."""
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

    signals_list = []
    candidates = []
    for c in candidates_raw:
        _, sig = composite_score(
            name=c["name"], label=c["label"], file_path=c["file_path"],
            caller_count=c["caller_count"], properties=c["properties"],
            issue_text=issue, issue_kws=kws,
        )
        signals_list.append(sig)
        candidates.append({
            "func": c["name"], "file": c["file_path"], "callers": c["caller_count"]
        })

    tiers = signal_decomposition_tiers(signals_list)
    lines, counts = render_orientation(candidates, tiers)
    # parse_query has direct match (=1.0) -> VERIFIED
    assert tiers[0] == "[VERIFIED]"
    assert any("Issue references" in line for line in lines)
    assert counts["verified"] >= 1


def test_e2e_pure_noise_emits_honest_note():
    """A candidate with NO signal except inverse_hub gets [INFO]."""
    _, sig = composite_score(
        name="unrelated",
        label="Function",
        file_path="src/totally/different.py",
        caller_count=100,
        properties=None,
        issue_text="Some specific bug",
        issue_kws={"specific", "bug"},
    )
    tiers = signal_decomposition_tiers([sig])
    candidates = [{"func": "unrelated", "file": "src/totally/different.py", "callers": 100}]
    lines, counts = render_orientation(candidates, tiers)
    assert counts["verified"] == 0
    assert counts["warning"] == 0
    assert any("could not match" in line.lower() for line in lines)

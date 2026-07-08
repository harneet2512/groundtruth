"""GT_OBLIGATIONS_V2 — exercised-clause detector primitives (plan §8 t15-t17).

Pins the strict credit discipline that prevents the v1 ANY-token false-pass
channel, the violations-only/silent-when-clean contract, and the honest
`unverifiable` tier. Views/edits must never credit exercise (the happy-dom
read-11-times-edited-never lesson)."""
from __future__ import annotations

from dataclasses import dataclass, field

from groundtruth.runtime.obligations import (
    CLAUSE_EDITED_UNEXERCISED,
    CLAUSE_EXERCISED,
    CLAUSE_UNADDRESSED,
    CLAUSE_UNVERIFIABLE,
    clause_exercised,
    coverage_summary,
    exercise_statuses,
    render_unexercised_block,
)


@dataclass
class _View:
    idx: int
    verbatim: str
    subject_symbols: frozenset = frozenset()
    sym_parts: frozenset = frozenset()


def _v(idx, verbatim, subject, parts=None):
    return _View(idx, verbatim, frozenset(subject), frozenset(parts or subject))


# ── strict credit discipline (t17) ───────────────────────────────────────────
def test_compound_symbol_substring_credits():
    v = _v(0, "`filterMap` has a curried form `filterMap(fn)`", {"filterMap"})
    assert clause_exercised(v, {"test_filtermap"})          # substring, case-insens
    assert clause_exercised(v, {"filterMap"})               # exact
    assert not clause_exercised(v, {"filter", "map"})       # split never credits


def test_noncompound_case_sensitive_exact_only():
    v = _v(0, "add `compact` to `maybe`", {"maybe"})
    assert not clause_exercised(v, {"Maybe"})   # case mismatch -> no credit
    assert not clause_exercised(v, {"maybeX"})  # substring -> no credit
    assert clause_exercised(v, {"maybe"})       # exact, case-sensitive


def test_views_and_edits_never_credit():
    v = _v(0, "abort must propagate through `FetchBodyUtility`", {"FetchBodyUtility"})
    statuses = exercise_statuses(
        [v], edited_tokens={"FetchBodyUtility"}, tested_tokens=set()
    )
    assert statuses[0][1] == CLAUSE_EDITED_UNEXERCISED  # edited != exercised


# ── tiers ─────────────────────────────────────────────────────────────────────
def test_unverifiable_when_no_credit_eligible_symbols():
    v = _v(0, "If registered objects can be paired 1:1, use objects", set())
    assert exercise_statuses([v], set(), set())[0][1] == CLAUSE_UNVERIFIABLE


def test_unaddressed_without_edit_or_test():
    v = _v(0, "raise `ValueError` on negative timeout", {"ValueError"})
    assert exercise_statuses([v], set(), set())[0][1] == CLAUSE_UNADDRESSED


# ── violations-only rendering + silence (t15, with mutation-bite structure) ──
def test_silent_when_all_exercised():
    vs = [
        _v(0, "`filterMap` curried form", {"filterMap"}),
        _v(1, "`partition` splits results", {"partition"}),
    ]
    st = exercise_statuses(vs, set(), {"filterMap", "partition"})
    assert all(s == CLAUSE_EXERCISED for _v_, s in st)
    assert render_unexercised_block(st) == ""  # correct-or-quiet: SILENT


def test_speaks_only_about_violations():
    vs = [
        _v(0, "`filterMap` curried form works", {"filterMap"}),
        _v(1, "`partition` splits results", {"partition"}),
    ]
    st = exercise_statuses(vs, {"filterMap"}, {"partition"})
    block = render_unexercised_block(st)
    assert "filterMap" in block and "never exercised" in block
    assert "partition" not in block  # exercised clause NOT mentioned


def test_unverifiable_counted_not_itemized():
    vs = [_v(0, "use objects when pairable", set())]
    block = render_unexercised_block(exercise_statuses(vs, set(), set()))
    assert "could not be auto-checked" in block
    assert "use objects" not in block  # counted, never itemized


def test_leak_screen_drops_row_whole_and_can_silence():
    vs = [_v(0, "must pass test_secret_gold", {"test_secret_gold"})]
    st = exercise_statuses(vs, set(), set())
    block = render_unexercised_block(st, leak_screen=lambda row: "test_" in row)
    assert "test_secret_gold" not in block
    assert block == ""  # only row leaky, nothing unverifiable -> silent


def test_dose_cap_max_listed():
    vs = [_v(i, f"clause number {i} of `sym_{i}`", {f"sym_{i}"}) for i in range(9)]
    st = exercise_statuses(vs, set(), set())
    block = render_unexercised_block(st, max_listed=6)
    assert block.count("[not addressed") == 6
    assert "+3 more" in block


# ── coverage summary ─────────────────────────────────────────────────────────
def test_coverage_summary_fields():
    vs = [
        _v(0, "a `alpha_fn` clause", {"alpha_fn"}),
        _v(1, "b `beta_fn` clause", {"beta_fn"}),
        _v(2, "c prose-only clause", set()),
    ]
    st = exercise_statuses(vs, {"beta_fn"}, {"alpha_fn"})
    cs = coverage_summary(st)
    assert cs["coverage_version"] == 2
    assert cs["n_exercised"] == 1
    assert cs["n_edited_unexercised"] == 1
    assert cs["n_unverifiable"] == 1
    assert abs(cs["coverage_exercised"] - 0.5) < 1e-9

r"""C15 residue — the acquisition and delivery counts must stop sharing a name, END TO END.

`test_acquisition_counters_are_not_delivery_20260727.py` proved the unit-level defect and landed
`_l1_acquisition_counts`. It did NOT wire that counter into the brief, so `generate_v1r_brief` still
reported only the delivery count under the acquisition-sounding names. This file closes the residue
at the level the ledger's answer criterion demands: the REAL `generate_v1r_brief`, driven against a
real graph.db, under the REAL production flag pair.

THE ANSWER CRITERION (GT_OPEN_CONCERNS C15): "with both flags ON, leg counts > 0 AND delivered == 0
in the same record; with flags OFF, both > 0." Delivered is reported NOT_EVALUABLE rather than 0
under the re-slot, per the same entry's fix spec — a 0 there is the same lie under a better name.

MEASURED, by the tests below, on the fixture graph:
    flags OFF -> acquired 3/3/1/2   delivered 2/2/1/2   legacy 2/2/1/2
    flags ON  -> acquired 3/3/1/2   delivered NOT_EVAL  legacy 0/0/0/0   <-- the production defect

The flags-ON row is run 30297116212's signature: every acquisition-named counter reads 0 while the
legs demonstrably found three graph-backed candidates. That zero was read as "the acquisition
subsystem is dark", written into gt_gt.md as architecture state-of-record, and used to redirect a
day of work before being killed by artifact.

WHAT IS DELIBERATELY *NOT* CHANGED, and why a test guards it: the four legacy fields keep their
DELIVERY values. `scripts/metrics/foundational_gates.py` gate 3b reads
`semantic_signal_count / min(rendered_candidate_count, k_sem_top)` and judges the distribution in
`sem_components` — one coherent delivery-side triple, whose alignment `absorption_contract` asserts.
Repointing the numerator to acquisition while the denominator stayed delivery would have made a
FAIL-CLOSED GATE compare across namespaces and silently pass more often. Fixing a naming defect by
introducing a cross-namespace comparison would have been a strictly worse bug than the one repaired.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask import v1r_brief as vb

ISSUE = "SafeWatchdog._fd leaks the watchdog file descriptor when the postmaster restarts"

_ACQ = ("acquired_graph_edge_count", "acquired_semantic_signal_count",
        "acquired_structural_signal_count", "acquired_fts5_signal_count")
_DEL = ("delivered_graph_edge_count", "delivered_semantic_signal_count",
        "delivered_structural_signal_count", "delivered_fts5_signal_count")
_LEGACY = ("graph_edge_count", "semantic_signal_count",
           "structural_signal_count", "fts5_signal_count")


@pytest.fixture
def repo_root(tmp_path: Path) -> str:
    """A source tree matching the `tiny_graph_db` fixture, so the grep-to-seed leg has bytes."""
    pkg = tmp_path / "repo" / "patroni"
    pkg.mkdir(parents=True)
    (pkg / "watchdog.py").write_text(
        "class SafeWatchdog:\n    def _fd(self):\n        return 1\n", encoding="utf-8")
    (pkg / "postmaster.py").write_text("class Postmaster:\n    pass\n", encoding="utf-8")
    return str(tmp_path / "repo")


def _brief(graph_db: str, repo: str, monkeypatch, *, reslot: bool):
    for flag in ("GT_BRIEF_MINIMAL", "GT_LOC_RESLOT"):
        monkeypatch.delenv(flag, raising=False)
    if reslot:
        monkeypatch.setenv("GT_BRIEF_MINIMAL", "1")
        monkeypatch.setenv("GT_LOC_RESLOT", "1")
    return vb.generate_v1r_brief(ISSUE, repo, graph_db, bug_id="c15", repo="patroni")


def _vals(result, names) -> list:
    return [getattr(result, n) for n in names]


# --------------------------------------------------------------------------- #
# POSITIVE CONTROL — run FIRST. Without it every zero below is unreadable, which
# is precisely the failure this concern exists to repair.
# --------------------------------------------------------------------------- #
def test_positive_control_both_families_are_nonzero_with_the_flags_OFF(
        tiny_graph_db, repo_root, monkeypatch):
    """Prove the instruments CAN produce non-zero on this graph before reading any zero.

    If this test ever fails, no other assertion in this file means anything: a zero from a
    counter that cannot produce a non-zero says nothing about the subsystem it names.
    """
    r = _brief(tiny_graph_db, repo_root, monkeypatch, reslot=False)

    assert r.files, "control failed: nothing was delivered even with the reduction OFF"
    assert all(v > 0 for v in _vals(r, _ACQ)), f"acquisition all-zero: {_vals(r, _ACQ)}"
    assert all(v is not None and v > 0 for v in _vals(r, _DEL)), (
        f"delivery all-zero with the reduction OFF: {_vals(r, _DEL)}")


def test_acquired_is_never_less_than_delivered(tiny_graph_db, repo_root, monkeypatch):
    """The two families count the SAME signals over DIFFERENT populations — delivered is a
    subset of ranked. If acquired < delivered the two are being computed off different
    inputs and the pair is meaningless."""
    r = _brief(tiny_graph_db, repo_root, monkeypatch, reslot=False)
    for a, d in zip(_ACQ, _DEL):
        assert getattr(r, a) >= getattr(r, d), (
            f"{a}={getattr(r, a)} < {d}={getattr(r, d)}: delivered is not a subset of ranked")


# --------------------------------------------------------------------------- #
# THE DEFECT, reproduced live under the production flag pair.
# --------------------------------------------------------------------------- #
def test_the_legacy_names_still_read_zero_under_the_reslot(
        tiny_graph_db, repo_root, monkeypatch):
    """THE DEFECT ITSELF, pinned so it cannot be quietly re-armed.

    This is not a regression to fix — it is the documented, intended DELIVERY semantics of
    these four fields, and gate 3b depends on it. The bug was never the value; it was that
    the value shipped under a name that reads as acquisition with no acquisition field beside
    it. This test exists so anyone who "fixes" the zero by repointing these names to
    acquisition breaks a test that explains why they must not.
    """
    r = _brief(tiny_graph_db, repo_root, monkeypatch, reslot=True)
    assert r.files == [], "the re-slot did not empty the delivered set; wrong mechanism"
    assert _vals(r, _LEGACY) == [0, 0, 0, 0], (
        f"expected the legacy delivery counters to read 0 under the re-slot, got "
        f"{_vals(r, _LEGACY)}")


def test_acquisition_survives_the_reslot_that_zeroes_delivery(
        tiny_graph_db, repo_root, monkeypatch):
    """THE FIX. Same run, same reduction, same emptied delivered set — and the acquisition
    family still reports what the legs found. This single assertion is what makes the
    "all acquisition legs read ZERO" reading impossible to repeat."""
    r = _brief(tiny_graph_db, repo_root, monkeypatch, reslot=True)
    assert all(v > 0 for v in _vals(r, _ACQ)), (
        f"acquisition zeroed by a DELIVERY-side reduction — the C15 defect is back: "
        f"{_vals(r, _ACQ)}")


def test_delivery_is_NOT_EVALUABLE_not_zero_when_the_reduction_emptied_it(
        tiny_graph_db, repo_root, monkeypatch):
    """Under the re-slot "0 delivered" is a statement about the re-slot, not about delivery
    quality. Reporting it as 0 is the same lie under a better name, so the delivered family
    reports None == NOT_EVALUABLE."""
    r = _brief(tiny_graph_db, repo_root, monkeypatch, reslot=True)
    assert _vals(r, _DEL) == [None] * 4, f"expected NOT_EVALUABLE, got {_vals(r, _DEL)}"
    assert r.delivered_candidate_count is None


# --------------------------------------------------------------------------- #
# NOT_EVALUABLE MUST NOT SPREAD — the guard against curing a false 0 with a false None.
# --------------------------------------------------------------------------- #
def test_an_honest_zero_stays_zero_when_nothing_was_ranked(tmp_path, monkeypatch):
    """The no-match early return: the legs ran and ranked NOTHING. Acquisition is genuinely 0
    and the reduction withheld nothing, so delivery is genuinely 0 — NOT NOT_EVALUABLE.

    Without this pin, "report None when empty" would creep across every empty path and destroy
    the only signal that distinguishes "the legs found nothing" from "the re-slot removed the
    population". That would be the same defect class, inverted.
    """
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, "
        "name TEXT NOT NULL, qualified_name TEXT, file_path TEXT NOT NULL, start_line INTEGER, "
        "end_line INTEGER, signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0, "
        "is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, "
        "target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER, source_file TEXT, "
        "resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT);"
    )
    conn.commit()
    conn.close()
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()

    monkeypatch.setenv("GT_BRIEF_MINIMAL", "1")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    r = vb.generate_v1r_brief(ISSUE, str(empty_repo), str(db), bug_id="c15-nm", repo="none")

    assert r.files == []
    assert _vals(r, _ACQ) == [0, 0, 0, 0], "nothing was ranked, so acquisition must be an honest 0"
    assert _vals(r, _DEL) == [0, 0, 0, 0], (
        "nothing was WITHHELD, so delivery must be an honest 0, never NOT_EVALUABLE")
    assert r.delivered_candidate_count == 0


# --------------------------------------------------------------------------- #
# GATE PROTECTION — the delivery-side triple gate 3b reads stays same-namespace.
# --------------------------------------------------------------------------- #
def test_gate_3b_numerator_and_denominator_remain_the_same_population(
        tiny_graph_db, repo_root, monkeypatch):
    """`gate_embedder_consumption` computes semantic_signal_count / min(rendered_candidate_count,
    k_sem_top) and judges sem_components. All three must stay DELIVERY-side. This test fails if
    someone repoints the numerator at acquisition, which would make a fail-closed gate compare an
    acquisition numerator against a delivery denominator and pass more often for a bad reason."""
    r = _brief(tiny_graph_db, repo_root, monkeypatch, reslot=False)

    assert r.rendered_candidate_count == len(r.files)
    assert r.semantic_signal_count == sum(1 for s in r.sem_components if s > 0), (
        "semantic_signal_count is no longer the count of nonzero sem_components — the gate's "
        "numerator and its distribution have drifted apart")
    assert r.semantic_signal_count <= r.rendered_candidate_count, (
        "numerator exceeds its own denominator: the two are no longer the same population")
    assert r.semantic_signal_count == r.delivered_semantic_signal_count, (
        "the legacy field must remain the DELIVERY count")


# --------------------------------------------------------------------------- #
# WIRE FORM — a JSON reader must not receive a bare 0 for the unanswerable question.
# --------------------------------------------------------------------------- #
def test_not_evaluable_has_an_explicit_wire_form():
    assert vb._NOT_EVALUABLE == "NOT_EVALUABLE"


def test_both_families_survive_the_brief_cache_boundary():
    """The gate->emit cache round-trips a whitelist. A field missing from it arrives downstream
    as its DEFAULT — 0 for acquisition, which would silently restore the exact false zero this
    work removed, and None for delivery, which would fake NOT_EVALUABLE on every task."""
    from groundtruth.runtime import brief_cache

    for name in _ACQ + _DEL + ("delivered_candidate_count",):
        assert name in brief_cache._METRIC_FIELDS, (
            f"{name} is not carried across the gate->emit boundary; it will read as its default")

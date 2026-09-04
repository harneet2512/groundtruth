"""LIPI audit of the oracle stack (2026-06-10) — red->green for 4 real defects.

Defect 1 (Integration, gt_mini_patch._augment_output / _oracle_gate_blocks):
    producer fire-once latches (`_cochange_fired`, `_contract_seen`, `_seen`,
    `_l5_*_fired`, `_oracle_review_fired`) are consumed at PRODUCTION time, but
    the Stage-4 gate emits only ONE candidate per turn — every outranked /
    irrelevant loser burned its latch and was lost FOREVER, not deferred.
    Guaranteed case: on the first edit turn the L3 contract (sev 3) always
    outranks the L3 cochange (sev 1) -> the cochange completeness signal was
    structurally dead on every graph-bearing task.  The cochange producer's own
    comment states the contract: "consume the one-shot only on a REAL emit".
    Fix: gate losers re-arm their latches (deferred, not destroyed).

Defect 2 (Integration, same family): an EMPTY `_scope_completeness_block()` at
    the review transition still consumed `_oracle_review_fired` — a later,
    non-empty completeness check could never fire.

Defect 3 (Implementation, gt_oracle._l5_fire_streaming): the streamed
    failure_persisted confidence denominator (`runner_count`) skipped real
    test-runner turns whose observation was EMPTY (timeout/SIGKILL -> no
    output), while the pure reference path (`_l5_raw_fire`) counts them.
    Same fire, different confidence -> the documented "IDENTICAL to
    _l5_decision_at" contract was violated.

Defect 4 (Logic, spec.py `_add`): the kept-symbol filter (`not s.islower()`)
    admitted sentence-initial capitalized ENGLISH words (The/When/Run/...) as
    obligation symbols -> polluted `gt_issue_anchors.json` obligations[] ->
    polluted the live `_oracle_focus()` relevance gate (a noise token like
    "The" can make junk blocks "relevant") and the §5.2 edited?/tested?
    intersections.  Fix: a capitalized single-hump word counts only when it
    occurs mid-fragment (Optional, Trace), never fragment-initial-only.

LEG 1: all fixes are structural (latch lifecycle, ratio denominator, English
requirement grammar) — no task IDs, repos, or gold.  LEG 2: defers + delivers
the completeness/evidence classes the gate was silently destroying.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_ORACLE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load(path: Path, name: str):
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


@pytest.fixture(scope="module")
def sense_mod():
    return _load(_SENSE_PATH, "gt_oracle_sense_lipi")


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_lipi")


@pytest.fixture(scope="module")
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_lipi")


def _turn(sense_mod, cmd: str, obs: str = ""):
    return sense_mod.Turn(index=0, command=cmd, raw_obs=obs, full_obs=obs)


# ---------------------------------------------------------------------------
# Defect 1 — gate losers must re-arm their production latches (deferred, not
# destroyed).  The guaranteed live case: first-edit turn, contract (sev 3)
# outranks cochange (sev 1); pre-fix `_cochange_fired` stayed True forever.
# ---------------------------------------------------------------------------
def _reset_oracle_state(patch_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(patch_mod, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(patch_mod, "_ORACLE_ROUTE", True, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_delivered_hashes", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 0, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_review_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_contract_seen", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_seen", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_cochange_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_consensus_scope", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_marker_sent", True, raising=False)
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))


def test_outranked_cochange_rearms_and_delivers_later(patch_mod, monkeypatch, tmp_path):
    """LANE-SPLIT (2026-06-13) UPDATE: contract + cochange are both Lane A now.

    PRE-LANE-SPLIT this asserted the gate-competition model (contract wins,
    cochange is outranked and re-arms to deliver on turn 2).  The data-plane /
    control-plane bulkhead removed that competition: both context blocks deliver
    on the edit turn, and the shared content+state ledger suppresses the
    byte-identical turn-2 re-send.  The latch re-arm for Lane A producers is now
    intentionally dead (they never lose the turn).  The l5.failure-vs-scope
    re-arm path (a real Lane B competition) is still covered by
    test_outranked_l5_failure_nudge_rearms below."""
    _reset_oracle_state(patch_mod, monkeypatch, tmp_path)
    monkeypatch.setattr(
        patch_mod, "_oracle_focus_cache", {"capture_snapshot", "snapshots"}, raising=False
    )
    monkeypatch.setattr(patch_mod, "_classify", lambda c: ("post_edit", "pkg/m.py"), raising=False)
    monkeypatch.setattr(patch_mod, "_to_repo_rel", lambda f, r: "pkg/m.py", raising=False)
    monkeypatch.setattr(patch_mod, "_invalidate_on_edit", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(patch_mod, "_evidence", lambda c: "", raising=False)

    contract = '<gt-contract file="m.py">\n[SIGNATURE] def capture_snapshot()\n</gt-contract>'
    cochange = '<gt-cochange file="m.py">\npkg/snapshots.py changes with this file\n</gt-cochange>'

    def fake_contract(rel):
        # mimic the real producer's production-time latch
        if rel in patch_mod._contract_seen:
            return ""
        patch_mod._contract_seen.add(rel)
        return contract

    def fake_cochange(rel):
        if patch_mod._cochange_fired:
            return ""
        patch_mod._cochange_fired = True
        return cochange

    monkeypatch.setattr(patch_mod, "_graph_contract_block", fake_contract, raising=False)
    monkeypatch.setattr(patch_mod, "_cochange_block", fake_cochange, raising=False)

    # LANE-SPLIT 2026-06-13: contract AND cochange are now BOTH Lane A (the
    # always-on data plane).  They no longer COMPETE in the oracle gate — the
    # old "contract wins, cochange outranked and deferred to turn 2" model is
    # GONE by design (bulkhead pattern; the data plane delivers all of its
    # always-needed context every edit turn, bounded only by the shared
    # content+state ledger dedup).  So BOTH deliver on the edit turn.
    out1 = {"output": "x"}
    patch_mod._augment_output({"command": "sed -i 's/a/b/' pkg/m.py"}, out1)
    assert contract in out1["output"], "Lane A contract must deliver on the edit turn"
    assert cochange in out1["output"], (
        "Lane A cochange must ALSO deliver on the edit turn — it is the "
        "completeness pillar of the always-on data plane, not a gate loser"
    )

    # Turn 2: the SAME edit in the SAME behavioral state -> identical content ->
    # identical content+state hash -> the SHARED _oracle_delivered_hashes ledger
    # suppresses BOTH re-sends (no double-ship across turns either).
    out2 = {"output": "y"}
    patch_mod._augment_output({"command": "sed -i 's/a/b/' pkg/m.py"}, out2)
    assert contract not in out2["output"], (
        "turn-2 byte-identical contract must be dedup-suppressed by the ledger"
    )
    assert cochange not in out2["output"], (
        "turn-2 byte-identical cochange must be dedup-suppressed by the ledger"
    )


def test_outranked_l5_failure_nudge_rearms(patch_mod, monkeypatch, tmp_path):
    """A sev-tie loser (l5.failure vs consensus.scope at the review turn) must
    not permanently lose its fire-once class."""
    _reset_oracle_state(patch_mod, monkeypatch, tmp_path)
    monkeypatch.setattr(patch_mod, "_oracle_focus_cache", {"anything"}, raising=False)
    monkeypatch.setattr(patch_mod, "_classify", lambda c: (None, None), raising=False)
    monkeypatch.setattr(patch_mod, "_evidence", lambda c: "", raising=False)
    monkeypatch.setattr(patch_mod, "_l5_nudge", lambda c, o="", **k: "", raising=False)
    monkeypatch.setattr(patch_mod, "_l5_no_test_evidence_nudge", lambda c, o: "", raising=False)
    scope_block = (
        '\n<gt-scope reason="completeness">\nanything pkg/b.py — in '
        "GT scope, not yet edited\n</gt-scope>"
    )
    monkeypatch.setattr(patch_mod, "_scope_completeness_block", lambda: scope_block, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", {"pkg/a.py"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 2, raising=False)

    nudge = '\n<gt-nudge reason="failure_persisted">\nGT: anything persisted\n</gt-nudge>'

    def fake_failure(cmd, out_text):
        if patch_mod._l5_failure_fired:
            return ""
        patch_mod._l5_failure_fired = True
        return nudge

    monkeypatch.setattr(patch_mod, "_l5_failure_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_l5_failure_nudge", fake_failure, raising=False)

    # review-transition turn: scope (sev 2) and failure nudge (sev 2) co-fire;
    # whichever loses must re-arm its latch.
    out = {"output": "FAILED test_x"}
    patch_mod._augment_output({"command": "pytest -x"}, out)
    emitted_scope = scope_block in out["output"]
    emitted_nudge = nudge in out["output"]
    assert emitted_scope != emitted_nudge  # exactly one wins (budget <=1)
    if emitted_scope:
        assert patch_mod._l5_failure_fired is False, (
            "outranked failure_persisted burned its fire-once latch"
        )
    else:
        assert patch_mod._oracle_review_fired is False


# ---------------------------------------------------------------------------
# Defect 2 — an EMPTY scope-completeness block must not consume the
# review-transition latch.
# ---------------------------------------------------------------------------
def test_empty_scope_block_does_not_consume_review_latch(patch_mod, monkeypatch, tmp_path):
    _reset_oracle_state(patch_mod, monkeypatch, tmp_path)
    monkeypatch.setattr(patch_mod, "_oracle_focus_cache", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_classify", lambda c: (None, None), raising=False)
    monkeypatch.setattr(patch_mod, "_evidence", lambda c: "", raising=False)
    monkeypatch.setattr(patch_mod, "_l5_nudge", lambda c, o="", **k: "", raising=False)
    monkeypatch.setattr(patch_mod, "_l5_failure_nudge", lambda c, o: "", raising=False)
    monkeypatch.setattr(patch_mod, "_l5_no_test_evidence_nudge", lambda c, o: "", raising=False)
    # review transition reached, but the completeness check has nothing to say
    # (nothing edited in scope -> "").
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", {"pkg/other.py"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 3, raising=False)
    monkeypatch.setattr(patch_mod, "_consensus_scope", {"pkg/a.py"}, raising=False)
    out = {"output": "x"}
    patch_mod._augment_output({"command": "ls"}, out)
    assert patch_mod._oracle_review_fired is False, (
        "empty completeness block consumed the review-transition latch — a "
        "later, non-empty check can never fire"
    )


# ---------------------------------------------------------------------------
# Defect 3 — streaming failure_persisted confidence == pure reference path,
# including EMPTY-observation test-runner turns in the denominator.
# ---------------------------------------------------------------------------
def test_streaming_failure_confidence_matches_pure(sense_mod, oracle_mod):
    turns = [
        _turn(sense_mod, "sed -i 's/a/b/' pkg/m.py", ""),
        _turn(sense_mod, "pytest -x", ""),  # killed: empty obs
        _turn(sense_mod, "pytest -x", "FAILED test_x.py::t"),
        _turn(sense_mod, "pytest -x", "FAILED test_x.py::t"),
    ]
    pure = oracle_mod._l5_decision_at(turns, 3)
    assert pure is not None and pure[0] == "failure_persisted"
    streamed = oracle_mod._l5_fire_streaming(turns)[3]
    assert streamed is not None and streamed[0] == "failure_persisted"
    assert streamed[2] == pytest.approx(pure[2], abs=1e-12), (
        f"streamed confidence {streamed[2]!r} != pure reference {pure[2]!r} — "
        "the empty-observation runner turn was dropped from the denominator"
    )
    # the pure value itself: 2 recurrences / 3 real-runner turns.
    assert pure[2] == pytest.approx(2.0 / 3.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Defect 4 — sentence-initial capitalized ENGLISH words are not symbols;
# mid-fragment capitalized identifiers (Optional, Trace) still are.
# ---------------------------------------------------------------------------
def test_spec_sentence_initial_english_caps_not_symbols():
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.spec import extract_spec

    spec = extract_spec(
        "The capture_snapshot method should be async (it currently blocks).\n"
        "When the id is missing it should raise KeyError.\n"
        "get_user must return Optional[User]."
    )
    assert "capture_snapshot" in spec.all_symbols
    assert "KeyError" in spec.all_symbols  # CamelCase-internal kept
    assert "Optional" in spec.all_symbols  # mid-fragment capital kept
    for noise in ("The", "When"):
        assert noise not in spec.all_symbols, (
            f"fragment-initial English word {noise!r} admitted as an obligation "
            "symbol — pollutes obligations[] -> _oracle_focus() -> the live "
            "relevance gate"
        )


def test_spec_numbered_step_leading_verb_not_symbol():
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.spec import extract_spec

    spec = extract_spec("Steps to reproduce:\n1. Run the parser on empty input")
    assert "Run" not in spec.all_symbols


# ---------------------------------------------------------------------------
# Defect 5 (Plumbing, Stage-5 pipe) — the brief's callee-signature renderer
# (`_callee_sig_args`) cut every signature at the balanced arg-list close,
# re-stripping exactly the post-arg contract Stage 5's extractSignature now
# preserves (the boa [86] Rust where-clause `T: Trace + 'static`).  Non-Python
# headers must render in full; the documented Python `name(args)` compaction
# is preserved.
# ---------------------------------------------------------------------------
def test_callee_sig_args_preserves_nonpython_bounds():
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.contract_map import _callee_sig_args

    rust = (
        "fn from_copy_closure_with_captures<F, T>(closure: F, captures: T) "
        "-> NativeFunction where F: Fn(&JsValue, &[JsValue], &mut T, "
        "&mut Context) -> JsResult<JsValue> + Copy + 'static, "
        "T: Trace + 'static"
    )
    out = _callee_sig_args(rust, "from_copy_closure_with_captures")
    assert "T: Trace + 'static" in out, (
        "the boa [86] killing fact was re-stripped by the brief renderer — "
        "Stage 5's full-header signature never reaches the agent"
    )
    # the documented Python compaction is unchanged:
    py = "def set_parse(self, key, string: str) -> None:"
    assert _callee_sig_args(py, "set_parse") == "set_parse(self, key, string: str)"


# ---------------------------------------------------------------------------
# Fail-loud guard — gt_oracle without its Stage-0 sensor sibling must raise a
# clear ImportError at import, not AttributeError ('sense' on None) mid-replay.
# ---------------------------------------------------------------------------
def test_oracle_import_fails_loud_without_sensor(tmp_path):
    iso = tmp_path / "iso"
    iso.mkdir()
    shutil.copy(_ORACLE_PATH, iso / "gt_oracle.py")
    # no gt_oracle_sense.py sibling, and no cached module instance:
    for key in ("gt_oracle_sense_oracle", "gt_mini_patch_oracle"):
        sys.modules.pop(key, None)
    spec = importlib.util.spec_from_file_location("gt_oracle_iso", iso / "gt_oracle.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt_oracle_iso"] = mod  # dataclass annotation resolution needs it
    try:
        with pytest.raises(ImportError):
            spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("gt_oracle_iso", None)
        for key in ("gt_oracle_sense_oracle", "gt_mini_patch_oracle"):
            sys.modules.pop(key, None)

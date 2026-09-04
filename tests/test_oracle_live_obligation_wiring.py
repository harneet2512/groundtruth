"""LIVE obligation wiring (red->green) — the LIPI-flagged gap closed.

The LIPI audit found `gt_oracle.load_obligations()` (gt_oracle.py:389) had ZERO
live callers: the obligation / test-evidence-gap nudge (the Rank-1 flip lever,
gt_gt §15.3 WHAT rank-1 `issue_verbatim` class) fired in replay (`_decide_walk`
Stage 3) but NEVER reached a live agent.  This suite proves the live wiring in
`gt_mini_patch._augment_output`:

  - at the review-transition edge (the proven GT_VERIFY predicate: >=1 source
    edit followed by >=3 non-edit actions) the live path now calls
    `load_obligations()` (via `_obligation_nudge_block`), produces obligation
    candidates, and routes them through the oracle gate alongside the existing
    L5/L3/L3b candidates;
  - the trigger is the anchor-aware test-evidence gap: issue-named symbols were
    EDITED (symbols ∩ edit-command tokens) but NO observed test output mentions
    them (symbols ∩ tested tokens = ∅);
  - confidence is distribution-derived (edit-overlap ratio gated by
    gt_oracle.gate_pool's median+1*MAD floor) — never hardcoded;
  - DOSE: the class fires at most ONCE per task (Stage-3 contract);
  - correct-or-quiet: a tested obligation never fires;
  - kill switch: GT_ORACLE_ROUTE=0 restores exact legacy behavior (no
    obligation fires when off).

RED before the wiring: `_augment_output` never produced a
`<gt-nudge reason="test_evidence_gap">` block on any live turn.
GREEN after: the synthetic review-transition fires it, and the frozen boa
trajectory (tenpack 27307362054) fires it through the LIVE path, not just
replay.

LEG 1: the trigger is a token-set predicate over trajectory + issue text — no
task IDs, no gold, no repo shapes.  LEG 2: this re-positions the issue's own
requirement at the agent's decision point (the un-misdirectable class).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_CORPUS = _ROOT / ".claude" / "reports" / "runs" / "tenpack_27307362054"
_BOA_GLOB = "boa-hierarchical-evaluation-cancellation/jobs/*/*/agent/mini-swe-agent.trajectory.json"


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
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_live_obl")


@pytest.fixture(scope="module")
def sense_mod():
    return _load(_SENSE_PATH, "gt_oracle_sense_live_obl")


_OBL_ASYNC = {
    "verbatim_text": "The capture_snapshot method should be async.",
    "kind": "behavior",
    "symbols": ["capture_snapshot"],
    "keywords": ["async", "should"],
    "checkable_forms": ["async"],
}


def _write_anchors(tmp_path: Path, obligations: list[dict]) -> str:
    p = tmp_path / "gt_issue_anchors.json"
    p.write_text(
        json.dumps(
            {
                "symbols": ["capture_snapshot"],
                "obligations": obligations,
            }
        ),
        encoding="utf-8",
    )
    return str(p)


def _reset_live_state(patch_mod, monkeypatch, *, route: bool = True) -> None:
    """Fresh per-task live state (the per-process state a new mini-swe-agent
    process would start with)."""
    monkeypatch.setattr(patch_mod, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(patch_mod, "_ORACLE_ROUTE", route, raising=False)
    monkeypatch.setattr(patch_mod, "_marker_sent", True, raising=False)
    monkeypatch.setattr(patch_mod, "_action_count", 0, raising=False)
    monkeypatch.setattr(patch_mod, "_source_edit_count", 0, raising=False)
    monkeypatch.setattr(patch_mod, "_cmd_history", [], raising=False)
    monkeypatch.setattr(patch_mod, "_l5_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_l5_failure_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_l5_notest_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_l5_finish_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_test_fail_history", [], raising=False)
    monkeypatch.setattr(patch_mod, "_blind_test_runs", 0, raising=False)
    monkeypatch.setattr(patch_mod, "_test_evidence_seen", False, raising=False)
    monkeypatch.setattr(patch_mod, "_seen", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_contract_seen", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_consensus_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_consensus_scope", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_offscope_views", 0, raising=False)
    monkeypatch.setattr(patch_mod, "_cochange_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_focus_cache", None, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_delivered_hashes", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_nonedit_streak", 0, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_review_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_last_losers", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_obligation_fired", False, raising=False)
    monkeypatch.setattr(patch_mod, "_oblig_status_emitted", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oblig_status_last_hash", None, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_edited_tokens", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_tested_tokens", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_edited_tokens_by_file", {}, raising=False)
    monkeypatch.setattr(patch_mod, "_edit_churn", {}, raising=False)
    monkeypatch.setattr(patch_mod, "_gt_oracle_tried", False, raising=False)
    monkeypatch.setattr(patch_mod, "_gt_oracle_mod", None, raising=False)


def _drive(patch_mod, cmd: str, obs: str = "") -> str:
    out = {"output": obs, "returncode": 0}
    patch_mod._augment_output({"command": cmd}, out)
    return out.get("output") or ""


_EDIT_CMD = "sed -i 's/def capture_snapshot/async def capture_snapshot/' pkg/mod.py"
_NONEDIT_CMDS = ["git status", "git diff", "ls -la"]


# ---------------------------------------------------------------------------
# THE red->green: the live path fires the obligation at the review transition.
# ---------------------------------------------------------------------------
def test_live_obligation_fires_at_review_transition(patch_mod, tmp_path, monkeypatch):
    _reset_live_state(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, [_OBL_ASYNC]))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    outputs = [_drive(patch_mod, _EDIT_CMD)]
    for c in _NONEDIT_CMDS:
        outputs.append(_drive(patch_mod, c))
    joined = "\n".join(outputs)
    assert 'reason="test_evidence_gap"' in joined, (
        f"the live path must fire the obligation nudge at the review transition — got:\n{joined}"
    )
    # the payload quotes the issue requirement VERBATIM and names the symbol.
    assert "capture_snapshot" in joined
    assert "The capture_snapshot method should be async." in joined
    # fired exactly at the transition turn (the 3rd non-edit), not earlier.
    assert "test_evidence_gap" not in outputs[0]
    assert "test_evidence_gap" not in outputs[1]
    # latch consumed: the class is spent for this task.
    assert patch_mod._oracle_obligation_fired is True


def test_live_obligation_status_dedup_same_vector_one_fire(patch_mod, tmp_path, monkeypatch):
    """Delivery-engine Stage 2 dose law: the dedup key is the status VECTOR.
    A second review transition with an UNCHANGED vector (same edit tokens, no
    new test evidence) must NOT re-fire; the once-per-task latch is gone but
    an identical checklist is never re-sent."""
    _reset_live_state(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, [_OBL_ASYNC]))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    outputs = [_drive(patch_mod, _EDIT_CMD)]
    for c in _NONEDIT_CMDS:
        outputs.append(_drive(patch_mod, c))
    # a SECOND identical edit + review transition: same status vector -> quiet.
    outputs.append(_drive(patch_mod, _EDIT_CMD))
    for c in _NONEDIT_CMDS:
        outputs.append(_drive(patch_mod, c))
    fires = sum(o.count('reason="test_evidence_gap"') for o in outputs)
    assert fires == 1, f"identical status vector must not re-fire, got {fires}"


def test_live_obligation_tested_stays_quiet(patch_mod, tmp_path, monkeypatch):
    """Correct-or-quiet: when an observed test result mentions the symbol
    (tested? == True), the obligation never fires."""
    _reset_live_state(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, [_OBL_ASYNC]))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    outputs = [_drive(patch_mod, _EDIT_CMD)]
    # a real test run whose OBSERVED output names the symbol's test: evidence.
    outputs.append(
        _drive(
            patch_mod,
            "python -m pytest tests/test_snapshots.py -q",
            "tests/test_snapshots.py::test_capture_snapshot PASSED\n1 passed in 0.2s",
        )
    )
    for c in _NONEDIT_CMDS:
        outputs.append(_drive(patch_mod, c))
    joined = "\n".join(outputs)
    assert 'reason="test_evidence_gap"' not in joined, joined


def test_live_obligation_untouched_symbol_stays_quiet(patch_mod, tmp_path, monkeypatch):
    """An obligation whose symbols never intersect the edit evidence is not a
    candidate (edited? == False)."""
    _reset_live_state(patch_mod, monkeypatch)
    obl = dict(_OBL_ASYNC, symbols=["totally_other_api"], verbatim_text="x must y.")
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, [obl]))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    outputs = [_drive(patch_mod, _EDIT_CMD)]
    for c in _NONEDIT_CMDS:
        outputs.append(_drive(patch_mod, c))
    assert 'reason="test_evidence_gap"' not in "\n".join(outputs)


def test_kill_switch_legacy_no_obligation(patch_mod, tmp_path, monkeypatch):
    """GT_ORACLE_ROUTE=0 — exact legacy behavior: no obligation fires when off."""
    _reset_live_state(patch_mod, monkeypatch, route=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, [_OBL_ASYNC]))

    outputs = [_drive(patch_mod, _EDIT_CMD)]
    for c in _NONEDIT_CMDS * 3:
        outputs.append(_drive(patch_mod, c))
    joined = "\n".join(outputs)
    assert 'reason="test_evidence_gap"' not in joined
    # the legacy path's own state is untouched by the oracle wiring.
    assert patch_mod._oracle_obligation_fired is False


def test_baseline_arm_no_obligation(patch_mod, tmp_path, monkeypatch):
    """GT_BASELINE: the control arm gets zero GT content, obligations included."""
    _reset_live_state(patch_mod, monkeypatch)
    monkeypatch.setattr(patch_mod, "_GT_BASELINE", True, raising=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", _write_anchors(tmp_path, [_OBL_ASYNC]))
    outputs = [_drive(patch_mod, _EDIT_CMD)]
    for c in _NONEDIT_CMDS:
        outputs.append(_drive(patch_mod, c))
    assert "<gt-" not in "\n".join(outputs)


def test_anchors_path_resolution(patch_mod, tmp_path, monkeypatch):
    """Plumbing (LIPI avenue 4): GT_ANCHORS_PATH wins; else the substrate
    container mount $GT_CERT_DIR/gt_issue_anchors.json; else the /tmp default.
    The live deepswe_full container has the artifact ONLY under /gt_artifacts
    (= GT_CERT_DIR) — the /tmp default never exists there."""
    monkeypatch.setenv("GT_ANCHORS_PATH", "/explicit/a.json")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    assert patch_mod._anchors_path() == "/explicit/a.json"
    monkeypatch.delenv("GT_ANCHORS_PATH")
    # cert-dir fallback requires the file to exist.
    cert_file = tmp_path / "gt_issue_anchors.json"
    cert_file.write_text("{}", encoding="utf-8")
    assert patch_mod._anchors_path() == str(cert_file)
    cert_file.unlink()
    assert patch_mod._anchors_path() == "/tmp/gt_issue_anchors.json"
    monkeypatch.delenv("GT_CERT_DIR")
    assert patch_mod._anchors_path() == "/tmp/gt_issue_anchors.json"


# ---------------------------------------------------------------------------
# The corpus proof: boa's frozen trajectory fires the obligation through the
# LIVE path (not just replay) — the red->green the task demanded.
# ---------------------------------------------------------------------------
def _boa_trajectory() -> str | None:
    import glob

    hits = sorted(glob.glob(str(_CORPUS / _BOA_GLOB)))
    return hits[0] if hits else None


_GT_BLOCK_RE = re.compile(r"<gt-[a-z-]+[^>]*>.*?</gt-[a-z-]+>", re.S)


def test_corpus_boa_live_obligation_fire(patch_mod, sense_mod, tmp_path, monkeypatch):
    tj = _boa_trajectory()
    if tj is None:
        pytest.skip("tenpack corpus not present")
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.spec import extract_spec

    messages = json.load(open(tj, encoding="utf-8", errors="replace"))["messages"]
    issue = ""
    for m in messages:
        if m.get("role") == "user":
            issue = _GT_BLOCK_RE.sub("", m.get("content") or "")
            break
    obls = extract_spec(issue).to_serializable()
    assert obls, "boa issue must extract obligations"

    anchors = tmp_path / "gt_issue_anchors.json"
    anchors.write_text(json.dumps({"obligations": obls}), encoding="utf-8")
    _reset_live_state(patch_mod, monkeypatch)
    monkeypatch.setenv("GT_ANCHORS_PATH", str(anchors))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    turns = sense_mod.load_trajectory(tj)
    fires = 0
    hashes: list[str] = []
    for t in turns:
        out_text = _drive(patch_mod, t.command, t.raw_obs)
        appended = out_text[len(t.raw_obs) :] if out_text.startswith(t.raw_obs) else out_text
        n = appended.count('reason="test_evidence_gap"')
        fires += n
        if n:
            hashes += re.findall(r'reason="test_evidence_gap" h="([0-9a-f]+)"', appended)
    # Stage-2 semantics: >=1 fire through the LIVE path (the original
    # red->green), and EVERY fire carries a DISTINCT status vector (the
    # content×phase×status dedup — an identical checklist is never re-sent).
    assert fires >= 1, "boa must fire the obligation status through the LIVE path"
    assert len(hashes) == fires and len(set(hashes)) == fires, (
        f"every fire must be a distinct status vector: {hashes}"
    )

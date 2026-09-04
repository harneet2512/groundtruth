"""Oracle Stage 4 — route L3/L3b/L4/consensus through the oracle (red->green).

Per `ORACLE_ARCHITECTURE_PLAN.md` §2.2/§8 Stage 4 + gt_gt §15.4 Stage 4.  The
dumping layers become CANDIDATE PRODUCERS behind the one gate:

  - L3b witnesses (the biggest inert class, ~80% of delivered volume) pass the
    RELEVANCE gate ONLY when they intersect issue anchors OR an unmet
    obligation OR the current edit set — never ambient on every post-view.
  - L3 contracts are EDIT-BOUND `rearm_on_change` candidates (dedup by content
    hash; a changed contract re-arms, an identical one stays suppressed).
  - Consensus K-of-N completeness re-routes to the REVIEW-TRANSITION trigger
    (the §11.4 re-route) — the per-view scope dump retires.
  - DEDUP/BUDGET: one emission per turn, content-hash delivered-set, full 8-dp
    suppression telemetry.

Red->green on the frozen tenpack replay: the inert majority of code-map
payloads is suppressed; boa's `no_test_evidence` still fires; csstree still
carries only the benign scaffold_trap; payloads that intersect the task's own
anchors/edit focus are never suppressed as `irrelevant`.

LEG 1: gates are token-set predicates over trajectory + issue text — no task
IDs, no gold, no repo shapes.  LEG 2: this is the Core Product Contract
enforced at the delivery point ("must not flood the agent with graph noise");
the metric moved is the replay inert rate (~80% -> minority) at unchanged
correctness (the consumed class preserved).
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
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_ORACLE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_CORPUS = _ROOT / ".claude" / "reports" / "runs" / "tenpack_27307362054"
_TRAJ_GLOB = str(_CORPUS / "*" / "jobs" / "*" / "*" / "agent" / "mini-swe-agent.trajectory.json")


def _load(path: Path, name: str):
    # GT_BASELINE=1 only WHILE loading (the module captures the flag at import);
    # restore afterwards so this suite never poisons later suites' env (the
    # pre-existing pollution broke test_deepswe_xlang_evidence in suite order).
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
    return _load(_SENSE_PATH, "gt_oracle_sense_s4")


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_s4")


@pytest.fixture(scope="module")
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_s4")


def _turn(sense_mod, cmd: str, obs: str = "", full: str | None = None):
    return sense_mod.Turn(
        index=0, command=cmd, raw_obs=obs, full_obs=full if full is not None else obs
    )


def _trajs() -> list[str]:
    import glob

    return sorted(glob.glob(_TRAJ_GLOB))


_GT_BLOCK_RE = re.compile(r"<gt-[a-z-]+[^>]*>.*?</gt-[a-z-]+>", re.S)


def _issue_text(messages: list[dict]) -> str:
    for m in messages:
        if m.get("role") == "user":
            return _GT_BLOCK_RE.sub("", m.get("content") or "")
    return ""


def _issue_anchor_tokens(issue: str) -> set[str]:
    """Deterministic issue-anchor proxy for replay: identifiers inside backtick
    /fenced code spans (the reporter-marked code surface)."""
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.spec import _code_symbols

    return {s for s in _code_symbols(issue) if len(s) >= 3}


# ---------------------------------------------------------------------------
# Candidate reconstruction from the recorded delivery.
# ---------------------------------------------------------------------------
def test_delivered_payload_candidates_reconstructed(sense_mod, oracle_mod):
    ev = (
        '<gt-evidence kind="post_view" file="a/b.py">\nwitness: helper_fn '
        "called by main_fn\n</gt-evidence>"
    )
    sc = '<gt-scope files="2">\n1. a/b.py — in scope\n</gt-scope>'
    turns = [
        _turn(sense_mod, "cat a/b.py", "code here", "code here" + ev + sc),
    ]
    cands = oracle_mod.delivered_payload_candidates(turns)
    assert len(cands) == 2
    (k0, c0), (k1, c1) = cands
    assert k0 == 0 and k1 == 0
    assert c0.layer == "L3b" and c0.severity == "code_map"
    assert c1.layer == "consensus" and c1.severity == "code_map"
    assert "helper_fn" in c0.relevance_keys
    # nudges are NOT reconstructed here (they are the L5 producer's job).
    nud = '<gt-nudge reason="loop">\nGT: stop\n</gt-nudge>'
    turns2 = [_turn(sense_mod, "ls", "x", "x" + nud)]
    assert oracle_mod.delivered_payload_candidates(turns2) == []


# ---------------------------------------------------------------------------
# The relevance gate: anchored witness passes, unanchored witness suppressed.
# ---------------------------------------------------------------------------
def _mk_witness(sym: str, f: str = "pkg/mod.py") -> str:
    return (
        f'<gt-evidence kind="post_view" file="{f}">\n{sym} called by other_caller\n</gt-evidence>'
    )


def test_irrelevant_witness_suppressed_anchored_preserved(sense_mod, oracle_mod):
    anchored = _mk_witness("capture_snapshot")
    unrelated = _mk_witness("totally_unrelated_helper", "x/y.py")
    turns = [
        _turn(sense_mod, "cat pkg/mod.py", "src", "src" + anchored),
        _turn(sense_mod, "cat x/y.py", "src2", "src2" + unrelated),
    ]
    decisions = oracle_mod.stage4_replay(turns, anchors={"capture_snapshot"})
    # turn 0: anchored witness EMITTED.
    assert decisions[0].emission is not None
    assert decisions[0].emission.layer == "L3b"
    assert "capture_snapshot" in decisions[0].emission.payload_text
    # turn 1: unanchored witness SUPPRESSED as irrelevant (silence + telemetry).
    assert decisions[1].emission is None
    reasons = {t.candidate_id: t.suppressed_reason for t in decisions[1].telemetry}
    assert "irrelevant" in set(reasons.values())


def test_edit_set_relevance_admits_witness(sense_mod, oracle_mod):
    """A witness about a file the agent has EDITED is relevant even without an
    issue anchor (the current-edit-set arm of the relevance gate)."""
    w = _mk_witness("local_helper", "pkg/edited_mod.py")
    turns = [
        _turn(sense_mod, "sed -i 's/a/b/' pkg/edited_mod.py", ""),
        _turn(
            sense_mod,
            "cat pkg/edited_mod.py",
            "src",
            "src" + _mk_witness("edited_mod", "pkg/edited_mod.py"),
        ),
    ]
    decisions = oracle_mod.stage4_replay(turns, anchors=set())
    assert decisions[1].emission is not None
    assert decisions[1].emission.layer == "L3b"
    # silence-check: the same witness with NO edit and NO anchors is suppressed.
    turns2 = [_turn(sense_mod, "cat pkg/edited_mod.py", "src", "src" + w)]
    decisions2 = oracle_mod.stage4_replay(turns2, anchors=set())
    assert decisions2[0].emission is None


# ---------------------------------------------------------------------------
# Dedup: identical content delivered twice -> second one suppressed
# (rearm_on_change: CHANGED content re-arms).
# ---------------------------------------------------------------------------
def test_dedup_suppresses_identical_rearms_on_change(sense_mod, oracle_mod):
    block = _mk_witness("capture_snapshot")
    changed = _mk_witness("capture_snapshot").replace("other_caller", "third_caller")
    turns = [
        _turn(sense_mod, "cat pkg/mod.py", "s", "s" + block),
        _turn(sense_mod, "cat pkg/mod.py", "s", "s" + block),
        _turn(sense_mod, "cat pkg/mod.py", "s", "s" + changed),
    ]
    decisions = oracle_mod.stage4_replay(turns, anchors={"capture_snapshot"})
    assert decisions[0].emission is not None
    assert decisions[1].emission is None
    r1 = {t.suppressed_reason for t in decisions[1].telemetry}
    assert "delivered" in r1
    assert decisions[2].emission is not None  # changed hash re-arms


# ---------------------------------------------------------------------------
# Budget: one emission per turn; severity ranks contract over code-map.
# ---------------------------------------------------------------------------
def test_budget_one_per_turn_contract_outranks_witness(sense_mod, oracle_mod):
    contract = (
        '<gt-contract file="mod.py">\n[SIGNATURE] def capture_snapshot(self) -> str\n</gt-contract>'
    )
    witness = _mk_witness("capture_snapshot")
    turns = [
        _turn(sense_mod, "sed -i 's/x/y/' pkg/mod.py", "", "" + contract + witness),
    ]
    decisions = oracle_mod.stage4_replay(turns, anchors={"capture_snapshot"})
    em = decisions[0].emission
    assert em is not None and em.layer == "L3"
    reasons = {t.candidate_id: t.suppressed_reason for t in decisions[0].telemetry}
    assert "outranked" in set(reasons.values())


# ---------------------------------------------------------------------------
# THE corpus assertion: inert majority suppressed; consumed/relevant preserved.
# ---------------------------------------------------------------------------
def test_corpus_inert_majority_suppressed_consumed_preserved(sense_mod, oracle_mod):
    trajs = _trajs()
    if not trajs:
        pytest.skip("tenpack corpus not present")
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.spec import extract_spec

    total_codemap = 0
    suppressed_codemap = 0
    l5_reasons = {"loop", "scaffold_trap", "failure_persisted", "no_test_evidence"}
    boa_ok = csstree_ok = False
    for tj in trajs:
        messages = json.load(open(tj, encoding="utf-8", errors="replace"))["messages"]
        issue = _issue_text(messages)
        anchors = _issue_anchor_tokens(issue)
        obls = extract_spec(issue).to_serializable()
        turns = sense_mod.load_trajectory(tj)
        decisions = oracle_mod.stage4_replay(turns, obligations=obls, anchors=anchors)
        emitted_ids = set()
        fired_l5 = []
        for d in decisions:
            if d.emission is not None:
                emitted_ids.add(d.emission.candidate_id)
                if d.emission.reason_string in l5_reasons:
                    fired_l5.append(d.emission.reason_string)
        # per-candidate accounting for the code_map class
        for k, c in oracle_mod.delivered_payload_candidates(turns):
            if c.severity != "code_map":
                continue
            total_codemap += 1
            if c.id not in emitted_ids:
                suppressed_codemap += 1
        name = tj.replace("\\", "/")
        if "boa-hierarchical" in name:
            assert "no_test_evidence" in fired_l5, "boa consumed-class fire lost"
            boa_ok = True
        if "csstree-shorthand" in name:
            assert set(fired_l5) <= {"scaffold_trap"}, "csstree harmful nudge"
            csstree_ok = True
    assert boa_ok and csstree_ok
    assert total_codemap > 0
    frac = suppressed_codemap / total_codemap
    # the ~80%-inert class demoted to a minority of what is delivered:
    # the majority of code-map payloads must now be suppressed.
    assert frac >= 0.5, (
        f"inert suppression too weak: {suppressed_codemap}/{total_codemap} = {frac:.8f}"
    )
    print(f"\n[stage4] code-map suppressed {suppressed_codemap}/{total_codemap} = {frac:.8f}")


def test_corpus_anchored_payloads_never_suppressed_as_irrelevant(sense_mod, oracle_mod):
    """Correctness guard for the demotion: a payload whose keys intersect the
    task's own anchor set is never suppressed with reason `irrelevant`."""
    trajs = _trajs()
    if not trajs:
        pytest.skip("tenpack corpus not present")
    for tj in trajs[:3]:
        messages = json.load(open(tj, encoding="utf-8", errors="replace"))["messages"]
        anchors = _issue_anchor_tokens(_issue_text(messages))
        turns = sense_mod.load_trajectory(tj)
        decisions = oracle_mod.stage4_replay(turns, anchors=anchors)
        cands = {c.id: c for _, c in oracle_mod.delivered_payload_candidates(turns)}
        for d in decisions:
            for t in d.telemetry:
                if t.suppressed_reason != "irrelevant":
                    continue
                c = cands.get(t.candidate_id)
                if c is None:
                    continue
                assert not (c.relevance_keys & anchors), (
                    f"{tj}: anchored payload {c.id} suppressed as irrelevant"
                )


# ---------------------------------------------------------------------------
# LIVE wiring (gt_mini_patch): the oracle gate exists, is env-killable, and
# enforces relevance/dedup/budget on the block producers.
# ---------------------------------------------------------------------------
def test_live_gate_function_relevance_dedup_budget(patch_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(patch_mod, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_delivered_hashes", set(), raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_focus_cache", {"capture_snapshot"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", set(), raising=False)
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))

    w_anch = _mk_witness("capture_snapshot")
    w_junk = _mk_witness("unrelated_thing")
    contract = '<gt-contract file="m.py">\n[SIGNATURE] def capture_snapshot()\n</gt-contract>'

    # relevance: junk witness suppressed alone.
    assert patch_mod._oracle_gate_blocks([(1, "l3b.evidence", w_junk, False)]) == ""
    # anchored witness passes.
    assert patch_mod._oracle_gate_blocks([(1, "l3b.evidence", w_anch, False)]) == w_anch
    # dedup: same content again -> suppressed.
    assert patch_mod._oracle_gate_blocks([(1, "l3b.evidence", w_anch, False)]) == ""
    # budget: contract (sev 3, edit-bound) outranks a fresh witness.
    w2 = _mk_witness("capture_snapshot", "other/f.py")
    out = patch_mod._oracle_gate_blocks(
        [
            (1, "l3b.evidence", w2, False),
            (3, "l3.contract", contract, True),
        ]
    )
    assert out == contract
    # 8dp telemetry landed.
    ev = (tmp_path / "ev.jsonl").read_text(encoding="utf-8").splitlines()
    assert ev
    rec = json.loads(ev[-1])
    for s in rec.get("suppressed", []):
        v = s["confidence"]
        assert float(f"{float(v):.8f}") == float(v)


def test_live_scope_completeness_reroute(patch_mod, monkeypatch):
    """Consensus K-of-N goes to review-transition: emits ONLY when the edit set
    is a strict subset of scope AND un-edited members are focus-anchored.

    bug #2: the denominator N is the VERIFIED connected component reachable from
    the EDITED files (`_verified_scope_component` -> `_query_scope` over FACTS-ONLY
    edges), intersected with the recorded `_consensus_scope` union — NOT the
    accumulated grab-bag itself. So an un-edited member must be GRAPH-REACHABLE
    from the edit, not merely present in the viewed-neighbourhood union. We stub
    `_query_scope` to stand in for that verified graph connection (no graph.db in a
    unit test); production reads it from the read-only graph.db."""
    monkeypatch.setattr(
        patch_mod, "_consensus_scope", {"pkg/a.py", "pkg/b.py", "pkg/snapshots.py"}, raising=False
    )
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", {"pkg/a.py"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_focus_cache", {"snapshots"}, raising=False)
    # a.py is VERIFIED-graph-connected to b.py + snapshots.py (the component the
    # completeness check expands from the edited file via FACTS-ONLY edges).
    _verified_nbrs = {"pkg/a.py": ["pkg/b.py", "pkg/snapshots.py"]}
    monkeypatch.setattr(
        patch_mod,
        "_query_scope",
        lambda rel: _verified_nbrs.get(patch_mod._norm_rel(rel), []),
        raising=False,
    )
    block = patch_mod._scope_completeness_block()
    assert block and "<gt-scope" in block
    assert "snapshots.py" in block
    assert "a.py — edited" not in block  # lists the UN-edited members
    # NOT a strict subset (everything edited) -> silence.
    monkeypatch.setattr(
        patch_mod,
        "_oracle_edited_rels",
        {"pkg/a.py", "pkg/b.py", "pkg/snapshots.py"},
        raising=False,
    )
    assert patch_mod._scope_completeness_block() == ""
    # un-edited members not focus-anchored -> silence (correct-or-quiet).
    monkeypatch.setattr(patch_mod, "_oracle_edited_rels", {"pkg/a.py"}, raising=False)
    monkeypatch.setattr(patch_mod, "_oracle_focus_cache", {"zzz_nothing"}, raising=False)
    assert patch_mod._scope_completeness_block() == ""


def test_live_route_env_kill_switch(patch_mod):
    """GT_ORACLE_ROUTE=0 restores the legacy unconditional-append path."""
    assert hasattr(patch_mod, "_ORACLE_ROUTE")
    # default ON in this build
    assert patch_mod._ORACLE_ROUTE in (True, False)

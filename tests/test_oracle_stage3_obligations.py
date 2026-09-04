"""Oracle Stage 3 — obligation + drift candidates fire through the oracle
(red->green).

Per `ORACLE_ARCHITECTURE_PLAN.md` §5.2/§8 Stage 3 + gt_gt §15.4 Stage 3.  The
spec-extracted obligations (wired in the pre-Stage-3 fixes) now have their
TRIGGER: the anchor-aware test-evidence gap — an obligation candidate fires at
the REVIEW transition (the proven GT_VERIFY predicate: >=1 source edit followed
by >=3 non-edit actions) when

  (a) the agent EDITED an issue-named symbol (obligation symbols intersect the
      sensed edit-evidence tokens), AND
  (b) NO observed test output mentions it (obligation symbols do not intersect
      the sensed tested_tokens).

This extends the proven-consumed `no_test_evidence` class with the obligations
(the Rank-1 lever: "none of the tests you ran reference the issue-named API").

The DRIFT class ships WHITELIST-ONLY AND UNCERTIFIED (plan §11: no drift fire on
C1 until a held-out non-benchmark corpus certifies per-class trigger precision):
drift candidates are PRODUCED (telemetry-visible) and ALWAYS suppressed with
reason `drift_uncertified`.

Confidence stays distribution-derived: each obligation's confidence is its
edit-evidence overlap ratio (provenance: the agent's own commands), gated by the
median+1*MAD floor over the triggered pool.  No invented constants.

LEG 1: triggers are predicates over trajectory text + issue text — no task IDs,
no gold, no repo shapes; fixtures cover python AND rust surface forms.
LEG 2: the consumed-class payload (boa's no_test_evidence) is preserved on the
frozen corpus; the new fire is the highest-severity `issue_verbatim` class the
flip levers need (correct context AT the moment the agent decides it is done).
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
    return _load(_SENSE_PATH, "gt_oracle_sense_s3")


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_s3")


def _turn(sense_mod, cmd: str, obs: str = ""):
    return sense_mod.Turn(index=0, command=cmd, raw_obs=obs, full_obs=obs)


_OBL_ASYNC = {
    "verbatim_text": "The capture_snapshot method should be async.",
    "kind": "behavior",
    "symbols": ["capture_snapshot"],
    "keywords": ["async", "should"],
    "checkable_forms": ["async"],
}
_OBL_RETURNS = {
    "verbatim_text": "get_user must return Optional[User].",
    "kind": "behavior",
    "symbols": ["get_user"],
    "keywords": ["must", "returns"],
    "checkable_forms": [],
}


def _python_fixture(sense_mod, *, test_obs: str, edit_cmd: str | None = None):
    """edit issue symbol -> test run (with results) -> 3 non-edit actions ->
    REVIEW transition at the 3rd."""
    edit = edit_cmd or (
        "sed -i 's/def capture_snapshot/async def capture_snapshot/' aiomonitor/monitor.py"
    )
    return [
        _turn(sense_mod, "cat aiomonitor/monitor.py", "def capture_snapshot(self): ..."),
        _turn(sense_mod, edit, ""),
        _turn(sense_mod, "python -m pytest tests/test_other.py -q", test_obs),
        _turn(sense_mod, "ls aiomonitor", "monitor.py"),
        _turn(sense_mod, "git diff --stat", "1 file changed"),
    ]


# ---------------------------------------------------------------------------
# THE Stage-3 fire: edited issue-symbol + no test evidence -> obligation fires
# at the review transition.
# ---------------------------------------------------------------------------
def test_obligation_fires_on_edited_but_untested(sense_mod, oracle_mod):
    turns = _python_fixture(sense_mod, test_obs="..\n2 passed in 0.10s")
    decisions = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    fired = [d for d in decisions if d.emission is not None]
    detail = [(d.turn_index, d.emission.reason_string) for d in fired]
    assert len(fired) == 1, f"expected exactly one fire, got {detail}"
    em = fired[0].emission
    assert em.reason_string == "test_evidence_gap"
    assert em.layer == "spec"
    assert em.channel == "C1"
    # fires AT the review transition (the 3rd non-edit action after the edit).
    assert fired[0].turn_index == 4
    # active check: names the symbol + re-surfaces the issue text VERBATIM.
    assert "capture_snapshot" in em.payload_text
    assert "The capture_snapshot method should be async." in em.payload_text
    assert em.payload_text.lstrip().startswith("<gt-nudge")


def test_obligation_silent_when_tested(sense_mod, oracle_mod):
    """Observed test output mentions the symbol -> obligation met -> silence."""
    turns = _python_fixture(
        sense_mod,
        test_obs="tests/test_monitor.py::test_capture_snapshot PASSED\n1 passed",
    )
    decisions = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    assert all(d.emission is None for d in decisions)


def test_obligation_silent_when_not_edited(sense_mod, oracle_mod):
    """The agent edited an UNRELATED symbol -> obligation not triggered."""
    turns = _python_fixture(
        sense_mod,
        test_obs="..\n2 passed in 0.10s",
        edit_cmd="sed -i 's/def unrelated_helper/def helper2/' aiomonitor/utils.py",
    )
    decisions = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    assert all(d.emission is None for d in decisions)
    # telemetry still records the candidate as no_trigger (silence telemetry).
    last = decisions[-1].telemetry
    assert any(
        t.candidate_id == "spec.obligation.0" and t.suppressed_reason == "no_trigger" for t in last
    )


def test_obligation_fires_once_not_every_review_turn(sense_mod, oracle_mod):
    """persistent_until_met is edge-triggered: one fire at the transition, no
    re-fire while the phase simply stays REVIEW."""
    turns = _python_fixture(sense_mod, test_obs="..\n2 passed in 0.10s")
    turns += [
        _turn(sense_mod, "git log --oneline -3", "abc fix"),
        _turn(sense_mod, "cat README.md", "readme"),
    ]
    decisions = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    fired = [d for d in decisions if d.emission is not None]
    assert len(fired) == 1 and fired[0].turn_index == 4


# ---------------------------------------------------------------------------
# Generalization (LEG 1): the same predicate fires on a rust surface form.
# ---------------------------------------------------------------------------
def test_rust_shape_fires_same_predicate(sense_mod, oracle_mod):
    obl = {
        "verbatim_text": "You must add `impl Trace for EvaluationHandle`.",
        "kind": "behavior",
        "symbols": ["EvaluationHandle"],
        "keywords": ["must"],
        "checkable_forms": ["impl Trace for EvaluationHandle"],
    }
    turns = [
        _turn(sense_mod, "cat core/engine/src/job.rs", "struct EvaluationHandle;"),
        _turn(
            sense_mod,
            "sed -i 's/struct EvaluationHandle;/struct EvaluationHandle { x: u8 }/' "
            "core/engine/src/job.rs",
            "",
        ),
        _turn(
            sense_mod,
            "timeout 120 cargo test --lib other_module",
            "test result: ok. 3 passed; 0 failed",
        ),
        _turn(sense_mod, "ls core/engine/src", "job.rs"),
        _turn(sense_mod, "git diff --stat", "1 file changed"),
    ]
    decisions = oracle_mod.replay(turns, obligations=[obl])
    fired = [d for d in decisions if d.emission is not None]
    assert len(fired) == 1
    assert fired[0].emission.reason_string == "test_evidence_gap"
    assert "EvaluationHandle" in fired[0].emission.payload_text


# ---------------------------------------------------------------------------
# Distribution floor over the triggered pool: weak obligation suppressed.
# ---------------------------------------------------------------------------
def test_weak_obligation_suppressed_below_floor(sense_mod, oracle_mod):
    strong = _OBL_ASYNC  # 1/1 symbol parts touched -> conf 1.0
    weak = {
        "verbatim_text": "The exporter must serialize snapshots, diffs and indexes.",
        "kind": "behavior",
        # 3 symbol parts, only one (capture_snapshot) will appear in the edit cmd
        "symbols": ["capture_snapshot", "serialize_diffs", "serialize_indexes"],
        "keywords": ["must"],
        "checkable_forms": [],
    }
    turns = _python_fixture(sense_mod, test_obs="..\n2 passed in 0.10s")
    decisions = oracle_mod.replay(turns, obligations=[strong, weak])
    fired = [d for d in decisions if d.emission is not None]
    assert len(fired) == 1
    assert fired[0].emission.candidate_id == "spec.obligation.0"
    tel = {t.candidate_id: t for t in fired[0].telemetry}
    assert tel["spec.obligation.1"].suppressed_reason == "below_floor"
    # 8dp-meaningful: the weak conf is the exact overlap ratio 1/3.
    assert tel["spec.obligation.1"].confidence == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------
# DRIFT class: whitelist-only AND uncertified -> produced, never emitted.
# ---------------------------------------------------------------------------
def test_drift_uncertified_never_emits(sense_mod, oracle_mod):
    """The edit touches the obligation symbol WITHOUT the checkable 'async'
    surface form -> a drift candidate is produced and telemetry-recorded, but it
    NEVER emits while the class is uncertified (plan §11 / Stage-3 constraint)."""
    assert oracle_mod.DRIFT_CERTIFIED is False
    turns = [
        _turn(sense_mod, "cat aiomonitor/monitor.py", "def capture_snapshot(self): ..."),
        # touches the symbol, no 'async' anywhere in the edit evidence:
        _turn(
            sense_mod,
            "sed -i 's/def capture_snapshot(self)/def capture_snapshot(self, name)/' "
            "aiomonitor/monitor.py",
            "",
        ),
    ]
    decisions = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    drift_records = [
        t for d in decisions for t in d.telemetry if t.candidate_id.startswith("spec.drift.")
    ]
    assert drift_records, "drift candidate was not produced/telemetry-recorded"
    assert all(t.suppressed_reason == "drift_uncertified" for t in drift_records)
    assert all(d.emission is None or "drift" not in d.emission.reason_string for d in decisions)


def test_drift_not_produced_when_form_satisfied(sense_mod, oracle_mod):
    """The edit carries the 'async' surface form -> no surface contradiction ->
    no drift candidate at the edit turn (correct-or-quiet)."""
    turns = [
        _turn(
            sense_mod,
            "sed -i 's/def capture_snapshot/async def capture_snapshot/' aiomonitor/monitor.py",
            "",
        ),
    ]
    decisions = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    drift_records = [
        t for d in decisions for t in d.telemetry if t.candidate_id.startswith("spec.drift.")
    ]
    assert drift_records == []


# ---------------------------------------------------------------------------
# Pure single-turn path agrees with the streamed replay (statelessness holds
# with obligations in the pool).
# ---------------------------------------------------------------------------
def test_pure_decide_matches_replay_with_obligations(sense_mod, oracle_mod):
    turns = _python_fixture(sense_mod, test_obs="..\n2 passed in 0.10s")
    streamed = oracle_mod.replay(turns, obligations=[_OBL_ASYNC, _OBL_RETURNS])
    for k in range(len(turns)):
        pure = oracle_mod.oracle_decide(turns, k, obligations=[_OBL_ASYNC, _OBL_RETURNS])
        s = streamed[k]
        assert (pure.emission is None) == (s.emission is None)
        if pure.emission is not None:
            assert pure.emission.payload_text == s.emission.payload_text
            assert pure.emission.candidate_id == s.emission.candidate_id


def test_replay_deterministic_with_obligations(sense_mod, oracle_mod):
    turns = _python_fixture(sense_mod, test_obs="..\n2 passed in 0.10s")
    a = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    b = oracle_mod.replay(turns, obligations=[_OBL_ASYNC])
    sa = [(d.turn_index, None if d.emission is None else d.emission.payload_text) for d in a]
    sb = [(d.turn_index, None if d.emission is None else d.emission.payload_text) for d in b]
    assert sa == sb


# ---------------------------------------------------------------------------
# Frozen-corpus regression: the L5 family is unchanged by the obligation class;
# the proven-consumed boa fire is preserved.
# ---------------------------------------------------------------------------
def _trajs() -> list[str]:
    import glob

    return sorted(glob.glob(_TRAJ_GLOB))


_GT_BLOCK_RE = re.compile(r"<gt-[a-z-]+[^>]*>.*?</gt-[a-z-]+>", re.S)


def _issue_text(messages: list[dict]) -> str:
    """The task text the agent saw, with GT's own injected blocks removed."""
    for m in messages:
        if m.get("role") == "user":
            return _GT_BLOCK_RE.sub("", m.get("content") or "")
    return ""


def test_corpus_l5_reasons_preserved_with_obligations(sense_mod, oracle_mod):
    trajs = _trajs()
    if not trajs:
        pytest.skip("tenpack corpus not present")
    sys.path.insert(0, str(_ROOT / "src"))
    from groundtruth.pretask.spec import extract_spec

    l5_reasons = {"loop", "scaffold_trap", "failure_persisted", "no_test_evidence"}
    boa_seen = False
    for tj in trajs:
        messages = json.load(open(tj, encoding="utf-8", errors="replace"))["messages"]
        turns = sense_mod.load_trajectory(tj)
        obls = extract_spec(_issue_text(messages)).to_serializable()
        plain = [r for r in oracle_mod.replay_nudge_reasons(turns) if r in l5_reasons]
        withobl_all = [
            d.emission.reason_string
            for d in oracle_mod.replay(turns, obligations=obls)
            if d.emission is not None
        ]
        withobl_l5 = [r for r in withobl_all if r in l5_reasons]
        # The L5 family must be byte-stable under the new candidate class.
        assert withobl_l5 == plain, f"{tj}: L5 drift {plain} -> {withobl_l5}"
        if "boa-hierarchical" in tj.replace("\\", "/"):
            assert "no_test_evidence" in withobl_l5  # the consumed class survives
            boa_seen = True
    assert boa_seen

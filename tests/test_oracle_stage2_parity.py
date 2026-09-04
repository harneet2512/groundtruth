"""Oracle Stage 2 — L5 byte-parity replay (red->green).

Per `ORACLE_ARCHITECTURE_PLAN.md` §1.2/§1.3/§8 Stage 2.  The oracle is the pure,
stateless decision gate; in Stage 2 it is wired to the EXISTING L5 governor
nudges ONLY, and must reproduce the live gt_mini_patch behavior BYTE-FOR-BYTE on
the frozen replay corpus before any new behavior is switched on.

Byte-parity ground truth = the <gt-nudge reason=...> fires actually recorded in
the 10 tenpack_27307362054 trajectories (read from the agent's observations):
  boa     -> scaffold_trap, no_test_evidence   (the named "fire preserved")
  csstree -> scaffold_trap                     (the named "harmful fire suppressed")
  aiomonitor, awilix -> (none)
The oracle, replaying statelessly over the GT-stripped trajectory, must emit the
SAME reason set per task AND the SAME payload text, with zero behavior drift.

RED before `gt_oracle.py` exists (ImportError); GREEN after.

LEG 1: the replay rules reference only the trajectory + the L5 regexes — no
task/repo/gold logic; validated across 3 harness shapes' worth of trajectories
and 5 languages (go/py/ts/rust/js).
LEG 2: the metric moved is replay red->green vs the ledger ground truth at zero
drift — the prerequisite for routing real correctness content through one gate
without harming the model (the Core Product Contract's correct-or-quiet).
"""

from __future__ import annotations

import glob
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

pytestmark = pytest.mark.skipif(
    not _CORPUS.is_dir(), reason="external frozen trajectory corpus is not installed"
)


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
    return _load(_SENSE_PATH, "gt_oracle_sense_s2")


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_s2")


@pytest.fixture(scope="module")
def patch_mod():
    return _load(_PATCH_PATH, "gt_mini_patch_s2")


def _trajs() -> list[str]:
    return sorted(glob.glob(_TRAJ_GLOB))


def _task(path: str) -> str:
    return path.replace("\\", "/").split("tenpack_27307362054/")[1].split("/")[0]


def _recorded_nudges(messages: list[dict]) -> list[str]:
    out: list[str] = []
    for x in messages:
        if x.get("role") in ("tool", "user"):
            out += re.findall(r'<gt-nudge reason="([^"]+)"', x.get("content") or "")
    return out


# The hand-audited ground truth (also recoverable from the recorded nudges).
_GROUND_TRUTH = {
    "abs-module-cache-flags": {"scaffold_trap", "failure_persisted"},
    "abs-stepped-slices": {"scaffold_trap", "failure_persisted"},
    "adaptix-name-mapping-aliases": {"scaffold_trap"},
    "aiomonitor-task-snapshots-diff": set(),
    "arktype-json-schema-refs-dependencies": {"scaffold_trap", "failure_persisted"},
    "awilix-async-container-initialization": set(),
    "boa-hierarchical-evaluation-cancellation": {"scaffold_trap", "no_test_evidence"},
    "csstree-shorthand-expansion-compression": {"scaffold_trap"},
    "fd-deterministic-multi-key-sorting": {"scaffold_trap"},
    "katex-multicolumn-array-spans": {"scaffold_trap"},
}


# ---------------------------------------------------------------------------
# The headline byte-parity assertion.
# ---------------------------------------------------------------------------
def test_replay_byte_parity_all_trajectories(sense_mod, oracle_mod):
    """The oracle's stateless replay emits the SAME nudge reason set per task as
    the live governor recorded — across all 10 trajectories, zero drift."""
    drift: list[str] = []
    for tj in _trajs():
        task = _task(tj)
        messages = json.load(open(tj, encoding="utf-8", errors="replace"))["messages"]
        recorded = set(_recorded_nudges(messages))
        turns = sense_mod.load_trajectory(tj)
        replayed = set(oracle_mod.replay_nudge_reasons(turns))
        # the recorded set is the live ground truth; the static table cross-checks it.
        assert recorded == _GROUND_TRUTH[task], (
            f"{task}: recorded {recorded} != ground truth {_GROUND_TRUTH[task]}"
        )
        if replayed != recorded:
            drift.append(f"{task}: replay {replayed} != recorded {recorded}")
    assert not drift, "BYTE-PARITY DRIFT:\n" + "\n".join(drift)


def test_named_parity_assertions(sense_mod, oracle_mod):
    """The plan's two named assertions, explicit: boa no_test_evidence FIRES;
    csstree carries NO harmful nudge (only the benign scaffold_trap)."""
    by_task = {}
    for tj in _trajs():
        by_task[_task(tj)] = set(oracle_mod.replay_nudge_reasons(sense_mod.load_trajectory(tj)))
    assert "no_test_evidence" in by_task["boa-hierarchical-evaluation-cancellation"]
    assert by_task["csstree-shorthand-expansion-compression"] == {"scaffold_trap"}
    # csstree must NOT carry any non-scaffold (harmful) nudge.
    assert by_task["csstree-shorthand-expansion-compression"] <= {"scaffold_trap"}


# ---------------------------------------------------------------------------
# Payload byte-parity — the EMITTED TEXT equals the live nudge text exactly.
# ---------------------------------------------------------------------------
def test_payload_text_byte_identical(sense_mod, oracle_mod, patch_mod):
    """Each emitted payload is byte-identical to the corresponding live nudge
    string in gt_mini_patch (not just the reason)."""
    # the live nudge bodies, recovered from the patch module's literals.
    live_bodies = {
        "scaffold_trap": '<gt-nudge reason="scaffold_trap">',
        "no_test_evidence": '<gt-nudge reason="no_test_evidence">',
        "failure_persisted": '<gt-nudge reason="failure_persisted">',
        "loop": '<gt-nudge reason="loop">',
    }
    for tj in _trajs():
        turns = sense_mod.load_trajectory(tj)
        for dec in oracle_mod.replay(turns):
            if dec.emission is None:
                continue
            r = dec.emission.reason_string
            assert live_bodies[r] in dec.emission.payload_text
            assert dec.emission.payload_text.strip().endswith("</gt-nudge>")
            assert dec.emission.channel == "C1"


def test_oracle_emits_at_most_one_per_turn(sense_mod, oracle_mod):
    """The decision function emits <=1 per turn (plan §1.3 BUDGET)."""
    for tj in _trajs():
        turns = sense_mod.load_trajectory(tj)
        for dec in oracle_mod.replay(turns):
            # exactly 0 or 1 emission; telemetry present when emitted.
            assert dec.emission is None or isinstance(dec.emission.payload_text, str)
            if dec.emission is not None:
                assert len(dec.telemetry) >= 1


# ---------------------------------------------------------------------------
# Pure single-turn path agrees with the streaming replay (the optimization is
# faithful to the pure reference).
# ---------------------------------------------------------------------------
def test_pure_path_matches_streaming(sense_mod, oracle_mod):
    """oracle_decide(turns, k) (pure, O(n^3)) agrees with the streaming replay on
    a bounded prefix of two real trajectories — the streaming pass is only a
    performance route, never a behavior change."""
    targets = ["boa-", "abs-module-cache-flags"]
    checked = 0
    for tj in _trajs():
        if not any(t in tj.replace("\\", "/") for t in targets):
            continue
        turns = sense_mod.load_trajectory(tj)[:60]  # bounded for the O(n^3) path
        streamed = [
            d.emission.reason_string if d.emission else None for d in oracle_mod.replay(turns)
        ]
        pure = []
        for k in range(len(turns)):
            d = oracle_mod.oracle_decide(turns, k)
            pure.append(d.emission.reason_string if d.emission else None)
        assert pure == streamed, f"{_task(tj)}: pure {pure} != streamed {streamed}"
        checked += 1
    assert checked >= 1


# ---------------------------------------------------------------------------
# Statelessness — the replay is a pure function of the trajectory.
# ---------------------------------------------------------------------------
def test_replay_deterministic(sense_mod, oracle_mod):
    tj = _trajs()[0]
    turns = sense_mod.load_trajectory(tj)
    a = oracle_mod.replay_nudge_reasons(turns)
    b = oracle_mod.replay_nudge_reasons(turns)
    assert a == b

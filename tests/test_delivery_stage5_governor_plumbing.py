"""Delivery engine STAGE 5 — governor FP closure + plumbing (red->green).

(a) failure_persisted false positives (run 27321848581: 3/3 FP) — LIPI-proven
    root cause on the frozen fd trajectory: `\\b\\d+ failed\\b` matched the
    fully GREEN line `test result: ok. 106 passed; 0 failed; …` — two green
    runs made an identical "failure" signature -> "persisted" -> fired right
    after a 106/106 pass.  Closures (all suppress-only, correct-or-quiet):
      1. zero-count results are never failure lines (_TEST_FAIL_STRICT_RE);
      2. patch/apply bookkeeping (`Hunk #N FAILED`, `error: patch failed:`)
         is never a failure line (_PATCH_NOISE_RE);
      3. baseline failures — observed before any source edit OR inside a
         `git stash` window (incl. the dominant SINGLE-TURN disproof shape
         `git stash && test … ; git stash pop`) — never drive the nudge.
    The replay twin gets the same semantics under parity_mode=False;
    parity_mode=True stays BYTE-IDENTICAL to the frozen corpus (the Stage-2
    contract is never drifted).

(c) heredoc edit detection — the live oracle route must count heredoc and
    python-heredoc writes as source edits (the csstree `edits: 0.0` class).

(d) --ae plumbing — every host-set GT env var the in-container patch/agent
    reads must be forwarded explicitly (the P0 that mislabeled the first-3
    run: GT_SELF_VERIFY_ATTEMPTS was set on the host and never forwarded).
"""

from __future__ import annotations

import glob
import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_ORACLE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle.py"
_WF_PATH = _ROOT / ".github" / "workflows" / "deepswe_full.yml"
_FP_CORPUS = _ROOT / ".claude" / "reports" / "runs" / "oracle_tenpack_27321848581"


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
def gmp():
    return _load(_PATCH_PATH, "gt_mini_patch_stage5")


@pytest.fixture(scope="module")
def sense_mod():
    return _load(_SENSE_PATH, "gt_oracle_sense_stage5")


@pytest.fixture(scope="module")
def oracle_mod():
    return _load(_ORACLE_PATH, "gt_oracle_stage5")


def _reset_failure_state(gmp, monkeypatch):
    monkeypatch.setattr(gmp, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(gmp, "_l5_failure_fired", False, raising=False)
    monkeypatch.setattr(gmp, "_test_fail_history", [], raising=False)
    monkeypatch.setattr(gmp, "_baseline_fail_sigs", set(), raising=False)
    monkeypatch.setattr(gmp, "_stash_depth", 0, raising=False)
    monkeypatch.setattr(gmp, "_source_edit_count", 1, raising=False)


_GREEN_CARGO = (
    "running 106 tests\n"
    "test result: ok. 106 passed; 0 failed; 0 ignored; 0 measured; "
    "0 filtered out; finished in 1.44s\n"
)
_REAL_FAIL = "--- FAIL: TestEnv (0.00s)\n--- FAIL: TestCommand (0.08s)\nFAIL\n"
_PATCH_NOISE = (
    "Checking patch src/walk.rs...\n"
    "error: patch failed: src/walk.rs:24\n"
    "Hunk #2 FAILED at 120.\n"
    "1 out of 3 hunks FAILED\n"
)


# ---------------------------------------------------------------------------
# (a)-1: zero-count green lines are never failure lines — THE fd FP.
# ---------------------------------------------------------------------------
class TestZeroCountClosure:
    def test_failure_lines_exclude_zero_counts(self, gmp):
        assert gmp._failure_lines(_GREEN_CARGO) == []

    def test_failure_lines_keep_real_failures(self, gmp):
        assert len(gmp._failure_lines(_REAL_FAIL)) >= 2

    def test_fd_green_run_never_fires(self, gmp, monkeypatch):
        """Two consecutive fully-green cargo runs must NOT 'persist'."""
        _reset_failure_state(gmp, monkeypatch)
        for _ in range(3):
            out = gmp._l5_failure_nudge("cargo test 2>&1 | tail -5", _GREEN_CARGO)
        assert out == "", f"green-run FP reopened: {out!r}"

    def test_pivot_recency_not_set_by_green_run(self, gmp, monkeypatch):
        """A green `0 failed` line must not flip _last_test_outcome_failed
        (the false-pivot channel)."""
        for name, val in [
            ("_GT_BASELINE", False),
            ("_ORACLE_ROUTE", True),
            ("_marker_sent", True),
            ("_action_count", 0),
            ("_last_test_outcome_failed", False),
            ("_oracle_edited_rels", set()),
            ("_oracle_nonedit_streak", 0),
            ("_oracle_edited_tokens", set()),
            ("_oracle_tested_tokens", set()),
            ("_oracle_edited_tokens_by_file", {}),
            ("_edit_churn", {}),
            ("_traj_state_keys", []),
            ("_traj_loop_sigs", []),
            ("_lr_history", []),
            ("_nsr_history", []),
            ("_oracle_focus_cache", None),
            ("_oracle_delivered_hashes", set()),
            ("_cycle_edit_start", None),
            ("_test_cycle_spans", []),
        ]:
            monkeypatch.setattr(gmp, name, val, raising=False)
        out = {"output": _GREEN_CARGO, "returncode": 0}
        gmp._augment_output({"command": "cargo test 2>&1 | tail -5"}, out)
        assert gmp._last_test_outcome_failed is False


# ---------------------------------------------------------------------------
# (a)-2: patch/apply bookkeeping is never a failure line.
# ---------------------------------------------------------------------------
class TestPatchNoiseClosure:
    def test_patch_noise_lines_excluded(self, gmp):
        assert gmp._failure_lines(_PATCH_NOISE) == []

    def test_mixed_output_keeps_only_real_failures(self, gmp):
        fl = gmp._failure_lines(_PATCH_NOISE + _REAL_FAIL)
        assert fl and all("Hunk" not in ln and "patch failed" not in ln for ln in fl)


# ---------------------------------------------------------------------------
# (a)-3: baseline failures never "persist".
# ---------------------------------------------------------------------------
class TestBaselineClosure:
    def test_pre_edit_failure_never_fires(self, gmp, monkeypatch):
        """A failure observed BEFORE any source edit is the repo's own state;
        recurring later it still never fires."""
        _reset_failure_state(gmp, monkeypatch)
        monkeypatch.setattr(gmp, "_source_edit_count", 0, raising=False)
        out = gmp._l5_failure_nudge("go test ./evaluator/...", _REAL_FAIL)
        assert out == ""
        monkeypatch.setattr(gmp, "_source_edit_count", 5, raising=False)
        for _ in range(3):
            out = gmp._l5_failure_nudge("go test ./evaluator/...", _REAL_FAIL)
        assert out == "", "pre-existing failure drove failure_persisted"

    def test_single_turn_stash_disproof_shields_signature(self, gmp, monkeypatch):
        """The abs shape: `git stash && go test … ; git stash pop` proves the
        failure pre-exists — the SAME signature must never fire afterwards."""
        _reset_failure_state(gmp, monkeypatch)
        # one post-edit observation (count 1 — no fire yet)
        assert gmp._l5_failure_nudge("go test ./evaluator/...", _REAL_FAIL) == ""
        # the agent disproves it in a single-turn stash window
        assert (
            gmp._l5_failure_nudge(
                "git stash && go test ./evaluator/... ; git stash pop", _REAL_FAIL
            )
            == ""
        )
        # recurrence after the disproof: shielded forever
        for _ in range(3):
            out = gmp._l5_failure_nudge("go test ./evaluator/...", _REAL_FAIL)
        assert out == "", "stash-disproven failure drove failure_persisted"

    def test_genuine_persistent_failure_still_fires(self, gmp, monkeypatch):
        """The TRUE-positive path is preserved: a NEW failure appearing only
        after edits, recurring, still fires."""
        _reset_failure_state(gmp, monkeypatch)
        out1 = gmp._l5_failure_nudge(
            "pytest tests/ -q", "FAILED tests/t.py::test_new - AssertionError\n1 failed\n"
        )
        assert out1 == ""
        out2 = gmp._l5_failure_nudge(
            "pytest tests/ -q", "FAILED tests/t.py::test_new - AssertionError\n1 failed\n"
        )
        assert 'reason="failure_persisted"' in out2


# ---------------------------------------------------------------------------
# (a)-4: the replay twin — corrected under parity_mode=False, byte-parity
# under parity_mode=True (the frozen Stage-2 contract).
# ---------------------------------------------------------------------------
def _fp_traj(task_prefix: str) -> str | None:
    hits = sorted(
        glob.glob(
            str(
                _FP_CORPUS
                / f"{task_prefix}*"
                / "jobs"
                / "*"
                / "*"
                / "agent"
                / "mini-swe-agent.trajectory.json"
            )
        )
    )
    return hits[0] if hits else None


class TestReplayTwin:
    @pytest.mark.parametrize(
        "task,fp_suppressed",
        [
            ("fd-deterministic", True),  # green-run zero-count FP
            ("abs-module", True),  # stash-disproof baseline FP
            ("abs-stepped", False),  # fires 1 turn BEFORE the disproof —
            # unknowable at fire time (honest residual)
        ],
    )
    def test_corrected_mode_kills_proven_fps(self, sense_mod, oracle_mod, task, fp_suppressed):
        tj = _fp_traj(task)
        if tj is None:
            pytest.skip("FP corpus not present")
        turns = sense_mod.load_trajectory(tj)
        parity = set(oracle_mod.replay_nudge_reasons(turns, parity_mode=True))
        corrected = set(oracle_mod.replay_nudge_reasons(turns, parity_mode=False))
        assert "failure_persisted" in parity  # the historical (FP) behavior
        if fp_suppressed:
            assert "failure_persisted" not in corrected, (
                f"{task}: the proven FP must be suppressed in corrected mode"
            )
        # corrected mode NEVER adds new fires (suppress-only):
        assert corrected <= parity

    def test_boa_true_positive_preserved_in_corrected_mode(self, sense_mod, oracle_mod):
        tj = _fp_traj("boa-")
        if tj is None:
            pytest.skip("FP corpus not present")
        turns = sense_mod.load_trajectory(tj)
        corrected = set(oracle_mod.replay_nudge_reasons(turns, parity_mode=False))
        assert "no_test_evidence" in corrected  # the proven-consumed class survives


# ---------------------------------------------------------------------------
# (c) heredoc edit detection on the LIVE oracle route.
# ---------------------------------------------------------------------------
class TestHeredocEdits:
    def _reset(self, gmp, monkeypatch):
        for name, val in [
            ("_GT_BASELINE", False),
            ("_ORACLE_ROUTE", True),
            ("_marker_sent", True),
            ("_action_count", 0),
            ("_source_edit_count", 0),
            ("_oracle_edited_rels", set()),
            ("_oracle_nonedit_streak", 0),
            ("_oracle_edited_tokens", set()),
            ("_oracle_edited_tokens_by_file", {}),
            ("_edit_churn", {}),
            ("_contract_seen", set()),
            ("_seen", set()),
            ("_cochange_fired", False),
            ("_oracle_focus_cache", None),
            ("_oracle_delivered_hashes", set()),
            ("_traj_state_keys", []),
            ("_traj_loop_sigs", []),
            ("_lr_history", []),
            ("_nsr_history", []),
            ("_cycle_edit_start", None),
            ("_last_test_outcome_failed", False),
            ("_cmd_history", []),
            ("_l5_fired", False),
            ("_l5_failure_fired", False),
            ("_l5_notest_fired", False),
            ("_test_fail_history", []),
            ("_blind_test_runs", 0),
            ("_test_evidence_seen", False),
            ("_consensus_fired", False),
            ("_consensus_scope", set()),
        ]:
            monkeypatch.setattr(gmp, name, val, raising=False)

    def test_heredoc_cat_write_counts_as_edit(self, gmp, monkeypatch, tmp_path):
        self._reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        out = {"output": "", "returncode": 0}
        gmp._augment_output(
            {
                "command": "cat > lib/lexer/Lexer.js << 'EOF'\nclass Lexer { expandShorthand() {} }\nEOF"
            },
            out,
        )
        assert gmp._source_edit_count == 1
        assert "lib/lexer/Lexer.js" in gmp._oracle_edited_rels

    def test_python_heredoc_open_write_counts_as_edit(self, gmp, monkeypatch, tmp_path):
        """The csstree shape: `python3 << EOF` rewriting a source file via
        open(...,'w') INSIDE the heredoc body (measured `edits: 0.0` live)."""
        self._reset(gmp, monkeypatch)
        monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp_path / "ev.jsonl"))
        out = {"output": "", "returncode": 0}
        gmp._augment_output(
            {
                "command": "python3 << 'EOF'\nsrc = open('lib/lexer/Lexer.js').read()\n"
                "open('lib/lexer/Lexer.js', 'w').write(src.replace('a', 'b'))\nEOF"
            },
            out,
        )
        assert gmp._source_edit_count == 1
        assert "lib/lexer/Lexer.js" in gmp._oracle_edited_rels


# ---------------------------------------------------------------------------
# (d) --ae plumbing: every host-set GT var reaches the container explicitly.
# ---------------------------------------------------------------------------
_REQUIRED_AE_KEYS = [
    # substrate consume (pre-existing)
    "GT_HOST_GRAPH_DB",
    "GT_CERT_DIR",
    "GT_HOST_SRC_ROOT",
    "GT_PORTABLE_SUBSTRATE",
    "GT_FORBID_PREBUILT_GRAPH",
    "GT_PROOF_MODE",
    "GT_CONTAINERIZED",
    "GT_RUNTIME_STRATEGY",
    # verification horizon (pre-existing)
    "GT_STEP_LIMIT",
    "GT_VERIFICATION_CYCLE_COST",
    # Stage-5 closures: the audit's named P0 (set on host, never forwarded)
    "GT_SELF_VERIFY_ATTEMPTS",
    # oracle kill switch + Stage-4 escalation calibration channel
    "GT_ORACLE_ROUTE",
    "GT_ESC_ADV_TCOV",
    "GT_ESC_ADV_B",
    "GT_ESC_URG_TCOV",
    "GT_ESC_URG_B",
    "GT_ESC_GATE_TCOV",
    "GT_ESC_GATE_KV",
    # FIX 3 (2026-06-11): oracle suppression-telemetry harvest path (writable
    # /gt_out mount) — without it the 8-dp decision record dies in-container.
    "GT_ORACLE_EVENTS",
]


def test_workflow_forwards_every_gt_env_via_ae():
    text = _WF_PATH.read_text(encoding="utf-8") + (
        _ROOT / "artifact_deepswe" / "gt_integration" / "gt_ae_block.sh"
    ).read_text(encoding="utf-8")
    missing = [k for k in _REQUIRED_AE_KEYS if not re.search(rf"--ae [\"']?{re.escape(k)}=", text)]
    assert not missing, (
        "GT env vars set on the host but NOT forwarded into the task "
        f"container via --ae (the Stage-C-killing P0 class): {missing}"
    )

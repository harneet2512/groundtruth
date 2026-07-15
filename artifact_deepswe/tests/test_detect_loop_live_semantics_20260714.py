"""Focused live-seam pins for truthful detect.loop semantics."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


_PATCH = Path(__file__).resolve().parents[1] / "gt_mini_patch.py"


@pytest.fixture(scope="module")
def g():
    previous = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("gt_loop_live_semantics", _PATCH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = previous


def _reset(g, monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    monkeypatch.setattr(g, "_traj_state_keys", [])
    monkeypatch.setattr(g, "_traj_loop_sigs", [])
    monkeypatch.setattr(g, "_lr_history", [])
    monkeypatch.setattr(g, "_nsr_history", [])
    monkeypatch.setattr(g, "_detect_loop_fired", False)
    monkeypatch.setattr(g, "_detect_loop_epoch_step", -1)
    monkeypatch.setattr(g, "_ss_edit_events", [])


def test_full_raw_output_digest_distinguishes_progress_after_long_prefix(g, monkeypatch):
    _reset(g, monkeypatch)
    prefix = "shared build preamble\n" + ("x" * 800)
    seen = [
        g._degenerate_loop_candidate("pytest -q", prefix + f"\ncase {i} passed")
        for i in range(40)
    ]
    assert all(candidate is None for candidate in seen)
    assert len(set(g._traj_loop_sigs)) == 40


def test_state_key_distinguishes_different_edits_to_same_file(g):
    first = g._behavior_state_key("sed -i 's/a/b/' src/core.py", "")
    second = g._behavior_state_key("sed -i 's/b/c/' src/core.py", "")
    assert first != second


def test_result_identity_includes_explicit_returncode(g):
    assert (
        g._behavior_loop_signature("pytest -q", "same output", 0)
        != g._behavior_loop_signature("pytest -q", "same output", 1)
    )


def test_byte_proven_source_progress_starts_a_new_loop_epoch(g, monkeypatch):
    _reset(g, monkeypatch)
    g._traj_state_keys.extend(["old-state"] * 12)
    g._traj_loop_sigs.extend(["old-signature"] * 3)
    g._lr_history.extend([0.5] * 24)
    g._nsr_history.extend([0.5] * 24)
    g._detect_loop_fired = True
    g._ss_edit_events.append(("src/core.py", 17, True))

    assert g._degenerate_loop_candidate("pytest -q", "1 failed") is None
    assert g._detect_loop_epoch_step == 17
    assert g._detect_loop_fired is False
    # Dynamic trajectory calibration remains warm; only exact repeats are
    # scoped to the new byte-proven source-state epoch.
    assert len(g._traj_state_keys) == 13
    assert len(g._traj_loop_sigs) == 1
    assert len(g._lr_history) == len(g._nsr_history) == 25


def test_failed_or_same_byte_write_does_not_reset_loop_epoch(g, monkeypatch):
    _reset(g, monkeypatch)
    g._traj_state_keys.append("prior-state")
    g._traj_loop_sigs.append("prior-signature")
    g._lr_history.append(0.0)
    g._nsr_history.append(1.0)
    g._ss_edit_events.append(("src/core.py", 17, False))

    assert g._degenerate_loop_candidate("pytest -q", "1 failed") is None
    assert g._detect_loop_epoch_step == -1
    assert len(g._traj_state_keys) == 2
    assert len(g._traj_loop_sigs) == 2


def test_payload_states_only_the_proven_epoch_and_exact_repeat(g, monkeypatch):
    _reset(g, monkeypatch)
    command = "pytest -q"
    output = "same failure"
    signature = g._behavior_loop_signature(command, output)
    state_key = g._behavior_state_key(command, output)
    assert signature is not None
    g._traj_loop_sigs.extend([signature, signature])
    g._traj_state_keys.extend([state_key] * 12)
    g._lr_history.extend([0.0] * 24)
    g._nsr_history.extend([1.0] * 24)

    candidate = g._degenerate_loop_candidate(command, output)
    assert candidate is not None
    payload = candidate[1]
    assert "identical native output and return code 3 times" in payload
    assert "current byte-proven source-state epoch" in payload
    assert "almost no new state" not in payload


def test_genuine_loop_after_progress_fires_without_calibration_rewarm(g, monkeypatch):
    _reset(g, monkeypatch)
    command = "pytest -q"
    output = "same failure"
    # Warm the trajectory's own dynamic baseline and include two old-epoch
    # repeats that must not count after the byte-changing edit.
    for i in range(22):
        assert g._degenerate_loop_candidate(f"cat src/f{i}.py", f"content {i}") is None
    assert g._degenerate_loop_candidate(command, output) is None
    assert g._degenerate_loop_candidate(command, output) is None
    assert len(g._lr_history) == 24

    g._ss_edit_events.append(("src/core.py", 25, True))
    first = g._degenerate_loop_candidate(command, output)
    second = g._degenerate_loop_candidate(command, output)
    third = g._degenerate_loop_candidate(command, output)

    assert first is None and second is None
    assert third is not None
    assert "identical native output and return code 3 times" in third[1]


def test_all_ss_off_retains_legacy_loop_and_state_identity(g, monkeypatch):
    """The replay counterfactual must be byte-identical to the pre-V2 detector."""
    _reset(g, monkeypatch)
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")

    command = "cat privacyidea/lib/eventhandler/requestmangler.py"
    output = "same native source bytes"
    assert g._degenerate_loop_candidate(command, output, 0) is None
    first_signature = g._traj_loop_sigs[-1]
    first_state = g._traj_state_keys[-1]
    assert g._degenerate_loop_candidate(command, output, 1) is None

    # Legacy detect.loop deliberately predates explicit return-code identity.
    assert first_signature == command + "\x00" + g._obs_collapse(output)
    assert g._traj_loop_sigs[-1] == first_signature
    assert first_state == "post_view\x00privacyidea/lib/eventhandler/requestmangler.py"
    assert g._traj_state_keys[-1] == first_state


def test_all_ss_off_retains_legacy_detect_loop_payload(g, monkeypatch):
    _reset(g, monkeypatch)
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")
    command = "cat privacyidea/lib/eventhandler/requestmangler.py"
    output = "same native source bytes"
    signature = command + "\x00" + g._obs_collapse(output)
    state_key = "post_view\x00privacyidea/lib/eventhandler/requestmangler.py"
    g._traj_loop_sigs.extend([signature, signature])
    g._traj_state_keys.extend([state_key] * 12)
    g._lr_history.extend([0.0] * 24)
    g._nsr_history.extend([1.0] * 24)

    candidate = g._degenerate_loop_candidate(command, output, 0)

    assert candidate is not None
    payload = candidate[1]
    body = (
        "GT: you have run this exact command with this exact output 3 times, "
        "and your recent actions are producing almost no new state \u2014 this is "
        "a degenerate loop, not progress. If you edited a compiled binary's source, "
        "REBUILD before re-running; otherwise re-read the last real error and try a "
        "different approach (different file, different hypothesis, or a narrower test)."
    )
    assert payload == g._nudge_native(
        f'\n<gt-nudge reason="degenerate_loop">\n{body}\n</gt-nudge>')

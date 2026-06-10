"""Three DeepSWE delivery-surface fixes — red->green (2026-06-09).

  FIX 1 (pier pin)   : every `datacurve-pier` install in the deepswe workflows is
     pinned to ==0.2.0 — the version the --ae/--mounts-json env+mount plumbing was
     source-verified against. An unpinned install lets an upstream pier release
     silently change the contract mid-run.
  FIX 2 (G1 markers) : the substrate-proof step's §E failure echoes exit the job
     BEFORE the agent step creates trial_output.log, so deepswe_outcome.py's INFRA
     classification (which scans that log) yielded UNKNOWN instead of INFRA. Every
     §E marker echo site in deepswe_full.yml must ALSO append the marker line to
     trial_output.log (`| tee -a trial_output.log`, creates the file if absent),
     line-anchored (the classifier matches line-start), with the CANONICAL token —
     the old task-image echo was "FATAL: task image pull failed" while the marker
     list has TASK_IMAGE_PULL_FAIL.
  FIX 3 (G2 wrap)    : brief.txt (gt_run_proof.emit_brief -> generate_v1r_brief
     .brief_text, v1r_brief.py:1417) already STARTS with <gt-task-brief>; gt_agent's
     instruction assembly wrapped it in the tag AGAIN -> nested duplicate tags in
     the agent prompt. The assembly must consume a pre-tagged brief as-is and wrap
     only when the tag is absent — exactly ONE <gt-task-brief> block either way
     (same invariant tests/preflight/test_brief_delivery_invariants.py pins on the
     OH wrapper side).

All deterministic: workflow-text + classifier behavior + pure assembly helper.
No network, no Go toolchain, no task IDs.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_WF_DIR = _ROOT / ".github" / "workflows"
_FULL_WF = _WF_DIR / "deepswe_full.yml"
_OUTCOME_PATH = _ROOT / "scripts" / "verify" / "deepswe_outcome.py"
_AGENT_PATH = _ROOT / "artifact_deepswe" / "gt_agent.py"

_load_count = 0


def _load(path: Path, name_prefix: str):
    """Fresh module instance per call (module-level state isolated per test)."""
    global _load_count
    _load_count += 1
    name = f"{name_prefix}_{_load_count}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _gt_env_clear(monkeypatch):
    for k in list(os.environ):
        if k.startswith("GT_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def outcome_mod():
    return _load(_OUTCOME_PATH, "deepswe_outcome_uut")


@pytest.fixture
def agent_mod(monkeypatch):
    _gt_env_clear(monkeypatch)
    return _load(_AGENT_PATH, "gt_agent_wrapfix_uut")


# ===========================================================================
# FIX 1 — pier pinned to ==0.2.0 in EVERY deepswe workflow that installs it
# ===========================================================================
_PIER_WORKFLOWS = (
    "deepswe_full.yml",
    "deepswe_trial.yml",
    "deepswe_preindex.yml",
    "deepswe_proof_sweep.yml",  # no pier install today; pinned if one is added
)


def test_fix1_every_pier_install_is_pinned_to_0_2_0():
    """The --ae/--mounts-json integration was source-verified against pier 0.2.0;
    any `pip install datacurve-pier` without ==0.2.0 is a silent contract risk."""
    found_any = False
    for wf_name in _PIER_WORKFLOWS:
        p = _WF_DIR / wf_name
        if not p.is_file():
            continue
        for lineno, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "datacurve-pier" in ln and "install" in ln:
                found_any = True
                assert "datacurve-pier==0.2.0" in ln, (
                    f"UNPINNED pier install at {wf_name}:{lineno}: {ln.strip()!r} "
                    f"— must pin datacurve-pier==0.2.0 (the source-verified "
                    f"--ae/--mounts-json contract version)"
                )
    assert found_any, "no pier install line found in any deepswe workflow (paths moved?)"


# ===========================================================================
# FIX 2 — G1: every §E marker echo site tees the marker line into trial_output.log
# ===========================================================================
def test_fix2_every_infra_marker_echo_site_tees_to_trial_log(outcome_mod):
    """For each canonical INFRA_LOG_MARKERS token: deepswe_full.yml must echo it
    (line-start inside the quoted string) AND pipe that echo through
    `tee -a trial_output.log` so the classifier sees it even when the job exits
    before the agent step creates the log."""
    wf_lines = _FULL_WF.read_text(encoding="utf-8").splitlines()
    for marker in outcome_mod.INFRA_LOG_MARKERS:
        sites = [(i, ln) for i, ln in enumerate(wf_lines, 1)
                 if f'echo "{marker}' in ln]
        assert sites, (
            f"deepswe_full.yml has NO echo site for canonical marker {marker!r} "
            f"(INFRA_LOG_MARKERS expects the workflow to emit this exact token)"
        )
        for lineno, ln in sites:
            assert "tee -a trial_output.log" in ln, (
                f"G1: marker echo at deepswe_full.yml:{lineno} does not append to "
                f"trial_output.log — the classifier scans that file, and this "
                f"failure site exits before the agent step creates it:\n  {ln.strip()}"
            )


def test_fix2_task_image_pull_fail_uses_canonical_token():
    """The audit found TASK_IMAGE_PULL_FAIL in INFRA_LOG_MARKERS while the workflow
    echoed 'FATAL: task image pull failed' — a token the classifier can never match."""
    wf = _FULL_WF.read_text(encoding="utf-8")
    assert 'echo "FATAL: task image pull failed"' not in wf, (
        "G1: non-canonical task-image failure echo still present (classifier "
        "matches TASK_IMAGE_PULL_FAIL, not 'FATAL: ...')"
    )
    assert 'echo "FATAL: task image not present after pull"' not in wf, (
        "G1: non-canonical post-pull inspect failure echo still present"
    )
    assert 'echo "TASK_IMAGE_PULL_FAIL' in wf


def test_fix2_workflow_echoed_strings_classify_infra(outcome_mod):
    """END-TO-END token parity: take the EXACT quoted strings the workflow echoes
    for each marker, feed them to find_infra_markers + build_signal_record, and
    require class INFRA. Proves the workflow emission and the classifier tokens
    can never drift apart silently."""
    wf_lines = _FULL_WF.read_text(encoding="utf-8").splitlines()
    for marker in outcome_mod.INFRA_LOG_MARKERS:
        emitted: list[str] = []
        for ln in wf_lines:
            m = re.search(r'echo "([^"]+)"', ln)
            if m and m.group(1).startswith(marker):
                emitted.append(m.group(1))
        assert emitted, f"no workflow echo string starts with {marker!r}"
        for text in emitted:
            log = f"earlier unrelated output\n{text}\n"
            assert marker in outcome_mod.find_infra_markers(log), (
                f"classifier missed the workflow's own emission for {marker!r}: "
                f"{text!r}"
            )
            rec = outcome_mod.build_signal_record(
                instance_id="task-x", reward=None, n_agent_steps=None,
                exit_status=None, trial_log=log, cert_dir=None,
            )
            assert rec["failure_class"] == "INFRA", (
                f"emitted marker line did not classify INFRA (got "
                f"{rec['failure_class']!r}): {text!r}"
            )


def test_fix2_marker_absent_from_log_stays_unknown(outcome_mod):
    """Negative control (the pre-fix symptom): a substrate failure whose marker
    never reached trial_output.log classifies UNKNOWN — the bug G1 closes."""
    rec = outcome_mod.build_signal_record(
        instance_id="task-x", reward=None, n_agent_steps=None,
        exit_status=None, trial_log="", cert_dir=None,
    )
    assert rec["failure_class"] == "UNKNOWN"


# ===========================================================================
# FIX 3 — G2: exactly ONE <gt-task-brief> block in the assembled instruction
# ===========================================================================
def test_fix3_pretagged_brief_is_not_double_wrapped(agent_mod):
    """brief.txt already starts with <gt-task-brief> (v1r_brief.py:1417 via
    gt_run_proof.emit_brief) -> consume as-is, exactly ONE open + ONE close tag."""
    brief = "<gt-task-brief>\nFOCUS: app/core.py — anchor hit\n</gt-task-brief>"
    out = agent_mod._prepend_brief(brief, "Fix the bug in app/core.py.")
    assert out.count("<gt-task-brief>") == 1, f"nested duplicate open tags:\n{out}"
    assert out.count("</gt-task-brief>") == 1, f"nested duplicate close tags:\n{out}"
    assert "FOCUS: app/core.py" in out
    assert "Fix the bug in app/core.py." in out
    # brief precedes the instruction (the brief is a preamble, not an appendix)
    assert out.index("</gt-task-brief>") < out.index("Fix the bug in app/core.py.")


def test_fix3_untagged_brief_wrapped_exactly_once(agent_mod):
    """A tag-less brief (legacy host generation paths) still gets the wrap — once."""
    out = agent_mod._prepend_brief("plain brief content", "Fix the bug.")
    assert out.count("<gt-task-brief>") == 1
    assert out.count("</gt-task-brief>") == 1
    assert "plain brief content" in out and "Fix the bug." in out


def test_fix3_empty_brief_is_passthrough(agent_mod):
    """No brief -> no tag, instruction untouched (correct-or-quiet)."""
    assert agent_mod._prepend_brief("", "Fix the bug.") == "Fix the bug."
    assert "<gt-task-brief" not in agent_mod._prepend_brief("", "Fix the bug.")


def test_fix3_substrate_brief_end_to_end_single_tag(agent_mod, monkeypatch, tmp_path):
    """Through the REAL consume path: a substrate brief.txt in the canonical
    pre-tagged shape -> _generate_brief -> _prepend_brief -> ONE tag pair."""
    (tmp_path / "brief.txt").write_text(
        "<gt-task-brief>\n1. app/core.py (def run(self):)\n</gt-task-brief>",
        encoding="utf-8",
    )
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    brief = agent_mod._generate_brief("fix the bug")
    out = agent_mod._prepend_brief(brief, "fix the bug")
    assert out.count("<gt-task-brief>") == 1, f"double-wrapped substrate brief:\n{out}"
    assert out.count("</gt-task-brief>") == 1


def test_fix3_run_routes_through_prepend_brief(agent_mod):
    """Integration avenue: GTMiniSweAgent.run must assemble via _prepend_brief —
    no residual inline wrap that could reintroduce the double tag."""
    import inspect
    src = inspect.getsource(agent_mod.GTMiniSweAgent.run)
    assert "_prepend_brief" in src, "run() no longer routes through _prepend_brief"
    assert '<gt-task-brief>\\n{brief}' not in src, (
        "run() still carries the unconditional inline wrap"
    )

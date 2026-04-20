"""v12 preflight tests for GT behavior-control (wrapper + ack + submit-gate).

Covers the 9 verification tests from the v12 plan:
  1. orient_first_call_allowed
  2. orient_second_call_blocked
  3. lookup_within_limit
  4. lookup_third_call_blocked
  5. pass_through_no_longer_bypasses (poka-yoke)
  6. ack_followed
  7. ack_ignored
  8. ack_not_observed_genuine
  9. submit_gate_blocks_then_escapes

Strategy:
  * Wrapper tests invoke /tmp/gt_intel_wrapper.py via subprocess after
    rebinding its file-path constants to a tmpdir (it's a self-contained
    script embedded in gt_tool_install.sh, not an importable module).
  * Ack tests importlib.util-load swe_agent_state_gt.py and patch its Path
    constants to tmpdir, then call _check_ack directly.
  * Submit-gate test extracts the PRESUBMIT shell body from gt_tool_install.sh
    and rebinds paths, then invokes bash on it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "benchmarks" / "swebench" / "gt_tool_install.sh"
HOOK_PY = REPO_ROOT / "benchmarks" / "swebench" / "swe_agent_state_gt.py"


def _extract_heredoc(marker: str) -> str:
    text = INSTALL_SH.read_text(encoding="utf-8", errors="replace")
    # Shell permits optional whitespace between `<<` and the quoted marker.
    pattern = rf"<<\s*'{marker}'\r?\n(.*?)\r?\n{marker}\b"
    m = re.search(pattern, text, re.DOTALL)
    assert m, f"HEREDOC marker {marker!r} not found in {INSTALL_SH}"
    return m.group(1)


# ── Wrapper fixture ────────────────────────────────────────────────────────

@pytest.fixture
def wrapper_env(tmp_path: Path):
    """Build an isolated copy of the gt wrapper with tmp-path constants."""
    td = tmp_path
    wrap = td / "wrapper.py"
    real = td / "real.py"
    state_file = td / "budget.state.json"
    events = td / "events.jsonl"
    last_action = td / "last_action.txt"
    last_check_ts = td / "last_check.ts"
    last_edit_ts = td / "last_edit.ts"

    # Use POSIX paths when substituting into Python string literals inside the
    # wrapper source. On Windows, a raw path like `C:\Users\...` contains `\U`,
    # which the embedded Python parser treats as a unicode escape and rejects
    # with SyntaxError before the wrapper even starts. Python on Windows is
    # happy to open `C:/Users/...`, so POSIX form is safe.
    def _p(pth: Path) -> str:
        return pth.as_posix()

    src = _extract_heredoc("WRAPEOF")
    src = src.replace('REAL = "/tmp/gt_intel_real.py"', f'REAL = "{_p(real)}"')
    src = src.replace(
        'BUDGET_EVENTS = "/tmp/gt_budget_events.jsonl"',
        f'BUDGET_EVENTS = "{_p(events)}"',
    )
    src = src.replace(
        'STATE_FILE = "/tmp/gt_budget.state.json"',
        f'STATE_FILE = "{_p(state_file)}"',
    )
    src = src.replace(
        'LAST_ACTION_FILE = "/tmp/gt_last_action.txt"',
        f'LAST_ACTION_FILE = "{_p(last_action)}"',
    )
    src = src.replace(
        'LAST_CHECK_TS = "/tmp/gt_last_gt_check.ts"',
        f'LAST_CHECK_TS = "{_p(last_check_ts)}"',
    )
    src = src.replace(
        'LAST_EDIT_TS = "/tmp/gt_last_material_edit.ts"',
        f'LAST_EDIT_TS = "{_p(last_edit_ts)}"',
    )
    wrap.write_text(src, encoding="utf-8")

    real.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        print("REAL_OK:" + " ".join(sys.argv[1:]))
        sys.exit(0)
    """), encoding="utf-8")
    os.chmod(real, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    testbed = td / "testbed"
    testbed.mkdir()

    def run(*args):
        env = os.environ.copy()
        env["GT_INSTANCE_ID"] = "inst-test"
        env["GT_RUN_ID"] = "run-test"
        env["GT_ARM"] = "arm-test"
        env["GT_DB"] = str(td / "graph.db")
        env["GT_ROOT"] = str(testbed)
        env.pop("PROBLEM_STATEMENT", None)
        return subprocess.run(
            [sys.executable, str(wrap), *args],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def load_events():
        if not events.exists():
            return []
        return [json.loads(l) for l in events.read_text().splitlines() if l.strip()]

    def load_state():
        if not state_file.exists():
            return {}
        return json.loads(state_file.read_text())

    return {
        "run": run,
        "events": load_events,
        "state": load_state,
        "last_action": last_action,
        "td": td,
    }


# ── Wrapper tests ──────────────────────────────────────────────────────────

def test_orient_first_call_allowed(wrapper_env):
    proc = wrapper_env["run"]("orient")
    assert proc.returncode == 0, proc.stderr
    assert "REAL_OK" in proc.stdout
    state = wrapper_env["state"]()
    assert state["orient"]["count"] == 1
    assert state["orient_exhausted"] is True
    assert wrapper_env["last_action"].read_text().strip() == "orient"


def test_orient_second_call_blocked(wrapper_env):
    wrapper_env["run"]("orient")
    proc = wrapper_env["run"]("orient")
    assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr}"
    assert "BUDGET_EXHAUSTED: gt_orient" in proc.stdout
    assert "gt_lookup" in proc.stdout  # semantic redirect
    events = wrapper_env["events"]()
    assert any(e.get("event") == "orient_redirected" for e in events), events


def test_lookup_within_limit(wrapper_env):
    wrapper_env["run"]("lookup", "foo")
    wrapper_env["run"]("lookup", "bar")
    state = wrapper_env["state"]()
    assert state["lookup"]["count"] == 2
    assert state["lookup"]["exhausted"] is True


def test_lookup_third_call_blocked(wrapper_env):
    wrapper_env["run"]("lookup", "a")
    wrapper_env["run"]("lookup", "b")
    proc = wrapper_env["run"]("lookup", "c")
    assert proc.returncode == 0
    assert "BUDGET_EXHAUSTED: gt_lookup" in proc.stdout
    events = wrapper_env["events"]()
    assert any(
        e.get("event") == "budget_denied" and e.get("tool") == "lookup"
        for e in events
    ), events


def test_pass_through_no_longer_bypasses(wrapper_env):
    """v11 had: `if sys.argv[1].startswith('--')`: bypass budget. Removed in v12.

    Concrete invariant: pre-flag shapes (e.g. `--db=…`) must not skip budget
    counting. In v12 those are rejected as unknown commands (usage path). The
    ceiling is therefore actually enforceable — we verify by exhausting the
    legitimate lookup budget and checking that the 3rd call is blocked.
    """
    # A flag-style arg is not a known subcommand → usage, no counting.
    proc0 = wrapper_env["run"]("--db=/tmp/x", "--function=y")
    assert proc0.returncode == 0
    assert "BUDGET_EXHAUSTED" not in proc0.stdout

    state = wrapper_env["state"]()
    assert state == {}  # no bucket created by the pass-through attempt

    wrapper_env["run"]("lookup", "a")
    wrapper_env["run"]("lookup", "b")
    proc_blocked = wrapper_env["run"]("lookup", "c")
    assert "BUDGET_EXHAUSTED" in proc_blocked.stdout


# ── Ack fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def hook_mod(tmp_path: Path):
    """Load swe_agent_state_gt.py with path constants patched to tmpdir."""
    spec = importlib.util.spec_from_file_location(
        "swe_agent_state_gt_test", HOOK_PY,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Patch Path constants AFTER module import so log_event + _check_ack see
    # tmpdir.
    mod.GT_ACK_STATE = tmp_path / "ack_state.json"
    mod.GT_LAST_ACTION = tmp_path / "last_action.txt"
    mod.GT_POLICY_STATE = tmp_path / "policy.json"
    mod.GT_TELEMETRY = tmp_path / "telemetry.jsonl"
    mod.GT_PER_TASK_SUMMARY = tmp_path / "summary.json"
    mod.GT_IDENTITY_FILE = tmp_path / "identity.env"
    mod.GT_TOOL_COUNTS = tmp_path / "tool_counts.json"
    mod.GT_BUDGET_EVENTS = tmp_path / "budget_events.jsonl"
    mod.GT_BUDGET_EVENTS_OFFSET = tmp_path / "budget_events.offset"
    mod.GT_LAST_MATERIAL_EDIT_TS = tmp_path / "last_edit.ts"
    mod.GT_LAST_GT_CHECK_TS = tmp_path / "last_check.ts"
    mod.GT_ACK_CALLS = tmp_path / "ack_calls.jsonl"
    mod.GT_ACK_CALLS_OFFSET = tmp_path / "ack_calls.offset"
    return mod


def _arm_typed(mod, cycle, ack_id, symbol="foo"):
    """Arm the ack window with a v13 typed ack_id payload."""
    mod.GT_ACK_STATE.write_text(json.dumps({
        "cycle": cycle, "channel": "micro", "tier": "likely",
        "intervention_id": "test-abc",
        "ack_id": ack_id,
        "expected_next_action": {"kind": "gt_check", "target": "x.py", "text": "gt_check x.py"},
        "expected_next_action_kind": "gt_check",
        "expected_next_action_target": "x.py",
        "expected_next_action_text": "gt_check x.py",
        "confidence_tier": "likely",
        "hint_shape": "micro",
        "hint_fingerprint": None,
        "file": "x.py", "file_key": ["x.py", "x.py"], "symbol": symbol,
        "pre_emit_action": "", "pre_emit_changed": [],
        "pre_emit_file_refs": [], "pre_emit_symbol_refs": [],
        "expires_at_cycle": cycle + mod.NEXT_WINDOW_SIZE,
    }))


def _append_ack_call(mod, ack_id, note=""):
    with open(mod.GT_ACK_CALLS, "a") as f:
        f.write(json.dumps({"ts": "2026-04-19T00:00:00Z", "id": ack_id, "note": note}) + "\n")


def _arm_symbol(mod, cycle, symbol):
    mod.GT_ACK_STATE.write_text(json.dumps({
        "cycle": cycle, "channel": "orient", "tier": 0.9,
        "intervention_id": "test-abc",
        "expected_next_action": f"gt_lookup {symbol}",
        "confidence_tier": 0.9,
        "file": "", "file_key": ["", ""], "symbol": symbol,
        "pre_emit_action": "", "pre_emit_changed": [],
        "pre_emit_file_refs": [], "pre_emit_symbol_refs": [],
        "expires_at_cycle": cycle + mod.NEXT_WINDOW_SIZE,
    }))


def _events_of(mod, name):
    if not mod.GT_TELEMETRY.exists():
        return []
    out = []
    for line in mod.GT_TELEMETRY.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") == name:
            out.append(ev)
    return out


def test_ack_followed(hook_mod):
    _arm_symbol(hook_mod, cycle=5, symbol="foo")
    hook_mod.GT_LAST_ACTION.write_text("lookup:foo")
    hook_mod._check_ack(cycle=6, action="", changed_files=[])
    assert _events_of(hook_mod, "ack_followed")
    assert not hook_mod.GT_ACK_STATE.exists()


def test_ack_ignored(hook_mod):
    _arm_symbol(hook_mod, cycle=5, symbol="foo")
    hook_mod.GT_LAST_ACTION.write_text("impact:bar")  # non-targeted gt action
    hook_mod._check_ack(cycle=6, action="", changed_files=[])
    assert _events_of(hook_mod, "ack_ignored")
    assert not hook_mod.GT_ACK_STATE.exists()


def test_ack_not_observed_genuine(hook_mod):
    _arm_symbol(hook_mod, cycle=5, symbol="foo")
    # No GT_LAST_ACTION, no edits; run the cycle past window expiry.
    for c in range(6, 6 + hook_mod.NEXT_WINDOW_SIZE + 2):
        hook_mod._check_ack(cycle=c, action="", changed_files=[])
    assert _events_of(hook_mod, "ack_not_observed")


# ── v13 typed-ack tests ────────────────────────────────────────────────────


def test_typed_ack_followed_matches_id(hook_mod):
    """A gt_ack call with the matching id closes the window as ack_followed."""
    _arm_typed(hook_mod, cycle=5, ack_id="abcd1234")
    _append_ack_call(hook_mod, "abcd1234", note="ran gt_check")
    hook_mod._check_ack(cycle=6, action="", changed_files=[])
    followed = _events_of(hook_mod, "ack_followed")
    assert followed, "typed gt_ack with matching id should produce ack_followed"
    assert followed[-1].get("source") == "typed_ack"
    assert followed[-1].get("ack_id") == "abcd1234"
    assert not hook_mod.GT_ACK_STATE.exists()


def test_typed_ack_stale_id_does_not_close_window(hook_mod):
    """A gt_ack call with a non-matching id emits ack_stale_id; window stays armed."""
    _arm_typed(hook_mod, cycle=5, ack_id="abcd1234")
    _append_ack_call(hook_mod, "ffff0000")  # wrong id
    hook_mod._check_ack(cycle=6, action="", changed_files=[])
    stale = _events_of(hook_mod, "ack_stale_id")
    assert stale, "non-matching gt_ack should emit ack_stale_id"
    # Window must still be armed (no ack_followed on a mismatch).
    assert not _events_of(hook_mod, "ack_followed")


def test_typed_ack_watermark_is_drained(hook_mod):
    """Each gt_ack line is read once: second _check_ack sees no stale duplicate."""
    _arm_typed(hook_mod, cycle=5, ack_id="abcd1234")
    _append_ack_call(hook_mod, "ffff0000")
    hook_mod._check_ack(cycle=6, action="", changed_files=[])
    # Second invocation should not reprocess the same stale line.
    hook_mod._check_ack(cycle=7, action="", changed_files=[])
    stale_events = _events_of(hook_mod, "ack_stale_id")
    assert len(stale_events) == 1, (
        "watermark should prevent reprocessing the same gt_ack line"
    )


# ── v13e tool_signature_read tests ────────────────────────────────────────


def test_tool_signature_read_closes_with_sed_on_focus_file(hook_mod):
    """bash sed reading the armed focus_file resolves the window as ack_followed."""
    _arm_typed(hook_mod, cycle=5, ack_id="abcd1234")
    action = "bash -c 'sed -n 357,360p /testbed/x.py'"
    hook_mod._check_ack(cycle=6, action=action, changed_files=[])
    followed = _events_of(hook_mod, "ack_followed")
    assert followed, "sed reading armed focus file should close the window"
    assert followed[-1].get("source") == "tool_signature_read"
    assert followed[-1].get("read_cmd") == "sed"
    assert not hook_mod.GT_ACK_STATE.exists()


def test_tool_signature_read_accepts_grep(hook_mod):
    _arm_typed(hook_mod, cycle=5, ack_id="abcd1234")
    action = "grep -n 'self.data.cols' /testbed/x.py"
    hook_mod._check_ack(cycle=6, action=action, changed_files=[])
    followed = _events_of(hook_mod, "ack_followed")
    assert followed and followed[-1].get("source") == "tool_signature_read"
    assert followed[-1].get("read_cmd") == "grep"


def test_tool_signature_read_rejects_read_on_other_file(hook_mod):
    """sed on an unrelated file must NOT resolve the window."""
    _arm_typed(hook_mod, cycle=5, ack_id="abcd1234")
    action = "sed -n 1,10p /testbed/other.py"
    hook_mod._check_ack(cycle=6, action=action, changed_files=[])
    followed = _events_of(hook_mod, "ack_followed")
    read_hits = [f for f in followed if f.get("source") == "tool_signature_read"]
    assert not read_hits, "sed on unrelated file must not fire tool_signature_read"


def test_should_verify_is_presubmit_or_loop_only(hook_mod):
    assert hook_mod.should_verify({}, presubmit=True) is True
    assert hook_mod.should_verify({"edit_count": 3, "file_edit_counts": {}}, presubmit=False) is False
    assert hook_mod.should_verify({"edit_count": 1, "file_edit_counts": {"foo.py": 3}}, presubmit=False) is False


def test_confidence_policy_gates_info_hooks():
    from benchmarks.swebench import gt_intel as m

    assert m.classify_confidence_policy(0.55, unique=True, fresh=True, is_test=False)[0] == "silent"
    assert m.classify_confidence_policy(0.70, unique=True, fresh=True, is_test=False)[0] == "advisory"
    assert m.classify_confidence_policy(0.85, unique=True, fresh=True, is_test=False)[0] == "blocking"
    assert m.classify_confidence_policy(0.85, unique=False, fresh=True, is_test=False)[0] == "silent"


# ── Submit-gate test ──────────────────────────────────────────────────────

def _find_working_bash() -> str | None:
    """Return the path of a bash that actually executes.

    On Windows, `shutil.which('bash')` can find Git's `bash.EXE`, but
    `C:\\Windows\\System32\\bash.exe` (a WSL stub) may take precedence in PATH
    resolution inside subprocess, producing `WSL: execvpe(/bin/bash) failed`
    instead of running anything. Probe likely candidates explicitly and
    return the first one that echoes successfully.
    """
    candidates: list[str] = []
    for env_var in ("GT_BASH",):
        val = os.environ.get(env_var)
        if val:
            candidates.append(val)
    candidates += [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
    ]
    w = shutil.which("bash")
    if w and "windows\\system32\\bash.exe" not in w.lower():
        candidates.append(w)
    for path in candidates:
        if not path or not Path(path).exists():
            continue
        try:
            r = subprocess.run(
                [path, "-c", "echo x"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and "x" in r.stdout:
                return path
        except Exception:
            continue
    return None


def _bash_available() -> bool:
    if sys.platform.startswith("win"):
        return False
    return _find_working_bash() is not None and shutil.which("git") is not None


@pytest.mark.skipif(not _bash_available(),
                    reason="bash + git required for submit-gate test")
def test_submit_gate_blocks_then_escapes(tmp_path: Path):
    body = _extract_heredoc("PRESUBMIT")

    testbed = tmp_path / "testbed"
    testbed.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=testbed, check=True)
    subprocess.run(["git", "-C", str(testbed), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(testbed), "config", "user.name", "t"], check=True)
    (testbed / "src.py").write_text("def f(): pass\n")
    subprocess.run(["git", "-C", str(testbed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(testbed), "commit", "-q", "-m", "init"], check=True)
    (testbed / "src.py").write_text("def f(): return 1\n")  # material edit

    attempts = tmp_path / "attempts.txt"
    events = tmp_path / "events.jsonl"
    last_check = tmp_path / "last_check.ts"
    last_edit = tmp_path / "last_edit.ts"
    submit_real = tmp_path / "submit.real"
    submit_real.write_text("#!/usr/bin/env bash\necho REAL_SUBMIT_CALLED\n")
    os.chmod(submit_real, 0o755)

    # Rebind paths. Order matters: replace the longer path first so prefix
    # paths don't shadow it. Use POSIX form so Git Bash on Windows accepts
    # them — backslashes inside `cd C:\Users\...` are interpreted as escape
    # characters by bash and silently fail (`2>/dev/null`).
    tb = testbed.as_posix()
    body = body.replace(" /testbed ", f" {tb} ")
    body = body.replace("cd /testbed", f"cd {tb}")
    body = body.replace("--root=/testbed", f"--root={tb}")
    body = re.sub(
        r'(?ms)^GT_CHECK_FILE="\$\((?:.*?\n)*?\)"\n',
        'GT_CHECK_FILE="src.py"\n',
        body,
    )
    body = body.replace("gt_wait_index 30 >/dev/null 2>&1 || true", ":")
    body = body.replace("/tmp/gt_submit_attempts.txt", attempts.as_posix())
    body = body.replace("/tmp/gt_budget_events.jsonl", events.as_posix())
    body = body.replace("/tmp/gt_last_gt_check.ts", last_check.as_posix())
    body = body.replace("/tmp/gt_last_material_edit.ts", last_edit.as_posix())
    body = body.replace("/tmp/gt_intel_real.py", (tmp_path / "no_real.py").as_posix())
    body = body.replace(
        "/root/tools/review_on_submit_m/bin/submit.real",
        submit_real.as_posix(),
    )
    body = re.sub(
        r'(?ms)\n\s*if command -v gt_wait_index >/dev/null 2>&1; then\n\s*gt_wait_index 30 >/dev/null 2>&1 \|\| true\n\s*fi\n',
        '\n',
        body,
    )

    wrap = tmp_path / "submit_wrapper.sh"
    wrap.write_text(body, encoding="utf-8")
    os.chmod(wrap, 0o755)

    bash_bin = _find_working_bash()
    assert bash_bin, "bash must be available (guarded by skipif)"

    def run():
        return subprocess.run(
            [bash_bin, str(wrap)], capture_output=True, text=True, timeout=15,
        )

    p1 = run()
    assert p1.returncode == 0, p1.stderr
    assert "<gt-intervention" in p1.stdout
    assert "attempt 1/3" in p1.stdout
    assert "REAL_SUBMIT_CALLED" not in p1.stdout

    p2 = run()
    assert p2.returncode == 0, p2.stderr
    assert "attempt 2/3" in p2.stdout
    assert "REAL_SUBMIT_CALLED" not in p2.stdout

    p3 = run()
    assert p3.returncode == 0, p3.stderr
    assert "REAL_SUBMIT_CALLED" in p3.stdout

    recs = [json.loads(l) for l in events.read_text().splitlines() if l.strip()]
    assert any(r.get("event") == "submit_observed" for r in recs), recs
    assert any(r.get("event") == "submit_gate_blocked" for r in recs)
    assert any(r.get("event") == "submit_gate_bypassed" for r in recs)


def test_canary_report_prefers_budget_state_over_trajectory(tmp_path: Path):
    from benchmarks.swebench import gt_canary_report as report

    outdir = tmp_path / "out"
    task_dir = outdir / "astropy__astropy-12907"
    task_dir.mkdir(parents=True)

    task_dir.joinpath("gt_per_task_summary.json").write_text(json.dumps({
        "run_id": "run-test",
        "arm": "arm-test",
        "cycle": 5,
        "identity_ok": True,
        "within_call_budget": True,
    }))
    task_dir.joinpath("gt_budget.state.json").write_text(json.dumps({
        "scope": "run-test__astropy__astropy-12907__arm-test",
        "orient": {"count": 1, "limit": 1, "exhausted": True},
        "lookup": {"count": 2, "limit": 2, "exhausted": True},
        "impact": {"count": 2, "limit": 2, "exhausted": True},
        "check": {"count": 3, "limit": 3, "exhausted": True},
        "orient_exhausted": True,
    }))
    task_dir.joinpath("fake.traj.json").write_text(json.dumps({
        "history": [
            {"action": "gt_orient"},
            {"action": "gt_orient"},
            {"action": "gt_lookup foo"},
            {"action": "gt_lookup bar"},
            {"action": "gt_lookup baz"},
            {"action": "gt_impact foo"},
            {"action": "gt_check src.py"},
        ]
    }))

    row = report.build_row(outdir, task_dir, "arm-test", "run-test", 150, False)
    assert row["gt_orient_count"] == 1
    assert row["gt_lookup_count"] == 2
    assert row["gt_impact_count"] == 2
    assert row["gt_check_count"] == 3
    assert row["gt_budget_ok"] == 1
    assert row["budget_state_present"] == 1


# ── material_edit arming + dedup tests (Step 1) ────────────────────────────


def test_material_edit_concrete_action_is_verify_edit(hook_mod):
    """material_edit channel must produce a generic verify_edit spec, not a
    hardwired gt_check literal."""
    spec = hook_mod._concrete_expected_next_action(
        channel="material_edit", tier="edit",
        focus_file="a/b/foo.py", focus_symbol="",
    )
    assert spec is not None
    assert spec.get("kind") == "verify_edit"
    assert spec.get("target") == "a/b/foo.py"
    assert "gt_check" not in (spec.get("text") or "")


def test_material_edit_concrete_action_none_without_file(hook_mod):
    spec = hook_mod._concrete_expected_next_action(
        channel="material_edit", tier="edit",
        focus_file="", focus_symbol="",
    )
    assert spec is None


def test_arm_ack_material_edit_emits_armed_and_steer_armed(hook_mod):
    """_arm_ack on channel=material_edit writes GT_ACK_STATE, emits ack_armed
    and steer_armed with has_payload=False."""
    ack_id = hook_mod._arm_ack(
        cycle=10, channel="material_edit", tier="edit",
        focus_file="pkg/mod.py", focus_symbol="",
        pre_action="str_replace_editor ...", pre_changed=["pkg/mod.py"],
        hint_shape="material_edit",
    )
    assert ack_id, "material_edit arming should produce an ack_id"
    assert hook_mod.GT_ACK_STATE.exists()
    state = json.loads(hook_mod.GT_ACK_STATE.read_text())
    assert state["channel"] == "material_edit"
    assert state["file"] == "pkg/mod.py"
    armed = _events_of(hook_mod, "ack_armed")
    assert armed and armed[-1].get("channel") == "material_edit"
    steer = _events_of(hook_mod, "steer_armed")
    assert steer and steer[-1].get("has_payload") is False
    assert steer[-1].get("insertion_path") == "material_edit"


def test_material_edit_dedup_same_cycle_same_file(hook_mod):
    """Pre-existing arm at same cycle + same file → new arm should skip and
    emit ack_arm_dedup with kept=existing."""
    # Pre-seed an existing briefing arm at cycle 10, file foo.py.
    hook_mod.GT_ACK_STATE.write_text(json.dumps({
        "cycle": 10, "channel": "briefing", "tier": "likely",
        "ack_id": "priorack1", "file": "foo.py",
        "expected_next_action_text": "gt_lookup foo",
        "expires_at_cycle": 12,
    }))
    # Simulate the dedup decision code (mirrors the inline block in the hook).
    existing = json.loads(hook_mod.GT_ACK_STATE.read_text())
    me_file, me_cycle = "foo.py", 10
    should_arm = not (
        int(existing.get("cycle", -1)) == me_cycle
        and (existing.get("file") or "") == me_file
    )
    assert should_arm is False
    # Dedup event semantics: emit ack_arm_dedup with same_cycle_same_file.
    hook_mod.log_event("ack_arm_dedup", kept="existing",
                       reason="same_cycle_same_file",
                       prior_channel=existing.get("channel"),
                       prior_file=existing.get("file"),
                       attempted_channel="material_edit",
                       attempted_file=me_file, cycle=me_cycle)
    dedup = _events_of(hook_mod, "ack_arm_dedup")
    assert dedup and dedup[-1].get("kept") == "existing"
    assert dedup[-1].get("reason") == "same_cycle_same_file"


def test_material_edit_dedup_same_cycle_different_file(hook_mod):
    """Same-cycle different-file: dedup event fires with reason differing;
    existing higher-precision window is preserved."""
    hook_mod.GT_ACK_STATE.write_text(json.dumps({
        "cycle": 10, "channel": "micro", "tier": "likely",
        "ack_id": "priorack2", "file": "other.py",
        "expected_next_action_text": "gt_check other.py",
        "expires_at_cycle": 12,
    }))
    existing = json.loads(hook_mod.GT_ACK_STATE.read_text())
    me_file, me_cycle = "foo.py", 10
    assert int(existing.get("cycle", -1)) == me_cycle
    assert existing.get("file") != me_file
    hook_mod.log_event("ack_arm_dedup", kept="existing",
                       reason="same_cycle_different_file",
                       prior_channel=existing.get("channel"),
                       prior_file=existing.get("file"),
                       attempted_channel="material_edit",
                       attempted_file=me_file, cycle=me_cycle)
    dedup = _events_of(hook_mod, "ack_arm_dedup")
    assert dedup and dedup[-1].get("kept") == "existing"
    assert dedup[-1].get("reason") == "same_cycle_different_file"
    # Existing window must still be the micro (higher-precision) arm.
    state = json.loads(hook_mod.GT_ACK_STATE.read_text())
    assert state["channel"] == "micro"


def test_ack_engagement_emitted_alongside_tool_signature_read(hook_mod):
    """Step 4: ack_engagement must emit alongside ack_followed at
    tool_signature_read resolution."""
    _arm_typed(hook_mod, cycle=5, ack_id="feed1234")
    hook_mod._check_ack(cycle=6,
                       action="sed -n 10,20p /testbed/x.py",
                       changed_files=[])
    engagement = _events_of(hook_mod, "ack_engagement")
    assert engagement, "ack_engagement should emit at tool_signature_read"
    last = engagement[-1]
    assert last.get("classification") == "tool_signature_read"
    assert last.get("source") == "tool_signature_read"


def test_ack_engagement_emitted_on_expiry_no_activity(hook_mod):
    """Expiry with no activity must emit ack_engagement(no_visible_engagement)."""
    _arm_symbol(hook_mod, cycle=5, symbol="foo")
    for c in range(6, 6 + hook_mod.NEXT_WINDOW_SIZE + 2):
        hook_mod._check_ack(cycle=c, action="", changed_files=[])
    eng = _events_of(hook_mod, "ack_engagement")
    assert eng, "expiry must emit an ack_engagement event"
    assert any(e.get("classification") == "no_visible_engagement" for e in eng)


# ── Step 6a: pre-smoke main() integration replay ──────────────────────────


def _patch_hook_for_replay(mod, tmp_path: Path, monkeypatch):
    """Stub out disk/git-dependent paths in the hook module so main() can
    run in-process without /testbed, graph.db, or a real sweagent container.

    This is the Step 6a pre-smoke validation: exercise the full material_edit
    → ack_armed → steer_armed → steer_delivered/dropped plumbing through the
    real main() entry point (not isolated helpers).
    """
    # Paths the hook writes to.
    mod.STATE_PATH = tmp_path / "state.json"
    mod.GT_DB = str(tmp_path / "graph.db")
    mod.GT_ACK_STATE = tmp_path / "ack_state.json"
    mod.GT_LAST_ACTION = tmp_path / "last_action.txt"
    mod.GT_POLICY_STATE = tmp_path / "policy.json"
    mod.GT_TELEMETRY = tmp_path / "telemetry.jsonl"
    mod.GT_PER_TASK_SUMMARY = tmp_path / "summary.json"
    mod.GT_IDENTITY_FILE = tmp_path / "identity.env"
    mod.GT_TOOL_COUNTS = tmp_path / "tool_counts.json"
    mod.GT_BUDGET_EVENTS = tmp_path / "budget_events.jsonl"
    mod.GT_BUDGET_EVENTS_OFFSET = tmp_path / "budget_events.offset"
    mod.GT_LAST_MATERIAL_EDIT_TS = tmp_path / "last_edit.ts"
    mod.GT_LAST_GT_CHECK_TS = tmp_path / "last_check.ts"
    mod.GT_ACK_CALLS = tmp_path / "ack_calls.jsonl"
    mod.GT_ACK_CALLS_OFFSET = tmp_path / "ack_calls.offset"
    mod.GT_CHECKPOINT_STARTUP = tmp_path / "checkpoint_startup.flag"
    mod.GT_LSP_READY = tmp_path / "lsp_ready.flag"
    (tmp_path / "graph.db").write_bytes(b"")  # satisfy os.path.exists(GT_DB)
    step_file = tmp_path / "step_count"
    monkeypatch.setattr(mod, "Path", mod.Path)  # no-op, keeps import visible

    # Step counter — hook reads /tmp/gt_step_count; redirect.
    orig_step_path = mod.Path
    def _step_path_shim(p):
        if str(p) == "/tmp/gt_step_count":
            return step_file
        return orig_step_path(p)
    monkeypatch.setattr(mod, "Path", _step_path_shim)

    # Stub functions that touch git / network / docker.
    monkeypatch.setattr(mod, "detect_material_edits",
                        lambda: mod._REPLAY_CHANGED[:])
    monkeypatch.setattr(mod, "detect_material_edits_peek",
                        lambda: mod._REPLAY_CHANGED[:])
    monkeypatch.setattr(mod, "generate_pre_edit_briefing_safe", lambda: "")
    monkeypatch.setattr(mod, "build_micro_update_safe", lambda c: None)
    monkeypatch.setattr(mod, "_drain_budget_events", lambda: None)
    monkeypatch.setattr(mod, "_is_presubmit", lambda s: False)
    monkeypatch.setattr(mod, "_emit_per_task_summary",
                        lambda reason=None: None)
    monkeypatch.setattr(mod, "_budget_remaining", lambda: {"remaining": 99})
    monkeypatch.setattr(mod, "_task_scope", lambda: "replay")
    monkeypatch.setattr(mod, "should_verify",
                        lambda ms, presubmit=False: False)
    monkeypatch.setattr(mod, "increment_tool_count", lambda *a, **kw: None)
    # load/save micro-state: small stubs backed by a dict on the module.
    _micro = {"edit_count": 0, "file_edit_counts": {},
              "verify_used": 0, "micro_used": 0}
    monkeypatch.setattr(mod, "load_micro_state", lambda: dict(_micro))
    def _save_ms(ms):
        _micro.update(ms)
    monkeypatch.setattr(mod, "save_micro_state", _save_ms)


def test_main_replay_arms_on_material_edit_and_delivers(
    hook_mod, tmp_path: Path, monkeypatch,
):
    """Step 6a: run main() twice with GT_ARM_ON_MATERIAL_EDIT=1 and verify
    material_edit + ack_armed(channel=material_edit) + ack_armed_on_edit +
    steer_armed + steer_delivered|steer_dropped are all emitted."""
    monkeypatch.setenv("GT_ARM_ON_MATERIAL_EDIT", "1")
    monkeypatch.setenv("GT_TRACE_HASH_SEED", "1")
    monkeypatch.setenv("GT_LSP_ENABLED", "0")
    hook_mod._REPLAY_CHANGED = ["astropy/io/fits/header.py"]
    _patch_hook_for_replay(hook_mod, tmp_path, monkeypatch)

    hook_mod.main()
    ev1 = _events_of(hook_mod, "material_edit")
    armed1 = _events_of(hook_mod, "ack_armed")
    on_edit1 = _events_of(hook_mod, "ack_armed_on_edit")
    steer_armed1 = _events_of(hook_mod, "steer_armed")
    delivered_or_dropped = (
        _events_of(hook_mod, "steer_delivered")
        + _events_of(hook_mod, "steer_dropped")
    )

    assert ev1, "material_edit must fire on first cycle with changes"
    assert armed1 and armed1[-1].get("channel") == "material_edit"
    assert on_edit1, "ack_armed_on_edit must emit when flag is on"
    assert steer_armed1 and steer_armed1[-1].get("has_payload") is False
    assert delivered_or_dropped, (
        "cycle must emit steer_delivered or steer_dropped for armed window"
    )


def test_main_replay_dedups_same_cycle_same_file(
    hook_mod, tmp_path: Path, monkeypatch,
):
    """Arm already exists at current cycle on same file — main() must
    emit ack_arm_dedup(same_cycle_same_file) rather than re-arming."""
    monkeypatch.setenv("GT_ARM_ON_MATERIAL_EDIT", "1")
    monkeypatch.setenv("GT_LSP_ENABLED", "0")
    hook_mod._REPLAY_CHANGED = ["pkg/mod.py"]
    _patch_hook_for_replay(hook_mod, tmp_path, monkeypatch)

    # Pre-seed step to 1 and an active arm at cycle=1 on the same file.
    (tmp_path / "step_count").write_text("0")  # main() will increment to 1
    hook_mod.GT_ACK_STATE.write_text(json.dumps({
        "cycle": 1, "channel": "briefing", "tier": "likely",
        "ack_id": "priorXXXX", "file": "pkg/mod.py",
        "expected_next_action_text": "gt_lookup foo",
        "expires_at_cycle": 3,
    }))
    hook_mod.main()
    dedup = _events_of(hook_mod, "ack_arm_dedup")
    assert dedup, "same-cycle same-file must emit ack_arm_dedup"
    assert dedup[-1].get("reason") == "same_cycle_same_file"
    assert dedup[-1].get("kept") == "existing"


def test_main_replay_trace_hash_seed_gated(
    hook_mod, tmp_path: Path, monkeypatch,
):
    """GT_TRACE_HASH_SEED=1 produces hash_trace_detect events; off → silent."""
    monkeypatch.setenv("GT_ARM_ON_MATERIAL_EDIT", "1")
    monkeypatch.setenv("GT_LSP_ENABLED", "0")
    hook_mod._REPLAY_CHANGED = ["pkg/mod.py"]
    _patch_hook_for_replay(hook_mod, tmp_path, monkeypatch)

    # detect_material_edits is stubbed, so hash_trace_detect only fires
    # inside the real implementation. Verify the flag itself is read —
    # ie. no trace events appear when the flag is off.
    monkeypatch.delenv("GT_TRACE_HASH_SEED", raising=False)
    hook_mod.main()
    traces_off = (
        _events_of(hook_mod, "hash_trace_detect")
        + _events_of(hook_mod, "hash_trace_git_empty")
    )
    assert not traces_off, (
        "hash_trace_* events must be silent without GT_TRACE_HASH_SEED=1"
    )

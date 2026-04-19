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
    return mod


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


def test_should_verify_is_presubmit_or_loop_only(hook_mod):
    assert hook_mod.should_verify({}, presubmit=True) is True
    assert hook_mod.should_verify({"edit_count": 3, "file_edit_counts": {}}, presubmit=False) is False
    assert hook_mod.should_verify({"edit_count": 1, "file_edit_counts": {"foo.py": 3}}, presubmit=False) is True


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

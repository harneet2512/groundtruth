"""Structural pin: every `run:` block in the paid-run + substrate-build workflows must be
syntactically valid bash (`bash -n`).

BUG (live-reproduced, smoke run 29206615090, killed 30/30 tasks): the "Run GT Pro trial"
step in `swebench_live_lite_full.yml` runs the agent inside a SINGLE-QUOTED
`docker run ... bash -c '...'` block. That block's own guard comment says "NEVER put an
apostrophe in any comment here: one apostrophe closes the quote and the agent silently
never launches." A later-added comment (the 2026-07-08 ROOT-CAUSE FIX text) contained
`mini-swe-agent's PTY` -- the apostrophe closed the single-quoted docker argument early,
so bash then parsed the REST of the block as top-level shell text and died on
`syntax error near unexpected token '('` before mini-swe-agent ever booted
(agent_started=False on every task). Root cause: a *comment*, inside a *single-quoted*
shell string, is NOT inert -- the shell has no concept of "comment" while scanning for
the closing quote.

This test is the STRUCTURAL PIN so the class cannot recur silently: it does not know
about "apostrophes" specifically -- it parses every `run:` block in the three workflows
that ship real shell (the paid Live-Lite trial, the paid DeepSWE trial, and the substrate
image build) with `bash -n`, so ANY quote-imbalance / stray-token break in ANY step is
caught the same way, regardless of what character caused it.

`${{ ... }}` GitHub Actions expressions are not shell syntax (the runner substitutes them
into the script text before bash ever sees it), so they are replaced with a benign `x`
placeholder before parsing -- non-greedy so it does not stop early on an incidental `}`
inside an expression body (e.g. `format('... {0} ...', x)`).

RED-first (verified manually before this file was written, by `git stash`-ing the fix and
rerunning the check): exactly one of the 66 run: blocks across the three files failed --
`swebench_live_lite_full.yml` / job `trial` / step 19 "Run GT Pro trial" -- with
`syntax error near unexpected token '('` pointing at the `mini-swe-agent's PTY` comment
line. All 65 other blocks were already clean, so the substitution/parse approach produces
no false positives. Mutation: reintroducing any apostrophe inside the `bash -c '...'`
block's comments reddens this same test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIR = _ROOT / ".github" / "workflows"
_WORKFLOWS = [
    _WORKFLOW_DIR / "swebench_live_lite_full.yml",
    _WORKFLOW_DIR / "deepswe_full.yml",
    _WORKFLOW_DIR / "gt_substrate_image.yml",
]

# GitHub Actions expressions are substituted into the script text by the runner before
# bash sees it -- they are NOT shell syntax. Non-greedy + DOTALL so a `}` incidental to
# the expression body (e.g. `format('...{0}...', x)`) does not truncate the match early;
# it correctly extends to the real closing `}}`.
_GHA_EXPR_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def _resolve_bash() -> str:
    """Resolve a real Git-Bash interpreter, never the Windows WSL `System32\\bash.exe`
    shim (which either hangs waiting for a WSL distro or behaves nothing like the
    GHA ubuntu-latest bash this pin is emulating)."""
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    if git_bash.is_file():
        return str(git_bash)
    found = shutil.which("bash")
    if found and "System32" in found:
        alt = Path(r"C:\Program Files\Git\usr\bin\bash.exe")
        if alt.is_file():
            return str(alt)
        pytest.skip("only the WSL bash.exe shim is on PATH and Git Bash is not installed")
    if found:
        return found
    pytest.skip("no usable bash interpreter found (Git Bash not installed)")


def _iter_run_steps(wf_path: Path):
    """Yield (job_name, step_index, step_name, run_text) for every step with a `run:`
    block in the workflow file."""
    doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and doc, f"{wf_path.name} did not parse to a mapping"
    jobs = doc.get("jobs", {}) or {}
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps", []) or []
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            run_text = step.get("run")
            if not isinstance(run_text, str):
                continue
            name = step.get("name", f"step[{idx}]")
            yield job_name, idx, name, run_text


def _bash_parse_check(bash_exe: str, script_text: str) -> tuple[bool, str]:
    """Substitute GHA `${{ }}` expressions with a benign placeholder, write the result to
    a temp file, and `bash -n` it. Returns (ok, stderr)."""
    sanitized = _GHA_EXPR_RE.sub("x", script_text)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
    ) as f:
        f.write(sanitized)
        tmp_path = f.name
    try:
        proc = subprocess.run(
            [bash_exe, "-n", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode == 0, proc.stderr
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _collect_cases() -> list[tuple[str, str, int, str, str]]:
    cases = []
    for wf in _WORKFLOWS:
        if not wf.is_file():
            continue
        for job_name, idx, step_name, run_text in _iter_run_steps(wf):
            cases.append((wf.name, job_name, idx, step_name, run_text))
    return cases


_CASES = _collect_cases()


def test_target_workflows_exist():
    """Sanity: the pin must actually be scanning the three intended files, not silently
    scanning zero of them (e.g. after a workflow rename)."""
    missing = [str(wf) for wf in _WORKFLOWS if not wf.is_file()]
    assert not missing, f"expected workflow(s) missing: {missing}"


def test_collected_at_least_one_run_step_per_workflow():
    """Guards against a YAML-structure regression silently making `_iter_run_steps` yield
    nothing (which would make the parse pin below vacuously pass)."""
    seen_files = {name for name, _job, _idx, _step, _run in _CASES}
    expected = {wf.name for wf in _WORKFLOWS}
    assert seen_files == expected, f"expected run: steps from {expected}, saw {seen_files}"
    assert len(_CASES) >= 60, f"expected >=60 run: steps across the 3 workflows, found {len(_CASES)}"


@pytest.mark.parametrize(
    "wf_name,job_name,step_idx,step_name,run_text",
    _CASES,
    ids=[f"{wf}::{job}::{idx}::{name}" for wf, job, idx, name, _run in _CASES],
)
def test_workflow_run_block_is_valid_bash(wf_name, job_name, step_idx, step_name, run_text):
    """Every `run:` block, with `${{ }}` expressions neutralized, must parse as valid bash.

    This is the general structural pin: it does not special-case apostrophes -- any
    quote-imbalance or stray-token break inside any `run:` block (this one, or a future
    one added later) trips this same assertion.
    """
    bash_exe = _resolve_bash()
    ok, stderr = _bash_parse_check(bash_exe, run_text)
    assert ok, (
        f"{wf_name} job={job_name!r} step[{step_idx}]={step_name!r} is not valid bash "
        f"after neutralizing ${{{{ }}}} expressions:\n{stderr}"
    )

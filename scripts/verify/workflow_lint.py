#!/usr/bin/env python3
"""workflow_lint.py — catch recurring CI bug classes BEFORE the paid benchmark matrix.

This is a PRE-MATRIX fail-fast lint. It is wired into the `prepare`/Stage-0 job of
both official pipelines (.github/workflows/swebench_300task.yml and
.github/workflows/deepswe_full.yml) so a workflow/shell bug aborts in the cheap
Stage-0 prepare job instead of sliding into the paid agent matrix.

It detects 4 classes of failure, each of which was a REAL paid-matrix failure:

  CLASS 1 (bash-ism under sh): a `run:` block uses bash-only syntax
    (`declare -A`, `${PIPESTATUS...}`, `${!VAR}`, `mapfile `, `readarray `) while
    the step is NOT pinned to bash. Container jobs default to sh/dash, so without
    `shell: bash` on the step OR `defaults.run.shell: bash` on the job, dash chokes:
    "Syntax error: ( unexpected" / "Bad substitution".

  CLASS 2 (bare pip): a `run:` line invokes `pip install ...` directly. The
    hosted-runner tool-cache pip BINARY shim is sometimes broken
    ("pip: cannot execute: required file not found"). Must be `python -m pip install`.

  CLASS 3 (dataset fetch unguarded): a `run:` block calls `load_dataset(` or
    `from datasets import` WITHOUT both (a) an install guard (`import datasets` /
    a `pip install` of datasets in the same block) AND (b) an HF offline override
    for that one Stage-0 fetch (HF_DATASETS_OFFLINE set to 0 in the step env, or
    `export HF_DATASETS_OFFLINE=0` in the block). Otherwise: ModuleNotFoundError,
    offline FileNotFoundError, or a silent empty matrix.

  CLASS 4 (swallow on required op): a `run:` line that performs a REQUIRED gt
    operation (gt-index go build, gt-index -root/-output, groundtruth.resolve,
    preflight_pipeline.py) ends with `|| true` or `|| echo ...`. The failure is
    swallowed and slides into the paid matrix instead of failing fast. Benign
    `cp ... || true` / artifact copies are NOT flagged.

Exit 0 = clean, exit 1 = violations. Each violation prints as:
    FILE:STEP: CLASS: <line>

Design notes:
  - Conservative by intent: prefer a few false-negatives over false-positives that
    would block a good workflow. The 4 real bugs above MUST be caught.
  - Stdlib only (re, sys, argparse, os). Uses pyyaml ONLY if importable, purely to
    learn job/step *structure* (container? defaults.run.shell? step shell?); the
    `run:` block TEXT is always re-scanned from the raw file so a YAML edge case can
    never hide a bash-ism. If pyyaml is absent, a pure line-scanner derives the same
    structure from indentation.

This is generalized: it keys on shell semantics and op patterns, never on task IDs,
gold labels, repo names, or benchmark structure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

try:  # optional, structure-only
    import yaml  # type: ignore

    _HAS_YAML = True
except Exception:  # pragma: no cover - environment dependent
    _HAS_YAML = False


# ── Detector patterns ────────────────────────────────────────────────────────

# CLASS 1: bash-only constructs that dash/sh do not support.
_BASHISM_RE = re.compile(
    r"declare\s+-A\b"
    r"|\$\{PIPESTATUS"
    r"|\$\{!"
    r"|(?:^|\s)mapfile\s"
    r"|(?:^|\s)readarray\s"
)

# CLASS 2: a bare `pip install` (not `python -m pip`, `python3 -m pip`, `uv pip`,
# or a `-m pip`). We require the literal `pip install ` token at a command
# boundary (start, after ; & |, or a word boundary) and reject it when it is
# preceded on the same line by a module-runner prefix.
_BARE_PIP_RE = re.compile(r"(?:^|[;&|]\s*|\b)pip\s+install\s", re.MULTILINE)
_PIP_OK_PREFIX_RE = re.compile(r"(?:python3?|uv)\s+-m\s+pip\s+install\s|-m\s+pip\s+install\s")

# CLASS 3: dataset fetch markers and their two required guards.
_DATASET_USE_RE = re.compile(r"load_dataset\s*\(|from\s+datasets\s+import\b")
_DATASET_INSTALL_GUARD_RE = re.compile(
    r"import\s+datasets\b"  # `python -c "import datasets"` guard
    r"|pip\s+install[^\n]*datasets"  # explicit install of datasets
    r"|-m\s+pip\s+install[^\n]*datasets"
)
# offline override inside the block itself (export ... =0)
_DATASET_OFFLINE_INLINE_RE = re.compile(
    r"HF_DATASETS_OFFLINE\s*[:=]\s*[\"']?0[\"']?"
    r"|export\s+HF_DATASETS_OFFLINE=0"
)

# CLASS 4: required gt ops whose failure must NOT be swallowed.
_REQUIRED_OP_RE = re.compile(
    r"go\s+build\b[^\n]*gt-index"
    r"|gt-index(?:-linux)?\b[^\n]*-root"
    r"|gt-index(?:-linux)?\b[^\n]*-output"
    r"|groundtruth\.resolve\b"
    r"|preflight_pipeline\.py\b"
)
# a swallow at the END of the command: `|| true` or `|| echo ...`
_SWALLOW_RE = re.compile(r"\|\|\s*(?:true\b|echo\b)")


# ── Step model ───────────────────────────────────────────────────────────────


class Step:
    __slots__ = (
        "name",
        "shell_bash",
        "job_shell_bash",
        "run_lines",
        "first_lineno",
        "_is_container",
    )

    def __init__(self) -> None:
        self.name: str = "<unnamed>"
        self.shell_bash: bool = False
        self.job_shell_bash: bool = False
        self.run_lines: list[str] = []
        self.first_lineno: int = 0
        self._is_container: bool = False

    def pinned_to_bash(self) -> bool:
        return self.shell_bash or self.job_shell_bash

    def run_text(self) -> str:
        return "\n".join(self.run_lines)


# ── Structure helpers (pyyaml when present) ──────────────────────────────────


def _job_shell_bash_from_yaml(job: dict) -> bool:
    """True if job-level defaults.run.shell == bash."""
    try:
        return str(job.get("defaults", {}).get("run", {}).get("shell", "")).strip() == "bash"
    except Exception:
        return False


def _job_is_container(job: dict) -> bool:
    return "container" in (job or {})


def _yaml_jobs_shell_map(path: str) -> dict:
    """Return {job_key: job_default_shell_is_bash} using pyyaml structure.

    For composite actions (action.yml) there are no jobs; the steps live under
    runs.steps and run on the host runner (default bash), so callers treat those
    steps as host/bash-default. We return {} for actions and let the line scanner
    pick up per-step `shell:`.
    """
    if not _HAS_YAML:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for jk, job in jobs.items():
            if isinstance(job, dict):
                out[jk] = _job_shell_bash_from_yaml(job)
    return out


# ── Pure line scanner (always runs; owns run-block TEXT) ──────────────────────

_INDENT_RE = re.compile(r"^(\s*)")


def _indent(line: str) -> int:
    return len(_INDENT_RE.match(line).group(1).expandtabs(2))


def parse_steps(path: str) -> list[Step]:
    """Line-scan a workflow or composite action into Steps.

    Tracks, per step:
      - the step `name:`
      - whether the step has `shell: bash`
      - whether the enclosing job pins bash via `defaults.run.shell: bash`
      - whether the enclosing job is a `container:` job (and is therefore
        sh/dash by default unless pinned)
      - the literal lines of the step's `run:` block

    A job whose default shell is unspecified runs bash on a host runner but sh/dash
    inside a `container:`. So a step is treated as "bash-default" (i.e. NOT needing
    an explicit pin) only when its job is NOT a container job. CLASS-1 only fires
    when the step is genuinely unpinned AND its job is a container job (or shell
    is otherwise sh) — this is the conservative posture the spec asks for.
    """
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    yaml_job_shell = _yaml_jobs_shell_map(path)

    steps: list[Step] = []

    # Job-scope state. A workflow has top-level `jobs:`; each job is a mapping.
    # We track the current job's: name, container?, defaults.run.shell bash?.
    in_jobs = False
    jobs_indent = -1
    cur_job_key = None
    cur_job_indent = -1
    cur_job_is_container = False
    cur_job_shell_bash = False
    # detecting defaults: -> run: -> shell: bash within a job header
    defaults_run_shell_bash = False

    # Step-scope state.
    cur_step: Step | None = None
    in_run = False
    run_indent = -1
    run_is_block_scalar = False
    step_list_indent = -1  # indent of the `- ` step bullets within steps:

    def flush_step():
        nonlocal cur_step
        if cur_step is not None and cur_step.run_lines:
            steps.append(cur_step)
        cur_step = None

    for idx, raw in enumerate(lines):
        lineno = idx + 1
        line = raw.rstrip("\n")
        stripped = line.strip()
        ind = _indent(line)

        # --- inside a run block: collect raw text until indentation drops ---
        if in_run and cur_step is not None:
            if stripped == "" or ind > run_indent:
                # body line of the block scalar (or blank line within it)
                if run_is_block_scalar:
                    cur_step.run_lines.append(line)
                    continue
            # run: value was inline (not a block scalar) — handled at assignment
            if stripped != "" and ind <= run_indent:
                in_run = False
                # fall through to re-process this line as structure

        # --- top-level jobs: detection (workflows) ---
        if re.match(r"^jobs\s*:\s*$", line):
            in_jobs = True
            jobs_indent = ind
            continue

        # composite action: runs:/steps: — steps run on host (bash default)
        # we don't need special job tracking; cur_job_is_container stays False.

        if in_jobs and ind == jobs_indent + 2 and re.match(r"^[A-Za-z0-9_\-]+\s*:\s*$", stripped):
            # a new job key
            flush_step()
            cur_job_key = stripped[:-1].strip()
            cur_job_indent = ind
            cur_job_is_container = False
            cur_job_shell_bash = bool(yaml_job_shell.get(cur_job_key, False))
            defaults_run_shell_bash = False
            step_list_indent = -1
            continue

        # within a job header, detect container: and defaults.run.shell: bash
        if cur_job_key is not None and not _HAS_YAML:
            if re.match(r"^container\s*:", stripped):
                cur_job_is_container = True
            # defaults: \n run: \n shell: bash
            if re.match(r"^shell\s*:\s*bash\s*$", stripped) and step_list_indent == -1:
                # shell:bash appearing in the job header region (before steps bullets)
                # is the defaults.run.shell. Heuristic: only treat as job-default if
                # we are not currently inside a step.
                if cur_step is None:
                    defaults_run_shell_bash = True
            cur_job_shell_bash = cur_job_shell_bash or defaults_run_shell_bash
        elif cur_job_key is not None and _HAS_YAML:
            if re.match(r"^container\s*:", stripped):
                cur_job_is_container = True
            # cur_job_shell_bash already came from yaml structure

        # --- step bullets: `- name:` / `- uses:` / `- run:` ---
        is_bullet = stripped.startswith("- ")
        if is_bullet:
            # entering a new step
            flush_step()
            if step_list_indent == -1:
                step_list_indent = ind
            cur_step = Step()
            cur_step.first_lineno = lineno
            cur_step.job_shell_bash = cur_job_shell_bash
            # an action's host steps are bash-default; a container job is sh-default
            cur_step._is_container = cur_job_is_container  # type: ignore[attr-defined]
            body = stripped[2:].strip()
            # the bullet may carry name:/uses:/run: inline
            m = re.match(r"^(name|uses|run|shell)\s*:\s*(.*)$", body)
            if m:
                key, val = m.group(1), m.group(2)
                _apply_step_kv(cur_step, key, val, lines, idx, ind)
                if key == "run":
                    in_run, run_indent, run_is_block_scalar = _enter_run(
                        cur_step, val, ind
                    )
            continue

        # --- step attribute lines (name:/shell:/run:/env:) ---
        if cur_step is not None and ind > step_list_indent and not in_run:
            m = re.match(r"^(name|shell|run)\s*:\s*(.*)$", stripped)
            if m:
                key, val = m.group(1), m.group(2)
                _apply_step_kv(cur_step, key, val, lines, idx, ind)
                if key == "run":
                    in_run, run_indent, run_is_block_scalar = _enter_run(
                        cur_step, val, ind
                    )
                continue
            # step env: HF_DATASETS_OFFLINE: "0" — we capture env offline override
            # by appending env lines into run text so CLASS-3 inline check sees it.
            if re.search(r"HF_DATASETS_OFFLINE\s*:", stripped):
                cur_step.run_lines.append("__ENV__ " + stripped)
                continue

    flush_step()

    # Post-process: decide job_shell_bash for line-scan path; mark container.
    for s in steps:
        is_container = getattr(s, "_is_container", False)
        # A non-container job step is bash-default; a container step needs a pin.
        # We fold this into pinned_to_bash by setting job_shell_bash True when the
        # job is NOT a container (host/bash default), unless already True.
        if not is_container:
            s.job_shell_bash = True
    return steps


def _enter_run(step: Step, inline_val: str, key_indent: int):
    """Handle the `run:` value. Returns (in_run, run_indent, is_block_scalar)."""
    v = inline_val.strip()
    if v in ("|", ">", "|-", ">-", "|+", ">+"):
        # block scalar; body lines follow at deeper indent
        return True, key_indent, True
    if v:
        # inline single-line run value
        step.run_lines.append(v)
        return False, key_indent, False
    # empty (rare) — treat as block start
    return True, key_indent, True


def _apply_step_kv(step: Step, key: str, val: str, lines, idx, ind) -> None:
    if key == "name":
        step.name = _unquote(val) or step.name
    elif key == "shell":
        if val.strip().strip("\"'") == "bash":
            step.shell_bash = True
    # run handled by caller


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


# ── Detectors ────────────────────────────────────────────────────────────────


def _significant_run_lines(step: Step) -> list[str]:
    """Run-block lines with the __ENV__ sentinel lines stripped of their prefix
    for text matching but excluded from per-line CLASS 2/4 (they are env, not cmd)."""
    return step.run_lines


def lint_step(file_label: str, step: Step) -> list[str]:
    violations: list[str] = []
    run_text = step.run_text()
    # env sentinel lines: keep their content for CLASS-3 block check, strip prefix
    run_text_for_block = run_text.replace("__ENV__ ", "")

    # CLASS 1 — bash-ism in an unpinned step.
    if not step.pinned_to_bash():
        for ln in step.run_lines:
            if ln.startswith("__ENV__ "):
                continue
            if _BASHISM_RE.search(ln):
                violations.append(
                    f"{file_label}:{step.name}: CLASS1(bash-ism, no shell:bash): {ln.strip()}"
                )

    # CLASS 2 — bare pip install (per-line).
    for ln in step.run_lines:
        if ln.startswith("__ENV__ "):
            continue
        if _BARE_PIP_RE.search(ln) and not _PIP_OK_PREFIX_RE.search(ln):
            violations.append(
                f"{file_label}:{step.name}: CLASS2(bare pip): {ln.strip()}"
            )

    # CLASS 3 — dataset fetch without BOTH install guard AND offline override.
    if _DATASET_USE_RE.search(run_text_for_block):
        has_install = bool(_DATASET_INSTALL_GUARD_RE.search(run_text_for_block))
        has_offline = bool(_DATASET_OFFLINE_INLINE_RE.search(run_text))
        if not (has_install and has_offline):
            miss = []
            if not has_install:
                miss.append("no-install-guard")
            if not has_offline:
                miss.append("no-offline-override")
            # report on the first dataset-use line for locality
            first = next(
                (
                    ln.strip()
                    for ln in step.run_lines
                    if not ln.startswith("__ENV__ ") and _DATASET_USE_RE.search(ln)
                ),
                "load_dataset(...)",
            )
            violations.append(
                f"{file_label}:{step.name}: CLASS3(dataset {'+'.join(miss)}): {first}"
            )

    # CLASS 4 — swallow on a required op (per-line).
    for ln in step.run_lines:
        if ln.startswith("__ENV__ "):
            continue
        if _REQUIRED_OP_RE.search(ln) and _SWALLOW_RE.search(ln):
            violations.append(
                f"{file_label}:{step.name}: CLASS4(swallow on required op): {ln.strip()}"
            )

    return violations


def lint_file(path: str) -> list[str]:
    label = os.path.basename(path)
    try:
        steps = parse_steps(path)
    except FileNotFoundError:
        return [f"{label}:<file>: ERROR: file not found"]
    out: list[str] = []
    for step in steps:
        out.extend(lint_step(label, step))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-matrix workflow lint: catch the 4 recurring CI bug classes."
    )
    ap.add_argument("files", nargs="+", help="workflow / action yml files to lint")
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the OK line on clean (still prints violations)",
    )
    args = ap.parse_args(argv)

    all_violations: list[str] = []
    for f in args.files:
        all_violations.extend(lint_file(f))

    if all_violations:
        for v in all_violations:
            print(v)
        print(
            f"\nworkflow_lint: {len(all_violations)} violation(s) across "
            f"{len(args.files)} file(s) — FAIL",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"workflow_lint: clean — {len(args.files)} file(s), 0 violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

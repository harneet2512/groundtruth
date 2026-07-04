#!/usr/bin/env python3
"""GT kernel — single-file post-edit decision hook for SWE-bench-Live containers.

Stdlib only. Mirrors the injection shape of gt_hook.py: gets chunked into the
container as base64, decoded to /tmp/gt_kernel_check.py, then invoked by the
OH PostToolUse hook after each str_replace_editor / file_editor edit.

Inputs (CLI):
    --edit-path <path>     The file the agent just edited.
    --brief-jsonl <path>   The v7.3 pretask brief jsonl (default /tmp/gt_pretask.jsonl).
    --workspace-root <p>   Container workspace root (default first /workspace/* dir).
    --edit-history <p>     JSONL log of prior edits this task (default /tmp/gt_edits.jsonl).

Output:
    Structured block on stdout if the kernel rule fires:
        <gt-kernel-decision>
        action: block
        rule: first_edit_root_scaffold
        message: ...
        </gt-kernel-decision>
    Empty stdout otherwise (allow / audit-only).

Rule set (validated on n=26 counterfactual, 22/26 hits, 0 false positives):

    first_edit_root_scaffold:
        Triggers IFF this is the agent's FIRST edit AND the edit path is a
        scaffold file at the workspace root (no in-tree directory component).
        Scaffold pattern: test_*.py, *_test.py, reproduce*.py, repro*.py,
        *.test.{js,ts}, *.spec.{js,ts}, *_template.{yaml,yml}, reproduction*.{yaml,yml}.

    first_edit_missed_focus:
        Triggers IFF this is the agent's first edit AND focus_files is non-empty
        AND the edit path is NOT in focus_files. Severity gated by brief
        confidence (>= 0.6 -> block, else visible).

Append-only edit history file at --edit-history is the state — checking len()
gives "is this the first edit". Hook must be idempotent on retries.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path
from typing import Any

KERNEL_VERSION = "kernel-0.1-hook"
HIGH_CONFIDENCE_MIN = 0.6

_SCAFFOLD_STEMS = re.compile(
    r"^(test[_-]|reproduce|repro|reproduction)|(_test|\.test|\.spec|_template)$",
    re.IGNORECASE,
)
_SCAFFOLD_EXT = {".py", ".js", ".ts", ".jsx", ".tsx", ".yaml", ".yml", ".go"}


def normalize_path(p: str, workspace_root: str | None = None) -> str:
    """Make path repo-relative.

    workspace_root is the absolute path of the per-task workspace dir,
    e.g. ``/workspace/aws-cloudformation__cfn-lint-3767``. We strip exactly
    that prefix from p so the result is the path as it appears in the
    repo's git tree (``src/cfnlint/config.py``, not ``cfn-lint-3767/src/...``
    and not ``/workspace/cfn-lint-3767/src/...``).
    """
    text = p.replace("\\", "/")
    if workspace_root:
        wr = workspace_root.replace("\\", "/").rstrip("/")
        # Try absolute and relative forms
        if text.startswith(wr + "/"):
            return text[len(wr) + 1:]
        if text == wr:
            return ""
        wr_rel = wr.lstrip("/")
        if text.lstrip("/").startswith(wr_rel + "/"):
            return text.lstrip("/")[len(wr_rel) + 1:]
    # Fallback: just strip workspace/testbed prefix
    return re.sub(r"^/?(?:workspace|testbed)/", "", text, count=1)


def is_root_scaffold(rel_path: str) -> bool:
    """True iff rel_path is at the workspace root AND matches a scaffold pattern."""
    if "/" in rel_path or "\\" in rel_path:
        return False
    p = Path(rel_path)
    if p.suffix.lower() not in _SCAFFOLD_EXT:
        return False
    stem = p.stem
    if _SCAFFOLD_STEMS.search(stem):
        return True
    # Catch *_template / *.spec / *.test by full filename
    name = rel_path.lower()
    if any(t in name for t in ("template.yaml", "template.yml", ".spec.", ".test.")):
        return True
    return False


def load_brief(path: Path) -> dict[str, Any]:
    """Read the first non-empty line of v7.3 pretask jsonl. Returns gt_plan dict."""
    if not path.exists():
        return {}
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            return rec.get("gt_plan") or rec.get("plan") or {}
    except Exception:
        return {}
    return {}


def focus_files_from_plan(plan: dict[str, Any]) -> list[str]:
    raw = plan.get("agent_focus_files") or plan.get("focus_files") or []
    out: list[str] = []
    for item in raw[:3]:
        if isinstance(item, dict):
            v = item.get("file") or item.get("path")
            if v:
                out.append(str(v))
        elif item:
            out.append(str(item))
    return out


def append_edit_history(history_path: Path, edit_path: str) -> None:
    """Append normalized edit path to the JSONL log."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"path": edit_path}) + "\n")


def infer_workspace_root(provided: str | None) -> str | None:
    if provided:
        return provided
    cand = sorted(glob.glob("/workspace/*/"))
    if cand:
        return cand[0].rstrip("/")
    return "/workspace"


def emit_decision(*, action: str, rule: str, message: str, confidence: float) -> None:
    sys.stdout.write("<gt-kernel-decision>\n")
    sys.stdout.write(f"action: {action}\n")
    sys.stdout.write(f"rule: {rule}\n")
    sys.stdout.write(f"confidence: {confidence:.2f}\n")
    sys.stdout.write(f"version: {KERNEL_VERSION}\n")
    sys.stdout.write(f"message: {message}\n")
    sys.stdout.write("</gt-kernel-decision>\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edit-path", required=True)
    ap.add_argument("--brief-jsonl", default="/tmp/gt_pretask.jsonl")
    ap.add_argument("--workspace-root", default=None)
    ap.add_argument("--edit-history", default="/tmp/gt_edits.jsonl")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress output entirely on allow (default: empty stdout)")
    args = ap.parse_args(argv)

    workspace_root = infer_workspace_root(args.workspace_root)
    rel = normalize_path(args.edit_path, workspace_root)
    n_prior = sum(1 for _ in Path(args.edit_history).open(encoding="utf-8", errors="replace")) \
        if Path(args.edit_history).exists() else 0
    append_edit_history(Path(args.edit_history), rel)
    is_first_edit = (n_prior == 0)

    plan = load_brief(Path(args.brief_jsonl))
    focus = focus_files_from_plan(plan)
    confidence = float(plan.get("confidence", 0.0))

    if not is_first_edit:
        return 0  # rules in this hook only apply to first edit

    # Rule 1: root scaffold (validated 22/26 on counterfactual, 0 FP)
    if is_root_scaffold(rel):
        msg_focus = focus[0] if focus else "an existing in-tree source file"
        emit_decision(
            action="block",
            rule="first_edit_root_scaffold",
            message=(
                f"First edit at workspace root ({rel}) looks like throwaway "
                f"scaffolding. Refocus on {msg_focus} (or another file in the "
                f"focus_files list). Editing root scaffolds rarely fixes the bug."
            ),
            confidence=1.0,
        )
        return 0

    # Rule 2: missed focus (brief had focus_files, edit not in them)
    if focus and rel not in focus:
        action = "block" if confidence >= HIGH_CONFIDENCE_MIN else "visible"
        emit_decision(
            action=action,
            rule="first_edit_missed_focus",
            message=(
                f"First edit ({rel}) is outside the GT-ranked focus_files: "
                f"{focus}. Confidence={confidence:.2f}. "
                f"Consider editing one of the ranked targets first."
            ),
            confidence=confidence,
        )
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

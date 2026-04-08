"""Guard consistency signal.

Research basis: Gunawi et al., "Finding Error-Handling Bugs in Systems Code."

If N-1 of N call sites guard a function's return value against None or
a falsy result, but the edit's call site does not, flag it.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from .call_site_voting import (
    SemanticEvidence,
    _extract_diff_calls,
    _git_env,
    _is_test_file,
)


# ---------------------------------------------------------------------------
# Guard detection helpers
# ---------------------------------------------------------------------------


def _line_has_guard(line_text: str) -> bool:
    """Return True if the line or its assignment target is guarded.

    A guard is:
      - result = func(...)  immediately followed by  if result is None:
      - if not result:
      - if result is None:
      - if result is not None:  (checking but not a guard per se — still counts
        as the caller being aware of None possibility)

    We detect this heuristically from the raw text of a few lines around the
    call site.
    """
    guard_patterns = [
        r"\bif\s+not\s+\w+\b",
        r"\bif\s+\w+\s+is\s+None\b",
        r"\bif\s+\w+\s+is\s+not\s+None\b",
        r"\bif\s+\w+\s*==\s*None\b",
        r"\bif\s+\w+\s*!=\s*None\b",
        r"\bor\s+None\b",
        r"\bif\s+\w+\b",  # bare "if result:" counts as awareness
    ]
    for pat in guard_patterns:
        if re.search(pat, line_text):
            return True
    return False


def _assignment_target(line_text: str, func_name: str) -> str | None:
    """Return the variable name that receives the result of func_name().

    E.g. "user = get_user(id)" → "user"
         "result = obj.get_user(id)" → "result"
    """
    # Match: varname = ... func_name(
    m = re.match(r"^\s*(\w+)\s*=\s*.*\b" + re.escape(func_name) + r"\s*\(", line_text)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Git grep sampler
# ---------------------------------------------------------------------------

_CONTEXT_LINES = 3  # lines of context to fetch around each hit


def _sample_call_sites(
    root: str,
    func_name: str,
    exclude_file: str,
    max_sites: int = 20,
    deadline: float = 0.0,
) -> list[dict]:
    """Return list of {file, line, guarded, assignment_target} dicts."""
    results = []
    try:
        proc = subprocess.run(
            ["git", "grep", "-n", "-A", str(_CONTEXT_LINES), "--", f"{func_name}("],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=8,
            env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return results

    rel_exclude = (
        os.path.relpath(exclude_file, root) if os.path.isabs(exclude_file) else exclude_file
    )

    # git grep -A output: lines are "file:lineno:content" or "file-lineno-content" (context)
    # Group by call-site hit
    current_hit: dict | None = None
    context_lines: list[str] = []

    for raw in proc.stdout.splitlines():
        if deadline and time.time() > deadline:
            break
        if len(results) >= max_sites:
            break

        # Separator between groups
        if raw == "--":
            if current_hit is not None:
                _finalize_hit(current_hit, context_lines, results)
            current_hit = None
            context_lines = []
            continue

        # Match lines: "file:lineno:content"
        m = re.match(r"^([^:]+):(\d+):(.*)", raw)
        if m:
            rel_path, lineno_str, content = m.group(1), m.group(2), m.group(3)
            if rel_path == rel_exclude or _is_test_file(rel_path):
                current_hit = None
                context_lines = []
                continue

            # Is this a direct hit (contains the call)?
            if f"{func_name}(" in content:
                if current_hit is not None:
                    _finalize_hit(current_hit, context_lines, results)
                current_hit = {
                    "file": rel_path,
                    "line": int(lineno_str),
                    "call_line": content,
                    "target": _assignment_target(content, func_name),
                }
                context_lines = [content]
            elif current_hit is not None:
                context_lines.append(content)
        else:
            # Context line in "file-lineno-content" format
            m2 = re.match(r"^([^-]+)-(\d+)-(.*)", raw)
            if m2 and current_hit is not None:
                context_lines.append(m2.group(3))

    if current_hit is not None:
        _finalize_hit(current_hit, context_lines, results)

    return results


def _finalize_hit(hit: dict, context_lines: list[str], results: list[dict]) -> None:
    """Determine whether the call site is guarded and append to results."""
    all_text = "\n".join(context_lines)
    target = hit.get("target")

    guarded = False
    if target:
        # Check context lines for a guard on the target variable
        for ctx_line in context_lines[1:]:  # skip the call line itself
            if re.search(r"\b" + re.escape(target) + r"\b", ctx_line):
                if _line_has_guard(ctx_line):
                    guarded = True
                    break
        # Also check inline: "if get_user(x) is None"
        if not guarded and _line_has_guard(hit["call_line"]):
            guarded = True
    else:
        # No assignment — check if the call line itself has a guard context
        if _line_has_guard(all_text):
            guarded = True

    results.append(
        {
            "file": hit["file"],
            "line": hit["line"],
            "guarded": guarded,
            "target": target,
        }
    )


# ---------------------------------------------------------------------------
# Diff-side guard checker
# ---------------------------------------------------------------------------


def _edit_has_guard(diff_text: str, func_name: str, call_file: str, call_line: int) -> bool:
    """Check whether the edit's call site has a guard in the diff context."""
    in_file = False
    current_line = 0
    call_line_content = ""
    post_lines: list[str] = []
    collecting_post = False

    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            in_file = raw[6:] == call_file
            current_line = 0
            collecting_post = False
            post_lines = []
        elif in_file and raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                current_line = int(m.group(1)) - 1
            collecting_post = False
        elif in_file and raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            content = raw[1:]
            if current_line == call_line:
                call_line_content = content
                collecting_post = True
            elif collecting_post:
                post_lines.append(content)
                if len(post_lines) >= _CONTEXT_LINES:
                    break
        elif in_file and not raw.startswith("-"):
            current_line += 1
            if collecting_post:
                post_lines.append(raw)
                if len(post_lines) >= _CONTEXT_LINES:
                    break

    if not call_line_content:
        return False  # can't determine; assume no guard (conservative)

    target = _assignment_target(call_line_content, func_name)
    if target:
        for line in post_lines:
            if re.search(r"\b" + re.escape(target) + r"\b", line):
                if _line_has_guard(line):
                    return True
    return _line_has_guard(call_line_content)


# ---------------------------------------------------------------------------
# Main checker
# ---------------------------------------------------------------------------


class GuardConsistencyChecker:
    """Flag call sites that don't guard return values when most callers do."""

    GUARD_RATE_THRESHOLD = 0.75
    CONFIDENCE_CAP = 0.85
    CONFIDENCE_FLOOR = 0.65
    MIN_SITES = 3

    def analyze(
        self, root: str, diff_text: str, time_budget: float = 3.0
    ) -> list[SemanticEvidence]:
        deadline = time.time() + time_budget
        findings: list[SemanticEvidence] = []

        diff_calls = _extract_diff_calls(diff_text)
        if not diff_calls:
            return findings

        seen_funcs: set[str] = set()

        for file_path, line_no, func_name, _ in diff_calls:
            if time.time() > deadline:
                break
            if func_name in seen_funcs:
                continue
            seen_funcs.add(func_name)

            abs_file = os.path.join(root, file_path) if not os.path.isabs(file_path) else file_path

            sites = _sample_call_sites(
                root,
                func_name,
                abs_file,
                max_sites=20,
                deadline=deadline,
            )
            if len(sites) < self.MIN_SITES:
                continue

            guarded_count = sum(1 for s in sites if s["guarded"])
            total = len(sites)
            guard_rate = guarded_count / total

            if guard_rate < self.GUARD_RATE_THRESHOLD:
                continue

            # Check whether the edit's call site guards the return value
            if _edit_has_guard(diff_text, func_name, file_path, line_no):
                continue

            confidence = min(guard_rate * self.CONFIDENCE_CAP, self.CONFIDENCE_CAP)
            if confidence < self.CONFIDENCE_FLOOR:
                continue

            findings.append(
                SemanticEvidence(
                    kind="guard_consistency",
                    file_path=file_path,
                    line=line_no,
                    message=(
                        f"{guarded_count}/{total} call sites guard {func_name}() "
                        f"against None -- edit does not check return value"
                    ),
                    confidence=confidence,
                )
            )

        return findings

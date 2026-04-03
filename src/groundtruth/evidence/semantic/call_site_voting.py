"""Call-site voting signal.

Research basis: Google Error Prone ArgumentSelectionDefectChecker,
DeepBugs (OOPSLA 2018).

For function calls in the diff, find other call sites via git grep and
compare argument name patterns. Flag positions where the edit's argument
name is a statistical outlier vs. the majority pattern at that position.
Also detect suspected argument swaps.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass


@dataclass
class SemanticEvidence:
    """Evidence item emitted by a semantic signal."""
    kind: str
    file_path: str
    line: int
    message: str
    confidence: float
    family: str = "semantic"


def _git_env() -> dict[str, str]:
    """Git environment that handles safe.directory in containers."""
    import copy
    env: dict[str, str] = dict(copy.copy(os.environ))
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    return env


def _is_test_file(path: str) -> bool:
    """Return True if path looks like a test file (language-agnostic)."""
    fp = "/" + path.lower().replace("\\", "/")
    basename = os.path.basename(fp)
    stem = os.path.splitext(basename)[0]
    if any(p in fp for p in ["/tests/", "/test/", "/testing/", "/spec/", "/__tests__/"]):
        return True
    if basename.startswith("test_") or stem.endswith("_test"):
        return True
    if ".test." in basename or ".spec." in basename:
        return True
    if stem.endswith("Test") or stem.endswith("Tests") or stem.endswith("Spec") or stem.endswith("_spec"):
        return True
    return False


def _extract_arg_name(node: ast.expr) -> str | None:
    """Extract a simple string name from an AST argument node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        # e.g. self.user_id -> "user_id"
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _parse_call_args_from_line(line_text: str, func_name: str) -> list[str | None] | None:
    """Parse argument names from a single source line containing a call to func_name.

    Returns a list of arg name strings (or None for unresolvable args),
    or None if parsing fails.
    """
    # Wrap in a dummy expression so ast.parse can handle it
    stripped = line_text.strip()
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError:
        # Try wrapping in an assignment
        try:
            tree = ast.parse(f"_={stripped}", mode="eval")
        except SyntaxError:
            return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match the function name
        func_id = None
        if isinstance(node.func, ast.Name):
            func_id = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_id = node.func.attr
        if func_id != func_name:
            continue
        return [_extract_arg_name(a) for a in node.args]
    return None


@dataclass
class _CallRecord:
    """One sampled call site."""
    file_path: str
    line_no: int
    args: list[str | None]  # per-position arg names


def _git_grep_call_sites(
    root: str,
    func_name: str,
    exclude_file: str,
    max_sites: int = 20,
    deadline: float = 0.0,
) -> list[_CallRecord]:
    """Find call sites of func_name via git grep.

    Returns up to max_sites records from non-test, non-self files.
    """
    records: list[_CallRecord] = []
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "--", f"{func_name}("],
            capture_output=True, text=True, cwd=root, timeout=8,
            env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return records

    rel_exclude = os.path.relpath(exclude_file, root) if os.path.isabs(exclude_file) else exclude_file

    for raw_line in result.stdout.splitlines():
        if deadline and time.time() > deadline:
            break
        if len(records) >= max_sites:
            break

        # Format: path:lineno:content
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        rel_path, lineno_str, content = parts[0], parts[1], parts[2]

        if rel_path == rel_exclude:
            continue
        if _is_test_file(rel_path):
            continue

        try:
            line_no = int(lineno_str)
        except ValueError:
            continue

        parsed = _parse_call_args_from_line(content, func_name)
        if parsed is None or len(parsed) < 2:
            continue

        records.append(_CallRecord(file_path=rel_path, line_no=line_no, args=parsed))

    return records


def _extract_diff_calls(diff_text: str) -> list[tuple[str, int, str, list[str | None]]]:
    """Extract function calls from added lines of a diff.

    Returns list of (file_path, line_no, func_name, [arg_names]).
    """
    results: list[tuple[str, int, str, list[str | None]]] = []
    current_file = ""
    current_line = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            current_line = 0
        elif raw.startswith("@@ "):
            m = re.search(r"\+(\d+)", raw)
            if m:
                current_line = int(m.group(1)) - 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            content = raw[1:]

            # Python: use AST for precise call extraction
            if current_file.endswith(".py"):
                try:
                    tree = ast.parse(content.strip(), mode="eval")
                except SyntaxError:
                    try:
                        tree = ast.parse(f"_={content.strip()}", mode="eval")
                    except SyntaxError:
                        continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func_id = None
                    if isinstance(node.func, ast.Name):
                        func_id = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_id = node.func.attr
                    if not func_id:
                        continue
                    args = [_extract_arg_name(a) for a in node.args]
                    if len(args) >= 2 and any(a is not None for a in args):
                        results.append((current_file, current_line, func_id, args))
            else:
                # Other languages: regex-based call extraction
                for m in re.finditer(r'(\w+)\s*\(([^)]+)\)', content):
                    func_id = m.group(1)
                    args_str = m.group(2)
                    args: list[str | None] = []
                    for a in args_str.split(","):
                        tok = a.strip().split("=")[0].strip().split(":")[-1].strip()
                        if tok and tok[0].isalpha():
                            args.append(tok)
                        else:
                            args.append(None)
                    if len(args) >= 2 and any(a is not None for a in args):
                        results.append((current_file, current_line, func_id, args))
        elif not raw.startswith("-"):
            current_line += 1

    return results


class CallSiteVoter:
    """Compare argument patterns at each position against sampled call sites."""

    MIN_SITES = 3          # need at least this many sites to vote
    MAJORITY_THRESHOLD = 0.70  # 70% majority to flag
    CONFIDENCE_FLOOR = 0.65

    def analyze(
        self, root: str, diff_text: str, time_budget: float = 3.0
    ) -> list[SemanticEvidence]:
        deadline = time.time() + time_budget
        findings: list[SemanticEvidence] = []

        diff_calls = _extract_diff_calls(diff_text)
        if not diff_calls:
            return findings

        for file_path, line_no, func_name, edit_args in diff_calls:
            if time.time() > deadline:
                break

            abs_file = os.path.join(root, file_path) if not os.path.isabs(file_path) else file_path
            sites = _git_grep_call_sites(
                root, func_name, abs_file,
                max_sites=20, deadline=deadline,
            )
            if len(sites) < self.MIN_SITES:
                continue

            total = len(sites)

            # Build per-position majority arg name
            max_pos = max(len(s.args) for s in sites)
            for pos in range(min(len(edit_args), max_pos)):
                edit_arg = edit_args[pos]
                if edit_arg is None:
                    continue
                pos_counter: Counter[str] = Counter()
                for site in sites:
                    if pos < len(site.args) and site.args[pos] is not None:
                        pos_counter[site.args[pos]] += 1  # type: ignore[arg-type]

                if not pos_counter:
                    continue
                majority_arg, majority_count = pos_counter.most_common(1)[0]
                freq = majority_count / total
                if freq >= self.MAJORITY_THRESHOLD and majority_arg != edit_arg:
                    confidence = freq * (1.0 - _levenshtein_similarity(edit_arg, majority_arg))
                    if confidence >= self.CONFIDENCE_FLOOR:
                        findings.append(SemanticEvidence(
                            kind="call_site_voting",
                            file_path=file_path,
                            line=line_no,
                            message=(
                                f"{majority_count}/{total} call sites of {func_name}() "
                                f"pass {majority_arg} at pos {pos + 1} -- edit passes {edit_arg}"
                            ),
                            confidence=min(confidence, 0.95),
                        ))

            # Detect suspected argument swaps (only 2-arg calls for now)
            if len(edit_args) == 2:
                a0, a1 = edit_args[0], edit_args[1]
                if a0 is None or a1 is None:
                    continue
                # Count sites where args appear in reversed order
                swap_count = sum(
                    1 for s in sites
                    if len(s.args) == 2
                    and s.args[0] == a1
                    and s.args[1] == a0
                )
                match_count = sum(
                    1 for s in sites
                    if len(s.args) == 2
                    and s.args[0] == a0
                    and s.args[1] == a1
                )
                two_arg_total = swap_count + match_count
                if two_arg_total >= self.MIN_SITES and swap_count > match_count:
                    freq = swap_count / two_arg_total
                    if freq >= self.MAJORITY_THRESHOLD:
                        confidence = freq * 0.9
                        if confidence >= self.CONFIDENCE_FLOOR:
                            findings.append(SemanticEvidence(
                                kind="call_site_swap",
                                file_path=file_path,
                                line=line_no,
                                message=(
                                    f"suspected arg swap at {func_name}({a0}, {a1}) -- "
                                    f"majority passes ({a1}, {a0})"
                                ),
                                confidence=min(confidence, 0.92),
                            ))

        return findings


def _levenshtein_similarity(a: str, b: str) -> float:
    """Return similarity in [0, 1] based on Levenshtein distance."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    dist = prev[n]
    return 1.0 - dist / max(m, n)

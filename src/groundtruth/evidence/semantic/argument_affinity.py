"""Argument affinity signal.

Research basis: Hungarian algorithm on edit distances between argument
names and parameter names (Rice et al., OOPSLA 2017).

For each function call in the diff, find the function definition via git
grep, extract parameter names, compute the edit-distance matrix between
the actual argument names and the parameter names, then check whether a
different ordering of the arguments would produce a lower total distance.
If so, flag the call as potentially misordered.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time

from .call_site_voting import (
    SemanticEvidence,
    _extract_diff_calls,
    _git_env,
)


# ---------------------------------------------------------------------------
# Greedy minimum-cost matching (sufficient for k < 10 parameters)
# ---------------------------------------------------------------------------


def _edit_distance(a: str, b: str) -> int:
    """Standard Levenshtein distance."""
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[n]


def _greedy_optimal_assignment(args: list[str], params: list[str]) -> list[int]:
    """Greedy min-cost bipartite matching: returns param index for each arg position.

    Uses a simple greedy approach that repeatedly picks the lowest-cost
    unmatched (arg, param) pair — adequate for k < 10.
    """
    k = min(len(args), len(params))
    used_params: set[int] = set()
    assignment: list[int] = [-1] * k

    # Build cost matrix
    costs = [[_edit_distance(args[i], params[j]) for j in range(len(params))] for i in range(k)]

    # Greedy assignment
    for _ in range(k):
        best_cost = 10**9
        best_i = best_j = -1
        for i in range(k):
            if assignment[i] != -1:
                continue
            for j in range(len(params)):
                if j in used_params:
                    continue
                if costs[i][j] < best_cost:
                    best_cost = costs[i][j]
                    best_i, best_j = i, j
        if best_i == -1:
            break
        assignment[best_i] = best_j
        used_params.add(best_j)

    return assignment


def _identity_cost(args: list[str], params: list[str]) -> int:
    """Cost of using args in the same order as params (identity mapping)."""
    k = min(len(args), len(params))
    return sum(_edit_distance(args[i], params[i]) for i in range(k))


def _optimal_cost(args: list[str], params: list[str]) -> tuple[int, list[int]]:
    """Return (optimal_cost, optimal_assignment) via greedy matching."""
    assignment = _greedy_optimal_assignment(args, params)
    k = min(len(args), len(params))
    cost = sum(
        _edit_distance(args[i], params[assignment[i]]) for i in range(k) if assignment[i] != -1
    )
    return cost, assignment


# ---------------------------------------------------------------------------
# Function definition finder
# ---------------------------------------------------------------------------


def _find_function_def(root: str, func_name: str, deadline: float) -> list[str] | None:
    """Return parameter names for func_name found anywhere in the repo.

    Uses git grep to find function definition lines, then parses the signature.
    Works for any language — uses AST for Python, regex for others.
    """
    # Search for function definitions across all languages
    try:
        result = subprocess.run(
            ["git", "grep", "-n", "-E", "--", f"(def |func |function |fn |fun ){func_name}\\("],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
            env=_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    for raw_line in result.stdout.splitlines():
        if time.time() > deadline:
            break
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        rel_path, _, content = parts
        stub = content.strip()

        # Python: use AST for precise param extraction
        if rel_path.endswith(".py") and stub.startswith("def "):
            try:
                tree = ast.parse(stub + "\n    pass", mode="exec")
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name != func_name:
                    continue
                params = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
                if params:
                    return params
            continue

        # Other languages: regex param extraction from signature
        params = _extract_params_regex(stub, func_name)
        if params:
            return params

    return None


def _extract_params_regex(line: str, func_name: str) -> list[str] | None:
    """Extract parameter names from a function signature line via regex."""
    # Match: funcName(param1, param2, param3)
    m = re.search(rf"{re.escape(func_name)}\s*\(([^)]*)\)", line)
    if not m:
        return None
    params_str = m.group(1).strip()
    if not params_str:
        return []
    params = []
    for p in params_str.split(","):
        p = p.strip()
        if not p:
            continue
        # Strip type annotations: "name: Type", "Type name", "name Type"
        # Take the first word-like token that looks like a param name
        # Skip "self", "cls", "this"
        tokens = re.split(r"[\s:=]+", p)
        for tok in tokens:
            tok = tok.strip("*&")  # strip pointer/ref markers
            if tok and tok[0].islower() and tok not in ("self", "cls", "this", "mut"):
                params.append(tok)
                break
    return params if params else None


# ---------------------------------------------------------------------------
# Main checker
# ---------------------------------------------------------------------------


class ArgumentAffinityChecker:
    """Detect mismatched argument-parameter ordering via edit distance."""

    MIN_IMPROVEMENT_FRACTION = 0.25  # optimal must be ≥25% better than identity
    CONFIDENCE_CAP = 0.90
    CONFIDENCE_FLOOR = 0.65

    def analyze(
        self, root: str, diff_text: str, time_budget: float = 3.0
    ) -> list[SemanticEvidence]:
        deadline = time.time() + time_budget
        findings: list[SemanticEvidence] = []

        diff_calls = _extract_diff_calls(diff_text)
        if not diff_calls:
            return findings

        # De-duplicate by func_name so we only grep each definition once
        seen_funcs: dict[str, list[str] | None] = {}

        for file_path, line_no, func_name, raw_edit_args in diff_calls:
            if time.time() > deadline:
                break

            # Resolve arg names; skip calls with all-None args
            edit_args = [a for a in raw_edit_args if a is not None]
            if len(edit_args) < 2:
                continue

            # Find the function definition
            if func_name not in seen_funcs:
                seen_funcs[func_name] = _find_function_def(root, func_name, deadline)
            params = seen_funcs[func_name]
            if not params or len(params) < 2:
                continue

            k = min(len(edit_args), len(params))
            if k < 2:
                continue

            args_k = edit_args[:k]
            params_k = params[:k]

            id_cost = _identity_cost(args_k, params_k)
            opt_cost, assignment = _optimal_cost(args_k, params_k)

            # No cost improvement worth reporting
            if id_cost == 0 or opt_cost >= id_cost:
                continue

            improvement = (id_cost - opt_cost) / id_cost
            if improvement < self.MIN_IMPROVEMENT_FRACTION:
                continue

            # Check that optimal assignment differs from identity
            if all(assignment[i] == i for i in range(k)):
                continue

            # Build suggested reordering
            suggested_order = [
                args_k[assignment.index(j)] if j in assignment else "?" for j in range(k)
            ]

            confidence = min(improvement * 0.9, self.CONFIDENCE_CAP)
            if confidence < self.CONFIDENCE_FLOOR:
                continue

            findings.append(
                SemanticEvidence(
                    kind="arg_affinity",
                    file_path=file_path,
                    line=line_no,
                    message=(
                        f"arg order may be wrong in {func_name}({', '.join(args_k)}) -- "
                        f"parameter names suggest ({', '.join(suggested_order)})"
                    ),
                    confidence=confidence,
                )
            )

        return findings

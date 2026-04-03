"""Post-edit hook v4 -- 5 evidence families synthesized into 0-3 lines.

Called by OpenHands PostToolUse hook on file_editor operations.
Composes: CHANGE + CONTRACT + PATTERN + STRUCTURAL + SEMANTIC evidence.
Outputs evidence items (not errors) to stdout. Logs per-family detail to JSONL.

Usage:
    python -m groundtruth.hooks.post_edit --root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import subprocess
import time

from groundtruth.hooks.logger import log_hook


def _git_env() -> dict[str, str]:
    """Git environment that handles safe.directory in containers."""
    import copy
    env: dict[str, str] = dict(copy.copy(os.environ))
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    return env


def _detect_workspace_root(provided_root: str) -> str:
    """Detect the actual workspace root dynamically.

    1. Try git rev-parse --show-toplevel from the provided root.
    2. If that fails, scan /workspace/*/ for a .git directory.
    3. Fall back to the provided root.
    """
    # Step 1: try git rev-parse from the provided root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=provided_root, timeout=5,
            env=_git_env(),
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return toplevel
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, NotADirectoryError):
        pass

    # Step 2: scan /workspace/*/ for a .git directory
    try:
        workspace_dirs = _glob.glob("/workspace/*/")
        for candidate in sorted(workspace_dirs):
            if os.path.isdir(os.path.join(candidate, ".git")):
                return candidate.rstrip("/")
    except OSError:
        pass

    # Step 3: fall back to the provided root
    return provided_root


def _is_view_operation() -> bool:
    """Return True if the current hook invocation is for a view-only operation.

    OpenHands sets TOOL_INPUT or OPENHANDS_TOOL_INPUT to a JSON payload
    containing the tool arguments. If the payload has {"command": "view"}
    we skip all processing — no diff was produced.
    """
    for env_var in ("TOOL_INPUT", "OPENHANDS_TOOL_INPUT"):
        raw = os.environ.get(env_var, "")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("command") == "view":
                return True
        except (json.JSONDecodeError, ValueError):
            pass
    return False


_SUPPORTED_EXTENSIONS = frozenset({
    ".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java",
    ".kt", ".kts", ".scala", ".cs", ".php", ".swift", ".c", ".h",
    ".cpp", ".cc", ".cxx", ".hpp", ".rb", ".ex", ".exs", ".lua",
    ".ml", ".groovy", ".gradle", ".mjs", ".cjs",
})


def _get_modified_files(root: str) -> list[str]:
    """Get modified source files from git diff (all supported languages)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, cwd=root, timeout=10,
            env=_git_env(),
        )
        return [f.strip() for f in result.stdout.strip().split("\n")
                if f.strip() and os.path.splitext(f.strip())[1].lower() in _SUPPORTED_EXTENSIONS]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_diff_text(root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, cwd=root, timeout=10,
            env=_git_env(),
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _read_file(root: str, relpath: str) -> str:
    try:
        with open(os.path.join(root, relpath), "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _extract_changed_func_names(diff_text: str) -> dict[str, list[str]]:
    """Parse diff to find changed function names per file.

    Returns dict: filepath -> list of function names in changed line ranges.
    """
    import ast as _ast

    # Parse diff for file + line ranges
    changes: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file and os.path.splitext(current_file)[1].lower() in _SUPPORTED_EXTENSIONS:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                changes.setdefault(current_file, []).append((start, start + count - 1))

    # Map line ranges to function names
    result: dict[str, list[str]] = {}
    for fpath, ranges in changes.items():
        # We'd need to parse the CURRENT file to find functions at those lines
        # This is done by the caller who has the AST
        result[fpath] = []  # Populated later when we have the source

    return result


def _find_funcs_at_lines(source: str, line_ranges: list[tuple[int, int]],
                         file_path: str = "", store=None) -> list[str]:
    """Find function/method names that overlap with given line ranges.

    Uses graph.db node positions when available, falls back to Python AST.
    """
    # Path 1: graph.db (language-agnostic)
    if store and file_path:
        try:
            funcs = store.get_functions_in_file(file_path)
            if funcs:
                names = []
                for func in funcs:
                    fs, fe = func["start_line"], func["end_line"]
                    for ls, le in line_ranges:
                        if fs <= le and ls <= fe:
                            names.append(func["name"])
                            break
                if names:
                    return names
        except Exception:
            pass

    # Path 2: Python AST (for .py files)
    if file_path.endswith(".py") or not file_path:
        import ast as _ast
        try:
            tree = _ast.parse(source)
        except SyntaxError:
            return []
        func_names = []
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func_start = node.lineno
                func_end = getattr(node, "end_lineno", func_start + 50)
                for ls, le in line_ranges:
                    if func_start <= le and ls <= func_end:
                        func_names.append(node.name)
                        break
        return func_names

    # Path 3: Regex fallback for non-Python without graph.db
    func_names = []
    lines = source.splitlines()
    func_pattern = re.compile(
        r'\s*(?:(?:pub\s+)?(?:async\s+)?(?:def|func|function|fn|fun)\s+)(\w+)'
    )
    for ls, le in line_ranges:
        for i in range(max(0, ls - 10), min(len(lines), le + 5)):
            m = func_pattern.match(lines[i] if i < len(lines) else "")
            if m and m.group(1) not in func_names:
                func_names.append(m.group(1))
    return func_names


def _apply_abstention(findings: list, min_confidence: float = 0.65) -> list:
    """Universal abstention across all evidence families."""
    passed = []
    for f in findings:
        conf = getattr(f, "confidence", 0)
        if conf < min_confidence:
            continue
        # Skip private methods
        msg = getattr(f, "message", "")
        if msg.startswith("_") and not msg.startswith("__init__"):
            continue
        passed.append(f)
    return passed


def _format_evidence(item) -> str:
    """Format a single evidence item as a compact one-liner."""
    family = getattr(item, "family", "?")

    # CallerExpectation: "3 callers destructure return as (x, y)"
    if hasattr(item, "usage_type"):
        detail = getattr(item, "detail", "")
        return f"GT: {detail} [{family}]"

    # TestExpectation: "test_serialize:42 asserts format X"
    if hasattr(item, "assertion_type"):
        test_func = getattr(item, "test_func", "test")
        line = getattr(item, "line", "?")
        assertion = getattr(item, "assertion_type", "")
        expected = getattr(item, "expected", "")[:60]
        return f"GT: {test_func}:{line} {assertion} {expected} [{family}]"

    # PatternEvidence, ChangeEvidence, StructuralEvidence: have "message"
    msg = getattr(item, "message", str(item))
    if len(msg) > 140:
        msg = msg[:137] + "..."
    return f"GT: {msg} [{family}]"


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-edit verify hook v4")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-items", type=int, default=3)
    args = parser.parse_args()

    start = time.time()

    # Skip view operations immediately — no diff was produced
    if _is_view_operation():
        return

    # Detect the actual workspace root (handles /testbed vs /workspace/django/ etc.)
    root = _detect_workspace_root(args.root)

    log_entry = {
        "hook": "post_edit",
        "endpoint": "verify",
        "root": root,
        "root_provided": args.root,
        "evidence": {},
    }

    modified_files = _get_modified_files(root)
    if not modified_files:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_entry["output"] = ""
        log_hook(log_entry)
        return

    log_entry["files_changed"] = modified_files
    diff_text = _get_diff_text(root)

    # Open GraphStore for language-agnostic evidence (v16+)
    graph_store = None
    try:
        from groundtruth.index.graph_store import GraphStore, is_graph_db
        if os.path.exists(args.db) and is_graph_db(args.db):
            graph_store = GraphStore(args.db)
            graph_store.initialize()
    except Exception:
        graph_store = None

    # Parse diff for changed line ranges per file
    diff_ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file and os.path.splitext(current_file)[1].lower() in _SUPPORTED_EXTENSIONS:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                s = int(match.group(1))
                c = int(match.group(2)) if match.group(2) else 1
                diff_ranges.setdefault(current_file, []).append((s, s + c - 1))

    # Find changed function names per file
    changed_funcs: dict[str, list[str]] = {}
    for fpath, ranges in diff_ranges.items():
        source = _read_file(root, fpath)
        if source:
            changed_funcs[fpath] = _find_funcs_at_lines(
                source, ranges, file_path=fpath, store=graph_store
            )

    all_findings = []

    # === EVIDENCE FAMILY 1: CHANGE (before/after AST diff) ===
    change_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.change import ChangeAnalyzer
        analyzer = ChangeAnalyzer(store=graph_store)
        change_items = analyzer.analyze(root, diff_text)
        change_signal["ran"] = True
        change_signal["items_found"] = len(change_items)
        all_findings.extend(change_items)
    except Exception as e:
        import traceback
        change_signal["error"] = str(e)
        change_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["change"] = change_signal

    # === EVIDENCE FAMILY 2: CONTRACT (caller usage + test assertions) ===
    contract_signal = {"ran": False, "callers_analyzed": 0, "tests_analyzed": 0, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.contract import CallerUsageMiner, TestAssertionMiner

        caller_miner = CallerUsageMiner(root, store=graph_store)
        test_miner = TestAssertionMiner(root, store=graph_store)

        # Try to get caller info from index
        caller_files: list[str] = []
        test_files: list[str] = []
        try:
            from groundtruth.index.store import SymbolStore
            store = SymbolStore(args.db)
            store.initialize()
            for fpath in modified_files:
                result = store.get_importers_of_file(fpath)
                importers = getattr(result, "value", []) or []
                if importers:
                    for imp in importers:
                        if "test" in imp.lower():
                            test_files.append(imp)
                        else:
                            caller_files.append(imp)
        except Exception:
            pass

        contract_signal["callers_analyzed"] = len(caller_files)
        contract_signal["tests_analyzed"] = len(test_files)

        # Mine caller expectations for each changed function
        for fpath, funcs in changed_funcs.items():
            for func_name in funcs:
                caller_items = caller_miner.mine(func_name, caller_files)
                all_findings.extend(caller_items)

        # Mine test assertions (pass function names for targeted graph.db queries)
        for fpath in modified_files:
            funcs = changed_funcs.get(fpath, [])
            for func_name in (funcs or [None]):
                test_items = test_miner.mine(fpath, test_files, symbol_name=func_name)
                all_findings.extend(test_items)

        contract_signal["ran"] = True
        contract_signal["items_found"] = sum(1 for f in all_findings if getattr(f, "family", "") == "contract")
    except Exception as e:
        import traceback
        contract_signal["error"] = str(e)
        contract_signal["traceback"] = traceback.format_exc()
    log_entry["evidence"]["contract"] = contract_signal

    # === EVIDENCE FAMILY 3: PATTERN (sibling analysis) ===
    pattern_signal = {"ran": False, "siblings_found": 0, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.pattern import SiblingAnalyzer
        sibling_analyzer = SiblingAnalyzer(store=graph_store)

        for fpath, funcs in changed_funcs.items():
            source = _read_file(root, fpath)
            if not source:
                continue
            for func_name in funcs:
                pattern_items = sibling_analyzer.analyze(source, func_name, file_path=fpath)
                all_findings.extend(pattern_items)

        pattern_signal["ran"] = True
        pattern_signal["items_found"] = sum(1 for f in all_findings if getattr(f, "family", "") == "pattern")
    except Exception as e:
        pattern_signal["error"] = str(e)
    log_entry["evidence"]["pattern"] = pattern_signal

    # === EVIDENCE FAMILY 4: STRUCTURAL (obligations + contradictions + conventions) ===
    structural_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.structural import run_obligations, run_contradictions, run_conventions

        store = None
        graph = None
        try:
            from groundtruth.index.store import SymbolStore
            from groundtruth.index.graph import ImportGraph
            store = SymbolStore(args.db)
            store.initialize()
            graph = ImportGraph(store)
        except Exception:
            pass

        struct_items = []
        if store and graph and diff_text:
            struct_items.extend(run_obligations(store, graph, diff_text))
        if store:
            struct_items.extend(run_contradictions(store, root, modified_files))
        struct_items.extend(run_conventions(root, modified_files))

        structural_signal["ran"] = True
        structural_signal["items_found"] = len(struct_items)
        all_findings.extend(struct_items)
    except Exception as e:
        structural_signal["error"] = str(e)
    log_entry["evidence"]["structural"] = structural_signal

    # === EVIDENCE FAMILY 5: SEMANTIC (call-site voting + arg affinity + guard consistency) ===
    semantic_signal: dict = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.semantic.call_site_voting import CallSiteVoter
        from groundtruth.evidence.semantic.argument_affinity import ArgumentAffinityChecker
        from groundtruth.evidence.semantic.guard_consistency import GuardConsistencyChecker

        voter = CallSiteVoter()
        affinity = ArgumentAffinityChecker()
        guard = GuardConsistencyChecker()

        semantic_items = []
        remaining_time = max(2.0, 8.0 - (time.time() - start))

        if diff_text:
            semantic_items.extend(voter.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(affinity.analyze(root, diff_text, time_budget=remaining_time / 3))
            semantic_items.extend(guard.analyze(root, diff_text, time_budget=remaining_time / 3))

        semantic_signal["ran"] = True
        semantic_signal["items_found"] = len(semantic_items)
        all_findings.extend(semantic_items)
    except Exception as e:
        semantic_signal["error"] = str(e)
    log_entry["evidence"]["semantic"] = semantic_signal

    # === ABSTENTION ===
    passed = _apply_abstention(all_findings)

    # Update after_abstention counts per family
    for family_name in ("change", "contract", "pattern", "structural", "semantic"):
        count = sum(1 for f in passed if getattr(f, "family", "") == family_name)
        log_entry["evidence"].get(family_name, {})["after_abstention"] = count

    log_entry["abstention_summary"] = {
        "total_raw": len(all_findings),
        "total_emitted": len(passed),
        "total_suppressed": len(all_findings) - len(passed),
    }

    # === FORMAT OUTPUT ===
    output_lines = []
    if passed:
        # Sort by confidence descending, take top N
        passed.sort(key=lambda f: -getattr(f, "confidence", 0))
        for item in passed[:args.max_items]:
            output_lines.append(_format_evidence(item))

    output = "\n".join(output_lines)
    log_entry["output"] = output
    log_entry["output_lines"] = len(output_lines)
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)

    if output:
        print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass

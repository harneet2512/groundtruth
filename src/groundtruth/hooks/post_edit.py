"""Post-edit hook v3 -- 4 evidence families synthesized into 0-3 lines.

Called by OpenHands PostToolUse hook on file_editor operations.
Composes: CHANGE + CONTRACT + PATTERN + STRUCTURAL evidence.
Outputs evidence items (not errors) to stdout. Logs per-family detail to JSONL.

Usage:
    python -m groundtruth.hooks.post_edit --root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import time

from groundtruth.hooks.logger import log_hook


def _get_modified_files(root: str) -> list[str]:
    """Get modified .py files from git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, cwd=root, timeout=10,
        )
        return [f.strip() for f in result.stdout.strip().split("\n")
                if f.strip().endswith(".py")]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _get_diff_text(root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, cwd=root, timeout=10,
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
        elif line.startswith("@@") and current_file and current_file.endswith(".py"):
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


def _find_funcs_at_lines(source: str, line_ranges: list[tuple[int, int]]) -> list[str]:
    """Find function/method names that overlap with given line ranges."""
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
    msg = getattr(item, "message", str(item))
    # Truncate to 150 chars
    if len(msg) > 140:
        msg = msg[:137] + "..."
    return f"GT: {msg} [{family}]"


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-edit verify hook v3")
    parser.add_argument("--root", default="/testbed")
    parser.add_argument("--db", default="/tmp/gt_index.db")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--max-items", type=int, default=3)
    args = parser.parse_args()

    start = time.time()
    log_entry = {
        "hook": "post_edit",
        "endpoint": "verify",
        "root": args.root,
        "evidence": {},
    }

    modified_files = _get_modified_files(args.root)
    if not modified_files:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_entry["output"] = ""
        log_hook(log_entry)
        return

    log_entry["files_changed"] = modified_files
    diff_text = _get_diff_text(args.root)

    # Parse diff for changed line ranges per file
    diff_ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@") and current_file and current_file.endswith(".py"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                s = int(match.group(1))
                c = int(match.group(2)) if match.group(2) else 1
                diff_ranges.setdefault(current_file, []).append((s, s + c - 1))

    # Find changed function names per file
    changed_funcs: dict[str, list[str]] = {}
    for fpath, ranges in diff_ranges.items():
        source = _read_file(args.root, fpath)
        if source:
            changed_funcs[fpath] = _find_funcs_at_lines(source, ranges)

    all_findings = []

    # === EVIDENCE FAMILY 1: CHANGE (before/after AST diff) ===
    change_signal = {"ran": False, "items_found": 0, "after_abstention": 0}
    try:
        from groundtruth.evidence.change import ChangeAnalyzer
        analyzer = ChangeAnalyzer()
        change_items = analyzer.analyze(args.root, diff_text)
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

        caller_miner = CallerUsageMiner(args.root)
        test_miner = TestAssertionMiner(args.root)

        # Try to get caller info from index
        caller_files: list[str] = []
        test_files: list[str] = []
        try:
            from groundtruth.index.store import SymbolStore
            store = SymbolStore(args.db)
            store.initialize()
            for fpath in modified_files:
                result = store.get_importers_of_file(fpath)
                if hasattr(result, "value"):
                    importers = result.value if hasattr(result, "value") else []
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

        # Mine test assertions
        for fpath in modified_files:
            test_items = test_miner.mine(fpath, test_files)
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
        sibling_analyzer = SiblingAnalyzer()

        for fpath, funcs in changed_funcs.items():
            source = _read_file(args.root, fpath)
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
            struct_items.extend(run_contradictions(store, args.root, modified_files))
        struct_items.extend(run_conventions(args.root, modified_files))

        structural_signal["ran"] = True
        structural_signal["items_found"] = len(struct_items)
        all_findings.extend(struct_items)
    except Exception as e:
        structural_signal["error"] = str(e)
    log_entry["evidence"]["structural"] = structural_signal

    # === ABSTENTION ===
    passed = _apply_abstention(all_findings)

    # Update after_abstention counts per family
    for family_name in ("change", "contract", "pattern", "structural"):
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

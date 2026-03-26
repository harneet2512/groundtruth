"""Post-edit hook — runs verify pipeline after file edits.

Called by OpenHands PostToolUse hook on file_editor operations.
Composes: ObligationEngine + ContradictionDetector + ConventionChecker + AbstentionPolicy.
Outputs 0-3 compact findings to stdout. Logs per-signal detail to JSONL.

Usage:
    python -m groundtruth.hooks.post_edit --root=/testbed --db=/tmp/gt_index.db --quiet --max-items=3
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
import time

# Ensure the hooks logger is used instead of structlog-dependent one
from groundtruth.hooks.logger import get_logger, log_hook

log = get_logger("hooks.post_edit")


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
    """Get unified diff text."""
    try:
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, cwd=root, timeout=10,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _read_file(root: str, relpath: str) -> str:
    """Read a source file, returning empty string on failure."""
    try:
        with open(os.path.join(root, relpath), "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _run_obligations(store, graph, diff_text: str, modified_files: list[str]) -> dict:
    """Run ObligationEngine. Returns signal log dict."""
    signal = {"ran": False, "raw_findings": 0, "after_abstention": 0, "findings": []}
    try:
        from groundtruth.validators.obligations import ObligationEngine
        engine = ObligationEngine(store, graph)
        obligations = engine.infer_from_patch(diff_text)
        signal["ran"] = True
        signal["raw_findings"] = len(obligations)
        for ob in obligations:
            signal["findings"].append({
                "target": ob.target,
                "target_file": ob.target_file,
                "target_line": ob.target_line,
                "kind": ob.kind,
                "reason": ob.reason,
                "confidence": ob.confidence,
            })
    except Exception as e:
        signal["error"] = str(e)
    return signal


def _run_contradictions(store, root: str, modified_files: list[str]) -> dict:
    """Run ContradictionDetector. Returns signal log dict."""
    signal = {"ran": False, "raw_findings": 0, "after_abstention": 0, "findings": []}
    try:
        from groundtruth.validators.contradictions import ContradictionDetector
        detector = ContradictionDetector(store)
        for fpath in modified_files[:5]:  # cap at 5 files
            source = _read_file(root, fpath)
            if not source:
                continue
            contras = detector.check_file(fpath, source)
            signal["ran"] = True
            signal["raw_findings"] += len(contras)
            for c in contras:
                signal["findings"].append({
                    "kind": c.kind,
                    "file": c.file_path,
                    "line": c.line,
                    "message": c.message,
                    "confidence": c.confidence,
                })
    except Exception as e:
        signal["error"] = str(e)
    return signal


def _run_conventions(root: str, modified_files: list[str]) -> dict:
    """Run ConventionChecker. Returns signal log dict."""
    signal = {"ran": False, "raw_findings": 0, "after_abstention": 0, "findings": []}
    try:
        from groundtruth.analysis.conventions import detect_all
        for fpath in modified_files[:5]:
            source = _read_file(root, fpath)
            if not source:
                continue
            conventions = detect_all(source, scope=fpath)
            signal["ran"] = True
            # Compare: find conventions where frequency < 1.0 (the edit breaks the pattern)
            for conv in conventions:
                if conv.frequency < 1.0 and conv.confidence >= 0.6:
                    signal["raw_findings"] += 1
                    signal["findings"].append({
                        "kind": conv.kind,
                        "scope": conv.scope,
                        "pattern": conv.pattern,
                        "frequency": conv.frequency,
                        "confidence": conv.confidence,
                    })
    except Exception as e:
        signal["error"] = str(e)
    return signal


def _apply_abstention(findings: list[dict], is_contradiction: bool = False) -> list[dict]:
    """Apply abstention policy. Returns findings that pass."""
    try:
        from groundtruth.policy.abstention import AbstentionPolicy, TrustTier
        policy = AbstentionPolicy()
        passed = []
        for f in findings:
            conf = f.get("confidence", 0)
            # Use YELLOW trust for AST-only findings
            trust = TrustTier.YELLOW
            if policy.should_emit(trust, evidence_count=1, coverage=5.0):
                if conf >= 0.7:
                    passed.append(f)
        return passed
    except Exception:
        # If abstention module fails, be conservative — emit nothing
        return []


def _format_finding(f: dict) -> str:
    """Format a single finding as a compact one-liner."""
    kind = f.get("kind", "unknown")
    if "target" in f:
        # Obligation
        target = f["target"]
        line = f.get("target_line", "?")
        reason = f.get("reason", "")[:80]
        return f"{target}:{line} ({reason})"
    elif "message" in f:
        # Contradiction
        msg = f["message"][:100]
        line = f.get("line", "?")
        return f"line {line}: {msg}"
    elif "pattern" in f:
        # Convention
        pattern = f["pattern"][:80]
        freq = f.get("frequency", 0)
        return f"convention: {pattern} ({freq:.0%} of siblings)"
    return str(f)[:100]


def main() -> None:
    parser = argparse.ArgumentParser(description="GT post-edit verify hook")
    parser.add_argument("--root", default="/testbed", help="Repository root")
    parser.add_argument("--db", default="/tmp/gt_index.db", help="Index database path")
    parser.add_argument("--quiet", action="store_true", help="Compact output mode")
    parser.add_argument("--max-items", type=int, default=3, help="Max findings to show")
    args = parser.parse_args()

    start = time.time()
    log_entry = {
        "hook": "post_edit",
        "endpoint": "verify",
        "root": args.root,
        "signals": {},
    }

    # Get modified files
    modified_files = _get_modified_files(args.root)
    if not modified_files:
        log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
        log_entry["output"] = ""
        log_hook(log_entry)
        return

    log_entry["files_changed"] = modified_files
    diff_text = _get_diff_text(args.root)

    # Initialize store (lazy — may fail in some containers)
    store = None
    graph = None
    try:
        from groundtruth.index.store import SymbolStore
        from groundtruth.index.graph import ImportGraph
        store = SymbolStore(args.db)
        result = store.initialize()
        graph = ImportGraph(store)
    except Exception as e:
        log_entry["store_error"] = str(e)

    # Run signals
    all_findings = []

    # 1. Obligations (needs store + graph)
    if store and graph and diff_text:
        ob_signal = _run_obligations(store, graph, diff_text, modified_files)
        log_entry["signals"]["obligations"] = ob_signal
        for f in ob_signal["findings"]:
            f["_source"] = "obligation"
            all_findings.append(f)

    # 2. Contradictions (needs store)
    if store:
        ct_signal = _run_contradictions(store, args.root, modified_files)
        log_entry["signals"]["contradictions"] = ct_signal
        for f in ct_signal["findings"]:
            f["_source"] = "contradiction"
            all_findings.append(f)

    # 3. Conventions (pure AST — always runs)
    cv_signal = _run_conventions(args.root, modified_files)
    log_entry["signals"]["conventions"] = cv_signal
    for f in cv_signal["findings"]:
        f["_source"] = "convention"
        all_findings.append(f)

    # Apply abstention
    passed = _apply_abstention(all_findings)

    # Update signal logs with after_abstention counts
    for signal_name in ("obligations", "contradictions", "conventions"):
        sig = log_entry["signals"].get(signal_name, {})
        source_name = signal_name.rstrip("s")  # obligation, contradiction, convention
        if source_name == "obligation":
            source_name = "obligation"
        sig["after_abstention"] = sum(
            1 for f in passed if f.get("_source", "").startswith(source_name[:5])
        )

    log_entry["abstention_summary"] = {
        "total_raw": len(all_findings),
        "total_emitted": len(passed),
        "total_suppressed": len(all_findings) - len(passed),
    }

    # Format output
    output = ""
    if passed:
        top = passed[:args.max_items]
        parts = [_format_finding(f) for f in top]
        n = len(top)
        source_types = set(f.get("_source", "") for f in top)
        if "obligation" in source_types:
            prefix = f"GT: {n} uncovered"
        elif "contradiction" in source_types:
            prefix = f"GT: {n} conflict(s)"
        else:
            prefix = f"GT: {n} finding(s)"
        output = prefix + " -- " + ", ".join(parts)
        # Cap at 200 chars
        if len(output) > 200:
            output = output[:197] + "..."

    log_entry["output"] = output
    log_entry["output_lines"] = 1 if output else 0
    log_entry["wall_time_ms"] = int((time.time() - start) * 1000)
    log_hook(log_entry)

    if output:
        print(output)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never crash — silent exit
        pass

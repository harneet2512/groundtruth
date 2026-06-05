"""groundtruth.hooks.drift_cli — the contract-DRIFT engine (the "build_drift" payload).

ONE engine, TWO transports (see docs/MINISWE_CONTRACT_DRIFT_INTEGRATION_20260605.md):
  - OpenHands PUSH: the OH wrapper calls build_drift() and injects the block.
  - mini-swe PULL:  the agent runs `gt drift <file>` -> this module's CLI -> build_drift().

What it does: compares the CURRENT behavioral contract of the edited file(s) against the
session-start baseline graph (``<db>.orig``, frozen by the harness at standup) and reports
contract changes that *callers depend on* — altered return shape, changed raised
exceptions, a dropped guard/precondition — with the verified caller count for the symbol.

Invariants (non-negotiable):
  * ZERO test contact — never reads/runs tests, FAIL_TO_PASS/PASS_TO_PASS, or gold.
  * NO execution — never runs the patch/build; reads the code graph (graph.db) only.
  * ADVISORY — prints a text block; never edits the tree, never blocks.
  * DETERMINISTIC — same graphs + file -> identical block (no LLM, no randomness, no net).
  * LANGUAGE-AGNOSTIC — uses graph property kinds present across languages.
  * CORRECT-OR-QUIET — emits nothing when there is no real, caller-affecting drift.

CLI (the contract pier's `gt drift` shim invokes):
    python3 -m groundtruth.hooks.drift_cli --root <repo> --db <graph.db> \
        [--file <path> ...] | [--all-modified]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess

# Behavioral-contract property kinds (language-agnostic; see gt_gt.md §2.4).
_RETURN_KINDS = ("return_shape",)
_RAISE_KINDS = ("exception_flow", "exception_type")
_GUARD_KINDS = ("guard_clause", "conditional_return")
# Verified-edge boundary (gt_gt.md §2.3 EDGE_CONFIDENCE_FLOOR) — a caller "depends on" the
# contract only if the edge is a deterministic fact, never a name_match guess.
_EDGE_FLOOR = 0.7


def _norm(path: str, root: str) -> str:
    """Repo-relative, forward-slash path — matches how gt-index stores file_path."""
    p = path.replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if r and p.startswith(r + "/"):
        p = p[len(r) + 1:]
    return p.lstrip("./")


def _git_modified(root: str) -> list[str]:
    """Source files changed in the working tree (for --all-modified). git only — no exec."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "diff", "--name-only", "--diff-filter=ACMR"],
            capture_output=True, text=True, timeout=15,
        )
        files = [f.strip() for f in (out.stdout or "").splitlines() if f.strip()]
        return files
    except (OSError, subprocess.SubprocessError):
        return []


def _reindex(gt_index: str, root: str, db: str, rel: str) -> None:
    """Incrementally re-index ONE edited file into the CURRENT db (gt-index -file).

    This is what makes the current db reflect the edit; the frozen <db>.orig is the
    baseline. Best-effort: a missing/failed binary leaves the db unchanged -> the diff
    finds no drift (correct-or-quiet), it never errors the agent.
    """
    if not gt_index:
        return
    abs_path = os.path.join(root, rel)
    if not os.path.exists(abs_path):
        return
    try:
        subprocess.run(
            [gt_index, "-file", abs_path, "-root", root, "-output", db],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _func_names_in_file(con: sqlite3.Connection, rel: str) -> list[str]:
    """DISTINCT function/method names defined in `rel`. Distinct because a name can recur
    in a file (e.g. a Go interface method on several receivers); contracts are compared at
    (file, name) granularity, so iterate each name ONCE to avoid duplicate findings."""
    try:
        rows = con.execute(
            "SELECT DISTINCT name FROM nodes "
            "WHERE file_path = ? AND label IN ('Function','Method') "
            "AND COALESCE(is_test,0)=0",
            (rel,),
        ).fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []


def _contract_for(con: sqlite3.Connection, rel: str, name: str, kinds: tuple) -> set[str]:
    """The set of contract property VALUES of `kinds` for function `name` in `rel`.

    Keyed by (file, name) so it is comparable across the baseline and current graphs even
    though node ids differ between the two databases.
    """
    if not kinds:
        return set()
    placeholders = ",".join("?" for _ in kinds)
    try:
        rows = con.execute(
            f"SELECT DISTINCT TRIM(p.value) FROM properties p "
            f"JOIN nodes n ON n.id = p.node_id "
            f"WHERE n.file_path = ? AND n.name = ? AND p.kind IN ({placeholders}) "
            f"AND TRIM(COALESCE(p.value,'')) != ''",
            (rel, name, *kinds),
        ).fetchall()
        return {str(r[0]) for r in rows}
    except sqlite3.Error:
        return set()


def _verified_caller_count(con: sqlite3.Connection, rel: str, name: str) -> int:
    """Distinct caller FILES via VERIFIED incoming CALLS edges (excludes name_match)."""
    try:
        row = con.execute(
            "SELECT COUNT(DISTINCT nsrc.file_path) "
            "FROM nodes nt "
            "JOIN edges e ON e.target_id = nt.id AND e.type='CALLS' "
            "  AND COALESCE(e.confidence,0) >= ? "
            "  AND LOWER(COALESCE(e.resolution_method,'')) != 'name_match' "
            "JOIN nodes nsrc ON e.source_id = nsrc.id "
            "WHERE nt.file_path = ? AND nt.name = ? AND nsrc.file_path != ?",
            (_EDGE_FLOOR, rel, name, rel),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def build_drift(root: str, db: str, files: list[str]) -> str:
    """THE engine. Compare current vs baseline (<db>.orig) contracts for `files`.

    Returns the advisory drift block (possibly empty = correct-or-quiet). Pure read of two
    graph databases + git; never runs tests, never executes the patch.
    """
    baseline = db + ".orig"
    # No baseline -> we cannot compute drift; stay quiet (never guess).
    if not os.path.isfile(db) or not os.path.isfile(baseline):
        return ""

    gt_index = os.environ.get("GT_INDEX_BINARY", "") or "gt-index"
    rels = [_norm(f, root) for f in files]
    # Refresh the current graph for each edited file so it reflects the edit.
    for rel in rels:
        _reindex(gt_index, root, db, rel)

    try:
        cur = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        orig = sqlite3.connect(f"file:{baseline}?mode=ro", uri=True)
    except sqlite3.Error:
        return ""

    findings: list[str] = []
    try:
        for rel in rels:
            for name in _func_names_in_file(cur, rel):
                changes: list[str] = []

                ret_now = _contract_for(cur, rel, name, _RETURN_KINDS)
                ret_was = _contract_for(orig, rel, name, _RETURN_KINDS)
                if ret_was and ret_now and ret_was != ret_now:
                    changes.append(
                        f"return shape changed: {sorted(ret_was)} -> {sorted(ret_now)}")

                rs_now = _contract_for(cur, rel, name, _RAISE_KINDS)
                rs_was = _contract_for(orig, rel, name, _RAISE_KINDS)
                added, removed = rs_now - rs_was, rs_was - rs_now
                if added or removed:
                    bits = []
                    if removed:
                        bits.append(f"no longer raises {sorted(removed)}")
                    if added:
                        bits.append(f"now raises {sorted(added)}")
                    changes.append("exceptions changed: " + "; ".join(bits))

                g_now = _contract_for(cur, rel, name, _GUARD_KINDS)
                g_was = _contract_for(orig, rel, name, _GUARD_KINDS)
                dropped = g_was - g_now
                if dropped:
                    changes.append(f"dropped guard/precondition: {sorted(dropped)}")

                if not changes:
                    continue  # correct-or-quiet
                callers = _verified_caller_count(cur, rel, name)
                # Surface drift the agent should confirm; lead with caller dependency.
                dep = (f"{callers} caller file(s) depend on it" if callers
                       else "no verified callers (low blast radius)")
                findings.append(
                    f"  - {rel}::{name} — {dep}\n      " + "\n      ".join(changes))
    finally:
        cur.close()
        orig.close()

    if not findings:
        return ""
    return (
        "<gt-drift>\n"
        "CONTRACT DRIFT (advisory — graph-only, no tests run). Confirm each change is "
        "intentional and update dependents if needed:\n"
        + "\n".join(findings)
        + "\n</gt-drift>"
    )


def drift_hook(root: str, db: str, files: list[str]) -> str:
    """Transport-agnostic entry: both the OH push path and the mini-swe `gt drift`
    pull path call this. Thin wrapper over build_drift so the two transports share the
    identical payload."""
    try:
        return build_drift(root, db, files)
    except Exception:  # noqa: BLE001 — advisory layer must never break the agent
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gt drift",
        description="Advisory contract-drift check (graph-only; never runs tests).",
    )
    ap.add_argument("--root", required=True, help="repo root")
    ap.add_argument("--db", required=True, help="current graph.db (baseline = <db>.orig)")
    ap.add_argument("--file", action="append", default=[], help="edited file (repeatable)")
    ap.add_argument("--all-modified", action="store_true",
                    help="use `git diff` to find edited source files")
    args = ap.parse_args(argv)

    files = list(args.file)
    if args.all_modified or not files:
        files = _git_modified(args.root) or files
    if not files:
        return 0  # nothing to check — quiet

    block = drift_hook(args.root, args.db, files)
    if block:
        print(block)
    return 0  # advisory: success regardless, never fail the agent


if __name__ == "__main__":
    raise SystemExit(main())

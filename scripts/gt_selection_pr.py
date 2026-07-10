#!/usr/bin/env python3
"""gt_selection_pr.py — covering-SELECTION precision/recall harness (the ② SOTA bar for B1).

The B1 verification-execution stack (src/groundtruth/runtime/covering_runner.py) first
SELECTS the repo's own test files that reach an edited symbol (``select_covering_tests``,
FACT-tier CALLS edges, confidence>=0.7), then RUNS them. This harness measures the
SELECTION half in isolation: given an edited symbol, how PRECISE and how COMPLETE is the
graph-selected covering-test set versus the test files that actually reference the symbol?

Reference tiers (honest about which was available, per run):
  * grep-reference (ALWAYS available when the repo checkout is present) — the test files
    that textually reference the symbol as a whole word. This is a RECALL-GENEROUS
    reference: a textual mention is a superset of a real call, so selector PRECISION vs
    grep-ref is a LOWER BOUND on true precision, and selector RECALL vs grep-ref is
    conservative (grep counts mention-only files the FACT-tier selector deliberately omits).
  * collect-reference (pytest --collect-only) — only attempted with --collect AND a usable
    repo env; skipped honestly otherwise (never fabricated).

Imports the SHIPPING engine ``select_covering_tests`` (importing is allowed; editing is
not). Deterministic, LLM-free, no network. Writes only under --out (never in the repo).

Usage:
  gt_selection_pr.py --testset [--testset-dir D:/gt_runs/localization_testset] [--out F]
  gt_selection_pr.py --graph G.db --repo R --symbols a,b,c [--gold f.py]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys

sys.path.insert(0, "D:/Groundtruth/src")

from groundtruth.runtime.covering_runner import select_covering_tests  # noqa: E402

# F7: verification_plan is imported LAZILY (inside _pr_two_arms, the only consumer)
# so an ImportError in the new planner module can never kill the frozen --testset
# instrument.

TESTSET_DIR = "D:/gt_runs/localization_testset"

# FILENAME-anchored test-file detector (mirrors measure_brief._TESTPATH): a tests/
# directory component, or a basename test_*/ *_test / *.test / *.spec. Deliberately
# NOT the bare substring "test" (which would catch conftest.py / a product file).
_TESTPATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/"
    r"|(^|/)test_[^/]*$"
    r"|[^/]*_test\.[A-Za-z0-9]+$"
    r"|[^/]*\.(test|spec)\.[A-Za-z0-9]+$")
_SRC_TEST_EXT = (".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rb", ".rs", ".java")
_MIN_SYMBOL_LEN = 3


def _np(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./")


def _discover_test_files(repo_dir: str) -> list[str]:
    """Repo-relative paths of test-shaped source files (independent of the graph)."""
    out: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "venv")]
        for fn in files:
            if not fn.endswith(_SRC_TEST_EXT):
                continue
            rel = _np(os.path.relpath(os.path.join(root, fn), repo_dir))
            if _TESTPATH.search(rel):
                out.append(rel)
    return sorted(set(out))


def _grep_reference(repo_dir: str, symbol: str, test_files: list[str]) -> list[str]:
    """Test files that reference ``symbol`` as a whole word (grep-reference tier)."""
    if len(symbol) < _MIN_SYMBOL_LEN:
        return []
    pat = re.compile(r"(?<![\w])" + re.escape(symbol) + r"(?![\w])")
    hits: list[str] = []
    for rel in test_files:
        p = os.path.join(repo_dir, rel)
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                if pat.search(fh.read()):
                    hits.append(rel)
        except OSError:
            continue
    return sorted(hits)


def _gold_file_symbols(graph_db: str, gold_files: list[str], cap: int = 40) -> list[str]:
    """Non-test Function/Method symbols DEFINED in the gold files (the edit surface).
    Sorted + capped for determinism/boundedness. Names <3 chars dropped (unmatchable)."""
    if not gold_files or not os.path.isfile(graph_db):
        return []
    try:
        con = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    syms: set[str] = set()
    try:
        for g in gold_files:
            gn = _np(g)
            for (name,) in con.execute(
                "SELECT DISTINCT name FROM nodes WHERE file_path=? "
                "AND label IN ('Function','Method') AND COALESCE(is_test,0)=0",
                (gn,),
            ).fetchall():
                if name and len(name) >= _MIN_SYMBOL_LEN:
                    syms.add(name)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return sorted(syms)[:cap]


def _pr_for_symbol(pred: set[str], ref: set[str]) -> dict:
    """Precision/recall for one symbol. precision=None if no prediction; recall=None if
    no reference. inter = |pred ∩ ref|. (Pure — unit-tested.)"""
    inter = len(pred & ref)
    precision = round(inter / len(pred), 8) if pred else None
    recall = round(inter / len(ref), 8) if ref else None
    return {
        "n_pred": len(pred), "n_ref": len(ref), "n_intersection": inter,
        "precision": precision, "recall": recall,
        "pred": sorted(pred), "ref": sorted(ref),
    }


def measure_task(task_id: str, graph_db: str, repo_dir: str, gold_files: list[str],
                 symbols: list[str] | None = None) -> dict:
    """Selection P/R for one task. Skips (with reason) when inputs are missing."""
    rep: dict = {
        "task_id": task_id, "graph_db": _np(graph_db), "repo_dir": _np(repo_dir),
        "gold_files": [_np(g) for g in (gold_files or [])],
        "reference_tier": "grep-reference", "status": "measured",
        "per_symbol": {}, "skipped_reason": None,
    }
    if not graph_db or not os.path.isfile(graph_db):
        rep.update(status="skipped", skipped_reason="graph_db missing")
        return rep
    if not repo_dir or not os.path.isdir(repo_dir):
        rep.update(status="skipped", skipped_reason="repo_dir missing (no grep reference)")
        return rep

    probe = symbols or _gold_file_symbols(graph_db, gold_files)
    if not probe:
        rep.update(status="skipped",
                   skipped_reason="no probeable gold-file symbols in graph")
        return rep
    rep["n_symbols_probed"] = len(probe)

    test_files = _discover_test_files(repo_dir)
    rep["n_repo_test_files"] = len(test_files)

    per: dict[str, dict] = {}
    for s in probe:
        pred = {_np(x["file"]) for x in select_covering_tests(graph_db, {s}, limit=8)}
        ref = set(_grep_reference(repo_dir, s, test_files))
        per[s] = _pr_for_symbol(pred, ref)
    rep["per_symbol"] = per

    # aggregate over symbols that carry a signal (pred or ref non-empty)
    prec_vals = [v["precision"] for v in per.values() if v["precision"] is not None]
    rec_vals = [v["recall"] for v in per.values() if v["recall"] is not None]
    sum_inter = sum(v["n_intersection"] for v in per.values())
    sum_pred = sum(v["n_pred"] for v in per.values())
    sum_ref = sum(v["n_ref"] for v in per.values())
    rep["aggregate"] = {
        "n_symbols_probed": len(probe),
        "n_pred_nonempty": sum(1 for v in per.values() if v["n_pred"]),
        "n_ref_nonempty": sum(1 for v in per.values() if v["n_ref"]),
        "n_both_empty_skipped": sum(1 for v in per.values()
                                    if not v["n_pred"] and not v["n_ref"]),
        "micro_precision": round(sum_inter / sum_pred, 8) if sum_pred else None,
        "micro_recall": round(sum_inter / sum_ref, 8) if sum_ref else None,
        "macro_precision": round(statistics.fmean(prec_vals), 8) if prec_vals else None,
        "macro_recall": round(statistics.fmean(rec_vals), 8) if rec_vals else None,
    }
    return rep


def _aggregate(reports: list[dict]) -> dict:
    measured = [r for r in reports if r.get("status") == "measured"]
    sum_inter = sum_pred = sum_ref = 0
    prec_vals: list[float] = []
    rec_vals: list[float] = []
    for r in measured:
        for v in (r.get("per_symbol") or {}).values():
            sum_inter += v["n_intersection"]
            sum_pred += v["n_pred"]
            sum_ref += v["n_ref"]
            if v["precision"] is not None:
                prec_vals.append(v["precision"])
            if v["recall"] is not None:
                rec_vals.append(v["recall"])
    return {
        "n_tasks": len(reports),
        "n_measured": len(measured),
        "n_skipped": len(reports) - len(measured),
        "skips": sorted({r["skipped_reason"] for r in reports
                         if r.get("skipped_reason")}),
        "reference_tier": "grep-reference",
        "reference_tier_note": (
            "grep-reference is recall-generous (textual mention ⊇ real call): selector "
            "precision here is a LOWER BOUND; selector recall is conservative. "
            "collect-reference (pytest --collect-only) NOT run — no repo env."),
        "n_symbols_probed": sum(r.get("n_symbols_probed", 0) for r in measured),
        "micro_precision": round(sum_inter / sum_pred, 8) if sum_pred else None,
        "micro_recall": round(sum_inter / sum_ref, 8) if sum_ref else None,
        "macro_precision": round(statistics.fmean(prec_vals), 8) if prec_vals else None,
        "macro_recall": round(statistics.fmean(rec_vals), 8) if rec_vals else None,
    }


def _load_task_configs(testset_dir: str) -> list[dict]:
    tasks_dir = os.path.join(testset_dir, "tasks")
    out = []
    for fn in sorted(os.listdir(tasks_dir)):
        if fn.endswith(".json"):
            with open(os.path.join(tasks_dir, fn), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


# ===========================================================================
# --planner MODE (additive): the k=2-expanded selection (verification_plan
# select_targeted_tests: direct FACT covering + k=2 caller closure + test-dir
# convention) scored side-by-side with the DIRECT-covering baseline (R=0.019),
# against BOTH reference tiers — grep-reference (always) and, on the signature
# dataset, the label reference (the companion test files touched in the same
# signature-change commit). Deterministic, LLM-free, offline. Additive: the raw
# `select_covering_tests` remains the baseline; select_targeted_tests is the arm.
# ===========================================================================
_PLANNER_BASES = ("fact_covering", "closure_k2", "test_dir_convention")


def _pr_two_arms(sym: str, repo_dir: str, graph_db: str, test_files: list[str],
                 grep_ref: set[str], label_ref: set[str] | None) -> dict:
    """Baseline (direct covering) vs planner (k=2-expanded) predictions for one
    symbol, scored against the grep tier and (optionally) the label tier — PLUS a
    PER-BASIS decomposition (F1): each selection_basis's own prediction set is
    scored separately so no basis can claim credit another basis earned."""
    from groundtruth.runtime.verification_plan import select_targeted_tests  # F7 lazy

    base_pred = {_np(x["file"]) for x in select_covering_tests(graph_db, {sym}, limit=8)}
    plan_sel = select_targeted_tests(graph_db, repo_dir, [sym], limit=8)
    plan_pred = {_np(x["file"]) for x in plan_sel}
    basis_pred: dict[str, set[str]] = {b: set() for b in _PLANNER_BASES}
    for x in plan_sel:
        basis_pred.setdefault(x["selection_basis"], set()).add(_np(x["file"]))
    out = {
        "n_base_pred": len(base_pred), "n_plan_pred": len(plan_pred),
        "base_vs_grep": _pr_for_symbol(base_pred, grep_ref),
        "plan_vs_grep": _pr_for_symbol(plan_pred, grep_ref),
        "plan_by_basis_vs_grep": {
            b: _pr_for_symbol(basis_pred[b], grep_ref) for b in _PLANNER_BASES
        },
    }
    if label_ref is not None:
        out["base_vs_label"] = _pr_for_symbol(base_pred, label_ref)
        out["plan_vs_label"] = _pr_for_symbol(plan_pred, label_ref)
        out["plan_by_basis_vs_label"] = {
            b: _pr_for_symbol(basis_pred[b], label_ref) for b in _PLANNER_BASES
        }
    return out


def measure_task_planner(task_id: str, graph_db: str, repo_dir: str,
                         gold_files: list[str], symbols: list[str] | None = None) -> dict:
    """Planner-vs-baseline selection P/R for one task (grep reference tier)."""
    rep: dict = {"task_id": task_id, "graph_db": _np(graph_db), "repo_dir": _np(repo_dir),
                 "status": "measured", "per_symbol": {}, "skipped_reason": None}
    if not graph_db or not os.path.isfile(graph_db):
        rep.update(status="skipped", skipped_reason="graph_db missing"); return rep
    if not repo_dir or not os.path.isdir(repo_dir):
        rep.update(status="skipped", skipped_reason="repo_dir missing (no grep reference)"); return rep
    probe = symbols or _gold_file_symbols(graph_db, gold_files)
    if not probe:
        rep.update(status="skipped", skipped_reason="no probeable gold-file symbols in graph"); return rep
    rep["n_symbols_probed"] = len(probe)
    test_files = _discover_test_files(repo_dir)
    rep["n_repo_test_files"] = len(test_files)
    per: dict[str, dict] = {}
    for s in probe:
        grep_ref = set(_grep_reference(repo_dir, s, test_files))
        per[s] = _pr_two_arms(s, repo_dir, graph_db, test_files, grep_ref, None)
    rep["per_symbol"] = per
    return rep


def measure_sig_labels(sig_labels_path: str, sig_graph: str, sig_repo: str,
                       repo_name: str | None = None) -> dict:
    """Planner-vs-baseline P/R over the signature-label dataset, scored against BOTH
    the grep tier AND the label tier (row['test_callers_fixed'] — the companion test
    files actually touched in the signature-change commit). Per-row skip when the
    symbol is absent from the provided graph (correct-or-quiet). A single graph/repo
    is supplied; rows whose repo does not match are skipped."""
    rep: dict = {"dataset": _np(sig_labels_path), "sig_graph": _np(sig_graph),
                 "sig_repo": _np(sig_repo), "status": "measured", "per_row": [],
                 "skipped_reason": None, "graph_sha_caveat":
                 "graph may not be SHA-matched to each mined commit; per-row symbol "
                 "absence is skipped honestly, present-symbol rows measured as an "
                 "approximate (mechanism-demonstrating) surface"}
    if not os.path.isfile(sig_labels_path):
        rep.update(status="skipped", skipped_reason="sig labels file missing"); return rep
    if not os.path.isfile(sig_graph):
        rep.update(status="skipped", skipped_reason="sig graph missing"); return rep
    if not os.path.isdir(sig_repo):
        rep.update(status="skipped", skipped_reason="sig repo missing (no grep reference)"); return rep
    try:
        con = sqlite3.connect(f"file:{sig_graph}?mode=ro", uri=True)
    except sqlite3.Error:
        rep.update(status="skipped", skipped_reason="sig graph unopenable"); return rep
    test_files = _discover_test_files(sig_repo)
    rows_out: list[dict] = []
    with open(sig_labels_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if repo_name and row.get("repo") != repo_name:
                continue
            sym = row.get("symbol")
            if not sym or len(sym) < _MIN_SYMBOL_LEN:
                continue
            try:
                present = con.execute(
                    "SELECT 1 FROM nodes WHERE name=? LIMIT 1", (sym,)).fetchone()
            except sqlite3.Error:
                present = None
            if not present:
                rows_out.append({"symbol": sym, "sha": row.get("sha", ""),
                                 "status": "skipped", "reason": "symbol absent from graph"})
                continue
            grep_ref = set(_grep_reference(sig_repo, sym, test_files))
            label_ref = {_np(t) for t in (row.get("test_callers_fixed") or [])}
            arms = _pr_two_arms(sym, sig_repo, sig_graph, test_files, grep_ref,
                                label_ref if label_ref else None)
            arms.update({"symbol": sym, "sha": row.get("sha", ""), "status": "measured",
                         "has_label_ref": bool(label_ref)})
            rows_out.append(arms)
    con.close()
    rep["per_row"] = rows_out
    rep["n_rows"] = len(rows_out)
    rep["n_measured"] = sum(1 for r in rows_out if r.get("status") == "measured")
    rep["n_skipped_symbol_absent"] = sum(1 for r in rows_out if r.get("status") == "skipped")
    return rep


def _agg_arms(per_symbol_iter, key: str, basis: str | None = None) -> dict | None:
    """Micro/macro P/R (8dp) over a per-symbol arm key ('base_vs_grep' etc.), or a
    nested per-basis arm when ``basis`` is given ('plan_by_basis_vs_grep'/basis).

    F1 DENOMINATOR HONESTY: macro_precision averages ONLY over rows where the arm
    PREDICTED something (n_pred>0) — abstentions are excluded from that mean — so
    the denominators are reported alongside: ``n_rows`` (rows carrying the arm),
    ``n_pred_nonempty`` (the macro_precision denominator), ``n_ref_nonempty`` (the
    macro_recall denominator), and the raw micro sums (inter/pred/ref)."""
    si = sp = sr = 0
    precs: list[float] = []
    recs: list[float] = []
    n = n_pred_nonempty = n_ref_nonempty = 0
    for v in per_symbol_iter:
        arm = v.get(key) if basis is None else (v.get(key) or {}).get(basis)
        if not arm:
            continue
        n += 1
        si += arm["n_intersection"]; sp += arm["n_pred"]; sr += arm["n_ref"]
        if arm["n_pred"]:
            n_pred_nonempty += 1
        if arm["n_ref"]:
            n_ref_nonempty += 1
        if arm["precision"] is not None:
            precs.append(arm["precision"])
        if arm["recall"] is not None:
            recs.append(arm["recall"])
    if n == 0:
        return None
    return {
        "n_rows": n,
        "n_pred_nonempty": n_pred_nonempty,   # macro_precision denominator
        "n_ref_nonempty": n_ref_nonempty,     # macro_recall denominator
        "sum_intersection": si, "sum_pred": sp, "sum_ref": sr,
        "micro_precision": round(si / sp, 8) if sp else None,
        "micro_recall": round(si / sr, 8) if sr else None,
        "macro_precision": round(statistics.fmean(precs), 8) if precs else None,
        "macro_recall": round(statistics.fmean(recs), 8) if recs else None,
    }


_F10_CAVEAT = (
    "F10 CAVEAT: the label tier (test_callers_fixed) is CONVENTION-BIASED BY "
    "CONSTRUCTION — a commit changing X.py predictably touches tests/test_X.py — "
    "so it inflates exactly the test_dir_convention lever. Measured 2026-07-10: "
    "ALL label-tier true positives were selection_basis=test_dir_convention; "
    "closure_k2 earned 0 predictions and 0 hits on that tier. No aggregate here "
    "may be cited as closure credit; judge closure_k2 on the grep tier / future "
    "SHA-matched sets."
)


def _aggregate_planner(reports: list[dict]) -> dict:
    measured = [r for r in reports if r.get("status") == "measured"]
    all_syms = [v for r in measured for v in (r.get("per_symbol") or {}).values()]
    agg = {
        "reference_tier": "grep-reference (recall-generous: textual mention >= real call)",
        "n_tasks": len(reports), "n_measured": len(measured),
        "n_skipped": len(reports) - len(measured),
        "skips": sorted({r["skipped_reason"] for r in reports if r.get("skipped_reason")}),
        "n_symbols": len(all_syms),
        "baseline_direct_covering_vs_grep": _agg_arms(all_syms, "base_vs_grep"),
        "planner_k2_expanded_vs_grep": _agg_arms(all_syms, "plan_vs_grep"),
        # F1: per-basis decomposition — which lever earned the recall.
        "planner_by_basis_vs_grep": {
            b: _agg_arms(all_syms, "plan_by_basis_vs_grep", b) for b in _PLANNER_BASES
        },
        "baseline_recall_reference_pin": 0.019,
        "attribution_note": (
            "per-basis rows are the credit assignment: the union arm may not be "
            "cited for a lever whose own basis row is null/zero"),
    }
    return agg


def _aggregate_sig(rep: dict) -> dict:
    rows = [r for r in rep.get("per_row", []) if r.get("status") == "measured"]
    lab_rows = [r for r in rows if r.get("has_label_ref")]
    return {
        "n_measured": len(rows),
        "n_skipped_symbol_absent": rep.get("n_skipped_symbol_absent", 0),
        "baseline_vs_grep": _agg_arms(rows, "base_vs_grep"),
        "planner_vs_grep": _agg_arms(rows, "plan_vs_grep"),
        "planner_by_basis_vs_grep": {
            b: _agg_arms(rows, "plan_by_basis_vs_grep", b) for b in _PLANNER_BASES
        },
        "baseline_vs_label": _agg_arms(lab_rows, "base_vs_label"),
        "planner_vs_label": _agg_arms(lab_rows, "plan_vs_label"),
        # F1: THE per-basis label-tier table — the honest credit assignment.
        "planner_by_basis_vs_label": {
            b: _agg_arms(lab_rows, "plan_by_basis_vs_label", b) for b in _PLANNER_BASES
        },
        "label_tier_caveat": _F10_CAVEAT,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--testset", action="store_true")
    ap.add_argument("--planner", action="store_true",
                    help="score k=2-expanded planner selection vs the direct-covering baseline")
    ap.add_argument("--testset-dir", default=TESTSET_DIR)
    ap.add_argument("--graph")
    ap.add_argument("--repo")
    ap.add_argument("--gold", action="append", default=[])
    ap.add_argument("--symbols", help="comma-separated symbols (override gold-file derivation)")
    ap.add_argument("--sig-labels", help="signature_labels.jsonl (planner mode, label reference tier)")
    ap.add_argument("--sig-graph", help="graph.db for the signature-label repo")
    ap.add_argument("--sig-repo", help="checkout dir for the signature-label repo")
    ap.add_argument("--sig-repo-name", help="restrict signature rows to this repo field value")
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.planner:
        reports = []
        for cfg in _load_task_configs(args.testset_dir):
            rep = measure_task_planner(cfg["id"], cfg.get("graph_db", ""),
                                       cfg.get("repo_dir", ""), cfg.get("gold_files", []))
            reports.append(rep)
            print(f"  {rep['task_id']:<32} status={rep['status']:<8} "
                  f"probed={rep.get('n_symbols_probed','-')} "
                  f"{('['+rep['skipped_reason']+']') if rep.get('skipped_reason') else ''}")
        agg = _aggregate_planner(reports)
        out = {"aggregate": agg, "tasks": reports}
        if args.sig_labels and args.sig_graph and args.sig_repo:
            sig = measure_sig_labels(args.sig_labels, args.sig_graph, args.sig_repo,
                                     args.sig_repo_name)
            out["signature_dataset"] = {"aggregate": _aggregate_sig(sig), "detail": sig}
        print("\n=== PLANNER vs BASELINE (grep tier) ===")
        print(json.dumps(agg, indent=2, sort_keys=True, default=str))
        if "signature_dataset" in out:
            print("\n=== SIGNATURE DATASET (grep + label tiers) ===")
            print(json.dumps(out["signature_dataset"]["aggregate"], indent=2,
                             sort_keys=True, default=str))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2, sort_keys=True, default=str)
            print(f"\n[WROTE] {args.out}", file=sys.stderr)
        return

    if args.testset:
        reports = []
        for cfg in _load_task_configs(args.testset_dir):
            rep = measure_task(
                cfg["id"],
                cfg.get("graph_db", ""),
                cfg.get("repo_dir", ""),
                cfg.get("gold_files", []),
            )
            reports.append(rep)
            agg = rep.get("aggregate") or {}
            print(f"  {rep['task_id']:<32} status={rep['status']:<8} "
                  f"probed={rep.get('n_symbols_probed','-')} "
                  f"microP={agg.get('micro_precision')} microR={agg.get('micro_recall')} "
                  f"{('['+rep['skipped_reason']+']') if rep.get('skipped_reason') else ''}")
        out_agg = {"aggregate": _aggregate(reports), "tasks": reports}
        print("\n=== AGGREGATE ===")
        print(json.dumps(out_agg["aggregate"], indent=2, sort_keys=True, default=str))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(out_agg, fh, indent=2, sort_keys=True, default=str)
            print(f"\n[WROTE] {args.out}", file=sys.stderr)
        return

    if not (args.graph and args.repo):
        ap.error("provide --testset, or --graph G --repo R [--symbols a,b] [--gold f]")
    syms = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    rep = measure_task(os.path.basename(args.graph), args.graph, args.repo,
                       args.gold, syms)
    print(json.dumps(rep, indent=2, sort_keys=True, default=str))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, sort_keys=True, default=str)


if __name__ == "__main__":
    main()

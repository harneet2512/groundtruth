#!/usr/bin/env python3
"""aggregate_sweep.py — aggregate proof-sweep rows into SWEEP_REPORT.md.

Port of the `summarize` job in .github/workflows/deepswe_proof_sweep.yml for the
generic VM runner (scripts/vm/gt_proof_sweep.sh): per-language substrate audit
table, foundational-gate counts, the exact classified-failure list, missing-row
detection, and the optimization verdict. Stdlib-only, host-generic — no cloud
provider specifics, no credentials.

Exit code mirrors the workflow's "Enforce sweep verdict" step: nonzero when any
task row is failing or missing (never silent).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time


def mean(xs):
    return (sum(xs) / len(xs)) if xs else 0.0


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def load_rows(out_dir):
    rows = []
    for p in sorted(glob.glob(os.path.join(out_dir, "*", "row.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                rows.append(json.load(f))
        except Exception as e:
            print(f"WARN: unreadable {p}: {e}")
    return rows


def load_expected(tasks_path):
    try:
        with open(tasks_path, encoding="utf-8") as f:
            j = json.load(f)
        return j.get("include") or [], j
    except Exception:
        return [], {}


def build_report(rows, expected, meta, run_id=""):
    exp = {e["task"]: e.get("language", "?") for e in expected}
    got = {r.get("instance_id") for r in rows}
    missing = sorted(t for t in exp if t not in got)

    L = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    digest = (rows[0].get("substrate_digest", "") if rows else meta.get("substrate_digest", ""))
    sha = (rows[0].get("gt_git_commit", "") if rows else meta.get("gt_git_commit", ""))
    rid = run_id or (rows[0].get("run_id", "") if rows else meta.get("run_id", ""))
    bench = meta.get("benchmark") or "substrate-proof"
    L.append(f"# {bench} substrate-proof SWEEP — {len(rows)}/{len(exp)} task rows ({ts})")
    L.append("")
    L.append(f"- substrate: `{digest or '<unknown>'}`")
    L.append(f"- gt commit: `{sha or '<unknown>'}`  run: {rid}")
    L.append("- NO LLM / NO agent: gt-run-proof only, identical command per task ($0 LLM)")
    L.append("")

    # ── per-language table ──
    langs = {}
    for r in rows:
        langs.setdefault(r.get("language", "?"), []).append(r)
    L.append("## Per-language substrate audit")
    L.append("")
    L.append("| language | tasks | lsp_warm | LSP verdicts | embedder ok | "
             "proof s mean/med | pull s mean | probe ms med |")
    L.append("|---|---:|---:|---|---:|---|---:|---:|")
    for lang in sorted(langs):
        rs = langs[lang]
        warm = sum(1 for r in rs if r.get("lsp", {}).get("lsp_warm"))
        embok = sum(1 for r in rs if r.get("embedder", {}).get("ok"))
        hist = {}
        for r in rs:
            v = r.get("lsp", {}).get("verdict_hint") or "<none>"
            hist[v] = hist.get(v, 0) + 1
        hist_s = ", ".join(f"{k}:{v}" for k, v in sorted(hist.items(), key=lambda kv: -kv[1]))
        pr = [r["timings_s"]["proof"] for r in rs if r.get("timings_s", {}).get("proof", -1) >= 0]
        pl = [r["timings_s"]["task_pull"] for r in rs if r.get("timings_s", {}).get("task_pull", -1) >= 0]
        probe = [r["lsp"]["probe_latency_ms"] for r in rs if r.get("lsp", {}).get("probe_latency_ms", 0) > 0]
        L.append(f"| {lang} | {len(rs)} | {warm} | {hist_s} | {embok} | "
                 f"{mean(pr):.1f}/{pct(pr, 0.5):.0f} | {mean(pl):.1f} | {pct(probe, 0.5):.0f} |")
    L.append("")

    # ── gate verdicts ──
    g_all = sum(1 for r in rows if r.get("gates", {}).get("all_on"))
    g1 = sum(1 for r in rows if r.get("gates", {}).get("gate1_resolution"))
    g2 = sum(1 for r in rows if r.get("gates", {}).get("gate2_lsp"))
    g3 = sum(1 for r in rows if r.get("gates", {}).get("gate3_embedder"))
    L.append("## Foundational gates")
    L.append("")
    L.append(f"- gate1 resolution ON: {g1}/{len(rows)}  |  gate2 LSP ON: {g2}/{len(rows)}  |  "
             f"gate3 embedder ON: {g3}/{len(rows)}  |  ALL ON: {g_all}/{len(rows)}")
    L.append("")

    # ── failures (exact list, classified) ──
    fails = [r for r in rows
             if r.get("failure_class") or r.get("proof_exit_code", -1) != 0]
    L.append(f"## Failing tasks ({len(fails)} classified + {len(missing)} missing rows)")
    L.append("")
    if fails:
        L.append("| instance_id | language | class | proof exit | LSP verdict | embedder verdict |")
        L.append("|---|---|---|---:|---|---|")
        for r in sorted(fails, key=lambda r: (r.get("failure_class", ""), r.get("instance_id", ""))):
            L.append(f"| {r.get('instance_id')} | {r.get('language')} | "
                     f"{r.get('failure_class') or '<none>'} | {r.get('proof_exit_code')} | "
                     f"{r.get('lsp', {}).get('verdict_hint') or '<none>'} | "
                     f"{r.get('embedder', {}).get('verdict')} |")
    else:
        L.append("(none)")
    if missing:
        L.append("")
        L.append("Rows MISSING (task never produced a row — class JOB_DIED_BEFORE_ROW):")
        for t in missing:
            L.append(f"- {t} ({exp[t]})")
    L.append("")

    # ── optimization verdict ──
    dl = [r for r in rows if r.get("embedder", {}).get("model_download_attempted")]
    slow = sorted((r for r in rows if r.get("timings_s", {}).get("task_pull", -1) > 120),
                  key=lambda r: -r["timings_s"]["task_pull"])
    ghcr_hits = sum(1 for r in rows if str(r.get("pull_source", "")).startswith("ghcr.io/"))
    pr_all = [r["timings_s"]["proof"] for r in rows if r.get("timings_s", {}).get("proof", -1) >= 0]
    sp_all = [r["timings_s"]["substrate_pull"] for r in rows
              if r.get("timings_s", {}).get("substrate_pull", -1) >= 0]
    L.append("## Optimization verdict (setup efficiency)")
    L.append("")
    L.append(f"- per-task model downloads: "
             f"{'NONE (baked model — OPTIMIZED)' if not dl else 'VIOLATION: ' + ', '.join(r['instance_id'] for r in dl)}")
    L.append(f"- GHCR cache hit rate: {ghcr_hits}/{len(rows)}")
    L.append(f"- task pulls >120s: {len(slow)}"
             + ("" if not slow else " — worst: "
                + ", ".join(f"{r['instance_id']}={r['timings_s']['task_pull']}s" for r in slow[:5])))
    if pr_all:
        L.append(f"- proof wall-time s: min={min(pr_all)} p50={pct(pr_all, 0.5):.0f} "
                 f"p90={pct(pr_all, 0.9):.0f} max={max(pr_all)} mean={mean(pr_all):.1f}")
    if sp_all:
        L.append(f"- substrate pull s: mean={mean(sp_all):.1f} max={max(sp_all)} "
                 f"(single host: pulled once, cached for every task)")
    optimized = (not dl) and (not slow) and not missing and not fails
    L.append(f"- **VERDICT: {'OPTIMIZED + CLEAN' if optimized else 'ATTENTION NEEDED (see flags above)'}**")
    L.append("")

    return "\n".join(L) + "\n", fails, missing


def main():
    ap = argparse.ArgumentParser(description="Aggregate proof-sweep rows -> SWEEP_REPORT.md")
    ap.add_argument("--out-dir", required=True,
                    help="sweep OUT_DIR (rows at <out-dir>/<instance_id>/row.json)")
    ap.add_argument("--tasks", default="",
                    help="sweep_tasks.json with the expected task list "
                         "(default: <out-dir>/sweep_tasks.json)")
    ap.add_argument("--report", default="",
                    help="report path (default: <out-dir>/SWEEP_REPORT.md)")
    ap.add_argument("--run-id", default="", help="sweep run id for the header")
    a = ap.parse_args()

    tasks_path = a.tasks or os.path.join(a.out_dir, "sweep_tasks.json")
    report_path = a.report or os.path.join(a.out_dir, "SWEEP_REPORT.md")

    rows = load_rows(a.out_dir)
    expected, meta = load_expected(tasks_path)
    report, fails, missing = build_report(rows, expected, meta, run_id=a.run_id)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)

    # Enforce sweep verdict (nonzero on any failing/missing task — never silent).
    if fails or missing or not rows:
        print(f"::error::sweep has failing or missing tasks: "
              f"fails={len(fails)} missing={len(missing)} rows={len(rows)}", file=sys.stderr)
        return 1
    print("sweep CLEAN: all task rows present, proof exit 0 everywhere")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""report.py — render the audit report + summary CSV from classified tasks.

Consumes the per-task classifications (classify.py) + their contracts and emits
artifacts/10_task_audit_report.md, artifacts/10_task_audit_summary.csv, and the
300-task reliability_report.{csv,md}. Read-only.
"""
from __future__ import annotations

import csv
import io
import os
from collections import Counter

_SURFACE_OK = {
    "gha": lambda t: t["surfaces"].get("gha_ok"),
    "container": lambda t: t["surfaces"].get("container_ok"),
    "graph": lambda t: t["surfaces"].get("graph_structural_ok"),
    "embedder": lambda t: t["surfaces"].get("embedder_ok"),
    "absorption": lambda t: t["surfaces"].get("absorption_ok"),
}

_FIX_BUCKET = {
    "GHA_PIPELINE_FAIL": ("a", "orchestration"),
    "CONTAINER_RUNTIME_FAIL": ("b", "container runtime"),
    "GRAPH_BASE_FAIL": ("c", "graph-base"),
    "LSP_FAIL": ("d", "LSP"),
    "EMBEDDER_FAIL": ("e", "embedder"),
    "ABSORPTION_FAIL": ("f", "absorption/join"),
    "GATE_FALSE_FAIL": ("g", "gate-invariant"),
    "PRODUCT_QUALITY_FAIL": ("h", "product-quality"),
}


def _ok(t, surface):  # tri-state cell
    v = _SURFACE_OK.get(surface, lambda _t: None)(t)
    return "ok" if v else ("FAIL" if v is False else "-")


def write_summary_csv(tasks: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["task_id", "GHA", "container", "graph", "LSP", "embedder",
                "absorption", "hook", "final_class", "evidence_path"])
    for tid, t in sorted(tasks.items()):
        lsp = "no_op_valid" if t["surfaces"].get("lsp_no_op_valid") else (
            "FAIL" if t["surfaces"].get("lsp_real_fail") else
            ("work" if t["surfaces"].get("lsp_did_work") else "-"))
        w.writerow([tid, _ok(t, "gha"), _ok(t, "container"), _ok(t, "graph"), lsp,
                    _ok(t, "embedder"), _ok(t, "absorption"),
                    t.get("hook_status", "N/A"), t["final_class"],
                    t.get("task_dir", "")])
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


def write_report_md(tasks: dict, path: str, title: str = "10-task audit") -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    classes = Counter(t["final_class"] for t in tasks.values())
    reds = {tid: t for tid, t in tasks.items()
            if t["final_class"] not in ("GREEN_ROBUST", "GREEN_THIN", "VALID_INFRA_READY")}

    # which surfaces are proven reliable (ok on ALL tasks) vs implicated
    surface_fail = {s: [tid for tid, t in tasks.items() if _SURFACE_OK[s](t) is False]
                    for s in _SURFACE_OK}

    L = []
    L.append(f"# {title} — surface-attributed failure report\n")
    L.append("## 1. Executive summary\n")
    L.append(f"- Tasks: {len(tasks)} · classes: " +
             ", ".join(f"{k}={v}" for k, v in classes.most_common()) + "\n")
    for label, surf in [("GHA orchestration", "gha"), ("containerization", "container"),
                        ("graph base", "graph"), ("embedder", "embedder"),
                        ("absorption/render", "absorption")]:
        fails = surface_fail.get(surf, [])
        L.append(f"- Is the issue **{label}**? "
                 + ("NO — reliable on all tasks." if not fails
                    else f"YES on {fails}.") + "\n")
    n_lspfalse = sum(1 for t in tasks.values() if t["final_class"] == "GATE_FALSE_FAIL")
    n_prod = sum(1 for t in tasks.values() if t["final_class"] == "PRODUCT_QUALITY_FAIL")
    n_abs = sum(1 for t in tasks.values() if t["final_class"] == "ABSORPTION_FAIL")
    L.append(f"- Is the issue **gate logic**? {n_lspfalse} GATE_FALSE_FAIL.\n")
    L.append(f"- Is the issue **absorption/rendering**? {n_abs} ABSORPTION_FAIL.\n")
    L.append(f"- Is the issue **true product quality**? {n_prod} PRODUCT_QUALITY_FAIL.\n")

    L.append("\n## 2. Per-task classification\n")
    L.append("| task | GHA | container | graph | LSP | embedder | absorption | final_class | reason |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for tid, t in sorted(tasks.items()):
        lsp = "no_op_valid" if t["surfaces"].get("lsp_no_op_valid") else (
            "FAIL" if t["surfaces"].get("lsp_real_fail") else "work")
        L.append(f"| {tid} | {_ok(t,'gha')} | {_ok(t,'container')} | {_ok(t,'graph')} | {lsp} "
                 f"| {_ok(t,'embedder')} | {_ok(t,'absorption')} | **{t['final_class']}** "
                 f"| {str(t.get('reason',''))[:140]} |")

    L.append("\n## 3. Cross-task pattern\n")
    by_class: dict[str, list] = {}
    for tid, t in tasks.items():
        by_class.setdefault(t["final_class"], []).append(tid)
    for fc, tids in sorted(by_class.items()):
        if len(tids) > 1 and fc not in ("GREEN_ROBUST", "GREEN_THIN"):
            L.append(f"- **Shared seam** `{fc}`: {sorted(tids)}\n")
        elif fc not in ("GREEN_ROBUST", "GREEN_THIN", "VALID_INFRA_READY"):
            L.append(f"- **Isolated** `{fc}`: {sorted(tids)}\n")
    reliable = [s for s, f in surface_fail.items() if not f]
    L.append(f"- **Surfaces proven reliable (ok on all):** {reliable}\n")

    L.append("\n## 4. Required fixes (by bucket; only where a contract proves it)\n")
    buckets: dict[tuple, list] = {}
    for tid, t in reds.items():
        b = _FIX_BUCKET.get(t["final_class"])
        if b:
            buckets.setdefault(b, []).append(tid)
    for (letter, name), tids in sorted(buckets.items()):
        L.append(f"- **({letter}) {name}** — {sorted(tids)}\n")
    if not buckets:
        L.append("- (none — all tasks green or infra-ready)\n")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def write_all(tasks: dict, out_dir: str, title: str = "10-task audit") -> None:
    write_summary_csv(tasks, os.path.join(out_dir, "10_task_audit_summary.csv"))
    write_report_md(tasks, os.path.join(out_dir, "10_task_audit_report.md"), title)

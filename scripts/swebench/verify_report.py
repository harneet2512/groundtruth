#!/usr/bin/env python3
"""Verify a GT run + append the report to verify_results.md.

Strict conjunctive verdict: PASS iff every gate is satisfied, FAIL if any gate
fails. No PASS/WARN middle ground. Every characteristic shows its real observed
value in the tables — no "all clean" shorthand.

Usage:
    python3 scripts/swebench/verify_report.py append --run-dir <path>

Override thresholds via env:
    VERIFY_MIN_DELIVERY, VERIFY_MIN_ENGAGEMENT, VERIFY_MIN_MUST_OK,
    VERIFY_MIN_PATCH  (defaults: 0.65, 0.80, 0.90, 0.50 — calibrated to p10 of
    observed distribution across n=12 runs)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _num(v, default=0.0) -> float:
    if v is None:
        return default
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Canonical mechanism-rate mapping: name -> (numerator_key, denominator_key).
# Single source of truth for both the writer (gt_canary_report.arm_summary)
# and the reader (this file). Kept in sync with
# benchmarks/swebench/vm_bundle/gt_metrics.py.
_RATE_CONTRACT = {
    "delivery_rate": ("steer_delivered_total", "ack_armed_total"),
    "engagement_rate": ("ack_engagement_total", "steer_delivered_total"),
}


def _rate(summary, rate_key):
    """Read a mechanism rate with a raw-totals fallback.

    Precedence:
      1. Pre-computed key on summary (writer emits it).
      2. Derived from (numerator / denominator) using _RATE_CONTRACT.
      3. Returns None if the schema is invalid (missing denominator or
         denominator == 0). The gate layer surfaces None as schema_invalid
         rather than silently coercing to 0.0 — a rate that cannot be
         computed must NOT be treated as "zero steering".
    """
    raw = summary.get(rate_key)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, bool):  # be explicit: a True literal is not a rate
        pass
    num_key, den_key = _RATE_CONTRACT[rate_key]
    num = summary.get(num_key)
    den = summary.get(den_key)
    try:
        num_f = float(num) if isinstance(num, (int, float)) else None
        den_f = float(den) if isinstance(den, (int, float)) else None
    except (TypeError, ValueError):
        return None
    if num_f is None or den_f is None or den_f == 0:
        return None
    return num_f / den_f


BOOTSTRAP_FAILURE_THRESHOLD = 0.30


def check_bootstrap_rate(run_dir: Path) -> dict:
    """Check what fraction of tasks are bootstrap failures (0 edits, cycle <= 2).

    An arm with bootstrap_failure_rate >= BOOTSTRAP_FAILURE_THRESHOLD is invalid
    for baseline comparison — agents crashed before doing any work.

    Returns dict with bootstrap_failure_count, bootstrap_failure_rate, arm_valid.
    """
    rows = _load_rows(run_dir)
    total = len(rows)
    if total == 0:
        return {"bootstrap_failure_count": 0, "bootstrap_failure_rate": 0.0,
                "arm_valid": False, "reason": "no_rows"}

    failures = 0
    for row in rows:
        edits = int(row.get("material_edit_count", 0) or 0)
        cycle = int(row.get("cycle", 999) or 999)
        if edits == 0 and cycle <= 2:
            failures += 1

    rate = failures / total
    return {
        "bootstrap_failure_count": failures,
        "bootstrap_failure_rate": round(rate, 2),
        "total_tasks": total,
        "arm_valid": rate < BOOTSTRAP_FAILURE_THRESHOLD,
    }


def _load(run_dir: Path, name: str) -> dict | list:
    p = run_dir / name
    if not p.exists():
        return {} if name.endswith(".json") else []
    try:
        if name.endswith(".jsonl"):
            out = []
            for line in p.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
            return out
        return json.loads(p.read_text())
    except Exception:
        return {} if name.endswith(".json") else []


def _load_rows(run_dir: Path) -> list[dict]:
    p = run_dir / "gt_report.csv"
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _sum_col(rows, key):
    return int(sum(_num(r.get(key, 0)) for r in rows))


def _coverage(rows, key):
    if not rows:
        return 0.0
    return sum(1 for r in rows if _num(r.get(key, 0)) > 0) / len(rows)


# ---- thresholds (calibrated to p10 of observed distribution, n=12 runs) ----

HARD_ZERO_GATES = [
    ("killed_task_count", "killed"),
    ("run_invalid_count", "run_invalid"),
    ("infra_contaminated_total", "infra_contaminated"),
    ("identity_missing_total", "identity_missing"),
    ("startup_failed", "startup_failed"),
    ("budget_denied_total", "budget_denied"),
]

MECHANISM_GATES = [
    ("material_edit_total", "material_edit_total"),
    ("ack_armed_total", "ack_armed_total"),
    ("steer_delivered_total", "steer_delivered_total"),
    ("ack_engagement_total", "ack_engagement_total"),
]


def compute(run_dir: Path) -> dict:
    summary = _load(run_dir, "gt_arm_summary.json") or {}
    classification = _load(run_dir, "run_classification.json") or {}
    killed = _load(run_dir, "killed_tasks.jsonl") or []
    rows = _load_rows(run_dir)

    arm = (summary.get("arm") or "").lower()
    is_lsp = "lsp" in arm and "nolsp" not in arm

    task_count = int(_num(summary.get("task_count"), len(rows)))

    # material_edit_total: prefer explicit total, else avg * task_count
    material_total = _num(summary.get("material_edit_total"))
    if material_total == 0 and summary.get("avg_material_edit") is not None:
        material_total = _num(summary.get("avg_material_edit")) * max(1, task_count)

    raw = {
        "task_count": task_count,
        "killed": len(killed),
        "run_invalid_count": _num(summary.get("run_invalid_count")),
        "infra_contaminated_total": _num(summary.get("infra_contaminated_total",
                                                     summary.get("infra_contaminated_count", 0))),
        "identity_missing_total": _num(summary.get("identity_missing_total",
                                                   summary.get("identity_missing", 0))),
        "startup_failed": _sum_col(rows, "startup_failed"),
        "budget_denied_total": _num(summary.get("budget_denied_total")),
        "material_edit_total": material_total,
        "ack_armed_total": _num(summary.get("ack_armed_total")),
        "steer_delivered_total": _num(summary.get("steer_delivered_total")),
        "ack_engagement_total": _num(summary.get("ack_engagement_total")),
        "ack_followed_total": _num(summary.get("ack_followed_total")),
        "typed_ack_followed_total": _num(summary.get("typed_ack_followed_total")),
        "lsp_promotion_total": _num(summary.get("lsp_promotion_total",
                                                summary.get("lsp_promotion_count", 0))),
        # Use _rate() for contract-safe lookup: prefer pre-computed, fall
        # back to raw totals, surface None on schema_invalid instead of 0.0.
        "delivery_rate": _rate(summary, "delivery_rate"),
        "engagement_rate": _rate(summary, "engagement_rate"),
        "delivery_rate_status": "schema_invalid" if _rate(summary, "delivery_rate") is None else "ok",
        "engagement_rate_status": "schema_invalid" if _rate(summary, "engagement_rate") is None else "ok",
        "ack_followed_rate": _num(summary.get("ack_followed_rate")),
        "must_ok_rate": _num(summary.get("must_ok_rate")),
        "has_patch_rate": _num(summary.get("has_patch_rate")),
        "gt_impact_coverage": _coverage(rows, "gt_impact_count"),
        "stuck_loop_fired": _num(summary.get("stuck_loop_fired_total",
                                             summary.get("stuck_loop_fired_count", 0))),
        "submit_bypassed": _num(summary.get("submit_bypassed_total",
                                            summary.get("submit_bypassed_count", 0))),
    }

    thresholds = {
        "delivery_rate": _env_float("VERIFY_MIN_DELIVERY", 0.65),
        "engagement_rate": _env_float("VERIFY_MIN_ENGAGEMENT", 0.80),
        "must_ok_rate": _env_float("VERIFY_MIN_MUST_OK", 0.90),
        "has_patch_rate": _env_float("VERIFY_MIN_PATCH", 0.50),
    }

    # --- gate checks (strict conjunctive) ---
    gates = []
    # hard-zero
    for key, label in HARD_ZERO_GATES:
        val = raw.get(label if label in raw else key, 0)
        gates.append({
            "characteristic": label,
            "threshold": "== 0",
            "value": int(val) if isinstance(val, float) and val.is_integer() else val,
            "pass": val == 0,
        })
    # mechanism fire
    for key, label in MECHANISM_GATES:
        val = raw[label]
        gates.append({
            "characteristic": label,
            "threshold": "> 0",
            "value": int(val) if isinstance(val, float) and val.is_integer() else round(val, 1),
            "pass": val > 0,
        })
    # lsp-only mechanism
    if is_lsp:
        gates.append({
            "characteristic": "lsp_promotion_total",
            "threshold": "> 0 (LSP arm)",
            "value": int(raw["lsp_promotion_total"]),
            "pass": raw["lsp_promotion_total"] > 0,
        })
    # rate gates
    rate_specs = [
        ("delivery_rate", thresholds["delivery_rate"]),
        ("engagement_rate", thresholds["engagement_rate"]),
        ("must_ok_rate", thresholds["must_ok_rate"]),
        ("has_patch_rate", thresholds["has_patch_rate"]),
    ]
    for name, thresh in rate_specs:
        val = raw[name]
        # schema_invalid (None) must FAIL the gate with a distinct label, not
        # silently coerce to 0.0 which hides the underlying schema bug.
        if val is None:
            gates.append({
                "characteristic": name,
                "threshold": f">= {thresh:.2f}",
                "value": "schema_invalid",
                "pass": False,
            })
        else:
            gates.append({
                "characteristic": name,
                "threshold": f">= {thresh:.2f}",
                "value": round(val, 2),
                "pass": val >= thresh,
            })

    verdict = "PASS" if all(g["pass"] for g in gates) else "FAIL"

    return {
        "run_dir": str(run_dir),
        "run_id": summary.get("run_id", run_dir.name),
        "arm": summary.get("arm", "(unknown)"),
        "classification": classification.get("classification", "(unclassified)"),
        "verdict": verdict,
        "raw": raw,
        "gates": gates,
        "killed_entries": killed,
    }


def render_section(result: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    r = result["raw"]
    icon = {"PASS": "[PASS]", "FAIL": "[FAIL]"}.get(result["verdict"], "[?]")

    lines = [f"### {icon} `{result['run_id']}`", ""]
    lines.append(f"- **When:** {now}")
    lines.append(f"- **Arm:** `{result['arm']}` | **Classification:** `{result['classification']}` | **Verdict:** **{result['verdict']}**")
    lines.append(f"- **Archive:** `{result['run_dir']}`")
    lines.append("")

    # Counters table — every characteristic with real value
    lines.append("**Raw counters (real values per characteristic)**")
    lines.append("")
    lines.append("| characteristic | value |")
    lines.append("|---|---:|")
    for k in ["task_count", "killed", "run_invalid_count", "infra_contaminated_total",
              "identity_missing_total", "startup_failed", "budget_denied_total",
              "material_edit_total", "ack_armed_total", "steer_delivered_total",
              "ack_engagement_total", "ack_followed_total", "typed_ack_followed_total",
              "lsp_promotion_total", "stuck_loop_fired", "submit_bypassed"]:
        v = r[k]
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        elif isinstance(v, float):
            v = round(v, 1)
        lines.append(f"| {k} | {v} |")
    lines.append("")

    # Rates table
    lines.append("**Rates (real values per characteristic)**")
    lines.append("")
    lines.append("| characteristic | value |")
    lines.append("|---|---:|")
    for k in ["delivery_rate", "engagement_rate", "ack_followed_rate",
              "must_ok_rate", "has_patch_rate", "gt_impact_coverage"]:
        v = r[k]
        if v is None:
            lines.append(f"| {k} | schema_invalid |")
        else:
            lines.append(f"| {k} | {v:.2f} |")
    lines.append("")

    # Gate table — real value vs threshold vs PASS/FAIL
    lines.append("**Gates (strict conjunctive)**")
    lines.append("")
    lines.append("| characteristic | value | threshold | result |")
    lines.append("|---|---:|---|:---:|")
    for g in result["gates"]:
        res = "PASS" if g["pass"] else "**FAIL**"
        lines.append(f"| {g['characteristic']} | {g['value']} | {g['threshold']} | {res} |")
    lines.append("")

    # Failure summary
    failed_gates = [g for g in result["gates"] if not g["pass"]]
    if failed_gates:
        lines.append(f"**Failed gates ({len(failed_gates)}):**")
        for g in failed_gates:
            lines.append(f"- `{g['characteristic']}` = {g['value']}, threshold {g['threshold']}")
        lines.append("")

    if result["killed_entries"]:
        lines.append(f"**Killed tasks ({len(result['killed_entries'])}):**")
        for e in result["killed_entries"]:
            tid = e.get("instance_id", "?")
            reason = e.get("reason", "?")
            at = e.get("killed_at", "?")
            lines.append(f"- `{tid}` @ {at} — {reason}")
        lines.append("")

    # Report-only (no gate) — show values for context
    lines.append("**Report-only (not gated — population median is 0, gating blocks everything):**")
    lines.append(f"- ack_followed_rate = {r['ack_followed_rate']:.2f}")
    lines.append(f"- typed_ack_followed_total = {int(r['typed_ack_followed_total'])}")
    lines.append(f"- gt_impact_coverage = {r['gt_impact_coverage']*100:.0f}%")
    lines.append("")

    lines.append("---")
    return "\n".join(lines)


def append_to_log(doc_path: Path, section: str) -> None:
    marker = "<!-- APPEND_MARKER -->"
    if not doc_path.exists():
        raise FileNotFoundError(f"{doc_path} not found")
    text = doc_path.read_text(encoding="utf-8")
    if marker in text:
        text = text.replace(marker, section + "\n\n" + marker, 1)
    else:
        text = text.rstrip() + "\n\n" + section + "\n"
    doc_path.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a GT run + append to verify_results.md")
    sub = ap.add_subparsers(dest="command", required=True)

    p_app = sub.add_parser("append", help="Compute verdict, append to doc, print to stdout")
    p_app.add_argument("--run-dir", required=True)
    p_app.add_argument("--doc", default=None)
    p_app.add_argument("--no-append", action="store_true")
    p_app.set_defaults(func=_cmd_append)

    args = ap.parse_args()
    return int(args.func(args))


def _cmd_append(args) -> int:
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "gt_arm_summary.json").exists():
        print(f"ERROR: {run_dir}/gt_arm_summary.json missing", file=sys.stderr)
        return 2

    result = compute(run_dir)
    section = render_section(result)
    print(section)

    if not args.no_append:
        doc = Path(args.doc) if args.doc else Path(__file__).resolve().parents[2] / "verify_results.md"
        try:
            append_to_log(doc, section)
            print(f"\n(appended to {doc})", file=sys.stderr)
        except FileNotFoundError as exc:
            print(f"\nWARNING: {exc}", file=sys.stderr)

    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

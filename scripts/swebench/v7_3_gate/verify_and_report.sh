#!/usr/bin/env bash
# v7.3 gate -- verify_and_report.sh
# Runs ON VM1. Consumes verify_report.py for both arms, then computes paired
# Wilcoxon + localization + brief_chars / sanitizer counts.
# Renders ~/runs/v7_3_gate_<ts>_VERDICT.md and prints to stdout.

set -euo pipefail

PIDS_FILE="/tmp/v7_3_gate.pids"
if [ ! -f "$PIDS_FILE" ]; then
  echo "FAIL: $PIDS_FILE missing. Run run_gate.sh + monitor.sh first."
  exit 1
fi
# shellcheck disable=SC1090
source "$PIDS_FILE"

VERIFY="/opt/groundtruth/scripts/swebench/verify_report.py"
if [ ! -f "$VERIFY" ]; then
  # Try a fallback common path. If still missing, bail.
  ALT="$HOME/groundtruth/scripts/swebench/verify_report.py"
  if [ -f "$ALT" ]; then
    VERIFY="$ALT"
  else
    echo "FAIL: verify_report.py not found at $VERIFY or $ALT"
    echo "      Locate it on this VM and re-run with VERIFY=<path> in env."
    exit 1
  fi
fi

VERDICT="$HOME/runs/v7_3_gate_${TS}_VERDICT.md"

echo "============================================================"
echo "  v7.3 gate verify_and_report"
echo "  v7.3: $ARM_V73_DIR"
echo "  v7.2: $ARM_V72_DIR"
echo "  Out:  $VERDICT"
echo "============================================================"

# --- Step 1: verify_report.py per arm ---
echo ""
echo "=== verify_report.py: v7.3 arm ==="
V73_VERIFY_LOG=$(mktemp)
set +e
python3 "$VERIFY" append --run-dir "$ARM_V73_DIR" 2>&1 | tee "$V73_VERIFY_LOG"
V73_VERIFY_RC=${PIPESTATUS[0]}
set -e

echo ""
echo "=== verify_report.py: v7.2 arm ==="
V72_VERIFY_LOG=$(mktemp)
set +e
python3 "$VERIFY" append --run-dir "$ARM_V72_DIR" 2>&1 | tee "$V72_VERIFY_LOG"
V72_VERIFY_RC=${PIPESTATUS[0]}
set -e

# Verdict per arm: scrape "PASS" or "FAIL" from the verify_report stdout.
V73_VERDICT="UNKNOWN"
V72_VERDICT="UNKNOWN"
if grep -qE '\bPASS\b' "$V73_VERIFY_LOG"; then V73_VERDICT="PASS"; fi
if grep -qE '\bFAIL\b' "$V73_VERIFY_LOG"; then V73_VERDICT="FAIL"; fi
if grep -qE '\bPASS\b' "$V72_VERIFY_LOG"; then V72_VERDICT="PASS"; fi
if grep -qE '\bFAIL\b' "$V72_VERIFY_LOG"; then V72_VERDICT="FAIL"; fi

# --- Step 2: paired Wilcoxon + localization + brief_chars (Python inline) ---
echo ""
echo "=== Paired Wilcoxon + localization + brief_chars ==="

python3 - "$ARM_V73_DIR" "$ARM_V72_DIR" "$VERDICT" "$V73_VERDICT" "$V72_VERDICT" <<'PYEOF'
import csv
import json
import os
import sys
from pathlib import Path
from statistics import mean, median, quantiles

try:
    from scipy.stats import wilcoxon
    HAS_SCIPY = True
except Exception:
    HAS_SCIPY = False

arm_v73_dir = Path(sys.argv[1])
arm_v72_dir = Path(sys.argv[2])
verdict_path = Path(sys.argv[3])
v73_verdict = sys.argv[4]
v72_verdict = sys.argv[5]


def read_gt_report(arm_dir: Path) -> dict:
    """Return {instance_id: row_dict}. Handles missing file gracefully."""
    f = arm_dir / "gt_report.csv"
    if not f.exists():
        return {}
    out = {}
    with f.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            iid = row.get("instance_id") or row.get("task_id") or row.get("id")
            if iid:
                out[iid] = row
    return out


def read_telemetry(arm_dir: Path) -> list:
    f = arm_dir / "gt_runtime_telemetry.jsonl"
    if not f.exists():
        return []
    out = []
    with f.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def read_pass_at_1(arm_dir: Path) -> dict:
    """Best-effort pass@1 per task. Tries gt_report.csv 'resolved' column,
    then output.jsonl 'resolved' / 'is_resolved' field."""
    out = {}
    # Try gt_report.csv first
    rows = read_gt_report(arm_dir)
    for iid, row in rows.items():
        for key in ("resolved", "is_resolved", "pass", "passed"):
            if key in row and row[key] != "":
                v = row[key].strip().lower()
                out[iid] = 1 if v in ("1", "true", "yes", "t", "y") else 0
                break
    if out:
        return out
    # Fallback: output.jsonl
    f = arm_dir / "output.jsonl"
    if f.exists():
        with f.open() as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                iid = rec.get("instance_id") or rec.get("task_id")
                if not iid:
                    continue
                for key in ("resolved", "is_resolved", "pass", "passed"):
                    if key in rec:
                        out[iid] = 1 if bool(rec[key]) else 0
                        break
    return out


def read_localization(arm_dir: Path) -> dict:
    """Return {iid: {first_gold, precision, coverage}} from gt_report.csv if cols exist."""
    rows = read_gt_report(arm_dir)
    out = {}
    for iid, row in rows.items():
        rec = {}
        for col, alias in (
            ("first_gold_step", "first_gold"),
            ("first_gold", "first_gold"),
            ("localization_precision", "precision"),
            ("precision", "precision"),
            ("localization_coverage", "coverage"),
            ("coverage", "coverage"),
        ):
            if col in row and row[col] not in ("", None):
                try:
                    rec[alias] = float(row[col])
                except Exception:
                    pass
        if rec:
            out[iid] = rec
    return out


def brief_metrics(arm_dir: Path) -> dict:
    """brief_chars distribution + sanitizer-affected line count from telemetry."""
    tele = read_telemetry(arm_dir)
    chars = []
    sanitized_lines = 0
    sanitized_events = 0
    for rec in tele:
        # Common shapes -- we accept any.
        bc = rec.get("brief_chars") or rec.get("brief_len") or rec.get("brief_size")
        if isinstance(bc, (int, float)):
            chars.append(int(bc))
        sa = (
            rec.get("sanitizer_affected_lines")
            or rec.get("sanitized_line_count")
            or rec.get("sanitized_lines")
        )
        if isinstance(sa, (int, float)) and sa > 0:
            sanitized_lines += int(sa)
            sanitized_events += 1
    out = {
        "n": len(chars),
        "min": min(chars) if chars else None,
        "median": int(median(chars)) if chars else None,
        "max": max(chars) if chars else None,
        "mean": int(mean(chars)) if chars else None,
        "sanitizer_affected_lines_total": sanitized_lines,
        "sanitizer_events": sanitized_events,
    }
    return out


def confidence_distribution(arm_dir: Path) -> dict:
    """Bucket per-task confidence from telemetry into [<0.5, 0.5-0.6, 0.6-0.7, >=0.7]."""
    tele = read_telemetry(arm_dir)
    buckets = {"lt_0_5": 0, "0_5_to_0_6": 0, "0_6_to_0_7": 0, "ge_0_7": 0, "unknown": 0}
    for rec in tele:
        c = rec.get("confidence") or rec.get("brief_confidence") or rec.get("top_confidence")
        if not isinstance(c, (int, float)):
            buckets["unknown"] += 1
            continue
        if c < 0.5:
            buckets["lt_0_5"] += 1
        elif c < 0.6:
            buckets["0_5_to_0_6"] += 1
        elif c < 0.7:
            buckets["0_6_to_0_7"] += 1
        else:
            buckets["ge_0_7"] += 1
    return buckets


# --- Compute everything ---
v73_pass = read_pass_at_1(arm_v73_dir)
v72_pass = read_pass_at_1(arm_v72_dir)
v73_loc = read_localization(arm_v73_dir)
v72_loc = read_localization(arm_v72_dir)
v73_brief = brief_metrics(arm_v73_dir)
v72_brief = brief_metrics(arm_v72_dir)
v73_conf = confidence_distribution(arm_v73_dir)
v72_conf = confidence_distribution(arm_v72_dir)

# Paired set: tasks present in BOTH arms.
shared_iids = sorted(set(v73_pass.keys()) & set(v72_pass.keys()))
v73_vec = [v73_pass[i] for i in shared_iids]
v72_vec = [v72_pass[i] for i in shared_iids]
deltas = [a - b for a, b in zip(v73_vec, v72_vec)]
n_pairs = len(shared_iids)
n_nonzero_delta = sum(1 for d in deltas if d != 0)
v73_pass_rate = (sum(v73_vec) / n_pairs) if n_pairs else 0.0
v72_pass_rate = (sum(v72_vec) / n_pairs) if n_pairs else 0.0

wilcoxon_p = None
wilcoxon_stat = None
wilcoxon_note = ""
if HAS_SCIPY:
    if n_nonzero_delta >= 1:
        try:
            res = wilcoxon(v73_vec, v72_vec, zero_method="wilcox", alternative="two-sided")
            wilcoxon_stat = float(res.statistic)
            wilcoxon_p = float(res.pvalue)
        except Exception as e:
            wilcoxon_note = f"wilcoxon failed: {e}"
    else:
        wilcoxon_note = "all paired deltas are zero -- no signed-rank test possible (no regression and no improvement)"
else:
    wilcoxon_note = "scipy not installed -- pip install scipy on this VM to compute paired Wilcoxon"


# Localization paired diffs
def loc_avg(d: dict, key: str) -> float | None:
    vals = [r[key] for r in d.values() if key in r]
    if not vals:
        return None
    return sum(vals) / len(vals)


# --- Render verdict markdown ---
lines = []
lines.append(f"# v7.3 verification gate -- VERDICT")
lines.append("")
lines.append(f"- v7.3 arm dir: `{arm_v73_dir}`")
lines.append(f"- v7.2 arm dir: `{arm_v72_dir}`")
lines.append(f"- Paired tasks: n={n_pairs}")
lines.append("")
lines.append("## Per-arm verify_report.py verdict")
lines.append("")
lines.append("| Arm | verify_report verdict |")
lines.append("|---|---|")
lines.append(f"| v7.3 | {v73_verdict} |")
lines.append(f"| v7.2 | {v72_verdict} |")
lines.append("")
lines.append("(Full per-arm gate tables are appended to `verify_results.md` Part 3 by verify_report.py.)")
lines.append("")
lines.append("## Paired pass@1")
lines.append("")
lines.append("| Metric | v7.3 | v7.2 | delta |")
lines.append("|---|---|---|---|")
lines.append(f"| pass_rate | {v73_pass_rate:.3f} | {v72_pass_rate:.3f} | {(v73_pass_rate - v72_pass_rate):+.3f} |")
lines.append(f"| n_paired | {n_pairs} | {n_pairs} | -- |")
lines.append(f"| nonzero_deltas | -- | -- | {n_nonzero_delta} |")
lines.append("")
lines.append("Per-task deltas (v7.3 - v7.2): " + ", ".join(str(d) for d in deltas))
lines.append("")
lines.append("Paired Wilcoxon signed-rank (two-sided):")
if wilcoxon_p is not None:
    lines.append(f"- statistic = {wilcoxon_stat:.4f}")
    lines.append(f"- p-value = {wilcoxon_p:.4f}")
else:
    lines.append(f"- not computed: {wilcoxon_note}")
lines.append("")

lines.append("## Localization metrics (mean across tasks)")
lines.append("")
lines.append("| Metric | v7.3 | v7.2 |")
lines.append("|---|---|---|")
for key in ("first_gold", "precision", "coverage"):
    a = loc_avg(v73_loc, key)
    b = loc_avg(v72_loc, key)
    a_s = f"{a:.3f}" if isinstance(a, (int, float)) else "n/a"
    b_s = f"{b:.3f}" if isinstance(b, (int, float)) else "n/a"
    lines.append(f"| {key} | {a_s} | {b_s} |")
lines.append("")

lines.append("## Brief size + sanitizer counts")
lines.append("")
lines.append("| Metric | v7.3 | v7.2 |")
lines.append("|---|---|---|")
for key in ("n", "min", "median", "mean", "max", "sanitizer_affected_lines_total", "sanitizer_events"):
    a = v73_brief.get(key)
    b = v72_brief.get(key)
    lines.append(f"| {key} | {a} | {b} |")
lines.append("")

lines.append("## Confidence distribution (per-task brief confidence)")
lines.append("")
lines.append("| Bucket | v7.3 | v7.2 |")
lines.append("|---|---|---|")
for key in ("lt_0_5", "0_5_to_0_6", "0_6_to_0_7", "ge_0_7", "unknown"):
    lines.append(f"| {key} | {v73_conf.get(key, 0)} | {v72_conf.get(key, 0)} |")
lines.append("")

# --- Top-level outcome flag (used by decision_tree.sh) ---
both_pass = (v73_verdict == "PASS" and v72_verdict == "PASS")
if not both_pass:
    if v73_verdict == "FAIL" or v72_verdict == "FAIL":
        outcome = "ARM_FAIL"
    else:
        outcome = "INCONCLUSIVE"
elif wilcoxon_p is None:
    outcome = "BOTH_PASS_NO_PAIRED_TEST"
elif wilcoxon_p >= 0.10 and (v73_pass_rate - v72_pass_rate) >= -0.001:
    outcome = "BOTH_PASS_NO_REGRESSION"
elif wilcoxon_p < 0.05 and (v73_pass_rate - v72_pass_rate) < 0:
    outcome = "BOTH_PASS_V73_REGRESSION"
else:
    outcome = "BOTH_PASS_AMBIGUOUS"

lines.append("## Top-level outcome")
lines.append("")
lines.append(f"OUTCOME: `{outcome}`")
lines.append("")
lines.append("Run `bash /opt/gt_bundle/v7_3_gate/decision_tree.md` -- the README maps each outcome")
lines.append("to a recommended action per handoff section 4.")
lines.append("")

# Marker line for decision_tree consumer.
lines.append(f"<!-- gate_outcome: {outcome} -->")
lines.append(f"<!-- v73_verdict: {v73_verdict} -->")
lines.append(f"<!-- v72_verdict: {v72_verdict} -->")
lines.append(f"<!-- v73_pass_rate: {v73_pass_rate:.3f} -->")
lines.append(f"<!-- v72_pass_rate: {v72_pass_rate:.3f} -->")
if wilcoxon_p is not None:
    lines.append(f"<!-- wilcoxon_p: {wilcoxon_p:.4f} -->")
else:
    lines.append(f"<!-- wilcoxon_p: na -->")

text = "\n".join(lines) + "\n"
verdict_path.write_text(text)
print(text)
PYEOF

echo ""
echo "============================================================"
echo "  Verdict written: $VERDICT"
echo "  verify_report.py per-run sections appended to verify_results.md"
echo "  Next: read $VERDICT, then consult decision_tree.md."
echo "============================================================"

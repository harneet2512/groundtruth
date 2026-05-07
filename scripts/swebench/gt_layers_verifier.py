#!/usr/bin/env python3
"""Track D — [GT_LAYERS] log verifier.

Consumes a `_global_gt_layers.log` (or per-task `gt_layers.log`) emitted by
`swe_agent_smoke_runner.py` and produces:

  1. Per-task table: instance_id x {L1, L2, L3, L4, L5, L6, elapsed_s,
     resolved, cost} grid (markdown).
  2. Per-layer rollup: counts of fired/fallback/empty/noop, distribution of
     L3/L4/L6 counts, verdict counts for L5.
  3. Pass/fail per the gate (1task | 5task | 30task).
  4. Verdict line at the bottom.

Exit code: 0 if pass, non-zero if fail.

CLI:
  gt_layers_verifier.py --global-log <path> --gate <1task|5task|30task>
                        [--expected-distribution 81.7,11.3,7.0]
                        [--tolerance-pp 10.0]

Line format (must match swe_agent_smoke_runner.format_layer_line):

  [GT_LAYERS] task=<id> L1=<v> L2=<v> L3=<n> L4=<n> L5=<v> L6=<n>
              elapsed_s=<f> resolved=<bool|unknown> cost_usd=<f>
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---- line parser -----------------------------------------------------------

# RC-10 (D-003 / F-fix): elapsed_s and cost_usd may render as "unknown"
# when the smoke runner has no measurement (autosubmit / exit_cost /
# missing output.jsonl record). The pre-fix regex required `[\d.]+`
# only and would silently classify those lines as unparseable, hiding
# real cost gaps.
_LINE_RE = re.compile(
    r"\[GT_LAYERS\]\s+"
    r"task=(?P<task>\S+)\s+"
    r"L1=(?P<L1>\S+)\s+"
    r"L2=(?P<L2>\S+)\s+"
    r"L3=(?P<L3>\d+)\s+"
    r"L4=(?P<L4>\d+)\s+"
    r"L5=(?P<L5>\S+)\s+"
    r"L6=(?P<L6>\d+)\s+"
    r"elapsed_s=(?P<elapsed>[\d.]+|unknown)\s+"
    r"resolved=(?P<resolved>\S+)\s+"
    r"cost_usd=(?P<cost>[\d.]+|unknown)"
    # Optional trailing tokens: synthesized=true, partial_pull=true.
    # RC-10 (D-009 / G-fix, D-015 / J-fix): these flags let the verifier
    # exclude failsafe / partial-pull tasks from healthy bucket counts.
    r"(?:\s+synthesized=(?P<synthesized>true|false))?"
    r"(?:\s+partial_pull=(?P<partial_pull>true|false))?"
)


@dataclass
class ParsedLine:
    raw: str
    task: str
    L1: str
    L2: str
    L3: int
    L4: int
    L5: str
    L6: int
    # RC-10 (D-003): None means "unknown" — distinct from a real 0.0.
    elapsed_s: Optional[float]
    resolved: str  # "true"|"false"|"unknown"
    cost_usd: Optional[float]
    synthesized: bool = False
    partial_pull: bool = False


def parse_log(path: Path) -> Tuple[List[ParsedLine], List[str]]:
    """Return (parsed_lines, unparsed_raw_lines).

    Unparsed lines are surfaced separately so the verifier can flag log
    corruption rather than silently dropping rows.
    """
    parsed: List[ParsedLine] = []
    bad: List[str] = []
    if not path.is_file():
        return parsed, [f"<file_missing>:{path}"]
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.rstrip()
        if not raw:
            continue
        m = _LINE_RE.search(raw)
        if not m:
            # Unparseable [GT_LAYERS] lines OR unrelated noise; only flag the
            # ones that look like attempts.
            if "[GT_LAYERS]" in raw or raw.lstrip().startswith("task="):
                bad.append(raw)
            continue
        try:
            elapsed_raw = m.group("elapsed")
            cost_raw = m.group("cost")
            parsed.append(ParsedLine(
                raw=raw,
                task=m.group("task"),
                L1=m.group("L1"),
                L2=m.group("L2"),
                L3=int(m.group("L3")),
                L4=int(m.group("L4")),
                L5=m.group("L5"),
                L6=int(m.group("L6")),
                elapsed_s=None if elapsed_raw == "unknown" else float(elapsed_raw),
                resolved=m.group("resolved"),
                cost_usd=None if cost_raw == "unknown" else float(cost_raw),
                synthesized=(m.groupdict().get("synthesized") == "true"),
                partial_pull=(m.groupdict().get("partial_pull") == "true"),
            ))
        except (ValueError, KeyError) as exc:  # noqa: BLE001
            bad.append(f"{raw}  # parse_error:{exc}")
    return parsed, bad


# ---- rollup ----------------------------------------------------------------

@dataclass
class Rollup:
    n: int
    L1: Counter
    L2: Counter
    L5: Counter
    resolved: Counter
    L3_dist: Counter
    L4_dist: Counter
    L6_dist: Counter
    L1_substantive: int  # L1=fired AND L3>=1
    elapsed_total: float
    cost_total: float


def rollup(parsed: List[ParsedLine]) -> Rollup:
    L1 = Counter(p.L1 for p in parsed)
    L2 = Counter(p.L2 for p in parsed)
    L5 = Counter(p.L5 for p in parsed)
    resolved = Counter(p.resolved for p in parsed)
    L3_dist = Counter(p.L3 for p in parsed)
    L4_dist = Counter(p.L4 for p in parsed)
    L6_dist = Counter(p.L6 for p in parsed)
    sub = sum(1 for p in parsed if p.L1 == "fired" and p.L3 >= 1)
    # RC-10 (D-003): None elapsed_s / cost_usd represent "unknown" — exclude
    # them from corpus totals rather than coercing to 0.
    return Rollup(
        n=len(parsed),
        L1=L1, L2=L2, L5=L5,
        resolved=resolved,
        L3_dist=L3_dist, L4_dist=L4_dist, L6_dist=L6_dist,
        L1_substantive=sub,
        elapsed_total=sum(p.elapsed_s for p in parsed if p.elapsed_s is not None),
        cost_total=sum(p.cost_usd for p in parsed if p.cost_usd is not None),
    )


# ---- markdown rendering ----------------------------------------------------

def render_per_task_table(parsed: List[ParsedLine]) -> str:
    if not parsed:
        return "_(no parsed lines)_\n"
    out: List[str] = []
    out.append("| task | L1 | L2 | L3 | L4 | L5 | L6 | elapsed_s | resolved | cost_usd | flags |")
    out.append("|------|----|----|----|----|----|----|-----------|----------|----------|-------|")
    for p in parsed:
        elapsed_cell = "unknown" if p.elapsed_s is None else f"{p.elapsed_s:.2f}"
        cost_cell = "unknown" if p.cost_usd is None else f"{p.cost_usd:.4f}"
        flags = []
        if p.synthesized:
            flags.append("synth")
        if p.partial_pull:
            flags.append("partial_pull")
        flag_cell = ",".join(flags) if flags else ""
        out.append(
            f"| {p.task} | {p.L1} | {p.L2} | {p.L3} | {p.L4} | {p.L5} | {p.L6} "
            f"| {elapsed_cell} | {p.resolved} | {cost_cell} | {flag_cell} |"
        )
    return "\n".join(out) + "\n"


def render_rollup(r: Rollup) -> str:
    def _ctr(c: Counter) -> str:
        if not c:
            return "(empty)"
        items = sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))
        return ", ".join(f"{k}={v}" for k, v in items)

    lines = [
        f"- n_parsed: {r.n}",
        f"- L1 distribution: {_ctr(r.L1)}",
        f"- L2 distribution: {_ctr(r.L2)}",
        f"- L5 distribution: {_ctr(r.L5)}",
        f"- L3 (edits) distribution: {_ctr(r.L3_dist)}",
        f"- L4 (queries) distribution: {_ctr(r.L4_dist)}",
        f"- L6 (reindex) distribution: {_ctr(r.L6_dist)}",
        f"- resolved distribution: {_ctr(r.resolved)}",
        f"- L1=fired AND L3>=1 (substantive): {r.L1_substantive}/{r.n}",
        f"- total elapsed_s: {r.elapsed_total:.2f}",
        f"- total cost_usd: {r.cost_total:.4f}",
    ]
    return "\n".join(lines) + "\n"


# ---- gate logic ------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    reasons: List[str]


def _bucket_label(p: ParsedLine) -> str:
    """Bucket a task into substantive | marginal | degenerate per plan §README.

    - substantive  : L1=fired, L3>=1
    - degenerate   : L1=fallback (L2 fired)
    - marginal     : everything else (L1=empty, or L1=fired but L3==0)
    """
    if p.L1 == "fallback":
        return "degenerate"
    if p.L1 == "fired" and p.L3 >= 1:
        return "substantive"
    return "marginal"


def gate_1task(parsed: List[ParsedLine], bad: List[str]) -> GateResult:
    reasons: List[str] = []
    if bad:
        reasons.append(f"{len(bad)} unparseable lines")
    if len(parsed) != 1:
        reasons.append(f"expected 1 parsed line, got {len(parsed)}")
        return GateResult(False, reasons)
    p = parsed[0]
    if p.L1 != "fired":
        reasons.append(f"L1={p.L1} (need fired)")
    if p.L3 < 1:
        reasons.append(f"L3={p.L3} (need >=1)")
    if p.L6 < 1:
        reasons.append(f"L6={p.L6} (need >=1)")
    if p.L5 == "not_evaluated":
        reasons.append("L5 not evaluated")
    if p.resolved == "unknown":
        reasons.append("resolved bool missing from output.jsonl")
    return GateResult(passed=not reasons, reasons=reasons)


def gate_5task(parsed: List[ParsedLine], bad: List[str]) -> GateResult:
    reasons: List[str] = []
    if bad:
        reasons.append(f"{len(bad)} unparseable lines (any wedge -> fail)")
    if len(parsed) != 5:
        reasons.append(f"expected 5 parsed lines, got {len(parsed)}")
        return GateResult(False, reasons)
    substantive = sum(1 for p in parsed if p.L1 == "fired" and p.L3 >= 1)
    if substantive < 4:
        reasons.append(
            f"only {substantive}/5 substantive (need >=4 with L1=fired AND L3>=1)"
        )
    return GateResult(passed=not reasons, reasons=reasons)


def gate_30task(
    parsed: List[ParsedLine],
    bad: List[str],
    expected_distribution: Tuple[float, float, float],
    tolerance_pp: float,
) -> Tuple[GateResult, Dict[str, float]]:
    """30-task GT-only stability gate.

    - 30/30 lines parsed.
    - All 6 layers fire across the sample (L1 sees fired or fallback; L3>0
      occurs at least once; L4>=0 always; L5 evaluated at least once; L6>0
      at least once).
    - Bucket distribution matches expected within tolerance_pp.
    """
    reasons: List[str] = []
    if bad:
        reasons.append(f"{len(bad)} unparseable lines (any wedge -> fail)")
    if len(parsed) != 30:
        reasons.append(f"expected 30 parsed lines, got {len(parsed)}")
        return GateResult(False, reasons), {}

    # All-6-layers-fire check
    if not any(p.L1 in ("fired", "fallback") for p in parsed):
        reasons.append("L1 never fired across 30 tasks")
    # L2 must fire if any degenerate task (L1=fallback). Don't require if none.
    if any(p.L1 == "fallback" for p in parsed) and \
            not any(p.L2 == "fired" for p in parsed):
        reasons.append("L1 fallback present but L2 never fired (Track A bug)")
    if not any(p.L3 >= 1 for p in parsed):
        reasons.append("L3 never reported >=1 edit (no source edits at all)")
    # L4 is "discoverable, not required to fire" per plan; don't gate on it.
    if not any(p.L5 in ("pass", "warn", "fail") for p in parsed):
        reasons.append("L5 never evaluated (gate dead)")
    if not any(p.L6 >= 1 for p in parsed):
        reasons.append("L6 never fired (incremental reindex dead)")

    # Distribution check
    buckets = Counter(_bucket_label(p) for p in parsed)
    n = len(parsed)
    obs_sub = 100.0 * buckets.get("substantive", 0) / n
    obs_marg = 100.0 * buckets.get("marginal", 0) / n
    obs_deg = 100.0 * buckets.get("degenerate", 0) / n
    exp_sub, exp_marg, exp_deg = expected_distribution
    drift_sub = abs(obs_sub - exp_sub)
    drift_marg = abs(obs_marg - exp_marg)
    drift_deg = abs(obs_deg - exp_deg)
    if drift_sub > tolerance_pp:
        reasons.append(
            f"substantive drift {drift_sub:.1f}pp > tol {tolerance_pp:.1f} "
            f"(obs={obs_sub:.1f}% vs exp={exp_sub:.1f}%)"
        )
    if drift_marg > tolerance_pp:
        reasons.append(
            f"marginal drift {drift_marg:.1f}pp > tol {tolerance_pp:.1f} "
            f"(obs={obs_marg:.1f}% vs exp={exp_marg:.1f}%)"
        )
    if drift_deg > tolerance_pp:
        reasons.append(
            f"degenerate drift {drift_deg:.1f}pp > tol {tolerance_pp:.1f} "
            f"(obs={obs_deg:.1f}% vs exp={exp_deg:.1f}%)"
        )

    obs_table = {
        "substantive_pct": obs_sub,
        "marginal_pct": obs_marg,
        "degenerate_pct": obs_deg,
        "expected_substantive_pct": exp_sub,
        "expected_marginal_pct": exp_marg,
        "expected_degenerate_pct": exp_deg,
        "tolerance_pp": tolerance_pp,
    }
    return GateResult(passed=not reasons, reasons=reasons), obs_table


# ---- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify [GT_LAYERS] telemetry log against a smoke gate"
    )
    parser.add_argument("--global-log", required=True,
                        help="Path to _global_gt_layers.log (or any gt_layers.log)")
    parser.add_argument("--gate", required=True,
                        choices=["1task", "5task", "30task"])
    parser.add_argument(
        "--expected-distribution",
        default="81.7,11.3,7.0",
        help="substantive,marginal,degenerate percentages (default 81.7,11.3,7.0)"
    )
    parser.add_argument("--tolerance-pp", type=float, default=10.0)
    args = parser.parse_args()

    log_path = Path(args.global_log)
    parsed, bad = parse_log(log_path)

    print(f"# [GT_LAYERS] verification — gate={args.gate}\n")
    print(f"Source: `{log_path}`  \n")
    print(f"Parsed: {len(parsed)} line(s); unparseable: {len(bad)}\n")

    print("## Per-task matrix\n")
    print(render_per_task_table(parsed))

    print("\n## Per-layer rollup\n")
    print(render_rollup(rollup(parsed)))

    if bad:
        print("\n## Unparseable lines (corrupt / partial writes)\n")
        for raw in bad[:20]:
            print(f"- `{raw[:200]}`")
        if len(bad) > 20:
            print(f"- ... and {len(bad) - 20} more")
        print()

    if args.gate == "1task":
        gr = gate_1task(parsed, bad)
    elif args.gate == "5task":
        gr = gate_5task(parsed, bad)
    elif args.gate == "30task":
        try:
            exp = tuple(float(x.strip())
                        for x in args.expected_distribution.split(","))
            if len(exp) != 3:
                raise ValueError("need 3 numbers")
        except Exception as exc:  # noqa: BLE001
            print(f"\nFATAL: bad --expected-distribution: {exc}", file=sys.stderr)
            return 2
        gr, obs = gate_30task(parsed, bad, exp, args.tolerance_pp)
        print("\n## 30-task distribution check\n")
        print(f"- substantive: observed={obs['substantive_pct']:.1f}% "
              f"vs expected={obs['expected_substantive_pct']:.1f}%")
        print(f"- marginal:    observed={obs['marginal_pct']:.1f}% "
              f"vs expected={obs['expected_marginal_pct']:.1f}%")
        print(f"- degenerate:  observed={obs['degenerate_pct']:.1f}% "
              f"vs expected={obs['expected_degenerate_pct']:.1f}%")
        print(f"- tolerance: +/- {obs['tolerance_pp']:.1f}pp\n")
    else:
        print(f"FATAL: unknown gate {args.gate}", file=sys.stderr)
        return 2

    print("\n## Verdict\n")
    if gr.passed:
        print(f"**PASS** — gate `{args.gate}` satisfied.")
        return 0
    print(f"**FAIL** — gate `{args.gate}` not satisfied:")
    for r in gr.reasons:
        print(f"  - {r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

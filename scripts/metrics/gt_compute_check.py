#!/usr/bin/env python3
"""gt_compute_check.py — the EXECUTABLE behind the §5 GT-compute doctrine.

Task #30 R5 (2026-07-29): `.agents/skills/gt-compute/` states the compute doctrine
(CLAUDE.md §5 — three levels, per-task deltas before aggregation, locked union with
explicit missingness, paired bootstrap, median + +/-/tie, McNemar, absolute pp before
relative lift, never condition efficiency on resolves, never double-subtract GT tokens)
in prose only.  `compute_paired_metrics.py` now IMPLEMENTS most of it — but nothing
CHECKS that a produced artifact actually carries the doctrine.  Prose plus a good
analyzer is still an honour system: a hand-edited report, an older analyzer version, a
downstream re-write, or a future refactor can all ship a report that silently violates
§5 and nothing fails.

This is a VALIDATOR, not another analyzer.  It recomputes nothing about the RUN; it
recomputes only what the artifact must be internally consistent with (the paired
resolution count, the measured net-token count) and otherwise asserts structure.  It
never opens a trajectory, never touches an arm directory, and never writes to the
report it is given.

Third status on purpose: a check that the artifact cannot answer reports UNVERIFIABLE,
never PASS.  Claiming "no violation" from an artifact that carries no evidence either
way is exactly the failure mode §5 exists to stop.

Usage:
    python scripts/metrics/gt_compute_check.py <paired_report_*.json | gt_metrics_report.json>
    python scripts/metrics/gt_compute_check.py <report.json> --json
    python scripts/metrics/gt_compute_check.py <report.json> --strict-unverifiable

Exit codes:
    0  every check PASS (UNVERIFIABLE allowed unless --strict-unverifiable)
    1  at least one named violation (or, under --strict-unverifiable, any UNVERIFIABLE)
    2  the file could not be read/parsed as a report
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIABLE = "UNVERIFIABLE"

# Recomputation tolerance: the analyzer rounds floats to 8dp (`_fmt`), so anything
# looser than that is a real disagreement, not float noise.
PP_TOLERANCE = 1e-6


class CheckResult:
    """One doctrine check: a status plus the NAMED violations behind it."""

    def __init__(self, check: str, doctrine: str) -> None:
        self.check = check
        self.doctrine = doctrine
        self.violations: List[Dict[str, str]] = []
        self.unverifiable: List[Dict[str, str]] = []

    def violate(self, name: str, detail: str) -> None:
        self.violations.append({"name": name, "detail": detail})

    def unverified(self, name: str, detail: str) -> None:
        self.unverifiable.append({"name": name, "detail": detail})

    @property
    def status(self) -> str:
        if self.violations:
            return FAIL
        if self.unverifiable:
            return UNVERIFIABLE
        return PASS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "doctrine": self.doctrine,
            "status": self.status,
            "violations": self.violations,
            "unverifiable": self.unverifiable,
        }


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _per_task(report: Dict[str, Any]) -> Dict[str, Any]:
    pt = report.get("per_task")
    return pt if isinstance(pt, dict) else {}


def _resolved(side: Any) -> Optional[bool]:
    if isinstance(side, dict) and "resolved" in side:
        return bool(side["resolved"])
    return None


# ---------------------------------------------------------------------------
# C1 — locked population, explicit missingness
# ---------------------------------------------------------------------------

def check_population(report: Dict[str, Any]) -> CheckResult:
    r = CheckResult(
        "population_locked_union",
        "§5 locked task union; explicit missingness — every unpaired task NAMED with a reason",
    )
    pop = report.get("population")
    if not isinstance(pop, dict):
        r.violate(
            "POPULATION_BLOCK_ABSENT",
            "report carries no `population` block — a silent intersection is indistinguishable "
            "from a complete population",
        )
        return r

    n_union = pop.get("n_union")
    n_paired = pop.get("n_paired")
    if not _is_number(n_union):
        r.violate("POPULATION_N_UNION_ABSENT", "population.n_union missing or non-numeric")
    if not _is_number(n_paired):
        r.violate("POPULATION_N_PAIRED_ABSENT", "population.n_paired missing or non-numeric")
    if _is_number(n_union) and _is_number(n_paired) and n_union < n_paired:
        r.violate(
            "POPULATION_UNION_LT_PAIRED",
            f"n_union={n_union} < n_paired={n_paired}: the union cannot be smaller than the "
            f"paired subset it contains",
        )

    missing = pop.get("missing_tasks")
    if not isinstance(missing, dict):
        r.violate(
            "POPULATION_MISSING_TASKS_ABSENT",
            "population.missing_tasks missing — unpaired tasks would vanish unnamed",
        )
        return r

    for tid, entry in sorted(missing.items()):
        if not isinstance(entry, dict):
            r.violate(
                f"POPULATION_MISSING_REASON_ABSENT:{tid}",
                f"missing_tasks[{tid}] is not an object",
            )
            continue
        reason = entry.get("missing_reason")
        if not isinstance(reason, str) or not reason.strip():
            r.violate(
                f"POPULATION_MISSING_REASON_ABSENT:{tid}",
                f"missing_tasks[{tid}] carries no non-empty missing_reason",
            )

    # Arithmetic guard: union == paired + tasks missing from exactly ONE arm.  Ids absent
    # from BOTH arms (a locked-task-set id no arm ran) are named in missing_tasks by the
    # runner but were never in either arm's key set, so they are excluded here.
    if _is_number(n_union) and _is_number(n_paired):
        one_arm = sum(
            1 for e in missing.values()
            if isinstance(e, dict) and (e.get("in_baseline") or e.get("in_oracle"))
        )
        if n_union != n_paired + one_arm:
            r.violate(
                "POPULATION_COUNT_MISMATCH",
                f"n_union={n_union} != n_paired={n_paired} + single-arm missing={one_arm}",
            )

    pt = _per_task(report)
    if pt and _is_number(n_paired) and len(pt) != n_paired:
        r.violate(
            "POPULATION_PER_TASK_COUNT_MISMATCH",
            f"per_task has {len(pt)} rows but population.n_paired={n_paired}",
        )
    return r


# ---------------------------------------------------------------------------
# C2 — absolute pp on the PAIRED denominator, recomputed from per_task
# ---------------------------------------------------------------------------

def check_absolute_resolution_pp(report: Dict[str, Any]) -> CheckResult:
    r = CheckResult(
        "absolute_resolution_pp",
        "§5 absolute pp before relative lift, computed on the PAIRED denominator",
    )
    headline = report.get("headline")
    if not isinstance(headline, dict):
        r.violate("HEADLINE_BLOCK_ABSENT", "report carries no `headline` block")
        return r
    if "absolute_resolution_pp" not in headline:
        r.violate(
            "HEADLINE_ABSOLUTE_PP_ABSENT",
            "headline carries no absolute_resolution_pp — a relative lift alone is not §5-citable",
        )
        return r

    stated = headline["absolute_resolution_pp"]
    pt = _per_task(report)
    if not pt:
        r.unverified(
            "HEADLINE_ABSOLUTE_PP_UNVERIFIABLE",
            "no per_task rows: the stated pp cannot be recomputed from the artifact",
        )
        return r

    n_paired = len(pt)
    base_res = 0
    ora_res = 0
    for tid, row in pt.items():
        b = _resolved(row.get("baseline"))
        o = _resolved(row.get("oracle"))
        if b is None or o is None:
            r.unverified(
                f"HEADLINE_ABSOLUTE_PP_ROW_UNVERIFIABLE:{tid}",
                f"per_task[{tid}] has no baseline/oracle `resolved` field",
            )
            return r
        base_res += int(b)
        ora_res += int(o)

    recomputed = (ora_res - base_res) / n_paired * 100.0 if n_paired else None
    if recomputed is None:
        r.unverified(
            "HEADLINE_ABSOLUTE_PP_UNVERIFIABLE",
            "n_paired == 0: pp is undefined, nothing to compare",
        )
        return r
    if not _is_number(stated):
        r.violate(
            "HEADLINE_ABSOLUTE_PP_MISMATCH",
            f"absolute_resolution_pp={stated!r} is not numeric but the paired population "
            f"({n_paired} tasks) yields {recomputed}",
        )
        return r
    if abs(float(stated) - recomputed) > PP_TOLERANCE:
        r.violate(
            "HEADLINE_ABSOLUTE_PP_MISMATCH",
            f"headline absolute_resolution_pp={stated} but per_task recomputation on the "
            f"paired denominator ({ora_res}-{base_res})/{n_paired}*100 = {recomputed}",
        )

    # The paired counts must ALSO be the ones the headline reports; a headline that
    # differences full-run counts is denominator-blind even when the pp happens to match.
    for key, expected in (
        ("resolved_baseline_paired", base_res),
        ("resolved_oracle_paired", ora_res),
    ):
        if key not in headline:
            r.violate(
                f"HEADLINE_PAIRED_COUNT_ABSENT:{key}",
                f"headline carries no {key}; the pp denominator is unstated",
            )
        elif _is_number(headline[key]) and int(headline[key]) != expected:
            r.violate(
                f"HEADLINE_PAIRED_COUNT_MISMATCH:{key}",
                f"headline {key}={headline[key]} but per_task shows {expected}",
            )
    return r


# ---------------------------------------------------------------------------
# C3 — net tokens: never fabricated from missing telemetry
# ---------------------------------------------------------------------------

def check_net_tokens_missingness(report: Dict[str, Any]) -> CheckResult:
    r = CheckResult(
        "net_tokens_missingness",
        "§5 net tokens off-on; a missing-telemetry task is NULL + named reason, never a "
        "manufactured 0, and GT tokens are never double-subtracted",
    )
    pt = _per_task(report)
    if not pt:
        r.unverified(
            "NET_TOKENS_UNVERIFIABLE",
            "no per_task rows: net-token missingness cannot be inspected",
        )
        return r

    n_measured = 0
    seen_field = False
    for tid, row in sorted(pt.items()):
        delta = row.get("delta")
        if not isinstance(delta, dict):
            r.violate(
                f"PER_TASK_DELTA_ABSENT:{tid}",
                f"per_task[{tid}] carries no `delta` block — §5 requires per-task deltas "
                f"BEFORE aggregation",
            )
            continue
        if "net_tokens_off_minus_on" not in delta:
            continue
        seen_field = True
        value = delta["net_tokens_off_minus_on"]
        reason = delta.get("net_tokens_missing_reason")
        if value is None:
            if not isinstance(reason, str) or not reason.strip():
                r.violate(
                    f"NET_TOKENS_MISSING_REASON_ABSENT:{tid}",
                    f"per_task[{tid}] net_tokens_off_minus_on is null with no "
                    f"net_tokens_missing_reason — unmeasured is indistinguishable from zero",
                )
        else:
            if not _is_number(value):
                r.violate(
                    f"NET_TOKENS_NON_NUMERIC:{tid}",
                    f"per_task[{tid}] net_tokens_off_minus_on={value!r} is neither null nor numeric",
                )
                continue
            n_measured += 1
            if isinstance(reason, str) and reason.strip():
                r.violate(
                    f"NET_TOKENS_FABRICATED:{tid}",
                    f"per_task[{tid}] carries a measured net_tokens value ({value}) AND "
                    f"net_tokens_missing_reason={reason!r} — a value derived from telemetry "
                    f"declared missing",
                )

    if not seen_field:
        r.unverified(
            "NET_TOKENS_UNVERIFIABLE",
            "no per_task row carries net_tokens_off_minus_on: this artifact does not measure "
            "the §5 net-token endpoint",
        )
        return r

    headline = report.get("headline")
    if isinstance(headline, dict) and "net_tokens_n_measured" in headline:
        stated = headline["net_tokens_n_measured"]
        if not _is_number(stated) or int(stated) != n_measured:
            r.violate(
                "NET_TOKENS_N_MEASURED_MISMATCH",
                f"headline net_tokens_n_measured={stated} but per_task carries {n_measured} "
                f"measured rows",
            )
    return r


# ---------------------------------------------------------------------------
# C4 — every delta aggregate carries its sign split
# ---------------------------------------------------------------------------

def check_aggregate_sign_split(report: Dict[str, Any]) -> CheckResult:
    r = CheckResult(
        "aggregate_sign_split",
        "§5 report median and +/-/tie counts with every mean; n_positive+n_negative+n_tie == n",
    )
    aggregate = report.get("aggregate")
    if not isinstance(aggregate, dict):
        r.unverified(
            "AGGREGATE_BLOCK_ABSENT",
            "report carries no `aggregate` block: no delta aggregates to check",
        )
        return r

    checked = 0
    for name, agg in sorted(aggregate.items()):
        if not isinstance(agg, dict):
            continue
        # Only aggregates that actually report a paired delta are in scope; a
        # report-only block (e.g. M14_overhead) has no deltas to split.
        if "delta_mean" not in agg or "n" not in agg:
            continue
        checked += 1
        missing_fields = [
            f for f in ("n_positive", "n_negative", "n_tie")
            if not _is_number(agg.get(f))
        ]
        if missing_fields:
            for f in missing_fields:
                r.violate(
                    f"AGGREGATE_SIGN_SPLIT_ABSENT:{name}:{f}",
                    f"aggregate[{name}] reports delta_mean but no {f} — a mean without its "
                    f"sign split hides a bimodal delta",
                )
            continue
        if "delta_median" not in agg:
            r.violate(
                f"AGGREGATE_MEDIAN_ABSENT:{name}",
                f"aggregate[{name}] reports delta_mean with no delta_median",
            )
        n = agg["n"]
        total = agg["n_positive"] + agg["n_negative"] + agg["n_tie"]
        if _is_number(n) and total != n:
            r.violate(
                f"AGGREGATE_SIGN_SPLIT_MISMATCH:{name}",
                f"aggregate[{name}] n_positive+n_negative+n_tie={total} != n={n} — deltas are "
                f"unaccounted for",
            )

    if checked == 0 and not r.violations:
        r.unverified(
            "AGGREGATE_SIGN_SPLIT_UNVERIFIABLE",
            "no aggregate reports a paired delta_mean: nothing to sign-split",
        )
    return r


# ---------------------------------------------------------------------------
# C5 — multiplicity control + bootstrap CIs
# ---------------------------------------------------------------------------

def check_statistical_tests(report: Dict[str, Any]) -> CheckResult:
    r = CheckResult(
        "statistical_tests",
        "§5 paired bootstrap CI on every delta test; family-wise multiplicity control "
        "(Holm) over the battery before any significance is trusted",
    )
    tests = report.get("statistical_tests")
    if not isinstance(tests, dict):
        r.violate(
            "STATISTICAL_TESTS_ABSENT",
            "report carries no `statistical_tests` block",
        )
        return r

    holm = tests.get("_holm")
    if not isinstance(holm, dict):
        r.violate(
            "STATS_HOLM_BLOCK_ABSENT",
            "statistical_tests carries no `_holm` block — an uncorrected battery of one-sided "
            "tests has a family-wise error rate far above alpha",
        )
    else:
        for field in ("n_tests", "alpha", "method"):
            if field not in holm:
                r.violate(
                    f"STATS_HOLM_FIELD_ABSENT:{field}",
                    f"statistical_tests._holm carries no {field}",
                )

    n_delta_tests = 0
    for name, t in sorted(tests.items()):
        if name == "_holm" or not isinstance(t, dict):
            continue
        if "mcnemar" in name or "binomial" in name:
            # A discordant-pair / count test has no continuous delta and therefore no
            # bootstrap CI over deltas; requiring one would be a false positive.
            continue
        n_delta_tests += 1
        for field in ("bootstrap_ci_lo", "bootstrap_ci_hi"):
            if field not in t:
                r.violate(
                    f"STATS_BOOTSTRAP_CI_ABSENT:{name}:{field}",
                    f"statistical_tests[{name}] reports a paired delta test with no {field}",
                )
        if "median_delta" not in t:
            r.violate(
                f"STATS_MEDIAN_ABSENT:{name}",
                f"statistical_tests[{name}] carries no median_delta",
            )

    if n_delta_tests == 0 and isinstance(holm, dict):
        r.unverified(
            "STATS_BOOTSTRAP_UNVERIFIABLE",
            "no continuous delta test present: bootstrap CI coverage cannot be checked",
        )
    return r


# ---------------------------------------------------------------------------
# C6 — efficiency never conditioned on resolves
# ---------------------------------------------------------------------------

def check_efficiency_not_conditioned_on_resolves(report: Dict[str, Any]) -> CheckResult:
    r = CheckResult(
        "efficiency_not_conditioned_on_resolves",
        "§5 never condition efficiency on resolves — the net-token population must not be "
        "the resolved population",
    )
    pt = _per_task(report)
    if not pt:
        r.unverified(
            "EFFICIENCY_CONDITIONING_UNVERIFIABLE",
            "no per_task rows: the net-token population cannot be compared to the resolved "
            "population",
        )
        return r

    measured: Set[str] = set()
    resolved_oracle: Set[str] = set()
    resolved_baseline: Set[str] = set()
    saw_resolved = True
    for tid, row in pt.items():
        delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
        if _is_number(delta.get("net_tokens_off_minus_on")):
            measured.add(tid)
        o = _resolved(row.get("oracle"))
        b = _resolved(row.get("baseline"))
        if o is None or b is None:
            saw_resolved = False
            continue
        if o:
            resolved_oracle.add(tid)
        if b:
            resolved_baseline.add(tid)

    if not saw_resolved:
        r.unverified(
            "EFFICIENCY_CONDITIONING_UNVERIFIABLE",
            "per_task rows carry no per-arm `resolved` field: conditioning cannot be tested",
        )
        return r
    if not measured:
        r.unverified(
            "EFFICIENCY_CONDITIONING_UNVERIFIABLE",
            "no measured net_tokens rows: there is no efficiency population to test",
        )
        return r

    all_tids = set(pt)
    if measured == all_tids:
        # Every paired task carries a measurement: the efficiency population IS the
        # paired population, which cannot be a resolved subset. Definitively clean.
        return r

    for label, resolved in (("oracle", resolved_oracle), ("baseline", resolved_baseline)):
        if resolved == all_tids or not resolved:
            # Populations do not differ (every task resolved, or none did): the artifact
            # cannot distinguish "measured everywhere" from "measured only on resolves".
            continue
        if measured == resolved:
            r.violate(
                "EFFICIENCY_CONDITIONED_ON_RESOLVES",
                f"the {len(measured)} tasks with a measured net_tokens delta are EXACTLY the "
                f"{label}-arm resolved set ({len(resolved)} of {len(all_tids)} paired) — the "
                f"efficiency endpoint is conditioned on resolution",
            )
            return r

    populations_differ = any(
        res != all_tids and res for res in (resolved_oracle, resolved_baseline)
    )
    if not populations_differ:
        r.unverified(
            "EFFICIENCY_CONDITIONING_UNVERIFIABLE",
            "every paired task carries the same resolution outcome: conditioning on resolves is "
            "indistinguishable from measuring the full population in this artifact",
        )
        return r
    if measured <= resolved_oracle and measured <= resolved_baseline:
        r.violate(
            "EFFICIENCY_CONDITIONED_ON_RESOLVES",
            f"every net_tokens-measured task ({sorted(measured)}) is resolved in BOTH arms while "
            f"unresolved paired tasks carry no measurement — the efficiency population is a "
            f"resolved subset",
        )
    return r


CHECKS = (
    check_population,
    check_absolute_resolution_pp,
    check_net_tokens_missingness,
    check_aggregate_sign_split,
    check_statistical_tests,
    check_efficiency_not_conditioned_on_resolves,
)


def validate_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Run every §5 doctrine check against a produced report dict (never mutated)."""
    results = [fn(report) for fn in CHECKS]
    violations = [v["name"] for res in results for v in res.violations]
    unverifiable = [u["name"] for res in results for u in res.unverifiable]
    return {
        "validator": "gt_compute_check.v1",
        "doctrine": "CLAUDE.md §5 / .agents/skills/gt-compute",
        "report_schema": report.get("schema"),
        "baseline_run_id": report.get("baseline_run_id"),
        "oracle_run_id": report.get("oracle_run_id"),
        "checks": [res.to_dict() for res in results],
        "violations": violations,
        "unverifiable": unverifiable,
        "n_pass": sum(1 for res in results if res.status == PASS),
        "n_fail": sum(1 for res in results if res.status == FAIL),
        "n_unverifiable": sum(1 for res in results if res.status == UNVERIFIABLE),
        "ok": not violations,
    }


def load_report(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a report object, got {type(data).__name__}")
    return data


def render_text(result: Dict[str, Any]) -> str:
    lines = ["=== GT COMPUTE DOCTRINE CHECK (CLAUDE.md §5) ==="]
    for res in result["checks"]:
        lines.append(f"[{res['status']:<13}] {res['check']}")
        for v in res["violations"]:
            lines.append(f"    VIOLATION {v['name']}")
            lines.append(f"              {v['detail']}")
        for u in res["unverifiable"]:
            lines.append(f"    UNVERIFIABLE {u['name']}")
            lines.append(f"                 {u['detail']}")
    lines.append(
        f"--- pass={result['n_pass']} fail={result['n_fail']} "
        f"unverifiable={result['n_unverifiable']}"
    )
    if result["violations"]:
        lines.append("VIOLATIONS: " + ", ".join(result["violations"]))
        lines.append("VERDICT: FAIL — this artifact is not §5-citable.")
    elif result["unverifiable"]:
        lines.append("UNVERIFIABLE: " + ", ".join(result["unverifiable"]))
        lines.append(
            "VERDICT: PASS-WITH-UNVERIFIABLE — no violation found, but the named checks "
            "could not be answered from this artifact. Never report them as PASS."
        )
    else:
        lines.append("VERDICT: PASS — every §5 doctrine check holds on this artifact.")
    return "\n".join(lines)


def _safe_print(text: str) -> None:
    """Print without dying on a legacy console encoding."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that a produced paired report obeys the CLAUDE.md §5 compute doctrine. "
            "Checks the ARTIFACT; it does not recompute the run."
        ),
    )
    parser.add_argument(
        "report",
        help="paired_report_<timestamp>.json or gt_metrics_report.json",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="emit the machine-readable result instead of the text report",
    )
    parser.add_argument(
        "--strict-unverifiable", action="store_true",
        help="treat any UNVERIFIABLE check as a failure (exit 1)",
    )
    args = parser.parse_args(argv)

    path = Path(args.report)
    try:
        report = load_report(path)
    except Exception as exc:  # noqa: BLE001 — a bad path/parse is a usage error, exit 2
        print(f"[ERROR] cannot read report {path}: {exc}", file=sys.stderr)
        return 2

    result = validate_report(report)
    result["report_path"] = str(path)

    # The detail strings carry non-ASCII doctrine glyphs; a cp1252 console must not turn
    # a validator run into a UnicodeEncodeError.
    if args.as_json:
        _safe_print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        _safe_print(render_text(result))

    if result["violations"]:
        return 1
    if args.strict_unverifiable and result["unverifiable"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

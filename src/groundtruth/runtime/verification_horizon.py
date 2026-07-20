"""Product-owned verification horizon semantics."""
from __future__ import annotations

from dataclasses import dataclass


HORIZON_VERSION = "gt.runtime.verification_horizon.v1"


@dataclass(frozen=True)
class HorizonThresholds:
    # V-4 (2026-07-10) — PROVENANCE: these are NOT arbitrary. The live seam loads the
    # per-run values from the horizon calibration artifact (``GT_HORIZON_CALIBRATION``
    # -> ``.claude/calibration/horizon_v1.json``, its ``thresholds`` block, derived from
    # the observed edit->test-pace distribution) via
    # ``gt_mini_patch._load_horizon_calibration_defaults`` and applies them through the
    # product thresholds dataclass (env still wins per-key). The 8-dp literals below are
    # the SHIPPED DEFAULTS used only when no calibration file is present — the fallback,
    # not the source of truth. See CLAUDE.md "GT_HORIZON_CALIBRATION".
    advisory_test_coverage: float = 0.125
    advisory_budget: float = 0.11166666
    urgent_test_coverage: float = 0.125
    urgent_budget: float = 0.35333333
    gate_test_coverage: float = 1.0
    gate_cycles: float = 7.48


def composite_severity(base: float, budget_fraction: float, unmet_ratio: float) -> float:
    return float(base) + 2.0 * float(budget_fraction) + float(unmet_ratio)


def verify_horizon_band(
    action_count: int,
    step_limit: int | None,
    v: int,
    edit_coverage: float | None,
    test_coverage: float | None,
    has_edits: bool,
    last_test_failed: bool = False,
    confidence_tier: str | None = None,
    thresholds: HorizonThresholds | None = None,
) -> str | None:
    if step_limit is None or step_limit <= 0 or not has_edits:
        return None
    th = thresholds or HorizonThresholds()
    remaining = step_limit - action_count
    budget = action_count / step_limit
    tc = 0.0 if test_coverage is None else float(test_coverage)
    # V-3 (2026-07-10): edit_coverage None == UNKNOWN, and we default ec_pos=True
    # DELIBERATELY (fail-toward-verification). This band already requires has_edits
    # (guarded above), so "unknown edit coverage" means we know edits exist but not how
    # covered they are — firing a verify NUDGE there is correct-or-quiet (a reminder to
    # verify is never harmful; suppressing it on a missing signal could ship unverified
    # work). Only a KNOWN-zero edit_coverage (0.0) sets ec_pos False.
    ec_pos = True if edit_coverage is None else float(edit_coverage) > 0.0
    if last_test_failed and (budget >= th.urgent_budget or remaining <= th.gate_cycles * v):
        return "pivot"
    if remaining <= th.gate_cycles * v and tc < th.gate_test_coverage:
        return "gate"
    if budget >= th.urgent_budget and tc < th.urgent_test_coverage:
        return "urgent"
    tier = (confidence_tier or "").strip().lower()
    # B-4: a LOW-confidence localization advises verification SOONER than the budget-gated
    # regular advisory below (its purpose). But it must still require ec_pos — you cannot
    # advise "verify your edit" before any creditable edit exists — else, once un-deadened,
    # it would fire on nearly every early turn (noise / correct-or-quiet violation).
    if tier == "low" and tc < th.urgent_test_coverage and ec_pos:
        return "advisory"
    if budget >= th.advisory_budget and tc < th.advisory_test_coverage and ec_pos:
        return "advisory"
    return None


def render_verify_emission(
    band: str,
    action_count: int,
    step_limit: int,
    edited_rels: set[str],
    covering_tests: list | None = None,
    risk_note: str = "",
) -> str:
    import os

    remaining = step_limit - action_count
    edited_summary = ", ".join(os.path.basename(r) for r in sorted(edited_rels)[:3])
    if len(edited_rels) > 3:
        edited_summary += f" (+{len(edited_rels) - 3} more)"
    has_covering = bool(covering_tests)
    # V-1: pair the verb with the subject's NUMBER — "a … test COVERS" (singular) vs
    # "the relevant tests COVER" (plural). A fixed "covers" produced the live garble
    # "the relevant tests covers them".
    test_info = "a graph-linked covering test" if has_covering else "the relevant tests"
    test_verb = "covers" if has_covering else "cover"
    test_action = (
        "the narrowest relevant repo test target" if has_covering
        else "the relevant test suite or narrowest related target"
    )
    if band == "advisory":
        if has_covering:
            # grounded: a graph-linked covering test is known to exist
            body = (
                f"GT: you have edited {edited_summary} but no test output observed "
                f"so far references these changes. {test_info} {test_verb} them - "
                f"consider running {test_action}."
            )
        else:
            # correct-or-quiet: no covering-test evidence — advise verification
            # WITHOUT asserting that any relevant test covers the edit (the false
            # "the relevant tests cover them" for files with no linked tests).
            body = (
                f"GT: you have edited {edited_summary} but no test output observed "
                f"so far references these changes. Consider running {test_action} "
                f"to exercise them."
            )
    elif band == "urgent":
        body = (
            f"GT: ~{remaining} of {step_limit} steps remain. Your edits to {edited_summary} "
            "are still unverified - nothing you have run exercises them. "
            f"Run {test_action} now. A failing result with "
            f"~{remaining} steps left is still fixable; unverified work you finalize is not."
        )
    elif band == "gate":
        body = (
            f"GT: {remaining} steps left - at your observed pace this is your LAST "
            f"window to verify. You edited {edited_summary}; no test has "
            f"exercised them. Run {test_action} NOW. If it passes, finish. "
            "If it fails, make the single smallest fix and re-run. "
            "Do not finalize unverified work."
        )
    elif band == "pivot":
        body = (
            f"GT: the targeted verification has failed and ~{remaining} steps remain. "
            "Re-read the failing assertion once; if the fix is not one edit "
            "away, revert to your last passing state and finalize the minimal "
            "correct change."
        )
    else:
        return ""
    if risk_note:
        body = f"{body} Highest-impact unverified change: {risk_note}."
    return f'\n<gt-verify level="{band}">\n{body}\n</gt-verify>'


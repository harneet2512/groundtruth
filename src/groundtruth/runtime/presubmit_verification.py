"""Bounded deterministic verification immediately before native submission."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .verification_plan import Check, CheckResult, VerificationPlan


@dataclass(frozen=True)
class PreSubmitSummary:
    blocking_failure: dict[str, Any] | None
    syntax: dict[str, Any] | None
    covering: dict[str, Any] | None
    plan_results: dict[str, Any]
    unknowns: tuple[str, ...]
    results: tuple[CheckResult, ...]


def _skipped(check: Check, reason: str) -> Check:
    if check.kind == "unit":
        return replace(
            check,
            targets=(),
            reason=f"presubmit_policy_skipped:{reason}",
        )
    return replace(
        check,
        command=None,
        reason=f"presubmit_policy_skipped:{reason}",
    )


def restrict_presubmit_plan(plan: VerificationPlan) -> VerificationPlan:
    """Keep only bounded, non-guessing commands suitable for the submit seam."""

    restricted: list[Check] = []
    for check in plan.checks:
        if check.kind == "syntax":
            restricted.append(check)
        elif check.kind == "unit":
            restricted.append(
                check
                if check.selection_basis == "fact_covering"
                else _skipped(check, "unit_selection_not_fact_attributed")
            )
        elif check.kind == "integration":
            command = tuple(check.command or ())
            safe_pytest = (
                command
                and command[0] in {"pytest", "py.test"}
                and check.selection_basis
                in {
                    "config:pyproject.pytest",
                    "config:pytest_ini",
                    "config:setup_cfg",
                }
            )
            restricted.append(
                check
                if safe_pytest
                else _skipped(check, "command_not_allowlisted")
            )
        else:
            restricted.append(_skipped(check, "check_kind_not_allowlisted"))
    return replace(plan, checks=tuple(restricted))


def summarize_presubmit_results(
    plan: VerificationPlan,
    results: tuple[CheckResult, ...] | list[CheckResult],
) -> PreSubmitSummary:
    """Name unknowns and expose only positive, attributable blocking failures."""

    normalized = tuple(results)
    blocking: dict[str, Any] | None = None
    syntax: dict[str, Any] | None = None
    covering: dict[str, Any] | None = None
    plan_results: dict[str, Any] = {}
    unknowns: list[str] = []

    for result in normalized:
        result_dict = {
            "executed": result.executed,
            "graph_revision": result.graph_revision,
            "patch_revision": result.patch_revision,
            "reason": str(result.detail.get("reason") or ""),
            "verdict": result.verdict,
        }
        if result.kind == "syntax":
            per_file = result.detail.get("per_file")
            syntax = (
                dict(per_file[0])
                if isinstance(per_file, list)
                and len(per_file) == 1
                and isinstance(per_file[0], dict)
                else result_dict
            )
            syntax["patch_revision"] = result.patch_revision
            if (
                blocking is None
                and result.executed
                and result.verdict == "syntax_error"
            ):
                blocking = {
                    "blocking": True,
                    "reason": "syntax_invalid",
                    "detail": "fresh deterministic syntax verification failed",
                    "kind": result.kind,
                    "patch_revision": result.patch_revision,
                }
        elif result.kind == "unit":
            covering = dict(result.detail)
            covering.setdefault("verdict", result.verdict)
            covering["patch_revision"] = result.patch_revision
            if (
                blocking is None
                and result.executed
                and result.verdict == "fail"
                and result.attribution_satisfied
            ):
                blocking = {
                    "blocking": True,
                    "reason": "covering_test_failed",
                    "detail": (
                        "a fresh deterministic covering test failed and was "
                        "attributed to the edited surface"
                    ),
                    "kind": result.kind,
                    "patch_revision": result.patch_revision,
                }
        elif result.kind in {"build", "type"}:
            plan_results[result.kind] = result_dict

        if (
            not result.executed
            or result.verdict
            in {
                "unknown",
                "unavailable",
                "partial",
                "executed_no_tests",
                "skipped",
            }
        ):
            unknowns.append(
                f"{result.kind}:{result.selection_basis}:{result.verdict}"
            )

    return PreSubmitSummary(
        blocking_failure=blocking,
        syntax=syntax,
        covering=covering,
        plan_results=plan_results,
        unknowns=tuple(sorted(set(unknowns))),
        results=normalized,
    )


__all__ = [
    "PreSubmitSummary",
    "restrict_presubmit_plan",
    "summarize_presubmit_results",
]

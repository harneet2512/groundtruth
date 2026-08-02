from __future__ import annotations

from groundtruth.runtime.presubmit_verification import (
    restrict_presubmit_plan,
    summarize_presubmit_results,
)
from groundtruth.runtime.verification_plan import (
    Check,
    CheckResult,
    VerificationPlan,
)


def _plan(*checks: Check) -> VerificationPlan:
    return VerificationPlan(
        patch_revision="patch-a",
        graph_revision="graph-a",
        changed_entities=("edited_symbol",),
        obligations=(),
        checks=checks,
        edited_files=("src/a.py",),
    )


def _result(
    kind: str,
    verdict: str,
    *,
    attributed: bool,
    executed: bool = True,
) -> CheckResult:
    return CheckResult(
        kind=kind,
        selection_basis="fact_covering",
        executed=executed,
        verdict=verdict,
        graph_revision="graph-a",
        patch_revision="patch-a",
        covered_entities=("edited_symbol",),
        covered_obligations=(),
        attribution_requirement=(
            "none" if kind == "syntax" else "edit_attributed"
        ),
        attribution_satisfied=attributed,
        detail={"verdict": verdict},
    )


def test_only_positive_attributed_presubmit_failure_blocks() -> None:
    plan = _plan()

    unknown = summarize_presubmit_results(
        plan,
        (_result("integration", "unavailable", attributed=False),),
    )
    unattributed = summarize_presubmit_results(
        plan,
        (_result("unit", "fail", attributed=False),),
    )
    attributed = summarize_presubmit_results(
        plan,
        (_result("unit", "fail", attributed=True),),
    )
    syntax = summarize_presubmit_results(
        plan,
        (_result("syntax", "syntax_error", attributed=True),),
    )

    assert unknown.blocking_failure is None
    assert unknown.unknowns == ("integration:fact_covering:unavailable",)
    assert unattributed.blocking_failure is None
    assert attributed.blocking_failure["reason"] == "covering_test_failed"
    assert syntax.blocking_failure["reason"] == "syntax_invalid"


def test_presubmit_plan_names_but_does_not_execute_unsafe_commands() -> None:
    plan = _plan(
        Check(
            kind="syntax",
            command=None,
            selection_basis="edit_check",
            targets=("src/a.py",),
        ),
        Check(
            kind="unit",
            command=("pytest", "tests/test_a.py"),
            selection_basis="fact_covering",
            targets=("tests/test_a.py",),
        ),
        Check(
            kind="unit",
            command=("pytest", "tests/test_guess.py"),
            selection_basis="test_dir_convention",
            targets=("tests/test_guess.py",),
        ),
        Check(
            kind="integration",
            command=("npm", "test"),
            selection_basis="config:package_json",
        ),
    )

    restricted = restrict_presubmit_plan(plan)

    assert restricted.checks[0].targets == ("src/a.py",)
    assert restricted.checks[1].targets == ("tests/test_a.py",)
    assert restricted.checks[2].targets == ()
    assert restricted.checks[2].reason.startswith(
        "presubmit_policy_skipped:"
    )
    assert restricted.checks[3].command is None
    assert restricted.checks[3].reason.startswith(
        "presubmit_policy_skipped:"
    )

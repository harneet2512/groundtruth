"""SM-3 (2026-07-11) — VerificationPlan activation at the verify boundary.

The progressive ladder (syntax -> focused covering/unit -> integration) REPLACES the
single covering lever behind ``GT_VERIFICATION_PLAN`` (default OFF byte-identical).

PINNED HERE:
  1. DISPATCH. Off -> the risk boundary calls ``_executed_covering_emission`` (the single
     lever, unchanged); On -> it calls ``_verification_plan_emission`` (the plan). No
     double covering execution.
  2. PROGRESSIVE DELIVERY. The FIRST attributed-RED rung wins (syntax before unit); an
     UNATTRIBUTED unit RED never delivers (invariant ②); build/type/integration REDs are
     HOST-side only (no model delivery this wave).
  3. SUBMIT PARITY. The unit rung verdict feeds ``_last_covering_result`` /
     ``_last_test_outcome_failed`` so the submit gate reuses it.
  4. LEAK-0. A delivered block carries no ``<gt-*>`` and no test identity.

Mutations bitten: route the wrong function (M1) -> #1 fails; deliver an unattributed unit
RED (M2) -> #2 fails; skip the ``_last_covering_result`` capture (M3) -> #3 fails.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import native_render as nr  # noqa: E402
from groundtruth.runtime import verification_plan as vp  # noqa: E402


class _Res:
    """A minimal CheckResult stand-in carrying exactly the attrs the emission reads."""
    def __init__(self, kind, verdict, detail, attribution_satisfied=False,
                 covered_entities=()):  # noqa: D401
        self.kind = kind
        self.verdict = verdict
        self.detail = detail
        self.attribution_satisfied = attribution_satisfied
        self.covered_entities = covered_entities


class _Green:
    def __init__(self, status):
        self.status = status


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    # Every emission test exercises the execution-enabled production branch.
    # Pin the prerequisite here so suite order or a prior flag-off test cannot
    # silently turn an emission assertion into a no-op.
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_action_count", 9, raising=False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    monkeypatch.setattr(g, "_last_covering_result", None, raising=False)
    monkeypatch.setattr(g, "_last_test_outcome_failed", False, raising=False)
    monkeypatch.setattr(g, "_root", lambda: "/repo")
    monkeypatch.setattr(g, "_db_path", lambda: "/repo/graph.db")
    monkeypatch.setattr(g, "_build_env_executor", lambda: None)
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: {"foo"})
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: set())
    # a REAL VerificationPlan (frozen dataclass) so the F4 deliverable-rung filter
    # (dataclasses.replace) works; run_plan/green are mocked per-test.
    monkeypatch.setattr(vp, "build_verification_plan", lambda *a, **k: vp.VerificationPlan(
        patch_revision="p", graph_revision="g", changed_entities=("foo",),
        obligations=(), checks=(), edited_files=()))


def _wire_plan(monkeypatch, results):
    monkeypatch.setattr(vp, "run_plan", lambda *a, **k: results)
    monkeypatch.setattr(vp, "green",
                        lambda res, plan: _Green("red" if res.verdict in
                                                 ("fail", "syntax_error") else "unavailable"))


# --------------------------------------------------------------------------- #
# 1. DISPATCH: flag routes the verify boundary to the right emitter.
# --------------------------------------------------------------------------- #
@pytest.fixture
def risk_boundary(monkeypatch):
    monkeypatch.setattr(g, "_horizon_advisory_fired", False, raising=False)
    monkeypatch.setattr(g, "_oracle_edited_rels", {"x.py"}, raising=False)
    monkeypatch.setattr(g, "_structural_risk_note", lambda: ("risk", True))
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda *a, **k: [])
    plan_calls, cov_calls = [], []
    monkeypatch.setattr(g, "_verification_plan_emission",
                        lambda *a, **k: plan_calls.append(1) or "PLAN BLOCK")
    monkeypatch.setattr(g, "_executed_covering_emission",
                        lambda *a, **k: cov_calls.append(1) or "COV BLOCK")
    return plan_calls, cov_calls


def test_flag_off_uses_single_lever(risk_boundary, monkeypatch):
    plan_calls, cov_calls = risk_boundary
    monkeypatch.delenv("GT_VERIFICATION_PLAN", raising=False)
    out = g._verification_horizon_candidate()
    assert out is not None and out[2] == "COV BLOCK"
    assert cov_calls == [1] and plan_calls == []


def test_flag_on_uses_verification_plan(risk_boundary, monkeypatch):
    plan_calls, cov_calls = risk_boundary
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    out = g._verification_horizon_candidate()
    assert out is not None and out[2] == "PLAN BLOCK"
    assert plan_calls == [1] and cov_calls == []


# --------------------------------------------------------------------------- #
# 2/3/4. Emission logic: progressive, attribution-gated, submit-parity, leak-0.
# --------------------------------------------------------------------------- #
def test_flag_off_emission_is_noop(monkeypatch):
    monkeypatch.delenv("GT_VERIFICATION_PLAN", raising=False)
    assert g._verification_plan_emission({"x.py"}, {"foo"}) is None


def test_execute_flag_off_emission_is_noop(monkeypatch):
    """The plan flag alone cannot execute repo commands without explicit execution."""
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    assert g._verification_plan_emission({"x.py"}, {"foo"}) is None


def test_attributed_unit_red_delivers_and_feeds_submit(monkeypatch):
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    cov_detail = {"verdict": "fail", "ran": ["t.py"]}
    _wire_plan(monkeypatch, [
        _Res("syntax", "ok", {"per_file": []}),
        _Res("unit", "fail", cov_detail, attribution_satisfied=True,
             covered_entities=("foo",)),
    ])
    rendered_symbols = []
    monkeypatch.setattr(g, "_edited_symbol_spans", lambda: set())
    monkeypatch.setattr(
        nr, "render_covering_failure_native",
        lambda *a, **k: rendered_symbols.append(k.get("edited_symbol")) or
        "a covering test is failing",
    )
    block = g._verification_plan_emission({"x.py"}, {"foo"})
    assert block == "a covering test is failing"
    assert g._last_test_outcome_failed is True
    # submit parity: the unit verdict is captured for the gate.
    assert g._last_covering_result and g._last_covering_result.get("verdict") == "fail"
    assert not nr.contains_test_identity(block)
    assert rendered_symbols == [None]


def test_unattributed_unit_red_does_not_deliver(monkeypatch):
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    _wire_plan(monkeypatch, [
        _Res("unit", "fail", {"verdict": "fail"}, attribution_satisfied=False),
    ])
    monkeypatch.setattr(nr, "render_covering_failure_native", lambda *a, **k: "SHOULD NOT SHOW")
    assert g._verification_plan_emission({"x.py"}, {"foo"}) is None
    # captured for the gate, but NOT delivered (invariant ②) and not marked failed.
    assert g._last_test_outcome_failed is False


def test_syntax_red_wins_before_unit(monkeypatch):
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    serr = {"verdict": "syntax_error", "file": "src/x.py"}
    _wire_plan(monkeypatch, [
        _Res("syntax", "syntax_error", {"per_file": [serr]}),
        _Res("unit", "fail", {"verdict": "fail"}, attribution_satisfied=True),
    ])
    monkeypatch.setattr(nr, "render_syntax_error_native", lambda r: "SyntaxError: bad token")
    monkeypatch.setattr(nr, "render_covering_failure_native", lambda *a, **k: "UNIT")
    block = g._verification_plan_emission({"x.py"}, {"foo"})
    assert block == "SyntaxError: bad token", "syntax rung precedes unit in plan order"


def test_replay_plan_syntax_uses_parse_boundary_not_pytest_boundary(
    monkeypatch, tmp_path,
):
    """A pytest-only covering executor must not make the plan's syntax rung dark."""
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    source = tmp_path / "pkg" / "mod.py"
    source.parent.mkdir(parents=True)
    source.write_text("def foo(:\n", encoding="utf-8")
    plan = vp.VerificationPlan(
        patch_revision="p", graph_revision="g", changed_entities=("foo",),
        obligations=(), edited_files=("pkg/mod.py",), checks=(
            vp.Check(
                kind="syntax", command=None, selection_basis="edit_check",
                covered_entities=("foo",), confidence="high",
                attribution_requirement="none", targets=("pkg/mod.py",),
            ),
        ),
    )
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(vp, "build_verification_plan", lambda *a, **k: plan)
    parse_calls = []
    covering_calls = []

    def parse_executor(cmd, cwd, timeout):
        parse_calls.append((cmd, cwd, timeout))
        return 1, "", (
            '  File "/testbed/pkg/mod.py", line 1\n'
            "    def foo(:\n"
            "            ^\n"
            "SyntaxError: invalid syntax\n"
        )

    def pytest_only_executor(cmd, cwd, timeout):
        covering_calls.append((cmd, cwd, timeout))
        return None, "", "replay_verification_unsupported_command"

    monkeypatch.setattr(g, "_build_edit_check_executor", lambda: parse_executor)
    monkeypatch.setattr(g, "_build_verification_executor", lambda: pytest_only_executor)

    block = g._verification_plan_emission({"pkg/mod.py"}, {"foo"})

    assert block and "SyntaxError" in block
    assert parse_calls, "syntax must use the dedicated parse executor"
    assert not covering_calls, "pytest-only executor must not receive ast.parse argv"


def test_integration_red_is_host_side_only(monkeypatch):
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    _wire_plan(monkeypatch, [
        _Res("integration", "fail", {"verdict": "fail"}, attribution_satisfied=True),
    ])
    # no model-facing renderer for integration -> None (host-side only this wave).
    assert g._verification_plan_emission({"x.py"}, {"foo"}) is None


def test_non_deliverable_rungs_are_filtered_before_execution(monkeypatch):
    """F4 (SM-3 LIPI): the whole-suite integration rung is DROPPED before run_plan, so its
    ≤120s discarded compute is never paid. The plan passed to run_plan carries only the
    deliverable rungs (syntax + unit)."""
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    from groundtruth.runtime.verification_plan import Check, VerificationPlan

    full = VerificationPlan(
        patch_revision="p", graph_revision="g", changed_entities=("foo",), obligations=(),
        edited_files=(), checks=(
            Check(kind="syntax", command=None, selection_basis="edit_check"),
            Check(kind="unit", command=None, selection_basis="fact_covering"),
            Check(kind="integration", command=("pytest",), selection_basis="suite"),
        ))
    monkeypatch.setattr(vp, "build_verification_plan", lambda *a, **k: full)
    seen = {}
    monkeypatch.setattr(vp, "run_plan",
                        lambda plan, *a, **k: seen.setdefault("kinds",
                                                               [c.kind for c in plan.checks]) or [])
    monkeypatch.setattr(vp, "green", lambda res, plan: _Green("unavailable"))
    g._verification_plan_emission({"x.py"}, {"foo"})
    assert seen["kinds"] == ["syntax", "unit"], "integration rung must be filtered out"

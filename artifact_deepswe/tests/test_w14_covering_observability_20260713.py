r"""W14 (2026-07-13) — verify.horizon.executed OBSERVABILITY + non-completing-run LATCH.

Two S-sized fixes from a forensic diagnosis of the executed-covering channel. Both are
HOST-side only (ZERO model-facing observation bytes change) and flag-gated byte-identical
when GT_VERIFY_EXECUTE is off.

  FIX 1 — instrument the SUPPRESSED None-branches. ``_executed_covering_emission`` and
    ``_verification_plan_emission`` return None SILENTLY when the covering run yields
    green / unavailable / timeout / unattributable / no-covering / nothing-rendered, so an
    executed-count delta was undiagnosable. Each None-branch now writes ONE host-side ledger
    row (kind=verify.horizon.executed, a SUPPRESSED-class outcome, reason=covering_<verdict>,
    chars=0), the verdict pulled from what the branch actually knows — never fabricated.

  FIX 2 — do NOT latch a NON-COMPLETING run. The per-symbol ``_covering_exec_fired_syms``
    latch was burned BEFORE emission was confirmed (independent producer + risk path), so an
    unavailable/timeout covering run (heavy repos, 35s budget) burned the latch and the
    executed channel went DARK until a re-edit. The latch burn is now gated on a COMPLETING
    verdict (NOT in unavailable/timeout/error): a green / no-covering-file / attributed-fail
    latches (nothing to re-attempt); a non-completing run does NOT (re-attempts next fire
    point). Intra-turn double-execution is prevented by the per-turn ``_covering_exec_pending``
    record, so the gate-loss re-arm semantics in ``_rearm_latches`` are untouched.

RED-first: every FIX test FAILS on the pre-fix code (confirmed by running before the patch).
Windows: run with PYTHONIOENCODING=utf-8. Scope: gt_mini_patch.py only.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    g._reset_oracle_state()
    g._GT_LIVE_ENV = None
    g._GT_LIVE_ORIG_EXECUTE = None
    yield
    g._reset_oracle_state()
    g._GT_LIVE_ENV = None
    g._GT_LIVE_ORIG_EXECUTE = None


def _suppressed(outcome) -> bool:
    return str(getattr(outcome, "value", outcome)).startswith("suppressed")


# ============================================================================ #
# FIX 1 — the SUPPRESSED None-branches write a host-side ledger row
# ============================================================================ #
def _emission_env(monkeypatch, tmp_path):
    """Pass every _executed_covering_emission early-return guard; return a spy row list."""
    (tmp_path / "test_mod.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.delenv("GT_VERIFICATION_PLAN", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    rows: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: rows.append(kw))
    return rows


def test_red1_timeout_writes_suppressed_ledger_row(monkeypatch, tmp_path):
    """RED-1. A covering run that verdicts ``timeout`` (non-fail) returns None. Pre-fix it wrote
    NO ledger row; post-fix it writes the suppressed row with reason=covering_timeout."""
    import groundtruth.runtime.covering_runner as cr
    rows = _emission_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cr, "run_covering_tests",
                        lambda root, files, **kw: {"verdict": "timeout", "ran": [],
                                                   "reason": "budget_exhausted"})
    out = g._executed_covering_emission([{"file": "test_mod.py", "confidence": 0.9}],
                                        {"mod.py"}, {"foo"})
    assert out is None
    hit = [r for r in rows if r.get("kind") == "verify.horizon.executed"
           and r.get("reason") == "covering_timeout"]
    assert hit, ("the timeout None-branch must write a suppressed row reason=covering_timeout; "
                 "got %r" % rows)
    assert hit[0].get("chars") == 0
    assert _suppressed(hit[0].get("outcome")), hit[0]


def test_fix1_green_verdict_pulls_the_real_verdict(monkeypatch, tmp_path):
    """A completing but non-fail (green -> here 'pass') run records the ACTUAL verdict."""
    import groundtruth.runtime.covering_runner as cr
    rows = _emission_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cr, "run_covering_tests",
                        lambda root, files, **kw: {"verdict": "pass", "ran": files})
    assert g._executed_covering_emission([{"file": "test_mod.py"}], {"mod.py"}, {"foo"}) is None
    assert any(r.get("reason") == "covering_pass" for r in rows), rows


def test_fix1_no_covering_file_records_no_covering(monkeypatch, tmp_path):
    """The no-covering-file branch (a covering entry whose file is absent on disk) records
    reason=covering_no_covering — the branch's own condition, not a fabricated verdict."""
    rows = _emission_env(monkeypatch, tmp_path)
    out = g._executed_covering_emission([{"file": "does_not_exist_test.py"}], {"mod.py"}, {"foo"})
    assert out is None
    assert any(r.get("reason") == "covering_no_covering" for r in rows), rows


def test_none_produced_pin_on_empty_render(monkeypatch, tmp_path):
    """THE none_produced PIN (mutation (b) target). An attributed RED whose native renderer
    yields NOTHING records reason=covering_none_produced — it must NOT fabricate the raw 'fail'
    verdict, because nothing reached the model."""
    import groundtruth.runtime.covering_runner as cr
    import groundtruth.runtime.native_render as nr
    rows = _emission_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cr, "run_covering_tests",
                        lambda root, files, **kw: {"verdict": "fail", "ran": files})
    monkeypatch.setattr(
        cr, "attribute_covering_red", lambda *a, **k: cr.CoveringAttribution(
            attributed=True, method="differential", current_verdict="fail",
            base_verdict="pass", implicated_edited_paths=("mod.py",),
            covering_files=("test_mod.py",)))
    monkeypatch.setattr(nr, "render_covering_failure_native", lambda *a, **k: "")  # empty render
    out = g._executed_covering_emission([{"file": "test_mod.py"}], {"mod.py"}, {"foo"})
    assert out is None
    hit = [r for r in rows if r.get("kind") == "verify.horizon.executed"]
    assert hit and hit[0].get("reason") == "covering_none_produced", (
        "empty render must record none_produced, never the fabricated raw verdict; got %r" % rows)


def test_fix1_plan_none_produced(monkeypatch, tmp_path):
    """The VerificationPlan path records plan_none_produced when it reaches its end with no
    deliverable RED rung."""
    import groundtruth.runtime.verification_plan as vp
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "graph.db"))
    monkeypatch.setattr(g, "_build_env_executor", lambda: None)
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: set())
    monkeypatch.setattr(vp, "build_verification_plan", lambda *a, **k: vp.VerificationPlan(
        patch_revision="p", graph_revision="gg", changed_entities=("foo",),
        obligations=(), checks=(), edited_files=()))
    monkeypatch.setattr(vp, "run_plan", lambda *a, **k: [])  # no rung reddens
    rows: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: rows.append(kw))
    assert g._verification_plan_emission({"x.py"}, {"foo"}) is None
    assert any(r.get("reason") == "plan_none_produced"
               and r.get("kind") == "verify.horizon.executed" for r in rows), rows


# ============================================================================ #
# FIX 2 — the independent producer does NOT latch a non-completing run
# ============================================================================ #
def _prep_candidate(monkeypatch, *, emit, edited_syms=("foo",), covering=({"file": "t.py"},)):
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.delenv("GT_VERIFICATION_PLAN", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("mod.py")
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: set(edited_syms))
    monkeypatch.setattr(g, "_covering_tests_for_symbols",
                        lambda syms: list(covering) if syms else [])
    monkeypatch.setattr(g, "_executed_covering_emission", emit)


def test_red2_timeout_does_not_latch_and_reattempts(monkeypatch):
    """RED-2. A timeout run must NOT burn the per-symbol latch, so a SECOND call re-attempts
    (the emission mock is called twice). Pre-fix: the latch is burned before emission -> the
    symbol is latched -> the second call is dark (mock called once)."""
    calls = {"n": 0}

    def _emit(cov, rels, syms):
        calls["n"] += 1
        g._last_covering_result = {"verdict": "timeout"}
        return None  # non-fail -> no block

    _prep_candidate(monkeypatch, emit=_emit)
    assert g._executed_covering_candidate() is None
    assert "foo" not in g._covering_exec_fired_syms, "a timeout must NOT burn the per-symbol latch"
    assert g._executed_covering_candidate() is None
    assert calls["n"] == 2, "a non-completing run must re-attempt at the next fire point"


def test_red2_green_verdict_latches(monkeypatch):
    """The correct-latch pin: a COMPLETING green run DOES latch, so a second call does not
    re-run (mock called once)."""
    calls = {"n": 0}

    def _emit(cov, rels, syms):
        calls["n"] += 1
        g._last_covering_result = {"verdict": "pass"}
        return None

    _prep_candidate(monkeypatch, emit=_emit)
    assert g._executed_covering_candidate() is None
    assert "foo" in g._covering_exec_fired_syms, "a completing green run MUST latch"
    assert g._executed_covering_candidate() is None
    assert calls["n"] == 1, "a latched symbol must not re-run"


def test_fix2_attributed_fail_latches_and_delivers(monkeypatch):
    """A completing attributed FAIL latches AND delivers (the flagship RED path)."""
    def _emit(cov, rels, syms):
        g._last_covering_result = {"verdict": "fail"}
        return "A covering test fails:\n  at mod.py:2"

    _prep_candidate(monkeypatch, emit=_emit)
    cand = g._executed_covering_candidate()
    assert cand is not None and cand[1] == "verify.horizon.executed"
    assert "foo" in g._covering_exec_fired_syms
    assert g._covering_exec_pending["syms"] == {"foo"}  # re-arm record populated (untouched)


def test_fix2_no_covering_file_still_latches(monkeypatch):
    """No covering FILE (empty selection) -> nothing to re-attempt -> still latches (the
    behavior the existing SM-10 pin depends on)."""
    _prep_candidate(monkeypatch, emit=lambda *a, **k: "SHOULD NOT RUN", covering=())
    assert g._executed_covering_candidate() is None
    assert "foo" in g._covering_exec_fired_syms


# ============================================================================ #
# FIX 2 — the RISK path defers the latch on a non-completing run but still
#         de-dups intra-turn (no double execution)
# ============================================================================ #
def _prep_risk(monkeypatch, *, emit):
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.delenv("GT_VERIFICATION_PLAN", raising=False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_action_count", 9, raising=False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    monkeypatch.setattr(g, "_horizon_advisory_fired", False, raising=False)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("x.py")
    monkeypatch.setattr(g, "_structural_risk_note", lambda: ("risk", True))
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: {"foo"})
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda *a, **k: [{"file": "t.py"}])
    monkeypatch.setattr(g, "_obligation_symbol_set", lambda: set())
    monkeypatch.setattr(g, "_executed_covering_emission", emit)
    monkeypatch.setattr(g, "_render_verify_emission", lambda *a, **k: "ADVISORY")


def test_fix2_risk_timeout_defers_latch_but_dedups_intraturn(monkeypatch):
    """A RISK-path covering timeout must (a) NOT burn the cross-turn latch (so the independent
    producer re-attempts next turn) but (b) still record the symbol in the per-turn
    `_covering_exec_pending` set (so the independent producer does not double-run THIS turn)."""
    def _emit(cov, rels, syms):
        g._last_covering_result = {"verdict": "timeout"}
        return None

    _prep_risk(monkeypatch, emit=_emit)
    out = g._verification_horizon_candidate()
    assert out is not None and out[1] == "verify.horizon.advisory"
    assert "foo" not in g._covering_exec_fired_syms, "risk timeout must NOT burn the cross-turn latch"
    assert "foo" in g._covering_exec_pending["syms"], "intra-turn de-dup record must still hold foo"


def test_fix2_risk_green_latches(monkeypatch):
    """A RISK-path COMPLETING green run latches (nothing to re-attempt)."""
    def _emit(cov, rels, syms):
        g._last_covering_result = {"verdict": "pass"}
        return None

    _prep_risk(monkeypatch, emit=_emit)
    g._verification_horizon_candidate()
    assert "foo" in g._covering_exec_fired_syms, "a completing green risk run MUST latch"


# ============================================================================ #
# BYTE-IDENTITY — the model-facing observation bytes (the returned block) are
# unchanged on delivery, and every SUPPRESSED branch returns None (no append).
# ============================================================================ #
def test_byte_identity_delivery_and_suppression(monkeypatch, tmp_path):
    import groundtruth.runtime.covering_runner as cr
    import groundtruth.runtime.native_render as nr
    _emission_env(monkeypatch, tmp_path)  # spies _runtime_ledger_record too (host rows only)
    EXPECT = "\n$ pytest\nTypeError\n[exit 1]\n"
    monkeypatch.setattr(nr, "render_covering_failure_native", lambda *a, **k: EXPECT)
    monkeypatch.setattr(
        cr, "attribute_covering_red", lambda *a, **k: cr.CoveringAttribution(
            attributed=True, method="differential", current_verdict="fail",
            base_verdict="pass", implicated_edited_paths=("mod.py",),
            covering_files=("test_mod.py",)))
    monkeypatch.setattr(cr, "run_covering_tests",
                        lambda root, files, **kw: {"verdict": "fail", "ran": files,
                                                   "exit_code": 1, "stdout_tail": "TypeError",
                                                   "command": ["pytest"]})
    cov = [{"file": "test_mod.py", "confidence": 0.9}]
    block = g._executed_covering_emission(cov, {"mod.py"}, {"foo"})
    assert block == EXPECT, "delivery bytes must be byte-identical (FIX 1 is host-side only)"
    # every suppressed branch: None (no observation append at all)
    monkeypatch.setattr(cr, "run_covering_tests",
                        lambda root, files, **kw: {"verdict": "unavailable"})
    assert g._executed_covering_emission(cov, {"mod.py"}, {"foo"}) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

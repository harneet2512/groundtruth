r"""ITEM 2 (2026-07-12) — the verification-plan divergence seam (named in the Item-C LIPI).

Under ``GT_VERIFICATION_PLAN=1`` the RISK path (``_verification_horizon_candidate``) routes its
covering execution through the PROGRESSIVE plan (``build_verification_plan`` -> ``run_plan``:
syntax -> unit -> …). But the INDEPENDENT executed-covering producer
(``_executed_covering_candidate``) always used the SINGLE covering lever
(``_executed_covering_emission``) — so the two verify producers DIVERGED: one ran the ladder,
the other did not.

FIX: the independent producer uses the SAME plan machinery when ``GT_VERIFICATION_PLAN`` is on;
the single lever otherwise. Byte-identical when the plan flag is off.

PINNED HERE (hermetic — the plan / single-lever emitters are spied, no graph/runner):
  * plan ON  -> ``_verification_plan_emission`` is invoked (the ladder runs), NOT the single lever;
  * plan OFF -> the single lever runs, the plan is NOT invoked (byte-identical);
  * MUTATION (revert to single-lever-always) -> the plan is never invoked under the flag -> reddens.

Windows: run with PYTHONIOENCODING=utf-8.
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
    yield
    g._reset_oracle_state()


def _prep(monkeypatch, *, edited_syms=("foo",), covering=None):
    """Reach the covering branch of ``_executed_covering_candidate`` deterministically."""
    covering = covering if covering is not None else [{"file": "test_mod.py", "confidence": 0.9}]
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    monkeypatch.setattr(g, "_last_test_step", None, raising=False)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("mod.py")
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: set(edited_syms))
    monkeypatch.setattr(g, "_covering_tests_for_symbols",
                        lambda syms: list(covering) if syms else [])
    calls = {"plan": [], "single": []}
    monkeypatch.setattr(g, "_verification_plan_emission",
                        lambda rels, syms: calls["plan"].append((set(rels), set(syms))) or "PLAN_BLOCK")
    monkeypatch.setattr(g, "_executed_covering_emission",
                        lambda cov, rels, syms: calls["single"].append(set(syms)) or "SINGLE_BLOCK")
    return calls


def test_plan_on_routes_independent_producer_through_the_plan(monkeypatch):
    calls = _prep(monkeypatch)
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")
    cand = g._executed_covering_candidate()
    assert cand is not None and cand[1] == "verify.horizon.executed"
    assert cand[2] == "PLAN_BLOCK", "the delivered block must come from the PLAN machinery"
    assert calls["plan"] == [({"mod.py"}, {"foo"})], "the plan runner must be invoked"
    assert calls["single"] == [], "the single covering lever must NOT run under the plan flag"


def test_plan_off_uses_single_lever_byte_identical(monkeypatch):
    calls = _prep(monkeypatch)
    monkeypatch.delenv("GT_VERIFICATION_PLAN", raising=False)
    cand = g._executed_covering_candidate()
    assert cand is not None and cand[2] == "SINGLE_BLOCK"
    assert calls["single"] == [{"foo"}], "off-path must use the single covering lever (byte-identical)"
    assert calls["plan"] == [], "the plan must NOT be invoked when the flag is off"


def test_mutation_single_lever_always_never_reaches_the_plan(monkeypatch):
    """MUTATION — revert the producer to the single lever ALWAYS (ignore the plan flag): under
    ``GT_VERIFICATION_PLAN=1`` the plan is never invoked -> reddens the plan-routing pin."""
    calls = _prep(monkeypatch)
    monkeypatch.setenv("GT_VERIFICATION_PLAN", "1")

    # the mutated producer: single lever regardless of the flag (the pre-fix divergence).
    def _mutated():
        if os.environ.get("GT_VERIFY_EXECUTE") != "1" or g._GT_BASELINE:
            return None
        fresh = {s for s in g._edited_symbols_for_selection()
                 if s and s not in g._covering_exec_fired_syms}
        cov = g._covering_tests_for_symbols(fresh)
        g._covering_exec_fired_syms |= fresh
        if not cov:
            return None
        block = g._executed_covering_emission(cov, g._oracle_edited_rels, fresh)  # single-lever-always
        return block

    assert _mutated() == "SINGLE_BLOCK"
    assert calls["plan"] == [], "the mutation must never reach the plan (this is the reddening state)"
    assert calls["single"] == [{"foo"}]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

"""Wave-2 Super-Mode Gateway changes (2026-07-11).

Three pins with proven-biting mutations:
  (1) SM-0 hardening tripwire — `_FINE_EVENT_TO_COARSE_ORDINAL` MUST cover exactly
      `fact_registry.EVENTS`, so a future EVENT can never silently route to step0.
  (2) cochange RETIRED-TO-INTERNAL — the Gateway must NOT deliver `cochange_partner`
      as a model-facing narration envelope (SM-1 pinned cochange with no native form).
  (3) the CONSUMABLE reactive facts (`signature_mismatch`, `companion_surface`) — the
      migration targets for l3.contract / l3b.evidence — survive GT_REGISTRY_ENFORCE.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime import gateway as gw


# =========================================================================== #
# (1) event-map / registry parity tripwire
# =========================================================================== #
def test_event_map_covers_exactly_registry_events():
    assert set(gw._FINE_EVENT_TO_COARSE_ORDINAL) == set(fr.EVENTS)


def test_event_map_tripwire_bites_a_missing_event(monkeypatch):
    """MUTATION: drop one EVENT from the coarse-ordinal map. `_self_check_event_map`
    (the import-time tripwire) must RAISE — a registry EVENT with no mapping would
    otherwise `.get(ev, 0)` silently to step0 and never DEFER/EXPIRE under enforcement."""
    saved = dict(gw._FINE_EVENT_TO_COARSE_ORDINAL)
    try:
        gw._FINE_EVENT_TO_COARSE_ORDINAL.pop(next(iter(gw._FINE_EVENT_TO_COARSE_ORDINAL)))
        with pytest.raises(ValueError):
            gw._self_check_event_map()
    finally:
        gw._FINE_EVENT_TO_COARSE_ORDINAL.clear()
        gw._FINE_EVENT_TO_COARSE_ORDINAL.update(saved)
    # restored -> the real invariant holds again
    gw._self_check_event_map()


def test_event_map_tripwire_bites_an_extra_event(monkeypatch):
    """MUTATION: add a bogus event -> parity BITES (extra key with no registry event)."""
    saved = dict(gw._FINE_EVENT_TO_COARSE_ORDINAL)
    try:
        gw._FINE_EVENT_TO_COARSE_ORDINAL["not_a_real_event"] = 0
        with pytest.raises(ValueError):
            gw._self_check_event_map()
    finally:
        gw._FINE_EVENT_TO_COARSE_ORDINAL.clear()
        gw._FINE_EVENT_TO_COARSE_ORDINAL.update(saved)


# =========================================================================== #
# (2) cochange RETIRED-TO-INTERNAL — never a delivered Gateway fact
# =========================================================================== #
def _cochange_only_result():
    return SimpleNamespace(
        signature_mismatches=[],
        companion_surfaces=[],
        cochange_partners=[SimpleNamespace(file="a/x.py", count=3)],
    )


def test_patch_delta_does_not_deliver_cochange(monkeypatch):
    monkeypatch.setattr(gw, "analyze_patch_delta", lambda *a, **k: _cochange_only_result())
    ev = gw.ToolEvent(kind="edit", command="sed -i s/a/b/ a/x.py",
                      edit_before_after={"a/x.py": (None, "after")})
    st = gw.GatewayState(graph_db=None, repo_root="")
    out = gw._produce_patch_delta(ev, st)
    assert out == [], "cochange is an internal ranking prior, never a delivered fact"
    assert not any(e.evidence_type == "cochange_partner" for e in out)


def test_augment_edit_with_only_cochange_delivers_nothing(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setattr(gw, "analyze_patch_delta", lambda *a, **k: _cochange_only_result())
    ev = gw.ToolEvent(kind="edit", command="sed -i s/a/b/ a/x.py", output="",
                      edit_before_after={"a/x.py": (None, "after")}, action_index=1)
    st = gw.GatewayState(graph_db=None, repo_root="")
    assert gw.augment(ev, st) == []


def test_mutation_restored_cochange_delivery_bites(monkeypatch):
    """MUTATION: reproduce the OLD producer that shipped cochange_partner as narration.
    The pin above (`test_patch_delta_does_not_deliver_cochange` -> []) BITES it: the
    mutant emits one `cochange_partner` narration envelope."""
    def _mutant(event, state):
        res = _cochange_only_result()
        out = []
        for cp in res.cochange_partners:                      # the removed loop
            out.append(gw._mk_add(
                state, event, fact_kind="cochange_partner", target=cp.file,
                body_lines=[f"co-changed with the edit in {cp.count} commits"],
                evidence=[], tier=gw.INFO, producer="patch_delta", symbol=cp.file))
        return out

    ev = gw.ToolEvent(kind="edit", edit_before_after={"a/x.py": (None, "after")})
    st = gw.GatewayState(graph_db=None, repo_root="")
    mutant_out = _mutant(ev, st)
    assert len(mutant_out) == 1 and mutant_out[0].evidence_type == "cochange_partner"


def test_mutation_restored_cochange_surfaces_through_augment_bites(monkeypatch):
    """MUTATION (augment level): reinstate the cochange producer -> the augment pin
    (`test_augment_edit_with_only_cochange_delivers_nothing` -> []) BITES it: a
    cochange narration envelope surfaces through the full augment path."""
    monkeypatch.setenv("GT_GATEWAY", "1")

    def _mutant(event, state):
        return [gw._mk_add(state, event, fact_kind="cochange_partner", target="a/x.py",
                           body_lines=["co-changed with the edit in 3 commits"],
                           evidence=[], tier=gw.INFO, producer="patch_delta", symbol="a/x.py")]

    monkeypatch.setattr(gw, "_produce_patch_delta", _mutant)
    ev = gw.ToolEvent(kind="edit", command="sed -i s/a/b/ a/x.py", output="",
                      edit_before_after={"a/x.py": (None, "after")}, action_index=1)
    st = gw.GatewayState(graph_db=None, repo_root="")
    out = gw.augment(ev, st)
    assert any(e.evidence_type == "cochange_partner" for e in out)  # mutant delivers it


# =========================================================================== #
# (3) the CONSUMABLE reactive migration targets survive GT_REGISTRY_ENFORCE
# =========================================================================== #
def _sig_mismatch_result():
    sm = SimpleNamespace(
        caller="bar", caller_file="pkg/mod.py", caller_line=12, symbol="foo",
        positional_args=2, new_min_params=1, new_max_params=1,
        call_site_text="foo(a, b)", confidence=0.8)
    return SimpleNamespace(
        signature_mismatches=[sm], companion_surfaces=[], cochange_partners=[])


def test_signature_mismatch_survives_registry_enforce(monkeypatch):
    """l3.contract's CONSUMABLE reactive break migrates to `signature_mismatch`; it MUST
    pass SM-0's enforcement (fires at its declared `edit_result` boundary; class has a
    registered `diff-native` renderer)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    monkeypatch.setattr(gw, "analyze_patch_delta", lambda *a, **k: _sig_mismatch_result())
    ev = gw.ToolEvent(kind="edit", command="sed -i s/a/b/ pkg/mod.py", output="",
                      edit_before_after={"pkg/mod.py": (None, "after")}, action_index=1)
    st = gw.GatewayState(graph_db=None, repo_root="")
    out = gw.augment(ev, st)
    assert any(e.evidence_type == "signature_mismatch" for e in out), out
    # registry boundary conformance: the class resolves + has a declared renderer
    assert fr.required_renderer("signature_mismatch") == "diff-native"
    assert fr.required_event("signature_mismatch") == "edit_result"

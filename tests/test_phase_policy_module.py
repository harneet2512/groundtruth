"""P0-11 — phase_policy module is the canonical allowlist contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_POLICY_PATH = Path(__file__).resolve().parents[1] / "artifact_deepswe" / "phase_policy.py"
_spec = importlib.util.spec_from_file_location("phase_policy_testmod", _POLICY_PATH)
assert _spec and _spec.loader
_pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pp)
PHASE_POLICY = _pp.PHASE_POLICY
Phase = _pp.Phase
phase_allows = _pp.phase_allows


def test_orient_allows_brief_only():
    assert phase_allows("brief", Phase.ORIENT)
    assert phase_allows("orientation", Phase.ORIENT)
    assert not phase_allows("l3b.evidence", Phase.ORIENT)


def test_view_allows_local_evidence_only():
    assert phase_allows("l3b.evidence", Phase.VIEW)
    assert not phase_allows("spec.obligation", Phase.VIEW)


def test_edit_allows_contract_pillars():
    assert phase_allows("l3.contract", Phase.EDIT)
    assert phase_allows("spec.obligation", Phase.EDIT)
    assert not phase_allows("consensus.scope", Phase.EDIT)


def test_horizon_prefix_matches_verify_family():
    assert phase_allows("verify.horizon.advisory", Phase.VERIFY)
    assert phase_allows("verify.horizon.gate", Phase.SUBMIT)


def test_policy_table_matches_patch_import():
    import importlib.util
    import os
    import sys
    from pathlib import Path

    patch_path = Path(__file__).resolve().parents[1] / "artifact_deepswe" / "gt_mini_patch.py"
    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("gt_patch_policy_sync", patch_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt_patch_policy_sync"] = mod
        spec.loader.exec_module(mod)
        # Enum identity differs across importlib loads; compare by phase value.
        patch_by_name = {p.value: frozenset(v) for p, v in mod._PHASE_POLICY.items()}
        canon_by_name = {p.value: frozenset(v) for p, v in PHASE_POLICY.items()}
        assert patch_by_name == canon_by_name
        assert {p.value for p in mod.Phase} == {p.value for p in Phase}
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev

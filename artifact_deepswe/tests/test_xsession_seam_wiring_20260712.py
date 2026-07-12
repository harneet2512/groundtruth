r"""SM-9c (2026-07-11) — the CROSS-SESSION memory wiring on the REAL mini seam.

The durable per-repo causal-consumption store is READ once at task-start into
``_EPISODE.xsession_policy`` (the explicit delivery-policy input the Gateway consults) and
the receipt ladder is WRITTEN back per gateway turn. These pins prove, on the REAL seam
(``gt_mini_patch``):

  * WRITE — ``_xsession_flush`` folds the sealed deliveries' receipt ladder (per canonical
    fact-class delivered/consumed) into a deterministic, byte-stable per-repo store file;
  * WRITE is IDEMPOTENT from the frozen task-start base (repeated flushes never double-count);
  * READ — ``_xsession_ensure_loaded`` reads the store back into ``_EPISODE.xsession_policy``
    (a full write->read round-trip agrees on the repo key);
  * RESET — ``_reset_oracle_state`` / ``reset_attempt`` PRESERVE the loaded store (a per-run
    input) while CLEARING the per-attempt delivery list;
  * FLAG-OFF — GT_XSESSION_MEMORY unset is byte-identical (nothing loaded, nothing written).

Hermetic: a synthetic repo dir + store dir under tmp_path; no ONNX, no Docker, no task IDs.
Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import dataclasses
import glob
import os

import pytest

import gt_mini_patch as g
from groundtruth.runtime.evidence_envelope import (
    RECEIPT_ACTED,
    RECEIPT_DELIVERED,
    WARNING,
    EvidenceEnvelope,
)
from groundtruth.runtime.xsession_memory import load as _xm_load
from groundtruth.runtime.xsession_memory import repo_key as _xm_repo_key
from groundtruth.runtime.xsession_memory import store_path_for_key as _xm_store_path


def _reset_sm9c() -> None:
    """Clear the SM-9c load latch + base + policy + delivery list. Needed because
    ``_reset_oracle_state`` DELIBERATELY preserves the cross-session store across attempts, so
    a test must reset it explicitly for isolation (a real run gets a fresh process per task)."""
    g._gt_xsession_base = None
    g._gt_xsession_loaded = False
    g._EPISODE.xsession_policy = {}
    g._gt_gateway_deliveries = []


@pytest.fixture
def seam(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(g, "_root", lambda: str(repo))
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    monkeypatch.setenv("GT_XSESSION_DIR", str(store))
    _reset_sm9c()
    yield repo, store
    _reset_sm9c()


def _delivery(evidence_type: str, receipt_state: str) -> EvidenceEnvelope:
    e = EvidenceEnvelope.build(
        producer="caller_contract", fact_id="get_user", target="svc/users.py",
        evidence_type=evidence_type, payload=("body",),
        provenance=(("app/main.py", 3),), confidence=0.5, tier=WARNING)
    return dataclasses.replace(e, receipt_state=receipt_state, rendered_bytes_hash="0" * 64)


def _store_files(store) -> list[str]:
    return glob.glob(os.path.join(str(store), "xsession_*.json"))


# --------------------------------------------------------------------------- #
# WRITE — the receipt ladder is folded into a deterministic per-repo store
# --------------------------------------------------------------------------- #
def test_flush_writes_deterministic_store(seam):
    repo, store = seam
    # two caller_break deliveries: one ACTED (consumed), one only DELIVERED (inert)
    g._gt_gateway_deliveries = [
        _delivery("caller_break", RECEIPT_ACTED),
        _delivery("caller_break", RECEIPT_DELIVERED),
    ]
    g._xsession_flush()
    files = _store_files(store)
    assert len(files) == 1, "one per-repo store file written"
    b1 = open(files[0], "rb").read()
    g._xsession_flush()                       # idempotent from the frozen base
    assert open(files[0], "rb").read() == b1  # byte-identical rewrite

    loaded = _xm_load(files[0])
    # canonical class = caller_contract; delivered=2, consumed=1 (only the ACTED one)
    assert dict(loaded.class_stats) == {"caller_contract": (2, 1)}


def test_flush_is_idempotent_no_double_count(seam):
    repo, store = seam
    g._gt_gateway_deliveries = [_delivery("caller_break", RECEIPT_ACTED)]
    for _ in range(3):
        g._xsession_flush()                   # flushing every turn must NOT accumulate
    files = _store_files(store)
    assert dict(_xm_load(files[0]).class_stats) == {"caller_contract": (1, 1)}


# --------------------------------------------------------------------------- #
# READ — a full write->read round-trip populates the policy input
# --------------------------------------------------------------------------- #
def test_write_then_load_round_trips_into_episode(seam):
    repo, store = seam
    g._gt_gateway_deliveries = [
        _delivery("caller_break", RECEIPT_ACTED),
        _delivery("covering_verdict", RECEIPT_DELIVERED),
    ]
    g._xsession_flush()
    # simulate the next session: fresh load latch, empty policy, empty delivery list
    _reset_sm9c()
    assert g._EPISODE.xsession_policy == {}
    g._xsession_ensure_loaded()
    # the store is read back into the EXPLICIT policy input the Gateway consults
    assert g._EPISODE.xsession_policy == {"caller_contract": (1, 1), "covering_red": (1, 0)}
    # idempotent: a second ensure_loaded (latch set) does not reload/mutate
    g._xsession_ensure_loaded()
    assert g._EPISODE.xsession_policy == {"caller_contract": (1, 1), "covering_red": (1, 0)}


def test_load_uses_the_repo_identity_key(seam):
    repo, store = seam
    g._gt_gateway_deliveries = [_delivery("caller_break", RECEIPT_ACTED)]
    g._xsession_flush()
    # the file is named by the repo-identity key (project_memory._repo_identity)
    from pathlib import Path
    from groundtruth.runtime.project_memory import _repo_identity
    key = _xm_repo_key(_repo_identity(Path(str(repo))))
    assert os.path.isfile(_xm_store_path(key, str(store)))


# --------------------------------------------------------------------------- #
# RESET — the loaded store is PRESERVED across an attempt; deliveries cleared
# --------------------------------------------------------------------------- #
def test_reset_oracle_state_preserves_loaded_store(seam):
    repo, store = seam
    g._gt_gateway_deliveries = [_delivery("caller_break", RECEIPT_ACTED)]
    g._xsession_flush()
    _reset_sm9c()
    g._xsession_ensure_loaded()
    policy_before = dict(g._EPISODE.xsession_policy)
    assert policy_before == {"caller_contract": (1, 1)}
    base_before = g._gt_xsession_base

    g._reset_oracle_state()                   # attempt boundary

    # the per-run store INPUT survives (parity with episode_id / delivery_ledger)...
    assert g._EPISODE.xsession_policy == policy_before
    assert g._gt_xsession_base is base_before
    assert g._gt_xsession_loaded is True
    # ...while the per-attempt delivery list is cleared (a retry re-sees facts fresh)
    assert g._gt_gateway_deliveries == []


# --------------------------------------------------------------------------- #
# FLAG-OFF — byte-identical: nothing loaded, nothing written
# --------------------------------------------------------------------------- #
def test_flag_off_is_a_no_op(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(g, "_root", lambda: str(repo))
    monkeypatch.delenv("GT_XSESSION_MEMORY", raising=False)
    monkeypatch.setenv("GT_XSESSION_DIR", str(store))
    _reset_sm9c()
    try:
        g._gt_gateway_deliveries = [_delivery("caller_break", RECEIPT_ACTED)]
        g._xsession_ensure_loaded()
        g._xsession_flush()
        assert g._EPISODE.xsession_policy == {}          # nothing loaded
        assert _store_files(store) == []                 # nothing written
    finally:
        _reset_sm9c()


def test_no_store_dir_is_a_no_op(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(g, "_root", lambda: str(repo))
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    monkeypatch.delenv("GT_XSESSION_DIR", raising=False)
    _reset_sm9c()
    try:
        g._gt_gateway_deliveries = [_delivery("caller_break", RECEIPT_ACTED)]
        g._xsession_ensure_loaded()
        g._xsession_flush()                              # no $GT_XSESSION_DIR -> no-op
        assert g._EPISODE.xsession_policy == {}
    finally:
        _reset_sm9c()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

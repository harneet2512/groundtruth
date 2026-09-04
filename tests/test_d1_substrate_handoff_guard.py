"""D1 (Priority C) — gt_agent EARLY fail-closed startup guard.

In proof/benchmark mode the substrate handoff (authoritative graph + certs +
brief) MUST be wired before any agent work. ``_assert_substrate_handoff``:
  * aborts if GT_PROOF_MODE=1 and _substrate_active() is false (no handoff env);
  * aborts if GT_PROOF_MODE=1 but a required handoff env var is unset;
  * passes when the full handoff is present;
  * is a no-op outside proof mode (legacy dev/CI may host-build).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GA = ROOT / "artifact_deepswe" / "gt_agent.py"

_HANDOFF_KEYS = (
    "GT_PROOF_MODE",
    "GT_PORTABLE_SUBSTRATE",
    "GT_HOST_GRAPH_DB",
    "GT_CERT_DIR",
    "GT_BASELINE",
    "GT_FORBID_PREBUILT_GRAPH",
)


def _load_ga(modname: str = "gt_agent_d1"):
    spec = importlib.util.spec_from_file_location(modname, GA)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def ga():
    return _load_ga()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in _HANDOFF_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_proof_without_substrate_handoff_aborts(ga, monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    # No GT_PORTABLE_SUBSTRATE / GT_HOST_GRAPH_DB / GT_CERT_DIR -> not substrate-active.
    with pytest.raises(ga.DeepSweAdapterError, match="substrate handoff is absent"):
        ga._assert_substrate_handoff()


def test_proof_substrate_active_but_missing_handoff_env_aborts(ga, monkeypatch):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    # substrate-active via GT_PORTABLE_SUBSTRATE, but the required handoff paths unset.
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    with pytest.raises(ga.DeepSweAdapterError, match="required substrate-handoff env"):
        ga._assert_substrate_handoff()


def test_proof_with_full_handoff_passes(ga, monkeypatch, tmp_path):
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_PORTABLE_SUBSTRATE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    # Full handoff present -> must not raise (artifact/hash checks happen downstream).
    ga._assert_substrate_handoff()


def test_proof_handoff_via_host_graph_only_still_needs_cert_dir(ga, monkeypatch, tmp_path):
    # GT_HOST_GRAPH_DB makes substrate-active True, but GT_CERT_DIR is still required.
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "graph.db"))
    with pytest.raises(ga.DeepSweAdapterError, match="GT_CERT_DIR"):
        ga._assert_substrate_handoff()


def test_non_proof_is_noop(ga):
    # No GT_PROOF_MODE -> no-op even with zero handoff env (legacy dev/CI host-build).
    ga._assert_substrate_handoff()

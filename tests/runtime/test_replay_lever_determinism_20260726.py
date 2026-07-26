"""Replay must reconstruct the RECORDING's decisions, not the replay process's.

Architecture item 8: given the same repository state, canonical events and evidence producers,
the runtime must reconstruct the same work state, reasoning graph, lifecycle, release decisions
and suppression decisions.

``GT_ROLE_DRIVEN_COALITION`` decides which evidence is eligible for a decision. An adversarial
audit found that ``install_canonical_replay_runtime`` built its env from ``os.environ`` and
handed that to ``install_canonical_runtime`` -- so the same recording replayed differently
depending on the operator's shell.

THE CORRECTION THIS FILE ENCODES: function purity does NOT buy replayability. The temporal
gate, the coalition composer and the capsule compiler are all pure -- verified by AST, no
os/time/random -- and replay was still non-deterministic, because the lever is chosen OUTSIDE
those functions and is recorded NOWHERE. Purity means "no hidden reads inside"; replayability
additionally requires that every input be part of the recorded state.

Every recording that exists predates the lever, so its decisions were produced with it OFF.
Replay therefore pins it rather than inheriting it.

KNOWN REMAINING GAP, deliberately not papered over: the lever is still not written into the
journal or attestation, so a FUTURE recording made with it ON cannot be replayed faithfully
either -- it would replay as OFF. Closing that needs a recorded per-attempt field; this file
fixes the silent-divergence half and pins the contract for the rest.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


REPLAY_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "swebench" / "ss_replay_oracle.py"
)


@pytest.fixture(scope="module")
def replay_module():
    """Load the replay driver by path.

    It resolves ``gt_mini_patch`` at module scope, so ``artifact_deepswe`` must be importable
    or a dataclass field annotation resolves to None and import dies inside ``dataclasses``.
    """
    import sys

    repo = Path(__file__).resolve().parents[2]
    for extra in (repo, repo / "src", repo / "artifact_deepswe"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location("ss_replay_oracle", REPLAY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ss_replay_oracle"] = module
    spec.loader.exec_module(module)
    return module


def _installed_env(replay_module, monkeypatch, *, ambient, recorded):
    """Capture the env `install_canonical_replay_runtime` hands to the seam."""
    captured = {}

    class _Seam:
        @staticmethod
        def install_canonical_runtime(*, model, agent, env, task):
            captured.update(env)
            return type("A", (), {"attached": True})()

    if ambient is None:
        monkeypatch.delenv("GT_ROLE_DRIVEN_COALITION", raising=False)
    else:
        monkeypatch.setenv("GT_ROLE_DRIVEN_COALITION", ambient)
    monkeypatch.setenv("GT_CANONICAL_REPLAY", "1")

    # env is a copy of the ambient process environment, as the real driver passes it.
    replay_module.install_canonical_replay_runtime(
        _Seam(), "task-1", env=dict(os.environ), recorded_role_driven=recorded
    )
    return captured


def test_replay_does_not_inherit_an_ambient_lever(replay_module, monkeypatch):
    """THE DEFECT. Ambient ON must not silently change how a recording replays."""
    captured = _installed_env(
        replay_module, monkeypatch, ambient="1", recorded=None
    )
    assert captured.get("GT_ROLE_DRIVEN_COALITION") == "0"


def test_replay_pins_the_lever_off_when_unset(replay_module, monkeypatch):
    captured = _installed_env(
        replay_module, monkeypatch, ambient=None, recorded=None
    )
    assert captured.get("GT_ROLE_DRIVEN_COALITION") == "0"


def test_replay_honours_an_explicitly_supplied_lever(replay_module, monkeypatch):
    """The recorded value is the caller stating what the recording used.

    This is the seam a future recorded-lever lookup plugs into: read it from the recording,
    pass it here, and replay reconstructs faithfully.
    """
    captured = _installed_env(
        replay_module, monkeypatch, ambient="0", recorded="1"
    )
    assert captured.get("GT_ROLE_DRIVEN_COALITION") == "1"


def test_recorded_lever_beats_a_conflicting_ambient_value(replay_module, monkeypatch):
    captured = _installed_env(
        replay_module, monkeypatch, ambient="1", recorded="0"
    )
    assert captured.get("GT_ROLE_DRIVEN_COALITION") == "0"


def test_the_lever_is_still_not_recorded_anywhere(replay_module):
    """Characterization of the REMAINING gap, so it cannot be forgotten.

    When the lever becomes part of the recorded attempt state, this test should FAIL --
    delete it then and assert the recorded value is what replay uses.
    """
    runtime_source = (
        Path(__file__).resolve().parents[2]
        / "src" / "groundtruth" / "runtime" / "reasoning_runtime.py"
    ).read_text(encoding="utf-8")
    journal_region = runtime_source[runtime_source.index("CREATE TABLE IF NOT EXISTS"):]
    assert "role_driven" not in journal_region.split("def ")[0], (
        "the lever now appears in the journal schema -- replay should read it from the "
        "recording instead of pinning a default; update this test and the replay driver"
    )

from __future__ import annotations

import importlib
import importlib.util
import sys
import types

import pytest

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# Every entrypoint that runs on a LIVE benchmark path (OH + DeepSWE). None may
# import a retired brief/scorer lineage.
LIVE_ENTRYPOINTS = [
    ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py",
    ROOT / "scripts" / "swebench" / "oh_gt_live_lite_wrapper.py",
    ROOT / "artifact_deepswe" / "gt_agent.py",
    ROOT / "artifact_deepswe" / "gt_mini_patch.py",
]
DEAD_IMPORTS = [
    "groundtruth.pretask.v22_brief",
    "groundtruth.pretask.v2_ranker",
    "groundtruth.brief.graph_map",
]


def test_live_wrappers_do_not_import_dead_brief_paths():
    from groundtruth.runtime.dead_path_registry import DEAD_PATHS

    for dead in DEAD_IMPORTS:
        assert dead in DEAD_PATHS, f"{dead} must be quarantined in the dead-path registry"

    offenders: list[str] = []
    for path in LIVE_ENTRYPOINTS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for dead in DEAD_IMPORTS:
            if dead in text:
                offenders.append(f"{path.relative_to(ROOT)} imports {dead}")
    assert offenders == [], offenders


def test_live_brief_path_is_v1r():
    """The live benchmark path POSITIVELY uses v1r_brief.generate_v1r_brief — not
    just 'absence of dead'. Locks the live brief lineage."""
    wrapper = ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py"
    agent = ROOT / "artifact_deepswe" / "gt_agent.py"
    assert "generate_v1r_brief" in wrapper.read_text(encoding="utf-8")
    assert "generate_v1r_brief" in agent.read_text(encoding="utf-8")


def test_v2_ranker_is_dead_by_association_only():
    """v2_ranker is reachable ONLY from the (also-dead) v22_brief — no other live
    src/groundtruth module imports it. If that changes, v2_ranker is no longer
    safe to keep quarantined and this test must be revisited."""
    src = ROOT / "src" / "groundtruth"
    importers: list[str] = []
    for py in src.rglob("*.py"):
        if py.name in ("v2_ranker.py",):
            continue
        if "from groundtruth.pretask.v2_ranker import" in py.read_text(encoding="utf-8"):
            importers.append(py.relative_to(ROOT).as_posix())
    assert importers == ["src/groundtruth/pretask/v22_brief.py"], importers


def test_stale_snapshots_cannot_masquerade_as_live():
    """The pregen_gt/ + gen_lab/ snapshot trees contain stale copies of the old
    src (incl. v22_brief), but they are NOT importable as `groundtruth.*` — they
    have no root package marker and `import groundtruth.pretask.v22_brief`
    resolves to the REAL (registry-guarded) src tree. Prove the resolved module
    is the live src, never a snapshot copy."""
    spec = importlib.util.find_spec("groundtruth.pretask.v22_brief")
    assert spec is not None and spec.origin is not None
    origin = Path(spec.origin).resolve()
    live_src = (ROOT / "src" / "groundtruth").resolve()
    assert str(origin).startswith(str(live_src)), origin
    for snap in ("pregen_gt", "gen_lab"):
        assert snap not in origin.parts, f"v22_brief resolved into snapshot {snap}: {origin}"


# --------------------------------------------------------------------------------------
# Runtime teeth: assert_no_dead_surface_loaded() — fail-closed guard on the proof path.
# These are TRIPWIRE tests (TTD): the guard must PASS on a clean live chain, RAISE when a
# DEAD_PATHS module is loaded under proof mode, and stay a NO-OP outside proof mode.
# Mutation-check: weakening the guard (drop the proof-mode/loaded check) turns these RED.
# --------------------------------------------------------------------------------------

LIVE_CHAIN_MODULES = [
    "groundtruth.runtime.brief_cache",
    "groundtruth.pretask.v1r_brief",
    "groundtruth.pretask.v7_4_brief",
    "groundtruth.pretask.graph_localizer",
    "groundtruth.pretask.anchor_select",
    "groundtruth.pretask.anchor_proximity",
    "groundtruth.pretask.hybrid",
    "groundtruth.lsp.config",
    "groundtruth.memory.enrich.embed",
]


def test_guard_passes_on_clean_live_chain_under_proof_mode():
    """The guard must NOT false-fail: importing the real live DeepSWE brief chain
    (v1r_brief -> v7_4_brief -> localizers/anchors + embedder + LSP config) imports ZERO
    DEAD_PATHS module, so the guard returns cleanly even with proof mode active. Hermetic:
    measured as a DELTA (the live chain must not NEWLY load a dead module) and the guard is
    checked against the live-chain module map only, so a dead module another test left in the
    global sys.modules cannot pollute this assertion (the real proof runs in a fresh process)."""
    from groundtruth.runtime.dead_path_registry import (
        DEAD_PATHS,
        assert_no_dead_surface_loaded,
    )

    dead_before = {d for d in DEAD_PATHS if d in sys.modules}
    for m in LIVE_CHAIN_MODULES:
        importlib.import_module(m)
    dead_after = {d for d in DEAD_PATHS if d in sys.modules}
    # The live chain itself imports no dead module (delta, not global process state).
    assert dead_after - dead_before == set(), f"live chain newly imported dead: {dead_after - dead_before}"
    # Proof mode active, live-chain-only module map -> must not raise.
    live_only = {m: sys.modules[m] for m in LIVE_CHAIN_MODULES if m in sys.modules}
    assert assert_no_dead_surface_loaded(env={"GT_PROOF_MODE": "1"}, modules=live_only) is None
    assert assert_no_dead_surface_loaded(env={"GT_REQUIRE_FULL_STACK": "1"}, modules=live_only) is None


@pytest.mark.parametrize("flag", ["GT_PROOF_MODE", "GT_REQUIRE_FULL_STACK"])
def test_guard_raises_when_dead_module_loaded_under_proof_mode(flag):
    """Force a DEAD_PATHS module into the injected module map under proof mode; the guard
    MUST raise, and the error MUST name the dead module + its live replacement."""
    from groundtruth.runtime.dead_path_registry import (
        DEAD_PATHS,
        DeadSurfaceLoadedError,
        assert_no_dead_surface_loaded,
    )

    dead_name = "groundtruth.pretask.v22_brief"
    fake_modules = {dead_name: types.ModuleType(dead_name)}
    with pytest.raises(DeadSurfaceLoadedError) as exc:
        assert_no_dead_surface_loaded(env={flag: "1"}, modules=fake_modules)

    msg = str(exc.value)
    assert dead_name in msg, msg
    assert DEAD_PATHS[dead_name]["replacement"] in msg, msg


def test_guard_is_noop_outside_proof_mode():
    """Off the proof/substrate path (no proof flags) the guard is a strict no-op even with
    a dead module loaded — OH/MCP/CLI harnesses that legitimately reach CLI-legacy surface
    must never be tripped."""
    from groundtruth.runtime.dead_path_registry import assert_no_dead_surface_loaded

    fake_modules = {"groundtruth.pretask.v22_brief": types.ModuleType("x")}
    # No proof flags set -> no-op, regardless of loaded dead modules.
    assert assert_no_dead_surface_loaded(env={}, modules=fake_modules) is None
    assert (
        assert_no_dead_surface_loaded(
            env={"GT_PROOF_MODE": "0"}, modules=fake_modules
        )
        is None
    )

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

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
    "groundtruth.pretask.v7_brief",
    "groundtruth.pretask.brief_v5",
    "groundtruth.pretask.v7_layers",
]

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


def test_live_wrappers_do_not_import_dead_brief_paths():
    from groundtruth.runtime.dead_path_registry import DEAD_PATHS

    for dead in DEAD_IMPORTS:
        assert dead in DEAD_PATHS, f"{dead} must be in the dead-path registry"

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
    wrapper = ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py"
    agent = ROOT / "artifact_deepswe" / "gt_agent.py"
    assert "generate_v1r_brief" in wrapper.read_text(encoding="utf-8")
    assert "generate_v1r_brief" in agent.read_text(encoding="utf-8")


def test_dead_pretask_modules_only_exist_in_deprecated_archive():
    for dead in DEAD_IMPORTS:
        assert importlib.util.find_spec(dead) is None, dead
    assert importlib.util.find_spec("groundtruth.pretask._deprecated.v22_brief") is not None


def test_live_source_does_not_import_dead_surface_names():
    src = ROOT / "src" / "groundtruth"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if "/_deprecated/" in rel:
            continue
        text = py.read_text(encoding="utf-8")
        for dead in DEAD_IMPORTS:
            if dead in text and rel != "src/groundtruth/runtime/dead_path_registry.py":
                offenders.append(f"{rel} mentions {dead}")
    assert offenders == [], offenders


def test_guard_passes_on_clean_live_chain_under_proof_mode():
    from groundtruth.runtime.dead_path_registry import (
        DEAD_PATHS,
        assert_no_dead_surface_loaded,
    )

    dead_before = {d for d in DEAD_PATHS if d in sys.modules}
    for m in LIVE_CHAIN_MODULES:
        importlib.import_module(m)
    dead_after = {d for d in DEAD_PATHS if d in sys.modules}
    assert dead_after - dead_before == set(), (
        f"live chain newly imported dead: {dead_after - dead_before}"
    )

    live_only = {m: sys.modules[m] for m in LIVE_CHAIN_MODULES if m in sys.modules}
    assert assert_no_dead_surface_loaded(env={"GT_PROOF_MODE": "1"}, modules=live_only) is None
    assert (
        assert_no_dead_surface_loaded(env={"GT_REQUIRE_FULL_STACK": "1"}, modules=live_only) is None
    )


@pytest.mark.parametrize("flag", ["GT_PROOF_MODE", "GT_REQUIRE_FULL_STACK"])
def test_guard_raises_when_dead_module_loaded_under_proof_mode(flag):
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
    from groundtruth.runtime.dead_path_registry import assert_no_dead_surface_loaded

    fake_modules = {"groundtruth.pretask.v22_brief": types.ModuleType("x")}
    assert assert_no_dead_surface_loaded(env={}, modules=fake_modules) is None
    assert assert_no_dead_surface_loaded(env={"GT_PROOF_MODE": "0"}, modules=fake_modules) is None

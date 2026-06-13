from __future__ import annotations

import importlib.util
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

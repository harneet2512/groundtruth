from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIVE_ENTRYPOINTS = [
    ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py",
    ROOT / "scripts" / "swebench" / "oh_gt_live_lite_wrapper.py",
]
DEAD_IMPORTS = [
    "groundtruth.pretask.v22_brief",
    "groundtruth.brief.graph_map",
]


def test_live_wrappers_do_not_import_dead_brief_paths():
    from groundtruth.runtime.dead_path_registry import DEAD_PATHS

    for dead in DEAD_IMPORTS:
        assert dead in DEAD_PATHS

    offenders: list[str] = []
    for path in LIVE_ENTRYPOINTS:
        text = path.read_text(encoding="utf-8")
        for dead in DEAD_IMPORTS:
            if dead in text:
                offenders.append(f"{path.relative_to(ROOT)} imports {dead}")
    assert offenders == []

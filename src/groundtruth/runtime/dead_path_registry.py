"""Registry of retired runtime paths that must not load on proof paths."""

from __future__ import annotations

import os
import sys

DEAD_PATHS: dict[str, dict[str, str]] = {
    "groundtruth.pretask.v22_brief": {
        "reason": "retired L2 brief generator; live runtime must use v1r or stay silent",
        "replacement": "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.pretask.v2_ranker": {
        "reason": "retired RRF file/function ranker reachable only from the dead v22 brief",
        "replacement": "groundtruth.pretask.v7_4_brief.run_v74",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.brief.graph_map": {
        "reason": "deleted graph-map renderer bypassed v1r provenance policy",
        "replacement": "v1r <gt-graph-map> rendering",
        "status": "DEAD_PATH_DELETED",
    },
    "groundtruth.runtime.reindex_helper": {
        "reason": "obsolete --incremental helper; live wrapper constructs gt-index -file directly",
        "replacement": "oh_gt_full_wrapper.make_reindex_command",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.pretask.v7_brief": {
        "reason": "retired v7 brief orchestrator; duplicate localization/render lineage",
        "replacement": "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.pretask.brief_v5": {
        "reason": "retired v5/v6 deterministic localization predecessor",
        "replacement": "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.pretask.v7_layers": {
        "reason": "retired layer collector for the v7 brief body",
        "replacement": "v1r_brief / v7_4_brief inline pillars",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
}


class DeadSurfaceLoadedError(RuntimeError):
    """Raised when a retired module is loaded on the proof/substrate path."""


def _proof_mode_active(env: dict[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return e.get("GT_PROOF_MODE") == "1" or e.get("GT_REQUIRE_FULL_STACK") == "1"


def assert_no_dead_surface_loaded(
    env: dict[str, str] | None = None,
    modules: dict[str, object] | None = None,
) -> None:
    """Fail closed if a retired module is loaded while proof mode is active."""
    if not _proof_mode_active(env):
        return

    loaded = sys.modules if modules is None else modules
    offenders: list[str] = []
    for dead, meta in DEAD_PATHS.items():
        if dead in loaded:
            replacement = meta.get("replacement", "(no replacement recorded)")
            offenders.append(f"{dead}  ->  LIVE replacement: {replacement}")

    if offenders:
        raise DeadSurfaceLoadedError(
            "GT_DEAD_SURFACE_LOADED: a retired DEAD_PATHS module is loaded on the live "
            "proof/substrate path. Offending module(s):\n  " + "\n  ".join(offenders)
        )

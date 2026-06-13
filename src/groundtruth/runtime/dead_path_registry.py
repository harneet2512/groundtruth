"""Registry of retired runtime paths that must not be imported by live wrappers."""

from __future__ import annotations

DEAD_PATHS: dict[str, dict[str, str]] = {
    "groundtruth.pretask.v22_brief": {
        "reason": "retired L2 brief generator; live runtime must use v1r or stay silent",
        "replacement": "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.pretask.v2_ranker": {
        "reason": "v8.2.2 RRF file/function ranker reachable ONLY from the dead v22_brief "
                  "(dead-by-association, F/2026-06-13); live ranking is v7_4_brief.run_v74",
        "replacement": "groundtruth.pretask.v7_4_brief.run_v74",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.brief.graph_map": {
        "reason": "old graph-map renderer bypasses v1r provenance policy",
        "replacement": "v1r <gt-graph-map> rendering",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
    "groundtruth.runtime.reindex_helper": {
        "reason": "obsolete --incremental helper; live wrapper constructs gt-index -file directly",
        "replacement": "oh_gt_full_wrapper.make_reindex_command",
        "status": "DEAD_PATH_CONFIRMED_AFTER_IMPORT_GUARD",
    },
}

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

# CLI_LEGACY (2026-06-17): dead on the DeepSWE substrate path but STILL LIVE on the OpenHands /
# kernel CLI runners — so they are NOT hard DEAD_PATHS (a hard ban would break those runners).
# Advisory only: the live DeepSWE brief is v1r_brief.generate_v1r_brief -> v7_4_brief.run_v74.
# Importer-traced 2026-06-17 (see GT_ARCHITECTURE_LINEAGE.md): these reach the live tree ONLY via
# cli/commands.py + control/kernel.py + scripts/run_baseline_v73.py / run_v74_holdout.py /
# phase0_audit.py / run_kernel_paired_gate.py / run_live_lite_paired.py — never v1r_brief/v7_4_brief.
CLI_LEGACY: dict[str, dict[str, str]] = {
    "groundtruth.pretask.v7_brief": {
        "reason": "old v7.3/7.4 brief orchestrator (pre-v1r); DEAD on DeepSWE, live only on OH/kernel CLI",
        "replacement": "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        "status": "DEAD_ON_DEEPSWE_CLI_LEGACY",
    },
    "groundtruth.pretask.brief_v5": {
        "reason": "v5/v6 deterministic localization predecessor; reached only via v7_brief + lazy __init__ shim",
        "replacement": "groundtruth.pretask.v1r_brief.generate_v1r_brief",
        "status": "DEAD_ON_DEEPSWE_CLI_LEGACY",
    },
    "groundtruth.pretask.v7_layers": {
        "reason": "v7 layer collector for v7_brief body; dead-by-association (only v7_brief imports it)",
        "replacement": "v1r_brief / v7_4_brief inline pillars",
        "status": "DEAD_ON_DEEPSWE_CLI_LEGACY",
    },
}

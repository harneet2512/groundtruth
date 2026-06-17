"""Tripwire: the 4 hard-dead modules import cleanly but FAIL LOUD on call.

These modules are quarantined in groundtruth.runtime.dead_path_registry.DEAD_PATHS
and are superseded on every live path (see GT_ARCHITECTURE_LINEAGE.md). The
dead-path registry test imports them (to assert registration), so a module-level
raise is forbidden — the guard lives at the START of each module's PRIMARY ENTRY
producer. This test locks BOTH halves of that contract:

  1. ``import <module>`` succeeds (registry test must keep passing).
  2. calling the producer raises RuntimeError with "DEAD SURFACE".

If a future change re-wires one of these onto a live path, the entry guard must
be removed deliberately AND this tripwire updated — it is not collateral.
"""
from __future__ import annotations

import pytest


def test_v22_brief_import_ok_but_call_raises():
    from groundtruth.pretask import v22_brief  # import must succeed

    with pytest.raises(RuntimeError, match="DEAD SURFACE"):
        v22_brief.generate_brief("real issue text", "/repo", "/db")


def test_v2_ranker_import_ok_but_entrypoints_raise():
    from groundtruth.pretask import v2_ranker  # import must succeed

    for entry in (v2_ranker.rank_files, v2_ranker.rank_functions, v2_ranker.rank):
        with pytest.raises(RuntimeError, match="DEAD SURFACE"):
            # args are irrelevant — the guard is the first statement.
            entry(object(), [], "/repo", "/db") if entry is v2_ranker.rank_functions \
                else entry(object(), "/repo", "/db")


def test_graph_map_import_ok_but_call_raises():
    from groundtruth.brief import graph_map  # import must succeed

    with pytest.raises(RuntimeError, match="DEAD SURFACE"):
        graph_map.build_graph_map([{"file": "x.py", "score": 1.0}], "/db")


def test_reindex_helper_import_ok_but_call_raises():
    from groundtruth.runtime import reindex_helper  # import must succeed

    with pytest.raises(RuntimeError, match="DEAD SURFACE"):
        reindex_helper.check_and_reindex_modified_files(
            modified_files=["a.py"], root="/repo", db_path="/db"
        )

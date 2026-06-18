"""Tripwire: hard-dead modules no longer resolve at their old live names."""

from __future__ import annotations

import importlib.util


def test_old_dead_surface_names_do_not_resolve():
    for name in (
        "groundtruth.pretask.v22_brief",
        "groundtruth.pretask.v2_ranker",
        "groundtruth.pretask.v7_brief",
        "groundtruth.pretask.brief_v5",
        "groundtruth.pretask.v7_layers",
        "groundtruth.brief.graph_map",
    ):
        assert importlib.util.find_spec(name) is None, name


def test_deprecated_archive_is_explicit():
    assert importlib.util.find_spec("groundtruth.pretask._deprecated.v7_brief") is not None

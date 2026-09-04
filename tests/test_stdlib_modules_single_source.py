"""Drift lock (I8): the two STDLIB_MODULES twins must stay equal.

curation_map._STDLIB_MODULES (the <gt-graph-map> shadow guard) and
delivery.name_policy.STDLIB_MODULES (the brief/delivery fact-filter) are two
independently-defined frozensets. A cross-module import is avoided (both ship
in-container stdlib-only), so this test is the single enforcement that a one-sided edit
cannot land silently — drift would drop different stdlib-shadow edges on the same repo.
"""

from __future__ import annotations

from groundtruth.pretask.curation_map import _STDLIB_MODULES
from groundtruth.delivery.name_policy import STDLIB_MODULES


def test_stdlib_modules_twins_are_equal():
    assert _STDLIB_MODULES == STDLIB_MODULES, (
        "STDLIB_MODULES drifted between curation_map and name_policy: "
        f"only-in-curation={sorted(_STDLIB_MODULES - STDLIB_MODULES)} "
        f"only-in-name_policy={sorted(STDLIB_MODULES - _STDLIB_MODULES)}"
    )

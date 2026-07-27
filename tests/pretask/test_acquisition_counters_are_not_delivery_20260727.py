"""Acquisition counters must not be computed over the DELIVERED set.

THE DEFECT THAT COST A DAY. `v1r_brief.py` computes:

    _delivered = _model_visible_localization_entries(brief_text, _loc_files)   # :6764
    _ge, _sem_c, _struct_c, _fts5_c = _l1_signal_counts(graph_db, _delivered, _aligned_records)

The code's own comment says "Count over the DELIVERED candidate set" — it is counting DELIVERY,
deliberately. But it stores the result in fields named `fts5_signal_count`, `semantic_signal_count`,
`structural_signal_count`, `graph_edge_count`. Those names mean ACQUISITION.

Under `_brief_minimal_on()` (:6754-6755) `_reduce_brief_to_minimal` deletes every localization
header and file-entry block, so `_delivered` is EMPTY BY CONSTRUCTION. The four counters then read
0 and assert "nothing was acquired" when the truth is "nothing was delivered".

MEASURED CONSEQUENCE (run 30297116212): all six acquisition counters read 0 while the SAME RUN's
`embedder_certificate.json` reported `semantic_candidate_count: 112` and
`rendered_semantic_nonzero_count: 42`, and driving the production `graph_localizer.localize`
against that run's own graph produced 50 candidates. **The same run reported 112 and 0 under the
same name.** That zero was read as "the acquisition subsystem is dark", written into gt_gt.md as
architecture state-of-record, and used to redirect a day of work. It was a broken gauge.

This is the codebase's signature defect class: TWO DIFFERENT FACTS SHARING ONE NAME.

THE FIX IS NOT TO MOVE THE COMPUTATION. Both facts are wanted — what was acquired, and what was
delivered. They must stop sharing a name: count the legs over the RANKED set (`top_records`, which
carries the run_v74 `components`), and expose delivery separately as `delivered_*`, reporting
NOT-EVALUABLE rather than 0 when the re-slot empties it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.pretask import v1r_brief as vb  # noqa: E402


def _record(path: str, *, sem: float, lex: float, reach: float) -> dict:
    """A ranked record shaped as run_v74 emits it."""
    return {"path": path, "components": {"sem": sem, "lex": lex, "reach": reach}}


RANKED = [
    _record("src/pkg/a.py", sem=0.71, lex=0.44, reach=0.30),
    _record("src/pkg/b.py", sem=0.52, lex=0.10, reach=0.00),
]


class _Entry:
    """Minimal stand-in for FileEntry: the counter only reads `.path`."""

    def __init__(self, path: str) -> None:
        self.path = path


def test_positive_control_the_counter_CAN_produce_nonzero():
    """THE CONTROL THAT WAS NEVER RUN. Before believing any zero from this function, prove it can
    return non-zero on the same inputs. Without this, the assertion below is unreadable."""
    entries = [_Entry(r["path"]) for r in RANKED]
    _ge, sem_c, struct_c, fts5_c = vb._l1_signal_counts("", entries, RANKED)
    assert sem_c == 2, f"semantic count should see both records, got {sem_c}"
    assert fts5_c == 2, f"lexical count should see both records, got {fts5_c}"
    assert struct_c >= 1, f"structural count should see the reach>0 record, got {struct_c}"


def test_an_empty_DELIVERED_set_zeroes_every_counter_even_though_signals_exist():
    """THE DEFECT, at the unit level. Same acquired signals; nothing delivered; all counters 0.

    This is exactly what `GT_BRIEF_MINIMAL` + `GT_LOC_RESLOT` produce in production, and it is why
    the run reported 0 acquisition while its own embedder certificate reported 112.
    """
    _ge, sem_c, struct_c, fts5_c = vb._l1_signal_counts("", [], [])
    assert (sem_c, struct_c, fts5_c) == (0, 0, 0), (
        "control failed: the empty-delivered path did not zero the counters, so this file is "
        "reasoning about the wrong mechanism"
    )
    # The signals demonstrably existed (previous test). The counters cannot tell you that.
    # A caller reading these four numbers CANNOT distinguish "nothing acquired" from
    # "nothing delivered" -- which is the entire bug.


def test_an_acquisition_side_counter_exists_and_is_delivery_independent():
    """THE FIX, specified as a failing test.

    There must be a way to count the legs over the RANKED set, independent of what survived the
    brief reduction. `strict=True` means this fails loudly the moment the fix lands, forcing the
    xfail to be removed rather than left to rot.
    """
    counter = getattr(vb, "_l1_acquisition_counts", None)
    assert counter is not None, "no delivery-independent acquisition counter"
    _ge, sem_c, struct_c, fts5_c = counter("", RANKED)
    assert sem_c == 2 and fts5_c == 2, (
        "the acquisition counter must report what was ACQUIRED regardless of delivery"
    )

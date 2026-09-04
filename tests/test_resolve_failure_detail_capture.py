"""LSP cert diagnosability fix — red->green (DeepSWE non-Python audit, run
27290157847).

The go cert stamped ``failed_breakdown.lsp_error=7/7`` and the rust cert
``empty=6620/6620`` — both with ``failure_detail: ""``. The pass converted ZERO
edges and the artifact carried no evidence of WHY (gopls workspace-load error
text / rust-analyzer still indexing), so the failure was undiagnosable from
the cert. Fix: ``_note_failure_detail`` (first-detail-wins, bounded, never
raises) called at the per-edge lsp_error site and at the all-empty
project-not-ready epilogue.
"""

from __future__ import annotations

from groundtruth.resolve import _note_failure_detail


def test_first_detail_wins():
    stats: dict = {"failure_detail": ""}
    _note_failure_detail(stats, "definition: no package metadata for file x.go")
    _note_failure_detail(stats, "definition: a later, different error")
    assert stats["failure_detail"] == ("definition: no package metadata for file x.go")


def test_never_overwrites_existing_detail():
    stats: dict = {"failure_detail": "start: gopls exited 2"}
    _note_failure_detail(stats, "definition: whatever")
    assert stats["failure_detail"] == "start: gopls exited 2"


def test_bounded_length():
    stats: dict = {"failure_detail": ""}
    _note_failure_detail(stats, "x" * 5000)
    assert len(stats["failure_detail"]) <= 300


def test_empty_and_none_are_ignored():
    stats: dict = {"failure_detail": ""}
    _note_failure_detail(stats, "")
    _note_failure_detail(stats, "   ")
    _note_failure_detail(stats, None)  # type: ignore[arg-type]
    assert stats["failure_detail"] == ""


def test_never_raises_on_hostile_stats():
    class Hostile(dict):
        def __setitem__(self, k, v):  # noqa: D105
            raise RuntimeError("boom")

    _note_failure_detail(Hostile({"failure_detail": ""}), "detail")  # no raise

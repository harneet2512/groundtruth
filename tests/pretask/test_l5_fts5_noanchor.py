"""Fable L5 (RED→GREEN): the FTS5 leg must be allowed to seed on the repo-less / no-anchor
path (behavior-described / stratum-B issues, MCP-without-repo).

Before the fix, `localize()` early-returned `no_anchor_hit` whenever there were no symbol
anchors AND no `repo_root` AND `GT_CONTENT_LEG` was off — even though the FTS5 leg
(`_fts5_candidates`) lives INSIDE graph.db (it builds an in-memory index from `nodes` when the
persisted `nodes_fts` is absent) and needs NO repo and NO external deps. The carve-out named
only the content-BM25 leg, so FTS5 was dead on precisely the path it should rescue.

The fix lets FTS5 try when the issue has tokens; the existing `if not seeds` net still returns
`no_anchor_hit` when every leg comes up empty, so nothing is over-claimed. The measured/paid path
(repo_root present) never hit this bail → byte-identical there.

Mutation check: restoring the guard to `... and os.getenv("GT_CONTENT_LEG") != "1"` (dropping the
`and not _fts_seedable` clause) makes the no-anchor call return no_anchor_hit → RED.
"""
import sqlite3

import pytest

from groundtruth.pretask.graph_localizer import IssueAnchors, localize


def _fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_l5_fts5_seeds_when_no_anchor_and_no_repo(tiny_graph_db):
    # No symbol anchors (empty IssueAnchors) and NO repo_root — the exact stratum-B repo-less
    # path. The issue tokens ("watchdog") FTS-match the fixture's `patroni/watchdog.py` nodes
    # via the file_path column that _FTS5_POPULATE indexes.
    res = localize(
        "watchdog connection intermittently fails",
        tiny_graph_db,
        issue_anchors=IssueAnchors(),  # force zero anchors → exercises the no-anchor branch
        repo_root="",                  # repo-less → the branch the bail wrongly killed
    )
    assert res.gate_reason != "no_anchor_hit", (
        "FTS5 must seed the no-anchor/repo-less path; got an early no_anchor_hit bail. "
        f"gate_reason={res.gate_reason!r}"
    )
    assert res.candidates, "expected FTS5-seeded candidates on the no-anchor path, got none"
    # The FTS5-recalled file is the watchdog module (the only nodes whose file_path carries the
    # 'watchdog' token). Generalized: the assertion is on FTS recall, not a task-specific symbol.
    files = {c.file_path for c in res.candidates}
    assert any("watchdog" in f for f in files), f"watchdog module not recalled; files={files}"

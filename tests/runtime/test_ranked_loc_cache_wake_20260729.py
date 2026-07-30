"""Ranked-localization cache must not immortalize emptiness across graph births.

RED-FIRST (2026-07-29). Baseline defect, pinned before the fix:
``gateway._ranked_localization_rows`` memoized its rows on ``state.episode``
UNKEYED — a search that ran before graph.db existed (dormant-start task,
pre-L6-wake) cached ``[]`` and returned it for the ENTIRE episode, muting
GT_LOC_RESLOT even after the graph was born or re-indexed (live mini run:
``cached_empty_ranked_rows: 38``).

The fix keys the cache on a graph-identity token (``graph_db`` path +
``st_mtime_ns`` + ``st_size``; missing file -> ``(path, None, None)``), so:
  * graph missing -> [] is cached only for the MISSING state; when the file is
    born the token differs and localize() re-runs (the wake sequence works);
  * a re-index (file replaced) also invalidates;
  * legitimate negative caching for a real no-match on an UNCHANGED graph is
    preserved (the localize()-runs-once cost memoization stays load-bearing).

MUTATION TARGETS:
  * make ``_graph_state_token`` return a constant -> the wake + re-index tests
    bite (stale [] / stale rows returned again);
  * drop the memoization write entirely -> the runs-once tests bite.
"""

from __future__ import annotations

import sqlite3
import types

from groundtruth.runtime import gateway as gw
from groundtruth.runtime.episode_state import EpisodeState


def _mk_graph(db_path, names=("verify_token",)) -> None:
    con = sqlite3.connect(str(db_path))
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
    )
    for i, name in enumerate(names, start=1):
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?)",
            (i, "Function", name, "auth.py", 10 * i, 0, "python"),
        )
    con.commit()
    con.close()


def _fake_localize(*cands, anchors=("verify_token",)):
    def _fn(issue_text, graph_db, *, repo_root="", **kw):
        return types.SimpleNamespace(
            candidates=[types.SimpleNamespace(file_path=f) for f in cands],
            anchor_symbols=list(anchors),
        )
    return _fn


def _state(tmp_path, db_path, episode):
    return gw.GatewayState(
        graph_db=str(db_path),
        repo_root=str(tmp_path),
        issue_text="verify token verification fails",
        episode=episode,
    )


def test_graph_birth_unmutes_ranked_localization(tmp_path, monkeypatch) -> None:
    """THE WAKE SEQUENCE: search before the graph exists -> [] (honest); the
    graph is born (L6 wake / index completes) -> the SAME episode's next search
    must produce rows. Baseline cached [] forever -> RED."""
    monkeypatch.setattr(gw, "_localize", _fake_localize("auth.py"))
    episode = EpisodeState()
    db_path = tmp_path / "graph.db"  # does not exist yet (dormant start)
    st = _state(tmp_path, db_path, episode)

    assert gw._ranked_localization_rows(st) == []  # no substrate to ask: quiet

    _mk_graph(db_path)  # the graph is born
    rows = gw._ranked_localization_rows(st)

    assert rows == [("auth.py", 10, "verify_token")], rows


def test_reindex_invalidates_stale_rows(tmp_path, monkeypatch) -> None:
    """A REPLACED graph.db (re-index) must invalidate the memoized answer —
    the cache serves one graph state, never a prior one."""
    episode = EpisodeState()
    db_path = tmp_path / "graph.db"
    _mk_graph(db_path, names=("verify_token",))
    st = _state(tmp_path, db_path, episode)

    monkeypatch.setattr(gw, "_localize", _fake_localize("auth.py"))
    assert gw._ranked_localization_rows(st) == [("auth.py", 10, "verify_token")]

    db_path.unlink()
    _mk_graph(db_path, names=("verify_token", "refresh"))  # re-index: new content
    # Deterministic mtime advance: a same-size sqlite rewrite can land inside
    # the filesystem's timestamp granularity under load (observed flaky on
    # NTFS in the full-suite run). A real re-index takes far longer than one
    # mtime tick; the token mechanism is what this test pins, so advance the
    # clock explicitly instead of racing it.
    import os
    stat = os.stat(db_path)
    os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(
        gw, "_localize", _fake_localize("auth.py", anchors=("refresh",)),
    )
    rows = gw._ranked_localization_rows(st)

    assert rows == [("auth.py", 20, "refresh")], rows


def test_memoization_still_runs_localize_once_on_stable_graph(
    tmp_path, monkeypatch,
) -> None:
    """The load-bearing cost memoization is preserved: on an UNCHANGED graph,
    localize() runs exactly once however many search turns consume it."""
    calls = {"n": 0}

    def _counting(*a, **k):
        calls["n"] += 1
        return types.SimpleNamespace(
            candidates=[types.SimpleNamespace(file_path="auth.py")],
            anchor_symbols=["verify_token"],
        )

    monkeypatch.setattr(gw, "_localize", _counting)
    episode = EpisodeState()
    db_path = tmp_path / "graph.db"
    _mk_graph(db_path)
    st = _state(tmp_path, db_path, episode)

    for _ in range(3):
        assert gw._ranked_localization_rows(st) == [
            ("auth.py", 10, "verify_token"),
        ]
    assert calls["n"] == 1


def test_negative_cache_preserved_for_real_no_match_on_unchanged_graph(
    tmp_path, monkeypatch,
) -> None:
    """'The graph answered: nothing relevant' on an EXISTING, unchanged graph
    stays cacheable — the fix must not turn every honest no-match into a
    per-turn localize() re-run."""
    calls = {"n": 0}

    def _no_candidates(*a, **k):
        calls["n"] += 1
        return types.SimpleNamespace(candidates=[], anchor_symbols=[])

    monkeypatch.setattr(gw, "_localize", _no_candidates)
    episode = EpisodeState()
    db_path = tmp_path / "graph.db"
    _mk_graph(db_path)
    st = _state(tmp_path, db_path, episode)

    assert gw._ranked_localization_rows(st) == []
    assert gw._ranked_localization_rows(st) == []
    assert calls["n"] == 1  # negative result memoized while the graph is stable

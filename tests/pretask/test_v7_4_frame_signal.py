"""Frame/explicit-path signal tests for the v7.4 reranker.

THE FLIP LEVER: a file NAMED IN THE FAILING TRACEBACK FRAME (or typed verbatim
as a path in the issue) is a universal, high-precision localization signal that
the pre-change ranker ignored (run_v74 fed an EMPTY IssueAnchors() to BM25 and
its only path awareness was a basename bag-of-words prior).

Two properties are proven here:

  1. POSITIVE — an issue with a Python traceback naming ``app/importer.py:601``
     ranks that file ABOVE a keyword-only competitor (``app/importer_utils.py``)
     whose basename matches the issue keyword but which the runtime never named.

  2. NEGATIVE CONTROL (mandatory, no-regression) — an issue with NO traceback and
     NO resolvable path produces a ranking IDENTICAL to the pre-change ranker.
     We prove this by comparing run_v74 with the default weights (W_FRAME=0.60)
     against run_v74 forced to W_FRAME=0.0 (the pre-change behavior): the frame
     component contributes 0 to EVERY candidate, so the two full rankings must be
     byte-identical. This is the critical property: no-traceback tasks are
     untouched, so the next 30 tasks cannot regress from the frame signal.

The graph + files are synthetic (built in tmp_path). A deterministic FakeModel
(seeded RNG embeddings) stands in for sentence-transformers so the test does not
depend on a model download and the ranking is reproducible.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import numpy as np
import pytest


_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        name TEXT NOT NULL,
        qualified_name TEXT,
        file_path TEXT NOT NULL,
        start_line INTEGER,
        end_line INTEGER,
        signature TEXT,
        return_type TEXT,
        is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0,
        language TEXT NOT NULL,
        parent_id INTEGER REFERENCES nodes(id)
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        source_line INTEGER,
        source_file TEXT,
        resolution_method TEXT,
        confidence REAL DEFAULT 0.0,
        metadata TEXT
    );
"""


class _FakeModel:
    """Deterministic stand-in for sentence-transformers (seeded RNG embeddings).

    Returns the SAME embedding for the SAME input order on every call, so the
    semantic component is reproducible (and, being random, decisive on no
    component — which is what makes the frame/keyword contrast the lever).
    """

    def encode(self, texts, **kw):
        rng = np.random.default_rng(1234)
        embs = rng.random((len(texts), 384)).astype(np.float32)
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        return embs / np.where(norms == 0, 1, norms)


@pytest.fixture
def frame_repo(tmp_path: Path) -> tuple[str, str]:
    """Synthetic repo + graph.db with a gold file and a keyword decoy.

    Layout:
        app/importer.py        — gold: the runtime names this in the traceback.
                                  Function ``read_item`` lives here.
        app/importer_utils.py  — DECOY: basename + content stuffed with the issue
                                  keyword ("import"), but the trace never names it.
        app/unrelated.py       — filler so the candidate set is non-trivial.

    No graph edges are needed for the frame signal (it is path-resolution only),
    but we add one trivial same-file edge so the db is realistic.
    """
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)

    # Gold: the file the traceback names. Keep keyword density LOW so it cannot
    # win on BM25/keyword alone — the frame signal must be what lifts it.
    (repo / "app" / "importer.py").write_text(textwrap.dedent(
        """
        def read_item(record):
            # process a single record
            value = record.get("value")
            return value + 1
        """
    ))
    # Decoy: same keyword family, HIGH keyword density, but never in the trace.
    (repo / "app" / "importer_utils.py").write_text(textwrap.dedent(
        """
        # import import import helpers for importing imports during import
        def import_helper(import_arg):
            # importer importer importer import import import
            return import_arg
        def another_import_thing(import_x):
            return import_x
        """
    ))
    (repo / "app" / "unrelated.py").write_text(textwrap.dedent(
        """
        def do_other_stuff(x):
            return x * 2
        """
    ))

    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    nodes = [
        (1, "Function", "read_item",          None, "app/importer.py",       2, 5, None, None, 1, 0, "python", None),
        (2, "Function", "import_helper",       None, "app/importer_utils.py", 2, 4, None, None, 1, 0, "python", None),
        (3, "Function", "another_import_thing",None, "app/importer_utils.py", 5, 6, None, None, 1, 0, "python", None),
        (4, "Function", "do_other_stuff",      None, "app/unrelated.py",      2, 3, None, None, 1, 0, "python", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        nodes,
    )
    # One trivial edge so the db isn't edge-empty.
    conn.execute(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        (2, 3, "CALLS", 3, "app/importer_utils.py", "same_file", 1.0),
    )
    conn.commit()
    conn.close()
    return str(repo), str(db)


def _patch_model(monkeypatch):
    """Force run_v74 to use the deterministic FakeModel and mark sem available."""
    import groundtruth.pretask.v7_4_brief as mod
    monkeypatch.setattr(mod, "_get_model", lambda: _FakeModel())
    monkeypatch.setattr(mod, "_SEMANTIC_AVAILABLE", True)


def _rank_of(result, path: str) -> int:
    for r in result.ranked_full:
        if r["path"].replace("\\", "/").lstrip("./").lstrip("/") == path:
            return r["rank"]
    return 10**9


# ---------------------------------------------------------------- POSITIVE


def test_traceback_frame_outranks_keyword_competitor(frame_repo, monkeypatch):
    """A file named in the deepest in-repo traceback frame ranks above a
    keyword-only competitor that the runtime never named."""
    from groundtruth.pretask.v7_4_brief import run_v74

    _patch_model(monkeypatch)
    repo_root, graph_db = frame_repo

    # Python traceback: the FAILING (deepest) frame names app/importer.py:4 in
    # read_item. The issue keyword "import" matches the DECOY basename/content.
    issue = textwrap.dedent(
        """
        Importing a record raises a TypeError.

        Traceback (most recent call last):
          File "/build/repo/app/cli.py", line 12, in main
            run_import(records)
          File "/build/repo/app/importer.py", line 4, in read_item
            return value + 1
        TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'
        """
    )

    result = run_v74(
        issue_text=issue,
        repo_root=repo_root,
        graph_db=graph_db,
        bug_id="frame-pos",
        repo="test/repo",
        ablation="C",
    )

    gold_rank = _rank_of(result, "app/importer.py")
    decoy_rank = _rank_of(result, "app/importer_utils.py")

    # The frame-named gold must outrank the keyword-only decoy.
    assert gold_rank < decoy_rank, (
        f"gold app/importer.py ranked {gold_rank}, decoy ranked {decoy_rank}; "
        f"frame signal failed to lift the traceback-named file. "
        f"ranking={[(r['rank'], r['path'], r['components'].get('frame')) for r in result.ranked_full]}"
    )

    # The gold must actually carry a nonzero frame component (proves the signal
    # fired, not that it won by chance on another component).
    gold_comp = next(
        r["components"] for r in result.ranked_full
        if r["path"].replace("\\", "/").lstrip("./").lstrip("/") == "app/importer.py"
    )
    assert gold_comp.get("frame", 0.0) > 0.0


def test_verbatim_path_mention_resolves(frame_repo, monkeypatch):
    """A path typed verbatim in the issue (no traceback) carries a frame score."""
    from groundtruth.pretask.v7_4_brief import _compute_frame_scores
    from groundtruth.pretask.anchors import extract_issue_anchors

    repo_root, graph_db = frame_repo
    issue = "The bug is in `app/importer.py` — read_item mishandles None."
    anchors = extract_issue_anchors(issue, graph_db)
    scores = _compute_frame_scores(issue, repo_root, graph_db, anchors)
    assert scores.get("app/importer.py", 0.0) == pytest.approx(1.0)
    # The decoy was not mentioned -> no frame score.
    assert "app/importer_utils.py" not in scores


# ---------------------------------------------------------------- NEGATIVE CONTROL


def test_no_traceback_ranking_identical_to_prechange(frame_repo, monkeypatch):
    """MANDATORY no-regression: an issue with NO traceback / NO resolvable path
    yields a ranking byte-identical to the pre-change ranker.

    Pre-change behavior == frame component disabled (W_FRAME=0.0). If the frame
    component contributes 0 on no-traceback input, the default-weights run and
    the W_FRAME=0 run must produce the SAME full ranking (same paths, same order,
    same scores). Any divergence means the frame signal leaked a false boost into
    a task it must not touch.
    """
    from groundtruth.pretask.v7_4_brief import run_v74

    _patch_model(monkeypatch)
    repo_root, graph_db = frame_repo

    # Pure prose: a [question]-style issue with NO traceback and NO file path.
    issue = (
        "Question: how does the import flow handle a missing value? "
        "It seems like importing sometimes returns the wrong number."
    )

    kwargs = dict(
        issue_text=issue,
        repo_root=repo_root,
        graph_db=graph_db,
        bug_id="frame-neg",
        repo="test/repo",
        ablation="C",
    )

    with_frame = run_v74(**kwargs)              # default W_FRAME=0.60
    without_frame = run_v74(weights={"W_FRAME": 0.0}, **kwargs)  # pre-change

    seq_with = [(r["rank"], r["path"], r["score"]) for r in with_frame.ranked_full]
    seq_without = [(r["rank"], r["path"], r["score"]) for r in without_frame.ranked_full]

    assert seq_with == seq_without, (
        "no-traceback ranking diverged when the frame signal is enabled — "
        "the frame component must contribute 0 on no-traceback input.\n"
        f"with_frame   = {seq_with}\n"
        f"without_frame= {seq_without}"
    )

    # And every candidate's frame component is exactly 0 (the signal is inert).
    for r in with_frame.ranked_full:
        assert r["components"].get("frame", 0.0) == 0.0, (
            f"{r['path']} got a nonzero frame component on a no-traceback issue"
        )


def test_unresolvable_traceback_is_inert(frame_repo, monkeypatch):
    """A traceback that names only stdlib / out-of-repo files resolves to nothing,
    so the frame signal stays inert (no false boost) — correct-or-quiet."""
    from groundtruth.pretask.v7_4_brief import run_v74

    _patch_model(monkeypatch)
    repo_root, graph_db = frame_repo

    issue = textwrap.dedent(
        """
        Traceback (most recent call last):
          File "/usr/lib/python3.11/json/decoder.py", line 355, in raw_decode
            obj, end = self.scan_once(s, idx)
          File "/opt/venv/lib/python3.11/site-packages/requests/api.py", line 59, in request
            return session.request(method=method, url=url)
        ValueError: something
        """
    )

    kwargs = dict(
        issue_text=issue,
        repo_root=repo_root,
        graph_db=graph_db,
        bug_id="frame-unres",
        repo="test/repo",
        ablation="C",
    )
    with_frame = run_v74(**kwargs)
    without_frame = run_v74(weights={"W_FRAME": 0.0}, **kwargs)

    seq_with = [(r["rank"], r["path"], r["score"]) for r in with_frame.ranked_full]
    seq_without = [(r["rank"], r["path"], r["score"]) for r in without_frame.ranked_full]
    assert seq_with == seq_without, (
        "out-of-repo traceback must resolve to nothing (no in-repo frame), "
        "leaving the ranking identical to pre-change."
    )

"""Step 1b — anchors.py must stop dropping real short/domain symbols (the
190-word _STOPWORDS + len<5 _looks_like_natural_word poison) and instead drop
generic hubs by the repo's OWN specificity distribution (data-derived).

Red-before-green discriminators on a synthetic graph:
  * ``run``  — a real, distinctive symbol whose name is an old _STOPWORDS entry.
               OLD: dropped by the blocklist.   NEW: survives.
  * ``String`` — a real symbol but a massive in-degree HUB (not in the old list,
               so it leaked through). OLD: kept.   NEW: dropped as a generic hub.
  * ``compute_delta_table`` — distinctive, survives both (control).
  * ``the`` — pure English, never a graph node, dropped both (control).
"""
from __future__ import annotations

import sqlite3

from groundtruth.confidence import clear_cache
from groundtruth.pretask.anchors import extract_issue_anchors


def _mk_graph(path, defs, edges):
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT,"
        " signature TEXT, return_type TEXT, is_exported INT, is_test INT,"
        " language TEXT, parent_id INT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INT, target_id INT,"
        " type TEXT, source_line INT, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    ids = {}
    for i, (name, label, fpath) in enumerate(defs, 1):
        conn.execute(
            "INSERT INTO nodes(id,label,name,file_path,is_test,language)"
            " VALUES(?,?,?,?,0,'python')", (i, label, name, fpath))
        ids.setdefault(name, i)
    for src, dst in edges:
        conn.execute(
            "INSERT INTO edges(source_id,target_id,type,confidence)"
            " VALUES(?,?, 'CALLS', 1.0)", (ids.get(src, 0), ids.get(dst, 0)))
    conn.commit()
    conn.close()


def _build_repo(tmp_path):
    """A repo big enough for a meaningful P95 (>=20 def samples). 'String' is a
    HOMONYM (defined in many files) -> dropped; 'run' (an old _STOPWORDS entry) is
    uniquely defined -> survives. Hub-ness (in-degree) is NOT a drop signal: the
    genericness axis is definition-frequency (Aider; Step-2 finding #1)."""
    callers = [(f"caller_fn_{i}", "Function", f"c{i}.py") for i in range(22)]
    helpers = [(f"helper_{i}", "Method", f"h{i}.py") for i in range(1, 23)]  # 22
    homonym_string = [("String", "Class", f"pkg{i}/types.py") for i in range(8)]  # homonym
    distinctive = [
        ("compute_delta_table", "Function", "delta.py"),
        ("run", "Function", "cmd.py"),            # distinctive, uniquely defined
    ]
    defs = callers + helpers + homonym_string + distinctive
    edges = []
    for i in range(1, 23):                         # helper_i called by i distinct callers
        for j in range(i):
            edges.append((f"caller_fn_{j}", f"helper_{i}"))
    edges += [("compute_delta_table", "run")]       # run: distinctive, uniquely defined
    db = str(tmp_path / "graph.db")
    _mk_graph(db, defs, edges)
    return db


_ISSUE = (
    "Calling `run` raises an Error when `String` is empty. The fix belongs in "
    "compute_delta_table which should validate the manifest before dispatch."
)


def test_run_survives_and_hub_string_dropped(tmp_path):
    clear_cache()
    db = _build_repo(tmp_path)
    syms = extract_issue_anchors(_ISSUE, db).symbols
    assert "run" in syms, ("real distinctive symbol wrongly dropped", syms)
    assert "compute_delta_table" in syms, ("distinctive control dropped", syms)
    assert "String" not in syms, ("generic hub leaked into anchors", syms)
    assert "the" not in syms  # pure NL, never a node


def test_no_graph_keeps_nl_filtered_set(tmp_path):
    # With no DB (unit path), the NL pre-filter still applies but no hub-drop /
    # cross-check happens; real symbols survive, pure function words do not.
    clear_cache()
    a = extract_issue_anchors("run compute_delta_table the and with", None)
    # "run" is a prose-only common word (≤5 chars, lowercase, no underscore, not
    # backtick-wrapped) — intentionally demoted to prevent false graph witnesses
    # (flask-5637 class: "check" the verb → check() the function). "compute_delta_table"
    # survives (long, has underscore). NL function words ("the", "and") still dropped.
    assert "compute_delta_table" in a.symbols
    assert "run" not in a.symbols  # prose-demoted (short common word)
    assert "the" not in a.symbols and "and" not in a.symbols


if __name__ == "__main__":
    import tempfile, pathlib, traceback
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    p = f = 0
    for name, fn in tests:
        try:
            with tempfile.TemporaryDirectory() as d:
                fn(pathlib.Path(d))
            print(f"PASS {name}"); p += 1
        except Exception:
            print(f"FAIL {name}"); traceback.print_exc(); f += 1
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)

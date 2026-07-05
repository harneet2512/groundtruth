"""Fable LIPI fixes for the localizer (graph_localizer.py) — B-Finding2 + B-Finding1b."""
import inspect
import sqlite3

from groundtruth.delivery import path_policy
from groundtruth.pretask import graph_localizer as gl


def test_bfinding2_localizer_uses_canonical_test_predicate_not_divergent_substring():
    """B-Finding2: the non-source demote must classify test files via the CANONICAL segment-based
    predicate (path_policy.is_test_path, P11-respecting) — the same one the brief-render filter
    uses — NOT a divergent local substring predicate that matched 'testing/' and sank real source
    the render kept (Django `django/test`, Go `testing` helpers).

    Mutation: reintroducing a module-local `_is_test_file` (substring, incl. 'testing/') reddens
    the hasattr check — the re-divergence surface is back.
    """
    assert not hasattr(gl, "_is_test_file"), "the divergent local _is_test_file must be removed"
    assert gl._is_test_path_pp is path_policy.is_test_path, "demote must route through is_test_path"
    # P11: a production 'testing/' dir is NOT a test path; a genuine test dir still is.
    assert not path_policy.is_test_path("pkg/testing/helpers.py")
    assert path_policy.is_test_path("pkg/tests/test_foo.py")


def _build_100_symbol_db(tmp_path):
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported INT, is_test INT, language TEXT, parent_id INT)"
    )
    for i in range(1, 101):
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, signature, is_test, language) "
            "VALUES (?,?,?,?,?,0,'python')",
            (i, "Function", f"sym{i:03d}", "src/mod.py", f"def sym{i:03d}()"),
        )
    conn.commit()
    conn.close()
    return db


def test_bfinding1b_passage_cap_is_deterministic_first_80_by_id(tmp_path):
    """B-Finding1b: the per-file 80-passage cap selects a DETERMINISTIC set — ORDER BY id → the 80
    lowest-id symbols, in id order — so measure_brief is reproducible and the deferred L4(b)
    relevance-ordering is a clean follow-on. (SQLite's default scan is already id-order today for an
    INTEGER PRIMARY KEY, so this locks the contract as a GUARANTEE against future index/plan/schema
    changes; the structural companion below is the RED-provable half.)
    """
    db = _build_100_symbol_db(tmp_path)
    key = gl._normalize("src/mod.py")
    _passages, symnames = gl._assemble_symbol_passages(db, {key}, False, True)
    got = symnames[key]
    assert len(got) == 80, f"cap should keep exactly 80, got {len(got)}"
    assert got == [f"sym{i:03d}" for i in range(1, 81)], "cap must keep the 80 lowest-id symbols, in id order"


def test_bfinding1b_passage_cap_query_is_explicitly_ordered():
    """The determinism guarantee is structural: the passage-cap SELECT must be explicitly
    `ORDER BY id`, not rely on SQLite's default scan order.

    Mutation: dropping `ORDER BY id` from the SELECT reddens this.
    """
    src = inspect.getsource(gl._assemble_symbol_passages)
    # Match the SQL fragment specifically (the surrounding comment also mentions ORDER BY id).
    assert "is_test=0 ORDER BY id" in src, "the 80-cap SELECT must be deterministically ordered by id"

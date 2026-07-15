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


def test_bfinding1b_passage_cap_is_deterministic_first_80_by_source_identity(tmp_path):
    """The cap uses stable source identity, never graph insertion identity."""
    db = _build_100_symbol_db(tmp_path)
    key = gl._normalize("src/mod.py")
    _passages, symnames = gl._assemble_symbol_passages(db, {key}, False, True)
    got = symnames[key]
    assert len(got) == 80, f"cap should keep exactly 80, got {len(got)}"
    assert got == [f"sym{i:03d}" for i in range(1, 81)]


def test_bfinding1b_passage_cap_has_explicit_source_identity_sort():
    """Fetched rows are source-sorted before the bounded cut."""
    src = inspect.getsource(gl._assemble_symbol_passages)
    assert "_symbol_rows.sort" in src
    assert '_normalize(str(row[1] or ""))' in src
    assert "row[4] is None" in src
    assert 'str(row[2] or "")' in src

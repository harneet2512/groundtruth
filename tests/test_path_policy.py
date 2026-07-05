"""CP009 — centralized path_policy tests."""
import sqlite3

from groundtruth.delivery.path_policy import (
    is_delivery_excluded,
    is_generated,
    is_test_or_demo,
    is_test_path,
    is_test_tooling,
    is_vendored_path,
    test_tooling_roots as tooling_roots,
)


def test_vendor_paths_excluded():
    assert is_vendored_path("vendor/jquery/foo.min.js")
    assert is_vendored_path("node_modules/pkg/index.js")
    assert not is_vendored_path("src/distribute.py")


def test_generated_markers():
    assert is_generated("api/run_function.pb.go")
    assert not is_generated("src/main.py")


def test_tailwind_asset_excluded():
    assert is_delivery_excluded("static/tailwind.min.js")
    assert is_delivery_excluded("assets/tailwind.config.js")


def test_p11_production_ambiguous_dirs_are_not_test_or_demo():
    """Fable P11 (mirrors walker.go nonSourceDirSegments): `testing` / `compat` /
    `conformance` are PRODUCTION-ambiguous (Django/Go `testing` utilities, pandas/numpy
    `compat` shims, protobuf `conformance`) and must NOT be classified test/nonsource —
    else real source gold is demoted/excluded. Genuine test dirs still classify.

    Mutation check: re-adding any of the three to the segment sets → RED.
    """
    assert not is_test_path("pkg/testing/helpers.py"), "`testing/` is production-ambiguous"
    assert not is_test_or_demo("pandas/compat/numpy_.py"), "`compat/` is a production shim dir"
    assert not is_test_or_demo("proto/conformance/runner.py"), "`conformance/` is real source"
    # genuine test/demo dirs still classify (no over-removal)
    assert is_test_path("pkg/tests/test_foo.py")
    assert is_test_or_demo("examples/quickstart.py")


def test_test_imports_do_not_make_generic_source_root_tooling(tmp_path):
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            file_path TEXT
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            type TEXT
        );
        INSERT INTO nodes(id, file_path) VALUES
            (1, 'test/request.test.js'),
            (2, 'lib/utils.js'),
            (3, 'lib/request.js');
        INSERT INTO edges(source_id, target_id, type) VALUES
            (1, 2, 'IMPORTS'),
            (1, 3, 'IMPORTS');
        """
    )
    conn.close()

    roots = tooling_roots(str(db))

    assert "lib" not in roots
    assert not is_test_tooling("lib/utils.js", roots)


def test_test_support_root_can_be_tooling_when_imported_only_by_tests(tmp_path):
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY,
            file_path TEXT
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            type TEXT
        );
        INSERT INTO nodes(id, file_path) VALUES
            (1, 'test/request.test.js'),
            (2, 'test/support/utils.js');
        INSERT INTO edges(source_id, target_id, type) VALUES
            (1, 2, 'IMPORTS');
        """
    )
    conn.close()

    roots = tooling_roots(str(db))

    assert "test/support" in roots
    assert is_test_tooling("test/support/utils.js", roots)

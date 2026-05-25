"""Tests for GT_VERIFY confidence labeling."""
import os
import sqlite3
import tempfile

from groundtruth.hooks.post_edit import (
    _classify_test_target,
    _get_targeted_verification_suggestion,
)


class TestClassifyTestTarget:
    def test_real_test_file(self):
        assert _classify_test_target("tests/test_foo.py", "test_bar") == "real_test"

    def test_real_test_file_suffix(self):
        assert _classify_test_target("tests/foo_test.py", "test_bar") == "real_test"

    def test_conftest(self):
        assert _classify_test_target("tests/conftest.py", "isatty") == "conftest"

    def test_utility_file(self):
        assert _classify_test_target("test/tracing/utils.py", "trace") == "test_utility"

    def test_helpers_file(self):
        assert _classify_test_target("tests/helpers.py", "make_thing") == "test_utility"

    def test_common_file(self):
        assert _classify_test_target("tests/common.py", "setup") == "test_utility"

    def test_non_test_integration_fixture(self):
        assert _classify_test_target(
            "cdk_integration_tests/src/python/ALBDropHttpHeaders/fail__1__.py",
            "__init__",
        ) == "non_test"

    def test_non_test_random_file(self):
        assert _classify_test_target("src/app/main.py", "run") == "non_test"


class TestGetTargetedVerificationSuggestion:
    def _make_db(self, resolution_method="import", test_file="tests/test_foo.py",
                 test_name="test_bar", confidence=1.0):
        """Create a minimal graph.db with one source node, one test node, one edge."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute("""CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        )""")
        conn.execute("""CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL, metadata TEXT
        )""")
        # Source node
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) VALUES (1, 'Function', 'my_func', 'src/app.py', 0, 'python')"
        )
        # Test node
        conn.execute(
            f"INSERT INTO nodes (id, label, name, file_path, is_test, language) VALUES (2, 'Function', '{test_name}', '{test_file}', 1, 'python')"
        )
        # Edge
        conn.execute(
            f"INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) VALUES (2, 1, 'CALLS', '{resolution_method}', {confidence})"
        )
        conn.commit()
        conn.close()
        return tmp.name

    def test_high_confidence_real_test(self):
        db = self._make_db(resolution_method="import", test_file="tests/test_app.py", test_name="test_my_func")
        result = _get_targeted_verification_suggestion(db, "src/app.py", ["my_func"])
        assert "Run: pytest tests/test_app.py::test_my_func" in result
        os.unlink(db)

    def test_medium_confidence_name_match(self):
        db = self._make_db(resolution_method="name_match", test_file="tests/test_app.py", test_name="test_my_func", confidence=0.6)
        result = _get_targeted_verification_suggestion(db, "src/app.py", ["my_func"])
        assert "Run: pytest" in result
        os.unlink(db)

    def test_low_confidence_conftest(self):
        db = self._make_db(resolution_method="import", test_file="tests/conftest.py", test_name="isatty")
        result = _get_targeted_verification_suggestion(db, "src/app.py", ["my_func"])
        assert "Run: pytest" in result
        os.unlink(db)

    def test_low_confidence_utility(self):
        db = self._make_db(resolution_method="import", test_file="test/tracing/utils.py", test_name="trace")
        result = _get_targeted_verification_suggestion(db, "src/app.py", ["my_func"])
        assert "Run: pytest" in result
        os.unlink(db)

    def test_low_confidence_non_test(self):
        db = self._make_db(
            resolution_method="name_match",
            test_file="cdk_integration_tests/src/python/ALBDropHttpHeaders/fail__1__.py",
            test_name="__init__",
            confidence=0.3,
        )
        result = _get_targeted_verification_suggestion(db, "src/app.py", ["my_func"])
        # confidence=0.3 is below the SQL filter threshold (>= 0.5), so no result
        assert result == ""
        os.unlink(db)

    def test_no_test_found(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute("""CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        )""")
        conn.execute("""CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL, metadata TEXT
        )""")
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) VALUES (1, 'Function', 'my_func', 'src/app.py', 0, 'python')"
        )
        conn.commit()
        conn.close()
        result = _get_targeted_verification_suggestion(tmp.name, "src/app.py", ["my_func"])
        assert result == ""
        os.unlink(tmp.name)

    def test_low_edge_confidence(self):
        db = self._make_db(resolution_method="name_match", test_file="tests/test_app.py", test_name="test_my_func", confidence=0.3)
        result = _get_targeted_verification_suggestion(db, "src/app.py", ["my_func"])
        # confidence=0.3 is below the SQL filter threshold (>= 0.5), so no result
        assert result == ""
        os.unlink(db)

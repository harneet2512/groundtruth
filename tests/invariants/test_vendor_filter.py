"""Invariant 9: Vendor Exclusion

Vendor/static/minified JS files must never appear in caller evidence.
Patterns: /static/, /vendor/, /node_modules/, /dist/, .min., /assets/

Violation = D2 in failure taxonomy. ENGINEERING_INVARIANT.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from groundtruth.hooks.post_view import _is_vendor_path


class TestVendorPathDetection:
    """_is_vendor_path correctly identifies vendor files."""

    def test_static_jquery(self):
        assert _is_vendor_path("beetsplug/web/static/jquery.js")

    def test_node_modules(self):
        assert _is_vendor_path("node_modules/lodash/lodash.js")

    def test_minified(self):
        assert _is_vendor_path("assets/app.min.js")

    def test_vendor_dir(self):
        assert _is_vendor_path("vendor/lib/helper.js")

    def test_dist_dir(self):
        assert _is_vendor_path("dist/bundle.js")

    def test_assets_dir(self):
        assert _is_vendor_path("public/assets/vendor.js")

    def test_normal_python_not_vendor(self):
        assert not _is_vendor_path("beets/importer.py")

    def test_normal_js_not_vendor(self):
        assert not _is_vendor_path("src/components/App.js")

    def test_test_file_not_vendor(self):
        assert not _is_vendor_path("test/test_importer.py")

    def test_backslash_normalized(self):
        assert _is_vendor_path("beetsplug\\web\\static\\jquery.js")


class TestVendorFilterInGraphNavigation:
    """post_view.py graph_navigation() must exclude vendor callers."""

    def test_jquery_excluded_from_navigation(self):
        from groundtruth.hooks.post_view import graph_navigation

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
                file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
                signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
                is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
            )""")
            conn.execute("""CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER, target_id INTEGER, type TEXT,
                source_line INTEGER, source_file TEXT, resolution_method TEXT,
                confidence REAL DEFAULT 0.0, metadata TEXT,
                trust_tier TEXT DEFAULT 'SPECULATIVE',
                candidate_count INTEGER DEFAULT 1,
                evidence_type TEXT,
                verification_status TEXT DEFAULT 'unverified'
            )""")
            conn.execute("""CREATE TABLE properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER, kind TEXT, value TEXT, line INTEGER, confidence REAL DEFAULT 1.0
            )""")
            # Target
            conn.execute(
                "INSERT INTO nodes VALUES (1, 'Function', 'target', NULL, "
                "'src/main.py', 10, 20, NULL, NULL, 1, 0, 'python', NULL)"
            )
            # Vendor caller
            conn.execute(
                "INSERT INTO nodes VALUES (2, 'Function', 'each', NULL, "
                "'static/jquery.js', 100, 200, NULL, NULL, 1, 0, 'javascript', NULL)"
            )
            conn.execute("INSERT INTO edges VALUES (1, 2, 1, 'CALLS', 105, NULL, 'name_match', 0.9, NULL, NULL, NULL, NULL, NULL)")
            # Legit caller
            conn.execute(
                "INSERT INTO nodes VALUES (3, 'Function', 'caller', NULL, "
                "'src/utils.py', 50, 60, NULL, NULL, 1, 0, 'python', NULL)"
            )
            conn.execute("INSERT INTO edges VALUES (2, 3, 1, 'CALLS', 55, NULL, 'import', 1.0, NULL, NULL, NULL, NULL, NULL)")
            conn.commit()
            conn.close()

            lines, _ = graph_navigation("src/main.py", db_path, limit=5)
            output = "\n".join(lines)
            assert "jquery" not in output.lower(), f"Vendor JS in output: {output}"

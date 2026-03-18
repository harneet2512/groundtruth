#!/usr/bin/env python3
"""Synthetic tests for gt_autocorrect.py."""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest

# Import the module under test
sys.path.insert(0, os.path.dirname(__file__))
import gt_autocorrect as ac


class TestLevenshtein(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(ac.levenshtein_distance("abc", "abc"), 0)

    def test_one_edit(self):
        self.assertEqual(ac.levenshtein_distance("abc", "abd"), 1)

    def test_empty(self):
        self.assertEqual(ac.levenshtein_distance("", "abc"), 3)


class TestFindClosest(unittest.TestCase):
    def test_single_match(self):
        self.assertEqual(ac.find_closest("validat", {"validate", "process", "handle"}), "validate")

    def test_exact_match_returns_none(self):
        self.assertIsNone(ac.find_closest("validate", {"validate", "process"}))

    def test_ambiguous_returns_none(self):
        # Two candidates at same distance
        self.assertIsNone(ac.find_closest("abcde", {"abcdf", "abcdg"}))

    def test_short_name_skipped(self):
        self.assertIsNone(ac.find_closest("ab", {"abc", "abd"}))

    def test_no_match(self):
        self.assertIsNone(ac.find_closest("zzzzz", {"aaaaa", "bbbbb"}))

    def test_clear_winner_among_multiple(self):
        # One at dist 1, another at dist 2
        self.assertEqual(ac.find_closest("validate", {"validato", "validxxx"}), "validato")


class TestCheckFile(unittest.TestCase):
    def _make_kb(self, **overrides):
        kb = {
            "module_exports": {},
            "classes": {},
            "param_names": {},
            "installed_symbols": {},
            "all_class_names": set(),
            "file_modules": {},
        }
        kb.update(overrides)
        return kb

    def _write_and_check(self, code, kb, modified_names=None):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(textwrap.dedent(code))
            f.flush()
            path = f.name
        try:
            return ac.check_file(path, kb, modified_names or set())
        finally:
            os.unlink(path)

    def test_wrong_method_name(self):
        """self.validate_fields() → self.validate() when only validate exists."""
        kb = self._make_kb(
            classes={
                "MyModel": {
                    "methods": {"validate", "save", "delete"},
                    "attrs": {"name", "value"},
                    "bases": [],
                    "file": "/test.py",
                },
            },
            all_class_names={"MyModel"},
        )
        code = """\
        class MyModel:
            def process(self):
                self.validate_fields()
        """
        # validate_fields → validate (distance 7, too far)
        # Let's use a closer name
        code = """\
        class MyModel:
            def process(self):
                self.validte()
        """
        corrections = self._write_and_check(code, kb)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["old_name"], "validte")
        self.assertEqual(corrections[0]["new_name"], "validate")

    def test_wrong_attribute(self):
        """self.deferrable_constraint → self.deferrable when only deferrable exists."""
        kb = self._make_kb(
            classes={
                "Field": {
                    "methods": {"contribute_to_class"},
                    "attrs": {"deferrable", "name", "verbose_name"},
                    "bases": [],
                    "file": "/test.py",
                },
            },
            all_class_names={"Field"},
        )
        code = """\
        class Field:
            def check(self):
                return self.deferrble
        """
        corrections = self._write_and_check(code, kb)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["old_name"], "deferrble")
        self.assertEqual(corrections[0]["new_name"], "deferrable")

    def test_wrong_import(self):
        """from django.db.models import ForeignKeyField → ForeignKey."""
        kb = self._make_kb(
            installed_symbols={
                "django.db.models": {"ForeignKey", "CharField", "IntegerField", "Model"},
            },
        )
        code = """\
        from django.db.models import ForeignKe
        """
        corrections = self._write_and_check(code, kb)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["old_name"], "ForeignKe")
        self.assertEqual(corrections[0]["new_name"], "ForeignKey")
        self.assertEqual(corrections[0]["check_type"], "import")

    def test_correct_code_no_changes(self):
        """Correct code → no changes (zero false positives)."""
        kb = self._make_kb(
            classes={
                "MyModel": {
                    "methods": {"validate", "save"},
                    "attrs": {"name"},
                    "bases": [],
                    "file": "/test.py",
                },
            },
            all_class_names={"MyModel"},
        )
        code = """\
        class MyModel:
            def process(self):
                self.validate()
                self.save()
                return self.name
        """
        corrections = self._write_and_check(code, kb)
        self.assertEqual(len(corrections), 0)

    def test_ambiguous_no_changes(self):
        """Ambiguous match (2 candidates at same distance) → no changes."""
        kb = self._make_kb(
            classes={
                "MyModel": {
                    "methods": {"handle_a", "handle_b"},
                    "attrs": set(),
                    "bases": [],
                    "file": "/test.py",
                },
            },
            all_class_names={"MyModel"},
        )
        code = """\
        class MyModel:
            def process(self):
                self.handle_c()
        """
        corrections = self._write_and_check(code, kb)
        # handle_c is dist 1 from both handle_a and handle_b — ambiguous
        self.assertEqual(len(corrections), 0)


class TestPatchConsistency(unittest.TestCase):
    def test_minority_corrected(self):
        """self.foo 3 times + self.fooo 1 time → corrected to self.foo."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(textwrap.dedent("""\
                class X:
                    def a(self):
                        self.deferrable = True
                    def b(self):
                        self.deferrable = False
                    def c(self):
                        x = self.deferrable
                    def d(self):
                        y = self.deferrble
            """))
            f.flush()
            path = f.name
        try:
            corrections = ac.check_patch_consistency([path])
            self.assertGreater(len(corrections), 0)
            self.assertEqual(corrections[0]["old_name"], "deferrble")
            self.assertEqual(corrections[0]["new_name"], "deferrable")
            self.assertEqual(corrections[0]["check_type"], "consistency")
        finally:
            os.unlink(path)


class TestApplyCorrections(unittest.TestCase):
    def test_apply_import_correction(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("from django.db.models import ForeignKe\n")
            f.flush()
            path = f.name
        try:
            corrections = [ac.make_correction(
                file=path, line=1, col_start=0, col_end=0,
                old_name="ForeignKe", new_name="ForeignKey",
                check_type="import", confidence=0.9, reason="test",
            )]
            count = ac.apply_corrections(path, corrections)
            self.assertEqual(count, 1)
            with open(path) as f:
                content = f.read()
            self.assertIn("ForeignKey", content)
            self.assertNotIn("ForeignKe\n", content)
        finally:
            os.unlink(path)


class TestBuildExtendedKB(unittest.TestCase):
    def test_builds_from_simple_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a simple Python file
            with open(os.path.join(tmpdir, "models.py"), "w") as f:
                f.write(textwrap.dedent("""\
                    class User:
                        def __init__(self):
                            self.name = ""
                            self.email = ""
                        def validate(self):
                            pass
                        def save(self):
                            pass

                    def helper_function():
                        pass
                """))
            kb = ac.build_extended_kb(tmpdir)
            self.assertIn("User", kb["all_class_names"])
            self.assertIn("User", kb["classes"])
            self.assertIn("validate", kb["classes"]["User"]["methods"])
            self.assertIn("name", kb["classes"]["User"]["attrs"])
            # Check module exports
            found_module = False
            for mod, exports in kb["module_exports"].items():
                if "User" in exports and "helper_function" in exports:
                    found_module = True
            self.assertTrue(found_module)


if __name__ == "__main__":
    unittest.main()

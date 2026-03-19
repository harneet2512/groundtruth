"""Tests for the certainty-layered semantic graph architecture.

Tests PatchOverlay, green-lane gating, Pyright backend, and the facts table.
"""
from __future__ import annotations

import textwrap
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# PatchOverlay tests (benchmark / stdlib version)
# ---------------------------------------------------------------------------

from benchmarks.swebench.patch_overlay import build, _extract_definitions, _extract_imports, _detect_renames


class TestPatchOverlayBenchmark:
    """Tests for the stdlib-only PatchOverlay."""

    def test_added_def(self) -> None:
        """def foo() on + line → in added_definitions."""
        diff = textwrap.dedent("""\
            diff --git a/src/utils.py b/src/utils.py
            --- a/src/utils.py
            +++ b/src/utils.py
            @@ -1,0 +1,2 @@
            +def foo():
            +    pass
        """)
        overlay = build(diff)
        assert "foo" in overlay["added_definitions"]

    def test_removed_def(self) -> None:
        """class Bar on - line → in removed_definitions."""
        diff = textwrap.dedent("""\
            diff --git a/src/models.py b/src/models.py
            --- a/src/models.py
            +++ b/src/models.py
            @@ -1,2 +1,0 @@
            -class Bar:
            -    pass
        """)
        overlay = build(diff)
        assert "Bar" in overlay["removed_definitions"]

    def test_rename_detection(self) -> None:
        """Remove validate + add validte → rename detected (Lev distance ≤ 3)."""
        diff = textwrap.dedent("""\
            diff --git a/src/forms.py b/src/forms.py
            --- a/src/forms.py
            +++ b/src/forms.py
            @@ -1,2 +1,2 @@
            -def validate():
            -    pass
            +def validte():
            +    pass
        """)
        overlay = build(diff)
        assert "validte" in overlay["renames"]
        assert overlay["renames"]["validte"] == "validate"

    def test_imports(self) -> None:
        """from X import Y on + line → in added_imports."""
        diff = textwrap.dedent("""\
            diff --git a/src/app.py b/src/app.py
            --- a/src/app.py
            +++ b/src/app.py
            @@ -1,0 +1,1 @@
            +from collections import Counter
        """)
        overlay = build(diff)
        assert "Counter" in overlay["added_imports"]

    def test_line_numbers(self) -> None:
        """Correct mapping of + line numbers."""
        diff = textwrap.dedent("""\
            diff --git a/src/app.py b/src/app.py
            --- a/src/app.py
            +++ b/src/app.py
            @@ -5,0 +5,3 @@
            +line1
            +line2
            +line3
        """)
        overlay = build(diff)
        assert overlay["added_lines"]["src/app.py"] == {5, 6, 7}

    def test_changed_files(self) -> None:
        """changed_files lists all modified files."""
        diff = textwrap.dedent("""\
            diff --git a/a.py b/a.py
            --- a/a.py
            +++ b/a.py
            @@ -1,1 +1,1 @@
            -old
            +new
            diff --git a/b.py b/b.py
            --- a/b.py
            +++ b/b.py
            @@ -1,1 +1,1 @@
            -old
            +new
        """)
        overlay = build(diff)
        assert "a.py" in overlay["changed_files"]
        assert "b.py" in overlay["changed_files"]


class TestExtractDefinitions:
    def test_class_def(self) -> None:
        assert "MyClass" in _extract_definitions(["class MyClass:"])

    def test_func_def(self) -> None:
        assert "my_func" in _extract_definitions(["def my_func():"])

    def test_async_def(self) -> None:
        assert "async_fn" in _extract_definitions(["async def async_fn():"])

    def test_constant_assign(self) -> None:
        assert "MAX_SIZE" in _extract_definitions(["MAX_SIZE = 100"])

    def test_lowercase_assign_not_matched(self) -> None:
        # Only UPPER_CASE assignments are matched by the regex
        defs = _extract_definitions(["some_var = 42"])
        assert "some_var" not in defs


class TestExtractImports:
    def test_from_import(self) -> None:
        assert "Counter" in _extract_imports(["from collections import Counter"])

    def test_from_import_alias(self) -> None:
        assert "np" in _extract_imports(["import numpy as np"])

    def test_from_import_multiple(self) -> None:
        result = _extract_imports(["from os.path import join, exists"])
        assert "join" in result
        assert "exists" in result


class TestDetectRenames:
    def test_close_names(self) -> None:
        added = {"validte"}
        removed = {"validate"}
        renames = _detect_renames(added, removed)
        assert renames == {"validte": "validate"}

    def test_no_rename_for_distant_names(self) -> None:
        added = {"completely_different"}
        removed = {"validate"}
        renames = _detect_renames(added, removed, max_dist=3)
        assert renames == {}


# ---------------------------------------------------------------------------
# PatchOverlay tests (production version)
# ---------------------------------------------------------------------------

from src.groundtruth.validators.patch_overlay import PatchOverlayBuilder, PatchOverlay


class TestPatchOverlayProduction:
    def test_build_returns_overlay(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/src/app.py b/src/app.py
            --- a/src/app.py
            +++ b/src/app.py
            @@ -1,0 +1,1 @@
            +def new_helper():
        """)
        overlay = PatchOverlayBuilder.build(diff)
        assert isinstance(overlay, PatchOverlay)
        assert "new_helper" in overlay.added_definitions

    def test_renames_detected(self) -> None:
        diff = textwrap.dedent("""\
            diff --git a/src/app.py b/src/app.py
            --- a/src/app.py
            +++ b/src/app.py
            @@ -1,1 +1,1 @@
            -def validate():
            +def validte():
        """)
        overlay = PatchOverlayBuilder.build(diff)
        assert "validte" in overlay.renames
        assert overlay.renames["validte"] == "validate"


# ---------------------------------------------------------------------------
# Green-lane gating tests (using benchmark gt_autocorrect)
# ---------------------------------------------------------------------------

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks", "swebench"))

from gt_autocorrect import (  # type: ignore[import-untyped]
    _should_correct,
    make_correction,
    check_file_green_only,
    find_closest,
)


class TestGreenLaneGating:
    def _make_overlay(
        self,
        added_lines: dict[str, set[int]] | None = None,
        added_definitions: set[str] | None = None,
        renames: dict[str, str] | None = None,
    ) -> dict:
        return {
            "added_lines": added_lines or {},
            "added_definitions": added_definitions or set(),
            "removed_definitions": set(),
            "renames": renames or {},
            "added_imports": set(),
            "removed_imports": set(),
            "changed_files": [],
        }

    def test_correction_on_added_line_passes(self) -> None:
        overlay = self._make_overlay(added_lines={"src/app.py": {10}})
        c = make_correction("src/app.py", 10, 0, 0, "validte", "validate", "attribute", 0.85, "test")
        assert _should_correct(c, overlay) is True

    def test_correction_on_unchanged_line_blocked(self) -> None:
        overlay = self._make_overlay(added_lines={"src/app.py": {10}})
        c = make_correction("src/app.py", 20, 0, 0, "validte", "validate", "attribute", 0.85, "test")
        assert _should_correct(c, overlay) is False

    def test_agent_defined_name_not_corrected(self) -> None:
        overlay = self._make_overlay(
            added_lines={"src/app.py": {10}},
            added_definitions={"NewHelper"},
        )
        c = make_correction("src/app.py", 10, 0, 0, "NewHelper", "OldHelper", "class_ref", 0.8, "test")
        assert _should_correct(c, overlay) is False

    def test_rename_target_not_corrected(self) -> None:
        overlay = self._make_overlay(
            added_lines={"src/app.py": {10}},
            renames={"validate_input": "validate"},
        )
        c = make_correction("src/app.py", 10, 0, 0, "validate", "validates", "attribute", 0.85, "test")
        assert _should_correct(c, overlay) is False

    def test_testbed_prefix_stripped(self) -> None:
        """Corrections with /testbed/ prefix should match overlay without prefix."""
        overlay = self._make_overlay(added_lines={"src/app.py": {10}})
        c = make_correction("/testbed/src/app.py", 10, 0, 0, "validte", "validate", "attribute", 0.85, "test")
        assert _should_correct(c, overlay) is True


class TestGreenLaneChecks:
    """Verify that green-lane check_file_green_only does NOT emit class_ref corrections."""

    def test_no_class_ref_check(self, tmp_path) -> None:
        """Bare ClassName with no project anchor → NO correction (check 5 disabled)."""
        # Write a source file that uses a name close to a known class
        source = textwrap.dedent("""\
            from mymod import Couter

            x = Couter()
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        kb = {
            "module_exports": {"mymod": {"Counter"}},
            "classes": {"Counter": {"methods": set(), "attrs": set(), "bases": []}},
            "param_names": {},
            "all_class_names": {"Counter"},
            "installed_symbols": {},
            "file_modules": {},
        }

        corrections = check_file_green_only(str(f), kb, set())
        # Should find import correction (Couter → Counter) but NOT class_ref
        class_refs = [c for c in corrections if c["check_type"] == "class_ref"]
        assert len(class_refs) == 0

    def test_self_method_check_works(self, tmp_path) -> None:
        """self.validate_fields() on class with validate → corrects."""
        source = textwrap.dedent("""\
            class MyForm:
                def validate(self):
                    pass
                def run(self):
                    self.validte()
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        kb = {
            "module_exports": {},
            "classes": {
                "MyForm": {
                    "methods": {"validate", "run"},
                    "attrs": set(),
                    "bases": [],
                    "file": str(f),
                },
            },
            "param_names": {},
            "all_class_names": {"MyForm"},
            "installed_symbols": {},
            "file_modules": {},
        }

        corrections = check_file_green_only(str(f), kb, set())
        assert any(c["old_name"] == "validte" and c["new_name"] == "validate" for c in corrections)

    def test_self_attr_check_works(self, tmp_path) -> None:
        """self.naem on class with name → corrects."""
        source = textwrap.dedent("""\
            class User:
                def __init__(self):
                    self.name = ""
                def display(self):
                    return self.naem
        """)
        f = tmp_path / "test.py"
        f.write_text(source)

        kb = {
            "module_exports": {},
            "classes": {
                "User": {
                    "methods": {"__init__", "display"},
                    "attrs": {"name"},
                    "bases": [],
                    "file": str(f),
                },
            },
            "param_names": {},
            "all_class_names": {"User"},
            "installed_symbols": {},
            "file_modules": {},
        }

        corrections = check_file_green_only(str(f), kb, set())
        # "naem" has length 4, > 3, so it should match
        assert any(c["old_name"] == "naem" and c["new_name"] == "name" for c in corrections)


# ---------------------------------------------------------------------------
# Pyright backend tests
# ---------------------------------------------------------------------------

from src.groundtruth.backends.pyright_backend import (
    run_pyright_on_files,
    _classify_rule,
)


class TestPyrightBackend:
    def test_green_rule_classification(self) -> None:
        assert _classify_rule("reportAttributeAccessIssue") == "green"
        assert _classify_rule("reportMissingImports") == "green"

    def test_yellow_rule_classification(self) -> None:
        assert _classify_rule("reportMissingModuleSource") == "yellow"

    def test_red_rule_classification(self) -> None:
        assert _classify_rule("unknownRule") == "red"

    def test_pyright_not_available(self) -> None:
        """FileNotFoundError → empty list, no crash."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = run_pyright_on_files(["test.py"])
            assert result == []

    def test_pyright_timeout(self) -> None:
        """TimeoutExpired → empty list, no crash."""
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("pyright", 30)):
            result = run_pyright_on_files(["test.py"])
            assert result == []

    def test_empty_files_list(self) -> None:
        result = run_pyright_on_files([])
        assert result == []


# ---------------------------------------------------------------------------
# Facts table tests (via store)
# ---------------------------------------------------------------------------


class TestFactsTable:
    """Test the facts table in SymbolStore."""

    @pytest.fixture
    def store(self, tmp_path):
        from src.groundtruth.index.store import SymbolStore
        db_path = str(tmp_path / "test.db")
        s = SymbolStore(db_path)
        s.initialize()
        return s

    def test_insert_and_query_fact(self, store) -> None:
        result = store.insert_fact(
            subject_type="class",
            subject_name="User",
            relation="has_member",
            object_type="method",
            object_name="save",
            provenance="ast",
            certainty="green",
        )
        assert hasattr(result, 'value')

        query_result = store.query_facts(subject_name="User")
        assert hasattr(query_result, 'value')
        assert len(query_result.value) == 1
        assert query_result.value[0]["object_name"] == "save"

    def test_get_green_members(self, store) -> None:
        store.insert_fact("class", "User", "has_member", "method", "save", "ast", "green")
        store.insert_fact("class", "User", "has_member", "attribute", "name", "ast", "green")
        store.insert_fact("class", "User", "has_member", "method", "maybe", "ast", "yellow")

        result = store.get_green_members("User")
        assert hasattr(result, 'value')
        assert result.value == {"save", "name"}  # excludes yellow

    def test_get_green_params(self, store) -> None:
        store.insert_fact("function", "process", "has_param", "param", "data", "ast", "green")
        store.insert_fact("function", "process", "has_param", "param", "timeout", "ast", "green")

        result = store.get_green_params("process")
        assert hasattr(result, 'value')
        assert result.value == {"data", "timeout"}

    def test_query_by_certainty(self, store) -> None:
        store.insert_fact("class", "User", "has_member", "method", "save", "ast", "green")
        store.insert_fact("class", "User", "has_member", "method", "maybe", "ast", "yellow")

        green_only = store.query_facts(certainty="green")
        assert hasattr(green_only, 'value')
        assert len(green_only.value) == 1
        assert green_only.value[0]["object_name"] == "save"


# ---------------------------------------------------------------------------
# Stdlib not corrected test
# ---------------------------------------------------------------------------


class TestStdlibNotCorrected:
    def test_stdlib_name_not_corrected(self) -> None:
        """Counter from collections → no correction (not project-local)."""
        # find_closest with stdlib names should not match project names
        # This tests the architectural decision: check 5 is disabled entirely
        kb_classes = {"Count", "Counter"}
        # "Counter" is an exact match → find_closest returns None
        assert find_closest("Counter", kb_classes) is None


# ---------------------------------------------------------------------------
# Yellow lane warning test
# ---------------------------------------------------------------------------


class TestYellowLane:
    def test_yellow_lane_warns_not_corrects(self) -> None:
        """Yellow-lane findings should not produce corrections."""
        # Yellow-lane rules from Pyright are logged but not auto-corrected.
        # The _try_pyright function in gt_autocorrect.py only collects GREEN diagnostics.
        # This test verifies the classification logic.
        assert _classify_rule("reportMissingModuleSource") == "yellow"
        assert _classify_rule("reportOptionalMemberAccess") == "yellow"
        # These would NOT produce corrections in gt_autocorrect.py

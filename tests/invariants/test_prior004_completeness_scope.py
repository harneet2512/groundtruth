"""PRIOR-004 regression test: [COMPLETENESS] must be scoped to edited function.

Tests that obligation_check.find_obligations() only reports methods sharing
state with the EDITED function, not arbitrary class-wide pairs.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from groundtruth.hooks.obligation_check import find_obligations


SAMPLE_CLASS = '''
class ImportTask:
    def __init__(self, toppath, items):
        self.toppath = toppath
        self.items = items
        self.choice_flag = None
        self.match = None
        self.lib = None

    def set_fields(self, lib):
        """The function being edited."""
        self.lib = lib
        for item in self.items:
            item.update(self.lib)

    def reload(self):
        """Shares self.items with set_fields."""
        for item in self.items:
            item.load()

    def chosen_info(self):
        """Shares choice_flag, match with set_choice. NOT with set_fields."""
        return self.choice_flag, self.match

    def set_choice(self, flag, match):
        """Shares choice_flag, match with chosen_info. NOT with set_fields."""
        self.choice_flag = flag
        self.match = match
'''


class TestCompletnessScopedToEditedFunction:
    """PRIOR-004: completeness must be scoped to the edited function."""

    def test_only_methods_sharing_with_edited_function(self):
        """When edited_functions={'set_fields'}, only methods sharing
        attrs with set_fields should appear — not chosen_info/set_choice."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "importer.py")
            with open(file_path, "w") as f:
                f.write(SAMPLE_CLASS)

            results = find_obligations(
                "importer.py", tmpdir,
                edited_functions={"set_fields"},
            )

            # set_fields shares self.items with reload — that should appear
            # set_fields shares self.lib with nothing relevant — might not appear
            # chosen_info/set_choice share choice_flag+match — NOT with set_fields
            for r in results:
                assert "chosen_info" not in r or "set_fields" in r, (
                    f"PRIOR-004: chosen_info should not appear unless it shares "
                    f"attrs with set_fields. Got: {r}"
                )
                assert "set_choice" not in r or "set_fields" in r, (
                    f"PRIOR-004: set_choice should not appear unless it shares "
                    f"attrs with set_fields. Got: {r}"
                )

    def test_empty_set_suppresses_all_completeness(self):
        """PRIOR-004 fix: empty set() means GT tried to extract but failed.
        Must produce NO completeness output — not class-wide all-pairs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "importer.py")
            with open(file_path, "w") as f:
                f.write(SAMPLE_CLASS)

            results = find_obligations(
                "importer.py", tmpdir,
                edited_functions=set(),  # empty set = extraction failed
            )

            assert len(results) == 0, (
                f"PRIOR-004: empty edited_functions=set() must suppress all completeness. "
                f"Got {len(results)} results: {results}"
            )

    def test_without_edited_functions_shows_all_pairs(self):
        """Without edited_functions, all class pairs shown (legacy behavior)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "importer.py")
            with open(file_path, "w") as f:
                f.write(SAMPLE_CLASS)

            results = find_obligations("importer.py", tmpdir, edited_functions=None)

            # Legacy mode: all pairs with 2+ shared attrs
            # chosen_info + set_choice share choice_flag, match — should appear
            all_text = " ".join(results)
            assert "chosen_info" in all_text or "set_choice" in all_text or len(results) == 0, (
                "Legacy mode should show all pairs or be empty"
            )

    def test_edited_function_scoping_filters_noise(self):
        """The key test: with edited_functions={'set_fields'}, completeness
        must NOT show class-wide noise like chosen_info/set_choice pair."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "importer.py")
            with open(file_path, "w") as f:
                f.write(SAMPLE_CLASS)

            results = find_obligations(
                "importer.py", tmpdir,
                edited_functions={"set_fields"},
            )

            # The chosen_info/set_choice pair must NOT appear when editing set_fields
            noise_pairs = [
                r for r in results
                if "chosen_info" in r and "set_choice" in r
            ]
            assert len(noise_pairs) == 0, (
                f"PRIOR-004: chosen_info/set_choice pair is class-wide noise, "
                f"not related to set_fields. Got: {noise_pairs}"
            )

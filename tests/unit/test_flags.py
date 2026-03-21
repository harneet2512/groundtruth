"""Tests for feature flag infrastructure."""

from __future__ import annotations

import os
from unittest.mock import patch

from groundtruth.core.flags import (
    abstention_enabled,
    communication_enabled,
    content_hash_enabled,
    contradictions_enabled,
    convention_fingerprint_enabled,
    is_enabled,
    repo_intel_enabled,
    state_flow_enabled,
)


class TestIsEnabled:
    """Test the generic is_enabled function."""

    def test_default_off(self) -> None:
        """Unset env var returns False."""
        with patch.dict(os.environ, {}, clear=True):
            assert is_enabled("NONEXISTENT_FLAG") is False

    def test_enabled_with_1(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "1"}):
            assert is_enabled("FOO") is True

    def test_enabled_with_true(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "true"}):
            assert is_enabled("FOO") is True

    def test_enabled_with_yes(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "yes"}):
            assert is_enabled("FOO") is True

    def test_case_insensitive_flag_name(self) -> None:
        """Flag name is uppercased automatically."""
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "1"}):
            assert is_enabled("foo") is True
            assert is_enabled("Foo") is True

    def test_case_insensitive_value(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "True"}):
            assert is_enabled("FOO") is True
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "YES"}):
            assert is_enabled("FOO") is True

    def test_disabled_with_0(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "0"}):
            assert is_enabled("FOO") is False

    def test_disabled_with_empty(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": ""}):
            assert is_enabled("FOO") is False

    def test_disabled_with_random_string(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "maybe"}):
            assert is_enabled("FOO") is False


class TestNamedAccessors:
    """Test that each named accessor reads the correct env var."""

    def test_contradictions(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_CONTRADICTIONS": "1"}):
            assert contradictions_enabled() is True
        with patch.dict(os.environ, {}, clear=True):
            assert contradictions_enabled() is False

    def test_abstention(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_ABSTENTION": "1"}):
            assert abstention_enabled() is True

    def test_communication(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_COMMUNICATION": "1"}):
            assert communication_enabled() is True

    def test_state_flow(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_STATE_FLOW": "1"}):
            assert state_flow_enabled() is True

    def test_convention_fingerprint(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_CONVENTION_FINGERPRINT": "1"}):
            assert convention_fingerprint_enabled() is True

    def test_content_hash(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_CONTENT_HASH": "1"}):
            assert content_hash_enabled() is True

    def test_repo_intel(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}):
            assert repo_intel_enabled() is True

    def test_all_default_off(self) -> None:
        """All flags default to OFF when no env vars set."""
        with patch.dict(os.environ, {}, clear=True):
            assert contradictions_enabled() is False
            assert abstention_enabled() is False
            assert communication_enabled() is False
            assert state_flow_enabled() is False
            assert convention_fingerprint_enabled() is False
            assert content_hash_enabled() is False
            assert repo_intel_enabled() is False

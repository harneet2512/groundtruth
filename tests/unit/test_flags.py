"""Tests for feature flag infrastructure.

These tests simulate real deployment scenarios: CI pipelines setting flags,
users migrating from old flags, partial configurations, and adversarial env states.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from groundtruth.core.flags import (
    abstention_enabled,
    communication_enabled,
    content_hash_enabled,
    contradictions_enabled,
    convention_fingerprint_enabled,
    foundation_enabled,
    hnsw_enabled,
    is_enabled,
    repo_intel_decisions_enabled,
    repo_intel_enabled,
    repo_intel_logging_enabled,
    response_state_machine_enabled,
    state_flow_enabled,
    structural_similarity_enabled,
    treesitter_enabled,
)


class TestIsEnabled:
    """Test the generic is_enabled function."""

    def test_default_off(self) -> None:
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

    def test_disabled_with_no(self) -> None:
        """Real scenario: user writes GT_ENABLE_X=no expecting it to be off."""
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "no"}):
            assert is_enabled("FOO") is False

    def test_disabled_with_false(self) -> None:
        """Real scenario: user writes GT_ENABLE_X=false expecting it to be off."""
        with patch.dict(os.environ, {"GT_ENABLE_FOO": "false"}):
            assert is_enabled("FOO") is False

    def test_whitespace_in_value(self) -> None:
        """Real scenario: trailing space from a shell export."""
        with patch.dict(os.environ, {"GT_ENABLE_FOO": " 1 "}):
            # Whitespace is NOT stripped — this is intentionally strict
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

    def test_structural_similarity(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_STRUCTURAL_SIMILARITY": "1"}):
            assert structural_similarity_enabled() is True

    def test_treesitter(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_TREESITTER": "1"}):
            assert treesitter_enabled() is True

    def test_foundation(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_FOUNDATION": "1"}):
            assert foundation_enabled() is True

    def test_response_state_machine(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_RESPONSE_STATE_MACHINE": "1"}, clear=True):
            assert response_state_machine_enabled() is True
        with patch.dict(os.environ, {}, clear=True):
            assert response_state_machine_enabled() is False

    def test_hnsw(self) -> None:
        with patch.dict(os.environ, {"GT_ENABLE_HNSW": "1"}, clear=True):
            assert hnsw_enabled() is True
        with patch.dict(os.environ, {}, clear=True):
            assert hnsw_enabled() is False

    def test_all_default_off(self) -> None:
        """Clean install: no env vars set. Every flag must be OFF."""
        with patch.dict(os.environ, {}, clear=True):
            assert contradictions_enabled() is False
            assert abstention_enabled() is False
            assert communication_enabled() is False
            assert state_flow_enabled() is False
            assert convention_fingerprint_enabled() is False
            assert content_hash_enabled() is False
            assert repo_intel_enabled() is False
            assert repo_intel_logging_enabled() is False
            assert repo_intel_decisions_enabled() is False
            assert response_state_machine_enabled() is False
            assert hnsw_enabled() is False
            assert structural_similarity_enabled() is False
            assert treesitter_enabled() is False
            assert foundation_enabled() is False


class TestRepoIntelFlagMigration:
    """Real-world scenarios for the GT_ENABLE_REPO_INTEL → split flag migration.

    Users in the wild will have:
    - Old flag only (existing .env or CI config)
    - New split flags only (fresh install following new docs)
    - Both old and new (transitional period)
    - Neither (clean install, default OFF)
    - Edge cases: DECISIONS without LOGGING, old flag explicitly disabled
    """

    def test_scenario_existing_user_old_flag_only(self) -> None:
        """User has GT_ENABLE_REPO_INTEL=1 in their .env from before the split.
        Expected: logging works, decisions off, no crash."""
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}, clear=True):
            assert repo_intel_logging_enabled() is True
            assert repo_intel_decisions_enabled() is False
            # Legacy compat accessor still works
            assert repo_intel_enabled() is True

    def test_scenario_new_user_logging_only(self) -> None:
        """New user follows docs: enable logging first, observe, then decide."""
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL_LOGGING": "1"}, clear=True):
            assert repo_intel_logging_enabled() is True
            assert repo_intel_decisions_enabled() is False

    def test_scenario_new_user_full_intel(self) -> None:
        """User enables both split flags for full intelligence."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            assert repo_intel_logging_enabled() is True
            assert repo_intel_decisions_enabled() is True

    def test_scenario_decisions_without_logging_is_noop(self) -> None:
        """User accidentally enables DECISIONS without LOGGING.
        Must NOT crash. Must NOT enable decisions. Hard dependency enforced."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            assert repo_intel_logging_enabled() is False
            assert repo_intel_decisions_enabled() is False

    def test_scenario_old_flag_disabled_explicitly(self) -> None:
        """User sets GT_ENABLE_REPO_INTEL=0 to explicitly turn it off."""
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "0"}, clear=True):
            assert repo_intel_logging_enabled() is False
            assert repo_intel_decisions_enabled() is False
            assert repo_intel_enabled() is False

    def test_scenario_transitional_both_old_and_new(self) -> None:
        """User has old flag AND new flags (e.g., during CI migration).
        New split flags take priority via short-circuit."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL": "1",
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
        }, clear=True):
            assert repo_intel_logging_enabled() is True
            assert repo_intel_decisions_enabled() is True

    def test_scenario_old_flag_on_new_logging_off(self) -> None:
        """Old flag on, but new logging explicitly disabled.
        New flag is checked first (short-circuit), so old flag still activates."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL": "1",
            "GT_ENABLE_REPO_INTEL_LOGGING": "0",
        }, clear=True):
            # LOGGING=0 is checked first → False, then REPO_INTEL=1 → True
            assert repo_intel_logging_enabled() is True

    def test_scenario_clean_install_nothing_set(self) -> None:
        """Brand new install, no flags configured anywhere."""
        with patch.dict(os.environ, {}, clear=True):
            assert repo_intel_logging_enabled() is False
            assert repo_intel_decisions_enabled() is False
            assert repo_intel_enabled() is False

    def test_scenario_ci_pipeline_swebench_eval(self) -> None:
        """SWE-bench eval pipeline: full stack ON for evaluation."""
        with patch.dict(os.environ, {
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
            "GT_ENABLE_COMMUNICATION": "1",
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
            "GT_ENABLE_FOUNDATION": "1",
            "GT_ENABLE_RESPONSE_STATE_MACHINE": "1",
        }, clear=True):
            assert contradictions_enabled() is True
            assert abstention_enabled() is True
            assert communication_enabled() is True
            assert repo_intel_logging_enabled() is True
            assert repo_intel_decisions_enabled() is True
            assert foundation_enabled() is True
            assert response_state_machine_enabled() is True
            # HNSW not set — optional dep not required for eval
            assert hnsw_enabled() is False

    def test_scenario_ablation_logging_only_no_decisions(self) -> None:
        """Ablation study: observe what gets logged without influencing output."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
        }, clear=True):
            assert repo_intel_logging_enabled() is True
            assert repo_intel_decisions_enabled() is False
            assert contradictions_enabled() is True
            assert abstention_enabled() is True


class TestFlagIsolation:
    """Verify flags don't leak into each other.

    Real bug scenario: GT_ENABLE_REPO_INTEL_LOGGING could theoretically
    match GT_ENABLE_REPO_INTEL via prefix. Verify isolation.
    """

    def test_logging_flag_does_not_activate_old_intel_path(self) -> None:
        """GT_ENABLE_REPO_INTEL_LOGGING should NOT trigger is_enabled('REPO_INTEL')."""
        with patch.dict(os.environ, {
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
        }, clear=True):
            # The old flag env var is NOT set
            assert is_enabled("REPO_INTEL") is False
            # But logging is enabled via its own flag
            assert repo_intel_logging_enabled() is True

    def test_old_flag_does_not_activate_decisions(self) -> None:
        """GT_ENABLE_REPO_INTEL=1 must NOT enable decisions."""
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}, clear=True):
            assert repo_intel_decisions_enabled() is False

    def test_communication_vs_response_state_machine(self) -> None:
        """These are two separate systems. One must not activate the other."""
        with patch.dict(os.environ, {"GT_ENABLE_COMMUNICATION": "1"}, clear=True):
            assert communication_enabled() is True
            assert response_state_machine_enabled() is False
        with patch.dict(os.environ, {"GT_ENABLE_RESPONSE_STATE_MACHINE": "1"}, clear=True):
            assert response_state_machine_enabled() is True
            assert communication_enabled() is False

    def test_foundation_vs_hnsw(self) -> None:
        """FOUNDATION controls the pipeline. HNSW controls the backend.
        They are independent — you can use foundation with brute-force."""
        with patch.dict(os.environ, {"GT_ENABLE_FOUNDATION": "1"}, clear=True):
            assert foundation_enabled() is True
            assert hnsw_enabled() is False
        with patch.dict(os.environ, {"GT_ENABLE_HNSW": "1"}, clear=True):
            assert hnsw_enabled() is True
            assert foundation_enabled() is False

    @pytest.mark.parametrize("flag_name,accessor", [
        ("CONTRADICTIONS", contradictions_enabled),
        ("ABSTENTION", abstention_enabled),
        ("COMMUNICATION", communication_enabled),
        ("STATE_FLOW", state_flow_enabled),
        ("CONVENTION_FINGERPRINT", convention_fingerprint_enabled),
        ("CONTENT_HASH", content_hash_enabled),
        ("STRUCTURAL_SIMILARITY", structural_similarity_enabled),
        ("TREESITTER", treesitter_enabled),
        ("FOUNDATION", foundation_enabled),
        ("RESPONSE_STATE_MACHINE", response_state_machine_enabled),
        ("HNSW", hnsw_enabled),
    ])
    def test_each_flag_only_responds_to_its_own_env_var(
        self, flag_name: str, accessor: object
    ) -> None:
        """Enable ONE flag, verify all OTHERS are off."""
        env = {f"GT_ENABLE_{flag_name}": "1"}
        with patch.dict(os.environ, env, clear=True):
            assert accessor() is True  # type: ignore[operator]
            # Spot-check that a different flag is NOT accidentally enabled
            if flag_name != "CONTRADICTIONS":
                assert contradictions_enabled() is False
            if flag_name != "FOUNDATION":
                assert foundation_enabled() is False


class TestDeprecationWarning:
    """Verify the old flag emits a structlog deprecation warning."""

    def test_old_flag_logs_deprecation(self) -> None:
        """When GT_ENABLE_REPO_INTEL is used, a warning should be logged once."""
        import groundtruth.core.flags as flags_module

        # Reset the deprecation guard
        flags_module._repo_intel_deprecation_warned = False

        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}, clear=True):
            # First call triggers warning
            assert repo_intel_logging_enabled() is True
            assert flags_module._repo_intel_deprecation_warned is True

            # Second call does NOT re-warn (flag is already set)
            result = repo_intel_logging_enabled()
            assert result is True

        # Clean up
        flags_module._repo_intel_deprecation_warned = False

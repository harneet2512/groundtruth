"""Tests for ablation configuration.

Real scenarios: eval pipeline configs, partial rollouts, flag migration compat,
configuration presets matching actual ablation study designs.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from groundtruth.core.ablation import CONFIGURATIONS, AblationConfig


class TestAblationConfig:
    def test_from_env_clean_install(self) -> None:
        """Brand new install — every field must be False."""
        with patch.dict(os.environ, {}, clear=True):
            config = AblationConfig.from_env()
        assert config.contradictions is False
        assert config.abstention is False
        assert config.communication is False
        assert config.state_flow is False
        assert config.convention_fingerprint is False
        assert config.content_hash is False
        assert config.repo_intel_logging is False
        assert config.repo_intel_decisions is False
        assert config.structural_similarity is False
        assert config.response_state_machine is False
        assert config.hnsw is False
        assert config.any_enabled() is False

    def test_from_env_swebench_eval_config(self) -> None:
        """Real eval pipeline: full stack minus HNSW (optional dep)."""
        with patch.dict(os.environ, {
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
            "GT_ENABLE_COMMUNICATION": "1",
            "GT_ENABLE_STATE_FLOW": "1",
            "GT_ENABLE_CONVENTION_FINGERPRINT": "1",
            "GT_ENABLE_CONTENT_HASH": "1",
            "GT_ENABLE_REPO_INTEL_LOGGING": "1",
            "GT_ENABLE_REPO_INTEL_DECISIONS": "1",
            "GT_ENABLE_STRUCTURAL_SIMILARITY": "1",
            "GT_ENABLE_RESPONSE_STATE_MACHINE": "1",
        }, clear=True):
            config = AblationConfig.from_env()
        assert config.contradictions is True
        assert config.repo_intel_logging is True
        assert config.repo_intel_decisions is True
        assert config.response_state_machine is True
        assert config.hnsw is False  # not set — no hnswlib in eval image
        assert config.any_enabled() is True

    def test_from_env_old_flag_compat(self) -> None:
        """Existing user has GT_ENABLE_REPO_INTEL=1 — should map to logging."""
        with patch.dict(os.environ, {"GT_ENABLE_REPO_INTEL": "1"}, clear=True):
            config = AblationConfig.from_env()
        assert config.repo_intel_logging is True
        assert config.repo_intel_decisions is False

    def test_from_env_precision_trust_ablation(self) -> None:
        """Ablation study: contradictions + abstention only.
        Measures whether trust gating alone reduces false positives."""
        with patch.dict(os.environ, {
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
        }, clear=True):
            config = AblationConfig.from_env()
        assert config.contradictions is True
        assert config.abstention is True
        assert config.communication is False
        assert config.repo_intel_logging is False
        desc = config.describe()
        enabled_count = sum(1 for v in desc.values() if v)
        assert enabled_count == 2

    def test_describe_returns_all_fields(self) -> None:
        config = AblationConfig(
            contradictions=True,
            abstention=False,
            communication=True,
            state_flow=False,
            convention_fingerprint=False,
            content_hash=True,
            repo_intel_logging=False,
            repo_intel_decisions=False,
            structural_similarity=False,
            response_state_machine=False,
            hnsw=False,
        )
        desc = config.describe()
        assert desc["contradictions"] is True
        assert desc["abstention"] is False
        assert desc["communication"] is True
        assert desc["content_hash"] is True
        assert len(desc) == 11
        # Verify every AblationConfig field has a describe() entry
        for key in desc:
            assert hasattr(config, key), f"describe() key '{key}' not in dataclass"

    def test_describe_keys_match_dataclass_fields(self) -> None:
        """Guard against describe() drifting from the dataclass fields."""
        import dataclasses
        config = AblationConfig.from_env()
        field_names = {f.name for f in dataclasses.fields(config)}
        desc_keys = set(config.describe().keys())
        assert field_names == desc_keys, (
            f"Mismatch: fields={field_names - desc_keys}, desc={desc_keys - field_names}"
        )

    def test_any_enabled_single_flag(self) -> None:
        """any_enabled() must detect even a single flag."""
        config = AblationConfig(
            contradictions=False,
            abstention=False,
            communication=False,
            state_flow=False,
            convention_fingerprint=False,
            content_hash=False,
            repo_intel_logging=False,
            repo_intel_decisions=False,
            structural_similarity=False,
            response_state_machine=True,
            hnsw=False,
        )
        assert config.any_enabled() is True

    def test_any_enabled_false(self) -> None:
        config = AblationConfig(
            contradictions=False,
            abstention=False,
            communication=False,
            state_flow=False,
            convention_fingerprint=False,
            content_hash=False,
            repo_intel_logging=False,
            repo_intel_decisions=False,
            structural_similarity=False,
            response_state_machine=False,
            hnsw=False,
        )
        assert config.any_enabled() is False

    def test_frozen_immutable(self) -> None:
        """AblationConfig is a snapshot — must not be mutable at runtime."""
        config = AblationConfig.from_env()
        with pytest.raises(AttributeError):
            config.contradictions = True  # type: ignore[misc]
        with pytest.raises(AttributeError):
            config.repo_intel_logging = True  # type: ignore[misc]
        with pytest.raises(AttributeError):
            config.hnsw = True  # type: ignore[misc]

    def test_two_configs_from_different_envs_are_independent(self) -> None:
        """Snapshot A taken under env X, snapshot B under env Y — must not share state."""
        with patch.dict(os.environ, {"GT_ENABLE_CONTRADICTIONS": "1"}, clear=True):
            config_a = AblationConfig.from_env()
        with patch.dict(os.environ, {"GT_ENABLE_ABSTENTION": "1"}, clear=True):
            config_b = AblationConfig.from_env()
        assert config_a.contradictions is True
        assert config_a.abstention is False
        assert config_b.contradictions is False
        assert config_b.abstention is True


class TestConfigurations:
    def test_baseline_is_empty(self) -> None:
        """Baseline = no incubator features. Must be empty dict."""
        assert CONFIGURATIONS["baseline"] == {}

    def test_full_stack_has_every_flag(self) -> None:
        """full_stack must enable ALL flags — if a new flag is added to
        AblationConfig but not to full_stack, this test catches it."""
        import dataclasses
        full = CONFIGURATIONS["full_stack"]
        config_fields = {f.name for f in dataclasses.fields(AblationConfig)}
        full_keys = set(full.keys())
        assert full_keys == config_fields, (
            f"full_stack missing: {config_fields - full_keys}, "
            f"full_stack extra: {full_keys - config_fields}"
        )
        for key, val in full.items():
            assert val is True, f"full_stack['{key}'] should be True"

    def test_intel_logging_preset(self) -> None:
        """intel_logging enables only data collection, not decisions."""
        preset = CONFIGURATIONS["intel_logging"]
        assert preset.get("repo_intel_logging") is True
        assert "repo_intel_decisions" not in preset

    def test_intel_full_preset(self) -> None:
        """intel_full enables both logging and decisions."""
        preset = CONFIGURATIONS["intel_full"]
        assert preset.get("repo_intel_logging") is True
        assert preset.get("repo_intel_decisions") is True

    def test_precision_trust_preset(self) -> None:
        """Precision trust = contradictions + abstention only."""
        preset = CONFIGURATIONS["precision_trust"]
        assert preset.get("contradictions") is True
        assert preset.get("abstention") is True
        assert len(preset) == 2

    def test_all_preset_names_are_valid(self) -> None:
        """Preset names must be valid Python identifiers (for programmatic use)."""
        for name in CONFIGURATIONS:
            assert name.isidentifier(), f"'{name}' is not a valid identifier"

    def test_no_preset_has_unknown_keys(self) -> None:
        """Every key in every preset must be a valid AblationConfig field."""
        import dataclasses
        valid_fields = {f.name for f in dataclasses.fields(AblationConfig)}
        for preset_name, preset in CONFIGURATIONS.items():
            for key in preset:
                assert key in valid_fields, (
                    f"Preset '{preset_name}' has unknown key '{key}'"
                )

    def test_configurations_count(self) -> None:
        """Guard against accidentally removing presets."""
        assert len(CONFIGURATIONS) >= 10

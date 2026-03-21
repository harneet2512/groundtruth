"""Tests for ablation configuration."""

from __future__ import annotations

import os
from unittest.mock import patch

from groundtruth.core.ablation import CONFIGURATIONS, AblationConfig


class TestAblationConfig:
    def test_from_env_all_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = AblationConfig.from_env()
        assert config.contradictions is False
        assert config.abstention is False
        assert config.communication is False
        assert config.state_flow is False
        assert config.convention_fingerprint is False
        assert config.content_hash is False
        assert config.repo_intel is False
        assert config.structural_similarity is False

    def test_from_env_some_on(self) -> None:
        with patch.dict(os.environ, {
            "GT_ENABLE_CONTRADICTIONS": "1",
            "GT_ENABLE_ABSTENTION": "1",
        }, clear=True):
            config = AblationConfig.from_env()
        assert config.contradictions is True
        assert config.abstention is True
        assert config.communication is False

    def test_describe(self) -> None:
        config = AblationConfig(
            contradictions=True,
            abstention=False,
            communication=True,
            state_flow=False,
            convention_fingerprint=False,
            content_hash=True,
            repo_intel=False,
            structural_similarity=False,
        )
        desc = config.describe()
        assert desc["contradictions"] is True
        assert desc["abstention"] is False
        assert desc["communication"] is True
        assert len(desc) == 8

    def test_any_enabled_true(self) -> None:
        config = AblationConfig(
            contradictions=True,
            abstention=False,
            communication=False,
            state_flow=False,
            convention_fingerprint=False,
            content_hash=False,
            repo_intel=False,
            structural_similarity=False,
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
            repo_intel=False,
            structural_similarity=False,
        )
        assert config.any_enabled() is False

    def test_frozen(self) -> None:
        config = AblationConfig.from_env()
        try:
            config.contradictions = True  # type: ignore[misc]
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestConfigurations:
    def test_baseline_is_empty(self) -> None:
        assert CONFIGURATIONS["baseline"] == {}

    def test_full_stack_has_all(self) -> None:
        full = CONFIGURATIONS["full_stack"]
        assert full["contradictions"] is True
        assert full["abstention"] is True
        assert full["communication"] is True
        assert full["state_flow"] is True
        assert full["convention_fingerprint"] is True
        assert full["content_hash"] is True
        assert full["repo_intel"] is True
        assert full["structural_similarity"] is True

    def test_nine_configurations(self) -> None:
        assert len(CONFIGURATIONS) == 9

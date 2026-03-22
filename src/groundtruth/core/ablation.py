"""Ablation configuration — tracks which incubator subsystems are active.

Used for systematic comparison of feature configurations.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundtruth.core import flags


@dataclass(frozen=True)
class AblationConfig:
    """Snapshot of which incubator subsystems are enabled."""

    contradictions: bool
    abstention: bool
    communication: bool
    state_flow: bool
    convention_fingerprint: bool
    content_hash: bool
    repo_intel_logging: bool
    repo_intel_decisions: bool
    structural_similarity: bool
    response_state_machine: bool
    hnsw: bool

    @classmethod
    def from_env(cls) -> AblationConfig:
        """Read all GT_ENABLE_* flags from environment."""
        return cls(
            contradictions=flags.contradictions_enabled(),
            abstention=flags.abstention_enabled(),
            communication=flags.communication_enabled(),
            state_flow=flags.state_flow_enabled(),
            convention_fingerprint=flags.convention_fingerprint_enabled(),
            content_hash=flags.content_hash_enabled(),
            repo_intel_logging=flags.repo_intel_logging_enabled(),
            repo_intel_decisions=flags.repo_intel_decisions_enabled(),
            structural_similarity=flags.structural_similarity_enabled(),
            response_state_machine=flags.response_state_machine_enabled(),
            hnsw=flags.hnsw_enabled(),
        )

    def describe(self) -> dict[str, bool]:
        """Return a dict of flag names to their states."""
        return {
            "contradictions": self.contradictions,
            "abstention": self.abstention,
            "communication": self.communication,
            "state_flow": self.state_flow,
            "convention_fingerprint": self.convention_fingerprint,
            "content_hash": self.content_hash,
            "repo_intel_logging": self.repo_intel_logging,
            "repo_intel_decisions": self.repo_intel_decisions,
            "structural_similarity": self.structural_similarity,
            "response_state_machine": self.response_state_machine,
            "hnsw": self.hnsw,
        }

    def any_enabled(self) -> bool:
        """True if any incubator subsystem is active."""
        return any(self.describe().values())


# Named configurations for ablation studies
CONFIGURATIONS: dict[str, dict[str, bool]] = {
    "baseline": {},  # all OFF
    "substrate_only": {"content_hash": True},
    "substrate_plus_trust": {"content_hash": True, "contradictions": True, "abstention": True},
    "substrate_plus_conventions": {"content_hash": True, "state_flow": True, "convention_fingerprint": True},
    "precision_trust": {"contradictions": True, "abstention": True},
    "judgment_depth": {"state_flow": True, "convention_fingerprint": True},
    "intel_logging": {"repo_intel_logging": True},
    "intel_full": {"repo_intel_logging": True, "repo_intel_decisions": True},
    "structural_similarity": {"structural_similarity": True},
    "full_stack": {
        "contradictions": True,
        "abstention": True,
        "communication": True,
        "state_flow": True,
        "convention_fingerprint": True,
        "content_hash": True,
        "repo_intel_logging": True,
        "repo_intel_decisions": True,
        "structural_similarity": True,
        "response_state_machine": True,
        "hnsw": True,
    },
}

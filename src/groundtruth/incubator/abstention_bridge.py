"""AbstentionBridge — single callable for emission decisions.

Extracts the abstention logic that was previously inline in core_tools.py.
Used by both the check handler and IncubatorRuntime to prevent split authority.

When GT_ENABLE_ABSTENTION is OFF, all findings pass through unfiltered.
"""

from __future__ import annotations

import os
from typing import Any

from groundtruth.core import flags
from groundtruth.index.freshness import FreshnessChecker, FreshnessLevel
from groundtruth.index.store import SymbolStore
from groundtruth.policy.abstention import AbstentionPolicy, EmissionLevel, TrustTier
from groundtruth.utils.result import Ok


class AbstentionBridge:
    """Single authority for abstention decisions.

    Wraps AbstentionPolicy + FreshnessChecker into one callable.
    Handles trust tier mapping, staleness detection, and emission routing.
    """

    def __init__(self, store: SymbolStore, root_path: str) -> None:
        self._store = store
        self._root_path = root_path
        self._policy = AbstentionPolicy() if flags.abstention_enabled() else None
        self._freshness = FreshnessChecker() if flags.abstention_enabled() else None

    @property
    def active(self) -> bool:
        """True if abstention filtering is enabled."""
        return self._policy is not None

    def classify_finding(
        self,
        finding: dict[str, Any],
        file_path: str,
    ) -> str:
        """Classify a finding as 'emit', 'soft_info', or 'suppress'.

        When abstention is OFF, always returns 'emit'.
        When ON, uses freshness + trust tier to decide.

        Args:
            finding: Dict with at least 'kind', 'file', 'confidence'.
            file_path: The source file path for freshness checking.

        Returns:
            'emit' — include as hard blocker
            'soft_info' — include as informational
            'suppress' — do not include
        """
        if self._policy is None or self._freshness is None:
            return "emit"

        meta_result = self._store.get_file_metadata(file_path)
        file_meta = meta_result.value if isinstance(meta_result, Ok) else None
        indexed_at = file_meta["indexed_at"] if file_meta else None

        fr = self._freshness.check_file(
            os.path.join(self._root_path, file_path), indexed_at
        )
        is_stale = fr.level == FreshnessLevel.STALE
        trust = TrustTier.YELLOW if is_stale else TrustTier.GREEN

        emission = self._policy.decide(
            trust=trust,
            evidence_count=2,
            coverage=5.0,
            is_stale=is_stale,
            is_contradiction=True,
        )

        if emission == EmissionLevel.EMIT_NOTHING:
            return "suppress"
        if emission == EmissionLevel.EMIT_SOFT_INFO:
            return "soft_info"
        return "emit"

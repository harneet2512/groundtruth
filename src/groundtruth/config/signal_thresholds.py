"""Centralized thresholds for GT signal emission.

Every threshold is a named constant with justification. When used at runtime,
the calling code logs which threshold was applied via _log_threshold_use().

Justification source: 30-task edge confidence distribution across 13 repos
(run 26082940766, 2026-05-19). p50=0.5, p75=0.7, p90=0.9.
"""
from __future__ import annotations

import sys

# ---------------------------------------------------------------------------
# Signal 1: Cross-file scope in L1 brief
# ---------------------------------------------------------------------------

SCOPE_MIN_CALLER_FILES = 2
"""Minimum distinct caller files to claim multi-file scope.
1 caller file = single-file dependency, not scope. 2+ = real cross-file impact."""

SCOPE_MIN_EDGE_CONFIDENCE = 0.9
"""Only import/same_file edges (binary fact). name_match edges at 0.6-0.9
may connect wrong functions — too unreliable for scope claims."""

SCOPE_HIGH_RESOLUTION_METHODS = ("same_file", "import")
"""Edge resolution methods considered high-confidence for scope."""

SPARSE_GRAPH_THRESHOLD = 2.0
"""Edges per file below this = graph too sparse for structural claims.
Derived from: repos with <2 edges/file had 0% useful scope signals in 30-task run."""

# ---------------------------------------------------------------------------
# Signal 2: Diff-aware signature detection
# ---------------------------------------------------------------------------

SIGNATURE_HIGH_CONFIDENCE_METHODS = ("same_file", "import")
"""Only deterministic resolution methods produce high-confidence arity warnings.
same_file: caller and callee in same file (1.0 confidence).
import: caller imports callee (1.0 confidence)."""

SIGNATURE_MEDIUM_CONFIDENCE_METHODS = ("name_match",)
"""name_match edges may connect wrong functions (0.2-0.9 confidence).
Arity warnings from these edges are advisory, not imperative."""

# ---------------------------------------------------------------------------
# Signal 3: GT_VERIFY test commands
# ---------------------------------------------------------------------------

VERIFY_MIN_EDGE_CONFIDENCE = 0.7
"""Minimum edge confidence for emitting a test command.
Below 0.7 = likely name_match with multiple candidates (0.4-0.6 range)."""

VERIFY_LABEL_HIGH_METHODS = ("same_file", "import")
"""Test directly imports or co-locates with edited module. High confidence."""

VERIFY_LABEL_MEDIUM_METHODS = ("name_match",)
"""Test connected by name match. May be wrong test."""

# ---------------------------------------------------------------------------
# Signal 4: Co-change completeness
# ---------------------------------------------------------------------------

COCHANGE_HIGH_THRESHOLD = 5
"""Co-changed in >=5 of last 10 commits = strong historical signal.
Below 5: too many false positives from unrelated batch commits."""

COCHANGE_MEDIUM_THRESHOLD = 3
"""Co-changed in 3-4 commits = weak but worth advisory mention.
Below 3: noise — could be coincidental proximity in a refactor."""

COCHANGE_WINDOW_COMMITS = 30
"""Look back 30 commits for co-change patterns. Deeper history adds noise
from old refactors that no longer apply."""


def log_threshold_use(
    threshold_name: str, value: object, context: str = ""
) -> None:
    """Log which threshold was applied. Goes to stderr (not agent-visible)."""
    msg = f"[GT_CONFIG] {threshold_name}={value}"
    if context:
        msg += f" context={context}"
    print(msg, file=sys.stderr, flush=True)

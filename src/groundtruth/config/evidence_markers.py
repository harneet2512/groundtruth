"""Shared evidence marker contract — single source of truth.

Every GT layer uses these marker groups to detect evidence in hook output.
No inline marker tuples in the wrapper — import from here.
"""

# L3b markers: post-view navigation + structural signals
L3B_MARKERS: tuple[str, ...] = (
    "Called by:", "Calls into:", "Imported by:",
    "[GT] ", "<gt-context", "<gt-scope",
    "[CONTRACT]", "[CONTRACT ~]", "[PEER]", "[PATTERN]",
    "[SIGNATURE]", "[TEST]", "[GT_VERIFY",
    "[PROPAGATE]", "[CO-CHANGE]", "[SCOPE]",
    "[BEHAVIORAL CONTRACT]", "[RECALL]",
    "[GT_AUTO]", "[MISMATCH]", "[FORMAT]", "[GT_CONTRACT",
)

# L3 markers: post-edit evidence (superset of L3b + legacy compat)
L3_MARKERS: tuple[str, ...] = (
    *L3B_MARKERS,
    "<gt-post-edit", "<gt-edit-target", "<gt-orientation",
    "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]",
    "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_COUPLING]",
    "[GT L3:", "[TWINS]",
    # Semantic check markers (prepended by wrapper from groundtruth.hooks.semantic_check)
    "GUARD_ADDED:", "GUARD_REMOVED:",
    # Obligation check markers (from groundtruth.hooks.obligation_check)
    "[COMPLETENESS]",
    # Exception path markers (from L4b-1 in post_view.py)
    "[CATCHES]", "[RAISES]",
    # Override chain markers (P15)
    "[OVERRIDE]",
    # Similar function markers (P4)
    "[SIMILAR]",
    # Legacy markers (backward compat with older post_edit.py)
    "SIGNATURE:", "SIBLING:", "CALLERS:", "WARNING:",
    "TOP CALLER:", "MUST PRESERVE:", "TEST EXPECTS:", "TEST:",
)

# Rescue markers: minimal — rescue payload always starts with [GT]
RESCUE_MARKERS: tuple[str, ...] = ("[GT]",)


def has_gt_evidence(text: str, layer: str = "l3b") -> bool:
    """Check if text contains recognized GT evidence markers for the given layer."""
    if layer == "l3b":
        markers = L3B_MARKERS
    elif layer == "l3":
        markers = L3_MARKERS
    elif layer == "rescue":
        markers = RESCUE_MARKERS
    else:
        markers = L3B_MARKERS
    return any(m in text for m in markers)

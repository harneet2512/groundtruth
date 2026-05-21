"""Shared evidence marker contract — single source of truth.

Every GT layer uses these marker groups to detect evidence in hook output.
No inline marker tuples in the wrapper — import from here.
"""

# L3b markers: post-view navigation + structural signals
L3B_MARKERS: tuple[str, ...] = (
    "Called by:", "Calls into:", "Imported by:", "Next:",
    "[GT] ", "[GT_STATUS] success",
    "[CONTRACT]", "[CONTRACT ~]", "[PEER]", "[PATTERN]",
    "[SIGNATURE]", "[TEST]", "[GT_VERIFY",
    "[PROPAGATE]", "[CO-CHANGE]", "[SCOPE]",
    "[BEHAVIORAL CONTRACT]", "[RECALL]",
    "[GT_AUTO]", "[MISMATCH]", "[FORMAT]", "[GT_CONTRACT",
)

# L3 markers: post-edit evidence (superset of L3b + legacy compat)
L3_MARKERS: tuple[str, ...] = (
    *L3B_MARKERS,
    "[GT_CHANGE]", "[GT_CONTRACT]", "[GT_PATTERN]",
    "[GT_STRUCTURAL]", "[GT_SEMANTIC]", "[GT_COUPLING]",
    "[GT L3:", "[TWINS]",
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

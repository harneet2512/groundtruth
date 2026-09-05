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
    # Behavioral-contract return-shape marker (post_edit.py return_shape branch)
    "[RETURNS]",
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


# ---------------------------------------------------------------------------
# Relevance gate for non-edge signals — [RECALL] / [FORMAT] (TASK #47).
#
# [RECALL] (emitted by the wrapper from an evidence cache) and [FORMAT]
# (emitted by evidence.format_contract from caller-subscript mining) are NOT
# backed by a CALLS/IMPORTS edge, so the categorical edge filter cannot judge
# them. They are derived from stale per-file dumps / fixture keys and can carry
# content unrelated to the function the agent just edited (the observed leaks:
# a [RECALL] of ``progress_write`` while ``set_fields`` was edited; [FORMAT]
# fixture keys "path" / "SKIP_SLOW_TESTS").
#
# These helpers live in this leaf config module (no hooks/sqlite imports) so
# every emitter can reuse one definition. They mirror the build-1 helpers
# ``_passes_relevance_gate`` / ``_identifier_tokens`` in
# ``groundtruth.hooks.post_edit`` (which gate [SIMILAR]); centralizing them
# here avoids importing the hooks layer into config/evidence.
#
# Pillar alignment (.claude/CLAUDE.md): correct-or-quiet — when no relevance
# anchor exists we DROP rather than launder; generalized — pure token overlap,
# no task IDs / repo names / magic thresholds.
import re as _re_em


def identifier_tokens(name: str) -> set[str]:
    """Split a snake_case / camelCase identifier into lowercase sub-tokens.

    Builds a relevance anchor from an edited function's name.
    ``set_fields`` -> {"set", "fields", "set_fields"};
    ``embedAlbum`` -> {"embed", "album", "embedalbum"}.

    Sub-tokens shorter than 3 chars are dropped (too generic to anchor on),
    but the full lowercased name is always retained.
    """
    n = (name or "").strip()
    if not n:
        return set()
    parts = _re_em.split(r"[_\W]+|(?<=[a-z0-9])(?=[A-Z])", n)
    toks = {p.lower() for p in parts if p and len(p) >= 3}
    toks.add(n.lower())
    return toks


def passes_relevance_gate(
    candidate_text: str,
    issue_terms: set[str] | None,
    fn_tokens: set[str] | None,
) -> bool:
    """Render a non-edge signal only when it overlaps the edit's relevance anchor.

    A non-edge signal ([RECALL] / [FORMAT] / [SIMILAR]) passes only when its
    text overlaps EITHER the issue terms OR the edited function's identifier
    tokens. Otherwise it is unkeyed noise and is suppressed.

    Correct-or-quiet: when neither issue terms nor fn tokens are available we
    cannot judge relevance, so we DROP the signal (return False) rather than
    laundering an unrelated entry as evidence (wrong info that misdirects the
    agent is worse than no info).

    Pure function — no I/O, safe to call from any layer.
    """
    text = (candidate_text or "").lower()
    if not text:
        return False
    anchor = {t for t in (issue_terms or set()) if t} | {
        t for t in (fn_tokens or set()) if t
    }
    if not anchor:
        # No relevance anchor available — stay silent rather than guess.
        return False
    return any(a in text for a in anchor)

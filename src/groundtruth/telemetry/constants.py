"""Telemetry constants — token caps, iteration bands, verification kinds."""

from __future__ import annotations

# Schema version
SCHEMA_VERSION = "1.0.0"

# Token caps per layer/sublayer
L3_POST_EDIT_TOKEN_CAP = 300
L3_POST_FAILURE_TOKEN_CAP = 300
L3_LATE_REPAIR_TOKEN_CAP = 180
L3B_LATE_TOKEN_CAP = 120
L4_TOKEN_CAP = 120
L5B_TOKEN_CAP = 180

# Iteration bands
BAND_EARLY = "early_0_25"
BAND_MID = "mid_25_60"
BAND_LATE = "late_60_85"
BAND_FINAL = "final_85_100"

# L3b edge limits by band
L3B_EDGE_LIMITS: dict[str, int] = {
    BAND_EARLY: 3,
    BAND_MID: 2,
    BAND_LATE: 1,
    BAND_FINAL: 0,
}

L3B_BROAD_NAV_CUTOFF_RATIO = 0.60

# Verification kind constants
VK_TARGETED_SYMBOL = "targeted_to_edited_symbol"
VK_TARGETED_FILE = "targeted_to_edited_file"
VK_TARGETED_RELATED = "targeted_to_related_test"
VK_BROAD = "broad_project_verification"
VK_IRRELEVANT = "irrelevant_verification"
VK_UNKNOWN = "unknown"

VALID_VERIFICATION_KINDS = {
    VK_TARGETED_SYMBOL, VK_TARGETED_FILE, VK_TARGETED_RELATED,
    VK_BROAD, VK_IRRELEVANT, VK_UNKNOWN,
}

# Follow type constants
FT_FOLLOWED_EXACT = "FOLLOWED_EXACT"
FT_FOLLOWED_RELATED_FILE = "FOLLOWED_RELATED_FILE"
FT_FOLLOWED_RELATED_TEST = "FOLLOWED_RELATED_TEST"
FT_FOLLOWED_BROAD_ONLY = "FOLLOWED_BROAD_ONLY"
FT_FOLLOWED_REPAIR = "FOLLOWED_REPAIR"
FT_PARTIAL = "PARTIAL"
FT_IGNORED = "IGNORED"
FT_CONTRADICTED = "CONTRADICTED"
FT_TOO_LATE = "TOO_LATE"
FT_NOT_MEASURABLE = "NOT_MEASURABLE"

VALID_FOLLOW_TYPES = {
    FT_FOLLOWED_EXACT, FT_FOLLOWED_RELATED_FILE, FT_FOLLOWED_RELATED_TEST,
    FT_FOLLOWED_BROAD_ONLY, FT_FOLLOWED_REPAIR, FT_PARTIAL, FT_IGNORED,
    FT_CONTRADICTED, FT_TOO_LATE, FT_NOT_MEASURABLE,
}

# Belief statuses
BS_CANDIDATE = "candidate"
BS_SUPPORTED = "supported"
BS_UNVERIFIED = "unverified"
BS_VERIFIED = "verified"
BS_STALE = "stale"
BS_CONTRADICTED = "contradicted"
BS_ABANDONED = "abandoned"
BS_PROMOTED = "promoted"

VALID_BELIEF_STATUSES = {
    BS_CANDIDATE, BS_SUPPORTED, BS_UNVERIFIED, BS_VERIFIED,
    BS_STALE, BS_CONTRADICTED, BS_ABANDONED, BS_PROMOTED,
}

# Valid layers
# L3_router_v2 = FINAL_ARCH_V2 Layer 3 CollaborationRouter emissions
#   (distinct from legacy L3 so paired-metrics analysis can attribute
#   injection counts cleanly).
VALID_LAYERS = {
    "L1", "L3", "L3b", "L3_router_v2", "L4", "L5", "L5b", "L6", "HYGIENE",
}

# --- Decision 34: Generalized event taxonomy ---

# Event buckets (generalized action categories)
VALID_EVENT_BUCKETS = {
    "ORIENTATION", "SEARCH", "OPEN_INSPECT", "CONTEXT_NAVIGATION",
    "EDIT_COMMITMENT", "PATCH_DIFF_STATE", "VERIFICATION_CHECK",
    "FEEDBACK_FAILURE", "LOOP_STALL", "FINISH_TERMINAL",
    "ENVIRONMENT", "MAX_ITER", "REINDEX", "HYGIENE_EVENT",
}

# Generalized L5 event types (canonical JSONL names — Decision 34 §5)
VALID_L5_EVENT_TYPES = {
    # P0: agent-visible intervention candidates
    "STRUCTURAL_WITNESS_IGNORED",
    "WEAK_VERIFICATION_AFTER_EDIT",
    "FINISH_WITH_UNVERIFIED_EDIT",
    "PATCH_COLLAPSED_OR_LOST",
    "NO_DURABLE_PROGRESS",
    # P1: structured, agent-visible only late/final with concrete next_action
    "DURABLE_EDIT_STARTED",
    "REPEATED_UNPRODUCTIVE_LOOP",
    "STALE_CONTEXT_PATH",
    "LOW_CONFIDENCE_CONTEXT_DRIFT",
    "HYPOTHESIS_FALSIFIED",
    # P2: structured-only, never agent-visible this pass
    "STRONG_VERIFICATION_AFTER_EDIT",
    "NORMAL_EXPLORATION",
    "ENVIRONMENT_FAILURE",
    "MAX_ITER_EXIT_AUDIT",
}

# File kinds (generalized file classification)
VALID_FILE_KINDS = {
    "DURABLE_PRODUCT_FILE",
    "VALIDATION_FILE",
    "SCAFFOLD_FILE",
    "CONFIG_FILE",
    "GENERATED_FILE",
    "UNKNOWN_FILE",
}

# Check kinds (generalized, framework-agnostic)
VALID_CHECK_KINDS = {
    "TARGETED_CHECK",
    "STATIC_SANITY",
    "BROAD_CHECK",
    "IRRELEVANT_CHECK",
    "SETUP_OR_INSTALL",
    "UNKNOWN_CHECK",
    "NO_CHECK",
}

# Verification strength
VALID_VERIFICATION_STRENGTHS = {"STRONG", "WEAK", "NONE", "UNKNOWN"}

# Confidence levels
VALID_CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW", "NONE"}

# Extended follow types (add to existing set)
FT_FOLLOWED_STRUCTURAL_WITNESS = "FOLLOWED_STRUCTURAL_WITNESS"
FT_FOLLOWED_STATIC_SANITY = "FOLLOWED_STATIC_SANITY"
FT_FOLLOWED_TARGETED_CHECK = "FOLLOWED_TARGETED_CHECK"

VALID_FOLLOW_TYPES |= {
    FT_FOLLOWED_STRUCTURAL_WITNESS,
    FT_FOLLOWED_STATIC_SANITY,
    FT_FOLLOWED_TARGETED_CHECK,
}

# L5 safety caps (Decision 34 §7, tightened after beets-5495 regression)
# Context budget rule: L5b injections consume agent context window.
# Most L5 detections should be structured-only (JSONL, not injected).
# Only HIGH confidence + LATE/FINAL band + concrete next_action → inject.
L5_MAX_INJECTIONS_PER_TASK = 2
L5_DEBOUNCE_ITERATIONS = 3
L5_INJECTION_MIN_BAND = "late_repair"

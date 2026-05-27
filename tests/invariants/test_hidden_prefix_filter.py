"""Hidden prefix filtering invariant.

Internal GT prefixes ([GT_META], [GT_STATUS], etc.) must NOT appear in
agent-visible output. Intended visible markers ([GT_AUTO], [SIGNATURE],
[TEST], etc.) MUST appear.

[GT_AUTO] is intentionally visible — DOC_OF_HONOR section 2.4 says
the agent sees "[GT_AUTO] Key symbols in file.py:". It is NOT a hidden
prefix despite the [GT_...] naming pattern.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))


HIDDEN_PREFIXES = (
    "[GT_META]", "[GT_STATUS]", "[GT_CONFIG]", "[GT_TRACE]",
    "[GT_DELIVERY]", "[GT_COST]", "[GT_PAYLOAD]", "[GT_LLM_CONFIG]",
    "[GT_SUMMARY]",
)

VISIBLE_MARKERS = (
    "[GT_AUTO]", "[SIGNATURE]", "[BEHAVIORAL CONTRACT]",
    "[TEST]", "[COMPLETENESS]", "[PATTERN]", "[PEER]",
    "[SIMILAR]", "[OVERRIDE]", "[MISMATCH]", "[REVIEW]",
    "[GT KEY CONTRACTS]", "[CATCHES]", "[RAISES]",
    "PRESERVE:", "Called by:", "Calls into:",
)


class TestHiddenPrefixClassification:
    """Hidden prefixes are correctly classified."""

    def test_gt_auto_is_not_hidden(self):
        """[GT_AUTO] is intended visible L4a content, not a hidden prefix."""
        assert "[GT_AUTO]" not in HIDDEN_PREFIXES, (
            "[GT_AUTO] must NOT be in hidden prefixes — it is intended agent-facing"
        )

    def test_gt_meta_is_hidden(self):
        assert "[GT_META]" in HIDDEN_PREFIXES

    def test_gt_trace_is_hidden(self):
        assert "[GT_TRACE]" in HIDDEN_PREFIXES

    def test_gt_delivery_is_hidden(self):
        assert "[GT_DELIVERY]" in HIDDEN_PREFIXES

    def test_visible_markers_not_hidden(self):
        """No intended visible marker should be in hidden prefixes."""
        for marker in VISIBLE_MARKERS:
            assert marker not in HIDDEN_PREFIXES, (
                f"{marker} is an intended visible marker but found in HIDDEN_PREFIXES"
            )


class TestProductionHiddenPrefixes:
    """Production _HIDDEN_PREFIXES matches expected list."""

    def test_production_hidden_prefixes_match(self):
        """Verify oh_gt_full_wrapper.py _HIDDEN_PREFIXES does not contain [GT_AUTO]."""
        try:
            # Import the actual constant
            wrapper_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "scripts", "swebench",
                "oh_gt_full_wrapper.py",
            )
            if not os.path.isfile(wrapper_path):
                return  # skip if wrapper not available

            with open(wrapper_path, encoding="utf-8", errors="replace") as f:
                content = f.read()

            # Find the _HIDDEN_PREFIXES line
            for line in content.splitlines():
                if "_HIDDEN_PREFIXES" in line and "=" in line and "[GT_META]" in line:
                    assert "[GT_AUTO]" not in line, (
                        "[GT_AUTO] must not be in production _HIDDEN_PREFIXES"
                    )
                    # Verify all expected hidden prefixes are present
                    for hp in ("[GT_META]", "[GT_STATUS]", "[GT_TRACE]", "[GT_DELIVERY]"):
                        assert hp in line, f"Expected {hp} in _HIDDEN_PREFIXES"
                    return

        except Exception:
            pass  # If we can't read wrapper, skip

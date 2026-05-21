"""Issue-text obligation detection.

Deterministic, $0 AI. Extracts behavioral obligations from issue text
(e.g., "without specifying old_url") and validates the agent's diff
against them. Catches the common failure mode where the agent wraps
a call in try/except instead of removing the problematic parameter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class IssueObligation:
    kind: Literal["remove_parameter", "remove_behavior", "add_parameter"]
    parameter: str
    confidence: float
    source: str


_REMOVE_PARAM_PATTERNS = [
    (r"without\s+(?:specifying|passing|providing|using)\s+(?:an?\s+)?['\"]?`?([A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)?)`?['\"]?", 0.9),
    (r"(?:remove|drop|omit|stop\s+passing|no\s+longer\s+pass|delete)\s+(?:the\s+)?['\"]?`?([A-Za-z_]\w*)`?['\"]?", 0.9),
    (r"`([A-Za-z_]\w*)`\s+(?:parameter|argument|kwarg)\s+(?:is\s+)?(?:not\s+)?(?:required|needed|allowed|deprecated|obsolete)", 0.8),
    (r"(?:deprecated|obsolete|removed?)\s+`?([A-Za-z_]\w*)`?\s+(?:parameter|argument|option)", 0.8),
]


def _normalize_param(raw: str) -> str:
    """Convert 'old url' to 'old_url', 'oldUrl' stays as-is."""
    return re.sub(r'\s+', '_', raw.strip())

_ISSUE_TEXT_PATH = "/tmp/gt_issue.txt"


def extract_issue_obligations(issue_text: str) -> list[IssueObligation]:
    """Extract behavioral obligations from issue text."""
    obligations: list[IssueObligation] = []
    for pattern, confidence in _REMOVE_PARAM_PATTERNS:
        for m in re.finditer(pattern, issue_text, re.IGNORECASE):
            param = m.group(1)
            param = _normalize_param(param)
            if len(param) >= 3 and param.lower() not in _SKIP_WORDS:
                obligations.append(IssueObligation(
                    kind="remove_parameter",
                    parameter=param,
                    confidence=confidence,
                    source=m.group(0)[:80],
                ))
    return list({o.parameter: o for o in obligations}.values())


def check_obligations_against_diff(
    issue_text: str,
    diff_text: str,
) -> list[str]:
    """Check if the agent's diff satisfies issue obligations.

    Returns warning strings when the diff appears to violate an obligation.
    """
    if not issue_text or not diff_text:
        return []

    obligations = extract_issue_obligations(issue_text)
    if not obligations:
        return []

    added = "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    has_try_except = bool(re.search(r"\btry\s*:|\bexcept\b", added))

    warnings: list[str] = []
    for ob in obligations:
        if ob.kind == "remove_parameter":
            param_in_added = bool(re.search(rf"\b{re.escape(ob.parameter)}\s*=", added))
            param_in_removed = bool(re.search(rf"\b{re.escape(ob.parameter)}\s*=", removed))
            param_gone = param_in_removed and not param_in_added
            if param_in_added and not param_gone:
                if has_try_except:
                    warnings.append(
                        f"[GT_CONTRACT high] Issue says to omit `{ob.parameter}`; "
                        f"current patch wraps the call in try/except instead of removing the parameter."
                    )
                else:
                    warnings.append(
                        f"[GT_CONTRACT high] Issue says to omit `{ob.parameter}`; "
                        f"your patch still passes it."
                    )
    return warnings


def load_and_check(diff_text: str, issue_path: str = _ISSUE_TEXT_PATH) -> list[str]:
    """Load issue text from file and check against diff."""
    if not os.path.isfile(issue_path):
        return []
    try:
        with open(issue_path, encoding="utf-8", errors="ignore") as fh:
            issue_text = fh.read()
        return check_obligations_against_diff(issue_text, diff_text)
    except OSError:
        return []


_SKIP_WORDS = frozenset({
    "the", "that", "this", "with", "from", "have", "should", "would",
    "could", "when", "not", "none", "true", "false", "self", "return",
    "def", "class", "import", "try", "except", "raise", "pass",
})

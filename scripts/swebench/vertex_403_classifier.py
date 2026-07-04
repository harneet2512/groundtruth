"""Vertex 403 body classifier — RC-02 (cost discipline / retry policy).

Vertex MaaS returns HTTP 403 for two distinct failure modes (per
`.claude/CLAUDE.md` "Disambiguating 403 — IAM vs quota throttle"):

  * RESOURCE_EXHAUSTED  -> real quota throttle    (retry-eligible)
  * IAM_PERMISSION_DENIED / PERMISSION_DENIED -> identity-side fail-fast

litellm's exception hierarchy collapses both onto BadRequestError /
AuthenticationError, which makes the proxy `retry_policy.RateLimitErrorRetries`
config dead code (RateLimitError is only raised on HTTP 429, never 403).

This module is a deterministic, regex-only classifier we call from the
preflight (and any future retry wrapper) so the IAM case fails fast and
the throttle case can be back-off-retried under the CLAUDE.md "<=2 per
20-min window" rule. Generic — applies to every Vertex MaaS publisher,
not just Qwen3 / Track 4.
"""
from __future__ import annotations

from typing import Literal

Verdict = Literal["throttle", "iam", "unknown"]


def classify_403(body: str) -> Verdict:
    """Classify a Vertex 403 response body.

    >>> classify_403('{"error":{"status":"RESOURCE_EXHAUSTED","message":"quota"}}')
    'throttle'
    >>> classify_403('{"error":{"status":"PERMISSION_DENIED","reason":"IAM_PERMISSION_DENIED"}}')
    'iam'
    >>> classify_403('')
    'unknown'
    """
    if not body:
        return "unknown"
    s = body.upper()
    # Order matters: RESOURCE_EXHAUSTED is the throttle signal even if
    # the body also mentions PERMISSION_DENIED in some auxiliary field.
    if "RESOURCE_EXHAUSTED" in s:
        return "throttle"
    if "IAM_PERMISSION_DENIED" in s or "PERMISSION_DENIED" in s:
        return "iam"
    return "unknown"


def is_retryable(body: str) -> bool:
    """True iff the 403 should be retried (with back-off)."""
    return classify_403(body) == "throttle"

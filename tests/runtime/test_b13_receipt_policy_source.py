"""B-13 — a receipt may be earned ONLY from POLICY-produced evidence, never env output.

Finding (verifyandobserve B-13 / bug-journey B-13): receipt promotion treated the
ENVIRONMENT's later command output as an agent REFERENCED receipt — an unrelated
``ls``/``grep`` result that merely NAMES a delivered path promoted the receipt
without the policy referencing or acting on the evidence. The seam fed ``orig_out``
(tool output) into the referencing channel.

Fix: ``update_receipts`` earns REFERENCED only from ``policy_text`` (the agent's own
assistant message) and ACTED only from ``next_action_cmd`` (the agent's own tool
call). ``env_output`` (and the deprecated ``observed_text`` alias the pre-fix seam
used) is accepted so the seam can pass tool output EXPLICITLY, but it is structurally
NEVER consulted for promotion.

TTD: mandate (b) env output naming a delivered path does NOT promote to REFERENCED,
but a policy message/action naming it DOES. The behavioral RED bites the pre-fix code
through the ``observed_text`` alias (pre-fix that channel promoted; post-fix it never
does). Biting mutation = add env_output/observed_text back into the referencing OR of
promotion -> ``test_b13_env_output_does_not_promote`` /
``test_b13_deprecated_observed_text_does_not_promote`` fail.

PURE · DETERMINISTIC · LLM-FREE · stdlib only. No network, no time, no randomness.
"""
from __future__ import annotations

from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import (
    RECEIPT_ACTED,
    RECEIPT_DELIVERED,
    RECEIPT_REFERENCED,
    VERIFIED,
    EvidenceEnvelope,
)


def _sealed(*, target: str, fact_id: str = "sym") -> EvidenceEnvelope:
    e = EvidenceEnvelope.build(
        producer="p", fact_id=fact_id, target=target,
        evidence_type="def_ref_partition", payload=(f"def: {target}:1",),
        provenance=((target, 1),), confidence=0.9, tier=VERIFIED)
    s, _ = ad.seal_delivery(e, episode_id="t", event_id="1", parent_hash="",
                            rendered_bytes=b"x", renderer_id="native")
    assert s.receipt_state == RECEIPT_DELIVERED
    return s


# --------------------------------------------------------------------------- #
# env output must NOT promote — the finding (mandate b)
# --------------------------------------------------------------------------- #
def test_b13_precondition_delivered_starts_level1() -> None:
    """Baseline: a fresh sealed delivery is DELIVERED (level 1)."""
    assert _sealed(target="src/foo.py").receipt_state == RECEIPT_DELIVERED


def test_b13_env_output_does_not_promote() -> None:
    """Environment/tool OUTPUT that NAMES a delivered path (e.g. an unrelated `ls src`
    listing it) must NOT promote the receipt — the policy never referenced or acted on
    the evidence. (Mutation: reading env_output for `referenced` makes this REFERENCED,
    so the test bites.)"""
    s = _sealed(target="src/foo.py")
    out = ad.update_receipts([s], env_output="ls src\nsrc/foo.py\nsrc/bar.py")
    assert out[0].receipt_state == RECEIPT_DELIVERED


def test_b13_deprecated_observed_text_does_not_promote() -> None:
    """The pre-fix seam passed tool output via ``observed_text``; that alias must now
    be treated as environment output and NEVER promote — an un-migrated seam degrades
    to the correct (no-false-promotion) behavior. THIS is the behavioral RED against
    the pre-fix code (which promoted this exact call to REFERENCED)."""
    s = _sealed(target="src/foo.py")
    out = ad.update_receipts([s], observed_text="grep -rn foo .\nsrc/foo.py:12: foo()")
    assert out[0].receipt_state == RECEIPT_DELIVERED


def test_b13_env_output_symbol_does_not_promote() -> None:
    """Same rule for a symbol entity: tool output NAMING the delivered symbol as a
    whole token must NOT promote."""
    s = _sealed(target="src/foo.py", fact_id="compute_total")
    out = ad.update_receipts([s], env_output="pytest -k compute_total ... compute_total PASSED")
    assert out[0].receipt_state == RECEIPT_DELIVERED


def test_b13_env_output_cannot_sneak_in_alongside_policy() -> None:
    """Even when a policy message IS present but names something else, env output that
    names the delivered path must NOT promote — the two channels are independent and
    only the policy channel can earn REFERENCED."""
    s = _sealed(target="src/foo.py")
    out = ad.update_receipts(
        [s], policy_text="I'll inspect the unrelated module", env_output="cat src/foo.py")
    assert out[0].receipt_state == RECEIPT_DELIVERED


# --------------------------------------------------------------------------- #
# policy-produced evidence DOES promote — the fix must not disable all promotion
# --------------------------------------------------------------------------- #
def test_b13_policy_text_promotes_referenced() -> None:
    """The agent's OWN message naming the delivered path -> REFERENCED (level 2)."""
    s = _sealed(target="src/foo.py")
    out = ad.update_receipts([s], policy_text="Next I will open src/foo.py to fix it")
    assert out[0].receipt_state == RECEIPT_REFERENCED


def test_b13_policy_action_promotes_acted() -> None:
    """The agent's OWN next tool CALL targeting the delivered path -> ACTED (level 3),
    dominating REFERENCED. next_action_cmd is policy-produced (the agent chose it)."""
    s = _sealed(target="src/foo.py")
    out = ad.update_receipts([s], next_action_cmd="sed -i 's/a/b/' src/foo.py")
    assert out[0].receipt_state == RECEIPT_ACTED


def test_b13_policy_symbol_reference_promotes() -> None:
    """A policy message naming the delivered SYMBOL as a whole token -> REFERENCED."""
    s = _sealed(target="src/foo.py", fact_id="compute_total")
    out = ad.update_receipts([s], policy_text="the bug is in compute_total")
    assert out[0].receipt_state == RECEIPT_REFERENCED


def test_b13_env_output_then_policy_message() -> None:
    """A full turn: env output first (no promotion), then the agent's message (promote).
    Proves the fix leaves the policy channel intact while closing the env channel."""
    s = _sealed(target="src/foo.py")
    after_env = ad.update_receipts([s], env_output="ls\nsrc/foo.py")
    assert after_env[0].receipt_state == RECEIPT_DELIVERED
    after_policy = ad.update_receipts(after_env, policy_text="opening src/foo.py")
    assert after_policy[0].receipt_state == RECEIPT_REFERENCED

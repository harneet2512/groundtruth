"""EvidenceEnvelope — the early-fixed internal evidence contract (subsystem 9 / gt_gt §25).

Every GT producer (localization, context construction, edit-intelligence, verification,
recovery, termination) emits :class:`EvidenceEnvelope` objects and NOTHING ELSE. No
producer renders model-facing text, owns its own dedup, tags, timing policy, or context
budget: the RL-delivery kernel (the Gateway, gt_gt §25) owns ALL model-facing bytes —
normalize event -> collect eligible -> drop stale -> suppress-acquired -> cross-producer
dedup -> resolve conflicts -> minimal dose -> native render -> same-observation insert ->
record delivery + consumption. This module fixes the boundary those stages agree on,
BEFORE the producer subsystems build further. The receipt for fixing it early: B1's
verification engines went offline-green with ZERO live callers because the execution seam
was an afterthought (GT_CONSUMPTION_DOCTRINE §1/§4; gt_gt §24-§26).

PURE · DETERMINISTIC · LLM-FREE · stdlib-only (+ ``groundtruth.delivery.path_policy``).
No time, no randomness, no I/O. THE LAWS (enforced by :func:`validate`):

  (a) LEAK LAW — ``target`` and every ``provenance`` path pass the delivery path policy
      (``is_test_or_demo`` / ``is_vendored_path`` => violation, composed via the Class-A
      chokepoint ``is_deliverable``). Wrong evidence is worse than none; a test identity
      must NEVER reach the model.
  (b) TIER HONESTY — VERIFIED requires nonempty provenance AND confidence >= 0.7;
      HYPOTHESIS caps confidence < 0.7. A tier is a promise about provenance, not a vibe.
  (c) UNMEASURED => ADVISORY — ``blocking_eligibility`` beyond ``"advisory"`` requires
      ``measured=True`` (default ``False``), so nothing unmeasured can be enforcing BY
      CONSTRUCTION (gt_gt §24.4 / §26.6: nothing unmeasured ships as enforcing).
  (d) DEDUP DETERMINISM — ``dedup_key == derive_dedup_key(producer, evidence_type,
      target, fact_id, content_signature(payload, provenance))``, a stable sha
      (no randomness, no time) so the ONE dedup chain is reproducible across
      producers and runs. The FIVE-component derivation carries the Gateway F1
      lesson: ``fact_id`` is the producer-natural SYMBOL (two distinct facts whose
      def sets share the alphabetically-first file must never collide) and the
      content component is the SHIPPED payload+provenance (computed post-leak-filter
      by the producer), so the same fact re-fires when its def-set changes
      (freshness) and never re-fires when identical.

TITO (Tokens-In-Tokens-Out) DELIVERY CONTRACT — the RL-training-safe metadata + the
10 laws. RL training on tool-augmented trajectories requires byte/prefix-preserving
tool-response extension: GT APPENDS facts into a tool observation, and re-rendering
sampled history would train the policy on tokens it never sampled. So every delivery
must carry the metadata that makes it auditable and byte-stable (episode_id, event_id,
parent_observation_hash, payload_schema_version, renderer_id, rendered_bytes_hash,
receipt_state, delivery_reason, suppression_reason). The TEN laws, split by where they
are enforceable:

  LOCALLY TESTABLE NOW (pure, this module + its suite):
    1. APPEND-ONLY — GT extends an observation by appending; rendering an envelope
       NEVER rewrites bytes the policy already sampled (:func:`render_bytes` is a pure
       read; the frozen dataclass makes mutation impossible).
    5. OBSERVATION-PREFIX HASH-CHAIN — each delivery extends a per-episode chain
       ``h_i = sha256(raw32(h_{i-1}) + framed(tool_output_i) + framed(gt_delta_i)
       + framed(boundary_i))`` (:func:`chain_hash`): the parent link is EXACTLY 64
       lowercase hex decoded to its raw 32 bytes (empty => 32 zero bytes genesis) —
       fixed width makes the parent/rest boundary unambiguous with no separator; a
       malformed parent RAISES (corrupt chains fail loudly). B-32: the chain commits
       the FULL observation — the base tool output the GT delta is spliced into, the
       delta itself, and the message-boundary metadata — not just the delta, so two
       DIFFERENT observations carrying identical deltas produce DIFFERENT heads. Each
       field is length-prefixed (``framed``), so no bytes can shift across a field
       boundary; ``tool_output``/``boundary`` default to ``b""`` (the documented "seam
       not yet wired" degrade to a delta-only commitment). GIVEN the fixed-width parent
       + per-field framing + the injective :func:`render_bytes` delta commitment, what
       the suite pins holds: reorder, omission, boundary shift, newline-split,
       payload/provenance fungibility, and a differing base observation all change the
       terminal hash. The claim is exactly that — commitment-chain integrity — not
       general tamper-proofing of anything outside the committed content.
    9. LEAK LAW — no test identity / FAIL_TO_PASS / assertion ever enters the rendered
       bytes; host-side metadata (delivery_reason, renderer_id, receipt_state, …) is
       NEVER rendered — :func:`render_bytes` commits ONLY payload + provenance.
   10. BYTE/PREFIX DETERMINISM — the same envelope renders byte-identical across
       processes / hash seeds / runs (no set-iteration, time, or randomness in render;
       canonical JSON with sorted keys + fixed separators + raw UTF-8).

  ADAPTER OBLIGATIONS (W2 — the observation-splice adapter, documented here, tested there):
    2. ADAPTER BYTE-SPLICE — the rendered bytes are appended at the observation
       boundary the trainer re-materializes, a pure suffix onto the parent observation
       (no re-tokenization of the prefix).
    8. ADAPTER IDEMPOTENCE — re-delivering the same envelope into the same observation
       is a byte-level no-op (dedup at the splice boundary; the dedup_key carries it).

  TRAINER-INTEGRATION OBLIGATIONS (documented only — owned by the RL trainer):
    3. SAMPLED-TOKEN FIDELITY — loss is computed only over tokens the policy sampled;
       appended GT bytes are masked as environment (non-policy) tokens.
    4. CREDIT ASSIGNMENT — GT-appended tokens carry zero policy-gradient weight but
       remain in the value/return computation as environment context.
    6. REPLAY DETERMINISM AT TRAIN TIME — replaying the recorded trajectory reproduces
       the exact appended bytes from the log (rendered_bytes_hash is the check) without
       the live graph.
    7. REWARD ATTRIBUTION — ``receipt_state`` (none -> delivered -> referenced -> acted
       -> resolved_state -> causal) grades a delivery's causal contribution for shaped
       reward / credit.

UNENCODABLE-CONTENT POLICY (W1, bounce #2 2026-07-10). Lone-surrogate strings — the
realistic producer bug is repo file bytes decoded with ``errors="surrogateescape"`` —
cannot be UTF-8 encoded. The contract is total-or-loud, per surface:
  * ``build()`` RAISES a clear ``ValueError`` at construction (producer bug caught at
    the boundary, never a raw ``UnicodeEncodeError`` downstream);
  * ``validate()`` is TOTAL — it NEVER raises; an encode failure surfaces as a
    violation string ("... not UTF-8 encodable ...") in the returned list;
  * ``render_bytes()`` / ``content_signature()`` raise the DOCUMENTED ``ValueError``
    (never a raw ``UnicodeEncodeError``);
  * ``derive_dedup_key()`` is TOTAL (ASCII-escaped JSON framing never fails on str).

ADOPTION NOTE (reconciliation DONE, gt_gt §25.3): the Gateway
(``groundtruth.runtime.gateway``) now emits :class:`EvidenceEnvelope` objects
directly — its former ``Addition`` capsule was REPLACED (option (b), 2026-07-10):
``fact_kind`` -> ``evidence_type``, ``body_lines`` -> ``payload``, ``evidence`` ->
``provenance``, its F1 symbol -> ``fact_id``, and its content-aware dedup key became
:func:`derive_dedup_key`'s five-component derivation here. This module still does NOT
import ``gateway.py`` (the dependency points one way: gateway -> envelope).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar

from .feature_lineage import DeliveryLineage

from groundtruth.delivery.path_policy import is_deliverable

__all__ = [
    "CanonicalRuntimeWitness",
    "EvidenceEnvelope",
    "ObservationBinding",
    "build_observation_binding",
    "observation_binding_to_dict",
    "observation_binding_from_dict",
    "validate_observation_binding",
    "observation_candidate_id",
    "policy_observation_id",
    "feature_opportunity_id",
    "content_signature",
    "derive_dedup_key",
    "runtime_witness_violations",
    "validate",
    "to_dict",
    "from_dict",
    # TITO conformance helpers (locally-testable laws 1/5/9/10)
    "render_bytes",
    "chain_hash",
    # trust tiers
    "VERIFIED",
    "WARNING",
    "INFO",
    "HYPOTHESIS",
    # blocking-eligibility levels
    "ADVISORY",
    "ONE_BOUNCE",
    "BLOCKING",
    # preferred-event vocabulary
    "EVENT_SEARCH",
    "EVENT_VIEW",
    "EVENT_EDIT",
    "EVENT_TEST",
    "EVENT_SUBMIT",
    "EVENT_STEP0",
    # receipt-state vocabulary (TITO reward attribution, law 7)
    "RECEIPT_NONE",
    "RECEIPT_DELIVERED",
    "RECEIPT_REFERENCED",
    "RECEIPT_ACTED",
    "RECEIPT_RESOLVED_STATE",
    "RECEIPT_CAUSAL",
]

# --------------------------------------------------------------------------- #
# controlled vocabularies (the only legal values for the enum-ish fields)
# --------------------------------------------------------------------------- #
VERIFIED = "VERIFIED"
WARNING = "WARNING"
INFO = "INFO"
HYPOTHESIS = "HYPOTHESIS"
_TIERS: frozenset[str] = frozenset({VERIFIED, WARNING, INFO, HYPOTHESIS})

ADVISORY = "advisory"
ONE_BOUNCE = "one_bounce"
BLOCKING = "blocking"
_BLOCKING_LEVELS: frozenset[str] = frozenset({ADVISORY, ONE_BOUNCE, BLOCKING})

EVENT_SEARCH = "search"
EVENT_VIEW = "view"
EVENT_EDIT = "edit"
EVENT_TEST = "test"
EVENT_SUBMIT = "submit"
EVENT_STEP0 = "step0"
_EVENTS: frozenset[str] = frozenset(
    {EVENT_SEARCH, EVENT_VIEW, EVENT_EDIT, EVENT_TEST, EVENT_SUBMIT, EVENT_STEP0}
)

# receipt-state vocabulary (TITO law 7 — reward attribution): the monotone ladder of a
# delivery's observed causal contribution. "none" == not yet delivered (the default);
# any other state is a CLAIM that bytes reached the model, so it REQUIRES delivery
# evidence (rendered_bytes_hash) by validate() law (e).
RECEIPT_NONE = "none"
RECEIPT_DELIVERED = "delivered"  # bytes appended into the observation
RECEIPT_REFERENCED = "referenced"  # the agent quoted / read the delivered fact
RECEIPT_ACTED = "acted"  # the agent took an action following the fact
RECEIPT_RESOLVED_STATE = "resolved_state"  # the run reached a resolved state after it
RECEIPT_CAUSAL = "causal"  # counterfactually attributed (paired evidence)
_RECEIPT_STATES: frozenset[str] = frozenset(
    {
        RECEIPT_NONE,
        RECEIPT_DELIVERED,
        RECEIPT_REFERENCED,
        RECEIPT_ACTED,
        RECEIPT_RESOLVED_STATE,
        RECEIPT_CAUSAL,
    }
)

# THE LADDER'S ORDER, DECLARED ONCE (2026-07-28).
#
# The states above already lived here, but their ORDER did not: it was hand-duplicated as a
# frozen literal in `adapters/miniswe.py:_RECEIPT_ORDER` and again in
# `scripts/swebench/receipt_sidecar.py:_TRANSITION_RANK` -- and the sidecar re-spelled the
# STRINGS too, despite already importing this module. A third copy lives in
# `scripts/gt_substitution_grader.py` as a 4-element tuple that deliberately drops `causal`,
# under a name that COLLIDES with miniswe's but has a different type and membership.
#
# Two things that must agree by hand is the same defect family that produced
# `attestation_persist_error:brief:ValueError` (a producer emitting triples, a consumer
# unpacking pairs, each half agreeing with its own fixture and neither with the other). The
# copies happen to agree today, which is exactly why the drift would be silent.
#
# `MappingProxyType` because the rank map is shared by readers in two packages: a mutable dict
# would let one reader corrupt the other's ladder at a distance.
RECEIPT_LADDER: tuple[str, ...] = (
    RECEIPT_NONE,
    RECEIPT_DELIVERED,
    RECEIPT_REFERENCED,
    RECEIPT_ACTED,
    RECEIPT_RESOLVED_STATE,
    RECEIPT_CAUSAL,
)
RECEIPT_RANK: Mapping[str, int] = MappingProxyType(
    {state: rank for rank, state in enumerate(RECEIPT_LADDER)}
)

# WHICH RUNGS THE RUNTIME MAY WRITE. A property of the ladder, not of any one writer.
#
# Everything above `delivered` is a claim about what the POLICY did after delivery, and the
# env-facing seam cannot evaluate it: it has no `policy_text` (the promotion call site passes
# `""`, so `referenced` is structurally unreachable) and no `decision_commit_index`. The exact
# definition is implemented offline in `fair_probe_result._treatment_acted` as
# `ON_TIME AND registry-receipt-predicate AND delivery_index < action_index <=
# decision_commit_index`. One concept, one authority.
RUNTIME_EMITTABLE_RECEIPT_STATES: frozenset[str] = frozenset({RECEIPT_NONE, RECEIPT_DELIVERED})

# VERIFIED / HYPOTHESIS pivot (parity with the fact-tier conf floor used across GT).
_VERIFIED_MIN_CONF = 0.7


# sha256 hex-digest width — the ONE legal non-empty width for hash-valued fields.
# WIDTH IS LOAD-BEARING (F1, adversarial review 2026-07-10): an any-length hex parent
# let sha256(parent_hex_bytes + rendered) be boundary-shifted (("ab", b"cdef") ==
# ("a", b"bcdef")); exactly-64-hex parents decode to raw 32 bytes = fixed-width
# unambiguous concatenation.
_HASH_HEX_LEN = 64


def _is_hash_shaped(s: str) -> bool:
    """True iff ``s`` is empty OR EXACTLY 64 lowercase hexadecimal characters — the
    shape every hash-valued field must hold (TITO law (f)). Width is pinned to the
    sha256 digest so a stored parent can always be decoded to raw 32 bytes (the F1
    boundary fix); uppercase is rejected so a hash has ONE canonical form."""
    if not s:
        return True
    return len(s) == _HASH_HEX_LEN and all(c in "0123456789abcdef" for c in s)


OBSERVATION_BINDING_SCHEMA = "gt.observation_binding.v1"


@dataclass(frozen=True)
class ObservationBinding:
    """Exact host-only identity of one candidate in one policy observation.

    This is the shared atomic join key for opportunity, delivery, control, receipt,
    and physical-span records. It is audit metadata only: no field is rendered or
    participates in evidence dedup/content identity.
    """

    schema: str
    observation_id: str
    opportunity_id: str
    batch_start_iteration: int
    parent_policy_sha256: str
    parent_policy_chars: int
    action_batch_sha256: str
    candidate_ordinal: int
    candidate_kind_sha256: str
    candidate_dedup_sha256: str
    candidate_id: str


def policy_observation_id(
    start_iteration: int,
    parent_sha256: str,
    action_sha256: str,
) -> str:
    """Framed identity for one parent-policy observation and its exact action batch."""
    framed = (
        b"gt.observation.v1\x00"
        + int(start_iteration).to_bytes(8, "big", signed=False)
        + bytes.fromhex(parent_sha256)
        + bytes.fromhex(action_sha256)
    )
    return hashlib.sha256(framed).hexdigest()


def feature_opportunity_id(
    observation_id: str,
    ordinal: int,
    kind_sha256: str,
    dedup_sha256: str,
) -> str:
    """Framed identity for one candidate opportunity inside an observation."""
    framed = (
        b"gt.opportunity.v1\x00"
        + bytes.fromhex(observation_id)
        + int(ordinal).to_bytes(8, "big", signed=False)
        + bytes.fromhex(kind_sha256)
        + bytes.fromhex(dedup_sha256)
    )
    return hashlib.sha256(framed).hexdigest()


def observation_candidate_id(candidate_id: str) -> str:
    """Return the audit-safe canonical candidate identifier.

    Existing content-addressed IDs are already opaque and remain byte-identical. Any
    producer key that is not a 16/64-character lowercase hexadecimal digest is reduced
    to its full SHA-256 identity so policy text, commands, paths, or symbols cannot be
    copied into host-only observation metadata.
    """
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be non-empty text")
    if len(candidate_id) in {16, 64} and all(char in "0123456789abcdef" for char in candidate_id):
        return candidate_id
    return hashlib.sha256(candidate_id.encode("utf-8", "surrogatepass")).hexdigest()


def build_observation_binding(
    *,
    batch_start_iteration: int,
    parent_policy_sha256: str,
    parent_policy_chars: int,
    action_batch_sha256: str,
    candidate_ordinal: int,
    candidate_kind: str,
    candidate_id: str,
) -> ObservationBinding:
    """Build the canonical candidate/observation binding from producer identities."""
    if type(batch_start_iteration) is not int or batch_start_iteration < 0:
        raise ValueError("batch_start_iteration must be a non-negative integer")
    if type(parent_policy_chars) is not int or parent_policy_chars < 0:
        raise ValueError("parent_policy_chars must be a non-negative integer")
    if type(candidate_ordinal) is not int or candidate_ordinal < 0:
        raise ValueError("candidate_ordinal must be a non-negative integer")
    if not _is_hash_shaped(parent_policy_sha256) or not parent_policy_sha256:
        raise ValueError("parent_policy_sha256 must be 64 lowercase hex chars")
    if not _is_hash_shaped(action_batch_sha256) or not action_batch_sha256:
        raise ValueError("action_batch_sha256 must be 64 lowercase hex chars")
    if not isinstance(candidate_kind, str) or not candidate_kind:
        raise ValueError("candidate_kind must be non-empty text")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("candidate_id must be non-empty text")
    kind_sha256 = hashlib.sha256(candidate_kind.encode("utf-8", "surrogatepass")).hexdigest()
    dedup_sha256 = hashlib.sha256(candidate_id.encode("utf-8", "surrogatepass")).hexdigest()
    canonical_candidate_id = observation_candidate_id(candidate_id)
    observation_id = policy_observation_id(
        batch_start_iteration,
        parent_policy_sha256,
        action_batch_sha256,
    )
    return ObservationBinding(
        schema=OBSERVATION_BINDING_SCHEMA,
        observation_id=observation_id,
        opportunity_id=feature_opportunity_id(
            observation_id,
            candidate_ordinal,
            kind_sha256,
            dedup_sha256,
        ),
        batch_start_iteration=batch_start_iteration,
        parent_policy_sha256=parent_policy_sha256,
        parent_policy_chars=parent_policy_chars,
        action_batch_sha256=action_batch_sha256,
        candidate_ordinal=candidate_ordinal,
        candidate_kind_sha256=kind_sha256,
        candidate_dedup_sha256=dedup_sha256,
        candidate_id=canonical_candidate_id,
    )


def validate_observation_binding(
    binding: ObservationBinding,
    *,
    expected_candidate_id: str | None = None,
) -> list[str]:
    """Return every binding violation in deterministic order."""
    issues: list[str] = []
    if not isinstance(binding, ObservationBinding):
        return ["observation_binding:wrong_type"]
    if binding.schema != OBSERVATION_BINDING_SCHEMA:
        issues.append("observation_binding:schema")
    for field_name in (
        "observation_id",
        "opportunity_id",
        "parent_policy_sha256",
        "action_batch_sha256",
        "candidate_kind_sha256",
        "candidate_dedup_sha256",
    ):
        value = getattr(binding, field_name, "")
        if not isinstance(value, str) or not value or not _is_hash_shaped(value):
            issues.append(f"observation_binding:{field_name}")
    for field_name in (
        "batch_start_iteration",
        "parent_policy_chars",
        "candidate_ordinal",
    ):
        value = getattr(binding, field_name, None)
        if type(value) is not int or value < 0:
            issues.append(f"observation_binding:{field_name}")
    if not isinstance(binding.candidate_id, str) or not binding.candidate_id:
        issues.append("observation_binding:candidate_id")
    if issues:
        return issues
    expected_observation = policy_observation_id(
        binding.batch_start_iteration,
        binding.parent_policy_sha256,
        binding.action_batch_sha256,
    )
    if binding.observation_id != expected_observation:
        issues.append("observation_binding:observation_id_mismatch")
    expected_opportunity = feature_opportunity_id(
        binding.observation_id,
        binding.candidate_ordinal,
        binding.candidate_kind_sha256,
        binding.candidate_dedup_sha256,
    )
    if binding.opportunity_id != expected_opportunity:
        issues.append("observation_binding:opportunity_id_mismatch")
    expected_dedup_sha = hashlib.sha256(
        binding.candidate_id.encode("utf-8", "surrogatepass")
    ).hexdigest()
    if binding.candidate_dedup_sha256 not in {
        expected_dedup_sha,
        binding.candidate_id,
    }:
        issues.append("observation_binding:candidate_dedup_sha256_mismatch")
    if expected_candidate_id is not None:
        try:
            expected_candidate_id = observation_candidate_id(expected_candidate_id)
        except ValueError:
            issues.append("observation_binding:candidate_id_mismatch")
        else:
            if binding.candidate_id != expected_candidate_id:
                issues.append("observation_binding:candidate_id_mismatch")
    return issues


def observation_binding_to_dict(binding: ObservationBinding) -> dict[str, Any]:
    """Canonical JSON-native binding representation shared by every ledger/sidecar."""
    return {
        "schema": binding.schema,
        "observation_id": binding.observation_id,
        "opportunity_id": binding.opportunity_id,
        "batch_start_iteration": binding.batch_start_iteration,
        "parent_policy_sha256": binding.parent_policy_sha256,
        "parent_policy_chars": binding.parent_policy_chars,
        "action_batch_sha256": binding.action_batch_sha256,
        "candidate_ordinal": binding.candidate_ordinal,
        "candidate_kind_sha256": binding.candidate_kind_sha256,
        "candidate_dedup_sha256": binding.candidate_dedup_sha256,
        "candidate_id": binding.candidate_id,
    }


def _binding_nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"observation_binding {field_name} must be a non-negative integer")
    return value


def observation_binding_from_dict(value: object) -> ObservationBinding | None:
    """Rebuild a binding; ``None`` remains the explicit legacy/no-policy state."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("observation_binding must be an object or null")
    binding = ObservationBinding(
        schema=str(value.get("schema", "")),
        observation_id=str(value.get("observation_id", "")),
        opportunity_id=str(value.get("opportunity_id", "")),
        batch_start_iteration=_binding_nonnegative_int(
            value.get("batch_start_iteration"), "batch_start_iteration"
        ),
        parent_policy_sha256=str(value.get("parent_policy_sha256", "")),
        parent_policy_chars=_binding_nonnegative_int(
            value.get("parent_policy_chars"), "parent_policy_chars"
        ),
        action_batch_sha256=str(value.get("action_batch_sha256", "")),
        candidate_ordinal=_binding_nonnegative_int(
            value.get("candidate_ordinal"), "candidate_ordinal"
        ),
        candidate_kind_sha256=str(value.get("candidate_kind_sha256", "")),
        candidate_dedup_sha256=str(value.get("candidate_dedup_sha256", "")),
        candidate_id=str(value.get("candidate_id", "")),
    )
    issues = validate_observation_binding(binding)
    if issues:
        raise ValueError("invalid observation_binding: " + ",".join(issues))
    return binding


# --------------------------------------------------------------------------- #
# deterministic dedup derivation — the canonical key every producer must set.
# --------------------------------------------------------------------------- #
def _commitment_bytes(
    payload: "tuple[str, ...] | list[str]",
    provenance: "tuple[tuple[str, int], ...] | list[tuple[str, int]]",
) -> bytes:
    """The ONE canonical-JSON commitment serialization of shipped content, shared by
    :func:`render_bytes` (chain commitment) and :func:`content_signature` (dedup
    identity) — W2 (bounce #2, 2026-07-10): ONE injective surface, so chain
    commitment and dedup identity can never disagree about what the content IS.
    Canonical JSON of ``{"payload": [...], "provenance": [[file, line], ...]}`` with
    sorted keys, fixed separators, ``ensure_ascii=False``, UTF-8. Raises a DOCUMENTED
    ``ValueError`` (never a raw ``UnicodeEncodeError``) on non-UTF-8-encodable
    content — W1: lone surrogates, e.g. surrogateescape-decoded repo file bytes."""
    obj = {
        "payload": [str(p) for p in (payload or ())],
        "provenance": [[str(f), int(ln)] for f, ln in (provenance or ())],
    }  # noqa: E501 — see _UnencodableContent below for the encode-failure contract
    return _encode_commitment(obj)


class _UnencodableContent(ValueError):
    """Raised ONLY for the UTF-8 encode failure of commitment content (W1 policy).

    A dedicated subclass so ``validate()``'s totality catch cannot swallow or
    mislabel unrelated ``ValueError``s (e.g. an off-type provenance line failing
    ``int(...)`` during obj construction) — Fable re-review finding F-B."""


def _encode_commitment(obj: dict[str, Any]) -> bytes:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except UnicodeEncodeError as e:
        # Dedicated subclass (Fable re-review F-B): validate()'s totality catch must
        # match ONLY the encode failure — an off-type int("abc") ValueError from the
        # obj construction above propagates as its own clear error, never mislabeled
        # as "not UTF-8 encodable".
        raise _UnencodableContent(
            f"content not UTF-8 encodable (lone surrogate — e.g. surrogateescape-"
            f"decoded file bytes; sanitize before shipping evidence): {e}"
        ) from e


def content_signature(
    payload: "tuple[str, ...] | list[str]",
    provenance: "tuple[tuple[str, int], ...] | list[tuple[str, int]]",
) -> str:
    """The deterministic signature of a fact's SHIPPED content (payload + provenance)
    — the content component of :func:`derive_dedup_key`. W2 (bounce #2): the sha256
    hex of the EXACT :func:`render_bytes` canonical-JSON commitment bytes, replacing
    the old unframed ``"\\x00".join``/``";".join`` (whose delimiters were forgeable
    from payload/file-name content: ``("a\\x00b",)`` collided with ``("a", "b")`` and
    ``("x:1;y", 2)`` with ``("x", 1), ("y", 2)`` => wrongful dedup suppression of
    distinct facts). Producers compute it POST-leak-filter (the shipped values);
    :func:`validate` recomputes it from the envelope's own stored fields. Raises the
    documented ``ValueError`` on non-UTF-8-encodable content (W1)."""
    return hashlib.sha256(_commitment_bytes(payload, provenance)).hexdigest()


def derive_dedup_key(
    producer: str,
    evidence_type: str,
    target: str,
    fact_id: str = "",
    content: str = "",
) -> str:
    """The ONE dedup key for a fact: a stable sha over ``(producer, evidence_type,
    target, fact_id, content)``. No randomness, no time — the same fact identity
    always hashes the same, so the delivery kernel's ONE dedup chain is reproducible
    across producers/runs.

    F1 lesson (Gateway bounce, 2026-07-10): the 3-component form collided two
    DISTINCT symbols whose def sets share the alphabetically-first file. ``fact_id``
    (the producer-natural symbol) separates fact identities; ``content`` (the shipped
    payload+provenance via :func:`content_signature`) lets the SAME fact re-fire when
    its def-set changes after an edit (freshness) while an identical repeat stays
    suppressed.

    W2 (bounce #2): the 5 components are framed INJECTIVELY as a canonical JSON list
    — the old ``"\\x00".join`` let a NUL inside one field shift the field boundary
    (``("t\\x00x", "f")`` collided with ``("t", "x\\x00f")``). ``ensure_ascii=True``
    escapes every non-ASCII code point (including lone surrogates), so the derivation
    is TOTAL — it never raises on any str input (W1)."""
    raw = json.dumps(
        [producer or "", evidence_type or "", target or "", fact_id or "", content or ""],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# leak predicate — the single chokepoint (== not is_deliverable), fail-closed.
# --------------------------------------------------------------------------- #
def _leaky_path(path_or_symbol: str) -> bool:
    """True iff ``path_or_symbol`` must NOT surface. A path-shaped value routes through
    ``is_deliverable`` (which composes ``is_test_or_demo`` OR ``is_vendored_path``); a
    bare symbol has no path shape and passes. Any error => treat as a test => leak
    (fail-closed, parity with ``gateway._is_leaky``)."""
    x = (path_or_symbol or "").strip()
    if not x:
        return False
    try:
        return not is_deliverable(x)
    except Exception:  # noqa: BLE001 — unsure => treat as a test => drop
        return True


# --------------------------------------------------------------------------- #
# Canonical runtime witness sidecar.
# --------------------------------------------------------------------------- #
_RUNTIME_WITNESS_EVENT = "canonical_event"
_RUNTIME_WITNESS_COMPUTATION = "deterministic_computation"
_RUNTIME_WITNESS_DIAGNOSTIC = "diagnostic_location"
_RUNTIME_WITNESS_KINDS = frozenset(
    {
        _RUNTIME_WITNESS_EVENT,
        _RUNTIME_WITNESS_COMPUTATION,
        _RUNTIME_WITNESS_DIAGNOSTIC,
    }
)


@dataclass(frozen=True)
class CanonicalRuntimeWitness:
    """Exact host-only proof for evidence derived from runtime state.

    Runtime results do not necessarily have a truthful source line.  This sidecar
    binds such evidence to one of three exact authorities instead: a committed
    canonical event, a deterministic computation, or a diagnostic's exact source
    location.  It is intentionally absent from evidence identity, rendering, and
    serialization; those surfaces remain byte-compatible with legacy envelopes.
    """

    kind: str
    witness_id: str
    content_sha256: str
    source_path: str = ""
    source_line: int = 0
    source_column: int = 0

    def __post_init__(self) -> None:
        violations = runtime_witness_violations(self)
        if violations:
            raise ValueError("; ".join(violations))

    @classmethod
    def canonical_event(
        cls,
        *,
        event_id: str,
        content_sha256: str,
    ) -> "CanonicalRuntimeWitness":
        """Bind evidence to an exact committed canonical-event identity and hash."""
        return cls(
            kind=_RUNTIME_WITNESS_EVENT,
            witness_id=event_id,
            content_sha256=content_sha256,
        )

    @classmethod
    def deterministic_computation(
        cls,
        *,
        computation_id: str,
        content_sha256: str,
    ) -> "CanonicalRuntimeWitness":
        """Bind evidence to the canonical hash of one deterministic computation."""
        return cls(
            kind=_RUNTIME_WITNESS_COMPUTATION,
            witness_id=computation_id,
            content_sha256=content_sha256,
        )

    @classmethod
    def diagnostic_location(
        cls,
        *,
        checker_id: str,
        diagnostic_sha256: str,
        source_path: str,
        source_line: int,
        source_column: int = 0,
    ) -> "CanonicalRuntimeWitness":
        """Bind a diagnostic to its exact deliverable source location and bytes."""
        return cls(
            kind=_RUNTIME_WITNESS_DIAGNOSTIC,
            witness_id=checker_id,
            content_sha256=diagnostic_sha256,
            source_path=source_path,
            source_line=source_line,
            source_column=source_column,
        )


def runtime_witness_violations(
    witness: CanonicalRuntimeWitness,
) -> list[str]:
    """Return deterministic, fail-closed violations for a runtime witness."""
    violations: list[str] = []
    if not isinstance(witness, CanonicalRuntimeWitness):
        return ["runtime witness must be a CanonicalRuntimeWitness"]
    if witness.kind not in _RUNTIME_WITNESS_KINDS:
        violations.append(f"runtime witness kind is unknown: {witness.kind!r}")
    if not isinstance(witness.witness_id, str) or not witness.witness_id.strip():
        violations.append("runtime witness id is required")
    if (
        not isinstance(witness.content_sha256, str)
        or not witness.content_sha256
        or not _is_hash_shaped(witness.content_sha256)
    ):
        violations.append("runtime witness content_sha256 must be exactly 64 lowercase hex")

    if witness.kind == _RUNTIME_WITNESS_DIAGNOSTIC:
        path = witness.source_path
        path_parts = path.split("/") if isinstance(path, str) else ()
        if (
            not isinstance(path, str)
            or not path
            or path.replace("\\", "/") != path
            or path.startswith("/")
            or (len(path) >= 2 and path[1] == ":")
            or ".." in path_parts
            or _leaky_path(path)
        ):
            violations.append(
                "runtime diagnostic source path must be relative, normalized, and deliverable"
            )
        if type(witness.source_line) is not int or witness.source_line < 1:
            violations.append("runtime diagnostic source line must be positive")
        if type(witness.source_column) is not int or witness.source_column < 0:
            violations.append("runtime diagnostic source column must be non-negative")
    elif witness.kind in {
        _RUNTIME_WITNESS_EVENT,
        _RUNTIME_WITNESS_COMPUTATION,
    }:
        if witness.source_path or witness.source_line != 0 or witness.source_column != 0:
            violations.append("event/computation runtime witness cannot carry source location")
    return violations


# --------------------------------------------------------------------------- #
# THE CONTRACT
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvidenceEnvelope:
    """One immutable evidence fact, producer -> delivery kernel.

    Structured, never a model-facing string: ``payload`` is body lines, ``provenance``
    is ``(file, line)`` rows. Rendering, timing, dedup arbitration, and budget are the
    kernel's job — the envelope only DECLARES the fact and its trust/eligibility.
    """

    producer: str  # which intelligence produced the fact (subsystem/engine name)
    fact_id: str  # producer-natural symbol/identifier (traceability; a dedup component)
    target: str  # the file/symbol the fact is ABOUT (leak-checked)
    evidence_type: str  # the fact KIND (was Addition.fact_kind pre-reconciliation); dedup input
    payload: tuple[str, ...] = ()  # body lines (structured, not agent-facing prose)
    provenance: tuple[tuple[str, int], ...] = ()  # (file, line) evidence rows
    confidence: float = 0.0  # [0, 1]
    tier: str = INFO  # VERIFIED / WARNING / INFO / HYPOTHESIS
    graph_revision: str = ""  # the graph the fact was computed against
    valid_until: str = ""  # freshness token (kernel drops when live rev != this)
    preferred_event: str = EVENT_STEP0  # the decision point to deliver AT
    blocking_eligibility: str = ADVISORY  # advisory / one_bounce / blocking
    estimated_cost_tokens: int = 0  # rough render cost (kernel dose accounting)
    measured: bool = False  # has this fact class earned enforcing authority?
    dedup_key: str = ""  # MUST == derive_dedup_key(producer, evidence_type, target,
    #                          fact_id, content_signature(payload, provenance))

    # --- TITO delivery metadata (host-side; NEVER rendered — see LEAK LAW 9). These
    # make a delivery auditable + byte-stable for RL training; they are NOT part of
    # fact identity, so they do NOT feed derive_dedup_key (two deliveries of the SAME
    # fact in different episodes share a dedup_key but differ in episode/event id). ---
    episode_id: str = ""  # per-episode identity (the hash-chain is scoped to it)
    event_id: str = ""  # the tool-event this delivery rode
    parent_observation_hash: str = ""  # h_{i-1} of the chain (law 5; 64-hex or empty)
    payload_schema_version: str = "env.1"  # lets the trainer parse the shipped bytes
    renderer_id: str = ""  # which adapter/renderer produced the bytes (law 2/8)
    rendered_bytes_hash: str = ""  # sha256 hex (64) of the SHIPPED bytes = delivery evidence
    receipt_state: str = RECEIPT_NONE  # reward-attribution ladder (law 7)
    delivery_reason: str = ""  # why delivered (mutually exclusive with suppression)
    suppression_reason: str = ""  # why suppressed (mutually exclusive with delivery)
    # Exact candidate-in-policy-observation identity. Audit-only and never rendered or
    # included in the evidence dedup key; serialized so later receipt transitions retain
    # the originating opportunity rather than borrowing the current turn.
    observation_binding: "ObservationBinding | None" = field(default=None, compare=False)

    # --- SM-10 (2026-07-12) NATIVE-RENDER SIDE-CAR — structured args, NOT identity, NOT
    # serialized. A producer attaches the numeric/structured fields a per-class native
    # renderer (``native_render.render_*_native``) needs to emit its real tool-channel
    # DIAGNOSTIC (mypy/gopls arity error, linter registration warning) instead of the
    # tag-free prose ``_render_generic`` falls back to. It is DELIBERATELY absent from
    # FIELD_ORDER / to_dict / from_dict / derive_dedup_key / content_signature (dedup +
    # byte-chain identity are UNCHANGED), and ``compare=False`` keeps it OUT of __eq__ /
    # __hash__ (two envelopes differing only in native_args are still ==, same hash), so
    # every existing envelope is byte/hash/eq-identical (default None). It travels with the
    # LIVE object via ``dataclasses.replace`` (the seal) — the delivery path renders the
    # object BEFORE any JSON round-trip — never through serialization.
    native_args: "dict[str, Any] | None" = field(default=None, compare=False)
    # Typed feature lineage is an audit-only sidecar.  Like ``native_args`` it is
    # excluded from identity, serialization, rendering, and the dedup key.
    lineage: "DeliveryLineage | None" = field(default=None, compare=False)
    # Structured producer inputs are retained for later producer-owned truth /
    # freshness attestation. They are audit-only and deliberately excluded from
    # identity, serialization, rendering, and the dedup key.
    producer_inputs: "object | None" = field(default=None, compare=False)
    # Canonical decision semantics are a separate producer-owned sidecar.  They
    # must never replace ``producer_inputs``: the latter is the re-verifiable
    # computation input, while this object declares the decision, roles,
    # consequence, authority, and complete revision binding consumed by the
    # reasoning runtime.  Like every other sidecar it is identity-neutral and is
    # deliberately absent from serialization and rendering.
    canonical_semantics: "object | None" = field(default=None, compare=False)
    # Exact proof for runtime-derived evidence that has no truthful source-line
    # provenance.  This sidecar is deliberately identity-, render-, equality-,
    # hash-, and serialization-neutral.  Its integrity is enforced by build() and
    # validate(); consumers may turn a validated witness into host-only audit
    # provenance, but it never enters model-visible bytes.
    runtime_witnesses: "tuple[CanonicalRuntimeWitness, ...]" = field(default=(), compare=False)

    # explicit field order — the source of deterministic (de)serialization order.
    # ClassVar so it is NOT a dataclass field (never in __init__/__eq__/to_dict).
    FIELD_ORDER: ClassVar[tuple[str, ...]] = (
        "producer",
        "fact_id",
        "target",
        "evidence_type",
        "payload",
        "provenance",
        "confidence",
        "tier",
        "graph_revision",
        "valid_until",
        "preferred_event",
        "blocking_eligibility",
        "estimated_cost_tokens",
        "measured",
        "dedup_key",
        # TITO delivery metadata
        "episode_id",
        "event_id",
        "parent_observation_hash",
        "payload_schema_version",
        "renderer_id",
        "rendered_bytes_hash",
        "receipt_state",
        "delivery_reason",
        "suppression_reason",
        "observation_binding",
    )

    @classmethod
    def build(
        cls,
        *,
        producer: str,
        fact_id: str,
        target: str,
        evidence_type: str,
        payload: tuple[str, ...] = (),
        provenance: tuple[tuple[str, int], ...] = (),
        confidence: float = 0.0,
        tier: str = INFO,
        graph_revision: str = "",
        valid_until: str | None = None,
        preferred_event: str = EVENT_STEP0,
        blocking_eligibility: str = ADVISORY,
        estimated_cost_tokens: int = 0,
        measured: bool = False,
        episode_id: str = "",
        event_id: str = "",
        parent_observation_hash: str = "",
        payload_schema_version: str = "env.1",
        renderer_id: str = "",
        rendered_bytes_hash: str = "",
        receipt_state: str = RECEIPT_NONE,
        delivery_reason: str = "",
        suppression_reason: str = "",
        observation_binding: "ObservationBinding | None" = None,
        native_args: "dict[str, Any] | None" = None,
        lineage: "DeliveryLineage | None" = None,
        producer_inputs: "object | None" = None,
        canonical_semantics: "object | None" = None,
        runtime_witnesses: "tuple[CanonicalRuntimeWitness, ...]" = (),
    ) -> EvidenceEnvelope:
        """Construct an envelope with the dedup key DERIVED (never hand-set) and the
        freshness token defaulted to ``graph_revision`` (a fact is valid until the graph
        it was computed against changes). Normalizes payload/provenance to tuples so a
        built envelope is frozen-hashable and round-trips exactly. The dedup key is
        derived over the NORMALIZED payload/provenance (the same values ``validate``
        recomputes from), including ``fact_id`` + content (the F1 discipline).

        The TITO delivery-metadata params are all defaulted, so every existing caller
        (e.g. the Gateway) is byte-for-byte unchanged; they are host-side metadata that
        do NOT feed :func:`derive_dedup_key` (fact identity is producer/type/target/
        fact_id/content only — never the episode a delivery rode).

        W1 (bounce #2): raises a CLEAR ``ValueError`` when payload/provenance contains
        a non-UTF-8-encodable string (lone surrogate) — a producer bug caught loudly
        at construction, never a raw ``UnicodeEncodeError`` downstream."""
        vu = graph_revision if valid_until is None else valid_until
        norm_payload = tuple(payload)
        norm_prov = tuple((str(f), int(ln)) for f, ln in provenance)
        norm_runtime_witnesses = tuple(runtime_witnesses)
        witness_violations = [
            violation
            for witness in norm_runtime_witnesses
            for violation in runtime_witness_violations(witness)
        ]
        if witness_violations:
            raise ValueError(
                "EvidenceEnvelope.build: invalid canonical runtime witness: "
                + "; ".join(witness_violations)
            )
        try:
            sig = content_signature(norm_payload, norm_prov)
        except ValueError as e:
            raise ValueError(
                f"EvidenceEnvelope.build: payload/provenance is not UTF-8 encodable "
                f"— producer bug (sanitize surrogateescape-decoded content before "
                f"constructing evidence): {e}"
            ) from e
        return cls(
            producer=producer,
            fact_id=fact_id,
            target=target,
            evidence_type=evidence_type,
            payload=norm_payload,
            provenance=norm_prov,
            confidence=float(confidence),
            tier=tier,
            graph_revision=graph_revision,
            valid_until=vu,
            preferred_event=preferred_event,
            blocking_eligibility=blocking_eligibility,
            estimated_cost_tokens=int(estimated_cost_tokens),
            measured=bool(measured),
            dedup_key=derive_dedup_key(producer, evidence_type, target, fact_id, sig),
            episode_id=episode_id,
            event_id=event_id,
            parent_observation_hash=parent_observation_hash,
            payload_schema_version=payload_schema_version,
            renderer_id=renderer_id,
            rendered_bytes_hash=rendered_bytes_hash,
            receipt_state=receipt_state,
            delivery_reason=delivery_reason,
            suppression_reason=suppression_reason,
            observation_binding=observation_binding,
            native_args=native_args,
            lineage=lineage,
            producer_inputs=producer_inputs,
            canonical_semantics=canonical_semantics,
            runtime_witnesses=norm_runtime_witnesses,
        )


# --------------------------------------------------------------------------- #
# validation — returns ALL violations (never raises); [] == law-abiding.
# --------------------------------------------------------------------------- #
def validate(env: EvidenceEnvelope) -> list[str]:
    """Return every law violation of ``env`` (empty list == clean). Deterministic
    ordering: enum sanity -> confidence bounds -> (a) leak -> (b) tier honesty ->
    (c) unmeasured=>advisory -> (d) dedup derivation -> (e) receipt evidence ->
    (f) hash shape -> (g) delivery/suppression exclusivity."""
    violations: list[str] = []

    # enum sanity floor (an unknown value can never be silently honored).
    if env.tier not in _TIERS:
        violations.append(f"tier: unknown tier {env.tier!r}")
    if env.blocking_eligibility not in _BLOCKING_LEVELS:
        violations.append(f"blocking_eligibility: unknown level {env.blocking_eligibility!r}")
    if env.preferred_event not in _EVENTS:
        violations.append(f"preferred_event: unknown event {env.preferred_event!r}")

    # confidence bounds.
    if not (0.0 <= env.confidence <= 1.0):
        violations.append(f"confidence: {env.confidence!r} out of [0.0, 1.0]")

    # (a) LEAK LAW — target + every provenance path.
    if _leaky_path(env.target):
        violations.append(f"leak: target {env.target!r} is a test/demo/vendored path")
    for f, ln in env.provenance:
        if _leaky_path(f):
            violations.append(f"leak: provenance {f!r}:{ln} is a test/demo/vendored path")

    # Runtime-witness integrity is independent of the evidence tier. A malformed
    # sidecar cannot rescue otherwise unsupported evidence.
    for witness in env.runtime_witnesses:
        violations.extend(runtime_witness_violations(witness))

    # (b) TIER HONESTY.
    if env.tier == VERIFIED:
        if not env.provenance and not env.runtime_witnesses:
            violations.append(
                "tier: VERIFIED requires nonempty provenance or a canonical runtime witness"
            )
        if env.confidence < _VERIFIED_MIN_CONF:
            violations.append(f"tier: VERIFIED requires confidence >= {_VERIFIED_MIN_CONF}")
    if env.tier == HYPOTHESIS and env.confidence >= _VERIFIED_MIN_CONF:
        violations.append(f"tier: HYPOTHESIS caps confidence < {_VERIFIED_MIN_CONF}")

    # (c) UNMEASURED => ADVISORY (nothing unmeasured can be enforcing).
    if env.blocking_eligibility != ADVISORY and not env.measured:
        violations.append(
            f"authority: blocking_eligibility={env.blocking_eligibility!r} requires measured=True"
        )

    # (d) DEDUP DETERMINISM — recomputed from the envelope's OWN stored fields
    # (fact_id + shipped payload/provenance), so the check needs no outside state.
    # TOTALITY (W1, bounce #2): validate NEVER raises FOR TYPE-CONFORMING envelopes —
    # unencodable content (lone surrogates) surfaces as a violation string, not an
    # exception. Off-type inputs (non-str payload members, non-int provenance lines)
    # raise their own clear TypeError/ValueError — they are contract breaches every
    # production entry point (build/from_dict) rejects earlier. This is the ONLY
    # encode-dependent check (derive_dedup_key itself is total; the leak predicate is
    # already fail-closed via try/except).
    try:
        expected = derive_dedup_key(
            env.producer,
            env.evidence_type,
            env.target,
            env.fact_id,
            content_signature(env.payload, env.provenance),
        )
    except _UnencodableContent:
        violations.append(
            "content: payload/provenance not UTF-8 encodable (lone surrogate) — "
            "dedup key underivable"
        )
    else:
        if env.dedup_key != expected:
            violations.append(f"dedup_key: {env.dedup_key!r} != derive_dedup_key(...) {expected!r}")

    # (e) RECEIPT EVIDENCE (TITO law 7) — receipt_state must be a legal value, and any
    # state past "none" is a CLAIM bytes reached the model, so it REQUIRES delivery
    # evidence (a non-empty rendered_bytes_hash). No receipt without a rendered payload.
    if env.receipt_state not in _RECEIPT_STATES:
        violations.append(f"receipt_state: unknown state {env.receipt_state!r}")
    elif env.receipt_state != RECEIPT_NONE and not env.rendered_bytes_hash:
        violations.append(
            f"receipt_state: {env.receipt_state!r} requires a non-empty "
            f"rendered_bytes_hash (no receipt without delivery evidence)"
        )

    # (f) HASH SHAPE (TITO law (f)) — the observation-chain hashes are empty or EXACTLY
    # 64 lowercase hex (sha256 width; one canonical form). The width is the F1 fix: an
    # any-length parent enabled the chain boundary-shift forgery.
    if not _is_hash_shaped(env.parent_observation_hash):
        violations.append(
            f"parent_observation_hash: {env.parent_observation_hash!r} is not empty "
            f"or exactly {_HASH_HEX_LEN} lowercase hex"
        )
    if not _is_hash_shaped(env.rendered_bytes_hash):
        violations.append(
            f"rendered_bytes_hash: {env.rendered_bytes_hash!r} is not empty or "
            f"exactly {_HASH_HEX_LEN} lowercase hex"
        )

    # (g) DELIVERY / SUPPRESSION EXCLUSIVITY — a delivery is EITHER delivered (with a
    # reason) OR suppressed (with a reason), never both; both set is a contradiction.
    if env.delivery_reason and env.suppression_reason:
        violations.append(
            "delivery_reason and suppression_reason are mutually exclusive (both set)"
        )

    # (h) OBSERVATION BINDING — when present, the sidecar must be internally exact and
    # name this envelope's canonical candidate identity. Legacy/unbound envelopes remain
    # valid with ``None``; a partial or cross-candidate binding fails loudly.
    if env.observation_binding is not None:
        violations.extend(
            validate_observation_binding(
                env.observation_binding,
                expected_candidate_id=env.dedup_key,
            )
        )

    return violations


# --------------------------------------------------------------------------- #
# TITO conformance — the locally-testable RL delivery laws (1, 5, 9, 10).
# --------------------------------------------------------------------------- #
def render_bytes(env: EvidenceEnvelope) -> bytes:
    """The canonical CHAIN-COMMITMENT serialization of the SHIPPED structured content —
    NOT the model-facing display rendering. Renderers own display; W2's adapter hashes
    the bytes it ACTUALLY appends into the observation. This function is what the
    hash-chain (:func:`chain_hash`) commits to.

    Serialization: canonical JSON of ``{"payload": [...], "provenance": [[file, line],
    ...]}`` with ``sort_keys=True``, ``separators=(",", ":")``, ``ensure_ascii=False``,
    encoded UTF-8. This is INJECTIVE on the structured content (F2, adversarial review
    2026-07-10): JSON string framing kills the old ``"\\n".join`` collisions —
    ``payload=("a\\nb",)`` vs ``("a", "b")`` and a payload LINE vs a provenance ROW
    (colon fungibility) now commit to different bytes.

    LAW 9 (leak): it reads ONLY ``payload`` + ``provenance`` (both already
    leak-filtered by the producer / :func:`validate`); the host-side metadata fields
    (renderer_id, delivery_reason, episode_id, …) are NEVER serialized, so no new
    field can smuggle a leak. LAW 10 (determinism): sorted keys, fixed separators,
    order-preserving lists, raw UTF-8 — no set, no time, no randomness — byte-identical
    across processes / hash seeds. LAW 1 (append-only): a PURE read of a frozen
    dataclass; rendering twice yields identical bytes.

    W1/W2 (bounce #2): delegates to the ONE shared :func:`_commitment_bytes`
    serialization (the same bytes :func:`content_signature` hashes), and raises the
    DOCUMENTED ``ValueError`` — never a raw ``UnicodeEncodeError`` — on
    non-UTF-8-encodable content (lone surrogates)."""
    return _commitment_bytes(env.payload, env.provenance)


# Fixed-width length prefix (unsigned 64-bit big-endian) that FRAMES each
# variable-length byte field folded into the chain commitment. Framing makes the
# concatenation INJECTIVE on the field tuple, so no bytes can migrate across a field
# boundary (B-32: the base tool output, the GT delta, and the message-boundary
# metadata commit as three distinct fields, never one fungible blob).
_CHAIN_LEN_PREFIX_BYTES = 8


def _framed(b: bytes) -> bytes:
    """Length-prefix ``b`` (8-byte big-endian length, then the raw bytes) so a
    concatenation of framed fields is injective on the field tuple: two different
    ``(tool_output, gt, boundary)`` splits of the same underlying byte stream commit
    to DIFFERENT bytes. This generalizes the single-field boundary defense (the
    fixed-32-byte parent) to the multi-field observation commitment."""
    return len(b).to_bytes(_CHAIN_LEN_PREFIX_BYTES, "big") + bytes(b)


def chain_hash(
    parent_hash: str,
    gt_bytes: bytes,
    *,
    tool_output_bytes: bytes = b"",
    boundary: bytes = b"",
) -> str:
    """One step of the observation-prefix hash-chain (TITO LAW 5), committing the
    FULL observation — not merely the appended GT delta (B-32 fix):

        ``sha256(parent_raw_32 + framed(tool_output_bytes) + framed(gt_bytes)
                 + framed(boundary))``

    * ``parent_raw_32`` is the parent link decoded from EXACTLY 64 lowercase hex to
      its raw 32 bytes, or 32 zero bytes for the chain's genesis (``parent_hash ==
      ""``). The FIXED-WIDTH parent (F1, adversarial review 2026-07-10) makes the
      parent/rest boundary unambiguous with no separator; a non-empty parent that is
      not exactly 64 lowercase hex RAISES ``ValueError`` (a corrupt chain fails
      loudly, never silently hashes). ``None``/``bytes`` parents also RAISE (W3).
    * ``gt_bytes`` are the EXACT appended GT delta bytes; ``tool_output_bytes`` are
      the EXACT base tool-output bytes of the observation the delta is spliced into;
      ``boundary`` is the message-boundary metadata (e.g. a canonical encoding of the
      splice offset / message framing). Each is length-prefixed by :func:`_framed`,
      so the three fields cannot collide by shifting bytes across their boundaries
      (mutation M4 — dropping the framing — re-opens that shift).

    B-32 (this fix): the old step committed ONLY the GT delta
    (``sha256(parent_raw + rendered)``), so two DIFFERENT agent observations that
    receive identical GT deltas produced the SAME chain — it could not establish
    observation-prefix integrity / exact TITO replay. Folding the base tool output
    and the message boundary into the commitment fixes that: a different base
    observation now yields a different chain head even under an identical delta
    (mutation M5 — dropping ``tool_output_bytes`` — makes those heads EQUAL again).

    ``tool_output_bytes`` / ``boundary`` default to ``b""`` — the DOCUMENTED "seam
    not yet wired" state: the caller (the mini seam, via :func:`seal_delivery`) has
    not yet supplied the base observation, so the step degrades EXPLICITLY to the
    GT-delta-only commitment. The chain head is audit metadata that is NEVER appended
    to an observation, so this change is not a model-facing byte change; a 2-argument
    caller (relational chain tests, dedup identity) keeps its exact relationships.

    Deterministic (no time / randomness): replaying a trajectory (base outputs +
    deltas + boundaries) reproduces the identical chain. The parent is load-bearing
    (dropping it collapses the chain to a per-link content hash that no longer detects
    reorder/omission — mutation M1); the width gate closes the parent boundary shift
    (mutation M3). NOTE: genesis (32 zero bytes) equals a literal all-zeros parent by
    construction — the zero hash IS the genesis encoding (standard hash-chain
    convention)."""
    if not isinstance(parent_hash, str):
        raise ValueError(
            f"chain_hash: parent must be a str (empty or {_HASH_HEX_LEN} lowercase "
            f"hex), got {type(parent_hash).__name__}"
        )
    for _name, _val in (
        ("gt_bytes", gt_bytes),
        ("tool_output_bytes", tool_output_bytes),
        ("boundary", boundary),
    ):
        if not isinstance(_val, (bytes, bytearray)):
            raise ValueError(f"chain_hash: {_name} must be bytes, got {type(_val).__name__}")
    if not parent_hash:
        parent_raw = b"\x00" * 32  # genesis: the zero hash
    elif len(parent_hash) == _HASH_HEX_LEN and all(c in "0123456789abcdef" for c in parent_hash):
        parent_raw = bytes.fromhex(parent_hash)
    else:
        raise ValueError(
            f"chain_hash: parent must be empty or exactly {_HASH_HEX_LEN} lowercase "
            f"hex, got {parent_hash!r}"
        )
    return hashlib.sha256(
        parent_raw + _framed(tool_output_bytes) + _framed(gt_bytes) + _framed(boundary)
    ).hexdigest()


# --------------------------------------------------------------------------- #
# serialization — deterministic, exact round-trip.
# --------------------------------------------------------------------------- #
def to_dict(env: EvidenceEnvelope) -> dict[str, Any]:
    """A JSON-serializable dict with keys in :attr:`EvidenceEnvelope.FIELD_ORDER`.
    Tuples become lists (JSON has no tuple); ``from_dict`` restores them exactly."""
    return {
        "producer": env.producer,
        "fact_id": env.fact_id,
        "target": env.target,
        "evidence_type": env.evidence_type,
        "payload": list(env.payload),
        "provenance": [[f, ln] for f, ln in env.provenance],
        "confidence": env.confidence,
        "tier": env.tier,
        "graph_revision": env.graph_revision,
        "valid_until": env.valid_until,
        "preferred_event": env.preferred_event,
        "blocking_eligibility": env.blocking_eligibility,
        "estimated_cost_tokens": env.estimated_cost_tokens,
        "measured": env.measured,
        "dedup_key": env.dedup_key,
        "episode_id": env.episode_id,
        "event_id": env.event_id,
        "parent_observation_hash": env.parent_observation_hash,
        "payload_schema_version": env.payload_schema_version,
        "renderer_id": env.renderer_id,
        "rendered_bytes_hash": env.rendered_bytes_hash,
        "receipt_state": env.receipt_state,
        "delivery_reason": env.delivery_reason,
        "suppression_reason": env.suppression_reason,
        "observation_binding": (
            observation_binding_to_dict(env.observation_binding)
            if env.observation_binding is not None
            else None
        ),
    }


def from_dict(d: dict[str, Any]) -> EvidenceEnvelope:
    """Inverse of :func:`to_dict`. ``from_dict(to_dict(env)) == env`` exactly (payload
    and provenance rebuilt as tuples; numeric/bool coercion mirrors ``build``). Input is
    ``Any``-typed because JSON is untyped — coercion is the boundary's job."""
    prov_raw = d.get("provenance", ()) or ()
    return EvidenceEnvelope(
        producer=str(d.get("producer", "")),
        fact_id=str(d.get("fact_id", "")),
        target=str(d.get("target", "")),
        evidence_type=str(d.get("evidence_type", "")),
        payload=tuple(d.get("payload", ()) or ()),
        provenance=tuple((str(f), int(ln)) for f, ln in prov_raw),
        confidence=float(d.get("confidence", 0.0)),
        tier=str(d.get("tier", INFO)),
        graph_revision=str(d.get("graph_revision", "")),
        valid_until=str(d.get("valid_until", "")),
        preferred_event=str(d.get("preferred_event", EVENT_STEP0)),
        blocking_eligibility=str(d.get("blocking_eligibility", ADVISORY)),
        estimated_cost_tokens=int(d.get("estimated_cost_tokens", 0)),
        measured=bool(d.get("measured", False)),
        dedup_key=str(d.get("dedup_key", "")),
        episode_id=str(d.get("episode_id", "")),
        event_id=str(d.get("event_id", "")),
        parent_observation_hash=str(d.get("parent_observation_hash", "")),
        payload_schema_version=str(d.get("payload_schema_version", "env.1")),
        renderer_id=str(d.get("renderer_id", "")),
        rendered_bytes_hash=str(d.get("rendered_bytes_hash", "")),
        receipt_state=str(d.get("receipt_state", RECEIPT_NONE)),
        delivery_reason=str(d.get("delivery_reason", "")),
        suppression_reason=str(d.get("suppression_reason", "")),
        observation_binding=observation_binding_from_dict(d.get("observation_binding")),
    )

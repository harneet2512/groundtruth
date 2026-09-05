"""Provider-terminal delivery witness for the Mini-SWE/LiteLLM boundary.

The adapter is intentionally narrow: it stages a compiled capsule, appends one
structural content block only after Mini-SWE has prepared native messages, binds
and dispatches those exact bytes, records provider-terminal inference, and
commits the returned response only when the agent accepts it into its trajectory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from types import MethodType
from typing import Any, Callable, Mapping

from groundtruth.runtime.control_participation import participation_to_dict
from groundtruth.runtime.evidence_envelope import (
    ObservationBinding,
    observation_binding_to_dict,
    validate_observation_binding,
)
from groundtruth.runtime.ledger import append_ledger_line, resolve_sink_path
from groundtruth.runtime.reasoning_runtime import (
    AttemptReasoningRuntime,
    CapsuleCompilation,
    CapsuleCompilationState,
    DeliveryAttempt,
    DeliveryState,
    EvidenceLifecycle,
    ModelCallAttempt,
    ProviderTerminalKind,
    advance_delivery,
    bind_capsule_to_final_payload,
    commit_response,
    record_delivery_failure,
    record_delivery_withheld,
    record_provider_terminal,
    verify_bound_payload_at_dispatch,
)
from groundtruth.runtime.shadow_holdout import HOLDOUT as _SHADOW_HOLDOUT
from groundtruth.runtime.terminal_ack import (
    TerminalAckIdentity,
    build_ack_participation,
)
from groundtruth.runtime.terminal_evidence import (
    ClosedBlockerRegistry,
    SubmitSuppressionReceipt,
    compile_submit_suppression,
)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# L2 SAME-STATE COUNTERFACTUAL (#31, 2026-07-28). The cheapest REAL causal signal
# available without a paired run: after the real call, optionally re-issue the SAME
# turn with the staged capsule removed and record both arms' actions.
#
# Every other GT signal is observational -- GT delivered, the agent did something, and we
# argue about whether the two are related. Here the state is IDENTICAL (same messages,
# same kwargs, same provider, same turn) and the ONLY difference is the capsule.
#
# IT DOES NOT SIGN ITSELF, deliberately. Deciding whether the GT-arm action was BETTER
# needs an anchor this layer does not have and must not invent, so the pair is recorded
# UNSIGNED and the direction is computed offline where a measurement-only anchor exists.
# An unsigned pair is honest; a direction invented here would be telemetry dressed as
# evidence. Note an unsigned magnitude also hides HARM, which is exactly why the SIGN
# must be computed somewhere that can actually establish it.
# ─────────────────────────────────────────────────────────────────────────────
_COUNTERFACTUAL_SCHEMA = "gt.counterfactual_pair.v1"
_ACTION_FENCE_RE = re.compile(r"```(?:bash|sh|shell)?\s*\n(.*?)```", re.DOTALL)


def _response_text(response: Any) -> str:
    """Best-effort assistant text. NEVER raises: a malformed provider object reads ''."""
    try:
        choices = getattr(response, "choices", ()) or ()
        if not choices:
            return ""
        first = choices[0]
        message = getattr(first, "message", None)
        content = getattr(message, "content", None)
        if content is None and isinstance(first, Mapping):
            content = (first.get("message") or {}).get("content")
        return content if isinstance(content, str) else ""
    except Exception:  # noqa: BLE001 -- measurement must never break the turn
        return ""


def _first_action(text: str) -> str:
    """The first fenced command block — a PROXY for the action the agent will take.

    Named a proxy because that is what it is: the boundary cannot run the agent's parser,
    so this approximates the action rather than reproducing it. No fence => "" (no guess).
    """
    match = _ACTION_FENCE_RE.search(text or "")
    return match.group(1).strip() if match else ""


def _usage_tokens(response: Any) -> dict[str, int]:
    """Token usage for ONE response. Never raises; absence or garbage reads 0.

    TOKENS, NOT USD, on purpose. Pricing belongs to the run's cost note, which knows the
    model and the rate card; computing dollars here would couple this boundary to one
    provider's calculator and rot silently when rates change. Tokens are provider-agnostic.
    """
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    try:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, Mapping):
            usage = response.get("usage")
        if usage is None:
            return out
        for key in out:
            value = getattr(usage, key, None)
            if value is None and isinstance(usage, Mapping):
                value = usage.get(key)
            # `bool` is an int subclass; a provider sending True must not read as 1 token.
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = value
    except Exception:  # noqa: BLE001 -- accounting must never break the turn
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return out


def _l2_probe_rate() -> float:
    """`GT_L2_PROBE_RATE`, default 0.0 => OFF and byte-identical. Clamped to [0, 1]."""
    try:
        rate = float(os.environ.get("GT_L2_PROBE_RATE", "0") or "0")
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, rate))


def _l2_probe_selected(key: str, rate: float) -> bool:
    """DETERMINISTIC sampling on the model-call id — never `random`.

    A random probe would make the same recording replay differently, and SS-10 replay
    already suffers from stale recordings; a nondeterministic measurement would be
    indistinguishable from a replay defect.
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) / 0xFFFFFFFF) < rate


def _maybe_record_counterfactual_pair(
    boundary: Any,
    messages: Any,
    active: Any,
    response: Any,
    kwargs: Mapping[str, Any],
) -> None:
    """Record one same-state counterfactual pair. Returns None always; never raises.

    The counterfactual response is DISCARDED — it is never returned to the agent and
    never enters the delivery state machine. It has no capsule bound to it, so a row that
    could be mistaken for a delivery would be worse than no measurement: the emitted row
    is deliberately schema'd and layered OUTSIDE the delivery-proof namespace, carries
    `chars_delivered: 0`, and omits `delivery_attempt_id` / `content_sha256_16` entirely.
    """
    try:
        rate = _l2_probe_rate()
        model_call_id = str(getattr(active, "model_call_id", "") or "")
        if not _l2_probe_selected(model_call_id, rate):
            return None
        native_messages = boundary._without_staged_capsule(messages, active)
        control = boundary._original_query(native_messages, **dict(kwargs or {}))
        treatment_action = _first_action(_response_text(response))
        control_action = _first_action(_response_text(control))
        row = {
            "schema": _COUNTERFACTUAL_SCHEMA,
            "layer": "measurement.counterfactual_pair",
            "event_type": "l2_counterfactual_pair",
            "outcome": "measurement_only",
            "chars_delivered": 0,
            "model_call_id": model_call_id,
            "observation_id": str(getattr(active, "observation_id", "") or ""),
            "capsule_hash": str(getattr(active, "capsule_hash", "") or ""),
            "probe_rate": rate,
            "treatment_action": treatment_action,
            "control_action": control_action,
            "treatment_action_sha256_16": hashlib.sha256(
                treatment_action.encode("utf-8")
            ).hexdigest()[:16],
            "control_action_sha256_16": hashlib.sha256(
                control_action.encode("utf-8")
            ).hexdigest()[:16],
            "actions_differ": treatment_action != control_action,
            # SPEND THE PROBE ITSELF INCURRED. `_original_query` is the LOW-LEVEL `_query`;
            # LitellmModel accounts cost in the PUBLIC `query` (`_calculate_cost` ->
            # `GLOBAL_MODEL_STATS.add`), so this completion is really billed but invisible
            # to GLOBAL_MODEL_STATS / agent.cost / total_cost_usd. Declaring it here lets a
            # run's cost note ADD measurement overhead instead of silently omitting it.
            #
            # Calling the public `query` instead would "fix" the accounting by adding this
            # cost to the AGENT's — inflating the GT arm with measurement overhead and
            # corrupting the very on/off comparison the probe exists to make. The low-level
            # call is the RIGHT choice; only the silent omission was wrong.
            "measurement_overhead_tokens": _usage_tokens(control),
            # UNSIGNED BY CONSTRUCTION. This layer has no anchor; offline analysis signs it.
            "signed": False,
        }
        append_ledger_line(row, boundary._receipt_sink_path)
    except Exception:  # noqa: BLE001 -- the agent's turn is not ours to break
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CAPSULE-LEVEL SHADOW HOLDOUT (#30 step 3, 2026-07-28). Compile the capsule, then
# DELIBERATELY do not send it, so the coin's decision becomes measurable.
#
# OFF unless BOTH `GT_SS_SHADOW` and a positive `GT_SS_SHADOW_RATE` are set. A knob that
# withholds evidence from the agent must never act on a default.
# ─────────────────────────────────────────────────────────────────────────────
_SHADOW_ASSIGNMENT_SCHEMA = "gt.shadow_assignment.v1"

# The class axis of the capsule draw, held CONSTANT so only capsule identity varies it. Any
# participating class serves; it is a coordinate, not an assertion about what the capsule
# carries. Every real member's participation is checked separately and strictly.
_CAPSULE_DRAW_CLASS = "localization"


def _shadow_enabled() -> bool:
    raw = str(os.environ.get("GT_SS_SHADOW", "") or "").strip().lower()
    return raw not in {"", "0", "false", "no", "off", "none"}


def _capsule_member_classes(compilation: Any) -> tuple[str, ...]:
    """The fact classes carried by this capsule. Absence reads as empty (=> DELIVER)."""
    try:
        members = getattr(compilation, "member_fact_classes", None) or ()
        return tuple(str(m) for m in members if str(m))
    except Exception:  # noqa: BLE001 -- a measurement must never break the turn
        return ()


def _capsule_holdout_verdict(task_id: str, compilation: Any) -> str:
    """DELIVER or HOLDOUT for ONE capsule. Never raises; every fault path DELIVERS.

    SAFETY FIRST. A capsule carries SEVERAL evidence records and withholding it withholds ALL
    of them, so ONE non-participating member ships the WHOLE capsule. `submit_refusal` and
    `syntax_result` are safety-excluded: withholding "your patch is broken" would actively harm
    the agent, which is the opposite of what this arm exists to measure.

    ONE DRAW PER CAPSULE, and that is the load-bearing decision. The obvious composition --
    HOLDOUT iff `assign()` says HOLDOUT for EVERY member -- is conservative and WRONG: with
    rate 0.5 and three members, P(all HOLDOUT) is about 0.125, so the arm would RECORD a
    propensity of 0.5 while actually drawing 0.125, and every weighted estimate built on it
    would be silently biased. A randomized arm whose recorded propensity does not match its
    draw is worse than no arm. The capsule is ONE dose, so it gets ONE draw keyed on
    `capsule_hash`; participation is established for all members first, so the class argument
    then only gates participation.
    """
    try:
        from groundtruth.runtime.shadow_holdout import (
            DELIVER,
            assign,
            is_participating,
            parse_rate,
        )

        if not _shadow_enabled():
            return DELIVER
        rate = parse_rate(os.environ.get("GT_SS_SHADOW_RATE", "0"))
        if rate <= 0.0:
            return DELIVER
        classes = _capsule_member_classes(compilation)
        if not classes or not all(is_participating(c) for c in classes):
            return DELIVER
        # FIXED draw coordinate, NOT a claim about content. `assign`'s bucket folds
        # `fact_class` in, so deriving it from the members (e.g. `sorted(classes)[0]`) makes
        # the verdict depend on capsule COMPOSITION: the same capsule hash would be held out
        # or delivered according to which classes happen to ride along, and the recorded
        # propensity would again be a lie. My first draft did exactly that and
        # `test_member_count_does_not_shrink_the_holdout_share` caught it.
        #
        # The randomization unit is the CAPSULE, so only `dedup_key=capsule_hash` may vary the
        # draw. Participation of every REAL member is established above; this constant is
        # merely a participating coordinate that holds the class axis still.
        return assign(
            task_id=str(task_id or ""),
            fact_class=_CAPSULE_DRAW_CLASS,
            dedup_key=str(getattr(compilation, "capsule_hash", "") or ""),
            rate=rate,
        )
    except Exception:  # noqa: BLE001 -- fail OPEN: never withhold because of a fault
        from groundtruth.runtime.shadow_holdout import DELIVER

        return DELIVER


def _record_shadow_assignment_row(
    boundary: Any,
    task_id: str,
    compilation: Any,
    arm: str,
) -> None:
    """Record the assignment on BOTH arms. Never raises.

    Recording only holdouts makes the propensity uncomputable -- you cannot estimate a
    probability from the numerator alone. The withheld render is accounted by SEAL
    (`withheld_capsule_hash`) with ZERO model-facing bytes, so the arm is fully auditable
    without ever putting the withheld text where a reader could mistake it for a delivery.
    """
    try:
        from groundtruth.runtime.shadow_holdout import parse_rate

        rate = parse_rate(os.environ.get("GT_SS_SHADOW_RATE", "0"))
        row = {
            "schema": _SHADOW_ASSIGNMENT_SCHEMA,
            "layer": "measurement.shadow_assignment",
            "event_type": "shadow_assignment",
            "outcome": "measurement_only",
            "chars_delivered": 0,
            "arm": str(arm),
            "propensity": rate,
            "task_id": str(task_id or ""),
            "model_call_id": str(getattr(compilation, "model_call_id", "") or ""),
            "observation_id": str(getattr(compilation, "observation_id", "") or ""),
            "withheld_capsule_hash": str(
                getattr(compilation, "capsule_hash", "") or ""
            ),
            "member_fact_classes": list(_capsule_member_classes(compilation)),
        }
        append_ledger_line(row, boundary._receipt_sink_path)
    except Exception:  # noqa: BLE001 -- accounting must never break the turn
        return None
    return None


def _response_id(response: Any) -> str:
    value = getattr(response, "id", "")
    if isinstance(value, str):
        return value
    return ""


def _provider_status(response: Any) -> str:
    return str(getattr(response, "status", "") or "").lower()


def _terminal_kind(response: Any) -> ProviderTerminalKind | None:
    status = _provider_status(response)
    if status in {"queued", "pending", "running", "in_progress"}:
        return None
    if status == "failed":
        return ProviderTerminalKind.FAILED
    if status == "cancelled":
        return ProviderTerminalKind.CANCELLED
    if status == "partial_stream":
        return ProviderTerminalKind.PARTIAL_STREAM
    if status not in {"", "completed", "incomplete"}:
        return None
    choices = getattr(response, "choices", ()) or ()
    if not choices:
        return None
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    if message is None:
        return None
    refusal = getattr(message, "refusal", None)
    finish_reason = str(getattr(choice, "finish_reason", "") or "").lower()
    if refusal:
        return ProviderTerminalKind.REFUSAL
    if status == "incomplete" or finish_reason in {
        "length",
        "max_tokens",
        "max_output_tokens",
    }:
        return ProviderTerminalKind.INCOMPLETE
    if finish_reason in {"tool_calls", "tool_use"}:
        return ProviderTerminalKind.TOOL_USE
    return ProviderTerminalKind.COMPLETED


@dataclass(frozen=True)
class _PendingCommit:
    compilation: CapsuleCompilation
    delivery_attempt_id: str
    delivered_iteration: int
    decision_window_key: str
    delivery_phase_ordinal: int
    observation_binding: ObservationBinding | None
    canonical_delivery_emitted: bool


def _normalized_phrase(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _explicit_claim_reference(content: object, claim: object) -> bool:
    normalized_claim = _normalized_phrase(claim)
    normalized_content = _normalized_phrase(content)
    return bool(
        normalized_claim
        and normalized_content
        and normalized_claim in normalized_content
    )


def _path_anchor(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().replace("\\", "/")
    candidate = candidate.split("::", 1)[0]
    candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
    if candidate.startswith("/testbed/"):
        candidate = candidate[len("/testbed/"):]
    return candidate if "/" in candidate else ""


def _symbol_anchor(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.rsplit("::", 1)[-1].strip()
    if "/" in candidate or "\\" in candidate:
        return ""
    return (
        candidate
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", candidate)
        else ""
    )


def _materially_linked_action(
    actions: object,
    evidence: Any,
) -> bool:
    if not isinstance(actions, (list, tuple)):
        return False
    subject = (
        evidence.get("subject", "")
        if isinstance(evidence, Mapping)
        else getattr(evidence, "subject", "")
    )
    provenance = (
        evidence.get("provenance", ())
        if isinstance(evidence, Mapping)
        else getattr(evidence, "provenance", ())
    )
    if not isinstance(provenance, (list, tuple)):
        return False
    path_anchors = {
        anchor
        for value in (subject, *provenance)
        if (anchor := _path_anchor(value))
    }
    symbol_anchors = {
        anchor
        for value in (subject, *provenance)
        if (anchor := _symbol_anchor(value))
    }
    for action in actions:
        if not isinstance(action, Mapping):
            continue
        fragments = [
            action.get(key)
            for key in ("command", "path", "file_path", "target", "subject")
            if isinstance(action.get(key), str)
        ]
        action_text = " ".join(fragments).replace("\\", "/")
        if not action_text:
            continue
        if any(
            re.search(
                rf"(?<![A-Za-z0-9_./-]){re.escape(path)}"
                rf"(?![A-Za-z0-9_./-])",
                action_text,
            )
            for path in path_anchors
        ):
            return True
        if any(
            re.search(rf"(?<!\w){re.escape(symbol)}(?!\w)", action_text)
            for symbol in symbol_anchors
        ):
            return True
    return False


class MiniSweProviderBoundary:
    """Install the sole provider/response delivery lifecycle on one model+agent."""

    def __init__(
        self,
        *,
        model: Any,
        agent: Any,
        attempt_runtime: AttemptReasoningRuntime | None = None,
        fault_handler: Callable[[str, BaseException], Any] | None = None,
        delivery_handler: Callable[[CapsuleCompilation], Any] | None = None,
    ):
        self.model = model
        self.agent = agent
        self.attempt_runtime = attempt_runtime
        self.fault_handler = fault_handler
        # Success-path twin of ``fault_handler``: called with the compilation AFTER its
        # canonical delivery row is durably written. It exists so the SEAM can finalize
        # producer attestations at the one moment both halves of the join identity are
        # known -- the delivered bytes' seal and the evidence lineage -- without this
        # module re-deriving the attestation output root and becoming a second authority
        # for where audit artifacts land.
        self.delivery_handler = delivery_handler
        self._records: list[DeliveryAttempt] = []
        self._fallback_records: list[DeliveryAttempt] = []
        self._delivery_attempt_ids: list[str] = []
        self._bound_compilations: list[CapsuleCompilation] = []
        self._active: CapsuleCompilation | None = None
        self._active_delivery_attempt_id = ""
        self._active_observation_binding: ObservationBinding | None = None
        self._pending_commits: dict[str, _PendingCommit] = {}
        self._ack_receipt_keys: set[str] = set()
        self._ack_receipts: list[dict[str, Any]] = []
        self._submit_suppression_receipts: list[SubmitSuppressionReceipt] = []
        self._provider_phase_ordinal = 0
        self._receipt_sink_path = resolve_sink_path()
        self._original_prepare = model._prepare_messages_for_api
        self._original_query = model._query
        self._original_model_query = getattr(model, "query", None)
        self._original_add_messages = agent.add_messages
        self._install()

    @property
    def records(self) -> tuple[DeliveryAttempt, ...]:
        if self.attempt_runtime is not None:
            persisted = tuple(
                record
                for delivery_attempt_id in self._delivery_attempt_ids
                for record in self.attempt_runtime.journal.delivery_history(
                    delivery_attempt_id
                )
            )
            return persisted + tuple(self._fallback_records)
        return tuple(self._records)

    @property
    def bound_compilations(self) -> tuple[CapsuleCompilation, ...]:
        return tuple(self._bound_compilations)

    @property
    def acknowledgment_receipts(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in self._ack_receipts)

    @property
    def submit_suppression_receipts(self) -> tuple[SubmitSuppressionReceipt, ...]:
        return tuple(self._submit_suppression_receipts)

    def authorize_submit_suppression(
        self,
        *,
        registry: ClosedBlockerRegistry,
        current_revision: str,
        current_invalidation_keys: Mapping[str, str],
        action_bytes: bytes,
        provider_payload_bytes: bytes,
    ) -> SubmitSuppressionReceipt | None:
        """Authorize a pre-provider submit suppression and persist zero delivery.

        Returning a receipt means the caller must not dispatch the selected submit
        action. Returning ``None`` is fail-open and preserves the native path. The
        recorded payload hash is over the exact provider payload with the action
        absent; ``chars_delivered`` is mechanically fixed at zero.
        """

        try:
            receipt = compile_submit_suppression(
                registry=registry,
                current_revision=current_revision,
                current_invalidation_keys=current_invalidation_keys,
                action_bytes=action_bytes,
                provider_payload_bytes=provider_payload_bytes,
            )
        except Exception:  # noqa: BLE001 -- suppression must fail open
            return None
        if receipt is None:
            return None
        row = {
            "layer": "provider.submit_suppression",
            "event_type": "submit_suppression",
            "outcome": "suppressed",
            **asdict(receipt),
        }
        try:
            append_ledger_line(row, self._receipt_sink_path)
        except Exception:  # noqa: BLE001 -- no durable zero-delivery proof => native submit
            return None
        self._submit_suppression_receipts.append(receipt)
        return receipt

    @property
    def has_unconsumed_capsule(self) -> bool:
        """Whether the provider slot is occupied by an unfinished capsule.

        This is the public host-side guard for the one-capsule-per-observation
        invariant.  It intentionally exposes only occupancy: callers must
        leave later evidence in the reasoning runtime rather than inspecting,
        replacing, or clearing the staged compilation.
        """

        return self._active is not None

    def stage(
        self,
        compilation: CapsuleCompilation,
        *,
        delivery_attempt_id: str = "",
        observation_binding: ObservationBinding | None = None,
    ) -> None:
        if compilation.state is CapsuleCompilationState.DISABLED:
            # A disabled candidate means "do not stage this candidate."  It is
            # never authority to discard an earlier capsule whose provider
            # inference has not reached a terminal response.
            return
        if self._active is not None:
            raise ValueError(
                "observation_id already staged with a compiled capsule"
            )
        if (
            compilation.state is not CapsuleCompilationState.COMPILED
            or compilation.delivery_attempt is None
            or compilation.delivery_attempt.state is not DeliveryState.COMPILED
        ):
            raise ValueError("Mini-SWE boundary requires a COMPILED capsule")
        if any(
            record.model_call_id == compilation.model_call_id
            for record in self.records
        ):
            raise ValueError("model_call_id already has a delivery attempt")
        if self.attempt_runtime is not None and observation_binding is None:
            raise ValueError(
                "canonical runtime boundary requires observation_binding"
            )
        if (
            observation_binding is not None
            and validate_observation_binding(
                observation_binding,
                expected_candidate_id=compilation.capsule_hash,
            )
        ):
            raise ValueError(
                "observation_binding does not identify the staged capsule"
            )
        prior_for_observation = [
            record for record in self.records
            if record.observation_id == compilation.observation_id
        ]
        if prior_for_observation:
            latest_by_call = {
                record.model_call_id: record
                for record in prior_for_observation
            }
            retryable_states = {
                DeliveryState.JOIN_FAILED,
                DeliveryState.DISPATCH_FAILED,
                DeliveryState.PROVIDER_REJECTED,
                DeliveryState.INFERENCE_FAILED,
                DeliveryState.CANCELLED,
                DeliveryState.PARTIAL_OUTPUT,
            }
            if (
                any(
                    record.state not in retryable_states
                    for record in latest_by_call.values()
                )
                or any(
                    record.capsule_hash != compilation.capsule_hash
                    for record in latest_by_call.values()
                )
            ):
                raise ValueError("observation_id already has a delivery attempt")
        if self.attempt_runtime is not None:
            if not delivery_attempt_id:
                raise ValueError(
                    "canonical runtime boundary requires delivery_attempt_id"
                )
            history = self.attempt_runtime.journal.delivery_history(
                delivery_attempt_id
            )
            if (
                not history
                or history[-1] != compilation.delivery_attempt
            ):
                raise ValueError(
                    "staged compilation does not match canonical journal"
                )
            self._delivery_attempt_ids.append(delivery_attempt_id)
        self._active = compilation
        self._active_delivery_attempt_id = delivery_attempt_id
        self._active_observation_binding = observation_binding
        if self.attempt_runtime is None:
            self._records.append(compilation.delivery_attempt)

    def _replace_active_attempt(
        self,
        compilation: CapsuleCompilation,
        attempt: DeliveryAttempt,
    ) -> CapsuleCompilation:
        updated = CapsuleCompilation(
            **{
                **compilation.__dict__,
                "delivery_attempt": attempt,
            }
        )
        self._active = updated
        return updated

    def _record_local(self, attempt: DeliveryAttempt) -> None:
        if self.attempt_runtime is None:
            self._records.append(attempt)

    def _notify_fault(
        self,
        stage: str,
        exc: BaseException,
    ) -> None:
        """Report a boundary fault without making the native path depend on it."""

        if self.fault_handler is None:
            return
        try:
            self.fault_handler(stage, exc)
        except Exception:
            # Failure telemetry is strictly subordinate to the native path.
            return

    def _clear_active(self) -> None:
        self._active = None
        self._active_delivery_attempt_id = ""
        self._active_observation_binding = None

    def _advance_provider_phase(self) -> int:
        self._provider_phase_ordinal += 1
        return self._provider_phase_ordinal

    @staticmethod
    def _without_staged_capsule(
        messages: list[dict[str, Any]],
        compilation: CapsuleCompilation,
    ) -> list[dict[str, Any]]:
        """Remove only the structural message appended by ``prepare``."""

        capsule_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": compilation.capsule_text},
            ],
        }
        if messages and messages[-1] == capsule_message:
            return list(messages[:-1])
        return list(messages)

    @staticmethod
    def _is_provider_rejection(exc: BaseException) -> bool:
        """Distinguish an authoritative provider response from no response."""

        if isinstance(getattr(exc, "status_code", None), int):
            return True
        if getattr(exc, "response", None) is not None:
            return True
        return type(exc).__name__ in {
            "AuthenticationError",
            "BadRequestError",
            "PermissionDeniedError",
            "NotFoundError",
            "RateLimitError",
            "ServiceUnavailableError",
            "UnprocessableEntityError",
            "ContentPolicyViolationError",
            "ContextWindowExceededError",
        }

    def _record_active_failure(
        self,
        state: DeliveryState,
        *,
        reason: str,
    ) -> DeliveryAttempt | None:
        """Persist a failure when possible, with a truthful local fallback."""

        active = self._active
        if active is None or active.delivery_attempt is None:
            return None

        persisted: DeliveryAttempt | None = None
        latest = active.delivery_attempt
        if self.attempt_runtime is not None:
            try:
                history = self.attempt_runtime.journal.delivery_history(
                    self._active_delivery_attempt_id
                )
                if history:
                    latest = history[-1]
                persisted = self.attempt_runtime.record_delivery_failure(
                    self._active_delivery_attempt_id,
                    state,
                    reason=reason,
                )
            except Exception:
                persisted = None

        if persisted is None:
            try:
                failure_source = latest
                if (
                    state is DeliveryState.DISPATCH_FAILED
                    and failure_source.state is DeliveryState.JOINED
                ):
                    # The dispatch journal failed before the provider handoff.
                    # Build a locally terminal record without claiming provider
                    # acceptance or delivery.
                    failure_source = advance_delivery(
                        failure_source,
                        DeliveryState.DISPATCHED,
                    )
                persisted = record_delivery_failure(
                    failure_source,
                    state,
                    reason=reason,
                )
            except Exception:
                return None
            if self.attempt_runtime is not None:
                self._fallback_records.append(persisted)
            else:
                self._record_local(persisted)

        self._replace_active_attempt(active, persisted)
        return persisted

    def _record_active_withheld(self, *, reason: str) -> DeliveryAttempt | None:
        """Persist a DELIBERATE holdout on the active compilation, with a local fallback.

        Mirrors `_record_active_failure`'s persist-then-fallback shape but routes through
        `record_delivery_withheld`, NOT the failure recorder: a holdout is terminal and is not
        a defect, and letting it travel the failure path would put a measurement decision into
        failure accounting and the release gate.

        Returns None if there is nothing to withhold, so the caller can fall through to normal
        delivery rather than silently dropping a capsule it failed to account for.
        """
        active = self._active
        if active is None or active.delivery_attempt is None:
            return None

        persisted: DeliveryAttempt | None = None
        if self.attempt_runtime is not None:
            try:
                persisted = self.attempt_runtime.record_delivery_withheld(
                    self._active_delivery_attempt_id,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001 -- fall back to a truthful local record
                persisted = None

        if persisted is None:
            try:
                persisted = record_delivery_withheld(
                    active.delivery_attempt,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001
                return None
            if self.attempt_runtime is not None:
                self._fallback_records.append(persisted)
            else:
                self._record_local(persisted)

        self._replace_active_attempt(active, persisted)
        return persisted

    def _discard_pending(
        self,
        provider_response_id: str,
        *,
        reason: str,
    ) -> None:
        pending = self._pending_commits.pop(provider_response_id, None)
        if pending is None:
            return
        compilation = pending.compilation
        delivery_attempt_id = pending.delivery_attempt_id
        if (
            compilation.delivery_attempt is None
            or compilation.delivery_attempt.state is not DeliveryState.DELIVERED
        ):
            return
        if self.attempt_runtime is not None:
            discarded = self.attempt_runtime.discard_provider_response(
                delivery_attempt_id,
                reason=reason,
            )
        else:
            discarded = record_delivery_failure(
                compilation.delivery_attempt,
                DeliveryState.RESPONSE_DISCARDED,
                reason=reason,
            )
        self._record_local(discarded)

    def _safe_discard_pending(
        self,
        provider_response_id: str,
        *,
        reason: str,
    ) -> None:
        try:
            self._discard_pending(
                provider_response_id,
                reason=reason,
            )
        except Exception as exc:
            self._pending_commits.pop(provider_response_id, None)
            self._notify_fault("RESPONSE_COMMIT", exc)

    @staticmethod
    def _canonical_manifest(
        compilation: CapsuleCompilation,
    ) -> Mapping[str, Any] | None:
        manifest_json = compilation.evidence_manifest_json
        if (
            not manifest_json
            or hashlib.sha256(
                manifest_json.encode("utf-8")
            ).hexdigest() != compilation.evidence_manifest_hash
        ):
            return None
        try:
            manifest = json.loads(manifest_json)
        except (TypeError, ValueError):
            return None
        if (
            not isinstance(manifest, Mapping)
            or json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) != manifest_json
        ):
            return None
        evidence = manifest.get("evidence")
        if not isinstance(evidence, list):
            return None
        evidence_ids = [
            item.get("evidence_id")
            for item in evidence
            if isinstance(item, Mapping)
        ]
        if (
            len(evidence_ids) != len(evidence)
            or evidence_ids != list(compilation.evidence_ids)
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            return None
        return manifest

    def _emit_canonical_delivery(
        self,
        *,
        compilation: CapsuleCompilation,
        delivery_attempt_id: str,
        delivery: DeliveryAttempt,
        observation_binding: ObservationBinding,
        delivery_phase_ordinal: int,
    ) -> bool:
        manifest = self._canonical_manifest(compilation)
        if (
            manifest is None
            or delivery.state is not DeliveryState.DELIVERED
            or delivery.capsule_hash != compilation.capsule_hash
            or delivery.evidence_ids != compilation.evidence_ids
            or compilation.binding is None
            or compilation.binding.provider_payload_hash
            != delivery.provider_payload_hash
        ):
            return False
        capsule_binding = compilation.binding
        row = {
            "schema": "gt.canonical_delivery.v1",
            "layer": "canonical.provider_delivery",
            "event_type": "canonical_provider_delivery",
            "outcome": "delivered",
            "delivery_attempt_id": delivery_attempt_id,
            "observation_id": compilation.observation_id,
            "model_call_id": compilation.model_call_id,
            "capsule_text": compilation.capsule_text,
            "capsule_hash": compilation.capsule_hash,
            "rendered_content_hash": compilation.rendered_content_hash,
            "chars_delivered": len(compilation.capsule_text),
            "content_sha256_16": compilation.rendered_content_hash[:16],
            "evidence_manifest_json": compilation.evidence_manifest_json,
            "evidence_manifest_hash": compilation.evidence_manifest_hash,
            "evidence_ids": list(compilation.evidence_ids),
            # The join identity of what this capsule delivered, one entry per evidence.
            # `content_sha256_16` is NOT repeated here: every entry shares the ROW's seal,
            # because the capsule is one sealed byte string, not a set of separately sealed
            # blocks. `fact_class` rides along so an offline reader can check the class
            # against the registry from the row's own bytes instead of trusting that the
            # producer validated it upstream (J6 self-evidence).
            "evidence_lineage": [
                {
                    "candidate_id": candidate_id,
                    "fact_class": fact_class,
                    # The ALREADY-AUTHORIZED CAP byte owners for this evidence. Without them
                    # the grader has no mechanism to prove a byte owner on the canonical
                    # route -- this row carries neither `feature_ids` nor `profile_member`.
                    "cap_owners": list(cap_owners),
                }
                for candidate_id, fact_class, cap_owners in compilation.evidence_lineage
            ],
            "provider_payload_hash": delivery.provider_payload_hash,
            "bound_provider_payload_json": (
                compilation.bound_provider_payload_json
            ),
            "provider_response_id": delivery.provider_response_id,
            "provider_terminal_kind": (
                delivery.terminal_kind.value
                if delivery.terminal_kind is not None else ""
            ),
            "capsule_binding": {
                "schema": capsule_binding.schema,
                "model_call_id": capsule_binding.model_call_id,
                "observation_id": capsule_binding.observation_id,
                "decision_context": capsule_binding.decision_context.value,
                "evidence_ids": list(capsule_binding.evidence_ids),
                "capsule_hash": capsule_binding.capsule_hash,
                "provider_payload_hash": (
                    capsule_binding.provider_payload_hash
                ),
                "message_index": capsule_binding.message_index,
                "content_index": capsule_binding.content_index,
                "decision_id": capsule_binding.decision_id,
                "evidence_manifest_hash": (
                    capsule_binding.evidence_manifest_hash
                ),
            },
            "observation_binding": observation_binding_to_dict(
                observation_binding
            ),
            "delivery_phase_ordinal": delivery_phase_ordinal,
        }
        try:
            append_ledger_line(row, self._receipt_sink_path)
        except Exception as exc:
            self._notify_fault("CANONICAL_DELIVERY", exc)
            return False
        # AFTER the row is durable, so a handler can never observe a delivery the join
        # will not see. A handler fault is reported and swallowed: audit persistence may
        # not un-deliver bytes the model has already received.
        if self.delivery_handler is not None:
            try:
                self.delivery_handler(compilation)
            except Exception as exc:  # noqa: BLE001
                self._notify_fault("DELIVERY_HANDLER", exc)
        return True

    def reconcile_provider_response(
        self,
        response: Any,
    ) -> DeliveryAttempt:
        """Reconcile an accepted asynchronous call with provider truth.

        ``PROVIDER_ACCEPTED`` is deliberately an occupied provider slot: the
        capsule was dispatched, but no terminal inference has yet been
        proven.  A later provider response may close that slot only when its
        response identity matches the accepted call and it carries a terminal
        provider status.  Repeated nonterminal observations are idempotent.
        """

        active = self._active
        if (
            active is None
            or active.delivery_attempt is None
            or active.delivery_attempt.state
            is not DeliveryState.PROVIDER_ACCEPTED
        ):
            raise ValueError(
                "provider reconciliation requires an active "
                "PROVIDER_ACCEPTED call"
            )
        accepted = active.delivery_attempt
        provider_response_id = _response_id(response)
        if not provider_response_id:
            raise ValueError(
                "provider_response identity is required for reconciliation"
            )
        if provider_response_id != accepted.provider_response_id:
            raise ValueError(
                "provider_response identity does not match accepted response"
            )

        terminal_kind = _terminal_kind(response)
        if (
            terminal_kind is None
            and _provider_status(response)
            in {"queued", "pending", "running", "in_progress"}
        ):
            return accepted
        if terminal_kind is None:
            terminal_kind = ProviderTerminalKind.FAILED

        choices = getattr(response, "choices", ()) or ()
        first_choice = choices[0] if choices else None
        model_call = ModelCallAttempt(
            model_call_id=accepted.model_call_id,
            joined_capsule_hash=accepted.joined_capsule_hash,
            provider_payload_hash=accepted.provider_payload_hash,
            provider_response_id=provider_response_id,
            terminal_kind=terminal_kind,
            terminal_reason=(
                str(getattr(first_choice, "finish_reason", "") or "")
                or "MALFORMED_PROVIDER_RESPONSE"
            ),
        )
        if self.attempt_runtime is not None:
            terminal = self.attempt_runtime.record_provider_terminal(
                self._active_delivery_attempt_id,
                model_call,
            )
        else:
            terminal = record_provider_terminal(
                accepted,
                model_call,
            )

        completed = self._replace_active_attempt(active, terminal)
        self._record_local(terminal)
        completed_delivery_attempt_id = self._active_delivery_attempt_id
        observation_binding = self._active_observation_binding
        self._clear_active()
        if terminal.state is DeliveryState.DELIVERED:
            delivery_phase_ordinal = self._advance_provider_phase()
            delivered_iteration = 0
            decision_window_key = ""
            if self.attempt_runtime is not None:
                delivered_iteration = self.attempt_runtime.work_state.sequence
                decision_window_key = (
                    self.attempt_runtime.work_state.decision_window_key
                )
            canonical_delivery_emitted = False
            if observation_binding is not None:
                canonical_delivery_emitted = self._emit_canonical_delivery(
                    compilation=completed,
                    delivery_attempt_id=completed_delivery_attempt_id,
                    delivery=terminal,
                    observation_binding=observation_binding,
                    delivery_phase_ordinal=delivery_phase_ordinal,
                )
            self._pending_commits[provider_response_id] = _PendingCommit(
                compilation=completed,
                delivery_attempt_id=completed_delivery_attempt_id,
                delivered_iteration=delivered_iteration,
                decision_window_key=decision_window_key,
                delivery_phase_ordinal=delivery_phase_ordinal,
                observation_binding=observation_binding,
                canonical_delivery_emitted=canonical_delivery_emitted,
            )
        return terminal

    def _observe_acknowledgment(
        self,
        pending: _PendingCommit,
        committed_message: Mapping[str, Any],
        committed: DeliveryAttempt,
    ) -> None:
        runtime = self.attempt_runtime
        if runtime is None:
            return
        delivered = pending.compilation.delivery_attempt
        if delivered is None:
            return
        if (
            committed.state is not DeliveryState.RESPONSE_COMMITTED
            or committed.provider_response_id
            != delivered.provider_response_id
            or committed.response_hash != _canonical_hash(committed_message)
            or runtime.work_state.decision_window_key
            != pending.decision_window_key
        ):
            return
        extra = committed_message.get("extra")
        if not isinstance(extra, Mapping):
            return
        response = extra.get("response")
        if (
            not isinstance(response, Mapping)
            or response.get("id") != committed.provider_response_id
        ):
            return
        content = committed_message.get("content")
        actions = extra.get("actions")
        canonical_manifest = (
            self._canonical_manifest(pending.compilation)
            if (
                pending.observation_binding is not None
                and pending.canonical_delivery_emitted
            )
            else None
        )
        matched: list[Any] = []
        if canonical_manifest is not None:
            manifest_evidence = canonical_manifest.get("evidence")
            if not isinstance(manifest_evidence, list):
                return
            for item in manifest_evidence:
                if not isinstance(item, Mapping):
                    return
                evidence_id = item.get("evidence_id")
                claim = item.get("claim")
                subject = item.get("subject")
                provenance = item.get("provenance")
                if (
                    not isinstance(evidence_id, str)
                    or not isinstance(claim, str)
                    or not isinstance(subject, str)
                    or not isinstance(provenance, list)
                    or not all(
                        isinstance(value, str) for value in provenance
                    )
                ):
                    return
                evidence = runtime.evidence_record(evidence_id)
                if (
                    evidence.lifecycle is EvidenceLifecycle.ACTIVE
                    and evidence.claim == claim
                    and evidence.subject == subject
                    and list(evidence.provenance) == provenance
                    and _explicit_claim_reference(content, claim)
                    and _materially_linked_action(actions, item)
                ):
                    matched.append(item)
        else:
            for evidence_id in pending.compilation.evidence_ids:
                evidence = runtime.evidence_record(evidence_id)
                if (
                    evidence.lifecycle is EvidenceLifecycle.ACTIVE
                    and _explicit_claim_reference(content, evidence.claim)
                    and _materially_linked_action(actions, evidence)
                ):
                    matched.append(evidence)
        if len(matched) != 1:
            return

        evidence = matched[0]
        evidence_id = (
            str(evidence.get("evidence_id"))
            if isinstance(evidence, Mapping)
            else evidence.evidence_id
        )
        receipt_key = _canonical_hash(
            {
                "delivery_attempt_id": pending.delivery_attempt_id,
                "capsule_hash": pending.compilation.capsule_hash,
                "evidence_id": evidence_id,
                "provider_response_id": committed.provider_response_id,
                "response_hash": committed.response_hash,
            }
        )
        if receipt_key in self._ack_receipt_keys:
            return
        if canonical_manifest is not None:
            acknowledgment_phase_ordinal = self._advance_provider_phase()
            row = {
                "schema": "gt.canonical_ack_receipt.v1",
                "layer": "canonical.ack_receipt",
                "event_type": "canonical_ack_receipt",
                "outcome": "evaluated",
                "chars_delivered": 0,
                "receipt": 3,
                "delivery_attempt_id": pending.delivery_attempt_id,
                "capsule_hash": pending.compilation.capsule_hash,
                "evidence_manifest_hash": (
                    pending.compilation.evidence_manifest_hash
                ),
                "evidence_ids": list(pending.compilation.evidence_ids),
                "matched_evidence_id": evidence_id,
                "provider_response_id": committed.provider_response_id,
                "response_hash": committed.response_hash,
                "observation_binding": observation_binding_to_dict(
                    pending.observation_binding
                ),
                "delivery_phase_ordinal": pending.delivery_phase_ordinal,
                "acknowledgment_phase_ordinal": (
                    acknowledgment_phase_ordinal
                ),
                "receipt_key": receipt_key,
            }
            append_ledger_line(row, self._receipt_sink_path)
            self._ack_receipt_keys.add(receipt_key)
            self._ack_receipts.append(row)
            return

        candidate_id = (
            f"{pending.delivery_attempt_id}:"
            f"{pending.compilation.capsule_hash}:"
            f"{evidence_id}"
        )
        participation = build_ack_participation(
            TerminalAckIdentity(
                candidate_text=pending.compilation.capsule_text,
                fact_class=evidence.feature_id,
                candidate_id=candidate_id,
                delivered_iteration=pending.delivered_iteration,
            ),
            acknowledgment_iteration=pending.delivered_iteration + 1,
            acknowledged=True,
        )
        row = {
            **participation_to_dict(participation),
            "layer": "control.participation",
            "event_type": "canonical_ack_receipt",
            "outcome": "evaluated",
            "chars_delivered": 0,
            "participation_decision": participation.decision,
            "decision_reason": participation.reason,
            "receipt": 3,
            "delivery_attempt_id": pending.delivery_attempt_id,
            "capsule_hash": pending.compilation.capsule_hash,
            "evidence_manifest_hash": (
                pending.compilation.evidence_manifest_hash
            ),
            "evidence_id": evidence_id,
            "provider_response_id": committed.provider_response_id,
            "response_hash": committed.response_hash,
            "receipt_key": receipt_key,
        }
        append_ledger_line(row, self._receipt_sink_path)
        self._ack_receipt_keys.add(receipt_key)
        self._ack_receipts.append(row)

    def _install(self) -> None:
        boundary = self

        def prepare(_model: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            prepared = boundary._original_prepare(messages)
            active = boundary._active
            if active is None:
                return prepared
            if (
                active.delivery_attempt is None
                or active.delivery_attempt.state is not DeliveryState.COMPILED
            ):
                return prepared
            augmented = list(prepared)
            augmented.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": active.capsule_text}],
                }
            )
            return augmented

        def query_transport(
            _model: Any,
            messages: list[dict[str, Any]],
            **kwargs: Any,
        ) -> Any:
            active = boundary._active
            if (
                active is not None
                and active.delivery_attempt is not None
                and active.delivery_attempt.state is DeliveryState.COMPILED
            ):
                # SHADOW HOLDOUT (#30). Decided HERE -- after the capsule exists, BEFORE it is
                # bound and dispatched. Binding first and then not sending would claim
                # DELIVERED for bytes the model never saw, forging the exact proof this chain
                # exists to establish. OFF unless BOTH GT_SS_SHADOW and a positive
                # GT_SS_SHADOW_RATE, so the default path is byte-identical.
                _task_id = str(
                    getattr(boundary.attempt_runtime, "attempt_id", "") or ""
                )
                _verdict = _capsule_holdout_verdict(_task_id, active)
                # Recorded on BOTH arms: a propensity cannot be estimated from the numerator
                # alone, so the DELIVER arm must be observable too.
                _record_shadow_assignment_row(boundary, _task_id, active, _verdict)
                if _verdict == _SHADOW_HOLDOUT:
                    _withheld = boundary._record_active_withheld(
                        reason="shadow_holdout"
                    )
                    if _withheld is not None:
                        native_messages = boundary._without_staged_capsule(
                            messages,
                            active,
                        )
                        boundary._clear_active()
                        return boundary._original_query(
                            native_messages,
                            **kwargs,
                        )
                    # Could not ACCOUNT for the holdout, so do not perform it: an unrecorded
                    # withholding is evidence lost with no measurement gained.
                try:
                    exact_payload = boundary._exact_provider_payload(
                        messages,
                        kwargs,
                    )
                    active = bind_capsule_to_final_payload(
                        active,
                        exact_payload,
                    )
                    if boundary.attempt_runtime is not None:
                        persisted = (
                            boundary.attempt_runtime.bind_provider_payload(
                                boundary._active_delivery_attempt_id,
                                exact_payload,
                            )
                        )
                        active = boundary._replace_active_attempt(
                            active,
                            persisted,
                        )
                    boundary._bound_compilations.append(active)
                    assert active.delivery_attempt is not None
                    boundary._record_local(active.delivery_attempt)
                    active = verify_bound_payload_at_dispatch(
                        active,
                        exact_payload,
                    )
                    if boundary.attempt_runtime is not None:
                        persisted = boundary.attempt_runtime.mark_dispatched(
                            boundary._active_delivery_attempt_id,
                            exact_payload,
                        )
                        active = boundary._replace_active_attempt(
                            active,
                            persisted,
                        )
                    else:
                        boundary._active = active
                    assert active.delivery_attempt is not None
                    boundary._record_local(active.delivery_attempt)
                except Exception as exc:
                    current = boundary._active
                    current_state = (
                        current.delivery_attempt.state
                        if current is not None
                        and current.delivery_attempt is not None
                        else DeliveryState.COMPILED
                    )
                    if current_state is DeliveryState.COMPILED:
                        failure_state = DeliveryState.JOIN_FAILED
                        fault_stage = "OBSERVATION_JOIN"
                    else:
                        failure_state = DeliveryState.DISPATCH_FAILED
                        fault_stage = "PROVIDER_DISPATCH"
                    reason = f"{type(exc).__name__}:{exc}"
                    boundary._record_active_failure(
                        failure_state,
                        reason=reason,
                    )
                    native_messages = boundary._without_staged_capsule(
                        messages,
                        active,
                    )
                    boundary._notify_fault(fault_stage, exc)
                    boundary._clear_active()
                    return boundary._original_query(
                        native_messages,
                        **kwargs,
                    )
            try:
                response = boundary._original_query(messages, **kwargs)
            except Exception as exc:
                active = boundary._active
                if (
                    active is not None
                    and active.delivery_attempt is not None
                    and active.delivery_attempt.state is DeliveryState.DISPATCHED
                ):
                    reason = f"{type(exc).__name__}:{exc}"
                    if boundary._is_provider_rejection(exc):
                        failure_state = DeliveryState.PROVIDER_REJECTED
                        fault_stage = "PROVIDER_REJECTION"
                    else:
                        failure_state = DeliveryState.DISPATCH_FAILED
                        fault_stage = "PROVIDER_DISPATCH"
                    boundary._record_active_failure(
                        failure_state,
                        reason=reason,
                    )
                    boundary._notify_fault(fault_stage, exc)
                    boundary._clear_active()
                raise
            active = boundary._active
            if (
                active is None
                or active.delivery_attempt is None
                or active.delivery_attempt.state is not DeliveryState.DISPATCHED
            ):
                return response
            # L2 SAME-STATE COUNTERFACTUAL (#31). Placed HERE because reaching this line
            # proves a capsule was actually dispatched on this turn, which is exactly the
            # opportunity a counterfactual is defined against. OFF by default
            # (GT_L2_PROBE_RATE=0) => no extra provider call and no row, so the dispatch
            # path stays byte-identical. It cannot raise, and its response is discarded
            # without ever touching the delivery state machine.
            # `active` IS the CapsuleCompilation here — the same object the fault path
            # hands to `_without_staged_capsule` above. Passing `active.compilation`
            # would raise AttributeError, and this probe's own broad except would have
            # swallowed it, silently disabling the measurement while looking wired.
            _maybe_record_counterfactual_pair(
                boundary,
                messages,
                active,
                response,
                kwargs,
            )
            provider_response_id = _response_id(response)
            if not provider_response_id:
                reason = "MISSING_PROVIDER_RESPONSE_ID"
                fault = RuntimeError(reason)
                boundary._record_active_failure(
                    DeliveryState.PROVIDER_REJECTED,
                    reason=reason,
                )
                boundary._notify_fault("PROVIDER_REJECTION", fault)
                boundary._clear_active()
                return response
            try:
                if boundary.attempt_runtime is not None:
                    accepted = (
                        boundary.attempt_runtime.mark_provider_accepted(
                            boundary._active_delivery_attempt_id,
                            provider_response_id=provider_response_id,
                        )
                    )
                else:
                    accepted = advance_delivery(
                        active.delivery_attempt,
                        DeliveryState.PROVIDER_ACCEPTED,
                        provider_response_id=provider_response_id,
                    )
            except Exception as exc:
                boundary._notify_fault("PROVIDER_ACCEPTANCE", exc)
                boundary._clear_active()
                return response
            boundary._record_local(accepted)
            boundary._replace_active_attempt(active, accepted)
            if (
                _terminal_kind(response) is None
                and _provider_status(response)
                in {"queued", "pending", "running", "in_progress"}
            ):
                return response
            try:
                boundary.reconcile_provider_response(response)
            except Exception as exc:
                boundary._notify_fault("PROVIDER_TERMINAL", exc)
                return response
            return response

        def _committed_message(
            messages: tuple[dict[str, Any], ...],
        ) -> Mapping[str, Any] | None:
            return next(
                (
                    message for message in messages
                    if isinstance(message, Mapping)
                    and isinstance(message.get("extra"), Mapping)
                    and isinstance(message["extra"].get("response"), Mapping)
                    and message["extra"]["response"].get("id")
                    in boundary._pending_commits
                ),
                None,
            )

        def add_messages(
            _agent: Any,
            *messages: dict[str, Any],
        ) -> list[dict[str, Any]]:
            committed_message = _committed_message(messages)
            try:
                result = boundary._original_add_messages(*messages)
            except Exception as exc:
                if committed_message is not None:
                    provider_response_id = str(
                        committed_message["extra"]["response"]["id"]
                    )
                    boundary._safe_discard_pending(
                        provider_response_id,
                        reason=f"{type(exc).__name__}:{exc}",
                    )
                raise
            if committed_message is None:
                return result
            provider_response_id = str(
                committed_message["extra"]["response"]["id"]
            )
            pending = boundary._pending_commits[provider_response_id]
            active = pending.compilation
            delivery_attempt_id = pending.delivery_attempt_id
            if (
                active.delivery_attempt is None
                or active.delivery_attempt.state is not DeliveryState.DELIVERED
            ):
                return result
            response_hash = _canonical_hash(committed_message)
            try:
                if boundary.attempt_runtime is not None:
                    committed = (
                        boundary.attempt_runtime.commit_provider_response(
                            delivery_attempt_id,
                            response_hash=response_hash,
                        )
                    )
                else:
                    committed = commit_response(
                        active.delivery_attempt,
                        response_hash=response_hash,
                    )
            except Exception as exc:
                boundary._pending_commits.pop(
                    provider_response_id,
                    None,
                )
                boundary._notify_fault("RESPONSE_COMMIT", exc)
                return result
            boundary._pending_commits.pop(provider_response_id, None)
            boundary._record_local(committed)
            try:
                boundary._observe_acknowledgment(
                    pending,
                    committed_message,
                    committed,
                )
            except Exception as exc:
                boundary._notify_fault("ACK_RECEIPT", exc)
            return result

        def model_query(
            _model: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            pending_before = set(boundary._pending_commits)
            try:
                return boundary._original_model_query(*args, **kwargs)
            except Exception as exc:
                new_pending = tuple(
                    sorted(
                        set(boundary._pending_commits)
                        - pending_before
                    )
                )
                for provider_response_id in new_pending:
                    boundary._safe_discard_pending(
                        provider_response_id,
                        reason=f"{type(exc).__name__}:{exc}",
                    )
                raise

        self.model._prepare_messages_for_api = MethodType(prepare, self.model)
        self.model._query = MethodType(query_transport, self.model)
        if callable(self._original_model_query):
            self.model.query = MethodType(model_query, self.model)
        self.agent.add_messages = MethodType(add_messages, self.agent)

    def _exact_provider_payload(
        self,
        messages: list[dict[str, Any]],
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return a provably literal request or fail before GT dispatch."""

        exact_builder = getattr(
            self.model,
            "_gt_exact_provider_payload",
            None,
        )
        if callable(exact_builder):
            payload = exact_builder(messages, dict(kwargs))
            if not isinstance(payload, Mapping):
                raise TypeError(
                    "exact provider payload builder must return a mapping"
                )
            return dict(payload)

        try:
            from minisweagent.models.litellm_model import (
                BASH_TOOL,
                LitellmModel,
            )

            if getattr(self._original_query, "__func__", None) is LitellmModel._query:
                return {
                    "model": self.model.config.model_name,
                    "messages": messages,
                    "tools": [BASH_TOOL],
                    **(dict(self.model.config.model_kwargs) | dict(kwargs)),
                }
        except (AttributeError, ImportError, TypeError):
            pass
        raise RuntimeError("EXACT_PROVIDER_PAYLOAD_UNAVAILABLE")

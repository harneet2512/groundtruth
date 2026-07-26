"""Provider-terminal delivery witness for the Mini-SWE/LiteLLM boundary.

The adapter is intentionally narrow: it stages a compiled capsule, appends one
structural content block only after Mini-SWE has prepared native messages, binds
and dispatches those exact bytes, records provider-terminal inference, and
commits the returned response only when the agent accepts it into its trajectory.
"""

from __future__ import annotations

import hashlib
import json
from types import MethodType
from typing import Any, Callable, Mapping

from groundtruth.runtime.reasoning_runtime import (
    AttemptReasoningRuntime,
    CapsuleCompilation,
    CapsuleCompilationState,
    DeliveryAttempt,
    DeliveryState,
    ModelCallAttempt,
    ProviderTerminalKind,
    advance_delivery,
    bind_capsule_to_final_payload,
    commit_response,
    record_delivery_failure,
    record_provider_terminal,
    verify_bound_payload_at_dispatch,
)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


class MiniSweProviderBoundary:
    """Install the sole provider/response delivery lifecycle on one model+agent."""

    def __init__(
        self,
        *,
        model: Any,
        agent: Any,
        attempt_runtime: AttemptReasoningRuntime | None = None,
        fault_handler: Callable[[str, BaseException], Any] | None = None,
    ):
        self.model = model
        self.agent = agent
        self.attempt_runtime = attempt_runtime
        self.fault_handler = fault_handler
        self._records: list[DeliveryAttempt] = []
        self._fallback_records: list[DeliveryAttempt] = []
        self._delivery_attempt_ids: list[str] = []
        self._bound_compilations: list[CapsuleCompilation] = []
        self._active: CapsuleCompilation | None = None
        self._active_delivery_attempt_id = ""
        self._pending_commits: dict[
            str, tuple[CapsuleCompilation, str]
        ] = {}
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

    def _discard_pending(
        self,
        provider_response_id: str,
        *,
        reason: str,
    ) -> None:
        pending = self._pending_commits.pop(provider_response_id, None)
        if pending is None:
            return
        compilation, delivery_attempt_id = pending
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
        self._clear_active()
        if terminal.state is DeliveryState.DELIVERED:
            self._pending_commits[provider_response_id] = (
                completed,
                completed_delivery_attempt_id,
            )
        return terminal

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
            active, delivery_attempt_id = boundary._pending_commits[
                provider_response_id
            ]
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

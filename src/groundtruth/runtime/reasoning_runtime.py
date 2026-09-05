"""Deterministic foundations for GroundTruth's canonical reasoning runtime.

This module deliberately owns value types and pure transitions.  Harness
adapters may normalize native events into these values, but no feature is
allowed to recover semantic truth by reparsing carrier text after that point.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import warnings
from dataclasses import asdict, dataclass, replace
from enum import Enum, IntEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


class EventIntegrityError(RuntimeError):
    """The append-only causal event stream failed an integrity check."""


class EventSchemaVersionError(EventIntegrityError):
    """A stored event was written under a DIFFERENT canonical hash schema.

    C5. `content_hash` is a derived property recomputed from the live class definition on
    every access (`_canonical_data` walks `asdict`, with no allowlist and no omit-if-default),
    so ADDING A FIELD to `CanonicalResult` or `CanonicalEvent` changes the recomputed digest
    of rows that were ALREADY WRITTEN. `EventStore.events` then compares the recomputed hash
    to the stored one, they differ, and it raises.

    Before this class existed the failure surfaced as `"event content hash/tamper mismatch"`,
    which `gt_mini_patch._record_fault` maps to `FaultCode.CAUSAL_EVENT_GAP` -- a member of
    `CORE_CORRUPTION_CODES` -- so an ordinary schema evolution was reported as TAMPERING and
    silently isolated the canonical observer for the rest of the attempt. A compat break that
    presents as an attack is the worst possible diagnosis: it sends the reader hunting for
    corruption that does not exist while GT quietly produces nothing.

    Subclasses `EventIntegrityError` deliberately, so every existing `except` clause keeps
    catching it and no caller regresses; only the MESSAGE and the type narrow.
    """


# The schema identity of the canonical hash. BUMP THIS whenever a field is added to, removed
# from, or renamed on `CanonicalEvent` / `CanonicalResult` / anything they serialize -- that is
# exactly when previously-written rows stop re-deriving their stored digest. Bumping it turns a
# silent false tamper accusation into an explicit, self-describing version mismatch.
CANONICAL_HASH_SCHEMA = "gt.canonical_event.v2"

# The schema identity of a PERSISTED EvidenceRecord payload (C28a, 2026-07-28). BUMP THIS
# whenever a field is added to, removed from, or renamed on `EvidenceRecord`.
#
# WHY IT EXISTS. `_evidence_record_from_json` reads optional fields with `raw.get(name, ())`,
# so a row written BEFORE a field existed rehydrates byte-identically to a row whose field is
# legitimately empty. For `observed_substrates` that is not cosmetic: the substrate gate holds
# any record that cannot evidence its own substrate, which is CORRECT for the second case and
# undiagnosable for the first. Recording which schema wrote each row separates them.
#
# WHAT IT DOES NOT DO: it does not "unhold" legacy rows. Substrate evidence that was never
# recorded cannot be recovered, and holding such a record is correct-or-quiet. The gate stays
# strict -- weakening it re-opens cross-record substrate lending. This makes the condition
# DIAGNOSABLE and makes an unknown future schema fail LOUDLY instead of being silently misread.
EVIDENCE_RECORD_SCHEMA = "gt.evidence_record.v1"

# THE SINGLE SOURCE OF TRUTH for the capsule-hash preimage label. Exported 2026-07-28 because
# this literal was hand-duplicated in FOUR places -- `reasoning_runtime` (the writer),
# `runtime_attestation.py:580` and `gt_feature_metrics.py:1843` (readers that RECOMPUTE the
# hash to verify it), and a provider-boundary test. Bumping only the writer silently broke
# every reader: 8 runtime_attestation tests and 3 canonical-ack collector tests went red
# because they recomputed a v2 digest and compared it to a v3 one.
#
# That is the same "two things that must agree by hand" defect the metrics work has been
# unpicking all week, sitting inside the mechanism whose whole job is to make disagreement
# detectable. A hash label that four files must keep in sync manually is not a version -- it
# is four versions that happen to match today.
#
# BUMP THIS (here, once) whenever `_evidence_manifest` or the rendered-content hash definition
# changes. Every consumer must IMPORT it; a new hardcoded copy is a defect.
DECISION_CAPSULE_SCHEMA = "gt.decision_capsule.v3"


class StateIntegrityError(RuntimeError):
    """A canonical projection cannot be trusted or deterministically reduced."""


class Authority(IntEnum):
    """Authority of normalized semantic truth, ordered from weakest to strongest."""

    LEGACY_INFERRED = 0
    COMMAND_FALLBACK = 1
    RESULT_SHAPE = 2
    REPOSITORY_DELTA = 3
    RESULT_DERIVED = 4
    STRUCTURED = 5


class EventKind(str, Enum):
    TASK_RECEIVED = "TASK_RECEIVED"
    MODEL_CALL_STARTED = "MODEL_CALL_STARTED"
    MODEL_CALL_COMPLETED = "MODEL_CALL_COMPLETED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    ACTION_RESULT = "ACTION_RESULT"
    OBSERVATION_COMMITTED = "OBSERVATION_COMMITTED"
    COMPACTION_STARTED = "COMPACTION_STARTED"
    COMPACTION_COMPLETED = "COMPACTION_COMPLETED"


class CausalRefKind(str, Enum):
    ACTION = "ACTION"
    EVENT = "EVENT"
    MODEL_CALL = "MODEL_CALL"
    OBSERVATION = "OBSERVATION"
    TOOL_RESULT = "TOOL_RESULT"


@dataclass(frozen=True)
class CausalRef:
    kind: CausalRefKind
    ref_id: str

    def __post_init__(self) -> None:
        if not self.ref_id:
            raise ValueError("causal ref_id is required")


class SemanticKind(str, Enum):
    SEARCH_REQUESTED = "SEARCH_REQUESTED"
    SEARCH_RESULT = "SEARCH_RESULT"
    SEARCH_EMPTY = "SEARCH_EMPTY"
    SEARCH_FAILED = "SEARCH_FAILED"
    SOURCE_READ_REQUESTED = "SOURCE_READ_REQUESTED"
    SOURCE_VIEWED = "SOURCE_VIEWED"
    SYMBOL_VIEWED = "SYMBOL_VIEWED"
    TEST_REQUESTED = "TEST_REQUESTED"
    TEST_RESULT = "TEST_RESULT"
    TEST_PASS = "TEST_PASS"
    TEST_FAIL = "TEST_FAIL"
    TEST_ENV_FAIL = "TEST_ENV_FAIL"
    TEST_EXECUTED_NO_TESTS = "TEST_EXECUTED_NO_TESTS"
    COMPILE_RESULT = "COMPILE_RESULT"
    EDIT_PROPOSED = "EDIT_PROPOSED"
    EDIT_EXECUTED = "EDIT_EXECUTED"
    EDIT_FAILED = "EDIT_FAILED"
    DIFF_CREATED = "DIFF_CREATED"
    SIGNATURE_CHANGED = "SIGNATURE_CHANGED"
    FILE_CREATED = "FILE_CREATED"
    FILE_DELETED = "FILE_DELETED"
    FILE_RENAMED = "FILE_RENAMED"
    SUBMIT_PROPOSED = "SUBMIT_PROPOSED"
    SUBMIT_ACCEPTED = "SUBMIT_ACCEPTED"
    SUBMIT_BLOCKED = "SUBMIT_BLOCKED"


class ActionOperation(str, Enum):
    OTHER = "OTHER"
    SEARCH = "SEARCH"
    VIEW_SOURCE = "VIEW_SOURCE"
    VIEW_SYMBOL = "VIEW_SYMBOL"
    EDIT = "EDIT"
    TEST = "TEST"
    COMPILE = "COMPILE"
    SIGNATURE_CHANGE = "SIGNATURE_CHANGE"
    FILE_CREATE = "FILE_CREATE"
    FILE_DELETE = "FILE_DELETE"
    FILE_RENAME = "FILE_RENAME"
    SUBMIT = "SUBMIT"


class Phase(str, Enum):
    ORIENTATION = "ORIENTATION"
    DISCOVERY = "DISCOVERY"
    LOCALIZATION = "LOCALIZATION"
    UNDERSTANDING = "UNDERSTANDING"
    IMPLEMENTATION = "IMPLEMENTATION"
    VALIDATION = "VALIDATION"
    RECOVERY = "RECOVERY"
    REVIEW = "REVIEW"
    COMPLETION = "COMPLETION"


def _canonical_data(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: _canonical_data(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_data(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_data(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_sha256(value: str, *, field_name: str, allow_empty: bool = False) -> None:
    if allow_empty and value == "":
        return
    if (
        value != value.lower()
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True)
class RevisionVector:
    repository_content: str
    graph: str
    lsp: str
    runtime_evidence: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.repository_content,
                self.graph,
                self.lsp,
                self.runtime_evidence,
            )
        ):
            raise ValueError("all revision-vector dimensions are required")


@dataclass(frozen=True)
class CanonicalAction:
    action_id: str
    operation: ActionOperation
    tool_family: str
    tool_name: str
    structured_operation: str
    subject: str
    query: str = ""
    targets: tuple[str, ...] = ()
    raw_command: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", tuple(self.targets))
        if (
            not self.action_id
            or not self.tool_family
            or not self.tool_name
            or not self.structured_operation
        ):
            raise ValueError("canonical action identity is incomplete")


@dataclass(frozen=True)
class CanonicalResult:
    status: str
    exit_code: int | None = None
    changed: bool | None = None
    hit_count: int | None = None
    files_hit: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    # The repository-qualified SYMBOL identities the operation actually showed, resolved against
    # repository truth (graph.db) -- never scraped from rendered output. Values use the stable
    # ``repo/path.py::symbol`` representation so same-named definitions in different files do
    # not collapse in WorkState. The field shape and empty default remain unchanged for stored
    # canonical-event compatibility.
    viewed_symbols: tuple[str, ...] = ()
    # The repository-qualified identity for a bare SEARCH operand only when the graph resolves
    # it to exactly one production definition file. A literal command argument validated
    # against repository truth -- never an inference from rendered output. Ambiguous bare names
    # abstain rather than widening focus to several unrelated definitions.
    resolved_symbols: tuple[str, ...] = ()
    failure_fingerprint: str = ""
    signature_before: str = ""
    signature_after: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "files_hit", tuple(self.files_hit))
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "viewed_symbols", tuple(self.viewed_symbols))
        object.__setattr__(self, "resolved_symbols", tuple(self.resolved_symbols))
        if not self.status:
            raise ValueError("canonical result status is required")
        if self.hit_count is not None and self.hit_count < 0:
            raise ValueError("hit_count must be non-negative")
        if self.changed is False and self.changed_files:
            raise ValueError("changed_files contradict changed=False")


@dataclass(frozen=True)
class SemanticOutcome:
    kind: SemanticKind
    subject: str = ""
    status: str = ""
    changed: bool | None = None
    failure_fingerprint: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    authority: Authority = Authority.LEGACY_INFERRED
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metadata",
            tuple((str(key), str(value)) for key, value in self.metadata),
        )
        object.__setattr__(
            self,
            "provenance",
            tuple(str(item) for item in self.provenance),
        )


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    attempt_id: str
    sequence: int
    kind: EventKind
    authority: Authority
    outcomes: tuple[SemanticOutcome, ...]
    revision_before: RevisionVector
    revision_after: RevisionVector
    previous_event_hash: str
    action_id: str = ""
    model_turn_id: str = ""
    observation_id: str = ""
    carrier: str = ""
    parents: tuple[CausalRef, ...] = ()
    action: CanonicalAction | None = None
    result: CanonicalResult | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.attempt_id:
            raise ValueError("event_id and attempt_id are required")
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if not isinstance(self.outcomes, tuple):
            object.__setattr__(self, "outcomes", tuple(self.outcomes))
        if not isinstance(self.parents, tuple):
            object.__setattr__(self, "parents", tuple(self.parents))
        if self.action is not None and self.action_id != self.action.action_id:
            raise ValueError("canonical event/action identity mismatch")
        if self.kind is EventKind.ACTION_PROPOSED and (
            self.action is None or self.result is not None
        ):
            raise ValueError("ACTION_PROPOSED requires action and no result")
        if self.kind is EventKind.ACTION_RESULT and (self.action is None or self.result is None):
            raise ValueError("ACTION_RESULT requires action and result")

    def canonical_json(self) -> str:
        # Events rehydrated from an older hash schema must retain the exact bytes
        # that own their historical content hash. The parsed dataclass may contain
        # newer defaulted fields, but those defaults were not present in the
        # append-only row and must not silently rewrite its causal identity.
        stored = getattr(self, "_stored_canonical_json", None)
        return stored if isinstance(stored, str) else _canonical_json(self)

    @property
    def content_hash(self) -> str:
        return _sha256(self.canonical_json())

    @classmethod
    def from_json(cls, payload: str) -> "CanonicalEvent":
        raw = json.loads(payload)
        event = cls(
            event_id=raw["event_id"],
            attempt_id=raw["attempt_id"],
            sequence=int(raw["sequence"]),
            kind=EventKind(raw["kind"]),
            authority=Authority(int(raw["authority"])),
            outcomes=tuple(
                SemanticOutcome(
                    kind=SemanticKind(item["kind"]),
                    subject=item.get("subject", ""),
                    status=item.get("status", ""),
                    changed=item.get("changed"),
                    failure_fingerprint=item.get("failure_fingerprint", ""),
                    metadata=tuple(
                        (str(key), str(value)) for key, value in item.get("metadata", ())
                    ),
                    authority=Authority(int(item.get("authority", Authority.LEGACY_INFERRED))),
                    provenance=tuple(item.get("provenance", ())),
                )
                for item in raw.get("outcomes", ())
            ),
            revision_before=RevisionVector(**raw["revision_before"]),
            revision_after=RevisionVector(**raw["revision_after"]),
            previous_event_hash=raw.get("previous_event_hash", ""),
            action_id=raw.get("action_id", ""),
            model_turn_id=raw.get("model_turn_id", ""),
            observation_id=raw.get("observation_id", ""),
            carrier=raw.get("carrier", ""),
            parents=tuple(
                CausalRef(
                    kind=CausalRefKind(item["kind"]),
                    ref_id=item["ref_id"],
                )
                for item in raw.get("parents", ())
            ),
            action=(
                CanonicalAction(
                    action_id=raw["action"]["action_id"],
                    operation=ActionOperation(raw["action"]["operation"]),
                    tool_family=raw["action"]["tool_family"],
                    tool_name=raw["action"]["tool_name"],
                    structured_operation=raw["action"]["structured_operation"],
                    subject=raw["action"].get("subject", ""),
                    query=raw["action"].get("query", ""),
                    targets=tuple(raw["action"].get("targets", ())),
                    raw_command=raw["action"].get("raw_command", ""),
                )
                if raw.get("action") is not None
                else None
            ),
            result=(
                CanonicalResult(
                    status=raw["result"]["status"],
                    exit_code=raw["result"].get("exit_code"),
                    changed=raw["result"].get("changed"),
                    hit_count=raw["result"].get("hit_count"),
                    files_hit=tuple(raw["result"].get("files_hit", ())),
                    changed_files=tuple(raw["result"].get("changed_files", ())),
                    # MUST be listed here. This reconstruction is a HAND-MAINTAINED field list,
                    # and canonical events are hash-chained over the result's content: a field
                    # that serializes but does not rehydrate yields a DIFFERENT recomputed hash
                    # and the chain fails with `EventIntegrityError: event content hash/tamper
                    # mismatch`. Omitting it did exactly that -- and because
                    # `observe_action_result` swallows observer faults by design, the symptom
                    # appeared far away, as "the gateway produced nothing".
                    viewed_symbols=tuple(raw["result"].get("viewed_symbols", ())),
                    resolved_symbols=tuple(raw["result"].get("resolved_symbols", ())),
                    failure_fingerprint=raw["result"].get("failure_fingerprint", ""),
                    signature_before=raw["result"].get("signature_before", ""),
                    signature_after=raw["result"].get("signature_after", ""),
                )
                if raw.get("result") is not None
                else None
            ),
        )
        # Host-only compatibility state. This is deliberately not a dataclass
        # field, so it never enters `_canonical_data`, equality, or new-event
        # serialization. `object.__setattr__` is required by the frozen type.
        object.__setattr__(event, "_stored_canonical_json", payload)
        return event


@dataclass(frozen=True)
class WorkState:
    attempt_id: str
    revision: RevisionVector
    sequence: int = 0
    phase: Phase = Phase.ORIENTATION
    focused_files: tuple[str, ...] = ()
    focused_symbols: tuple[str, ...] = ()
    viewed_files: tuple[str, ...] = ()
    edited_files: tuple[str, ...] = ()
    search_count: int = 0
    test_count: int = 0
    compile_count: int = 0
    consecutive_no_test_results: int = 0
    no_test_recovery_event_id: str = ""
    current_failures: tuple[str, ...] = ()
    failure_scopes: tuple[tuple[str, str], ...] = ()
    submit_proposed: bool = False
    decision_window_key: str = ""
    transition_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "focused_files",
            "focused_symbols",
            "viewed_files",
            "edited_files",
            "current_failures",
            "failure_scopes",
            "transition_rules",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    @classmethod
    def initial(cls, *, attempt_id: str, revision: RevisionVector) -> "WorkState":
        return cls(attempt_id=attempt_id, revision=revision)

    def canonical_json(self) -> str:
        return _canonical_json(self)

    @property
    def state_hash(self) -> str:
        return _sha256(self.canonical_json())

    @classmethod
    def from_json(cls, payload: str) -> "WorkState":
        raw = json.loads(payload)
        return cls(
            attempt_id=raw["attempt_id"],
            revision=RevisionVector(**raw["revision"]),
            sequence=int(raw.get("sequence", 0)),
            phase=Phase(raw.get("phase", Phase.ORIENTATION.value)),
            focused_files=tuple(raw.get("focused_files", ())),
            focused_symbols=tuple(raw.get("focused_symbols", ())),
            viewed_files=tuple(raw.get("viewed_files", ())),
            edited_files=tuple(raw.get("edited_files", ())),
            search_count=int(raw.get("search_count", 0)),
            test_count=int(raw.get("test_count", 0)),
            compile_count=int(raw.get("compile_count", 0)),
            consecutive_no_test_results=int(raw.get("consecutive_no_test_results", 0)),
            no_test_recovery_event_id=str(raw.get("no_test_recovery_event_id", "")),
            current_failures=tuple(raw.get("current_failures", ())),
            failure_scopes=tuple(
                (str(scope), str(fingerprint))
                for scope, fingerprint in raw.get("failure_scopes", ())
            ),
            submit_proposed=bool(raw.get("submit_proposed", False)),
            decision_window_key=str(raw.get("decision_window_key", "")),
            transition_rules=tuple(raw.get("transition_rules", ())),
        )


def _append_unique(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    if not value or value in values:
        return values
    return values + (value,)


_REPOSITORY_SYMBOL_SEPARATOR = "::"


def repository_symbol_identity(file_path: str, symbol: str) -> str:
    """Return the stable repository-qualified identity for one graph definition.

    This remains a string so canonical event and WorkState JSON shapes do not change. Paths are
    separator-normalized and leading ``./`` segments are removed; repository-root translation
    belongs to the graph resolver that owns the path authority.
    """
    path = str(file_path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    name = str(symbol or "").strip()
    if (
        not path
        or not name
        or _REPOSITORY_SYMBOL_SEPARATOR in path
        or _REPOSITORY_SYMBOL_SEPARATOR in name
    ):
        return ""
    return f"{path}{_REPOSITORY_SYMBOL_SEPARATOR}{name}"


def split_repository_symbol_identity(identity: str) -> tuple[str, str] | None:
    """Split ``repo/path.py::symbol`` without treating a legacy bare name as qualified."""
    value = str(identity or "").strip()
    path, separator, symbol = value.rpartition(_REPOSITORY_SYMBOL_SEPARATOR)
    if not separator:
        return None
    normalized = repository_symbol_identity(path, symbol)
    if not normalized:
        return None
    normalized_path, _separator, normalized_symbol = normalized.rpartition(
        _REPOSITORY_SYMBOL_SEPARATOR
    )
    return normalized_path, normalized_symbol


def _validation_scope(subject: str) -> str:
    normalized = str(subject).strip().replace("\\", "/")
    return _sha256(normalized)[:16] if normalized else ""


def _normalize_repository_subject(subject: str) -> str:
    """Normalize a repository-relative subject without resolving parent traversal."""
    normalized = str(subject or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def reduce_event(state: WorkState, event: CanonicalEvent) -> WorkState:
    """Purely reduce one canonical event into a new immutable work-state."""

    if event.attempt_id != state.attempt_id:
        raise StateIntegrityError("attempt identity mismatch")
    if event.sequence != state.sequence + 1:
        raise StateIntegrityError(
            f"event sequence gap: expected {state.sequence + 1}, got {event.sequence}"
        )
    if event.revision_before != state.revision:
        raise StateIntegrityError("repository revision mismatch before reduction")

    phase = state.phase
    focused_files = state.focused_files
    focused_symbols = state.focused_symbols
    viewed_files = state.viewed_files
    edited_files = state.edited_files
    search_count = state.search_count
    test_count = state.test_count
    compile_count = state.compile_count
    consecutive_no_test_results = state.consecutive_no_test_results
    no_test_recovery_event_id = state.no_test_recovery_event_id
    current_failures = state.current_failures
    failure_scopes = state.failure_scopes
    submit_proposed = state.submit_proposed
    decision_window_key = state.decision_window_key
    rules: list[str] = []
    repository_changed = False
    saw_test_result = False
    saw_compile_result = False

    for outcome in event.outcomes:
        if outcome.kind in {
            SemanticKind.SEARCH_RESULT,
            SemanticKind.SEARCH_EMPTY,
            SemanticKind.SEARCH_FAILED,
        }:
            search_count += 1
            # ORDER IS LOAD-BEARING: this guard reads `focused_symbols` as it stood BEFORE
            # this search. Extending focus first would suppress `search_without_selected_symbol`
            # on the very search that resolved the symbol, silently changing phase behaviour
            # that predates this feature.
            if not focused_symbols and phase is Phase.ORIENTATION:
                phase = Phase.DISCOVERY
                rules.append("search_without_selected_symbol")
            # A search whose bare operand resolves to one repository-qualified graph definition
            # puts that identity in play. Carried as metadata rather than as a SYMBOL_VIEWED
            # outcome on purpose:
            # that kind advances `phase` to UNDERSTANDING, and searching is not understanding
            # -- `_active_decision` derives the open decision from the phase, so a false
            # advance would make the oracle reason about the wrong moment. Phase handling above
            # is deliberately untouched.
            for _key, _value in outcome.metadata:
                if _key != "resolved_symbols":
                    continue
                for _symbol in str(_value or "").split("|"):
                    if _symbol:
                        focused_symbols = _append_unique(focused_symbols, _symbol)
                        rules.append("search_resolved_symbol")
        elif outcome.kind is SemanticKind.SOURCE_VIEWED:
            viewed_files = _append_unique(viewed_files, outcome.subject)
            focused_files = _append_unique(focused_files, outcome.subject)
            phase = Phase.UNDERSTANDING
            rules.append("source_viewed")
        elif outcome.kind is SemanticKind.SYMBOL_VIEWED:
            focused_symbols = _append_unique(focused_symbols, outcome.subject)
            phase = Phase.UNDERSTANDING
            rules.append("symbol_viewed")
        elif outcome.kind is SemanticKind.EDIT_PROPOSED:
            phase = Phase.IMPLEMENTATION
            rules.append("edit_proposed")
        elif outcome.kind in {
            SemanticKind.EDIT_EXECUTED,
            SemanticKind.FILE_CREATED,
            SemanticKind.FILE_DELETED,
            SemanticKind.FILE_RENAMED,
        }:
            if outcome.changed is True:
                edited_files = _append_unique(edited_files, outcome.subject)
                focused_files = _append_unique(focused_files, outcome.subject)
                phase = Phase.IMPLEMENTATION
                repository_changed = True
                # The recovery threshold belongs to the current edit epoch.
                # A zero-test result observed before this mutation cannot count
                # as one of two executions after it.
                consecutive_no_test_results = 0
                rules.append("repository_mutation")
        elif outcome.kind is SemanticKind.TEST_EXECUTED_NO_TESTS:
            consecutive_no_test_results += 1
            phase = Phase.VALIDATION
            if consecutive_no_test_results == 2 and edited_files and not no_test_recovery_event_id:
                no_test_recovery_event_id = event.event_id
                phase = Phase.RECOVERY
                rules.append("repeated_no_tests_after_edit")
        elif outcome.kind in {
            SemanticKind.TEST_RESULT,
            SemanticKind.TEST_PASS,
            SemanticKind.TEST_FAIL,
            SemanticKind.TEST_ENV_FAIL,
        }:
            saw_test_result = True
            phase = Phase.VALIDATION
            if outcome.kind in {
                SemanticKind.TEST_PASS,
                SemanticKind.TEST_FAIL,
                SemanticKind.TEST_ENV_FAIL,
            }:
                consecutive_no_test_results = 0
            if outcome.kind in {SemanticKind.TEST_FAIL, SemanticKind.TEST_ENV_FAIL}:
                fingerprint = outcome.failure_fingerprint or outcome.subject
                scope = _validation_scope(outcome.subject)
                failure_key = (scope, fingerprint)
                repeated_failure = (
                    failure_key in failure_scopes if scope else fingerprint in current_failures
                )
                current_failures = _append_unique(current_failures, fingerprint)
                if scope and failure_key not in failure_scopes:
                    failure_scopes = failure_scopes + (failure_key,)
                if outcome.kind is SemanticKind.TEST_FAIL and edited_files:
                    phase = Phase.RECOVERY
                    rules.append(
                        "repeated_failure_after_edit" if repeated_failure else "failure_after_edit"
                    )
                elif repeated_failure and edited_files:
                    phase = Phase.RECOVERY
                    rules.append("repeated_failure_after_edit")
            elif outcome.kind is SemanticKind.TEST_PASS:
                scope = _validation_scope(outcome.subject)
                if scope:
                    removed = {
                        fingerprint
                        for failure_scope, fingerprint in failure_scopes
                        if failure_scope == scope
                    }
                    failure_scopes = tuple(pair for pair in failure_scopes if pair[0] != scope)
                    remaining_fingerprints = {fingerprint for _, fingerprint in failure_scopes}
                    current_failures = tuple(
                        fingerprint
                        for fingerprint in current_failures
                        if (fingerprint not in removed or fingerprint in remaining_fingerprints)
                    )
                elif outcome.failure_fingerprint:
                    current_failures = tuple(
                        item for item in current_failures if item != outcome.failure_fingerprint
                    )
        elif outcome.kind is SemanticKind.COMPILE_RESULT:
            saw_compile_result = True
            consecutive_no_test_results = 0
            phase = Phase.VALIDATION
        elif outcome.kind is SemanticKind.SUBMIT_PROPOSED:
            submit_proposed = True
            phase = Phase.COMPLETION
            rules.append("submit_proposed")
        elif outcome.kind is SemanticKind.SUBMIT_BLOCKED:
            submit_proposed = True
            phase = Phase.REVIEW
            rules.append("submit_blocked")

    if saw_test_result:
        test_count += 1
        rules.append("validation_result")
    if saw_compile_result:
        compile_count += 1
        rules.append("compile_result")

    repository_content_advanced = (
        event.revision_after.repository_content != event.revision_before.repository_content
    )
    if repository_changed and not repository_content_advanced:
        # A NO-OP EDIT. Not corruption, and NOT the mirror of the check below.
        #
        # `changed=True` means GT OBSERVED A WRITE (`changed_files` non-empty from the edit
        # bridge). `repository_content` digests HEAD + status + diff + untracked bytes, so a
        # write producing IDENTICAL BYTES advances neither -- which is routine: a `sed -i`
        # whose pattern matches nothing, a rewrite of content already present, an editor
        # action retried after it already landed, or a digest command that failed and
        # returned the same `unavailable:` sentinel on both sides.
        #
        # This check was kept fatal when H2 relaxed the opposite direction, on the argument
        # that GT was "contradicting itself". That does not survive the data: the reducer
        # cannot distinguish a hallucinated mutation from one that wrote identical bytes --
        # the two produce byte-identical state -- so it cannot justify calling one
        # corruption. And the penalty is total: `append_event` persists before reducing, the
        # raise classifies as REDUCER_INVARIANT_VIOLATION (a CORE corruption code), replay
        # re-reduces the same event, and the attempt is quarantined. Measured on run
        # 30246661710: 45 oracle evaluations at iteration 0, then ONE of these, then
        # `dark_fallback` on every iteration after -- the oracle never cycled again.
        #
        # Record it and continue. "The agent believes it edited and the repository did not
        # move" is a real signal worth counting, not one worth dying on.
        rules.append("no_op_mutation")
    if not repository_changed and repository_content_advanced:
        # NOT the mirror image of the check above, and NOT corruption.
        #
        # `repository_content` is the live `_canonical_repository_digest`: git HEAD +
        # `status --porcelain` + `diff --binary` + the CONTENTS of untracked files. It
        # therefore advances for many benign reasons that have nothing to do with GT's
        # bookkeeping -- the agent running the test suite and pytest writing a cache,
        # `pip install -e .` writing egg-info, codegen or a lockfile updater -- and for one
        # genuine GT blind spot: a source mutation made through a shape GT does not
        # classify as an edit (`sed -i`, `git apply`, a heredoc redirect).
        #
        # Only that last case is interesting, and none of them is GT contradicting itself.
        # Raising was the most destructive available response: `append_event` persists
        # before reducing, so the raise surfaces as a REDUCER_INVARIANT_VIOLATION -- a core
        # corruption code -- replay re-reduces the same event, and the attempt is
        # quarantined with `gt_emission_enabled=False`. Because `_augment_output` falls back
        # to the legacy path only when the attachment is None, a quarantined runtime is not
        # a degradation but TOTAL delivery loss for the rest of the task.
        #
        # Record the fact and adopt the advanced revision instead. Nothing is swallowed:
        # the rule is durable replayable state an audit can count, and `revision` below
        # takes `revision_after`, so freshness invalidation retires evidence keyed to the
        # stale revision exactly as it would for a classified mutation.
        rules.append("unclassified_repository_advance")
    revision = event.revision_after
    decision_boundary_kinds = {
        SemanticKind.EDIT_PROPOSED,
        SemanticKind.EDIT_EXECUTED,
        SemanticKind.FILE_CREATED,
        SemanticKind.FILE_DELETED,
        SemanticKind.FILE_RENAMED,
        SemanticKind.TEST_RESULT,
        SemanticKind.TEST_PASS,
        SemanticKind.TEST_FAIL,
        SemanticKind.TEST_ENV_FAIL,
        SemanticKind.TEST_EXECUTED_NO_TESTS,
        SemanticKind.COMPILE_RESULT,
        SemanticKind.SUBMIT_PROPOSED,
        SemanticKind.SUBMIT_BLOCKED,
    }
    if phase is not state.phase or any(
        outcome.kind in decision_boundary_kinds for outcome in event.outcomes
    ):
        decision_window_key = event.event_id

    return WorkState(
        attempt_id=state.attempt_id,
        revision=revision,
        sequence=event.sequence,
        phase=phase,
        focused_files=focused_files,
        focused_symbols=focused_symbols,
        viewed_files=viewed_files,
        edited_files=edited_files,
        search_count=search_count,
        test_count=test_count,
        compile_count=compile_count,
        consecutive_no_test_results=consecutive_no_test_results,
        no_test_recovery_event_id=no_test_recovery_event_id,
        current_failures=current_failures,
        failure_scopes=failure_scopes,
        submit_proposed=submit_proposed,
        decision_window_key=decision_window_key,
        transition_rules=state.transition_rules + tuple(rules),
    )


class ReasoningNodeKind(str, Enum):
    ISSUE_REQUIREMENT = "ISSUE_REQUIREMENT"
    BEHAVIORAL_INVARIANT = "BEHAVIORAL_INVARIANT"
    UNKNOWN = "UNKNOWN"
    QUESTION = "QUESTION"
    CANDIDATE_TARGET = "CANDIDATE_TARGET"
    OPERATIONAL_HYPOTHESIS = "OPERATIONAL_HYPOTHESIS"
    EVIDENCE = "EVIDENCE"
    COUNTEREVIDENCE = "COUNTEREVIDENCE"
    CONFLICT = "CONFLICT"
    CONTRACT = "CONTRACT"
    OBLIGATION = "OBLIGATION"
    DECISION = "DECISION"
    EDIT = "EDIT"
    VALIDATION = "VALIDATION"
    FAILURE = "FAILURE"
    CLOSED_BRANCH = "CLOSED_BRANCH"


class ReasoningEdgeKind(str, Enum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    DEPENDS_ON = "DEPENDS_ON"
    REQUIRES = "REQUIRES"
    SATISFIES = "SATISFIES"
    VIOLATES = "VIOLATES"
    TARGETS = "TARGETS"
    TESTS = "TESTS"
    INVALIDATES = "INVALIDATES"
    SUPERSEDES = "SUPERSEDES"
    CLOSES = "CLOSES"
    DERIVED_FROM = "DERIVED_FROM"
    VISIBLE_BEFORE = "VISIBLE_BEFORE"


class HypothesisState(str, Enum):
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    SUPPORTED = "SUPPORTED"
    WEAKENED = "WEAKENED"
    CONTRADICTED = "CONTRADICTED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


class OperationalSignalKind(str, Enum):
    EXACT_SEARCH = "EXACT_SEARCH"
    FOCUSED_SYMBOL_VIEW = "FOCUSED_SYMBOL_VIEW"
    EDIT_PROPOSED = "EDIT_PROPOSED"
    VALIDATION_SUPPORT = "VALIDATION_SUPPORT"
    UNCHANGED_FAILURE_AFTER_EDIT = "UNCHANGED_FAILURE_AFTER_EDIT"
    VERIFIED_COUNTEREVIDENCE = "VERIFIED_COUNTEREVIDENCE"
    ABANDON_TARGET = "ABANDON_TARGET"
    SUPERSEDING_HYPOTHESIS = "SUPERSEDING_HYPOTHESIS"


@dataclass(frozen=True)
class OperationalSignal:
    attempt_id: str
    event_id: str
    sequence: int
    source_event_sequence: int
    source_event_hash: str
    revision: RevisionVector
    authority: Authority
    hypothesis_id: str
    subject: str
    kind: OperationalSignalKind
    related_node_id: str = ""

    def __post_init__(self) -> None:
        if (
            not self.attempt_id
            or not self.event_id
            or self.sequence < 1
            or self.source_event_sequence < 1
            or not self.hypothesis_id
            or not self.subject
        ):
            raise ValueError("operational signal identity is incomplete")
        _validate_sha256(
            self.source_event_hash,
            field_name="source_event_hash",
        )


@dataclass(frozen=True)
class HypothesisTransition:
    from_state: HypothesisState | None
    to_state: HypothesisState
    event_id: str
    reason_code: str


@dataclass(frozen=True)
class ReasoningNode:
    node_id: str
    kind: ReasoningNodeKind
    subject: str
    hypothesis_state: HypothesisState | None = None
    transitions: tuple[HypothesisTransition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "transitions", tuple(self.transitions))


@dataclass(frozen=True)
class ReasoningEdge:
    source_id: str
    target_id: str
    kind: ReasoningEdgeKind
    event_id: str


@dataclass(frozen=True)
class ReasoningGraph:
    attempt_id: str
    revision: RevisionVector
    sequence: int = 0
    last_source_event_sequence: int = 0
    last_source_event_hash: str = ""
    source_event_ids: tuple[str, ...] = ()
    nodes: tuple[ReasoningNode, ...] = ()
    edges: tuple[ReasoningEdge, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "source_event_ids", tuple(self.source_event_ids))
        ids = tuple(node.node_id for node in self.nodes)
        if len(set(ids)) != len(ids):
            raise StateIntegrityError("reasoning graph contains duplicate node ids")
        id_set = set(ids)
        if any(edge.source_id not in id_set or edge.target_id not in id_set for edge in self.edges):
            raise StateIntegrityError("reasoning graph edge endpoint is unknown")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise StateIntegrityError("reasoning graph repeats a source event")

    @classmethod
    def initial(
        cls,
        *,
        attempt_id: str,
        revision: RevisionVector,
    ) -> "ReasoningGraph":
        return cls(attempt_id=attempt_id, revision=revision)

    def node(self, node_id: str) -> ReasoningNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def canonical_json(self) -> str:
        return _canonical_json(self)

    @property
    def graph_hash(self) -> str:
        return _sha256(self.canonical_json())

    def connected(self, source_id: str, target_id: str) -> bool:
        node_ids = {node.node_id for node in self.nodes}
        if source_id not in node_ids or target_id not in node_ids:
            return False
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source_id, set()).add(edge.target_id)
            adjacency.setdefault(edge.target_id, set()).add(edge.source_id)
        frontier = [source_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current == target_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(sorted(adjacency.get(current, ()), reverse=True))
        return False


_HYPOTHESIS_TRANSITIONS: dict[
    OperationalSignalKind,
    tuple[frozenset[HypothesisState | None], HypothesisState, str],
] = {
    OperationalSignalKind.EXACT_SEARCH: (
        frozenset({None}),
        HypothesisState.CANDIDATE,
        "EXACT_SEARCH_OPENED_CANDIDATE",
    ),
    # ACTIVE is a legal FROM (added 2026-07-28). An agent views many symbols in a row --
    # `SemanticKind.SYMBOL_VIEWED` maps here unconditionally -- so the SECOND view of a task
    # arrived on an already-ACTIVE hypothesis, raised, and the raise isolated the canonical
    # observer for the whole attempt. Run 30390877219, 4/4 tasks:
    #   observe_failed:StateIntegrityError:
    #     illegal hypothesis transition HypothesisState.ACTIVE via FOCUSED_SYMBOL_VIEW
    # followed by `canonical_observer_dark:legacy_delivery_resumed` on 59/61, 38/42, 12/18 and
    # 6/17 remaining observations. One ObservationBinding per task -- the step-0 one, made
    # before the death -- so the canonical proof chain was dark for ~97% of the run.
    #
    # This is the MIRROR of the `orphaned_outcome` case below: that one exists because an
    # OUTCOME can arrive when GT never opened a hypothesis; this one is an OPENING arriving
    # when GT already did. Same root -- GT is never told the agent's hypotheses, it infers
    # them -- with the polarity flipped.
    #
    # Widening rather than skipping, because the target state IS ACTIVE: admitting ACTIVE as a
    # FROM introduces no newly reachable state, so the invariant this table protects is
    # untouched. It also matches EDIT_PROPOSED directly below, which has always been
    # idempotent on ACTIVE for exactly the same reason. Subject rebinding is still refused
    # upstream, so a view of a DIFFERENT subject on the same hypothesis id still raises.
    OperationalSignalKind.FOCUSED_SYMBOL_VIEW: (
        frozenset({None, HypothesisState.CANDIDATE, HypothesisState.ACTIVE}),
        HypothesisState.ACTIVE,
        "FOCUSED_SYMBOL_VIEW_ACTIVATED",
    ),
    OperationalSignalKind.EDIT_PROPOSED: (
        frozenset({None, HypothesisState.CANDIDATE, HypothesisState.ACTIVE}),
        HypothesisState.ACTIVE,
        "EDIT_PROPOSED_CONFIRMED_COMMITMENT",
    ),
    # CANDIDATE is a legal FROM (added 2026-07-28). This was the LAST live-reachable cell that
    # quarantined the canonical observer: the agent greps a symbol (GT opens CANDIDATE) and a
    # test then passes on it, with no intervening focused view. Ordinary behaviour; it cost the
    # whole attempt's proof chain.
    #
    # WIDEN rather than skip -- the opposite of the rule for the three OPENING kinds. An opening
    # is a re-observation and asserts nothing about progress, so skipping it loses nothing. This
    # is an OUTCOME: a test actually passed, which is observed execution truth, and a no-op
    # would DISCARD it. Widening keeps it.
    #
    # Safe because the move is MONOTONE: CANDIDATE -> SUPPORTED introduces no newly reachable
    # state (SUPPORTED was already the target from ACTIVE), and the only thing dropped is an
    # ACTIVE waypoint GT never actually observed. Nothing regresses: unlike widening an opening
    # -- which would drag a validated hypothesis BACKWARDS to CANDIDATE -- this only advances.
    OperationalSignalKind.VALIDATION_SUPPORT: (
        frozenset({HypothesisState.CANDIDATE, HypothesisState.ACTIVE}),
        HypothesisState.SUPPORTED,
        "VALIDATION_SUPPORTED_HYPOTHESIS",
    ),
    OperationalSignalKind.UNCHANGED_FAILURE_AFTER_EDIT: (
        frozenset({HypothesisState.ACTIVE, HypothesisState.SUPPORTED}),
        HypothesisState.WEAKENED,
        "UNCHANGED_FAILURE_WEAKENED_HYPOTHESIS",
    ),
    OperationalSignalKind.VERIFIED_COUNTEREVIDENCE: (
        frozenset({HypothesisState.ACTIVE, HypothesisState.SUPPORTED, HypothesisState.WEAKENED}),
        HypothesisState.CONTRADICTED,
        "VERIFIED_COUNTEREVIDENCE_CONTRADICTED",
    ),
    OperationalSignalKind.ABANDON_TARGET: (
        frozenset({HypothesisState.ACTIVE, HypothesisState.WEAKENED, HypothesisState.CONTRADICTED}),
        HypothesisState.ABANDONED,
        "OBSERVABLE_TARGET_ABANDONED",
    ),
    OperationalSignalKind.SUPERSEDING_HYPOTHESIS: (
        frozenset({HypothesisState.ACTIVE, HypothesisState.SUPPORTED, HypothesisState.WEAKENED}),
        HypothesisState.SUPERSEDED,
        "NEW_OPERATIONAL_HYPOTHESIS_SUPERSEDED",
    ),
}


def reduce_reasoning_signal(
    graph: ReasoningGraph,
    signal: OperationalSignal,
) -> ReasoningGraph:
    if signal.sequence != graph.sequence + 1:
        raise StateIntegrityError(f"reasoning signal sequence gap: expected {graph.sequence + 1}")
    if signal.attempt_id != graph.attempt_id:
        raise StateIntegrityError("reasoning signal attempt identity mismatch")
    same_source_event = signal.event_id in graph.source_event_ids
    if same_source_event and (
        signal.source_event_sequence != graph.last_source_event_sequence
        or signal.source_event_hash != graph.last_source_event_hash
    ):
        raise StateIntegrityError("reasoning signal source event identity diverged")
    if signal.source_event_sequence < graph.last_source_event_sequence:
        raise StateIntegrityError("reasoning source event order regressed")
    if signal.source_event_sequence == graph.last_source_event_sequence and not same_source_event:
        raise StateIntegrityError("reasoning source event sequence is duplicated")
    nodes = list(graph.nodes)
    edges = list(graph.edges)
    index = next(
        (position for position, node in enumerate(nodes) if node.node_id == signal.hypothesis_id),
        None,
    )
    current = None if index is None else nodes[index].hypothesis_state
    if index is not None and nodes[index].subject != signal.subject:
        raise StateIntegrityError("hypothesis subject cannot be rebound")
    allowed, target, reason = _HYPOTHESIS_TRANSITIONS[signal.kind]
    # AN ORPHANED OUTCOME IS NOT CORRUPTION.
    #
    # The table splits into OPENING transitions, which admit None (EXACT_SEARCH,
    # FOCUSED_SYMBOL_VIEW, EDIT_PROPOSED), and OUTCOME transitions, which require a
    # hypothesis to already exist (VALIDATION_SUPPORT, UNCHANGED_FAILURE_AFTER_EDIT,
    # VERIFIED_COUNTEREVIDENCE, ABANDON_TARGET, SUPERSEDING_HYPOTHESIS). But GT is never
    # TOLD the agent's hypotheses -- it infers them from observations. When the opening
    # observation is missed or classified as something else, the outcome arrives orphaned
    # with `current is None`.
    #
    # Measured on run 30276041709, where the fault detail named it in one line:
    #   observe_failed:StateIntegrityError:illegal hypothesis transition None via
    #   VALIDATION_SUPPORT
    # 52 compile attempts, all at iteration 0, then that fault, then dark_fallback forever.
    # The agent had gone find -> grep -> run a repro script: a validation outcome before GT
    # had opened anything for that subject. `append_event` persists before reducing, so the
    # raise classifies REDUCER_INVARIANT_VIOLATION (a CORE corruption code), replay
    # re-reduces the same event, and the observer is isolated for the whole attempt -- GT
    # loses its entire timing authority because the agent validated something GT had not yet
    # formed an opinion about.
    #
    # Skip the transition instead. Nothing transitions and NO node is invented, so the
    # invariant this check protects -- the machine never enters an illegal state -- holds
    # exactly as before. A mismatch from a REAL state still raises below: that means the
    # graph itself is inconsistent, which IS corruption.
    # A RE-OBSERVATION IS NOT A TRANSITION, AND NOT CORRUPTION EITHER.
    #
    # The ACTIVE+FOCUSED_SYMBOL_VIEW widening above fixed ONE cell. It is one of EIGHT where
    # the signal's target state IS the state the node is already in, and the other seven still
    # raise. Measured on the live producer surface: `_OUTCOME_SIGNAL_KIND` is the only thing
    # that constructs an OperationalSignal and it emits 4 of the 8 kinds, so the reachable grid
    # is 4x4 = 16 cells -- and 8 of those 16 raise today. Two are ordinary agent behaviour:
    #   CANDIDATE + EXACT_SEARCH        -- grep the same symbol twice
    #   SUPPORTED + VALIDATION_SUPPORT  -- run a passing test twice
    # and the whole SUPPORTED row raises, so once a passing test moves a hypothesis to
    # SUPPORTED, no signal the producer can emit for that subject avoids quarantine. A passing
    # test makes its own subject radioactive for the rest of the attempt.
    #
    # Same root cause as `orphaned_outcome` below and as the run-30390877219 crash: GT is never
    # TOLD the agent's hypotheses, it INFERS them from observations, so the same observation
    # legitimately arrives more than once. Widening each cell's allowed-from set one at a time
    # (what the FOCUSED_SYMBOL_VIEW fix did) also mints a fabricated self-transition per repeat.
    # No production code reads `.transitions` -- but a transition ledger whose entries are not
    # transitions is a bad thing to keep, and `transitions` is inside `canonical_json()` and so
    # inside `graph_hash`.
    #
    # NOT a claim that this makes `graph_hash` stable across repeats -- it does not, and an
    # earlier draft of this comment said otherwise. `sequence`, `last_source_event_sequence`
    # and `source_event_ids` are ALSO graph fields, and the reducer requires
    # `signal.sequence == graph.sequence + 1`, so ten repeat observations produce ten distinct
    # graph hashes both before and after this change. What changes is WHAT the hash encodes,
    # not how many values it takes.
    #
    # So: classify `current == target` as a state-preserving UPDATE. Nothing transitions, no
    # node is invented, no self-transition is recorded, and the edge block below still runs
    # because new counterevidence against an already-CONTRADICTED hypothesis is real
    # information. A mismatch from a state that is genuinely not the target still raises: that
    # means the graph itself is inconsistent, which IS corruption.
    #
    # A RE-OBSERVED OPENING IS NOT CORRUPTION EITHER, AND IT MUST NOT REGRESS THE STATE.
    #
    # `state_preserving` closed the six self-target cells. FIVE live-reachable cells still
    # raised, four of them an OPENING kind arriving at a state further along than the one it
    # opens (probe, 2026-07-28, over the whole `_OUTCOME_SIGNAL_KIND` 4x4 surface):
    #   ACTIVE    + EXACT_SEARCH         -- view a symbol, then grep it again
    #   SUPPORTED + EXACT_SEARCH / FOCUSED_SYMBOL_VIEW / EDIT_PROPOSED
    #                                    -- a test passed, then search/view/edit that surface
    # The whole opening half of the SUPPORTED row raised, so ONE passing test made its own
    # subject radioactive for the rest of the attempt.
    #
    # The table models GT's INFERENCE of the agent, not a structural invariant of the graph.
    # This function is the SOLE writer of `hypothesis_state` (the two `replace`/append sites
    # below and the SUPERSEDING related node); every graph reaching it was folded from
    # `ReasoningGraph.initial` over committed events -- `reduce_reasoning_event`,
    # `replay_reasoning_signals`, `recovery_assurance._RuntimeProjection`. No path deserialises
    # a node state from bytes. So `current` is always a state this function itself wrote, and an
    # opening that cannot advance from it means GT's inference lags the agent -- not corruption.
    #
    # Restricted to OPENINGS, derived as `None in allowed` (EXACT_SEARCH, FOCUSED_SYMBOL_VIEW,
    # EDIT_PROPOSED). The five OUTCOME kinds make CLAIMS about hypothesis progress -- validated,
    # weakened, contradicted, abandoned, superseded -- and their allowed-from sets encode which
    # claims are coherent; admitting those too would make the raise below unreachable for all 64
    # cells, i.e. dead code, and 18 outcome cells still raise because of this restriction.
    #
    # SKIP, never widen. Widening was right for the FOCUSED_SYMBOL_VIEW fix above because ACTIVE
    # is its target. It is wrong here: giving EXACT_SEARCH `SUPPORTED` as a legal source would
    # drive a validated hypothesis BACKWARDS to CANDIDATE, and EDIT_PROPOSED from SUPPORTED back
    # to ACTIVE, destroying observed execution truth because the agent grepped something twice.
    # Nothing transitions and no node is invented, so the invariant this check protects holds.
    #
    # Openings never reach the edge block below -- only VERIFIED_COUNTEREVIDENCE and
    # SUPERSEDING_HYPOTHESIS append edges, and neither admits None -- so a skipped opening
    # cannot fabricate an edge. Subject rebinding is still refused above, so this does NOT
    # become "any signal may hit any node".
    #
    # NOT FIXED HERE: `CANDIDATE + VALIDATION_SUPPORT` (grep a symbol, then a test passes on it)
    # is live-reachable and still raises. It is an OUTCOME, so this rule does not reach it, and
    # its repair -- widening VALIDATION_SUPPORT to `{CANDIDATE, ACTIVE}`, a monotone advance to
    # an already-reachable state -- is blocked by the explicit pin on that exact cell in
    # tests/runtime/test_orphaned_outcome_signal_20260727.py.
    state_preserving = current is not None and current == target
    orphaned_outcome = current is None and None not in allowed
    reobserved_opening = current is not None and None in allowed and current not in allowed
    skip_transition = orphaned_outcome or state_preserving or reobserved_opening
    if not skip_transition and current not in allowed:
        raise StateIntegrityError(
            f"illegal hypothesis transition {current} via {signal.kind.value}"
        )
    if not skip_transition:
        transition = HypothesisTransition(
            from_state=current,
            to_state=target,
            event_id=signal.event_id,
            reason_code=reason,
        )
        if index is None:
            nodes.append(
                ReasoningNode(
                    node_id=signal.hypothesis_id,
                    kind=ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
                    subject=signal.subject,
                    hypothesis_state=target,
                    transitions=(transition,),
                )
            )
        else:
            existing = nodes[index]
            nodes[index] = replace(
                existing,
                hypothesis_state=target,
                transitions=existing.transitions + (transition,),
            )

    if orphaned_outcome:
        # No hypothesis to relate an edge TO. Recording a CONTRADICTS/SUPERSEDES edge into a
        # node that was never opened would fabricate the very structure the skip avoids.
        pass
    elif signal.kind is OperationalSignalKind.VERIFIED_COUNTEREVIDENCE:
        if not signal.related_node_id:
            raise StateIntegrityError("counterevidence signal lacks related node")
        if all(node.node_id != signal.related_node_id for node in nodes):
            nodes.append(
                ReasoningNode(
                    node_id=signal.related_node_id,
                    kind=ReasoningNodeKind.COUNTEREVIDENCE,
                    subject=signal.related_node_id,
                )
            )
        edges.append(
            ReasoningEdge(
                source_id=signal.related_node_id,
                target_id=signal.hypothesis_id,
                kind=ReasoningEdgeKind.CONTRADICTS,
                event_id=signal.event_id,
            )
        )
    elif signal.kind is OperationalSignalKind.SUPERSEDING_HYPOTHESIS:
        if not signal.related_node_id:
            raise StateIntegrityError("superseding signal lacks related hypothesis")
        if all(node.node_id != signal.related_node_id for node in nodes):
            related_transition = HypothesisTransition(
                from_state=None,
                to_state=HypothesisState.CANDIDATE,
                event_id=signal.event_id,
                reason_code="SUPERSEDING_HYPOTHESIS_OBSERVED",
            )
            nodes.append(
                ReasoningNode(
                    node_id=signal.related_node_id,
                    kind=ReasoningNodeKind.OPERATIONAL_HYPOTHESIS,
                    subject=signal.related_node_id,
                    hypothesis_state=HypothesisState.CANDIDATE,
                    transitions=(related_transition,),
                )
            )
        edges.append(
            ReasoningEdge(
                source_id=signal.related_node_id,
                target_id=signal.hypothesis_id,
                kind=ReasoningEdgeKind.SUPERSEDES,
                event_id=signal.event_id,
            )
        )
    return ReasoningGraph(
        attempt_id=graph.attempt_id,
        revision=signal.revision,
        sequence=signal.sequence,
        last_source_event_sequence=signal.source_event_sequence,
        last_source_event_hash=signal.source_event_hash,
        source_event_ids=(
            graph.source_event_ids
            if same_source_event
            else graph.source_event_ids + (signal.event_id,)
        ),
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


_OUTCOME_SIGNAL_KIND: Mapping[SemanticKind, OperationalSignalKind] = {
    SemanticKind.SEARCH_RESULT: OperationalSignalKind.EXACT_SEARCH,
    SemanticKind.SYMBOL_VIEWED: OperationalSignalKind.FOCUSED_SYMBOL_VIEW,
    SemanticKind.EDIT_PROPOSED: OperationalSignalKind.EDIT_PROPOSED,
    SemanticKind.TEST_PASS: OperationalSignalKind.VALIDATION_SUPPORT,
}


def derive_operational_signals(
    event: CanonicalEvent,
    *,
    starting_sequence: int,
) -> tuple[OperationalSignal, ...]:
    """Project observable commitments from one committed canonical event."""

    if type(starting_sequence) is not int or starting_sequence < 1:
        raise ValueError("starting_sequence must be a positive integer")
    signals: list[OperationalSignal] = []
    for outcome in event.outcomes:
        kind = _OUTCOME_SIGNAL_KIND.get(outcome.kind)
        if kind is None or not outcome.subject:
            continue
        signals.append(
            OperationalSignal(
                attempt_id=event.attempt_id,
                event_id=event.event_id,
                sequence=starting_sequence + len(signals),
                source_event_sequence=event.sequence,
                source_event_hash=event.content_hash,
                revision=event.revision_after,
                authority=outcome.authority,
                hypothesis_id=f"hyp:{outcome.subject}",
                subject=outcome.subject,
                kind=kind,
            )
        )
    return tuple(signals)


def reduce_reasoning_event(
    graph: ReasoningGraph,
    *,
    event: CanonicalEvent,
    signals: Sequence[OperationalSignal] | None = None,
) -> ReasoningGraph:
    """Reduce one canonical event, rejecting independently invented signals."""

    expected_event_sequence = graph.last_source_event_sequence + 1
    if event.sequence != expected_event_sequence:
        raise StateIntegrityError(
            "canonical reasoning event sequence gap: "
            f"expected {expected_event_sequence}, got {event.sequence}"
        )
    if event.attempt_id != graph.attempt_id:
        raise StateIntegrityError("reasoning event attempt identity mismatch")
    if graph.last_source_event_hash and (event.previous_event_hash != graph.last_source_event_hash):
        raise StateIntegrityError("reasoning event previous hash mismatch")
    expected = derive_operational_signals(
        event,
        starting_sequence=graph.sequence + 1,
    )
    provided = expected if signals is None else tuple(signals)
    if provided != expected:
        for signal in provided:
            if signal.source_event_hash != event.content_hash:
                raise StateIntegrityError("reasoning signal source hash mismatch")
            if signal.revision != event.revision_after:
                raise StateIntegrityError("reasoning signal revision mismatch")
            if signal.authority not in {outcome.authority for outcome in event.outcomes}:
                raise StateIntegrityError("reasoning signal authority mismatch")
        raise StateIntegrityError("reasoning signals do not equal canonical event projection")
    result = graph
    for signal in provided:
        result = reduce_reasoning_signal(result, signal)
    if not provided:
        return replace(
            graph,
            revision=event.revision_after,
            last_source_event_sequence=event.sequence,
            last_source_event_hash=event.content_hash,
            source_event_ids=graph.source_event_ids + (event.event_id,),
        )
    return result


def replay_reasoning_signals(
    *,
    attempt_id: str,
    revision: RevisionVector,
    signals: Iterable[OperationalSignal],
) -> ReasoningGraph:
    graph = ReasoningGraph.initial(attempt_id=attempt_id, revision=revision)
    for signal in signals:
        graph = reduce_reasoning_signal(graph, signal)
    return graph


class EventStore:
    """SQLite WAL-backed append-only event log with verified snapshots."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> "EventStore":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("event store is not open")
        return self._connection

    def open(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS canonical_events (
                event_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                previous_event_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                hash_schema TEXT NOT NULL DEFAULT '',
                UNIQUE(attempt_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS verified_snapshots (
                attempt_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_hash TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY(attempt_id, sequence)
            );
            """
        )
        # C5 -- additive migration for journals created before the hash-schema marker existed.
        # `CREATE TABLE IF NOT EXISTS` does NOT retrofit a column onto an existing file, so
        # without this an older journal would keep reporting `hash_schema` as absent rather
        # than as ''. Same shape as the `RuntimeJournal.open` migration ("additive migration
        # keeps those append-only records readable without rewriting them").
        #
        # ALTER TABLE ADD COLUMN is DDL, not a row UPDATE, so the `canonical_events_no_update`
        # append-only trigger does not fire and no historical row is rewritten -- the existing
        # rows simply read ''. An empty marker means "verify the stored bytes as
        # historical schema"; it is not permission to recompute them with the live
        # dataclass shape.
        _event_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(canonical_events)").fetchall()
        }
        if "hash_schema" not in _event_columns:
            self._connection.execute(
                "ALTER TABLE canonical_events ADD COLUMN hash_schema TEXT NOT NULL DEFAULT ''"
            )
        self._connection.commit()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _head(self, attempt_id: str) -> tuple[int, str] | None:
        row = self.connection.execute(
            """
            SELECT sequence, content_hash
            FROM canonical_events
            WHERE attempt_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), str(row[1])

    def _validate_next(
        self,
        event: CanonicalEvent,
        *,
        head: tuple[int, str] | None,
        seen_ids: set[str],
    ) -> tuple[int, str]:
        if event.event_id in seen_ids:
            raise EventIntegrityError(f"duplicate event id: {event.event_id}")
        existing = self.connection.execute(
            "SELECT 1 FROM canonical_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            raise EventIntegrityError(f"duplicate event id: {event.event_id}")

        expected_sequence = 1 if head is None else head[0] + 1
        if event.sequence != expected_sequence:
            raise EventIntegrityError(
                f"event sequence gap: expected {expected_sequence}, got {event.sequence}"
            )
        expected_parent = "" if head is None else head[1]
        if event.previous_event_hash != expected_parent:
            raise EventIntegrityError("event parent hash does not match the committed causal head")
        return event.sequence, event.content_hash

    def append(self, event: CanonicalEvent) -> None:
        self.append_batch((event,))

    def append_batch(self, events: Sequence[CanonicalEvent]) -> None:
        if not events:
            return
        heads: dict[str, tuple[int, str] | None] = {}
        seen_ids: set[str] = set()
        rows: list[tuple[str, str, int, str, str, str, str]] = []

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for event in events:
                if event.attempt_id not in heads:
                    heads[event.attempt_id] = self._head(event.attempt_id)
                heads[event.attempt_id] = self._validate_next(
                    event,
                    head=heads[event.attempt_id],
                    seen_ids=seen_ids,
                )
                seen_ids.add(event.event_id)
                payload = event.canonical_json()
                # A rehydrated historical event owns its original serialized
                # bytes. Never relabel those bytes as the current schema merely
                # because this process is current.
                hash_schema = CANONICAL_HASH_SCHEMA if payload == _canonical_json(event) else ""
                rows.append(
                    (
                        event.event_id,
                        event.attempt_id,
                        event.sequence,
                        event.previous_event_hash,
                        event.content_hash,
                        payload,
                        hash_schema,
                    )
                )
            self.connection.executemany(
                """
                INSERT INTO canonical_events(
                    event_id, attempt_id, sequence, previous_event_hash,
                    content_hash, canonical_json, hash_schema
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    @staticmethod
    def _verified_event_row(
        row: Sequence[object],
        *,
        attempt_id: str,
    ) -> CanonicalEvent:
        """Verify one stored row before interpreting its hash schema.

        Raw stored bytes own the append-only integrity claim. Schema labels are
        interpreted only after that claim passes, otherwise a corrupt payload
        could hide behind an arbitrary "old schema" marker.
        """
        row_event_id = str(row[0])
        row_sequence = int(row[1])
        row_parent_hash = str(row[2])
        row_content_hash = str(row[3])
        payload = str(row[4])
        row_hash_schema = str(row[5] or "")

        if _sha256(payload) != row_content_hash:
            raise EventIntegrityError(
                f"event content hash/tamper mismatch at sequence {row_sequence}"
            )

        if row_hash_schema and row_hash_schema != CANONICAL_HASH_SCHEMA:
            raise EventSchemaVersionError(
                f"canonical event at sequence {row_sequence} uses unknown hash "
                f"schema {row_hash_schema!r}; this build reads "
                f"{CANONICAL_HASH_SCHEMA!r}"
            )

        try:
            event = CanonicalEvent.from_json(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise EventIntegrityError(
                f"corrupt canonical event payload at sequence {row_sequence}"
            ) from exc

        if (
            event.event_id != row_event_id
            or event.attempt_id != attempt_id
            or event.sequence != row_sequence
            or event.previous_event_hash != row_parent_hash
        ):
            raise EventIntegrityError(f"event identity/tamper mismatch at sequence {row_sequence}")

        # A row claiming the current schema must reproduce the current canonical
        # shape exactly. A hash-valid older payload carrying a current marker is
        # schema drift, not content tampering.
        if row_hash_schema == CANONICAL_HASH_SCHEMA and _canonical_json(event) != payload:
            raise EventSchemaVersionError(
                f"canonical event at sequence {row_sequence} claims hash schema "
                f"{CANONICAL_HASH_SCHEMA!r} but its payload does not round-trip "
                "through that schema"
            )
        return event

    def events(self, attempt_id: str, *, after_sequence: int = 0) -> tuple[CanonicalEvent, ...]:
        parent_hash = ""
        if after_sequence:
            parent = self.connection.execute(
                """
                SELECT event_id, sequence, previous_event_hash, content_hash,
                       canonical_json, hash_schema
                FROM canonical_events
                WHERE attempt_id = ? AND sequence = ?
                """,
                (attempt_id, after_sequence),
            ).fetchone()
            if parent is None:
                raise EventIntegrityError(
                    "event tail has no committed parent at requested sequence"
                )
            self._verified_event_row(parent, attempt_id=attempt_id)
            parent_hash = str(parent[3])
        rows = self.connection.execute(
            """
            SELECT event_id, sequence, previous_event_hash, content_hash, canonical_json,
                   hash_schema
            FROM canonical_events
            WHERE attempt_id = ? AND sequence > ?
            ORDER BY sequence ASC
            """,
            (attempt_id, after_sequence),
        ).fetchall()
        result: list[CanonicalEvent] = []
        expected_sequence = after_sequence + 1
        for row in rows:
            row_sequence = int(row[1])
            row_parent_hash = str(row[2])
            row_content_hash = str(row[3])
            event = self._verified_event_row(row, attempt_id=attempt_id)
            if row_sequence != expected_sequence:
                raise EventIntegrityError(
                    f"event sequence gap at {row_sequence}; expected {expected_sequence}"
                )
            if row_parent_hash != parent_hash:
                raise EventIntegrityError(f"event parent hash mismatch at sequence {row_sequence}")
            result.append(event)
            expected_sequence += 1
            parent_hash = row_content_hash
        return tuple(result)

    def save_snapshot(self, state: WorkState) -> None:
        committed_events = self.events(state.attempt_id)
        prefix = tuple(event for event in committed_events if event.sequence <= state.sequence)
        if state.sequence == 0:
            replayed = WorkState.initial(
                attempt_id=state.attempt_id,
                revision=state.revision,
            )
        elif not prefix or prefix[-1].sequence != state.sequence:
            raise StateIntegrityError("snapshot sequence has no committed replay prefix")
        else:
            replayed = WorkState.initial(
                attempt_id=state.attempt_id,
                revision=prefix[0].revision_before,
            )
            for event in prefix:
                replayed = reduce_event(replayed, event)
        if replayed != state:
            raise StateIntegrityError(
                "snapshot state is not derived from deterministic event replay"
            )
        head = self.connection.execute(
            """
            SELECT content_hash
            FROM canonical_events
            WHERE attempt_id = ? AND sequence = ?
            """,
            (state.attempt_id, state.sequence),
        ).fetchone()
        if state.sequence and head is None:
            raise StateIntegrityError("snapshot sequence has no committed event")
        event_hash = "" if head is None else str(head[0])
        payload = state.canonical_json()
        self.connection.execute(
            """
            INSERT OR REPLACE INTO verified_snapshots(
                attempt_id, sequence, event_hash, state_hash, state_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                state.attempt_id,
                state.sequence,
                event_hash,
                _sha256(payload),
                payload,
            ),
        )
        self.connection.commit()

    def load_snapshot_and_tail(
        self,
        attempt_id: str,
    ) -> tuple[WorkState, tuple[CanonicalEvent, ...]]:
        row = self.connection.execute(
            """
            SELECT sequence, event_hash, state_hash, state_json
            FROM verified_snapshots
            WHERE attempt_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise StateIntegrityError("no verified snapshot for attempt")
        sequence, event_hash, state_hash, payload = (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
        )
        if _sha256(payload) != state_hash:
            raise StateIntegrityError("snapshot state hash mismatch")
        committed_events = self.events(attempt_id)
        if sequence:
            if (
                len(committed_events) < sequence
                or committed_events[sequence - 1].content_hash != event_hash
            ):
                raise StateIntegrityError("snapshot event hash mismatch")
        state = WorkState.from_json(payload)
        if state.attempt_id != attempt_id or state.sequence != sequence:
            raise StateIntegrityError("snapshot identity mismatch")
        if sequence:
            replayed = WorkState.initial(
                attempt_id=attempt_id,
                revision=committed_events[0].revision_before,
            )
            for event in committed_events[:sequence]:
                replayed = reduce_event(replayed, event)
        else:
            replayed = WorkState.initial(
                attempt_id=attempt_id,
                revision=state.revision,
            )
        if replayed != state:
            raise StateIntegrityError("snapshot state does not match deterministic replay")
        return state, committed_events[sequence:]


def _delivery_attempt_from_json(payload: str) -> "DeliveryAttempt":
    raw = json.loads(payload)
    terminal_kind = raw.get("terminal_kind")
    return DeliveryAttempt(
        evidence_ids=tuple(raw["evidence_ids"]),
        capsule_hash=raw["capsule_hash"],
        model_call_id=raw["model_call_id"],
        state=DeliveryState(raw["state"]),
        observation_id=raw.get("observation_id", ""),
        joined_capsule_hash=raw.get("joined_capsule_hash", ""),
        provider_payload_hash=raw.get("provider_payload_hash", ""),
        provider_response_id=raw.get("provider_response_id", ""),
        terminal_kind=(ProviderTerminalKind(terminal_kind) if terminal_kind is not None else None),
        terminal_reason=raw.get("terminal_reason", ""),
        response_hash=raw.get("response_hash", ""),
        failure_reason=raw.get("failure_reason", ""),
    )


class RuntimeJournal(EventStore):
    """Unified append-only event and provider-delivery persistence."""

    def open(self) -> None:
        super().open()
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                delivery_attempt_id TEXT PRIMARY KEY,
                model_call_id TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS delivery_journal (
                delivery_attempt_id TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL,
                model_call_id TEXT NOT NULL,
                state TEXT NOT NULL,
                capsule_hash TEXT NOT NULL,
                provider_payload_hash TEXT NOT NULL,
                response_hash TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                PRIMARY KEY(delivery_attempt_id, journal_sequence),
                FOREIGN KEY(delivery_attempt_id)
                    REFERENCES delivery_attempts(delivery_attempt_id)
            );
            CREATE TABLE IF NOT EXISTS evidence_journal (
                evidence_id TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL,
                attempt_id TEXT NOT NULL DEFAULT '',
                lifecycle TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                record_schema TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(evidence_id, journal_sequence)
            );
            CREATE TABLE IF NOT EXISTS evidence_attempt_journal (
                attempt_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL,
                lifecycle TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                record_schema TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(attempt_id, evidence_id, journal_sequence)
            );
            CREATE TABLE IF NOT EXISTS oracle_journal (
                attempt_id TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL,
                decision_id TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                PRIMARY KEY(attempt_id, journal_sequence)
            );
            CREATE TABLE IF NOT EXISTS compilation_journal (
                delivery_attempt_id TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL,
                attempt_id TEXT NOT NULL DEFAULT '',
                model_call_id TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                PRIMARY KEY(delivery_attempt_id, journal_sequence)
            );
            CREATE TABLE IF NOT EXISTS failure_policy_journal (
                attempt_id TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL,
                state_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                PRIMARY KEY(attempt_id, journal_sequence)
            );
            CREATE TRIGGER IF NOT EXISTS canonical_events_no_update
            BEFORE UPDATE ON canonical_events
            BEGIN
                SELECT RAISE(ABORT, 'canonical_events append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS canonical_events_no_delete
            BEFORE DELETE ON canonical_events
            BEGIN
                SELECT RAISE(ABORT, 'canonical_events append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS delivery_journal_no_update
            BEFORE UPDATE ON delivery_journal
            BEGIN
                SELECT RAISE(ABORT, 'delivery_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS delivery_journal_no_delete
            BEFORE DELETE ON delivery_journal
            BEGIN
                SELECT RAISE(ABORT, 'delivery_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS delivery_attempts_no_update
            BEFORE UPDATE ON delivery_attempts
            BEGIN
                SELECT RAISE(ABORT, 'delivery_attempts append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS delivery_attempts_no_delete
            BEFORE DELETE ON delivery_attempts
            BEGIN
                SELECT RAISE(ABORT, 'delivery_attempts append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_journal_no_update
            BEFORE UPDATE ON evidence_journal
            BEGIN
                SELECT RAISE(ABORT, 'evidence_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_journal_no_delete
            BEFORE DELETE ON evidence_journal
            BEGIN
                SELECT RAISE(ABORT, 'evidence_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_attempt_journal_no_update
            BEFORE UPDATE ON evidence_attempt_journal
            BEGIN
                SELECT RAISE(ABORT, 'evidence_attempt_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_attempt_journal_no_delete
            BEFORE DELETE ON evidence_attempt_journal
            BEGIN
                SELECT RAISE(ABORT, 'evidence_attempt_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS oracle_journal_no_update
            BEFORE UPDATE ON oracle_journal
            BEGIN
                SELECT RAISE(ABORT, 'oracle_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS oracle_journal_no_delete
            BEFORE DELETE ON oracle_journal
            BEGIN
                SELECT RAISE(ABORT, 'oracle_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS compilation_journal_no_update
            BEFORE UPDATE ON compilation_journal
            BEGIN
                SELECT RAISE(ABORT, 'compilation_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS compilation_journal_no_delete
            BEFORE DELETE ON compilation_journal
            BEGIN
                SELECT RAISE(ABORT, 'compilation_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS failure_policy_journal_no_update
            BEFORE UPDATE ON failure_policy_journal
            BEGIN
                SELECT RAISE(ABORT, 'failure_policy_journal append-only immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS failure_policy_journal_no_delete
            BEFORE DELETE ON failure_policy_journal
            BEGIN
                SELECT RAISE(ABORT, 'failure_policy_journal append-only immutable');
            END;
            """
        )
        # Local journals created by the pre-reconstruction implementation lack
        # attempt ownership on these identity tables.  Additive migration keeps
        # those append-only records readable without rewriting them.
        for table, column in (
            ("delivery_attempts", "attempt_id"),
            ("evidence_journal", "attempt_id"),
            # C28a: which EVIDENCE_RECORD_SCHEMA wrote each row. Without this an older
            # journal keeps rehydrating absent optional fields to their defaults with no
            # way to tell "the field did not exist yet" from "the field was empty".
            # DEFAULT '' is precisely the legacy marker: empty means pre-schema.
            ("evidence_journal", "record_schema"),
            ("evidence_attempt_journal", "record_schema"),
        ):
            columns = {
                str(row[1])
                for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        # The original evidence table keyed lifecycle only by evidence_id.
        # Retain it as an immutable audit source and backfill the corrected
        # attempt-owned projection without rewriting historical rows.
        self.connection.execute(
            """
            INSERT OR IGNORE INTO evidence_attempt_journal(
                attempt_id, evidence_id, journal_sequence, lifecycle,
                state_hash, canonical_json, record_schema
            )
            SELECT attempt_id, evidence_id, journal_sequence, lifecycle,
                   state_hash, canonical_json, record_schema
            FROM evidence_journal
            WHERE attempt_id <> ''
            """
        )
        self.connection.commit()

    def append(self, event: CanonicalEvent) -> None:
        self.events(event.attempt_id)
        super().append(event)

    def append_batch(self, events: Sequence[CanonicalEvent]) -> None:
        for attempt_id in sorted({event.attempt_id for event in events}):
            self.events(attempt_id)
        super().append_batch(events)

    def delivery_history(
        self,
        delivery_attempt_id: str,
    ) -> tuple["DeliveryAttempt", ...]:
        rows = self.connection.execute(
            """
            SELECT journal_sequence, model_call_id, state, capsule_hash,
                   provider_payload_hash, response_hash, state_hash, canonical_json
            FROM delivery_journal
            WHERE delivery_attempt_id = ?
            ORDER BY journal_sequence ASC
            """,
            (delivery_attempt_id,),
        ).fetchall()
        history: list[DeliveryAttempt] = []
        expected_sequence = 1
        for row in rows:
            sequence = int(row[0])
            payload = str(row[7])
            state_hash = str(row[6])
            if sequence != expected_sequence or _sha256(payload) != state_hash:
                raise StateIntegrityError("delivery journal sequence/hash integrity failure")
            attempt = _delivery_attempt_from_json(payload)
            if (
                attempt.model_call_id != str(row[1])
                or attempt.state.value != str(row[2])
                or attempt.capsule_hash != str(row[3])
                or attempt.provider_payload_hash != str(row[4])
                or attempt.response_hash != str(row[5])
            ):
                raise StateIntegrityError(
                    "delivery journal persisted columns/canonical state mismatch"
                )
            history.append(attempt)
            expected_sequence += 1
        return tuple(history)

    def delivery_attempt_ids_for_attempt(
        self,
        attempt_id: str,
    ) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT delivery_attempt_id
            FROM delivery_attempts
            WHERE attempt_id = ?
            ORDER BY delivery_attempt_id
            """,
            (attempt_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def append_delivery(
        self,
        delivery_attempt_id: str,
        attempt: "DeliveryAttempt",
        *,
        attempt_id: str = "",
    ) -> None:
        if not delivery_attempt_id:
            raise ValueError("delivery_attempt_id is required")
        history = self.delivery_history(delivery_attempt_id)
        identity = self.connection.execute(
            """
            SELECT delivery_attempt_id, model_call_id, attempt_id
            FROM delivery_attempts
            WHERE delivery_attempt_id = ? OR model_call_id = ?
            """,
            (delivery_attempt_id, attempt.model_call_id),
        ).fetchall()
        for stored_id, stored_call, stored_attempt in identity:
            if str(stored_id) != delivery_attempt_id:
                raise StateIntegrityError(
                    "model_call identity already belongs to another delivery attempt"
                )
            if str(stored_call) != attempt.model_call_id:
                raise StateIntegrityError("delivery attempt model_call identity is immutable")
            if attempt_id and str(stored_attempt) and str(stored_attempt) != attempt_id:
                raise StateIntegrityError("delivery attempt owner identity is immutable")

        payload = _canonical_json(attempt)
        if history:
            latest = history[-1]
            if latest.model_call_id != attempt.model_call_id:
                raise StateIntegrityError("delivery attempt model_call identity is immutable")
            if latest.capsule_hash != attempt.capsule_hash:
                raise StateIntegrityError("delivery attempt capsule identity is immutable")
            if latest == attempt:
                return
            terminal_states = {
                # A withheld capsule is FINISHED -- nothing follows it. It belongs here (the
                # "already terminal" check) and in NONE of the other predicate sets: it was
                # never joined to a payload, no provider call carried it, it has no provider
                # terminal kind, and it must never be retried (a retry would deliver the
                # evidence the coin said to withhold, silently un-randomising the arm).
                DeliveryState.WITHHELD_FOR_MEASUREMENT,
                DeliveryState.JOIN_FAILED,
                DeliveryState.DELIVERED,
                DeliveryState.DISPATCH_FAILED,
                DeliveryState.PROVIDER_REJECTED,
                DeliveryState.INFERENCE_FAILED,
                DeliveryState.CANCELLED,
                DeliveryState.PARTIAL_OUTPUT,
                DeliveryState.RESPONSE_DISCARDED,
            }
            if (
                latest.state in terminal_states
                and attempt.state in terminal_states
                and not (
                    latest.state is DeliveryState.DELIVERED
                    and attempt.state is DeliveryState.RESPONSE_DISCARDED
                )
            ):
                raise StateIntegrityError(
                    "contradictory provider terminal outcome for delivery attempt"
                )
            allowed = _INITIAL_DELIVERY_TRANSITIONS
            if attempt.state not in allowed.get(latest.state, set()):
                raise StateIntegrityError(
                    f"delivery journal lifecycle integrity failure: "
                    f"{latest.state.value}->{attempt.state.value}"
                )
        elif attempt.state is not DeliveryState.SELECTED:
            raise StateIntegrityError("delivery journal initial lifecycle must be SELECTED")

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT OR IGNORE INTO delivery_attempts(
                    delivery_attempt_id, model_call_id, attempt_id
                ) VALUES (?, ?, ?)
                """,
                (delivery_attempt_id, attempt.model_call_id, attempt_id),
            )
            sequence = len(history) + 1
            self.connection.execute(
                """
                INSERT INTO delivery_journal(
                    delivery_attempt_id, journal_sequence, model_call_id, state,
                    capsule_hash, provider_payload_hash, response_hash,
                    state_hash, canonical_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_attempt_id,
                    sequence,
                    attempt.model_call_id,
                    attempt.state.value,
                    attempt.capsule_hash,
                    attempt.provider_payload_hash,
                    attempt.response_hash,
                    _sha256(payload),
                    payload,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateIntegrityError("delivery journal identity/integrity conflict") from exc

    def evidence_history(
        self,
        evidence_id: str,
        *,
        attempt_id: str = "",
    ) -> tuple["EvidenceRecord", ...]:
        if not attempt_id:
            owners = self.connection.execute(
                """
                SELECT DISTINCT attempt_id
                FROM evidence_attempt_journal
                WHERE evidence_id = ?
                ORDER BY attempt_id
                """,
                (evidence_id,),
            ).fetchall()
            if len(owners) > 1:
                raise StateIntegrityError("evidence history is attempt-ambiguous")
            if owners:
                attempt_id = str(owners[0][0])
        rows = self.connection.execute(
            """
            SELECT journal_sequence, lifecycle, state_hash, canonical_json,
                   record_schema
            FROM evidence_attempt_journal
            WHERE attempt_id = ? AND evidence_id = ?
            ORDER BY journal_sequence ASC
            """,
            (attempt_id, evidence_id),
        ).fetchall()
        history: list[EvidenceRecord] = []
        for expected, row in enumerate(rows, start=1):
            payload = str(row[3])
            if int(row[0]) != expected or _sha256(payload) != str(row[2]):
                raise StateIntegrityError("evidence journal sequence/hash integrity failure")
            # C28a, FAIL-CLOSED on an UNKNOWN schema -- parity with `canonical_events`.
            # A row stamped by a build we do not know may carry fields this reader would
            # silently drop, or omit fields this reader would silently default. Empty is
            # NOT an error: it is the legacy marker for rows written before the column
            # existed, and they must stay readable so replay of recorded artifacts keeps
            # working. What empty does NOT mean is "observed nothing" -- that distinction
            # is the entire point of the column.
            row_schema = str(row[4] or "")
            if row_schema and row_schema != EVIDENCE_RECORD_SCHEMA:
                raise StateIntegrityError(
                    f"evidence journal row was written under record schema "
                    f"{row_schema!r}; this build reads "
                    f"{EVIDENCE_RECORD_SCHEMA!r}"
                )
            record = _evidence_record_from_json(payload)
            if record.evidence_id != evidence_id or record.lifecycle.value != str(row[1]):
                raise StateIntegrityError("evidence journal persisted state mismatch")
            history.append(record)
        return tuple(history)

    def append_evidence(
        self,
        evidence: "EvidenceRecord",
        *,
        attempt_id: str = "",
    ) -> None:
        history = self.evidence_history(
            evidence.evidence_id,
            attempt_id=attempt_id,
        )
        if history and history[-1] == evidence:
            return
        if history:
            latest = history[-1]
            owner_enrichment = evidence.lifecycle is latest.lifecycle
            if owner_enrichment:
                merged = _merge_same_evidence_generation(latest, evidence)
                if merged == latest:
                    return
                if merged != evidence:
                    raise StateIntegrityError(
                        "evidence journal byte-owner lineage cannot be removed"
                    )
            elif evidence.lifecycle not in _EVIDENCE_TRANSITIONS[latest.lifecycle]:
                raise StateIntegrityError(
                    "evidence journal lifecycle integrity failure: "
                    f"{latest.lifecycle.value}->{evidence.lifecycle.value}"
                )
            if not owner_enrichment and (
                len(evidence.transition_history) != len(latest.transition_history) + 1
                or evidence.transition_history[:-1] != latest.transition_history
            ):
                raise StateIntegrityError("evidence journal lifecycle transition proof mismatch")
            if not owner_enrichment:
                transition = evidence.transition_history[-1]
                if (
                    transition.from_state is not latest.lifecycle
                    or transition.to_state is not evidence.lifecycle
                    or (
                        latest.lifecycle,
                        evidence.lifecycle,
                    )
                    not in _EVIDENCE_REASON_TRANSITIONS[transition.reason_code]
                ):
                    raise StateIntegrityError("evidence journal lifecycle reason integrity failure")
                expected = replace(
                    latest,
                    lifecycle=evidence.lifecycle,
                    fresh=(
                        False
                        if evidence.lifecycle
                        in {
                            EvidenceLifecycle.INVALIDATED,
                            EvidenceLifecycle.EXPIRED,
                        }
                        else latest.fresh
                    ),
                    superseded=(
                        True
                        if evidence.lifecycle is EvidenceLifecycle.SUPERSEDED
                        else latest.superseded
                    ),
                    transition_history=evidence.transition_history,
                )
                if expected != evidence:
                    raise StateIntegrityError(
                        "evidence journal immutable claim/provenance identity changed"
                    )
        elif evidence.lifecycle not in {
            EvidenceLifecycle.DISCOVERED,
            EvidenceLifecycle.PENDING,
        }:
            raise StateIntegrityError(
                "evidence journal initial lifecycle must be DISCOVERED or PENDING"
            )
        payload = evidence.canonical_json()
        try:
            self.connection.execute(
                """
                INSERT INTO evidence_attempt_journal(
                    attempt_id, evidence_id, journal_sequence, lifecycle,
                    state_hash, canonical_json, record_schema
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    evidence.evidence_id,
                    len(history) + 1,
                    evidence.lifecycle.value,
                    _sha256(payload),
                    payload,
                    EVIDENCE_RECORD_SCHEMA,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateIntegrityError("evidence journal identity/integrity conflict") from exc

    def oracle_history(self, attempt_id: str) -> tuple["OracleDecision", ...]:
        rows = self.connection.execute(
            """
            SELECT journal_sequence, decision_id, state_hash, canonical_json
            FROM oracle_journal
            WHERE attempt_id = ?
            ORDER BY journal_sequence ASC
            """,
            (attempt_id,),
        ).fetchall()
        history: list[OracleDecision] = []
        for expected, row in enumerate(rows, start=1):
            payload = str(row[3])
            if int(row[0]) != expected or _sha256(payload) != str(row[2]):
                raise StateIntegrityError("oracle journal sequence/hash integrity failure")
            decision = _oracle_decision_from_json(payload)
            if decision.decision_id != str(row[1]):
                raise StateIntegrityError("oracle journal decision identity mismatch")
            history.append(decision)
        return tuple(history)

    def append_oracle(
        self,
        attempt_id: str,
        decision: "OracleDecision",
    ) -> None:
        history = self.oracle_history(attempt_id)
        payload = _canonical_json(decision)
        try:
            self.connection.execute(
                """
                INSERT INTO oracle_journal(
                    attempt_id, journal_sequence, decision_id,
                    state_hash, canonical_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    len(history) + 1,
                    decision.decision_id,
                    _sha256(payload),
                    payload,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateIntegrityError("oracle journal identity/integrity conflict") from exc

    def evidence_records_for_attempt(
        self,
        attempt_id: str,
    ) -> tuple["EvidenceRecord", ...]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT evidence_id
            FROM evidence_attempt_journal
            WHERE attempt_id = ?
            ORDER BY evidence_id
            """,
            (attempt_id,),
        ).fetchall()
        return tuple(
            self.evidence_history(
                str(row[0]),
                attempt_id=attempt_id,
            )[-1]
            for row in rows
        )

    def compilation_history(
        self,
        delivery_attempt_id: str,
    ) -> tuple["CapsuleCompilation", ...]:
        rows = self.connection.execute(
            """
            SELECT journal_sequence, model_call_id, state_hash, canonical_json
            FROM compilation_journal
            WHERE delivery_attempt_id = ?
            ORDER BY journal_sequence ASC
            """,
            (delivery_attempt_id,),
        ).fetchall()
        history: list[CapsuleCompilation] = []
        for expected, row in enumerate(rows, start=1):
            payload = str(row[3])
            if int(row[0]) != expected or _sha256(payload) != str(row[2]):
                raise StateIntegrityError("compilation journal sequence/hash integrity failure")
            compilation = _capsule_compilation_from_json(payload)
            if compilation.model_call_id != str(row[1]):
                raise StateIntegrityError("compilation journal model-call identity mismatch")
            history.append(compilation)
        return tuple(history)

    def append_compilation(
        self,
        delivery_attempt_id: str,
        compilation: "CapsuleCompilation",
        *,
        attempt_id: str,
    ) -> None:
        if (
            not delivery_attempt_id
            or not attempt_id
            or compilation.state is not CapsuleCompilationState.COMPILED
            or compilation.delivery_attempt is None
        ):
            raise StateIntegrityError("compilation journal requires an owned COMPILED capsule")
        history = self.compilation_history(delivery_attempt_id)
        if history and history[-1] == compilation:
            return
        if history:
            latest = history[-1]
            if (
                latest.model_call_id != compilation.model_call_id
                or latest.observation_id != compilation.observation_id
                or latest.capsule_hash != compilation.capsule_hash
                or latest.evidence_ids != compilation.evidence_ids
                or latest.decision_id != compilation.decision_id
            ):
                raise StateIntegrityError("compilation journal immutable identity changed")
            assert latest.delivery_attempt is not None
            current_delivery = compilation.delivery_attempt
            prior_delivery = latest.delivery_attempt
            if current_delivery == prior_delivery:
                raise StateIntegrityError("compilation journal changed without delivery transition")
            allowed = _DELIVERY_TRANSITIONS
            if current_delivery.state not in allowed.get(prior_delivery.state, set()):
                raise StateIntegrityError(
                    "compilation journal delivery lifecycle integrity failure"
                )
        elif compilation.delivery_attempt.state is not DeliveryState.COMPILED:
            raise StateIntegrityError("compilation journal initial delivery state must be COMPILED")
        payload = _canonical_json(compilation)
        try:
            self.connection.execute(
                """
                INSERT INTO compilation_journal(
                    delivery_attempt_id, journal_sequence, attempt_id,
                    model_call_id, state_hash, canonical_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_attempt_id,
                    len(history) + 1,
                    attempt_id,
                    compilation.model_call_id,
                    _sha256(payload),
                    payload,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateIntegrityError("compilation journal identity/integrity conflict") from exc

    def compilations_for_attempt(
        self,
        attempt_id: str,
    ) -> tuple[tuple[str, "CapsuleCompilation"], ...]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT delivery_attempt_id
            FROM compilation_journal
            WHERE attempt_id = ?
            ORDER BY delivery_attempt_id
            """,
            (attempt_id,),
        ).fetchall()
        return tuple(
            (
                str(row[0]),
                self.compilation_history(str(row[0]))[-1],
            )
            for row in rows
        )

    def append_compilation_transition(
        self,
        delivery_attempt_id: str,
        compilation: "CapsuleCompilation",
        *,
        attempt_id: str,
        evidence_updates: Sequence["EvidenceRecord"] = (),
    ) -> None:
        """Atomically commit one compilation/delivery/evidence transition.

        The compilation embeds the exact delivery state.  Persisting those
        projections in separate transactions can leave an orphan delivery
        head or an evidence lifecycle that disagrees with provider truth after
        a crash.  This high-level operation validates the complete transition
        first, then appends every affected journal row under one SQLite write
        transaction.
        """

        if (
            not delivery_attempt_id
            or not attempt_id
            or compilation.state is not CapsuleCompilationState.COMPILED
            or compilation.delivery_attempt is None
        ):
            raise StateIntegrityError(
                "atomic compilation transition requires an owned COMPILED capsule"
            )
        delivery = compilation.delivery_attempt
        delivery_history = self.delivery_history(delivery_attempt_id)
        compilation_history = self.compilation_history(delivery_attempt_id)
        if bool(delivery_history) != bool(compilation_history):
            raise StateIntegrityError(
                "atomic transition found orphan delivery or compilation history"
            )
        if compilation_history and compilation_history[-1].delivery_attempt != delivery_history[-1]:
            raise StateIntegrityError(
                "atomic transition found divergent delivery/compilation heads"
            )

        identity = self.connection.execute(
            """
            SELECT delivery_attempt_id, model_call_id, attempt_id
            FROM delivery_attempts
            WHERE delivery_attempt_id = ? OR model_call_id = ?
            """,
            (delivery_attempt_id, delivery.model_call_id),
        ).fetchall()
        for stored_id, stored_call, stored_attempt in identity:
            if (
                str(stored_id) != delivery_attempt_id
                or str(stored_call) != delivery.model_call_id
                or (str(stored_attempt) and str(stored_attempt) != attempt_id)
            ):
                raise StateIntegrityError("atomic transition delivery identity conflict")

        delivery_rows: list[DeliveryAttempt]
        if not delivery_history:
            if delivery.state is not DeliveryState.COMPILED:
                raise StateIntegrityError("atomic initial delivery state must be COMPILED")
            selected = replace(
                delivery,
                state=DeliveryState.SELECTED,
                observation_id="",
            )
            delivery_rows = [selected, delivery]
        else:
            latest_delivery = delivery_history[-1]
            latest_compilation = compilation_history[-1]
            if (
                latest_compilation.model_call_id != compilation.model_call_id
                or latest_compilation.observation_id != compilation.observation_id
                or latest_compilation.capsule_hash != compilation.capsule_hash
                or latest_compilation.evidence_ids != compilation.evidence_ids
                or latest_compilation.decision_id != compilation.decision_id
            ):
                raise StateIntegrityError("atomic compilation immutable identity changed")
            if (
                latest_delivery.model_call_id != delivery.model_call_id
                or latest_delivery.capsule_hash != delivery.capsule_hash
            ):
                raise StateIntegrityError("atomic delivery immutable identity changed")
            if latest_delivery == delivery and latest_compilation == compilation:
                for item in evidence_updates:
                    history = self.evidence_history(
                        item.evidence_id,
                        attempt_id=attempt_id,
                    )
                    if not history or history[-1] != item:
                        raise StateIntegrityError(
                            "atomic evidence changed without compilation transition"
                        )
                return
            allowed = _DELIVERY_TRANSITIONS
            if delivery.state not in allowed.get(latest_delivery.state, set()):
                raise StateIntegrityError(
                    "atomic delivery lifecycle integrity failure: "
                    f"{latest_delivery.state.value}->{delivery.state.value}"
                )
            delivery_rows = [delivery]

        evidence_plans: list[tuple[EvidenceRecord, tuple[EvidenceRecord, ...], str]] = []
        seen_evidence_ids: set[str] = set()
        for evidence in evidence_updates:
            if evidence.evidence_id in seen_evidence_ids:
                raise StateIntegrityError("duplicate evidence update in atomic transition")
            seen_evidence_ids.add(evidence.evidence_id)
            if evidence.evidence_id not in delivery.evidence_ids:
                raise StateIntegrityError("atomic evidence update is not bound to the capsule")
            history = self.evidence_history(
                evidence.evidence_id,
                attempt_id=attempt_id,
            )
            if not history:
                raise StateIntegrityError(
                    "atomic transition cannot introduce new evidence identity"
                )
            latest = history[-1]
            if evidence == latest:
                continue
            if evidence.lifecycle not in _EVIDENCE_TRANSITIONS[latest.lifecycle]:
                raise StateIntegrityError(
                    "atomic evidence lifecycle integrity failure: "
                    f"{latest.lifecycle.value}->{evidence.lifecycle.value}"
                )
            if (
                len(evidence.transition_history) != len(latest.transition_history) + 1
                or evidence.transition_history[:-1] != latest.transition_history
            ):
                raise StateIntegrityError("atomic evidence lifecycle transition proof mismatch")
            transition = evidence.transition_history[-1]
            if (
                evidence.lifecycle is EvidenceLifecycle.RELEASED
                and delivery.state is not DeliveryState.COMPILED
            ):
                raise StateIntegrityError(
                    "atomic RELEASED evidence requires COMPILED delivery proof"
                )
            if (
                evidence.lifecycle is EvidenceLifecycle.DELIVERED
                and delivery.state is not DeliveryState.DELIVERED
            ):
                raise StateIntegrityError(
                    "atomic DELIVERED evidence requires provider-terminal proof"
                )
            if (
                transition.from_state is not latest.lifecycle
                or transition.to_state is not evidence.lifecycle
                or (
                    latest.lifecycle,
                    evidence.lifecycle,
                )
                not in _EVIDENCE_REASON_TRANSITIONS[transition.reason_code]
            ):
                raise StateIntegrityError("atomic evidence lifecycle reason integrity failure")
            expected = replace(
                latest,
                lifecycle=evidence.lifecycle,
                fresh=(
                    False
                    if evidence.lifecycle
                    in {
                        EvidenceLifecycle.INVALIDATED,
                        EvidenceLifecycle.EXPIRED,
                    }
                    else latest.fresh
                ),
                superseded=(
                    True
                    if evidence.lifecycle is EvidenceLifecycle.SUPERSEDED
                    else latest.superseded
                ),
                transition_history=evidence.transition_history,
            )
            if expected != evidence:
                raise StateIntegrityError(
                    "atomic evidence immutable claim/provenance identity changed"
                )
            evidence_plans.append((evidence, history, evidence.canonical_json()))

        compilation_payload = _canonical_json(compilation)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT OR IGNORE INTO delivery_attempts(
                    delivery_attempt_id, model_call_id, attempt_id
                ) VALUES (?, ?, ?)
                """,
                (
                    delivery_attempt_id,
                    delivery.model_call_id,
                    attempt_id,
                ),
            )
            delivery_sequence = len(delivery_history)
            for item in delivery_rows:
                delivery_sequence += 1
                payload = _canonical_json(item)
                self.connection.execute(
                    """
                    INSERT INTO delivery_journal(
                        delivery_attempt_id, journal_sequence, model_call_id,
                        state, capsule_hash, provider_payload_hash,
                        response_hash, state_hash, canonical_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delivery_attempt_id,
                        delivery_sequence,
                        item.model_call_id,
                        item.state.value,
                        item.capsule_hash,
                        item.provider_payload_hash,
                        item.response_hash,
                        _sha256(payload),
                        payload,
                    ),
                )
            self.connection.execute(
                """
                INSERT INTO compilation_journal(
                    delivery_attempt_id, journal_sequence, attempt_id,
                    model_call_id, state_hash, canonical_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_attempt_id,
                    len(compilation_history) + 1,
                    attempt_id,
                    compilation.model_call_id,
                    _sha256(compilation_payload),
                    compilation_payload,
                ),
            )
            for evidence, history, payload in evidence_plans:
                self.connection.execute(
                    """
                    INSERT INTO evidence_attempt_journal(
                        attempt_id, evidence_id, journal_sequence, lifecycle,
                        state_hash, canonical_json, record_schema
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        evidence.evidence_id,
                        len(history) + 1,
                        evidence.lifecycle.value,
                        _sha256(payload),
                        payload,
                        EVIDENCE_RECORD_SCHEMA,
                    ),
                )
            self.connection.commit()
        except sqlite3.Error as exc:
            self.connection.rollback()
            raise StateIntegrityError(
                "atomic delivery/compilation/evidence journal conflict"
            ) from exc

    def append_failure_state(self, state: "FailurePolicyState") -> None:
        history = self.failure_history(state.attempt_id)
        if history and history[-1] == state:
            return
        payload = _canonical_json(state)
        try:
            self.connection.execute(
                """
                INSERT INTO failure_policy_journal(
                    attempt_id, journal_sequence, state_hash, canonical_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    state.attempt_id,
                    len(history) + 1,
                    _sha256(payload),
                    payload,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise StateIntegrityError("failure-policy journal identity/integrity conflict") from exc

    def failure_history(
        self,
        attempt_id: str,
    ) -> tuple["FailurePolicyState", ...]:
        rows = self.connection.execute(
            """
            SELECT journal_sequence, state_hash, canonical_json
            FROM failure_policy_journal
            WHERE attempt_id = ?
            ORDER BY journal_sequence ASC
            """,
            (attempt_id,),
        ).fetchall()
        history: list[FailurePolicyState] = []
        for expected, row in enumerate(rows, start=1):
            payload = str(row[2])
            if int(row[0]) != expected or _sha256(payload) != str(row[1]):
                raise StateIntegrityError("failure-policy journal sequence/hash integrity failure")
            state = _failure_policy_state_from_json(payload)
            if state.attempt_id != attempt_id:
                raise StateIntegrityError("failure-policy journal attempt identity mismatch")
            if not state.native_path_enabled:
                raise StateIntegrityError("failure-policy journal disabled the native path")
            if state.health in {
                RuntimeHealthState.HEALTHY,
                RuntimeHealthState.RECOVERED,
            } and (
                state.assurance is not AssuranceStatus.ASSURED
                or not state.gt_emission_enabled
                or not state.gt_interruption_enabled
                or not state.gt_certification_enabled
                or state.quarantine_reason is not None
            ):
                raise StateIntegrityError("assured failure-policy state has inconsistent controls")
            if state.health is RuntimeHealthState.DEGRADED and (
                state.assurance is not AssuranceStatus.DEGRADED
                or state.gt_certification_enabled
                or not state.isolated_components
            ):
                raise StateIntegrityError("degraded failure-policy state has inconsistent controls")
            if state.health is RuntimeHealthState.QUARANTINED and (
                state.assurance is not AssuranceStatus.UNASSURED
                or state.gt_emission_enabled
                or state.gt_interruption_enabled
                or state.gt_certification_enabled
                or state.quarantine_reason is None
            ):
                raise StateIntegrityError(
                    "quarantined failure-policy state has inconsistent controls"
                )
            if history:
                previous = history[-1]
                if previous.health is RuntimeHealthState.QUARANTINED and state != previous:
                    raise StateIntegrityError("quarantined failure-policy state is terminal")
                if not set(previous.recovery_attempted_signatures).issubset(
                    state.recovery_attempted_signatures
                ):
                    raise StateIntegrityError("failure-policy recovery history was rewritten")
                if not set(previous.isolated_components).issubset(state.isolated_components):
                    raise StateIntegrityError("failure-policy component isolation was rewritten")
            elif state.health is not RuntimeHealthState.HEALTHY:
                raise StateIntegrityError("failure-policy journal initial state must be HEALTHY")
            history.append(state)
        return tuple(history)


class DeliveryState(str, Enum):
    SELECTED = "SELECTED"
    COMPILED = "COMPILED"
    JOIN_FAILED = "JOIN_FAILED"
    JOINED = "JOINED"
    DISPATCHED = "DISPATCHED"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    DELIVERED = "DELIVERED"
    RESPONSE_COMMITTED = "RESPONSE_COMMITTED"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    INFERENCE_FAILED = "INFERENCE_FAILED"
    CANCELLED = "CANCELLED"
    PARTIAL_OUTPUT = "PARTIAL_OUTPUT"
    RESPONSE_DISCARDED = "RESPONSE_DISCARDED"
    # A DELIBERATE measurement holdout: the capsule was compiled and then NOT sent, so the
    # shadow arm's coin can be measured. It is its OWN terminal because neither neighbour
    # fits -- CANCELLED means THE PROVIDER cancelled, and RESPONSE_DISCARDED is about the
    # RESPONSE and is reached through record_delivery_failure. Both live in terminal-FAILURE
    # sets, so reusing either would book a measurement decision as a delivery DEFECT.
    WITHHELD_FOR_MEASUREMENT = "WITHHELD_FOR_MEASUREMENT"


# THE delivery state machine, in ONE place (#30 step 1, 2026-07-28). This exact table was
# written out by hand THREE times -- `RuntimeJournal.append_delivery`,
# `.append_compilation` and `.append_compilation_transition`. Three hand-maintained copies of
# a state machine is the D4 hazard made worse: a hash label that drifts is mislabelled, but a
# transition edge that drifts means one validator ACCEPTS what another REJECTS, and the
# journal becomes internally inconsistent.
#
# `frozenset` targets on purpose: a shared mutable dict-of-sets is one `.add()` away from
# widening every validator at once.
_DELIVERY_TRANSITIONS: "dict[DeliveryState, frozenset[DeliveryState]]" = {
    # WITHHELD_FOR_MEASUREMENT is reachable ONLY from COMPILED: the holdout is decided after
    # the capsule exists and BEFORE binding/dispatch. Withholding something already dispatched
    # would be a lie (the bytes went out); withholding something never compiled is vacuous.
    DeliveryState.COMPILED: frozenset(
        {
            DeliveryState.JOINED,
            DeliveryState.JOIN_FAILED,
            DeliveryState.WITHHELD_FOR_MEASUREMENT,
        }
    ),
    DeliveryState.JOINED: frozenset({DeliveryState.DISPATCHED}),
    DeliveryState.DISPATCHED: frozenset(
        {
            DeliveryState.PROVIDER_ACCEPTED,
            DeliveryState.DISPATCH_FAILED,
            DeliveryState.PROVIDER_REJECTED,
        }
    ),
    DeliveryState.PROVIDER_ACCEPTED: frozenset(
        {
            DeliveryState.DELIVERED,
            DeliveryState.INFERENCE_FAILED,
            DeliveryState.CANCELLED,
            DeliveryState.PARTIAL_OUTPUT,
        }
    ),
    DeliveryState.DELIVERED: frozenset(
        {DeliveryState.RESPONSE_COMMITTED, DeliveryState.RESPONSE_DISCARDED}
    ),
}

# `append_delivery` ALSO accepts the initial edge; the compilation validators deliberately do
# NOT. COMPOSED rather than shared-and-widened: flattening all three onto one table would
# silently grant `SELECTED -> COMPILED` to two validators that never permitted it, which is
# precisely the membership change this extraction must not make.
_INITIAL_DELIVERY_TRANSITIONS: "dict[DeliveryState, frozenset[DeliveryState]]" = {
    DeliveryState.SELECTED: frozenset({DeliveryState.COMPILED}),
    **_DELIVERY_TRANSITIONS,
}


class ProviderTerminalKind(str, Enum):
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    TOOL_USE = "TOOL_USE"
    REFUSAL = "REFUSAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PARTIAL_STREAM = "PARTIAL_STREAM"


@dataclass(frozen=True)
class ModelCallAttempt:
    model_call_id: str
    joined_capsule_hash: str
    provider_payload_hash: str
    provider_response_id: str
    terminal_kind: ProviderTerminalKind
    terminal_reason: str = ""

    def __post_init__(self) -> None:
        if not self.model_call_id or not self.provider_response_id:
            raise ValueError("model_call_id and provider_response_id are required")
        _validate_sha256(
            self.joined_capsule_hash,
            field_name="joined_capsule_hash",
        )
        _validate_sha256(
            self.provider_payload_hash,
            field_name="provider_payload_hash",
        )


@dataclass(frozen=True)
class DeliveryAttempt:
    evidence_ids: tuple[str, ...]
    capsule_hash: str
    model_call_id: str
    state: DeliveryState = DeliveryState.SELECTED
    observation_id: str = ""
    joined_capsule_hash: str = ""
    provider_payload_hash: str = ""
    provider_response_id: str = ""
    terminal_kind: ProviderTerminalKind | None = None
    terminal_reason: str = ""
    response_hash: str = ""
    failure_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if not self.evidence_ids or len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("delivery evidence_ids must be non-empty and unique")
        if not self.model_call_id:
            raise ValueError("model_call_id is required")
        _validate_sha256(self.capsule_hash, field_name="capsule_hash")
        progressed = self.state is not DeliveryState.SELECTED
        if progressed and not self.observation_id:
            raise ValueError(f"{self.state.value} requires compilation proof")
        joined_states = {
            DeliveryState.JOINED,
            DeliveryState.DISPATCHED,
            DeliveryState.PROVIDER_ACCEPTED,
            DeliveryState.DELIVERED,
            DeliveryState.RESPONSE_COMMITTED,
            DeliveryState.INFERENCE_FAILED,
            DeliveryState.CANCELLED,
            DeliveryState.PARTIAL_OUTPUT,
            DeliveryState.DISPATCH_FAILED,
            DeliveryState.PROVIDER_REJECTED,
            DeliveryState.RESPONSE_DISCARDED,
        }
        if self.state in joined_states:
            if self.joined_capsule_hash != self.capsule_hash:
                raise ValueError("joined capsule proof does not match capsule_hash")
            _validate_sha256(
                self.provider_payload_hash,
                field_name="provider_payload_hash",
            )
        provider_states = {
            DeliveryState.PROVIDER_ACCEPTED,
            DeliveryState.DELIVERED,
            DeliveryState.RESPONSE_COMMITTED,
            DeliveryState.INFERENCE_FAILED,
            DeliveryState.CANCELLED,
            DeliveryState.PARTIAL_OUTPUT,
            DeliveryState.RESPONSE_DISCARDED,
        }
        if self.state in provider_states and not self.provider_response_id:
            raise ValueError(f"{self.state.value} requires provider response proof")
        terminal_expected = {
            DeliveryState.DELIVERED: {
                ProviderTerminalKind.COMPLETED,
                ProviderTerminalKind.INCOMPLETE,
                ProviderTerminalKind.TOOL_USE,
                ProviderTerminalKind.REFUSAL,
            },
            DeliveryState.RESPONSE_COMMITTED: {
                ProviderTerminalKind.COMPLETED,
                ProviderTerminalKind.INCOMPLETE,
                ProviderTerminalKind.TOOL_USE,
                ProviderTerminalKind.REFUSAL,
            },
            DeliveryState.INFERENCE_FAILED: {ProviderTerminalKind.FAILED},
            DeliveryState.CANCELLED: {ProviderTerminalKind.CANCELLED},
            DeliveryState.PARTIAL_OUTPUT: {ProviderTerminalKind.PARTIAL_STREAM},
            DeliveryState.RESPONSE_DISCARDED: {
                ProviderTerminalKind.COMPLETED,
                ProviderTerminalKind.INCOMPLETE,
                ProviderTerminalKind.TOOL_USE,
                ProviderTerminalKind.REFUSAL,
            },
        }
        if (
            self.state in terminal_expected
            and self.terminal_kind not in terminal_expected[self.state]
        ):
            raise ValueError(f"{self.state.value} requires matching terminal proof")
        if self.state is DeliveryState.RESPONSE_COMMITTED:
            _validate_sha256(self.response_hash, field_name="response_hash")
        if (
            self.state
            in {
                DeliveryState.JOIN_FAILED,
                DeliveryState.DISPATCH_FAILED,
                DeliveryState.PROVIDER_REJECTED,
                DeliveryState.RESPONSE_DISCARDED,
            }
            and not self.failure_reason
        ):
            raise ValueError(f"{self.state.value} requires a failure reason")


_NEXT_DELIVERY_STATE = {
    DeliveryState.SELECTED: DeliveryState.COMPILED,
    DeliveryState.COMPILED: DeliveryState.JOINED,
    DeliveryState.JOINED: DeliveryState.DISPATCHED,
    DeliveryState.DISPATCHED: DeliveryState.PROVIDER_ACCEPTED,
}


def advance_delivery(
    attempt: DeliveryAttempt,
    state: DeliveryState,
    **proof: str,
) -> DeliveryAttempt:
    expected = _NEXT_DELIVERY_STATE.get(attempt.state)
    if state is not expected:
        raise ValueError(
            f"invalid delivery transition {attempt.state.value} -> {state.value}; "
            f"expected {expected.value if expected else 'terminal handling'}"
        )
    if state is DeliveryState.COMPILED:
        observation_id = proof.get("observation_id", "")
        if not observation_id:
            raise ValueError("COMPILED requires an observation_id")
        return replace(attempt, state=state, observation_id=observation_id)
    if state is DeliveryState.JOINED:
        joined = proof.get("joined_capsule_hash", "")
        payload = proof.get("provider_payload_hash", "")
        if joined != attempt.capsule_hash:
            raise ValueError("joined capsule hash does not match selected capsule")
        _validate_sha256(joined, field_name="joined_capsule_hash")
        _validate_sha256(payload, field_name="provider_payload_hash")
        return replace(
            attempt,
            state=state,
            joined_capsule_hash=joined,
            provider_payload_hash=payload,
        )
    if state is DeliveryState.DISPATCHED:
        return replace(attempt, state=state)
    response_id = proof.get("provider_response_id", "")
    if not response_id:
        raise ValueError("PROVIDER_ACCEPTED requires a provider_response_id")
    return replace(
        attempt,
        state=state,
        provider_response_id=response_id,
    )


def record_provider_terminal(
    attempt: DeliveryAttempt,
    model_call: ModelCallAttempt,
) -> DeliveryAttempt:
    if attempt.state is not DeliveryState.PROVIDER_ACCEPTED:
        raise ValueError("provider terminal proof requires PROVIDER_ACCEPTED state")
    if model_call.model_call_id != attempt.model_call_id:
        raise ValueError("model_call identity does not match delivery attempt")
    if model_call.joined_capsule_hash != attempt.joined_capsule_hash:
        raise ValueError("capsule identity does not match the exact joined capsule")
    if model_call.provider_payload_hash != attempt.provider_payload_hash:
        raise ValueError("payload identity does not match the outbound provider payload")
    if not model_call.provider_response_id:
        raise ValueError("provider_response identity is required")
    if model_call.provider_response_id != attempt.provider_response_id:
        raise ValueError("provider_response identity does not match accepted response")

    terminal_state = {
        ProviderTerminalKind.COMPLETED: DeliveryState.DELIVERED,
        ProviderTerminalKind.INCOMPLETE: DeliveryState.DELIVERED,
        ProviderTerminalKind.TOOL_USE: DeliveryState.DELIVERED,
        ProviderTerminalKind.REFUSAL: DeliveryState.DELIVERED,
        ProviderTerminalKind.FAILED: DeliveryState.INFERENCE_FAILED,
        ProviderTerminalKind.CANCELLED: DeliveryState.CANCELLED,
        ProviderTerminalKind.PARTIAL_STREAM: DeliveryState.PARTIAL_OUTPUT,
    }[model_call.terminal_kind]
    return replace(
        attempt,
        state=terminal_state,
        terminal_kind=model_call.terminal_kind,
        terminal_reason=model_call.terminal_reason,
    )


def record_delivery_withheld(
    attempt: DeliveryAttempt,
    *,
    reason: str,
) -> DeliveryAttempt:
    """Record a DELIBERATE measurement holdout: compiled, then not sent.

    Separate from `record_delivery_failure` ON PURPOSE. A holdout is terminal but it is NOT a
    failure, and that recorder's allow-table is failure-only. The moment a holdout can travel
    the failure path it starts appearing in failure accounting and the release gate, and a
    measurement arm becomes indistinguishable from a defect.

    Validated against the SHARED `_DELIVERY_TRANSITIONS` rather than a second hand-written edge
    list, so the one-source property from #30 step 1 keeps holding: only a COMPILED capsule can
    be withheld. Once the bytes went out, calling it withheld would be a lie.
    """
    withheld_reason = reason.strip()
    if not withheld_reason:
        raise ValueError("delivery holdout requires a reason")
    if DeliveryState.WITHHELD_FOR_MEASUREMENT not in _DELIVERY_TRANSITIONS.get(
        attempt.state, frozenset()
    ):
        raise ValueError(
            f"invalid delivery holdout {attempt.state.value}->"
            f"{DeliveryState.WITHHELD_FOR_MEASUREMENT.value}"
        )
    return replace(
        attempt,
        state=DeliveryState.WITHHELD_FOR_MEASUREMENT,
        failure_reason=withheld_reason,
    )


def record_delivery_failure(
    attempt: DeliveryAttempt,
    state: DeliveryState,
    *,
    reason: str,
) -> DeliveryAttempt:
    """Record an immutable non-terminal-boundary failure without overdelivery."""

    failure_reason = reason.strip()
    if not failure_reason:
        raise ValueError("delivery failure requires a reason")
    allowed = {
        DeliveryState.COMPILED: {
            DeliveryState.JOIN_FAILED,
        },
        DeliveryState.DISPATCHED: {
            DeliveryState.DISPATCH_FAILED,
            DeliveryState.PROVIDER_REJECTED,
        },
        DeliveryState.DELIVERED: {
            DeliveryState.RESPONSE_DISCARDED,
        },
    }
    if state not in allowed.get(attempt.state, set()):
        raise ValueError(f"invalid delivery failure {attempt.state.value}->{state.value}")
    return replace(
        attempt,
        state=state,
        failure_reason=failure_reason,
    )


def commit_response(
    attempt: DeliveryAttempt,
    *,
    response_hash: str,
) -> DeliveryAttempt:
    if attempt.state is not DeliveryState.DELIVERED:
        raise ValueError("response cannot be committed before DELIVERED")
    _validate_sha256(response_hash, field_name="response_hash")
    return replace(
        attempt,
        state=DeliveryState.RESPONSE_COMMITTED,
        response_hash=response_hash,
    )


def is_delivered(attempt: DeliveryAttempt) -> bool:
    return attempt.state in {
        DeliveryState.DELIVERED,
        DeliveryState.RESPONSE_COMMITTED,
    }


class AssuranceStatus(str, Enum):
    ASSURED = "ASSURED"
    DEGRADED = "DEGRADED"
    UNASSURED = "UNASSURED"
    BLOCKED = "BLOCKED"


class RuntimeHealthState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERED = "RECOVERED"
    QUARANTINED = "QUARANTINED"


class FaultCode(str, Enum):
    EVIDENCE_PRODUCER_FAILED = "EVIDENCE_PRODUCER_FAILED"
    SUBSTRATE_FAILED = "SUBSTRATE_FAILED"
    SCHEDULER_FAILED = "SCHEDULER_FAILED"
    COALITION_COMPOSITION_FAILED = "COALITION_COMPOSITION_FAILED"
    RENDERING_FAILED = "RENDERING_FAILED"
    OBSERVATION_JOIN_FAILED = "OBSERVATION_JOIN_FAILED"
    DELIVERY_WITNESS_FAILED = "DELIVERY_WITNESS_FAILED"
    CAUSAL_EVENT_GAP = "CAUSAL_EVENT_GAP"
    DUPLICATE_TERMINAL_OUTCOME = "DUPLICATE_TERMINAL_OUTCOME"
    SNAPSHOT_HASH_MISMATCH = "SNAPSHOT_HASH_MISMATCH"
    NONDETERMINISTIC_REPLAY = "NONDETERMINISTIC_REPLAY"
    REDUCER_INVARIANT_VIOLATION = "REDUCER_INVARIANT_VIOLATION"
    IMPOSSIBLE_LIFECYCLE_TRANSITION = "IMPOSSIBLE_LIFECYCLE_TRANSITION"
    REPOSITORY_REVISION_INCONSISTENCY = "REPOSITORY_REVISION_INCONSISTENCY"
    STATE_HASH_MISMATCH = "STATE_HASH_MISMATCH"
    UNKNOWN_PARTIAL_COMMIT = "UNKNOWN_PARTIAL_COMMIT"


CORE_CORRUPTION_CODES = frozenset(
    {
        FaultCode.CAUSAL_EVENT_GAP,
        FaultCode.DUPLICATE_TERMINAL_OUTCOME,
        FaultCode.SNAPSHOT_HASH_MISMATCH,
        FaultCode.NONDETERMINISTIC_REPLAY,
        FaultCode.REDUCER_INVARIANT_VIOLATION,
        FaultCode.IMPOSSIBLE_LIFECYCLE_TRANSITION,
        FaultCode.REPOSITORY_REVISION_INCONSISTENCY,
        FaultCode.STATE_HASH_MISMATCH,
        FaultCode.UNKNOWN_PARTIAL_COMMIT,
    }
)


@dataclass(frozen=True)
class RuntimeFault:
    code: FaultCode
    component: str
    signature: str
    event_id: str = ""


@dataclass(frozen=True)
class RecoveryInput:
    snapshot_id: str
    snapshot_state_hash: str
    committed_event_ids: tuple[str, ...]
    committed_tail_hash: str


@dataclass(frozen=True)
class RecoveryProof:
    snapshot_id: str
    snapshot_state_hash: str
    committed_event_ids: tuple[str, ...]
    committed_tail_hash: str
    snapshot_hash_valid: bool
    event_sequence_complete: bool
    deterministic_replay: bool
    state_hash_matches: bool
    reasoning_graph_hash_matches: bool
    evidence_graph_hash_matches: bool
    repository_revision_consistent: bool
    invariants_pass: bool
    recovered_state_hash: str

    def valid_for(self, request: RecoveryInput) -> bool:
        if (
            self.snapshot_id != request.snapshot_id
            or self.snapshot_state_hash != request.snapshot_state_hash
            or self.committed_event_ids != request.committed_event_ids
            or self.committed_tail_hash != request.committed_tail_hash
        ):
            return False
        try:
            _validate_sha256(
                request.snapshot_state_hash,
                field_name="snapshot_state_hash",
            )
            _validate_sha256(
                request.committed_tail_hash,
                field_name="committed_tail_hash",
            )
        except ValueError:
            return False
        checks = (
            self.snapshot_hash_valid,
            self.event_sequence_complete,
            self.deterministic_replay,
            self.state_hash_matches,
            self.reasoning_graph_hash_matches,
            self.evidence_graph_hash_matches,
            self.repository_revision_consistent,
            self.invariants_pass,
        )
        if not all(checks):
            return False
        try:
            _validate_sha256(
                self.recovered_state_hash,
                field_name="recovered_state_hash",
            )
        except ValueError:
            return False
        return True


@dataclass(frozen=True)
class FailurePolicyState:
    attempt_id: str
    health: RuntimeHealthState
    assurance: AssuranceStatus
    isolated_components: tuple[str, ...]
    recovery_attempted_signatures: tuple[str, ...]
    last_verified_snapshot_id: str
    gt_emission_enabled: bool
    gt_interruption_enabled: bool
    gt_certification_enabled: bool
    native_path_enabled: bool
    quarantine_reason: FaultCode | None = None
    failed_event_id: str = ""

    @classmethod
    def initial(cls, *, attempt_id: str) -> "FailurePolicyState":
        return cls(
            attempt_id=attempt_id,
            health=RuntimeHealthState.HEALTHY,
            assurance=AssuranceStatus.ASSURED,
            isolated_components=(),
            recovery_attempted_signatures=(),
            last_verified_snapshot_id="",
            gt_emission_enabled=True,
            gt_interruption_enabled=True,
            gt_certification_enabled=True,
            native_path_enabled=True,
        )


def _quarantine(
    state: FailurePolicyState,
    fault: RuntimeFault,
    *,
    attempted_signatures: tuple[str, ...],
) -> FailurePolicyState:
    return replace(
        state,
        health=RuntimeHealthState.QUARANTINED,
        assurance=AssuranceStatus.UNASSURED,
        recovery_attempted_signatures=attempted_signatures,
        gt_emission_enabled=False,
        gt_interruption_enabled=False,
        gt_certification_enabled=False,
        native_path_enabled=True,
        quarantine_reason=fault.code,
        failed_event_id=fault.event_id,
    )


def apply_failure_policy(
    state: FailurePolicyState,
    fault: RuntimeFault,
    *,
    recovery_input: RecoveryInput,
    recover: Any,
) -> FailurePolicyState:
    """Apply the centralized component-isolation/core-quarantine policy."""

    if state.health is RuntimeHealthState.QUARANTINED:
        return state

    if fault.code not in CORE_CORRUPTION_CODES:
        isolated = _append_unique(state.isolated_components, fault.component)
        return replace(
            state,
            health=RuntimeHealthState.DEGRADED,
            assurance=AssuranceStatus.DEGRADED,
            isolated_components=isolated,
            gt_certification_enabled=False,
            native_path_enabled=True,
        )

    if fault.signature in state.recovery_attempted_signatures:
        return _quarantine(
            state,
            fault,
            attempted_signatures=state.recovery_attempted_signatures,
        )

    attempted = state.recovery_attempted_signatures + (fault.signature,)
    try:
        proof = recover(recovery_input)
    except Exception:
        return _quarantine(state, fault, attempted_signatures=attempted)
    if not isinstance(proof, RecoveryProof) or not proof.valid_for(recovery_input):
        return _quarantine(state, fault, attempted_signatures=attempted)

    retains_degradation = bool(state.isolated_components)
    return replace(
        state,
        health=(
            RuntimeHealthState.DEGRADED if retains_degradation else RuntimeHealthState.RECOVERED
        ),
        assurance=(AssuranceStatus.DEGRADED if retains_degradation else AssuranceStatus.ASSURED),
        recovery_attempted_signatures=attempted,
        last_verified_snapshot_id=recovery_input.snapshot_id,
        gt_emission_enabled=True,
        gt_interruption_enabled=True,
        gt_certification_enabled=not retains_degradation,
        native_path_enabled=True,
        quarantine_reason=None,
        failed_event_id="",
    )


class DecisionContext(str, Enum):
    SOURCE_TARGET_SELECTION = "SOURCE_TARGET_SELECTION"
    SOURCE_UNDERSTANDING = "SOURCE_UNDERSTANDING"
    PATCH_CONSTRUCTION = "PATCH_CONSTRUCTION"
    PATCH_PROPAGATION = "PATCH_PROPAGATION"
    FAILURE_RECOVERY = "FAILURE_RECOVERY"
    COMPLETION = "COMPLETION"


class EvidenceRole(str, Enum):
    TARGET_IDENTITY = "TARGET_IDENTITY"
    EXECUTION_REACHABILITY = "EXECUTION_REACHABILITY"
    BEHAVIORAL_CONTRACT = "BEHAVIORAL_CONTRACT"
    AFFECTED_CALLER = "AFFECTED_CALLER"
    STATE_DEPENDENCY = "STATE_DEPENDENCY"
    CONTRADICTION = "CONTRADICTION"
    VALIDATION = "VALIDATION"
    MATERIAL_UNCERTAINTY = "MATERIAL_UNCERTAINTY"
    BLOCKER = "BLOCKER"
    TERMINAL_ASSURANCE = "TERMINAL_ASSURANCE"
    HISTORICAL_SUPPORT = "HISTORICAL_SUPPORT"


class EvidenceGrade(IntEnum):
    INFO = 0
    HYPOTHESIS = 1
    WARNING = 2
    VERIFIED = 3


class EvidenceLifecycle(str, Enum):
    DISCOVERED = "DISCOVERED"
    PENDING = "PENDING"
    READY = "READY"
    HELD = "HELD"
    RELEASED = "RELEASED"
    DELIVERED = "DELIVERED"
    ACTIVE = "ACTIVE"
    SATISFIED = "SATISFIED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class MandatoryReason(str, Enum):
    BLOCKER = "BLOCKER"
    VERIFIED_CONTRADICTION = "VERIFIED_CONTRADICTION"
    MATERIAL_UNCERTAINTY = "MATERIAL_UNCERTAINTY"
    TASK_OBLIGATION = "TASK_OBLIGATION"


class SuppressionReason(str, Enum):
    OTHER_DECISION = "OTHER_DECISION"
    DISCONNECTED = "DISCONNECTED"
    NOT_READY = "NOT_READY"
    STALE = "STALE"
    ALREADY_VISIBLE = "ALREADY_VISIBLE"
    ALREADY_ACQUIRED = "ALREADY_ACQUIRED"
    SUPERSEDED = "SUPERSEDED"
    REDUNDANT_ROLE = "REDUNDANT_ROLE"
    BUDGET = "BUDGET"
    NON_POSITIVE_VALUE = "NON_POSITIVE_VALUE"
    INCOMPLETE_DECISION = "INCOMPLETE_DECISION"
    NOT_ACTIONABLE_FOR_DECISION = "NOT_ACTIONABLE_FOR_DECISION"
    DUPLICATE_CLAIM = "DUPLICATE_CLAIM"


@dataclass(frozen=True)
class ActiveDecision:
    decision_id: str
    context: DecisionContext
    primary_claim: str
    required_roles: tuple[EvidenceRole, ...]
    causal_neighborhood: tuple[str, ...]
    token_budget: int
    current_revision: RevisionVector
    useful_roles: tuple[EvidenceRole, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_roles", tuple(self.required_roles))
        object.__setattr__(self, "causal_neighborhood", tuple(self.causal_neighborhood))
        object.__setattr__(self, "useful_roles", tuple(self.useful_roles))
        if not self.decision_id or not self.primary_claim:
            raise ValueError("decision_id and primary_claim are required")
        if not self.required_roles or len(set(self.required_roles)) != len(self.required_roles):
            raise ValueError("required_roles must be non-empty and unique")
        if len(set(self.useful_roles)) != len(self.useful_roles):
            raise ValueError("useful_roles must be unique")
        if not self.causal_neighborhood:
            raise ValueError("causal_neighborhood is required")
        if type(self.token_budget) is not int or self.token_budget < 1:
            raise ValueError("token_budget must be a positive integer")


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    feature_id: str
    decision_context: DecisionContext
    roles: tuple[EvidenceRole, ...]
    subject: str
    claim: str
    actionable_consequence: str
    provenance: tuple[str, ...]
    grade: EvidenceGrade
    revision: RevisionVector
    causal_neighborhood: tuple[str, ...]
    lifecycle: EvidenceLifecycle
    fresh: bool
    already_visible: bool
    superseded: bool
    mandatory_reason: MandatoryReason | None
    token_cost: int
    failure_prevention: int
    causal_value: int
    contradiction_resolution: int
    anchoring_risk: int
    revision_dependencies: tuple[str, ...] = ()
    transition_history: tuple["EvidenceTransition", ...] = ()
    authority: Authority = Authority.RESULT_DERIVED
    visible_to_decision_ids: tuple[str, ...] = ()
    # CAP byte owners are audit lineage on the canonical FACT computation. They
    # never become separate evidence objects and are never model-facing.
    owner_feature_ids: tuple[str, ...] = ()
    # Runtime substrates observed by the producer computation. This is not a
    # declaration of what the feature would prefer; the temporal gate consumes
    # only this producer-owned execution evidence.
    observed_substrates: tuple[str, ...] = ()
    # Operational lineage for a standing task obligation. The root producer
    # record has an empty source id; a rematerialized decision-window generation
    # names that immutable root. This never changes model-facing evidence.
    standing_source_evidence_id: str = ""
    decision_window_generation: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "roles",
            "provenance",
            "causal_neighborhood",
            "revision_dependencies",
            "transition_history",
            "visible_to_decision_ids",
            "owner_feature_ids",
            "observed_substrates",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if not self.evidence_id or not self.feature_id:
            raise ValueError("evidence_id and feature_id are required")
        if self.owner_feature_ids != tuple(sorted(set(self.owner_feature_ids))) or any(
            not item.startswith("GT_") for item in self.owner_feature_ids
        ):
            raise ValueError("owner_feature_ids must be sorted unique GT_* audit identities")
        if self.observed_substrates != tuple(sorted(set(self.observed_substrates))) or any(
            not item.strip() for item in self.observed_substrates
        ):
            raise ValueError("observed_substrates must be sorted unique non-empty identities")
        if (
            self.standing_source_evidence_id == self.evidence_id
            or (self.standing_source_evidence_id and not self.decision_window_generation)
            or (
                self.decision_window_generation
                and not self.decision_window_generation.startswith("GT-W-")
            )
            or (
                (self.standing_source_evidence_id or self.decision_window_generation)
                and (
                    self.feature_id != "obligations"
                    or self.mandatory_reason is not MandatoryReason.TASK_OBLIGATION
                )
            )
        ):
            raise ValueError(
                "standing evidence lineage requires a task obligation, a "
                "GT-W generation, and a distinct source for rematerialized rows"
            )
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise ValueError("evidence roles must be non-empty and unique")
        if not self.subject or not self.claim or not self.actionable_consequence:
            raise ValueError("evidence subject, claim and actionable consequence are required")
        if not self.provenance:
            raise ValueError("evidence provenance is required")
        if not self.causal_neighborhood:
            raise ValueError("evidence causal_neighborhood is required")
        if not self.revision_dependencies:
            raise ValueError("evidence revision_dependencies are required")
        if type(self.token_cost) is not int or self.token_cost < 1:
            raise ValueError("evidence token_cost must be a positive integer")
        for field_name in (
            "failure_prevention",
            "causal_value",
            "contradiction_resolution",
            "anchoring_risk",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.mandatory_reason is MandatoryReason.VERIFIED_CONTRADICTION and (
            self.grade is not EvidenceGrade.VERIFIED or self.authority < Authority.RESULT_DERIVED
        ):
            raise ValueError(
                "verified contradiction requires VERIFIED grade and "
                "result-derived semantic authority"
            )
        contract_registry = globals().get("FEATURE_CONTRACTS")
        if contract_registry is not None:
            contract = contract_registry.get(self.feature_id)
            if contract is not None:
                if self.decision_context is not contract.decision_context:
                    raise ValueError("evidence decision_context violates its feature contract")
                mandatory_overlay = {
                    MandatoryReason.BLOCKER: {EvidenceRole.BLOCKER},
                    MandatoryReason.VERIFIED_CONTRADICTION: {EvidenceRole.CONTRADICTION},
                    MandatoryReason.MATERIAL_UNCERTAINTY: {EvidenceRole.MATERIAL_UNCERTAINTY},
                    MandatoryReason.TASK_OBLIGATION: {EvidenceRole.BEHAVIORAL_CONTRACT},
                    None: set(),
                }[self.mandatory_reason]
                allowed_role_sets = (
                    {frozenset(contract.roles)}
                    if self.mandatory_reason is None
                    else {
                        frozenset(mandatory_overlay),
                        frozenset(set(contract.roles) | mandatory_overlay),
                    }
                )
                if frozenset(self.roles) not in allowed_role_sets:
                    raise ValueError("evidence roles must exactly match its feature contract")
                if self.revision_dependencies != contract.revision_dependencies:
                    raise ValueError(
                        "evidence revision_dependencies must exactly match its feature contract"
                    )

    def canonical_json(self) -> str:
        return _canonical_json(self)

    @property
    def content_hash(self) -> str:
        return _sha256(self.canonical_json())


class EvidenceLifecycleError(StateIntegrityError):
    """An evidence lifecycle transition violates its canonical contract."""


@dataclass(frozen=True)
class EvidenceTransition:
    from_state: EvidenceLifecycle
    to_state: EvidenceLifecycle
    reason_code: "EvidenceTransitionReason"
    reason_detail: str = ""


@dataclass(frozen=True)
class EvidenceTransitionRequest:
    to_state: EvidenceLifecycle
    reason_code: "EvidenceTransitionReason | str"
    delivery_attempt: DeliveryAttempt | None = None


class EvidenceTransitionReason(str, Enum):
    PRODUCER_DISCOVERED = "PRODUCER_DISCOVERED"
    PREREQUISITES_PENDING = "PREREQUISITES_PENDING"
    READINESS_RULES_SATISFIED = "READINESS_RULES_SATISFIED"
    OTHER_DECISION_CURRENTLY_ACTIVE = "OTHER_DECISION_CURRENTLY_ACTIVE"
    DECISION_WINDOW_OPEN = "DECISION_WINDOW_OPEN"
    PROVIDER_TERMINAL_DELIVERY_PROVEN = "PROVIDER_TERMINAL_DELIVERY_PROVEN"
    ACTIVATED_AFTER_PROVIDER_DELIVERY = "ACTIVATED_AFTER_PROVIDER_DELIVERY"
    DECISION_SATISFIED = "DECISION_SATISFIED"
    STRONGER_EVIDENCE_SUPERSEDED = "STRONGER_EVIDENCE_SUPERSEDED"
    DECISION_WINDOW_EXPIRED = "DECISION_WINDOW_EXPIRED"
    REVISION_DEPENDENCY_CHANGED = "REVISION_DEPENDENCY_CHANGED"


_LEGACY_EVIDENCE_REASON_ALIASES: Mapping[str, EvidenceTransitionReason] = {
    "READY": EvidenceTransitionReason.READINESS_RULES_SATISFIED,
    "HELD": EvidenceTransitionReason.OTHER_DECISION_CURRENTLY_ACTIVE,
    "RELEASE": EvidenceTransitionReason.DECISION_WINDOW_OPEN,
    "RELEASED": EvidenceTransitionReason.DECISION_WINDOW_OPEN,
    "MODEL_EXPOSURE_PROVEN": (EvidenceTransitionReason.PROVIDER_TERMINAL_DELIVERY_PROVEN),
}


_EVIDENCE_REASON_TRANSITIONS: Mapping[
    EvidenceTransitionReason,
    frozenset[tuple[EvidenceLifecycle, EvidenceLifecycle]],
] = {
    EvidenceTransitionReason.PRODUCER_DISCOVERED: frozenset(
        {
            (EvidenceLifecycle.DISCOVERED, EvidenceLifecycle.PENDING),
        }
    ),
    EvidenceTransitionReason.PREREQUISITES_PENDING: frozenset(
        {
            (EvidenceLifecycle.DISCOVERED, EvidenceLifecycle.PENDING),
            (EvidenceLifecycle.READY, EvidenceLifecycle.HELD),
        }
    ),
    EvidenceTransitionReason.READINESS_RULES_SATISFIED: frozenset(
        {
            (EvidenceLifecycle.PENDING, EvidenceLifecycle.READY),
            (EvidenceLifecycle.HELD, EvidenceLifecycle.READY),
        }
    ),
    EvidenceTransitionReason.OTHER_DECISION_CURRENTLY_ACTIVE: frozenset(
        {
            (EvidenceLifecycle.READY, EvidenceLifecycle.HELD),
        }
    ),
    EvidenceTransitionReason.DECISION_WINDOW_OPEN: frozenset(
        {
            (EvidenceLifecycle.READY, EvidenceLifecycle.RELEASED),
            (EvidenceLifecycle.HELD, EvidenceLifecycle.RELEASED),
        }
    ),
    EvidenceTransitionReason.PROVIDER_TERMINAL_DELIVERY_PROVEN: frozenset(
        {
            (EvidenceLifecycle.RELEASED, EvidenceLifecycle.DELIVERED),
        }
    ),
    EvidenceTransitionReason.ACTIVATED_AFTER_PROVIDER_DELIVERY: frozenset(
        {
            (EvidenceLifecycle.DELIVERED, EvidenceLifecycle.ACTIVE),
        }
    ),
    EvidenceTransitionReason.DECISION_SATISFIED: frozenset(
        {
            (EvidenceLifecycle.DELIVERED, EvidenceLifecycle.SATISFIED),
            (EvidenceLifecycle.ACTIVE, EvidenceLifecycle.SATISFIED),
        }
    ),
    EvidenceTransitionReason.STRONGER_EVIDENCE_SUPERSEDED: frozenset(
        {
            (EvidenceLifecycle.DELIVERED, EvidenceLifecycle.SUPERSEDED),
            (EvidenceLifecycle.ACTIVE, EvidenceLifecycle.SUPERSEDED),
        }
    ),
    EvidenceTransitionReason.DECISION_WINDOW_EXPIRED: frozenset(
        {
            (EvidenceLifecycle.READY, EvidenceLifecycle.EXPIRED),
            (EvidenceLifecycle.HELD, EvidenceLifecycle.EXPIRED),
            (EvidenceLifecycle.RELEASED, EvidenceLifecycle.EXPIRED),
            (EvidenceLifecycle.ACTIVE, EvidenceLifecycle.EXPIRED),
        }
    ),
    EvidenceTransitionReason.REVISION_DEPENDENCY_CHANGED: frozenset(
        {
            (state, EvidenceLifecycle.INVALIDATED)
            for state in (
                EvidenceLifecycle.DISCOVERED,
                EvidenceLifecycle.PENDING,
                EvidenceLifecycle.READY,
                EvidenceLifecycle.HELD,
                EvidenceLifecycle.RELEASED,
                EvidenceLifecycle.DELIVERED,
                EvidenceLifecycle.ACTIVE,
            )
        }
    ),
}


_EVIDENCE_TRANSITIONS: Mapping[
    EvidenceLifecycle,
    frozenset[EvidenceLifecycle],
] = {
    EvidenceLifecycle.DISCOVERED: frozenset({EvidenceLifecycle.PENDING}),
    EvidenceLifecycle.PENDING: frozenset(
        {
            EvidenceLifecycle.READY,
            EvidenceLifecycle.INVALIDATED,
            EvidenceLifecycle.EXPIRED,
        }
    ),
    EvidenceLifecycle.READY: frozenset(
        {
            EvidenceLifecycle.HELD,
            EvidenceLifecycle.RELEASED,
            EvidenceLifecycle.INVALIDATED,
            EvidenceLifecycle.EXPIRED,
        }
    ),
    EvidenceLifecycle.HELD: frozenset(
        {
            EvidenceLifecycle.READY,
            EvidenceLifecycle.RELEASED,
            EvidenceLifecycle.INVALIDATED,
            EvidenceLifecycle.EXPIRED,
        }
    ),
    EvidenceLifecycle.RELEASED: frozenset(
        {
            EvidenceLifecycle.DELIVERED,
            EvidenceLifecycle.INVALIDATED,
            EvidenceLifecycle.EXPIRED,
        }
    ),
    EvidenceLifecycle.DELIVERED: frozenset(
        {
            EvidenceLifecycle.ACTIVE,
            EvidenceLifecycle.SATISFIED,
            EvidenceLifecycle.SUPERSEDED,
            EvidenceLifecycle.INVALIDATED,
        }
    ),
    EvidenceLifecycle.ACTIVE: frozenset(
        {
            EvidenceLifecycle.SATISFIED,
            EvidenceLifecycle.SUPERSEDED,
            EvidenceLifecycle.INVALIDATED,
            EvidenceLifecycle.EXPIRED,
        }
    ),
    EvidenceLifecycle.SATISFIED: frozenset(),
    EvidenceLifecycle.SUPERSEDED: frozenset(),
    EvidenceLifecycle.EXPIRED: frozenset(),
    EvidenceLifecycle.INVALIDATED: frozenset(),
}


def transition_evidence(
    evidence: EvidenceRecord,
    to_state: EvidenceLifecycle,
    *,
    reason_code: EvidenceTransitionReason | str,
    delivery_attempt: DeliveryAttempt | None = None,
) -> EvidenceRecord:
    reason_detail = ""
    if isinstance(reason_code, EvidenceTransitionReason):
        reason = reason_code
    elif isinstance(reason_code, str):
        raw_reason = reason_code.strip()
        if raw_reason.startswith("REVISION_DEPENDENCY_CHANGED:"):
            reason = EvidenceTransitionReason.REVISION_DEPENDENCY_CHANGED
            reason_detail = raw_reason.partition(":")[2]
        else:
            try:
                reason = EvidenceTransitionReason(raw_reason)
            except ValueError:
                reason = _LEGACY_EVIDENCE_REASON_ALIASES.get(raw_reason)
                if reason is None:
                    raise EvidenceLifecycleError(
                        "reason_code must be a typed EvidenceTransitionReason"
                    )
    else:
        raise TypeError("reason_code must be an EvidenceTransitionReason")
    if to_state not in _EVIDENCE_TRANSITIONS[evidence.lifecycle]:
        if evidence.lifecycle is EvidenceLifecycle.RELEASED and to_state in {
            EvidenceLifecycle.ACTIVE,
            EvidenceLifecycle.SATISFIED,
        }:
            raise EvidenceLifecycleError(
                "RELEASED evidence requires provider-proven DELIVERED state "
                "before activation or satisfaction"
            )
        raise EvidenceLifecycleError(
            f"illegal evidence transition {evidence.lifecycle.value} -> {to_state.value}"
        )
    if (evidence.lifecycle, to_state) not in _EVIDENCE_REASON_TRANSITIONS[reason]:
        raise EvidenceLifecycleError(
            f"reason {reason.value} is not valid for transition "
            f"{evidence.lifecycle.value} -> {to_state.value}"
        )
    if to_state is EvidenceLifecycle.DELIVERED:
        if delivery_attempt is None or not is_delivered(delivery_attempt):
            raise EvidenceLifecycleError("DELIVERED requires provider-terminal delivery proof")
        if evidence.evidence_id not in delivery_attempt.evidence_ids:
            raise EvidenceLifecycleError("provider delivery proof does not bind this evidence")
    transition = EvidenceTransition(
        from_state=evidence.lifecycle,
        to_state=to_state,
        reason_code=reason,
        reason_detail=reason_detail,
    )
    return replace(
        evidence,
        lifecycle=to_state,
        fresh=(
            False
            if to_state
            in {
                EvidenceLifecycle.INVALIDATED,
                EvidenceLifecycle.EXPIRED,
            }
            else evidence.fresh
        ),
        superseded=(True if to_state is EvidenceLifecycle.SUPERSEDED else evidence.superseded),
        transition_history=evidence.transition_history + (transition,),
    )


def replay_evidence_transitions(
    evidence: EvidenceRecord,
    requests: Iterable[EvidenceTransitionRequest],
) -> EvidenceRecord:
    result = evidence
    for request in requests:
        result = transition_evidence(
            result,
            request.to_state,
            reason_code=request.reason_code,
            delivery_attempt=request.delivery_attempt,
        )
    return result


_REVISION_DEPENDENCY_DIMENSION = {
    "repository_content": "repository_content",
    "graph": "graph",
    "lsp": "lsp",
    "runtime_evidence": "runtime_evidence",
    "nodes": "graph",
    "edges": "graph",
    "edges_rev": "graph",
    "content_rev": "graph",
    "props_rev": "graph",
    "cochange_rev": "graph",
    "closure_rev": "graph",
    "graph_rev": "graph",
    "patch_rev": "runtime_evidence",
    "edit_rev": "runtime_evidence",
    "episode_state": "runtime_evidence",
    "issue": "runtime_evidence",
}

# Dependencies on state that CANNOT change during an attempt.
#
# `issue` is the problem statement handed to the attempt at task start. It is fixed for the
# attempt's lifetime, so evidence derived from it does not become false because a file
# changed -- the requirements the fix must satisfy are the same requirements.
#
# Treating it as mutable was fatal exactly where it mattered most. `obligations` is the only
# standing carrier of BEHAVIORAL_CONTRACT, which is the required role of PATCH_CONSTRUCTION
# -- the phase the agent enters AFTER editing. Mapped onto `runtime_evidence`, the record was
# invalidated by the very edit that opened the decision needing it, so on any task where the
# agent edits (i.e. every real task) the coalition could never complete.
#
# This is an exemption from FRESHNESS, not from any other check: role fit, connectivity,
# supersession, dedup, the token budget and decision-completeness all still apply.
#
# Deliberately an explicit set rather than deleting the mapping: `_evidence_revision_is_fresh`
# returns False for an unmapped dependency (fail-closed), so a deletion would make the record
# permanently stale -- the opposite of the intent.
#
# DO NOT add `patch_rev`, `edit_rev` or `episode_state`. Those are genuinely derived from
# mutable runtime state and MUST retire when the repository moves; exempting them would let
# GT serve edit-derived evidence about a file that has since changed -- stale evidence
# presented as fact, which is worse than silence.
_IMMUTABLE_REVISION_DEPENDENCIES = frozenset({"issue"})


def invalidate_stale_evidence(
    evidence: EvidenceRecord,
    *,
    current_revision: RevisionVector,
) -> EvidenceRecord:
    changed: list[str] = []
    for dependency in evidence.revision_dependencies:
        # THIS loop is what actually retires evidence -- `_evidence_revision_is_fresh` is a
        # separate predicate used elsewhere, so the immutable exemption must be applied in
        # BOTH or the fix silently does nothing on the live path. (It was applied only to
        # the predicate first; an offline reproduction caught that the record was still
        # INVALIDATED while the predicate reported fresh.)
        if dependency in _IMMUTABLE_REVISION_DEPENDENCIES:
            continue
        dimension = _REVISION_DEPENDENCY_DIMENSION.get(dependency)
        if dimension is None:
            raise EvidenceLifecycleError(f"unknown revision dependency: {dependency}")
        if getattr(evidence.revision, dimension) != getattr(current_revision, dimension):
            changed.append(dependency)
    if not changed:
        return evidence
    if evidence.lifecycle in {
        EvidenceLifecycle.SATISFIED,
        EvidenceLifecycle.SUPERSEDED,
        EvidenceLifecycle.EXPIRED,
        EvidenceLifecycle.INVALIDATED,
    }:
        return evidence
    return transition_evidence(
        evidence,
        EvidenceLifecycle.INVALIDATED,
        reason_code="REVISION_DEPENDENCY_CHANGED:" + ",".join(changed),
    )


@dataclass(frozen=True)
class FeatureFallbackPolicy:
    feature_id: str
    preferred_substrates: tuple[str, ...]
    fallback_substrates: tuple[str, ...]
    minimum_grade: EvidenceGrade
    minimum_authority: Authority

    def __post_init__(self) -> None:
        object.__setattr__(self, "preferred_substrates", tuple(self.preferred_substrates))
        object.__setattr__(self, "fallback_substrates", tuple(self.fallback_substrates))
        if (
            not self.feature_id
            or not self.preferred_substrates
            or not self.fallback_substrates
            or set(self.preferred_substrates).intersection(self.fallback_substrates)
        ):
            raise ValueError("invalid feature fallback assurance policy")


class TemporalPredicate(str, Enum):
    PRODUCER_COMPUTATION_COMPLETE = "producer_computation_complete"
    REVISION_DEPENDENCIES_CAPTURED = "declared_revision_dependencies_captured"
    ACTIVE_DECISION_CONTEXT_MATCHES = "active_decision_context_matches"
    ACTIVE_DECISION_ID_MATCHES = "active_decision_id_matches"
    REASONING_GRAPH_CONNECTED = "reasoning_graph_path_connects_subject"
    COMMITMENT_WINDOW_OPEN = "decision_commitment_window_open"
    ACTIVE_DECISION_CLOSED = "active_decision_closed"
    REVISION_DEPENDENCY_CHANGED = "declared_revision_dependency_changed"
    AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT = "authorized_byte_owner_lineage_present"


class CommitmentWindowState(str, Enum):
    NOT_OPEN = "NOT_OPEN"
    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    CLOSED = "CLOSED"


# The temporal order of the decision spine. This is an ordering over DecisionContext,
# which is DEFINED in this module, so it is the natural home for "which decision comes
# after which" rather than a copy of a rule owned elsewhere. FAILURE_RECOVERY is
# deliberately absent: it is an off-spine excursion, not a later point in time.
_DECISION_SPINE: tuple[DecisionContext, ...] = (
    DecisionContext.SOURCE_TARGET_SELECTION,
    DecisionContext.SOURCE_UNDERSTANDING,
    DecisionContext.PATCH_CONSTRUCTION,
    DecisionContext.PATCH_PROPAGATION,
    DecisionContext.COMPLETION,
)

# Which decision is open at each registered fact_registry delivery boundary. This is a
# PROJECTION of the live chain
#   boundary -> SemanticKind -> reduce_event -> work-state phase -> _active_decision
# and it is not allowed to be a convention: it is asserted equal to that live chain, for
# every boundary in the table, by
# tests/runtime/test_feature_window_caller_contract_20260728.py.
_BOUNDARY_DECISION: Mapping[str, DecisionContext] = MappingProxyType(
    {
        "task_start": DecisionContext.SOURCE_TARGET_SELECTION,
        "search_result": DecisionContext.SOURCE_TARGET_SELECTION,
        "failed_search": DecisionContext.SOURCE_TARGET_SELECTION,
        "file_view": DecisionContext.SOURCE_UNDERSTANDING,
        "first_view_edit": DecisionContext.SOURCE_UNDERSTANDING,
        "edit_result": DecisionContext.PATCH_CONSTRUCTION,
        "test_result": DecisionContext.PATCH_PROPAGATION,
        "failure_obs": DecisionContext.PATCH_PROPAGATION,
        "submit": DecisionContext.COMPLETION,
    }
)


@dataclass(frozen=True)
class FeatureWindow:
    """A feature's THREE-POINT commitment window, in fact_registry EVENT vocabulary.

    Why this exists: the scheduler's release test used to be the literal constant
    ``CommitmentWindowState.OPEN``, which made ``release_allowed == relevant`` — timing
    was algebraically eliminated and the commitment window was UNFALSIFIABLE. A contract
    that carries a window can be asked whether its moment has arrived, has not arrived
    yet, or has passed.

    * ``earliest_event`` -- before this, the fact is premature (NOT_OPEN).
    * ``deliver_by`` -- the last boundary at which the fact can still SHAPE the decision.
    * ``corrective_boundary`` -- the last boundary at which the fact is still useful as a
      CORRECTION of a decision already taken. After it, the decision is COMMITTED.

    :meth:`resolve` NEVER returns ``CLOSED``. A window that has passed routes to
    ``COMMITTED``, and the scheduler projects that to a HELD (recoverable) record --
    never to the terminal, unrecoverable EXPIRED.
    """

    earliest_event: str
    deliver_by: str
    corrective_boundary: str

    def __post_init__(self) -> None:
        ranks: list[int] = []
        for field_name in ("earliest_event", "deliver_by", "corrective_boundary"):
            boundary = getattr(self, field_name)
            decision = _BOUNDARY_DECISION.get(boundary)
            if decision is None:
                raise ValueError(
                    f"feature window {field_name} is not a known delivery boundary: {boundary!r}"
                )
            ranks.append(_DECISION_SPINE.index(decision))
        if not ranks[0] <= ranks[1] <= ranks[2]:
            raise ValueError(
                "feature window boundaries must be non-decreasing in time: "
                f"{self.earliest_event} -> {self.deliver_by} -> "
                f"{self.corrective_boundary}"
            )

    def resolve(self, observed: DecisionContext | None) -> CommitmentWindowState:
        """Where ``observed`` sits relative to this window.

        Fail-OPEN by construction: an absent or off-spine (FAILURE_RECOVERY) decision
        resolves to OPEN, so an unrecognised runtime position can never withhold a fact.
        """

        if observed is None:
            return CommitmentWindowState.OPEN
        try:
            now = _DECISION_SPINE.index(observed)
        except ValueError:
            return CommitmentWindowState.OPEN
        if now < _DECISION_SPINE.index(_BOUNDARY_DECISION[self.earliest_event]):
            return CommitmentWindowState.NOT_OPEN
        if now <= _DECISION_SPINE.index(_BOUNDARY_DECISION[self.corrective_boundary]):
            return CommitmentWindowState.OPEN
        return CommitmentWindowState.COMMITTED


@dataclass(frozen=True)
class FeatureContract:
    feature_id: str
    failure_definition: str
    decision_context: DecisionContext
    roles: tuple[EvidenceRole, ...]
    ready_predicates: tuple[TemporalPredicate, ...]
    relevance_predicates: tuple[TemporalPredicate, ...]
    commitment_predicates: tuple[TemporalPredicate, ...]
    expiry_predicates: tuple[TemporalPredicate, ...]
    revision_dependencies: tuple[str, ...]
    fallback_policy: FeatureFallbackPolicy
    commitment_boundary: str
    # TRAILING + defaulted on purpose. ``None`` means "this contract declares no window",
    # and the scheduler then uses the historical unconditional-OPEN release. That None
    # branch is the BYTE-IDENTITY guarantee for every contract that does not opt in.
    # No __post_init__ branch is required: this field is neither one of the tuple-coerced
    # sequence fields nor one of the non-empty-validated ones, and FeatureWindow performs
    # its own validation in its own __post_init__.
    window: FeatureWindow | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "roles",
            "ready_predicates",
            "relevance_predicates",
            "commitment_predicates",
            "expiry_predicates",
            "revision_dependencies",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if not self.feature_id or not self.failure_definition.strip():
            raise ValueError("feature contract identity/failure definition is required")
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise ValueError("feature contract roles must be non-empty and unique")
        for field_name in (
            "ready_predicates",
            "relevance_predicates",
            "commitment_predicates",
            "expiry_predicates",
            "revision_dependencies",
        ):
            values = getattr(self, field_name)
            if not values:
                raise ValueError(f"feature contract {field_name} must be non-empty")
        if self.fallback_policy.feature_id != self.feature_id:
            raise ValueError("fallback policy feature identity mismatch")
        if not self.commitment_boundary.strip():
            raise ValueError("feature contract commitment boundary is required")

    @property
    def ready_rules(self) -> tuple[str, ...]:
        return tuple(predicate.value for predicate in self.ready_predicates)

    @property
    def relevance_rules(self) -> tuple[str, ...]:
        return tuple(predicate.value for predicate in self.relevance_predicates)

    @property
    def commitment_rules(self) -> tuple[str, ...]:
        return tuple(predicate.value for predicate in self.commitment_predicates)

    @property
    def expiry_rules(self) -> tuple[str, ...]:
        return tuple(predicate.value for predicate in self.expiry_predicates)

    @property
    def fallback_substrates(self) -> tuple[str, ...]:
        return self.fallback_policy.fallback_substrates


@dataclass(frozen=True)
class TemporalRuntimeContext:
    active_decision: ActiveDecision | None
    satisfied_predicates: frozenset[TemporalPredicate]
    commitment_window: CommitmentWindowState
    current_revision: RevisionVector
    available_substrates: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "satisfied_predicates", frozenset(self.satisfied_predicates))
        object.__setattr__(self, "available_substrates", tuple(self.available_substrates))


@dataclass(frozen=True)
class TemporalContractEvaluation:
    ready: bool
    relevant: bool
    release_allowed: bool
    expired: bool
    invalidated: bool
    next_lifecycle: EvidenceLifecycle
    reason: EvidenceTransitionReason | None
    unsatisfied_predicates: tuple[TemporalPredicate, ...] = ()


_FACT_DECISION_CONTRACTS: Mapping[
    str,
    tuple[str, DecisionContext, tuple[EvidenceRole, ...]],
] = {
    "caller_contract": (
        "changing behavior without preserving dependent caller contracts",
        DecisionContext.PATCH_CONSTRUCTION,
        (EvidenceRole.BEHAVIORAL_CONTRACT, EvidenceRole.AFFECTED_CALLER),
    ),
    "covering_red": (
        "continuing repair without the repository test that proves the failure",
        DecisionContext.FAILURE_RECOVERY,
        (EvidenceRole.BLOCKER, EvidenceRole.VALIDATION),
    ),
    "def_partition": (
        "selecting a use, test-only copy, or dead definition as the source target",
        DecisionContext.SOURCE_TARGET_SELECTION,
        (EvidenceRole.TARGET_IDENTITY, EvidenceRole.EXECUTION_REACHABILITY),
    ),
    "localization": (
        "opening excessive or causally unrelated source files",
        DecisionContext.SOURCE_TARGET_SELECTION,
        (EvidenceRole.TARGET_IDENTITY, EvidenceRole.EXECUTION_REACHABILITY),
    ),
    "newfile_precedent": (
        "inventing a destination or integration pattern for a missing file",
        DecisionContext.SOURCE_TARGET_SELECTION,
        (EvidenceRole.TARGET_IDENTITY, EvidenceRole.STATE_DEPENDENCY),
    ),
    "obligations": (
        "constructing a patch that omits an exact task requirement",
        DecisionContext.PATCH_CONSTRUCTION,
        (EvidenceRole.BEHAVIORAL_CONTRACT,),
    ),
    "recovery": (
        "repeating an operational hypothesis contradicted by unchanged failure",
        DecisionContext.FAILURE_RECOVERY,
        (EvidenceRole.CONTRADICTION, EvidenceRole.VALIDATION),
    ),
    "signature_delta": (
        "changing a signature without propagating caller consequences",
        DecisionContext.PATCH_PROPAGATION,
        (EvidenceRole.BEHAVIORAL_CONTRACT, EvidenceRole.AFFECTED_CALLER),
    ),
    "submit_refusal": (
        "declaring completion while required validation remains unresolved",
        DecisionContext.COMPLETION,
        (EvidenceRole.BLOCKER, EvidenceRole.TERMINAL_ASSURANCE),
    ),
    "syntax_result": (
        "continuing from a structurally invalid edit",
        DecisionContext.PATCH_PROPAGATION,
        (EvidenceRole.BLOCKER, EvidenceRole.VALIDATION),
    ),
}


_CAP_FACT_BINDING = {
    "GT_CHANGE_SURFACE": "newfile_precedent",
    "GT_PATCH_DELTA": "signature_delta",
    "GT_LOC_RESLOT": "localization",
    "GT_SS_SUBMIT_RED": "submit_refusal",
    "GT_EDIT_CHECK": "syntax_result",
    "GT_HYPOTHESIS": "recovery",
    "GT_CERT_DELIVERY": "submit_refusal",
}


def _validate_evidence_byte_owners(evidence: EvidenceRecord) -> None:
    """Fail closed when audit ownership is not bound to this physical FACT."""

    invalid = tuple(
        owner
        for owner in evidence.owner_feature_ids
        if _CAP_FACT_BINDING.get(owner) != evidence.feature_id
    )
    if invalid:
        raise StateIntegrityError(
            "evidence byte owner is not authorized for its canonical FACT: " + ",".join(invalid)
        )


# THE SINGLE SOURCE OF TRUTH for how the decision-window marker projects out of a generation
# identity. Exported 2026-07-28 because it was a hand-duplicated RULE, not a shared one: the
# WRITER (`_evidence_generation_projection` below) normalized the field, while the offline
# READER (`runtime_attestation._evidence_generation_projection`) compared it RAW. The runtime
# therefore accepted a re-offer whose window had advanced, wrote both journal rows, and the
# reader then rejected the identical history as EVIDENCE_GENERATION_REWRITTEN.
#
# Observed on run 30390877219, all 5/5 tasks, always the same record: the task-start
# `obligations` capsule (feature_id="obligations", mandatory_reason=TASK_OBLIGATION), whose
# window advanced from "" to a real GT-W-* marker between journal seq 1 and 2. The reject made
# `delivered_count` 0 and `integrity_ok` false, which dropped
# `canonical_runtime_attestation_integrity` from the required inputs and marked EVERY task
# uncitable. Nothing about the delivery was wrong; the two halves of one rule disagreed.
#
# WHY A SENTINEL RATHER THAN "": the marker is runtime-owned scheduling state, but the
# obligations task-start record must never project-equal a record that legitimately carries no
# window at all. Two constants keep those distinguishable while erasing the volatile value.
#
# Every consumer must IMPORT this; a new hand-written copy of the conditional is a defect.
def projected_decision_window(
    feature_id: object,
    mandatory_reason: object,
) -> str:
    """Normalize the runtime-owned window marker for generation comparison.

    `mandatory_reason` is accepted either as the enum (runtime, in-process) or as its
    serialized `name` (offline, reading `canonical_json` out of the journal), so the one rule
    serves both halves without either side re-deriving it.
    """

    reason_name = (
        mandatory_reason.name
        if isinstance(mandatory_reason, MandatoryReason)
        else str(mandatory_reason or "")
    )
    if feature_id == "obligations" and reason_name == "TASK_OBLIGATION":
        return "GT-W-PROJECTION"
    return ""


def _evidence_generation_projection(
    evidence: EvidenceRecord,
) -> EvidenceRecord:
    """Return the immutable computation/revision identity of one generation.

    Lifecycle progress and authorized CAP audit ownership are projections over
    a generation, not producer-owned semantic identity.  Normalizing those
    fields lets a repeated producer offer match the already-advanced runtime
    record without resetting it.
    """

    return replace(
        evidence,
        lifecycle=EvidenceLifecycle.PENDING,
        fresh=True,
        superseded=False,
        transition_history=(),
        owner_feature_ids=(),
        # The window marker is runtime-owned scheduling state, not part of the
        # producer computation identity. The source identity remains part of
        # the generation: a clone can never merge back into its root.
        decision_window_generation=projected_decision_window(
            evidence.feature_id,
            evidence.mandatory_reason,
        ),
    )


def _merge_same_evidence_generation(
    existing: EvidenceRecord,
    incoming: EvidenceRecord,
) -> EvidenceRecord:
    """Preserve lifecycle while enriching authorized runtime-owned lineage."""

    if existing.evidence_id != incoming.evidence_id:
        raise StateIntegrityError("cannot merge different evidence identities")
    _validate_evidence_byte_owners(existing)
    _validate_evidence_byte_owners(incoming)
    if _evidence_generation_projection(existing) != _evidence_generation_projection(incoming):
        raise StateIntegrityError("evidence identity reused with different canonical generation")
    existing_window = existing.decision_window_generation
    incoming_window = incoming.decision_window_generation
    if existing_window and incoming_window and existing_window != incoming_window:
        if existing.lifecycle not in {
            EvidenceLifecycle.DISCOVERED,
            EvidenceLifecycle.PENDING,
            EvidenceLifecycle.READY,
            EvidenceLifecycle.HELD,
        }:
            raise StateIntegrityError("provider-bound evidence decision generation cannot change")
        merged_window = incoming_window
    else:
        merged_window = existing_window or incoming_window
    return replace(
        existing,
        owner_feature_ids=tuple(
            sorted(set(existing.owner_feature_ids) | set(incoming.owner_feature_ids))
        ),
        decision_window_generation=merged_window,
    )


def decision_window_generation(
    *,
    attempt_id: str,
    decision_context: DecisionContext,
    decision_window_key: str,
) -> str:
    """Return the stable scheduling generation for one open decision window."""

    if not attempt_id:
        raise ValueError("attempt_id is required")
    digest = _sha256(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "context": decision_context.value,
                "window": decision_window_key or "attempt-start",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )[:16]
    return f"GT-W-{digest}"


def rematerialize_task_obligation(
    source: EvidenceRecord,
    *,
    generation: str,
) -> EvidenceRecord:
    """Mint one fresh operational generation from immutable task evidence.

    The already-released source remains untouched. The clone resets only
    lifecycle/delivery-visibility projections and receives a deterministic id
    bound to the source plus the reopened decision window.
    """

    if (
        source.feature_id != "obligations"
        or source.mandatory_reason is not MandatoryReason.TASK_OBLIGATION
        or source.standing_source_evidence_id
        or not source.decision_window_generation
        or source.decision_window_generation == generation
        or not generation
    ):
        raise ValueError(
            "standing rematerialization requires a root task obligation "
            "from a different non-empty generation"
        )
    digest = _sha256(f"{source.evidence_id}\0{generation}")[:16]
    return replace(
        source,
        evidence_id=f"{source.evidence_id}-w{digest}",
        lifecycle=EvidenceLifecycle.PENDING,
        fresh=True,
        already_visible=False,
        superseded=False,
        transition_history=(),
        visible_to_decision_ids=(),
        standing_source_evidence_id=source.evidence_id,
        decision_window_generation=generation,
    )


_FACT_FALLBACK_POLICIES: Mapping[
    str,
    tuple[
        tuple[str, ...],
        tuple[str, ...],
        EvidenceGrade,
        Authority,
    ],
] = {
    "caller_contract": (
        ("graph", "lsp"),
        ("ast_references", "exact_lexical_references"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
    "covering_red": (
        ("structured_test_result", "graph"),
        ("native_test_result", "exact_test_execution"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
    "def_partition": (
        ("graph", "ast"),
        ("exact_lexical_definitions", "repository_paths"),
        EvidenceGrade.WARNING,
        Authority.RESULT_SHAPE,
    ),
    "localization": (
        ("graph", "fts5"),
        ("exact_lexical_search", "repository_paths"),
        EvidenceGrade.WARNING,
        Authority.RESULT_SHAPE,
    ),
    "newfile_precedent": (
        ("graph", "history"),
        ("sibling_structure", "build_metadata"),
        EvidenceGrade.WARNING,
        Authority.RESULT_DERIVED,
    ),
    "obligations": (
        ("issue_text", "obligation_parser"),
        ("exact_issue_text", "canonical_task_event"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
    "recovery": (
        ("canonical_event_history", "test_fingerprint"),
        ("exact_failure_history", "repository_diff"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
    "signature_delta": (
        ("graph", "lsp"),
        ("ast_references", "exact_lexical_references"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
    "submit_refusal": (
        ("canonical_validation_state", "graph"),
        ("native_test_result", "native_compile_result"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
    "syntax_result": (
        ("parser_result", "compiler_result"),
        ("native_tool_result", "exact_exit_status"),
        EvidenceGrade.VERIFIED,
        Authority.RESULT_DERIVED,
    ),
}


def _fallback_policy_for(feature_id: str) -> FeatureFallbackPolicy:
    preferred, fallbacks, grade, authority = _FACT_FALLBACK_POLICIES[feature_id]
    return FeatureFallbackPolicy(
        feature_id=feature_id,
        preferred_substrates=preferred,
        fallback_substrates=fallbacks,
        minimum_grade=grade,
        minimum_authority=authority,
    )


def _build_feature_contracts() -> Mapping[str, FeatureContract]:
    from groundtruth.runtime import fact_registry
    from groundtruth.runtime import feature_lineage

    # DECLARED WINDOWS. Exactly one contract opts in today; every other contract keeps
    # ``window=None`` and therefore the historical unconditional-OPEN release, which is
    # what makes this change byte-identical for the other sixteen DIRECT features.
    #
    # ``caller_contract`` is the opt-in because its three-point window ALREADY exists in
    # the registry, merely shattered across three rows, and because it binds no CAP byte
    # owner (it is absent from _CAP_FACT_BINDING.values()), so the blast radius is this
    # one contract row:
    #   earliest    <- ``caller_contract_search`` boundary override (search_result): the
    #                  PRE-EDIT mirror, the first moment a caller contract is answerable.
    #   deliver_by  <- the canonical registration (file_view): the last boundary at which
    #                  the contract can still SHAPE the edit.
    #   corrective  <- ``caller_break`` boundary override (edit_result): the same facts one
    #                  boundary later, as a CORRECTION to an edit already made.
    # Every value is read from the live registry; none is written down here.
    feature_windows: dict[str, FeatureWindow] = {
        "caller_contract": FeatureWindow(
            earliest_event=fact_registry.earliest_event_for("caller_contract_search"),
            deliver_by=fact_registry.required_event("caller_contract"),
            corrective_boundary=fact_registry.required_event("caller_break"),
        )
    }

    rows: dict[str, FeatureContract] = {}
    delivery_facts = {
        feature_id
        for feature_id, registration in fact_registry.REGISTRY.items()
        if registration.fact_role == fact_registry.FACT_ROLE_DELIVERY
    }
    if delivery_facts != set(_FACT_DECISION_CONTRACTS):
        raise ValueError("canonical feature contracts drifted from FACT registry")
    for feature_id in sorted(delivery_facts):
        failure, context, roles = _FACT_DECISION_CONTRACTS[feature_id]
        registration = fact_registry.REGISTRY[feature_id]
        rows[feature_id] = FeatureContract(
            feature_id=feature_id,
            failure_definition=failure,
            decision_context=context,
            roles=roles,
            ready_predicates=(
                TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
                TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
            ),
            relevance_predicates=(
                TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
                TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
                TemporalPredicate.REASONING_GRAPH_CONNECTED,
            ),
            commitment_predicates=(TemporalPredicate.COMMITMENT_WINDOW_OPEN,),
            expiry_predicates=(
                TemporalPredicate.ACTIVE_DECISION_CLOSED,
                TemporalPredicate.REVISION_DEPENDENCY_CHANGED,
            ),
            revision_dependencies=registration.freshness_deps,
            fallback_policy=_fallback_policy_for(feature_id),
            commitment_boundary=registration.deliver_by,
            window=feature_windows.get(feature_id),
        )

    if set(feature_lineage.CAP_BYTE_OWNER_IDS) != set(_CAP_FACT_BINDING):
        raise ValueError("canonical feature contracts drifted from CAP byte owners")
    for owner in sorted(feature_lineage.CAP_BYTE_OWNER_IDS):
        fact = rows[_CAP_FACT_BINDING[owner]]
        roles = fact.roles
        if owner == "GT_CERT_DELIVERY":
            roles = (EvidenceRole.TERMINAL_ASSURANCE,)
        rows[owner] = FeatureContract(
            feature_id=owner,
            failure_definition=(
                "the byte-owning capability fails to expose its canonical "
                f"{_CAP_FACT_BINDING[owner]} evidence at the open decision"
            ),
            decision_context=fact.decision_context,
            roles=roles,
            ready_predicates=fact.ready_predicates
            + (TemporalPredicate.AUTHORIZED_BYTE_OWNER_LINEAGE_PRESENT,),
            relevance_predicates=fact.relevance_predicates,
            commitment_predicates=fact.commitment_predicates,
            expiry_predicates=fact.expiry_predicates,
            revision_dependencies=fact.revision_dependencies,
            fallback_policy=replace(fact.fallback_policy, feature_id=owner),
            commitment_boundary=fact.commitment_boundary,
        )
    return MappingProxyType(rows)


FEATURE_CONTRACTS: Mapping[str, FeatureContract] = _build_feature_contracts()


def feature_contract_for(feature_id: str) -> FeatureContract | None:
    return FEATURE_CONTRACTS.get(feature_id)


def role_driven_coalition_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``GT_ROLE_DRIVEN_COALITION`` -- default OFF, so the default is byte-identical.

    When on, coalition eligibility follows the roles a decision declares it needs rather
    than which producer raised the evidence. Measured motivation: 7 of the 17 features fire
    at a boundary whose open decision differs from their declared context, and 12 of 25
    required/useful role slots have no in-context carrier at all.

    Read ONCE by the seam and passed into ``AttemptReasoningRuntime``; never read inside the
    gate or the composer, which must stay pure for replay.
    """
    source = os.environ if env is None else env
    return str(source.get("GT_ROLE_DRIVEN_COALITION", "0")).strip() == "1"


@dataclass(frozen=True)
class CanonicalEvidenceSemantics:
    """Producer-owned structured meaning required for canonical evidence.

    Legacy envelope ``payload`` text is intentionally not interpreted here.
    Producers must attach this typed sidecar so the canonical runtime never
    guesses a claim, consequence, decision, or reasoning role from rendered
    prose.
    """

    decision_context: DecisionContext
    roles: tuple[EvidenceRole, ...]
    claim: str
    actionable_consequence: str
    causal_neighborhood: tuple[str, ...]
    authority: Authority
    revision: RevisionVector
    revision_dependencies: tuple[str, ...]
    mandatory_reason: MandatoryReason | None
    failure_prevention: int
    causal_value: int
    contradiction_resolution: int
    anchoring_risk: int
    observed_substrates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "roles",
            "causal_neighborhood",
            "revision_dependencies",
            "observed_substrates",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise ValueError("canonical semantics roles must be non-empty and unique")
        if not self.claim or not self.actionable_consequence:
            raise ValueError("canonical semantics claim and consequence are required")
        if not self.causal_neighborhood:
            raise ValueError("canonical semantics causal neighborhood is required")
        if not self.revision_dependencies:
            raise ValueError("canonical semantics revision dependencies are required")
        if self.observed_substrates != tuple(sorted(set(self.observed_substrates))) or any(
            not item.strip() for item in self.observed_substrates
        ):
            raise ValueError(
                "canonical semantics observed_substrates must be sorted unique non-empty identities"
            )
        for field_name in (
            "failure_prevention",
            "causal_value",
            "contradiction_resolution",
            "anchoring_risk",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")


def _roles_match_contract(
    contract: FeatureContract,
    roles: tuple[EvidenceRole, ...],
    mandatory_reason: MandatoryReason | None,
) -> bool:
    """Return whether producer roles are the exact contract plus a valid overlay."""

    overlay = {
        MandatoryReason.BLOCKER: {EvidenceRole.BLOCKER},
        MandatoryReason.VERIFIED_CONTRADICTION: {EvidenceRole.CONTRADICTION},
        MandatoryReason.MATERIAL_UNCERTAINTY: {EvidenceRole.MATERIAL_UNCERTAINTY},
        MandatoryReason.TASK_OBLIGATION: {EvidenceRole.BEHAVIORAL_CONTRACT},
        None: set(),
    }[mandatory_reason]
    allowed = (
        {frozenset(contract.roles)}
        if mandatory_reason is None
        else {
            frozenset(overlay),
            frozenset(set(contract.roles) | overlay),
        }
    )
    return frozenset(roles) in allowed


def _authorized_cap_byte_owners(envelope: object, lineage: object) -> tuple[str, ...]:
    """Return only CAP byte-owner claims authorized for this exact computation."""

    from groundtruth.runtime.feature_lineage import (
        CAP_BYTE_OWNER_MECHANISMS,
    )

    owner_ids: list[str] = []
    for ref in getattr(lineage, "features", ()):
        if getattr(ref, "category", "") != "CAP":
            continue
        if getattr(ref, "role", "") != "byte_owner":
            continue
        feature_id = getattr(ref, "feature_id", "")
        mechanism = CAP_BYTE_OWNER_MECHANISMS.get(feature_id)
        if mechanism is None:
            continue
        # The canonical FACT lineage already proves the registered producer and
        # fine evidence-type alias. CAP ownership binds to that physical FACT
        # computation; legacy aliases may legitimately differ from the original
        # mechanism's producer/layer spelling.
        if any(
            binding.fact_class == getattr(lineage, "fact_class", "")
            for binding in mechanism.bindings
        ):
            owner_ids.append(feature_id)
    return tuple(sorted(set(owner_ids)))


def _canonical_sidecar_provenance(
    envelope: object,
    semantics: CanonicalEvidenceSemantics,
    *,
    committed_event_hashes: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Validate render-neutral repository/runtime witnesses for a provenance-empty fact."""
    from groundtruth.runtime.evidence_envelope import (
        _leaky_path,
        runtime_witness_violations,
    )
    from groundtruth.runtime.producer_inputs import (
        PRODUCER_INPUTS_SCHEMA,
        ProducerInputs,
        RepositoryWitnessRow,
        SourceState,
    )

    runtime_witnesses = tuple(getattr(envelope, "runtime_witnesses", ()))
    if runtime_witnesses:
        provenances: list[str] = []
        for witness in runtime_witnesses:
            if runtime_witness_violations(witness):
                return ()
            if witness.kind == "canonical_event":
                if (
                    committed_event_hashes is None
                    or committed_event_hashes.get(witness.witness_id) != witness.content_sha256
                ):
                    return ()
                provenances.append(f"event:{witness.witness_id}:sha256:{witness.content_sha256}")
            elif witness.kind == "deterministic_computation":
                provenances.append(
                    f"computation:{witness.witness_id}:sha256:{witness.content_sha256}"
                )
            elif witness.kind == "diagnostic_location":
                provenances.append(
                    "diagnostic:"
                    f"{witness.source_path}:{witness.source_line}:"
                    f"{witness.source_column}:sha256:{witness.content_sha256}"
                )
            else:
                return ()
        return tuple(dict.fromkeys(provenances))

    inputs = getattr(envelope, "producer_inputs", None)
    if not isinstance(inputs, ProducerInputs):
        return ()
    if (
        getattr(envelope, "producer", "") != "change_surface"
        or getattr(envelope, "evidence_type", "") != "new_file_destination"
        or inputs.schema != PRODUCER_INPUTS_SCHEMA
        or inputs.evidence_type != getattr(envelope, "evidence_type", "")
        or inputs.candidate_id != getattr(envelope, "dedup_key", "")
        or inputs.graph_revision != semantics.revision.graph
        or inputs.graph_revision != getattr(envelope, "graph_revision", "")
        or not inputs.repository_witness_rows
    ):
        return ()
    allowed_kinds = {
        "template_definition",
        "template_lexical_reference",
        "registration_reference",
    }
    provenances: list[str] = []
    for row in inputs.repository_witness_rows:
        if not isinstance(row, RepositoryWitnessRow):
            return ()
        source_state = row.source_state
        normalized = row.file.replace("\\", "/")
        if (
            not isinstance(source_state, SourceState)
            or not row.file
            or normalized != row.file
            or normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in normalized.split("/")
            or _leaky_path(normalized)
            or type(row.line) is not int
            or row.line <= 0
            or row.kind not in allowed_kinds
            or not row.identity.strip()
            or source_state.file != row.file
        ):
            return ()
        try:
            _validate_sha256(
                source_state.sha256,
                field_name="repository witness sha256",
            )
        except ValueError:
            return ()
        if source_state.revision != f"source:{source_state.sha256}":
            return ()
        provenances.append(f"{row.file}:{row.line}")
    return tuple(dict.fromkeys(provenances))


def _revision_bound_evidence_id(
    physical_dedup_key: str,
    revision: RevisionVector,
) -> str:
    """Bind physical fact identity to the immutable computation generation."""

    generation_hash = _sha256(_canonical_json(revision))
    return f"GT-E-{physical_dedup_key}-g{generation_hash}"


def _dedup_key_from_evidence_id(evidence_id: str) -> str:
    """The inverse of :func:`_revision_bound_evidence_id` -- the physical dedup key, or "".

    It lives HERE, beside the constructor, so the id format is written down exactly once. The
    dedup key is what every other lane calls a row's ``candidate_id`` (the gateway stamps
    ``candidate_id=winner.dedup_key``), so recovering it is what lets a capsule delivery be
    joined on the SAME identity contract as a lane delivery instead of a second, divergent one.

    FAIL-CLOSED. An id this module did not mint yields "" rather than a guess: a wrong candidate
    id would seat an attestation against bytes that never carried that fact, which is strictly
    worse than no join. The split is on the LAST ``-g`` because ``_sha256`` is hex and a dedup
    key is hex, so neither half can contain the separator.
    """
    if not isinstance(evidence_id, str) or not evidence_id.startswith("GT-E-"):
        return ""
    body = evidence_id[len("GT-E-") :]
    key, separator, generation = body.rpartition("-g")
    if not separator or not key or not generation:
        return ""
    return key


def canonical_evidence_from_envelope(
    envelope: object,
    *,
    committed_event_hashes: Mapping[str, str] | None = None,
) -> EvidenceRecord | None:
    """Convert one trusted legacy envelope into one canonical FACT record.

    Unknown, crossed, or incompletely typed producer output is dropped
    correct-or-quiet. The authoritative physical identity is the FACT class in
    ``DeliveryLineage``; a finer evidence type and CAP ownership are audit
    metadata only.
    """

    from groundtruth.runtime import fact_registry
    from groundtruth.runtime.evidence_envelope import EvidenceEnvelope, validate
    from groundtruth.runtime.feature_lineage import FeatureRef

    if not isinstance(envelope, EvidenceEnvelope):
        return None
    # Serialized legacy envelopes predate canonical semantics and can be
    # replaced/tampered after construction. Re-run the original fail-closed
    # leak and tier laws at the conversion boundary before trusting sidecars.
    if validate(envelope):
        return None
    lineage = envelope.lineage
    semantics = envelope.canonical_semantics
    if lineage is None or not isinstance(semantics, CanonicalEvidenceSemantics):
        return None
    if (
        not lineage.producer_registration_match
        or lineage.runtime_producer_id != envelope.producer
        or lineage.evidence_type != envelope.evidence_type
    ):
        return None
    registration = fact_registry.REGISTRY.get(lineage.fact_class)
    if (
        registration is None
        or registration.fact_role != fact_registry.FACT_ROLE_DELIVERY
        or registration.producer != lineage.registered_producer_id
        or FeatureRef("FACT", lineage.fact_class, "fact") not in lineage.features
    ):
        return None
    contract = feature_contract_for(lineage.fact_class)
    if contract is None or (
        semantics.decision_context is not contract.decision_context
        or not _roles_match_contract(
            contract,
            semantics.roles,
            semantics.mandatory_reason,
        )
        or semantics.revision_dependencies != contract.revision_dependencies
    ):
        return None
    # The legacy graph token and the full canonical vector must agree.  A
    # producer may use a composite ``valid_until`` token, so only the graph
    # identity itself is compared here.
    if envelope.graph_revision and envelope.graph_revision != semantics.revision.graph:
        return None
    tier_map = {
        "VERIFIED": EvidenceGrade.VERIFIED,
        "WARNING": EvidenceGrade.WARNING,
        "HYPOTHESIS": EvidenceGrade.HYPOTHESIS,
        "INFO": EvidenceGrade.INFO,
    }
    grade = tier_map.get(envelope.tier)
    if grade is None:
        return None
    provenance = tuple(f"{path}:{line}" for path, line in envelope.provenance)
    if not provenance:
        provenance = _canonical_sidecar_provenance(
            envelope,
            semantics,
            committed_event_hashes=committed_event_hashes,
        )
    if not provenance:
        return None
    try:
        return EvidenceRecord(
            evidence_id=_revision_bound_evidence_id(
                envelope.dedup_key,
                semantics.revision,
            ),
            feature_id=lineage.fact_class,
            decision_context=contract.decision_context,
            roles=semantics.roles,
            subject=envelope.target,
            claim=semantics.claim,
            actionable_consequence=semantics.actionable_consequence,
            provenance=provenance,
            grade=grade,
            revision=semantics.revision,
            causal_neighborhood=semantics.causal_neighborhood,
            lifecycle=EvidenceLifecycle.PENDING,
            fresh=True,
            already_visible=False,
            superseded=False,
            mandatory_reason=semantics.mandatory_reason,
            token_cost=max(1, envelope.estimated_cost_tokens),
            failure_prevention=semantics.failure_prevention,
            causal_value=semantics.causal_value,
            contradiction_resolution=semantics.contradiction_resolution,
            anchoring_risk=semantics.anchoring_risk,
            revision_dependencies=contract.revision_dependencies,
            authority=semantics.authority,
            owner_feature_ids=_authorized_cap_byte_owners(envelope, lineage),
            observed_substrates=semantics.observed_substrates,
        )
    except (TypeError, ValueError):
        return None


def canonicalize_evidence_envelopes(
    envelopes: Iterable[object],
    *,
    committed_event_hashes: Mapping[str, str] | None = None,
) -> tuple[EvidenceRecord, ...]:
    """Normalize and merge physical evidence while unioning audit ownership.

    A semantic conflict poisons its physical identity for the entire batch.
    Later duplicates cannot launder a fail-closed suppression back into output.
    """

    by_id: dict[str, EvidenceRecord] = {}
    poisoned_ids: set[str] = set()
    for envelope in envelopes:
        record = canonical_evidence_from_envelope(
            envelope,
            committed_event_hashes=committed_event_hashes,
        )
        if record is None:
            continue
        if record.evidence_id in poisoned_ids:
            continue
        previous = by_id.get(record.evidence_id)
        if previous is None:
            by_id[record.evidence_id] = record
            continue
        # The dedup key is a physical-content identity. Conflicting canonical
        # semantics under that identity indicate corrupt producer output and
        # are suppressed instead of silently merged.
        comparable_previous = replace(
            previous,
            owner_feature_ids=(),
            observed_substrates=(),
        )
        comparable_record = replace(
            record,
            owner_feature_ids=(),
            observed_substrates=(),
        )
        if comparable_previous != comparable_record:
            by_id.pop(record.evidence_id, None)
            poisoned_ids.add(record.evidence_id)
            continue
        by_id[record.evidence_id] = replace(
            previous,
            owner_feature_ids=tuple(
                sorted(set(previous.owner_feature_ids) | set(record.owner_feature_ids))
            ),
            # INTERSECTION, deliberately -- and NOT an inconsistency with the
            # `owner_feature_ids` union directly above. The two fields answer different
            # questions. Ownership is additive: both features do own this record. SUBSTRATE
            # ASSURANCE is not: it authorizes RELEASE, so a merged record may only claim the
            # substrates BOTH observations support. Union would let one envelope's observation
            # authorize a release the other could not support -- the same cross-lending hole
            # the per-record gate exists to close.
            #
            # I changed this to a union on 2026-07-28 and
            # `test_duplicate_substrates_use_order_independent_intersection_and_owner_union`
            # ([empty] / [disjoint] / [partial-overlap]) caught it. Reverted. Recording it so
            # the "union is obviously right / it is inconsistent with the line above" argument
            # is not made a third time: the asymmetry is the point.
            observed_substrates=tuple(
                sorted(set(previous.observed_substrates) & set(record.observed_substrates))
            ),
        )
    return tuple(by_id[key] for key in sorted(by_id))


def feature_contract_registry_json() -> str:
    return _canonical_json(dict(FEATURE_CONTRACTS))


def evaluate_feature_contract(
    contract: FeatureContract,
    evidence: EvidenceRecord,
    context: TemporalRuntimeContext,
    *,
    role_driven: bool = False,
) -> TemporalContractEvaluation:
    """Evaluate readiness, relevance, release, expiry, and invalidation.

    ``role_driven`` must match the value passed to ``select_evidence_coalition``. This gate
    runs FIRST in ``AttemptReasoningRuntime``; evidence it rules irrelevant is downgraded to
    HELD, and the composer then drops it as NOT_READY before any role reasoning. If only the
    composer honoured the lever, the change would be invisible in production -- see
    ``tests/runtime/test_role_driven_temporal_gate_20260726.py``.

    A parameter, not an environment read: this must stay pure so replay reconstructs the same
    release and suppression decisions.
    """

    if evidence.feature_id != contract.feature_id:
        raise ValueError("evidence/feature contract identity mismatch")
    if (
        evidence.decision_context is not contract.decision_context
        or not _roles_match_contract(
            contract,
            evidence.roles,
            evidence.mandatory_reason,
        )
        or evidence.revision_dependencies != contract.revision_dependencies
    ):
        raise ValueError("evidence does not exactly satisfy its feature contract")

    if not _evidence_revision_is_fresh(evidence, context.current_revision):
        return TemporalContractEvaluation(
            ready=False,
            relevant=False,
            release_allowed=False,
            expired=False,
            invalidated=True,
            next_lifecycle=EvidenceLifecycle.INVALIDATED,
            reason=EvidenceTransitionReason.REVISION_DEPENDENCY_CHANGED,
        )

    # Provider-proven or terminal evidence is no longer schedulable.  In
    # particular, DELIVERED cannot later be forced through an impossible
    # DELIVERED -> EXPIRED transition when a commitment window closes.
    if evidence.lifecycle in {
        EvidenceLifecycle.DELIVERED,
        EvidenceLifecycle.ACTIVE,
        EvidenceLifecycle.SATISFIED,
        EvidenceLifecycle.SUPERSEDED,
        EvidenceLifecycle.EXPIRED,
        EvidenceLifecycle.INVALIDATED,
    }:
        return TemporalContractEvaluation(
            ready=True,
            relevant=True,
            release_allowed=False,
            expired=evidence.lifecycle is EvidenceLifecycle.EXPIRED,
            invalidated=(evidence.lifecycle is EvidenceLifecycle.INVALIDATED),
            next_lifecycle=evidence.lifecycle,
            reason=None,
        )

    if context.commitment_window in {
        CommitmentWindowState.COMMITTED,
        CommitmentWindowState.CLOSED,
    }:
        return TemporalContractEvaluation(
            ready=True,
            relevant=False,
            release_allowed=False,
            expired=True,
            invalidated=False,
            next_lifecycle=EvidenceLifecycle.EXPIRED,
            reason=EvidenceTransitionReason.DECISION_WINDOW_EXPIRED,
        )

    missing = tuple(
        predicate
        for predicate in contract.ready_predicates
        if predicate not in context.satisfied_predicates
    )
    if missing:
        return TemporalContractEvaluation(
            ready=False,
            relevant=False,
            release_allowed=False,
            expired=False,
            invalidated=False,
            next_lifecycle=EvidenceLifecycle.PENDING,
            reason=None,
            unsatisfied_predicates=missing,
        )

    active = context.active_decision
    if active is None:
        return TemporalContractEvaluation(
            ready=True,
            relevant=False,
            release_allowed=False,
            expired=False,
            invalidated=False,
            next_lifecycle=EvidenceLifecycle.READY,
            reason=EvidenceTransitionReason.READINESS_RULES_SATISFIED,
        )
    decision_type_anchors = {
        f"decision:{active.context.value}",
        f"decision:{active.decision_id}",
    }
    active_semantic_nodes = {
        node for node in active.causal_neighborhood if not node.startswith("decision:")
    }
    evidence_semantic_nodes = {
        node for node in evidence.causal_neighborhood if not node.startswith("decision:")
    }
    if evidence.mandatory_reason is MandatoryReason.TASK_OBLIGATION:
        evidence_semantic_nodes.add("obligation:task")
    # Compare against the record's OWN stamped context rather than the contract's primary.
    # Equivalent today (every record carries its contract's primary) but it states the
    # actual question -- does THIS record answer the open decision -- and stays correct if a
    # producer ever stamps a boundary-derived context.
    serves_open_decision = active.context is evidence.decision_context
    if not serves_open_decision and role_driven:
        # Provenance did not match, so fall back to what the decision says it NEEDS.
        # Measured on the live registry: 9 of the 17 features fire at a boundary whose open
        # decision differs from their declared context, and role fit takes oracle
        # eligibility from 8/17 to 17/17.
        serves_open_decision = bool(
            set(evidence.roles) & (set(active.required_roles) | set(active.useful_roles))
        )
    # The `decision:` anchor records which decision the evidence was PRODUCED for. The
    # installed producer (`gateway.py`, which runs at file_view) emits exactly
    # `decision:{contract.decision_context.value}` -- its own provenance context, never the
    # open decision's id. So requiring that anchor to name the open decision is the
    # producer-identity partition again, one layer below the gate, the composer and the
    # capsule compiler. It is what made a real view-boundary caller_contract record
    # DECISION_INCOMPLETE while a hand-anchored one compiled.
    #
    # Causal connection is NOT waived: `active_semantic_nodes.intersection(...)` below is the
    # real guard and it still applies -- it strips `decision:` prefixes entirely and matches
    # on subject/obligation, so unrelated evidence is still rejected.
    anchored_on_open_decision = bool(
        decision_type_anchors.intersection(evidence.causal_neighborhood)
    )
    relevant = (
        serves_open_decision
        and (anchored_on_open_decision or role_driven)
        and bool(active_semantic_nodes.intersection(evidence_semantic_nodes))
    )
    if not relevant:
        return TemporalContractEvaluation(
            ready=True,
            relevant=False,
            release_allowed=False,
            expired=False,
            invalidated=False,
            next_lifecycle=EvidenceLifecycle.HELD,
            reason=EvidenceTransitionReason.OTHER_DECISION_CURRENTLY_ACTIVE,
        )

    # PER-RECORD, deliberately. `available_substrates` is an ATTEMPT-WIDE union computed by
    # `_available_substrates(records)`, so without this intersection one record that observed
    # `parser_result` LENDS its assurance to a different record that observed nothing. That is
    # the same defect class as C12's possession gate: attributing one thing's evidence to
    # another. Pinned by `test_other_record_cannot_lend_parser_assurance_to_target_record`.
    #
    # I briefly "fixed" this by treating an empty `observed_substrates` as "unreported, do not
    # constrain". That was WRONG and their test caught it: it re-opens exactly the cross-record
    # lending hole, because attempt-wide availability would then authorize a record that
    # observed nothing. Correct-or-quiet means a record that cannot evidence its own substrate
    # stays HELD.
    #
    # THE REAL RESIDUAL IS ELSEWHERE, and weakening this gate must not be used to paper over
    # it: `_evidence_record_from_json` reads `raw.get("observed_substrates", ())`, and the
    # evidence journal has NO schema marker or migration, so every row written before the
    # field existed rehydrates to `()` and is permanently HELD on replay/resume. That is a
    # JOURNAL VERSIONING gap and must be fixed there -- the same way `canonical_events` got
    # `hash_schema` -- not by making this gate permissive.
    available = set(context.available_substrates).intersection(evidence.observed_substrates)
    preferred_available = bool(
        available.intersection(contract.fallback_policy.preferred_substrates)
    )
    fallback_available = bool(available.intersection(contract.fallback_policy.fallback_substrates))
    fallback_assured = (
        fallback_available
        and evidence.grade >= contract.fallback_policy.minimum_grade
        and evidence.authority >= contract.fallback_policy.minimum_authority
    )
    if not preferred_available and not fallback_assured:
        return TemporalContractEvaluation(
            ready=True,
            relevant=True,
            release_allowed=False,
            expired=False,
            invalidated=False,
            next_lifecycle=EvidenceLifecycle.HELD,
            reason=EvidenceTransitionReason.PREREQUISITES_PENDING,
        )

    if evidence.lifecycle is EvidenceLifecycle.RELEASED:
        return TemporalContractEvaluation(
            ready=True,
            relevant=True,
            release_allowed=False,
            expired=False,
            invalidated=False,
            next_lifecycle=evidence.lifecycle,
            reason=None,
        )

    release = context.commitment_window is CommitmentWindowState.OPEN
    return TemporalContractEvaluation(
        ready=True,
        relevant=True,
        release_allowed=release,
        expired=False,
        invalidated=False,
        next_lifecycle=(EvidenceLifecycle.RELEASED if release else EvidenceLifecycle.READY),
        reason=(
            EvidenceTransitionReason.DECISION_WINDOW_OPEN
            if release
            else EvidenceTransitionReason.READINESS_RULES_SATISFIED
        ),
    )


def _evaluate_current_decision_contract(
    contract: FeatureContract,
    evidence: EvidenceRecord,
    context: TemporalRuntimeContext,
    *,
    role_driven: bool = False,
) -> TemporalContractEvaluation:
    """Derive the scheduler window from record relevance, never a global scalar."""
    relevance = evaluate_feature_contract(
        contract,
        evidence,
        replace(
            context,
            commitment_window=CommitmentWindowState.NOT_OPEN,
        ),
        role_driven=role_driven,
    )
    if not relevance.relevant:
        return relevance

    # Pass 2 decides RELEASE. This used to be the literal CommitmentWindowState.OPEN,
    # which collapsed the identity to ``release_allowed == relevant`` and made the
    # commitment window unfalsifiable. A contract that declares a window is now asked
    # where the OPEN decision actually sits relative to it; a contract that declares
    # none keeps the exact former constant, and is therefore byte-identical.
    resolved = (
        contract.window.resolve(
            context.active_decision.context if context.active_decision is not None else None
        )
        if contract.window is not None
        else CommitmentWindowState.OPEN
    )

    # THE HELD GUARANTEE. Only OPEN is passed through as OPEN; NOT_OPEN and COMMITTED are
    # both projected onto NOT_OPEN. This is deliberate and load-bearing: feeding COMMITTED
    # (or CLOSED) into evaluate_feature_contract would hit its terminal window branch and
    # yield expired=True -> the unrecoverable EXPIRED lifecycle. NOT_OPEN instead yields
    # release_allowed=False with next_lifecycle=READY, which the selector downgrades to
    # HELD for this decision only while storage stays READY -- so a record withheld under
    # one decision still RELEASES when its own decision returns. The pure evaluator keeps
    # its explicit per-record expiry model untouched; this is the scheduler policy.
    return evaluate_feature_contract(
        contract,
        evidence,
        replace(
            context,
            commitment_window=(
                CommitmentWindowState.OPEN
                if resolved is CommitmentWindowState.OPEN
                else CommitmentWindowState.NOT_OPEN
            ),
        ),
        role_driven=role_driven,
    )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    feature_id: str
    decision_context: DecisionContext
    roles: tuple[EvidenceRole, ...]
    claim: str
    actionable_consequence: str
    provenance: tuple[str, ...]
    grade: EvidenceGrade
    owner_feature_ids: tuple[str, ...] = ()
    subject: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "provenance", tuple(self.provenance))
        object.__setattr__(self, "owner_feature_ids", tuple(self.owner_feature_ids))


@dataclass(frozen=True)
class SuppressionRecord:
    evidence_id: str
    reason: SuppressionReason
    held: bool = True


@dataclass(frozen=True)
class OracleDecision:
    decision_context: DecisionContext
    primary_claim: str
    coalition: tuple[EvidenceRef, ...]
    mandatory_items: tuple[str, ...]
    suppressed: tuple[SuppressionRecord, ...]
    total_tokens: int
    coverage: tuple[EvidenceRole, ...]
    unresolved_roles: tuple[EvidenceRole, ...]
    overall_grade: EvidenceGrade
    decision_complete: bool
    release_allowed: bool
    over_budget: bool
    decision_id: str = ""


def _evidence_record_from_json(payload: str) -> EvidenceRecord:
    raw = json.loads(payload)
    transitions = tuple(
        EvidenceTransition(
            from_state=EvidenceLifecycle(item["from_state"]),
            to_state=EvidenceLifecycle(item["to_state"]),
            reason_code=EvidenceTransitionReason(item["reason_code"]),
            reason_detail=item.get("reason_detail", ""),
        )
        for item in raw.get("transition_history", ())
    )
    mandatory = raw.get("mandatory_reason")
    return EvidenceRecord(
        evidence_id=raw["evidence_id"],
        feature_id=raw["feature_id"],
        decision_context=DecisionContext(raw["decision_context"]),
        roles=tuple(EvidenceRole(value) for value in raw["roles"]),
        subject=raw["subject"],
        claim=raw["claim"],
        actionable_consequence=raw["actionable_consequence"],
        provenance=tuple(raw["provenance"]),
        grade=EvidenceGrade(int(raw["grade"])),
        revision=RevisionVector(**raw["revision"]),
        causal_neighborhood=tuple(raw["causal_neighborhood"]),
        lifecycle=EvidenceLifecycle(raw["lifecycle"]),
        fresh=bool(raw["fresh"]),
        already_visible=bool(raw["already_visible"]),
        superseded=bool(raw["superseded"]),
        mandatory_reason=(MandatoryReason(mandatory) if mandatory is not None else None),
        token_cost=int(raw["token_cost"]),
        failure_prevention=int(raw["failure_prevention"]),
        causal_value=int(raw["causal_value"]),
        contradiction_resolution=int(raw["contradiction_resolution"]),
        anchoring_risk=int(raw["anchoring_risk"]),
        revision_dependencies=tuple(raw.get("revision_dependencies", ())),
        transition_history=transitions,
        authority=Authority(int(raw.get("authority", Authority.RESULT_DERIVED))),
        visible_to_decision_ids=tuple(raw.get("visible_to_decision_ids", ())),
        owner_feature_ids=tuple(raw.get("owner_feature_ids", ())),
        observed_substrates=tuple(raw.get("observed_substrates", ())),
        standing_source_evidence_id=str(raw.get("standing_source_evidence_id", "")),
        decision_window_generation=str(raw.get("decision_window_generation", "")),
    )


def _oracle_decision_from_json(payload: str) -> OracleDecision:
    raw = json.loads(payload)
    coalition = tuple(
        EvidenceRef(
            evidence_id=item["evidence_id"],
            feature_id=item["feature_id"],
            decision_context=DecisionContext(item["decision_context"]),
            roles=tuple(EvidenceRole(value) for value in item["roles"]),
            claim=item["claim"],
            actionable_consequence=item["actionable_consequence"],
            provenance=tuple(item["provenance"]),
            grade=EvidenceGrade(int(item["grade"])),
            owner_feature_ids=tuple(item.get("owner_feature_ids", ())),
            subject=str(item.get("subject", "")),
        )
        for item in raw["coalition"]
    )
    return OracleDecision(
        decision_context=DecisionContext(raw["decision_context"]),
        primary_claim=raw["primary_claim"],
        coalition=coalition,
        mandatory_items=tuple(raw["mandatory_items"]),
        suppressed=tuple(
            SuppressionRecord(
                evidence_id=item["evidence_id"],
                reason=SuppressionReason(item["reason"]),
                held=bool(item.get("held", True)),
            )
            for item in raw["suppressed"]
        ),
        total_tokens=int(raw["total_tokens"]),
        coverage=tuple(EvidenceRole(value) for value in raw["coverage"]),
        unresolved_roles=tuple(EvidenceRole(value) for value in raw["unresolved_roles"]),
        overall_grade=EvidenceGrade(int(raw["overall_grade"])),
        decision_complete=bool(raw["decision_complete"]),
        release_allowed=bool(raw["release_allowed"]),
        over_budget=bool(raw["over_budget"]),
        decision_id=raw.get("decision_id", ""),
    )


class CapsuleCompilationState(str, Enum):
    DISABLED = "DISABLED"
    COMPILED = "COMPILED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CapsuleBudget:
    target_tokens: int
    hard_max_tokens: int


_CAPSULE_BUDGETS = {
    DecisionContext.SOURCE_TARGET_SELECTION: CapsuleBudget(120, 220),
    DecisionContext.SOURCE_UNDERSTANDING: CapsuleBudget(180, 350),
    DecisionContext.PATCH_CONSTRUCTION: CapsuleBudget(180, 350),
    DecisionContext.PATCH_PROPAGATION: CapsuleBudget(160, 300),
    DecisionContext.FAILURE_RECOVERY: CapsuleBudget(140, 280),
    DecisionContext.COMPLETION: CapsuleBudget(100, 200),
}


def capsule_budget_for(context: DecisionContext) -> CapsuleBudget:
    return _CAPSULE_BUDGETS[context]


@dataclass(frozen=True)
class CapsuleBinding:
    model_call_id: str
    observation_id: str
    decision_context: DecisionContext
    evidence_ids: tuple[str, ...]
    capsule_hash: str
    provider_payload_hash: str
    message_index: int
    content_index: int
    decision_id: str = ""
    evidence_manifest_hash: str = ""
    schema: str = "gt.capsule_binding.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if (
            self.schema != "gt.capsule_binding.v1"
            or not self.model_call_id
            or not self.observation_id
            or not self.evidence_ids
            or self.message_index < 0
            or self.content_index < 0
        ):
            raise ValueError("capsule binding identity/index invariants failed")
        _validate_sha256(self.capsule_hash, field_name="capsule_hash")
        _validate_sha256(
            self.provider_payload_hash,
            field_name="provider_payload_hash",
        )
        if self.evidence_manifest_hash:
            _validate_sha256(
                self.evidence_manifest_hash,
                field_name="evidence_manifest_hash",
            )


@dataclass(frozen=True)
class CapsuleCompilation:
    state: CapsuleCompilationState
    native_observation: str
    decision_context: DecisionContext
    observation_id: str
    source_model_call_id: str
    model_call_id: str
    evidence_ids: tuple[str, ...] = ()
    capsule_text: str = ""
    capsule_hash: str = ""
    overall_grade: EvidenceGrade = EvidenceGrade.INFO
    delivery_attempt: DeliveryAttempt | None = None
    failure_code: str = ""
    binding: CapsuleBinding | None = None
    bound_provider_payload_json: str = ""
    rendered_token_estimate: int = 0
    decision_id: str = ""
    rendered_content_hash: str = ""
    evidence_manifest_hash: str = ""
    evidence_manifest_json: str = ""
    # ``(candidate_id, fact_class, cap_owner_ids)`` per delivered evidence, parallel to
    # ``evidence_ids``. The CAP owners are the ALREADY-AUTHORIZED byte-owner ids from the
    # record (`_authorized_cap_byte_owners`), never an inference from a flag or a layer name;
    # they let the grader prove a byte owner on the canonical route, which previously had no
    # `feature_ids`/`profile_member` stamp at all.
    # The canonical delivery row stamps these so an offline reader can join a capsule
    # delivery on the SAME ``(candidate_id, seal)`` contract as a lane delivery, and can
    # check the class against the registry from the row's OWN bytes (J6 self-evidence).
    # Empty on the failure/disabled constructors, which deliver nothing to identify.
    evidence_lineage: tuple[tuple[str, str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(
            self,
            "evidence_lineage",
            tuple(
                (str(key), str(fact), tuple(str(owner) for owner in owners))
                for key, fact, owners in self.evidence_lineage
            ),
        )
        if not self.observation_id or not self.model_call_id:
            raise ValueError("capsule compilation observation/model-call identity is required")
        if self.state is CapsuleCompilationState.COMPILED:
            if not self.capsule_text or not self.capsule_hash or self.delivery_attempt is None:
                raise ValueError("COMPILED capsule lacks content/delivery proof")
            _validate_sha256(self.capsule_hash, field_name="capsule_hash")
            _validate_sha256(
                self.rendered_content_hash,
                field_name="rendered_content_hash",
            )
            _validate_sha256(
                self.evidence_manifest_hash,
                field_name="evidence_manifest_hash",
            )
            if self.evidence_manifest_json:
                try:
                    manifest = json.loads(self.evidence_manifest_json)
                except (TypeError, ValueError) as exc:
                    raise ValueError("evidence_manifest_json must be valid JSON") from exc
                if (
                    _canonical_json(manifest) != self.evidence_manifest_json
                    or _sha256(self.evidence_manifest_json) != self.evidence_manifest_hash
                ):
                    raise ValueError("evidence manifest JSON/hash identity mismatch")
        if self.binding is not None:
            if (
                self.delivery_attempt is None
                or self.delivery_attempt.state
                not in {
                    DeliveryState.JOINED,
                    DeliveryState.DISPATCHED,
                    DeliveryState.PROVIDER_ACCEPTED,
                    DeliveryState.DELIVERED,
                    DeliveryState.RESPONSE_COMMITTED,
                    DeliveryState.INFERENCE_FAILED,
                    DeliveryState.CANCELLED,
                    DeliveryState.PARTIAL_OUTPUT,
                    DeliveryState.DISPATCH_FAILED,
                    DeliveryState.PROVIDER_REJECTED,
                    DeliveryState.RESPONSE_DISCARDED,
                }
                or not self.bound_provider_payload_json
            ):
                raise ValueError("bound capsule lacks joined payload proof")


def _capsule_binding_from_data(
    raw: Mapping[str, Any] | None,
) -> CapsuleBinding | None:
    if raw is None:
        return None
    return CapsuleBinding(
        model_call_id=str(raw["model_call_id"]),
        observation_id=str(raw["observation_id"]),
        decision_context=DecisionContext(raw["decision_context"]),
        evidence_ids=tuple(raw["evidence_ids"]),
        capsule_hash=str(raw["capsule_hash"]),
        provider_payload_hash=str(raw["provider_payload_hash"]),
        message_index=int(raw["message_index"]),
        content_index=int(raw["content_index"]),
        decision_id=str(raw.get("decision_id", "")),
        evidence_manifest_hash=str(raw.get("evidence_manifest_hash", "")),
        schema=str(raw.get("schema", "gt.capsule_binding.v1")),
    )


def _capsule_compilation_from_json(payload: str) -> CapsuleCompilation:
    raw = json.loads(payload)
    delivery_raw = raw.get("delivery_attempt")
    return CapsuleCompilation(
        state=CapsuleCompilationState(raw["state"]),
        native_observation=str(raw["native_observation"]),
        decision_context=DecisionContext(raw["decision_context"]),
        observation_id=str(raw["observation_id"]),
        source_model_call_id=str(raw["source_model_call_id"]),
        model_call_id=str(raw["model_call_id"]),
        evidence_ids=tuple(raw.get("evidence_ids", ())),
        capsule_text=str(raw.get("capsule_text", "")),
        capsule_hash=str(raw.get("capsule_hash", "")),
        overall_grade=EvidenceGrade(int(raw.get("overall_grade", 0))),
        delivery_attempt=(
            _delivery_attempt_from_json(
                json.dumps(
                    delivery_raw,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            if delivery_raw is not None
            else None
        ),
        failure_code=str(raw.get("failure_code", "")),
        binding=_capsule_binding_from_data(raw.get("binding")),
        bound_provider_payload_json=str(raw.get("bound_provider_payload_json", "")),
        rendered_token_estimate=int(raw.get("rendered_token_estimate", 0)),
        decision_id=str(raw.get("decision_id", "")),
        rendered_content_hash=str(raw.get("rendered_content_hash", "")),
        evidence_manifest_hash=str(raw.get("evidence_manifest_hash", "")),
        evidence_manifest_json=str(raw.get("evidence_manifest_json", "")),
        # A dropped identity would be invisible until a REPLAYED capsule silently lost its
        # join key, so the round-trip carries it like every other field. Malformed entries
        # are skipped rather than coerced -- a half-read pair is not an identity.
        evidence_lineage=tuple(
            (
                str(entry[0]),
                str(entry[1]),
                tuple(str(owner) for owner in entry[2])
                if isinstance(entry[2], (list, tuple))
                else (),
            )
            for entry in raw.get("evidence_lineage", ())
            if isinstance(entry, (list, tuple)) and len(entry) == 3
        ),
    )


def _failure_policy_state_from_json(payload: str) -> FailurePolicyState:
    raw = json.loads(payload)
    quarantine_reason = raw.get("quarantine_reason")
    return FailurePolicyState(
        attempt_id=str(raw["attempt_id"]),
        health=RuntimeHealthState(raw["health"]),
        assurance=AssuranceStatus(raw["assurance"]),
        isolated_components=tuple(raw.get("isolated_components", ())),
        recovery_attempted_signatures=tuple(raw.get("recovery_attempted_signatures", ())),
        last_verified_snapshot_id=str(raw.get("last_verified_snapshot_id", "")),
        gt_emission_enabled=bool(raw["gt_emission_enabled"]),
        gt_interruption_enabled=bool(raw["gt_interruption_enabled"]),
        gt_certification_enabled=bool(raw["gt_certification_enabled"]),
        native_path_enabled=bool(raw["native_path_enabled"]),
        quarantine_reason=(FaultCode(quarantine_reason) if quarantine_reason is not None else None),
        failed_event_id=str(raw.get("failed_event_id", "")),
    )


def _model_visible_provenance(
    provenance: Sequence[str],
) -> tuple[str, ...]:
    """Keep host-only runtime witness identities out of model-facing bytes."""

    return tuple(
        row for row in provenance if not row.startswith(("event:", "computation:", "diagnostic:"))
    )


def _render_decision_capsule(decision: OracleDecision) -> str:
    lines = [
        f"[GroundTruth · {decision.decision_context.value.replace('_', ' ')}]",
        "",
        "Decision",
        decision.primary_claim,
        "",
        "Evidence",
    ]
    for item in decision.coalition:
        lines.append(f"• [{item.grade.name}] {item.claim}")
        lines.append(f"  Action: {item.actionable_consequence}")
        # Canonical runtime/computation witness identities are host-only proof.
        # They bind the FACT journal and attestation but must never become model
        # context. Repository source provenance remains model-visible.
        for witness in _model_visible_provenance(item.provenance):
            lines.append(f"  Source: {witness}")
    return "\n".join(lines).rstrip() + "\n"


_CAPSULE_RESERVED_HEADING = re.compile(
    r"(?:^|\n)(?:Decision|Evidence|Uncertainty|Constraint|Impact)(?:\n|$)",
    re.IGNORECASE,
)
_PRODUCER_DISCLOSURE = re.compile(
    r"(?im)\b(?:producer(?:[\s_-]*id)?|feature[\s_-]*id)\s*[:=]\s*"
    r"([A-Za-z0-9_.-]+)"
)


def _normalized_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _evidence_manifest(decision: OracleDecision) -> Mapping[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "decision_context": decision.decision_context.value,
        "primary_claim": decision.primary_claim,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "subject": item.subject,
                "roles": [role.value for role in item.roles],
                "claim": item.claim,
                "actionable_consequence": item.actionable_consequence,
                "provenance": list(item.provenance),
                "grade": item.grade.name,
            }
            for item in decision.coalition
        ],
    }


def _validate_untrusted_capsule_fields(decision: OracleDecision) -> str | None:
    values = [decision.primary_claim]
    for item in decision.coalition:
        values.extend(
            (
                item.claim,
                item.actionable_consequence,
                *_model_visible_provenance(item.provenance),
            )
        )
    if any(_CAPSULE_RESERVED_HEADING.search(value) for value in values):
        return "UNSAFE_EVIDENCE_TEXT"
    return None


def _renderer_manifest_matches(
    capsule_text: str,
    decision: OracleDecision,
) -> bool:
    for item in decision.coalition:
        required = (
            item.claim,
            item.actionable_consequence,
            *_model_visible_provenance(item.provenance),
            f"[{item.grade.name}]",
        )
        if any(value not in capsule_text for value in required):
            return False
    return decision.primary_claim in capsule_text


def _failed_compilation(
    *,
    native_observation: str,
    decision: OracleDecision,
    observation_id: str,
    source_model_call_id: str,
    model_call_id: str,
    failure_code: str,
) -> CapsuleCompilation:
    return CapsuleCompilation(
        state=CapsuleCompilationState.FAILED,
        native_observation=native_observation,
        decision_context=decision.decision_context,
        observation_id=observation_id,
        source_model_call_id=source_model_call_id,
        model_call_id=model_call_id,
        overall_grade=EvidenceGrade.INFO,
        failure_code=failure_code,
    )


class UncalibratedCapsuleBudgetWarning(RuntimeWarning):
    """The capsule token budget is being enforced with an ESTIMATE, not real BPE.

    Raised ONCE per process when ``tiktoken`` cannot be loaded. See
    :func:`capsule_token_estimator_kind` for the machine-readable marker.
    """


# Resolved ONCE per process (was: re-imported + `get_encoding` on EVERY compile).
# ``None`` until first use; the pair is (encoding_or_None, kind_marker).
_CAPSULE_ENCODING: Any = None
_CAPSULE_ESTIMATOR_KIND: str = ""


def _resolve_capsule_token_estimator() -> tuple[Any, str]:
    """Resolve the capsule token counter once, and NAME which one won.

    THE DEGRADATION THIS MAKES LOUD (isolated 2026-07-29 on a clean install of
    only the declared deps): ``tiktoken`` is NOT a declared dependency of this
    package -- it arrives transitively via the ``benchmark`` extra (openai /
    litellm). Without it the old code silently fell back to
    ``len(capsule_text.encode("utf-8"))``. That byte count is ~4x a real cl100k
    count on ASCII, so EVERY capsule tripped ``hard_max_tokens`` ->
    ``CAPSULE_BUDGET_EXCEEDED`` -> no COMPILED delivery -> empty
    ``delivery_attempt_id`` -> evidence pinned at READY, never RELEASED. GT
    delivered ZERO bytes and said nothing about it. "Conservative upper bound"
    was fail-closed in the wrong direction: it does not degrade the budget, it
    deletes the product.

    The fallback is now the repo's OWN house estimate (``v1r_brief._estimate_tokens``,
    char/4) rather than a byte count, and the choice is recorded in a marker the
    same way ``v1r_brief._tokenizer_kind`` records its counter. char/4 is an
    APPROXIMATION and is documented as such in both places -- it can under- or
    over-count -- but it is within ~1x of truth instead of ~4x.
    """
    global _CAPSULE_ENCODING, _CAPSULE_ESTIMATOR_KIND
    if _CAPSULE_ESTIMATOR_KIND:
        return _CAPSULE_ENCODING, _CAPSULE_ESTIMATOR_KIND
    try:
        import tiktoken

        _CAPSULE_ENCODING = tiktoken.get_encoding("cl100k_base")
        _CAPSULE_ESTIMATOR_KIND = "tiktoken_cl100k_base"
    except Exception as exc:
        _CAPSULE_ENCODING = None
        _CAPSULE_ESTIMATOR_KIND = "char4_estimate"
        # One-shot, named, and machine-readable. Never silent again.
        warnings.warn(
            "GT capsule budget is UNCALIBRATED: tiktoken unavailable "
            f"({type(exc).__name__}: {exc}); falling back to the char/4 estimate. "
            "Install tiktoken for real cl100k budgeting.",
            UncalibratedCapsuleBudgetWarning,
            stacklevel=2,
        )
    return _CAPSULE_ENCODING, _CAPSULE_ESTIMATOR_KIND


def capsule_token_estimator_kind() -> str:
    """Marker for WHICH token counter the capsule budget uses in this process.

    ``"tiktoken_cl100k_base"`` (calibrated) or ``"char4_estimate"`` (degraded).
    Parallel to :func:`groundtruth.pretask.v1r_brief._tokenizer_kind`; read it
    when auditing whether a run's budget decisions were real counts.
    """
    return _resolve_capsule_token_estimator()[1]


def _estimate_capsule_tokens(capsule_text: str) -> int:
    """Real cl100k count when tiktoken is present; else the char/4 ESTIMATE."""
    encoding, _kind = _resolve_capsule_token_estimator()
    if encoding is not None:
        try:
            return len(encoding.encode(capsule_text, disallowed_special=()))
        except Exception:
            pass
    return len(capsule_text) // 4 + 1


def compile_observation_capsule(
    *,
    native_observation: str,
    decision: OracleDecision,
    observation_id: str,
    source_model_call_id: str,
    model_call_id: str,
    enabled: bool,
    renderer: Any = None,
    prior_compilations: Sequence[CapsuleCompilation] = (),
    token_counter: Any = None,
    role_driven: bool = False,
) -> CapsuleCompilation:
    """Compile one decision-complete coalition without mutating native bytes.

    ``role_driven`` must match the value given to the temporal gate and the coalition
    composer. This is the THIRD place that compared a record's provenance context against
    the open decision; with only the first two updated, an admitted out-of-context record
    reached here and the whole capsule failed ``MIXED_DECISION_CONTEXT``. Found by driving
    the installed path end to end, not by unit tests of the earlier two stages.
    """

    if not enabled:
        return CapsuleCompilation(
            state=CapsuleCompilationState.DISABLED,
            native_observation=native_observation,
            decision_context=decision.decision_context,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
        )
    if not model_call_id or model_call_id == source_model_call_id:
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="FRESH_MODEL_CALL_REQUIRED",
        )
    retryable_delivery_states = {
        DeliveryState.JOIN_FAILED,
        DeliveryState.DISPATCH_FAILED,
        DeliveryState.PROVIDER_REJECTED,
        DeliveryState.INFERENCE_FAILED,
        DeliveryState.CANCELLED,
        DeliveryState.RESPONSE_DISCARDED,
    }
    retry_candidates: list[CapsuleCompilation] = []
    for previous in prior_compilations:
        if previous.state is not CapsuleCompilationState.COMPILED:
            continue
        if previous.observation_id == observation_id:
            if (
                previous.delivery_attempt is not None
                and previous.delivery_attempt.state in retryable_delivery_states
                and previous.model_call_id != model_call_id
            ):
                retry_candidates.append(previous)
                continue
            return _failed_compilation(
                native_observation=native_observation,
                decision=decision,
                observation_id=observation_id,
                source_model_call_id=source_model_call_id,
                model_call_id=model_call_id,
                failure_code="OBSERVATION_ALREADY_HAS_CAPSULE",
            )
        if previous.model_call_id == model_call_id:
            return _failed_compilation(
                native_observation=native_observation,
                decision=decision,
                observation_id=observation_id,
                source_model_call_id=source_model_call_id,
                model_call_id=model_call_id,
                failure_code="MODEL_CALL_ALREADY_HAS_CAPSULE",
            )
    if (
        not decision.coalition
        or not decision.decision_complete
        or not decision.release_allowed
        or decision.unresolved_roles
    ):
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="DECISION_INCOMPLETE",
        )
    # ONE capsule serves ONE decision -- that invariant is what this guard protects, and it
    # still holds under role-driven eligibility because every coalition item was selected BY
    # `select_evidence_coalition` FOR this single active decision. What differs is only
    # PROVENANCE: an item may have been produced for a different decision and still be the
    # right thing to say now (a covering RED raised during recovery is exactly what a patch
    # decision needs). Comparing provenance here would re-impose the producer-identity
    # partition one stage later and fail the whole capsule.
    if not role_driven and any(
        item.decision_context is not decision.decision_context for item in decision.coalition
    ):
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="MIXED_DECISION_CONTEXT",
        )
    budget = capsule_budget_for(decision.decision_context)
    if decision.over_budget or decision.total_tokens > budget.hard_max_tokens:
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="CAPSULE_BUDGET_EXCEEDED",
        )
    unsafe_code = _validate_untrusted_capsule_fields(decision)
    if unsafe_code is not None:
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code=unsafe_code,
        )
    try:
        capsule_text = (
            renderer(decision) if renderer is not None else _render_decision_capsule(decision)
        )
    except Exception:
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="RENDERING_FAILED",
        )
    if not isinstance(capsule_text, str) or not capsule_text.strip():
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="RENDERING_FAILED",
        )
    if "<gt-" in capsule_text.lower():
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="PRODUCER_IDENTITY_LEAK",
        )
    producer_identities = {
        _normalized_identity(item.feature_id) for item in decision.coalition if item.feature_id
    }
    disclosed = {
        _normalized_identity(match.group(1))
        for match in _PRODUCER_DISCLOSURE.finditer(capsule_text)
    }
    if producer_identities.intersection(disclosed):
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="PRODUCER_IDENTITY_LEAK",
        )
    if not _renderer_manifest_matches(capsule_text, decision):
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="EVIDENCE_MANIFEST_MISMATCH",
        )
    if token_counter is None:
        rendered_token_estimate = _estimate_capsule_tokens(capsule_text)
    else:
        rendered_token_estimate = int(token_counter(capsule_text))
        if rendered_token_estimate < 0:
            raise ValueError("token_counter returned a negative count")
    if rendered_token_estimate > budget.hard_max_tokens:
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="CAPSULE_BUDGET_EXCEEDED",
        )
    rendered_content_hash = _sha256(capsule_text)
    manifest_json = _canonical_json(_evidence_manifest(decision))
    evidence_manifest_hash = _sha256(manifest_json)
    capsule_hash = _sha256(
        _canonical_json(
            {
                # v3 (2026-07-28): BUMPED because `_evidence_manifest` gained a `subject` key,
                # which changes `evidence_manifest_hash` and therefore this preimage. Leaving the
                # label at v2 while the preimage moved is exactly the C5 defect one layer up --
                # an unversioned hash change that makes old and new digests silently
                # incomparable. `capsule_hash` is not decorative: it flows into
                # `observation_candidate_id` -> `candidate_dedup_sha256` -> `opportunity_id`, and
                # `retry_candidates` compares `previous.capsule_hash != capsule_hash` to detect
                # RETRY_CAPSULE_MISMATCH. A silent preimage change therefore breaks joins AND
                # manufactures spurious retry mismatches for any capsule compiled before it.
                #
                # Model-visible bytes are unaffected: `capsule_text` and its
                # `rendered_content_hash` are untouched, and `_renderer_manifest_matches` does not
                # consult `subject`. This is a JOIN-KEY version bump, not a delivery change.
                #
                # From the exported constant -- see DECISION_CAPSULE_SCHEMA for why this must never
                # be a literal again. Readers import it and recompute against the same label.
                "schema": DECISION_CAPSULE_SCHEMA,
                "rendered_content_hash": rendered_content_hash,
                "evidence_manifest_hash": evidence_manifest_hash,
            }
        )
    )
    if any(previous.capsule_hash != capsule_hash for previous in retry_candidates):
        return _failed_compilation(
            native_observation=native_observation,
            decision=decision,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            failure_code="RETRY_CAPSULE_MISMATCH",
        )
    delivery = advance_delivery(
        DeliveryAttempt(
            evidence_ids=tuple(item.evidence_id for item in decision.coalition),
            capsule_hash=capsule_hash,
            model_call_id=model_call_id,
        ),
        DeliveryState.COMPILED,
        observation_id=observation_id,
    )
    return CapsuleCompilation(
        state=CapsuleCompilationState.COMPILED,
        native_observation=native_observation,
        decision_context=decision.decision_context,
        observation_id=observation_id,
        source_model_call_id=source_model_call_id,
        model_call_id=model_call_id,
        evidence_ids=delivery.evidence_ids,
        # Carry each delivered record's identity FORWARD onto the artifact. `feature_id` is
        # `lineage.fact_class` for every canonical record (the only two constructors are the
        # envelope conversion, which sets it from the lineage, and the JSON rehydration).
        # A record whose id this module did not mint contributes no pair rather than a
        # guessed one -- correct-or-quiet applied to identity.
        evidence_lineage=tuple(
            (
                _dedup_key_from_evidence_id(item.evidence_id),
                item.feature_id,
                tuple(item.owner_feature_ids),
            )
            for item in decision.coalition
            if _dedup_key_from_evidence_id(item.evidence_id) and item.feature_id
        ),
        capsule_text=capsule_text,
        capsule_hash=capsule_hash,
        overall_grade=decision.overall_grade,
        delivery_attempt=delivery,
        rendered_token_estimate=rendered_token_estimate,
        decision_id=decision.decision_id,
        rendered_content_hash=rendered_content_hash,
        evidence_manifest_hash=evidence_manifest_hash,
        evidence_manifest_json=manifest_json,
    )


def _provider_payload_hash(payload: Any) -> str:
    return _sha256(_canonical_provider_payload_json(payload))


def _canonical_provider_payload_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def bind_capsule_to_final_payload(
    compilation: CapsuleCompilation,
    final_payload: Mapping[str, Any],
) -> CapsuleCompilation:
    """Bind an exact capsule content block in the final outbound payload."""

    if (
        compilation.state is not CapsuleCompilationState.COMPILED
        or compilation.delivery_attempt is None
    ):
        raise ValueError("capsule must be COMPILED before exact payload binding")
    matches: list[tuple[int, int]] = []
    messages = final_payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("final payload has no structural messages")
    for message_index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content == compilation.capsule_text:
                matches.append((message_index, 0))
            continue
        if not isinstance(content, list):
            continue
        for content_index, block in enumerate(content):
            if (
                isinstance(block, Mapping)
                and block.get("type") == "text"
                and block.get("text") == compilation.capsule_text
            ):
                matches.append((message_index, content_index))
    if not matches:
        raise ValueError("exact capsule is absent from final provider payload")
    if len(matches) != 1:
        raise ValueError("multiple exact capsule matches make binding ambiguous")

    payload_json = _canonical_provider_payload_json(final_payload)
    payload_hash = _sha256(payload_json)
    joined = advance_delivery(
        compilation.delivery_attempt,
        DeliveryState.JOINED,
        joined_capsule_hash=compilation.capsule_hash,
        provider_payload_hash=payload_hash,
    )
    message_index, content_index = matches[0]
    binding = CapsuleBinding(
        model_call_id=compilation.model_call_id,
        observation_id=compilation.observation_id,
        decision_context=compilation.decision_context,
        evidence_ids=compilation.evidence_ids,
        capsule_hash=compilation.capsule_hash,
        provider_payload_hash=payload_hash,
        message_index=message_index,
        content_index=content_index,
        decision_id=compilation.decision_id,
        evidence_manifest_hash=compilation.evidence_manifest_hash,
    )
    return replace(
        compilation,
        delivery_attempt=joined,
        binding=binding,
        bound_provider_payload_json=payload_json,
    )


def verify_bound_payload_at_dispatch(
    compilation: CapsuleCompilation,
    final_payload: Mapping[str, Any],
) -> CapsuleCompilation:
    if (
        compilation.delivery_attempt is None
        or compilation.delivery_attempt.state is not DeliveryState.JOINED
        or compilation.binding is None
    ):
        raise ValueError("payload dispatch requires an exact JOINED capsule binding")
    payload_json = _canonical_provider_payload_json(final_payload)
    payload_hash = _sha256(payload_json)
    if (
        payload_json != compilation.bound_provider_payload_json
        or payload_hash != compilation.binding.provider_payload_hash
    ):
        raise ValueError("provider payload mutated after exact join/hash binding")
    return replace(
        compilation,
        delivery_attempt=advance_delivery(
            compilation.delivery_attempt,
            DeliveryState.DISPATCHED,
        ),
    )


def _evidence_ref(record: EvidenceRecord) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=record.evidence_id,
        feature_id=record.feature_id,
        decision_context=record.decision_context,
        roles=record.roles,
        claim=record.claim,
        actionable_consequence=record.actionable_consequence,
        provenance=record.provenance,
        grade=record.grade,
        owner_feature_ids=record.owner_feature_ids,
        subject=record.subject,
    )


def _role_order(role: EvidenceRole) -> int:
    return list(EvidenceRole).index(role)


def _grade_for_required_links(
    selected: Sequence[EvidenceRecord],
    required_roles: Sequence[EvidenceRole],
) -> EvidenceGrade:
    links = [
        item.grade
        for item in selected
        if (item.mandatory_reason is not None or any(role in required_roles for role in item.roles))
    ]
    return min(links) if links else EvidenceGrade.INFO


def _evidence_revision_is_fresh(
    evidence: EvidenceRecord,
    current_revision: RevisionVector,
) -> bool:
    for dependency in evidence.revision_dependencies:
        # Immutable-state dependencies are always satisfied: nothing that can move during an
        # attempt can falsify them. See `_IMMUTABLE_REVISION_DEPENDENCIES` for why `issue`
        # qualifies and why the mutable runtime deps deliberately do not.
        if dependency in _IMMUTABLE_REVISION_DEPENDENCIES:
            continue
        dimension = _REVISION_DEPENDENCY_DIMENSION.get(dependency)
        if dimension is None:
            return False
        if getattr(evidence.revision, dimension) != getattr(current_revision, dimension):
            return False
    return True


def select_evidence_coalition(
    decision: ActiveDecision,
    evidence: Iterable[EvidenceRecord],
    *,
    reasoning_graph: ReasoningGraph | None = None,
    role_driven: bool = False,
    acquired_subjects: Iterable[str] = (),
) -> OracleDecision:
    """Select one smallest connected, decision-complete evidence coalition.

    ``role_driven`` decides what makes evidence ELIGIBLE for the open decision.

    ``False`` (default, byte-identical): a record must have been produced FOR this decision
    context, so the pool is partitioned by producer identity.

    ``True``: eligibility follows the roles the decision actually declares it needs, and
    ``decision_context`` becomes provenance. Measured motivation -- under the partition,
    12 of 25 required/useful role slots have no reachable carrier and PATCH_CONSTRUCTION
    can never hold more than ``{caller_contract, obligations}``, so most of what the
    ``useful_roles`` tables ask for is unreachable.

    ``acquired_subjects`` is native WorkState truth, not provider visibility. It
    suppresses only target-identity evidence whose normalized repository subject
    was already viewed or edited; additive contract and validation evidence about
    the same file remains eligible.

    This relaxes NOTHING else. Role fit, connectivity, freshness, already-visible,
    supersession, duplicate-claim dedup, the token budget and -- decisively --
    decision-completeness all still apply: a coalition without a record carrying the
    decision's REQUIRED role still does not complete.

    Deliberately a parameter rather than an environment read: this function must stay pure
    so replay reconstructs identical release/suppression decisions.
    """

    suppressed: dict[str, SuppressionRecord] = {}
    eligible: list[EvidenceRecord] = []
    acquired = frozenset(
        normalized
        for subject in acquired_subjects
        if (normalized := _normalize_repository_subject(subject))
    )
    neighborhood = {
        node for node in decision.causal_neighborhood if not node.startswith("decision:")
    }

    evidence_items = tuple(evidence)
    evidence_ids = tuple(item.evidence_id for item in evidence_items)
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("duplicate evidence_id in coalition input")

    for item in sorted(evidence_items, key=lambda row: row.evidence_id):
        reason: SuppressionReason | None = None
        item_neighborhood = {
            node for node in item.causal_neighborhood if not node.startswith("decision:")
        }
        if item.mandatory_reason is MandatoryReason.TASK_OBLIGATION:
            item_neighborhood.add("obligation:task")
        shared_neighborhood = neighborhood.intersection(item_neighborhood)
        if not role_driven and item.decision_context is not decision.context:
            # Provenance-only under role-driven eligibility: what the decision NEEDS is
            # stated by its required/useful roles, checked below, not by which producer
            # happened to raise the evidence.
            reason = SuppressionReason.OTHER_DECISION
        elif (
            reasoning_graph is not None
            and not shared_neighborhood
            and not any(
                reasoning_graph.connected(decision_node, evidence_node)
                for decision_node in decision.causal_neighborhood
                for evidence_node in item_neighborhood
            )
        ):
            reason = SuppressionReason.DISCONNECTED
        elif reasoning_graph is None and not shared_neighborhood:
            reason = SuppressionReason.DISCONNECTED
        elif item.lifecycle not in {
            EvidenceLifecycle.READY,
            EvidenceLifecycle.ACTIVE,
        }:
            reason = SuppressionReason.NOT_READY
        elif not item.fresh or not _evidence_revision_is_fresh(item, decision.current_revision):
            reason = SuppressionReason.STALE
        elif item.already_visible or decision.decision_id in item.visible_to_decision_ids:
            reason = SuppressionReason.ALREADY_VISIBLE
        elif (
            EvidenceRole.TARGET_IDENTITY in item.roles
            and _normalize_repository_subject(item.subject) in acquired
        ):
            reason = SuppressionReason.ALREADY_ACQUIRED
        elif item.superseded:
            reason = SuppressionReason.SUPERSEDED
        elif item.mandatory_reason is None and not set(item.roles).intersection(
            set(decision.required_roles) | set(decision.useful_roles)
        ):
            reason = SuppressionReason.NOT_ACTIONABLE_FOR_DECISION
        if reason is not None:
            suppressed[item.evidence_id] = SuppressionRecord(item.evidence_id, reason)
        else:
            eligible.append(item)

    grouped: dict[tuple[str, str], list[EvidenceRecord]] = {}
    for item in eligible:
        fingerprint = (
            " ".join(item.claim.lower().split()),
            " ".join(item.actionable_consequence.lower().split()),
        )
        grouped.setdefault(fingerprint, []).append(item)
    deduped: list[EvidenceRecord] = []
    for items in grouped.values():
        ranked = sorted(
            items,
            key=lambda item: (
                -(1 if item.mandatory_reason is not None else 0),
                -int(item.grade),
                -item.failure_prevention,
                -item.causal_value,
                -item.contradiction_resolution,
                item.anchoring_risk,
                item.token_cost,
                item.evidence_id,
            ),
        )
        deduped.append(ranked[0])
        for duplicate in ranked[1:]:
            suppressed[duplicate.evidence_id] = SuppressionRecord(
                duplicate.evidence_id,
                SuppressionReason.DUPLICATE_CLAIM,
            )
    eligible = sorted(deduped, key=lambda item: item.evidence_id)

    mandatory = sorted(
        (item for item in eligible if item.mandatory_reason is not None),
        key=lambda item: (
            list(MandatoryReason).index(item.mandatory_reason),
            item.evidence_id,
        ),
    )
    selected: list[EvidenceRecord] = []
    selected_roles: set[EvidenceRole] = set()
    selected_subject_roles: set[tuple[str, EvidenceRole]] = set()
    selected_consequences: set[str] = set()
    total_tokens = 0

    for item in mandatory:
        selected.append(item)
        selected_roles.update(item.roles)
        selected_subject_roles.update((item.subject, role) for role in item.roles)
        selected_consequences.add(" ".join(item.actionable_consequence.lower().split()))
        total_tokens += item.token_cost

    remaining = [
        item
        for item in eligible
        if item.evidence_id not in {selected_item.evidence_id for selected_item in selected}
        and item.evidence_id not in suppressed
    ]

    while remaining:
        if total_tokens >= decision.token_budget:
            for item in remaining:
                suppressed[item.evidence_id] = SuppressionRecord(
                    item.evidence_id,
                    SuppressionReason.BUDGET,
                )
            remaining = []
            break
        unresolved_required = set(decision.required_roles) - selected_roles
        decision_candidates = (
            [item for item in remaining if unresolved_required.intersection(item.roles)]
            if unresolved_required
            else remaining
        )
        scored: list[tuple[float, int, str, EvidenceRecord]] = []
        for item in decision_candidates:
            new_roles = set(item.roles) - selected_roles
            consequence_key = " ".join(item.actionable_consequence.lower().split())
            same_role_unique_action = (
                bool(set(item.roles).intersection(selected_roles))
                and consequence_key not in selected_consequences
                and any((item.subject, role) not in selected_subject_roles for role in item.roles)
            )
            if not new_roles and not same_role_unique_action:
                suppressed[item.evidence_id] = SuppressionRecord(
                    item.evidence_id,
                    SuppressionReason.REDUNDANT_ROLE,
                )
                continue
            role_coverage = sum(4 if role in decision.required_roles else 1 for role in new_roles)
            if same_role_unique_action:
                role_coverage += 1
            raw_value = (
                role_coverage
                + item.causal_value
                + item.failure_prevention
                + item.contradiction_resolution
                + int(item.grade)
                - item.anchoring_risk
            )
            value_per_token = raw_value / max(item.token_cost, 1)
            scored.append(
                (
                    value_per_token,
                    raw_value,
                    item.evidence_id,
                    item,
                )
            )
        if not scored:
            break
        _, raw_value, _, winner = max(
            scored,
            key=lambda row: (row[0], row[1], -len(row[2]), row[2]),
        )
        if raw_value <= 0:
            suppressed[winner.evidence_id] = SuppressionRecord(
                winner.evidence_id,
                SuppressionReason.NON_POSITIVE_VALUE,
            )
        elif total_tokens + winner.token_cost > decision.token_budget:
            suppressed[winner.evidence_id] = SuppressionRecord(
                winner.evidence_id,
                SuppressionReason.BUDGET,
            )
        else:
            selected.append(winner)
            selected_roles.update(winner.roles)
            selected_subject_roles.update((winner.subject, role) for role in winner.roles)
            selected_consequences.add(" ".join(winner.actionable_consequence.lower().split()))
            total_tokens += winner.token_cost
        remaining = [
            item
            for item in remaining
            if item.evidence_id != winner.evidence_id and item.evidence_id not in suppressed
        ]

    if remaining:
        unresolved_required = set(decision.required_roles) - selected_roles
        reason = (
            SuppressionReason.INCOMPLETE_DECISION
            if unresolved_required
            else SuppressionReason.NON_POSITIVE_VALUE
        )
        for item in remaining:
            suppressed.setdefault(
                item.evidence_id,
                SuppressionRecord(item.evidence_id, reason),
            )

    coverage = tuple(sorted(selected_roles, key=_role_order))
    unresolved = tuple(role for role in decision.required_roles if role not in selected_roles)
    decision_complete = not unresolved
    over_budget = total_tokens > decision.token_budget
    ordered_suppressions = tuple(suppressed[key] for key in sorted(suppressed))
    return OracleDecision(
        decision_id=decision.decision_id,
        decision_context=decision.context,
        primary_claim=decision.primary_claim,
        coalition=tuple(_evidence_ref(item) for item in selected),
        mandatory_items=tuple(
            item.evidence_id for item in selected if item.mandatory_reason is not None
        ),
        suppressed=ordered_suppressions,
        total_tokens=total_tokens,
        coverage=coverage,
        unresolved_roles=unresolved,
        overall_grade=(
            _grade_for_required_links(selected, decision.required_roles)
            if decision_complete
            else EvidenceGrade.INFO
        ),
        decision_complete=decision_complete,
        release_allowed=bool(selected) and decision_complete and not over_budget,
        over_budget=over_budget,
    )


@dataclass(frozen=True)
class InferencePlan:
    """One attempt-runtime handoff into the sole provider boundary."""

    active_decision: ActiveDecision
    oracle_decision: OracleDecision
    compilation: CapsuleCompilation
    delivery_attempt_id: str
    held_evidence_ids: tuple[str, ...]
    suppressed_decision_ids: tuple[str, ...]
    native_observation: str
    assurance: AssuranceStatus


class AttemptReasoningRuntime:
    """Attempt-scoped owner of the canonical causal reasoning pipeline.

    This class intentionally performs orchestration only. Pure reducers,
    temporal contracts, coalition selection, compilation, delivery proof, and
    failure assurance remain independently testable functions.
    """

    def __init__(
        self,
        *,
        attempt_id: str,
        journal: RuntimeJournal,
        initial_revision: RevisionVector,
        role_driven_coalition: bool = False,
    ):
        if not attempt_id:
            raise ValueError("attempt_id is required")
        self.attempt_id = attempt_id
        self.journal = journal
        # Resolved ONCE by the caller (the seam reads the environment) and held as
        # construction state, so the runtime remains a pure function of its inputs and
        # replay reconstructs identical release/suppression decisions. Reading the
        # environment deeper in the pipeline would make replay depend on ambient state.
        # Both the temporal gate and the coalition composer must receive the SAME value:
        # if only one honours it, evidence is HELD upstream and the other never sees it.
        self.role_driven_coalition = bool(role_driven_coalition)
        self._initial_revision = initial_revision
        self.work_state = WorkState.initial(
            attempt_id=attempt_id,
            revision=initial_revision,
        )
        self.reasoning_graph = ReasoningGraph.initial(
            attempt_id=attempt_id,
            revision=initial_revision,
        )
        self.failure_state = FailurePolicyState.initial(attempt_id=attempt_id)
        self._evidence: dict[str, EvidenceRecord] = {}
        self._compilations: dict[str, CapsuleCompilation] = {}
        self._delivery_attempt_ids: dict[str, str] = {}

        # Reconstruct both projections from the same committed event truth.
        events = self.journal.events(attempt_id)
        if events and events[0].revision_before != initial_revision:
            raise StateIntegrityError(
                "attempt initial revision conflicts with canonical event truth"
            )
        for event in events:
            self.work_state = reduce_event(self.work_state, event)
            self.reasoning_graph = reduce_reasoning_event(
                self.reasoning_graph,
                event=event,
            )
        for evidence in self.journal.evidence_records_for_attempt(attempt_id):
            if evidence.evidence_id in self._evidence:
                raise StateIntegrityError("duplicate evidence identity during reconstruction")
            self._evidence[evidence.evidence_id] = evidence
        for delivery_attempt_id, compilation in self.journal.compilations_for_attempt(attempt_id):
            history = self.journal.delivery_history(delivery_attempt_id)
            if not history or compilation.delivery_attempt != history[-1]:
                raise StateIntegrityError("compilation/delivery journal reconstruction mismatch")
            if compilation.model_call_id in self._delivery_attempt_ids:
                raise StateIntegrityError("duplicate model-call identity during reconstruction")
            self._compilations[delivery_attempt_id] = compilation
            self._delivery_attempt_ids[compilation.model_call_id] = delivery_attempt_id
        if set(self.journal.delivery_attempt_ids_for_attempt(attempt_id)) != set(
            self._compilations
        ):
            raise StateIntegrityError(
                "orphan delivery or compilation journal during reconstruction"
            )
        failure_history = self.journal.failure_history(attempt_id)
        if failure_history:
            self.failure_state = failure_history[-1]
        else:
            self.journal.append_failure_state(self.failure_state)

    def append_event(self, event: CanonicalEvent) -> None:
        """Commit the causal event before deriving either runtime projection."""

        if event.attempt_id != self.attempt_id:
            raise StateIntegrityError("attempt runtime event identity mismatch")
        self.journal.append(event)
        self.work_state = reduce_event(self.work_state, event)
        self.reasoning_graph = reduce_reasoning_event(
            self.reasoning_graph,
            event=event,
        )
        self.journal.save_snapshot(self.work_state)

    def ingest_evidence(self, evidence: EvidenceRecord) -> None:
        _validate_evidence_byte_owners(evidence)
        existing = self._evidence.get(evidence.evidence_id)
        if existing is not None:
            merged = _merge_same_evidence_generation(existing, evidence)
            if merged == existing:
                return
            self.journal.append_evidence(
                merged,
                attempt_id=self.attempt_id,
            )
            self._evidence[evidence.evidence_id] = merged
            return
        self.journal.append_evidence(
            evidence,
            attempt_id=self.attempt_id,
        )
        self._evidence[evidence.evidence_id] = evidence

    def evidence_record(self, evidence_id: str) -> EvidenceRecord:
        return self._evidence[evidence_id]

    def _persist_evidence(self, evidence: EvidenceRecord) -> None:
        self.journal.append_evidence(
            evidence,
            attempt_id=self.attempt_id,
        )
        self._evidence[evidence.evidence_id] = evidence

    @staticmethod
    def _empty_oracle(decision: ActiveDecision) -> OracleDecision:
        return OracleDecision(
            decision_id=decision.decision_id,
            decision_context=decision.context,
            primary_claim=decision.primary_claim,
            coalition=(),
            mandatory_items=(),
            suppressed=(),
            total_tokens=0,
            coverage=(),
            unresolved_roles=decision.required_roles,
            overall_grade=EvidenceGrade.INFO,
            decision_complete=False,
            release_allowed=False,
            over_budget=False,
        )

    def _disabled_plan(
        self,
        *,
        decisions: Sequence[ActiveDecision],
        native_observation: str,
        observation_id: str,
        source_model_call_id: str,
        model_call_id: str,
    ) -> InferencePlan:
        if not decisions:
            raise ValueError("at least one active decision candidate is required")
        active = tuple(decisions)[0]
        oracle = self._empty_oracle(active)
        compilation = compile_observation_capsule(
            role_driven=self.role_driven_coalition,
            native_observation=native_observation,
            decision=oracle,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            enabled=False,
        )
        return InferencePlan(
            active_decision=active,
            oracle_decision=oracle,
            compilation=compilation,
            delivery_attempt_id="",
            held_evidence_ids=tuple(sorted(self._evidence)),
            suppressed_decision_ids=tuple(item.decision_id for item in tuple(decisions)[1:]),
            native_observation=native_observation,
            assurance=self.failure_state.assurance,
        )

    def _refresh_standing_obligation_generations(
        self,
        active: ActiveDecision,
    ) -> None:
        """Advance standing task evidence without rewinding provider proof."""

        if not self.role_driven_coalition:
            return
        generation = decision_window_generation(
            attempt_id=self.attempt_id,
            decision_context=active.context,
            decision_window_key=self.work_state.decision_window_key,
        )
        roots = tuple(
            item
            for item in sorted(self._evidence.values(), key=lambda row: row.evidence_id)
            if (
                item.feature_id == "obligations"
                and item.mandatory_reason is MandatoryReason.TASK_OBLIGATION
                and not item.standing_source_evidence_id
            )
        )
        for source in roots:
            prior_generation = source.decision_window_generation
            if not prior_generation:
                # Backward-compatible rows and newly ingested producer records
                # acquire their first operational window without changing
                # lifecycle, claim, provenance, or model-facing bytes.
                self.ingest_evidence(
                    replace(
                        source,
                        decision_window_generation=generation,
                    )
                )
                continue
            if prior_generation == generation:
                continue
            if source.lifecycle in {
                EvidenceLifecycle.DISCOVERED,
                EvidenceLifecycle.PENDING,
                EvidenceLifecycle.READY,
                EvidenceLifecycle.HELD,
            }:
                # No provider-proven dose exists yet. Move the operational
                # window marker and let the original generation compete.
                self.ingest_evidence(
                    replace(
                        source,
                        decision_window_generation=generation,
                    )
                )
                continue
            if (
                # P2-1 (2026-07-29, ARCH-D lever 1): SOURCE_UNDERSTANDING joins
                # PATCH_CONSTRUCTION. Its REQUIRED role is BEHAVIORAL_CONTRACT and
                # its only STANDING carrier is this record — with the guard
                # PATCH_CONSTRUCTION-only, every SOURCE_UNDERSTANDING window after
                # the task-start dose starved (1,529 of 1,533 unresolved-
                # BEHAVIORAL_CONTRACT compilation failures on run 30478454517),
                # which is the head of the commitment-boundary withhold loop.
                # PATCH_PROPAGATION and the other contexts stay excluded (pinned
                # by test_non_patch_decision_does_not_rematerialize_delivered_obligation).
                active.context
                in {
                    DecisionContext.PATCH_CONSTRUCTION,
                    DecisionContext.SOURCE_UNDERSTANDING,
                }
                and source.lifecycle
                in {
                    EvidenceLifecycle.RELEASED,
                    EvidenceLifecycle.DELIVERED,
                    EvidenceLifecycle.ACTIVE,
                    EvidenceLifecycle.SATISFIED,
                }
            ):
                rematerialized = rematerialize_task_obligation(
                    source,
                    generation=generation,
                )
                # Deterministic identity makes this idempotent within a window;
                # ingest_evidence preserves an already-advanced clone lifecycle.
                self.ingest_evidence(rematerialized)

    def prepare_next_inference(
        self,
        *,
        decisions: Sequence[ActiveDecision],
        satisfied_predicates: frozenset[TemporalPredicate],
        commitment_window: CommitmentWindowState | None = None,
        available_substrates: Sequence[str],
        native_observation: str,
        observation_id: str,
        source_model_call_id: str,
        model_call_id: str,
    ) -> InferencePlan:
        """Prepare one inference; the legacy global window input is intentionally inert."""
        decision_candidates = tuple(decisions)
        if not decision_candidates:
            raise ValueError("at least one active decision candidate is required")
        if not self.failure_state.gt_emission_enabled:
            return self._disabled_plan(
                decisions=decision_candidates,
                native_observation=native_observation,
                observation_id=observation_id,
                source_model_call_id=source_model_call_id,
                model_call_id=model_call_id,
            )

        # Candidate order is a priority, not a guarantee that candidate zero
        # can form a complete coalition. Prepare standing evidence for every
        # candidate before arbitration so a fallback patch decision is not
        # starved by an incomplete earlier decision.
        for candidate in decision_candidates:
            self._refresh_standing_obligation_generations(candidate)

        # Readiness is decision-independent. Advance it once, then arbitrate
        # exactly one active decision over that stable evidence snapshot.
        ready_records: list[EvidenceRecord] = []
        for evidence in sorted(self._evidence.values(), key=lambda item: item.evidence_id):
            current = invalidate_stale_evidence(
                evidence,
                current_revision=self.work_state.revision,
            )
            if current != evidence:
                self._persist_evidence(current)
            if current.lifecycle is EvidenceLifecycle.PENDING:
                contract = feature_contract_for(current.feature_id)
                if contract is None:
                    continue
                readiness_missing = tuple(
                    predicate
                    for predicate in contract.ready_predicates
                    if predicate not in satisfied_predicates
                )
                if not readiness_missing:
                    current = transition_evidence(
                        current,
                        EvidenceLifecycle.READY,
                        reason_code=(EvidenceTransitionReason.READINESS_RULES_SATISFIED),
                    )
                    self._persist_evidence(current)
            ready_records.append(current)

        evaluated: list[
            tuple[
                ActiveDecision,
                OracleDecision,
                dict[str, TemporalContractEvaluation],
            ]
        ] = []
        for active in decision_candidates:
            # Temporal evaluation is the scheduler gate. The selector receives
            # READY items; release is applied only after this decision wins.
            scheduled: list[EvidenceRecord] = []
            temporal_evaluations: dict[str, TemporalContractEvaluation] = {}
            for evidence in ready_records:
                contract = feature_contract_for(evidence.feature_id)
                if contract is None:
                    continue
                evaluation = _evaluate_current_decision_contract(
                    contract,
                    evidence,
                    TemporalRuntimeContext(
                        active_decision=active,
                        current_revision=self.work_state.revision,
                        commitment_window=CommitmentWindowState.NOT_OPEN,
                        satisfied_predicates=satisfied_predicates,
                        available_substrates=tuple(available_substrates),
                    ),
                    role_driven=self.role_driven_coalition,
                )
                temporal_evaluations[evidence.evidence_id] = evaluation
                if evaluation.release_allowed:
                    scheduled.append(
                        replace(
                            evidence,
                            lifecycle=EvidenceLifecycle.READY,
                        )
                    )
                else:
                    scheduled.append(
                        replace(
                            evidence,
                            # Selector eligibility is intentionally narrower
                            # than lifecycle persistence: an item that is
                            # READY but outside its release window must remain
                            # READY in storage while appearing ineligible to
                            # the coalition optimizer for this decision.
                            lifecycle=(
                                EvidenceLifecycle.HELD
                                if evaluation.next_lifecycle
                                in {
                                    EvidenceLifecycle.READY,
                                    EvidenceLifecycle.ACTIVE,
                                }
                                else evaluation.next_lifecycle
                            ),
                        )
                    )
            evaluated.append(
                (
                    active,
                    select_evidence_coalition(
                        active,
                        scheduled,
                        reasoning_graph=self.reasoning_graph,
                        role_driven=self.role_driven_coalition,
                        acquired_subjects=(
                            *self.work_state.viewed_files,
                            *self.work_state.edited_files,
                        ),
                    ),
                    temporal_evaluations,
                )
            )

        active, oracle, winning_evaluations = next(
            (pair for pair in evaluated if pair[1].release_allowed and pair[1].decision_complete),
            evaluated[0],
        )
        coalition_ids = {item.evidence_id for item in oracle.coalition}
        held_ids: list[str] = []
        for evidence in ready_records:
            current = self._evidence[evidence.evidence_id]
            evaluation = winning_evaluations.get(evidence.evidence_id)
            if (
                evaluation is not None
                and evaluation.next_lifecycle
                in {
                    EvidenceLifecycle.EXPIRED,
                    EvidenceLifecycle.INVALIDATED,
                }
                and current.lifecycle is not evaluation.next_lifecycle
            ):
                current = transition_evidence(
                    current,
                    evaluation.next_lifecycle,
                    reason_code=(
                        evaluation.reason or EvidenceTransitionReason.DECISION_WINDOW_EXPIRED
                    ),
                )
                self._persist_evidence(current)
            elif (
                evidence.evidence_id not in coalition_ids
                and current.lifecycle is EvidenceLifecycle.READY
                and (
                    evaluation is None
                    or evaluation.next_lifecycle is EvidenceLifecycle.HELD
                    or evaluation.release_allowed
                )
            ):
                current = transition_evidence(
                    current,
                    EvidenceLifecycle.HELD,
                    reason_code=(EvidenceTransitionReason.OTHER_DECISION_CURRENTLY_ACTIVE),
                )
                self._persist_evidence(current)
                held_ids.append(current.evidence_id)
            elif current.lifecycle is EvidenceLifecycle.HELD:
                held_ids.append(current.evidence_id)

        # Persist the full eligible-pool suppression record, not only coalition
        # losers from the winning decision.
        winner_suppressions = {item.evidence_id: item for item in oracle.suppressed}
        for evidence in ready_records:
            if (
                evidence.evidence_id not in coalition_ids
                and evidence.evidence_id not in winner_suppressions
            ):
                winner_suppressions[evidence.evidence_id] = SuppressionRecord(
                    evidence.evidence_id,
                    SuppressionReason.OTHER_DECISION,
                )
        oracle = replace(
            oracle,
            suppressed=tuple(winner_suppressions[key] for key in sorted(winner_suppressions)),
        )
        self.journal.append_oracle(self.attempt_id, oracle)

        compilation = compile_observation_capsule(
            role_driven=self.role_driven_coalition,
            native_observation=native_observation,
            decision=oracle,
            observation_id=observation_id,
            source_model_call_id=source_model_call_id,
            model_call_id=model_call_id,
            enabled=True,
            prior_compilations=tuple(self._compilations.values()),
        )
        delivery_attempt_id = ""
        if (
            compilation.state is CapsuleCompilationState.COMPILED
            and compilation.delivery_attempt is not None
        ):
            released_records: list[EvidenceRecord] = []
            for evidence_id in sorted(coalition_ids):
                current = self._evidence[evidence_id]
                if current.lifecycle in {
                    EvidenceLifecycle.READY,
                    EvidenceLifecycle.HELD,
                }:
                    current = transition_evidence(
                        current,
                        EvidenceLifecycle.RELEASED,
                        reason_code=(EvidenceTransitionReason.DECISION_WINDOW_OPEN),
                    )
                    released_records.append(current)
            delivery_attempt_id = f"delivery:{model_call_id}"
            self._commit_compilation_transition(
                delivery_attempt_id,
                compilation,
                evidence_updates=tuple(released_records),
            )
        return InferencePlan(
            active_decision=active,
            oracle_decision=oracle,
            compilation=compilation,
            delivery_attempt_id=delivery_attempt_id,
            held_evidence_ids=tuple(sorted(set(held_ids))),
            suppressed_decision_ids=tuple(
                item.decision_id
                for item in decision_candidates
                if item.decision_id != active.decision_id
            ),
            native_observation=native_observation,
            assurance=self.failure_state.assurance,
        )

    def _compilation_for(self, delivery_attempt_id: str) -> CapsuleCompilation:
        try:
            return self._compilations[delivery_attempt_id]
        except KeyError as exc:
            raise ValueError("unknown delivery attempt") from exc

    def _commit_compilation_transition(
        self,
        delivery_attempt_id: str,
        compilation: CapsuleCompilation,
        *,
        evidence_updates: Sequence[EvidenceRecord] = (),
    ) -> None:
        """Journal a coherent transition before advancing memory projections."""

        self.journal.append_compilation_transition(
            delivery_attempt_id,
            compilation,
            attempt_id=self.attempt_id,
            evidence_updates=evidence_updates,
        )
        self._compilations[delivery_attempt_id] = compilation
        self._delivery_attempt_ids[compilation.model_call_id] = delivery_attempt_id
        for evidence in evidence_updates:
            self._evidence[evidence.evidence_id] = evidence

    def bind_provider_payload(
        self,
        delivery_attempt_id: str,
        payload: Mapping[str, Any],
    ) -> DeliveryAttempt:
        compilation = bind_capsule_to_final_payload(
            self._compilation_for(delivery_attempt_id),
            payload,
        )
        assert compilation.delivery_attempt is not None
        self._commit_compilation_transition(
            delivery_attempt_id,
            compilation,
        )
        return compilation.delivery_attempt

    def mark_dispatched(
        self,
        delivery_attempt_id: str,
        payload: Mapping[str, Any],
    ) -> DeliveryAttempt:
        compilation = verify_bound_payload_at_dispatch(
            self._compilation_for(delivery_attempt_id),
            payload,
        )
        assert compilation.delivery_attempt is not None
        self._commit_compilation_transition(
            delivery_attempt_id,
            compilation,
        )
        return compilation.delivery_attempt

    def mark_provider_accepted(
        self,
        delivery_attempt_id: str,
        *,
        provider_response_id: str,
    ) -> DeliveryAttempt:
        compilation = self._compilation_for(delivery_attempt_id)
        assert compilation.delivery_attempt is not None
        accepted = advance_delivery(
            compilation.delivery_attempt,
            DeliveryState.PROVIDER_ACCEPTED,
            provider_response_id=provider_response_id,
        )
        next_compilation = replace(
            compilation,
            delivery_attempt=accepted,
        )
        self._commit_compilation_transition(
            delivery_attempt_id,
            next_compilation,
        )
        return accepted

    def record_provider_terminal(
        self,
        delivery_attempt_id: str,
        model_call: ModelCallAttempt,
    ) -> DeliveryAttempt:
        compilation = self._compilation_for(delivery_attempt_id)
        assert compilation.delivery_attempt is not None
        terminal = globals()["record_provider_terminal"](
            compilation.delivery_attempt,
            model_call,
        )
        next_compilation = replace(
            compilation,
            delivery_attempt=terminal,
        )
        evidence_updates: list[EvidenceRecord] = []
        if terminal.state is DeliveryState.DELIVERED:
            for evidence_id in terminal.evidence_ids:
                evidence = self._evidence[evidence_id]
                delivered = transition_evidence(
                    evidence,
                    EvidenceLifecycle.DELIVERED,
                    reason_code=(EvidenceTransitionReason.PROVIDER_TERMINAL_DELIVERY_PROVEN),
                    delivery_attempt=terminal,
                )
                evidence_updates.append(delivered)
        self._commit_compilation_transition(
            delivery_attempt_id,
            next_compilation,
            evidence_updates=tuple(evidence_updates),
        )
        return terminal

    def record_delivery_withheld(
        self,
        delivery_attempt_id: str,
        *,
        reason: str,
    ) -> DeliveryAttempt:
        """Persist a DELIBERATE measurement holdout on the canonical attempt.

        Same atomic commit path as every other delivery transition, on purpose: a holdout that
        lived only in memory would vanish on replay/resume, making the arm unmeasurable exactly
        when someone tried to measure it offline. It is NOT routed through
        `record_delivery_failure` -- a holdout is terminal but not a failure, and mixing the two
        would put a measurement decision into failure accounting and the release gate.
        """
        compilation = self._compilation_for(delivery_attempt_id)
        assert compilation.delivery_attempt is not None
        withheld = globals()["record_delivery_withheld"](
            compilation.delivery_attempt,
            reason=reason,
        )
        next_compilation = replace(
            compilation,
            delivery_attempt=withheld,
        )
        self._commit_compilation_transition(
            delivery_attempt_id,
            next_compilation,
        )
        return withheld

    def record_delivery_failure(
        self,
        delivery_attempt_id: str,
        state: DeliveryState,
        *,
        reason: str,
    ) -> DeliveryAttempt:
        """Persist a transport/provider failure on the canonical attempt."""

        compilation = self._compilation_for(delivery_attempt_id)
        assert compilation.delivery_attempt is not None
        failed = globals()["record_delivery_failure"](
            compilation.delivery_attempt,
            state,
            reason=reason,
        )
        next_compilation = replace(
            compilation,
            delivery_attempt=failed,
        )
        self._commit_compilation_transition(
            delivery_attempt_id,
            next_compilation,
        )
        return failed

    def commit_provider_response(
        self,
        delivery_attempt_id: str,
        *,
        response_hash: str,
    ) -> DeliveryAttempt:
        """Persist that a delivered provider response entered the trajectory."""

        compilation = self._compilation_for(delivery_attempt_id)
        assert compilation.delivery_attempt is not None
        committed = commit_response(
            compilation.delivery_attempt,
            response_hash=response_hash,
        )
        next_compilation = replace(
            compilation,
            delivery_attempt=committed,
        )
        evidence_updates: list[EvidenceRecord] = []
        for evidence_id in committed.evidence_ids:
            evidence = self._evidence[evidence_id]
            if evidence.lifecycle is not EvidenceLifecycle.DELIVERED:
                raise StateIntegrityError(
                    "response commitment requires provider-delivered evidence"
                )
            evidence_updates.append(
                transition_evidence(
                    evidence,
                    EvidenceLifecycle.ACTIVE,
                    reason_code=(EvidenceTransitionReason.ACTIVATED_AFTER_PROVIDER_DELIVERY),
                )
            )
        self._commit_compilation_transition(
            delivery_attempt_id,
            next_compilation,
            evidence_updates=tuple(evidence_updates),
        )
        return committed

    def discard_provider_response(
        self,
        delivery_attempt_id: str,
        *,
        reason: str,
    ) -> DeliveryAttempt:
        """Persist failure to parse or commit an already delivered response."""

        return self.record_delivery_failure(
            delivery_attempt_id,
            DeliveryState.RESPONSE_DISCARDED,
            reason=reason,
        )

    def _reconstruct_canonical_state(
        self,
    ) -> tuple[
        WorkState,
        ReasoningGraph,
        dict[str, EvidenceRecord],
        dict[str, CapsuleCompilation],
        dict[str, str],
    ]:
        """Rebuild every canonical projection from append-only journal truth."""

        events = self.journal.events(self.attempt_id)
        work_state = WorkState.initial(
            attempt_id=self.attempt_id,
            revision=self._initial_revision,
        )
        reasoning_graph = ReasoningGraph.initial(
            attempt_id=self.attempt_id,
            revision=self._initial_revision,
        )
        for event in events:
            work_state = reduce_event(work_state, event)
            reasoning_graph = reduce_reasoning_event(
                reasoning_graph,
                event=event,
            )

        if events:
            snapshot, tail = self.journal.load_snapshot_and_tail(self.attempt_id)
            replayed = snapshot
            for event in tail:
                replayed = reduce_event(replayed, event)
            if replayed != work_state:
                raise StateIntegrityError("snapshot plus committed tail diverges from full replay")

        evidence = {
            item.evidence_id: item
            for item in self.journal.evidence_records_for_attempt(self.attempt_id)
        }
        compilations: dict[str, CapsuleCompilation] = {}
        delivery_attempt_ids: dict[str, str] = {}
        delivered_evidence: set[str] = set()
        for delivery_attempt_id, compilation in self.journal.compilations_for_attempt(
            self.attempt_id
        ):
            history = self.journal.delivery_history(delivery_attempt_id)
            if not history or compilation.delivery_attempt != history[-1]:
                raise StateIntegrityError("compilation/delivery state diverges during replay")
            if compilation.model_call_id in delivery_attempt_ids:
                raise StateIntegrityError("model-call identity reused during replay")
            compilations[delivery_attempt_id] = compilation
            delivery_attempt_ids[compilation.model_call_id] = delivery_attempt_id
            if any(is_delivered(item) for item in history):
                delivered_evidence.update(history[-1].evidence_ids)
        if set(self.journal.delivery_attempt_ids_for_attempt(self.attempt_id)) != set(compilations):
            raise StateIntegrityError("orphan delivery or compilation journal during replay")
        for item in evidence.values():
            if (
                item.lifecycle
                in {
                    EvidenceLifecycle.DELIVERED,
                    EvidenceLifecycle.ACTIVE,
                    EvidenceLifecycle.SATISFIED,
                    EvidenceLifecycle.SUPERSEDED,
                }
                and item.evidence_id not in delivered_evidence
            ):
                raise StateIntegrityError(
                    "evidence delivery lifecycle has no provider-terminal proof"
                )
        return (
            work_state,
            reasoning_graph,
            evidence,
            compilations,
            delivery_attempt_ids,
        )

    def recovery_input(self) -> RecoveryInput:
        events = self.journal.events(self.attempt_id)
        if events:
            snapshot, tail = self.journal.load_snapshot_and_tail(self.attempt_id)
            snapshot_id = f"{self.attempt_id}:{snapshot.sequence}"
            snapshot_state_hash = snapshot.state_hash
            committed_event_ids = tuple(event.event_id for event in tail)
            committed_tail_hash = events[-1].content_hash
        else:
            initial = WorkState.initial(
                attempt_id=self.attempt_id,
                revision=self._initial_revision,
            )
            snapshot_id = f"{self.attempt_id}:0"
            snapshot_state_hash = initial.state_hash
            committed_event_ids = ()
            committed_tail_hash = _sha256("")
        return RecoveryInput(
            snapshot_id=snapshot_id,
            snapshot_state_hash=snapshot_state_hash,
            committed_event_ids=committed_event_ids,
            committed_tail_hash=committed_tail_hash,
        )

    def handle_fault(
        self,
        fault: RuntimeFault,
        *,
        recover: Any,
    ) -> FailurePolicyState:
        request = self.recovery_input()
        prior = self.failure_state
        recovered_proof: RecoveryProof | None = None

        def recover_and_capture(value: RecoveryInput) -> Any:
            nonlocal recovered_proof
            candidate = recover(value)
            if isinstance(candidate, RecoveryProof):
                recovered_proof = candidate
            return candidate

        disposition = apply_failure_policy(
            self.failure_state,
            fault,
            recovery_input=request,
            recover=recover_and_capture,
        )
        should_reconstruct = (
            fault.code in CORE_CORRUPTION_CODES
            and fault.signature not in prior.recovery_attempted_signatures
            and disposition.health
            in {
                RuntimeHealthState.RECOVERED,
                RuntimeHealthState.DEGRADED,
            }
        )
        if should_reconstruct:
            try:
                reconstructed = self._reconstruct_canonical_state()
                if (
                    recovered_proof is None
                    or recovered_proof.recovered_state_hash != reconstructed[0].state_hash
                ):
                    raise StateIntegrityError("recovery proof state hash does not match replay")
                if disposition.last_verified_snapshot_id != request.snapshot_id:
                    raise StateIntegrityError("recovery disposition lost snapshot identity")
                (
                    self.work_state,
                    self.reasoning_graph,
                    self._evidence,
                    self._compilations,
                    self._delivery_attempt_ids,
                ) = reconstructed
            except Exception:
                disposition = apply_failure_policy(
                    disposition,
                    fault,
                    recovery_input=request,
                    recover=lambda _request: None,
                )
        self.failure_state = disposition
        self.journal.append_failure_state(self.failure_state)
        return self.failure_state

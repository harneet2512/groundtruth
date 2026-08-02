"""Terminal deterministic-evidence carriers for bounded runtime decisions.

These types do not discover facts.  They bind existing producer output to the
minimum identity needed to state honest semantics at delivery time.  In
particular, advisory evidence never becomes a blocker and an open configuration
slice never claims exact coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Sequence


class EvidenceStatus(str, Enum):
    EXACT = "exact"
    SOUND_OVERAPPROX = "sound_overapprox"
    EXECUTION_SPECIFIC = "execution_specific"
    INCOMPLETE = "incomplete"
    ADVISORY = "advisory"
    UNSUPPORTED = "unsupported"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or (len(normalized) > 1 and normalized[1] == ":")
        or ".." in normalized.split("/")
    ):
        raise ValueError("path must be a non-empty repository-relative path")
    return normalized


@dataclass(frozen=True)
class SyntaxReceipt:
    """Result of checking exact postimage bytes with a named producer."""

    path: str
    source_sha256: str
    source_bytes_length: int
    repository_revision: str
    configuration_sha256: str
    producer: str
    producer_version: str
    status: EvidenceStatus
    verdict: str
    native_diagnostics: bytes
    native_diagnostics_sha256: str

    @classmethod
    def build(
        cls,
        *,
        path: str,
        source_bytes: bytes,
        repository_revision: str,
        configuration_sha256: str,
        producer: str,
        producer_version: str,
        status: EvidenceStatus,
        verdict: str,
        native_diagnostics: bytes,
    ) -> "SyntaxReceipt":
        if not isinstance(source_bytes, bytes) or not isinstance(native_diagnostics, bytes):
            raise TypeError("source and diagnostics must be bytes")
        if not _is_sha256(configuration_sha256):
            raise ValueError("configuration_sha256 must be a lowercase SHA-256")
        if not all((repository_revision, producer, producer_version, verdict)):
            raise ValueError("revision, producer, version, and verdict are required")
        return cls(
            path=_path(path),
            source_sha256=_sha256_bytes(source_bytes),
            source_bytes_length=len(source_bytes),
            repository_revision=repository_revision,
            configuration_sha256=configuration_sha256,
            producer=producer,
            producer_version=producer_version,
            status=status,
            verdict=verdict,
            native_diagnostics=native_diagnostics,
            native_diagnostics_sha256=_sha256_bytes(native_diagnostics),
        )


_OBLIGATION_STATES = frozenset({"open", "evidenced", "satisfied", "invalidated"})


def stable_obligation_id(
    *, task_sha256: str, start_byte: int, end_byte: int, parser_version: str
) -> str:
    if not _is_sha256(task_sha256):
        raise ValueError("task_sha256 must be a lowercase SHA-256")
    if start_byte < 0 or end_byte <= start_byte or not parser_version:
        raise ValueError("invalid obligation source span or parser version")
    payload = json.dumps(
        [task_sha256, start_byte, end_byte, parser_version],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, order=True)
class ObligationState:
    obligation_id: str
    state: str
    evidence_revision: str
    task_anchor: str

    def __post_init__(self) -> None:
        if not _is_sha256(self.obligation_id):
            raise ValueError("obligation_id must be a SHA-256")
        if self.state not in _OBLIGATION_STATES:
            raise ValueError("unsupported obligation state")
        if not self.evidence_revision or not self.task_anchor:
            raise ValueError("obligation revision and task anchor are required")


@dataclass(frozen=True)
class ObligationDelta:
    changed: tuple[ObligationState, ...]
    removed_ids: tuple[str, ...]


def diff_obligations(
    previous: Sequence[ObligationState], current: Sequence[ObligationState]
) -> ObligationDelta:
    before = {item.obligation_id: item for item in previous}
    after = {item.obligation_id: item for item in current}
    changed = tuple(after[key] for key in sorted(after) if before.get(key) != after[key])
    removed = tuple(sorted(set(before) - set(after)))
    return ObligationDelta(changed=changed, removed_ids=removed)


def obligation_states_from_issue(
    issue_text: str,
    *,
    task_revision: str,
    parser_version: str = "groundtruth.issue_obligations.v1",
) -> tuple[ObligationState, ...]:
    """Parse issue obligations and bind each one to its exact UTF-8 source span."""

    from groundtruth.evidence.issue_obligations import extract_issue_obligations

    if not task_revision:
        raise ValueError("task_revision is required")
    task_bytes = issue_text.encode("utf-8", "surrogatepass")
    task_sha = _sha256_bytes(task_bytes)
    states = []
    for item in extract_issue_obligations(issue_text):
        if item.end_byte <= item.start_byte or item.end_byte > len(task_bytes):
            continue
        oid = stable_obligation_id(
            task_sha256=task_sha,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            parser_version=parser_version,
        )
        states.append(
            ObligationState(
                obligation_id=oid,
                state="open",
                evidence_revision=task_revision,
                task_anchor=f"task:{item.start_byte}-{item.end_byte}",
            )
        )
    return tuple(sorted(states))


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DURATION_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:ms|s)(?!\w)", re.IGNORECASE)
_SPACE_RE = re.compile(r"[ \t]+")


def _normalize_diagnostics(value: str) -> str:
    text = _ANSI_RE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = _DURATION_RE.sub("<duration>", line)
        lines.append(_SPACE_RE.sub(" ", line).strip())
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


@dataclass(frozen=True)
class FailureIdentity:
    action: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]
    pre_state_revision: str
    exit_code: int | None
    signal: int | None
    diagnostic_sha256: str

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            {
                "action": self.action,
                "cwd": self.cwd,
                "environment": self.environment,
                "pre_state_revision": self.pre_state_revision,
                "exit_code": self.exit_code,
                "signal": self.signal,
                "diagnostic_sha256": self.diagnostic_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256_bytes(payload)

    @classmethod
    def build(
        cls,
        *,
        action: Sequence[str],
        cwd: str,
        environment: Mapping[str, str],
        pre_state_revision: str,
        exit_code: int | None,
        signal: int | None,
        diagnostics: str,
    ) -> "FailureIdentity":
        argv = tuple(str(item) for item in action)
        if not argv or not cwd or not pre_state_revision:
            raise ValueError("action, cwd, and pre-state revision are required")
        normalized = _normalize_diagnostics(diagnostics).encode("utf-8", "surrogatepass")
        return cls(
            action=argv,
            cwd=cwd.replace("\\", "/"),
            environment=tuple(sorted((str(key), str(value)) for key, value in environment.items())),
            pre_state_revision=pre_state_revision,
            exit_code=exit_code,
            signal=signal,
            diagnostic_sha256=_sha256_bytes(normalized),
        )


@dataclass(frozen=True)
class RecoveryOutcome:
    identity: FailureIdentity
    remedy: str
    outcome: str


class FailureRecoveryLedger:
    """In-memory exact-identity recovery lookup; similar failures never match."""

    def __init__(self) -> None:
        self._records: dict[FailureIdentity, RecoveryOutcome] = {}

    def record(self, identity: FailureIdentity, *, remedy: str, outcome: str) -> None:
        if not remedy or not outcome:
            raise ValueError("remedy and outcome are required")
        self._records[identity] = RecoveryOutcome(identity, remedy, outcome)

    def lookup(self, identity: FailureIdentity) -> RecoveryOutcome | None:
        return self._records.get(identity)


class TerminalEvidenceSession:
    """Episode-local obligation delivery and exact-failure memory.

    The session emits only obligation deltas and delegates recovery lookup to an
    exact :class:`FailureIdentity`; it never treats similar diagnostics as reusable.
    """

    def __init__(self, obligations: Sequence[ObligationState]) -> None:
        self._obligations = {item.obligation_id: item for item in obligations}
        self._delivered_obligations: tuple[ObligationState, ...] = ()
        self._recovery = FailureRecoveryLedger()

    @classmethod
    def from_issue(cls, issue_text: str, *, task_revision: str) -> "TerminalEvidenceSession":
        return cls(obligation_states_from_issue(issue_text, task_revision=task_revision))

    def obligation_delta(self) -> ObligationDelta:
        current = tuple(self._obligations[key] for key in sorted(self._obligations))
        delta = diff_obligations(self._delivered_obligations, current)
        self._delivered_obligations = current
        return delta

    def set_obligation_state(
        self, obligation_id: str, state: str, evidence_revision: str
    ) -> ObligationState:
        current = self._obligations[obligation_id]
        updated = replace(current, state=state, evidence_revision=evidence_revision)
        self._obligations[obligation_id] = updated
        return updated

    def record_failure(
        self, identity: FailureIdentity, *, remedy: str, outcome: str
    ) -> None:
        self._recovery.record(identity, remedy=remedy, outcome=outcome)

    def recovery_for(self, identity: FailureIdentity) -> RecoveryOutcome | None:
        return self._recovery.lookup(identity)


def bind_episode_terminal_evidence(
    episode_state: object,
    *,
    issue_text: str,
    task_revision: str,
) -> TerminalEvidenceSession:
    """Install terminal evidence beside the product-owned obligation tracker.

    The existing ``obligations`` slot may contain ``ObligationTracker`` and is
    deliberately not replaced.  EpisodeState is not slotted, so this private
    episode-local handle can coexist without changing serialized value identity.
    """

    if not hasattr(episode_state, "failure_fingerprints"):
        raise TypeError("object is not an episode state")
    session = TerminalEvidenceSession.from_issue(issue_text, task_revision=task_revision)
    setattr(episode_state, "_terminal_evidence_session", session)
    return session


def record_episode_failure(
    episode_state: object,
    identity: FailureIdentity,
    *,
    remedy: str,
    outcome: str,
) -> RecoveryOutcome:
    """Record exact failure identity in both terminal and legacy session views."""

    session = getattr(episode_state, "_terminal_evidence_session", None)
    if not isinstance(session, TerminalEvidenceSession):
        raise ValueError("terminal evidence session is not bound")
    session.record_failure(identity, remedy=remedy, outcome=outcome)
    fingerprints = getattr(episode_state, "failure_fingerprints", None)
    if isinstance(fingerprints, set):
        fingerprints.add(identity.sha256)
    record = {
        "failure_identity_sha256": identity.sha256,
        "pre_state_revision": identity.pre_state_revision,
        "diagnostic_sha256": identity.diagnostic_sha256,
        "remedy": remedy,
        "outcome": outcome,
    }
    if hasattr(episode_state, "last_failure_record"):
        setattr(episode_state, "last_failure_record", record)
    result = session.recovery_for(identity)
    if result is None:  # defensive: record() must make the exact key retrievable
        raise RuntimeError("exact failure record was not retained")
    return result


@dataclass(frozen=True)
class BuildConfigurationSlice:
    adapter: str
    configuration_id: str
    inputs_sha256: str
    status: EvidenceStatus
    coverage_closed: bool
    targets: tuple[str, ...]
    source_membership: tuple[str, ...]
    generated_inputs: tuple[str, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    omissions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.adapter or not self.configuration_id or not _is_sha256(self.inputs_sha256):
            raise ValueError("adapter, configuration, and inputs hash are required")
        if self.status is EvidenceStatus.EXACT and (not self.coverage_closed or self.omissions):
            raise ValueError("exact configuration evidence requires closed coverage without omissions")
        for path in (*self.source_membership, *self.generated_inputs):
            _path(path)


_BUILD_SCAN_LIMIT = 20_000


def detect_build_configuration_slices(
    repository_root: str | os.PathLike[str],
) -> tuple[BuildConfigurationSlice, ...]:
    """Detect common build manifests without claiming target/configuration closure.

    Detection reuses the runtime language-profile adapters. Source membership is
    a deterministic filesystem overapproximation; declared targets and generated
    inputs remain explicit omissions until a build-system-specific parser exists.
    """

    from groundtruth.runtime.repo_adapters import adapters_for_repo

    root = Path(repository_root).resolve()
    if not root.is_dir():
        return ()
    rows: list[BuildConfigurationSlice] = []
    for adapter in adapters_for_repo(str(root)):
        manifests = tuple(
            sorted(name for name in adapter.manifests if (root / name).is_file())
        )
        if not manifests and adapter.name == "generic":
            continue
        digest = hashlib.sha256()
        for name in manifests:
            data = (root / name).read_bytes()
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
        members: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if not adapter.is_generated_or_vendor(
                    str((Path(dirpath) / name / "placeholder").relative_to(root))
                )
                and name not in {".git", ".hg", ".svn"}
            )
            for filename in sorted(filenames):
                rel = (Path(dirpath) / filename).relative_to(root).as_posix()
                if adapter.is_source_file(rel):
                    members.append(rel)
                    if len(members) >= _BUILD_SCAN_LIMIT:
                        break
            if len(members) >= _BUILD_SCAN_LIMIT:
                break
        omissions = ["declared_targets_not_resolved", "target_membership_not_resolved"]
        if len(members) >= _BUILD_SCAN_LIMIT:
            omissions.append("source_scan_limit_reached")
        rows.append(
            BuildConfigurationSlice(
                adapter=adapter.name,
                configuration_id="detected-default",
                inputs_sha256=digest.hexdigest(),
                status=EvidenceStatus.SOUND_OVERAPPROX,
                coverage_closed=False,
                targets=(),
                source_membership=tuple(members),
                generated_inputs=(),
                dependency_edges=(),
                omissions=tuple(omissions),
            )
        )
    return tuple(sorted(rows, key=lambda item: item.adapter))


@dataclass(frozen=True)
class NewFilePrecedent:
    new_path: str
    precedent_path: str
    revision: str
    reasons: tuple[str, ...]
    score: float
    status: EvidenceStatus = EvidenceStatus.ADVISORY

    @classmethod
    def build(
        cls,
        *,
        new_path: str,
        precedent_path: str,
        revision: str,
        reasons: Sequence[str],
        score: float,
    ) -> "NewFilePrecedent":
        if not revision or not reasons or not 0.0 <= score <= 1.0:
            raise ValueError("revision, reasons, and a bounded score are required")
        return cls(
            new_path=_path(new_path),
            precedent_path=_path(precedent_path),
            revision=revision,
            reasons=tuple(str(item) for item in reasons),
            score=float(score),
        )


@dataclass(frozen=True)
class SubmitBlocker:
    blocker_id: str
    producer: str
    witness: str
    scope: str
    creating_revision: str
    invalidation_rule: str
    invalidation_key: str
    status: EvidenceStatus
    scope_closed: bool
    fresh_at_registration: bool
    resolved: bool = False
    remediation: str = ""

    @property
    def suppression_eligible(self) -> bool:
        return (
            self.status is EvidenceStatus.EXACT
            and self.scope_closed
            and self.fresh_at_registration
            and not self.resolved
        )


class ClosedBlockerRegistry:
    """Closed-world suppression authority with an immediate enforce kill switch."""

    def __init__(self, *, enforce: bool = False) -> None:
        self.enforce = enforce
        self._blockers: dict[str, SubmitBlocker] = {}

    def register(
        self,
        *,
        blocker_id: str,
        producer: str,
        witness: str,
        scope: str,
        creating_revision: str,
        current_revision: str,
        invalidation_rule: str,
        invalidation_key: str,
        status: EvidenceStatus,
        scope_closed: bool,
    ) -> SubmitBlocker:
        if not all(
            (blocker_id, producer, witness, scope, creating_revision, invalidation_rule)
        ):
            raise ValueError("blocker identity, witness, scope, and revision are required")
        if not _is_sha256(invalidation_key):
            raise ValueError("invalidation_key must be a SHA-256")
        blocker = SubmitBlocker(
            blocker_id=blocker_id,
            producer=producer,
            witness=witness,
            scope=scope,
            creating_revision=creating_revision,
            invalidation_rule=invalidation_rule,
            invalidation_key=invalidation_key,
            status=status,
            scope_closed=scope_closed,
            fresh_at_registration=creating_revision == current_revision,
        )
        self._blockers[blocker_id] = blocker
        return blocker

    def resolve(self, blocker_id: str, *, remediation: str) -> SubmitBlocker:
        current = self._blockers[blocker_id]
        resolved = replace(current, resolved=True, remediation=remediation)
        self._blockers[blocker_id] = resolved
        return resolved

    def should_suppress(
        self,
        current_revision: str,
        current_invalidation_keys: Mapping[str, str],
    ) -> bool:
        if not self.enforce:
            return False
        return any(
            blocker.suppression_eligible
            and blocker.creating_revision == current_revision
            and current_invalidation_keys.get(blocker.blocker_id) == blocker.invalidation_key
            for blocker in self._blockers.values()
        )

    def open_blockers(self) -> tuple[SubmitBlocker, ...]:
        return tuple(
            self._blockers[key]
            for key in sorted(self._blockers)
            if not self._blockers[key].resolved
        )


@dataclass(frozen=True)
class SubmitSuppressionReceipt:
    schema: str
    repository_revision: str
    action_sha256: str
    provider_payload_sha256: str
    blocker_ids: tuple[str, ...]
    provider_dispatched: bool = False
    chars_delivered: int = 0


def compile_submit_suppression(
    *,
    registry: ClosedBlockerRegistry,
    current_revision: str,
    current_invalidation_keys: Mapping[str, str],
    action_bytes: bytes,
    provider_payload_bytes: bytes,
) -> SubmitSuppressionReceipt | None:
    """Authorize suppression and bind proof that no submit bytes were dispatched."""

    if provider_payload_bytes:
        raise ValueError("submit suppression requires zero provider payload bytes")
    if not registry.should_suppress(current_revision, current_invalidation_keys):
        return None
    blockers = tuple(
        blocker.blocker_id
        for blocker in registry.open_blockers()
        if blocker.suppression_eligible
        and blocker.creating_revision == current_revision
        and current_invalidation_keys.get(blocker.blocker_id) == blocker.invalidation_key
    )
    if not blockers:
        return None
    return SubmitSuppressionReceipt(
        schema="gt.submit_suppression_receipt.v1",
        repository_revision=current_revision,
        action_sha256=_sha256_bytes(action_bytes),
        provider_payload_sha256=_sha256_bytes(provider_payload_bytes),
        blocker_ids=blockers,
    )

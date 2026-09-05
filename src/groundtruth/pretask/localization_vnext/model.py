"""Canonical, render-neutral data model for localization vNext.

The objects in this module are deliberately independent of every model-visible
renderer.  They are safe to persist as a shadow artifact and stable enough to
compare across processes and revisions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class CandidateAction(str, Enum):
    ADMIT = "ADMIT"
    REJECT = "REJECT"
    DEFER = "DEFER"


class EvidenceFamily(str, Enum):
    EXPLICIT_PATH = "explicit_path"
    TRACEBACK = "traceback"
    IDENTIFIER = "identifier"
    LEXICAL = "lexical"
    NODE_FTS = "node_fts"
    BODY_BM25 = "body_bm25"
    SEMANTIC = "semantic"
    GRAPH = "graph"
    PROPERTY = "property"
    HISTORY = "history"
    RUNTIME = "runtime"
    ECOSYSTEM = "ecosystem"
    TEST_LINK = "test_link"


class ReasonCode(str, Enum):
    NEW_MANDATORY_CERTIFIED = "new_mandatory_certified"
    NEW_MANDATORY_INDEPENDENT = "new_mandatory_independent"
    NEW_EXPECTED = "new_expected"
    NEW_PATH_OR_FACT = "new_path_or_fact"
    INDEPENDENT_CONFIRMATION = "independent_confirmation"
    REDUNDANT = "redundant"
    NO_ISSUE_CONTRIBUTION = "no_issue_conditioned_contribution"
    BELOW_CONFIDENCE = "below_confidence"
    DUPLICATE = "duplicate"
    WRAPPER_OR_PASS_THROUGH = "wrapper_or_pass_through"
    UNSUPPORTED_RELATION = "unsupported_relation"
    TEST_ONLY_SURFACE = "test_only_surface"
    WRONG_REPOSITORY = "wrong_repository"
    PREVIOUSLY_REJECTED = "previously_rejected"
    TOKEN_RAIL = "token_rail"
    CANDIDATE_RAIL = "candidate_rail"


@dataclass(frozen=True)
class LocalizationPolicy:
    confidence_floor: float = 0.5
    certified_floor: float = 0.9
    max_candidates: int = 500
    max_source_tokens: int = 16_000
    max_region_tokens: int = 2_000
    merge_adjacent_lines: int = 2
    disabled_components: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LocalizationState:
    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    unresolved_roles: tuple[str, ...] = ()
    decision_reasons: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class LocalizationRequest:
    issue_text: str
    repository_root: str
    graph_db: str
    revision_identity: str
    policy: LocalizationPolicy = LocalizationPolicy()
    prior_state: LocalizationState | None = None
    new_evidence: tuple["EvidenceUnit", ...] = ()


@dataclass(frozen=True)
class BehaviorFacet:
    issue_mode: str = "behavior_described"
    actor: str = ""
    operation: str = ""
    state: str = ""
    transition: str = ""
    invariant: str = ""
    observed_behavior: str = ""
    expected_behavior: str = ""
    architectural_boundary: str = ""
    policies: tuple[str, ...] = ()
    anchor_symbols: tuple[str, ...] = ()
    obligation_ids: tuple[str, ...] = ()
    required_roles: tuple[str, ...] = ()
    expected_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityMatrix:
    available: Mapping[str, bool]
    unavailable: Mapping[str, str]
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceUnit:
    evidence_id: str
    file_path: str
    symbol: str
    start_line: int
    end_line: int
    family: EvidenceFamily
    relation: str
    confidence: float
    provenance: tuple[str, ...]
    roles: tuple[str, ...]
    source_tokens: int
    signal_class: str
    signal_rank: int
    fact_span: bool = False
    explicit_provenance: bool = False
    metadata: tuple[tuple[str, str], ...] = ()

    @classmethod
    def create(
        cls,
        *,
        file_path: str,
        symbol: str = "",
        start_line: int = 0,
        end_line: int = 0,
        family: EvidenceFamily = EvidenceFamily.LEXICAL,
        relation: str = "",
        confidence: float = 0.0,
        provenance: tuple[str, ...] = (),
        roles: tuple[str, ...] = (),
        source_tokens: int = 0,
        signal_class: str = "lexical",
        signal_rank: int = 1,
        fact_span: bool = False,
        explicit_provenance: bool = False,
        metadata: tuple[tuple[str, str], ...] = (),
    ) -> "EvidenceUnit":
        fp = _norm_path(file_path)
        identity = {
            "file": fp,
            "symbol": symbol,
            "span": [int(start_line or 0), int(end_line or 0)],
            "family": family.value,
            "relation": relation,
            "provenance": list(provenance),
            "roles": sorted(set(roles)),
        }
        digest = hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
        return cls(
            evidence_id=f"ev_{digest}",
            file_path=fp,
            symbol=symbol or "",
            start_line=max(0, int(start_line or 0)),
            end_line=max(0, int(end_line or 0)),
            family=family,
            relation=relation or "",
            confidence=round(max(0.0, min(1.0, float(confidence))), 8),
            provenance=tuple(str(v) for v in provenance),
            roles=tuple(sorted(set(str(v) for v in roles if v))),
            source_tokens=max(0, int(source_tokens or 0)),
            signal_class=signal_class or family.value,
            signal_rank=max(1, int(signal_rank or 1)),
            fact_span=bool(fact_span),
            explicit_provenance=bool(explicit_provenance),
            metadata=tuple(sorted((str(k), str(v)) for k, v in metadata)),
        )


@dataclass(frozen=True)
class CandidateDecision:
    evidence_id: str
    action: CandidateAction
    reason_codes: tuple[ReasonCode, ...]
    newly_covered_roles: tuple[str, ...] = ()
    marginal: tuple[int, int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class SourceRegion:
    file_path: str
    symbol: str
    start_line: int
    end_line: int
    roles: tuple[str, ...]
    selection_reason: str
    line_count: int
    char_count: int
    source_tokens: int
    content_sha256: str
    _repository_root: str = field(default="", repr=False, compare=False)
    _content: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_source(
        cls,
        repository_root: str | Path,
        file_path: str,
        start_line: int,
        end_line: int,
        symbol: str,
        roles: tuple[str, ...],
        selection_reason: str,
    ) -> "SourceRegion":
        fp = _norm_path(file_path)
        target = Path(repository_root) / Path(fp)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, min(int(start_line or 1), max(1, len(lines))))
        end = max(start, min(int(end_line or start), max(1, len(lines))))
        content = "\n".join(lines[start - 1 : end])
        return cls(
            file_path=fp,
            symbol=symbol or "",
            start_line=start,
            end_line=end,
            roles=tuple(sorted(set(roles))),
            selection_reason=selection_reason,
            line_count=end - start + 1,
            char_count=len(content),
            source_tokens=(len(content) + 3) // 4,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            _repository_root=str(repository_root),
            _content=content,
        )


@dataclass(frozen=True)
class CoverageState:
    required: tuple[str, ...]
    covered: tuple[str, ...]
    unresolved: tuple[str, ...]
    unavailable: tuple[str, ...]


@dataclass(frozen=True)
class LocalizationDelta:
    newly_accepted: tuple[str, ...] = ()
    newly_rejected: tuple[str, ...] = ()
    newly_deferred: tuple[str, ...] = ()
    newly_resolved_roles: tuple[str, ...] = ()
    invalidated_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalizationResult:
    facets: BehaviorFacet
    capabilities: CapabilityMatrix
    discoveries: tuple[EvidenceUnit, ...]
    decisions: tuple[CandidateDecision, ...]
    admitted_regions: tuple[SourceRegion, ...]
    coverage: CoverageState
    stopping_reason: str
    state: LocalizationState
    delta: LocalizationDelta | None
    metrics: Mapping[str, Any]
    deterministic_hash: str = ""
    schema: str = "gt.localization.vnext.v1"

    def _semantic_payload(self) -> dict[str, Any]:
        payload = _to_primitive(self)
        payload.pop("deterministic_hash", None)
        metrics = dict(payload.get("metrics") or {})
        for key in (
            "latency_ms",
            "cold_latency_ms",
            "warm_latency_ms",
            "peak_memory_bytes",
            "stage_latency_ms",
        ):
            metrics.pop(key, None)
        payload["metrics"] = metrics
        return payload

    def compute_deterministic_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self._semantic_payload())).hexdigest()

    def sealed(self) -> "LocalizationResult":
        from dataclasses import replace

        return replace(self, deterministic_hash=self.compute_deterministic_hash())

    def to_dict(self) -> dict[str, Any]:
        payload = _to_primitive(self)
        payload["metrics"] = {
            key: _format_metric(value) for key, value in sorted(self.metrics.items())
        }
        return payload


def _norm_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _to_primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            f.name: _to_primitive(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {
            str(k): _to_primitive(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_to_primitive(v) for v in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _to_primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _format_metric(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.8f}"
    if isinstance(value, Mapping):
        return {str(k): _format_metric(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_format_metric(v) for v in value]
    return value


__all__ = [
    "BehaviorFacet",
    "CandidateAction",
    "CandidateDecision",
    "CapabilityMatrix",
    "CoverageState",
    "EvidenceFamily",
    "EvidenceUnit",
    "LocalizationDelta",
    "LocalizationPolicy",
    "LocalizationRequest",
    "LocalizationResult",
    "LocalizationState",
    "ReasonCode",
    "SourceRegion",
]

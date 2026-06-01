"""Telemetry schemas — structured event types for GT observability."""

from __future__ import annotations

import enum
import uuid
import time
from dataclasses import dataclass, field
from typing import Any


class EvidenceKind(str, enum.Enum):
    L1_CANDIDATE = "l1_candidate"
    L1_GRAPH_EDGE = "l1_graph_edge"
    L1_TEST_EDGE = "l1_test_edge"
    L1_SIGNATURE = "l1_signature"
    L1_CONFIRMING_EDGE = "l1_confirming_edge"
    L3_CALLER_CODE = "l3_caller_code"
    L3_SIBLING_PATTERN = "l3_sibling_pattern"
    L3_SIGNATURE = "l3_signature"
    L3_TEST_ASSERTION = "l3_test_assertion"
    L3_CONTRACT = "l3_contract"
    L3_TARGETED_VERIFICATION = "l3_targeted_verification"
    L3B_CALLER_EDGE = "l3b_caller_edge"
    L3B_CALLEE_EDGE = "l3b_callee_edge"
    L3B_IMPORTER_EDGE = "l3b_importer_edge"
    L4_GIT_PRECEDENT = "l4_git_precedent"
    L4_CONSTRAINT = "l4_constraint"
    L5_EVENT_DETECTED = "l5_event_detected"
    L5B_INTERVENTION = "l5b_intervention"
    L6_REINDEX = "l6_reindex"
    HYGIENE_STRIP = "hygiene_strip"


def _gen_event_id() -> str:
    return uuid.uuid4().hex[:16]


def _now_ms() -> int:
    return int(time.time() * 1000)


def get_iteration_band(iter_num: int, max_iter: int) -> str:
    if max_iter <= 0:
        return "early_0_25"
    ratio = iter_num / max_iter
    if ratio < 0.25:
        return "early_0_25"
    if ratio < 0.60:
        return "mid_25_60"
    if ratio < 0.85:
        return "late_60_85"
    return "final_85_100"


@dataclass
class EvidenceItem:
    kind: str
    item_id: str = ""
    file_path: str | None = None
    symbol: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    text: str | None = None
    confidence: float | None = None
    source: str | None = None
    resolution_method: str | None = None
    reason: str | None = None
    token_estimate: int | None = None

    def __post_init__(self) -> None:
        if not self.item_id:
            self.item_id = _gen_event_id()
        if self.text and self.token_estimate is None:
            self.token_estimate = max(1, len(self.text) // 4)
        valid = {e.value for e in EvidenceKind}
        if self.kind not in valid:
            raise ValueError(f"Invalid evidence kind: {self.kind!r}. Valid: {valid}")

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class GTLayerEvent:
    # Required
    layer: str
    event_type: str
    eligible: bool
    emitted: bool
    suppressed: bool

    # Identity (auto-populated)
    schema_version: str = ""
    run_id: str = ""
    task_id: str = ""
    event_id: str = ""
    parent_event_id: str | None = None
    timestamp_ms: int = 0

    # Position
    iter: int = 0
    max_iter: int = 0
    iteration_band: str = ""

    # Trigger
    action_id: str | None = None
    observation_id: str | None = None
    action_type: str | None = None
    observation_type: str | None = None
    file_path: str | None = None
    symbol: str | None = None
    command: str | None = None
    command_kind: str | None = None
    verification_kind: str | None = None

    # GT decision
    sublayer: str | None = None
    suppression_reason: str | None = None
    trigger_event: str | None = None
    selected_path: str | None = None
    producer_status: str | None = None
    delivery_status: str | None = None
    payload_chars: int | None = None
    agent_facing_marker: str | None = None
    agent_visible: bool | None = None

    # Evidence
    confidence: float | None = None
    evidence_kind: str | None = None
    evidence_sources: dict[str, Any] | None = None
    evidence_items: list[dict[str, Any]] = field(default_factory=list)

    # Rendered output
    rendered_text: str | None = None
    rendered_chars: int | None = None
    rendered_tokens_estimate: int | None = None

    # Next action
    next_action_type: str | None = None
    next_action_text: str | None = None
    next_action_file: str | None = None
    next_action_command: str | None = None
    next_action_test: str | None = None

    # State
    belief_before: str | None = None
    belief_after: str | None = None
    state_before_hash: str | None = None
    state_after_hash: str | None = None

    # Decision 34: Generalized event taxonomy
    event_bucket: str | None = None
    file_kind: str | None = None
    check_kind: str | None = None
    verification_strength: str | None = None
    confidence_level: str | None = None
    confidence_score: float | None = None
    confidence_basis: str | None = None

    def __post_init__(self) -> None:
        from .constants import SCHEMA_VERSION, VALID_LAYERS
        if not self.schema_version:
            self.schema_version = SCHEMA_VERSION
        if not self.event_id:
            self.event_id = _gen_event_id()
        if not self.timestamp_ms:
            self.timestamp_ms = _now_ms()
        if not self.iteration_band:
            self.iteration_band = get_iteration_band(self.iter, self.max_iter)
        if self.layer not in VALID_LAYERS:
            raise ValueError(f"Invalid layer: {self.layer!r}")
        if self.rendered_text and self.rendered_chars is None:
            self.rendered_chars = len(self.rendered_text)
        if self.rendered_text and self.rendered_tokens_estimate is None:
            self.rendered_tokens_estimate = max(1, len(self.rendered_text) // 4)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None and v != "" and v != []:
                d[k] = v
        d["schema_version"] = self.schema_version
        d["event_id"] = self.event_id
        d["timestamp_ms"] = self.timestamp_ms
        d["layer"] = self.layer
        d["event_type"] = self.event_type
        d["eligible"] = self.eligible
        d["emitted"] = self.emitted
        d["suppressed"] = self.suppressed
        d["iter"] = self.iter
        d["max_iter"] = self.max_iter
        d["iteration_band"] = self.iteration_band
        return d


@dataclass
class GTAgentReactionEvent:
    # Required
    gt_event_id: str
    gt_layer: str
    gt_iter: int
    follow_type: str

    # Identity
    schema_version: str = ""
    run_id: str = ""
    task_id: str = ""
    timestamp_ms: int = 0

    gt_sublayer: str | None = None
    gt_message_visible: bool = True
    gt_message_position_ratio: float | None = None
    gt_message_tokens: int | None = None

    gt_next_action_type: str | None = None
    gt_next_action_file: str | None = None
    gt_next_action_command: str | None = None
    gt_next_action_test: str | None = None

    reaction_window: int = 5
    checked_until_iter: int = 0

    next_agent_action_type: str | None = None
    next_agent_file: str | None = None
    next_agent_command: str | None = None
    next_agent_edit_file: str | None = None
    next_agent_test_command: str | None = None
    next_agent_test_kind: str | None = None

    followed_within_1: bool = False
    followed_within_3: bool = False
    followed_within_5: bool = False
    followed_eventually: bool = False

    ignored: bool = False
    partial_follow: bool = False
    contradicted: bool = False
    finished_without_follow: bool = False

    ran_broad_test_after_gt: bool = False
    ran_targeted_test_after_gt: bool = False
    ran_related_test_after_gt: bool = False
    ran_irrelevant_test_after_gt: bool = False

    opened_suggested_file: bool = False
    edited_suggested_file: bool = False
    changed_diff_after_gt: bool = False
    diff_before_gt_hash: str | None = None
    diff_after_gt_hash: str | None = None
    final_outcome_after_gt: str | None = None
    not_measurable_reason: str | None = None

    def __post_init__(self) -> None:
        from .constants import SCHEMA_VERSION, VALID_FOLLOW_TYPES
        if not self.schema_version:
            self.schema_version = SCHEMA_VERSION
        if not self.timestamp_ms:
            self.timestamp_ms = _now_ms()
        if self.follow_type not in VALID_FOLLOW_TYPES:
            raise ValueError(f"Invalid follow_type: {self.follow_type!r}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None and v != "" and v != []:
                d[k] = v
        d["schema_version"] = self.schema_version
        d["gt_event_id"] = self.gt_event_id
        d["gt_layer"] = self.gt_layer
        d["gt_iter"] = self.gt_iter
        d["follow_type"] = self.follow_type
        return d


@dataclass
class GTBeliefEvent:
    # Required
    file_path: str
    new_status: str
    reason: str
    source_event_id: str

    # Identity
    schema_version: str = ""
    run_id: str = ""
    task_id: str = ""
    event_id: str = ""
    timestamp_ms: int = 0
    iter: int = 0

    symbol: str | None = None
    previous_status: str | None = None
    previous_score: float | None = None
    new_score: float | None = None
    evidence_added: str | None = None

    def __post_init__(self) -> None:
        from .constants import SCHEMA_VERSION, VALID_BELIEF_STATUSES
        if not self.schema_version:
            self.schema_version = SCHEMA_VERSION
        if not self.event_id:
            self.event_id = _gen_event_id()
        if not self.timestamp_ms:
            self.timestamp_ms = _now_ms()
        if self.new_status not in VALID_BELIEF_STATUSES:
            raise ValueError(f"Invalid belief status: {self.new_status!r}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None and v != "":
                d[k] = v
        d["schema_version"] = self.schema_version
        d["event_id"] = self.event_id
        d["file_path"] = self.file_path
        d["new_status"] = self.new_status
        d["reason"] = self.reason
        d["source_event_id"] = self.source_event_id
        return d


@dataclass
class GTAgentEvent:
    """Structured record of one agent action, classified by GT's event taxonomy.

    Written to gt_agent_events_{task_id}.jsonl. One record per agent action.
    """

    agent_action_id: str
    iter: int
    event_bucket: str

    schema_version: str = ""
    run_id: str = ""
    task_id: str = ""
    timestamp_ms: int = 0

    agent_event_type: str = ""
    file_path: str | None = None
    file_kind: str | None = None
    symbol: str | None = None
    command: str | None = None
    check_kind: str | None = None
    verification_strength: str | None = None

    diff_lines_added: int = 0
    diff_lines_removed: int = 0
    state_changed: bool = False

    related_gt_event_id: str | None = None
    max_iter: int = 0
    iteration_band: str = ""

    def __post_init__(self) -> None:
        from .constants import (
            SCHEMA_VERSION, VALID_EVENT_BUCKETS, VALID_FILE_KINDS,
            VALID_CHECK_KINDS, VALID_VERIFICATION_STRENGTHS,
        )
        if not self.schema_version:
            self.schema_version = SCHEMA_VERSION
        if not self.timestamp_ms:
            self.timestamp_ms = _now_ms()
        if not self.iteration_band:
            self.iteration_band = get_iteration_band(self.iter, self.max_iter)
        if self.event_bucket not in VALID_EVENT_BUCKETS:
            raise ValueError(f"Invalid event_bucket: {self.event_bucket!r}")
        if self.file_kind and self.file_kind not in VALID_FILE_KINDS:
            raise ValueError(f"Invalid file_kind: {self.file_kind!r}")
        if self.check_kind and self.check_kind not in VALID_CHECK_KINDS:
            raise ValueError(f"Invalid check_kind: {self.check_kind!r}")
        if self.verification_strength and self.verification_strength not in VALID_VERIFICATION_STRENGTHS:
            raise ValueError(f"Invalid verification_strength: {self.verification_strength!r}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if v is not None and v != "" and v != [] and v != 0 and v is not False:
                d[k] = v
        d["schema_version"] = self.schema_version
        d["agent_action_id"] = self.agent_action_id
        d["iter"] = self.iter
        d["event_bucket"] = self.event_bucket
        d["timestamp_ms"] = self.timestamp_ms
        d["iteration_band"] = self.iteration_band
        return d

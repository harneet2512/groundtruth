"""L5 trajectory state — persists across interactions, survives condenser."""

from __future__ import annotations

import enum
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any


class IterationBand(str, enum.Enum):
    EARLY_EXPLORATION = "early_exploration"
    MID_COMMITMENT = "mid_commitment"
    LATE_REPAIR = "late_repair"
    FINALIZATION = "finalization"


class AgentPhase(str, enum.Enum):
    LOCALIZING = "localizing"
    READING = "reading"
    HYPOTHESIZING = "hypothesizing"
    EDITING = "editing"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    FINISHING = "finishing"


def _state_path(task_id: str = "") -> str:
    """Task-scoped state path. Fixes cross-worker contamination (Decision 34 §10)."""
    if task_id:
        safe = task_id.replace("/", "_").replace("\\", "_")
        return f"/tmp/gt_l5_state_{safe}.json"
    return "/tmp/gt_l5_state.json"


def compute_band(current_iter: int, max_iter: int) -> IterationBand:
    if max_iter <= 0:
        return IterationBand.EARLY_EXPLORATION
    ratio = current_iter / max_iter
    if ratio < 0.25:
        return IterationBand.EARLY_EXPLORATION
    if ratio < 0.60:
        return IterationBand.MID_COMMITMENT
    if ratio < 0.85:
        return IterationBand.LATE_REPAIR
    return IterationBand.FINALIZATION


@dataclass
class FailureSnapshot:
    """Compact record of one verification failure."""

    command_kind: str = ""
    failure_kind: str = ""
    failing_unit: str = ""
    file: str = ""
    line: int = 0
    assertion_or_error: str = ""
    expected: str = ""
    actual: str = ""
    exception_type: str = ""
    top_project_frame: str = ""
    raw_excerpt: str = ""
    signature_hash: str = ""
    iter_observed: int = 0

    def compute_hash(self) -> str:
        key = f"{self.failing_unit}:{self.assertion_or_error}:{self.expected}"
        self.signature_hash = hashlib.md5(key.encode()).hexdigest()[:12]
        return self.signature_hash


@dataclass
class L5TrajectoryState:
    """Full trajectory state for L5 governor decisions."""

    instance_id: str = ""
    current_iter: int = 0
    max_iter: int = 100
    band: IterationBand = IterationBand.EARLY_EXPLORATION
    phase: AgentPhase = AgentPhase.LOCALIZING

    edited_source_files: list[str] = field(default_factory=list)
    last_edit_iter: int = 0

    verification_commands_run: int = 0
    last_verification_iter: int = 0
    last_passing_verification_iter: int = 0
    last_failing_verification_iter: int = 0
    has_source_edit_before_last_failure: bool = False

    failure_records: list[dict[str, Any]] = field(default_factory=list)
    unresolved_failure_hashes: list[str] = field(default_factory=list)
    repeated_failure_count: int = 0

    l5_messages_emitted: int = 0
    last_l5_hook: str = ""
    last_l5_iter: int = 0
    suppressed_reasons: list[str] = field(default_factory=list)

    # Verification targeting (Change 1b)
    last_passing_broad_iter: int = 0
    last_passing_targeted_iter: int = 0
    broad_pass_after_edit_count: int = 0
    verification_targeting_history: list[dict[str, Any]] = field(default_factory=list)

    # Decision 34: Diff/patch tracking
    patch_nonzero_seen: bool = False
    patch_size_current: int = 0
    patch_size_previous: int = 0
    patch_collapsed: bool = False
    durable_edit_lost: bool = False

    # Decision 34: Structural witness tracking
    latest_gt_next_action_type: str | None = None
    latest_gt_next_action_file: str | None = None
    latest_gt_next_action_iter: int = 0
    actions_since_gt_next_action: int = 0
    structural_witness_followed: bool = False

    # Decision 34: Per-bucket emission counts
    l5_emissions_by_type: dict[str, int] = field(default_factory=dict)
    l5_last_emission_type: str = ""
    l5_last_emission_iter: int = 0

    # Decision 34: Loop detection
    last_action_signature: str = ""
    repeated_action_count: int = 0

    _initialized: bool = False
    _injection_disabled: bool = False
    _disable_reason: str = ""
    _prev_iter: int = -1

    def update_iter(self, action_count: int, max_iter: int) -> None:
        highest_seen = max(self.current_iter, self._prev_iter)
        if highest_seen > 0 and action_count < highest_seen:
            self._injection_disabled = True
            self._disable_reason = f"iter_decreased:{highest_seen}->{action_count}"
        self._prev_iter = max(action_count, highest_seen)
        self.current_iter = action_count
        self.max_iter = max_iter
        self.band = compute_band(action_count, max_iter)

    def record_source_edit(self, file_path: str) -> None:
        if file_path not in self.edited_source_files:
            self.edited_source_files.append(file_path)
        self.last_edit_iter = self.current_iter
        self.phase = AgentPhase.EDITING
        self.has_source_edit_before_last_failure = True

    def record_verification(
        self,
        passed: bool,
        failure: FailureSnapshot | None = None,
        target_level: str = "UNKNOWN",
    ) -> None:
        self.verification_commands_run += 1
        self.last_verification_iter = self.current_iter
        self.phase = AgentPhase.VALIDATING

        self.verification_targeting_history.append({
            "iter": self.current_iter,
            "target_level": target_level,
            "passed": passed,
        })
        if len(self.verification_targeting_history) > 50:
            self.verification_targeting_history = self.verification_targeting_history[-50:]

        is_targeted = target_level in (
            "targeted_to_edited_symbol",
            "targeted_to_edited_file",
            "targeted_to_related_test",
        )

        if passed:
            self.last_passing_verification_iter = self.current_iter
            self.unresolved_failure_hashes.clear()
            self.repeated_failure_count = 0
            if is_targeted:
                self.last_passing_targeted_iter = self.current_iter
                self.broad_pass_after_edit_count = 0
            else:
                self.last_passing_broad_iter = self.current_iter
                if self.edited_source_files and self.last_edit_iter >= self.last_passing_targeted_iter:
                    self.broad_pass_after_edit_count += 1
        else:
            self.last_failing_verification_iter = self.current_iter
            self.phase = AgentPhase.REPAIRING
            if failure:
                h = failure.compute_hash()
                rec = {
                    "hash": h,
                    "failing_unit": failure.failing_unit,
                    "assertion": failure.assertion_or_error,
                    "expected": failure.expected,
                    "actual": failure.actual,
                    "exception_type": failure.exception_type,
                    "top_frame": failure.top_project_frame,
                    "excerpt": failure.raw_excerpt[:300],
                    "iter": self.current_iter,
                }
                self.failure_records.append(rec)
                if h in self.unresolved_failure_hashes:
                    self.repeated_failure_count += 1
                else:
                    self.unresolved_failure_hashes.append(h)
                    self.repeated_failure_count = 0

    def record_l5_emission(self, hook_name: str) -> None:
        self.l5_messages_emitted += 1
        self.last_l5_hook = hook_name
        self.last_l5_iter = self.current_iter

    def has_unverified_patch(self) -> bool:
        """True if source edit followed only by broad (not targeted) verification."""
        if not self.edited_source_files:
            return False
        if self.last_passing_targeted_iter > 0 and self.last_passing_targeted_iter >= self.last_edit_iter:
            return False
        if self.broad_pass_after_edit_count > 0:
            return True
        return False

    def has_unresolved_failure(self) -> bool:
        if not self.failure_records:
            return False
        return self.last_failing_verification_iter > self.last_passing_verification_iter

    def last_failure(self) -> dict[str, Any] | None:
        return self.failure_records[-1] if self.failure_records else None

    def record_diff_snapshot(self, diff_size: int) -> None:
        """Record current diff size, detect collapse."""
        self.patch_size_previous = self.patch_size_current
        self.patch_size_current = diff_size
        if diff_size > 0:
            self.patch_nonzero_seen = True
        if self.patch_nonzero_seen and diff_size == 0:
            self.patch_collapsed = True
            self.durable_edit_lost = True

    def record_gt_next_action(
        self, next_action_type: str, next_action_file: str | None, iter_num: int,
    ) -> None:
        """Record a GT next_action emission for witness tracking."""
        self.latest_gt_next_action_type = next_action_type
        self.latest_gt_next_action_file = next_action_file
        self.latest_gt_next_action_iter = iter_num
        self.actions_since_gt_next_action = 0
        self.structural_witness_followed = False

    def record_action_after_gt(self, file_path: str | None = None) -> None:
        """Record agent action, check if it follows structural witness."""
        self.actions_since_gt_next_action += 1
        if (
            self.latest_gt_next_action_file
            and file_path
            and (
                self.latest_gt_next_action_file in file_path
                or file_path in self.latest_gt_next_action_file
            )
        ):
            self.structural_witness_followed = True

    def record_action_signature(self, signature: str) -> None:
        """Track repeated action detection."""
        if signature == self.last_action_signature:
            self.repeated_action_count += 1
        else:
            self.last_action_signature = signature
            self.repeated_action_count = 0

    def can_emit_l5(self, event_type: str) -> tuple[bool, str]:
        """Check debounce, max emissions, and iteration-band rules. Returns (allowed, reason)."""
        from ..telemetry.constants import L5_MAX_EMISSIONS_PER_TASK, L5_DEBOUNCE_ITERATIONS
        total = sum(self.l5_emissions_by_type.values())
        if total >= L5_MAX_EMISSIONS_PER_TASK:
            return False, f"max_emissions_reached:{total}>={L5_MAX_EMISSIONS_PER_TASK}"
        if (
            self.l5_last_emission_type == event_type
            and (self.current_iter - self.l5_last_emission_iter) < L5_DEBOUNCE_ITERATIONS
        ):
            return False, f"debounce:{event_type}:gap={self.current_iter - self.l5_last_emission_iter}<{L5_DEBOUNCE_ITERATIONS}"
        return True, ""

    def record_l5_goku_emission(self, event_type: str) -> None:
        """Record a Goku L5 emission for debounce and cap tracking."""
        self.l5_emissions_by_type[event_type] = self.l5_emissions_by_type.get(event_type, 0) + 1
        self.l5_last_emission_type = event_type
        self.l5_last_emission_iter = self.current_iter

    def save(self) -> None:
        try:
            data = {
                "instance_id": self.instance_id,
                "current_iter": self.current_iter,
                "max_iter": self.max_iter,
                "band": self.band.value,
                "phase": self.phase.value,
                "edited_source_files": self.edited_source_files,
                "last_edit_iter": self.last_edit_iter,
                "verification_commands_run": self.verification_commands_run,
                "last_verification_iter": self.last_verification_iter,
                "last_passing_verification_iter": self.last_passing_verification_iter,
                "last_failing_verification_iter": self.last_failing_verification_iter,
                "has_source_edit_before_last_failure": self.has_source_edit_before_last_failure,
                "failure_records": self.failure_records[-10:],
                "unresolved_failure_hashes": self.unresolved_failure_hashes,
                "repeated_failure_count": self.repeated_failure_count,
                "l5_messages_emitted": self.l5_messages_emitted,
                "last_l5_hook": self.last_l5_hook,
                "last_l5_iter": self.last_l5_iter,
                "last_passing_broad_iter": self.last_passing_broad_iter,
                "last_passing_targeted_iter": self.last_passing_targeted_iter,
                "broad_pass_after_edit_count": self.broad_pass_after_edit_count,
                "verification_targeting_history": self.verification_targeting_history[-20:],
                "injection_disabled": self._injection_disabled,
                "disable_reason": self._disable_reason,
                "patch_nonzero_seen": self.patch_nonzero_seen,
                "patch_size_current": self.patch_size_current,
                "patch_size_previous": self.patch_size_previous,
                "patch_collapsed": self.patch_collapsed,
                "durable_edit_lost": self.durable_edit_lost,
                "latest_gt_next_action_type": self.latest_gt_next_action_type,
                "latest_gt_next_action_file": self.latest_gt_next_action_file,
                "latest_gt_next_action_iter": self.latest_gt_next_action_iter,
                "actions_since_gt_next_action": self.actions_since_gt_next_action,
                "structural_witness_followed": self.structural_witness_followed,
                "l5_emissions_by_type": self.l5_emissions_by_type,
                "l5_last_emission_type": self.l5_last_emission_type,
                "l5_last_emission_iter": self.l5_last_emission_iter,
                "repeated_action_count": self.repeated_action_count,
                "timestamp": time.time(),
            }
            path = _state_path(self.instance_id)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    @classmethod
    def load_or_create(cls, instance_id: str, max_iter: int = 100) -> L5TrajectoryState:
        try:
            path = _state_path(instance_id)
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                if data.get("instance_id") == instance_id:
                    state = cls()
                    state.instance_id = instance_id
                    state.current_iter = data.get("current_iter", 0)
                    state.max_iter = data.get("max_iter", max_iter)
                    state.band = IterationBand(data.get("band", "early_exploration"))
                    state.phase = AgentPhase(data.get("phase", "localizing"))
                    state.edited_source_files = data.get("edited_source_files", [])
                    state.last_edit_iter = data.get("last_edit_iter", 0)
                    state.verification_commands_run = data.get("verification_commands_run", 0)
                    state.last_verification_iter = data.get("last_verification_iter", 0)
                    state.last_passing_verification_iter = data.get("last_passing_verification_iter", 0)
                    state.last_failing_verification_iter = data.get("last_failing_verification_iter", 0)
                    state.has_source_edit_before_last_failure = data.get("has_source_edit_before_last_failure", False)
                    state.failure_records = data.get("failure_records", [])
                    state.unresolved_failure_hashes = data.get("unresolved_failure_hashes", [])
                    state.repeated_failure_count = data.get("repeated_failure_count", 0)
                    state.l5_messages_emitted = data.get("l5_messages_emitted", 0)
                    state.last_l5_hook = data.get("last_l5_hook", "")
                    state.last_l5_iter = data.get("last_l5_iter", 0)
                    state.last_passing_broad_iter = data.get("last_passing_broad_iter", 0)
                    state.last_passing_targeted_iter = data.get("last_passing_targeted_iter", 0)
                    state.broad_pass_after_edit_count = data.get("broad_pass_after_edit_count", 0)
                    state.verification_targeting_history = data.get("verification_targeting_history", [])
                    state._injection_disabled = data.get("injection_disabled", False)
                    state._disable_reason = data.get("disable_reason", "")
                    state.patch_nonzero_seen = data.get("patch_nonzero_seen", False)
                    state.patch_size_current = data.get("patch_size_current", 0)
                    state.patch_size_previous = data.get("patch_size_previous", 0)
                    state.patch_collapsed = data.get("patch_collapsed", False)
                    state.durable_edit_lost = data.get("durable_edit_lost", False)
                    state.latest_gt_next_action_type = data.get("latest_gt_next_action_type")
                    state.latest_gt_next_action_file = data.get("latest_gt_next_action_file")
                    state.latest_gt_next_action_iter = data.get("latest_gt_next_action_iter", 0)
                    state.actions_since_gt_next_action = data.get("actions_since_gt_next_action", 0)
                    state.structural_witness_followed = data.get("structural_witness_followed", False)
                    state.l5_emissions_by_type = data.get("l5_emissions_by_type", {})
                    state.l5_last_emission_type = data.get("l5_last_emission_type", "")
                    state.l5_last_emission_iter = data.get("l5_last_emission_iter", 0)
                    state.repeated_action_count = data.get("repeated_action_count", 0)
                    state._prev_iter = state.current_iter
                    state._initialized = True
                    return state
        except Exception:
            pass
        state = cls(instance_id=instance_id, max_iter=max_iter)
        state._initialized = True
        state._prev_iter = 0
        return state

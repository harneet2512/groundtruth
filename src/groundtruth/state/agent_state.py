"""AgentState — FINAL_ARCH_V2 Layer 2 (Agent-State Tracker).

One canonical view of the agent's trajectory. Constructed once per task by the
wrapper; updated by every hook; queried by Layer 3 (Collaboration Router).

Schema follows DECISIONS.md `## FINAL_ARCH_V2` §3 Layer 2:

- ``task_id``
- ``issue_terms``         — keywords extracted at task start
- ``brief_candidates``    — files surfaced by Layer 1 Pre-Task Seed
- ``viewed_files``        — every file the agent has read (canonical paths, ordered)
- ``edited_files``        — every source file the agent has edited (ordered)
- ``searches``            — every grep/find command the agent has run
- ``current_file``        — last edited file
- ``current_focus``       — last viewed-or-edited file (i.e. where the agent is right now)
- ``pending_suggestions`` — GT next_action emissions awaiting follow/ignore classification
- ``suggested_edges``     — every (caller/callee/importer/etc.) edge GT has shown the agent
- ``ignored_suggestions`` — pending suggestions whose TTL expired without follow
- ``drift_flags``         — derived signals (repeat_loop, scope_unrelated_to_edits, stale_evidence_count)
- ``iteration`` / ``max_iterations``

Backwards compatibility: this class wraps ``L5TrajectoryState`` (the existing
L5 governor state) so callers that still import ``groundtruth.trajectory.state``
keep working. The tmp-file readers in ``post_view.py`` are preserved for the
subprocess code path.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Iteration bands and agent phase (preserved from trajectory/state.py)
# ---------------------------------------------------------------------------


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


def compute_band(current_iter: int, max_iter: int) -> IterationBand:
    """Bucket an iteration count into one of four bands (D34 §6)."""
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


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


def canonical_repo_path(path: str, repo_root: str | None = None) -> str:
    """Return a canonical repo-relative path.

    Strips:
    - ``repo_root`` prefix (if given and present)
    - any ``/workspace/<anything>/`` prefix (OH container convention)
    - leading ``./`` and any backslashes are converted to forward slashes

    Empty / falsy input returns ``""``.
    """
    if not path:
        return ""
    p = path.replace("\\", "/")
    # Strip explicit repo_root if it matches.
    if repo_root:
        rr = repo_root.replace("\\", "/").rstrip("/")
        if rr and p.startswith(rr + "/"):
            p = p[len(rr) + 1:]
        elif rr and p == rr:
            return ""
    # Strip /workspace/<repo>/ container prefix.
    if p.startswith("/workspace/"):
        rest = p[len("/workspace/"):]
        # Drop the next path segment which is the repo root inside the container.
        if "/" in rest:
            p = rest.split("/", 1)[1]
        else:
            p = ""
    # Strip leading ./ and leading slash.
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return p


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------


@dataclass
class FailureSnapshot:
    """Compact record of one verification failure (preserved from L5)."""

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
class ViewedFile:
    """One file the agent read at least once (canonical path)."""

    path: str
    first_iter: int = 0
    last_iter: int = 0
    view_count: int = 1


@dataclass
class SearchEvent:
    """One grep/find/list issued by the agent."""

    iter: int
    command: str
    hits: int = 0


# ---------------------------------------------------------------------------
# Pending suggestion / classification
# ---------------------------------------------------------------------------


class SuggestionStatus(str, enum.Enum):
    PENDING = "pending"
    FOLLOWED_EXACT = "followed_exact"
    FOLLOWED_RELATED_FILE = "followed_related_file"
    IGNORED = "ignored"
    CONTRADICTED = "contradicted"
    NOT_MEASURABLE = "not_measurable"
    TOO_LATE = "too_late"


@dataclass
class PendingSuggestion:
    """A GT next_action emission awaiting agent reaction.

    TTL is expressed in *real agent actions checked* (not iterations) so it
    matches how D33 Goku items 4–5 implemented the online tracker.
    """

    event_id: str
    next_action_type: str
    next_action_file: str
    iter_emitted: int
    ttl_actions: int = 3
    checked_count: int = 0
    status: SuggestionStatus = SuggestionStatus.PENDING

    @property
    def followed(self) -> bool:
        return self.status in (
            SuggestionStatus.FOLLOWED_EXACT,
            SuggestionStatus.FOLLOWED_RELATED_FILE,
        )

    @property
    def expired(self) -> bool:
        """True once we've checked the maximum number of agent actions."""
        return self.checked_count >= self.ttl_actions

    def to_legacy_dict(self) -> dict[str, Any]:
        """Compatibility view matching the wrapper's pre-AgentState dict shape."""
        return {
            "event_id": self.event_id,
            "next_action_type": self.next_action_type,
            "next_action_file": self.next_action_file,
            "iter_emitted": self.iter_emitted,
            "checked_count": self.checked_count,
            "followed": self.followed,
            "status": self.status.value,
        }


# ---------------------------------------------------------------------------
# L5TrajectoryState — preserved verbatim (the existing trajectory/state.py
# class lives here now). DO NOT change its on-disk shape: trajectory/state.py
# re-exports this name and existing tests load JSON written by it.
# ---------------------------------------------------------------------------


def _l5_state_path(task_id: str = "") -> str:
    """Legacy path for the L5 sidecar (Decision 34 §10)."""
    if task_id:
        safe = task_id.replace("/", "_").replace("\\", "_")
        return f"/tmp/gt_l5_state_{safe}.json"
    return "/tmp/gt_l5_state.json"


# Kept available under both names for callers that import the private form.
_state_path = _l5_state_path


@dataclass
class L5TrajectoryState:
    """Full trajectory state for L5 governor decisions (legacy)."""

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

    last_passing_broad_iter: int = 0
    last_passing_targeted_iter: int = 0
    broad_pass_after_edit_count: int = 0
    verification_targeting_history: list[dict[str, Any]] = field(default_factory=list)

    patch_nonzero_seen: bool = False
    patch_size_current: int = 0
    patch_size_previous: int = 0
    patch_collapsed: bool = False
    durable_edit_lost: bool = False

    latest_gt_next_action_type: str | None = None
    latest_gt_next_action_file: str | None = None
    latest_gt_next_action_iter: int = 0
    actions_since_gt_next_action: int = 0
    structural_witness_followed: bool = False

    l5_emissions_by_type: dict[str, int] = field(default_factory=dict)
    l5_last_emission_type: str = ""
    l5_last_emission_iter: int = 0

    last_action_signature: str = ""
    repeated_action_count: int = 0

    scaffold_files_created: int = 0
    scaffold_trigger_fired: bool = False

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
        self.latest_gt_next_action_type = next_action_type
        self.latest_gt_next_action_file = next_action_file
        self.latest_gt_next_action_iter = iter_num
        self.actions_since_gt_next_action = 0
        self.structural_witness_followed = False

    def record_action_after_gt(self, file_path: str | None = None) -> None:
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

    def record_scaffold_file(self) -> None:
        self.scaffold_files_created += 1

    def record_action_signature(self, signature: str) -> None:
        if signature == self.last_action_signature:
            self.repeated_action_count += 1
        else:
            self.last_action_signature = signature
            self.repeated_action_count = 0

    def can_emit_l5(self, event_type: str) -> tuple[bool, str]:
        from groundtruth.telemetry.constants import L5_MAX_INJECTIONS_PER_TASK, L5_DEBOUNCE_ITERATIONS
        total = sum(self.l5_emissions_by_type.values())
        if total >= L5_MAX_INJECTIONS_PER_TASK:
            return False, f"max_emissions_reached:{total}>={L5_MAX_INJECTIONS_PER_TASK}"
        if (
            self.l5_last_emission_type == event_type
            and (self.current_iter - self.l5_last_emission_iter) < L5_DEBOUNCE_ITERATIONS
        ):
            return False, f"debounce:{event_type}:gap={self.current_iter - self.l5_last_emission_iter}<{L5_DEBOUNCE_ITERATIONS}"
        return True, ""

    def record_l5_goku_emission(self, event_type: str) -> None:
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
                "scaffold_files_created": self.scaffold_files_created,
                "scaffold_trigger_fired": self.scaffold_trigger_fired,
                "timestamp": time.time(),
            }
            path = _l5_state_path(self.instance_id)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    @classmethod
    def load_or_create(cls, instance_id: str, max_iter: int = 100) -> L5TrajectoryState:
        try:
            path = _l5_state_path(instance_id)
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
                    state.scaffold_files_created = data.get("scaffold_files_created", 0)
                    state.scaffold_trigger_fired = data.get("scaffold_trigger_fired", False)
                    state._prev_iter = state.current_iter
                    state._initialized = True
                    return state
        except Exception:
            pass
        state = cls(instance_id=instance_id, max_iter=max_iter)
        state._initialized = True
        state._prev_iter = 0
        return state


# ---------------------------------------------------------------------------
# AgentState — the FINAL_ARCH_V2 Layer 2 entry point.
# ---------------------------------------------------------------------------


def _agent_state_path(task_id: str = "") -> str:
    """Task-scoped path for the AgentState JSON sidecar."""
    if task_id:
        safe = task_id.replace("/", "_").replace("\\", "_")
        return f"/tmp/gt_agent_state_{safe}.json"
    return "/tmp/gt_agent_state.json"


# Legacy tmp-file paths still consumed by post_view.py subprocess mode. New
# code should treat AgentState as the source of truth and use these only as a
# compatibility mirror.
LEGACY_VIEWED_PATH = "/tmp/gt_viewed.txt"
LEGACY_BRIEF_CANDIDATES_PATH = "/tmp/gt_brief_candidates.txt"
LEGACY_ISSUE_TERMS_PATH = "/tmp/gt_issue_terms.txt"


@dataclass
class AgentState:
    """FINAL_ARCH_V2 Layer 2 — canonical agent-trajectory tracker.

    Construct with ``AgentState.create(task_id, max_iterations, repo_root)`` or
    ``AgentState.load_or_create(...)``. The object is mutated in place by hooks
    and queried by the Layer 3 router. Persistence is best-effort: each mutator
    optionally calls :py:meth:`save` so a subprocess hook can recover state by
    loading the JSON sidecar.
    """

    task_id: str = ""
    repo_root: str = "/testbed"
    max_iterations: int = 100
    iteration: int = 0

    issue_terms: set[str] = field(default_factory=set)
    brief_candidates: set[str] = field(default_factory=set)
    viewed_files: list[ViewedFile] = field(default_factory=list)
    edited_files: list[str] = field(default_factory=list)
    searches: list[SearchEvent] = field(default_factory=list)
    current_file: str = ""
    current_focus: str = ""

    pending_suggestions: list[PendingSuggestion] = field(default_factory=list)
    suggested_edges: list[dict[str, Any]] = field(default_factory=list)
    ignored_suggestions: list[PendingSuggestion] = field(default_factory=list)

    drift_flags: dict[str, Any] = field(default_factory=lambda: {
        "repeat_loop": False,
        "scope_unrelated_to_edits": False,
        "stale_evidence_count": 0,
    })

    # Embedded legacy state so existing L5 governor code can keep operating on
    # the same task. New code should prefer the AgentState fields above.
    legacy: L5TrajectoryState = field(default_factory=L5TrajectoryState)

    # Internal viewed-file lookup keyed by canonical path.
    _viewed_index: dict[str, int] = field(default_factory=dict, repr=False)

    # ---- Construction --------------------------------------------------

    @classmethod
    def create(
        cls,
        task_id: str = "",
        max_iterations: int = 100,
        repo_root: str = "/testbed",
    ) -> AgentState:
        state = cls(
            task_id=task_id,
            repo_root=repo_root,
            max_iterations=max_iterations,
        )
        state.legacy = L5TrajectoryState(instance_id=task_id, max_iter=max_iterations)
        state.legacy._initialized = True
        state.legacy._prev_iter = 0
        return state

    @classmethod
    def load_or_create(
        cls,
        task_id: str = "",
        max_iterations: int = 100,
        repo_root: str = "/testbed",
    ) -> AgentState:
        """Restore from the AgentState JSON sidecar if present; else create new.

        Always returns a fully-initialized object. Embedded ``legacy`` state is
        also loaded from the L5 sidecar so the two stay aligned.
        """
        path = _agent_state_path(task_id)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("task_id") == task_id:
                    state = cls(task_id=task_id, repo_root=data.get("repo_root", repo_root))
                    state.max_iterations = data.get("max_iterations", max_iterations)
                    state.iteration = data.get("iteration", 0)
                    state.issue_terms = set(data.get("issue_terms", []))
                    state.brief_candidates = set(data.get("brief_candidates", []))
                    state.viewed_files = [ViewedFile(**v) for v in data.get("viewed_files", [])]
                    state._viewed_index = {v.path: i for i, v in enumerate(state.viewed_files)}
                    state.edited_files = list(data.get("edited_files", []))
                    state.searches = [SearchEvent(**s) for s in data.get("searches", [])]
                    state.current_file = data.get("current_file", "")
                    state.current_focus = data.get("current_focus", "")
                    state.pending_suggestions = [
                        PendingSuggestion(
                            event_id=p["event_id"],
                            next_action_type=p["next_action_type"],
                            next_action_file=p["next_action_file"],
                            iter_emitted=p["iter_emitted"],
                            ttl_actions=p.get("ttl_actions", 3),
                            checked_count=p.get("checked_count", 0),
                            status=SuggestionStatus(p.get("status", "pending")),
                        )
                        for p in data.get("pending_suggestions", [])
                    ]
                    state.suggested_edges = list(data.get("suggested_edges", []))
                    state.ignored_suggestions = [
                        PendingSuggestion(
                            event_id=p["event_id"],
                            next_action_type=p["next_action_type"],
                            next_action_file=p["next_action_file"],
                            iter_emitted=p["iter_emitted"],
                            ttl_actions=p.get("ttl_actions", 3),
                            checked_count=p.get("checked_count", 0),
                            status=SuggestionStatus(p.get("status", "ignored")),
                        )
                        for p in data.get("ignored_suggestions", [])
                    ]
                    state.drift_flags = dict(data.get("drift_flags", state.drift_flags))
                    state.legacy = L5TrajectoryState.load_or_create(task_id, max_iterations)
                    return state
            except Exception:
                pass
        return cls.create(task_id=task_id, max_iterations=max_iterations, repo_root=repo_root)

    # ---- Iteration tracking --------------------------------------------

    @property
    def band(self) -> IterationBand:
        return compute_band(self.iteration, self.max_iterations)

    def set_iteration(self, action_count: int, max_iter: int | None = None) -> None:
        """Record the agent's action count. Updates the embedded legacy state."""
        if max_iter is not None:
            self.max_iterations = max_iter
        self.iteration = action_count
        self.legacy.update_iter(action_count, self.max_iterations)

    # ---- Issue terms / brief candidates --------------------------------

    def set_issue_terms(self, terms: set[str] | list[str] | str) -> None:
        if isinstance(terms, str):
            cleaned = {t.strip() for t in terms.splitlines() if t.strip()}
        else:
            cleaned = {t.strip() for t in terms if t and t.strip()}
        self.issue_terms = cleaned

    def set_brief_candidates(self, paths: set[str] | list[str]) -> None:
        self.brief_candidates = {canonical_repo_path(p, self.repo_root) for p in paths if p}

    # ---- View / edit / search -----------------------------------------

    def record_view(self, path: str, *, sync_legacy_file: bool = True) -> str:
        """Record a file the agent read. Returns the canonical path used."""
        canon = canonical_repo_path(path, self.repo_root)
        if not canon:
            return ""
        idx = self._viewed_index.get(canon)
        if idx is None:
            entry = ViewedFile(path=canon, first_iter=self.iteration, last_iter=self.iteration)
            self._viewed_index[canon] = len(self.viewed_files)
            self.viewed_files.append(entry)
        else:
            entry = self.viewed_files[idx]
            entry.last_iter = self.iteration
            entry.view_count += 1
        self.current_focus = canon
        if sync_legacy_file:
            self._mirror_viewed_to_tmp()
        return canon

    def record_edit(self, path: str, *, sync_legacy_state: bool = True) -> str:
        """Record a source edit. Returns the canonical path used."""
        canon = canonical_repo_path(path, self.repo_root)
        if not canon:
            return ""
        if canon not in self.edited_files:
            self.edited_files.append(canon)
        self.current_file = canon
        self.current_focus = canon
        if sync_legacy_state:
            self.legacy.record_source_edit(canon)
        return canon

    def record_search(self, command: str, hits: int = 0) -> None:
        self.searches.append(SearchEvent(iter=self.iteration, command=command, hits=hits))

    # ---- Pending suggestions ------------------------------------------

    def register_pending_suggestion(
        self,
        event_id: str,
        next_action_type: str,
        next_action_file: str = "",
        ttl_actions: int = 3,
    ) -> PendingSuggestion | None:
        """Register a GT next_action emission for follow/ignore classification.

        Returns the registered PendingSuggestion, or ``None`` if the
        ``next_action_type`` is not actionable (e.g. ``"NONE"``).
        """
        if not next_action_type or next_action_type in ("", "NONE", "NONE_UNVERIFIABLE", None):
            return None
        sug = PendingSuggestion(
            event_id=event_id or "",
            next_action_type=next_action_type,
            next_action_file=canonical_repo_path(next_action_file, self.repo_root) if next_action_file else "",
            iter_emitted=self.iteration,
            ttl_actions=ttl_actions,
        )
        self.pending_suggestions.append(sug)
        return sug

    def process_agent_action(
        self,
        action_file: str = "",
        action_type: str = "",
    ) -> list[PendingSuggestion]:
        """Update pending suggestions in response to one agent action.

        ``action_type`` is currently unused (reserved for CONTRADICTED
        classification once Layer 3 routes contradictions through here).

        Returns the list of suggestions that *just expired* with an IGNORED
        classification (caller may emit downstream events for these).
        """
        _ = action_type
        canon_action_file = canonical_repo_path(action_file, self.repo_root) if action_file else ""
        expired: list[PendingSuggestion] = []
        kept: list[PendingSuggestion] = []
        for sug in self.pending_suggestions:
            sug.checked_count += 1
            # Followed-exact: action file matches the suggested next-action file.
            if sug.next_action_file and canon_action_file:
                if (
                    sug.next_action_file == canon_action_file
                    or sug.next_action_file in canon_action_file
                    or canon_action_file in sug.next_action_file
                ):
                    sug.status = SuggestionStatus.FOLLOWED_EXACT
            if sug.expired:
                if sug.status == SuggestionStatus.PENDING:
                    sug.status = SuggestionStatus.IGNORED
                if sug.status == SuggestionStatus.IGNORED:
                    expired.append(sug)
                    self.ignored_suggestions.append(sug)
                    continue
                # Followed-on-the-last-check is not "ignored"; drop without listing.
                continue
            kept.append(sug)
        self.pending_suggestions = kept
        return expired

    # ---- Compatibility queries (used by post_view subprocess) ---------

    def visited_files_set(self) -> set[str]:
        return {v.path for v in self.viewed_files}

    def is_brief_candidate(self, path: str) -> bool:
        canon = canonical_repo_path(path, self.repo_root)
        if not canon:
            return False
        if canon in self.brief_candidates:
            return True
        # Match suffix / prefix for parity with post_view's existing logic.
        for cand in self.brief_candidates:
            if canon.endswith("/" + cand) or cand.endswith("/" + canon):
                return True
        return False

    # ---- Persistence --------------------------------------------------

    def save(self) -> None:
        try:
            data = {
                "task_id": self.task_id,
                "repo_root": self.repo_root,
                "max_iterations": self.max_iterations,
                "iteration": self.iteration,
                "issue_terms": sorted(self.issue_terms),
                "brief_candidates": sorted(self.brief_candidates),
                "viewed_files": [v.__dict__ for v in self.viewed_files],
                "edited_files": list(self.edited_files),
                "searches": [s.__dict__ for s in self.searches],
                "current_file": self.current_file,
                "current_focus": self.current_focus,
                "pending_suggestions": [p.__dict__ | {"status": p.status.value} for p in self.pending_suggestions],
                "suggested_edges": list(self.suggested_edges),
                "ignored_suggestions": [p.__dict__ | {"status": p.status.value} for p in self.ignored_suggestions],
                "drift_flags": dict(self.drift_flags),
                "timestamp": time.time(),
            }
            path = _agent_state_path(self.task_id)
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception:
            pass
        # Mirror the embedded L5 state too.
        try:
            self.legacy.save()
        except Exception:
            pass

    # ---- Legacy tmp-file mirror ---------------------------------------

    def _mirror_viewed_to_tmp(self) -> None:
        """Write canonical viewed paths to ``/tmp/gt_viewed.txt`` for the
        post_view subprocess fallback.

        Best-effort; ignored on any IO error so this stays harmless in tests.
        """
        try:
            with open(LEGACY_VIEWED_PATH, "w", encoding="utf-8") as f:
                for v in self.viewed_files:
                    f.write(v.path + "\n")
        except OSError:
            pass

    def mirror_brief_candidates_to_tmp(self) -> None:
        """Write brief candidates to the legacy tmp file."""
        try:
            with open(LEGACY_BRIEF_CANDIDATES_PATH, "w", encoding="utf-8") as f:
                for p in sorted(self.brief_candidates):
                    f.write(p + "\n")
        except OSError:
            pass

    def mirror_issue_terms_to_tmp(self) -> None:
        """Write issue terms to the legacy tmp file."""
        try:
            with open(LEGACY_ISSUE_TERMS_PATH, "w", encoding="utf-8") as f:
                for t in sorted(self.issue_terms):
                    f.write(t + "\n")
        except OSError:
            pass


__all__ = [
    "AgentPhase",
    "AgentState",
    "FailureSnapshot",
    "IterationBand",
    "L5TrajectoryState",
    "LEGACY_BRIEF_CANDIDATES_PATH",
    "LEGACY_ISSUE_TERMS_PATH",
    "LEGACY_VIEWED_PATH",
    "PendingSuggestion",
    "SearchEvent",
    "SuggestionStatus",
    "ViewedFile",
    "_agent_state_path",
    "_l5_state_path",
    "_state_path",
    "canonical_repo_path",
    "compute_band",
]

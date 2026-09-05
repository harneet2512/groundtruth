"""GT Gateway kernel (G1 + G2-pure) — the mini-swe-agent single-surface completer.

One interception point (the tool-observation constructor), one normalized event, one
acquisition-outcome classifier, existing producers, one dedup chain. Every agent
action (search / view / edit / test / submit) is completed with deterministic FACTS
inside the SAME observation the model already reads.

This module is PURE, DETERMINISTIC, and LLM-free. It **replicates** the semantics of
``artifact_deepswe/gt_mini_patch.py`` (post_search parse + the listen lattice) WITHOUT
importing it — importing gt_mini_patch executes runtime monkeypatches. The search-command
parse here is proven byte-equivalent to that module's ``_search_pattern`` by the golden
corpus in ``tests/runtime/test_gateway.py`` (re-derived from ``test_post_search.py``).

Governing laws (mostly pre-existing; the sub-flag kill-switches below are new, 2026-07-12 —
they make this line's long-standing claim true rather than aspirational):
  * Master flag ``GT_GATEWAY`` — unset/0 => :func:`augment` returns ``[]`` immediately.
  * Producer sub-flags are KILL-SWITCHES at their :func:`augment` call site — default-ON,
    ``GT_CHANGE_SURFACE`` for W-A / ``GT_PATCH_DELTA`` for W-C: unset => the producer fires
    exactly as if the flag did not exist (byte-identical); the literal string ``"0"`` => that
    call site is skipped, the OTHER producer unaffected. This is a DIFFERENT polarity from the
    ``_xxx_on()`` ENABLEMENT flags below (``GT_GATEWAY``, ``GT_REGISTRY_ENFORCE``, ``GT_LOC_RESLOT``,
    ...), which default OFF and require an explicit ``"1"`` to arm a capability. The SAME two env
    var NAMES are also read, with THAT (default-OFF, enablement) polarity, one layer further down
    by the producers' own DOWNSTREAM engines (``change_surface._flag_enabled``,
    ``patch_delta._flag_on``) — those are separate, pre-existing ``rl_profile`` Profile-2/SM-5
    members gating the ENGINES themselves; this call-site kill-switch is additional and needs no
    new ``rl_profile``/``gt_ae_block.sh`` wiring (the var already reaches the engine unchanged).
  * Correct-or-quiet: wrong evidence is worse than none; every ambiguity abstains.
  * ONE PAYLOAD CONTRACT: every produced fact is an
    ``evidence_envelope.EvidenceEnvelope`` (the canonical contract; the former
    ``Addition`` capsule was REPLACED at reconciliation, option (b), 2026-07-10).
    Field mapping: ``fact_kind`` -> ``evidence_type``, ``body_lines`` -> ``payload``,
    ``evidence`` -> ``provenance``, the F1 symbol -> ``fact_id``. Every envelope
    :func:`augment` ships passes ``evidence_envelope.validate`` (a violating
    envelope is DROPPED fail-closed, never delivered).
  * LEAK LAW: no envelope may carry a test-file path/name in its ``target``,
    ``payload``, OR ``provenance``. The single predicate :func:`_is_leaky`
    (== ``not is_deliverable``) gates the target and every provenance row; a
    belt-and-braces scan in :func:`_mk_add` drops any PAYLOAD line carrying a leaky
    path-shaped token; covering-test bodies are additionally routed through the
    EXISTING identity firewall ``native_render.render_covering_failure_native``
    (Format D — never a second scrubber). On any uncertainty a path is treated as
    a test and dropped. ``validate`` law (a) re-checks target+provenance.
  * Need gate: every envelope carries the CANONICAL deterministic ``dedup_key``
    (``evidence_envelope.derive_dedup_key``: producer, evidence_type, target,
    fact_id=the F1 symbol, content=the shipped payload+provenance) — the symbol
    keeps two DISTINCT facts from colliding on a shared def file, and the content
    component lets the SAME symbol re-fire when its def-set changes (freshness).
    STAMP-AT-SEAL (bounce 2026-07-10): :func:`augment` only READS
    ``state.delivered_keys`` for suppression; the DELIVERY COMMIT (the seam's seal
    step — ``adapters.miniswe.seal_delivery(dedup_chain=...)``) stamps the key when
    the bytes are actually appended. So a repeated event yields ``[]`` only once the
    fact was DELIVERED; a produced-but-dropped fact (arbitration loss / leak-guard /
    law-8 budget) is deferred and re-offered, never destroyed.

Rendering is intentionally NOT this module's job: an envelope is a minimal decision
capsule (target + change context + facts + provenance). No ``<gt-*>`` tags,
no agent-facing string assembly beyond deterministic ``file:line`` row formatting.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from groundtruth.delivery.path_policy import is_deliverable
from groundtruth.pretask.curation_map import (
    DETERMINISTIC_RESOLUTION_METHODS,
    parse_edge_metadata,
)
from groundtruth.runtime.covering_runner import (
    _connect_ro,
    _covering_file_on_disk,
    _edge_columns,
)
from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.evidence_envelope import (
    ADVISORY,
    EVENT_STEP0,
    HYPOTHESIS,
    INFO,
    VERIFIED,
    WARNING,
    EvidenceEnvelope,
    render_bytes as _envelope_candidate_bytes,
)
from groundtruth.runtime.evidence_envelope import validate as _validate_envelope
from groundtruth.runtime.feature_lineage import build_lineage
from groundtruth.runtime.fact_registry import (
    EVENTS as fr_EVENTS,
)
from groundtruth.runtime.fact_registry import EVENT_FAILURE_OBS
from groundtruth.runtime.fact_registry import (
    FRESHNESS_SURFACES as fr_FRESHNESS_SURFACES,
)
from groundtruth.runtime.fact_registry import (
    PATCH_BOUND_FACTCLASSES as fr_PATCH_BOUND_FACTCLASSES,
)
from groundtruth.runtime.fact_registry import (
    freshness_surfaces as _fr_freshness_surfaces,
)
from groundtruth.runtime.fact_registry import (
    is_patch_bound as _fr_is_patch_bound,
)
from groundtruth.runtime.fact_registry import (
    is_reactive as _fr_is_reactive,
)
from groundtruth.runtime.fact_registry import (
    registration_for as _fr_registration_for,
)
from groundtruth.runtime.fact_registry import (
    renderable as _renderable,
)
from groundtruth.runtime.fact_registry import (
    required_event as _fr_required_event,
)
from groundtruth.runtime.fact_registry import (
    required_renderer as _fr_required_renderer,
)
from groundtruth.runtime.native_render import render_covering_failure_native
from groundtruth.runtime.producer_audit import ProducerAudit
from groundtruth.runtime.producer_inputs import (
    PRODUCER_INPUTS_SCHEMA,
    CallerEvidenceRow,
    CallerUsageEvidenceRow,
    DefinitionRow,
    ProducerInputs,
    RepositoryWitnessRow,
    SignatureChange,
    SourceState,
)

# Producer engines (traces / change_surface / patch_delta) are OPTIONAL module-scope
# imports (W2 ship-closure, 2026-07-10): try-wrapped so `import gateway` never crashes
# in a container that ships gateway WITHOUT the whole pretask stack
# (anchors/stratum/cochange/confidence). Being under `ast.Try` they are EXCLUDED from
# the injection import-closure guard (which only flags UNGUARDED module-scope imports
# — the real container-startup-crash risk), while staying real module attributes so
# tests can monkeypatch them. Unavailable -> None -> the producer's own try/except
# degrades correct-or-quiet (returns []). On host all three resolve normally.
try:
    from groundtruth.pretask.traces import parse_stack_traces
except Exception:  # noqa: BLE001
    parse_stack_traces = None  # type: ignore[assignment]
try:
    from groundtruth.pretask.change_surface import ROLE_REGISTRATION, detect_change_surface
except Exception:  # noqa: BLE001
    detect_change_surface = None  # type: ignore[assignment]
    ROLE_REGISTRATION = "registration"  # type: ignore[assignment]
try:
    from groundtruth.runtime.patch_delta import analyze_patch_delta
except Exception:  # noqa: BLE001
    analyze_patch_delta = None  # type: ignore[assignment]
# The embedding-backed historical localizer is an isolated comparison control,
# not a default deterministic producer. Keep the injection seam for comparison
# fixtures, but never import/register it in the product runtime. Tests and an
# explicitly constructed control may still replace this module attribute.
_localize = None

__all__ = [
    "ToolEvent",
    "EvidenceEnvelope",  # re-export: the ONE payload contract the Gateway emits
    "CoveringResult",
    "GatewayState",
    "produce_raw",
    "augment",
    "classify_outcome",
    "classify_command",
    "normalize_search",
    "search_pattern",
    # B-12 timing/freshness routing + B-21 revision-scoped latch key
    "route_delivery",
    "delivery_latch_key",
    # B-GW committed-delivery mediation recorder
    "record_committed_delivery",
    "GATEWAY_COMMITTED_SITE",
    "ROUTE_DELIVER",
    "ROUTE_DEFER",
    "ROUTE_EXPIRED_LATE",
    "ROUTE_STALE",
    "ROUTE_REGISTRY_ERROR",
    # kinds
    "KIND_SEARCH",
    "KIND_VIEW",
    "KIND_EDIT",
    "KIND_TEST",
    "KIND_SUBMIT",
    "KIND_OTHER",
    # outcome states
    "EXACT_HIT",
    "SATISFIED",
    "AMBIGUOUS_HIT",
    "FLOOD",
    "WRONG_SURFACE",
    "TRACE_HIT",
    "ZERO_NAME",
    "ZERO_BEHAVIOR",
    "ZERO_ABSENT",
]

# --------------------------------------------------------------------------- #
# kind + outcome-state constants
# --------------------------------------------------------------------------- #
KIND_SEARCH = "search"
KIND_VIEW = "view"
KIND_EDIT = "edit"
KIND_TEST = "test"
KIND_SUBMIT = "submit"
KIND_OTHER = "other"

EXACT_HIT = "EXACT_HIT"  # silence — the agent already has the def
SATISFIED = "SATISFIED"  # silence — nothing to add
AMBIGUOUS_HIT = "AMBIGUOUS_HIT"  # def spans 2-3 files -> def/ref partition
FLOOD = "FLOOD"  # a very large raw hit set -> cut with the def
WRONG_SURFACE = "WRONG_SURFACE"  # every hit is test/vendored; the def is elsewhere
TRACE_HIT = "TRACE_HIT"  # output carries an in-repo stack trace
ZERO_NAME = "ZERO_NAME"  # empty grep; name exists under a fold variant
ZERO_BEHAVIOR = "ZERO_BEHAVIOR"  # empty grep; concept only in function bodies
ZERO_ABSENT = "ZERO_ABSENT"  # empty grep; name+path+body all miss (truly absent)

# Trust tiers come from the canonical envelope module (imported above): VERIFIED /
# WARNING / INFO / HYPOTHESIS — a single vocabulary, never re-declared here.

# Deterministic tier -> default confidence (tier-honest by construction: VERIFIED
# >= 0.7 with provenance; HYPOTHESIS < 0.7). A producer with a REAL measured
# confidence (patch_delta's FACT edges) passes it explicitly instead.
_TIER_DEFAULT_CONF: dict[str, float] = {
    VERIFIED: 0.9,
    WARNING: 0.7,
    INFO: 0.5,
    HYPOTHESIS: 0.5,
}


# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ToolEvent:
    """One normalized agent tool action + its observation (the interception unit)."""

    kind: str
    command: str = ""
    output: str = ""
    exit_status: int | None = None
    cwd: str = ""
    changed_files: tuple[str, ...] = ()
    # Exact structured source-view targets supplied by the harness action.
    # This is the authority for file-view evidence; command text is only a
    # legacy carrier fallback and must never manufacture a viewed path.
    viewed_files: tuple[str, ...] = ()
    action_index: int = 0
    # Optional seam bridges — NOT part of the raw observation; injected by the seam
    # when it has them (kept off the 7 core fields so the observation stays pure).
    edit_before_after: "Mapping[str, tuple[str | None, str]] | None" = None
    covering: "CoveringResult | None" = None
    # Fine semantic truth, distinct from the command carrier. A direct compiled
    # test binary is commonly carried as ``other`` but semantically observes a
    # ``test_result``. Conversely, an edit-shaped command that changed no bytes
    # has an ``edit`` carrier but no ``edit_result``.
    carrier_kind: str = ""
    semantic_events: tuple[str, ...] = ()
    primary_boundary: str = ""
    test_outcome: str = ""
    test_protocol: str = ""
    state_revision: str = ""
    # True when the environment boundary supplied the semantic set from exact
    # pre/post state. In that mode an empty set is authoritative (a no-op), not
    # an invitation to infer an event from command spelling.
    semantics_authoritative: bool = False


@dataclass
class CoveringResult:
    """A pre-computed covering-test verdict injected on a test event.

    The Gateway does NOT execute anything; running the covering test is seam work
    (``covering_runner`` + the executor bridge). The kernel only wraps the verdict.
    """

    target: str
    verdict: str = ""
    body_lines: list[str] = field(default_factory=list)
    evidence: list[tuple[str, int]] = field(default_factory=list)
    tier: str = WARNING
    # test files the runner executed — sharpens the firewall's identity match
    test_files: tuple[str, ...] = ()


@dataclass
class GatewayState:
    """Per-run mutable context — a thin facade over the canonical :class:`EpisodeState`.

    The probe-stem ``ledger``, the ``delivered_keys`` dedup chain, and the
    ``edit_events`` record are OWNED by the embedded :class:`EpisodeState` (W1a
    strangler consolidation, 2026-07-10): they were fragmented state (the ledger is
    field-identical to the mini seam's ``_search_seen``; ``delivered_keys`` duplicated
    ``_oracle_delivered_hashes``). They are exposed here as delegating properties so
    every producer's in-place mutation (``state.ledger[stem] = e`` /
    ``state.delivered_keys.add(k)`` / ``state.edit_events.append(...)``) is byte-
    identical, while storage has exactly ONE owner. Construct with a caller-supplied
    ``episode`` (the W2 seam passes the one live episode) or let it create a fresh one."""

    graph_db: str | None = None
    repo_root: str = ""
    issue_text: str = ""
    graph_revision: str = ""
    # Attempt-scoped canonical revision context.  Legacy gateway operation may
    # omit it and remains byte-identical; canonical evidence is attached only
    # when the live AttemptReasoningRuntime supplies the complete vector.
    canonical_revision: object | None = None
    episode: EpisodeState = field(default_factory=EpisodeState)
    # SM-4 (2026-07-11): the per-run LIGHTWEIGHT episode overlay — ``{norm_file_path:
    # overlay_entry}`` built per structured edit (edit_overlay.build_episode_overlay_entry),
    # where ``overlay_entry['changed']`` is the mark_stale_envelopes verdict that the file's
    # base graph.db structure no longer matches its CURRENT content. Passed IN by the seam
    # each turn (the persistent store lives on the seam beside the episode); read by
    # :func:`route_delivery` under ``GT_EDIT_OVERLAY`` to drop a base-read fact about an
    # edited file. Empty (default) -> byte-identical, the overlay is a no-op.
    episode_overlay: dict = field(default_factory=dict)
    # Optional host-side instrumentation sink. The gateway owns these decisions,
    # while the live seam owns durable ledger I/O. None is an exact no-op.
    control_recorder: object | None = None
    # Optional producer-causal instrumentation.  The recorder receives immutable
    # ``gt.producer_invocation.v1`` dictionaries; the context is published by the
    # canonical seam immediately before producer execution.  Neither is read by
    # producer logic, routing, arbitration, rendering, or delivery.
    producer_recorder: object | None = None
    producer_audit_context: object = field(default_factory=dict)

    @property
    def ledger(self) -> dict[str, dict[str, object]]:
        """The probe-stem ledger ``{stem: {probed_forms, probe_indices, outcomes}}`` —
        owned by :attr:`episode`.``probe_ledger`` (field-identical shape)."""
        return self.episode.probe_ledger

    @ledger.setter
    def ledger(self, value: dict[str, dict[str, object]]) -> None:
        self.episode.probe_ledger = value

    @property
    def delivered_keys(self) -> set[str]:
        """THE delivered-dedup chain (opaque dedup_key strings) — owned by
        :attr:`episode`.``delivered_dedup``."""
        return self.episode.delivered_dedup

    @delivered_keys.setter
    def delivered_keys(self, value: set[str]) -> None:
        self.episode.delivered_dedup = value

    @property
    def edit_events(self) -> list[dict[str, object]]:
        """The ordered EDIT record ``{"index": action_index, "blob": lowercase text of
        the edit's command + changed paths + after-content}`` — owned by
        :attr:`episode`.``edit_events``. The honest-negative gate mutes only on an
        edit RELATED to the probed stem (F4), never on any edit."""
        return self.episode.edit_events

    @edit_events.setter
    def edit_events(self, value: list[dict[str, object]]) -> None:
        self.episode.edit_events = value


def _producer_audit(
    state: GatewayState,
    event: ToolEvent,
    *,
    producer: str,
    evidence_types: tuple[str, ...],
    invocation_site: str,
    subject: str = "",
) -> ProducerAudit:
    """Create one optional, behavior-neutral producer invocation trace."""

    return ProducerAudit(
        recorder=(state.producer_recorder if callable(state.producer_recorder) else None),
        producer=producer,
        evidence_types=evidence_types,
        invocation_site=invocation_site,
        event_type=str(
            event.primary_boundary or (event.semantic_events[0] if event.semantic_events else "")
        ),
        subject=subject,
        action_index=event.action_index,
        context=state.producer_audit_context,
        delivered_keys=state.delivered_keys,
    )


# --------------------------------------------------------------------------- #
# flags
# --------------------------------------------------------------------------- #
def _gateway_on() -> bool:
    return os.environ.get("GT_GATEWAY", "").strip().lower() not in ("", "0", "false", "no", "off")


def _change_surface_producer_on() -> bool:
    """KILL-SWITCH for the W-A ``_produce_change_surface`` call site (gateway.py:16). DEFAULT
    ON: unset => ``True`` => the producer fires exactly as it did before this gate existed
    (byte-identical). Only the literal string ``"0"`` disables it — ``"1"``/garbage/anything
    else still fires. NOT an enablement flag like :func:`_gateway_on`/:func:`_loc_reslot_on`
    (those default OFF): this is a plain kill-switch layered on top of the SAME env var name's
    PRE-EXISTING, differently-polarized (default-OFF) reading one call further down, inside
    ``change_surface.detect_change_surface`` itself (``change_surface._flag_enabled``) — that
    downstream flag + its ``rl_profile`` Profile-2/SM-5 membership + its ``gt_ae_block.sh``
    forward are untouched by this gate and MUST NOT be duplicated/re-added here; a kill-switch
    is not a new capability to enable."""
    return os.environ.get("GT_CHANGE_SURFACE", "1").strip() != "0"


# B-NFP (2026-07-16): producer-owned snapshots for the newfile_precedent REGISTRATION claim,
# captured AT PRODUCTION TIME so the offline attestation can re-derive the delivered precedent
# from the producer's OWN inputs (the registry bytes + sibling members + a git commit anchor)
# rather than reconstructing it from the output. Stashed keyed by the delivered candidate id
# (envelope dedup_key); the seam pops it at the gateway seal site. Byte-identical to delivery:
# this only records a side-car and NEVER alters the delivered envelope/bytes.
_NEWFILE_PRECEDENT_SNAPSHOTS: dict = {}
_NEWFILE_PRECEDENT_SNAPSHOT_CAP = 64
_REPO_HEAD_CACHE: dict = {}


def _repo_head(repo_root: str) -> str:
    """The repo HEAD commit sha (the freshness commit anchor), or ``""`` when git is
    unavailable. One bounded ``git rev-parse HEAD`` per repo_root, cached. Correct-or-quiet:
    any fault yields ``""`` (freshness then stays honestly UNMEASURED)."""
    if not repo_root:
        return ""
    if repo_root in _REPO_HEAD_CACHE:
        return _REPO_HEAD_CACHE[repo_root]
    head = ""
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode == 0 and len(out) == 40 and all(c in "0123456789abcdef" for c in out):
            head = out
    except Exception:  # noqa: BLE001 — git absent/slow/erroring is not a signal
        head = ""
    _REPO_HEAD_CACHE[repo_root] = head
    return head


def _stash_newfile_precedent_snapshot(candidate_id: str, snapshot) -> None:
    """Record one producer-owned registration snapshot keyed by the delivered candidate id.
    Bounded (drops the oldest when full); never raises into the delivery path."""
    if not candidate_id or snapshot is None:
        return
    try:
        if len(_NEWFILE_PRECEDENT_SNAPSHOTS) >= _NEWFILE_PRECEDENT_SNAPSHOT_CAP:
            _NEWFILE_PRECEDENT_SNAPSHOTS.pop(next(iter(_NEWFILE_PRECEDENT_SNAPSHOTS)), None)
        _NEWFILE_PRECEDENT_SNAPSHOTS[candidate_id] = snapshot
    except Exception:  # noqa: BLE001 — a side-car stash can never break delivery
        return


def pop_newfile_precedent_snapshot(candidate_id: str):
    """Pop the producer-owned snapshot for a delivered candidate id (the seam calls this at
    the gateway seal site). ``None`` when none was captured (attestation then not persisted)."""
    return _NEWFILE_PRECEDENT_SNAPSHOTS.pop(candidate_id, None) if candidate_id else None


def _patch_delta_producer_on() -> bool:
    """KILL-SWITCH for the W-C ``_produce_patch_delta`` call site — the ``GT_PATCH_DELTA``
    sibling of :func:`_change_surface_producer_on` (same default-ON / literal-``"0"``-disables
    contract; same independence from ``patch_delta``'s own downstream ``_flag_on`` enablement
    flag and its existing ``rl_profile``/``gt_ae_block.sh`` wiring — see that function's
    docstring for the full rationale)."""
    return os.environ.get("GT_PATCH_DELTA", "1").strip() != "0"


def _registry_enforce() -> bool:
    """SM-0 Super-Mode master flag. When ON, the fact-registry becomes EXECUTABLE: the
    firing event is checked against each class's DECLARED ``deliver_by`` (via the registry,
    not the producer's self-stamp) and a renderer-less class is dropped. Default OFF ⇒ the
    Gateway is byte-identical to pre-SM-0 (only ``renderable`` membership + the self-stamped
    ``preferred_event`` timing)."""
    return os.environ.get("GT_REGISTRY_ENFORCE", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _episode_overlay_on() -> bool:
    """SM-4 flag (2026-07-11). The episode-overlay freshness DROP rides ``GT_EDIT_OVERLAY``
    — the same flag that arms the transactional edit overlay in the seam; SM-4 gives it its
    model-facing value: a base-read fact that is STALE against the CURRENT edit state is
    dropped, not shipped. Default OFF ⇒ ``route_delivery`` is byte-identical (the overlay is
    never consulted)."""
    return os.environ.get("GT_EDIT_OVERLAY", "").strip().lower() in ("1", "true", "yes", "on")


def _loc_reslot_on() -> bool:
    """T0->T2 localization re-slot flag (2026-07-12). Arms the reactive
    :func:`_produce_ranked_localization` producer at the D2/post-search boundary, so GT's
    RANKED localization answer (from ``localize()``, memoized per episode) rides the agent's
    OWN grep channel as ``path:line:sym`` rows instead of only the T0 baked ``<gt-localization>``
    brief tag. Default OFF ⇒ the producer is never dispatched from :func:`augment` ⇒
    byte-identical (no ``localization`` envelope is ever produced by the Gateway)."""
    return os.environ.get("GT_LOC_RESLOT", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _ss_arbiter_v2_on() -> bool:
    """SS-1 flag ``GT_SS_ARBITER_V2`` (2026-07-13). Arms the SS-V2 arbiter behaviors; in THIS
    module it gates the EMPTY-PAYLOAD guard in :func:`augment` — a produced envelope that
    would render ZERO bytes (empty ``payload``) is dropped so it can never enter the global
    pool and produce a ``chars=0`` delivered ledger row (the hydra-3005 empty-payload
    delivery). Default OFF ⇒ the guard is skipped ⇒ :func:`augment` is byte-identical. The
    same flag is read by ``global_arbiter.arbitrate`` (the ranked-competition v2 branches)."""
    return os.environ.get("GT_SS_ARBITER_V2", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _envelope_has_bytes(env: EvidenceEnvelope) -> bool:
    """True iff the envelope carries at least one non-whitespace payload line — i.e. it will
    render non-empty model-facing bytes. An envelope whose ``payload`` is empty (or all-blank
    after the leak filter) renders nothing; SS-V2 drops it (:func:`_ss_arbiter_v2_on`)."""
    return any(str(ln).strip() for ln in (env.payload or ()))


def _xsession_on() -> bool:
    """SM-9c flag (2026-07-11). Arms the CROSS-SESSION learned-delivery policy: the Gateway
    consults this repo's durable causal-consumption ledger (loaded once at task-start and
    threaded onto ``state.episode.xsession_policy``) to SUPPRESS a fact-class this repo
    NEVER once consumed and RANK-UP a class it historically acted on. Default OFF (and an
    empty policy) ⇒ :func:`_apply_xsession_policy` is a byte-identical no-op."""
    return os.environ.get("GT_XSESSION_MEMORY", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _record_control(
    state: GatewayState,
    feature_id: str,
    decision_site: str,
    decision: str,
    *,
    candidate_bytes: str = "",
    fact_class: str | None = None,
    candidate_id: str = "",
    reason: str = "",
) -> None:
    """Forward one terminal control decision to the seam's typed metadata sink."""
    recorder = getattr(state, "control_recorder", None)
    if not callable(recorder):
        return
    try:
        extra = {"candidate_bytes": candidate_bytes, "reason": reason}
        if fact_class is not None:
            extra["fact_class"] = fact_class
        if candidate_id:
            extra["candidate_id"] = candidate_id
        recorder(feature_id, decision_site, decision, **extra)
    except Exception:  # instrumentation never changes gateway routing
        pass


def _candidate_control_identity(env: EvidenceEnvelope) -> tuple[str, str]:
    """Return the gateway candidate's exact canonical bytes and concrete FACT class.

    This is the post-control envelope commitment, before the mini-seam chooses a
    tagged/native presentation.  It is deterministic and never guesses a class:
    an unregistered envelope has no mediator identity and is rejected upstream.
    """
    registration = _fr_registration_for(env.evidence_type)
    if registration is None:
        raise ValueError(f"unregistered evidence type: {env.evidence_type!r}")
    return (
        _envelope_candidate_bytes(env).decode("utf-8", "strict"),
        registration.fact_class,
    )


# The seam-committed GT_GATEWAY mediation site (B-GW, 2026-07-16). The admission-time row
# (:func:`augment` -> ``gateway.augment.candidate_admission``) stamps the PRE-render
# candidate bytes (``_candidate_control_identity`` == ``_envelope_candidate_bytes``); the
# seam then RE-renders + RE-keys the winner to its tagged/native presentation, so the
# admission seal can NEVER equal the delivered ``content_sha256_16`` and its mediator join
# can never close. :func:`record_committed_delivery` writes a SUPPLEMENTARY GT_GATEWAY row
# at THIS site carrying the COMMITTED-delivery identity — the seam's own final shipped bytes
# + the delivered candidate_id — so the exact join (chars + seal + candidate_id + iteration)
# closes, exactly as GT_GATEWAY_NATIVE already does. The admission row is preserved (it is
# the decision-precedes-delivery chronology anchor).
GATEWAY_COMMITTED_SITE = "mini_seam.gateway.candidate_committed"


def record_committed_delivery(
    env: EvidenceEnvelope,
    final_bytes: str,
    recorder: object | None,
    *,
    decision: str = "APPLIED",
    reason: str = "candidate_committed",
) -> None:
    """Record the GT_GATEWAY mediator at the COMMITTED-delivery identity.

    Called by the gateway's OWN delivery path (the seam's ``_record_gateway_final_controls``,
    where the winner's final shipped bytes are known) once the winner is committed. It emits
    ONE GT_GATEWAY control row at :data:`GATEWAY_COMMITTED_SITE` whose ``candidate_bytes`` are
    EXACTLY the caller's committed ``final_bytes`` (the bytes actually appended to the model's
    observation) and whose ``candidate_id`` is the winner's canonical ``dedup_key`` — the same
    identity the delivery ledger row carries — so the grader's exact mediation join closes.

    NON-FORGING + correct-or-quiet: the recorded seal is derived from the caller's real
    committed bytes, never copied from an unrelated source; it emits NOTHING (never a
    fabricated identity) when there is no recorder, when ``final_bytes`` is empty (nothing was
    shipped), when the class is unregistered, or when the envelope has no canonical
    ``dedup_key``. Instrumentation only — it never changes routing or the delivered bytes."""
    if not callable(recorder) or not final_bytes:
        return
    try:
        registration = _fr_registration_for(env.evidence_type)
        candidate_id = getattr(env, "dedup_key", "") or ""
        if registration is None or not candidate_id:
            return
        recorder(
            "GT_GATEWAY",
            GATEWAY_COMMITTED_SITE,
            decision,
            candidate_bytes=final_bytes,
            fact_class=registration.fact_class,
            candidate_id=candidate_id,
            reason=reason,
        )
    except Exception:  # noqa: BLE001 — instrumentation never changes gateway routing
        return


# SM-9c FIRST SLICE — the learned policy acts on exactly ONE fact class. Bounding the
# effect to ``caller_contract`` keeps the first cross-session slice small + auditable; the
# remaining classes are OWED (they pass through untouched, in their original order).
_XSESSION_POLICY_CLASSES: frozenset[str] = frozenset({"caller_contract"})


def _apply_xsession_policy(
    out: list[EvidenceEnvelope], state: GatewayState
) -> list[EvidenceEnvelope]:
    """Adapt the produced candidate list from this repo's LEARNED cross-session policy.

    THE LEARNED BEHAVIOUR (SM-9c, ONE class = ``caller_contract`` for the first slice):
      * SUPPRESS — drop a candidate whose canonical class this repo delivered
        ``>= MIN_SAMPLES`` times and NEVER once consumed (``is_inert``). Correct-or-quiet,
        learned from the repo's own causal history — this is the winner-CHANGING lever
        (removing the candidate lets a different class win the per-turn dose, or nothing).
      * RANK-UP — stable-reorder so a historically-CONSUMED policy class sorts ahead of a
        same-tier non-consumed one (observable candidate ranking). This SORT alone cannot
        promote a WINNER (the adapter's arbiter picks by a total order, not list position);
        the end-to-end winner promotion is delivered by :func:`_apply_xsession_rankup` (behind
        ``GT_XSESSION_RANKUP``), which stamps a ladder-derived arbitration boost the adapter
        adds to the class's severity rank — closing the "OWED to the adapter" gap.

    DETERMINISM-GIVEN-INPUTS: the ONLY state read is ``state.episode.xsession_policy`` (the
    DECLARED, loaded-once store snapshot) — never os.environ, time, or a module global. So
    the SAME ``(candidates, policy)`` -> the SAME result, and a DIFFERENT policy -> a
    deterministic change. FLAG-OFF / EMPTY-POLICY: returns the input list UNTOUCHED (the
    SAME object), so :func:`augment` is byte-identical when the feature is off."""
    if not out or not _xsession_on():
        return out
    policy = getattr(getattr(state, "episode", None), "xsession_policy", None)
    if not policy:
        return out
    try:
        from groundtruth.runtime.xsession_memory import (
            canonical_class as _xm_canonical,
            is_inert as _xm_inert,
            policy_rank as _xm_rank,
        )
    except Exception:  # noqa: BLE001 — engine absent in-container: preserve behaviour
        return out
    kept: list[EvidenceEnvelope] = []
    for a in out:
        try:
            canon = _xm_canonical(a.evidence_type)
            inert = bool(canon in _XSESSION_POLICY_CLASSES and _xm_inert(policy, canon))
        except Exception as exc:
            _record_control(
                state,
                "GT_XSESSION_MEMORY",
                "gateway.xsession_policy.inert_suppression",
                "ERROR",
                reason=f"policy_error:{type(exc).__name__}",
            )
            kept.append(a)
            continue
        if canon in _XSESSION_POLICY_CLASSES:
            _record_control(
                state,
                "GT_XSESSION_MEMORY",
                "gateway.xsession_policy.inert_suppression",
                "APPLIED" if inert else "NO_EFFECT",
                reason="inert_class" if inert else "class_not_inert",
            )
        if inert:
            continue  # LEARNED SUPPRESS: delivered here before, never once consumed
        kept.append(a)
    # LEARNED RANK-UP: stable sort (Python sort is stable) by descending policy rank; a
    # non-policy / non-consumed class has rank 0 and keeps its original relative order.
    kept.sort(
        key=lambda a: (
            -_xm_rank(
                policy,
                _xm_canonical(a.evidence_type)
                if _xm_canonical(a.evidence_type) in _XSESSION_POLICY_CLASSES
                else None,
            )
        )
    )
    return kept


def _xsession_rankup_on() -> bool:
    """SM-9c rank-up flag ``GT_XSESSION_RANKUP`` (2026-07-12). Arms the cross-session WINNER
    PROMOTION: a historically-CONSUMED policy class earns a ladder-derived arbitration boost
    (stamped onto the envelope's non-identity ``native_args`` side-car) so it can WIN the
    adapter's ``<= 1``-dose arbitration — the piece the SM-9c rank-up SORT could not deliver
    (the order-independent arbiter ignores list order). Default OFF (and an empty policy) ⇒
    :func:`_apply_xsession_rankup` is a byte-identical no-op (nothing stamped)."""
    return os.environ.get("GT_XSESSION_RANKUP", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _apply_xsession_rankup(
    out: list[EvidenceEnvelope], state: GatewayState
) -> list[EvidenceEnvelope]:
    """Stamp the LEARNED rank-up BOOST onto each already-eligible candidate whose canonical
    class this repo historically CONSUMED (``ladder_boost`` > 0). The boost rides the envelope's
    ``native_args`` side-car (identity-/serialization-neutral, ``compare=False``) under the
    reserved key ``_xsession_boost``; the adapter's arbiter ADDS it to the class's severity rank
    so a memory-consumed fact can WIN the per-turn ``<= 1`` dose — the winner promotion the SM-9c
    candidate SORT alone cannot deliver (``arbitrate`` picks by a total order, not list position).

    A RE-RANK ONLY, never a BYPASS: ``augment`` calls this LAST — after the render-gate /
    registry-enforce / leak / timing-freshness / dedup filters — so every candidate it touches is
    ALREADY on-time + fresh + non-acquired. The boost changes WHICH single dose wins; it never
    resurrects a dropped fact, never adds a dose, and (being ``>= 0``) never demotes one. Bounded
    to ``_XSESSION_POLICY_CLASSES`` (the SAME first slice the suppress path uses).

    DETERMINISM-GIVEN-INPUTS + BYTE-IDENTICAL-OFF: reads ONLY ``state.episode.xsession_policy``
    (never os.environ beyond the flag, time, or a module global). When ``GT_XSESSION_RANKUP`` is
    off, the policy is empty, or NO candidate earns a boost, it returns the input list UNCHANGED
    (the SAME object), so ``augment`` is byte-identical when the feature is off."""
    if not out or not _xsession_rankup_on():
        return out
    try:
        from groundtruth.runtime.xsession_memory import (
            canonical_class as _xm_canonical,
            ladder_boost as _xm_boost,
        )
    except Exception:  # noqa: BLE001 — engine absent in-container: preserve behaviour
        return out
    policy = getattr(getattr(state, "episode", None), "xsession_policy", None)
    if not policy:
        # DARK-FIX (2026-07-19): an empty cross-session policy (the common single-session
        # case: no prior run of this repo) is a CORRECT ABSTENTION, not an invisible no-op.
        # Witness it — emit a NO_EFFECT control.participation receipt for each participating
        # candidate so the REFEREE-3 terminal can bind an eligible-but-correctly-silent
        # verdict instead of grading the control DARK/UNMEASURED. Delivery is byte-identical:
        # the input list is returned UNCHANGED (the SAME object); the receipt is
        # observability-only and never mutates a candidate.
        for a in out:
            if _xm_canonical(a.evidence_type) not in _XSESSION_POLICY_CLASSES:
                continue
            try:
                candidate_bytes, fact_class = _candidate_control_identity(a)
                _record_control(
                    state,
                    "GT_XSESSION_RANKUP",
                    "gateway.xsession_rankup.boost",
                    "NO_EFFECT",
                    candidate_bytes=candidate_bytes,
                    fact_class=fact_class,
                    candidate_id=a.dedup_key,
                    reason="cold_policy",
                )
            except Exception as exc:  # noqa: BLE001 — measurement never changes delivery
                _record_control(
                    state,
                    "GT_XSESSION_RANKUP",
                    "gateway.xsession_rankup.boost",
                    "ERROR",
                    reason=f"identity_error:{type(exc).__name__}",
                )
        return out
    changed = False
    stamped: list[EvidenceEnvelope] = []
    for a in out:
        canon = _xm_canonical(a.evidence_type)
        boost = _xm_boost(policy, canon) if canon in _XSESSION_POLICY_CLASSES else 0
        if boost > 0:
            na = dict(a.native_args or {})
            na["_xsession_boost"] = int(boost)  # reserved arbitration side-car key
            boosted = replace(a, native_args=na)
            stamped.append(boosted)
            changed = True
            try:
                candidate_bytes, fact_class = _candidate_control_identity(boosted)
                _record_control(
                    state,
                    "GT_XSESSION_RANKUP",
                    "gateway.xsession_rankup.boost",
                    "APPLIED",
                    candidate_bytes=candidate_bytes,
                    fact_class=fact_class,
                    candidate_id=boosted.dedup_key,
                    reason=f"ladder_boost:{int(boost)}",
                )
            except Exception as exc:
                _record_control(
                    state,
                    "GT_XSESSION_RANKUP",
                    "gateway.xsession_rankup.boost",
                    "ERROR",
                    reason=f"identity_error:{type(exc).__name__}",
                )
        else:
            stamped.append(a)
            if canon in _XSESSION_POLICY_CLASSES:
                try:
                    candidate_bytes, fact_class = _candidate_control_identity(a)
                    _record_control(
                        state,
                        "GT_XSESSION_RANKUP",
                        "gateway.xsession_rankup.boost",
                        "NO_EFFECT",
                        candidate_bytes=candidate_bytes,
                        fact_class=fact_class,
                        candidate_id=a.dedup_key,
                        reason="no_ladder_boost",
                    )
                except Exception as exc:
                    _record_control(
                        state,
                        "GT_XSESSION_RANKUP",
                        "gateway.xsession_rankup.boost",
                        "ERROR",
                        reason=f"identity_error:{type(exc).__name__}",
                    )
    return stamped if changed else out  # SAME object when nothing stamped (byte-identical)


# --------------------------------------------------------------------------- #
# LEAK LAW — the single chokepoint (== not is_deliverable), fail-closed on error.
# --------------------------------------------------------------------------- #
def _is_leaky(path_or_symbol: str) -> bool:
    """True iff ``path_or_symbol`` must NOT surface as a delivered fact. A path-shaped
    value goes through ``is_deliverable`` (test/demo/docs OR vendored/generated => leaky);
    a bare symbol (no path shape) is not path-leaky. Any error => treat as leaky (drop)."""
    x = (path_or_symbol or "").strip()
    if not x:
        return False
    try:
        return not is_deliverable(x)
    except Exception:  # noqa: BLE001 — unsure => treat as a test => drop
        return True


# --------------------------------------------------------------------------- #
# path helpers (ported: repo-relative keys matching graph.db)
# --------------------------------------------------------------------------- #
_CONTAINER_ROOTS = ("/testbed/", "/home/user/", "/workspace/", "/app/", "/repo/")


def _norm_fp(file_path: str) -> str:
    p = (file_path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _to_repo_rel(f: str, root: str) -> str:
    if not f:
        return f
    nf = f.replace("\\", "/")
    if nf.startswith("/"):
        for cr in _CONTAINER_ROOTS:
            if nf.startswith(cr):
                return nf[len(cr) :]
        if root:
            nroot = root.replace("\\", "/").rstrip("/") + "/"
            if nf.startswith(nroot):
                return nf[len(nroot) :]
        try:
            return os.path.relpath(f, root) if root else f
        except (ValueError, TypeError):
            return f
    return f


# --------------------------------------------------------------------------- #
# FRAME ORIGIN — the repository-membership law for a delivered stack frame.
#
# 2026-07-28, run 30390877219. ``_produce_trace`` shipped three frames labelled
# "deepest in-repo frame" that were NOT in the repository:
#   yaml/constructor.py:427   (a site-packages dependency)
#   regex/regex.py:254        (a site-packages dependency)
#   <string>:18               (a ``python -c`` script — not a file at all)
# ROOT CAUSE (read, not assumed): ``pretask.traces._is_in_repo`` DOES screen for
# membership, but for a RELATIVE path it computes ``os.path.realpath(path)``, which
# resolves against the CURRENT PROCESS CWD. In-container the gateway runs with cwd ==
# repo_root, so EVERY relative frame satisfies ``realpath(path).startswith(repo_root)``
# and returns True BEFORE the existence fallback runs. ``site-packages/`` was already
# stripped by ``parse_stack_traces`` normalization, so the bad-marker screen was blind
# to it too. This classifier re-decides membership AT the delivery site, against
# ``repo_root`` ONLY — never against the process cwd — and only REPOSITORY ships.
# --------------------------------------------------------------------------- #
class FrameOrigin(str, Enum):
    """Where a parsed stack frame actually lives. Only REPOSITORY is deliverable."""

    REPOSITORY = "REPOSITORY"
    DEPENDENCY = "DEPENDENCY"
    GENERATED = "GENERATED"
    UNRESOLVED = "UNRESOLVED"


# Path SEGMENTS (never substrings) that mark third-party/installed code.
_VENDOR_SEGMENTS: frozenset[str] = frozenset(
    {
        "site-packages",
        "dist-packages",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        ".tox",
        ".cargo",
        ".gem",
        ".pub-cache",
        "bower_components",
        "third_party",
    }
)
# Segments marking machine-emitted / build-output code the agent must not be sent to.
_GENERATED_SEGMENTS: frozenset[str] = frozenset({"build", "dist"})


def classify_frame_origin(path: str, repo_root: str) -> FrameOrigin:
    """Classify one stack-frame path's ORIGIN relative to ``repo_root``.

    * ``REPOSITORY``  — carries no vendor/generated segment, resolves UNDER
      ``repo_root``, and EXISTS on disk there (``covering_runner._covering_file_on_disk``
      — the repo's one on-disk predicate, reused, never re-implemented).
    * ``DEPENDENCY``  — carries a vendor segment (site-packages, dist-packages,
      node_modules, vendor/, .venv, ``$GOPATH/pkg/mod``), or is an absolute path
      that resolves OUTSIDE ``repo_root``.
    * ``GENERATED``   — a pseudo-file (``<string>``, ``<stdin>``, ``<frozen ...>``) or
      machine-emitted output (``*_pb2.py``, ``build/``, ``dist/``).
    * ``UNRESOLVED``  — everything else, including every path that cannot be proven
      to exist under ``repo_root`` (no ``repo_root`` given => nothing to be a member OF).

    CORRECT-OR-QUIET: the caller delivers ONLY ``REPOSITORY``; any fault classifies
    ``UNRESOLVED``, so an unclassifiable frame is suppressed rather than mislabelled.
    """
    try:
        raw = (path or "").strip()
        if not raw:
            return FrameOrigin.UNRESOLVED
        norm = raw.replace("\\", "/")
        # 1. pseudo-files first: they are not paths at all, so no path test applies.
        if norm.startswith("<") and norm.endswith(">"):
            return FrameOrigin.GENERATED
        # COLLAPSE INTERIOR ``..`` BEFORE ANY RULE READS THE PATH. Without this the
        # escape test below only catches a LEADING ``../``: ``a/../../outside.py`` keeps
        # its interior ``..``, passes the startswith test, and then ``os.path.join`` in
        # the existence oracle collapses it for real -- so a file in the repo's PARENT
        # resolves, exists, and classifies REPOSITORY. Measured, not theorised.
        norm = os.path.normpath(norm).replace("\\", "/")
        segments = [s for s in norm.split("/") if s and s != "."]
        if not segments:
            return FrameOrigin.UNRESOLVED
        # 2. vendor segments (installed third-party code).
        if any(s in _VENDOR_SEGMENTS for s in segments):
            return FrameOrigin.DEPENDENCY
        # $GOPATH/pkg/mod — only as a NON-leading pair; a repo's own top-level
        # ``pkg/mod/...`` is repository source, not the module cache.
        for i in range(1, len(segments) - 1):
            if segments[i] == "pkg" and segments[i + 1] == "mod":
                return FrameOrigin.DEPENDENCY
        # 3. generated/build output.
        if segments[-1].endswith("_pb2.py") or segments[-1].endswith("_pb2_grpc.py"):
            return FrameOrigin.GENERATED
        if any(s in _GENERATED_SEGMENTS for s in segments):
            return FrameOrigin.GENERATED
        # 4. membership. No root => nothing to be a member of (fail closed).
        if not repo_root:
            return FrameOrigin.UNRESOLVED
        if os.path.isabs(norm):
            rel = _to_repo_rel(norm, repo_root).replace("\\", "/")
            # Still absolute, or it escaped the root ("../"): it resolves OUTSIDE the
            # repository — an installed/system file, never an editable repo target.
            if os.path.isabs(rel) or rel.startswith("../"):
                return FrameOrigin.DEPENDENCY
            norm = rel
        norm = _norm_fp(norm)
        if not norm:
            return FrameOrigin.UNRESOLVED
        if norm.startswith("../"):
            return FrameOrigin.DEPENDENCY
        # 5. EXISTENCE under repo_root — resolved against the ROOT, never the cwd.
        # A stack frame names a FILE. ``_covering_file_on_disk`` is an ``os.path.exists``
        # probe, which also answers True for a DIRECTORY -- the predecessor screen used
        # the stricter ``isfile`` (pretask/traces.py:171), so keep that strictness here.
        if _covering_file_on_disk(repo_root, norm) and os.path.isfile(
            os.path.join(repo_root, norm)
        ):
            return FrameOrigin.REPOSITORY
        return FrameOrigin.UNRESOLVED
    except Exception:  # noqa: BLE001 — correct-or-quiet: unclassifiable => suppressed
        return FrameOrigin.UNRESOLVED


# --------------------------------------------------------------------------- #
# SEARCH normalization (byte-equivalent to gt_mini_patch._search_pattern).
# --------------------------------------------------------------------------- #
_GREP_HEAD_RE = re.compile(r"(?:^|[|&;]\s*)(?:grep|egrep|fgrep|rg)\b")
_BARE_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
# Flags that CONSUME the next token as a value (so it is never mistaken for the
# pattern operand). -E / -T are DELIBERATELY absent (valueless in GNU grep).
_GREP_VALUE_FLAGS = frozenset(
    {
        "-e",
        "--regexp",
        "-f",
        "--file",
        "-m",
        "--max-count",
        "-A",
        "-B",
        "-C",
        "--context",
        "--after-context",
        "--before-context",
        "-d",
        "-t",
        "--type",
        "--type-not",
        "-g",
        "--glob",
        "--iglob",
        "--include",
        "--exclude",
        "--exclude-dir",
        "--exclude-from",
        "-M",
        "--max-columns",
        "-j",
        "--threads",
        "--encoding",
    }
)


@dataclass(frozen=True)
class SearchQuery:
    """The normalized shape of a grep/rg command."""

    pattern: str  # bare-symbol operand (== search_pattern), else ""
    raw_operand: str  # dequoted operand even when not a bare symbol
    scope: tuple[str, ...]  # path operands
    filters: tuple[str, ...]  # flag tokens (best-effort)


def _search_operand_raw(cmd: str) -> str | None:
    """The DEQUOTED pattern operand of a grep/rg command, or None (no bare gate)."""
    head = (cmd or "").split("\n", 1)[0]
    m = _GREP_HEAD_RE.search(head)
    if not m:
        return None
    try:
        toks = shlex.split(head[m.end() :])
    except ValueError:
        return None
    pat: str | None = None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--":
            pat = toks[i + 1] if i + 1 < len(toks) else None
            break
        if t.startswith("-"):
            if "=" in t:
                flag, _, val = t.partition("=")
                if flag in ("-e", "--regexp"):
                    pat = val
                    break
                i += 1
                continue
            if t in ("-e", "--regexp") and i + 1 < len(toks):
                pat = toks[i + 1]
                break
            i += 2 if t in _GREP_VALUE_FLAGS else 1
            continue
        pat = t
        break
    if not pat:
        return None
    return pat.strip().strip("'\"")


def search_pattern(cmd: str) -> str | None:
    """The grep/rg operand, ABSTAINING unless it is a single bare symbol (>=3 chars,
    no regex metachars / path separators). Byte-equivalent to gt_mini_patch._search_pattern."""
    pat = _search_operand_raw(cmd)
    if pat is None:
        return None
    return pat if _BARE_SYMBOL_RE.match(pat) else None


def normalize_search(cmd: str) -> SearchQuery | None:
    """Parse a grep/rg command into (pattern, raw_operand, scope, filters). None when
    the command is not a grep/rg / has no operand."""
    op = _search_operand_raw(cmd)
    if op is None:
        return None
    head = (cmd or "").split("\n", 1)[0]
    m = _GREP_HEAD_RE.search(head)
    try:
        toks = shlex.split(head[m.end() :]) if m else []
    except ValueError:
        toks = []
    scope: list[str] = []
    filters: list[str] = []
    i = 0
    seen_pat = False
    while i < len(toks):
        t = toks[i]
        if t == "--":
            i += 1
            continue
        if t.startswith("-"):
            filters.append(t)
            if "=" not in t and t in _GREP_VALUE_FLAGS and i + 1 < len(toks):
                i += 2
                continue
            i += 1
            continue
        if not seen_pat:
            seen_pat = True  # the first bare token is the operand, not scope
        else:
            scope.append(t.strip().strip("'\""))
        i += 1
    bare = op if _BARE_SYMBOL_RE.match(op) else ""
    return SearchQuery(pattern=bare, raw_operand=op, scope=tuple(scope), filters=tuple(filters))


def _search_probe_tokens(cmd: str) -> list[str]:
    """Bare-symbol tokens in the operand (quoted multi-word / `foo|bar` split), for the
    ledger — never rendered."""
    op = _search_operand_raw(cmd)
    if op is None:
        return []
    out: list[str] = []
    for part in re.split(r"[|\s]+", op):
        p = part.strip().strip("\\")
        if p and _BARE_SYMBOL_RE.match(p) and p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# COMMAND-kind classifier (F3, segment-head rule).
#
# The command is split into pipeline/chain SEGMENTS (quote-aware, on `|`, `||`,
# `&&`, `;`, newline — single `&` is NOT a separator so `2>&1` stays intact).
# A segment whose HEAD token is a grep-family binary is SEARCH regardless of its
# OPERAND (so `grep -rn pytest .` never classifies as a test run). The compound
# line's kind is the highest-precedence segment kind:
#   submit > test > edit > search > view > other.
# --------------------------------------------------------------------------- #
_SUBMIT_RE = re.compile(r"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT|MINI_SWE_AGENT_FINAL_OUTPUT")
_TEST_RE = re.compile(
    r"\b(pytest|py\.test|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test|yarn\s+test|"
    r"jest|mocha|vitest|tox|nosetests|unittest|rspec|phpunit|gradle\s+test|mvn\s+test)\b"
)
_EDIT_KIND_RE = re.compile(
    r"sed\s+-i|>\s*\S+\.\w+|>>\s*\S|tee\s|<<\s*['\"]?(?:EOF|PY|PATCH|TXT)|"
    r"apply_patch|patch\s+-|python\s+-\s*<<|git\s+apply\b|git\s+checkout\s+--(?:\s|$)"
)
_VIEW_SEG_RE = re.compile(r"^\s*(?:cat|less|more|head|tail|nl|sed\s+-n|view|open|bat)\b")

_GREP_BINS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})
_FIND_BINS = frozenset({"find", "fd"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_KIND_PRECEDENCE = {
    KIND_SUBMIT: 5,
    KIND_TEST: 4,
    KIND_EDIT: 3,
    KIND_SEARCH: 2,
    KIND_VIEW: 1,
    KIND_OTHER: 0,
}


def _split_segments(cmd: str) -> list[str]:
    """Quote-aware split on `|`/`||`/`&&`/`;`/newline. Single `&` (as in ``2>&1``)
    is NOT a separator. Unbalanced quotes swallow the rest into one segment
    (fail-safe: never splits inside what the shell would treat as one word)."""
    segs: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd or "")
    while i < n:
        c = cmd[i]
        if quote is not None:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in (";", "\n"):
            segs.append("".join(cur))
            cur = []
            i += 1
            continue
        if c == "|":
            segs.append("".join(cur))
            cur = []
            i += 2 if i + 1 < n and cmd[i + 1] == "|" else 1
            continue
        if c == "&" and i + 1 < n and cmd[i + 1] == "&":
            segs.append("".join(cur))
            cur = []
            i += 2
            continue
        cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def _seg_head(seg: str) -> str:
    """The segment's head BINARY (basename, lowercased), skipping VAR=val prefixes."""
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    for t in toks:
        if _ENV_ASSIGN_RE.match(t):
            continue
        return t.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return ""


def _classify_segment(seg: str) -> str:
    head = _seg_head(seg)
    if head in _GREP_BINS:
        return KIND_SEARCH  # HEAD RULE: a grep is a search regardless of operand
    if _SUBMIT_RE.search(seg):
        return KIND_SUBMIT
    if _TEST_RE.search(seg):
        return KIND_TEST
    if _EDIT_KIND_RE.search(seg):
        return KIND_EDIT
    if head in _FIND_BINS:
        return KIND_SEARCH
    if _VIEW_SEG_RE.match(seg):
        return KIND_VIEW
    return KIND_OTHER


def classify_command(cmd: str) -> str:
    kinds = [_classify_segment(s) for s in _split_segments(cmd or "")]
    if not kinds:
        return KIND_OTHER
    return max(kinds, key=lambda k: _KIND_PRECEDENCE[k])


# --------------------------------------------------------------------------- #
# grep RESULT parsing (emptiness + hit paths) — the lattice's result half.
# --------------------------------------------------------------------------- #
_GREP_PIPE_SPLIT_RE = re.compile(r"(?<!\|)\|(?!\|)")
_GREP_STAGE_HEAD_RE = re.compile(r"^\s*(?:grep|egrep|fgrep|rg)\b")
_HIT_PATH_EXT_RE = re.compile(r"\.\w+$")


def _grep_is_final_stage(head: str) -> bool:
    seg = _GREP_PIPE_SPLIT_RE.split(head)[-1]
    return bool(_GREP_STAGE_HEAD_RE.match(seg))


def _grep_is_count(seg: str) -> bool:
    for t in seg.split():
        if t == "--count":
            return True
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return True
    return False


def _grep_result_empty(cmd: str, out: str) -> bool:
    head = (cmd or "").split("\n", 1)[0]
    if not _grep_is_final_stage(head):
        return False
    s = (out or "").strip()
    if s == "":
        return True
    seg = _GREP_PIPE_SPLIT_RE.split(head)[-1]
    if _grep_is_count(seg):
        return all(
            ln.strip() == "0" or ln.strip().endswith(":0") for ln in s.splitlines() if ln.strip()
        )
    return False


def _grep_hit_paths(out: str, root: str) -> set[str]:
    paths: set[str] = set()
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        cand = ln.split(":", 1)[0].strip()
        if cand.startswith("./"):
            cand = cand[2:]
        if not cand or " " in cand:
            continue
        if "/" in cand or _HIT_PATH_EXT_RE.search(cand):
            paths.add(_norm_fp(_to_repo_rel(cand, root)))
    return paths


# --------------------------------------------------------------------------- #
# morphology (name-fold) — ported from graph_localizer._split_camel_subtokens.
# --------------------------------------------------------------------------- #
def _split_camel_subtokens(s: str) -> list[str]:
    if not s:
        return []
    out: list[str] = []
    start = 0
    for i in range(1, len(s)):
        prev, cur = s[i - 1], s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        lower_to_upper = ("a" <= prev <= "z") and ("A" <= cur <= "Z")
        acronym_end = ("A" <= cur <= "Z") and ("a" <= nxt <= "z") and ("A" <= prev <= "Z")
        if lower_to_upper or acronym_end:
            out.append(s[start:i])
            start = i
    out.append(s[start:])
    return out


def _stem_subtokens(sym: str) -> list[str]:
    parts: list[str] = []
    for seg in (sym or "").split("_"):
        for sub in _split_camel_subtokens(seg):
            if sub:
                parts.append(sub.lower())
    return parts


def _norm_stem(sym: str) -> str:
    return "".join(_stem_subtokens(sym))


def _fold_variants(sym: str) -> list[str]:
    subs = _stem_subtokens(sym)
    variants: list[str] = []

    def add(v: str) -> None:
        if v and v not in variants:
            variants.append(v)

    add(sym)
    add(sym.lower())
    add(sym.upper())
    if subs:
        add("_".join(subs))
        add("".join(subs))
        add(subs[0] + "".join(w.capitalize() for w in subs[1:]))
        add("".join(w.capitalize() for w in subs))
        add("_".join(w.upper() for w in subs))
    return variants


# --------------------------------------------------------------------------- #
# ledger (probe history for the ZERO_ABSENT repeat gate) + dedup + graph_revision.
# --------------------------------------------------------------------------- #
def _ledger_entry(state: GatewayState, stem: str) -> dict:
    e = state.ledger.get(stem)
    if e is None:
        e = {"probed_forms": set(), "probe_indices": [], "outcomes": []}
        state.ledger[stem] = e
    return e


def _ledger_record(state: GatewayState, sym: str, idx: int, outcome: str) -> None:
    e = _ledger_entry(state, _norm_stem(sym))
    e["probed_forms"].add(sym)
    ii = int(idx)
    # IDEMPOTENCE per (stem, action_index) — 2026-07-22. The probe ledger is ONE shared
    # object (GatewayState.ledger -> episode.probe_ledger, written by BOTH the post_search
    # lattice AND the gateway). When a search turn is re-admitted to the gateway (conditional
    # exclusion), classify_outcome re-records the SAME probe token at the SAME action index the
    # lattice already recorded — forging a 1-probe stem into a false [zero, zero] "stuck"
    # (the ZERO_ABSENT repeat gate reads len(outcomes)). One record per probe per action index
    # is the true invariant of the shared ledger: skip the duplicate append (keeps
    # probe_indices / outcomes parallel), benchmark-free and mutation-testable.
    if ii in e["probe_indices"]:
        return
    e["probe_indices"].append(ii)
    e["outcomes"].append(outcome)


# NOTE: the dedup key is no longer derived here — EvidenceEnvelope.build derives the
# CANONICAL key (evidence_envelope.derive_dedup_key: producer, evidence_type, target,
# fact_id=the F1 symbol, content=the shipped payload+provenance). One derivation, one owner.


# Cache keyed on (path, mtime_ns, size) so a graph rebuilt/updated mid-run gets a
# fresh revision hash instead of a stale cached one. Used ONLY for the legacy file-hash
# FALLBACK (an old graph.db without project_meta.post_revision) — the current-binary path
# reads the LOGICAL revision from project_meta live (B-11), never this byte-hash.
_GRAPH_REV_CACHE: dict[tuple[str, int, int], str] = {}


def _graph_revision_filehash(state: GatewayState) -> str:
    """LEGACY FALLBACK ONLY (an old graph.db with no ``project_meta.post_revision``): the
    truncated sha256 of the graph.db file bytes, cached on the file stat. WAL-BLIND by
    construction — a WAL-committed reindex that leaves the main file bytes unchanged is
    invisible here (B-11). The current binary stamps ``project_meta.post_revision`` and
    :func:`_read_meta_revisions` reads it LIVE, so this path is reached only on a
    pre-B-29 db."""
    db = state.graph_db or ""
    if not db:
        return ""
    try:
        st = os.stat(db)
        key = (db, st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    if key in _GRAPH_REV_CACHE:
        return _GRAPH_REV_CACHE[key]
    rev = ""
    try:
        with open(db, "rb") as fh:
            h = hashlib.sha256()
            h.update(fh.read())
        rev = h.hexdigest()[:12]
    except OSError:
        rev = ""
    _GRAPH_REV_CACHE[key] = rev
    return rev


def _read_meta_revisions(state: GatewayState) -> tuple[str, dict[str, str]]:
    """B-11/B-29: read the LIVE logical revision from ``project_meta`` (WAL-visible), never
    a byte-hash of the graph.db file. Returns ``(post_revision, {surface: subrev})``:

    * ``post_revision`` — the COMPOSITE content revision over ALL fact-producing surfaces
      (nodes · edges · properties · assertions · closure · cochanges · content_fts · …),
      so a same-span body / property / docstring edit that leaves nodes+edges byte-
      identical STILL moves it (B-29). ``""`` when the db/table is absent (old graph.db).
    * ``{surface: subrev}`` — the per-surface sub-revisions (``subrev_<surface>`` keys),
      so a consumer keys per-fact-class freshness on ONLY the surfaces a fact depends on.

    A LIVE query on a fresh read-only connection (NOT the stat-keyed file cache), so a
    WAL-committed reindex is seen the moment it commits. Never raises."""
    db = state.graph_db or ""
    if not db or not os.path.isfile(db):
        return "", {}
    con = _connect_ro(db)
    if con is None:
        return "", {}
    try:
        rows = con.execute(
            "SELECT key, value FROM project_meta WHERE key = 'post_revision' OR key LIKE 'subrev_%'"
        ).fetchall()
    except sqlite3.Error:
        return "", {}
    finally:
        con.close()
    post = ""
    subs: dict[str, str] = {}
    for k, v in rows:
        ks = str(k or "")
        if ks == "post_revision":
            post = str(v or "")
        elif ks.startswith("subrev_"):
            subs[ks[len("subrev_") :]] = str(v or "")
    return post, subs


# Per-FACT-CLASS (evidence_type) -> the graph SURFACES whose sub-revision (subrev_<surface>)
# a fact of that CLASS depends on (B-29). SINGLE SOURCE OF TRUTH (SM-0, 2026-07-11): the map
# + the patch-bound set now LIVE in ``fact_registry`` (co-located with the §1 ``freshness_deps``
# and cross-checked against them at import, so they can no longer SILENTLY diverge). These
# module aliases preserve the prior names/imports; :func:`_revisions_for` reads them through
# the registry accessors, NOT a private divergent copy.
_FACTCLASS_FRESHNESS_SURFACES: dict[str, tuple[str, ...]] = dict(fr_FRESHNESS_SURFACES)
_PATCH_BOUND_FACTCLASSES: frozenset[str] = fr_PATCH_BOUND_FACTCLASSES


def _revisions_for(state: GatewayState, fact_class: str) -> tuple[str, str]:
    """``(graph_revision, valid_until)`` for a fact of ``fact_class`` (B-11/B-29/Graph-F3).

    ``graph_revision`` = the live ``project_meta.post_revision`` (composite), or the legacy
    file hash on an old graph.db. ``valid_until`` = a deterministic composite over ONLY the
    ``subrev_<surface>`` keys this FACT CLASS depends on — the surfaces are now the SINGLE
    SOURCE ``fact_registry.freshness_surfaces`` (SM-0), so freshness is PER-FACT-CLASS: a
    change to a depended sub-rev moves it; a change to an unrelated surface does not.

    Keyed by the shipped ``evidence_type`` (a ``missing_role:<role>`` normalized to its base
    inside the accessor), NOT the producer, so two classes of the same producer with different
    deps are keyed independently. A PATCH-bound class (``fact_registry.is_patch_bound``) gets
    an EMPTY ``valid_until`` (never graph-staled). Falls back to ``graph_revision`` when the db
    has no sub-revisions (old graph.db) or the class is unmapped — preserving the legacy
    invariant ``valid_until == graph_revision``. An explicit ``state.graph_revision`` override
    wins (used by tests / a caller that pins the revision)."""
    canonical_graph = str(getattr(state.canonical_revision, "graph", "") or "")
    if canonical_graph:
        return canonical_graph, canonical_graph
    if state.graph_revision:
        return state.graph_revision, state.graph_revision
    post, subs = _read_meta_revisions(state)
    graph_rev = post or _graph_revision_filehash(state)
    if _fr_is_patch_bound(fact_class):
        return graph_rev, ""  # patch-bound: never graph-staled
    surfaces = _fr_freshness_surfaces(fact_class)  # single source (registry), base-normalized
    if not surfaces or not subs:
        return graph_rev, graph_rev
    parts: list[str] = []
    have = False
    for s in surfaces:
        sv = subs.get(s, "")
        if sv:
            have = True
        parts.append(s + "=" + sv)
    if not have:
        return graph_rev, graph_rev
    vu = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]
    return graph_rev, vu


def _graph_revision(state: GatewayState) -> str:
    """The live composite graph revision (B-11) — WAL-visible ``project_meta.post_revision``
    or the legacy file hash. Back-compat public shim; the per-fact freshness token is
    :func:`_revisions_for`."""
    return _revisions_for(state, "")[0]


# --------------------------------------------------------------------------- #
# graph queries (read-only) — def/ref partition, name-fold, body FTS.
# --------------------------------------------------------------------------- #
_DEF_LABELS = ("Function", "Method", "Class", "Interface", "Struct", "Enum", "Trait", "Constructor")
_FACT_CONF_FLOOR = 0.7
_FLOOD_HITS = 20  # distinct raw hit paths above which a search is a FLOOD


def _open(state: GatewayState):
    db = state.graph_db
    if not db or not os.path.isfile(db):
        return None
    return _connect_ro(db)


def defined_symbols_for_file(
    db_path: str,
    rel_path: str,
    *,
    start_line: "int | None" = None,
    end_line: "int | None" = None,
    repo_root: str = "",
) -> tuple[str, ...]:
    """The definitions `graph.db` places in `rel_path`, optionally bounded to a line window.

    The authority behind `CanonicalResult.viewed_symbols`, and therefore behind
    `work_state.focused_symbols` -- which is otherwise permanently empty on a shell harness,
    holding every symbol-subject evidence record at the relevance intersection forever and
    separately starving `select_covering_tests`.

    Deliberately a GRAPH query, not a text parse. Recovering "which symbol did this show" from
    `sed -n '100,140p' foo.py` is semantic reading, and GT is LLM-free and deterministic by
    mandate. The file the operation actually read is a fact; what is defined in it is a fact the
    graph already holds.

    The line window is not cosmetic: a view of lines 100-140 showed what is defined THERE.
    Returning the whole file would put symbols into focus that the agent never saw, and focus
    drives an intersection -- inventing relevance is worse than having none, because it turns a
    silent feature into a wrong one.

    Correct-or-quiet: no db, no schema, no rows, any sqlite error -> (). Read-only via
    `_connect_ro`; the graph is shared repository truth and a per-attempt path must never write
    it. Order is stable (start_line, name) so focus is deterministic.
    """
    if not db_path or not rel_path or not os.path.isfile(db_path):
        return ()
    con = _connect_ro(db_path)
    if con is None:
        return ()
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        rows = con.execute(
            f"SELECT name,file_path,start_line FROM nodes "
            f"WHERE COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql}) "
            f"ORDER BY start_line,name",
            _DEF_LABELS,
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001 -- close failure must not raise into the agent loop
            pass

    def _rel(path: object) -> str:
        # Same normalization the gateway's own graph readers use. `repo_root` is optional:
        # graph rows may store absolute or repo-relative paths, and `_to_repo_rel` is what
        # reconciles them. Without a root, separator normalization alone is the honest
        # comparison -- it cannot invent a match that is not there.
        if not isinstance(path, str) or not path:
            return ""
        return _norm_fp(_to_repo_rel(path, repo_root) if repo_root else path)

    wanted = _rel(rel_path)
    if not wanted:
        return ()
    out: list[str] = []
    for name, file_path, line in rows:
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            continue
        if _rel(file_path) != wanted:
            continue
        if start_line is not None and line < start_line:
            continue
        if end_line is not None and line > end_line:
            continue
        if name not in out:
            out.append(name)
    return tuple(out)


def definition_files_for_symbol(
    db_path: str,
    symbol: str,
    *,
    repo_root: str = "",
    max_files: int = 3,
) -> tuple[str, ...]:
    """The file(s) `graph.db` defines ``symbol`` in — the inverse of `defined_symbols_for_file`.

    C12. The relevance gate is a `subject:` intersection against work-state focus, so evidence is
    releasable only when its subject is a string already in focus. `_resolved_search_symbols` put
    the graph-validated search OPERAND into focus, which un-held SYMBOL-subject records
    (`def_partition`). It did nothing for FILE-subject records: `localization`'s subject is the
    ranked top FILE, which the agent has by definition NOT opened, because naming an unopened file
    is the entire feature (CLAUDE.md §3). So the gate stayed empty exactly when localization had
    something to say.

    This closes that gap WITHOUT relaxing the gate. The agent searched `parse_url`; the graph says
    `parse_url` is defined in `src/pkg/urls.py`; therefore evidence naming `src/pkg/urls.py` is
    evidence about what the agent is working on. That is a real, deterministic connection — the
    same graph relation the product is built on — not a widening of the bar. Evidence unconnected
    to anything focused still intersects nothing and is still HELD.

    BOUNDED ON PURPOSE. A name resolving to more than ``max_files`` definition sites is AMBIGUOUS
    and yields () rather than putting several unrelated files into focus: focus drives an
    intersection, and inventing relevance turns a quiet feature into a wrong one. Same reasoning,
    and the same 3-file bound, as `_resolve_symbol_defs`.

    The row predicate is character-for-character the one `defined_symbols_for_file` and
    `_resolved_search_symbols` use (`is_test=0`, `start_line>0`, `label IN _DEF_LABELS`). That is
    load-bearing: those two resolvers once disagreed on `start_line`, and a focus writer that
    admits a definition the focus reader cannot see re-creates the same class of ghost.

    Correct-or-quiet, never raises: no db, no schema, no rows, ambiguity, or any sqlite error -> ().
    Read-only via `_connect_ro` — the graph is shared repository truth. Order is stable.
    """
    if not db_path or not symbol or not os.path.isfile(db_path):
        return ()
    con = _connect_ro(db_path)
    if con is None:
        return ()
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        rows = con.execute(
            f"SELECT file_path FROM nodes "
            f"WHERE name=? AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql}) "
            f"ORDER BY file_path,start_line",
            (symbol, *_DEF_LABELS),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001 -- close failure must not raise into the agent loop
            pass
    out: list[str] = []
    for (file_path,) in rows:
        if not isinstance(file_path, str) or not file_path:
            continue
        rel = _norm_fp(_to_repo_rel(file_path, repo_root) if repo_root else file_path)
        # A gold-path / test-identity file must never enter focus: focus is compared against
        # evidence subjects, so admitting one here would hand the gate a leak vector.
        if not rel or _is_leaky(rel):
            continue
        if rel not in out:
            out.append(rel)
    if not out or len(out) > max_files:
        return ()
    return tuple(out)


def _resolve_symbol_defs(con, symbol: str, root: str) -> dict | None:
    """def-sites (1-3 non-leaky files) + FACT-tier caller provenance + test-ref COUNT.
    None when the symbol resolves to no deliverable def or spans >3 files (ambiguous)."""
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        rows = con.execute(
            f"SELECT id, file_path, start_line, label FROM nodes "
            f"WHERE name=? AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql}) ORDER BY file_path, start_line",
            (symbol, *_DEF_LABELS),
        ).fetchall()
    except sqlite3.Error:
        return None
    rows = [r for r in rows if not _is_leaky(_to_repo_rel(r[1] or "", root))]
    if not rows:
        return None
    if len({r[1] for r in rows}) > 3:
        return None  # ambiguous common / stdlib-shadow name -> not a fact
    def_ids = [r[0] for r in rows]
    def_sites = [(_to_repo_rel(r[1] or "", root), int(r[2] or 0)) for r in rows]
    # Typed def-partition rows (Cluster-2b): the concrete graph nodes the partition was
    # built from — node id + label (kind) preserved for the producer attestation. A bare
    # def-node lookup carries no edge resolution, so confidence/resolution_method stay None.
    def_rows = [
        (symbol, _to_repo_rel(r[1] or "", root), int(r[2] or 0), str(r[3] or ""), int(r[0]))
        for r in rows
    ]
    callers, receiver_types = _fact_callers(con, def_ids)
    test_ref_count = _test_ref_count(con, def_ids)
    return {
        "def_sites": def_sites,
        "def_rows": def_rows,
        "n_def_files": len({fp for fp, _ in def_sites}),
        "callers": callers,
        "receiver_types": receiver_types,
        "test_ref_count": test_ref_count,
    }


def _det_in_sql() -> str:
    return "','".join(sorted(DETERMINISTIC_RESOLUTION_METHODS))


def _edge_metadata_ready(con) -> bool:
    """True iff the normalized ``edge_metadata`` sub-table exists AND is populated (B-24).
    Absent/empty (old graph.db) -> receiver_type falls back to parsing edges.metadata."""
    try:
        return con.execute("SELECT 1 FROM edge_metadata LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _fact_callers(con, def_ids: list[int]) -> tuple[list[dict], list[str]]:
    """FACT-tier (deterministic method AND conf>=0.7), NON-test callers of the def ids,
    with receiver_type provenance (W-B).

    B-24: receiver_type is read from the NORMALIZED ``edge_metadata`` sub-table
    (``SELECT value FROM edge_metadata WHERE edge_id=? AND key='receiver_type'``, joined)
    rather than a regex on the polymorphic ``edges.metadata`` string; on an old graph.db
    (table absent/empty) it falls back to :func:`parse_edge_metadata` on the raw string —
    byte-identical to the prior behaviour. Leak-safe: is_test=0 on both ends; caller file
    leak-filtered by the consumer."""
    cols = _edge_columns(con)
    if not {"resolution_method"}.issubset(cols):
        return [], []
    has_conf = "confidence" in cols
    has_meta = "metadata" in cols
    conf_gate = f"AND COALESCE(e.confidence,0) >= {_FACT_CONF_FLOOR} " if has_conf else ""
    conf_sel = "COALESCE(e.confidence,1.0)" if has_conf else "1.0"
    qmarks = ",".join("?" * len(def_ids))
    use_em = _edge_metadata_ready(con)
    if use_em:
        # receiver_type straight from the normalized sub-table. LEFT JOIN so a caller
        # edge with no receiver_type still returns its caller row exactly once — the
        # (edge_id,key) PK guarantees at most one receiver_type row per edge.
        sql = (
            f"SELECT ns.name, ns.file_path, e.source_line, em.value, "
            f"{conf_sel}, LOWER(TRIM(e.resolution_method)), e.id, e.target_id, e.source_id "
            f"FROM edges e JOIN nodes ns ON ns.id=e.source_id "
            f"LEFT JOIN edge_metadata em ON em.edge_id=e.id AND em.key='receiver_type' "
            f"WHERE e.target_id IN ({qmarks}) AND e.type='CALLS' "
            f"AND COALESCE(ns.is_test,0)=0 "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{_det_in_sql()}') {conf_gate}"
        )
    else:
        meta_sel = "e.metadata" if has_meta else "NULL"
        sql = (
            f"SELECT ns.name, ns.file_path, e.source_line, {meta_sel}, "
            f"{conf_sel}, LOWER(TRIM(e.resolution_method)), e.id, e.target_id, e.source_id "
            f"FROM edges e JOIN nodes ns ON ns.id=e.source_id "
            f"WHERE e.target_id IN ({qmarks}) AND e.type='CALLS' "
            f"AND COALESCE(ns.is_test,0)=0 "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{_det_in_sql()}') {conf_gate}"
        )
    try:
        rows = con.execute(sql, def_ids).fetchall()
    except sqlite3.Error:
        return [], []
    callers: list[dict] = []
    receivers: set[str] = set()
    for (
        name,
        fp,
        line,
        meta,
        confidence,
        resolution_method,
        edge_id,
        target_id,
        source_id,
    ) in rows:
        callers.append(
            {
                "name": name or "",
                "file": fp or "",
                "line": int(line or 0),
                "confidence": float(confidence),
                "resolution_method": str(resolution_method or ""),
                "edge_id": int(edge_id) if edge_id is not None else None,
                "definition_id": int(target_id) if target_id is not None else None,
                "caller_node_id": int(source_id) if source_id is not None else None,
            }
        )
        if meta:
            if use_em:
                receivers.add(str(meta))  # em.value IS the normalized receiver_type
            else:
                rt = parse_edge_metadata(str(meta)).get("receiver_type")
                if rt:
                    receivers.add(rt)
    callers.sort(key=lambda c: (c["file"], c["line"], c["name"]))
    return callers, sorted(receivers)


def _test_ref_count(con, def_ids: list[int]) -> int:
    """COUNT of DISTINCT is_test callers over FACT-tier edges only (never a name)."""
    qmarks = ",".join("?" * len(def_ids))
    try:
        row = con.execute(
            f"SELECT COUNT(DISTINCT e.source_id) FROM edges e "
            f"JOIN nodes sn ON sn.id=e.source_id "
            f"WHERE e.target_id IN ({qmarks}) AND e.type='CALLS' "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{_det_in_sql()}') "
            f"AND COALESCE(sn.is_test,0)=1",
            def_ids,
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def _name_or_path_matches(con, sym: str) -> bool:
    try:
        for variant in _fold_variants(sym):
            if con.execute("SELECT 1 FROM nodes WHERE name=? LIMIT 1", (variant,)).fetchone():
                return True
        low = sym.lower()
        if con.execute(
            "SELECT 1 FROM nodes WHERE LOWER(file_path) LIKE '%'||?||'%' LIMIT 1", (low,)
        ).fetchone():
            return True
    except sqlite3.Error:
        return True  # fail toward suppression (no false absence)
    return False


def _has_content_fts(con) -> bool:
    try:
        return (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='symbol_content_fts' LIMIT 1"
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False


def _content_passages_ready(con) -> bool:
    """True iff BOTH ``content_passages`` and its FTS twin ``content_passages_fts`` exist
    (B-23). On an old graph.db without them a body-concept match cites the node
    ``start_line`` (correct-or-quiet)."""
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('content_passages','content_passages_fts')"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and int(row[0] or 0) == 2


def _passage_line(con, node_id: int, sym: str) -> int:
    """B-23: the ``start_line`` of the EXACT body passage (line) in ``node_id`` that
    matches ``sym``, so a body-concept hit cites the matching line, not the node
    ``start_line``. Returns 0 (caller falls back to the node start_line) on any
    miss/error/absent passage surface."""
    try:
        row = con.execute(
            "SELECT p.start_line, p.end_line, p.content, p.content_hash "
            "FROM content_passages_fts f JOIN content_passages p ON p.passage_id=f.rowid "
            "WHERE p.node_id=? AND f.content MATCH ? ORDER BY f.rank LIMIT 1",
            (node_id, '"' + sym.replace('"', "") + '"'),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] else 0


def _body_rows(con, sym: str, root: str) -> list[tuple]:
    try:
        raw = con.execute(
            "SELECT n.id, n.file_path, n.start_line, n.name, n.label "
            "FROM symbol_content_fts f JOIN nodes n ON n.id=f.rowid "
            "WHERE symbol_content_fts MATCH ? AND COALESCE(n.is_test,0)=0 "
            "ORDER BY n.file_path, n.start_line LIMIT 26",
            ('"' + sym.replace('"', "") + '"',),
        ).fetchall()
    except sqlite3.Error:
        return []
    # B-23: after the symbol_content_fts hit, cite the EXACT matching passage line from
    # content_passages_fts (fall back to the node start_line when the passage surface is
    # absent = old db). Probe existence once, not per row.
    passages_ready = _content_passages_ready(con)
    out = []
    for node_id, fp, ln, name, label in raw:
        rel = _to_repo_rel(fp or "", root)
        if _is_leaky(rel):
            continue
        cite = int(ln or 0)
        if passages_ready:
            pl = _passage_line(con, int(node_id), sym)
            if pl:
                cite = pl
        out.append((rel, cite, name or "", (label or "").lower(), int(node_id or 0)))
    return out


def _namefold_hit(con, sym: str, root: str) -> tuple[str, dict] | None:
    """First fold variant that resolves to a deliverable def -> (variant, info)."""
    for variant in _fold_variants(sym):
        try:
            hit = con.execute(
                "SELECT 1 FROM nodes WHERE name=? AND COALESCE(is_test,0)=0 LIMIT 1", (variant,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if not hit:
            continue
        info = _resolve_symbol_defs(con, variant, root)
        if info and info["def_sites"]:
            return variant, info
    return None


# --------------------------------------------------------------------------- #
# CLASSIFIER
# --------------------------------------------------------------------------- #
def classify_outcome(event: ToolEvent, state: GatewayState) -> str:
    """The acquisition-outcome classifier. Deterministic; may open graph.db read-only
    to distinguish the zero-classes and the hit-classes. Records search probes into the
    ledger (the ZERO_ABSENT repeat gate depends on probe history)."""
    # 1) An in-repo, non-test stack trace in the output is a strong localizer for ANY
    #    kind (a failing test / error observation). Highest precedence.
    if EVENT_FAILURE_OBS in _observe_semantic_events(event, state):
        return TRACE_HIT

    if event.kind != KIND_SEARCH:
        return SATISFIED  # edit/test/submit/view enriched by kind-dispatch, not outcome

    sym = search_pattern(event.command)
    empty = _grep_result_empty(event.command, event.output or "")
    idx = event.action_index
    for tok in _search_probe_tokens(event.command):
        _ledger_record(state, tok, idx, "zero" if empty else "hit")

    if not sym:
        return SATISFIED  # non-bare / regex / path grep -> nothing to complete

    con = _open(state)
    if con is None:
        return SATISFIED
    root = state.repo_root
    try:
        if empty:
            if _namefold_hit(con, sym, root) is not None:
                return ZERO_NAME
            if (
                _has_content_fts(con)
                and not _name_or_path_matches(con, sym)
                and _body_rows(con, sym, root)
            ):
                return ZERO_BEHAVIOR
            return ZERO_ABSENT
        # non-empty (hits)
        hit_paths = _grep_hit_paths(event.output or "", root)
        info = _resolve_symbol_defs(con, sym, root)
        if hit_paths and all(_is_leaky(p) for p in hit_paths):
            # every hit is test/vendored — is there a real def elsewhere?
            if info and any(_norm_fp(fp) not in hit_paths for fp, _ in info["def_sites"]):
                return WRONG_SURFACE
        if len(hit_paths) > _FLOOD_HITS:
            return FLOOD
        if info:
            if info["n_def_files"] >= 2:
                return AMBIGUOUS_HIT
            if info["n_def_files"] == 1:
                only = _norm_fp(info["def_sites"][0][0])
                # def IS among the hits -> the agent already has it (silence); def NOT in
                # the hits -> the grep surfaced refs but not the def -> nothing ambiguous
                # to disambiguate here, so stay quiet (correct-or-quiet).
                return EXACT_HIT if only in hit_paths else SATISFIED
        return SATISFIED
    finally:
        con.close()


def _has_repo_trace(event: ToolEvent, state: GatewayState) -> bool:
    if event.kind == KIND_SEARCH:
        return False
    try:
        frames = parse_stack_traces(event.output or "", state.repo_root or ".")
    except Exception:  # noqa: BLE001
        return False
    # SAME GATE AS DELIVERY (_produce_trace). ``_is_leaky`` alone is a path-shape screen
    # resolved against the CWD, so an observation whose only frames are an installed
    # ``yaml/constructor.py`` and ``<string>:18`` still reported a repository trace. This
    # predicate feeds _observe_semantic_events, i.e. the ROUTING truth -- closing the
    # delivery path while leaving the signal untouched fixes half the defect.
    return any(
        not _is_leaky(_to_repo_rel(f.file, state.repo_root))
        and classify_frame_origin(_to_repo_rel(f.file, state.repo_root), state.repo_root)
        is FrameOrigin.REPOSITORY
        for f in frames
    )


_SEMANTIC_BOUNDARY_ORDER = (
    EVENT_FAILURE_OBS,
    "test_executed_no_tests",
    "test_result",
    "edit_result",
    "failed_search",
    "search_result",
    "file_view",
    "submit",
)


def _observe_semantic_events(event: ToolEvent, state: GatewayState) -> frozenset[str]:
    """Complete and cache the event's fine semantic truth exactly once.

    The environment seam may supply an authoritative set from exact pre/post
    state. Legacy/library callers omit it; for them the carrier retains the old
    behavior. Repository trace classification needs ``repo_root`` and therefore
    completes here, at the first consumer that owns the required state.
    """
    events = set(event.semantic_events or ())
    if not event.semantics_authoritative:
        if event.kind == KIND_EDIT:
            events.add("edit_result")
        elif event.kind == KIND_TEST and (
            event.test_outcome in {"pass", "fail", "env_fail"} or event.covering is not None
        ):
            events.add("test_result")
        elif event.kind == KIND_VIEW:
            events.add("file_view")
        elif event.kind == KIND_SUBMIT:
            events.add("submit")
    if event.kind == KIND_SEARCH and not ({"failed_search", "search_result"} & events):
        events.add(
            "failed_search"
            if _grep_result_empty(event.command, event.output or "")
            else "search_result"
        )
    if event.kind != KIND_SEARCH and EVENT_FAILURE_OBS not in events:
        if _has_repo_trace(event, state):
            events.add(EVENT_FAILURE_OBS)
    ordered = tuple(boundary for boundary in _SEMANTIC_BOUNDARY_ORDER if boundary in events)
    event.semantic_events = ordered
    if not event.carrier_kind:
        event.carrier_kind = event.kind
    if not event.primary_boundary and ordered:
        event.primary_boundary = ordered[0]
    return frozenset(ordered)


# --------------------------------------------------------------------------- #
# PRODUCERS (each correct-or-quiet; each Addition provenance-carrying + hashed).
# --------------------------------------------------------------------------- #
# Path-shaped tokens inside a BODY line (belt-and-braces leak scan, F2):
# slash-joined tokens (`pkg/util.py`, `tests/helpers`) and bare source-file
# basenames (`conftest.py`). Each extracted token goes through _is_leaky.
_PATH_TOKEN_RE = re.compile(
    r"[\w.+\-]+(?:[/\\][\w.+\-]+)+"
    r"|\b[\w+\-]+\.(?:py|pyi|go|rs|js|jsx|mjs|cjs|ts|tsx|rb|java|kt|kts|cs|php"
    r"|swift|scala|c|h|cc|hh|cpp|hpp)\b"
)


def _body_line_leaky(line: str) -> bool:
    """True iff a body line carries ANY leaky (test/vendored) path-shaped token.
    Fail-closed direction: a leaky token drops the LINE, never ships redacted-ish."""
    for tok in _PATH_TOKEN_RE.findall(line or ""):
        if _is_leaky(tok):
            return True
    return False


def _event_actual(event: ToolEvent) -> str:
    """Return the finest proven semantic event for lineage and registry truth."""
    if event.primary_boundary:
        return event.primary_boundary
    for boundary in _SEMANTIC_BOUNDARY_ORDER:
        if boundary in (event.semantic_events or ()):
            return boundary
    k = (event.kind or "").strip().lower()
    return k if k in (KIND_SEARCH, KIND_VIEW, KIND_EDIT, KIND_TEST, KIND_SUBMIT) else EVENT_STEP0


def _event_pref(event: ToolEvent) -> str:
    """Project canonical semantic truth onto the envelope's coarse event ABI.

    ``EvidenceEnvelope.preferred_event`` deliberately uses the stable
    step0/search/view/edit/test/submit vocabulary. Lineage retains the finer
    semantic event through :func:`_event_actual`.
    """
    actual = _event_actual(event)
    return {
        "task_start": EVENT_STEP0,
        "failed_search": KIND_SEARCH,
        "search_result": KIND_SEARCH,
        "file_view": KIND_VIEW,
        "first_view_edit": KIND_EDIT,
        "edit_result": KIND_EDIT,
        "test_result": KIND_TEST,
        EVENT_FAILURE_OBS: KIND_TEST,
        "submit": KIND_SUBMIT,
    }.get(actual, actual)


def _mk_add(
    state: GatewayState,
    event: ToolEvent,
    *,
    fact_kind: str,
    target: str,
    body_lines: list[str],
    evidence: list[tuple[str, int]],
    tier: str,
    producer: str,
    symbol: str = "",
    confidence: float | None = None,
    native_args: "dict | None" = None,
    producer_inputs: "ProducerInputs | None" = None,
    cap_feature_ids: "tuple[str, ...]" = (),
    actual_event: str = "",
    canonical_claim: str = "",
    canonical_consequence: str = "",
    canonical_subject: str = "",
    observed_substrates: tuple[str, ...] = (),
) -> EvidenceEnvelope:
    """Build the CANONICAL EvidenceEnvelope for one fact. Leak filtering happens
    HERE, before the build, so the derived dedup key hashes exactly the shipped
    payload+provenance (the F1 content discipline). The symbol rides ``fact_id``.

    ``native_args`` (SM-10, defaulted None so every existing caller is byte-identical)
    is the structured-arg SIDE-CAR a producer attaches so the class's SM-1 native
    renderer can emit a real tool-channel DIAGNOSTIC (not prose); it is identity- and
    serialization-neutral (see :class:`EvidenceEnvelope.native_args`)."""
    body = [ln for ln in body_lines if not _body_line_leaky(ln)]
    ev = [(f, ln) for (f, ln) in evidence if not _is_leaky(f)]
    conf = _TIER_DEFAULT_CONF.get(tier, 0.5) if confidence is None else float(confidence)
    # B-11/B-29/Graph-F3: the live composite revision + the PER-FACT-CLASS freshness token (a
    # composite over ONLY the sub-revs this FACT CLASS — the shipped ``fact_kind`` — depends
    # on). Keyed by evidence_type, NOT producer, so two classes of one producer stale
    # independently. valid_until == graph_rev on an old graph.db (no sub-revs).
    graph_rev, valid_until = _revisions_for(state, fact_kind)
    canonical_graph_revision = str(getattr(state.canonical_revision, "graph", "") or "")
    if canonical_graph_revision:
        graph_rev = canonical_graph_revision
        valid_until = canonical_graph_revision
    lineage = build_lineage(
        runtime_producer_id=producer,
        evidence_type=fact_kind,
        actual_event=actual_event or _event_actual(event),
        cap_feature_ids=cap_feature_ids,
    )
    canonical_semantics = None
    try:
        # The producer declares typed decision meaning from its structured
        # computation identity.  It never asks the canonical converter to parse
        # rendered body prose.  Without the attempt's complete revision vector
        # the legacy envelope remains valid for legacy consumers but is
        # intentionally ineligible for canonical scheduling.
        from groundtruth.runtime.reasoning_runtime import (
            Authority,
            CanonicalEvidenceSemantics,
            RevisionVector,
            feature_contract_for,
        )

        contract = (
            feature_contract_for(lineage.fact_class)
            if lineage is not None and lineage.producer_registration_match
            else None
        )
        revision = state.canonical_revision
        if contract is not None and isinstance(revision, RevisionVector):
            subject = (canonical_subject or target or symbol or lineage.fact_class).strip()
            claim = canonical_claim.strip()
            consequence = canonical_consequence.strip()
            if not claim or not consequence:
                raise ValueError("canonical evidence requires producer-owned decision semantics")
            canonical_semantics = CanonicalEvidenceSemantics(
                decision_context=contract.decision_context,
                roles=contract.roles,
                claim=claim,
                actionable_consequence=consequence,
                causal_neighborhood=(
                    f"decision:{contract.decision_context.value}",
                    f"fact:{lineage.fact_class}",
                    f"subject:{subject}",
                ),
                authority=Authority.RESULT_DERIVED,
                revision=revision,
                revision_dependencies=contract.revision_dependencies,
                mandatory_reason=None,
                failure_prevention=3,
                causal_value=3,
                contradiction_resolution=0,
                anchoring_risk=0,
                observed_substrates=tuple(sorted(set(observed_substrates))),
            )
    except (ImportError, TypeError, ValueError):
        canonical_semantics = None
    envelope = EvidenceEnvelope.build(
        producer=producer,
        fact_id=symbol,
        target=target,
        evidence_type=fact_kind,
        payload=tuple(body),
        provenance=tuple(ev),
        confidence=conf,
        tier=tier,
        graph_revision=graph_rev,
        valid_until=valid_until,
        preferred_event=_event_pref(event),
        blocking_eligibility=ADVISORY,
        estimated_cost_tokens=(len("\n".join(body)) + 3) // 4,
        measured=False,
        native_args=native_args,
        lineage=lineage,
        producer_inputs=producer_inputs,
        canonical_semantics=canonical_semantics,
    )
    if producer_inputs is not None:
        envelope = replace(
            envelope,
            producer_inputs=replace(producer_inputs, candidate_id=envelope.dedup_key),
        )
    return envelope


def _def_partition_body(info: dict) -> list[str]:
    lines = [f"def: {fp}:{ln}" for fp, ln in info["def_sites"][:3]]
    if info["callers"]:
        # Graph-F8 (2026-07-10): count DISTINCT callers (name,file), not call-site
        # EDGES — one caller with two call sites is ONE caller. Parity with
        # _test_ref_count's COUNT(DISTINCT source_id); _fact_callers has no DISTINCT.
        n_callers = len({(c.get("name", ""), c.get("file", "")) for c in info["callers"]})
        lines.append(f"fact-tier callers: {n_callers}")
    if info["receiver_types"]:
        lines.append("resolved callers via receiver type(s): " + ", ".join(info["receiver_types"]))
    if info["test_ref_count"]:
        lines.append(f"test refs: {info['test_ref_count']}")
    return lines


def _def_partition_inputs(
    state: GatewayState,
    fact_kind: str,
    query: str,
    def_rows: "list[tuple]",
) -> "ProducerInputs | None":
    """Build the render-neutral producer sidecar for a def-partition fact.

    ``def_rows`` are the ``(identity, file, line, kind, definition_id)`` tuples from
    ``_resolve_symbol_defs``/``_body_rows``. ``graph_revision`` is read from the SAME
    per-fact-class revision the envelope will stamp (``_revisions_for``), so the Gateway
    attestation binds cleanly (``inputs.graph_revision == envelope.graph_revision``).
    Correct-or-quiet: no rows -> None (the caller ships the fact without a sidecar, which
    stays honestly UNMEASURED rather than fabricating an attestation)."""
    if not def_rows or not (query or "").strip():
        return None
    graph_revision, _valid_until = _revisions_for(state, fact_kind)
    if not graph_revision:
        return None
    rows = tuple(
        DefinitionRow(
            identity=str(identity),
            file=str(fp),
            line=int(line),
            kind=str(kind),
            definition_id=int(node_id),
        )
        for identity, fp, line, kind, node_id in def_rows
        if fp and int(line) > 0 and str(kind).strip() and int(node_id) > 0
    )
    if not rows:
        return None
    return ProducerInputs(
        schema=PRODUCER_INPUTS_SCHEMA,
        evidence_type=fact_kind,
        candidate_id="",
        before_state=None,
        after_state=None,
        caller_rows=(),
        graph_revision=graph_revision,
        definition_rows=rows,
        query_identity=str(query),
    )


def _produce_def_ref_partition(
    event: ToolEvent, state: GatewayState, *, note: str = ""
) -> list[EvidenceEnvelope]:
    audit = _producer_audit(
        state,
        event,
        producer="def_ref_partition",
        evidence_types=("def_ref_partition",),
        invocation_site="gateway.search.def_ref_partition",
    )
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym:
        audit.note("search_pattern_missing", category="dependency_failure")
        return audit.finish([])
    if con is None:
        audit.note("graph_unavailable", category="dependency_failure")
        return audit.finish([])
    try:
        info = _resolve_symbol_defs(con, sym, state.repo_root)
    finally:
        con.close()
    if not info or not info["def_sites"]:
        audit.note(
            "production_definition_absent",
            category="correct_quiet",
            detail={"symbol": sym},
        )
        return audit.finish([])
    body = ([note] if note else []) + _def_partition_body(info)
    target = info["def_sites"][0][0]
    inputs = _def_partition_inputs(state, "def_ref_partition", sym, info.get("def_rows") or [])
    return audit.finish(
        [
            _mk_add(
                state,
                event,
                fact_kind="def_ref_partition",
                target=target,
                body_lines=body,
                evidence=info["def_sites"],
                tier=VERIFIED,
                producer="def_ref_partition",
                symbol=sym,
                producer_inputs=inputs,
                observed_substrates=("graph",),
                canonical_subject=sym,
                canonical_claim=(
                    f"{sym} resolves to {len(info['def_sites'])} production definition site(s)."
                ),
                canonical_consequence=(
                    "Select the definition on the active execution path; "
                    "do not patch a test, vendored, or unrelated copy."
                ),
            )
        ]
    )


# --------------------------------------------------------------------------- #
# T0->T2 RANKED-LOCALIZATION producer (2026-07-12) — GT's ranked localization ANSWER,
# re-homed from the T0 baked <gt-localization> brief tag to the D2/post-search boundary.
# The ranking legs (FTS5 / semantic / graph RRF) are UNTOUCHED — they keep running inside
# localize(); this producer only DELIVERS their ranked candidate files reactively, as the
# agent's own grep channel (path:line:sym rows), where the agent can act on them.
# --------------------------------------------------------------------------- #
# Bounded dose ("small", matching the registry ``localization.max_dose``). A design
# starting point, NOT a fixture-tuned constant.
_LOC_RESLOT_TOPN = 5
_SEARCH_SCOPE_TOPN = 3
_CERTIFIED_SCOPE_CONFIDENCE = 0.9


@dataclass(frozen=True, order=True)
class SearchScopeRelation:
    """One certified structural neighbour of the selected localization anchor."""

    related_file: str
    resolution_method: str
    confidence: float
    edge_id: int


@dataclass(frozen=True)
class SearchScopeSelection:
    """Revision-bound scope rows selected in one graph read transaction."""

    graph_revision: str = ""
    relations: tuple[SearchScopeRelation, ...] = ()


def _record_scope_certification(
    state: GatewayState,
    anchor_file: str,
    selection: SearchScopeSelection,
) -> None:
    """Emit the typed GT_SCOPE_NATIVE scope-certification decision (REFEREE-3 terminal).

    GT_SCOPE_NATIVE is the mediator that shapes the ``localization`` FACT class with a
    certified must-ALSO-touch scope constraint. Its decision was DARK: the mini-seam
    splice (:func:`gt_mini_patch._splice_search_scope`) records the APPLIED/delivered
    receipt ONLY when it actually appends a scope block, so the FAR more common
    correctly-SILENT outcome — the graph certification ran and found NO qualifying
    cross-file neighbour above the confidence floor — produced no typed
    ``control.participation`` row at all, leaving the terminal unable to witness that
    the control ran and correctly withheld.

    This records exactly that NO_EFFECT half, keyed on the GENERAL condition (an empty
    certified selection), never on any task/repo/anchor. The APPLIED/delivered half
    stays owned by the delivery-time splice, and the two never compete for one
    certification: ``selection.relations`` is either empty (silent → here) or non-empty
    (shaped → splice), never both. A zero-byte NO_EFFECT can never exact-join a delivery
    and is skipped by the terminal's candidate-identity correctness grader, so it can
    never manufacture a False mediation verdict — it is honest presence, not a claim.

    Pure instrumentation: emits through :func:`_record_control` (a no-op when the seam
    wired no recorder), touches no delivered bytes, and is armed only when the
    GT_SCOPE_NATIVE FORM arm is active (byte-identical when the flag is off)."""
    if os.environ.get("GT_SCOPE_NATIVE") != "1" or selection.relations:
        return
    anchor = _to_repo_rel(anchor_file, state.repo_root) or anchor_file
    anchor = _norm_fp(anchor)
    if not anchor or _is_leaky(anchor):
        return
    _record_control(
        state,
        "GT_SCOPE_NATIVE",
        "mini_seam.scope.native_render",
        "NO_EFFECT",
        candidate_bytes="",
        fact_class="localization",
        candidate_id=f"localization:{anchor}",
        reason="certified_search_scope_no_certified_neighbour",
    )


def _certified_search_scope(
    state: GatewayState,
    anchor_file: str,
) -> SearchScopeSelection:
    """Return certified structural neighbours for the ranked top file.

    The graph revision and edges are read from the same SQLite snapshot.  Missing
    provenance, an ambiguous graph-frame anchor, a legacy graph without a logical
    revision, weak/name-match relations, and unsafe paths all fail closed.
    """
    db = state.graph_db or ""
    if not db or not anchor_file or _is_leaky(anchor_file) or not os.path.isfile(db):
        return SearchScopeSelection()
    con = _connect_ro(db)
    if con is None:
        return SearchScopeSelection()
    try:
        con.execute("BEGIN")
        revision_row = con.execute(
            "SELECT value FROM project_meta WHERE key='post_revision'"
        ).fetchone()
        revision = str(revision_row[0] or "") if revision_row else ""
        if not revision:
            return SearchScopeSelection()
        graph_paths = [
            str(row[0] or "")
            for row in con.execute(
                "SELECT DISTINCT file_path FROM nodes WHERE file_path IS NOT NULL"
            )
        ]
        anchor_norm = _norm_fp(anchor_file)
        anchors = [
            path
            for path in graph_paths
            if _norm_fp(_to_repo_rel(path, state.repo_root)) == anchor_norm
        ]
        if len(anchors) != 1:
            return SearchScopeSelection(graph_revision=revision)
        graph_anchor = anchors[0]
        methods = tuple(sorted(DETERMINISTIC_RESOLUTION_METHODS))
        marks = ",".join("?" for _ in methods)
        rows = con.execute(
            f"SELECT e.id, e.resolution_method, e.confidence, "
            f"CASE WHEN src.file_path=? THEN dst.file_path ELSE src.file_path END "
            f"FROM edges e JOIN nodes src ON src.id=e.source_id "
            f"JOIN nodes dst ON dst.id=e.target_id "
            f"WHERE (src.file_path=? OR dst.file_path=?) "
            f"AND src.file_path<>dst.file_path "
            f"AND UPPER(COALESCE(e.type,'')) IN ('CALLS','IMPORTS') "
            f"AND LOWER(TRIM(COALESCE(e.resolution_method,''))) IN ({marks}) "
            f"AND COALESCE(e.confidence,0)>=?",
            (graph_anchor, graph_anchor, graph_anchor, *methods, _CERTIFIED_SCOPE_CONFIDENCE),
        ).fetchall()
        best: dict[str, SearchScopeRelation] = {}
        for edge_id, method, confidence, raw_related in rows:
            related = _to_repo_rel(str(raw_related or ""), state.repo_root)
            if not related or _norm_fp(related) == anchor_norm or _is_leaky(related):
                continue
            relation = SearchScopeRelation(
                related_file=related,
                resolution_method=str(method or "").strip().lower(),
                confidence=float(confidence),
                edge_id=int(edge_id),
            )
            current = best.get(_norm_fp(related))
            if current is None or (
                -relation.confidence,
                relation.related_file,
                relation.resolution_method,
                relation.edge_id,
            ) < (
                -current.confidence,
                current.related_file,
                current.resolution_method,
                current.edge_id,
            ):
                best[_norm_fp(related)] = relation
        selected = tuple(
            sorted(
                best.values(),
                key=lambda row: (
                    -row.confidence,
                    row.related_file,
                    row.resolution_method,
                    row.edge_id,
                ),
            )[:_SEARCH_SCOPE_TOPN]
        )
        selection = SearchScopeSelection(revision, selected)
        # The certification RAN to completion: record the correctly-silent NO_EFFECT
        # mediation (empty selection) so GT_SCOPE_NATIVE's terminal has a typed receipt.
        # A shaped (non-empty) selection is left for the delivery-time splice's APPLIED.
        _record_scope_certification(state, anchor_file, selection)
        return selection
    except (sqlite3.Error, TypeError, ValueError):
        return SearchScopeSelection()
    finally:
        con.close()


def _pick_candidate_symbol(con, file_path: str, anchors: set[str]) -> "tuple[str, int] | None":
    """The representative ``(symbol, start_line)`` for a ranked candidate FILE: a non-test
    def whose name is an issue ANCHOR (the file's reason for ranking) when present, else the
    file's FIRST non-test def. ``None`` when the file has no non-test def node. ``file_path``
    is the RAW graph value (``Candidate.file_path`` == ``nodes.file_path``), so the exact
    match mirrors :func:`_resolve_symbol_defs`. Correct-or-quiet: any SQL fault -> None."""
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        rows = con.execute(
            f"SELECT name, start_line FROM nodes "
            f"WHERE file_path=? AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql}) ORDER BY start_line",
            (file_path, *_DEF_LABELS),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    for name, ln in rows:  # prefer an anchor-named def (the file's issue reason)
        if name and (name or "").lower() in anchors:
            return (name, int(ln or 0))
    name, ln = rows[0]  # else the first def
    return (name, int(ln or 0)) if name else None


def _compute_ranked_localization_rows(
    state: GatewayState,
    audit: ProducerAudit | None = None,
) -> "list[tuple[str, int, str]]":
    """Run localize() over the episode's graph.db + issue text and resolve the top-N ranked
    candidate FILES to leak-clean ``(repo_rel_path, line, symbol)`` rows. Pure/deterministic
    over a fixed graph (no rebake needed). Correct-or-quiet: no localize / no issue / no graph
    / no candidates / all-leaky -> []."""
    if _localize is None:
        if audit is not None:
            audit.note("localizer_unavailable", category="dependency_failure")
        return []
    if not (state.issue_text or "").strip():
        if audit is not None:
            audit.note("issue_text_empty", category="dependency_failure")
        return []
    db = state.graph_db
    if not db or not os.path.isfile(db):
        if audit is not None:
            audit.note("graph_unavailable", category="dependency_failure")
        return []
    try:
        res = _localize(state.issue_text, db, repo_root=state.repo_root)
    except Exception as exc:  # noqa: BLE001
        if audit is not None:
            audit.note(
                "localizer_fault",
                category="dependency_failure",
                detail={"fault_type": type(exc).__name__},
            )
        return []
    cands = list(getattr(res, "candidates", None) or [])
    if not cands:
        if audit is not None:
            audit.note("localizer_no_candidates", category="correct_quiet")
        return []
    anchors = {(a or "").lower() for a in (getattr(res, "anchor_symbols", None) or [])}
    con = _open(state)
    if con is None:
        if audit is not None:
            audit.note("graph_open_failed", category="dependency_failure")
        return []
    rows: list[tuple[str, int, str]] = []
    try:
        for c in cands[:_LOC_RESLOT_TOPN]:
            raw_fp = getattr(c, "file_path", "") or ""
            if not raw_fp:
                if audit is not None:
                    audit.note("candidate_path_empty", category="authority")
                continue
            rel = _to_repo_rel(raw_fp, state.repo_root)
            if _is_leaky(rel):  # firewall #1 (producer): a test/vendored candidate is
                if audit is not None:
                    audit.note(
                        "candidate_path_leaky",
                        category="authority",
                        detail={"path": rel},
                    )
                continue  # never a row (the localizer already demotes tests).
            picked = _pick_candidate_symbol(con, raw_fp, anchors)
            if picked is None:
                if audit is not None:
                    audit.note(
                        "candidate_definition_unresolved",
                        category="dependency_failure",
                        detail={"path": rel},
                    )
                continue
            sym, line = picked
            rows.append((rel, int(line), sym))
    finally:
        con.close()
    return rows


def _graph_state_token(state: GatewayState) -> "tuple[str, int | None, int | None]":
    """The ranked-loc cache's graph-identity discriminator: ``(graph_db path,
    st_mtime_ns, st_size)``; a missing/unreadable file is ``(path, None, None)``.
    One os.stat per search turn — negligible next to a localize() run."""
    db = state.graph_db or ""
    try:
        stat = os.stat(db)
    except OSError:
        return (db, None, None)
    return (db, stat.st_mtime_ns, stat.st_size)


def _ranked_localization_rows(
    state: GatewayState,
    audit: ProducerAudit | None = None,
) -> "list[tuple[str, int, str]]":
    """The episode-MEMOIZED ranked-localization rows: localize() runs ONCE per GRAPH STATE
    (the answer is issue-fixed over a fixed graph), reused on every subsequent search turn.
    Stored on ``state.episode`` (a non-serialized private attribute) as
    ``(graph_state_token, rows)``; a slotted episode that rejects the setattr just recomputes
    (correct-or-quiet, never a crash).

    KEYED, not unconditional (2026-07-29 fix): the unkeyed cache immortalized emptiness — a
    search before graph.db existed (dormant start, pre-L6-wake) cached ``[]`` for the ENTIRE
    episode and muted GT_LOC_RESLOT even after the graph was born or re-indexed (live mini
    run: ``cached_empty_ranked_rows: 38``). The token (:func:`_graph_state_token`) makes a
    graph birth or re-index a cache MISS while preserving the legitimate memoization — a
    real 'graph answered: nothing relevant' on an unchanged graph is still served cached."""
    ep = state.episode
    token = _graph_state_token(state)
    cached = getattr(ep, "_gt_ranked_loc_rows", None)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == token:
        rows = cached[1]
        if not rows and audit is not None:
            audit.note(
                "cached_empty_ranked_rows",
                category="dependency_failure",
            )
        return rows
    rows = _compute_ranked_localization_rows(state, audit)
    try:
        setattr(ep, "_gt_ranked_loc_rows", (token, rows))
    except Exception:  # noqa: BLE001 — a slotted episode can't cache; recompute is correct
        pass
    return rows


def _produce_ranked_localization(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """ONE ``localization``-class envelope carrying GT's top-N ranked candidate files as
    ``path:line:sym`` provenance rows (issue-keyed — fires even when the agent's grep operand
    is a behavior PHRASE, the stratum-B blind spot ``search_pattern`` returns None for). The
    rows also ride ``env.native_args['rows']`` so the adapter's ``ranked-list`` renderer emits
    the grep-native block DIRECTLY (no prose re-parse). Fire-ONCE-per-episode is handled by the
    delivery dedup chain (the answer is issue-fixed -> stable ``dedup_key`` -> re-offered but
    dropped after the first delivery). Correct-or-quiet: no ranked rows -> []."""
    audit = _producer_audit(
        state,
        event,
        producer="ranked_localization",
        evidence_types=("localization",),
        invocation_site="gateway.search.ranked_localization",
    )
    rows = _ranked_localization_rows(state, audit)
    if not rows:
        return audit.finish([])
    top_file, _top_line, top_sym = rows[0]
    body = [f"{p}:{ln}:{s}" for p, ln, s in rows]  # tag-free grep rows (hedge-free)
    evidence = [(p, ln) for p, ln, _s in rows]
    return audit.finish(
        [
            _mk_add(
                state,
                event,
                fact_kind="localization",
                target=top_file,
                body_lines=body,
                evidence=evidence,
                tier=HYPOTHESIS,
                producer="ranked_localization",
                symbol=top_sym,
                cap_feature_ids=("GT_LOC_RESLOT",),
                native_args={"rows": rows},
                observed_substrates=("fts5", "graph"),
                canonical_subject=top_file,
                canonical_claim=(
                    f"{top_file} is the highest-ranked production target; "
                    f"{len(rows)} ranked candidate file(s) remain."
                ),
                canonical_consequence=(
                    "Inspect the ranked target and its listed symbol before "
                    "opening a broader repository branch."
                ),
            )
        ]
    )


def _produce_wrong_surface(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    audit = _producer_audit(
        state,
        event,
        producer="wrong_surface",
        evidence_types=("wrong_surface",),
        invocation_site="gateway.search.wrong_surface",
    )
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym:
        audit.note("search_pattern_missing", category="dependency_failure")
        return audit.finish([])
    if con is None:
        audit.note("graph_unavailable", category="dependency_failure")
        return audit.finish([])
    try:
        info = _resolve_symbol_defs(con, sym, state.repo_root)
    finally:
        con.close()
    if not info or not info["def_sites"]:
        audit.note("production_definition_absent", category="correct_quiet")
        return audit.finish([])
    hit_paths = _grep_hit_paths(event.output or "", state.repo_root)
    novel = [(fp, ln) for fp, ln in info["def_sites"] if _norm_fp(fp) not in hit_paths]
    if not novel:
        audit.note(
            "no_novel_production_definition",
            category="correct_quiet",
        )
        return audit.finish([])
    body = ["your hits are all test/vendored copies; the definition is here"]
    body += [f"def: {fp}:{ln}" for fp, ln in novel[:3]]
    # Only the NOVEL def-sites (not among the agent's grep hits) are the delivered fact,
    # so the sidecar carries exactly those rows.
    novel_norm = {(_norm_fp(fp), int(ln)) for fp, ln in novel}
    novel_rows = [
        r for r in (info.get("def_rows") or []) if (_norm_fp(r[1]), int(r[2])) in novel_norm
    ]
    inputs = _def_partition_inputs(state, "wrong_surface", sym, novel_rows)
    return audit.finish(
        [
            _mk_add(
                state,
                event,
                fact_kind="wrong_surface",
                target=novel[0][0],
                body_lines=body,
                evidence=novel,
                tier=VERIFIED,
                producer="wrong_surface",
                symbol=sym,
                producer_inputs=inputs,
                observed_substrates=("graph",),
                canonical_subject=sym,
                canonical_claim=(
                    f"The observed hits for {sym} exclude its production "
                    f"definition; {len(novel)} production site(s) are listed."
                ),
                canonical_consequence=(
                    "Move target selection to a listed production definition before editing."
                ),
            )
        ]
    )


def _produce_name_fold(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    audit = _producer_audit(
        state,
        event,
        producer="name_fold",
        evidence_types=("name_fold",),
        invocation_site="gateway.search.name_fold",
    )
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym:
        audit.note("search_pattern_missing", category="dependency_failure")
        return audit.finish([])
    if con is None:
        audit.note("graph_unavailable", category="dependency_failure")
        return audit.finish([])
    try:
        hit = _namefold_hit(con, sym, state.repo_root)
    finally:
        con.close()
    if hit is None:
        audit.note("fold_variant_absent", category="correct_quiet")
        return audit.finish([])
    variant, info = hit
    if variant == sym:
        note = f'no grep hits for "{sym}", but it IS indexed (check your path/filetype filter)'
    else:
        note = f'"{sym}" not found; indexed as "{variant}"'
    body = [note] + _def_partition_body(info)
    # The fold resolves ``sym`` to the INDEXED name ``variant``; the def rows are keyed on
    # ``variant`` (``_resolve_symbol_defs(con, variant)``), so the query identity is variant.
    inputs = _def_partition_inputs(state, "name_fold", variant, info.get("def_rows") or [])
    return audit.finish(
        [
            _mk_add(
                state,
                event,
                fact_kind="name_fold",
                target=info["def_sites"][0][0],
                body_lines=body,
                evidence=info["def_sites"],
                tier=VERIFIED,
                producer="name_fold",
                symbol=sym,
                producer_inputs=inputs,
                observed_substrates=("graph",),
                canonical_subject=sym,
                canonical_claim=(f"{sym} resolves in the repository index as {variant}."),
                canonical_consequence=(
                    f"Use the indexed identity {variant} and its production "
                    "definition sites for the next lookup."
                ),
            )
        ]
    )


def _produce_body(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    audit = _producer_audit(
        state,
        event,
        producer="body_concept",
        evidence_types=("body_concept",),
        invocation_site="gateway.search.body_concept",
    )
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym:
        audit.note("search_pattern_missing", category="dependency_failure")
        return audit.finish([])
    if con is None:
        audit.note("graph_unavailable", category="dependency_failure")
        return audit.finish([])
    try:
        rows = _body_rows(con, sym, state.repo_root)
    finally:
        con.close()
    if not rows:
        audit.note("body_concept_absent", category="correct_quiet")
        return audit.finish([])
    body = [f'{len(rows)} function bodies mention "{sym}" (no name or path match)']
    body += [f"in {label} {name} - {fp}:{ln}" for fp, ln, name, label, _id in rows[:8]]
    evidence = [(fp, ln) for fp, ln, _n, _l, _id in rows[:8]]
    # SM (2026-07-12): the concept-hit function bodies as (path,line,sym) rows so the adapter's
    # body_concept renderer emits the grep-native ROW block DIRECTLY (no re-parse of GT's prose).
    # The SYMBOL is the concept-bearing function NAME. Side-car only (compare=False, unserialized)
    # — identity / dedup / byte-chain UNCHANGED; native-render-only, so the tagged path is unmoved.
    # Leak is enforced at RENDER time (render_body_concept_native drops test-file rows), matching
    # the localization precedent — native_args is not leak-filtered by _mk_add.
    native_rows = [(fp, ln, name) for fp, ln, name, _l, _id in rows[:8]]
    # def-partition sidecar (Cluster-2b): the concept-bearing function NODES that mention the
    # query. identity is the concept-bearing function NAME (the row's own node), not the query
    # symbol (which does not name-match any node — that is precisely the body-concept case).
    def_rows = [
        (name, fp, ln, label, node_id)
        for fp, ln, name, label, node_id in rows[:8]
        if name and node_id > 0
    ]
    inputs = _def_partition_inputs(state, "body_concept", sym, def_rows)
    return audit.finish(
        [
            _mk_add(
                state,
                event,
                fact_kind="body_concept",
                target=rows[0][0],
                body_lines=body,
                evidence=evidence,
                tier=INFO,
                producer="body_concept",
                symbol=sym,
                native_args={"rows": native_rows},
                producer_inputs=inputs,
                observed_substrates=("graph",),
                canonical_subject=sym,
                canonical_claim=(
                    f"{len(rows)} production function body/bodies mention "
                    f"the behavior concept {sym}."
                ),
                canonical_consequence=(
                    "Use the listed concept-bearing functions as localization "
                    "candidates; identity resolution remains uncertain."
                ),
            )
        ]
    )


def _edit_related_to_stem(edit_blob: str, sym: str) -> bool:
    """True iff a recorded edit plausibly touches the probed symbol: any fold
    variant of ``sym`` appears in the edit's command / changed paths / after-content
    (all lowercased). Conservative in the honest direction: only a RELATED edit
    mutes the honest-negative; an unrelated edit never resets the gate (F4)."""
    if not edit_blob:
        return False
    for v in _fold_variants(sym):
        if v.lower() in edit_blob:
            return True
    return False


def _zero_absent_repeat_ok(event: ToolEvent, state: GatewayState) -> bool:
    """The honest-negative repeat gate: fire only on a REPEAT of an already-failed stem
    (or fold variant) — never on the first, intentional probe — and stay silent when
    an intervening edit RELATED to the probed stem occurred (the agent may have just
    created it). Unrelated edits do not reset the gate (F4)."""
    for tok in _search_probe_tokens(event.command) or [search_pattern(event.command) or ""]:
        if not tok:
            continue
        e = state.ledger.get(_norm_stem(tok))
        if not e:
            continue
        prior_zero = [
            i
            for i, o in zip(e["probe_indices"], e["outcomes"])
            if o == "zero" and i < event.action_index
        ]
        if not prior_zero:
            continue
        prev = max(prior_zero)
        if any(
            prev < ee["index"] < event.action_index and _edit_related_to_stem(ee["blob"], tok)
            for ee in state.edit_events
        ):
            continue  # a RELATED edit intervened -> the agent may have created it
        return True
    return False


def _cs_edit_trigger_on() -> bool:
    """GT_CS_EDIT_TRIGGER (default off => byte-identical): give change_surface its OWN edit-path
    trigger — a file CREATION — instead of leaving it reachable only from the ZERO_ABSENT search
    outcome behind a repeat-a-failed-search gate.

    MEASURED: newfile_precedent / GT_CHANGE_SURFACE emitted 0 rows across all 4 tasks of run
    30121930273. Root cause is dispatch, not detection: change_surface is ONLY outcome-dispatched
    (ZERO_ABSENT), and that path additionally requires the agent to fail the SAME search stem twice
    (`_zero_absent_repeat_ok`). An agent that searches once, finds nothing, and creates the file
    never reaches it. Creating a file is the moment `newfile_precedent` (sibling/template/
    registration) is maximally useful, and it needs no search at all.

    This ADDS a trigger; it displaces nothing. obligations keeps task-start, patch_delta and
    caller_contract keep the edit path, and the existing ZERO_ABSENT route is unchanged."""
    return os.environ.get("GT_CS_EDIT_TRIGGER", "0").strip() == "1"


def _event_creates_new_file(event: ToolEvent) -> bool:
    """True iff this edit CREATED a file: some changed path has empty BEFORE content and non-empty
    AFTER. Structural and language-agnostic (no verb list, no repo/task keying) — an edit that
    merely modifies an existing file has non-empty before and never qualifies."""
    mapping = getattr(event, "edit_before_after", None) or {}
    for _path, pair in mapping.items():
        try:
            before, after = pair
        except Exception:  # noqa: BLE001 — malformed row is not evidence of a creation
            continue
        if not (before or "").strip() and (after or "").strip():
            return True
    return False


def _destination_already_exists(suggested_path: str, state: GatewayState) -> bool:
    """True iff the suggested new-file destination is ALREADY a file on disk.

    ``detect_change_surface`` derives destinations from the issue + the PRE-TASK graph.db, and its
    only "already present" test is entity MEMBERSHIP in a registry (change_surface.py:952) — not
    file existence. So it cannot see a file the agent created seconds ago, and would happily say
    "new file: X" about a path that now exists. On the edit path that is stale-by-construction."""
    root = getattr(state, "repo_root", None)
    if not root or not suggested_path:
        return False
    # os.path (not pathlib) because ``Path`` is NOT imported in this module — the first cut used it
    # and the resulting NameError was swallowed by a broad ``except Exception``, silently disabling
    # this filter entirely. Hence also the NARROW except below: a bare Exception here would hide the
    # very same class of defect again. Only genuine path faults may return False.
    try:
        return os.path.isfile(os.path.join(str(root), suggested_path))
    except (OSError, ValueError, TypeError):
        return False


def _produce_change_surface(
    event: ToolEvent,
    state: GatewayState,
    *,
    require_repeat: bool = True,
    drop_existing_destinations: bool = False,
    post_creation: bool = False,
) -> list[EvidenceEnvelope]:
    # ``require_repeat`` guards ONLY the search-probe ordering predicate, which is meaningless for
    # an edit event (there is no probe to repeat). The edit-path caller passes False; the
    # ZERO_ABSENT caller keeps the historical True => byte-identical on that route.
    if require_repeat and not _zero_absent_repeat_ok(event, state):
        return []  # protect the intentional first probe
    try:
        res = detect_change_surface(state.issue_text, state.repo_root, state.graph_db)
    except Exception:  # noqa: BLE001
        return []
    if res.abstained:
        return []
    out: list[EvidenceEnvelope] = []
    for d in res.destinations:
        if _is_leaky(d.suggested_path):
            continue
        # CORRECT-OR-QUIET (2026-07-24). The edit-path caller fires right AFTER the agent created a
        # file, where a "new file: X" line is either redundant (they just made it) or, worse, points
        # somewhere else and reads as "you created the wrong file" — wrong evidence at the moment of
        # peak trust. The registration/sibling ``missing_roles`` below are the genuinely useful
        # post-creation half (what to WIRE UP next) and are deliberately kept. Opt-in, so the
        # historical ZERO_ABSENT route stays byte-identical.
        if drop_existing_destinations and _destination_already_exists(d.suggested_path, state):
            continue
        # F2(a): template / registration / evidence rows go through the SAME leak
        # predicate as targets — a leaky row is dropped, the addition survives when
        # its core (suggested_path) is clean. _mk_add's belt re-checks every line.
        body = [f"new file: {d.suggested_path}"]
        if d.template_file and not _is_leaky(d.template_file):
            body.append(f"template: {d.template_file}")
        if d.registration_file and not _is_leaky(d.registration_file):
            body.append(f"integrate at: {d.registration_file}")
        body += [ev for ev in d.evidence[:4] if not _body_line_leaky(ev)]
        repository_witness_rows: list[RepositoryWitnessRow] = []
        for witness in getattr(d, "repository_witnesses", ()) or ():
            witness_file = _to_repo_rel(str(getattr(witness, "file", "") or ""), state.repo_root)
            witness_line = getattr(witness, "line", 0)
            witness_kind = str(getattr(witness, "kind", "") or "").strip()
            witness_identity = str(getattr(witness, "identity", "") or "").strip()
            if (
                not witness_file
                or _is_leaky(witness_file)
                or not isinstance(witness_line, int)
                or isinstance(witness_line, bool)
                or witness_line <= 0
                or witness_kind
                not in {
                    "template_definition",
                    "template_lexical_reference",
                    "registration_reference",
                }
                or not witness_identity
            ):
                continue
            source_state = _validated_repository_witness_state(
                event,
                state,
                witness_file,
                witness_line,
                witness_identity,
            )
            if source_state is None:
                continue
            repository_witness_rows.append(
                RepositoryWitnessRow(
                    file=witness_file,
                    line=witness_line,
                    kind=witness_kind,
                    identity=witness_identity,
                    source_state=source_state,
                )
            )
        producer_inputs = None
        if repository_witness_rows:
            graph_revision, _valid_until = _revisions_for(state, "new_file_destination")
            producer_inputs = ProducerInputs(
                schema=PRODUCER_INPUTS_SCHEMA,
                evidence_type="new_file_destination",
                candidate_id="",
                before_state=None,
                after_state=None,
                caller_rows=(),
                graph_revision=graph_revision,
                repository_witness_rows=tuple(sorted(set(repository_witness_rows))),
            )
        out.append(
            _mk_add(
                state,
                event,
                fact_kind="new_file_destination",
                target=d.suggested_path,
                body_lines=body,
                evidence=[],
                tier=HYPOTHESIS,
                producer="change_surface",
                symbol=d.entity,
                producer_inputs=producer_inputs,
                observed_substrates=("graph",),
                cap_feature_ids=("GT_CHANGE_SURFACE",),
                canonical_subject=d.entity,
                canonical_claim=(
                    f"{d.suggested_path} is the repository-derived "
                    f"destination for the missing {d.entity} surface."
                ),
                canonical_consequence=(
                    "Follow the listed sibling/template and registration "
                    "precedent when creating the new surface."
                ),
            )
        )
    for m in res.missing_roles:
        tgt = m.registration_file or (m.sibling_files[0] if m.sibling_files else m.entity)
        if _is_leaky(tgt):
            continue
        ev_rows = (
            [(m.registration_file, ln) for ln, _ in m.registration_lines]
            if m.registration_file
            else []
        )
        body = [ev for ev in m.evidence[:4] if not _body_line_leaky(ev)]
        missing_role_kind = (
            f"missing_role_postcreate:{m.role}" if post_creation else f"missing_role:{m.role}"
        )
        env = _mk_add(
            state,
            event,
            fact_kind=missing_role_kind,
            target=tgt,
            body_lines=body,
            evidence=ev_rows,
            tier=HYPOTHESIS,
            producer="change_surface",
            symbol=m.entity,
            observed_substrates=("graph",),
            cap_feature_ids=("GT_CHANGE_SURFACE",),
            canonical_subject=m.entity,
            canonical_claim=(f"{m.entity} is missing the repository role {m.role} at {tgt}."),
            canonical_consequence=(
                "Complete the missing companion or registration role "
                "before treating the new-file change as integrated."
            ),
        )
        out.append(env)
        # B-NFP: capture the producer-owned registration snapshot (the exact registry bytes,
        # sibling members, and git commit anchor the claim derives from) so the offline
        # attestation can re-run change_surface's OWN derivation on these inputs. Only the
        # REGISTRATION role carries re-derivable byte evidence; other roles/destinations are
        # left honestly unattested (UNMEASURED). Byte-identical: never touches ``env``.
        if m.role == ROLE_REGISTRATION and m.registration_file:
            _capture_registration_snapshot(res, m, state, env.dedup_key)
    return out


def _capture_registration_snapshot(res, missing_role, state, candidate_id: str) -> None:
    """Build + stash the newfile_precedent registration snapshot for one delivered claim.
    Correct-or-quiet: any capture fault leaves nothing stashed (no attestation persisted)."""
    try:
        from groundtruth.runtime.newfile_precedent_attestation import build_registration_snapshot

        members = next(
            (
                g["members"]
                for g in res.sibling_groups
                if g.get("registry_file") == missing_role.registration_file
            ),
            [],
        )
        snapshot = build_registration_snapshot(
            entity=missing_role.entity,
            registration_file=missing_role.registration_file,
            members=members,
            repo_root=state.repo_root,
            repo_head=_repo_head(state.repo_root),
        )
        _stash_newfile_precedent_snapshot(candidate_id, snapshot)
    except Exception:  # noqa: BLE001 — a side-car capture can never break delivery
        return


def _produce_trace(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    # NAME THE ROOT-RESOLUTION FAILURE. ``_root()`` never returns "" -- its failure
    # sentinel is ``"/"`` (gt_mini_patch.py, emitted with a GT_ROOT_MISSING marker and a
    # ``gt_root_missing`` ledger row), which the code there documents as LIVE: "the ro
    # /opt/gt mount shadows it". Under that sentinel EVERY frame classifies UNRESOLVED --
    # even ``/testbed/src/app.py``, because ``_to_repo_rel`` strips ``/testbed/`` first
    # and the existence probe then asks about ``/src/app.py``. So trace_frame goes 100%
    # dark, and the only trace is a pile of per-frame ``frame_origin:UNRESOLVED`` rows
    # indistinguishable from ordinary correct-or-quiet suppression.
    #
    # Fail closed as before -- with no root there is nothing to be a member OF -- but say
    # WHY, once, so a reader can tell "GT could not locate the repository" from "no frame
    # was in-repo". Note ``state.repo_root or "."`` below is dead on the production path
    # for the same reason: the sentinel is "/", never "".
    root = (state.repo_root or "").strip()
    if not root or root.replace("\\", "/") == "/":
        _record_control(
            state,
            "GT_GATEWAY",
            "gateway.augment.candidate_admission",
            "SUPPRESSED",
            fact_class="localization",
            candidate_id="trace_frame:root_unresolved",
            reason="repo_root_unresolved_sentinel",
        )
        return []
    try:
        frames = parse_stack_traces(event.output or "", state.repo_root or ".")
    except Exception:  # noqa: BLE001
        return []
    for fr in frames:  # deepest in-repo first (parse_stack_traces ordering)
        rel = _to_repo_rel(fr.file, state.repo_root)
        if _is_leaky(rel):
            continue
        # REPOSITORY-MEMBERSHIP LAW (2026-07-28). ``parse_stack_traces`` USED TO resolve a
        # relative frame with realpath() — i.e. against the process CWD, which in-container
        # IS repo_root — so a site-packages dependency or a ``<string>`` pseudo-file reached
        # this loop and was delivered verbatim as "deepest in-repo frame". That laundering
        # is now closed at the source (``pretask/traces.py:_is_in_repo`` applies the realpath
        # containment test to ABSOLUTE paths only; see
        # ``tests/pretask/test_trace_cwd_laundering_20260728.py``), so most such frames no
        # longer arrive here at all.
        #
        # This gate STAYS, and is not redundant: ``traces.py`` answers only "does this name a
        # real file under the root", which admits vendored code that physically lives in the
        # checkout (``.venv/``, ``dist-packages/``, ``.tox/``) and generated output. Re-decide
        # origin HERE against repo_root; anything but REPOSITORY is suppressed with an
        # auditable row, so the decision is visible instead of silent.
        origin = classify_frame_origin(rel, state.repo_root)
        if origin is not FrameOrigin.REPOSITORY:
            _record_control(
                state,
                "GT_GATEWAY",
                "gateway.augment.candidate_admission",
                "SUPPRESSED",
                fact_class="localization",
                candidate_id=f"trace_frame:{_norm_fp(rel)}:{fr.line}",
                reason=f"frame_origin:{origin.value}",
            )
            continue
        loc = f"{rel}:{fr.line}" + (f" in {fr.func}" if fr.func else "")
        return [
            _mk_add(
                state,
                event,
                fact_kind="trace_frame",
                target=rel,
                body_lines=[f"deepest in-repo frame: {loc}"],
                evidence=[(rel, fr.line)],
                tier=WARNING,
                producer="trace",
                symbol=fr.func or rel,
                actual_event=EVENT_FAILURE_OBS,
                observed_substrates=("repository_paths",),
                canonical_subject=fr.func or rel,
                canonical_claim=(f"{loc} is the deepest observed in-repository failure frame."),
                canonical_consequence=(
                    "Prioritize this executed frame for failure "
                    "localization before weaker static candidates."
                ),
            )
        ]
    return []


def _produce_patch_delta(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Audited entry (2026-07-29): the edit-turn producers were the largest
    UNNAMED absence on the live smokes — zero invocation rows, so 'never
    invoked' vs 'invoked and abstained' was undecidable. Behavior-neutral."""
    audit = _producer_audit(
        state,
        event,
        producer="patch_delta",
        evidence_types=("signature_mismatch",),
        invocation_site="gateway.edit.patch_delta",
    )
    if not event.edit_before_after:
        audit.note("no_edit_before_after", category="dependency_failure")
        return audit.finish([])
    try:
        findings = _produce_patch_delta_inner(event, state)
    except Exception as exc:  # noqa: BLE001 - preserve correct-or-quiet behavior
        audit.fault(exc)
        return []
    if not findings:
        audit.note("no_patch_delta_findings", category="correct_quiet")
    return audit.finish(findings)


def _produce_patch_delta_inner(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    if not event.edit_before_after:
        return []
    res = analyze_patch_delta(
        dict(event.edit_before_after),
        state.repo_root,
        state.graph_db or "",
    )
    out: list[EvidenceEnvelope] = []
    for sm in res.signature_mismatches:
        if _is_leaky(sm.caller_file):
            continue
        body = [
            f"{sm.caller}() call passes {sm.positional_args} positional arg(s); "
            f"{sm.symbol}() now takes {sm.new_min_params}-{sm.new_max_params}",
            sm.call_site_text,
        ]
        # SM-10: attach the STRUCTURED arity fields so the adapter can render the SM-1
        # mypy/gopls arity diagnostic natively (native_render.render_signature_delta_native)
        # instead of _render_generic prose. Side-car only — identity/dedup UNCHANGED.
        graph_revision, _valid_until = _revisions_for(state, "signature_mismatch")
        before_state = (
            SourceState(
                file=sm.edited_file,
                sha256=getattr(sm, "before_sha256", ""),
                revision=f"edit:{event.action_index}:before",
            )
            if getattr(sm, "before_sha256", "")
            else None
        )
        after_state = (
            SourceState(
                file=sm.edited_file,
                sha256=getattr(sm, "after_sha256", ""),
                revision=f"edit:{event.action_index}:after",
            )
            if getattr(sm, "after_sha256", "")
            else None
        )
        caller_state = (
            SourceState(
                file=sm.caller_file,
                sha256=getattr(sm, "caller_source_sha256", ""),
                revision="source:" + getattr(sm, "caller_source_sha256", ""),
            )
            if getattr(sm, "caller_source_sha256", "")
            else None
        )
        producer_inputs = ProducerInputs(
            schema=PRODUCER_INPUTS_SCHEMA,
            evidence_type="signature_mismatch",
            candidate_id="",
            before_state=before_state,
            after_state=after_state,
            caller_rows=(
                CallerEvidenceRow(
                    identity=sm.caller,
                    file=sm.caller_file,
                    line=sm.caller_line,
                    confidence=sm.confidence,
                    resolution_method=getattr(sm, "resolution_method", "") or None,
                    source_state=caller_state,
                    edge_id=getattr(sm, "edge_id", None),
                    definition_id=getattr(sm, "definition_id", None),
                ),
            ),
            graph_revision=graph_revision,
            signature_changes=(
                SignatureChange(
                    symbol=sm.symbol,
                    edited_file=getattr(sm, "edited_file", ""),
                    before_parameters=None,
                    after_parameters=None,
                    old_min_params=getattr(sm, "old_min_params", None),
                    old_max_params=getattr(sm, "old_max_params", None),
                    new_min_params=getattr(sm, "new_min_params", None),
                    new_max_params=getattr(sm, "new_max_params", None),
                    positional_args=getattr(sm, "positional_args", None),
                ),
            ),
        )
        out.append(
            _mk_add(
                state,
                event,
                fact_kind="signature_mismatch",
                target=sm.caller_file,
                body_lines=body,
                evidence=[(sm.caller_file, sm.caller_line)],
                tier=WARNING,
                producer="patch_delta",
                symbol=sm.symbol,
                confidence=max(0.0, min(1.0, sm.confidence)),
                cap_feature_ids=("GT_PATCH_DELTA",),
                producer_inputs=producer_inputs,
                observed_substrates=("graph",),
                native_args={
                    "caller_file": sm.caller_file,
                    "caller_line": sm.caller_line,
                    "symbol": sm.symbol,
                    "positional_args": sm.positional_args,
                    "new_min_params": sm.new_min_params,
                    "new_max_params": sm.new_max_params,
                },
                canonical_subject=sm.symbol,
                canonical_claim=(
                    f"{sm.caller_file}:{sm.caller_line} passes "
                    f"{sm.positional_args} positional argument(s) to "
                    f"{sm.symbol}, whose edited signature accepts "
                    f"{sm.new_min_params}-{sm.new_max_params}."
                ),
                canonical_consequence=(
                    "Update the impacted caller or restore a compatible "
                    "signature before validating the patch."
                ),
            )
        )
    for cs in res.companion_surfaces:
        if _is_leaky(cs.file):
            continue
        body = [f"registers siblings {', '.join(cs.siblings)} but not '{cs.symbol}'"]
        # referencing_lines are (line_no, text) — the FILE is cs.file itself
        ev_rows = [(cs.file, int(ln)) for ln, _txt in cs.referencing_lines]
        # SM-10: the companion producer OWNS the structured siblings LIST (cs.siblings) —
        # so it supplies render_registration_native's args directly via native_args (no
        # re-parsing of GT's own prose). ``line`` = the first referencing line (the surface
        # location); absent -> the renderer abstains ("") -> generic fallback.
        out.append(
            _mk_add(
                state,
                event,
                fact_kind="companion_surface",
                target=cs.file,
                body_lines=body,
                evidence=ev_rows,
                tier=WARNING,
                producer="patch_delta",
                symbol=cs.symbol,
                cap_feature_ids=("GT_PATCH_DELTA",),
                observed_substrates=("graph",),
                native_args={
                    "file": cs.file,
                    "line": (ev_rows[0][1] if ev_rows else None),
                    "symbol": cs.symbol,
                    "siblings": list(cs.siblings),
                },
                canonical_subject=cs.symbol,
                canonical_claim=(
                    f"{cs.file} registers sibling surfaces but does not register {cs.symbol}."
                ),
                canonical_consequence=(
                    "Update the companion registration surface so the "
                    "new or changed symbol participates in integration."
                ),
            )
        )
    # cochange is an INTERNAL RANKING SIGNAL ONLY — it has NO model-facing native form
    # (SM-1 ``native_render.render_cochange_native`` returns "" by construction; the
    # doctrine pins co-change as a ranking prior, never a delivered fact). Wave-2
    # delivery-equivalence ruling (l3.cochange -> RETIRE narration-cut/internal): the
    # Gateway MUST NOT ship ``cochange_partner`` as a delivered narration envelope — that
    # would merely re-home the "co-changed with the edit in N commits" narration from
    # Lane-A onto the Gateway plane. ``res.cochange_partners`` still biases the
    # pretask/localizer ranking; the Gateway simply does not DELIVER it. The registry
    # ``cochange_prior`` class + ``FRESHNESS_SURFACES['cochange_partner']`` mapping +
    # arbitration rank remain as dormant, documented infrastructure for the internal-only
    # pin (a future INTERNAL consumer may read cochange without a delivered byte).
    _ = res.cochange_partners  # consumed as a ranking prior elsewhere; never a delivered fact
    return out


# --------------------------------------------------------------------------- #
# SM-2b (2026-07-11): the CROSS-LANGUAGE caller_contract producer.
#
# The §26.4 replacement the SM-2 LIPI proved the gateway lacked: on a signature-CHANGING
# edit, name the FACT-tier CROSS-FILE callers the change breaks, so the legacy Lane-A
# caller-break (l3.contract / l3b.evidence) can retire under an EQUIVALENT gateway delivery.
# It reads graph.db's CALLS edges (tree-sitter, ALL languages) — so it delivers on a Go /
# Rust / TS / JS edit, the exact case ``patch_delta.analyze_patch_delta`` cannot (ast-only,
# _SCAN_EXTS = {.py,.pyi}). PURE text signature-change detection (no ast): the parameter-
# IDENTITY list of a keyword-headed definition (``def``/``func``/``fn``/``function`` …),
# extracted the SAME way from BEFORE and AFTER, so the comparison is source-symmetric.
# --------------------------------------------------------------------------- #
# A keyword-headed definition head: an optional def keyword, an optional Go receiver
# ``(r *T) ``, then ``name(``. Cross-language by the union of def keywords (a TS/JS class
# METHOD with no keyword is intentionally out of scope -> correct-or-quiet, not a false break).
_DEF_HEAD_RE = re.compile(
    r"(?:^|[^.\w])"
    r"(?:def|func|fn|function|fun|proc|sub)\s+"
    r"(?:\([^()]*\)\s*)?"  # optional Go receiver `(r *T) `
    r"(?P<name>[A-Za-z_]\w*)\s*\("
)
_PARAM_LEAD_IDENT_RE = re.compile(r"[*&\s]*([A-Za-z_]\w*)")


def _balanced_parens(s: str, open_idx: int) -> str | None:
    """The content of the balanced ``(...)`` whose ``(`` is at ``s[open_idx]``, or None."""
    depth = 0
    for i in range(open_idx, len(s)):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[open_idx + 1 : i]
    return None


def _param_idents(params: str | None) -> list[str] | None:
    """The leading identifier of each TOP-LEVEL comma-separated parameter (annotations,
    defaults, ``*``/``&`` decorations stripped) — the cross-language comparison unit:
    ``a int`` (Go) / ``a: i32`` (Rust) / ``a=1`` (Py) / ``a: number`` (TS) all reduce to
    ``a``. Bracket-aware split so a default ``x=(1,2)`` / generic ``List[int,str]`` never
    splits mid-parameter. None only when ``params`` is None (no parens found)."""
    if params is None:
        return None
    names: list[str] = []
    depth = 0
    cur: list[str] = []

    def _flush() -> None:
        seg = "".join(cur).strip()
        if seg:
            m = _PARAM_LEAD_IDENT_RE.match(seg)
            if m:
                names.append(m.group(1))

    for ch in params:
        if ch in "([{":
            depth += 1
            cur.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            _flush()
            cur = []
        else:
            cur.append(ch)
    _flush()
    return names


def _defs_params(content: str) -> dict[str, list[str]]:
    """``{name: [param_idents]}`` for every keyword-headed definition in ``content`` (first
    definition of a given name wins — a shadowing redefinition is rare and non-signal)."""
    out: dict[str, list[str]] = {}
    for m in _DEF_HEAD_RE.finditer(content or ""):
        name = m.group("name")
        if name in out:
            continue
        idents = _param_idents(_balanced_parens(content, m.end() - 1))
        if idents is not None:
            out[name] = idents
    return out


def _sig_changed_symbols(before: str, after: str) -> list[str]:
    """The symbols whose parameter-IDENTITY list DIFFERS between ``before`` and ``after``
    (both must DEFINE the symbol — a pure add/remove of a def is not a caller-break of an
    existing contract). Sorted, deterministic. Empty when either side is absent (a NEW file
    has no ``before`` contract to break)."""
    if not before or not after:
        return []
    b = _defs_params(before)
    a = _defs_params(after)
    return sorted(n for n in a if n in b and a[n] != b[n])


def _fact_callers_of_symbol_in_file(con, sym: str, rel: str, root: str) -> list[dict]:
    """FACT-tier (deterministic resolution_method AND conf>=0.7), NON-test, CROSS-FILE callers
    of ``sym`` DEFINED in ``rel`` (graph frame) — the callers a signature change breaks that the
    agent is NOT looking at. Returns typed identity/file/line/confidence/resolution rows,
    deduped to ONE row per distinct (caller_name, caller_file), and leak-filtered (a
    test/vendored caller file is dropped). Reuses :func:`_fact_callers` so the FACT gate
    is byte-identical to def_partition's."""
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        drows = con.execute(
            f"SELECT id FROM nodes WHERE name=? AND file_path=? "
            f"AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql})",
            (sym, rel, *_DEF_LABELS),
        ).fetchall()
    except sqlite3.Error:
        return []
    def_ids = [r[0] for r in drows]
    if not def_ids:
        return []
    callers, _receivers = _fact_callers(con, def_ids)
    nrel = _norm_fp(rel)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for c in callers:
        cf = _norm_fp(_to_repo_rel(c.get("file") or "", root))
        if not cf or cf == nrel:  # cross-file only
            continue
        if _is_leaky(cf):  # leak law: never a test/vendored caller
            continue
        key = (c.get("name") or "", cf)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "identity": c.get("name") or "",
                "file": cf,
                "line": int(c.get("line") or 0),
                "confidence": c.get("confidence"),
                "resolution_method": c.get("resolution_method") or None,
                "edge_id": c.get("edge_id"),
                "definition_id": c.get("definition_id"),
                "caller_node_id": c.get("caller_node_id"),
            }
        )
    out.sort(key=lambda row: (row["file"], row["line"], row["identity"]))
    return out


_CALLER_USAGE_KINDS = frozenset(
    {
        "boolean_check",
        "destructure_tuple",
        "exception_guard",
        "iterated",
    }
)


def _typed_caller_usage_rows(
    con: sqlite3.Connection,
    sites: list[dict],
    symbol: str,
    graph_revision: str,
) -> tuple[CallerUsageEvidenceRow, ...]:
    """Return exact, source-attributed usage properties for these caller rows.

    ``caller_usage`` is optional graph enrichment.  It earns bilateral authority
    only when the persisted property carries its own row identity, line,
    confidence, extractor/method, and source revision.  Old or partial schemas
    therefore remain quiet rather than upgrading a parsed string into truth.
    """
    try:
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(properties)")}
    except sqlite3.Error:
        return ()
    required = {
        "id",
        "node_id",
        "kind",
        "value",
        "line",
        "confidence",
        "extractor",
        "evidence_method",
        "source_revision",
    }
    if not required.issubset(columns):
        return ()
    site_by_node = {
        int(site["caller_node_id"]): site
        for site in sites
        if isinstance(site.get("caller_node_id"), int)
        and not isinstance(site.get("caller_node_id"), bool)
    }
    if not site_by_node:
        return ()
    placeholders = ",".join("?" * len(site_by_node))
    try:
        rows = con.execute(
            "SELECT id,node_id,value,line,confidence,extractor,evidence_method,"
            "source_revision FROM properties "
            f"WHERE kind='caller_usage' AND node_id IN ({placeholders}) "
            "ORDER BY node_id,line,id",
            tuple(sorted(site_by_node)),
        ).fetchall()
    except sqlite3.Error:
        return ()
    out: list[CallerUsageEvidenceRow] = []
    for (
        property_id,
        node_id,
        value,
        line,
        confidence,
        extractor,
        evidence_method,
        source_revision,
    ) in rows:
        if not isinstance(value, str) or ":" not in value:
            continue
        usage_kind, remainder = value.split(":", 1)
        callee, separator, call_site = remainder.partition("|")
        usage_kind = usage_kind.strip()
        callee = callee.strip()
        site = site_by_node.get(node_id)
        if (
            site is None
            or usage_kind not in _CALLER_USAGE_KINDS
            or callee != symbol
            or not separator
            or not call_site.strip()
            or not isinstance(property_id, int)
            or isinstance(property_id, bool)
            or property_id <= 0
            or not isinstance(line, int)
            or isinstance(line, bool)
            or line <= 0
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or float(confidence) < 0.7
            or not all(
                isinstance(item, str) and item.strip()
                for item in (extractor, evidence_method, source_revision)
            )
            or source_revision.strip() != graph_revision
        ):
            continue
        out.append(
            CallerUsageEvidenceRow(
                property_id=property_id,
                caller_node_id=node_id,
                caller_identity=str(site["identity"]),
                caller_file=str(site["file"]),
                usage_kind=usage_kind,
                callee=callee,
                call_site=call_site.strip(),
                line=line,
                confidence=float(confidence),
                source_revision=source_revision.strip(),
                extractor=extractor.strip(),
                evidence_method=evidence_method.strip(),
            )
        )
    return tuple(sorted(set(out)))


def _source_state_for_file(
    event: ToolEvent, state: GatewayState, file_path: str
) -> SourceState | None:
    """Exact UTF-8 source state observed for a caller, or ``None`` on a miss."""
    rel = _norm_fp(_to_repo_rel(file_path, state.repo_root))
    text: str | None = None
    for edited_file, before_after in (event.edit_before_after or {}).items():
        if _norm_fp(_to_repo_rel(edited_file, state.repo_root)) == rel:
            _before, text = before_after
            break
    if text is None:
        try:
            with open(os.path.join(state.repo_root, rel), "rb") as source:
                source_bytes = source.read()
        except OSError:
            return None
        digest = hashlib.sha256(source_bytes).hexdigest()
    else:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SourceState(file=rel, sha256=digest, revision="source:" + digest)


def _validated_repository_witness_state(
    event: ToolEvent,
    state: GatewayState,
    file_path: str,
    line: int,
    identity: str,
) -> SourceState | None:
    """Bind one detector witness to exact, currently readable source bytes.

    A graph can retain a stale positive ``start_line`` after an out-of-band
    repository change.  The detector's path/line is therefore not enough by
    itself: this boundary checks that the line exists in the current edit
    overlay/disk bytes and still contains the witnessed sibling/definition
    identity.  Failure is correct-or-quiet.
    """

    rel = _norm_fp(_to_repo_rel(file_path, state.repo_root))
    text: str | None = None
    raw: bytes | None = None
    for edited_file, before_after in (event.edit_before_after or {}).items():
        if _norm_fp(_to_repo_rel(edited_file, state.repo_root)) == rel:
            _before, text = before_after
            raw = (text or "").encode("utf-8")
            break
    if raw is None:
        try:
            with open(os.path.join(state.repo_root, rel), "rb") as source:
                raw = source.read()
        except OSError:
            return None
        text = raw.decode("utf-8", errors="replace")
    source_lines = (text or "").splitlines()
    if (
        line <= 0
        or line > len(source_lines)
        or not identity
        or identity.lower() not in source_lines[line - 1].lower()
    ):
        return None
    digest = hashlib.sha256(raw).hexdigest()
    return SourceState(file=rel, sha256=digest, revision="source:" + digest)


def _produce_caller_contract_view(
    event: ToolEvent,
    state: GatewayState,
) -> list[EvidenceEnvelope]:
    """Attach FACT-tier caller contracts to an exact structured source view.

    The viewed path comes only from ``ToolEvent.viewed_files``. The graph must
    still identify a current production definition at its recorded source line,
    and every retained caller must have a deterministic CALLS edge plus readable
    source bytes. Any incomplete or oversized result abstains correct-or-quiet.
    """

    def confined_repo_file(path: object) -> str:
        if not isinstance(path, str) or not path or not state.repo_root:
            return ""
        rel = _norm_fp(_to_repo_rel(path, state.repo_root))
        if not rel:
            return ""
        try:
            root = os.path.normcase(os.path.realpath(os.path.abspath(state.repo_root)))
            candidate = os.path.normcase(os.path.realpath(os.path.abspath(os.path.join(root, rel))))
            if os.path.commonpath((root, candidate)) != root:
                return ""
        except (OSError, TypeError, ValueError):
            return ""
        return rel

    # Lever 2 / T3 (2026-07-29): the largest unwired abstention surface — on run
    # 30478454517 the view-form abstained on ~1,529 view ticks with NO trace while
    # its class starved SOURCE_UNDERSTANDING (ARCH-B claim 1 branch census;
    # ARCH-D). Every termination now carries a named reason; behavior-neutral.
    audit = _producer_audit(
        state,
        event,
        producer="caller_contract",
        evidence_types=("caller_contract_view",),
        invocation_site="gateway.view.caller_contract_view",
    )
    viewed = tuple(
        dict.fromkeys(rel for path in event.viewed_files if (rel := confined_repo_file(path)))
    )
    if not viewed:
        audit.note("no_confined_viewed_file", category="dependency_failure")
        return audit.finish([])
    con = _open(state)
    if con is None:
        audit.note("graph_unavailable", category="dependency_failure")
        return audit.finish([])
    out: list[EvidenceEnvelope] = []
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        try:
            raw_definitions = con.execute(
                f"SELECT name,file_path,start_line FROM nodes "
                f"WHERE COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
                f"AND label IN ({labels_sql}) "
                f"ORDER BY file_path,start_line,name",
                _DEF_LABELS,
            ).fetchall()
        except sqlite3.Error:
            audit.note("definitions_query_failed", category="dependency_failure")
            return audit.finish([])
        for rel in viewed:
            if not rel or _is_leaky(rel):
                audit.note(
                    "viewed_file_leaky",
                    category="authority",
                    detail={"file": rel},
                )
                continue
            viewed_state = _source_state_for_file(event, state, rel)
            if viewed_state is None:
                audit.note(
                    "viewed_source_state_unavailable",
                    category="dependency_failure",
                    detail={"file": rel},
                )
                continue
            definitions: list[tuple[str, int]] = []
            for name, file_path, line in raw_definitions:
                definition_path = confined_repo_file(file_path)
                if (
                    definition_path != rel
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(line, int)
                    or isinstance(line, bool)
                    or line <= 0
                ):
                    continue
                definitions.append((name, line))
            contracts: list[tuple[str, list[dict]]] = []
            for symbol, definition_line in definitions:
                if (
                    _validated_repository_witness_state(
                        event,
                        state,
                        rel,
                        definition_line,
                        symbol,
                    )
                    is None
                ):
                    continue
                sites: list[dict] = []
                for site in _fact_callers_of_symbol_in_file(
                    con,
                    symbol,
                    rel,
                    state.repo_root,
                ):
                    caller_file = confined_repo_file(site.get("file"))
                    identity = site.get("identity")
                    line = site.get("line")
                    confidence = site.get("confidence")
                    resolution_method = site.get("resolution_method")
                    edge_id = site.get("edge_id")
                    definition_id = site.get("definition_id")
                    if (
                        not caller_file
                        or not isinstance(identity, str)
                        or not identity
                        or not isinstance(line, int)
                        or isinstance(line, bool)
                        or line <= 0
                        or not isinstance(confidence, (int, float))
                        or isinstance(confidence, bool)
                        or not _FACT_CONF_FLOOR <= float(confidence) <= 1.0
                        or resolution_method not in DETERMINISTIC_RESOLUTION_METHODS
                        or not isinstance(edge_id, int)
                        or isinstance(edge_id, bool)
                        or edge_id <= 0
                        or not isinstance(definition_id, int)
                        or isinstance(definition_id, bool)
                        or definition_id <= 0
                    ):
                        continue
                    source_state = _source_state_for_file(
                        event,
                        state,
                        caller_file,
                    )
                    if source_state is None:
                        continue
                    sites.append(
                        {
                            **site,
                            "identity": identity,
                            "file": caller_file,
                            "line": line,
                            "confidence": float(confidence),
                            "resolution_method": resolution_method,
                            "edge_id": edge_id,
                            "definition_id": definition_id,
                            "source_state": source_state,
                        }
                    )
                if sites:
                    contracts.append((symbol, sites))
            if not contracts:
                audit.note(
                    "no_verified_caller_contract",
                    category="correct_quiet",
                    detail={"file": rel, "definitions": len(definitions)},
                )
                continue
            all_sites = [(symbol, site) for symbol, sites in contracts for site in sites]
            # A source view with a very broad public surface needs a narrower
            # symbol decision; a truncated caller set would be misleading.
            if len(all_sites) > 12:
                audit.note(
                    "broad_public_surface",
                    category="correct_quiet",
                    detail={"file": rel, "caller_sites": len(all_sites)},
                )
                continue
            graph_revision, _valid_until = _revisions_for(
                state,
                "caller_contract_view",
            )
            usage_rows = tuple(
                row
                for symbol, sites in contracts
                for row in _typed_caller_usage_rows(
                    con,
                    sites,
                    symbol,
                    graph_revision,
                )
            )
            caller_rows = tuple(
                CallerEvidenceRow(
                    identity=str(site["identity"]),
                    file=str(site["file"]),
                    line=int(site["line"]),
                    confidence=float(site["confidence"]),
                    resolution_method=str(site["resolution_method"]),
                    source_state=site["source_state"],
                    edge_id=int(site["edge_id"]),
                    definition_id=int(site["definition_id"]),
                )
                for _symbol, site in all_sites
            )
            symbols = tuple(symbol for symbol, _sites in contracts)
            n_files = len({str(site["file"]) for _symbol, site in all_sites})
            body = [
                (
                    f"{symbol}() has {len(sites)} production caller(s) "
                    f"across {len({str(site['file']) for site in sites})} file(s)"
                )
                for symbol, sites in contracts
            ]
            producer_inputs = ProducerInputs(
                schema=PRODUCER_INPUTS_SCHEMA,
                evidence_type="caller_contract_view",
                candidate_id="",
                before_state=None,
                after_state=viewed_state,
                caller_rows=caller_rows,
                graph_revision=graph_revision,
                caller_usage_rows=usage_rows,
            )
            minimum_confidence = min(float(site["confidence"]) for _symbol, site in all_sites)
            out.append(
                _mk_add(
                    state,
                    event,
                    fact_kind="caller_contract_view",
                    target=rel,
                    body_lines=body,
                    evidence=[
                        (str(site["file"]), int(site["line"])) for _symbol, site in all_sites
                    ],
                    tier=(VERIFIED if minimum_confidence >= 0.9 else WARNING),
                    producer="caller_contract",
                    symbol=(symbols[0] if len(symbols) == 1 else "viewed_file_contract"),
                    confidence=minimum_confidence,
                    native_args={
                        "caller_rows": tuple(
                            (
                                str(site["file"]),
                                int(site["line"]),
                                str(site["identity"]),
                            )
                            for _symbol, site in all_sites
                        ),
                        "caller_usage_rows": tuple(
                            (
                                row.caller_file,
                                row.line,
                                row.callee,
                                row.usage_kind,
                            )
                            for row in usage_rows
                        ),
                    },
                    producer_inputs=producer_inputs,
                    observed_substrates=("graph",),
                    canonical_subject=rel,
                    canonical_claim=(
                        f"{rel} defines {len(symbols)} viewed production "
                        f"symbol(s) with {len(all_sites)} cross-file production "
                        f"caller(s) across {n_files} file(s)."
                    ),
                    canonical_consequence=(
                        "Preserve the caller-visible behavior of "
                        f"{', '.join(symbols)} or update every listed affected "
                        "caller before applying the edit."
                    ),
                )
            )
    finally:
        con.close()
    return audit.finish(out)


def _produce_caller_contract(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """The CROSS-LANGUAGE caller-contract break on a signature-changing edit. For each edited
    file whose function/method changed its parameter list (pure-text before/after diff), if
    the changed symbol has >=1 FACT-tier cross-file caller in graph.db, emit a ``caller_break``
    envelope (aliases to the registered ``caller_contract`` class; delivered at ``edit_result``
    per the registry boundary override). Correct-or-quiet: no before/after, no graph, no
    detectable signature change, or no cross-file caller -> []. Works on Go/Rust/TS/JS/Python
    because it reads the tree-sitter CALLS graph, NOT a Python ast."""
    # Lever 2 / T3 (2026-07-29): named terminal outcomes, behavior-neutral
    # (see _produce_caller_contract_view).
    audit = _producer_audit(
        state,
        event,
        producer="caller_contract",
        evidence_types=("caller_break",),
        invocation_site="gateway.edit.caller_contract",
    )
    eba = event.edit_before_after
    if not eba:
        audit.note("no_edited_before_after_pair", category="dependency_failure")
        return audit.finish([])
    con = _open(state)
    if con is None:
        audit.note("graph_unavailable", category="dependency_failure")
        return audit.finish([])
    out: list[EvidenceEnvelope] = []
    try:
        for cf, ba in sorted(eba.items()):
            before, after = ba if isinstance(ba, tuple) else (None, ba)
            before_parameters = _defs_params(before or "")
            after_parameters = _defs_params(after or "")
            rel = _norm_fp(_to_repo_rel(cf, state.repo_root))
            if not rel or _is_leaky(rel):
                audit.note(
                    "edited_file_leaky",
                    category="authority",
                    detail={"file": rel},
                )
                continue
            changed_symbols = _sig_changed_symbols(before or "", after or "")
            if not changed_symbols:
                audit.note(
                    "no_signature_change",
                    category="correct_quiet",
                    detail={"file": rel},
                )
            for sym in changed_symbols:
                sites = _fact_callers_of_symbol_in_file(con, sym, rel, state.repo_root)
                if not sites:
                    audit.note(
                        "no_cross_file_caller",
                        category="correct_quiet",
                        detail={"symbol": sym, "file": rel},
                    )
                    continue
                n_callers = len(sites)
                n_files = len({site["file"] for site in sites})
                body = [
                    f"{sym}() signature changed — {n_callers} caller(s) in "
                    f"{n_files} file(s); update the call sites"
                ]
                graph_revision, _valid_until = _revisions_for(state, "caller_break")
                caller_usage_rows = _typed_caller_usage_rows(
                    con,
                    sites,
                    sym,
                    graph_revision,
                )
                signature_change = SignatureChange(
                    symbol=sym,
                    edited_file=rel,
                    before_parameters=tuple(before_parameters[sym]),
                    after_parameters=tuple(after_parameters[sym]),
                    old_min_params=None,
                    old_max_params=None,
                    new_min_params=None,
                    new_max_params=None,
                    positional_args=None,
                )
                producer_inputs = ProducerInputs(
                    schema=PRODUCER_INPUTS_SCHEMA,
                    evidence_type="caller_break",
                    candidate_id="",
                    before_state=SourceState(
                        file=rel,
                        sha256=hashlib.sha256((before or "").encode("utf-8")).hexdigest(),
                        revision=f"edit:{event.action_index}:before",
                    ),
                    after_state=SourceState(
                        file=rel,
                        sha256=hashlib.sha256((after or "").encode("utf-8")).hexdigest(),
                        revision=f"edit:{event.action_index}:after",
                    ),
                    caller_rows=tuple(
                        CallerEvidenceRow(
                            identity=site["identity"],
                            file=site["file"],
                            line=site["line"],
                            confidence=site["confidence"],
                            resolution_method=site["resolution_method"],
                            source_state=_source_state_for_file(event, state, site["file"]),
                            edge_id=site["edge_id"],
                            definition_id=site["definition_id"],
                        )
                        for site in sites
                    ),
                    graph_revision=graph_revision,
                    signature_changes=(signature_change,),
                    caller_usage_rows=caller_usage_rows,
                )
                out.append(
                    _mk_add(
                        state,
                        event,
                        fact_kind="caller_break",
                        target=rel,
                        body_lines=body,
                        evidence=[(site["file"], site["line"]) for site in sites],
                        tier=WARNING,
                        producer="caller_contract",
                        symbol=sym,
                        native_args={
                            "before_parameters": signature_change.before_parameters,
                            "after_parameters": signature_change.after_parameters,
                            "caller_rows": tuple(
                                (site["file"], site["line"], site["identity"]) for site in sites
                            ),
                            "caller_usage_rows": tuple(
                                (
                                    row.caller_file,
                                    row.line,
                                    row.callee,
                                    row.usage_kind,
                                )
                                for row in caller_usage_rows
                            ),
                        },
                        producer_inputs=producer_inputs,
                        observed_substrates=("graph",),
                        canonical_subject=sym,
                        canonical_claim=(
                            f"{sym} changed signature and has "
                            f"{n_callers} production caller(s) across "
                            f"{n_files} file(s)."
                        ),
                        canonical_consequence=(
                            "Preserve the caller-visible contract or "
                            "update every listed affected caller before "
                            "continuing validation."
                        ),
                    )
                )
    finally:
        con.close()
    return audit.finish(out)


def _produce_covering(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Audited entry (2026-07-29): distinguishes 'covering never threaded'
    (the dominant live case — 4 red test outputs, zero rows) from an executed
    abstention. Behavior-neutral; delegates to the original body."""
    audit = _producer_audit(
        state,
        event,
        producer="covering",
        evidence_types=("covering_verdict",),
        invocation_site="gateway.test.covering",
    )
    if event.covering is None:
        audit.note("no_covering_result_threaded", category="dependency_failure")
        return audit.finish([])
    return audit.finish(_produce_covering_inner(event, state))


def _produce_covering_inner(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Wrap an injected covering verdict — the body goes through the EXISTING
    identity firewall (``native_render`` Format D), never raw runner stdout (F2).
    Correct-or-quiet: when nothing signal-bearing survives the firewall, abstain."""
    cov = event.covering
    if cov is None or not cov.target:
        return []
    if _is_leaky(cov.target):
        return []
    try:
        rendered = render_covering_failure_native(
            {"stdout_tail": "\n".join(cov.body_lines)},
            test_files=list(cov.test_files) or None,
        )
    except Exception:  # noqa: BLE001 — firewall failure => nothing ships
        return []
    if not rendered:
        return []
    return [
        _mk_add(
            state,
            event,
            fact_kind="covering_verdict",
            target=cov.target,
            body_lines=rendered.splitlines(),
            evidence=list(cov.evidence),
            tier=cov.tier or WARNING,
            producer="covering",
            symbol=cov.verdict or cov.target,
            observed_substrates=("structured_test_result",),
            canonical_subject=cov.target,
            canonical_claim=(
                f"The executed covering validation for {cov.target} "
                f"finished with verdict {cov.verdict}."
            ),
            canonical_consequence=(
                "Use this executed verdict as the validation constraint "
                "for the current patch decision."
            ),
        )
    ]


# --------------------------------------------------------------------------- #
# DELIVERY ROUTING (B-12 timing + freshness) + the B-21 revision-scoped latch key.
# --------------------------------------------------------------------------- #
ROUTE_DELIVER = "deliver"  # on-time, fresh, right decision point -> ship bytes
ROUTE_DEFER = "defer"  # event < available_at -> too early; re-offer later
ROUTE_EXPIRED_LATE = "expired_late"  # event > deliver_by -> too late; explicit non-delivery
ROUTE_STALE = "stale"  # a depended sub-rev changed -> discard/recompute
ROUTE_REGISTRY_ERROR = "registry_error"  # registry authority unavailable -> fail closed

# The decision-lifecycle order of the coarse observation boundaries. A fact ABOUT an
# action is useful only AT its own boundary (available_at == deliver_by == preferred_event
# — fact_registry: earliest_event == deliver_by for every class), so a wrong decision point
# is strictly BEFORE (defer) or AFTER (expire) that single boundary. ``other`` / unknown
# maps to step0 so a producer firing on a non-core event still routes deterministically.
_ROUTE_EVENT_ORDER: dict[str, int] = {
    EVENT_STEP0: 0,
    "search": 1,
    "view": 2,
    "edit": 3,
    "test": 4,
    "submit": 5,
}


def _event_ordinal(name: str) -> int:
    normalized = (name or "").strip().lower()
    return _FINE_EVENT_TO_COARSE_ORDINAL.get(normalized, _ROUTE_EVENT_ORDER.get(normalized, 0))


# SM-0: the registry's FINE §1 delivery boundary (fact_registry.EVENTS) -> the COARSE
# lifecycle ordinal above. This is how the DECLARED ``deliver_by`` of a class is compared to
# the firing ``event.kind`` under ``GT_REGISTRY_ENFORCE`` — replacing the self-stamped
# ``preferred_event`` (which always equalled the current event, making DEFER/EXPIRED_LATE
# structurally dead). ``failed_search`` is a search; ``failure_obs`` is realized in a test/
# command observation; ``first_view_edit`` is realized by the patch_delta producer on the
# EDIT event (view precedes edit, so binding it to edit is the producer's true realization).
_FINE_EVENT_TO_COARSE_ORDINAL: dict[str, int] = {
    "task_start": 0,  # EVENT_TASK_START  -> step0
    "search_result": 1,  # EVENT_SEARCH_RESULT
    "failed_search": 1,  # EVENT_FAILED_SEARCH (an empty search is still a search)
    "file_view": 2,  # EVENT_FILE_VIEW
    "first_view_edit": 3,  # EVENT_FIRST_VIEW_EDIT (realized on the edit event)
    "edit_result": 3,  # EVENT_EDIT_RESULT
    "test_result": 4,  # EVENT_TEST_RESULT
    "failure_obs": 4,  # EVENT_FAILURE_OBS (a trace/error rides a test/command obs)
    "submit": 5,  # EVENT_SUBMIT
}


# SM-0 hardening tripwire (LIPI recommendation, 2026-07-11): the FINE §1 event vocabulary
# (``fact_registry.EVENTS``) and this coarse-ordinal map MUST stay in lockstep.
# ``_registry_want_ordinal`` below reads ``_FINE_EVENT_TO_COARSE_ORDINAL.get(ev, 0)``, so a
# NEW ``fact_registry`` EVENT with no entry here would SILENTLY map to ordinal 0 (step0) —
# a genuinely-wrong-event class would then never DEFER/EXPIRE under GT_REGISTRY_ENFORCE
# (the boundary check would pass by accident at step0). Fail LOUD at import (mirroring
# ``fact_registry._self_check_executable``) so a future EVENT can never silently route to
# step-0 unnoticed.
def _self_check_event_map() -> None:
    fine = set(_FINE_EVENT_TO_COARSE_ORDINAL)
    events = set(fr_EVENTS)
    if fine != events:
        raise ValueError(
            "gateway._FINE_EVENT_TO_COARSE_ORDINAL is out of sync with "
            f"fact_registry.EVENTS (missing {sorted(events - fine)}, "
            f"extra {sorted(fine - events)}) — a fact_registry EVENT with no coarse-"
            "ordinal mapping silently routes to step0; add it to "
            "_FINE_EVENT_TO_COARSE_ORDINAL"
        )


_self_check_event_map()


def _registry_want_ordinal(evidence_type: str) -> int | None:
    """The coarse lifecycle ordinal of a class's DECLARED delivery boundary (registry
    ``deliver_by``, honoring the per-evidence-type override), or ``None`` when the type is
    unregistered (caller falls back to the self-stamped ``preferred_event``)."""
    ev = _fr_required_event(evidence_type)
    if ev is None:
        return None
    return _FINE_EVENT_TO_COARSE_ORDINAL.get(ev, 0)


def _reactive_semantic_event(
    env: EvidenceEnvelope, event: ToolEvent, state: GatewayState
) -> str | None:
    """Return the classifier-proven fine event for a reactive candidate.

    Carrier kinds such as ``test`` and ``other`` are not evidence that a failure
    observation occurred.  The current reactive producer is the trace-frame
    localizer, whose semantic boundary exists only when the raw observation has
    a permitted in-repository stack frame.
    """
    evidence_base = (env.evidence_type or "").split(":", 1)[0]
    observed = _observe_semantic_events(event, state)
    if evidence_base == "trace_frame" and EVENT_FAILURE_OBS in observed:
        return EVENT_FAILURE_OBS
    return None


def route_delivery(env: EvidenceEnvelope, event: ToolEvent, state: GatewayState) -> str:
    """B-12: enforce the envelope's timing + freshness as REAL routing. Returns one of
    :data:`ROUTE_DELIVER` / :data:`ROUTE_DEFER` / :data:`ROUTE_EXPIRED_LATE` /
    :data:`ROUTE_STALE`; ONLY ``ROUTE_DELIVER`` ships bytes.

    * FRESHNESS (B-29/B-12): the fact's ``valid_until`` is recomputed LIVE from the
      surfaces its producer depends on; if a depended sub-rev changed since the fact was
      built (recomputed != stored) it is ``ROUTE_STALE`` — discard/recompute, never deliver
      a fact computed against a superseded graph.
    * DEFER: the current event is BEFORE the fact's boundary (available_at) -> too early;
      the fact is re-offered at a later event (never destroyed).
    * EXPIRED_LATE: the current event is AFTER the fact's boundary (deliver_by) -> too late
      to influence the decision -> an EXPLICIT non-delivery (NOT a success), never a late
      append.

    TIMING SOURCE (SM-0): under ``GT_REGISTRY_ENFORCE`` the boundary ``want`` derives from
    the class's DECLARED ``deliver_by`` in the fact-registry (``_registry_want_ordinal``),
    NOT from the producer's self-stamped ``env.preferred_event`` — so the firing-event vs
    declared-boundary comparison is REAL and DEFER/EXPIRED_LATE are reachable for a genuinely
    wrong-event fact. With the flag OFF, ``want`` is the self-stamped ``preferred_event`` —
    byte-identical to pre-SM-0 (a fact built + delivered on its own event routes DELIVER)."""
    # Graph-F3: recompute the live freshness token keyed on the fact CLASS (evidence_type),
    # matching the build-time key in _mk_add — so a fact stales on ITS OWN depended surfaces.
    _gr, live_vu = _revisions_for(state, env.evidence_type)
    if env.valid_until and live_vu and env.valid_until != live_vu:
        return ROUTE_STALE
    # SM-4 (2026-07-11): EPISODE-OVERLAY freshness. The certified base graph.db is a frozen
    # snapshot (ro-mounted), so the class-level token above can NEVER stale a fact after an
    # edit — the base's revision does not move. A fact READ from that base about a file the
    # agent EDITED in a prior turn describes the file's OLD structure. The episode overlay
    # (built per structured edit; ``entry['changed']`` IS the mark_stale_envelopes verdict
    # that the base structure no longer matches the current file) marks such a file; drop the
    # fact STALE. SCOPED to the fact's TARGET file — a fact about an UNEDITED file (not in the
    # overlay) is UNAFFECTED. EXEMPT the file edited in THIS observation (``changed_files``):
    # its own edit-derived fact (patch_delta) is fresh, not a stale base read. Gated by
    # GT_EDIT_OVERLAY + a non-empty overlay ⇒ byte-identical when off/empty.
    if _episode_overlay_on():
        try:
            overlay = getattr(state, "episode_overlay", None)
            stale = False
            if overlay:
                tf = _norm_fp(env.target or "")
                entry = overlay.get(tf) if tf else None
                if isinstance(entry, dict) and entry.get("changed"):
                    edited_now = {_norm_fp(f) for f in (event.changed_files or ())}
                    stale = tf not in edited_now
            _record_control(
                state,
                "GT_EDIT_OVERLAY",
                "gateway.route_delivery.episode_overlay",
                "APPLIED" if stale else "NO_EFFECT",
                reason="stale_base_fact" if stale else "fresh_or_untracked_target",
            )
            if stale:
                return ROUTE_STALE
        except Exception as exc:
            _record_control(
                state,
                "GT_EDIT_OVERLAY",
                "gateway.route_delivery.episode_overlay",
                "ERROR",
                reason=f"overlay_error:{type(exc).__name__}",
            )
    observed_semantics = _observe_semantic_events(event, state)
    primary = event.primary_boundary or ""
    cur = _FINE_EVENT_TO_COARSE_ORDINAL.get(primary, _event_ordinal(event.kind))
    want: int | None = None
    registry_on = _registry_enforce()
    registry_error = False
    if registry_on:
        # Reactive means carrier-independent, not self-authorizing. The raw
        # observation must prove the registry's fine semantic event. Fixed
        # lifecycle facts continue to derive ``want`` from their declaration.
        try:
            if _fr_is_reactive(env.evidence_type):
                required = _fr_required_event(env.evidence_type)
                actual = _reactive_semantic_event(env, event, state)
                if actual is None or actual != required:
                    _record_control(
                        state,
                        "GT_REGISTRY_ENFORCE",
                        "gateway.route_delivery.registry_timing",
                        "APPLIED",
                        reason="reactive_semantic_event_unproven",
                    )
                    return ROUTE_DEFER
                want = cur
            else:
                required = _fr_required_event(env.evidence_type)
                # Always consult the executable registry ordinal even when the
                # exact semantic event is present. Semantic membership proves
                # THIS observation; it must not bypass fail-closed detection of
                # a broken/unavailable registry authority.
                declared_want = _registry_want_ordinal(env.evidence_type)
                # Exact semantic membership is stronger than carrier order. One
                # observation may validly contain both edit_result and
                # failure_obs; a single coarse ``kind`` cannot represent that.
                if (
                    event.semantics_authoritative
                    and required in _FINE_EVENT_TO_COARSE_ORDINAL
                    and required not in observed_semantics
                ):
                    _record_control(
                        state,
                        "GT_REGISTRY_ENFORCE",
                        "gateway.route_delivery.registry_timing",
                        "APPLIED",
                        reason="semantic_event_unproven",
                    )
                    return ROUTE_DEFER
                want = cur if required in observed_semantics else declared_want
        except Exception as exc:
            registry_error = True
            _record_control(
                state,
                "GT_REGISTRY_ENFORCE",
                "gateway.route_delivery.registry_timing",
                "ERROR",
                reason=f"registry_timing_error:{type(exc).__name__}",
            )
            # Registry metadata is the authority under enforcement. Never fall back to
            # the producer's self-declared preferred_event when that authority is unavailable:
            # doing so turns an outage into an apparently valid delivery. Suppress explicitly
            # and preserve the ERROR control for diagnosis.
            return ROUTE_REGISTRY_ERROR
    if want is None:
        want = _event_ordinal(env.preferred_event)  # OFF / unregistered -> byte-identical
    if registry_on and not registry_error:
        _record_control(
            state,
            "GT_REGISTRY_ENFORCE",
            "gateway.route_delivery.registry_timing",
            "APPLIED" if cur != want else "NO_EFFECT",
            reason=(
                "too_early"
                if cur < want
                else "too_late"
                if cur > want
                else "declared_boundary_match"
            ),
        )
    if cur < want:
        return ROUTE_DEFER
    if cur > want:
        return ROUTE_EXPIRED_LATE
    return ROUTE_DELIVER


def delivery_latch_key(kind: str, file: str, graph_revision: str) -> str:
    """B-21: the per-``(kind, file, graph_revision)`` delivery-latch key. A per-(kind,file)
    latch keyed by THIS string RE-PERMITS a refreshed contract for a file when the graph
    revision changes (a later edit + reindex bumps the revision), so a file's changed
    contract is no longer PERMANENTLY suppressed by a content-blind boolean latch.
    Deterministic; the file component is path-normalized so ``./a/b.py`` and ``a/b.py`` key
    identically. (The Gateway's own cross-event dedup — ``state.delivered_keys`` — is
    already CONTENT-scoped, so a changed contract re-fires there without noise; this key is
    the seam's per-(kind,file) contract latch adopts for revision-scoped re-permit.)"""
    k = (kind or "").strip().lower()
    f = _norm_fp(file or "")
    r = (graph_revision or "").strip()
    return f"{k}\x00{f}\x00{r}"


# --------------------------------------------------------------------------- #
# PRODUCTION + LEGACY ENTRY POINT
# --------------------------------------------------------------------------- #
def _produce_raw_candidates(
    event: ToolEvent,
    state: GatewayState,
) -> tuple[list[EvidenceEnvelope], list[EvidenceEnvelope]]:
    """Run only the deterministic evidence producers for one event.

    The second list is legacy control-participation metadata for edit bridges.
    It is intentionally private: canonical callers consume only the complete
    first list through :func:`produce_raw`.
    """
    if not _gateway_on():
        return [], []
    _observe_semantic_events(event, state)

    # Record edit events (index + a lowercase blob of command/paths/after-content)
    # for the ZERO_ABSENT ordering predicate — the honest-negative gate mutes only
    # on an edit RELATED to the probed stem (F4).
    if "edit_result" in event.semantic_events:
        parts = [event.command or ""]
        parts += list(event.changed_files or ())
        if event.edit_before_after:
            for _f, (_bef, aft) in sorted(event.edit_before_after.items()):
                parts.append(aft or "")
        state.edit_events.append(
            {
                "index": event.action_index,
                "blob": "\n".join(parts).lower(),
            }
        )

    outcome = classify_outcome(event, state)

    additions: list[EvidenceEnvelope] = []
    edit_bridge_candidates: list[EvidenceEnvelope] = []
    # kind-dispatched producers (independent of the search-outcome lattice)
    if "file_view" in event.semantic_events:
        additions += _produce_caller_contract_view(event, state)
    if "edit_result" in event.semantic_events:
        if _patch_delta_producer_on():  # kill-switch; see _patch_delta_producer_on docstring
            produced = _produce_patch_delta(event, state)
            additions += produced
            if event.edit_before_after:
                edit_bridge_candidates += produced
        # SM-2b: the CROSS-LANGUAGE caller-contract break — the graph-based replacement for
        # the legacy l3.contract/l3b.evidence caller-break patch_delta (ast-only) cannot
        # produce on a non-Python edit. Both are caller-break families; arbitration keeps the
        # per-turn dose at <=1 (signature_mismatch out-ranks caller_break on a Python edit).
        produced = _produce_caller_contract(event, state)
        additions += produced
        if event.edit_before_after:
            edit_bridge_candidates += produced
        # AUDIT 2026-07-24 — change_surface's OWN edit-path trigger (GT_CS_EDIT_TRIGGER, default
        # off => byte-identical). newfile_precedent / GT_CHANGE_SURFACE were reachable ONLY via the
        # ZERO_ABSENT search outcome behind a repeat-a-failed-search gate, so they emitted 0 rows
        # across all 4 tasks of run 30121930273. A file CREATION is the moment sibling/template/
        # registration evidence is maximally useful and needs no search. ADDITIVE: obligations keeps
        # task-start, patch_delta/caller_contract keep their edit slots, ZERO_ABSENT is untouched —
        # this competes for the SAME <=1 dose through the normal arbiter, displacing nothing.
        if (
            _change_surface_producer_on()
            and _cs_edit_trigger_on()
            and _event_creates_new_file(event)
        ):
            produced = _produce_change_surface(
                event,
                state,
                require_repeat=False,
                drop_existing_destinations=True,
                post_creation=True,
            )
            additions += produced
            if event.edit_before_after:
                edit_bridge_candidates += produced
    elif "test_result" in event.semantic_events:
        additions += _produce_covering(event, state)
    elif {"search_result", "failed_search"} & set(event.semantic_events) and _loc_reslot_on():
        # T0->T2 re-slot (2026-07-12, GT_LOC_RESLOT, default OFF -> byte-identical): GT's
        # ranked localization ANSWER, delivered reactively at the D2/post-search boundary
        # (issue-keyed, INDEPENDENT of the search-outcome lattice below — fires even on a
        # behavior-PHRASE grep where the outcome producers abstain). Competes for the single
        # per-turn dose via arbitration (rank 37 > def_ref_partition 35); fire-once via the
        # delivery dedup chain (the issue-fixed answer re-offers with a stable dedup_key).
        additions += _produce_ranked_localization(event, state)

    # outcome-dispatched producers
    if outcome == TRACE_HIT:
        additions += _produce_trace(event, state)
    elif outcome == ZERO_ABSENT:
        if _change_surface_producer_on():  # kill-switch; see _change_surface_producer_on docstring
            additions += _produce_change_surface(event, state)
    elif outcome == ZERO_NAME:
        additions += _produce_name_fold(event, state)
    elif outcome == ZERO_BEHAVIOR:
        additions += _produce_body(event, state)
    elif outcome in (AMBIGUOUS_HIT, FLOOD):
        additions += _produce_def_ref_partition(event, state)
    elif outcome == WRONG_SURFACE:
        additions += _produce_wrong_surface(event, state)
    # EXACT_HIT / SATISFIED -> silence
    return additions, edit_bridge_candidates


def produce_raw(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Produce canonical evidence inputs without making a delivery decision.

    This is the sole Gateway API for the canonical reasoning runtime.  It runs
    the same feature flags, semantic classification, acquisition ledger, and
    producer computations as :func:`augment`, but deliberately does *not*:

    * render or append model-facing bytes;
    * apply legacy registry timing or freshness routing;
    * validate native-renderer availability;
    * consult or mutate the delivered-evidence dedup chain;
    * apply cross-session suppression or winner rank-up; or
    * record legacy candidate-admission decisions.

    The canonical lifecycle, scheduler, coalition selector, and adherence
    compiler own those later decisions.  Producer-required acquisition state
    (for example repeated-search history and localization cache) remains
    attempt-scoped in ``state.episode`` and is intentionally shared with the
    legacy producer computations.
    """
    additions, _edit_bridge_candidates = _produce_raw_candidates(event, state)
    return additions


def augment(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Complete one legacy tool observation with deterministic facts.

    ``[]`` is returned when the master flag is off, when the outcome needs
    nothing, or when every producer abstains.  Legacy timing, freshness,
    validation, deduplication, cross-session policy, and rank-up behavior remain
    here; canonical callers must use :func:`produce_raw` instead.
    """
    additions, edit_bridge_candidates = _produce_raw_candidates(event, state)
    if not additions:
        return []

    # leak-law (target) + envelope-law validation + need gate (dedup), in
    # deterministic order. The validate() drop is fail-closed BY CONSTRUCTION:
    # a law-violating envelope (leak / tier dishonesty / unmeasured-enforcing /
    # tampered dedup key) is never delivered — correct-or-quiet.
    #
    # F1 (bounce 2026-07-10): production READS the dedup chain, it NEVER stamps it.
    # Stamping here destroyed produced-but-undelivered facts (an arbitration loss,
    # leak-guard drop, or law-8 budget drop downstream muted the fact for the whole
    # episode) — contradicting the seam's own deferral law ("produced-but-not-emitted
    # is DEFERRED, not destroyed"). The DELIVERY COMMIT stamps instead: the seal step
    # (adapters.miniswe.seal_delivery with dedup_chain=the episode chain) adds the
    # winner's key the moment its bytes are actually appended. A local intra-call
    # set still dedups duplicates WITHIN one production batch.
    out: list[EvidenceEnvelope] = []
    seen_this_call: set[str] = set()
    for a in additions:
        # Graph-F2 (bounce 2026-07-10): the fact-registry render invariant, now REAL. An
        # evidence_type with no registration (directly, via _EVIDENCE_TYPE_ALIASES, or a
        # base:suffix form) has no declared decision/deliver-by/dose, so it must never
        # render (correct-or-quiet). Every evidence_type the producers above emit is
        # registered/aliased, so this is a no-op for real facts and blocks only a
        # truly-unregistered class (tests pin the full emitted set as renderable).
        if not _renderable(a.evidence_type):
            continue
        # SS-1 EMPTY-PAYLOAD guard (GT_SS_ARBITER_V2): a produced envelope that would render
        # ZERO model-facing bytes must never be delivered (the hydra-3005 chars=0 / empty-seal
        # delivered row). Dropped here BEFORE it can enter the global pool. OFF ⇒ skipped ⇒
        # byte-identical (real producers always build a non-empty payload, so this rejects
        # only a degenerate empty-payload envelope, and only under the flag).
        if _ss_arbiter_v2_on() and not _envelope_has_bytes(a):
            try:
                _cand_bytes, _fc = _candidate_control_identity(a)
                _record_control(
                    state,
                    "GT_SS_ARBITER_V2",
                    "gateway.augment.empty_payload_guard",
                    "APPLIED",
                    candidate_bytes=_cand_bytes,
                    fact_class=_fc,
                    candidate_id=getattr(a, "dedup_key", "") or "",
                    reason="empty_payload_dropped",
                )
            except Exception:
                pass
            continue
        # SM-0 registry ENFORCEMENT (GT_REGISTRY_ENFORCE): bind producer -> the DECLARED
        # native renderer. A class with no available renderer has no native FORM to ride the
        # channel the model reads, so it is dropped correct-or-quiet. (The firing-event vs
        # DECLARED-boundary check is enforced by route_delivery below, which under the same
        # flag derives ``want`` from the registry ``deliver_by`` instead of the self-stamp.)
        # OFF ⇒ skipped ⇒ byte-identical. A registered class always has a renderer, so this
        # never rejects a real fact — it guards a future renderer-less / mis-declared class.
        if _registry_enforce():
            try:
                renderer_missing = _fr_registration_for(
                    a.evidence_type
                ) is None or not _fr_required_renderer(a.evidence_type)
                _record_control(
                    state,
                    "GT_REGISTRY_ENFORCE",
                    "gateway.augment.renderer_enforcement",
                    "APPLIED" if renderer_missing else "NO_EFFECT",
                    reason=("renderer_missing" if renderer_missing else "renderer_available"),
                )
            except Exception as exc:
                # Existing enforcement is fail-closed; preserve that behavior,
                # but distinguish the failed measurement from a valid drop.
                renderer_missing = True
                _record_control(
                    state,
                    "GT_REGISTRY_ENFORCE",
                    "gateway.augment.renderer_enforcement",
                    "ERROR",
                    reason=f"registry_renderer_error:{type(exc).__name__}",
                )
            if renderer_missing:
                continue
        if _is_leaky(a.target):
            continue
        if _validate_envelope(a):
            continue
        # B-12: enforce the envelope's timing + freshness. defer (too early) / expire (too
        # late) / stale (a depended sub-rev changed) are NOT delivered — only ROUTE_DELIVER
        # ships. No-op on the happy path (a fact built + delivered on its own event, fresh).
        if route_delivery(a, event, state) != ROUTE_DELIVER:
            continue
        if a.dedup_key in state.delivered_keys or a.dedup_key in seen_this_call:
            continue
        seen_this_call.add(a.dedup_key)
        out.append(a)
    # SM-9c: adapt the candidate list from this repo's LEARNED cross-session policy
    # (suppress a never-consumed class, rank-up a historically-consumed one). No-op /
    # same-object return when the flag is off or the policy is empty -> byte-identical.
    #   * _apply_xsession_policy — SUPPRESS (GT_XSESSION_MEMORY): drop an inert class.
    #   * _apply_xsession_rankup — WINNER PROMOTION (GT_XSESSION_RANKUP): stamp a ladder
    #     boost onto a consumed class so it can WIN the adapter's <=1 dose. Runs LAST, on the
    #     already-eligible list, so it re-ranks (never bypasses timing/freshness/leak/dedup).
    final = _apply_xsession_rankup(_apply_xsession_policy(out, state), state)
    final_keys = {candidate.dedup_key for candidate in final}
    for candidate in final:
        try:
            candidate_bytes, fact_class = _candidate_control_identity(candidate)
            _record_control(
                state,
                "GT_GATEWAY",
                "gateway.augment.candidate_admission",
                "APPLIED",
                candidate_bytes=candidate_bytes,
                fact_class=fact_class,
                candidate_id=candidate.dedup_key,
                reason="candidate_admitted",
            )
        except Exception as exc:
            _record_control(
                state,
                "GT_GATEWAY",
                "gateway.augment.candidate_admission",
                "ERROR",
                reason=f"identity_error:{type(exc).__name__}",
            )
    for candidate in edit_bridge_candidates:
        try:
            candidate_bytes, fact_class = _candidate_control_identity(candidate)
            admitted = candidate.dedup_key in final_keys
            _record_control(
                state,
                "GT_GATEWAY_EDIT_BRIDGES",
                "gateway.augment.edit_bridge_candidate",
                "APPLIED" if admitted else "SUPPRESSED",
                candidate_bytes=candidate_bytes if admitted else "",
                fact_class=fact_class,
                candidate_id=candidate.dedup_key,
                reason="candidate_admitted" if admitted else "candidate_filtered",
            )
        except Exception as exc:
            _record_control(
                state,
                "GT_GATEWAY_EDIT_BRIDGES",
                "gateway.augment.edit_bridge_candidate",
                "ERROR",
                reason=f"identity_error:{type(exc).__name__}",
            )
    return final

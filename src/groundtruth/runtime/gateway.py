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

from groundtruth.delivery.path_policy import is_deliverable
from groundtruth.pretask.curation_map import (
    DETERMINISTIC_RESOLUTION_METHODS,
    parse_edge_metadata,
)
from groundtruth.runtime.covering_runner import _connect_ro, _edge_columns
from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.evidence_envelope import (
    ADVISORY,
    EVENT_STEP0,
    HYPOTHESIS,
    INFO,
    VERIFIED,
    WARNING,
    EvidenceEnvelope,
)
from groundtruth.runtime.evidence_envelope import validate as _validate_envelope
from groundtruth.runtime.fact_registry import (
    EVENTS as fr_EVENTS,
)
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
    from groundtruth.pretask.change_surface import detect_change_surface
except Exception:  # noqa: BLE001
    detect_change_surface = None  # type: ignore[assignment]
try:
    from groundtruth.runtime.patch_delta import analyze_patch_delta
except Exception:  # noqa: BLE001
    analyze_patch_delta = None  # type: ignore[assignment]
# T0->T2 localization re-slot (2026-07-12): localize() is the OFFLINE source of GT's
# ranked localization answer, re-homed from the T0 baked brief to the D2/post-search
# reactive producer (_produce_ranked_localization). Heavy pretask leg -> try-wrapped
# exactly like detect_change_surface: unavailable -> None -> the producer degrades
# correct-or-quiet ([]). Kept a real module attribute so tests can monkeypatch it.
try:
    from groundtruth.pretask.graph_localizer import localize as _localize
except Exception:  # noqa: BLE001
    _localize = None  # type: ignore[assignment]

__all__ = [
    "ToolEvent",
    "EvidenceEnvelope",  # re-export: the ONE payload contract the Gateway emits
    "CoveringResult",
    "GatewayState",
    "augment",
    "classify_outcome",
    "classify_command",
    "normalize_search",
    "search_pattern",
    # B-12 timing/freshness routing + B-21 revision-scoped latch key
    "route_delivery",
    "delivery_latch_key",
    "ROUTE_DELIVER", "ROUTE_DEFER", "ROUTE_EXPIRED_LATE", "ROUTE_STALE",
    # kinds
    "KIND_SEARCH", "KIND_VIEW", "KIND_EDIT", "KIND_TEST", "KIND_SUBMIT", "KIND_OTHER",
    # outcome states
    "EXACT_HIT", "SATISFIED", "AMBIGUOUS_HIT", "FLOOD", "WRONG_SURFACE", "TRACE_HIT",
    "ZERO_NAME", "ZERO_BEHAVIOR", "ZERO_ABSENT",
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

EXACT_HIT = "EXACT_HIT"           # silence — the agent already has the def
SATISFIED = "SATISFIED"           # silence — nothing to add
AMBIGUOUS_HIT = "AMBIGUOUS_HIT"   # def spans 2-3 files -> def/ref partition
FLOOD = "FLOOD"                   # a very large raw hit set -> cut with the def
WRONG_SURFACE = "WRONG_SURFACE"   # every hit is test/vendored; the def is elsewhere
TRACE_HIT = "TRACE_HIT"           # output carries an in-repo stack trace
ZERO_NAME = "ZERO_NAME"           # empty grep; name exists under a fold variant
ZERO_BEHAVIOR = "ZERO_BEHAVIOR"   # empty grep; concept only in function bodies
ZERO_ABSENT = "ZERO_ABSENT"       # empty grep; name+path+body all miss (truly absent)

# Trust tiers come from the canonical envelope module (imported above): VERIFIED /
# WARNING / INFO / HYPOTHESIS — a single vocabulary, never re-declared here.

# Deterministic tier -> default confidence (tier-honest by construction: VERIFIED
# >= 0.7 with provenance; HYPOTHESIS < 0.7). A producer with a REAL measured
# confidence (patch_delta's FACT edges) passes it explicitly instead.
_TIER_DEFAULT_CONF: dict[str, float] = {
    VERIFIED: 0.9, WARNING: 0.7, INFO: 0.5, HYPOTHESIS: 0.5,
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
    action_index: int = 0
    # Optional seam bridges — NOT part of the raw observation; injected by the seam
    # when it has them (kept off the 7 core fields so the observation stays pure).
    edit_before_after: "Mapping[str, tuple[str | None, str]] | None" = None
    covering: "CoveringResult | None" = None


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
    episode: EpisodeState = field(default_factory=EpisodeState)
    # SM-4 (2026-07-11): the per-run LIGHTWEIGHT episode overlay — ``{norm_file_path:
    # overlay_entry}`` built per structured edit (edit_overlay.build_episode_overlay_entry),
    # where ``overlay_entry['changed']`` is the mark_stale_envelopes verdict that the file's
    # base graph.db structure no longer matches its CURRENT content. Passed IN by the seam
    # each turn (the persistent store lives on the seam beside the episode); read by
    # :func:`route_delivery` under ``GT_EDIT_OVERLAY`` to drop a base-read fact about an
    # edited file. Empty (default) -> byte-identical, the overlay is a no-op.
    episode_overlay: dict = field(default_factory=dict)

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
        "", "0", "false", "no", "off"
    )


def _episode_overlay_on() -> bool:
    """SM-4 flag (2026-07-11). The episode-overlay freshness DROP rides ``GT_EDIT_OVERLAY``
    — the same flag that arms the transactional edit overlay in the seam; SM-4 gives it its
    model-facing value: a base-read fact that is STALE against the CURRENT edit state is
    dropped, not shipped. Default OFF ⇒ ``route_delivery`` is byte-identical (the overlay is
    never consulted)."""
    return os.environ.get("GT_EDIT_OVERLAY", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _loc_reslot_on() -> bool:
    """T0->T2 localization re-slot flag (2026-07-12). Arms the reactive
    :func:`_produce_ranked_localization` producer at the D2/post-search boundary, so GT's
    RANKED localization answer (from ``localize()``, memoized per episode) rides the agent's
    OWN grep channel as ``path:line:sym`` rows instead of only the T0 baked ``<gt-localization>``
    brief tag. Default OFF ⇒ the producer is never dispatched from :func:`augment` ⇒
    byte-identical (no ``localization`` envelope is ever produced by the Gateway)."""
    return os.environ.get("GT_LOC_RESLOT", "").strip().lower() not in (
        "", "0", "false", "no", "off"
    )


def _xsession_on() -> bool:
    """SM-9c flag (2026-07-11). Arms the CROSS-SESSION learned-delivery policy: the Gateway
    consults this repo's durable causal-consumption ledger (loaded once at task-start and
    threaded onto ``state.episode.xsession_policy``) to SUPPRESS a fact-class this repo
    NEVER once consumed and RANK-UP a class it historically acted on. Default OFF (and an
    empty policy) ⇒ :func:`_apply_xsession_policy` is a byte-identical no-op."""
    return os.environ.get("GT_XSESSION_MEMORY", "").strip().lower() not in (
        "", "0", "false", "no", "off"
    )


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
        canon = _xm_canonical(a.evidence_type)
        if canon in _XSESSION_POLICY_CLASSES and _xm_inert(policy, canon):
            continue  # LEARNED SUPPRESS: delivered here before, never once consumed
        kept.append(a)
    # LEARNED RANK-UP: stable sort (Python sort is stable) by descending policy rank; a
    # non-policy / non-consumed class has rank 0 and keeps its original relative order.
    kept.sort(
        key=lambda a: -_xm_rank(
            policy,
            _xm_canonical(a.evidence_type)
            if _xm_canonical(a.evidence_type) in _XSESSION_POLICY_CLASSES
            else None,
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
        "", "0", "false", "no", "off"
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
    policy = getattr(getattr(state, "episode", None), "xsession_policy", None)
    if not policy:
        return out
    try:
        from groundtruth.runtime.xsession_memory import (
            canonical_class as _xm_canonical,
            ladder_boost as _xm_boost,
        )
    except Exception:  # noqa: BLE001 — engine absent in-container: preserve behaviour
        return out
    changed = False
    stamped: list[EvidenceEnvelope] = []
    for a in out:
        canon = _xm_canonical(a.evidence_type)
        boost = _xm_boost(policy, canon) if canon in _XSESSION_POLICY_CLASSES else 0
        if boost > 0:
            na = dict(a.native_args or {})
            na["_xsession_boost"] = int(boost)  # reserved arbitration side-car key
            stamped.append(replace(a, native_args=na))
            changed = True
        else:
            stamped.append(a)
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
                return nf[len(cr):]
        if root:
            nroot = root.replace("\\", "/").rstrip("/") + "/"
            if nf.startswith(nroot):
                return nf[len(nroot):]
        try:
            return os.path.relpath(f, root) if root else f
        except (ValueError, TypeError):
            return f
    return f


# --------------------------------------------------------------------------- #
# SEARCH normalization (byte-equivalent to gt_mini_patch._search_pattern).
# --------------------------------------------------------------------------- #
_GREP_HEAD_RE = re.compile(r"(?:^|[|&;]\s*)(?:grep|egrep|fgrep|rg)\b")
_BARE_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
# Flags that CONSUME the next token as a value (so it is never mistaken for the
# pattern operand). -E / -T are DELIBERATELY absent (valueless in GNU grep).
_GREP_VALUE_FLAGS = frozenset({
    "-e", "--regexp", "-f", "--file", "-m", "--max-count",
    "-A", "-B", "-C", "--context", "--after-context", "--before-context", "-d",
    "-t", "--type", "--type-not", "-g", "--glob", "--iglob",
    "--include", "--exclude", "--exclude-dir", "--exclude-from",
    "-M", "--max-columns", "-j", "--threads", "--encoding",
})


@dataclass(frozen=True)
class SearchQuery:
    """The normalized shape of a grep/rg command."""

    pattern: str                    # bare-symbol operand (== search_pattern), else ""
    raw_operand: str                # dequoted operand even when not a bare symbol
    scope: tuple[str, ...]          # path operands
    filters: tuple[str, ...]        # flag tokens (best-effort)


def _search_operand_raw(cmd: str) -> str | None:
    """The DEQUOTED pattern operand of a grep/rg command, or None (no bare gate)."""
    head = (cmd or "").split("\n", 1)[0]
    m = _GREP_HEAD_RE.search(head)
    if not m:
        return None
    try:
        toks = shlex.split(head[m.end():])
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
        toks = shlex.split(head[m.end():]) if m else []
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
_KIND_PRECEDENCE = {KIND_SUBMIT: 5, KIND_TEST: 4, KIND_EDIT: 3,
                    KIND_SEARCH: 2, KIND_VIEW: 1, KIND_OTHER: 0}


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
        return all(ln.strip() == "0" or ln.strip().endswith(":0")
                   for ln in s.splitlines() if ln.strip())
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
    e["probe_indices"].append(int(idx))
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
            "SELECT key, value FROM project_meta "
            "WHERE key = 'post_revision' OR key LIKE 'subrev_%'"
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
            subs[ks[len("subrev_"):]] = str(v or "")
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
_DEF_LABELS = ("Function", "Method", "Class", "Interface", "Struct", "Enum",
               "Trait", "Constructor")
_FACT_CONF_FLOOR = 0.7
_FLOOD_HITS = 20  # distinct raw hit paths above which a search is a FLOOD


def _open(state: GatewayState):
    db = state.graph_db
    if not db or not os.path.isfile(db):
        return None
    return _connect_ro(db)


def _resolve_symbol_defs(con, symbol: str, root: str) -> dict | None:
    """def-sites (1-3 non-leaky files) + FACT-tier caller provenance + test-ref COUNT.
    None when the symbol resolves to no deliverable def or spans >3 files (ambiguous)."""
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        rows = con.execute(
            f"SELECT id, file_path, start_line FROM nodes "
            f"WHERE name=? AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql}) ORDER BY file_path, start_line",
            (symbol, *_DEF_LABELS)).fetchall()
    except sqlite3.Error:
        return None
    rows = [r for r in rows if not _is_leaky(_to_repo_rel(r[1] or "", root))]
    if not rows:
        return None
    if len({r[1] for r in rows}) > 3:
        return None  # ambiguous common / stdlib-shadow name -> not a fact
    def_ids = [r[0] for r in rows]
    def_sites = [(_to_repo_rel(r[1] or "", root), int(r[2] or 0)) for r in rows]
    callers, receiver_types = _fact_callers(con, def_ids)
    test_ref_count = _test_ref_count(con, def_ids)
    return {
        "def_sites": def_sites,
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
    qmarks = ",".join("?" * len(def_ids))
    use_em = _edge_metadata_ready(con)
    if use_em:
        # receiver_type straight from the normalized sub-table. LEFT JOIN so a caller
        # edge with no receiver_type still returns its caller row exactly once — the
        # (edge_id,key) PK guarantees at most one receiver_type row per edge.
        sql = (
            f"SELECT ns.name, ns.file_path, e.source_line, em.value "
            f"FROM edges e JOIN nodes ns ON ns.id=e.source_id "
            f"LEFT JOIN edge_metadata em ON em.edge_id=e.id AND em.key='receiver_type' "
            f"WHERE e.target_id IN ({qmarks}) AND e.type='CALLS' "
            f"AND COALESCE(ns.is_test,0)=0 "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{_det_in_sql()}') {conf_gate}"
        )
    else:
        meta_sel = "e.metadata" if has_meta else "NULL"
        sql = (
            f"SELECT ns.name, ns.file_path, e.source_line, {meta_sel} "
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
    for name, fp, line, meta in rows:
        callers.append({"name": name or "", "file": fp or "", "line": int(line or 0)})
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
            f"AND COALESCE(sn.is_test,0)=1", def_ids).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def _name_or_path_matches(con, sym: str) -> bool:
    try:
        for variant in _fold_variants(sym):
            if con.execute("SELECT 1 FROM nodes WHERE name=? LIMIT 1", (variant,)).fetchone():
                return True
        low = sym.lower()
        if con.execute("SELECT 1 FROM nodes WHERE LOWER(file_path) LIKE '%'||?||'%' LIMIT 1",
                       (low,)).fetchone():
            return True
    except sqlite3.Error:
        return True  # fail toward suppression (no false absence)
    return False


def _has_content_fts(con) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='symbol_content_fts' LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _content_passages_ready(con) -> bool:
    """True iff BOTH ``content_passages`` and its FTS twin ``content_passages_fts`` exist
    (B-23). On an old graph.db without them a body-concept match cites the node
    ``start_line`` (correct-or-quiet)."""
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('content_passages','content_passages_fts')").fetchone()
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
            'SELECT p.start_line, p.end_line, p.content, p.content_hash '
            'FROM content_passages_fts f JOIN content_passages p ON p.passage_id=f.rowid '
            'WHERE p.node_id=? AND f.content MATCH ? ORDER BY f.rank LIMIT 1',
            (node_id, '"' + sym.replace('"', "") + '"')).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] else 0


def _body_rows(con, sym: str, root: str) -> list[tuple]:
    try:
        raw = con.execute(
            'SELECT n.id, n.file_path, n.start_line, n.name, n.label '
            'FROM symbol_content_fts f JOIN nodes n ON n.id=f.rowid '
            'WHERE symbol_content_fts MATCH ? AND COALESCE(n.is_test,0)=0 '
            'ORDER BY n.file_path, n.start_line LIMIT 26',
            ('"' + sym.replace('"', "") + '"',)).fetchall()
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
        out.append((rel, cite, name or "", (label or "").lower()))
    return out


def _namefold_hit(con, sym: str, root: str) -> tuple[str, dict] | None:
    """First fold variant that resolves to a deliverable def -> (variant, info)."""
    for variant in _fold_variants(sym):
        try:
            hit = con.execute("SELECT 1 FROM nodes WHERE name=? AND COALESCE(is_test,0)=0 LIMIT 1",
                              (variant,)).fetchone()
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
    if _has_repo_trace(event, state):
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
            if _has_content_fts(con) and not _name_or_path_matches(con, sym) \
                    and _body_rows(con, sym, root):
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
    return any(not _is_leaky(_to_repo_rel(f.file, state.repo_root)) for f in frames)


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


def _event_pref(event: ToolEvent) -> str:
    """The envelope's preferred_event from the triggering kind (``other`` -> step0)."""
    k = (event.kind or "").strip().lower()
    return k if k in (KIND_SEARCH, KIND_VIEW, KIND_EDIT, KIND_TEST, KIND_SUBMIT) \
        else EVENT_STEP0


def _mk_add(state: GatewayState, event: ToolEvent, *, fact_kind: str, target: str,
            body_lines: list[str], evidence: list[tuple[str, int]], tier: str,
            producer: str, symbol: str = "",
            confidence: float | None = None,
            native_args: "dict | None" = None) -> EvidenceEnvelope:
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
    return EvidenceEnvelope.build(
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
    )


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


def _produce_def_ref_partition(event: ToolEvent, state: GatewayState, *, note: str = "") -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        info = _resolve_symbol_defs(con, sym, state.repo_root)
    finally:
        con.close()
    if not info or not info["def_sites"]:
        return []
    body = ([note] if note else []) + _def_partition_body(info)
    target = info["def_sites"][0][0]
    return [_mk_add(state, event, fact_kind="def_ref_partition", target=target,
                    body_lines=body, evidence=info["def_sites"], tier=VERIFIED,
                    producer="def_ref_partition", symbol=sym)]


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
            (file_path, *_DEF_LABELS)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    for name, ln in rows:  # prefer an anchor-named def (the file's issue reason)
        if name and (name or "").lower() in anchors:
            return (name, int(ln or 0))
    name, ln = rows[0]                                     # else the first def
    return (name, int(ln or 0)) if name else None


def _compute_ranked_localization_rows(state: GatewayState) -> "list[tuple[str, int, str]]":
    """Run localize() over the episode's graph.db + issue text and resolve the top-N ranked
    candidate FILES to leak-clean ``(repo_rel_path, line, symbol)`` rows. Pure/deterministic
    over a fixed graph (no rebake needed). Correct-or-quiet: no localize / no issue / no graph
    / no candidates / all-leaky -> []."""
    if _localize is None or not (state.issue_text or "").strip():
        return []
    db = state.graph_db
    if not db or not os.path.isfile(db):
        return []
    try:
        res = _localize(state.issue_text, db, repo_root=state.repo_root)
    except Exception:  # noqa: BLE001 — a localizer fault degrades to no delivery
        return []
    cands = list(getattr(res, "candidates", None) or [])
    if not cands:
        return []
    anchors = {(a or "").lower() for a in (getattr(res, "anchor_symbols", None) or [])}
    con = _open(state)
    if con is None:
        return []
    rows: list[tuple[str, int, str]] = []
    try:
        for c in cands[:_LOC_RESLOT_TOPN]:
            raw_fp = getattr(c, "file_path", "") or ""
            if not raw_fp:
                continue
            rel = _to_repo_rel(raw_fp, state.repo_root)
            if _is_leaky(rel):        # firewall #1 (producer): a test/vendored candidate is
                continue              # never a row (the localizer already demotes tests).
            picked = _pick_candidate_symbol(con, raw_fp, anchors)
            if picked is None:
                continue
            sym, line = picked
            rows.append((rel, int(line), sym))
    finally:
        con.close()
    return rows


def _ranked_localization_rows(state: GatewayState) -> "list[tuple[str, int, str]]":
    """The episode-MEMOIZED ranked-localization rows: localize() runs ONCE per run (the answer
    is issue-fixed), reused on every subsequent search turn. Stored on ``state.episode`` (a
    non-serialized private attribute); a slotted episode that rejects the setattr just
    recomputes (correct-or-quiet, never a crash)."""
    ep = state.episode
    cached = getattr(ep, "_gt_ranked_loc_rows", None)
    if cached is not None:
        return cached
    rows = _compute_ranked_localization_rows(state)
    try:
        setattr(ep, "_gt_ranked_loc_rows", rows)
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
    rows = _ranked_localization_rows(state)
    if not rows:
        return []
    top_file, _top_line, top_sym = rows[0]
    body = [f"{p}:{ln}:{s}" for p, ln, s in rows]      # tag-free grep rows (hedge-free)
    evidence = [(p, ln) for p, ln, _s in rows]
    return [_mk_add(state, event, fact_kind="localization", target=top_file,
                    body_lines=body, evidence=evidence, tier=HYPOTHESIS,
                    producer="ranked_localization", symbol=top_sym,
                    native_args={"rows": rows})]


def _produce_wrong_surface(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        info = _resolve_symbol_defs(con, sym, state.repo_root)
    finally:
        con.close()
    if not info or not info["def_sites"]:
        return []
    hit_paths = _grep_hit_paths(event.output or "", state.repo_root)
    novel = [(fp, ln) for fp, ln in info["def_sites"] if _norm_fp(fp) not in hit_paths]
    if not novel:
        return []
    body = ['your hits are all test/vendored copies; the definition is here']
    body += [f"def: {fp}:{ln}" for fp, ln in novel[:3]]
    return [_mk_add(state, event, fact_kind="wrong_surface", target=novel[0][0],
                    body_lines=body, evidence=novel, tier=VERIFIED,
                    producer="wrong_surface", symbol=sym)]


def _produce_name_fold(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        hit = _namefold_hit(con, sym, state.repo_root)
    finally:
        con.close()
    if hit is None:
        return []
    variant, info = hit
    if variant == sym:
        note = f'no grep hits for "{sym}", but it IS indexed (check your path/filetype filter)'
    else:
        note = f'"{sym}" not found; indexed as "{variant}"'
    body = [note] + _def_partition_body(info)
    return [_mk_add(state, event, fact_kind="name_fold", target=info["def_sites"][0][0],
                    body_lines=body, evidence=info["def_sites"], tier=VERIFIED,
                    producer="name_fold", symbol=sym)]


def _produce_body(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        rows = _body_rows(con, sym, state.repo_root)
    finally:
        con.close()
    if not rows:
        return []
    body = [f'{len(rows)} function bodies mention "{sym}" (no name or path match)']
    body += [f"in {label} {name} - {fp}:{ln}" for fp, ln, name, label in rows[:8]]
    evidence = [(fp, ln) for fp, ln, _n, _l in rows[:8]]
    # SM (2026-07-12): the concept-hit function bodies as (path,line,sym) rows so the adapter's
    # body_concept renderer emits the grep-native ROW block DIRECTLY (no re-parse of GT's prose).
    # The SYMBOL is the concept-bearing function NAME. Side-car only (compare=False, unserialized)
    # — identity / dedup / byte-chain UNCHANGED; native-render-only, so the tagged path is unmoved.
    # Leak is enforced at RENDER time (render_body_concept_native drops test-file rows), matching
    # the localization precedent — native_args is not leak-filtered by _mk_add.
    native_rows = [(fp, ln, name) for fp, ln, name, _l in rows[:8]]
    return [_mk_add(state, event, fact_kind="body_concept", target=rows[0][0],
                    body_lines=body, evidence=evidence, tier=INFO,
                    producer="body_concept", symbol=sym,
                    native_args={"rows": native_rows})]


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
        prior_zero = [i for i, o in zip(e["probe_indices"], e["outcomes"])
                      if o == "zero" and i < event.action_index]
        if not prior_zero:
            continue
        prev = max(prior_zero)
        if any(prev < ee["index"] < event.action_index
               and _edit_related_to_stem(ee["blob"], tok)
               for ee in state.edit_events):
            continue  # a RELATED edit intervened -> the agent may have created it
        return True
    return False


def _produce_change_surface(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    if not _zero_absent_repeat_ok(event, state):
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
        # F2(a): template / registration / evidence rows go through the SAME leak
        # predicate as targets — a leaky row is dropped, the addition survives when
        # its core (suggested_path) is clean. _mk_add's belt re-checks every line.
        body = [f"new file: {d.suggested_path}"]
        if d.template_file and not _is_leaky(d.template_file):
            body.append(f"template: {d.template_file}")
        if d.registration_file and not _is_leaky(d.registration_file):
            body.append(f"integrate at: {d.registration_file}")
        body += [ev for ev in d.evidence[:4] if not _body_line_leaky(ev)]
        out.append(_mk_add(state, event, fact_kind="new_file_destination", target=d.suggested_path,
                           body_lines=body, evidence=[], tier=HYPOTHESIS,
                           producer="change_surface", symbol=d.entity))
    for m in res.missing_roles:
        tgt = m.registration_file or (m.sibling_files[0] if m.sibling_files else m.entity)
        if _is_leaky(tgt):
            continue
        ev_rows = [(m.registration_file, ln) for ln, _ in m.registration_lines] if m.registration_file else []
        body = [ev for ev in m.evidence[:4] if not _body_line_leaky(ev)]
        out.append(_mk_add(state, event, fact_kind=f"missing_role:{m.role}", target=tgt,
                           body_lines=body, evidence=ev_rows,
                           tier=HYPOTHESIS, producer="change_surface", symbol=m.entity))
    return out


def _produce_trace(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    try:
        frames = parse_stack_traces(event.output or "", state.repo_root or ".")
    except Exception:  # noqa: BLE001
        return []
    for fr in frames:  # deepest in-repo first (parse_stack_traces ordering)
        rel = _to_repo_rel(fr.file, state.repo_root)
        if _is_leaky(rel):
            continue
        loc = f"{rel}:{fr.line}" + (f" in {fr.func}" if fr.func else "")
        return [_mk_add(state, event, fact_kind="trace_frame", target=rel,
                        body_lines=[f"deepest in-repo frame: {loc}"],
                        evidence=[(rel, fr.line)], tier=WARNING, producer="trace",
                        symbol=fr.func or rel)]
    return []


def _produce_patch_delta(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    if not event.edit_before_after:
        return []
    try:
        res = analyze_patch_delta(dict(event.edit_before_after), state.repo_root, state.graph_db or "")
    except Exception:  # noqa: BLE001
        return []
    out: list[EvidenceEnvelope] = []
    for sm in res.signature_mismatches:
        if _is_leaky(sm.caller_file):
            continue
        body = [f"{sm.caller}() call passes {sm.positional_args} positional arg(s); "
                f"{sm.symbol}() now takes {sm.new_min_params}-{sm.new_max_params}",
                sm.call_site_text]
        # SM-10: attach the STRUCTURED arity fields so the adapter can render the SM-1
        # mypy/gopls arity diagnostic natively (native_render.render_signature_delta_native)
        # instead of _render_generic prose. Side-car only — identity/dedup UNCHANGED.
        out.append(_mk_add(state, event, fact_kind="signature_mismatch", target=sm.caller_file,
                           body_lines=body, evidence=[(sm.caller_file, sm.caller_line)],
                           tier=WARNING, producer="patch_delta", symbol=sm.symbol,
                           confidence=max(0.0, min(1.0, sm.confidence)),
                           native_args={
                               "caller_file": sm.caller_file,
                               "caller_line": sm.caller_line,
                               "symbol": sm.symbol,
                               "positional_args": sm.positional_args,
                               "new_min_params": sm.new_min_params,
                               "new_max_params": sm.new_max_params,
                           }))
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
        out.append(_mk_add(state, event, fact_kind="companion_surface", target=cs.file,
                           body_lines=body, evidence=ev_rows,
                           tier=WARNING, producer="patch_delta", symbol=cs.symbol,
                           native_args={
                               "file": cs.file,
                               "line": (ev_rows[0][1] if ev_rows else None),
                               "symbol": cs.symbol,
                               "siblings": list(cs.siblings),
                           }))
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
    r"(?:\([^()]*\)\s*)?"                      # optional Go receiver `(r *T) `
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
                return s[open_idx + 1:i]
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


def _fact_callers_of_symbol_in_file(con, sym: str, rel: str, root: str) -> list[tuple[str, int]]:
    """FACT-tier (deterministic resolution_method AND conf>=0.7), NON-test, CROSS-FILE callers
    of ``sym`` DEFINED in ``rel`` (graph frame) — the callers a signature change breaks that the
    agent is NOT looking at. Returns ``[(caller_rel, line)]`` deduped to ONE row per distinct
    (caller_name, caller_file), leak-filtered (a test/vendored caller file is dropped). Reuses
    :func:`_fact_callers` so the FACT gate is byte-identical to def_partition's."""
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        drows = con.execute(
            f"SELECT id FROM nodes WHERE name=? AND file_path=? "
            f"AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql})",
            (sym, rel, *_DEF_LABELS)).fetchall()
    except sqlite3.Error:
        return []
    def_ids = [r[0] for r in drows]
    if not def_ids:
        return []
    callers, _receivers = _fact_callers(con, def_ids)
    nrel = _norm_fp(rel)
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, int]] = []
    for c in callers:
        cf = _norm_fp(_to_repo_rel(c.get("file") or "", root))
        if not cf or cf == nrel:            # cross-file only
            continue
        if _is_leaky(cf):                   # leak law: never a test/vendored caller
            continue
        key = (c.get("name") or "", cf)
        if key in seen:
            continue
        seen.add(key)
        out.append((cf, int(c.get("line") or 0)))
    out.sort()
    return out


def _produce_caller_contract(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """The CROSS-LANGUAGE caller-contract break on a signature-changing edit. For each edited
    file whose function/method changed its parameter list (pure-text before/after diff), if
    the changed symbol has >=1 FACT-tier cross-file caller in graph.db, emit a ``caller_break``
    envelope (aliases to the registered ``caller_contract`` class; delivered at ``edit_result``
    per the registry boundary override). Correct-or-quiet: no before/after, no graph, no
    detectable signature change, or no cross-file caller -> []. Works on Go/Rust/TS/JS/Python
    because it reads the tree-sitter CALLS graph, NOT a Python ast."""
    eba = event.edit_before_after
    if not eba:
        return []
    con = _open(state)
    if con is None:
        return []
    out: list[EvidenceEnvelope] = []
    try:
        for cf, ba in sorted(eba.items()):
            before, after = ba if isinstance(ba, tuple) else (None, ba)
            rel = _norm_fp(_to_repo_rel(cf, state.repo_root))
            if not rel or _is_leaky(rel):
                continue
            for sym in _sig_changed_symbols(before or "", after or ""):
                sites = _fact_callers_of_symbol_in_file(con, sym, rel, state.repo_root)
                if not sites:
                    continue
                n_callers = len(sites)
                n_files = len({f for f, _ in sites})
                body = [f"{sym}() signature changed — {n_callers} caller(s) in "
                        f"{n_files} file(s); update the call sites"]
                out.append(_mk_add(state, event, fact_kind="caller_break", target=rel,
                                   body_lines=body, evidence=sites, tier=WARNING,
                                   producer="caller_contract", symbol=sym))
    finally:
        con.close()
    return out


def _produce_covering(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
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
    return [_mk_add(state, event, fact_kind="covering_verdict", target=cov.target,
                    body_lines=rendered.splitlines(), evidence=list(cov.evidence),
                    tier=cov.tier or WARNING, producer="covering",
                    symbol=cov.verdict or cov.target)]


# --------------------------------------------------------------------------- #
# DELIVERY ROUTING (B-12 timing + freshness) + the B-21 revision-scoped latch key.
# --------------------------------------------------------------------------- #
ROUTE_DELIVER = "deliver"            # on-time, fresh, right decision point -> ship bytes
ROUTE_DEFER = "defer"                # event < available_at -> too early; re-offer later
ROUTE_EXPIRED_LATE = "expired_late"  # event > deliver_by -> too late; explicit non-delivery
ROUTE_STALE = "stale"                # a depended sub-rev changed -> discard/recompute

# The decision-lifecycle order of the coarse observation boundaries. A fact ABOUT an
# action is useful only AT its own boundary (available_at == deliver_by == preferred_event
# — fact_registry: earliest_event == deliver_by for every class), so a wrong decision point
# is strictly BEFORE (defer) or AFTER (expire) that single boundary. ``other`` / unknown
# maps to step0 so a producer firing on a non-core event still routes deterministically.
_ROUTE_EVENT_ORDER: dict[str, int] = {
    EVENT_STEP0: 0, "search": 1, "view": 2, "edit": 3, "test": 4, "submit": 5,
}


def _event_ordinal(name: str) -> int:
    return _ROUTE_EVENT_ORDER.get((name or "").strip().lower(), 0)


# SM-0: the registry's FINE §1 delivery boundary (fact_registry.EVENTS) -> the COARSE
# lifecycle ordinal above. This is how the DECLARED ``deliver_by`` of a class is compared to
# the firing ``event.kind`` under ``GT_REGISTRY_ENFORCE`` — replacing the self-stamped
# ``preferred_event`` (which always equalled the current event, making DEFER/EXPIRED_LATE
# structurally dead). ``failed_search`` is a search; ``failure_obs`` is realized in a test/
# command observation; ``first_view_edit`` is realized by the patch_delta producer on the
# EDIT event (view precedes edit, so binding it to edit is the producer's true realization).
_FINE_EVENT_TO_COARSE_ORDINAL: dict[str, int] = {
    "task_start": 0,        # EVENT_TASK_START  -> step0
    "search_result": 1,     # EVENT_SEARCH_RESULT
    "failed_search": 1,     # EVENT_FAILED_SEARCH (an empty search is still a search)
    "file_view": 2,         # EVENT_FILE_VIEW
    "first_view_edit": 3,   # EVENT_FIRST_VIEW_EDIT (realized on the edit event)
    "edit_result": 3,       # EVENT_EDIT_RESULT
    "test_result": 4,       # EVENT_TEST_RESULT
    "failure_obs": 4,       # EVENT_FAILURE_OBS (a trace/error rides a test/command obs)
    "submit": 5,            # EVENT_SUBMIT
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
        overlay = getattr(state, "episode_overlay", None)
        if overlay:
            tf = _norm_fp(env.target or "")
            entry = overlay.get(tf) if tf else None
            if isinstance(entry, dict) and entry.get("changed"):
                edited_now = {_norm_fp(f) for f in (event.changed_files or ())}
                if tf not in edited_now:
                    return ROUTE_STALE
    cur = _event_ordinal(event.kind)
    want: int | None = None
    if _registry_enforce():
        # An observation-REACTIVE fact (trace_frame) is produced from and delivered at the
        # current observation — on-time by construction, so its boundary is the firing event
        # itself (only freshness above gates it). A fixed-lifecycle fact derives ``want`` from
        # its DECLARED registry ``deliver_by`` (not the self-stamp), so a genuinely wrong-event
        # fact DEFERs/EXPIREs.
        want = cur if _fr_is_reactive(env.evidence_type) else _registry_want_ordinal(env.evidence_type)
    if want is None:
        want = _event_ordinal(env.preferred_event)  # OFF / unregistered -> byte-identical
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
# ENTRY POINT
# --------------------------------------------------------------------------- #
def augment(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Complete one tool observation with deterministic facts. ``[]`` when the master
    flag is off, when the outcome needs nothing, or when every producer abstains."""
    if not _gateway_on():
        return []

    # Record edit events (index + a lowercase blob of command/paths/after-content)
    # for the ZERO_ABSENT ordering predicate — the honest-negative gate mutes only
    # on an edit RELATED to the probed stem (F4).
    if event.kind == KIND_EDIT:
        parts = [event.command or ""]
        parts += list(event.changed_files or ())
        if event.edit_before_after:
            for _f, (_bef, aft) in sorted(event.edit_before_after.items()):
                parts.append(aft or "")
        state.edit_events.append({
            "index": event.action_index,
            "blob": "\n".join(parts).lower(),
        })

    outcome = classify_outcome(event, state)

    additions: list[EvidenceEnvelope] = []
    # kind-dispatched producers (independent of the search-outcome lattice)
    if event.kind == KIND_EDIT:
        if _patch_delta_producer_on():  # kill-switch; see _patch_delta_producer_on docstring
            additions += _produce_patch_delta(event, state)
        # SM-2b: the CROSS-LANGUAGE caller-contract break — the graph-based replacement for
        # the legacy l3.contract/l3b.evidence caller-break patch_delta (ast-only) cannot
        # produce on a non-Python edit. Both are caller-break families; arbitration keeps the
        # per-turn dose at <=1 (signature_mismatch out-ranks caller_break on a Python edit).
        additions += _produce_caller_contract(event, state)
    elif event.kind == KIND_TEST:
        additions += _produce_covering(event, state)
    elif event.kind == KIND_SEARCH and _loc_reslot_on():
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
        # SM-0 registry ENFORCEMENT (GT_REGISTRY_ENFORCE): bind producer -> the DECLARED
        # native renderer. A class with no available renderer has no native FORM to ride the
        # channel the model reads, so it is dropped correct-or-quiet. (The firing-event vs
        # DECLARED-boundary check is enforced by route_delivery below, which under the same
        # flag derives ``want`` from the registry ``deliver_by`` instead of the self-stamp.)
        # OFF ⇒ skipped ⇒ byte-identical. A registered class always has a renderer, so this
        # never rejects a real fact — it guards a future renderer-less / mis-declared class.
        if _registry_enforce() and (
            _fr_registration_for(a.evidence_type) is None
            or not _fr_required_renderer(a.evidence_type)
        ):
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
    return _apply_xsession_rankup(_apply_xsession_policy(out, state), state)

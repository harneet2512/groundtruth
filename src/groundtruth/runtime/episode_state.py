"""EpisodeState — the TARGET canonical per-run runtime state (W1a, strangler wave IN PROGRESS).

GT's runtime state was fragmented across ~10 stores: action count tracked in 6
places, edited-files in 6, the delivered-hash dedup chain in 4, ledgers in 3, the
probe-stem ledger in 2. This module is the single owner those stores collapse into,
migrated one at a time via the STRANGLER pattern — each superseded store proven
byte-identical before it delegates here.

MIGRATION STATUS (Fable-LIPI round-2 seam Finding-2, 2026-07-11 — do NOT trust an
un-migrated field as live). Only TWO stores are DELEGATED so far, aliased to the live
seam owner (``_search_seen is probe_ledger`` and ``_oracle_delivered_hashes is
delivered_dedup`` in gt_mini_patch): ``probe_ledger`` and ``delivered_dedup``. Every
OTHER value field below (``action_index`` / ``edited_files`` / ``edited_tokens`` /
``tested_tokens`` / ``test_count`` / ``nonedit_streak`` / …) is DECLARED for the union
but is NOT YET the live owner — the seam still counts on ``_action_count`` /
``_oracle_edited_rels``, and the EpisodeState copy stays 0/empty (a read-view slot for
W1b/W2, not a source of truth). Wiring a consumer to those BEFORE the writer is migrated
reads a dead 0. "no two live owners" holds today only because the un-migrated copies are
inert, not because they were consolidated.

PURE · DETERMINISTIC · LLM-FREE · stdlib-only (+ ``groundtruth.runtime.ledger`` for
the canonical delivery-ledger handle). No time, no randomness, no network — a
consolidation must be behaviour-neutral, and ``to_dict``/``from_dict`` are an EXACT,
sorted, wallclock-free round trip so two processes serialize byte-identically.

OWNED value state (what this dataclass IS the single owner of):
  * ``action_index`` / ``step_limit`` — the one action counter + step budget.
  * ``phase`` / ``band`` — derived trajectory phase / iteration band (as strings).
  * ``edited_files`` (``path -> last action_index``) + ``edited_tokens`` /
    ``tested_tokens`` (sets) + ``edited_lines_by_file`` — the edit surface.
  * ``test_count`` / ``test_evidence_seen`` / ``nonedit_streak`` — verify signals.
  * ``failure_fingerprints`` (hash set) + ``last_failure_record`` — failure memory.
  * ``delivered_dedup`` — THE delivered-dedup chain (opaque ``dedup_key`` strings);
    the one set the Gateway consulted as ``delivered_keys`` and the mini seam as
    ``_oracle_delivered_hashes``.
  * ``probe_ledger`` (``{stem: {probed_forms:set, probe_indices:list, outcomes:list}}``)
    — the probe-stem ledger, field-identical to the Gateway's ``ledger`` and the
    mini's ``_search_seen``.
  * ``edit_events`` (``[{index, blob}]``) — the ordered edit record for the
    honest-negative repeat gate.
  * ``horizon_latches`` (``str -> bool|int``) — verify-horizon one-shot latches.
  * ``submit_bounce_count`` — the submit-gate bounce counter.
  * ``hypothesis_slots`` — empty placeholder for W4 (hypothesis tracking).

OPAQUE handles (referenced, NOT value-owned; excluded from ``to_dict`` identity):
  * ``delivery_ledger`` — the canonical :class:`~groundtruth.runtime.ledger.Ledger`
    (its durable file sink survives an attempt reset).
  * ``obligations`` — the obligation tracker, owned by its own engine (W2 seam).

RESET SEMANTICS. :meth:`reset_attempt` clears the per-attempt ACCUMULATORS — mirroring
``gt_mini_patch._reset_oracle_state`` for the fields owned here — so a paid-run retry
starts on a fresh slate (attempt-2 can re-deliver a fact attempt-1 already sent). It
PERSISTS the run identity (``episode_id``), the ``step_limit`` budget, and the durable
``delivery_ledger`` handle (parity with the ledger FILE surviving via
``_ledger_line_direct``). Per-turn accumulation is ordinary field mutation.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from groundtruth.runtime.ledger import Ledger

__all__ = ["EpisodeState"]

# D-11: an attempt-suffix `#a<n>` at the very end of an episode id (stripped before a
# fresh suffix is appended, so repeated resets never stack `#a1#a2`).
_ATTEMPT_SUFFIX_RE = re.compile(r"#a\d+$")


def _base_episode_id(episode_id: str) -> str:
    """The run identity WITHOUT any trailing ``#a<n>`` attempt suffix (D-11)."""
    return _ATTEMPT_SUFFIX_RE.sub("", str(episode_id or ""))


@dataclass
class EpisodeState:
    """The single canonical per-run runtime state. JSON-serializable, deterministic."""

    # --- run identity + budget (persist across an attempt reset) ---
    episode_id: str = ""
    attempt: int = 0  # D-11: per-attempt counter; reset_attempt increments + suffixes id
    action_index: int = 0
    step_limit: int | None = None

    # --- derived trajectory position ---
    phase: str = ""
    band: str = ""

    # --- edit surface ---
    edited_files: dict[str, int] = field(default_factory=dict)
    edited_tokens: set[str] = field(default_factory=set)
    tested_tokens: set[str] = field(default_factory=set)
    edited_lines_by_file: dict[str, list[Any]] = field(default_factory=dict)

    # --- verify signals ---
    test_count: int = 0
    test_evidence_seen: bool = False
    nonedit_streak: int = 0

    # --- failure memory ---
    failure_fingerprints: set[str] = field(default_factory=set)
    last_failure_record: dict[str, Any] = field(default_factory=dict)

    # --- THE delivered-dedup chain (one owner) + probe-stem ledger + edit record ---
    delivered_dedup: set[str] = field(default_factory=set)
    probe_ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    edit_events: list[dict[str, Any]] = field(default_factory=list)

    # --- verify-horizon latches + submit-gate bounce + W4 placeholder ---
    horizon_latches: dict[str, Any] = field(default_factory=dict)
    submit_bounce_count: int = 0
    hypothesis_slots: list[Any] = field(default_factory=list)

    # --- opaque handles (referenced, not value-owned; excluded from __eq__/to_dict) ---
    delivery_ledger: Ledger = field(default_factory=Ledger, compare=False, repr=False)
    obligations: Any = field(default=None, compare=False, repr=False)
    # SM-9c (2026-07-11): the CROSS-SESSION learned-delivery policy loaded ONCE at
    # task-start — this repo's durable causal-consumption ledger ``class_stats``
    # (canonical fact-class -> (delivered, consumed)). It is a per-RUN INPUT (a DECLARED,
    # versioned store snapshot), NOT a per-attempt accumulator: like ``episode_id`` /
    # ``delivery_ledger`` it PERSISTS across :meth:`reset_attempt` (attempt-2 still sees
    # the same prior-session learning). ``compare=False`` + absent from ``to_dict`` ⇒ it
    # is EXCLUDED from value identity, so serialization/replay bytes are unchanged (empty
    # by default ⇒ byte-identical to pre-SM-9c). The Gateway READS it via
    # ``gateway._apply_xsession_policy``; nothing here mutates it.
    xsession_policy: dict = field(default_factory=dict, compare=False, repr=False)
    # SM-10 Item D2 (2026-07-12): the per-attempt READ-SET — normalized targets (file rels /
    # symbol stems, PATHS/SYMBOLS ONLY, so leak-safe) the agent has already ACQUIRED through
    # its OWN actions (viewed / greped / edited). The mini seam seeds the global arbiter's
    # ``acquired`` set from it so a localization fact pointing at a file the agent already
    # OPENED/greped is SUPPRESSED (non-re-acquisition — the charter's consumption definition).
    # A per-attempt RUNTIME accumulator, NOT a serialized value: ``compare=False`` + absent
    # from ``to_dict``/``from_dict`` keeps the EXACT round-trip identity unchanged (empty by
    # default ⇒ byte-identical to pre-D2), and :meth:`reset_attempt` CLEARS it (a fresh
    # attempt must re-acquire). Distinct from ``xsession_policy`` (a persisted per-RUN INPUT):
    # this IS a per-attempt accumulator, so unlike xsession_policy it is in the clear list.
    read_targets: set[str] = field(default_factory=set, compare=False, repr=False)

    # ------------------------------------------------------------------ #
    # reset semantics
    # ------------------------------------------------------------------ #
    def reset_attempt(self) -> None:
        """Clear the per-attempt accumulators to a fresh slate; persist run identity,
        the step budget, and the durable ``delivery_ledger`` handle.

        Field coverage mirrors ``gt_mini_patch._reset_oracle_state`` for the state
        this class owns: ``_action_count`` -> :attr:`action_index`,
        ``_oracle_nonedit_streak`` -> :attr:`nonedit_streak`, ``_oracle_test_count`` /
        ``_oracle_test_evidence_seen`` -> :attr:`test_count` / :attr:`test_evidence_seen`,
        ``_oracle_edited_rels`` -> :attr:`edited_files`, ``_oracle_edited_tokens`` /
        ``_oracle_tested_tokens`` -> :attr:`edited_tokens` / :attr:`tested_tokens`,
        ``_oracle_edited_lines_by_file`` -> :attr:`edited_lines_by_file`,
        ``_oracle_delivered_hashes`` -> :attr:`delivered_dedup`, ``_search_seen`` ->
        :attr:`probe_ledger`, ``_gt_submit_bounce_count`` -> :attr:`submit_bounce_count`,
        and the ``_horizon_*_fired`` one-shots -> :attr:`horizon_latches`. The
        ``delivery_ledger`` handle is NOT cleared (its file sink is durable, matching
        the ledger FILE surviving an attempt in ``_ledger_line_direct``).

        SM-9c: :attr:`xsession_policy` is likewise PRESERVED (it is a per-RUN store INPUT,
        not a per-attempt accumulator — attempt-2 must still see the same prior-session
        learning). It is intentionally NOT in the clear list below; a future edit must not
        add it there."""
        self.action_index = 0
        self.phase = ""
        self.band = ""
        self.edited_files.clear()
        self.edited_tokens.clear()
        self.tested_tokens.clear()
        self.edited_lines_by_file.clear()
        self.test_count = 0
        self.test_evidence_seen = False
        self.nonedit_streak = 0
        self.failure_fingerprints.clear()
        self.last_failure_record = {}
        self.delivered_dedup.clear()
        self.probe_ledger.clear()
        self.edit_events.clear()
        self.horizon_latches.clear()
        self.submit_bounce_count = 0
        self.hypothesis_slots.clear()
        self.read_targets.clear()  # SM-10 Item D2: per-attempt read-set re-acquires fresh
        # D-11 (RL data identity): advance the attempt counter and suffix the run id so
        # two attempts of the SAME run get DISTINCT episode identities in the training
        # data (the hash-chain / event ids reset per attempt and would otherwise be
        # indistinguishable). The BASE identity is preserved (any prior `#a<n>` suffix is
        # stripped first), so run provenance survives — only the attempt index is added.
        self.attempt += 1
        if self.episode_id:
            self.episode_id = _base_episode_id(self.episode_id) + f"#a{self.attempt}"
        # PERSIST: episode_id (base + attempt suffix), step_limit, delivery_ledger, obligations.

    # ------------------------------------------------------------------ #
    # per-turn accumulation helpers (normalizing ingress)
    # ------------------------------------------------------------------ #
    def note_edited_lines(self, file: str, ranges: Iterable[Iterable[int]]) -> None:
        """Record touched line ranges for ``file`` — the SUPPORTED ingress for
        :attr:`edited_lines_by_file` (F1, micro-bounce 2026-07-10). W2 sources emit
        TUPLE rows (``gt_mini_patch._edited_line_ranges``); rows are normalized to
        LISTS here, at entry, so the exact round-trip contract
        ``from_dict(to_dict(e)) == e`` stays TRUE (JSON has no tuple — a tuple row
        assigned directly would deserialize as a list and break object equality).
        Appends to any existing rows for ``file``; never replaces."""
        rows = self.edited_lines_by_file.setdefault(str(file), [])
        for r in ranges:
            rows.append(list(r))

    # ------------------------------------------------------------------ #
    # deterministic serialization — exact, sorted, wallclock-free
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict of the OWNED value state, deterministic by
        construction: sets serialize to SORTED lists, dict keys are emitted sorted,
        ordered accumulators (lists) preserve order, and NO wallclock/timestamp value
        is ever included. The opaque handles (``delivery_ledger``, ``obligations``)
        are NOT part of value identity and are excluded — :meth:`from_dict` restores a
        fresh handle. ``to_dict(from_dict(to_dict(e))) == to_dict(e)`` exactly."""
        return {
            "episode_id": self.episode_id,
            "attempt": self.attempt,
            "action_index": self.action_index,
            "step_limit": self.step_limit,
            "phase": self.phase,
            "band": self.band,
            "edited_files": {k: self.edited_files[k] for k in sorted(self.edited_files)},
            "edited_tokens": sorted(self.edited_tokens),
            "tested_tokens": sorted(self.tested_tokens),
            "edited_lines_by_file": {
                k: [list(r) for r in self.edited_lines_by_file[k]]
                for k in sorted(self.edited_lines_by_file)
            },
            "test_count": self.test_count,
            "test_evidence_seen": self.test_evidence_seen,
            "nonedit_streak": self.nonedit_streak,
            "failure_fingerprints": sorted(self.failure_fingerprints),
            "last_failure_record": dict(self.last_failure_record),
            "delivered_dedup": sorted(self.delivered_dedup),
            "probe_ledger": {
                stem: {
                    "probed_forms": sorted(entry.get("probed_forms", ())),
                    "probe_indices": list(entry.get("probe_indices", ())),
                    "outcomes": list(entry.get("outcomes", ())),
                }
                for stem, entry in sorted(self.probe_ledger.items())
            },
            "edit_events": [dict(ev) for ev in self.edit_events],
            "horizon_latches": {k: self.horizon_latches[k]
                                for k in sorted(self.horizon_latches)},
            "submit_bounce_count": self.submit_bounce_count,
            # per-slot copy, mirroring edit_events (W4/F4 2026-07-10): slots are
            # envelope DICTS; sharing them let a later mark_* rewrite an exported
            # snapshot / an in-process round-tripped copy alias the original.
            "hypothesis_slots": [dict(s) for s in self.hypothesis_slots],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EpisodeState:
        """Inverse of :meth:`to_dict`. ``from_dict(to_dict(e)) == e`` on value fields
        (the opaque handles are excluded from equality and rebuilt fresh), PROVIDED
        list-shaped rows entered through their supported ingress — ``from_dict``
        itself or :meth:`note_edited_lines`, both of which normalize rows to lists
        (F1: JSON has no tuple, so a TUPLE row assigned directly to
        :attr:`edited_lines_by_file` round-trips only at the ``to_dict`` level:
        ``from_dict(to_dict(e)).to_dict() == to_dict(e)`` always holds). Sets are
        restored from their sorted lists; ``probe_ledger[*]['probed_forms']`` is
        restored as a SET (the Gateway calls ``.add()`` on it). Input is ``Any``-typed
        because JSON is untyped — coercion is the boundary's job."""
        e = cls(
            episode_id=str(d.get("episode_id", "")),
            attempt=int(d.get("attempt", 0)),
            action_index=int(d.get("action_index", 0)),
            step_limit=(None if d.get("step_limit") is None else int(d["step_limit"])),
            phase=str(d.get("phase", "")),
            band=str(d.get("band", "")),
        )
        e.edited_files = {str(k): int(v) for k, v in (d.get("edited_files") or {}).items()}
        e.edited_tokens = set(d.get("edited_tokens") or ())
        e.tested_tokens = set(d.get("tested_tokens") or ())
        e.edited_lines_by_file = {
            str(k): [list(r) for r in v]
            for k, v in (d.get("edited_lines_by_file") or {}).items()
        }
        e.test_count = int(d.get("test_count", 0))
        e.test_evidence_seen = bool(d.get("test_evidence_seen", False))
        e.nonedit_streak = int(d.get("nonedit_streak", 0))
        e.failure_fingerprints = set(d.get("failure_fingerprints") or ())
        e.last_failure_record = dict(d.get("last_failure_record") or {})
        e.delivered_dedup = set(d.get("delivered_dedup") or ())
        e.probe_ledger = {
            str(stem): {
                "probed_forms": set(entry.get("probed_forms", ())),
                "probe_indices": list(entry.get("probe_indices", ())),
                "outcomes": list(entry.get("outcomes", ())),
            }
            for stem, entry in (d.get("probe_ledger") or {}).items()
        }
        e.edit_events = [dict(ev) for ev in (d.get("edit_events") or ())]
        e.horizon_latches = dict(d.get("horizon_latches") or {})
        e.submit_bounce_count = int(d.get("submit_bounce_count", 0))
        e.hypothesis_slots = [dict(s) for s in (d.get("hypothesis_slots") or ())]
        return e

    # ------------------------------------------------------------------ #
    # read-view adapters — read-only projections of the legacy stores. They NEVER
    # mutate the source; they make EpisodeState the canonical READ model that can
    # represent every fragmented store (the first byte-safe strangler step for the
    # stores whose field-ownership migration lands in a later wave).
    # ------------------------------------------------------------------ #
    @classmethod
    def from_l5(cls, l5: Any, *, episode_id: str = "") -> EpisodeState:
        """Read-view projection of the LEGACY-FROZEN ``L5TrajectoryState``
        (``state/agent_state.py``). Duck-typed (reads attributes only) so this module
        never imports the frozen legacy module; read-only (does not mutate ``l5``)."""
        es = cls(episode_id=episode_id or str(getattr(l5, "instance_id", "") or ""))
        es.action_index = int(getattr(l5, "current_iter", 0) or 0)
        max_iter = int(getattr(l5, "max_iter", 0) or 0)
        es.step_limit = max_iter or None
        es.band = _enum_value(getattr(l5, "band", None))
        es.phase = _enum_value(getattr(l5, "phase", None))
        last_edit = int(getattr(l5, "last_edit_iter", 0) or 0)
        for f in getattr(l5, "edited_source_files", None) or ():
            es.edited_files[str(f)] = last_edit
        es.test_count = int(getattr(l5, "verification_commands_run", 0) or 0)
        es.test_evidence_seen = es.test_count > 0
        for h in getattr(l5, "unresolved_failure_hashes", None) or ():
            es.failure_fingerprints.add(str(h))
        recs = getattr(l5, "failure_records", None) or ()
        if recs:
            es.last_failure_record = dict(recs[-1])
        es.submit_bounce_count = 0
        return es

    @classmethod
    def from_trajectory_state(cls, ts: Any) -> EpisodeState:
        """Read-view projection of ``runtime.trajectory_state.TrajectoryState``.
        Read-only; the set-typed ``edited_files`` projects to the canonical dict with
        the current ``action_count`` as the (best-available) last-touch index."""
        es = cls()
        es.action_index = int(getattr(ts, "action_count", 0) or 0)
        es.step_limit = getattr(ts, "step_limit", None)
        for f in getattr(ts, "edited_files", None) or ():
            es.edited_files[str(f)] = es.action_index
        es.edited_tokens = set(getattr(ts, "edited_tokens", None) or ())
        es.tested_tokens = set(getattr(ts, "tested_tokens", None) or ())
        es.test_count = int(getattr(ts, "test_count", 0) or 0)
        es.test_evidence_seen = bool(getattr(ts, "test_evidence_seen", False))
        es.nonedit_streak = int(getattr(ts, "nonedit_streak", 0) or 0)
        return es

    @classmethod
    def from_state(cls, ats: Any) -> EpisodeState:
        """Read-view projection of ``runtime.state.AgentTrajectoryState``. Read-only;
        the list-typed edit/test histories project to the canonical scalar/dict
        representation (``len`` -> counts, files -> the dict keys)."""
        es = cls()
        es.action_index = int(getattr(ats, "action_count", 0) or 0)
        max_iter = int(getattr(ats, "max_iter", 0) or 0)
        es.step_limit = max_iter or None
        es.band = str(getattr(ats, "band", "") or "")
        for f in getattr(ats, "edited_files", None) or ():
            es.edited_files[str(f)] = es.action_index
        test_iters = getattr(ats, "test_iterations", None) or ()
        es.test_count = len(test_iters)
        es.test_evidence_seen = es.test_count > 0
        return es


def _enum_value(v: Any) -> str:
    """The ``.value`` of an Enum-ish, else its str; ``None`` -> ``""``."""
    if v is None:
        return ""
    return str(getattr(v, "value", v))

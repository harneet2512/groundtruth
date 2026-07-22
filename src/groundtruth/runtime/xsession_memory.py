"""xsession_memory — the cross-session CAUSAL-CONSUMPTION ledger (SM-9c).

A durable, per-repo, VERSIONED store that records — per canonical §1 fact CLASS — how
many times GT DELIVERED that class on THIS repo across prior sessions and how many of
those deliveries the agent CONSUMED (the delivery's ``receipt_state`` advanced past
``delivered`` on a later turn). GT is the ONLY acquisition layer that measures
consumption (the TITO receipt ladder in :mod:`groundtruth.runtime.evidence_envelope`), so
cross-session memory here is a **learned delivery policy**, NOT a bigger fact cache: at
task-start the store is loaded as an EXPLICIT, DECLARED input; the Gateway ranks UP a
fact-class historically CONSUMED on this repo and SUPPRESSES a class historically INERT
here (delivered ``>= MIN_SAMPLES`` and never once consumed) — correct-or-quiet, learned
from the repo's own causal history.

CARDINAL DETERMINISM (this lane's whole risk). The store is a DECLARED, VERSIONED input,
never hidden global state. Given ``(repo, issue, STORE-STATE)`` the delivery is a
deterministic function of the store: the SAME store -> byte-identical output; a DIFFERENT
store -> a deterministic change. Serialization is sorted, integer-valued, and
wallclock-free (NO timestamp / episode id / hash enters the value identity), so two
processes that delivered the SAME facts write byte-identical store files, and re-reading a
store reproduces the same policy. This preserves SM-7's exact-byte replay.

LEAK LAW (leak=0 by construction). The store keys ONLY on a canonical §1 fact-class name
(one of the 11 in :data:`groundtruth.runtime.fact_registry.REGISTRY`) plus two integer
counts — NEVER a test identity, a symbol body, a path, an assertion, or issue text.
:func:`sanitize_class` accepts a key ONLY when it is a registered §1 class, so a poisoned
key (``tests/test_x.py::test_leak``) can never enter the store nor influence delivery;
:func:`load` sanitizes on the way in and :func:`merge` on the way out. Because policy is
adapted on the CLASS NAME (not by re-delivering a stored payload), there is no cached fact
body to leak — the re-surface behaviour has ZERO payload leak surface.

PURE · DETERMINISTIC · LLM-FREE · stdlib + fact_registry only. No time, no randomness in
the value identity; the only I/O is the explicit :func:`load` / :func:`dump` of one small
per-repo JSON file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from groundtruth.runtime.fact_registry import is_registered, registration_for

__all__ = [
    "SCHEMA_VERSION",
    "MIN_SAMPLES",
    "INERT_CONSUMED_MAX",
    "CONSUMED_STATES",
    "XSessionStore",
    "sanitize_class",
    "canonical_class",
    "repo_key",
    "store_path",
    "store_path_for_key",
    "load",
    "dump",
    "merge",
    "consumption_rate",
    "is_inert",
    "is_consumed",
    "policy_rank",
    "ladder_boost",
]

# The versioned store schema. A file whose ``schema_version`` differs is treated as a
# fresh (empty) store — fail-SAFE: an unrecognized version never mis-adapts delivery.
SCHEMA_VERSION = "gt_xsession.v1"

# LEARNED-POLICY FLOORS (conservative; correct-or-quiet). These are NOT tuned to any task —
# they are the minimum evidence before the learned policy acts on a class:
#   * MIN_SAMPLES — a class must have been DELIVERED at least this many times on this repo
#     before its consumption record is trusted (below it: no suppress, no rank change).
#   * INERT_CONSUMED_MAX — a class is INERT only when it was delivered >= MIN_SAMPLES times
#     and consumed NO MORE than this (0 = never once consumed across all prior deliveries).
# OWED (measurement-gated): the floors + a per-symbol axis are first-slice constants; a
# larger sample should re-derive them from the measured consumption distribution.
MIN_SAMPLES = 3
INERT_CONSUMED_MAX = 0

# The receipt states that count as CONSUMED (the delivery's receipt advanced past level-1
# ``delivered``). Mirror of the ladder in ``evidence_envelope`` (RECEIPT_REFERENCED /
# _ACTED / _RESOLVED_STATE / _CAUSAL — the source of truth for the vocabulary). A delivery
# that stayed at ``delivered`` / ``none`` is INERT.
CONSUMED_STATES: frozenset[str] = frozenset(
    {"referenced", "acted", "resolved_state", "causal"}
)


# --------------------------------------------------------------------------- #
# leak-safe class vocabulary — the ONLY keys the store may ever contain.
# --------------------------------------------------------------------------- #
def sanitize_class(name: str) -> str | None:
    """The canonical §1 fact-class ``name`` iff it is a REGISTERED class, else ``None``.

    THE LEAK GATE: the store may key ONLY on a registered §1 class name (``caller_contract``,
    ``def_partition``, …). A registered name carries no path, no ``::`` nodeid, no
    ``test_`` echo — so accepting only registered names makes the store leak=0 BY
    CONSTRUCTION. Any other string (a test identity, a symbol, a path, issue text) is
    dropped here, so it can never enter the store nor influence a delivery decision.
    Removing the :func:`is_registered` guard is the mutation the leak-probe test catches."""
    n = (name or "").strip()
    if not n:
        return None
    return n if is_registered(n) else None


def canonical_class(evidence_type: str) -> str | None:
    """The canonical §1 class a SHIPPED ``evidence_type`` resolves to (via
    ``fact_registry.registration_for`` — direct, alias, or ``base:suffix`` form), or
    ``None`` when it resolves to no registered class. E.g. ``caller_break`` ->
    ``caller_contract``, ``covering_verdict`` -> ``covering_red``. The bridge from a
    Gateway/receipt ``evidence_type`` to the store's class key."""
    reg = registration_for(evidence_type or "")
    return reg.fact_class if reg is not None else None


# --------------------------------------------------------------------------- #
# repo identity -> a stable, opaque store key + path.
# --------------------------------------------------------------------------- #
def repo_key(repo_identity: Mapping[str, str]) -> str:
    """A stable 16-hex key for a repo from its identity dict (the shape
    ``project_memory._repo_identity`` returns: ``{root_name, remote, name}``).

    Prefers the git ``remote`` (globally unique across clones/checkouts), then ``name``,
    then ``root_name`` — the first non-empty. Deterministic (sha256 of the chosen token);
    opaque, so the on-disk filename never carries a raw path/URL. Empty identity -> ``""``
    (the caller then treats the store as disabled — never keys on nothing)."""
    ident = repo_identity or {}
    token = (
        (ident.get("remote") or "").strip()
        or (ident.get("name") or "").strip()
        or (ident.get("root_name") or "").strip()
    )
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def store_path_for_key(key: str, base_dir: str) -> str:
    """The per-repo store file path ``<base_dir>/xsession_<key>.json`` for a repo key, or
    ``""`` when either is empty (the caller no-ops — nothing to load/persist)."""
    k = (key or "").strip()
    b = (base_dir or "").strip()
    if not k or not b:
        return ""
    return os.path.join(b, f"xsession_{k}.json")


def store_path(repo_identity: Mapping[str, str], base_dir: str) -> str:
    """The per-repo store path for a repo identity dict + a durable base directory
    (``$GT_XSESSION_DIR``). ``""`` when the repo key or base dir is empty."""
    return store_path_for_key(repo_key(repo_identity), base_dir)


# --------------------------------------------------------------------------- #
# THE STORE — a frozen, versioned, leak-safe value.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class XSessionStore:
    """One immutable per-repo causal-consumption record.

    ``class_stats`` maps a canonical §1 fact-class -> ``(delivered_count, consumed_count)``.
    Frozen so a loaded store is an inert INPUT the delivery path only READS (never a
    mutable global). Value identity is exactly ``(schema_version, repo_key, class_stats)``
    — no timestamp, no session id — so two stores built from the same delivery history are
    equal and serialize byte-identically."""

    schema_version: str = SCHEMA_VERSION
    repo_key: str = ""
    class_stats: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    def stat(self, canonical: str | None) -> tuple[int, int]:
        """``(delivered, consumed)`` for a canonical class, or ``(0, 0)`` when absent/None."""
        if not canonical:
            return (0, 0)
        pair = self.class_stats.get(canonical)
        if not pair:
            return (0, 0)
        return (int(pair[0]), int(pair[1]))


def _coerce_pair(value: Any) -> tuple[int, int] | None:
    """A validated ``(delivered, consumed)`` from an untyped JSON value: two non-negative
    ints with ``consumed <= delivered`` (the monotone sanity a real receipt ladder obeys),
    else ``None`` (drop the row). Total — never raises on malformed input."""
    try:
        d = int(value[0])
        c = int(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if d < 0 or c < 0:
        return None
    if c > d:
        c = d  # a consumed count can never exceed the deliveries it was measured over
    return (d, c)


def _sanitize_stats(raw: Any) -> dict[str, tuple[int, int]]:
    """Every ``{class: [delivered, consumed]}`` row whose key is a REGISTERED class and
    whose value coerces to a valid pair. Poisoned keys (test identity / path) and malformed
    rows are DROPPED — the store's leak=0 + integrity invariant, applied on load and merge.
    Deterministic: keys are inserted in sorted order."""
    out: dict[str, tuple[int, int]] = {}
    if not isinstance(raw, dict):
        return out
    for key in sorted(raw):
        canon = sanitize_class(key)
        if canon is None:
            continue
        pair = _coerce_pair(raw[key])
        if pair is None:
            continue
        # If two raw keys sanitize to the SAME canonical class, sum them (defensive; the
        # writer never emits duplicates, but a hand-edited file might).
        d0, c0 = out.get(canon, (0, 0))
        out[canon] = (d0 + pair[0], c0 + pair[1])
    return out


def load(path: str, *, repo_key: str = "") -> XSessionStore:
    """Read the per-repo store at ``path`` (missing / unreadable / wrong-schema / corrupt
    -> a FRESH empty store, fail-SAFE). Keys are leak-sanitized and values integer-coerced
    on the way in (:func:`_sanitize_stats`), so a loaded store can only ever contain
    registered class names + valid counts. ``repo_key`` overrides an empty/missing file
    ``repo_key`` (the caller knows which repo it loaded for)."""
    key = (repo_key or "").strip()
    if not path or not os.path.isfile(path):
        return XSessionStore(repo_key=key)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return XSessionStore(repo_key=key)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return XSessionStore(repo_key=key)  # unknown schema => fresh (never mis-adapt)
    file_key = str(data.get("repo_key") or "").strip()
    return XSessionStore(
        schema_version=SCHEMA_VERSION,
        repo_key=file_key or key,
        class_stats=_sanitize_stats(data.get("class_stats")),
    )


def _to_json_obj(store: XSessionStore) -> dict[str, Any]:
    """The canonical JSON object for ``store`` — sorted class keys, ``[d, c]`` list values,
    NO wallclock/session field. Deterministic by construction."""
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_key": store.repo_key,
        "class_stats": {
            cls: [int(store.class_stats[cls][0]), int(store.class_stats[cls][1])]
            for cls in sorted(store.class_stats)
        },
    }


@contextmanager
def _store_lock(path: str):
    """Serialize cross-process writers without leaving a partially-written JSON file."""
    lock_path = f"{path}.lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            def unlock() -> None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            def unlock() -> None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()
    finally:
        handle.close()


def _merge_snapshot_max(current: XSessionStore, incoming: XSessionStore) -> XSessionStore:
    """Union monotone snapshots while preserving already-published peer updates."""
    stats: dict[str, tuple[int, int]] = dict(current.class_stats)
    for cls, pair in _sanitize_stats(dict(incoming.class_stats)).items():
        old = stats.get(cls, (0, 0))
        stats[cls] = (max(old[0], pair[0]), max(old[1], pair[1]))
    return replace(current, schema_version=SCHEMA_VERSION,
                   repo_key=current.repo_key or incoming.repo_key, class_stats=stats)


def dump(store: XSessionStore, path: str) -> bool:
    """Write ``store`` to ``path`` deterministically (``sort_keys=True``, sorted class
    rows, integer counts, no timestamp) so the same delivery history yields a byte-identical
    file. Creates the parent dir. Correct-or-quiet: ``False`` on any I/O fault (persistence
    must never break a run); ``""`` path -> ``False`` (no durable dir, nothing to persist)."""
    if not path:
        return False
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _store_lock(path):
            # Re-read under the lock: another session may have published a distinct
            # class since this writer loaded its task-start snapshot.  Monotone max
            # preserves both snapshots and remains idempotent for repeated flushes.
            current = load(path, repo_key=store.repo_key)
            merged = _merge_snapshot_max(current, store)
            text = json.dumps(_to_json_obj(merged), sort_keys=True, separators=(",", ":"))
            fd, temporary = tempfile.mkstemp(prefix=".xsession-", dir=parent or None)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return True
    except (OSError, TypeError, ValueError):
        return False


def merge(
    base: XSessionStore, session_stats: Mapping[str, tuple[int, int]]
) -> XSessionStore:
    """Fold THIS session's per-class ``(delivered, consumed)`` into ``base`` (the frozen
    task-start snapshot) and return a NEW store. PURE — ``base`` is untouched; ``session_stats``
    is leak-sanitized (only registered classes survive), so a poisoned session row can never
    reach the file. Idempotent given a fixed ``base`` + ``session_stats`` (re-merging the same
    inputs yields the same store), which is what lets the seam flush repeatedly from the frozen
    base without double-counting."""
    merged: dict[str, tuple[int, int]] = dict(base.class_stats)
    for cls, pair in _sanitize_stats(dict(session_stats or {})).items():
        d0, c0 = merged.get(cls, (0, 0))
        merged[cls] = (d0 + pair[0], c0 + pair[1])
    return replace(base, schema_version=SCHEMA_VERSION, class_stats=merged)


# --------------------------------------------------------------------------- #
# THE LEARNED POLICY — pure functions of the store, consumed by the Gateway.
# --------------------------------------------------------------------------- #
def _pair(policy: Mapping[str, Any] | None, canonical: str | None) -> tuple[int, int]:
    """``(delivered, consumed)`` for a class from a policy map (the store's ``class_stats``,
    or the loaded dict the seam threads onto ``EpisodeState.xsession_policy``). Tolerant of
    tuple OR list pair values (JSON round-trips tuples to lists)."""
    if not policy or not canonical:
        return (0, 0)
    pair = policy.get(canonical)
    if not pair:
        return (0, 0)
    try:
        return (int(pair[0]), int(pair[1]))
    except (TypeError, ValueError, IndexError):
        return (0, 0)


def consumption_rate(policy: Mapping[str, Any] | None, canonical: str | None) -> float | None:
    """``consumed / delivered`` for a class, or ``None`` when there is INSUFFICIENT evidence
    (delivered < :data:`MIN_SAMPLES`) — below the floor the policy has no opinion."""
    d, c = _pair(policy, canonical)
    if d < MIN_SAMPLES:
        return None
    return c / d if d else 0.0


def is_inert(policy: Mapping[str, Any] | None, canonical: str | None) -> bool:
    """True iff this class was delivered ``>= MIN_SAMPLES`` times on this repo and consumed
    ``<= INERT_CONSUMED_MAX`` (0) — i.e. NEVER once acted on despite ample deliveries. The
    learned SUPPRESS signal (correct-or-quiet: only fires on strong, sampled evidence)."""
    d, c = _pair(policy, canonical)
    return d >= MIN_SAMPLES and c <= INERT_CONSUMED_MAX


def is_consumed(policy: Mapping[str, Any] | None, canonical: str | None) -> bool:
    """True iff this class was delivered ``>= MIN_SAMPLES`` times AND consumed at least once
    — the learned RANK-UP signal (this repo's agents historically acted on the class)."""
    d, c = _pair(policy, canonical)
    return d >= MIN_SAMPLES and c > INERT_CONSUMED_MAX


def policy_rank(policy: Mapping[str, Any] | None, canonical: str | None) -> int:
    """A small deterministic rank boost for a historically-CONSUMED class (``1``) vs
    everything else (``0``). Used to stable-reorder the Gateway's candidate list so a
    consumed class sorts ahead of a same-tier non-consumed one (observable ranking; the
    per-turn winner is chosen by the adapter's order-independent arbiter)."""
    return 1 if is_consumed(policy, canonical) else 0


# ARBITRATION RANK-UP -> WINNER (SM-9c, 2026-07-12). ``policy_rank`` (0/1) only stable-SORTS
# the Gateway's candidate list, which the adapter's order-independent arbiter IGNORES — so a
# consumed class could never WIN the <=1-dose arbitration from the sort alone. ``ladder_boost``
# is the winner-promotion magnitude the adapter ADDS to a class's severity rank, DERIVED FROM
# THE LADDER (the consumed count), NOT a single hardcoded constant: a class this repo acted on
# MORE earns a larger boost.
#
# MEASUREMENT-GATED (first slice). The tier BOUNDARIES (1/3/6 prior consumptions) and their
# point VALUES (3/7/11) are a first-cut starting scheme — re-derive them from the measured
# consumed-count distribution once sampled (OWED, like MIN_SAMPLES/INERT_CONSUMED_MAX above).
# Two invariants the values are chosen to hold, and MUST keep holding when re-tuned:
#   * MONOTONE — more prior consumptions => a boost that is >= a smaller-count boost.
#   * CAPPED BELOW THE EXECUTED-RED GAP — the max boost (11) is strictly < the adapter's
#     severity gap from ``caller_break`` (48) to ``covering_verdict`` (60) == 12, so a learned
#     rank-up nudges a fact WITHIN the severity space but NEVER out-doses the repo's OWN
#     executed world-fact (the covering RED). A future measurement may raise the cap, but the
#     ceiling is a deliberate doctrine choice, not an accident.
# Checked HIGH-count first so the largest matching tier wins.
_BOOST_TIERS: tuple[tuple[int, int], ...] = ((6, 11), (3, 7), (1, 3))


def ladder_boost(policy: Mapping[str, Any] | None, canonical: str | None) -> int:
    """A tiered, ladder-DERIVED arbitration boost (``>= 0``) for a historically-CONSUMED class,
    or ``0`` when the class is under-sampled or inert (``is_consumed`` is False). This is the
    magnitude the adapter's ``_priority`` adds to the class's severity rank so a memory-consumed,
    ALREADY-ELIGIBLE fact can WIN the per-turn ``<= 1`` dose — the winner-promotion the SM-9c
    candidate SORT alone cannot deliver.

    Monotone in the consumed count (a class acted on MORE is boosted MORE), read from the SAME
    store snapshot the suppress / rank signals read, so the boost is a deterministic function of
    the policy. Because it gates on ``is_consumed`` (delivered ``>= MIN_SAMPLES`` AND consumed
    ``> INERT_CONSUMED_MAX``), an under-sampled or never-consumed class earns exactly ``0`` — a
    rank-up NEVER fires on weak evidence, and (being ``>= 0``) it never DEMOTES a class. See
    :data:`_BOOST_TIERS` for the MEASUREMENT-GATED tiers + the executed-RED cap rationale."""
    if not is_consumed(policy, canonical):
        return 0
    _d, c = _pair(policy, canonical)
    for min_consumed, boost in _BOOST_TIERS:
        if c >= min_consumed:
            return boost
    return 0  # unreachable: is_consumed guarantees c >= 1 == the lowest tier boundary

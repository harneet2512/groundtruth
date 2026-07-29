"""RED — the receipt-ladder rank order has NO single authority.

The monotone receipt ladder (``none -> delivered -> referenced -> acted ->
resolved_state -> causal``) is hand-duplicated as a frozen dict literal in at
least two independent readers:

  * ``src/groundtruth/runtime/adapters/miniswe.py:1354``  ``_RECEIPT_ORDER``
  * ``scripts/swebench/receipt_sidecar.py:43``            ``_TRANSITION_RANK``

(a third, ``scripts/gt_substitution_grader.py:94``, keeps its own tuple form —
out of scope for this test but the same defect class).

The ``RECEIPT_*`` string constants already live in ONE place
(``evidence_envelope.py:195-200``) and ``receipt_sidecar`` already imports from
that module, so nothing is missing but a shared *ordered* authority.

WHY THIS TEST IS A MUTATION CHECK, NOT AN EQUALITY CHECK
--------------------------------------------------------
The two literals AGREE today.  ``assert _RECEIPT_ORDER == {...}`` would be GREEN
before AND after the fix and would prove exactly nothing ("green signal can be
empty").  The only honest RED is to MUTATE the shared source of truth and prove
that every reader follows it:

  1. patch ``evidence_envelope.RECEIPT_LADDER`` / ``RECEIPT_RANK`` to carry one
     synthetic extra rung,
  2. load a FRESH copy of each reader module (see ``_fresh_module`` — a fresh
     module object under a throwaway name, NOT ``importlib.reload`` of the live
     entry; reload would rebind ``sys.modules`` and break class identity for
     every other test in the session),
  3. assert BOTH readers now carry the synthetic rung.

Today both readers are frozen literals that ignore the shared source, so step 3
fails.  After the planned fix (``_RECEIPT_ORDER = RECEIPT_RANK`` and
``_TRANSITION_RANK = {s: r for s, r in RECEIPT_RANK.items() if r > 0}``) it
passes.

No source file is modified by this test.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from types import ModuleType

import pytest

from groundtruth.runtime import evidence_envelope as ee

# EAGER, UNPATCHED live imports — load-bearing, do not make these lazy.
#
# ISOLATION BUG FIXED 2026-07-28 (found by this file's own control test, once
# `_RECEIPT_ORDER` actually started following the authority): these two modules
# used to be imported lazily, from inside `_fresh_module`, i.e. INSIDE the
# patched window.  Neither is in `sys.modules` when this test module is
# imported, so the first mutation probe performed each reader's FIRST LIVE
# import while `evidence_envelope.RECEIPT_RANK` was patched.  `miniswe` then
# bound `_RECEIPT_ORDER = RECEIPT_RANK` to the PATCHED proxy object
# (`live._RECEIPT_ORDER is patched_proxy` -> True), and `monkeypatch` teardown
# only rebinds the NAME in `evidence_envelope` — the live module never re-reads
# it, so the synthetic rung leaked permanently into the live reader.
#
# Ruled out by experiment, for the record: the original proxy's underlying dict
# is NOT mutated ("SYNTH" in orig_rank -> False), nothing re-reads the name
# after teardown, and no `sys.modules` residue is involved (the probe entry is
# popped in a `finally`).  The single cause was a first import under a patch.
#
# Binding them here, at module import time, guarantees both live modules exist
# and are correctly bound BEFORE any fixture can patch anything.
from groundtruth.runtime.adapters import miniswe as _live_miniswe  # noqa: E402
from scripts.swebench import receipt_sidecar as _live_sidecar  # noqa: E402

assert _live_miniswe.__file__ and _live_sidecar.__file__

# The rung that must not exist anywhere except the patched authority.  If a
# reader still shows the real 6-rung literal after the patch, it is not reading
# the authority.
SYNTHETIC_RUNG = "gt_synthetic_rung_20260728"


# --------------------------------------------------------------------------- #
# fresh-load helper
# --------------------------------------------------------------------------- #
def _fresh_module(live: ModuleType) -> ModuleType:
    """Execute ``live``'s source file a second time under a throwaway module
    name and return the new module object.

    Takes the ALREADY-IMPORTED module, never a dotted name: resolving a name to
    a module here would import it if absent, and this helper only ever runs
    inside a patched window — see the isolation note at the top of this file.

    Preferred over ``importlib.reload`` because reload REBINDS the live
    ``sys.modules`` entry: every dataclass/exception/function object other
    already-imported modules hold would stop being identical to the reloaded
    one, silently breaking unrelated tests in the same session.  A fresh object
    under a throwaway name has no such blast radius — it is discarded here.

    Both target modules are pure at import time (stdlib + intra-repo imports,
    dataclass/regex definitions only; no I/O, no monkeypatching, no global
    registration), which is what makes a second execution safe.  The fresh
    module's own ``from groundtruth.runtime.evidence_envelope import ...``
    resolves against the LIVE (monkeypatched) ``evidence_envelope`` in
    ``sys.modules`` — that is the whole point of the mutation probe.
    """
    path = live.__file__
    assert path, f"{live.__name__} has no __file__"
    probe_name = f"_gt_probe_{live.__name__.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(probe_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[probe_name] = module  # some import machinery needs the entry
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(probe_name, None)
    return module


@pytest.fixture()
def live_readers() -> tuple[ModuleType, ModuleType]:
    """The real, unpatched reader modules — bound at module import time, so this
    fixture can never trigger a first import inside a patched window."""
    return (_live_miniswe, _live_sidecar)


@pytest.fixture(autouse=True)
def _no_ladder_leak() -> object:
    """Fail ANY test in this file that leaks a patched ladder into a live module.

    A DETECTOR, not a repair: it snapshots the live readers' rank objects before
    the test and asserts they are byte-identical afterwards.  Restoring them
    instead would silently paper over exactly the leak this file exists to catch,
    and would let the two mutation probes go on passing while corrupting the rest
    of the session.  Deliberately does NOT reorder or skip anything.
    """
    before = (
        dict(_live_miniswe._RECEIPT_ORDER),
        dict(_live_sidecar._TRANSITION_RANK),
    )
    before_ids = (id(_live_miniswe._RECEIPT_ORDER), id(_live_sidecar._TRANSITION_RANK))
    yield
    after = (
        dict(_live_miniswe._RECEIPT_ORDER),
        dict(_live_sidecar._TRANSITION_RANK),
    )
    after_ids = (id(_live_miniswe._RECEIPT_ORDER), id(_live_sidecar._TRANSITION_RANK))
    assert after == before, (
        "a probe in this file LEAKED into a live reader's ladder — the live "
        f"module's rank changed from {before!r} to {after!r}. The probes must "
        "operate on throwaway module copies only."
    )
    assert after_ids == before_ids, (
        "a live reader's ladder object was REBOUND by a test in this file"
    )


@pytest.fixture()
def synthetic_ladder(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """Patch the shared authority to carry ONE extra rung above ``causal``.

    ``raising=False`` so the probe reports the real defect (readers ignoring the
    authority) rather than dying with ``AttributeError`` on a constant the fix
    has not introduced yet.  ``monkeypatch`` restores/deletes on teardown, so
    the live ``evidence_envelope`` is untouched afterwards.
    """
    base = (
        ee.RECEIPT_NONE,
        ee.RECEIPT_DELIVERED,
        ee.RECEIPT_REFERENCED,
        ee.RECEIPT_ACTED,
        ee.RECEIPT_RESOLVED_STATE,
        ee.RECEIPT_CAUSAL,
    )
    ladder = base + (SYNTHETIC_RUNG,)
    rank = types.MappingProxyType({state: i for i, state in enumerate(ladder)})
    monkeypatch.setattr(ee, "RECEIPT_LADDER", ladder, raising=False)
    monkeypatch.setattr(ee, "RECEIPT_RANK", rank, raising=False)
    return ladder


# --------------------------------------------------------------------------- #
# THE RED — mutation check
# --------------------------------------------------------------------------- #
def test_miniswe_receipt_order_follows_the_shared_ladder(
    synthetic_ladder: tuple[str, ...],
) -> None:
    """``miniswe._RECEIPT_ORDER`` must be DERIVED from the shared authority."""
    probe = _fresh_module(_live_miniswe)
    order = probe._RECEIPT_ORDER
    assert SYNTHETIC_RUNG in order, (
        "miniswe._RECEIPT_ORDER did not follow the patched "
        "evidence_envelope.RECEIPT_RANK — it is a hand-duplicated literal "
        f"(saw {dict(order)!r}). The ladder needs ONE authority."
    )
    assert order[SYNTHETIC_RUNG] == len(synthetic_ladder) - 1
    assert order[ee.RECEIPT_NONE] == 0


def test_receipt_sidecar_transition_rank_follows_the_shared_ladder(
    synthetic_ladder: tuple[str, ...],
) -> None:
    """``receipt_sidecar._TRANSITION_RANK`` must be DERIVED from the same one."""
    probe = _fresh_module(_live_sidecar)
    rank = probe._TRANSITION_RANK
    assert SYNTHETIC_RUNG in rank, (
        "receipt_sidecar._TRANSITION_RANK did not follow the patched "
        "evidence_envelope.RECEIPT_RANK — it is a hand-duplicated literal "
        f"(saw {dict(rank)!r}). The ladder needs ONE authority."
    )
    assert rank[SYNTHETIC_RUNG] == len(synthetic_ladder) - 1
    # the sidecar records TRANSITIONS, so rank 0 (``none``) is excluded by
    # construction — it is not a transition anyone can emit.
    assert ee.RECEIPT_NONE not in rank


def test_mutation_probe_is_honest_unpatched_readers_have_no_synthetic_rung(
    live_readers: tuple[ModuleType, ModuleType],
) -> None:
    """Control for the two probes above: without the patch, a freshly loaded
    reader must NOT contain the synthetic rung.  If this ever fails, the two
    RED assertions above are passing for the wrong reason."""
    miniswe, sidecar = live_readers
    assert SYNTHETIC_RUNG not in miniswe._RECEIPT_ORDER
    assert SYNTHETIC_RUNG not in sidecar._TRANSITION_RANK
    assert SYNTHETIC_RUNG not in _fresh_module(_live_miniswe)._RECEIPT_ORDER
    assert SYNTHETIC_RUNG not in _fresh_module(_live_sidecar)._TRANSITION_RANK


# --------------------------------------------------------------------------- #
# the planned authority itself
# --------------------------------------------------------------------------- #
def test_evidence_envelope_exports_the_ordered_ladder_authority() -> None:
    """``RECEIPT_LADDER`` / ``RECEIPT_RANK`` are the single ordered authority."""
    ladder = getattr(ee, "RECEIPT_LADDER", None)
    assert ladder is not None, (
        "evidence_envelope has no RECEIPT_LADDER — the rank order has no "
        "single authority, so every reader hand-duplicates it."
    )
    assert isinstance(ladder, tuple)
    assert ladder == (
        ee.RECEIPT_NONE,
        ee.RECEIPT_DELIVERED,
        ee.RECEIPT_REFERENCED,
        ee.RECEIPT_ACTED,
        ee.RECEIPT_RESOLVED_STATE,
        ee.RECEIPT_CAUSAL,
    )
    rank = getattr(ee, "RECEIPT_RANK", None)
    assert rank is not None, "evidence_envelope has no RECEIPT_RANK"
    assert dict(rank) == {state: i for i, state in enumerate(ladder)}
    # the ladder must cover exactly the declared vocabulary — no rung may be
    # orderable-but-illegal or legal-but-unorderable.
    assert set(ladder) == set(ee._RECEIPT_STATES)


def test_receipt_rank_is_immutable() -> None:
    """The shared authority must not be mutable by any reader that holds it —
    otherwise aliasing it into ``_RECEIPT_ORDER`` would let one consumer
    corrupt every other consumer's ladder."""
    rank = getattr(ee, "RECEIPT_RANK", None)
    assert rank is not None, "evidence_envelope has no RECEIPT_RANK"
    with pytest.raises(TypeError):
        rank["gt_mutation_attempt"] = 99  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        rank.pop(ee.RECEIPT_CAUSAL)  # type: ignore[attr-defined]


def test_runtime_emittable_receipt_states_is_declared() -> None:
    """The runtime seam may only STAMP ``none`` or ``delivered``; every higher
    rung is earned later by an observer.  That subset belongs to the same
    authority, not to a reader's private literal."""
    emittable = getattr(ee, "RUNTIME_EMITTABLE_RECEIPT_STATES", None)
    assert emittable is not None, (
        "evidence_envelope has no RUNTIME_EMITTABLE_RECEIPT_STATES"
    )
    assert isinstance(emittable, frozenset)
    assert emittable == frozenset({ee.RECEIPT_NONE, ee.RECEIPT_DELIVERED})
    assert emittable <= set(ee.RECEIPT_LADDER)


# --------------------------------------------------------------------------- #
# cheap regression guards (honest, but NOT the RED — they pass today)
# --------------------------------------------------------------------------- #
def test_the_two_rank_maps_agree_on_every_shared_key(
    live_readers: tuple[ModuleType, ModuleType],
) -> None:
    miniswe, sidecar = live_readers
    shared = set(miniswe._RECEIPT_ORDER) & set(sidecar._TRANSITION_RANK)
    assert shared, "the two rank maps share no key at all"
    mismatched = {
        k: (miniswe._RECEIPT_ORDER[k], sidecar._TRANSITION_RANK[k])
        for k in sorted(shared)
        if miniswe._RECEIPT_ORDER[k] != sidecar._TRANSITION_RANK[k]
    }
    assert not mismatched, f"receipt rank drift between readers: {mismatched}"


def test_sidecar_covers_the_full_ladder_minus_none(
    live_readers: tuple[ModuleType, ModuleType],
) -> None:
    miniswe, sidecar = live_readers
    assert set(sidecar._TRANSITION_RANK) | {ee.RECEIPT_NONE} == set(
        miniswe._RECEIPT_ORDER
    )


def test_ranks_are_a_dense_monotone_ladder(
    live_readers: tuple[ModuleType, ModuleType],
) -> None:
    miniswe, _sidecar = live_readers
    ranks = sorted(miniswe._RECEIPT_ORDER.values())
    assert ranks == list(range(len(ranks))), (
        "the ladder must be dense and gap-free — a rank comparison is only "
        f"meaningful over contiguous rungs (saw {ranks})"
    )
    assert miniswe._RECEIPT_ORDER[ee.RECEIPT_NONE] == 0

"""RED-5 (2026-07-28) — the runtime receipt ladder promotes ``delivered`` -> ``acted``
DISHONESTLY, and this file falsifies it at the real seam.

THE DEFECT (read, not guessed)
------------------------------
``artifact_deepswe/gt_mini_patch.py:15599-15632`` (inside ``_gt_gateway_deliver``) runs a
promotion loop over every sealed delivery recorded in ``_gt_gateway_deliveries``::

    _gt_gateway_deliveries[:] = _ad_update_receipts(
        _gt_gateway_deliveries, policy_text="",
        env_output=orig_out or "", next_action_cmd=cmd or "")

``update_receipts`` (``src/groundtruth/runtime/adapters/miniswe.py:1436``) decides ``acted``
with a PURE STRING MATCH against that command::

    acted = any(_entity_matches(k, e, cmd) for k, e in ents)   # :1485

where ``ents = _delivered_entities(env)`` (``:1364``) always contains ``("path", env.target)``
— i.e. the very file the evidence is ABOUT. GT delivers evidence about a file BECAUSE the
agent is working on that file, so the agent's own next command names it by construction. The
promotion therefore fires on the agent doing what it was already doing. There is:

  * no observation binding (``observation_binding`` is ``null`` on every runtime receipt),
  * no decision-commit window,
  * no registry acknowledgment predicate,
  * and ``referenced`` is structurally UNREACHABLE because ``policy_text=""`` is hardcoded
    at ``:15623`` (the agent's own message is not observable at this env-facing seam).

LIVE WITNESS — ``D:\\gt_runs\\30390877219\\artifacts\\ll-full-aiogram__aiogram-1594\\
gt_receipts_aiogram__aiogram-1594.jsonl`` has exactly 4 rows: two ``delivered``
(ts_ms 1785266845737 / 1785266845755) and two ``acted`` (ts_ms 1785266845762 — **7 ms**
later), ALL four with ``action_index: 2`` and ``observation_binding: null``. Same action
index means no later action was ever required to earn ``acted``.

THE SURVIVING AUTHORITY — the correct definition already exists OFFLINE:
``scripts/swebench/fair_probe_result.py:258`` ``_treatment_acted`` requires (a) timing
``ON_TIME``, (b) the class's registry receipt predicate, and (c) the strict precommit window
``delivery_index < action_index <= decision_commit_index``. Part 2 pins that authority where
it lives, so deleting the seam's fake ladder loses nothing that was ever real.

PLANNED FIX (NOT implemented here): delete the promotion block; every runtime receipt stays
``delivered``. NOTE for the implementer: the block is nested inside
``if _gt_gateway_deliveries:`` whose body ALSO contains ``_xsession_flush()`` (``:15637``,
outside the ``try``) — that call must survive the deletion.

SCOPE OF PART 1: this drives the LEGACY ``_augment_output`` seam through ss_gate's
``RealSeamDriver`` (production Super-Mode posture, Profile-2 + global arbiter). It reads the
RAW ``gt_receipts.jsonl`` sidecar, NOT ``scripts/swebench/receipt_sidecar``: the runtime
writes ``observation_binding: null``, which the strict sidecar reader rejects, so a
sidecar-mediated read would be blocked by a DIFFERENT (concurrently-owned) defect and could
not falsify this one.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts" / "swebench"
_SRC = _REPO / "src"
_ART = _REPO / "artifact_deepswe"
for _p in (str(_SCRIPTS), str(_SRC), str(_ART)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from groundtruth.runtime.chronological_adjudication import (  # noqa: E402
    LATE,
    ON_TIME,
    Chronology,
)

import fair_probe_result  # noqa: E402
import ss_gate  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — falsification at the REAL seam
# ══════════════════════════════════════════════════════════════════════════════
def _receipt_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _describe(row: dict) -> str:
    env = row.get("envelope") or {}
    return (
        f"{row.get('kind')}/{row.get('transition')} "
        f"action_index={row.get('action_index')} ts_ms={row.get('ts_ms')} "
        f"evidence_type={env.get('evidence_type')!r} target={env.get('target')!r} "
        f"observation_binding={'present' if env.get('observation_binding') else 'NULL'}"
    )


@pytest.fixture(scope="module")
def seam_receipts(tmp_path_factory) -> list[dict]:
    """Drive the real seam over a fixture where the agent works on ONE file across two
    turns, and return every receipt row the runtime persisted.

    THE FIXTURE IS THE NORMAL CASE, deliberately: turn 1 the agent views
    ``pkg/mod_b.py`` and GT delivers caller evidence ABOUT ``pkg/mod_b.py`` (the delivery
    is triggered BY that file); turn 2 the agent edits ``pkg/mod_b.py`` — the file it was
    already editing. Nothing in that stream is evidence that GT's bytes changed a single
    decision, yet the seam stamps ``acted``.
    """
    sidecar = tmp_path_factory.mktemp("gt_receipts") / "gt_receipts.jsonl"
    driver = ss_gate.RealSeamDriver()
    g = driver.g
    saved = g._receipts_sidecar_path
    g._receipts_sidecar_path = lambda: str(sidecar)
    try:
        driver.run(
            [
                ss_gate._cat(ss_gate._MOD_B, "def run(): return 'b'\n"),
                ss_gate._write(ss_gate._MOD_B, "return 'b'", "return 'B'"),
            ],
            {},
        )
    finally:
        g._receipts_sidecar_path = saved
    return _receipt_rows(sidecar)


def test_seam_receipt_channel_is_live(seam_receipts: list[dict]) -> None:
    """Guard against a vacuous green: the falsification below is only meaningful if the
    receipt channel actually produced ``delivered`` rows on this fixture."""
    delivered = [r for r in seam_receipts if r.get("transition") == "delivered"]
    assert delivered, (
        "the runtime receipt channel produced ZERO 'delivered' rows on this fixture - the "
        "seam/graph channel is dead, so the acted-promotion assertions below would pass "
        "vacuously. Fix the channel before reading any verdict from this file.\n"
        f"rows={[_describe(r) for r in seam_receipts]}"
    )


def test_no_runtime_receipt_is_promoted_to_acted(seam_receipts: list[dict]) -> None:
    """THE FALSIFICATION. A runtime receipt may only ever say ``delivered``.

    This FAILS today, and the failure IS the proof that ``acted`` is a fabricated field.
    """
    acted = [r for r in seam_receipts if r.get("transition") == "acted"]
    assert not acted, (
        "RECEIPT LADDER IS DISHONEST. The runtime promoted a delivery to 'acted' purely "
        "because the agent's next command NAMED the file the evidence was about - the file "
        "GT delivered evidence about precisely BECAUSE the agent was working on it. "
        "gt_mini_patch.py:15599-15632 calls adapters/miniswe.update_receipts, whose "
        "acted test is `any(_entity_matches(k, e, cmd) ...)` over entities that always "
        "include ('path', env.target). No observation binding, no decision-commit window, "
        "no registry acknowledgment predicate. 'acted' here means nothing beyond 'the agent "
        "kept doing what it was already doing', and it is read downstream as CONSUMPTION "
        "evidence. The honest terminal state at this seam is 'delivered'; the real "
        "definition lives offline in fair_probe_result._treatment_acted.\n"
        f"offending rows:\n  " + "\n  ".join(_describe(r) for r in acted)
    )


def test_no_runtime_receipt_is_promoted_to_referenced(seam_receipts: list[dict]) -> None:
    """``referenced`` is structurally unreachable at this seam (``policy_text=""`` is
    hardcoded at gt_mini_patch.py:15623). Lock that in: if a future change makes it
    reachable, it would be earned from environment/tool output, which is exactly the
    false-promotion class B-13 already closed inside ``update_receipts``."""
    referenced = [r for r in seam_receipts if r.get("transition") == "referenced"]
    assert not referenced, (
        "a runtime receipt claimed 'referenced', which this env-facing seam cannot "
        "legitimately observe - the policy's own message is not available here "
        "(policy_text=\"\" at gt_mini_patch.py:15623). Any 'referenced' row is earned from "
        "environment output, not from the policy.\n"
        f"offending rows:\n  " + "\n  ".join(_describe(r) for r in referenced)
    )


def test_runtime_receipts_carry_no_observation_binding_today() -> None:
    """DOCUMENTED CO-DEFECT (owned elsewhere, asserted here only as a live-witness fact):
    the run-30390877219 receipts carry ``observation_binding: null``, which is why this
    file reads the raw JSONL instead of going through ``receipt_sidecar``. If the binding
    ever becomes non-null, a sidecar-mediated read becomes possible — but it still would
    not make ``acted`` honest, because the binding identifies the DELIVERY observation, not
    the decision the action committed."""
    # Pure statement of the reader constraint; no filesystem dependency on a run artifact.
    from groundtruth.runtime.adapters.miniswe import update_receipts

    assert "next_action_cmd" in update_receipts.__code__.co_varnames, (
        "update_receipts no longer takes next_action_cmd — the promotion contract this "
        "file falsifies has moved; re-read the seam before trusting these assertions."
    )


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — the surviving authority, tested where it lives
# ══════════════════════════════════════════════════════════════════════════════
def _chron(delivery: int, action: int, commit: int) -> Chronology:
    return Chronology(
        decision_open_index=delivery,
        delivery_index=delivery,
        decision_commit_index=commit,
        native_acquisition_index=None,
        acknowledgment_index=action,
        action_index=action,
    )


def _ec(delivery: int, action: int, commit: int, verdict: str = ON_TIME) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_type="caller_contract",
        timing_verdict=verdict,
        chronology=_chron(delivery, action, commit),
    )


@pytest.fixture()
def ack_true(monkeypatch: pytest.MonkeyPatch):
    """Hold the registry acknowledgment predicate at True so each case below isolates the
    PRECOMMIT-WINDOW arithmetic — condition (c) of ``_treatment_acted``."""
    monkeypatch.setattr(
        fair_probe_result.receipt_predicates,
        "acknowledgment_for_row",
        lambda *a, **k: True,
    )


def test_treatment_acted_true_only_in_the_strict_precommit_window(ack_true) -> None:
    assert fair_probe_result._treatment_acted(_ec(delivery=3, action=4, commit=6)) is True
    assert fair_probe_result._treatment_acted(_ec(delivery=3, action=6, commit=6)) is True


def test_treatment_acted_false_when_action_not_after_delivery(ack_true) -> None:
    """``action_index <= delivery_index`` — the agent acted BEFORE (or at) the delivery, so
    the delivery cannot have caused it. This is exactly what the seam's string match cannot
    see: it has no delivery index at all."""
    assert fair_probe_result._treatment_acted(_ec(delivery=4, action=4, commit=6)) is False
    assert fair_probe_result._treatment_acted(_ec(delivery=4, action=3, commit=6)) is False


def test_treatment_acted_false_when_action_after_decision_commit(ack_true) -> None:
    """``action_index > decision_commit_index`` — post-hoc naming, after the decision the
    evidence was meant to shape was already committed."""
    assert fair_probe_result._treatment_acted(_ec(delivery=3, action=7, commit=6)) is False


def test_treatment_acted_false_when_timing_is_not_on_time(ack_true) -> None:
    """Condition (a): a LATE delivery is never ``acted``, whatever the window says."""
    ec = _ec(delivery=3, action=4, commit=6, verdict=LATE)
    assert fair_probe_result._treatment_acted(ec) is False


def test_treatment_acted_false_when_registry_predicate_is_not_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Condition (b): a bare mention is not an acknowledgment. ``None`` (unmeasured) is
    fail-closed too, not just ``False``."""
    for ack in (False, None):
        monkeypatch.setattr(
            fair_probe_result.receipt_predicates,
            "acknowledgment_for_row",
            lambda *a, _ack=ack, **k: _ack,
        )
        assert fair_probe_result._treatment_acted(_ec(3, 4, 6)) is False


def test_treatment_acted_false_on_missing_indices(ack_true) -> None:
    """A missing index is not a satisfied window — the seam's ladder has NO indices at all,
    which under this authority means it can never earn ``acted``."""
    ec = _ec(3, 4, 6)
    for field in ("delivery_index", "action_index", "decision_commit_index"):
        ec.chronology = replace(_chron(3, 4, 6), **{field: None})
        assert fair_probe_result._treatment_acted(ec) is False

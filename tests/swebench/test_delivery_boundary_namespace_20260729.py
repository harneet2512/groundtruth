"""The delivery-boundary anchor must be validated before it is ENFORCED.

Observed on real artifacts (run 30390877219, tasks
``aws-cloudformation__cfn-lint-3749`` / ``-3764``): 12 delivered ledger rows read
``physical_span_precedes_delivery_boundary`` -- every sealed window sat BEFORE the
boundary derived from the row's ``iteration``.

The bytes were fine. The FIELD was not. ``iteration`` on a delivery-side row is
``gt_mini_patch._action_count`` captured as ``producer_iteration``; on those runs it
was frozen for the whole task (all 19 delivered rows in -3749 carried ``8``; all 5 in
-3764 carried ``10``) while the sealed bytes demonstrably landed in observations
spanning tool ordinals 5..46. A single observation index cannot name several disjoint
observations, so the field carried NO ordering information -- and the reader was
comparing an action-counter namespace against a tool-observation namespace.

The invariant these tests pin: for one ``iteration`` value to be a delivery
boundary, every delivered row stamped with it must have a byte-proven window in a
COMMON observation. When that intersection is empty the anchor is not ordering-bearing
and must be treated as UNKNOWN (the documented earliest-window path) -- never as a
licence to fabricate a join, and never as a reason to disable the gate where the
anchor IS coherent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.swebench.consumption_ledger import build_consumption_ledger


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _delivered_row(payload: str, *, iteration: int, layer: str) -> dict:
    return {
        "layer": layer,
        "event_type": "post_view",
        "iteration": iteration,
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": _sha16(payload),
        "file_path": "src/pkg/mod.py",
    }


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _obs(payload: str, tag: str) -> dict:
    return {
        "role": "tool",
        "content": f"<returncode>0</returncode>\n<output>\n{tag}\n{payload}\n</output>",
    }


def _act(cmd: str) -> dict:
    return {"role": "assistant", "content": cmd}


# Three distinct sealed payloads, repo-agnostic; only the trajectory SHAPE matters.
_A = "\nsrc/pkg/mod.py:12:class Alpha(Base):\nsrc/pkg/mod.py:40:def alpha(self):"
_B = "\nsrc/pkg/other.py:7:class Beta(Base):\nsrc/pkg/other.py:31:def beta(self):"
_C = "\nsrc/pkg/third.py:3:class Gamma(Base):\nsrc/pkg/third.py:19:def gamma(self):"
_D = "\nsrc/pkg/forth.py:5:class Delta(Base):\nsrc/pkg/forth.py:22:def delta(self):"


def _long_trajectory(payload_by_ordinal: dict[int, str], n_tool: int) -> list[dict]:
    """assistant/tool alternation: tool ordinal k lives at message index 2k-1."""
    messages: list[dict] = []
    for k in range(1, n_tool + 1):
        messages.append(_act(f"grep -rn step{k} src/"))
        payload = payload_by_ordinal.get(k)
        messages.append(
            _obs(payload, f"observation {k}") if payload
            else {"role": "tool", "content": f"<output>\nunrelated {k}\n</output>"}
        )
    return messages


def test_frozen_iteration_stamp_is_not_a_delivery_boundary(tmp_path: Path) -> None:
    """RED before fix: the real run-30390877219 shape.

    Three delivered rows all carry the SAME frozen ``iteration`` (10) while their
    sealed bytes land in three DIFFERENT observations (ordinals 3, 4, 6 -> messages
    5, 7, 11), all before the boundary that ordinal 10 would name (message 19).
    Their common-observation intersection is empty, so the stamp is proven not to be
    an observation index: the boundary is UNKNOWN and all three rows must still join
    on their byte-proven windows.
    """
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [
        _delivered_row(_A, iteration=10, layer="consensus.scope_map"),
        _delivered_row(_B, iteration=10, layer="l3b.evidence"),
        _delivered_row(_C, iteration=10, layer="consensus.scope_map"),
    ])

    messages = _long_trajectory({3: _A, 4: _B, 6: _C}, n_tool=11)

    result = build_consumption_ledger(
        {"messages": messages}, runtime_ledger_path=str(ledger)
    )

    assert result["delivery_boundary_namespace_valid"] is False
    evidence = result["delivery_boundary_namespace_evidence"]
    assert evidence is not None
    assert evidence["iteration"] == 10
    # The witness is the first PROVEN contradiction (rows 1 and 2 -> messages 5
    # and 7), not a census of every stamped row.
    assert sorted(evidence["disjoint_home_msg_indices"]) == [5, 7]
    assert evidence["reason"] == "no_common_observation_for_shared_iteration"

    assert result["exact_seal_ambiguity_count"] == 0
    assert result["ledger_rows_delivered"] == 3
    assert result["ledger_rows_joined"] == 3
    assert result["visible_audit_complete"] is True

    homes = sorted(
        e["physical_id"] for e in result["entries"]
        if e.get("join_method") == "seal"
    )
    assert [h.split(":", 1)[0] for h in homes] == ["m11", "m5", "m7"]


def test_coherent_iteration_stamp_keeps_the_boundary_enforced(
    tmp_path: Path,
) -> None:
    """Mutation guard: the gate must NOT be blanket-disabled.

    Two rows share ``iteration`` 3 and both have a window in the SAME observation
    (message 5) -- coherent, so the anchor stays ordering-bearing. A third row claims
    ``iteration`` 6 (message 11) but its bytes exist ONLY at message 5, before that
    boundary: a genuine seal/attestation inconsistency that must still fail closed.
    """
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [
        _delivered_row(_A, iteration=3, layer="consensus.scope_map"),
        _delivered_row(_B, iteration=3, layer="l3b.evidence"),
        _delivered_row(_C, iteration=6, layer="consensus.scope_map"),
    ])

    messages = _long_trajectory({}, n_tool=11)
    # One observation (ordinal 3 -> message 5) carries all three payloads.
    messages[5] = _obs(_A + _B + _C, "observation 3")

    result = build_consumption_ledger(
        {"messages": messages}, runtime_ledger_path=str(ledger)
    )

    assert result["delivery_boundary_namespace_valid"] is True
    assert result["delivery_boundary_namespace_evidence"] is None

    # Rows 1 and 2 join at their coherent boundary; row 3 fails closed.
    assert result["ledger_rows_joined"] == 2
    assert result["exact_seal_ambiguity_count"] == 1
    ambiguity = result["exact_seal_ambiguities"][0]
    assert ambiguity["delivery_boundary_msg_index"] == 11
    assert all(
        pid.startswith("m5:") for pid in ambiguity["candidate_physical_ids"]
    )
    assert result["visible_audit_complete"] is False


def test_namespace_check_never_fabricates_an_absent_delivery(
    tmp_path: Path,
) -> None:
    """A row whose sealed bytes are nowhere in the visible stream stays unjoined,
    with or without a trustworthy boundary. The validation relaxes WHICH window may
    anchor a delivery -- never WHETHER the bytes must exist."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [
        _delivered_row(_A, iteration=10, layer="consensus.scope_map"),
        _delivered_row(_B, iteration=10, layer="l3b.evidence"),
        _delivered_row(_C, iteration=10, layer="consensus.scope_map"),
    ])

    # _C never appears; _A and _B land in disjoint observations (stamp incoherent).
    messages = _long_trajectory({3: _A, 4: _B}, n_tool=11)

    result = build_consumption_ledger(
        {"messages": messages}, runtime_ledger_path=str(ledger)
    )

    assert result["delivery_boundary_namespace_valid"] is False
    assert result["ledger_rows_delivered"] == 3
    assert result["ledger_rows_joined"] == 2
    assert result["exact_seal_ambiguity_count"] == 0
    assert not any(
        e.get("join_method") == "seal"
        and e.get("content_sha256_16") == _sha16(_C)
        for e in result["entries"]
    )


def test_absent_row_cannot_invalidate_the_boundary_namespace(
    tmp_path: Path,
) -> None:
    """A delivered row whose bytes never surfaced has an EMPTY home set. It must
    not testify: an empty set intersects everything to nothing, so letting it vote
    would let one undelivered row disarm the boundary for the whole ledger --
    fail-open through the back door. Here the absent row shares ``iteration`` 3
    with a coherent one, and the gate must stay armed."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [
        _delivered_row(_A, iteration=3, layer="consensus.scope_map"),
        _delivered_row(_D, iteration=3, layer="l3b.evidence"),   # never rendered
        _delivered_row(_C, iteration=6, layer="consensus.scope_map"),
    ])

    messages = _long_trajectory({}, n_tool=11)
    # Only _A and _C are visible, both in observation 3 (message 5).
    messages[5] = _obs(_A + _C, "observation 3")

    result = build_consumption_ledger(
        {"messages": messages}, runtime_ledger_path=str(ledger)
    )

    assert result["delivery_boundary_namespace_valid"] is True
    assert result["delivery_boundary_namespace_evidence"] is None
    # _A joins at its coherent boundary; _D has no bytes; _C is pre-boundary only.
    assert result["ledger_rows_joined"] == 1
    assert result["exact_seal_ambiguity_count"] == 1
    assert result["visible_audit_complete"] is False


def test_single_delivered_row_cannot_disprove_its_own_boundary(
    tmp_path: Path,
) -> None:
    """One row testifies about one observation: there is no contradiction to find,
    so the boundary stays ENFORCED and a pre-boundary-only window still fails closed.
    Pins that the check needs corroborating rows and can never self-clear."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [
        _delivered_row(_A, iteration=6, layer="l3b.evidence"),
    ])

    messages = _long_trajectory({3: _A}, n_tool=11)

    result = build_consumption_ledger(
        {"messages": messages}, runtime_ledger_path=str(ledger)
    )

    assert result["delivery_boundary_namespace_valid"] is True
    assert result["ledger_rows_joined"] == 0
    assert result["exact_seal_ambiguity_count"] == 1
    assert result["visible_audit_complete"] is False

"""D4 regression: a benign byte-identical duplicate of a sealed GT delivery must
not make the v2 visible-byte audit report incomplete -- while a genuine seal /
attestation inconsistency must still fail closed.

Root defect (observed on a live smoke run): GT delivered an l3b localization
block into an observation; the agent later ran a grep whose output reproduced
the exact same bytes in a second observation. The reconciler saw two
byte-identical windows for one sealed row, abandoned the row, and flagged an
``exact_seal_ambiguity`` -- which drove ``visible_audit_complete`` to False even
though ``physical_identity_conflict_count`` was 0 (no leak, no conflict).

The home of a duplicated sealed delivery is resolved from the row's delivery
iteration (``iteration`` -> tool_ordinal message index): the earliest window at or
after that boundary. (The anchor is enforced only once it survives the
namespace validation in ``test_delivery_boundary_namespace_20260729.py``; every
ledger here is single-row or coherent, so the boundary is live in all of them.)
A byte-identical window BEFORE the
boundary is the agent independently producing the same repo bytes (a pre-delivery
collision) and must not anchor the delivery -- anchoring it there would let the
receipt ladder count pre-delivery actions as GT consumption. If EVERY window is
before the boundary the row is genuinely inconsistent and stays incomplete.

These tests use generic, repo-agnostic content -- the load-bearing property is
the trajectory *shape*, never any task/repo identity.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.swebench.consumption_ledger import build_consumption_ledger


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _delivered_row(payload: str, *, iteration: int) -> dict:
    """A delivered post_view row. ``iteration`` is the Nth tool observation the
    row was delivered into (its tool_ordinal / delivery boundary)."""
    return {
        "layer": "l3b.evidence",
        "event_type": "post_view",
        "iteration": iteration,
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": _sha16(payload),
        "file_path": "src/commands/remote.py",
    }


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


# The exact GT-delivered localization block: two def/class anchor lines. Ordinary
# source-symbol text, identical in structure to what the seam renders for an l3b
# evidence delivery.
_LOC_BLOCK = (
    "\nsrc/config.py:25:class ConfigError(BaseError):"
    "\nsrc/config.py:373:def edit(self, level=None, validate=True):"
)


def test_duplicate_localization_echo_still_completes_visible_audit(
    tmp_path: Path,
) -> None:
    """RED before fix / GREEN after: GT delivers the block into a tool
    observation (iteration 1, tool_ordinal 1 -> boundary m0); the agent later
    echoes it verbatim in a subsequent grep output. One sealed row, two
    byte-identical windows -> the audit reconciles, resolves to the delivery
    boundary, and completes. Mirrors the live dvc shape (boundary == the delivery
    window; the later echo is non-anchoring)."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [_delivered_row(_LOC_BLOCK, iteration=1)])

    # m0: the GT delivery observation (block embedded in a large buffer).
    delivery_obs = (
        "<returncode>0</returncode>\n<output>\n"
        + ("noise " * 400)
        + _LOC_BLOCK
        + "\n" + ("tail " * 200)
        + "</output>"
    )
    # m2: the agent's own later grep, echoing the same bytes.
    echo_obs = (
        "<returncode>0</returncode>\n<output>\nDone\n"
        + _LOC_BLOCK
        + "</output>"
    )

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "tool", "content": delivery_obs},
                {"role": "assistant", "content": "grep -rn edit src/"},
                {"role": "tool", "content": echo_obs},
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    assert result["visible_audit_complete"] is True
    assert result["exact_seal_ambiguity_count"] == 0
    assert result["ledger_rows_joined"] == 1
    assert result["ledger_rows_delivered"] == 1
    assert result["physical_identity_conflict_count"] == 0

    # Resolved to the delivery-boundary window (m0), NOT the later grep echo (m2).
    assert result["exact_seal_duplicate_window_count"] == 1
    duplicate = result["exact_seal_duplicate_windows"][0]
    assert duplicate["delivery_boundary_msg_index"] == 0
    assert duplicate["resolved_physical_id"].startswith("m0:")
    assert [pid.split(":", 1)[0] for pid in duplicate["candidate_physical_ids"]] == [
        "m0",
        "m2",
    ]
    joined = [
        e for e in result["entries"]
        if e.get("source") == "trajectory" and e.get("join_method") == "seal"
    ]
    assert len(joined) == 1
    assert joined[0]["physical_id"].startswith("m0:")
    assert joined[0]["rendered_text"] == _LOC_BLOCK


def test_triplicate_echo_resolves_to_delivery_boundary_and_completes(
    tmp_path: Path,
) -> None:
    """Generalizes beyond two windows: three byte-identical echoes still complete
    and still resolve to the boundary. Proves the fix is not tuned to N=2."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [_delivered_row(_LOC_BLOCK, iteration=1)])

    obs = "<output>\n" + _LOC_BLOCK + "</output>"
    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "tool", "content": obs},
                {"role": "tool", "content": obs},
                {"role": "tool", "content": obs},
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    assert result["visible_audit_complete"] is True
    assert result["ledger_rows_joined"] == 1
    assert result["exact_seal_ambiguity_count"] == 0
    duplicate = result["exact_seal_duplicate_windows"][0]
    assert duplicate["delivery_boundary_msg_index"] == 0
    assert duplicate["resolved_physical_id"].startswith("m0:")
    assert len(duplicate["candidate_physical_ids"]) == 3


def test_pre_delivery_collision_does_not_anchor_the_delivery(
    tmp_path: Path,
) -> None:
    """The agent emits a byte-identical window BEFORE the delivery iteration. The
    join must anchor at/after the boundary (the real GT delivery), never on the
    earlier agent-produced copy -- otherwise the receipt ladder would credit
    pre-delivery actions as GT consumption."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    # Delivery iteration 2 -> boundary is the 2nd tool observation (m2).
    _write_ledger(ledger, [_delivered_row(_LOC_BLOCK, iteration=2)])

    pre_echo = "<output>\nagent grep first\n" + _LOC_BLOCK + "</output>"
    delivery_obs = "<output>\nGT delivery\n" + _LOC_BLOCK + "</output>"

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "tool", "content": pre_echo},              # m0 (ordinal 1)
                {"role": "assistant", "content": "grep -rn edit"},  # m1
                {"role": "tool", "content": delivery_obs},          # m2 (ordinal 2)
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    assert result["visible_audit_complete"] is True
    assert result["ledger_rows_joined"] == 1
    assert result["exact_seal_ambiguity_count"] == 0
    assert result["exact_seal_duplicate_window_count"] == 1
    duplicate = result["exact_seal_duplicate_windows"][0]
    assert duplicate["delivery_boundary_msg_index"] == 2
    # Anchored on the delivery (m2), NOT the pre-delivery agent echo (m0).
    assert duplicate["resolved_physical_id"].startswith("m2:")
    assert [pid.split(":", 1)[0] for pid in duplicate["candidate_physical_ids"]] == [
        "m0",
        "m2",
    ]
    joined = [
        e for e in result["entries"]
        if e.get("source") == "trajectory" and e.get("join_method") == "seal"
    ]
    assert len(joined) == 1
    assert joined[0]["physical_id"].startswith("m2:")


def test_all_windows_before_boundary_fail_closed(tmp_path: Path) -> None:
    """The sealed bytes surface ONLY before the row's own delivery boundary and
    nowhere at/after it: a genuine seal/attestation inconsistency. The revived
    exact_seal_ambiguities guard must fire and the audit must stay incomplete."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    # Claims delivery at iteration 2 (boundary = 2nd tool observation, m2)...
    _write_ledger(ledger, [_delivered_row(_LOC_BLOCK, iteration=2)])

    pre_echo = "<output>\nagent grep first\n" + _LOC_BLOCK + "</output>"
    # ...but the boundary observation (m2) does NOT contain the sealed bytes.
    unrelated = "<output>\nunrelated boundary observation\n</output>"

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "tool", "content": pre_echo},      # m0 (ordinal 1)
                {"role": "assistant", "content": "grep"},   # m1
                {"role": "tool", "content": unrelated},     # m2 (ordinal 2)
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    assert result["visible_audit_complete"] is False
    assert result["ledger_rows_joined"] == 0
    assert result["exact_seal_duplicate_window_count"] == 0
    assert result["exact_seal_ambiguity_count"] == 1
    ambiguity = result["exact_seal_ambiguities"][0]
    assert ambiguity["delivery_boundary_msg_index"] == 2
    assert any(
        pid.startswith("m0:") for pid in ambiguity["candidate_physical_ids"]
    )
    assert not any(
        e.get("join_method") == "seal"
        for e in result["entries"]
        if e.get("source") == "trajectory"
    )


# --------------------------------------------------------------------------- #
# Mutation checks: genuinely-unauditable inputs MUST still read incomplete.
# --------------------------------------------------------------------------- #

def test_unjoined_visible_gt_tag_still_fails_closed(tmp_path: Path) -> None:
    """A model-visible ``<gt-*>`` block that no delivered ledger row accounts for
    is genuinely unreconciled -> the audit stays incomplete, even in the presence
    of a resolved benign duplicate."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [_delivered_row(_LOC_BLOCK, iteration=1)])

    duplicated = "<output>\n" + _LOC_BLOCK + "</output>"
    orphan_tag = "<gt-contract>preserve unsealed callers</gt-contract>"

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "tool", "content": duplicated},   # m0 (ordinal 1)
                {"role": "tool", "content": duplicated},   # m1 (ordinal 2)
                {"role": "tool", "content": orphan_tag},   # m2 (ordinal 3)
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    # The benign duplicate resolved...
    assert result["exact_seal_ambiguity_count"] == 0
    assert result["exact_seal_duplicate_window_count"] == 1
    # ...but the orphan visible GT tag keeps the audit honestly incomplete.
    assert result["unjoined_visible_tag_count"] == 1
    assert result["visible_audit_complete"] is False


def test_delivered_bytes_absent_are_not_fabricated_into_a_join(
    tmp_path: Path,
) -> None:
    """A sealed delivery whose exact bytes never appear in the visible stream is
    not resolved, not counted as a duplicate, and not fabricated into a join."""
    ledger = tmp_path / "gt_runtime_ledger_synthetic.jsonl"
    _write_ledger(ledger, [_delivered_row(_LOC_BLOCK, iteration=1)])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": "unrelated observation bytes"}]},
        runtime_ledger_path=str(ledger),
    )

    assert result["ledger_rows_joined"] == 0
    assert result["exact_seal_duplicate_window_count"] == 0
    assert result["exact_seal_ambiguity_count"] == 0
    assert not any(
        e.get("join_method") == "seal"
        for e in result["entries"]
        if e.get("source") == "trajectory"
    )

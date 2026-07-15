from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

from scripts.swebench.consumption_ledger import build_consumption_ledger
from scripts.swebench import gt_feature_metrics


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _row(payload: str, *, layer: str = "l3.contract", byte_length: bool = False) -> dict:
    return {
        "layer": layer,
        "event_type": "post_view",
        "iteration": 1,
        "outcome": "delivered",
        "chars_delivered": len(payload.encode("utf-8")) if byte_length else len(payload),
        "content_sha256_16": _sha16(payload),
        "file_path": "src/pkg.py",
    }


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _write_task(path: Path, content: str, rows: list[dict]) -> None:
    (path / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": [{"role": "tool", "content": content}]}),
        encoding="utf-8",
    )
    _write_ledger(path / "gt_runtime_ledger_synthetic.jsonl", rows)


def test_overlapping_legacy_tag_and_sealed_window_are_one_physical_delivery(
    tmp_path: Path,
) -> None:
    tag = '<gt-contract file="src/pkg.py">preserve callers</gt-contract>'
    sealed = "\n" + tag + "\n"
    content = "native output" + sealed + "tail"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(sealed)])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": content}]},
        runtime_ledger_path=str(ledger),
    )

    visible = [entry for entry in result["entries"] if entry["source"] == "trajectory"]
    assert len(visible) == 1
    assert result["gt_blocks_delivered"] == 1
    assert visible[0]["physical_id"]
    assert visible[0]["span_start"] == len("native output")
    assert visible[0]["span_end"] == len("native output" + sealed)
    assert result["unjoined_visible_tag_count"] == 0
    assert result["visible_audit_complete"] is True


def test_disjoint_tag_and_native_payload_remain_two_physical_deliveries(
    tmp_path: Path,
) -> None:
    tag = '<gt-contract file="src/pkg.py">preserve callers</gt-contract>'
    native = "src/pkg.py:7 preserve parse_config"
    content = tag + "\nordinary output\n" + native
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(tag), _row(native, layer="edit.syntax")])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": content}]},
        runtime_ledger_path=str(ledger),
    )

    visible = [entry for entry in result["entries"] if entry["source"] == "trajectory"]
    assert len(visible) == 2
    assert len({entry["physical_id"] for entry in visible}) == 2
    assert result["gt_blocks_delivered"] == 2
    assert result["visible_audit_complete"] is True


def test_one_sealed_window_canonicalizes_all_overlapping_legacy_tags(
    tmp_path: Path,
) -> None:
    first = "<gt-contract>preserve callers</gt-contract>"
    second = "<gt-scope>inspect sibling.py</gt-scope>"
    sealed = "\n" + first + "\n" + second + "\n"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(sealed)])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": "output" + sealed}]},
        runtime_ledger_path=str(ledger),
    )

    visible = [entry for entry in result["entries"] if entry["source"] == "trajectory"]
    assert len(visible) == 1
    assert visible[0]["legacy_spans"] == [
        [len("output\n"), len("output\n" + first)],
        [len("output\n" + first + "\n"), len("output\n" + first + "\n" + second)],
    ]
    assert result["gt_blocks_delivered"] == 1


def test_sealed_byte_window_reports_character_offsets_for_unicode(tmp_path: Path) -> None:
    prefix = "α🙂 prefix "
    payload = "Δ src/pkg.py:9 preserve café"
    content = prefix + payload + " suffix"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    row = _row(payload, byte_length=True)
    _write_ledger(ledger, [row])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": content}]},
        runtime_ledger_path=str(ledger),
    )

    entry = next(e for e in result["entries"] if e["source"] == "trajectory")
    assert entry["rendered_text"] == payload
    assert entry["span_start"] == len(prefix)
    assert entry["span_end"] == len(prefix) + len(payload)
    assert gt_feature_metrics._row_has_seal_join(row, result["entries"]) is True


def test_ambiguous_duplicate_seal_occurrence_fails_visible_audit(
    tmp_path: Path,
) -> None:
    payload = "src/pkg.py:9 preserve callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(payload)])

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "user", "content": payload},
                {"role": "tool", "content": payload},
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    assert result["ledger_rows_joined"] == 0
    assert result["exact_seal_ambiguity_count"] == 1
    assert result["exact_seal_ambiguities"][0]["candidate_physical_ids"] == [
        f"m0:0:{len(payload)}",
        f"m1:0:{len(payload)}",
    ]
    assert result["visible_audit_complete"] is False
    assert all(
        entry.get("msg_index") is None
        for entry in result["entries"]
        if entry.get("source") == "ledger_only"
    )


def test_terminal_exit_content_is_not_model_visible_delivery(tmp_path: Path) -> None:
    payload = "src/pkg.py:9 preserve callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(payload)])

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "assistant", "content": "submit"},
                {"role": "exit", "content": payload},
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    assert result["ledger_rows_joined"] == 0
    assert result["gt_blocks_delivered"] == 0
    assert not any(
        entry.get("source") == "trajectory" for entry in result["entries"]
    )


def test_hidden_ledger_file_path_cannot_manufacture_acknowledgment(
    tmp_path: Path,
) -> None:
    payload = "verify this invariant"
    row = _row(payload, layer="obligation.unexercised")
    row["file_path"] = "tests/test_pkg.py"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [row])

    result = build_consumption_ledger(
        {
            "messages": [
                {"role": "tool", "content": payload},
                {
                    "role": "assistant",
                    "content": "I will run the relevant check",
                    "tool_calls": [{"function": {
                        "name": "bash",
                        "arguments": json.dumps({
                            "command": "pytest tests/test_pkg.py",
                        }),
                    }}],
                },
            ]
        },
        runtime_ledger_path=str(ledger),
    )

    entry = next(e for e in result["entries"] if e.get("joined") is True)
    assert entry["file_path"] == "tests/test_pkg.py"
    assert entry["receipt"] == 1
    assert entry["referenced_msg_index"] is None
    assert entry["acted_msg_index"] is None
    assert entry["verification_followup"] is False


def test_distinct_unjoined_visible_tag_fails_authoritative_audit_completeness(
    tmp_path: Path,
) -> None:
    tag = "<gt-contract>legacy distinct payload</gt-contract>"
    native = "src/pkg.py:11 preserve callers"
    content = tag + "\n" + native
    _write_task(tmp_path, content, [_row(native)])

    ledger = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": content}]},
        runtime_ledger_path=str(tmp_path / "gt_runtime_ledger_synthetic.jsonl"),
    )
    assert ledger["unjoined_visible_tag_count"] == 1
    assert ledger["visible_audit_complete"] is False

    record = gt_feature_metrics.collect_task(
        "synthetic__unjoined-visible", str(tmp_path), profile="2"
    )

    assert record["ss_integrity"]["visible_audit_complete"] is False
    assert "visible_audit" in record["ss_integrity"]["missing_required_inputs"]


def test_feature_dose_counts_unique_physical_ids(monkeypatch, tmp_path: Path) -> None:
    _write_task(tmp_path, "one visible payload", [])
    shared = {
        "source": "trajectory",
        "receipt": 1,
        "msg_index": 0,
        "physical_id": "m0:0:19",
    }
    synthetic_ledger = {
        "schema": "gt.consumption_ledger.v2",
        "visible_audit_complete": True,
        "entries": [dict(shared), dict(shared)],
    }
    monkeypatch.setattr(
        gt_feature_metrics,
        "_consumption_by_fact_class",
        lambda _trajectory, _ledger_path: ({}, synthetic_ledger),
    )

    record = gt_feature_metrics.collect_task(
        "synthetic__physical-dose", str(tmp_path), profile="2"
    )

    assert record["integrity"]["max_dose_per_observation"] == 1
    assert record["integrity"]["dose_violation_count"] == 0


def test_partial_overlap_does_not_promote_unsealed_legacy_tag(tmp_path: Path) -> None:
    tag = "<gt-contract>preserve callers and hidden framing</gt-contract>"
    sealed = "preserve callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(sealed)])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": tag}]},
        runtime_ledger_path=str(ledger),
    )

    visible = [entry for entry in result["entries"] if entry["source"] == "trajectory"]
    assert len(visible) == 2
    assert result["unjoined_visible_tag_count"] == 1
    assert result["visible_audit_complete"] is False


def test_merged_owner_recomputes_receipt_and_producer_fields(tmp_path: Path) -> None:
    tag = "<gt-nudge>verify tests/test_pkg.py</gt-nudge>"
    sealed = "\n" + tag
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    row = _row(sealed, layer="obligation.unexercised")
    row["file_path"] = "tests/test_pkg.py"
    _write_ledger(ledger, [row])
    messages = [
        {"role": "tool", "content": "prefix" + sealed},
        {
            "role": "assistant",
            "content": "I will verify tests/test_pkg.py",
            "tool_calls": [{"function": {"name": "bash", "arguments": json.dumps({
                "command": "pytest tests/test_pkg.py",
            })}}],
        },
        {"role": "tool", "content": "1 passed"},
    ]

    result = build_consumption_ledger(
        {"messages": messages}, runtime_ledger_path=str(ledger)
    )
    entry = next(e for e in result["entries"] if e["source"] == "trajectory")

    assert entry["kind"] == "obligation.unexercised"
    assert entry["file_path"] == "tests/test_pkg.py"
    assert entry["receipt"] == 3
    assert entry["referenced_msg_index"] == 1
    assert entry["acted_msg_index"] == 1
    assert entry["verification_followup"] is True


def test_merged_owner_scans_full_sealed_payload_for_leaks(tmp_path: Path) -> None:
    tag = "<gt-contract>preserve callers</gt-contract>"
    sealed = "FAIL_TO_PASS\n" + tag
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(sealed)])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": sealed}]},
        runtime_ledger_path=str(ledger),
    )

    assert result["test_identity_leak_hits"] == ["FAIL_TO_PASS"]


def test_physical_id_is_exact_seal_span_not_legacy_union(tmp_path: Path) -> None:
    tag = "<gt-contract>preserve callers</gt-contract>"
    sealed = "\n" + tag + "\n"
    prefix = "prefix"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, [_row(sealed)])

    result = build_consumption_ledger(
        {"messages": [{"role": "tool", "content": prefix + sealed}]},
        runtime_ledger_path=str(ledger),
    )
    entry = next(e for e in result["entries"] if e["source"] == "trajectory")

    assert entry["physical_id"] == f"m0:{len(prefix)}:{len(prefix + sealed)}"
    assert entry["physical_span_start"] == len(prefix)
    assert entry["physical_span_end"] == len(prefix + sealed)


def test_conflicting_duplicate_physical_ids_fail_visible_audit(
    monkeypatch, tmp_path: Path,
) -> None:
    _write_task(tmp_path, "one visible payload", [])
    common = {
        "source": "trajectory", "msg_index": 0, "physical_id": "m0:0:19",
        "kind": "edit.syntax", "ledger_layer": "edit.syntax",
    }
    synthetic_ledger = {
        "schema": "gt.consumption_ledger.v2",
        "visible_audit_complete": True,
        "entries": [
            {**common, "receipt": 1},
            {**common, "receipt": 3},
        ],
    }
    monkeypatch.setattr(
        gt_feature_metrics,
        "_consumption_by_fact_class",
        lambda _trajectory, _ledger_path: ({}, synthetic_ledger),
    )

    record = gt_feature_metrics.collect_task(
        "synthetic__physical-conflict", str(tmp_path), profile="2"
    )

    assert record["ss_integrity"]["visible_audit_complete"] is False
    assert record["ss_integrity"]["physical_identity_conflict_count"] == 1

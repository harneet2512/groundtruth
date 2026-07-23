from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))

from scripts.swebench.consumption_ledger import build_consumption_ledger
from scripts.swebench.gt_performance_metrics import (
    _entry_fact_class,
    compute_performance_metrics,
)
from scripts.swebench.gt_feature_metrics import (
    _consumption_by_fact_class,
    classify_ledger,
    layer_to_fact_class,
)
from scripts.swebench.gt_behavioral_impact import analyze_trajectory, main as behavioral_main
from scripts.swebench import gt_deep_metrics
from groundtruth.runtime.feature_lineage import build_lineage, lineage_to_dict


def _write_ledger(path: Path, payload: str, *, present_hash: bool = True) -> None:
    row = {
        "layer": "l3.contract",
        "event_type": "file_view",
        "iteration": 1,
        "outcome": "delivered",
        "chars_delivered": len(payload),
        "content_sha256_16": (
            hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
            if present_hash else None
        ),
        "file_path": "src/pkg.py",
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_tagless_native_read_is_referenced_not_acted(tmp_path: Path) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, payload)
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "I will inspect the implementation."},
            {"role": "tool", "content": "command output\n" + payload},
            {
                "role": "assistant",
                "content": "The pkg.py caller contract applies.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "sed -n '1,80p' src/pkg.py"}
                )}}],
            },
        ]
    }

    result = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger)
    )

    assert result["gt_blocks_delivered"] == 1
    assert result["gt_blocks_referenced"] == 1
    assert result["gt_blocks_consumed"] == 0
    assert result["ledger_rows_delivered"] == 1
    assert result["ledger_rows_joined"] == 1
    entry = result["entries"][0]
    assert entry["source"] == "trajectory"
    assert entry["joined"] is True
    assert entry["join_method"] == "seal"
    assert entry["content_sha256_16"] == hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()[:16]
    assert entry["ledger_layer"] == "l3.contract"
    assert entry["receipt"] == 2


def test_responses_content_arrays_and_function_calls_count_as_assistant_action(
    tmp_path: Path,
) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, payload)
    trajectory = {
        "messages": [
            {"role": "tool", "content": [{"type": "output_text", "text": payload}]},
            {
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I will update pkg.py."}],
                "output": [{
                    "type": "function_call", "name": "shell",
                    "arguments": json.dumps({"command": "sed -i 's/old/new/' src/pkg.py"}),
                }],
            },
        ]
    }
    result = build_consumption_ledger(trajectory, runtime_ledger_path=str(ledger))
    assert result["gt_blocks_referenced"] == 1
    assert result["gt_blocks_consumed"] == 1
    assert result["entries"][0]["receipt"] == 3


def test_ledger_delivered_without_exact_observation_bytes_stays_dark(tmp_path: Path) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, payload)
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "I will inspect the implementation."},
            {"role": "tool", "content": "ordinary command output only"},
        ]
    }

    result = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger)
    )

    assert result["gt_blocks_delivered"] == 0
    assert result["gt_blocks_referenced"] == 0
    assert result["gt_blocks_consumed"] == 0
    assert result["ledger_rows_delivered"] == 1
    assert result["ledger_rows_joined"] == 0
    assert result["join_rate"] == 0.0
    assert result["entries"][0]["source"] == "ledger_only"
    assert result["entries"][0]["receipt"] is None

    impact = analyze_trajectory(trajectory, consumption_ledger=result)["summary"]
    assert impact["total_deliveries"] == 0
    assert impact["gt_tokens_injected"] == 0
    assert impact["causal_status"] == "UNMEASURED"


def test_performance_metrics_use_native_receipts_not_retired_tags(tmp_path: Path) -> None:
    contract = "src/pkg.py:12: preserve parse_config callers"
    obligation = "Run the covering verification for src/pkg.py before completion."
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    rows = [
        {
            "layer": "l3.contract", "event_type": "file_view", "iteration": 1,
            "outcome": "delivered", "chars_delivered": len(contract),
            "content_sha256_16": hashlib.sha256(contract.encode()).hexdigest()[:16],
            "file_path": "src/pkg.py",
        },
        {
            "layer": "spec.obligation", "event_type": "test_result", "iteration": 2,
            "outcome": "delivered", "chars_delivered": len(obligation),
            "content_sha256_16": hashlib.sha256(obligation.encode()).hexdigest()[:16],
            "file_path": "src/pkg.py",
        },
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "Inspecting."},
            {"role": "tool", "content": "view output\n" + contract},
            {
                "role": "assistant", "content": "I will preserve pkg.py callers.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "sed -i 's/old/new/' src/pkg.py"}
                )}}],
            },
            {"role": "tool", "content": "edit output\n" + obligation},
            {
                "role": "assistant", "content": "I will verify pkg.py now.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "pytest -q"}
                )}}],
            },
            {"role": "tool", "content": "1 passed"},
        ],
        "info": {"model_stats": {"api_calls": 3, "prompt_tokens": 1000}},
    }
    traj = tmp_path / "mini-swe-agent.trajectory.json"
    traj.write_text(json.dumps(trajectory), encoding="utf-8")

    result = compute_performance_metrics(str(traj), str(tmp_path), gold_files=[])

    assert result["interface_preservation"]["_contract_warning_count"] == 1
    assert result["verify_before_submit"]["obligation_test_rate"] == 1.0
    assert result["gt_attribution"]["contract_consulted_rate"] == 1.0
    assert result["gt_attribution"]["obligation_completion_rate"] == 1.0
    expected_tokens = round((len(contract) + len(obligation)) / 4.0, 8)
    assert result["token_efficiency"]["_gt_injected_tokens_est"] == expected_tokens


def test_feature_metrics_inherit_native_receipt_level(tmp_path: Path) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, payload)
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "Inspecting."},
            {"role": "tool", "content": payload},
            {
                "role": "assistant", "content": "The pkg.py contract applies.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "sed -i 's/old/new/' src/pkg.py"}
                )}}],
            },
        ]
    }

    by_fact, full = _consumption_by_fact_class(trajectory, str(ledger))

    assert full["ledger_rows_joined"] == 1
    assert by_fact["caller_contract"]["delivered"] == 1
    assert by_fact["caller_contract"]["referenced"] == 1
    assert by_fact["caller_contract"]["acted"] == 1
    assert by_fact["caller_contract"]["max_level"] == 3


def test_behavioral_impact_counts_native_bytes_without_claiming_causality(tmp_path: Path) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, payload)
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "Searching."},
            {"role": "tool", "content": payload},
            {
                "role": "assistant", "content": "The pkg.py contract applies.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "sed -i 's/old/new/' src/pkg.py"}
                )}}],
            },
        ]
    }
    receipts = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger)
    )

    result = analyze_trajectory(trajectory, consumption_ledger=receipts)
    summary = result["summary"]

    assert summary["total_deliveries"] == 1
    assert summary["gt_tokens_injected"] == len(payload)
    assert summary["gt_tokens_injected_units"] == "rendered_chars_exact"
    assert summary["per_tag"]["caller_contract"]["total"] == 1
    assert summary["per_tag_impact"] == summary["per_tag"]
    assert summary["impact_rate"] == 1.0
    assert summary["impact_rate_semantics"] == "diagnostic_action_type_transition"
    assert summary["causal_status"] == "UNMEASURED"
    assert summary["nudge_compliance_rate"] is None


def test_behavioral_impact_excludes_unjoined_trajectory_entry_when_ledger_exists() -> None:
    trajectory = {
        "messages": [
            {"role": "tool", "content": "<gt-contract>unsealed text</gt-contract>"},
            {"role": "assistant", "content": "I will edit it."},
        ]
    }
    receipts = {
        "runtime_ledger_path": "gt_runtime_ledger_task.jsonl",
        "ledger_rows_delivered": 1,
        "entries": [{
            "source": "trajectory", "receipt": 1, "joined": False,
            "msg_index": 0, "kind": "contract", "ledger_layer": None,
            "chars": len("<gt-contract>unsealed text</gt-contract>"),
        }],
    }

    result = analyze_trajectory(trajectory, consumption_ledger=receipts)

    assert result["summary"]["total_deliveries"] == 0
    assert result["summary"]["gt_tokens_injected"] == 0


def test_behavioral_impact_normalizes_native_runtime_layer() -> None:
    trajectory = {
        "messages": [
            {"role": "tool", "content": "syntax output"},
            {"role": "assistant", "content": "I will repair the syntax."},
        ]
    }
    receipts = {
        "runtime_ledger_path": "gt_runtime_ledger_task.jsonl",
        "ledger_rows_delivered": 1,
        "entries": [{
            "source": "trajectory", "receipt": 2, "joined": True,
            "msg_index": 0, "kind": "edit.syntax", "ledger_layer": "edit.syntax",
            "chars": len("syntax output"),
        }],
    }

    result = analyze_trajectory(trajectory, consumption_ledger=receipts)

    assert result["summary"]["per_tag_impact"]["syntax_result"]["total"] == 1


def test_covering_red_requires_typed_executed_covering_runner_ownership(
    tmp_path: Path,
) -> None:
    advisory = "Run a focused verification before completion."
    executed = "$ pytest -q tests/unit\nFAILED tests/unit\n[exit 1]"
    lineage = build_lineage(
        runtime_producer_id="covering_runner",
        evidence_type="covering_verdict",
        actual_event="test_result",
    )
    assert lineage is not None
    ledger_lineage = lineage_to_dict(lineage)
    ledger_lineage["lineage_schema"] = ledger_lineage.pop("schema")
    ledger_lineage["feature_ids"] = ledger_lineage.pop("features")
    rows = [
        {
            "layer": "verify.horizon.advisory",
            "event_type": "review_transition",
            "iteration": 1,
            "outcome": "delivered",
            "chars_delivered": len(advisory),
            "content_sha256_16": hashlib.sha256(advisory.encode()).hexdigest()[:16],
            "file_path": "src/pkg.py",
            **ledger_lineage,
        },
        {
            "layer": "verify.horizon.executed",
            "event_type": "test_result",
            "iteration": 2,
            "outcome": "delivered",
            "chars_delivered": len(executed),
            "content_sha256_16": hashlib.sha256(executed.encode()).hexdigest()[:16],
            "file_path": "src/pkg.py",
            **ledger_lineage,
        },
    ]
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    ledger.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    trajectory = {
        "messages": [
            {"role": "tool", "content": advisory},
            {"role": "assistant", "content": "I will run the focused test."},
            {"role": "tool", "content": executed},
            {"role": "assistant", "content": "The covering failure identifies the repair."},
        ]
    }

    receipts = build_consumption_ledger(
        trajectory, runtime_ledger_path=str(ledger)
    )
    by_fact, _full = _consumption_by_fact_class(trajectory, str(ledger))
    covering_entries = [
        entry for entry in receipts["entries"]
        if _entry_fact_class(entry) == "covering_red"
    ]
    impact = analyze_trajectory(trajectory, consumption_ledger=receipts)["summary"]

    assert layer_to_fact_class("verify.horizon.advisory") is None
    assert layer_to_fact_class("verify.horizon.executed") is None
    assert classify_ledger(rows)["covering_red"]["delivered"] == 1
    assert by_fact["covering_red"]["delivered"] == 1
    assert len(covering_entries) == 1
    assert covering_entries[0]["ledger_layer"] == "verify.horizon.executed"
    assert impact["per_tag_impact"]["covering_red"]["total"] == 1
    assert impact["per_tag_impact"]["verify.horizon.advisory"]["total"] == 1


def test_deep_metrics_behavioral_impact_uses_shared_native_receipts(
    tmp_path: Path, monkeypatch,
) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    trajectory = {
        "messages": [
            {"role": "assistant", "content": "Searching."},
            {"role": "tool", "content": payload},
            {
                "role": "assistant",
                "content": "The pkg.py contract applies.",
                "tool_calls": [{"function": {"arguments": json.dumps(
                    {"command": "sed -i 's/old/new/' src/pkg.py"}
                )}}],
            },
        ]
    }
    traj = tmp_path / "mini-swe-agent.trajectory.json"
    traj.write_text(json.dumps(trajectory), encoding="utf-8")
    ledger = tmp_path / "gt_runtime_ledger_task.jsonl"
    _write_ledger(ledger, payload)
    monkeypatch.setattr(
        gt_deep_metrics, "_find_miniswe_trajectory", lambda _task, _results: str(traj)
    )

    result = gt_deep_metrics.build("task", str(tmp_path))
    behavioral = result["behavioral_impact"]

    assert behavioral["total_deliveries"] == 1
    assert behavioral["gt_tokens_injected"] == len(payload)
    assert behavioral["gt_tokens_injected_units"] == "rendered_chars_exact"
    assert behavioral["impact_rate_semantics"] == "diagnostic_action_type_transition"
    assert behavioral["causal_status"] == "UNMEASURED"
    assert behavioral["nudge_compliance_rate"] is None
    applicability = result["metric_applicability"]
    assert applicability["behavioral_impact"]["per_tag_impact"]["applicable"] is True
    assert applicability["behavioral_impact"]["gt_tokens_per_pivot"]["applicable"] is True
    assert applicability["behavioral_impact"]["nudge_compliance_rate"]["applicable"] is False
    assert applicability["localization"]["steps_to_gold_view"]["applicable"] is False


def test_behavioral_impact_cli_auto_joins_sibling_runtime_ledger(
    tmp_path: Path, monkeypatch,
) -> None:
    payload = "src/pkg.py:12: preserve parse_config callers"
    traj = tmp_path / "mini-swe-agent.trajectory.json"
    traj.write_text(json.dumps({
        "messages": [
            {"role": "assistant", "content": "Searching."},
            {"role": "tool", "content": payload},
            {"role": "assistant", "tool_calls": [{"function": {"arguments": json.dumps(
                {"command": "sed -i 's/old/new/' src/pkg.py"}
            )}}]},
        ]
    }), encoding="utf-8")
    _write_ledger(tmp_path / "gt_runtime_ledger_task.jsonl", payload)
    output = tmp_path / "behavioral.json"
    monkeypatch.setattr(
        sys, "argv", ["gt_behavioral_impact.py", str(traj), "--out", str(output)]
    )

    behavioral_main()

    summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
    assert summary["total_deliveries"] == 1
    assert summary["gt_tokens_injected"] == len(payload)

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_deep_metrics as deep_metrics  # noqa: E402
import gt_performance_metrics as performance  # noqa: E402


def _task_truth_module():
    path = ROOT / "scripts" / "swebench" / "task_truth.py"
    spec = importlib.util.spec_from_file_location("verifier_truth_task_truth", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task_root(
    tmp_path: Path, task: str, report_entry: dict, *, submission: str = "",
) -> Path:
    root = tmp_path / task
    root.mkdir()
    (root / "report.json").write_text(
        json.dumps({task: report_entry}), encoding="utf-8"
    )
    (root / "reward.txt").write_text("0\n", encoding="utf-8")
    (root / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({
            "messages": [],
            "info": {
                "instance_id": task,
                "exit_status": "Submitted",
                "model_stats": {"api_calls": 1},
                "submission": submission,
            },
        }),
        encoding="utf-8",
    )
    return root


def test_task_truth_derives_count_only_p2p_truth_from_official_report(
    tmp_path: Path, monkeypatch,
) -> None:
    task = "org__repo-1"
    root = _task_root(tmp_path, task, {
        "resolved": False,
        "tests_status": {
            "PASS_TO_PASS": {
                "success": ["tests/test_api.py::test_a", "tests/test_api.py::test_b"],
                "failure": ["tests/test_api.py::test_c"],
            },
            "FAIL_TO_PASS": {"success": [], "failure": ["tests/test_fix.py::test_d"]},
        },
    })
    monkeypatch.setenv("GT_INSTANCE_ID", task)
    monkeypatch.setenv("GT_MATRIX_TASK", task)

    truth = _task_truth_module().build_task_truth(str(root), instance_id=task)

    assert truth["verifier_truth"] == {
        "schema": "gt.verifier_truth.v1",
        "authority": "official_swebench_report.tests_status.PASS_TO_PASS",
        "source_present": True,
        "valid": True,
        "p2p_total": 3,
        "p2p_failed": 1,
        "caller_breakage_count": None,
        "caller_breakage_unmeasured_reason": "caller_aware_verifier_join_absent",
        "caller_joined_failures": 0,
        "caller_join_complete": False,
    }
    # Test identifiers are evaluator-only inputs; the truth artifact exposes counts only.
    assert "test_api" not in json.dumps(truth["verifier_truth"])


def test_task_truth_keeps_malformed_or_ambiguous_p2p_unmeasured(
    tmp_path: Path, monkeypatch,
) -> None:
    task = "org__repo-2"
    root = _task_root(tmp_path, task, {
        "resolved": False,
        "tests_status": {
            "PASS_TO_PASS": {"success": ["duplicate"], "failure": ["duplicate"]},
        },
    })
    monkeypatch.setenv("GT_INSTANCE_ID", task)
    monkeypatch.setenv("GT_MATRIX_TASK", task)

    verifier = _task_truth_module().build_task_truth(
        str(root), instance_id=task
    )["verifier_truth"]

    assert verifier["valid"] is False
    assert verifier["p2p_total"] is None
    assert verifier["p2p_failed"] is None
    assert verifier["caller_breakage_count"] is None


def test_verifier_truth_zero_failures_proves_zero_caller_breakages() -> None:
    task = "org__repo-3"
    verifier = _task_truth_module()._build_verifier_truth({
        task: {
            "tests_status": {
                "PASS_TO_PASS": {"success": ["tests/test_api.py::test_ok"], "failure": []},
            },
        },
    }, task)

    assert verifier["caller_breakage_count"] == 0
    assert "caller_breakage_unmeasured_reason" not in verifier


def test_verifier_truth_counts_only_graph_joined_failed_callers(tmp_path: Path) -> None:
    graph = tmp_path / "graph.db"
    with sqlite3.connect(graph) as connection:
        connection.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test BOOLEAN
            );
            CREATE TABLE edges (
                source_id INTEGER, target_id INTEGER,
                resolution_method TEXT, confidence REAL
            );
            INSERT INTO nodes VALUES
                (1, 'test_changed_api', 'tests/test_api.py', 1),
                (2, 'changed_api', 'src/api.py', 0);
            INSERT INTO edges VALUES (1, 2, 'import', 1.0);
        """)
    task = "org__repo-4"
    verifier = _task_truth_module()._build_verifier_truth(
        {
            task: {
                "tests_status": {
                    "PASS_TO_PASS": {
                        "success": [],
                        "failure": ["tests/test_api.py::test_changed_api[param]"],
                    },
                },
            },
        },
        task,
        graph_path=str(graph),
        changed_paths={"src/api.py"},
    )

    assert verifier["caller_breakage_count"] == 1
    assert verifier["caller_joined_failures"] == 1
    assert verifier["caller_join_complete"] is True
    assert "test_changed_api" not in json.dumps(verifier)


def test_verifier_truth_rejects_candidate_or_unedited_caller_edges(tmp_path: Path) -> None:
    graph = tmp_path / "graph.db"
    with sqlite3.connect(graph) as connection:
        connection.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test BOOLEAN
            );
            CREATE TABLE edges (
                source_id INTEGER, target_id INTEGER,
                resolution_method TEXT, confidence REAL
            );
            INSERT INTO nodes VALUES
                (1, 'test_api', 'tests/test_api.py', 1),
                (2, 'api', 'src/unedited.py', 0),
                (3, 'api_guess', 'src/api.py', 0);
            INSERT INTO edges VALUES
                (1, 2, 'import', 1.0),
                (1, 3, 'name_match', 0.95);
        """)
    task = "org__repo-candidate"
    verifier = _task_truth_module()._build_verifier_truth(
        {task: {"tests_status": {"PASS_TO_PASS": {
            "success": [], "failure": ["tests/test_api.py::test_api"],
        }}}},
        task,
        graph_path=str(graph),
        changed_paths={"src/api.py"},
    )

    assert verifier["caller_breakage_count"] is None
    assert verifier["caller_join_complete"] is False


def test_task_truth_plumbs_patch_and_task_graph_into_caller_join(
    tmp_path: Path, monkeypatch,
) -> None:
    task = "org__repo-5"
    root = _task_root(
        tmp_path,
        task,
        {
            "resolved": False,
            "tests_status": {
                "PASS_TO_PASS": {
                    "success": [],
                    "failure": ["tests/test_api.py::test_changed_api"],
                },
            },
        },
        submission=(
            "diff --git a/src/api.py b/src/api.py\n"
            "--- a/src/api.py\n+++ b/src/api.py\n"
            "@@ -1 +1 @@\n-old = 1\n+new = 1\n"
        ),
    )
    with sqlite3.connect(root / "graph.db") as connection:
        connection.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test BOOLEAN
            );
            CREATE TABLE edges (
                source_id INTEGER, target_id INTEGER,
                resolution_method TEXT, confidence REAL
            );
            INSERT INTO nodes VALUES
                (1, 'test_changed_api', 'tests/test_api.py', 1),
                (2, 'changed_api', 'src/api.py', 0);
            INSERT INTO edges VALUES (1, 2, 'import', 1.0);
        """)
    monkeypatch.setenv("GT_INSTANCE_ID", task)
    monkeypatch.setenv("GT_MATRIX_TASK", task)

    verifier = _task_truth_module().build_task_truth(
        str(root), instance_id=task,
    )["verifier_truth"]

    assert verifier["caller_breakage_count"] == 1
    assert verifier["caller_join_complete"] is True


def test_deep_and_standalone_performance_share_canonical_verifier_truth(
    tmp_path: Path,
) -> None:
    task = "probe"
    task_root = tmp_path / task
    task_root.mkdir()
    trajectory = task_root / "mini-swe-agent.trajectory.json"
    trajectory.write_text(
        json.dumps({"messages": [], "info": {"model_stats": {}, "submission": ""}}),
        encoding="utf-8",
    )
    truth = {
        "verifier_truth": {
            "schema": "gt.verifier_truth.v1",
            "authority": "official_swebench_report.tests_status.PASS_TO_PASS",
            "source_present": True,
            "valid": True,
            "p2p_total": 4,
            "p2p_failed": 1,
            "caller_breakage_count": None,
            "caller_breakage_unmeasured_reason": "caller_aware_verifier_join_absent",
        }
    }
    (tmp_path / "task_truth.json").write_text(json.dumps(truth), encoding="utf-8")

    standalone = performance.compute_performance_metrics(
        str(trajectory), str(tmp_path), gold_files=["src/x.py"],
        consumption_ledger={
            "schema": "gt.consumption_ledger.v2",
            "runtime_ledger_path": "ledger",
            "entries": [],
        },
        verifier_truth=truth,
    )
    interface = standalone["interface_preservation"]
    deep = deep_metrics.build(task, str(tmp_path))
    deep_interface = deep["performance"]["interface_preservation"]

    assert interface["p2p_regression_rate"] == 0.25
    assert interface["caller_breakage_count"] is None
    assert deep_interface["p2p_regression_rate"] == interface["p2p_regression_rate"]
    assert deep_interface["caller_breakage_count"] is interface["caller_breakage_count"]
    assert deep_metrics._verifier_interface_metrics(truth) == {
        "p2p_regression_rate": 0.25,
        "caller_breakage_count": None,
    }


def test_standalone_cli_reads_the_explicit_task_truth(
    tmp_path: Path, monkeypatch,
) -> None:
    trajectory = tmp_path / "mini-swe-agent.trajectory.json"
    trajectory.write_text(
        json.dumps({"messages": [], "info": {"model_stats": {}, "submission": ""}}),
        encoding="utf-8",
    )
    truth_path = tmp_path / "task_truth.json"
    truth_path.write_text(json.dumps({
        "verifier_truth": {
            "schema": "gt.verifier_truth.v1",
            "valid": True,
            "p2p_total": 5,
            "p2p_failed": 2,
            "caller_breakage_count": None,
        },
    }), encoding="utf-8")
    out = tmp_path / "performance.json"
    monkeypatch.setattr(sys, "argv", [
        "gt_performance_metrics.py", str(trajectory), str(tmp_path),
        "--task-truth", str(truth_path), "--out", str(out),
    ])

    assert performance.main() == 0
    interface = json.loads(out.read_text(encoding="utf-8"))["interface_preservation"]
    assert interface["p2p_regression_rate"] == 0.4
    assert interface["caller_breakage_count"] is None

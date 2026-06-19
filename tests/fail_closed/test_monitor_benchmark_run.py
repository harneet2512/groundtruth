import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "monitor_benchmark_run.py"
SPEC = importlib.util.spec_from_file_location("monitor_benchmark_run_under_test", SCRIPT)
assert SPEC and SPEC.loader
MON = importlib.util.module_from_spec(SPEC)
sys.modules["monitor_benchmark_run_under_test"] = MON
SPEC.loader.exec_module(MON)


def test_monitor_marks_github_failures_invalid_actionable():
    validity, diagnosis = MON.validity_signal(
        failure=1,
        counts={"RESOLVED": 0, "AGENT": 0, "GT": 0, "INFRA": 0, "UNKNOWN": 0},
    )

    assert validity == "INVALID_ACTIONABLE"
    assert diagnosis == "failure=1"


def test_monitor_marks_infra_and_unknown_invalid_actionable():
    validity, diagnosis = MON.validity_signal(
        failure=0,
        counts={"RESOLVED": 0, "AGENT": 0, "GT": 0, "INFRA": 2, "UNKNOWN": 1},
    )

    assert validity == "INVALID_ACTIONABLE"
    assert "INFRA=2" in diagnosis
    assert "UNKNOWN=1" in diagnosis


def test_monitor_observes_clean_incomplete_run():
    validity, diagnosis = MON.validity_signal(
        failure=0,
        counts={"RESOLVED": 1, "AGENT": 1, "GT": 0, "INFRA": 0, "UNKNOWN": 0},
    )

    assert validity == "OBSERVE"
    assert diagnosis == "-"

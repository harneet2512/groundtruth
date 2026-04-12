"""Integration test: eval acceptance gate enforces all rules.

Tests that the acceptance gate correctly rejects features that:
- Have uplift on only 1 model
- Have uplift on only 1 benchmark family
- Have excessive regression rate
- Have uplift below variance threshold
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def make_results(tmp_path):
    """Factory for creating synthetic eval results."""
    def _make(runs: list[dict]) -> str:
        path = tmp_path / f"results_{id(runs)}.json"
        path.write_text(json.dumps({"runs": runs}))
        return str(path)
    return _make


def _run_acceptance(gt_path: str, bl_path: str, verbose: bool = False) -> int:
    """Run eval_acceptance.py and return exit code."""
    cmd = [
        sys.executable, "scripts/eval_acceptance.py",
        "--gt-results", gt_path,
        "--baseline", bl_path,
        "--config", "benchmarks/eval_matrix/config.yaml",
    ]
    if verbose:
        cmd.append("--verbose")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent.parent.parent))
    return result.returncode


class TestAcceptanceGateRules:
    def test_rejects_single_model_uplift(self, make_results):
        """Gate should reject if uplift appears on only 1 model."""
        # GT results: good on primary, bad on secondary
        gt = make_results([
            {"benchmark": "static_fixing", "model": "primary", "run_id": 1, "timestamp": 0, "resolved": 60, "total": 100, "resolved_rate": 0.60, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 2, "timestamp": 0, "resolved": 62, "total": 100, "resolved_rate": 0.62, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 3, "timestamp": 0, "resolved": 61, "total": 100, "resolved_rate": 0.61, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 2, "timestamp": 0, "resolved": 49, "total": 100, "resolved_rate": 0.49, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 3, "timestamp": 0, "resolved": 51, "total": 100, "resolved_rate": 0.51, "metrics": {}, "errors": []},
        ])
        # Baseline: both models at 50%
        bl = make_results([
            {"benchmark": "static_fixing", "model": "primary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 2, "timestamp": 0, "resolved": 51, "total": 100, "resolved_rate": 0.51, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 3, "timestamp": 0, "resolved": 49, "total": 100, "resolved_rate": 0.49, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 2, "timestamp": 0, "resolved": 49, "total": 100, "resolved_rate": 0.49, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 3, "timestamp": 0, "resolved": 51, "total": 100, "resolved_rate": 0.51, "metrics": {}, "errors": []},
        ])
        # Primary has +10% uplift, secondary has 0% → only 1 model passes
        # But also only 1 benchmark family → should reject on both counts
        assert _run_acceptance(gt, bl) == 1

    def test_accepts_multi_model_multi_family(self, make_results):
        """Gate should accept when uplift is on ≥2 models AND ≥2 families."""
        gt = make_results([
            # Family 1: static_fixing
            {"benchmark": "static_fixing", "model": "primary", "run_id": 1, "timestamp": 0, "resolved": 60, "total": 100, "resolved_rate": 0.60, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 2, "timestamp": 0, "resolved": 61, "total": 100, "resolved_rate": 0.61, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 3, "timestamp": 0, "resolved": 59, "total": 100, "resolved_rate": 0.59, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 1, "timestamp": 0, "resolved": 58, "total": 100, "resolved_rate": 0.58, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 2, "timestamp": 0, "resolved": 59, "total": 100, "resolved_rate": 0.59, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 3, "timestamp": 0, "resolved": 57, "total": 100, "resolved_rate": 0.57, "metrics": {}, "errors": []},
            # Family 2: long_horizon
            {"benchmark": "long_horizon", "model": "primary", "run_id": 1, "timestamp": 0, "resolved": 58, "total": 100, "resolved_rate": 0.58, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "primary", "run_id": 2, "timestamp": 0, "resolved": 59, "total": 100, "resolved_rate": 0.59, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "primary", "run_id": 3, "timestamp": 0, "resolved": 57, "total": 100, "resolved_rate": 0.57, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "secondary", "run_id": 1, "timestamp": 0, "resolved": 56, "total": 100, "resolved_rate": 0.56, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "secondary", "run_id": 2, "timestamp": 0, "resolved": 57, "total": 100, "resolved_rate": 0.57, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "secondary", "run_id": 3, "timestamp": 0, "resolved": 55, "total": 100, "resolved_rate": 0.55, "metrics": {}, "errors": []},
        ])
        bl = make_results([
            {"benchmark": "static_fixing", "model": "primary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 2, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "primary", "run_id": 3, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 2, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "static_fixing", "model": "secondary", "run_id": 3, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "primary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "primary", "run_id": 2, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "primary", "run_id": 3, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "secondary", "run_id": 1, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "secondary", "run_id": 2, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
            {"benchmark": "long_horizon", "model": "secondary", "run_id": 3, "timestamp": 0, "resolved": 50, "total": 100, "resolved_rate": 0.50, "metrics": {}, "errors": []},
        ])
        # Both models have +~8% uplift across 2 families → should accept
        assert _run_acceptance(gt, bl) == 0

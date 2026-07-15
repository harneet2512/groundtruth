from __future__ import annotations

import math

import numpy as np

from groundtruth.memory.enrich.embed import _deterministic_session_options
from groundtruth.pretask.v7_4_brief import (
    _canonicalize_fusion_components,
    _rrf_fuse,
)


class _FakeSessionOptions:
    def __init__(self) -> None:
        self.execution_mode = None
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.entries[key] = value


class _FakeOrt:
    class ExecutionMode:
        ORT_SEQUENTIAL = "sequential"

    SessionOptions = _FakeSessionOptions


def test_shared_onnx_session_options_are_deterministic() -> None:
    options = _deterministic_session_options(_FakeOrt)

    assert options.execution_mode == _FakeOrt.ExecutionMode.ORT_SEQUENTIAL
    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert options.entries == {"session.use_deterministic_compute": "1"}


def _fused_semantic_scores(value_for_b: float) -> dict[str, float]:
    raw = {
        "a.py": {"sem": 0.5000003},
        "b.py": {"sem": value_for_b},
    }
    canonical = _canonicalize_fusion_components(raw)
    return _rrf_fuse(canonical, ["a.py", "b.py"], ("sem", "lex"))


def test_sub_quantum_alternating_noise_has_one_fusion_identity() -> None:
    below_a = _fused_semantic_scores(0.5000002)
    below_b = _fused_semantic_scores(0.5000004)

    assert below_a == below_b
    assert below_a["a.py"] > below_a["b.py"]  # canonical path breaks the tie


def test_supra_quantum_change_remains_observable() -> None:
    below = _fused_semantic_scores(0.5000004)
    above = _fused_semantic_scores(0.5000006)

    assert below != above
    assert below["a.py"] > below["b.py"]
    assert above["b.py"] > above["a.py"]


def test_canonicalization_copies_and_rounds_only_finite_components() -> None:
    raw = {
        "a.py": {
            "sem": 0.12345649,
            "numpy_sem": np.float32(0.7654324),
            "sentinel": math.inf,
        }
    }
    canonical = _canonicalize_fusion_components(raw)

    assert canonical == {
        "a.py": {
            "sem": 0.123456,
            "numpy_sem": 0.765432,
            "sentinel": math.inf,
        }
    }
    assert canonical is not raw
    assert canonical["a.py"] is not raw["a.py"]
    assert raw["a.py"]["sem"] == 0.12345649

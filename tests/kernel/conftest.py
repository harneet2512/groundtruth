"""Shared kernel-test fixtures.

Loads JSON fixtures from ``tests/kernel/fixtures/<scenario>/`` and exposes a
``MockGraphHandle`` for graph-validation tests so they run without a real
``graph.db``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict

from groundtruth.control.types import GraphHandle

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


class MockGraphHandle(GraphHandle):
    """Inline-JSON-backed graph handle for unit tests."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    callers_of: dict[str, list[dict[str, Any]]] = {}


def load_fixture(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    base = FIXTURES_ROOT / name
    input_data = json.loads((base / "input.json").read_text(encoding="utf-8"))
    expected = json.loads((base / "expected.json").read_text(encoding="utf-8"))
    return input_data, expected


@pytest.fixture
def fixture_loader():
    return load_fixture


@pytest.fixture
def mock_graph_factory():
    def _build(spec: dict[str, Any]) -> MockGraphHandle:
        return MockGraphHandle(
            kind="mock",
            graph_db_sha=spec.get("graph_db_sha", "test-sha"),
            nodes=spec.get("nodes", []),
            edges=spec.get("edges", []),
            callers_of=spec.get("callers_of", {}),
        )

    return _build

import json
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--trajectory-dir", action="store", default=None, help="Root directory containing per-task trajectory artifacts")
    parser.addoption("--graph-dir", action="store", default=None, help="Optional graph/index root for L1/L6 checks")
    parser.addoption("--baselines-file", action="store", default="tests/behavioral/baselines.json", help="Behavioral baseline JSON")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unverified(reason): marker for data-dependent checks")


@pytest.fixture(scope="session")
def trajectory_dir(pytestconfig: pytest.Config) -> Path:
    raw = pytestconfig.getoption("trajectory_dir")
    if not raw:
        pytest.skip("UNVERIFIED: --trajectory-dir not provided")
    p = Path(raw)
    if not p.exists():
        pytest.skip(f"UNVERIFIED: trajectory dir not found: {p}")
    return p


@pytest.fixture(scope="session")
def graph_dir(pytestconfig: pytest.Config) -> Path | None:
    raw = pytestconfig.getoption("graph_dir")
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


@pytest.fixture(scope="session")
def baselines(pytestconfig: pytest.Config) -> dict:
    p = Path(pytestconfig.getoption("baselines_file"))
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))

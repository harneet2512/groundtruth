"""End-to-end tests for the v7 edit-plan brief."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from groundtruth.pretask.v7_brief import V7BriefResult, generate_brief


ISSUE = """SafeWatchdog._fd fails on shutdown.

```python
watchdog.activate()
```

See `patroni/watchdog.py`.
"""


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=False)
        return True
    except OSError:
        return False


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_v7_brief_renders_cluster_contract_constraints_and_logs(
    tiny_graph_db: str, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "patroni").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "patroni" / "watchdog.py").write_text(
        "class SafeWatchdog:\n    def activate(self):\n        self._fd.write(b'x')\n",
        encoding="utf-8",
    )
    (repo / "patroni" / "postmaster.py").write_text(
        "class Postmaster:\n    pass\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_watchdog.py").write_text(
        "from patroni.watchdog import SafeWatchdog\n\n"
        "def test_watchdog_fires():\n"
        "    assert SafeWatchdog\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "Run pytest tests/test_watchdog.py for watchdog changes.\n"
        "Do not edit generated files.\n",
        encoding="utf-8",
    )
    _init_repo(repo)
    _commit(repo, "initial watchdog")
    (repo / "patroni" / "watchdog.py").write_text(
        "class SafeWatchdog:\n    def activate(self):\n        self._fd.write(b'y')\n",
        encoding="utf-8",
    )
    (repo / "patroni" / "postmaster.py").write_text(
        "class Postmaster:\n    def stop(self):\n        pass\n",
        encoding="utf-8",
    )
    _commit(repo, "fix watchdog shutdown")

    result = generate_brief(
        ISSUE,
        str(repo),
        tiny_graph_db,
        task_id="v7_schema",
        log_dir=str(tmp_path / "logs"),
        return_telemetry=True,
    )

    assert isinstance(result, V7BriefResult)
    assert "CANDIDATE CLUSTER:" in result.brief
    assert "CONTRACT:" in result.brief
    assert "IMPLEMENTATION PATTERN:" in result.brief
    assert "EXPECTED SIDE FILES:" in result.brief
    assert "CONSTRAINTS:" in result.brief
    assert "patroni/watchdog.py" in result.brief
    assert "patroni/postmaster.py" in result.brief
    assert "Do not add throwaway scaffolding at the repo root" in result.brief
    assert "Repo validation hint from AGENTS.md" in result.brief

    rec = result.telemetry.as_dict()
    assert rec["version"] == "v7.0"
    assert rec["module_7_cochange"]["enabled"] is True
    assert rec["module_7_contract"]["enabled"] is True
    assert rec["module_7_constraints"]["enabled"] is True
    assert rec["module_7_constraints"]["project_instruction_sources"] == ["AGENTS.md"]
    assert rec["module_7_cochange"]["cluster_files"]
    assert rec["module_7_contract"]["contract_lines"]

    assert result.telemetry_path is not None
    assert result.telemetry_path.endswith("_v7_brief.jsonl")
    assert result.plan_path is not None
    assert result.plan["cluster_files"]
    assert result.plan["agent_focus_files"]
    assert len(result.plan["agent_focus_files"]) <= 3
    assert result.plan["contract_lines"]
    assert result.plan["expected_side_files"]
    assert result.plan["confidence"] > 0
    assert len(result.brief) <= 3500
    parsed = json.loads(Path(result.telemetry_path).read_text(encoding="utf-8").splitlines()[-1])
    assert parsed["brief_text"] == result.brief
    assert parsed["gt_plan"]["cluster_files"] == result.plan["cluster_files"]
    assert parsed["module_7_constraints"]["hook_warning_fired"] is False
    plan_disk = json.loads(Path(result.plan_path).read_text(encoding="utf-8"))
    assert plan_disk["task_id"] == "v7_schema"
    runtime_records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "gt_runtime_telemetry.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    delivery = [record for record in runtime_records if record.get("block") == "gt_usable_delivery"]
    assert delivery[-1]["gt_usable_delivery"]["usable_delivery_ok"] is True
    project = [
        record for record in runtime_records if record.get("block") == "gt_project_instructions"
    ]
    assert project[-1]["gt_project_instructions"]["selected_sources"] == ["AGENTS.md"]
    assert project[-1]["gt_project_instructions"]["evidence"]


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_v7_agent_brief_prefers_source_over_low_value_init(
    tiny_graph_db: str, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "names.py").write_text(
        "def normalize_name(value):\n    return value.strip().lower()\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_names.py").write_text(
        "from pkg.names import normalize_name\n\n"
        "def test_normalize_name():\n"
        "    assert normalize_name(' Ada ') == 'ada'\n",
        encoding="utf-8",
    )
    _init_repo(repo)
    _commit(repo, "initial names")

    issue = (
        "Bug: normalize_name should collapse internal whitespace. "
        "See `pkg/__init__.py`, `pkg/names.py`, and `tests/test_names.py`."
    )
    result = generate_brief(
        issue,
        str(repo),
        tiny_graph_db,
        task_id="v7_focus",
        log_dir=str(tmp_path / "logs"),
        return_telemetry=True,
        max_files=5,
    )

    assert isinstance(result, V7BriefResult)
    focus = result.plan["agent_focus_files"]
    assert focus[0]["file"] == "pkg/names.py"
    assert all(item["file"] != "pkg/__init__.py" for item in focus[:2])
    assert "pkg/__init__.py" in result.plan["cluster_files"]
    assert len(result.brief) <= 3500


def test_v7_brief_still_abstains_without_signals(tmp_path: Path) -> None:
    brief = generate_brief(
        "the thing is wrong",
        str(tmp_path),
        None,
        task_id="v7_abstain",
        log_dir=str(tmp_path / "logs"),
    )
    assert "could not deterministically localize" in brief

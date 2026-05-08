"""End-to-end tests for the v7 edit-plan brief."""

from __future__ import annotations

import json
import re
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

FILE_MENTION_RE = re.compile(
    r"\b[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|c|h|cc|cpp|hpp|rb|php|cs|md|rst|toml|json|yaml|yml)\b"
)


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
    assert "GT deterministic edit plan (ranked):" in result.brief
    assert "patroni/watchdog.py" in result.brief
    assert "Do not add throwaway scaffolding at the repo root" in result.brief
    assert "AGENTS.md" not in result.brief

    # Flattening check: no empty lines and headers removed
    assert "\n\n" not in result.brief
    assert "CANDIDATE CLUSTER:" not in result.brief
    assert "CONTRACT:" not in result.brief
    assert "IMPLEMENTATION PATTERN:" not in result.brief
    assert "EXPECTED SIDE FILES:" not in result.brief
    assert "CONSTRAINTS:" not in result.brief

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
    focus_files = {item["file"] for item in result.plan["agent_focus_files"]}
    brief_mentions = {
        match.replace("\\", "/").lstrip("./")
        for match in FILE_MENTION_RE.findall(result.brief)
        if not re.search(rf"[*?]{re.escape(match)}", result.brief)
    }
    assert brief_mentions <= focus_files
    assert len(brief_mentions) <= 3
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
    brief_mentions = {
        match.replace("\\", "/").lstrip("./")
        for match in FILE_MENTION_RE.findall(result.brief)
        if not re.search(rf"[*?]{re.escape(match)}", result.brief)
    }
    assert brief_mentions <= {item["file"] for item in focus}


def test_v7_brief_still_abstains_without_signals(tmp_path: Path) -> None:
    brief = generate_brief(
        "the thing is wrong",
        str(tmp_path),
        None,
        task_id="v7_abstain",
        log_dir=str(tmp_path / "logs"),
    )
    assert "could not deterministically localize" in brief


def _build_layered_graph_db(db_path: Path) -> str:
    """Graph DB with Function nodes, properties, cross-file callers — exercises
    behavioral contract, caller evidence, and recent-edits layers together."""
    import sqlite3 as _sql

    conn = _sql.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL, parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
            line INTEGER, confidence REAL DEFAULT 1.0
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "Function", "normalize_name", "pkg/names.py", 1, 3, 0, "python", None),
            (2, "Function", "format_user", "pkg/users.py", 1, 6, 0, "python", None),
        ],
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?, ?, 'CALLS', ?, ?, 'name_match', ?)",
        [(2, 1, 4, "pkg/users.py", 0.9)],
    )
    conn.executemany(
        "INSERT INTO properties (node_id, kind, value, confidence) VALUES (?, ?, ?, ?)",
        [
            (1, "return_shape", "str", 1.0),
            (1, "guard_clause", "if not value: return ''", 0.9),
            (1, "docstring", "Lowercase and strip surrounding whitespace.", 1.0),
        ],
    )
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_v7_brief_emits_behavioral_contract_caller_and_recent_edits(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "names.py").write_text(
        "def normalize_name(value):\n"
        "    if not value: return ''\n"
        "    return value.strip().lower()\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "users.py").write_text(
        "from pkg.names import normalize_name\n\n"
        "def format_user(name):\n"
        "    cleaned = normalize_name(name)\n"
        "    return f'<{cleaned}>'\n",
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
    (repo / "pkg" / "names.py").write_text(
        "def normalize_name(value):\n"
        "    if value is None: return ''\n"
        "    return value.strip().lower()\n",
        encoding="utf-8",
    )
    _commit(repo, "tighten guard against None")

    db_path = _build_layered_graph_db(tmp_path / "graph.db")

    issue = (
        "Bug: normalize_name should also collapse internal whitespace. "
        "See `pkg/names.py`."
    )
    result = generate_brief(
        issue,
        str(repo),
        db_path,
        task_id="v7_layers",
        log_dir=str(tmp_path / "logs"),
        return_telemetry=True,
    )
    assert isinstance(result, V7BriefResult)

    assert "BEHAVIORAL CONTRACT:" in result.brief
    assert "returns: str" in result.brief
    assert "guards:" in result.brief or "doc:" in result.brief
    assert "CALLER EVIDENCE:" in result.brief
    assert "format_user" in result.brief
    assert "RECENT EDITS:" in result.brief

    layer_counts = result.telemetry.module_5_render["v7_layer_counts"]
    assert layer_counts["behavioral_contract"] >= 1
    assert layer_counts["caller_evidence"] >= 1
    assert layer_counts["recent_edits"] >= 1
    sections = result.telemetry.module_5_render["v7_sections"]
    assert "behavioral_contract" in sections
    assert "caller_evidence" in sections
    assert "recent_edits" in sections


def test_v7_brief_layers_silently_absent_for_freshly_indexed_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "names.py").write_text(
        "def normalize_name(value):\n    return value.strip().lower()\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "graph.db"
    import sqlite3 as _sql

    conn = _sql.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT,
            is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL, parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, label, name, file_path, start_line, end_line, "
        "is_test, language, parent_id) VALUES (1, 'Function', 'normalize_name', "
        "'pkg/names.py', 1, 2, 0, 'python', NULL)"
    )
    conn.commit()
    conn.close()

    result = generate_brief(
        "Bug: normalize_name. See `pkg/names.py`.",
        str(repo),
        str(db_path),
        task_id="v7_layers_empty",
        log_dir=str(tmp_path / "logs"),
        return_telemetry=True,
    )
    assert isinstance(result, V7BriefResult)
    assert "BEHAVIORAL CONTRACT:" not in result.brief
    assert "CALLER EVIDENCE:" not in result.brief
    assert "RECENT EDITS:" not in result.brief
    layer_counts = result.telemetry.module_5_render["v7_layer_counts"]
    assert layer_counts["behavioral_contract"] == 0
    assert layer_counts["caller_evidence"] == 0
    assert layer_counts["recent_edits"] == 0

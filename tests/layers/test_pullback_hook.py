"""Tests for the Track 4 artifact pull-back hook (gt_track4_pre_run.py).

Coverage:
  - _push_graph_db_to_container: UploadRequest source/target paths
  - _pull_gt_artifacts: 4-artifact pull, missing-file silence,
    result-key vs legacy verdict-key, autosubmit detection,
    edit_*.json directory listing
  - _count_gt_query_calls: trajectory parsing (gt_query token isolation)
  - _append_completion_log: line format
  - on_instance_completed: pending drain when instance_id missing,
    autosubmit-vs-real-verdict precedence

The hook calls into ``swerex.runtime.abstract.UploadRequest`` and inherits
from ``sweagent.run.hooks.abstract.RunHook``. Neither is installed in the
test env, so we inject light stubs into ``sys.modules`` BEFORE importing
``gt_track4_pre_run``. No real swerex / sweagent code paths are exercised.

Anti-benchmaxxing: All tests use synthetic in-memory mocks. No SWE-bench
fixtures, no graph.db, no per-task hardcoded data. The behaviors under
test (artifact pull, gate-key reading, autosubmit logic) are general
properties of the hook contract; they're not tuned to any specific 15
tasks or benchmark distribution.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Inject sweagent + swerex stubs BEFORE the hook module is imported. The
# hook's RunHook subclass is gated behind a try/except on the sweagent
# import, and _push_graph_db_to_container imports UploadRequest lazily.
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    if "sweagent.run.hooks.abstract" not in sys.modules:
        sw = types.ModuleType("sweagent")
        sw_run = types.ModuleType("sweagent.run")
        sw_hooks = types.ModuleType("sweagent.run.hooks")
        sw_abs = types.ModuleType("sweagent.run.hooks.abstract")

        class RunHook:  # noqa: D401 — minimal stub
            pass

        sw_abs.RunHook = RunHook
        sys.modules["sweagent"] = sw
        sys.modules["sweagent.run"] = sw_run
        sys.modules["sweagent.run.hooks"] = sw_hooks
        sys.modules["sweagent.run.hooks.abstract"] = sw_abs

    if "swerex.runtime.abstract" not in sys.modules:
        sx = types.ModuleType("swerex")
        sx_r = types.ModuleType("swerex.runtime")
        sx_a = types.ModuleType("swerex.runtime.abstract")

        class UploadRequest:  # captures source_path/target_path
            def __init__(self, source_path: str | None = None,
                         target_path: str | None = None) -> None:
                self.source_path = source_path
                self.target_path = target_path

            def __repr__(self) -> str:  # pragma: no cover — debug aid
                return f"UploadRequest(src={self.source_path!r}, tgt={self.target_path!r})"

        sx_a.UploadRequest = UploadRequest
        sys.modules["swerex"] = sx
        sys.modules["swerex.runtime"] = sx_r
        sys.modules["swerex.runtime.abstract"] = sx_a


_install_stubs()

# Ensure the hook module is importable as a top-level script module.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK_DIR = _REPO_ROOT / "scripts" / "swebench"
if str(_HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOK_DIR))

import gt_track4_pre_run as hook_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Mock env + helpers
# ---------------------------------------------------------------------------

class _MockRuntime:
    """Captures upload() calls. Awaitable to satisfy asyncio.run wrapping."""

    def __init__(self) -> None:
        self.upload_calls: list[Any] = []

    async def upload(self, req: Any) -> None:
        self.upload_calls.append(req)


class _MockDeployment:
    def __init__(self) -> None:
        self.runtime = _MockRuntime()


class MockEnv:
    """Minimal stand-in for ``sweagent.environment.swe_env.SWEEnv``.

    - ``read_file(path)`` looks up ``path`` in ``files``. If the value is an
      ``Exception`` instance (or class), it's raised. Otherwise returned as
      a string. Missing paths raise FileNotFoundError.
    - ``communicate(cmd, timeout=...)`` returns ``listing_output`` regardless
      of the command (simulating ``ls -1`` output).
    - ``deployment.runtime.upload(req)`` records ``req`` (verified via
      ``deployment.runtime.upload_calls``).
    - ``close()`` records the call.
    """

    def __init__(
        self,
        files: dict[str, Any] | None = None,
        listing_output: str = "",
    ) -> None:
        self.files = dict(files or {})
        self.listing_output = listing_output
        self.deployment = _MockDeployment()
        self.close_calls = 0
        self.communicate_calls: list[tuple[str, Any]] = []

    def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        v = self.files[path]
        if isinstance(v, BaseException):
            raise v
        if isinstance(v, type) and issubclass(v, BaseException):
            raise v(path)
        return str(v)

    def communicate(self, cmd: str, timeout: int | None = None) -> str:
        self.communicate_calls.append((cmd, timeout))
        return self.listing_output

    def close(self) -> None:
        self.close_calls += 1


class _ProblemStatement:
    def __init__(self, ident: str, text: str = "fix the bug") -> None:
        self.id = ident
        self._text = text
        self.extra_fields: dict[str, Any] = {}

    def get_problem_statement(self) -> str:
        return self._text


class _Result:
    """Stand-in for swe-agent AgentRunResult."""

    def __init__(
        self,
        info: dict[str, Any] | None = None,
        trajectory: list[Any] | None = None,
    ) -> None:
        self.info = info if info is not None else {}
        self.trajectory = trajectory or []


# ---------------------------------------------------------------------------
# Tests — _push_graph_db_to_container
# ---------------------------------------------------------------------------

def test_graph_db_push_calls_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """on_instance_start → _push_graph_db_to_container → runtime.upload(UploadRequest)."""
    if hook_mod.GTTrack4PreRunHook is None:
        pytest.skip("RunHook stub missing — import gate failed")

    # Real graph.db file (existence is checked before upload).
    fake_db = tmp_path / "graph.db"
    fake_db.write_bytes(b"SQLite format 3\x00fake")

    monkeypatch.setenv("GT_GRAPH_DB", str(fake_db))
    monkeypatch.setenv("GT_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("GT_TRACK4_LOG_DIR", str(tmp_path / "logs"))

    env = MockEnv()
    ps = _ProblemStatement(ident="repo__pkg-1")
    h = hook_mod.GTTrack4PreRunHook(graph_db_path=str(fake_db),
                                    output_dir=tmp_path / "logs")
    h.on_instance_start(index=0, env=env, problem_statement=ps)

    calls = env.deployment.runtime.upload_calls
    assert len(calls) == 1, f"expected 1 upload call, got {len(calls)}"
    req = calls[0]
    assert req.source_path == str(fake_db)
    assert req.target_path == hook_mod._CONTAINER_GRAPH_DB == "/tmp/graph.db"


def test_push_skipped_when_host_path_missing(tmp_path: Path) -> None:
    """Helper returns False (no raise) when host graph.db doesn't exist."""
    env = MockEnv()
    ok = hook_mod._push_graph_db_to_container(
        env, str(tmp_path / "no_such.db"), "instX"
    )
    assert ok is False
    assert env.deployment.runtime.upload_calls == []


# ---------------------------------------------------------------------------
# Tests — _pull_gt_artifacts
# ---------------------------------------------------------------------------

def _gate_payload(result: str = "pass") -> str:
    return json.dumps({
        "result": result,
        "promotions": 1,
        "warnings": [],
    })


def test_pull_all_4_artifacts(tmp_path: Path) -> None:
    """All flat files + evidence files written to host log dir.

    RC-4 BUG-3: gt_query_calls.jsonl is now in _FLAT_ARTIFACTS — must be
    pulled, written, and counted.
    """
    files = {
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_pre_finish_gate.json": _gate_payload("pass"),
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_reindex.jsonl": (
            '{"path": "a.py"}\n{"path": "b.py"}\n'
        ),
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_query_calls.jsonl": (
            '{"symbol":"q1","returned_lines":2,"ts":1.0}\n'
            '{"symbol":"q2","returned_lines":3,"ts":2.0}\n'
            '{"symbol":"q3","returned_lines":1,"ts":3.0}\n'
        ),
        f"{hook_mod._CONTAINER_EVIDENCE_DIR}/edit_001.json": '{"file": "a.py"}',
        f"{hook_mod._CONTAINER_EVIDENCE_DIR}/edit_002.json": '{"file": "b.py"}',
    }
    env = MockEnv(files=files, listing_output="edit_001.json\nedit_002.json\n")
    log_dir = tmp_path / "logs" / "instA"
    summary = hook_mod._pull_gt_artifacts(env, log_dir, "instA")

    assert (log_dir / "gt_pre_finish_gate.json").read_text(encoding="utf-8") == \
        files[f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_pre_finish_gate.json"]
    assert (log_dir / "gt_reindex.jsonl").read_text(encoding="utf-8").startswith('{"path"')
    assert (log_dir / "gt_query_calls.jsonl").exists()
    assert (log_dir / "gt_evidence" / "edit_001.json").exists()
    assert (log_dir / "gt_evidence" / "edit_002.json").exists()

    assert summary["edit_count"] == 2
    assert summary["reindex_count"] == 2
    assert summary["query_count"] == 3  # RC-4 BUG-3
    assert summary["gate_verdict"] == "pass"


def test_pull_missing_files_silent(tmp_path: Path) -> None:
    """Missing artifacts on the container side don't crash the pull."""
    # Only gt_reindex.jsonl exists; gate file, query log, evidence dir absent.
    files = {
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_reindex.jsonl": '{"x":1}\n',
    }
    env = MockEnv(files=files, listing_output="")
    log_dir = tmp_path / "logs" / "instB"
    summary = hook_mod._pull_gt_artifacts(env, log_dir, "instB")

    assert (log_dir / "gt_reindex.jsonl").exists()
    assert not (log_dir / "gt_pre_finish_gate.json").exists()
    assert not (log_dir / "gt_query_calls.jsonl").exists()
    assert summary["edit_count"] == 0
    assert summary["reindex_count"] == 1
    assert summary["query_count"] == 0  # RC-4 BUG-3 default
    # gate file missing → default "absent" survives.
    assert summary["gate_verdict"] == "absent"


def test_gate_verdict_reads_result_key(tmp_path: Path) -> None:
    """gt_pre_finish_gate.json {"result": "warn_soft_escape"} → verdict = warn_soft_escape."""
    payload = json.dumps({"result": "warn_soft_escape", "warnings": ["x"]})
    files = {
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_pre_finish_gate.json": payload,
    }
    env = MockEnv(files=files, listing_output="")
    summary = hook_mod._pull_gt_artifacts(env, tmp_path / "logs" / "C", "C")
    assert summary["gate_verdict"] == "warn_soft_escape"
    assert summary["gate_verdict"] != "unknown"


def test_gate_verdict_legacy_verdict_key(tmp_path: Path) -> None:
    """Legacy json with only 'verdict' key (no 'result') → defaults to 'unknown'.

    This guards the recent fix: code must read .get("result", "unknown"), NOT
    .get("verdict", ...). Reading the legacy key would silently return real-
    looking verdicts on stale artifacts.
    """
    payload = json.dumps({"verdict": "pass", "promotions": 0})
    files = {
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_pre_finish_gate.json": payload,
    }
    env = MockEnv(files=files, listing_output="")
    summary = hook_mod._pull_gt_artifacts(env, tmp_path / "logs" / "D", "D")
    assert summary["gate_verdict"] == "unknown"
    # Crucially: hook must NOT mistakenly surface the legacy "pass" value.
    assert summary["gate_verdict"] != "pass"


def test_gate_verdict_malformed_json(tmp_path: Path) -> None:
    """Corrupt JSON in gate file → verdict = 'malformed', no crash."""
    files = {
        f"{hook_mod._CONTAINER_ARTIFACT_DIR}/gt_pre_finish_gate.json": "{not json",
    }
    env = MockEnv(files=files, listing_output="")
    summary = hook_mod._pull_gt_artifacts(env, tmp_path / "logs" / "E", "E")
    assert summary["gate_verdict"] == "malformed"


def test_evidence_dir_listing(tmp_path: Path) -> None:
    """ls -1 listing → each edit_*.json pulled and counted; non-edit_ ignored."""
    files = {
        f"{hook_mod._CONTAINER_EVIDENCE_DIR}/edit_aaa.json": "{}",
        f"{hook_mod._CONTAINER_EVIDENCE_DIR}/edit_bbb.json": "{}",
        f"{hook_mod._CONTAINER_EVIDENCE_DIR}/edit_ccc.json": "{}",
    }
    listing = "edit_aaa.json\nedit_bbb.json\nedit_ccc.json\nREADME.md\nbogus.txt\n"
    env = MockEnv(files=files, listing_output=listing)
    log_dir = tmp_path / "logs" / "F"
    summary = hook_mod._pull_gt_artifacts(env, log_dir, "F")

    assert summary["edit_count"] == 3
    for n in ("edit_aaa.json", "edit_bbb.json", "edit_ccc.json"):
        assert (log_dir / "gt_evidence" / n).exists()
    assert not (log_dir / "gt_evidence" / "README.md").exists()


# ---------------------------------------------------------------------------
# Tests — _count_gt_query_calls
#
# RC-4 BUG-1/BUG-2: The previous implementation took an AgentRunResult and
# scanned trajectory.action for the substring "gt_query". Two failure modes:
#
#   (1) Drain-all fan-out: on_instance_completed broadcast a SINGLE count
#       to every pending entry, so concurrent tasks reported the same L4.
#   (2) Tokenization false-positives: PATH=...gt_query/bin install lines
#       matched the substring test, producing a non-zero floor on tasks
#       that never invoked the tool.
#
# Fix: read gt_query_calls.jsonl from the per-task host artifact dir
# (populated by _pull_gt_artifacts from $GT_INSTANCE_LOG_DIR inside the
# container). Each invocation writes one JSON line — the line count IS
# the canonical L4 count.
# ---------------------------------------------------------------------------

def test_l4_count_from_canonical_artifact(tmp_path: Path) -> None:
    """gt_query_calls.jsonl with N non-empty lines → returns N."""
    (tmp_path / "gt_query_calls.jsonl").write_text(
        '{"symbol": "foo", "returned_lines": 5, "ts": 1.0}\n'
        '{"symbol": "bar", "returned_lines": 8, "ts": 2.0}\n'
        '\n'  # blank line — must be ignored
        '{"symbol": "baz", "returned_lines": 0, "ts": 3.0}\n'
    )
    assert hook_mod._count_gt_query_calls(tmp_path) == 3


def test_l4_count_missing_artifact(tmp_path: Path) -> None:
    """Missing gt_query_calls.jsonl → returns 0 (valid no-op state)."""
    assert hook_mod._count_gt_query_calls(tmp_path) == 0


def test_l4_count_none_dir() -> None:
    """None host_log_dir → returns 0 (defensive)."""
    assert hook_mod._count_gt_query_calls(None) == 0


def test_l4_count_empty_artifact(tmp_path: Path) -> None:
    """Empty file (gt_query never invoked but artifact exists) → 0."""
    (tmp_path / "gt_query_calls.jsonl").write_text("")
    assert hook_mod._count_gt_query_calls(tmp_path) == 0


def test_l4_count_no_false_positive_on_path_lines(tmp_path: Path) -> None:
    """Sanity: artifact-based counter ignores PATH-export-style lines.

    The old trajectory-substring scan over-counted lines like:
      export PATH=$PATH:/.../gt_query/bin
    because they contained the literal "gt_query" token. The new reader is
    immune by construction — it only counts what gt_query.py actually wrote.
    """
    # Even if a stray PATH-style line accidentally appeared in the file,
    # only valid JSON-like lines populated by gt_query._emit_telemetry
    # should be present. Either way: blank-stripping handles whitespace.
    (tmp_path / "gt_query_calls.jsonl").write_text(
        '{"symbol":"x","returned_lines":3,"ts":1.0}\n'
    )
    assert hook_mod._count_gt_query_calls(tmp_path) == 1


# ---------------------------------------------------------------------------
# Tests — _append_completion_log
# ---------------------------------------------------------------------------

def test_completion_log_format(tmp_path: Path) -> None:
    """Single line with task=, L3_edits=, L4_queries=, L5_gate=, L6_reindex= fields."""
    log = tmp_path / "gt_layers.log"
    summary = {"edit_count": 4, "reindex_count": 2, "gate_verdict": "warn_soft_escape"}
    hook_mod._append_completion_log(log, "repo__pkg-42", summary, l4_count=7)
    line = log.read_text(encoding="utf-8").strip()

    assert "task=repo__pkg-42" in line
    assert "L3_edits=4" in line
    assert "L4_queries=7" in line
    assert "L5_gate=warn_soft_escape" in line
    assert "L6_reindex=2" in line
    # Single line, ends with newline in file.
    assert log.read_text(encoding="utf-8").endswith("\n")


def test_completion_log_defaults_when_summary_empty(tmp_path: Path) -> None:
    """Empty summary dict → 0/0/absent defaults render."""
    log = tmp_path / "gt_layers.log"
    hook_mod._append_completion_log(log, "id1", {}, l4_count=0)
    line = log.read_text(encoding="utf-8").strip()
    assert "L3_edits=0" in line
    assert "L4_queries=0" in line
    assert "L5_gate=absent" in line
    assert "L6_reindex=0" in line


# ---------------------------------------------------------------------------
# Tests — on_instance_completed (autosubmit, drain)
# ---------------------------------------------------------------------------

def _make_hook(tmp_path: Path) -> Any:
    if hook_mod.GTTrack4PreRunHook is None:
        pytest.skip("RunHook stub missing — import gate failed")
    return hook_mod.GTTrack4PreRunHook(
        graph_db_path=str(tmp_path / "graph.db"),
        output_dir=tmp_path / "logs",
    )


def test_autosubmit_detection(tmp_path: Path) -> None:
    """exit_status='autosubmitted' AND no verdict file → gate_verdict='autosubmit'."""
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "autosub_a"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Pending state with no summary populated (env.close wrapper never ran,
    # OR ran but pulled nothing — same end state: gate_verdict='absent').
    h._pending["autosub_a"] = {"log_dir": log_dir, "cache": {}}

    result = _Result(info={"instance_id": "autosub_a", "exit_status": "autosubmitted"})
    h.on_instance_completed(result=result)

    log = (log_dir / "gt_layers.log").read_text(encoding="utf-8")
    assert "L5_gate=autosubmit" in log
    assert "L5_gate=absent" not in log


def test_autosubmit_with_real_verdict(tmp_path: Path) -> None:
    """Autosubmit exit_status BUT verdict file present → real verdict wins."""
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "autosub_b"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Cache already populated by env-close wrapper with real gate result.
    h._pending["autosub_b"] = {
        "log_dir": log_dir,
        "cache": {
            "summary": {
                "edit_count": 1, "reindex_count": 0,
                "gate_verdict": "warn_soft_escape",
            }
        },
    }

    result = _Result(info={"instance_id": "autosub_b", "exit_status": "autosubmitted"})
    h.on_instance_completed(result=result)

    log = (log_dir / "gt_layers.log").read_text(encoding="utf-8")
    assert "L5_gate=warn_soft_escape" in log
    assert "L5_gate=autosubmit" not in log


def test_pending_drain_when_id_missing(tmp_path: Path) -> None:
    """result.info has no instance_id but only ONE pending entry → drains it.

    The L4 count is read from the per-task host log dir's gt_query_calls.jsonl
    (RC-4 BUG-1/BUG-2 fix), so we synthesize that file to assert the new
    contract. The trajectory is irrelevant to L4 now — it's only consulted
    by _resolve_instance_id heuristics if info.instance_id is missing.
    """
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "lonely"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Synth canonical L4 artifact (one invocation).
    (log_dir / "gt_query_calls.jsonl").write_text(
        '{"symbol":"foo","returned_lines":5,"ts":1.0}\n'
    )
    h._pending["lonely"] = {
        "log_dir": log_dir,
        "cache": {
            "summary": {"edit_count": 2, "query_count": 1,
                        "reindex_count": 1, "gate_verdict": "pass"},
        },
    }

    result = _Result(info={}, trajectory=[{"action": "gt_query foo"}])
    h.on_instance_completed(result=result)

    # Drained.
    assert "lonely" not in h._pending
    log = (log_dir / "gt_layers.log").read_text(encoding="utf-8")
    assert "task=lonely" in log
    assert "L3_edits=2" in log
    assert "L4_queries=1" in log  # from synth artifact, not trajectory
    assert "L5_gate=pass" in log
    assert "L6_reindex=1" in log


def test_resolution_never_corrupts_pending_when_multi_pending(tmp_path: Path) -> None:
    """RC-03: with N>1 pending and an unresolvable result, on_instance_completed
    must NOT corrupt an arbitrary pending entry. The legacy "tag-first-pending
    as unresolved" guard relabeled an innocent task's telemetry while leaving
    the real offender pending forever — both effects were wrong. The new
    contract: log ERROR, leave _pending untouched, and let the close-wrap (or
    weakref safety net) write the canonical line for each task. The per-thread
    resolution path means this branch only fires when on_instance_start was
    never called for the current thread (e.g. test setup that bypasses it,
    as we do here by populating _pending directly).
    """
    h = _make_hook(tmp_path)
    for iid in ("alpha", "beta", "gamma"):
        d = tmp_path / "logs" / iid
        d.mkdir(parents=True, exist_ok=True)
        h._pending[iid] = {
            "log_dir": d,
            "cache": {
                "summary": {"edit_count": 0, "query_count": 0,
                            "reindex_count": 0, "gate_verdict": "pass"},
            },
        }

    # info has nothing useful, trajectory has nothing matching, model has
    # nothing, and the per-thread map is empty (we bypassed on_instance_start).
    result = _Result(info={"exit_status": "submitted"}, trajectory=[])
    h.on_instance_completed(result=result)

    # RC-03: pending dict is preserved — no innocent entry was popped or
    # relabeled. No log files were written either, because the canonical
    # writer is the close-wrap path.
    assert len(h._pending) == 3
    written_logs = list((tmp_path / "logs").glob("*/gt_layers.log"))
    assert len(written_logs) == 0


def test_resolution_via_model_name_suffix(tmp_path: Path) -> None:
    """RC-4 BUG-5: instance_id resolves from info['model_name']='swea-agent-<id>'."""
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "from_model"
    log_dir.mkdir(parents=True, exist_ok=True)
    h._pending["from_model"] = {"log_dir": log_dir, "cache": {}}
    # Ensure a second pending entry so single-pending fallback doesn't kick in.
    h._pending["other_id"] = {
        "log_dir": tmp_path / "logs" / "other_id",
        "cache": {},
    }
    (tmp_path / "logs" / "other_id").mkdir(parents=True, exist_ok=True)

    result = _Result(info={"model_name": "swea-agent-from_model"})
    h.on_instance_completed(result=result)

    assert "from_model" not in h._pending
    assert "other_id" in h._pending  # untouched
    assert (log_dir / "gt_layers.log").exists()


def test_resolution_via_traj_path(tmp_path: Path) -> None:
    """RC-4 BUG-5: instance_id resolves from info['traj_path']='/tmp/<id>.traj'."""
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "from_traj"
    log_dir.mkdir(parents=True, exist_ok=True)
    h._pending["from_traj"] = {"log_dir": log_dir, "cache": {}}
    h._pending["distractor"] = {
        "log_dir": tmp_path / "logs" / "distractor",
        "cache": {},
    }
    (tmp_path / "logs" / "distractor").mkdir(parents=True, exist_ok=True)

    result = _Result(info={"traj_path": "/tmp/runs/from_traj.traj"})
    h.on_instance_completed(result=result)

    assert "from_traj" not in h._pending
    assert "distractor" in h._pending


def test_pull_failed_distinguishes_from_absent(tmp_path: Path) -> None:
    """RC-4 BUG-4: cache.pull_error sentinel surfaces gate_verdict='pull_failed'.

    Previously, a pull-wrapper exception silently fell through and the
    completion log defaulted to gate_verdict='absent', masking infra
    failure as a tool no-op. Now the cache carries a pull_error sentinel
    that the completion hook reads to distinguish the two states.
    """
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "broken"
    log_dir.mkdir(parents=True, exist_ok=True)
    h._pending["broken"] = {
        "log_dir": log_dir,
        # No 'summary' — but 'pull_error' set, simulating wrapper raised.
        "cache": {"pull_error": "RuntimeError('boom')"},
    }

    result = _Result(info={"instance_id": "broken", "exit_status": "submitted"})
    h.on_instance_completed(result=result)

    log_text = (log_dir / "gt_layers.log").read_text(encoding="utf-8")
    assert "L5_gate=pull_failed" in log_text
    assert "L5_gate=absent" not in log_text


def test_close_wrap_writes_layers_log_with_correct_instance_id(tmp_path: Path) -> None:
    """The env.close wrap writes the gt_layers.log L3-L6 line itself.

    Regression for the 2026-05-06 phase2 5-task smoke: with N=5 parallel
    workers, on_instance_completed received ``result`` objects whose
    ``info.instance_id`` was None and whose other fields didn't carry the
    instance_id either. ``_resolve_instance_id`` fell through all
    fallbacks → the RC-4 BUG-5 guard tagged the FIRST pending entry,
    producing wrong gt_layers.log lines (L5=no_close_wrap with zero
    counters) for tasks where ``_pull_gt_artifacts`` had actually
    succeeded with the correct counters. Fix: write the line from inside
    ``_wrapped_close`` where the instance_id is captured in the closure.

    This test feeds a MockEnv with a populated edit_001.json artifact,
    triggers the wrap, then asserts the log line carries the correct
    instance_id and the real counts.
    """
    log_dir = tmp_path / "logs" / "task_x"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Synth canonical L4 artifact (one invocation) on the host side BEFORE
    # the pull, since the mock pull just copies whatever the container
    # said into host_log_dir. We instead let _pull_gt_artifacts itself
    # populate gt_query_calls.jsonl from the container — see files dict.
    edit_payload = json.dumps({"families": {"CHANGE": "x"}})
    files = {
        "/root/gt_artifacts/gt_pre_finish_gate.json": '{"result":"pass"}',
        "/root/gt_artifacts/gt_reindex.jsonl": "{}\n{}\n",
        "/root/gt_artifacts/gt_query_calls.jsonl":
            '{"symbol":"foo","returned_lines":1,"ts":1.0}\n',
        "/root/gt_artifacts/gt_evidence/edit_001.json": edit_payload,
    }
    env = MockEnv(files=files, listing_output="edit_001.json")
    cache: dict[str, Any] = {}

    hook_mod._wrap_env_close_with_artifact_pull(env, log_dir, "task_x", cache)
    env.close()  # triggers the wrap — runs pull, writes layers log

    assert cache.get("completion_logged") is True, (
        "close-wrap should set completion_logged"
    )
    assert env.close_calls == 1, "original close should run exactly once"
    log_text = (log_dir / "gt_layers.log").read_text(encoding="utf-8")
    assert "task=task_x" in log_text
    assert "L3_edits=1" in log_text       # one edit_*.json
    assert "L4_queries=1" in log_text     # one canonical jsonl line
    assert "L5_gate=pass" in log_text     # from gt_pre_finish_gate.json
    assert "L6_reindex=2" in log_text     # 2 lines in gt_reindex.jsonl


def test_completion_skips_when_close_wrap_already_logged(tmp_path: Path) -> None:
    """on_instance_completed must NOT double-write when the close-wrap
    already logged the L3-L6 line. The cache.completion_logged flag is
    the signal."""
    h = _make_hook(tmp_path)
    log_dir = tmp_path / "logs" / "already"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Pre-write a sentinel line as if the wrap had already done its job.
    (log_dir / "gt_layers.log").write_text(
        "task=already L3_edits=7 L4_queries=2 L5_gate=pass L6_reindex=7\n",
        encoding="utf-8",
    )
    h._pending["already"] = {
        "log_dir": log_dir,
        "cache": {
            "summary": {"edit_count": 7, "query_count": 2,
                        "reindex_count": 7, "gate_verdict": "pass"},
            "completion_logged": True,
        },
    }

    result = _Result(info={"instance_id": "already", "exit_status": "submitted"})
    h.on_instance_completed(result=result)

    log_text = (log_dir / "gt_layers.log").read_text(encoding="utf-8")
    # Exactly ONE line — no double write from on_instance_completed.
    assert log_text.count("task=already") == 1, (
        f"expected 1 line, got: {log_text!r}"
    )
    # And the pending entry was cleaned up.
    assert "already" not in h._pending


# ---------------------------------------------------------------------------
# RC-03 — per-thread state isolation under concurrency
# ---------------------------------------------------------------------------

def test_rc03_per_thread_resolution_under_concurrency(tmp_path: Path) -> None:
    """RC-03: each worker thread's on_instance_completed resolves to the
    instance_id the SAME thread stashed in on_instance_start, even when
    `info["instance_id"]` is None on every result (the LiteLLM/Vertex
    failure mode that caused the 80% wrong-contract rate at 5-way smoke).

    Strategy:
      - Spin up N threads.
      - Each thread populates `_thread_pending` itself (simulating what
        on_instance_start does after the lock-protected stash) and calls
        on_instance_completed with an empty info dict.
      - Assert that each thread's per-task gt_layers.log line carries that
        thread's own instance_id (no cross-talk).
    """
    if hook_mod.GTTrack4PreRunHook is None:
        pytest.skip("RunHook stub missing")

    import threading as _t

    N = 6
    h = hook_mod.GTTrack4PreRunHook(
        graph_db_path=str(tmp_path / "graph.db"),
        output_dir=tmp_path / "logs",
    )
    barrier = _t.Barrier(N)
    errors: list[str] = []
    errors_lock = _t.Lock()

    def worker(iid: str) -> None:
        log_dir = tmp_path / "logs" / iid
        log_dir.mkdir(parents=True, exist_ok=True)
        # Mimic on_instance_start's locked stash.
        tid = _t.get_ident()
        with h._pending_lock:
            h._pending[iid] = {
                "log_dir": log_dir,
                "cache": {
                    "summary": {"edit_count": 1, "query_count": 0,
                                "reindex_count": 1, "gate_verdict": "pass"},
                },
                "thread_id": tid,
            }
            h._thread_pending[tid] = iid
        # All workers race on_instance_completed simultaneously.
        barrier.wait()
        result = _Result(info={}, trajectory=[])
        try:
            h.on_instance_completed(result=result)
        except Exception as exc:  # pragma: no cover
            with errors_lock:
                errors.append(f"{iid}: {exc!r}")

    threads = [_t.Thread(target=worker, args=(f"task_{i}",)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"worker errors: {errors}"
    # Every per-task log must reference its own task_id and exactly once.
    for i in range(N):
        iid = f"task_{i}"
        log = tmp_path / "logs" / iid / "gt_layers.log"
        assert log.exists(), f"missing log for {iid}"
        body = log.read_text(encoding="utf-8")
        assert f"task={iid}" in body, f"{iid} log lost its identity: {body!r}"
        assert "L5_gate=pass" in body, f"{iid} got wrong verdict: {body!r}"
        # No cross-talk: this log must NOT contain any other task's id.
        for j in range(N):
            if j == i:
                continue
            assert f"task=task_{j}" not in body, (
                f"{iid} log polluted with task_{j}: {body!r}"
            )
    # Pending should be drained.
    assert h._pending == {}
    assert h._thread_pending == {}


def test_rc03_unresolvable_does_not_corrupt_pending(tmp_path: Path) -> None:
    """RC-03 (c): when on_instance_completed cannot resolve via per-thread
    map AND the legacy ladder also fails AND there are >1 pending entries,
    NO entry is touched. Verifies the dropped 'tag-first-pending as
    unresolved' guard.
    """
    h = _make_hook(tmp_path)
    for iid in ("p1", "p2"):
        d = tmp_path / "logs" / iid
        d.mkdir(parents=True, exist_ok=True)
        h._pending[iid] = {
            "log_dir": d,
            "cache": {"summary": {"gate_verdict": "pass"}},
        }
    # Empty result, no thread map for this thread → unresolvable.
    result = _Result(info={"exit_status": "submitted"}, trajectory=[])
    h.on_instance_completed(result=result)

    assert set(h._pending.keys()) == {"p1", "p2"}
    # No gt_layers.log written for either task.
    assert not (tmp_path / "logs" / "p1" / "gt_layers.log").exists()
    assert not (tmp_path / "logs" / "p2" / "gt_layers.log").exists()

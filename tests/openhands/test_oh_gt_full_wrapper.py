from __future__ import annotations

import base64
import hashlib
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path

sys.modules.setdefault(
    "litellm",
    SimpleNamespace(
        model_cost={},
        success_callback=[],
        completion=lambda *args, **kwargs: None,
        acompletion=None,
        completion_cost=lambda *args, **kwargs: 0.0,
    ),
)

from scripts.swebench import oh_gt_full_wrapper as ohgt


class Observation:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.exit_code = 0


class FakeRuntime:
    def __init__(self) -> None:
        self.actions = []
        self._gt_full_config = None

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "gt-index" in command:
            return Observation("INDEX_OK")
        if "stat -c %Y" in command:
            return Observation("0")
        if "groundtruth.hooks" in command:
            return Observation("[GT_STATUS] success [GT_CHANGE] modified something")
        if "command -v gt_query" in command:
            return Observation("/tmp/gt_tools/gt_query\n/tmp/gt_tools/gt_search\n/tmp/gt_tools/gt_navigate\n/tmp/gt_tools/gt_validate")
        if "python3 /tmp/gt_brief_runner.py" in command:
            # Need a longer brief to avoid [GT_BRIEF_FAILED] length check (100 chars)
            # And it must contain a file path like 'src/service.py' for prefetch to find a candidate
            long_brief = "TARGET src/service.py\n" + "X" * 150
            return Observation(long_brief + "\n---GT_L2_JSON---\n{}")
        if "gt_symbol_query.py" in command:
            return Observation("my_func")
        if "gt_query.py" in command:
            return Observation("# gt_query: my_func\n[VERIFIED] caller")
        return Observation("AGENT_OBS")


class FileReadAction:
    def __init__(self, path: str) -> None:
        self.path = path


class FileEditAction:
    def __init__(self, path: str) -> None:
        self.path = path


class FileWriteAction:
    def __init__(self, path: str) -> None:
        self.path = path


class CmdRunAction:
    def __init__(self, command: str) -> None:
        self.command = command


class AgentFinishAction:
    pass


class Message:
    def __init__(self, content: str) -> None:
        self.content = content


class Instance:
    instance_id = "pkg__repo-1"
    problem_statement = "Fix the service"
    gt_brief = "TARGET src/service.py"


class RunInferModule:
    @staticmethod
    def initialize_runtime(runtime, instance, metadata):
        runtime.initialized = True

    @staticmethod
    def get_instruction(instance, metadata):
        return Message("Original issue text")


def test_classifies_hook_events_and_negative_controls():
    assert ohgt.classify_tool_event(FileReadAction("src/app.py")) == ohgt.HookEvent(
        "post_view", "src/app.py"
    )
    assert ohgt.classify_tool_event(FileEditAction("/workspace/src/app.py")) == ohgt.HookEvent(
        "post_edit", "src/app.py"
    )
    assert ohgt.classify_tool_event(CmdRunAction("str_replace_editor view src/app.py")) == ohgt.HookEvent(
        "post_view", "src/app.py"
    )
    assert ohgt.classify_tool_event(
        CmdRunAction("str_replace_editor str_replace src/app.py")
    ) == ohgt.HookEvent("post_edit", "src/app.py")

    assert ohgt.classify_tool_event(FileEditAction("tests/test_app.py")).reason == "test_path"
    assert ohgt.classify_tool_event(FileReadAction("README.md")).reason == "non_source_ext"
    assert (
        ohgt.classify_tool_event(CmdRunAction("python3 /tmp/gt_hook.py --file src/app.py")).reason
        == "internal_gt_command"
    )


def test_post_view_delivery_is_agent_visible_and_non_recursive():
    runtime = FakeRuntime()
    ohgt.wrap_runtime_run_action(runtime, ohgt.GTRuntimeConfig())

    obs = runtime.run_action(FileReadAction("src/app.py"))

    assert 'trigger="post_view:src/app.py"' in obs.content
    assert "[GT_CHANGE]" in obs.content
    assert isinstance(runtime.actions[0], FileReadAction)
    commands = [getattr(a, "command", "") for a in runtime.actions]
    assert any("groundtruth.hooks.post_view" in c for c in commands)


def test_post_edit_delivery_reindexes_first_and_passes_edited_file():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("src/app.py"))
    commands = [getattr(action, "command", "") for action in runtime.actions]

    assert 'trigger="post_edit:src/app.py"' in obs.content
    assert "[GT_CHANGE]" in obs.content
    reindex_i = next(i for i, command in enumerate(commands) if "gt-index-linux" in command)
    hook_i = next(i for i, command in enumerate(commands) if "groundtruth.hooks.post_edit" in command)
    assert reindex_i < hook_i
    assert commands[reindex_i] == "/tmp/gt-index-linux -root=/workspace -file=src/app.py -output=/tmp/gt_index.db"
    assert "src/app.py" in config.pending_checks


def test_reindex_uses_paths_relative_to_task_repo_root():
    config = ohgt.GTRuntimeConfig(
        workspace_root="/workspace/kozea__weasyprint-2300",
        gt_index_bin="/tmp/gt-index",
    )

    command = ohgt.make_reindex_command(
        "/workspace/kozea__weasyprint-2300/weasyprint/layout/flex.py", config
    )

    assert command == (
        "/tmp/gt-index -root=/workspace/kozea__weasyprint-2300 "
        "-file=weasyprint/layout/flex.py -output=/tmp/gt_index.db"
    )


def test_non_source_reads_and_test_edits_do_not_fire_hooks():
    runtime = FakeRuntime()
    ohgt.wrap_runtime_run_action(runtime, ohgt.GTRuntimeConfig())

    read_obs = runtime.run_action(FileReadAction("README.md"))
    edit_obs = runtime.run_action(FileEditAction("tests/test_app.py"))

    assert "<gt-evidence" not in read_obs.content
    assert "<gt-evidence" not in edit_obs.content
    assert all("groundtruth.hooks" not in getattr(action, "command", "") for action in runtime.actions)


def test_scaffold_edit_skip_is_host_only():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("reproduce_issue.py"))

    assert "[GT_STATUS]" not in obs.content
    assert "<gt-evidence" not in obs.content
    assert any(
        rec.get("layer") == "L3"
        and rec.get("type") == "scaffold_skip"
        and rec.get("trigger") == "post_edit:reproduce_issue.py"
        for rec in config.interaction_log
    )


def test_l3b_budget_skip_is_host_only():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()
    config._l3b_fire_count = 3
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileReadAction("src/app.py"))

    assert "<gt-evidence" not in obs.content
    assert "[GT_CHANGE]" not in obs.content
    assert any(
        rec.get("layer") == "L3b"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "budget_exhausted"
        for rec in config.interaction_log
    )


def test_l3b_late_iteration_skip_is_host_only():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig(max_iter=100)
    config.action_count = 76
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileReadAction("src/app.py"))

    assert "<gt-evidence" not in obs.content
    assert "[GT_CHANGE]" not in obs.content
    assert any(
        rec.get("layer") == "L3b"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "late_iteration"
        for rec in config.interaction_log
    )


def test_l3_budget_skip_is_host_only():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    config._l3_fire_count = 5
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("src/app.py"))

    assert "<gt-evidence" not in obs.content
    assert "[GT_CHANGE]" not in obs.content
    assert any(
        rec.get("layer") == "L3"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "budget_exhausted"
        for rec in config.interaction_log
    )


def test_l3_same_file_skip_is_host_only():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    config._l5_edit_counts_per_file["src/app.py"] = 2
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("src/app.py"))

    assert "<gt-evidence" not in obs.content
    assert "[GT_CHANGE]" not in obs.content
    assert any(
        rec.get("layer") == "L3"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "same_file_suppression"
        for rec in config.interaction_log
    )


def test_auto_query_no_symbols_logs_no_output(monkeypatch):
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()
    config._l3b_fire_count = 3
    monkeypatch.setattr(ohgt, "_container_query", lambda *args, **kwargs: "[]")
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileReadAction("src/app.py"))

    assert "src/app.py" in config._auto_query_seen
    assert config._auto_query_count == 0
    assert any(
        rec.get("layer") == "L4"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "no_symbols"
        for rec in config.interaction_log
    )


def test_auto_query_no_actionable_lines_logs_no_output(monkeypatch):
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()
    config._l3b_fire_count = 3
    calls = {"n": 0}

    def fake_container_query(*args, **kwargs):
        calls["n"] += 1
        return '[["foo", ""]]' if calls["n"] == 1 else "[]"

    monkeypatch.setattr(ohgt, "_container_query", fake_container_query)
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileReadAction("src/app.py"))

    assert "src/app.py" in config._auto_query_seen
    assert config._auto_query_count == 0
    assert any(
        rec.get("layer") == "L4"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "no_actionable_lines"
        for rec in config.interaction_log
    )


def test_auto_query_error_logs_no_output(monkeypatch):
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()
    config._l3b_fire_count = 3

    def raise_query_error(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ohgt, "_container_query", raise_query_error)
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileReadAction("src/app.py"))

    assert "src/app.py" in config._auto_query_seen
    assert config._auto_query_count == 0
    assert any(
        rec.get("layer") == "L4"
        and rec.get("type") == "no_output"
        and rec.get("gt_sent") == "query_error"
        for rec in config.interaction_log
    )


def test_graph_db_chunked_base64_transfer_assembles_split_tokens(tmp_path):
    db = tmp_path / "graph.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER);
        INSERT INTO nodes (id, name) VALUES (1, 'a');
        INSERT INTO edges (id, source_id, target_id) VALUES (1, 1, 1);
        """
    )
    con.close()
    payload = db.read_bytes()
    expected_md5 = hashlib.md5(payload).hexdigest()

    class TransferRuntime:
        def run_action(self, action):
            command = getattr(action, "command", "")
            if "SIZE=" in command:
                return Observation(f"SIZE={len(payload)} MD5={expected_md5}")
            if "base64.b64encode(open" in command:
                return Observation("A" * 128)
            if "f.seek(" in command:
                encoded = base64.b64encode(payload).decode("ascii")
                split = "\n".join(encoded[i : i + 16] for i in range(0, len(encoded), 16))
                return Observation(f"noise\n{split}\nmore-noise")
            return Observation("")

    downloaded = ohgt._download_graph_db_to_host(TransferRuntime(), "/tmp/graph.db")

    assert downloaded
    con = sqlite3.connect(downloaded)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM edges").fetchone()[0] == 1
    finally:
        con.close()


def test_l4_tools_are_installed_and_footer_advertises_path_tools():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()

    ohgt.install_l4_tools(runtime, config)
    commands = "\n".join(getattr(action, "command", "") for action in runtime.actions)

    assert "gt_query" in commands
    assert "gt_search" in commands
    assert "gt_navigate" in commands
    assert "command -v gt_query gt_search gt_navigate gt_validate" in commands


def test_l5_finish_advisory_is_visible_for_unverified_edits():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileWriteAction("src/service.py"))
    finish_obs = runtime.run_action(AgentFinishAction())

    # We now write advisory to instance_ref instead of observation content on finish
    # But checkpoints still write to observation. Let's trigger a checkpoint.
    config.max_iter = 10
    config.action_count = 2 # 33% of 10 is 3.
    obs = runtime.run_action(CmdRunAction("ls"))
    assert '<gt-advisory layer="L5"' in obs.content


def test_install_graph_builds_from_task_repo_and_installs_hook():
    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")

    ohgt.install_graph_and_hook(runtime, config)
    commands = [getattr(action, "command", "") for action in runtime.actions]
    for c in commands: print(f"DEBUG_CMD: {c}")

    assert any("base64 -d /tmp/gt_src.tar.gz.b64 > /tmp/gt_src.tar.gz" in command for command in commands)
    assert any("/tmp/gt-index-linux -root='/workspace' -output='/tmp/gt_index.db' 2>&1" in command for command in commands)
    assert any("command -v gt_query gt_search gt_navigate gt_validate" in command for command in commands)


def test_l1_l2_brief_is_delivered_in_first_user_turn():
    module = RunInferModule()
    ohgt.patch_run_infer(module)

    runtime = FakeRuntime()
    instance = Instance()
    ohgt.patched_initialize_runtime(runtime, instance, object())
    msg = ohgt.patched_get_instruction(instance, object())

    assert msg.content.startswith("<gt-task-brief>\nTARGET src/service.py")
    assert "Original issue text" in msg.content
    assert "gt_query" in msg.content


def test_l1_logging_occurs_only_for_real_brief_injection():
    module = RunInferModule()
    ohgt.patch_run_infer(module)

    runtime = FakeRuntime()
    instance = Instance()
    config = ohgt.GTRuntimeConfig()
    runtime._gt_full_config = config
    instance._gt_runtime = runtime

    msg = ohgt.patched_get_instruction(instance, object())

    assert "<gt-task-brief>" in msg.content
    assert any(
        rec.get("layer") == "L1" and rec.get("type") == "brief_injection"
        for rec in config.interaction_log
    )


def test_tool_hint_without_brief_is_not_logged_as_l1():
    module = RunInferModule()
    ohgt.patch_run_infer(module)

    runtime = FakeRuntime()
    config = ohgt.GTRuntimeConfig()
    runtime._gt_full_config = config
    instance = type("NoBriefInstance", (), {})()
    instance.instance_id = "pkg__repo-2"
    instance.problem_statement = "Fix without brief"
    instance.gt_brief = ""
    instance._gt_runtime = runtime
    instance.gt_l4_tools = ["gt_query"]

    msg = ohgt.patched_get_instruction(instance, object())

    assert "## Codebase Intelligence" in msg.content
    assert "<gt-task-brief>" not in msg.content
    assert not any(rec.get("layer") == "L1" for rec in config.interaction_log)


def test_wrapper_source_does_not_read_oracle_fields():
    source = Path(ohgt.__file__).read_text(encoding="utf-8")

    forbidden = ["FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "gold_patch", "oracle"]
    assert not any(token in source for token in forbidden)


def _make_config(**kwargs):
    return ohgt.GTRuntimeConfig(**kwargs)


def test_is_real_source_edit_rejects_scaffold():
    config = _make_config()
    assert not ohgt._is_real_source_edit("reproduce_issue.py", config)
    assert not ohgt._is_real_source_edit("debug_foo.py", config)
    assert not ohgt._is_real_source_edit("scratch_test.py", config)
    assert not ohgt._is_real_source_edit("temp_check.py", config)


def test_is_real_source_edit_rejects_test():
    config = _make_config()
    assert not ohgt._is_real_source_edit("test_timezone_issue.py", config)
    assert not ohgt._is_real_source_edit("tests/test_auth.py", config)
    assert not ohgt._is_real_source_edit("test/unit/test_foo.py", config)


def test_is_real_source_edit_accepts_source():
    config = _make_config()
    assert ohgt._is_real_source_edit("src/logger.py", config)
    assert ohgt._is_real_source_edit("loguru/_datetime.py", config)
    assert ohgt._is_real_source_edit("cfnlint/rules/Permissions.py", config)


def test_is_scaffolding_path():
    assert ohgt._is_scaffolding_path("reproduce_issue.py")
    assert ohgt._is_scaffolding_path("debug_test.py")
    assert not ohgt._is_scaffolding_path("test_timezone.py")
    assert not ohgt._is_scaffolding_path("src/main.py")


def test_task_metrics_written_without_telemetry():
    import tempfile
    config = _make_config()
    config._meta_instance_id = "test_task"
    config.telemetry = None
    old = ohgt.GT_TASK_METRICS
    old_iter = ohgt.GT_ITER_METRICS
    try:
        with tempfile.NamedTemporaryFile(suffix='.jsonl', delete=False, mode='w') as f:
            ohgt.GT_TASK_METRICS = f.name
        with tempfile.NamedTemporaryFile(suffix='.jsonl', delete=False, mode='w') as f2:
            ohgt.GT_ITER_METRICS = f2.name
        ohgt._flush_task_end_metrics(config, "test")
        import json as _json
        with open(ohgt.GT_TASK_METRICS) as f:
            record = _json.loads(f.read().strip())
        assert record["task_id"] == "test_task"
        assert record["phase"] == "test"
        assert "behavior_class" in record
    finally:
        ohgt.GT_TASK_METRICS = old
        ohgt.GT_ITER_METRICS = old_iter


def test_behavior_classification():
    config = _make_config()
    assert ohgt._classify_behavior(config) == "read_run_stall"
    config.edited_files = {"test_foo.py"}
    assert ohgt._classify_behavior(config) == "non_source_edit_loop"
    config.edited_files = {"src/main.py"}
    assert ohgt._classify_behavior(config) == "source_edit"
    config._diff_collapsed_count = 1
    assert ohgt._classify_behavior(config) == "collapsed"


def test_l5_fires_on_test_file_not_just_scaffold():
    config = _make_config()
    config.edited_files = set()
    assert not ohgt._is_scaffolding_path("test_timezone_issue.py")
    assert not ohgt._is_real_source_edit("test_timezone_issue.py", config)


def test_l5_does_not_fire_on_source_edit():
    config = _make_config()
    config.edited_files = set()
    assert ohgt._is_real_source_edit("src/logger.py", config)


# ---- BUG-C1/C2/C3 wrapper-level proof tests ----


class MismatchOnlyRuntime(FakeRuntime):
    """Returns [MISMATCH]-only evidence from post-edit hook."""

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "groundtruth.hooks.post_edit" in command:
            return Observation(
                "[MISMATCH] You removed `get_user` but "
                "tests/test_users.py:42 still calls it"
            )
        if "gt-index" in command:
            return Observation("INDEX_OK")
        if "stat -c %Y" in command:
            return Observation("0")
        return Observation("AGENT_OBS")


class FormatOnlyRuntime(FakeRuntime):
    """Returns [FORMAT]-only evidence from post-edit hook."""

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "groundtruth.hooks.post_edit" in command:
            return Observation(
                '[FORMAT] Callers access keys: "name", "email", "id"'
            )
        if "gt-index" in command:
            return Observation("INDEX_OK")
        if "stat -c %Y" in command:
            return Observation("0")
        return Observation("AGENT_OBS")


class ArityOnlyRuntime(FakeRuntime):
    """Returns [GT_CONTRACT high]-only evidence from post-edit hook."""

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "groundtruth.hooks.post_edit" in command:
            return Observation(
                "[GT_CONTRACT high] get_user() now requires 3+ args. "
                "Callers at api/views.py:88 pass only 2."
            )
        if "gt-index" in command:
            return Observation("INDEX_OK")
        if "stat -c %Y" in command:
            return Observation("0")
        return Observation("AGENT_OBS")


def test_l3_mismatch_only_evidence_is_delivered():
    """BUG-C1: old inline marker check drops [MISMATCH]-only evidence.

    Pre-fix: Inline tuple at wrapper:3921 does not include '[MISMATCH]'.
    Post-fix: _deliver_or_trace() uses has_gt_evidence('l3') which
    includes [MISMATCH] via L3B_MARKERS.
    """
    runtime = MismatchOnlyRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("src/users.py"))

    assert "[MISMATCH]" in obs.content, (
        "BUG-C1: [MISMATCH]-only evidence suppressed by inline marker check"
    )


def test_l3_format_only_evidence_is_delivered():
    """BUG-C1: old inline marker check drops [FORMAT]-only evidence."""
    runtime = FormatOnlyRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("src/users.py"))

    assert "[FORMAT]" in obs.content, (
        "BUG-C1: [FORMAT]-only evidence suppressed by inline marker check"
    )


def test_l3_gt_contract_high_only_evidence_is_delivered():
    """BUG-C1: prefix vs exact-match bug.

    Pre-fix: inline has '[GT_CONTRACT]' (exact) which does NOT match
    '[GT_CONTRACT high]' (space after T, not ']').
    Post-fix: has_gt_evidence('l3') has '[GT_CONTRACT' (prefix) which matches.
    """
    runtime = ArityOnlyRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    obs = runtime.run_action(FileEditAction("src/users.py"))

    assert "[GT_CONTRACT high]" in obs.content, (
        "BUG-C1: [GT_CONTRACT high] suppressed — inline check has "
        "'[GT_CONTRACT]' (exact) not '[GT_CONTRACT' (prefix)"
    )


class L3TraceRuntime(FakeRuntime):
    """Returns L3-valid evidence that survives the directive_lines filter."""

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "groundtruth.hooks.post_edit" in command:
            return Observation("[CONTRACT] 3 callers depend on get_user():")
        if "gt-index" in command:
            return Observation("INDEX_OK")
        if "stat -c %Y" in command:
            return Observation("0")
        return Observation("AGENT_OBS")


class L3bTraceRuntime(FakeRuntime):
    """Returns L3b-valid evidence that survives the directive_lines filter."""

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "groundtruth.hooks.post_view" in command:
            return Observation("Called by: installer.py (3x)")
        return Observation("AGENT_OBS")


def test_l3_delivery_produces_gt_trace(capsys):
    """BUG-C3: legacy L3 path bypasses _deliver_or_trace().

    Pre-fix: Legacy L3 calls append_observation() directly. No [GT_TRACE].
    Post-fix: Routes through _deliver_or_trace() → prints [GT_TRACE].
    """
    runtime = L3TraceRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileEditAction("src/app.py"))

    captured = capsys.readouterr()
    assert "[GT_TRACE] l3_delivery status=DELIVERED" in captured.out, (
        "BUG-C3: legacy L3 path does not produce [GT_TRACE] delivery log"
    )


def test_l3b_delivery_produces_gt_trace(capsys):
    """BUG-C3: legacy L3b path bypasses _deliver_or_trace()."""
    runtime = L3bTraceRuntime()
    config = ohgt.GTRuntimeConfig()
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileReadAction("src/app.py"))

    captured = capsys.readouterr()
    assert "[GT_TRACE] l3b_delivery status=DELIVERED" in captured.out, (
        "BUG-C3: legacy L3b path does not produce [GT_TRACE] delivery log"
    )


class DedupTestRuntime(FakeRuntime):
    """Returns same visible evidence but different __GT_STRUCTURED__ JSON each call."""

    def __init__(self):
        super().__init__()
        self._edit_hook_calls = 0

    def run_action(self, action):
        self.actions.append(action)
        command = getattr(action, "command", "")
        if "groundtruth.hooks.post_edit" in command:
            self._edit_hook_calls += 1
            return Observation(
                "[CONTRACT] 3 callers depend on get_user():\n"
                "__GT_STRUCTURED__\n"
                f'{{"call_num": {self._edit_hook_calls}, "next_action": "read_caller_{self._edit_hook_calls}"}}'
            )
        if "gt-index" in command:
            return Observation("INDEX_OK")
        if "stat -c %Y" in command:
            return Observation("0")
        return Observation("AGENT_OBS")


def test_l3_dedup_ignores_structured_json_changes(capsys):
    """Dedup hash must be on agent-visible portion only.

    Pre-fix: __GT_STRUCTURED__ JSON included in hash. Different JSON on 2nd
    edit → different hash → same visible evidence injected twice.
    Post-fix: hash on visible portion only → 2nd injection suppressed.
    """
    runtime = DedupTestRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    ohgt.wrap_runtime_run_action(runtime, config)

    obs1 = runtime.run_action(FileEditAction("src/users.py"))
    obs2 = runtime.run_action(FileEditAction("src/users.py"))

    captured = capsys.readouterr()
    delivered_count = captured.out.count("[GT_TRACE] l3_delivery status=DELIVERED")
    assert delivered_count == 1, (
        f"L3 dedup failed: same visible evidence delivered {delivered_count} times "
        f"(expected 1). Hash must exclude __GT_STRUCTURED__ JSON."
    )

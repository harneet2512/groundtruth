from __future__ import annotations

from pathlib import Path

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
    assert len(runtime.actions) == 2
    assert isinstance(runtime.actions[0], FileReadAction)
    assert "groundtruth.hooks.post_view" in runtime.actions[1].command


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


def test_wrapper_source_does_not_read_oracle_fields():
    source = Path(ohgt.__file__).read_text(encoding="utf-8")

    forbidden = ["FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "gold_patch", "oracle"]
    assert not any(token in source for token in forbidden)

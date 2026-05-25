#!/usr/bin/env python3
"""GT v1.0.5 wrapper for the SWE-bench-Live/OpenHands fork.

Targets `evaluation.benchmarks.swe_bench.run_infer` (the fork's older
controller-based pattern) instead of the upstream OpenHands SDK pattern.

Layer port status:

  L1 (v8.2.2 RRF localization)  — host-side, using prebuilt graph.db
                                  at GT_PREBUILT_INDEXES_ROOT/<id>/graph.db.
                                  Confidence: high (existing v22_brief.generate_brief).
  L2 (brief delivery)            — patches get_instruction() to prepend the
                                  v8.2.2 brief to the first user-turn content.
                                  Confidence: high (single-injection point).
  L3 (post-edit hook)            — patches initialize_runtime() to runtime.copy_to
                                  gt_hook.py + a polling watcher that re-runs
                                  the hook on .py mtime changes. Confidence: moderate
                                  (depends on python3 + git available in container).
  L4 (gt_lookup/impact/check)    — DEFERRED. Fork hard-codes enable_mcp=False at
                                  run_infer.py:249. Re-enabling requires patching
                                  AgentConfig + registering an MCP server inside
                                  the container, which the OH controller does not
                                  expose cleanly without code changes. Documented
                                  as a known gap for v1.0.6.
  L5 (pre_finish gate)           — DEFERRED. CodeActAgent's `finish` action is
                                  resolved by the controller's main loop; there
                                  is no `pre_finish` hook surface in this fork.
                                  Documented as a known gap for v1.0.6.
  L6 (incremental re-indexing)   — runs *inside* gt_hook.py (already wired via
                                  reindex_helper.check_and_reindex_modified_files
                                  + the gt-index-linux binary copied into the
                                  container under /tmp/gt-index). Confidence:
                                  moderate (depends on gt-index running on the
                                  task's installed Python; should be fine since
                                  it's a static Go binary).

Usage:
    python oh_gt_live_lite_v2_wrapper.py \\
        .llm_config/vertex_qwen3_v105.json \\
        --instance-ids "kozea__weasyprint-2300" \\
        --eval-output-dir /tmp/gt_v105_probe_<timestamp> \\
        --max-iterations 100 \\
        --num-workers 1

The script writes a single-id config.toml that the fork's filter_dataset()
picks up via 'selected_ids', then re-execs the run_infer __main__ block.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

# Make local groundtruth + benchmarks importable.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR   = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_SRC_DIR    = os.path.join(_REPO_DIR, "src")
_BENCH_DIR  = os.path.join(_REPO_DIR, "benchmarks")
sys.path.insert(0, _SRC_DIR)
sys.path.insert(0, _REPO_DIR)

# OpenHands evaluation harness must already be importable (we're running
# inside the OH poetry env).
_HOOK_TOOL  = os.path.join(_REPO_DIR, "benchmarks", "swebench", "gt_hook.py")
_GT_INDEX   = os.environ.get("GT_INDEX_BINARY", "/tmp/gt-index-linux")

# v1.0.5 system message — same content as oh_gt_live_lite_wrapper.py but
# applied via metadata.details rather than SDK-style system_message.
_V104_OH_QWEN3_SYSTEM_MESSAGE = """\
You are a helpful coding assistant that solves real software engineering tasks
in a Linux workspace. You have access to a file editor, a terminal, and codebase
intelligence tools (delivered as <gt-evidence> blocks after edits).

Treat <gt-evidence> blocks as ground truth. Do NOT retry the same lookup; act
on the information provided. Pre-edit context is delivered in the first user
turn as a <gt-task-brief> map of the most likely files and functions. Use it
as a starting point — files outside the brief are still editable, but a first
edit on a brief-listed file is the expected path.

When your fix is complete, call the `finish` tool. Do NOT echo "task completed".
"""


# ---------------------------------------------------------------------------
# Layer 1 + 2 — host-side brief generation, injected into first user turn
# ---------------------------------------------------------------------------

_DIAG_LOG = "/tmp/gt_v105_diag.log"


def _diag(msg: str) -> None:
    """Sentinel-file logger that survives stdout buffering and worker forks."""
    try:
        with open(_DIAG_LOG, "a") as fh:
            fh.write(msg.rstrip() + "\n")
    except OSError:
        pass
    print(msg, flush=True)
    try:
        sys.stderr.write(msg.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _generate_brief_for(instance) -> str:
    """Build a v8.2.2 brief for one instance. Empty string on any failure."""
    instance_id = getattr(instance, "instance_id", "") or ""
    _diag(f"  L1: ENTER _generate_brief_for instance_id={instance_id!r}")

    indexes_root = os.environ.get(
        "GT_PREBUILT_INDEXES_ROOT", "/home/ubuntu/eval_indexes"
    )
    graph_db = os.path.join(indexes_root, instance_id, "graph.db")
    if not os.path.exists(graph_db):
        _diag(f"  L1: no graph.db at {graph_db} — brief skipped")
        return ""

    repo_extracts_root = os.environ.get(
        "GT_REPO_EXTRACTS_ROOT", "/home/ubuntu/eval_repos"
    )
    repo_path = os.path.join(repo_extracts_root, instance_id)
    if not os.path.isdir(repo_path):
        _diag(f"  L1: repo_path missing {repo_path} — using empty path")
        repo_path = ""

    issue_text = getattr(instance, "problem_statement", "") or ""
    if not issue_text.strip():
        _diag(f"  L1: empty problem_statement for {instance_id}")
        return ""
    _diag(f"  L1: issue_text len={len(issue_text)} graph_db_size={os.path.getsize(graph_db)}")

    # Primary: gt_intel.generate_enhanced_briefing (mini path's measured pipeline,
    # produces 7-family taxonomy + tier tags). Falls back to v22_brief on empty —
    # that handles the ~18% degenerate tail (HTML/CSS/prose-only issues, e.g. kozea)
    # where extract_identifiers_from_issue returns no Python-style identifiers.
    brief = ""
    try:
        import sqlite3
        from benchmarks.swebench.gt_intel import (
            extract_identifiers_from_issue,
            generate_enhanced_briefing,
        )
    except ImportError as exc:
        _diag(f"  L1: import gt_intel failed: {exc}")
    else:
        conn = None
        try:
            identifiers = extract_identifiers_from_issue(issue_text)
            _diag(f"  L1: extracted {len(identifiers)} identifiers from issue")
            if identifiers:
                conn = sqlite3.connect(graph_db)
                brief = generate_enhanced_briefing(conn, repo_path, identifiers) or ""
            else:
                _diag(f"  L1: no identifiers extracted for {instance_id} — primary empty")
        except Exception as exc:
            import traceback
            _diag(f"  L1: gt_intel brief gen failed for {instance_id}: {exc}\n{traceback.format_exc()}")
        finally:
            if conn is not None:
                conn.close()

    # Fallback: v22_brief on empty primary. RRF map-only (no family tags),
    # but handles non-Python-identifier issues. Logged as L1_FALLBACK for
    # downstream rate accounting.
    if not brief.strip():
        _diag(f"  L1_FALLBACK: gt_intel empty for {instance_id} — trying v22_brief")
        try:
            from groundtruth.pretask.v22_brief import generate_brief as _v22_generate_brief
            brief = _v22_generate_brief(issue_text, repo_path, graph_db) or ""
            if brief.strip():
                _diag(f"  L1_FALLBACK: v22_brief produced {len(brief)} chars")
            else:
                _diag(f"  L1_FALLBACK: v22_brief also empty for {instance_id}")
        except Exception as exc:
            _diag(f"  L1_FALLBACK: v22_brief failed: {exc}")

    _diag(f"  L1: brief generated len={len(brief or '')}")
    return brief or ""


# Module-level original-function holder so multiprocessing can pickle the
# patched wrappers (closures over `original` are not pickleable).
_ORIG_GET_INSTRUCTION = None
_ORIG_INITIALIZE_RUNTIME = None
_ORIG_PROCESS_INSTANCE = None
_HOOK_BYTES = b""


def patched_get_instruction(instance, metadata):
    """Module-level patched get_instruction (pickleable for multiprocessing)."""
    iid = getattr(instance, "instance_id", "?")
    _diag(f"  L2: ENTER patched_get_instruction iid={iid!r} orig={_ORIG_GET_INSTRUCTION!r}")
    if _ORIG_GET_INSTRUCTION is None:
        _diag("  L2: _ORIG_GET_INSTRUCTION is None — patch did NOT propagate to worker")
        raise RuntimeError("L2 patch lost in worker process")
    brief = _generate_brief_for(instance)
    msg_action = _ORIG_GET_INSTRUCTION(instance, metadata)
    if brief:
        try:
            msg_action.content = (
                "<gt-task-brief>\n" + brief.strip() + "\n</gt-task-brief>\n\n"
                + (msg_action.content or "")
            )
            _diag(f"  L2: brief delivered ({len(brief)} chars) for {iid}; new_content_len={len(msg_action.content)}")
        except Exception as exc:
            _diag(f"  L2: msg_action.content set failed: {exc}; falling back to MessageAction(content=...)")
            from openhands.events.action import MessageAction
            new_content = (
                "<gt-task-brief>\n" + brief.strip() + "\n</gt-task-brief>\n\n"
                + (getattr(msg_action, "content", "") or "")
            )
            extra = {}
            if getattr(msg_action, "image_urls", None):
                extra["image_urls"] = msg_action.image_urls
            msg_action = MessageAction(content=new_content, **extra)
            _diag(f"  L2: rebuilt MessageAction len={len(new_content)}")
    else:
        _diag(f"  L2: brief empty for {iid} — not delivered")
    return msg_action


def _patch_get_instruction(ri_module):
    """Wrap ri.get_instruction so the brief is prepended to the first user turn."""
    global _ORIG_GET_INSTRUCTION
    _ORIG_GET_INSTRUCTION = ri_module.get_instruction
    ri_module.get_instruction = patched_get_instruction


# ---------------------------------------------------------------------------
# Layer 3 + 6 — gt_hook.py + watcher + gt-index binary inside container
# ---------------------------------------------------------------------------

def _b64_chunks(payload: bytes, chunk: int = 8000) -> list[str]:
    encoded = base64.b64encode(payload).decode("ascii")
    return [encoded[i : i + chunk] for i in range(0, len(encoded), chunk)]


_WATCHER_SCRIPT = r"""
import json, os, subprocess, sys, time

WATCH_DIR = "/workspace"
GT_INDEX = "/tmp/gt-index-linux"
GRAPH_DB = "/tmp/gt_index.db"
HOOK_CMD = ["python3", "/tmp/gt_hook.py", "--root=/workspace",
            "--db=" + GRAPH_DB, "--quiet", "--max-items=3"]
POLL = 2
LOG = "/tmp/gt_hook_stdout.log"
SENTINEL = "/tmp/gt_watcher_alive.sentinel"
FRESHNESS = "/tmp/gt_index_freshness.json"

# Sentinel-on-entry: prove the watcher actually started, regardless of
# parent's pgrep timing. Wrapper polls this file in a retry loop.
try:
    with open(SENTINEL, "w") as fh:
        fh.write("pid={} ts={:.1f}\n".format(os.getpid(), time.time()))
    with open(LOG, "a") as fh:
        fh.write("---\nWATCHER_BOOT pid={} ts={:.1f}\n".format(os.getpid(), time.time()))
except Exception as exc:
    sys.stderr.write("watcher sentinel write failed: {}\n".format(exc))


def reindex(reason):
    # Run gt-index full against /workspace into /tmp/gt_index.db.
    # Sub-second on typical task repos. Functionally equivalent to
    # per-file --incremental for hook query freshness.
    if not os.path.exists(GT_INDEX):
        return {"ok": False, "reason": "gt-index_missing"}
    t0 = time.time()
    try:
        res = subprocess.run(
            [GT_INDEX, "-root=" + WATCH_DIR, "-output=" + GRAPH_DB],
            capture_output=True, text=True, timeout=120,
        )
        dt = time.time() - t0
        ok = res.returncode == 0 and os.path.exists(GRAPH_DB)
        size = os.path.getsize(GRAPH_DB) if ok else 0
        record = {
            "ts": t0, "reason": reason, "elapsed_s": round(dt, 3),
            "ok": ok, "rc": res.returncode, "graph_db_size": size,
        }
        try:
            with open(FRESHNESS, "a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass
        return record
    except Exception as exc:
        return {"ok": False, "reason": "exception:{}".format(exc)}


def snapshot():
    out = {}
    for root, _, files in os.walk(WATCH_DIR):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(root, f)
            try:
                out[p] = os.path.getmtime(p)
            except OSError:
                pass
    return out


# --- Initial baseline reindex so first hook fire reads real graph data
boot_r = reindex("watcher_boot")
with open(LOG, "a") as fh:
    fh.write("---\nL6_BOOT_REINDEX {}\n".format(json.dumps(boot_r)))

prev = snapshot()
while True:
    time.sleep(POLL)
    cur = snapshot()
    changed = [p for p, m in cur.items() if prev.get(p) != m]
    if changed:
        with open(LOG, "a") as fh:
            fh.write("---\nfired_for={}\n".format(changed[:5]))

        # L6 — refresh graph.db before evidence query
        r = reindex("post_edit:n={}".format(len(changed)))
        with open(LOG, "a") as fh:
            fh.write("L6_REINDEX {}\n".format(json.dumps(r)))

        # L3 — emit OH families from gt_hook.py
        try:
            res = subprocess.run(HOOK_CMD, capture_output=True, text=True, timeout=20)
            with open(LOG, "a") as fh:
                fh.write(res.stdout or "")
                if res.stderr:
                    fh.write("[stderr]\n" + res.stderr)
        except Exception as exc:
            with open(LOG, "a") as fh:
                fh.write("[hook-error] {}\n".format(exc))
    prev = cur
"""


def patched_initialize_runtime(runtime, instance, metadata):
    """Module-level patched initialize_runtime (pickleable for multiprocessing)."""
    _ORIG_INITIALIZE_RUNTIME(runtime, instance, metadata)

    from openhands.events.action import CmdRunAction

    # Step 1: ship gt_hook.py via base64 chunks
    chunks = _b64_chunks(_HOOK_BYTES)
    for i, chunk in enumerate(chunks):
        op = ">" if i == 0 else ">>"
        cmd = CmdRunAction(command=f"echo -n '{chunk}' {op} /tmp/gt_hook.b64")
        cmd.set_hard_timeout(120)
        obs = runtime.run_action(cmd)
        if getattr(obs, "exit_code", -1) != 0:
            print(f"  L3: gt_hook.py chunk {i} write failed for {instance.instance_id}", flush=True)
            return

    cmd = CmdRunAction(
        command=(
            "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && "
            "chmod +x /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64 && "
            "echo GT_HOOK_READY"
        )
    )
    cmd.set_hard_timeout(120)
    obs = runtime.run_action(cmd)
    if "GT_HOOK_READY" not in (getattr(obs, "content", "") or ""):
        print(f"  L3: gt_hook.py decode uncertain for {instance.instance_id}", flush=True)

    # Step 2: ship gt-index binary (Layer 6)
    if os.path.exists(_GT_INDEX):
        try:
            runtime.copy_to(_GT_INDEX, "/tmp/")
            cmd = CmdRunAction(command="chmod +x /tmp/gt-index-linux && echo GT_INDEX_READY")
            cmd.set_hard_timeout(60)
            obs = runtime.run_action(cmd)
            if "GT_INDEX_READY" not in (getattr(obs, "content", "") or ""):
                print(f"  L6: gt-index install uncertain for {instance.instance_id}", flush=True)
        except Exception as exc:
            print(f"  L6: gt-index copy failed: {exc}", flush=True)

    # Step 3: ship + start the watcher in the background, then verify by
    # polling the watcher's sentinel file (replaces the racy pgrep pattern).
    # Env-flag: GT_DISABLE_WATCHER=1 skips this step (B-probe isolation).
    if os.environ.get("GT_DISABLE_WATCHER", "0") == "1":
        _diag(f"  L3: GT_DISABLE_WATCHER=1 — skipping watcher install for {instance.instance_id}")
    else:
        watcher_b64 = base64.b64encode(_WATCHER_SCRIPT.encode("utf-8")).decode("ascii")
        cmd = CmdRunAction(
            command=(
                f"rm -f /tmp/gt_watcher_alive.sentinel && "
                f"echo -n '{watcher_b64}' | base64 -d > /tmp/gt_watcher.py && "
                "setsid nohup python3 /tmp/gt_watcher.py "
                "> /tmp/gt_watcher.out 2>&1 < /dev/null & "
                "disown -a 2>/dev/null; "
                "for i in 1 2 3 4 5 6 7 8 9 10; do "
                "  if [ -f /tmp/gt_watcher_alive.sentinel ]; then "
                "    cat /tmp/gt_watcher_alive.sentinel; "
                "    echo GT_WATCHER_LIVE; exit 0; "
                "  fi; "
                "  sleep 0.5; "
                "done; "
                "echo GT_WATCHER_TIMEOUT; "
                "cat /tmp/gt_watcher.out 2>&1 | head -20"
            )
        )
        cmd.set_hard_timeout(60)
        obs = runtime.run_action(cmd)
        obs_content = getattr(obs, "content", "") or ""
        if "GT_WATCHER_LIVE" in obs_content:
            print(f"  L3: gt_hook + watcher live for {instance.instance_id}", flush=True)
        else:
            print(
                f"  L3: watcher start FAILED for {instance.instance_id} — "
                f"obs_tail={obs_content[-300:]!r}",
                flush=True,
            )

    # ---- L3 push channel: instance-level monkey-patch on runtime.run_action ----
    # OH 0.54.0 has no HookManager / hooks.json. The watcher above is telemetry-only
    # (writes /tmp/gt_hook_stdout.log inside container, which the agent never reads).
    # To deliver hook output back to the agent, we wrap run_action: when the agent
    # performs a file edit on a non-test source file, we run gt-index reindex (L6)
    # then gt_hook.py inline, then APPEND the evidence to the observation the agent
    # receives. This gives the agent the post-edit signal as part of the tool_result.
    # Env-flag: GT_DISABLE_L3_PUSH=1 skips the monkey-patch (B-probe isolation).
    if os.environ.get("GT_DISABLE_L3_PUSH", "0") == "1":
        _diag(f"  L3_PUSH: GT_DISABLE_L3_PUSH=1 — skipping run_action monkey-patch for {instance.instance_id}")
    else:
        _install_runtime_run_action_wrapper(runtime, instance)


_TEST_PATH_RE = __import__("re").compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|(^|/)[^/]*_test\.py$"
)
_MUTATING_EDITOR_CMDS = frozenset({"create", "str_replace", "insert"})
_EDIT_ACTION_CLASSES = frozenset({"FileEditAction", "FileWriteAction", "CmdRunAction"})
# Action classes whose skip we DO want logged for filter-observability — especially
# FileReadAction so reviewers can see "read was filtered, not just edits."
_LOGGED_SKIP_CLASSES = frozenset({"FileReadAction", "FileWriteAction", "FileEditAction", "CmdRunAction"})


def _action_is_source_edit(action) -> tuple[bool, str, str]:
    """Detect file_editor / file_write / str_replace_editor MUTATIONS on non-test source files.

    Skips read-only operations (view, undo_edit, FileReadAction, browse, etc.) and test-file paths.
    Returns (is_edit, edited_path, skip_reason).
    skip_reason is "" when is_edit=True; otherwise one of:
      - "non_edit_class"      → action class not in edit allow-list (e.g. FileReadAction)
      - "no_editor_invocation"→ CmdRunAction without str_replace_editor/file_editor verb
      - "no_path"             → FileEdit/Write/CmdRun without resolvable path
      - "non_mutating_verb:V" → str_replace_editor view/undo_edit (V = the verb)
      - "non_source_ext"      → CmdRun with non-source-extension path
      - "test_path"           → path matches test/spec pattern
    """
    cls_name = type(action).__name__
    if cls_name not in _EDIT_ACTION_CLASSES:
        return False, "", "non_edit_class"
    path = ""
    if cls_name in ("FileEditAction", "FileWriteAction"):
        for attr in ("path", "file_path"):
            if hasattr(action, attr):
                path = getattr(action, attr) or ""
                if path:
                    break
        if not path:
            return False, "", "no_path"
    elif cls_name == "CmdRunAction":
        cmd = getattr(action, "command", "") or ""
        m = __import__("re").search(
            r"(?:str_replace_editor|file_editor)\s+(\S+)\s+(\S+)", cmd
        )
        if not m:
            return False, "", "no_editor_invocation"
        verb, candidate = m.group(1), m.group(2)
        if verb not in _MUTATING_EDITOR_CMDS:
            return False, candidate, f"non_mutating_verb:{verb}"
        path = candidate
    if not path:
        return False, "", "no_path"
    if cls_name not in ("FileEditAction", "FileWriteAction") and not path.endswith(
        (".py", ".js", ".ts", ".go", ".java", ".rs", ".rb", ".php")
    ):
        return False, path, "non_source_ext"
    if _TEST_PATH_RE.search(path):
        return False, path, "test_path"
    return True, path, ""


def _install_runtime_run_action_wrapper(runtime, instance):
    """Wrap runtime.run_action to inject GT evidence after source edits.

    L3 push channel: agent's tool_result for an edit gets gt_hook.py output appended.
    L6 reindex:      gt-index runs against /workspace before the hook query.
    """
    iid = getattr(instance, "instance_id", "?")
    try:
        from openhands.events.action import CmdRunAction
    except Exception as exc:
        _diag(f"  L3_PUSH: import CmdRunAction failed for {iid}: {exc}")
        return

    if getattr(runtime, "_gt_run_action_wrapped", False):
        _diag(f"  L3_PUSH: runtime.run_action already wrapped for {iid}")
        return

    orig_run_action = runtime.run_action
    edit_count = {"n": 0}

    def _exec(cmd_str: str, timeout: int = 30) -> str:
        cmd = CmdRunAction(command=cmd_str)
        cmd.set_hard_timeout(timeout)
        obs = orig_run_action(cmd)
        return getattr(obs, "content", "") or ""

    def patched_run_action(action):
        obs = orig_run_action(action)
        try:
            is_edit, edited_path, skip_reason = _action_is_source_edit(action)
            if not is_edit:
                # Near-miss skip logging — emit only for action classes we care about
                # (FileReadAction, FileEditAction on test paths, CmdRunAction with
                # str_replace_editor view/undo_edit, etc.). Silent for the floods:
                # MessageAction, AgentDelegateAction, BrowseAction, plain CmdRun w/o editor.
                cls = type(action).__name__
                if cls in _LOGGED_SKIP_CLASSES and skip_reason not in (
                    "no_editor_invocation",  # plain bash CmdRun — silent (would flood)
                ):
                    _diag(
                        f"  L3_FILTER[{iid}]: SKIP cls={cls} "
                        f"path={edited_path or '-'} reason={skip_reason}"
                    )
                return obs
            edit_count["n"] += 1
            n = edit_count["n"]
            _diag(f"  L3_PUSH[{iid}]: edit #{n} path={edited_path}")

            # L6 — reindex before evidence query so graph.db reflects post-edit state
            t0 = __import__("time").time()
            reindex_out = _exec(
                "/tmp/gt-index-linux -root=/workspace -output=/tmp/gt_index.db "
                "2>&1 | tail -1; echo REINDEX_RC=$?",
                timeout=120,
            )
            reindex_dt = __import__("time").time() - t0

            # L3 — fire gt_hook.py inline against the fresh graph.db
            hook_out = _exec(
                "python3 /tmp/gt_hook.py --root=/workspace --db=/tmp/gt_index.db "
                f"--quiet --max-items=3 --file={edited_path} 2>&1 | head -200",
                timeout=30,
            )

            # Append GT evidence + freshness note to the agent-visible observation
            current = getattr(obs, "content", "") or ""
            evidence = (
                "\n\n<gt-evidence trigger=\"post_edit:" + edited_path + "\" "
                f"reindex_ms=\"{int(reindex_dt*1000)}\">\n"
                + hook_out.strip()
                + "\n</gt-evidence>\n"
            )
            try:
                obs.content = current + evidence
            except Exception:
                pass  # Some observations may freeze content; degrade gracefully

            # Telemetry — append to the same hook log the watcher uses, so
            # patched_complete_runtime extracts it post-task.
            log_line = "\\n---\\nrun_action_push edit_n=%d path=%s reindex_ms=%d hook_chars=%d" % (
                n, edited_path, int(reindex_dt * 1000), len(hook_out),
            )
            _exec("printf '%b' " + repr(log_line) + " >> /tmp/gt_hook_stdout.log; true", timeout=10)
        except Exception as exc:
            _diag(f"  L3_PUSH[{iid}] exception: {exc}")
        return obs

    runtime.run_action = patched_run_action  # type: ignore[assignment]
    runtime._gt_run_action_wrapped = True
    _diag(f"  L3_PUSH: runtime.run_action wrapped for {iid}")


def _patch_initialize_runtime(ri_module):
    """Wire the module-level patched initialize_runtime."""
    global _ORIG_INITIALIZE_RUNTIME, _HOOK_BYTES

    if not os.path.exists(_HOOK_TOOL):
        print(f"  L3: gt_hook.py missing at {_HOOK_TOOL} — L3 disabled")
        return
    if not os.path.exists(_GT_INDEX):
        print(f"  L6: gt-index missing at {_GT_INDEX} — L6 disabled (hook still runs without reindex)")

    with open(_HOOK_TOOL, "rb") as fh:
        _HOOK_BYTES = fh.read()
    _ORIG_INITIALIZE_RUNTIME = ri_module.initialize_runtime
    ri_module.initialize_runtime = patched_initialize_runtime


# ---------------------------------------------------------------------------
# Wrapper for process_instance — drains telemetry artifacts after agent finish
# ---------------------------------------------------------------------------

def patched_process_instance(instance, metadata, reset_logger=True, runtime_failure_count=0):
    """Module-level patched process_instance (pickleable for multiprocessing)."""
    return _ORIG_PROCESS_INSTANCE(instance, metadata, reset_logger, runtime_failure_count)


def _patch_process_instance(ri_module):
    """Wire the module-level patched process_instance."""
    global _ORIG_PROCESS_INSTANCE
    _ORIG_PROCESS_INSTANCE = ri_module.process_instance
    ri_module.process_instance = patched_process_instance


# ---------------------------------------------------------------------------
# patched_complete_runtime — runs INSIDE process_instance, AFTER the agent
# loop finishes but BEFORE runtime.close(). This is our last chance to pull
# telemetry artifacts (gt_hook_stdout.log, watcher.out) out of the container.
# ---------------------------------------------------------------------------

_ORIG_COMPLETE_RUNTIME = None
_TELEMETRY_HOST_ROOT = os.environ.get(
    "GT_TELEMETRY_HOST_ROOT", "/tmp/gt_telemetry"
)


def patched_complete_runtime(runtime, instance):
    """Extract GT telemetry from the runtime container before teardown."""
    iid = getattr(instance, "instance_id", "?")
    out_dir = os.path.join(_TELEMETRY_HOST_ROOT, iid)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        _diag(f"  TELEM: cannot create {out_dir}: {exc}")

    _diag(f"  TELEM: ENTER patched_complete_runtime iid={iid!r} out_dir={out_dir}")

    # Files we care about. Some may not exist (watcher never fired, etc.) —
    # tolerate per-file failure rather than aborting the whole drain.
    targets = [
        "/tmp/gt_hook_stdout.log",
        "/tmp/gt_watcher.out",
        "/tmp/gt_watcher_alive.sentinel",
        "/tmp/gt_index.db",
        "/tmp/gt_calls.json",
        "/tmp/gt_check_log.jsonl",
    ]
    extracted = {}
    for path in targets:
        try:
            host_path = runtime.copy_from(path)
        except Exception as exc:
            _diag(f"  TELEM: copy_from({path}) failed: {exc}")
            continue
        try:
            import shutil
            dest = os.path.join(out_dir, os.path.basename(path))
            # copy_from may return a temp file path; preserve as-is.
            shutil.copy2(str(host_path), dest)
            extracted[os.path.basename(path)] = os.path.getsize(dest)
        except Exception as exc:
            _diag(f"  TELEM: shutil.copy2({host_path}) failed: {exc}")

    _diag(f"  TELEM: extracted={extracted}")

    # Quick summary so the wrapper.log proves what happened, even without
    # a downstream analyzer running.
    hook_log = os.path.join(out_dir, "gt_hook_stdout.log")
    if os.path.exists(hook_log):
        try:
            content = open(hook_log, errors="replace").read()
            fires = content.count("---\nfired_for=")
            boots = content.count("WATCHER_BOOT")
            _diag(f"  TELEM: gt_hook_stdout.log size={len(content)} fires={fires} boots={boots}")
        except Exception:
            pass

    return _ORIG_COMPLETE_RUNTIME(runtime, instance)


def _patch_complete_runtime(ri_module):
    """Wire the module-level patched complete_runtime."""
    global _ORIG_COMPLETE_RUNTIME
    _ORIG_COMPLETE_RUNTIME = ri_module.complete_runtime
    ri_module.complete_runtime = patched_complete_runtime


# ---------------------------------------------------------------------------
# Single-instance config.toml + delegate to the fork's __main__
# ---------------------------------------------------------------------------

def _write_single_id_config(instance_id: str, swebench_dir: str) -> None:
    config_path = os.path.join(swebench_dir, "config.toml")
    with open(config_path, "w") as f:
        f.write(f'selected_ids = ["{instance_id}"]\n')
    print(f"  config.toml written: selected_ids=[{instance_id}]")


def _parse_condenser_config(
    condenser_name: str | None,
    get_condenser_config_arg,
    NoOpCondenserConfig,
):
    """Parse EVAL_CONDENSER env var into a condenser config object.

    Supports extended format: ``recent_events:keep_first=5,max_events=15``
    which OH's ``get_condenser_config_arg`` may not handle natively.
    Falls back to ``get_condenser_config_arg`` for simple formats.
    """
    if not condenser_name:
        return NoOpCondenserConfig() if NoOpCondenserConfig else None

    # Extended format: "recent_events:key=val,key=val"
    if ":" in condenser_name and "=" in condenser_name:
        ctype, params_str = condenser_name.split(":", 1)
        params = {}
        for part in params_str.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    params[k.strip()] = int(v.strip())
                except ValueError:
                    params[k.strip()] = v.strip()
        try:
            if ctype == "recent_events":
                from openhands.core.config.condenser_config import RecentEventsCondenserConfig
                return RecentEventsCondenserConfig(**params)
        except (ImportError, TypeError) as exc:
            print(f"[GT_META] condenser extended parse failed ({exc}), falling back", flush=True)

    # Simple format: "recent_events:5" or "noop"
    if get_condenser_config_arg:
        return get_condenser_config_arg(condenser_name)
    return NoOpCondenserConfig() if NoOpCondenserConfig else None


def main() -> None:
    # Parse our wrapper-specific args, leave the rest for the fork's parser.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--instance-ids", default="")
    wrapper_args, remainder = parser.parse_known_args()

    # Locate the fork's run_infer module (must be importable from the OH repo).
    try:
        from evaluation.benchmarks.swe_bench import run_infer as ri  # type: ignore[import]
    except ImportError as exc:
        print(f"FATAL: cannot import evaluation.benchmarks.swe_bench.run_infer: {exc}")
        print("Hint: run this wrapper from inside the OH repo (cd /home/ubuntu/OpenHands && poetry run python ...)")
        sys.exit(1)

    # Apply patches BEFORE __main__ executes.
    _patch_get_instruction(ri)
    _patch_initialize_runtime(ri)
    _patch_process_instance(ri)
    _patch_complete_runtime(ri)
    print("  patches applied: get_instruction, initialize_runtime, process_instance, complete_runtime")

    # Single-instance config.toml so filter_dataset narrows the run to one task.
    if wrapper_args.instance_ids:
        ids = [s.strip() for s in wrapper_args.instance_ids.split(",") if s.strip()]
        if len(ids) == 1:
            ri_dir = os.path.dirname(os.path.abspath(ri.__file__))
            _write_single_id_config(ids[0], ri_dir)
        else:
            # Multi-id path: write a config.toml with the full list
            ri_dir = os.path.dirname(os.path.abspath(ri.__file__))
            config_path = os.path.join(ri_dir, "config.toml")
            with open(config_path, "w") as f:
                f.write("selected_ids = " + json.dumps(ids) + "\n")

    # Hand off to the fork's __main__ logic.
    # IMPORTANT: do NOT use runpy.run_module — it re-executes the module's
    # top-level code, which redefines get_instruction/initialize_runtime/
    # process_instance and CLOBBERS our patches. Instead, replicate the
    # __main__ block here, calling into the (now-patched) module attributes.
    sys.argv = ["run_infer.py"] + remainder

    import openhands.agenthub  # noqa: F401  — needed for Agent.get_cls
    from datasets import load_dataset
    from openhands.core.config import (
        get_llm_config_arg,
        get_parser,
    )
    from openhands.core.config.condenser_config import NoOpCondenserConfig
    from openhands.core.config.utils import get_condenser_config_arg
    from evaluation.utils.shared import (
        make_metadata,
        prepare_dataset,
        run_evaluation,
    )

    parser = get_parser()
    parser.add_argument("--dataset", type=str, default="princeton-nlp/SWE-bench")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--mode", type=str, default="swe", choices=["swe", "swt", "swt-ci"])
    args, _ = parser.parse_known_args()

    dataset = load_dataset(args.dataset, split=args.split)
    ri.set_dataset_type(args.dataset)
    swe_bench_tests = ri.filter_dataset(dataset.to_pandas(), "instance_id")
    print(f"  dataset loaded: {len(swe_bench_tests)} task(s) after filter")

    llm_config = get_llm_config_arg(args.llm_config) if args.llm_config else None
    if llm_config is None:
        raise ValueError(f"Missing or unknown llm_config: {args.llm_config}")
    llm_config.log_completions = True
    llm_config.modify_params = False

    condenser_name = os.environ.get("EVAL_CONDENSER")
    condenser_config = (
        _parse_condenser_config(condenser_name, get_condenser_config_arg, NoOpCondenserConfig)
    )

    details = {"mode": args.mode}
    dataset_descrption = (
        args.dataset.replace("/", "__") + "-" + args.split.replace("/", "__")
    )
    metadata = make_metadata(
        llm_config,
        dataset_descrption,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
        details=details,
        condenser_config=condenser_config,
    )

    output_file = os.path.join(metadata.eval_output_dir, "output.jsonl")
    print(f"### OUTPUT FILE: {output_file} ###")

    instances = prepare_dataset(swe_bench_tests, output_file, args.eval_n_limit)
    if len(instances) > 0 and not isinstance(
        instances["PASS_TO_PASS"][instances["PASS_TO_PASS"].index[0]], str
    ):
        for col in ["PASS_TO_PASS", "FAIL_TO_PASS"]:
            instances[col] = instances[col].apply(lambda x: str(x))

    # NOTE: ri.process_instance is now our patched version. run_evaluation
    # will call it for each instance. Patches are preserved.
    run_evaluation(
        instances,
        metadata,
        output_file,
        args.eval_num_workers,
        ri.process_instance,
    )


if __name__ == "__main__":
    main()

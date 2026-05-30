#!/usr/bin/env python3
"""Standalone OH+GT driver for a single SWE-bench-Live task on a LOCAL runtime.

WHY THIS EXISTS
---------------
The production path (`scripts/swebench/oh_gt_full_wrapper.py`) is bound to the
OpenHands 0.54 SWE-bench eval harness:

    from evaluation.benchmarks.swe_bench import run_infer
    from evaluation.utils.shared import make_metadata, prepare_dataset, run_evaluation

That harness's `initialize_runtime()` does:

    runtime.run_action("source /swe_util/instance_swe_entry_live.sh")
    runtime.run_action("cd /workspace/<instance_id>")
    runtime.run_action("git reset --hard")

i.e. it assumes the SWE-bench-Live **Docker image** layout: repo pre-checked-out
at /workspace/<id>, conda env + per-instance setup script baked at /swe_util/.
Railway blocks Docker-in-Docker, so that path cannot run there.

THIS DRIVER replaces ONLY the harness's image-dependent setup with a local
equivalent, and reuses the GT wrapper's hook layer UNCHANGED:

  1. Build an OpenHandsConfig with runtime="local" (LocalRuntime: a bash
     action-execution server in THIS process, no Docker).
  2. Point workspace_base at /workspace (repo already cloned by the Dockerfile
     at /workspace/beetbox__beets-5495 and `pip install -e .`'d).
  3. create_runtime(config, ...) -> LocalRuntime; runtime.connect().
  4. Install the SAME GT hooks the wrapper installs, by calling the wrapper's
     `patched_initialize_runtime(runtime, instance, metadata)` (which runs
     install_graph_and_hook -> wrap_runtime_run_action) — the hooks monkeypatch
     `runtime.run_action`, which LocalRuntime inherits from the base Runtime, so
     GT view/edit hooks fire on local bash exactly as they do on Docker.
  5. Build the issue instruction via the wrapper's `patched_get_instruction`.
  6. run_controller(config, MessageAction(issue), runtime=runtime) — the agent
     loop. CodeActAgent edits files under /workspace, GT injects on each view/edit.
  7. Write output.jsonl (history -> the same record shape the eval harness emits,
     including git_patch) so convert_to_submission.py / the SWE-bench eval can
     score it OFF Railway.

This is NOT a custom eval. No FAIL_TO_PASS, no gold files, no test_patch. The
gold-test evaluation runs separately via the standard SWE-bench-Live harness on
the emitted predictions. This driver only produces the agent trajectory + patch.

VERIFICATION STATUS: this is a build-feasibility scaffold. The exact 0.54
create_runtime/run_controller call shapes were read from the 0.54.0 source
(openhands/core/main.py, openhands/runtime/__init__.py); first `railway up`
with RUN_SMOKE=1 is what proves it end-to-end. See railway/DEPLOY.md TOP RISKS.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

INSTANCE_ID = os.environ.get("GT_INSTANCE_ID", "beetbox__beets-5495")
REPO = os.environ.get("GT_REPO", "beetbox/beets")
BASE_COMMIT = os.environ.get(
    "GT_BASE_COMMIT", "fa10dcf11add0afd3b4b22af29f8d504e7ef8a0a"
)
WORKSPACE_ROOT = os.environ.get("GT_WORKSPACE_ROOT", "/workspace")
WORKSPACE_DIR = f"{WORKSPACE_ROOT}/{INSTANCE_ID}"
DATASET = os.environ.get("GT_DATASET", "SWE-bench-Live/SWE-bench-Live")
SPLIT = os.environ.get("GT_SPLIT", "lite")
OUT_DIR = os.environ.get("GT_OUT_DIR", "/workspace/results")
MAX_ITER = int(os.environ.get("GT_MAX_ITER", "100"))
CONFIG_TOML = os.environ.get("GT_CONFIG_TOML", "/app/railway/config.toml")


def _log(msg: str) -> None:
    print(f"[run_one_task] {msg}", flush=True)


def load_instance() -> dict[str, Any]:
    """Load the single SWE-bench-Live instance row (problem_statement + commit).

    Dataset metadata only (problem_statement, base_commit). No gold patch, no
    test_patch, no FAIL_TO_PASS pulled into product logic.
    """
    from datasets import load_dataset  # type: ignore

    # STREAMING: a tiny Railway container OOM-kills (rc=137) on a non-streaming
    # load of the full 1000-row split ("Generating test split 0/1000"). Stream
    # the split lazily and stop at the one instance — bounded memory.
    try:
        ds = load_dataset(DATASET, split=SPLIT, streaming=True)
        for row in ds:
            if row.get("instance_id") == INSTANCE_ID:
                return dict(row)
        raise SystemExit(f"FATAL: {INSTANCE_ID} not found in {DATASET}:{SPLIT} (streamed)")
    except SystemExit:
        raise
    except Exception as exc:  # streaming unsupported -> bounded fallback
        _log(f"streaming load failed ({exc}); falling back to non-streaming")
        ds = load_dataset(DATASET, split=SPLIT)
        for row in ds:
            if row.get("instance_id") == INSTANCE_ID:
                return dict(row)
        raise SystemExit(f"FATAL: {INSTANCE_ID} not found in {DATASET}:{SPLIT}")


def ensure_workspace(instance: dict[str, Any]) -> None:
    """Guarantee the repo is checked out at BASE_COMMIT in WORKSPACE_DIR.

    Normally the Dockerfile already did the clone + checkout + `pip install -e .`.
    This is a defensive re-check so the driver also works if the image baked a
    bare clone or the entrypoint is re-run.
    """
    git_dir = Path(WORKSPACE_DIR) / ".git"
    if not git_dir.is_dir():
        _log(f"workspace {WORKSPACE_DIR} has no .git — cloning {REPO}")
        Path(WORKSPACE_ROOT).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", f"https://github.com/{REPO}.git", WORKSPACE_DIR],
            check=True,
        )
    base = instance.get("base_commit") or BASE_COMMIT
    _log(f"checking out base_commit {base} in {WORKSPACE_DIR}")
    subprocess.run(["git", "fetch", "--all", "--quiet"], cwd=WORKSPACE_DIR, check=False)
    subprocess.run(["git", "checkout", "-f", base], cwd=WORKSPACE_DIR, check=True)
    subprocess.run(["git", "reset", "--hard", base], cwd=WORKSPACE_DIR, check=True)
    # Drop remotes so the agent can't `git pull` future fixes (matches harness).
    for remote in subprocess.run(
        ["git", "remote"], cwd=WORKSPACE_DIR, capture_output=True, text=True
    ).stdout.split():
        subprocess.run(["git", "remote", "remove", remote], cwd=WORKSPACE_DIR, check=False)


def build_config() -> Any:
    """Build an OpenHandsConfig with runtime=local + the [llm.eval] section.

    Reads railway/config.toml (runtime=local, no runtime_container_image) and the
    DeepSeek key from $DEEPSEEK_API_KEY. Mirrors the GHA [llm.eval] config so the
    model behaves identically; only the sandbox/runtime differs (local vs docker).
    """
    import toml  # type: ignore
    from openhands.core.config import OpenHandsConfig, LLMConfig  # type: ignore

    raw = toml.load(CONFIG_TOML)
    cfg = OpenHandsConfig()

    # --- runtime: force LOCAL, never docker ---
    cfg.runtime = "local"
    cfg.workspace_base = WORKSPACE_ROOT
    # LocalRuntime runs the agent in workspace_base; the agent cd's into the repo.
    if hasattr(cfg, "workspace_mount_path"):
        cfg.workspace_mount_path = WORKSPACE_ROOT
    cfg.max_iterations = MAX_ITER
    core = raw.get("core", {})
    cfg.default_agent = core.get("default_agent", "CodeActAgent")
    if hasattr(cfg, "sandbox"):
        # Never set runtime_container_image — that would re-engage Docker.
        cfg.sandbox.runtime_container_image = None
        if hasattr(cfg.sandbox, "local_runtime_url"):
            cfg.sandbox.local_runtime_url = os.environ.get(
                "OH_LOCAL_RUNTIME_URL", "http://localhost"
            )

    # --- LLM: [llm.eval] from the TOML, key from env ---
    sec = dict(raw.get("llm", {}).get("eval", {}))
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise SystemExit("FATAL: DEEPSEEK_API_KEY not set in env")
    model = sec.pop("model", "deepseek/deepseek-v4-flash")
    # api_key may be a ${DEEPSEEK_API_KEY} placeholder in the TOML — override.
    sec.pop("api_key", None)
    llm = LLMConfig(model=model, api_key=api_key, **{
        k: v for k, v in sec.items() if k not in {"vertex_project", "vertex_location"}
    })
    llm.log_completions = True
    # OH's LLM.__init__ does os.makedirs(log_completions_folder). Its default path
    # is not writable by the non-root `openhands` user -> PermissionError at
    # create_runtime. Point it at the writable results dir (chowned to openhands).
    _comp_dir = os.path.join(OUT_DIR, "completions")
    try:
        os.makedirs(_comp_dir, exist_ok=True)
    except Exception:
        _comp_dir = "/tmp/gt_completions"
        os.makedirs(_comp_dir, exist_ok=True)
    if hasattr(llm, "log_completions_folder"):
        llm.log_completions_folder = _comp_dir
    llm.modify_params = False
    if hasattr(llm, "reasoning_effort"):
        llm.reasoning_effort = None
    if hasattr(llm, "enable_thinking"):
        llm.enable_thinking = False
    # OH 0.54: LLM configs live in a dict keyed by name.
    try:
        cfg.set_llm_config(llm)  # some 0.54 builds
    except Exception:
        pass
    if hasattr(cfg, "llms") and isinstance(cfg.llms, dict):
        cfg.llms["llm"] = llm
        cfg.llms["eval"] = llm
    return cfg, llm


def install_gt_hooks(runtime: Any, instance: dict[str, Any], metadata: Any) -> str:
    """Install GT hooks by calling the wrapper's patched_initialize_runtime.

    This is the SAME code path the production wrapper uses (install_graph_and_hook
    -> wrap_runtime_run_action). It monkeypatches runtime.run_action, builds the
    GTRuntimeConfig, picks up graph.db. We pass a metadata object with the fields
    patched_initialize_runtime reads (max_iterations).

    Returns the GT-augmented instruction text for the agent.
    """
    import oh_gt_full_wrapper as wrapper  # PYTHONPATH includes scripts/swebench

    # The wrapper's hooks expect graph.db at config.graph_db (defaults derived
    # from workspace). Point GT at the baked db via env so install_graph_and_hook
    # promotes it instead of rebuilding from scratch.
    baked_db = f"{WORKSPACE_DIR}/graph.db"
    if os.path.exists(baked_db):
        os.environ.setdefault("GT_PREBUILT_GRAPH_DB", baked_db)
        _log(f"using baked graph.db: {baked_db}")
    else:
        _log(f"WARN: no baked graph.db at {baked_db}; GT will rebuild via gt-index if present")

    # patched_initialize_runtime: installs the GT hooks (install_graph_and_hook ->
    # wrap_runtime_run_action monkeypatches runtime.run_action) AND generates the
    # LIVE v1r brief in-container, storing it on instance["gt_brief"]. It calls
    # _ORIG_INITIALIZE_RUNTIME first only if set; we leave it None so NO SWE-bench
    # harness image setup runs (the local driver staged the workspace itself).
    wrapper.patched_initialize_runtime(runtime, instance, metadata)

    # Build the instruction WITHOUT patched_get_instruction: that function requires
    # _ORIG_GET_INSTRUCTION (None here -> RuntimeError) and returns a MessageAction
    # object, not a string. Instead use the public generate_task_brief(instance),
    # which reads the LIVE instance["gt_brief"] set above, and prepend it to the
    # problem statement (same effect patched_get_instruction has on msg.content).
    problem = (
        instance.get("problem_statement", "")
        if isinstance(instance, dict)
        else getattr(instance, "problem_statement", "")
    ) or ""
    brief = ""
    try:
        brief = wrapper.generate_task_brief(instance) or ""
    except Exception as exc:
        _log(f"generate_task_brief failed ({exc}); proceeding with problem_statement only")
    if brief.strip():
        _log(f"GT brief surfaced ({len(brief)} chars) — prepending to instruction")
        return f"{brief.strip()}\n\n{problem}"
    _log("no GT brief (graph/index unavailable) — instruction = problem_statement only")
    return problem


async def run_agent(cfg: Any, runtime: Any, instruction: str) -> Any:
    """Drive the CodeActAgent loop via OH 0.54 run_controller."""
    from openhands.core.main import run_controller  # type: ignore
    from openhands.events.action import MessageAction  # type: ignore

    state = await run_controller(
        config=cfg,
        initial_user_action=MessageAction(content=instruction),
        runtime=runtime,
        headless_mode=True,
    )
    return state


def extract_git_patch() -> str:
    """git diff of the workspace = the agent's candidate patch (gold-free)."""
    r = subprocess.run(
        ["git", "-C", WORKSPACE_DIR, "add", "-A"], capture_output=True, text=True
    )
    r = subprocess.run(
        ["git", "-C", WORKSPACE_DIR, "diff", "--cached", "--no-color"],
        capture_output=True,
        text=True,
    )
    return r.stdout


def write_output(instance: dict[str, Any], state: Any, patch: str) -> str:
    """Emit output.jsonl in the SWE-bench eval record shape (for off-Railway eval)."""
    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "output.jsonl")
    history = []
    try:
        for ev in getattr(state, "history", []) or []:
            try:
                history.append(
                    ev.__dict__ if hasattr(ev, "__dict__") else str(ev)
                )
            except Exception:
                history.append(str(ev))
    except Exception:
        pass
    record = {
        "instance_id": instance["instance_id"],
        "test_result": {"git_patch": patch},
        "git_patch": patch,
        "history": history,
        "metadata": {
            "agent_class": "CodeActAgent",
            "model": os.environ.get("GT_MODEL", "deepseek/deepseek-v4-flash"),
            "max_iterations": MAX_ITER,
            "runtime": "local",
        },
    }
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    _log(f"wrote {out_path} (patch {len(patch)} bytes, {len(history)} events)")
    # Stage any gt_interactions logs the hooks wrote in /tmp beside output.jsonl.
    for p in Path("/tmp").glob("gt_interactions_*.jsonl"):
        try:
            (Path(OUT_DIR) / p.name).write_bytes(p.read_bytes())
        except Exception:
            pass
    return out_path


def main() -> int:
    _log(f"instance={INSTANCE_ID} base={BASE_COMMIT} runtime=local out={OUT_DIR}")
    instance = load_instance()
    ensure_workspace(instance)

    cfg, _llm = build_config()

    # create_runtime builds the LLMRegistry + EventStream and returns a connected
    # LocalRuntime when cfg.runtime == "local". Signature varies slightly across
    # 0.54 point builds; try the documented form, then degrade.
    from openhands.core.main import create_runtime  # type: ignore

    runtime = None
    last_exc = None
    for kwargs in (
        {"sid": INSTANCE_ID, "headless_mode": True},
        {"sid": INSTANCE_ID},
        {},
    ):
        try:
            runtime = create_runtime(cfg, **kwargs)  # type: ignore[arg-type]
            break
        except TypeError as exc:
            last_exc = exc
            continue
    if runtime is None:
        raise SystemExit(f"FATAL: create_runtime failed across signatures: {last_exc}")

    # MEMORY TRIM: the free-tier container OOM-kills (rc=137) when the action
    # server loads the heavy `jupyter` plugin (jupyter_kernel_gateway + a live
    # kernel). Force plugins to AgentSkills-only BEFORE connect — CodeActAgent
    # still edits files (agent_skills) and runs bash/pytest (CmdRunAction); only
    # in-process IPython cells are lost, which this config-fix task doesn't need.
    try:
        from openhands.runtime.plugins import AgentSkillsRequirement  # type: ignore
        _before = [type(p).__name__ for p in (getattr(runtime, "plugins", None) or [])]
        if hasattr(runtime, "plugins"):
            runtime.plugins = [AgentSkillsRequirement()]
        _log(f"plugins memory-trim: {_before} -> [AgentSkillsRequirement]")
    except Exception as _pe:
        _log(f"plugin trim skipped ({_pe})")

    # Connect the local action-execution server (bash in THIS container).
    from openhands.utils.async_utils import call_async_from_sync  # type: ignore

    call_async_from_sync(runtime.connect)
    _log("LocalRuntime connected (bash action-execution server up, no Docker)")

    # Minimal metadata object for patched_initialize_runtime / get_instruction.
    class _Meta:
        max_iterations = MAX_ITER
        agent_class = "CodeActAgent"
        details = {"mode": "swe"}

    instruction = install_gt_hooks(runtime, instance, _Meta())
    _log(f"GT hooks installed; instruction {len(instruction)} chars")

    state = asyncio.run(run_agent(cfg, runtime, instruction))
    _log("agent loop finished")

    patch = extract_git_patch()
    write_output(instance, state, patch)

    try:
        runtime.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)

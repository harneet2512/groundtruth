#!/usr/bin/env python3
"""Headless mini-swe-agent runner — the non-interactive core of ``run/mini.py``.

WHY THIS EXISTS (proven on micro-verify 29214296174, both tasks 0 steps):
The ``mini`` / ``mini-swe-agent`` console entry is ``minisweagent.run.mini:app`` — the INTERACTIVE
Textual/prompt_toolkit app. In a headless container it imports, loads the global config, then never
reaches ``agent.run()`` — it produces 0 steps and writes no trajectory. ``--agent-class default``
does not help: it only changes the agent the app builds, not the app harness. The single-instance
SWE-bench runner (``run/benchmarks/swebench_single.py:87-96``) shows the construction the app wraps:

    env   = <environment>
    agent = get_agent(get_model(config=cfg["model"]), env, cfg["agent"], default_type=...)
    agent.run(problem_statement)

This runner replays exactly that construction with a LOCAL environment (the eval container IS the
task environment — no pier, no second docker env) and the non-interactive DefaultAgent
(``default_type="default"``), so the step loop actually runs. GT delivery is unchanged and
agent-class-agnostic: ``gt_mini_patch --install`` wraps ``Environment.execute`` (gt_mini_patch.py
:11623-11624), which DefaultAgent calls on every action.

ENV CONTRACT (set by the workflow so the single-quoted docker block carries NO apostrophe-prone
inline args):
  GT_RUN_MODEL   model name, e.g. deepseek/deepseek-v4-flash            (required)
  GT_RUN_TASK    the issue / problem statement                          (falls back to a literal)
  GT_RUN_CONFIG  agent config spec path                                 (default /opt/gt/agent_config.yaml)
  GT_RUN_OUTPUT  trajectory output path                                 (default /gt_out/mini-swe-agent.trajectory.json)

Exit code: 0 when the agent loop ran to a terminal exit (LimitsExceeded / submit / handled
exception — DefaultAgent.run always returns in those cases); 2 on a config/wiring error before the
loop. The workflow guards the call with ``|| true`` and reads the TRAJECTORY (n_calls) as the
authoritative step count, not this exit code.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import time
from collections.abc import Mapping

try:
    from artifact_deepswe import ledger_attestation
except ImportError:  # injected runner + sibling module inside /opt/gt
    import ledger_attestation  # type: ignore[no-redef]


def _bc(msg: str) -> None:
    """Flushed breadcrumb to stderr — survives a SIGKILL/hang (unbuffered), so the trial log shows
    exactly how far the runner got when it produces no trajectory."""
    sys.stderr.write(f"[GT-RUNNER] {msg}\n")
    sys.stderr.flush()


def _resolve_task(e: Mapping[str, str]) -> str:
    """Return only the host's native task bytes.

    Task-start repository material is evidence input for the canonical
    runtime. It is never directly prepended or pre-marked delivered here.
    """
    task = (e.get("GT_RUN_TASK") or "").strip() or "Fix the issue in the repository."
    if e.get("GT_BASELINE") == "1":
        _bc("GT_BASELINE=1 -> native task unchanged (pure control arm)")
    # The task-start brief is canonical evidence input.  Directly prepending it
    # would bypass evidence lifecycle, coalition composition, exact provider
    # joining, and terminal-inference delivery proof.
    return task


def _env_on(value: object) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off", "none"}


def _batch_hook_required(e: Mapping[str, str]) -> bool:
    """Whether this run promises observation-level one-dose arbitration."""
    if str(e.get("GT_BASELINE") or "") == "1":
        return False
    profile = str(e.get("GT_RL_PROFILE") or "").strip().lower()
    return (
        profile == "2"
        or _env_on(e.get("GT_GLOBAL_ARBITER"))
        or _env_on(e.get("GT_PROOF_MODE"))
        or _env_on(e.get("GT_REQUIRE_FULL_STACK"))
    )


def _batch_receipt_path(e: Mapping[str, str]) -> str:
    explicit = str(e.get("GT_BATCH_ACTIVATION_RECEIPT") or "").strip()
    if explicit:
        return explicit
    anchor = str(e.get("GT_RUNTIME_LEDGER") or e.get("GT_RUN_OUTPUT") or "").strip()
    parent = os.path.dirname(anchor) if anchor else "/tmp"
    return os.path.join(parent or ".", "gt_batch_activation.json")


def _mini_swe_version() -> str:
    try:
        import minisweagent

        version = str(getattr(minisweagent, "__version__", "") or "").strip()
        if version:
            return version
    except Exception:  # noqa: BLE001 -- metadata fallback remains available
        pass
    try:
        return importlib.metadata.version("mini-swe-agent")
    except Exception:  # noqa: BLE001 -- missing/broken metadata is recorded as unknown
        return ""


def _qualified_class(value: object) -> str:
    cls = value.__class__
    return f"{cls.__module__}.{cls.__qualname__}"


def _write_batch_activation_receipt(
    e: Mapping[str, str],
    *,
    agent: object,
    required: bool,
    result: str,
    attached: bool,
) -> bool:
    """Persist post-agent proof that the observation commit boundary is attached."""
    path = _batch_receipt_path(e)
    receipt = {
        "schema": "gt.batch_activation.v1",
        "required": bool(required),
        "result": str(result or ""),
        "wrapper_attached": bool(attached),
        "agent_class": _qualified_class(agent),
        "model_class": _qualified_class(getattr(agent, "model", None)),
        "mini_swe_version": _mini_swe_version(),
        "gt_rl_profile": str(e.get("GT_RL_PROFILE") or ""),
        "global_arbiter_on": _env_on(e.get("GT_GLOBAL_ARBITER")),
        "pid": os.getpid(),
        "timestamp_ms": int(time.time() * 1000),
    }
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(receipt, fh, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        _bc(f"WARN batch activation receipt unavailable ({type(exc).__name__} @ {path})")
        return False


def run(env: dict | None = None) -> int:
    """Build model + local env + DefaultAgent from the env contract and run one task.

    ``env`` defaults to ``os.environ``; it is a parameter so a test can drive the wiring with a
    fake config path without mutating the process environment.
    """
    e = os.environ if env is None else env
    _bc(f"start pid={os.getpid()} py={sys.version.split()[0]} exe={sys.executable}")
    model_name = (e.get("GT_RUN_MODEL") or "").strip()
    if not model_name:
        _bc("FATAL: GT_RUN_MODEL unset — cannot build the model")
        return 2
    cfg_path = e.get("GT_RUN_CONFIG") or "/opt/gt/agent_config.yaml"
    # STEP-0 BRIEF — CORRECTED 2026-07-27 (C3). This comment described a PREPEND that no longer
    # exists: it was removed in 4f525f424 when delivery was re-homed to the canonical runtime in
    # Wave 15/16, and it contradicted `_resolve_task`'s own docstring 100 lines above. A stale
    # comment describing a deleted mechanism is worse than none — it sends the next reader
    # looking for a prepend that cannot be found, which is how the step-0 path got audited as
    # "dead" more than once.
    #
    # WHAT ACTUALLY HAPPENS: `_resolve_task` returns the host's NATIVE task bytes only. The brief
    # reaches the model as canonical evidence — `install_canonical_runtime` stages a task-start
    # capsule (`_stage_initial_canonical_evidence`) BEFORE `agent.run`, and
    # `MiniSweProviderBoundary`'s patched `_prepare_messages_for_api` appends it as a trailing
    # user message on the first model call. Request path, not observation path: it shares
    # nothing with the batch-commit interface.
    task = _resolve_task(e)
    out = e.get("GT_RUN_OUTPUT") or "/gt_out/mini-swe-agent.trajectory.json"
    _bc(f"env ok model={model_name} cfg={cfg_path} task_chars={len(task)} out={out}")

    # Imports are deferred so a missing minisweagent surfaces as a clean rc=2, not an import-time
    # crash before the diagnostic print.
    from minisweagent.agents import get_agent
    from minisweagent.config import get_config_from_spec
    from minisweagent.environments import get_environment
    from minisweagent.models import get_model
    _bc("imported minisweagent")

    # DELIVERY — the second half of the B5 fix. GT evidence rides Environment.execute, which is
    # monkey-patched by gt_mini_patch._install() (gt_mini_patch.py:11639). NOTHING ELSE imports
    # gt_mini_patch in THIS process: the workflow's separate `python .../gt_mini_patch.py` runs in
    # a throwaway process, and on a uv-tool container that python cannot even import minisweagent,
    # so it patches ZERO env classes. The patch must therefore be imported in the SAME interpreter
    # that calls agent.run(). Import it here (patching the CLASS method reaches the already-built
    # env instance at call time). It self-gates on GT_BASELINE -> a no-op in the control arm; we
    # also skip the import outright there so the control process never loads GT. Fail-OPEN on a bad
    # import (breadcrumb the WARN, keep running) — the proof marker + harness gate enforce
    # fail-closed at the run boundary, not here. Breadcrumb the patched set so the trial log proves
    # delivery ATTACHED, not merely that the agent ran.
    gt_mini_patch = None
    if e.get("GT_BASELINE") != "1":
        _here = os.path.dirname(os.path.abspath(__file__))
        if _here not in sys.path:
            sys.path.insert(0, _here)
        try:
            import gt_mini_patch  # noqa: F401 -- import side effect: _install() patches env.execute
            _bc(f"gt_mini_patch installed patched={getattr(gt_mini_patch, '_PATCHED_CLASSES', '?')}")
        except Exception as _exc:  # noqa: BLE001 -- delivery import must not crash the agent loop
            _bc(f"WARN gt_mini_patch import failed: {type(_exc).__name__}: {_exc}")
    else:
        _bc("GT_BASELINE=1 -> gt_mini_patch NOT imported (pure control arm)")

    cfg = get_config_from_spec(cfg_path)
    if not isinstance(cfg, dict):
        _bc(f"FATAL: config {cfg_path!r} did not parse to a mapping")
        return 2
    _bc(f"config loaded keys={sorted(cfg)}")

    model = get_model(config={**cfg.get("model", {}), "model_name": model_name})
    _bc("model built")
    env_obj = get_environment(cfg.get("environment", {}), default_type="local")
    _bc("env built")
    agent_cfg = {**cfg.get("agent", {}), "output_path": out}
    # default_type="default" -> the non-interactive DefaultAgent (a pure while-True step loop). This
    # is the single line that fixes the 0-step wash: NEVER "interactive" here.
    agent = get_agent(model, env_obj, agent_cfg, default_type="default")
    batch_required = _batch_hook_required(e)
    batch_result = "patch_import_unavailable"
    batch_attached = False
    canonical_attachment = None
    if gt_mini_patch is not None:
        try:
            canonical_attachment = gt_mini_patch.install_canonical_runtime(
                model=model,
                agent=agent,
                env=e,
                task=task,
            )
            batch_attached = bool(
                getattr(canonical_attachment, "attached", False)
                and getattr(canonical_attachment, "attempt_runtime", None) is not None
                and getattr(canonical_attachment, "provider_boundary", None) is not None
                and getattr(canonical_attachment, "commitment_boundary", None) is not None
            )
            batch_result = "installed" if batch_attached else "install_unavailable"
            if not batch_attached:
                _bc("WARN GT canonical runtime attachment unavailable")
        except Exception as _exc:  # noqa: BLE001 -- report before the paid call
            batch_result = f"install_error:{type(_exc).__name__}"
            _bc(f"WARN GT canonical runtime install failed: {type(_exc).__name__}: {_exc}")
    receipt_written = True
    if e.get("GT_BASELINE") != "1":
        receipt_written = _write_batch_activation_receipt(
            e,
            agent=agent,
            required=batch_required,
            result=batch_result,
            attached=batch_attached,
        )
    if batch_required and (not batch_attached or not receipt_written):
        _bc(
            "FATAL: required canonical runtime attachment is unproven; "
            "refusing paid agent.run()"
        )
        return 2
    _bc("agent built — entering agent.run()")

    result = agent.run(task)
    steps = getattr(agent, "n_calls", "?")
    cost = getattr(agent, "cost", "?")
    exit_status = result.get("exit_status") if isinstance(result, dict) else "?"
    _bc(f"headless agent finished: exit={exit_status} steps={steps} cost={cost}")
    print(f"[GT] headless agent finished: exit={exit_status} steps={steps} cost={cost}")
    if e.get("GT_BASELINE") != "1":
        if gt_mini_patch is None:
            _bc("FATAL: GT seam module unavailable at terminal attestation")
            return 2
        try:
            seam_failures = int(gt_mini_patch.ledger_write_failures())
            failures = seam_failures
            # A canonical attachment owns persistence in RuntimeJournal.  A
            # stale legacy JSONL file at the historical default path is not
            # part of this attempt and must never determine its outcome.
            legacy_ledger = e.get("GT_RUNTIME_LEDGER") or "/tmp/gt_runtime_ledger.jsonl"
            if not batch_attached and os.path.isfile(legacy_ledger):
                attestation = ledger_attestation.write_attestation(
                    legacy_ledger,
                    write_failures=failures,
                )
                _bc(
                    "runtime ledger terminal attestation "
                    f"rows={attestation['row_count']} bytes={attestation['byte_count']} "
                    f"sha256={attestation['sha256']}"
                )
                failures = int(attestation["write_failures"])
            if failures != 0:
                _bc(
                    "FATAL: runtime ledger writer reported "
                    f"{failures} failure(s)"
                )
                return 2
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            _bc(f"FATAL: runtime ledger attestation failed: {type(exc).__name__}: {exc}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

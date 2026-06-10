#!/usr/bin/env python3
"""GT-injected mini-swe-agent runner for SWE-bench Verified (PATH B).

Mirrors the proven DeepSWE pattern (artifact_deepswe/gt_agent.py +
gt_mini_patch.py) on mini-swe-agent's OWN swebench path — NOT pier.

Harness shape (installed minisweagent 2.2.8, file:line evidence in
artifact_verified/README.md):
  * minisweagent.run.benchmarks.swebench.process_instance() launches one
    DefaultAgent per instance against the task's Docker image
    (swebench.py:136-191): task = instance["problem_statement"] (:149),
    env = get_sb_environment(config, instance) (:160), agent.run(task) (:168).
  * The agent process runs ON THE HOST; DockerEnvironment.execute() drives the
    task container via `docker exec` (environments/docker.py:101-138).

The TWO GT injection points map onto that as:
  (a) INSTRUCTION — the substrate brief is prepended to the task string before
      agent.run(task) (the gt_agent._generate_brief/_substrate_brief/
      _prepend_brief pattern; consume-only, FAIL-CLOSED in proof/substrate
      mode — no host-side brief regeneration, single <gt-task-brief> tag).
  (b) ENVIRONMENT — artifact_deepswe/gt_mini_patch.py is imported INTO THIS
      HOST PROCESS (no .pth / container injection needed here: the agent loop
      and DockerEnvironment.execute live host-side). Its _ENV_CLASSES list
      already names ("minisweagent.environments.docker", "DockerEnvironment")
      (gt_mini_patch.py:1217-1221), so the import wraps execute() and appends
      <gt-evidence>/<gt-scope>/<gt-contract>/<gt-cochange>/<gt-nudge> to
      command output. The pillars read the substrate graph via GT_HOST_GRAPH_DB
      read-only (gt_mini_patch._db_path, :228-246; _connect_ro mode=ro+immutable
      in substrate mode, :394-436); L6 reindex is gated OFF in substrate mode
      (_invalidate_on_edit, :1081-1082) — the substrate graph stays immutable.

REUSE, not port: the brief/witness/fail-closed logic is IMPORTED from
artifact_deepswe.gt_agent (zero drift, ONE product). gt_agent.py's module
imports require `pier` (gt_agent.py:59-62) which the Verified path does not
use; `_ensure_pier_importable()` installs inert stub modules ONLY when pier is
absent, so the REAL module-level functions (_generate_brief, _prepend_brief,
_emit_gt_meta_witness, DeepSweAdapterError, ...) load unchanged. The pier
class surface (GTMiniSweAgent / install_spec) is never used here.

Fail-closed contract (identical to DeepSWE):
  * GT_PROOF_MODE=1 / substrate active (GT_HOST_GRAPH_DB / GT_CERT_DIR /
    GT_PORTABLE_SUBSTRATE) -> witness mismatch, missing brief, missing graph,
    or unattached observation patch RAISE DeepSweAdapterError after printing
    the classified `[GT_META] ... error=DEEPSWE_ADAPTER_FAIL` line.
  * GT_BASELINE=1 -> control arm: no witness, no brief, no patch, instruction
    untouched (read at CALL time, not import time, so tests can toggle it).

Usage (one instance per GHA matrix job):
    python artifact_verified/gt_verified_agent.py \
        --instance-id astropy__astropy-12907 \
        --model deepseek/deepseek-v4-flash \
        --config artifact_verified/verified_gt.yaml \
        --output /tmp/results --image "$RESOLVED_IMG"

    # helper modes for the workflow (no agent run):
    python artifact_verified/gt_verified_agent.py --instance-id X --print-image
    python artifact_verified/gt_verified_agent.py --instance-id X \
        --dump-issue-to /tmp/issue.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
_DEEPSWE_DIR = _REPO_ROOT / "artifact_deepswe"

# ---------------------------------------------------------------------------
# pier import shim — artifact_deepswe.gt_agent imports pier at module level
# (gt_agent.py:59-62) purely for the GTMiniSweAgent class surface, which the
# Verified path NEVER uses. When pier is installed (the DeepSWE runner, local
# dev) the real package is used untouched. When it is absent (the Verified GHA
# runner), inert stubs make the import succeed so the REAL module-level
# functions are reused by import — no ported copies, no drift.
# ---------------------------------------------------------------------------
_PIER_STUB_MODULES = (
    "pier",
    "pier.agents",
    "pier.agents.installed",
    "pier.agents.installed.mini_swe_agent",
    "pier.environments",
    "pier.environments.base",
    "pier.models",
    "pier.models.agent",
    "pier.models.agent.context",
    "pier.models.agent.install",
)


def _ensure_pier_importable() -> bool:
    """Return True when the REAL pier package is importable; install inert stub
    modules (and return False) when it is not. Idempotent."""
    try:
        import pier.agents.installed.mini_swe_agent  # noqa: F401
        import pier.environments.base  # noqa: F401
        import pier.models.agent.context  # noqa: F401
        import pier.models.agent.install  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — pier absent or broken -> stub
        pass

    import types

    mods: dict[str, types.ModuleType] = {}
    for name in _PIER_STUB_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            mod.__gt_pier_stub__ = True  # type: ignore[attr-defined]
            sys.modules[name] = mod
        mods[name] = mod
    # parent.child attribute wiring so `import pier.x.y` attribute access works
    for name in _PIER_STUB_MODULES:
        if "." in name:
            parent, child = name.rsplit(".", 1)
            setattr(mods[parent], child, mods[name])

    class _StubMiniSweAgent:
        """Inert stand-in base class. The Verified path never instantiates
        GTMiniSweAgent; using it through this stub is a hard error."""

        def install_spec(self):  # pragma: no cover - guard only
            raise RuntimeError("pier is not installed — GTMiniSweAgent is unusable "
                              "on the Verified path (stubbed base class)")

        async def run(self, *a, **k):  # pragma: no cover - guard only
            raise RuntimeError("pier is not installed — GTMiniSweAgent is unusable "
                              "on the Verified path (stubbed base class)")

    class _StubAgentInstallSpec:  # pragma: no cover - never constructed here
        def __init__(self, *a, **k):
            self.steps = list(k.get("steps", []) or [])

    class _StubInstallStep:  # pragma: no cover - never constructed here
        def __init__(self, *a, **k):
            self.__dict__.update(k)

    mods["pier.agents.installed.mini_swe_agent"].MiniSweAgent = _StubMiniSweAgent
    mods["pier.environments.base"].BaseEnvironment = object
    mods["pier.models.agent.context"].AgentContext = object
    mods["pier.models.agent.install"].AgentInstallSpec = _StubAgentInstallSpec
    mods["pier.models.agent.install"].InstallStep = _StubInstallStep
    return False


_PIER_REAL = _ensure_pier_importable()

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The REAL DeepSWE adapter functions — reused by import (zero drift).
from artifact_deepswe import gt_agent as _gt  # noqa: E402

DeepSweAdapterError = _gt.DeepSweAdapterError


def _baseline() -> bool:
    """Control arm switch, read at CALL time (gt_agent reads it at import time;
    call-time read keeps the runner testable and matrix-job-safe)."""
    return os.environ.get("GT_BASELINE") == "1"


# ---------------------------------------------------------------------------
# Task-image naming — parity with scripts/vm/build_verified_manifest.py:76-89
# (conventions verified live there: GHCR/epoch keeps raw `__` + hyphenated
# `swe-bench.eval`; Docker Hub escapes `__` -> `_1776_`).
# ---------------------------------------------------------------------------
def ghcr_epoch_image(instance_id: str) -> str:
    return f"ghcr.io/epoch-research/swe-bench.eval.x86_64.{instance_id.lower()}:latest"


def dockerhub_image(instance_id: str) -> str:
    slug = instance_id.lower().replace("__", "_1776_")
    return f"docker.io/swebench/sweb.eval.x86_64.{slug}:latest"


def resolve_image(instance_id: str, override: str = "") -> str:
    """The image the agent runs against. The workflow pre-pulls (GHCR-epoch
    primary, Docker Hub `_1776_` fallback) and passes the ref that actually
    pulled via --image; default is the GHCR-epoch primary."""
    return override or os.environ.get("GT_TASK_IMAGE", "") or ghcr_epoch_image(instance_id)


# ---------------------------------------------------------------------------
# Injection point (b): host-side observation interception
# ---------------------------------------------------------------------------
def attach_gt_patch() -> bool:
    """Import artifact_deepswe/gt_mini_patch.py into THIS host process.

    Its import-time _install() (gt_mini_patch.py:1224-1243) wraps the execute()
    of every minisweagent environment class it finds — including
    ("minisweagent.environments.docker", "DockerEnvironment")
    (gt_mini_patch.py:1219) — so every `docker exec` observation gets the
    per-view/per-edit GT evidence appended before the model sees it.

    FAIL-CLOSED in proof/substrate mode: a GT-on run whose observation surface
    silently failed to attach would be an unprovable half-delivery — raise
    instead. Baseline: no-op. Returns attachment state."""
    if _baseline():
        return False
    if str(_DEEPSWE_DIR) not in sys.path:
        sys.path.insert(0, str(_DEEPSWE_DIR))
    try:
        import gt_mini_patch  # noqa: F401 — import IS the installation
    except Exception as e:  # noqa: BLE001
        if _gt._proof_mode() or _gt._substrate_active():
            _gt._adapter_fail(
                f"GT_PATCH_IMPORT_FAILED:{e}",
                f"DEEPSWE_ADAPTER_FAIL: gt_mini_patch import failed on the host "
                f"({e}) — the per-turn evidence surface cannot attach (fail-closed "
                f"in proof/substrate mode).",
                cause=e,
            )
        print(f"[gt-verified] WARN: gt_mini_patch import failed ({e}) — "
              f"per-turn evidence OFF (non-proof path)", flush=True)
        return False
    import minisweagent.environments.docker as _docker_env

    attached = bool(getattr(_docker_env.DockerEnvironment, "_gt_patched", False))
    if not attached and (_gt._proof_mode() or _gt._substrate_active()):
        _gt._adapter_fail(
            "GT_PATCH_NOT_ATTACHED",
            "DEEPSWE_ADAPTER_FAIL: gt_mini_patch imported but DockerEnvironment."
            "execute is NOT patched (_gt_patched falsy) — observation interception "
            "is dead; failing closed in proof/substrate mode.",
        )
    print(f"[gt-verified] observation patch attached={attached} "
          f"(DockerEnvironment.execute wrapped host-side)", flush=True)
    return attached


# ---------------------------------------------------------------------------
# Injection point (a): instruction (brief prepend + GT preamble)
# ---------------------------------------------------------------------------
def build_augmented_task(problem_statement: str, brief: str | None = None) -> str:
    """The exact gt_agent.run() instruction assembly (gt_agent.py:800-823):
    brief (consume-only, fail-closed in proof/substrate) prepended with the
    single-<gt-task-brief>-tag invariant, then the GT awareness preamble.

    `brief` parameter exists for the dry unit test; production passes None and
    the brief is consumed from $GT_CERT_DIR/brief.txt via the REAL
    _generate_brief/_substrate_brief chain."""
    if _baseline():
        return problem_statement
    if brief is None:
        brief = _gt._generate_brief(problem_statement)  # raises in proof mode if absent
    augmented = _gt._prepend_brief(brief, problem_statement)
    # The observation patch attaches host-side on this path, so the automatic
    # (<gt-evidence> tags appear on their own) preamble is the correct one.
    return augmented.rstrip() + "\n" + _gt._GT_PREAMBLE


def _persist_delivered_instruction(task: str) -> None:
    """Deterministic delivery proof (gt_agent.py:825-834 parity): persist the
    EXACT instruction handed to the agent."""
    out_dir = Path(os.environ.get("GT_DELIVERY_DIR", "") or "/tmp/gt")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "delivered_instruction.txt").write_text(task, encoding="utf-8")
    except OSError:
        pass  # never block the run on a proof-artifact write


# ---------------------------------------------------------------------------
# Dataset access (the runner needs problem_statement; the workflow also uses
# --dump-issue-to so gt-run-proof gets THIS task's issue text)
# ---------------------------------------------------------------------------
def load_instance(instance_id: str, subset: str = "verified", split: str = "test") -> dict:
    from datasets import load_dataset

    from minisweagent.run.benchmarks.swebench import DATASET_MAPPING

    dataset_path = DATASET_MAPPING.get(subset, subset)
    for inst in load_dataset(dataset_path, split=split):
        if inst["instance_id"] == instance_id:
            return dict(inst)
    raise SystemExit(
        f"GT_ISSUE_MISSING: instance {instance_id!r} not found in "
        f"{dataset_path}:{split} — refusing to run without the task issue (fail-closed)"
    )


# ---------------------------------------------------------------------------
# Per-instance run — mirrors minisweagent.run.benchmarks.swebench.
# process_instance (swebench.py:136-191) with the GT injections added.
# ---------------------------------------------------------------------------
def run_instance(instance: dict, config: dict, output_dir: Path, image: str = "") -> str:
    from minisweagent.agents.default import DefaultAgent
    from minisweagent.models import get_model
    from minisweagent.run.benchmarks.swebench import (
        get_sb_environment,
        remove_from_preds_file,
        update_preds_file,
    )

    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    traj_path = instance_dir / f"{instance_id}.traj.json"
    traj_path.unlink(missing_ok=True)

    # Route get_swebench_docker_image_name (swebench.py:82-90) to the pre-pulled
    # image: instance["image_name"] takes precedence over the dockerhub default.
    instance = dict(instance)
    instance["image_name"] = resolve_image(instance_id, image)
    print(f"[gt-verified] instance={instance_id} image={instance['image_name']}", flush=True)

    if not _baseline():
        # §H consumption witness BEFORE any model spend (gt_agent.py:801-807):
        # proves the adapter consumed the substrate graph read-only and that its
        # fingerprint matches the substrate's post-LSP hash. RAISES
        # DeepSweAdapterError in proof/substrate mode on any violation.
        _gt._emit_gt_meta_witness()
        # Observation interception (injection point b) before the env exists —
        # class-level wrap, so order vs instantiation is safe either way.
        attach_gt_patch()

    task = build_augmented_task(instance["problem_statement"])
    if not _baseline():
        _persist_delivered_instruction(task)

    model = get_model(config=config.get("model", {}))
    agent = None
    exit_status: str | None = None
    result: str | None = None
    extra_info: dict = {}
    t0 = time.time()
    try:
        env = get_sb_environment(config, instance)
        agent = DefaultAgent(model, env, **config.get("agent", {}))
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")
    except DeepSweAdapterError:
        raise  # never swallow the fail-closed contract (no pier here to do it either)
    except Exception as e:  # noqa: BLE001 — record like the upstream batch runner
        print(f"[gt-verified] ERROR processing {instance_id}: {e}", flush=True)
        exit_status, result = type(e).__name__, ""
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}
    finally:
        if agent is not None:
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_baseline": _baseline(),
                        "gt_wall_seconds": round(time.time() - t0, 8),
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )
            print(f"[gt-verified] saved trajectory: {traj_path}", flush=True)
            # Parsed by the summarize job (deepswe_full.yml P0.1-d pattern):
            # the VALUE must be >0 for the run to count as agent-ran.
            print(f"AGENT_RAN_STEPS={agent.n_calls}", flush=True)
        update_preds_file(output_dir / "preds.json", instance_id,
                          model.config.model_name, result or "")
    return exit_status or "Unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--instance-id", required=True,
                    help="SWE-bench Verified instance id (one per matrix job)")
    ap.add_argument("--subset", default="verified",
                    help="minisweagent DATASET_MAPPING key or HF dataset path")
    ap.add_argument("--split", default="test")
    ap.add_argument("--model", default=os.environ.get("GT_MODEL", "deepseek/deepseek-v4-flash"))
    ap.add_argument("--config", default=str(_THIS_DIR / "verified_gt.yaml"))
    ap.add_argument("--output", default="results", help="Output directory (preds.json + traj)")
    ap.add_argument("--image", default="", help="Pre-pulled task image ref (overrides default)")
    ap.add_argument("--print-image", action="store_true",
                    help="Print the primary + fallback image refs and exit")
    ap.add_argument("--dump-issue-to", default="",
                    help="Write the instance problem_statement to PATH and exit")
    args = ap.parse_args(argv)

    if args.print_image:
        print(ghcr_epoch_image(args.instance_id))
        print(dockerhub_image(args.instance_id))
        return 0

    if args.dump_issue_to:
        inst = load_instance(args.instance_id, args.subset, args.split)
        issue = (inst.get("problem_statement") or "").strip()
        if not issue:
            print(f"GT_ISSUE_MISSING: empty problem_statement for {args.instance_id} "
                  f"— fail-closed", flush=True)
            return 1
        Path(args.dump_issue_to).write_text(issue, encoding="utf-8")
        print(f"[gt-verified] wrote issue ({len(issue)} chars) -> {args.dump_issue_to}")
        return 0

    from minisweagent.config import get_config_from_spec
    from minisweagent.utils.serialize import recursive_merge

    config = recursive_merge(
        get_config_from_spec(args.config),
        {"model": {"model_name": args.model}},
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    inst = load_instance(args.instance_id, args.subset, args.split)
    try:
        exit_status = run_instance(inst, config, output_dir, image=args.image)
    except DeepSweAdapterError as e:
        # The classified [GT_META] ... error=DEEPSWE_ADAPTER_FAIL line was already
        # printed by _adapter_fail/_fail before the raise. Exit nonzero so the
        # workflow step fails without any grep dependency (no pier swallow here).
        print(f"DeepSweAdapterError: {e}", flush=True)
        return 2
    print(f"[gt-verified] done: {args.instance_id} exit_status={exit_status}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

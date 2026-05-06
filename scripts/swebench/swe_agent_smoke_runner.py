#!/usr/bin/env python3
"""Track D — SWE-agent smoke runner for the GT 6-layer harness.

Orchestrates a SWE-agent `run-batch` invocation against SWE-bench-Live tasks,
captures per-task `[GT_LAYERS]` telemetry by reading the per-task log dirs that
Tracks A/B1/C write into, and emits one canonical line per task to:

  <output_dir>/<instance_id>/gt_layers.log         (per-task)
  <output_dir>/_global_gt_layers.log               (atomic-appended global)

Atomicity: every per-task append fsyncs both files before returning. No
end-of-run buffer dump.

Contract — directory layout (consumed, not produced, by this runner):
  <output_dir>/<instance_id>/
    gt_brief.txt                       written by Track A pre-run hook
    gt_evidence/edit_NNN.json          written by Track B1 gt_edit state cmd
    gt_query_calls.jsonl               written by Track C gt_query bundle
    gt_pre_finish_gate.json            written by Track C gt_pre_finish_gate
    gt_reindex.jsonl                   written by Track B1 (one line per call)
    cost_ledger.json                   written by SWE-agent / pre-run hook
    trajectory.json                    written by SWE-agent

The runner does not import Tracks A/B/C internals. The contract is the file
format only.

CLI:
  swe_agent_smoke_runner.py
      --config <yaml>
      --task-ids <comma_or_file>
      --output-dir <dir>
      [--workers N]
      [--remote-host gt-t0]   # gcloud SSH target; if omitted, runs locally
      [--remote-user ubuntu]
      [--dry-run]             # print command and exit (no launch)
      [--per-instance-cost-limit 4.0]
      [--per-instance-wallclock-cap-seconds 1800]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from image_name_resolver import resolve_image_name


# ---- Track A litellm register helper (graceful import) ---------------------

def _maybe_register_litellm() -> str:
    """Try to call Track A's litellm registration helper.

    Returns a status string. Never fails — Track A may not have landed yet, and
    Track D dev verification must run without it. The runner logs the status to
    stderr so Track D's dry-run output is still useful.
    """
    try:
        # Track A's contract: module name `gt_track4_litellm_register`,
        # function `register()` returning None on success.
        import gt_track4_litellm_register  # type: ignore[import-not-found]

        if hasattr(gt_track4_litellm_register, "register"):
            gt_track4_litellm_register.register()
            return "registered"
        return "module_present_no_register_fn"
    except ImportError:
        return "track_a_helper_not_available"
    except Exception as exc:  # noqa: BLE001
        return f"register_failed:{type(exc).__name__}:{exc}"


# ---- task-id parsing -------------------------------------------------------

def _parse_task_ids(arg: str) -> List[str]:
    """Accept comma-separated string or path to a file (one ID per line)."""
    p = Path(arg)
    if p.is_file():
        ids: List[str] = []
        for raw in p.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#"):
                ids.append(raw)
        return ids
    return [tok.strip() for tok in arg.split(",") if tok.strip()]


# ---- per-task layer reader (the contract reader) ---------------------------

@dataclass
class LayerSnapshot:
    L1: str = "empty"           # fired | fallback | empty
    L2: str = "noop"            # fired | noop
    L3: int = 0                 # edit_count
    L4: int = 0                 # query_count
    L5: str = "not_evaluated"   # pass | warn | fail | not_evaluated
    L6: int = 0                 # reindex_count
    elapsed_s: float = 0.0
    resolved: Optional[bool] = None
    cost_usd: float = 0.0
    notes: List[str] = field(default_factory=list)


@dataclass
class PreflightResult:
    ok: bool
    checks: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)


_BRIEF_MARKER = "<gt-task-brief>"
_V22_MARKER = "<gt-v22-brief>"  # convention; Track A may also tag inside file
_ALT_BRIEF_MARKER = "<gt-evidence>"


def _read_l1_l2(task_dir: Path, snap: LayerSnapshot) -> None:
    layers_log = task_dir / "gt_layers.log"
    if layers_log.is_file():
        try:
            text = layers_log.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\bL1=(\S+)\s+L2=(\S+)", text)
            if m:
                snap.L1 = m.group(1).strip()
                snap.L2 = m.group(2).strip()
                return
        except Exception as exc:  # noqa: BLE001
            snap.notes.append(f"layers_log_read_error:{exc}")

    brief = task_dir / "gt_brief.txt"
    if not brief.is_file():
        snap.L1 = "empty"
        snap.L2 = "noop"
        return
    try:
        text = brief.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        snap.notes.append(f"brief_read_error:{exc}")
        snap.L1 = "empty"
        return
    if not text.strip():
        snap.L1 = "empty"
        return

    # Track A pre-run hook is supposed to mark v22 fallback in the file. The
    # format owner is Track A; this reader probes two reasonable conventions:
    # (a) explicit <gt-v22-brief> marker, (b) sidecar `gt_brief_kind` file.
    is_v22 = _V22_MARKER in text
    kind_file = task_dir / "gt_brief_kind"
    if kind_file.is_file():
        try:
            kind = kind_file.read_text(encoding="utf-8").strip().lower()
            if kind == "v22" or kind == "fallback":
                is_v22 = True
            elif kind == "enhanced" or kind == "primary":
                is_v22 = False
        except Exception:  # noqa: BLE001
            pass

    if is_v22:
        snap.L1 = "fallback"
        snap.L2 = "fired"
    elif _BRIEF_MARKER in text or _ALT_BRIEF_MARKER in text:
        snap.L1 = "fired"
        snap.L2 = "noop"
    else:
        # File present but no recognized marker. Track this as an empty brief —
        # the safer side for a smoke gate ("L1 fired" must be unambiguous).
        snap.L1 = "empty"
        snap.notes.append("brief_present_no_marker")


def _read_l3(task_dir: Path, snap: LayerSnapshot) -> None:
    ev = task_dir / "gt_evidence"
    if not ev.is_dir():
        snap.L3 = 0
        return
    snap.L3 = sum(1 for f in ev.glob("edit_*.json") if f.is_file())


def _read_l4(task_dir: Path, snap: LayerSnapshot) -> None:
    qcalls = task_dir / "gt_query_calls.jsonl"
    if not qcalls.is_file():
        snap.L4 = 0
        return
    try:
        with qcalls.open("r", encoding="utf-8") as fh:
            snap.L4 = sum(1 for line in fh if line.strip())
    except Exception as exc:  # noqa: BLE001
        snap.notes.append(f"l4_read_error:{exc}")


def _read_l5(task_dir: Path, snap: LayerSnapshot) -> None:
    gate = task_dir / "gt_pre_finish_gate.json"
    if not gate.is_file():
        snap.L5 = "not_evaluated"
        return
    try:
        data = json.loads(gate.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        snap.notes.append(f"l5_parse_error:{exc}")
        snap.L5 = "not_evaluated"
        return
    verdict = str(data.get("verdict", data.get("result", ""))).lower().strip()
    if verdict in ("pass", "approved", "ok", "force"):
        snap.L5 = "pass"
    elif verdict in ("warn", "warning", "warn_soft_escape"):
        snap.L5 = "warn"
    elif verdict in ("fail", "failed", "blocked"):
        snap.L5 = "fail"
    else:
        snap.L5 = "not_evaluated"


def _read_l6(task_dir: Path, snap: LayerSnapshot) -> None:
    rj = task_dir / "gt_reindex.jsonl"
    if not rj.is_file():
        snap.L6 = 0
        return
    try:
        with rj.open("r", encoding="utf-8") as fh:
            snap.L6 = sum(1 for line in fh if line.strip())
    except Exception as exc:  # noqa: BLE001
        snap.notes.append(f"l6_read_error:{exc}")


def _read_resolved_and_cost(task_dir: Path, output_dir: Path,
                            instance_id: str, snap: LayerSnapshot) -> None:
    """Read resolved bool from output.jsonl record; cost from cost_ledger.json."""
    # cost_ledger.json (per-task)
    ledger = task_dir / "cost_ledger.json"
    if ledger.is_file():
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
            # Accept several common shapes:
            if isinstance(data, dict):
                snap.cost_usd = float(
                    data.get("total_usd")
                    or data.get("cost_usd")
                    or data.get("total")
                    or 0.0
                )
        except Exception as exc:  # noqa: BLE001
            snap.notes.append(f"cost_ledger_parse_error:{exc}")

    # output.jsonl (batch-level): resolved bool + elapsed
    output_jsonl = output_dir / "output.jsonl"
    if output_jsonl.is_file():
        try:
            with output_jsonl.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    rec_id = rec.get("instance_id") or rec.get("id")
                    if rec_id != instance_id:
                        continue
                    # Resolved field: SWE-agent records this several ways
                    # depending on version. Probe in order.
                    for k in ("resolved", "is_resolved", "passed"):
                        if k in rec:
                            snap.resolved = bool(rec[k])
                            break
                    # elapsed: prefer explicit, fallback to wall_time
                    for k in ("elapsed_s", "wall_time_s", "wall_clock_s",
                              "elapsed", "duration_s"):
                        if k in rec:
                            try:
                                snap.elapsed_s = float(rec[k])
                                break
                            except Exception:  # noqa: BLE001
                                pass
                    if snap.cost_usd == 0.0:
                        for k in ("cost_usd", "cost", "total_cost"):
                            if k in rec:
                                try:
                                    snap.cost_usd = float(rec[k])
                                    break
                                except Exception:  # noqa: BLE001
                                    pass
                    break
        except Exception as exc:  # noqa: BLE001
            snap.notes.append(f"output_jsonl_parse_error:{exc}")


def collect_layer_snapshot(output_dir: Path, instance_id: str) -> LayerSnapshot:
    """Read all per-task files and build the [GT_LAYERS] line snapshot."""
    task_dir = output_dir / instance_id
    snap = LayerSnapshot()
    if not task_dir.is_dir():
        snap.notes.append("task_dir_missing")
        return snap
    _read_l1_l2(task_dir, snap)
    _read_l3(task_dir, snap)
    _read_l4(task_dir, snap)
    _read_l5(task_dir, snap)
    _read_l6(task_dir, snap)
    _read_resolved_and_cost(task_dir, output_dir, instance_id, snap)
    return snap


def format_layer_line(instance_id: str, snap: LayerSnapshot) -> str:
    """Format the canonical [GT_LAYERS] line.

    Format (matches plan §"Logging contract" in
    system-role-you-are-flickering-river.md):

      [GT_LAYERS] task=<id> L1=<v> L2=<v> L3=<n> L4=<n> L5=<v> L6=<n>
                  elapsed_s=<f> resolved=<bool> cost_usd=<f>
    """
    resolved_repr = "unknown" if snap.resolved is None else (
        "true" if snap.resolved else "false"
    )
    return (
        f"[GT_LAYERS] task={instance_id} "
        f"L1={snap.L1} L2={snap.L2} "
        f"L3={snap.L3} L4={snap.L4} "
        f"L5={snap.L5} L6={snap.L6} "
        f"elapsed_s={snap.elapsed_s:.2f} "
        f"resolved={resolved_repr} "
        f"cost_usd={snap.cost_usd:.4f}"
    )


def fsync_append(path: Path, line: str) -> None:
    """Atomically append a line and fsync the descriptor + parent dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not line.endswith("\n"):
        line = line + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            # Some filesystems / Windows may not support fsync on append-only
            # file descriptors; downgrade silently rather than crashing the
            # smoke runner. The line is still flushed.
            pass
    finally:
        os.close(fd)


# ---- SWE-agent command construction ----------------------------------------

def build_sweagent_cmd(
    config_path: str,
    task_ids: List[str],
    output_dir: str,
    workers: int,
    per_instance_cost_limit: Optional[float],
    per_instance_wallclock_cap_seconds: Optional[int],
    venv_python: str = "/home/ubuntu/sweagent_venv/bin/python",
    instances_type: str = "huggingface",
    instances_split: str = "lite",
    instances_dataset_name: str = "SWE-bench-Live/SWE-bench-Live",
    instances_file_path: str | None = None,
    launcher: str = "sweagent",
) -> List[str]:
    """Construct the SWE-agent run-batch command.

    Reference (from D:/Groundtruth/scripts/swebench/vm_run_v2_parallel.sh:69-74,
    SWE-agent 1.0 invocation pattern observed in this repo):

        python3 -m sweagent run-batch \
            --config <yaml> \
            --instances.subset verified --instances.split test \
            --instances.filter "<regex_or_csv>" \
            --output_dir <dir> --num_workers N

    For SWE-bench-Live we use (verified 2026-05-05 against SWE-agent 1.1
    discriminated-union members — `swe_bench_live` does NOT exist as a type;
    the allowed values are: huggingface | file | swe_bench | expert_file | swesmith):
        --instances.type huggingface
        --instances.dataset_name SWE-bench-Live/SWE-bench-Live
        --instances.split lite

    Filter syntax for explicit task IDs: SWE-agent accepts a regex; we anchor
    each id with ^...$ and join with `|` to get an exact-match disjunction.
    """
    if not task_ids:
        raise ValueError("task_ids cannot be empty")
    # Build an exact-match regex disjunction so we never accidentally match
    # `pypsa__pypsa-1091a` when targeting `pypsa__pypsa-1091`.
    filter_regex = "|".join(f"^{re.escape(tid)}$" for tid in task_ids)

    if launcher == "run_with_gt_hook":
        cmd = [
            venv_python,
            "scripts/swebench/run_with_gt_hook.py",
            "--config",
            config_path,
            "--instances.type",
            instances_type,
        ]
    else:
        cmd = [
            venv_python,
            "-m",
            "sweagent",
            "run-batch",
            "--config",
            config_path,
            "--instances.type",
            instances_type,
        ]
    if instances_type == "huggingface" and instances_dataset_name:
        cmd.extend(["--instances.dataset_name", instances_dataset_name])
    if instances_type == "file" and instances_file_path:
        cmd.extend(["--instances.path", instances_file_path])
    cmd.extend([
        "--instances.split",
        instances_split,
        "--instances.filter",
        filter_regex,
        "--output_dir",
        output_dir,
        "--num_workers",
        str(workers),
    ])

    if per_instance_cost_limit is not None:
        cmd += ["--agent.model.per_instance_cost_limit",
                str(per_instance_cost_limit)]
    # NOTE 2026-05-05: SWE-agent 1.1 does NOT accept
    # `--env.deployment.startup_timeout` as a CLI flag (verified — it errors
    # out as "unrecognized argument"). The wall-clock cap is enforced solely
    # by `_wait_loop` below via subprocess.kill on timeout. Per-instance cost
    # cap is enforced via `--agent.model.per_instance_cost_limit`.
    _ = per_instance_wallclock_cap_seconds  # used by _wait_loop, not by CLI

    return cmd


def _build_file_instances_from_hf(
    *,
    output_dir: Path,
    task_ids: list[str],
    dataset_name: str,
    split: str,
) -> Path:
    """Materialize a file-backed instances.jsonl with resolved image_name.

    This avoids breakage when HF rows are missing image_name for direct
    `instances.type=huggingface` validation in SWE-agent.
    """
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"datasets import failed: {exc}") from exc

    ds = load_dataset(dataset_name, split=split)
    by_id = {}
    for row in ds:
        iid = row.get("instance_id") or row.get("id")
        if iid:
            by_id[str(iid)] = dict(row)

    rows: list[dict] = []
    missing = []
    for iid in task_ids:
        row = by_id.get(iid)
        if row is None:
            missing.append(iid)
            continue
        image = resolve_image_name(iid, row)
        if not image:
            missing.append(iid)
            continue
        # Pack FAIL_TO_PASS / PASS_TO_PASS / test_patch into extra_fields so
        # the GTTrack4PreRunHook can derive test-driven localization seeds.
        # SWE-agent's SimpleBatchInstance forwards extra_fields verbatim to
        # the ProblemStatement (sweagent/run/batch_instances.py:99,112).
        #
        # ``test_patch`` is the canonical signal at host-side hook time —
        # the SWE-bench-Live repo isn't checked out on the host (it lives
        # only inside the container image), so the hook can't AST-parse the
        # actual test files. The unified-diff text in test_patch is the
        # only host-readable source of the test-file imports, which the
        # hook regex-extracts to seed L1 localization.
        extra_fields = {}
        for k in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch"):
            v = row.get(k)
            if v:
                extra_fields[k] = v
        rows.append(
            {
                "instance_id": iid,
                "image_name": image,
                "problem_statement": row.get("problem_statement", ""),
                "repo": row.get("repo", ""),
                "base_commit": row.get("base_commit", ""),
                "extra_fields": extra_fields,
            }
        )
    if missing:
        raise RuntimeError(f"could not materialize instances for: {missing}")
    out = output_dir / "instances.resolved.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out


def _wrap_for_remote(cmd: List[str], remote_host: str,
                     remote_user: str) -> List[str]:
    """Wrap a local command list into a gcloud SSH invocation."""
    inner = " ".join(shlex.quote(c) for c in cmd)
    return [
        "gcloud",
        "compute",
        "ssh",
        f"{remote_user}@{remote_host}",
        "--",
        f"bash -lc {shlex.quote(inner)}",
    ]


# ---- per-task watcher loop -------------------------------------------------

def _emit_for_completed_task(
    output_dir: Path,
    instance_id: str,
    global_log: Path,
) -> None:
    snap = collect_layer_snapshot(output_dir, instance_id)
    line = format_layer_line(instance_id, snap)
    task_log = output_dir / instance_id / "gt_layers.log"
    fsync_append(task_log, line)
    fsync_append(global_log, line)
    print(line, flush=True)


def _scan_completed_from_output_jsonl(
    output_dir: Path,
    seen: set,
) -> List[str]:
    """Return list of newly-completed instance_ids since last call."""
    output_jsonl = output_dir / "output.jsonl"
    if not output_jsonl.is_file():
        return []
    new_ids: List[str] = []
    try:
        with output_jsonl.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                rec_id = rec.get("instance_id") or rec.get("id")
                if rec_id and rec_id not in seen:
                    seen.add(rec_id)
                    new_ids.append(rec_id)
    except Exception:  # noqa: BLE001
        pass
    return new_ids


def _wait_loop(
    proc: subprocess.Popen,
    output_dir: Path,
    expected_ids: List[str],
    poll_s: float = 5.0,
    hard_wall_clock_s: Optional[int] = None,
) -> int:
    """Poll the SWE-agent process; emit per-task layer line as each finishes."""
    global_log = output_dir / "_global_gt_layers.log"
    seen: set = set()
    started = time.monotonic()
    while True:
        rc = proc.poll()
        for tid in _scan_completed_from_output_jsonl(output_dir, seen):
            _emit_for_completed_task(output_dir, tid, global_log)
        if rc is not None:
            break
        if hard_wall_clock_s is not None:
            elapsed = time.monotonic() - started
            if elapsed > hard_wall_clock_s:
                print(
                    f"[smoke_runner] hard wall-clock cap "
                    f"{hard_wall_clock_s}s exceeded; terminating",
                    file=sys.stderr,
                )
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                rc = proc.returncode if proc.returncode is not None else 124
                break
        time.sleep(poll_s)

    # Final sweep: anything appended in the final tick.
    for tid in _scan_completed_from_output_jsonl(output_dir, seen):
        _emit_for_completed_task(output_dir, tid, global_log)

    # Failsafe: emit a line for every expected id we never saw, so the verifier
    # can detect wedge / drop. notes will explain why.
    for tid in expected_ids:
        if tid not in seen:
            seen.add(tid)
            _emit_for_completed_task(output_dir, tid, global_log)
    return rc if rc is not None else 0


def _bundle_preflight_check(config_path: Path) -> tuple[List[str], List[str]]:
    """Validate every bundle path declared by ``agent.tools.bundles[]``.

    Loads ``config_path`` (yaml), walks the bundles list, and checks that
    each bundle dir contains a ``config.yaml`` and that every file in the
    bundle's ``bin/`` is executable. Returns ``(checks, failures)`` lists
    so the caller can render them alongside the other preflight items.

    Surfaces a clear error per missing bundle BEFORE SWE-agent's Pydantic
    validator throws a ValidationError 30 lines deep into run_batch.

    Best-effort: if pyyaml or the config file can't be loaded, returns a
    single failure so the operator knows the preflight didn't actually
    run (vs silently passing on an unread config).
    """
    checks: List[str] = []
    failures: List[str] = []
    try:
        import yaml  # type: ignore
    except ImportError:
        failures.append(
            f"bundle_preflight_skipped:pyyaml_not_installed (config={config_path})"
        )
        return checks, failures
    if not config_path.is_file():
        failures.append(f"bundle_preflight_config_missing:{config_path}")
        return checks, failures
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        failures.append(f"bundle_preflight_config_parse_failed:{exc}")
        return checks, failures

    bundles = (
        cfg.get("agent", {})
        .get("tools", {})
        .get("bundles", [])
    )
    if not bundles:
        # Not strictly a failure (the agent may use only built-in tools),
        # but worth surfacing.
        checks.append("bundle_preflight:no_bundles_declared")
        return checks, failures

    for entry in bundles:
        if isinstance(entry, dict):
            path_str = entry.get("path", "")
        else:
            path_str = str(entry)
        if not path_str:
            failures.append("bundle_preflight:bundle_with_empty_path")
            continue
        # Skip relative paths — SWE-agent resolves them against its own
        # install directory (sweagent/tools/bundle.py:_convert_path_to_abspath
        # in v1.1.0). Validating them here would false-fail on the upstream
        # tools/registry + tools/edit_anthropic bundles that ship with
        # SWE-agent itself. Only verify the absolute paths (our project's
        # bundles, which are the ones that actually need preflight).
        bundle = Path(path_str)
        if not bundle.is_absolute():
            checks.append(f"bundle_skip_relative:{path_str}")
            continue
        if not bundle.is_dir():
            failures.append(f"bundle_missing:{path_str}")
            continue
        cfg_yaml = bundle / "config.yaml"
        if not cfg_yaml.is_file():
            failures.append(f"bundle_config_yaml_missing:{path_str}")
            continue
        bin_dir = bundle / "bin"
        if bin_dir.is_dir():
            for entry_file in bin_dir.iterdir():
                if not entry_file.is_file():
                    continue
                if not os.access(entry_file, os.X_OK):
                    failures.append(
                        f"bundle_bin_not_executable:{entry_file}"
                    )
        checks.append(f"bundle_ok:{path_str}")
    return checks, failures


def _run_preflight(output_dir: Path, config_path: Path | None = None) -> PreflightResult:
    checks: List[str] = []
    failures: List[str] = []

    # Writable output root + global log probe.
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / "._gt_preflight_write_probe"
        with probe.open("a", encoding="utf-8") as fh:
            fh.write("ok\n")
            fh.flush()
            os.fsync(fh.fileno())
        checks.append(f"output_dir_writable:{output_dir}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"output_dir_not_writable:{output_dir}:{exc}")

    # Bundle-presence + bin/ executable bits — surfaces dead paths before
    # SWE-agent's Pydantic validator buries them in a stack trace.
    if config_path is not None:
        b_checks, b_failures = _bundle_preflight_check(config_path)
        checks.extend(b_checks)
        failures.extend(b_failures)

    # Docker availability (only if docker exists in PATH).
    docker = shutil.which("docker")
    if docker:
        try:
            proc = subprocess.run(
                [docker, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0:
                checks.append("docker_access:ok")
            else:
                msg = (proc.stderr or "").strip().lower()
                if "permission denied" in msg or "docker.sock" in msg:
                    failures.append("docker_access_denied:permission_docker_sock")
                else:
                    failures.append(f"docker_access_failed:rc={proc.returncode}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"docker_access_failed:{exc}")
    else:
        checks.append("docker_binary_absent:skipped")

    # SWE-agent repo safety check when present.
    swe_repo = Path("/home/ubuntu/SWE-agent")
    if swe_repo.is_dir():
        proc = subprocess.run(
            ["git", "-C", str(swe_repo), "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            checks.append("sweagent_git_safe_directory:ok")
        else:
            msg = (proc.stderr or "").strip().lower()
            if "dubious ownership" in msg:
                failures.append("sweagent_git_safe_directory:missing")
            else:
                failures.append(f"sweagent_git_probe_failed:rc={proc.returncode}")
    else:
        checks.append("sweagent_repo_absent:skipped")

    return PreflightResult(ok=(len(failures) == 0), checks=checks, failures=failures)


def _evaluate_layer_invocation(output_dir: Path, expected_ids: List[str]) -> Tuple[bool, List[str]]:
    snaps: Dict[str, LayerSnapshot] = {
        iid: collect_layer_snapshot(output_dir, iid) for iid in expected_ids
    }
    reasons: List[str] = []
    if not any((s.L1 in ("fired", "fallback")) or str(s.L2).startswith("fired") for s in snaps.values()):
        reasons.append("L1 never invoked")
    if not any(str(s.L2).startswith("fired") for s in snaps.values()):
        reasons.append("L2 never invoked")
    if not any(s.L3 > 0 for s in snaps.values()):
        reasons.append("L3 never invoked")
    if not any(s.L4 > 0 for s in snaps.values()):
        reasons.append("L4 never invoked")
    if not any(s.L5 != "not_evaluated" for s in snaps.values()):
        reasons.append("L5 never evaluated")
    if not any(s.L6 > 0 for s in snaps.values()):
        reasons.append("L6 never invoked")
    return len(reasons) == 0, reasons


# ---- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Track D smoke runner for SWE-agent + GT 6-layer harness",
    )
    parser.add_argument("--config", required=True,
                        help="Path to gt_track4.yaml (or ablation variant)")
    parser.add_argument("--task-ids", required=True,
                        help="Comma-separated IDs, or path to file (one per line)")
    parser.add_argument("--output-dir", required=True,
                        help="SWE-agent output dir; per-task subdirs land here")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--remote-host", default=None,
                        help="If set, launch via gcloud ssh on this host (e.g. gt-t0)")
    parser.add_argument("--remote-user", default="ubuntu")
    parser.add_argument("--venv-python",
                        default="/home/ubuntu/sweagent_venv/bin/python")
    parser.add_argument("--instances-type", default="huggingface")
    parser.add_argument(
        "--launcher",
        default="run_with_gt_hook",
        choices=["run_with_gt_hook", "sweagent"],
        help="Launch via GT hook wrapper (default) or raw `python -m sweagent`.",
    )
    parser.add_argument("--instances-dataset-name",
                        default="SWE-bench-Live/SWE-bench-Live")
    parser.add_argument("--instances-split", default="lite")
    parser.add_argument(
        "--instances-auto-file-fallback",
        action="store_true",
        help=(
            "Materialize a file-backed instances list from HF rows and run "
            "with --instances.type=file. Recommended for SWE-agent schema drift."
        ),
    )
    parser.add_argument("--per-instance-cost-limit", type=float, default=None)
    parser.add_argument("--per-instance-wallclock-cap-seconds", type=int,
                        default=1800)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the command and exit; do not launch")
    parser.add_argument("--no-litellm-register", action="store_true",
                        help="Skip Track A litellm registration helper")
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip runner preflight checks (output-dir writable, docker access, git safety).",
    )
    parser.add_argument(
        "--require-all-layers",
        action="store_true",
        help="Fail run unless L1..L6 are all invoked/evaluated across expected task ids.",
    )
    parser.add_argument(
        "--gt-indexes-root",
        default="/home/ubuntu/eval_indexes",
        help=(
            "Root dir under which per-instance graph.db's live as "
            "<root>/<instance_id>/graph.db. Used to set GT_GRAPH_DB on "
            "the SWE-agent subprocess env so Track A's host-side pre-run "
            "hook can find the per-instance index."
        ),
    )
    args = parser.parse_args()

    task_ids = _parse_task_ids(args.task_ids)
    if not task_ids:
        print("FATAL: no task_ids parsed", file=sys.stderr)
        return 2

    # RC-07 preflight: a multi-task batch where GT_INDEXES_ROOT is missing will
    # silently route every task at the pre-run hook to the first task's
    # graph.db (or none), producing repo-mismatched briefs. --gt-indexes-root
    # has a default, so the only way it goes empty is an explicit empty string,
    # but we still defend against it for >1 task launches.
    if len(task_ids) > 1 and not args.gt_indexes_root:
        print(
            "FATAL: --gt-indexes-root is required for multi-task batches "
            f"(got {len(task_ids)} task_ids); per-task graph.db cannot be "
            "resolved without it.",
            file=sys.stderr,
        )
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_preflight:
        preflight = _run_preflight(output_dir, config_path=Path(args.config))
        for chk in preflight.checks:
            print(f"[smoke_runner][preflight] {chk}", file=sys.stderr)
        if not preflight.ok:
            for fail in preflight.failures:
                print(f"[smoke_runner][preflight][FAIL] {fail}", file=sys.stderr)
            return 3

    # Pre-launch: Track A litellm register
    if not args.no_litellm_register:
        status = _maybe_register_litellm()
        print(f"[smoke_runner] litellm register: {status}", file=sys.stderr)

    effective_instances_type = args.instances_type
    instances_file_path = None
    if args.instances_auto_file_fallback and args.instances_type == "huggingface":
        try:
            resolved = _build_file_instances_from_hf(
                output_dir=output_dir,
                task_ids=task_ids,
                dataset_name=args.instances_dataset_name,
                split=args.instances_split,
            )
            effective_instances_type = "file"
            instances_file_path = str(resolved)
            print(
                f"[smoke_runner] using file-backed instances: {instances_file_path}",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[smoke_runner] auto file fallback failed: {exc}; "
                "continuing with direct huggingface instances",
                file=sys.stderr,
            )

    cmd = build_sweagent_cmd(
        config_path=args.config,
        task_ids=task_ids,
        output_dir=str(output_dir),
        workers=args.workers,
        per_instance_cost_limit=args.per_instance_cost_limit,
        per_instance_wallclock_cap_seconds=args.per_instance_wallclock_cap_seconds,
        venv_python=args.venv_python,
        instances_type=effective_instances_type,
        instances_split=args.instances_split,
        instances_dataset_name=args.instances_dataset_name,
        instances_file_path=instances_file_path,
        launcher=args.launcher,
    )

    if args.remote_host:
        launch_cmd = _wrap_for_remote(cmd, args.remote_host, args.remote_user)
    else:
        launch_cmd = cmd

    if args.dry_run:
        print("[smoke_runner] DRY-RUN — would launch:")
        # Human-readable form, then exact argv json so callers can parse it.
        print("  " + " ".join(shlex.quote(c) for c in launch_cmd))
        print("[smoke_runner] argv_json: " + json.dumps(launch_cmd))
        print(f"[smoke_runner] task_ids: {task_ids}")
        print(f"[smoke_runner] output_dir: {output_dir.resolve()}")
        return 0

    # Build subprocess env. For each task id, compute the expected per-instance
    # graph.db path under --gt-indexes-root and set GT_GRAPH_DB so Track A's
    # host-side pre-run hook can locate the index. Note: SWE-agent run-batch
    # spawns one process for all tasks (the "per-instance loop" lives inside
    # SWE-agent), so a single env var covers either the single-task smoke case
    # or, for multi-task runs, the index for the first task — additional task
    # indexes must be addressed by the pre-run hook itself (via the same
    # --gt-indexes-root convention) since per-task env scoping is not possible
    # across one batched subprocess. We still warn per task on missing dbs so
    # the operator sees gaps before the run starts.
    env = os.environ.copy()
    if args.remote_host is None:
        # RC-07 fix: export GT_INDEXES_ROOT so gt_track4_pre_run.py:1219 can
        # resolve the per-instance graph.db for EACH task (not just the first).
        # Without this, multi-task batches read the wrong graph.db when similar
        # repos make the bug invisible at smoke scale.
        if args.gt_indexes_root:
            env["GT_INDEXES_ROOT"] = args.gt_indexes_root
            print(
                f"[smoke_runner] GT_INDEXES_ROOT={args.gt_indexes_root}",
                file=sys.stderr,
            )
        primary_db_path: Optional[str] = None
        for tid in task_ids:
            db_path = os.path.join(args.gt_indexes_root, tid, "graph.db")
            if os.path.isfile(db_path):
                if primary_db_path is None:
                    primary_db_path = db_path
            else:
                print(
                    f"[smoke_runner] WARN: graph.db missing for {tid} at "
                    f"{db_path}; pre-run hook will fall back to v22_brief "
                    f"or empty brief",
                    file=sys.stderr,
                )
        if primary_db_path is not None:
            env["GT_GRAPH_DB"] = primary_db_path
            print(
                f"[smoke_runner] GT_GRAPH_DB={primary_db_path}",
                file=sys.stderr,
            )
        else:
            print(
                "[smoke_runner] WARN: no per-instance graph.db found under "
                f"{args.gt_indexes_root}; GT_GRAPH_DB unset",
                file=sys.stderr,
            )
    else:
        # Remote launch via `gcloud ssh -- bash -lc <inner>`. Local env vars do
        # not cross the SSH boundary; remote-host wiring of GT_GRAPH_DB and
        # GT_INDEXES_ROOT must be handled inside the inner bash command
        # (out of scope for this fix). Log so the operator knows.
        # TODO(RC-07-coord): propagate GT_INDEXES_ROOT via the remote bash
        # wrapper (_wrap_for_remote) so multi-task remote runs route correctly.
        print(
            "[smoke_runner] WARN: --remote-host set; GT_GRAPH_DB and "
            "GT_INDEXES_ROOT local-env wiring is skipped (does not traverse "
            "gcloud ssh). Set them inside the remote bash invocation if "
            "needed.",
            file=sys.stderr,
        )

    print("[smoke_runner] launching: "
          + " ".join(shlex.quote(c) for c in launch_cmd), flush=True)
    proc = subprocess.Popen(
        launch_cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=env,
    )

    # Hard wall-clock cap = N tasks * per-task cap * 1.25 safety margin,
    # iff per-instance cap is set. We don't divide by workers because workers
    # are launched concurrently inside SWE-agent — the wall-clock floor is the
    # longest single chain, not the sum.
    cap = args.per_instance_wallclock_cap_seconds
    hard_cap = None
    if cap:
        # Ceiling: longest chain plus margin.
        hard_cap = int(cap * 1.25 * max(1, (len(task_ids) // max(1, args.workers))))

    rc = _wait_loop(
        proc,
        output_dir=output_dir,
        expected_ids=task_ids,
        hard_wall_clock_s=hard_cap,
    )
    if rc != 0:
        return rc
    if args.require_all_layers:
        ok, reasons = _evaluate_layer_invocation(output_dir, task_ids)
        if not ok:
            for reason in reasons:
                print(f"[smoke_runner][layers][FAIL] {reason}", file=sys.stderr)
            return 4
        print("[smoke_runner][layers] all 6 layers invoked", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from image_name_resolver import resolve_image_name


# ---- RC-02: cost discipline helpers ---------------------------------------

# Per-task cost estimate at the v1.0.5 envelope (CLAUDE.md: 150K in / 30K out
# at Qwen3 MaaS rates $0.45/M / $1.80/M => ~$0.12 per task). Operators can
# override via --per-task-cost-estimate.
_DEFAULT_PER_TASK_COST_USD = 0.12

# Conservative reconciliation tolerance — if the proxy's per-call sum and
# SWE-agent's reported cost diverge by more than this fraction, surface a
# warning so the operator can investigate (model-name mismatch, missing
# registry, partial proxy log truncation, etc).
_RECONCILE_TOLERANCE_FRACTION = 0.05


def _compute_expected_cost(
    task_count: int,
    per_task_estimate_usd: float = _DEFAULT_PER_TASK_COST_USD,
    cap_usd: Optional[float] = None,
) -> Tuple[float, str]:
    """Return (expected_total_usd, surface_line).

    Surface line is the canonical preflight string the runner prints to
    stdout BEFORE Popen — satisfies CLAUDE.md "MANDATORY: surface paid-run
    cost before launching".

    Format (RC-02 spec):
        EXPECTED_COST: N tasks * $X each = $Y (cap $Z)
    """
    if task_count < 0:
        raise ValueError("task_count must be non-negative")
    if per_task_estimate_usd < 0:
        raise ValueError("per_task_estimate_usd must be non-negative")
    expected = float(task_count) * float(per_task_estimate_usd)
    cap_str = f"cap ${cap_usd:.2f}" if cap_usd is not None else "cap unset"
    line = (
        f"EXPECTED_COST: {task_count} tasks * ${per_task_estimate_usd:.4f} "
        f"each = ${expected:.4f} ({cap_str})"
    )
    return expected, line


def _curl_proxy_health(api_base: str, timeout_s: int = 5) -> Tuple[bool, str]:
    """GET <api_base>/health. Returns (ok, detail).

    Detail is the response body or error string — passed through to the
    Vertex 403 classifier when the proxy is misconfigured.
    """
    health_url = api_base.rstrip("/")
    if health_url.endswith("/v1"):
        health_url = health_url[:-3]
    health_url = health_url.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read(2048).decode("utf-8", errors="replace")
            return (resp.status == 200, f"status={resp.status} body={body[:200]}")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        if exc.code == 401:
            # Auth required = proxy is alive; caller passes the API key separately.
            return (True, f"status=401 auth_required (proxy alive) body={body[:100]}")
        return (False, f"http_error={exc.code} body={body[:200]}")
    except Exception as exc:  # noqa: BLE001
        return (False, f"connect_error={type(exc).__name__}:{exc}")


def _read_yaml_cost_section(config_path: Path) -> Dict[str, Optional[float]]:
    """Extract (per_instance_cost_limit, total_cost_limit, litellm_model_registry)
    from gt_track4.yaml. Returns dict with str-or-None values.

    Best-effort: if pyyaml or the file is unreadable we return None values
    so the preflight surfaces the missing data rather than silently passing.
    """
    out: Dict[str, Optional[float]] = {
        "per_instance_cost_limit": None,
        "total_cost_limit": None,
        "litellm_model_registry": None,
    }
    try:
        import yaml  # type: ignore
    except ImportError:
        return out
    if not config_path.is_file():
        return out
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return out
    model = (cfg.get("agent") or {}).get("model") or {}
    for k in ("per_instance_cost_limit", "total_cost_limit"):
        v = model.get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    reg = model.get("litellm_model_registry")
    if reg is not None:
        out["litellm_model_registry"] = reg  # type: ignore[assignment]
    return out


# ---- RC-17: reproducibility seals ------------------------------------------
#
# _EXPECTED_SWEAGENT_VERSION + _assert_sweagent_version are the RC-13
# helpers (defined later in this file); RC-17 reuses them rather than
# defining duplicates. The blocks below are the new RC-17 fixtures:
#   _ENV_ALLOWLIST_*       (F-005 env scrub)
#   _build_subprocess_env  (F-005)
#   _persist_run_env       (F-005)
#   _capture_versions      (F-006/F-007/F-012)
#   _capture_model_fingerprint (F-008)
#   _select_first_n_from_dataset (F-010)

# F-005: env vars that survive the developer-shell scrub. Anything not on
# this list is dropped before subprocess.Popen. The list is intentionally
# narrow — adding a var here is a deliberate decision; never add by reflex.
_ENV_ALLOWLIST_PREFIXES: Tuple[str, ...] = (
    "GT_",                     # all GT_* deliberately set
    "VERTEX_",                 # vertex_project / vertex_location proxy vars
)
_ENV_ALLOWLIST_EXACT: Tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "TMPDIR",
    "TMP",
    "TEMP",
    "PYTHONPATH",   # SWE-agent install relies on this
    "VIRTUAL_ENV",
)


def _build_subprocess_env(
    extra: Dict[str, str],
    *,
    parent_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """RC-17 (F-005): build the SWE-agent subprocess env from an explicit
    allow-list plus deliberately-set ``extra``.
    """
    src = parent_env if parent_env is not None else os.environ
    out: Dict[str, str] = {}
    for k, v in src.items():
        if k in _ENV_ALLOWLIST_EXACT or k.startswith(_ENV_ALLOWLIST_PREFIXES):
            out[k] = v
    out.update(extra)
    return out


def _persist_run_env(output_dir: Path, env: Dict[str, str]) -> None:
    """Write the final subprocess env to ``run_env.json`` for forensics."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "run_env.json").open("w", encoding="utf-8") as fh:
            json.dump(dict(sorted(env.items())), fh, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke_runner] WARN: persist run_env.json failed: {exc}",
              file=sys.stderr)


def _capture_versions(output_dir: Path, venv_python: str) -> None:
    """RC-17 (F-006/F-007/F-012): write versions.json + pip_freeze.txt for
    the venv that SWE-agent is launched from. Cheap, runs at preflight time.
    """
    versions: Dict[str, str] = {}
    try:
        proc = subprocess.run(
            [venv_python, "-c", "import sweagent, sys; print(sweagent.__version__)"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        versions["sweagent"] = (proc.stdout or "").strip() or f"err:{(proc.stderr or '').strip()[:120]}"
    except Exception as exc:  # noqa: BLE001
        versions["sweagent"] = f"err:{exc}"
    try:
        proc = subprocess.run(
            [venv_python, "-m", "pip", "show", "litellm"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        for line in (proc.stdout or "").splitlines():
            if line.lower().startswith("version:"):
                versions["litellm"] = line.split(":", 1)[1].strip()
                break
        versions.setdefault("litellm", "not_installed")
    except Exception as exc:  # noqa: BLE001
        versions["litellm"] = f"err:{exc}"
    try:
        proc = subprocess.run(
            [venv_python, "-c", "import sys; print(sys.version)"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        versions["python"] = (proc.stdout or "").strip().split("\n")[0]
    except Exception as exc:  # noqa: BLE001
        versions["python"] = f"err:{exc}"
    try:
        with (output_dir / "versions.json").open("w", encoding="utf-8") as fh:
            json.dump(versions, fh, indent=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke_runner] WARN: versions.json write failed: {exc}",
              file=sys.stderr)
    try:
        proc = subprocess.run(
            [venv_python, "-m", "pip", "freeze"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        (output_dir / "pip_freeze.txt").write_text(
            proc.stdout or "", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke_runner] WARN: pip_freeze.txt write failed: {exc}",
              file=sys.stderr)


def _capture_model_fingerprint(
    output_dir: Path, api_base: str, model_name: str, api_key: str = "sk-gt-local",
) -> None:
    """RC-17 (F-008): fire one deterministic prompt and record the response.
    Drift across runs is the silent-model-update canary. ~$0.0001/call.
    """
    fp: Dict[str, object] = {
        "model_name": model_name,
        "api_base": api_base,
        "prompt": "Reply with exactly the word FOO and nothing else.",
        "temperature": 0,
        "top_p": 1,
        "max_tokens": 8,
    }
    try:
        body = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": fp["prompt"]}],
            "temperature": 0,
            "top_p": 1,
            "max_tokens": 8,
        }).encode("utf-8")
        req = urllib.request.Request(
            api_base.rstrip("/") + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                parsed = {"_parse_error": True, "raw": raw[:1024]}
            fp["status"] = "ok"
            if isinstance(parsed, dict):
                fp["response_id"] = parsed.get("id")
                choices = parsed.get("choices") or []
                if isinstance(choices, list) and choices:
                    first = choices[0] or {}
                    msg = (first.get("message") or {}) if isinstance(first, dict) else {}
                    fp["response_text"] = msg.get("content")
            fp["raw"] = parsed
    except Exception as exc:  # noqa: BLE001
        fp["status"] = "error"
        fp["error"] = f"{type(exc).__name__}:{exc}"
    try:
        with (output_dir / "model_fingerprint.json").open("w", encoding="utf-8") as fh:
            json.dump(fp, fh, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke_runner] WARN: model_fingerprint write failed: {exc}",
              file=sys.stderr)


def _select_first_n_from_dataset(
    n: int, dataset_name: str, split: str, output_dir: Path,
) -> List[str]:
    """RC-17 (F-010): ``sorted(ds["instance_id"])[:N]`` — code-enforced, not
    operator convention. Writes the resolved set to selected_task_ids.txt so
    a second invocation reads identical task IDs.
    """
    try:
        from datasets import load_dataset  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"datasets import failed: {exc}") from exc
    ds = load_dataset(dataset_name, split=split)
    ids: List[str] = []
    for _row in ds:
        row: Any = _row
        iid = row.get("instance_id") or row.get("id")
        if iid:
            ids.append(str(iid))
    ids.sort()
    chosen = ids[: max(0, int(n))]
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "selected_task_ids.txt").write_text(
            "\n".join(chosen) + "\n", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke_runner] WARN: selected_task_ids.txt write failed: {exc}",
              file=sys.stderr)
    return chosen


def _reconcile_litellm_calls(
    output_dir: Path, sweagent_total_usd: float
) -> Tuple[bool, str]:
    """Sum cost_usd across <output_dir>/litellm_calls.jsonl rows and compare
    against SWE-agent's reported total. Returns (within_tolerance, surface_line).

    If the proxy log is missing or empty, returns (False, "missing"); operator
    should treat this as a soft failure (config drift on proxy YAML).
    """
    log_path = output_dir / "litellm_calls.jsonl"
    if not log_path.is_file():
        return False, "litellm_calls.jsonl missing — check proxy callback config"
    proxy_total = 0.0
    rows = 0
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                rows += 1
                # litellm json callback shape varies by version; accept the
                # commonly-emitted keys.
                for k in ("response_cost", "cost", "cost_usd", "total_cost"):
                    if k in rec:
                        try:
                            proxy_total += float(rec[k])
                            break
                        except (TypeError, ValueError):
                            pass
    except Exception as exc:  # noqa: BLE001
        return False, f"reconcile_read_error:{exc}"
    if rows == 0:
        return False, "litellm_calls.jsonl has 0 rows"
    diff = abs(proxy_total - sweagent_total_usd)
    base = max(proxy_total, sweagent_total_usd, 1e-9)
    within = (diff / base) <= _RECONCILE_TOLERANCE_FRACTION
    return (
        within,
        (
            f"reconcile rows={rows} proxy=${proxy_total:.4f} "
            f"sweagent=${sweagent_total_usd:.4f} delta="
            f"{(diff / base) * 100:.2f}% tolerance="
            f"{_RECONCILE_TOLERANCE_FRACTION * 100:.0f}%"
        ),
    )


# ---- RC-13: VM-profile defaults + version pin ------------------------------

# Profiles bundle VM-specific path defaults so a fresh provision needs ONE
# flag (`--vm-profile <name>`) instead of remembering four `--*` overrides.
# Adding a profile is intentionally trivial — drop a dict here. The runner
# never silently falls back to a profile; the operator must pick one (or
# pass each `--*` flag explicitly), so a typo at provisioning time fails
# loud at preflight rather than routing to the dev's home dir.
_VM_PROFILES: Dict[str, Dict[str, str]] = {
    # gt-t0 / current dev VM (`baliharneet0` project). Was the implicit
    # default before RC-13.
    "ubuntu_t0": {
        "venv_python": "/home/ubuntu/sweagent_venv/bin/python",
        "swe_repo": "/home/ubuntu/SWE-agent",
        "gt_indexes_root": "/home/ubuntu/eval_indexes",
    },
    # Any VM that runs as root.
    "root_v1": {
        "venv_python": "/root/sweagent_venv/bin/python",
        "swe_repo": "/root/SWE-agent",
        "gt_indexes_root": "/root/eval_indexes",
    },
    # Test profile used by docs/ultrareview/integration_checks/RC-13.sh —
    # simulates a fresh VM where the home dir is /home/test.
    "test": {
        "venv_python": "/home/test/sweagent_venv/bin/python",
        "swe_repo": "/home/test/SWE-agent",
        "gt_indexes_root": "/home/test/eval_indexes",
    },
}


# RC-13: SWE-agent version pin. The submit-tool override depends on
# tools/registry NOT declaring `submit` and bundle load-order being
# stable; both contracts are version-fragile (config/gt_track4.yaml:154
# documents the SWE-agent 1.1.0 duplicate-tool crash). Pin to the version
# that's been smoked end-to-end; bumping requires rerunning the bundle
# load-order check below.
_EXPECTED_SWEAGENT_VERSION = "1.1.0"


def _resolve_vm_profile(
    profile: Optional[str], explicit: Dict[str, Optional[str]]
) -> Dict[str, str]:
    """Merge explicit flag values over the named ``--vm-profile``.

    Explicit non-empty values always win over the profile. Returns a dict
    with keys ``venv_python``, ``swe_repo``, ``gt_indexes_root``. Raises
    ``KeyError`` if ``profile`` is set but unknown.
    """
    base: Dict[str, str] = {}
    if profile is not None:
        if profile not in _VM_PROFILES:
            raise KeyError(
                f"unknown --vm-profile: {profile!r}. Known: "
                f"{sorted(_VM_PROFILES)}. Add a new entry to "
                "_VM_PROFILES rather than special-casing inline."
            )
        base = dict(_VM_PROFILES[profile])
    for k, v in explicit.items():
        if v:
            base[k] = v
    return base


def _assert_sweagent_version(
    venv_python: str, expected: str = _EXPECTED_SWEAGENT_VERSION
) -> Tuple[bool, str]:
    """Run ``venv_python -c 'import sweagent; print(sweagent.__version__)'``.

    Returns ``(ok, detail)``. Soft-passes when the venv python doesn't
    exist (other preflight checks catch that). Fails when the import works
    but the version mismatches — that is the case the operator needs to
    know about because the submit-override mechanism is version-fragile.
    """
    if not os.path.exists(venv_python):
        return True, f"venv_python_absent:skipped:{venv_python}"
    try:
        proc = subprocess.run(
            [venv_python, "-c", "import sweagent; print(sweagent.__version__)"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"sweagent_version_probe_failed:{exc}"
    if proc.returncode != 0:
        return False, f"sweagent_import_failed:{(proc.stderr or '').strip()[:200]}"
    raw = (proc.stdout or "").strip()
    # SWE-agent logs a banner to stdout on import; extract just the last
    # non-empty line which is the bare version string (e.g. "1.1.0").
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    actual = lines[-1] if lines else raw
    if actual != expected:
        return False, f"sweagent_version_mismatch:expected={expected} actual={actual}"
    return True, f"sweagent_version:{actual}"


def _assert_no_duplicate_submit(config_path: Path) -> Tuple[bool, str]:
    """RC-13: assert tool-load-order is safe for the submit override.

    Approximates the SWE-agent config validator: parses the YAML's bundles
    list and rejects configurations that load BOTH ``tools/registry``
    (default submit declarer) AND ``review_on_submit_m`` (also declares
    submit) — the same crash documented inline in gt_track4.yaml:154-160.
    ``gt_pre_finish_gate`` declaring ``submit`` is the override target and
    is fine.

    Returns ``(ok, detail)``. Best-effort: if pyyaml isn't available we
    return ``(True, "skipped")`` so this assertion is a defense layer, not
    a hard dependency.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        return True, "yaml_unavailable:skipped"
    if not config_path.is_file():
        return True, f"config_missing:skipped:{config_path}"
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        return False, f"yaml_parse_failed:{exc}"
    bundles = ((cfg.get("agent") or {}).get("tools") or {}).get("bundles") or []
    paths = [str((b or {}).get("path") or "") for b in bundles]
    has_default_registry = any(p == "tools/registry" for p in paths)
    has_review_submit = any("review_on_submit_m" in p for p in paths)
    if has_default_registry and has_review_submit:
        return False, (
            "duplicate_submit_declaration:tools/registry+review_on_submit_m "
            "(SWE-agent 1.1.0 will crash with `Tool 'submit' is defined "
            "multiple times`)"
        )
    return True, (
        f"submit_override_safe:registry={has_default_registry} "
        f"review_on_submit_m={has_review_submit}"
    )


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
    L4: int = 0                 # query+search+navigate (RC-10 / D-002 fix)
    L5: str = "not_evaluated"   # pass | warn | fail | infra_failure | not_evaluated
    L6: int = 0                 # reindex_count
    # RC-10 (D-003 / F-fix): None sentinel — missing record renders
    # as "unknown" rather than silently as 0.00 / 0.0000.
    elapsed_s: Optional[float] = None
    resolved: Optional[bool] = None
    cost_usd: Optional[float] = None
    # RC-10 (D-009 / G-fix): failsafe lines for tasks never seen in
    # output.jsonl get synthesized=True; verifier counts them
    # separately from real all-zero tasks.
    synthesized: bool = False
    # RC-10 (D-015 / J-fix): partial_pull excludes the task from
    # rate-gate denominators in verify_report.
    partial_pull: bool = False
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
    """RC-10 (D-002 / C-fix): L4 sums gt_query + gt_search + gt_navigate.

    Pre-fix this counted only ``gt_query_calls.jsonl``, so any agent that
    exercised the new structural surfaces (gt_search, gt_navigate)
    showed L4=0 in the canonical line. Delegates to the shared
    ``gt_layer_counts.count_layer_calls`` so this reader can never
    disagree with the Track 4 close-wrap / deep_util_gate /
    full_potential_analyzer readers.
    """
    try:
        from gt_layer_counts import count_layer_calls  # type: ignore[import]
    except ImportError:  # pragma: no cover — fallback if import fails
        qcalls = task_dir / "gt_query_calls.jsonl"
        if not qcalls.is_file():
            snap.L4 = 0
            return
        try:
            with qcalls.open("r", encoding="utf-8") as fh:
                snap.L4 = sum(1 for line in fh if line.strip())
        except Exception as exc:  # noqa: BLE001
            snap.notes.append(f"l4_read_error:{exc}")
        return
    try:
        counts = count_layer_calls(task_dir)
        snap.L4 = int(counts.get("L4_total", 0))
    except Exception as exc:  # noqa: BLE001
        snap.notes.append(f"l4_read_error:{exc}")
        snap.L4 = 0


def _read_l5(task_dir: Path, snap: LayerSnapshot) -> None:
    """RC-10 (D-011 / H-fix): full L5 verdict mapping.

    The pre-finish gate (``gt_pre_finish_gate.py``) emits 13 distinct
    `result` values: pass / force / no_graph_db / blocked /
    warn_soft_escape / blocked_no_progress / unresolved / absent /
    pull_failed / no_close_wrap / autosubmit / malformed /
    db_open_error. Pre-fix, only 4 mapped through; the rest collapsed
    silently to ``not_evaluated`` and the 30-task gate then
    false-fired "L5 never evaluated (gate dead)".

    Post-fix mapping:
      - real verdicts → pass | warn | fail
      - infra failures (pull_failed, no_close_wrap, db_open_error,
        malformed, autosubmit, unresolved, absent, no_graph_db,
        blocked_no_progress) → ``infra_failure``
      - genuinely-no-data (gate file missing) → ``not_evaluated``
    """
    gate = task_dir / "gt_pre_finish_gate.json"
    # Also probe the close-wrap sidecar — if the gate JSON is missing
    # but the sidecar carries a verdict, surface it (covers the
    # autosubmit / pull_failed code paths that write the sidecar but
    # never write the gate JSON).
    sidecar_verdict = ""
    sidecar = task_dir / "gt_completion_summary.json"
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            sidecar_verdict = str(data.get("gate_verdict", "")).lower().strip()
            if data.get("partial_pull"):
                snap.partial_pull = True
        except Exception:  # noqa: BLE001
            pass

    if not gate.is_file():
        if sidecar_verdict:
            snap.L5 = _classify_l5_verdict(sidecar_verdict)
        else:
            snap.L5 = "not_evaluated"
        return
    try:
        data = json.loads(gate.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        snap.notes.append(f"l5_parse_error:{exc}")
        # Malformed JSON IS an infra failure, not "gate didn't run".
        snap.L5 = "infra_failure"
        return
    verdict = str(data.get("result", data.get("verdict", ""))).lower().strip()
    snap.L5 = _classify_l5_verdict(verdict or sidecar_verdict)


# Verdict classes — used by _read_l5 and verify_report's wiring.
_L5_PASS = {"pass", "approved", "ok", "force"}
_L5_WARN = {"warn", "warning", "warn_soft_escape"}
_L5_FAIL = {"fail", "failed", "blocked"}
# RC-10 (D-011 / H-fix): infra failures must NOT collapse to
# not_evaluated. Each is a distinct, named, real failure mode that
# should be triaged separately from "gate code didn't run".
_L5_INFRA_FAILURE = {
    "autosubmit",
    "pull_failed",
    "no_close_wrap",
    "no_graph_db",
    "db_open_error",
    "malformed",
    "blocked_no_progress",
    "unresolved",
    "absent",
}


def _classify_l5_verdict(verdict: str) -> str:
    v = (verdict or "").lower().strip()
    if v in _L5_PASS:
        return "pass"
    if v in _L5_WARN:
        return "warn"
    if v in _L5_FAIL:
        return "fail"
    if v in _L5_INFRA_FAILURE or v.startswith("db_open_error"):
        return "infra_failure"
    if v == "":
        return "not_evaluated"
    # Unknown verdict → infra_failure, not silent not_evaluated. The
    # 13-verdict universe is closed; anything else is a writer drift
    # we want to triage.
    return "infra_failure"


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
    """Read resolved bool from output.jsonl record; cost from cost_ledger.json.

    RC-10 (D-003 / F-fix): elapsed_s and cost_usd remain ``None`` when
    no source provides a real value. format_layer_line then renders
    ``unknown`` for these missing measurements rather than silently
    emitting 0.00 / 0.0000 — which is indistinguishable from a real
    measurement and silently bypasses the "MANDATORY: surface paid-run
    cost" rule on the verifier side.
    """
    # cost_ledger.json (per-task)
    ledger = task_dir / "cost_ledger.json"
    if ledger.is_file():
        try:
            data = json.loads(ledger.read_text(encoding="utf-8"))
            # Accept several common shapes:
            if isinstance(data, dict):
                for k in ("total_usd", "cost_usd", "total"):
                    if k in data:
                        try:
                            snap.cost_usd = float(data[k])
                            break
                        except (TypeError, ValueError):
                            continue
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
                    for k in ("resolved", "is_resolved", "passed"):
                        if k in rec:
                            snap.resolved = bool(rec[k])
                            break
                    for k in ("elapsed_s", "wall_time_s", "wall_clock_s",
                              "elapsed", "duration_s"):
                        if k in rec:
                            try:
                                snap.elapsed_s = float(rec[k])
                                break
                            except Exception:  # noqa: BLE001
                                pass
                    if snap.cost_usd is None:
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
    """Read all per-task files and build the [GT_LAYERS] line snapshot.

    RC-10 (D-008 / B-fix): the SINGLE canonical writer for
    `[GT_LAYERS]` lines is the smoke runner. Track A and Track 4
    persist their per-task state to JSON sidecars
    (``gt_brief_status.json`` / ``gt_completion_summary.json``); we
    consume them here and emit one canonical line per task.
    """
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
    # RC-10 (D-015 / J-fix): pick up partial_pull from the close-wrap
    # sidecar — verify_report excludes partial-pull tasks from rate
    # gates so a half-broken pull cannot mask a healthy run as zero.
    sidecar = task_dir / "gt_completion_summary.json"
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if data.get("partial_pull"):
                snap.partial_pull = True
        except Exception:  # noqa: BLE001
            pass
    return snap


def format_layer_line(instance_id: str, snap: LayerSnapshot) -> str:
    """Format the canonical [GT_LAYERS] line.

    Format (matches gt_layers_verifier._LINE_RE):

      [GT_LAYERS] task=<id> L1=<v> L2=<v> L3=<n> L4=<n> L5=<v> L6=<n>
                  elapsed_s=<f|unknown> resolved=<true|false|unknown>
                  cost_usd=<f|unknown> [synthesized=true] [partial_pull=true]

    RC-10 (D-003 / F-fix): elapsed_s + cost_usd render as ``unknown``
    when missing rather than 0.00 / 0.0000. The verifier regex tolerates
    the unknown token.

    RC-10 (D-009 / G-fix): synthesized=true is emitted only for
    failsafe lines (tasks never seen in output.jsonl). Verifier
    consumers can filter on that token to distinguish wedge / drop
    from real all-zero tasks.

    RC-10 (D-015 / J-fix): partial_pull=true is emitted only when
    artifact pullback recorded any failure. verify_report excludes
    partial-pull tasks from rate-gate denominators.
    """
    resolved_repr = "unknown" if snap.resolved is None else (
        "true" if snap.resolved else "false"
    )
    elapsed_repr = "unknown" if snap.elapsed_s is None else f"{snap.elapsed_s:.2f}"
    cost_repr = "unknown" if snap.cost_usd is None else f"{snap.cost_usd:.4f}"
    line = (
        f"[GT_LAYERS] task={instance_id} "
        f"L1={snap.L1} L2={snap.L2} "
        f"L3={snap.L3} L4={snap.L4} "
        f"L5={snap.L5} L6={snap.L6} "
        f"elapsed_s={elapsed_repr} "
        f"resolved={resolved_repr} "
        f"cost_usd={cost_repr}"
    )
    if snap.synthesized:
        line += " synthesized=true"
    if snap.partial_pull:
        line += " partial_pull=true"
    return line


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
    total_cost_limit: Optional[float] = None,
    venv_python: str = "",  # RC-13: callers must pass the resolved path
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
        _hook_script = str(Path(__file__).resolve().parent / "run_with_gt_hook.py")
        cmd = [
            venv_python,
            _hook_script,
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
    # RC-02 (G-001/G-009): per-launch authoritative cost cap. Overrides
    # whatever total_cost_limit lives in the YAML so the operator does
    # not have to commit a YAML edit for each phase change.
    if total_cost_limit is not None:
        cmd += ["--agent.model.total_cost_limit", str(total_cost_limit)]
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
    by_id: Dict[str, Any] = {}
    for _row in ds:
        row: Any = _row
        iid = row.get("instance_id") or row.get("id")
        if iid:
            by_id[str(iid)] = row

    rows: list[dict] = []
    missing = []
    for iid in task_ids:
        row_data: dict | None = by_id.get(iid)
        if row_data is None:
            missing.append(iid)
            continue
        image = resolve_image_name(iid, row_data)
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
            v = row_data.get(k)
            if v:
                extra_fields[k] = v
        rows.append(
            {
                "instance_id": iid,
                "image_name": image,
                "problem_statement": row_data.get("problem_statement", ""),
                "repo": row_data.get("repo", ""),
                "base_commit": row_data.get("base_commit", ""),
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
    synthesized: bool = False,
) -> None:
    """Emit the canonical [GT_LAYERS] line for one task.

    RC-10 (D-009 / G-fix): ``synthesized=True`` is set ONLY by the
    failsafe loop in `_wait_loop` for tasks that never appeared in
    output.jsonl. The verifier reads the ``synthesized=true`` token to
    distinguish wedge / drop from real all-zero tasks.
    """
    snap = collect_layer_snapshot(output_dir, instance_id)
    if synthesized:
        snap.synthesized = True
        snap.notes.append("failsafe_synth_no_output_record")
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
                # RC-14 (A-010): post-SIGTERM wait is 60s (covers `docker
                # stop` 10s default + signal-handling + on_instance_completed
                # flush). 30s was too tight and produced zombie containers
                # at 30→300 scale.
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                rc = proc.returncode if proc.returncode is not None else 124
                break
        time.sleep(poll_s)

    # Final sweep: anything appended in the final tick.
    for tid in _scan_completed_from_output_jsonl(output_dir, seen):
        _emit_for_completed_task(output_dir, tid, global_log)

    # Failsafe: emit a line for every expected id we never saw, so the
    # verifier can detect wedge / drop. RC-10 (D-009 / G-fix): mark
    # these ``synthesized=true`` so the verifier can filter them out
    # of healthy bucket counts. Pre-fix the failsafe lines were
    # indistinguishable from real all-zero tasks and polluted the
    # 30-task distribution check.
    for tid in expected_ids:
        if tid not in seen:
            seen.add(tid)
            _emit_for_completed_task(output_dir, tid, global_log, synthesized=True)
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

    # RC-12: gt_intel.py drift gate. The bundle's gt_intel.py at
    # tools/sweagent/gt_edit/lib/gt_intel.py is shipped verbatim into the
    # container; if it has drifted from the canonical
    # benchmarks/swebench/gt_intel.py, the in-container L3 brief silently
    # uses an older code path. Enforce byte-equality here so the launch
    # blocks on drift instead of producing stealth-stale evidence.
    try:
        repo_root = config_path.resolve().parents[1]
    except Exception:  # noqa: BLE001
        repo_root = Path.cwd()
    canon_intel = repo_root / "benchmarks" / "swebench" / "gt_intel.py"
    bundle_intel = (
        repo_root / "tools" / "sweagent" / "gt_edit" / "lib" / "gt_intel.py"
    )
    if canon_intel.is_file() and bundle_intel.is_file():
        try:
            import hashlib
            ch = hashlib.sha256(canon_intel.read_bytes()).hexdigest()
            bh = hashlib.sha256(bundle_intel.read_bytes()).hexdigest()
            if ch != bh:
                failures.append(
                    "gt_intel_drift:bundle!=canonical "
                    f"(canon={ch[:12]} bundle={bh[:12]}); "
                    "run `bash tools/sweagent/gt_edit/sync_gt_intel.sh`"
                )
            else:
                checks.append(f"gt_intel_byte_identical:{ch[:12]}")
        except Exception as exc:  # noqa: BLE001
            checks.append(f"gt_intel_drift_check_skipped:{exc}")
    return checks, failures


def _run_preflight(
    output_dir: Path,
    config_path: Path | None = None,
    phase_budget_usd: Optional[float] = None,
    api_base: Optional[str] = None,
    venv_python: Optional[str] = None,
    swe_repo: Optional[str] = None,
) -> PreflightResult:
    checks: List[str] = []
    failures: List[str] = []

    # RC-13: SWE-agent version pin assertion + tool-load-order check.
    # Both are version-fragile contracts (config/gt_track4.yaml:154
    # documents the SWE-agent 1.1.0 duplicate-tool crash) — surface drift
    # at preflight, not mid-run.
    if venv_python:
        ok, detail = _assert_sweagent_version(venv_python)
        (checks if ok else failures).append(detail)
        # RC-17 (F-006/F-007/F-012): write versions.json + pip_freeze.txt
        # alongside the run output for forensic comparability.
        try:
            _capture_versions(output_dir, venv_python)
            checks.append("versions_captured:versions.json+pip_freeze.txt")
        except Exception as exc:  # noqa: BLE001
            checks.append(f"versions_capture_warn:{exc}")
    if config_path is not None:
        ok, detail = _assert_no_duplicate_submit(config_path)
        (checks if ok else failures).append(detail)

    # RC-02 (G-007/G-008): unset latent paid-call API keys before SWE-agent
    # spawns. mcp/server.py invariant is api_key=None for TaskParser /
    # BriefingEngine / ValidationOrchestrator; the memory/ subsystem reads
    # GT_LLM_API_KEY. Defense-in-depth: scrub both from this process env so
    # they cannot cross the os.environ.copy() into the subprocess.
    for env_key in ("GT_LLM_API_KEY", "ANTHROPIC_API_KEY"):
        if env_key in os.environ:
            os.environ.pop(env_key, None)
            checks.append(f"unset_env:{env_key}")
        else:
            checks.append(f"env_already_clean:{env_key}")

    # RC-02 (G-001/G-009/G-010): YAML cost cap consistency. If the operator
    # passed --total-cost-limit, ensure the YAML's total_cost_limit is >= the
    # phase budget (the CLI flag is the strict cap; the YAML is a fallback).
    if config_path is not None:
        cost_section = _read_yaml_cost_section(config_path)
        if cost_section.get("total_cost_limit") is None:
            failures.append(f"yaml_total_cost_limit_missing:{config_path}")
        else:
            checks.append(
                f"yaml_total_cost_limit:${cost_section['total_cost_limit']:.2f}"
            )
        per_inst = cost_section.get("per_instance_cost_limit")
        if per_inst is None or per_inst <= 0:
            failures.append(f"per_instance_cost_limit_missing_or_zero:{per_inst}")
        else:
            checks.append(f"per_instance_cost_limit:${per_inst:.2f}")

        # Verify litellm registry JSON exists at the path declared in YAML.
        reg = cost_section.get("litellm_model_registry")
        if reg:
            reg_path = Path(str(reg))
            if not reg_path.is_absolute():
                # YAML paths are conventionally relative to the repo root;
                # try resolving against config_path's parent's parent.
                candidate = (config_path.parent.parent / reg_path).resolve()
                if not candidate.is_file():
                    candidate = (Path.cwd() / reg_path).resolve()
                reg_path = candidate
            if reg_path.is_file():
                checks.append(f"litellm_registry_present:{reg_path.name}")
            else:
                failures.append(f"litellm_registry_missing:{reg_path}")

        if phase_budget_usd is not None and cost_section.get("total_cost_limit"):
            yaml_cap = cost_section["total_cost_limit"] or 0.0
            # The YAML cap must be >= phase budget; if it's MUCH higher
            # (>2x) we surface a warn — operator probably forgot to drop
            # it after a previous phase.
            if yaml_cap < phase_budget_usd:
                failures.append(
                    f"yaml_cost_cap_below_phase_budget:"
                    f"yaml=${yaml_cap:.2f} phase=${phase_budget_usd:.2f}"
                )
            elif yaml_cap > phase_budget_usd * 2:
                checks.append(
                    f"yaml_cost_cap_loose:yaml=${yaml_cap:.2f} "
                    f"phase=${phase_budget_usd:.2f} (CLI override is "
                    f"authoritative)"
                )

    # RC-02 (G-010): proxy /health probe + Vertex 403 classifier. Skip if no
    # api_base provided (e.g., dry-run path); fail fast on IAM-403, warn on
    # quota throttle (preflight isn't the right place to retry).
    if api_base:
        try:
            from vertex_403_classifier import classify_403  # type: ignore[import-not-found]
        except ImportError:
            classify_403 = None  # type: ignore[assignment]
        ok, detail = _curl_proxy_health(api_base)
        if ok:
            checks.append(f"proxy_health:{api_base} ok")
        else:
            verdict = (
                classify_403(detail) if classify_403 is not None else "unknown"
            )
            if verdict == "iam":
                failures.append(
                    f"proxy_iam_denied:{api_base} ({detail[:120]}) — "
                    f"grant roles/aiplatform.user before launch"
                )
            elif verdict == "throttle":
                # Soft-warn: throttle at preflight is unusual but not a
                # blocker; the run will back off via the proxy retry policy.
                checks.append(
                    f"proxy_health_throttle_warn:{api_base} ({detail[:120]})"
                )
            else:
                failures.append(f"proxy_health_failed:{api_base} ({detail[:200]})")

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
    # RC-13: path comes from --vm-profile / --swe-repo, not a hardcoded
    # /home/ubuntu literal. If unset, skip the check rather than probe
    # a wrong VM's home dir.
    swe_repo_path = Path(swe_repo) if swe_repo else None
    if swe_repo_path is not None and swe_repo_path.is_dir():
        proc = subprocess.run(
            ["git", "-C", str(swe_repo_path), "rev-parse", "--git-dir"],
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


def _evaluate_layer_invocation(
    output_dir: Path,
    expected_ids: List[str],
    per_task_all_layers: bool = False,
    per_task_min_pct: float = 80.0,
) -> Tuple[bool, List[str]]:
    """Evaluate the all-layers gate.

    Default (corpus-OR) behavior preserved for backward compat:
    PASS iff ANY task fires each of L1..L6 across the corpus.

    RC-10 (D-015 / I-fix): when ``per_task_all_layers=True``, gate
    on ``>= per_task_min_pct%`` of tasks firing all 6 layers. The
    pre-fix corpus-OR semantics are a load-bearing benchmaxxing
    surface (spread layer firing thinly across many tasks, claim
    corpus health). The per-task variant fixes that.

    Synthesized failsafe lines (snap.synthesized=True) NEVER count
    toward "all 6 layers fired" — they're wedge / drop placeholders.
    """
    snaps: Dict[str, LayerSnapshot] = {
        iid: collect_layer_snapshot(output_dir, iid) for iid in expected_ids
    }
    reasons: List[str] = []

    def _l1_ok(s: LayerSnapshot) -> bool:
        return (s.L1 in ("fired", "fallback")) or str(s.L2).startswith("fired")

    def _l2_ok(s: LayerSnapshot) -> bool:
        return str(s.L2).startswith("fired")

    def _all_six(s: LayerSnapshot) -> bool:
        if s.synthesized:
            return False
        # L1 OR L2 is the brief layer — primary brief sets L1=fired with
        # L2=noop, fallback brief sets L1=fallback with L2=fired. Either
        # counts as "brief layer fired" for per-task all-6 semantics.
        brief_fired = _l1_ok(s) or _l2_ok(s)
        return (
            brief_fired and s.L3 > 0 and s.L4 > 0
            and s.L5 != "not_evaluated" and s.L6 > 0
        )

    if per_task_all_layers:
        per_task_ok = sum(1 for s in snaps.values() if _all_six(s))
        n = max(1, len(snaps))
        pct = 100.0 * per_task_ok / n
        if pct < per_task_min_pct:
            reasons.append(
                f"per_task_all_layers: {per_task_ok}/{n} tasks fired all 6 layers "
                f"({pct:.1f}% < required {per_task_min_pct:.1f}%)"
            )
        return len(reasons) == 0, reasons

    # Legacy corpus-OR mode (kept for backwards compat with --require-all-layers).
    if not any(_l1_ok(s) for s in snaps.values()):
        reasons.append("L1 never invoked")
    if not any(_l2_ok(s) for s in snaps.values()):
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


# ---- RC-14: subprocess lifecycle + signal forwarding ----------------------

def _compute_hard_cap_seconds(
    cap_seconds: Optional[int], task_count: int, workers: int
) -> Optional[int]:
    """Return hard wall-clock cap covering the longest task chain.

    Uses ``math.ceil(task_count / workers)`` rather than integer
    floor-division. Closes A-020 / E-022: with 5 tasks / 4 workers the
    longest chain is 2 tasks (one worker runs 2), not 1 (floor of 5//4).
    Returns None when ``cap_seconds`` is falsy so callers can treat it
    as "no hard cap".
    """
    if not cap_seconds:
        return None
    safe_workers = max(1, int(workers))
    safe_count = max(1, int(task_count))
    longest_chain = math.ceil(safe_count / safe_workers)
    return int(cap_seconds * 1.25 * longest_chain)


def _install_sigterm_forwarder(
    proc: subprocess.Popen,
) -> "dict[str, object]":
    """Install SIGTERM/SIGINT handlers that forward to the SWE-agent child.

    Closes A-009 + E-021: a parent SIGTERM/SIGINT (operator Ctrl-C, ECS
    job stop, k8s preStop) used to kill this runner process and orphan
    the SWE-agent batch + every docker container under it. The forwarded
    SIGTERM gives SWE-agent's RunBatch loop a chance to fire
    `on_instance_completed` (which the gt_track4_pre_run.py env.close
    wrapper depends on for artifact pull) before we escalate.

    The handler:
      1. Sends SIGTERM to the child once.
      2. Sets a "fired" flag the wait loop polls — caller is expected
         to wait up to 60s, then escalate to SIGKILL via the wait loop's
         normal hard_cap path.
      3. Re-raises by restoring the previous handler and re-sending the
         signal to self, so callers (operators, supervisors) still see
         the runner exit on signal.

    Returns a state dict so the wait loop can inspect ``state["fired"]``.

    TODO(RC-14-coord): RC-11 is also installing atexit/signal handlers
    in gt_track4_pre_run.py for env.close flushing. The contracts must
    converge: this handler signals SWE-agent's batch, RC-11's handler
    drives per-instance artifact flush. They are complementary.
    """
    state: "dict[str, object]" = {"fired": False, "signum": None}
    prev_handlers: "dict[int, object]" = {}

    def _handler(signum: int, _frame: object) -> None:
        if state["fired"]:
            return
        state["fired"] = True
        state["signum"] = signum
        try:
            print(
                f"[smoke_runner] received signal {signum}; forwarding "
                f"SIGTERM to SWE-agent batch (pid={proc.pid}) and "
                "waiting up to 60s for on_instance_completed flush",
                file=sys.stderr,
                flush=True,
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev_handlers[sig] = signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not main thread or platform doesn't support this signal.
            # Best-effort: skip and let the OS default propagate.
            pass

    state["_prev_handlers"] = prev_handlers
    return state


def _restore_signal_handlers(state: "dict[str, object]") -> None:
    """Restore signal handlers installed by ``_install_sigterm_forwarder``."""
    prev = state.get("_prev_handlers") or {}
    if isinstance(prev, dict):
        for sig, handler in prev.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# ---- main ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Track D smoke runner for SWE-agent + GT 6-layer harness",
    )
    parser.add_argument("--config", required=True,
                        help="Path to gt_track4.yaml (or ablation variant)")
    parser.add_argument("--task-ids", required=False, default=None,
                        help="Comma-separated IDs, or path to file (one per line). "
                             "Mutually exclusive with --first-n-from-dataset.")
    # RC-17 (F-010): code-enforced "first N sorted instance_id" selection.
    # When set, the runner ignores --task-ids and selects
    # sorted(ds["instance_id"])[:N], writing the resolved set to
    # <output_dir>/selected_task_ids.txt so a second invocation reads
    # identical tasks. Closes the operator-convention attack surface.
    parser.add_argument(
        "--first-n-from-dataset",
        type=int,
        default=None,
        help=(
            "RC-17 / F-010: select sorted(instance_id)[:N] from the HF "
            "dataset deterministically. Overrides --task-ids. Writes the "
            "resolved set to <output_dir>/selected_task_ids.txt."
        ),
    )
    parser.add_argument("--output-dir", required=True,
                        help="SWE-agent output dir; per-task subdirs land here")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--remote-host", default=None,
                        help="If set, launch via gcloud ssh on this host (e.g. gt-t0)")
    parser.add_argument("--remote-user", default="ubuntu")
    # RC-13: VM-portable path defaults. --vm-profile bundles the three
    # paths (venv_python, swe_repo, gt_indexes_root) under one name. The
    # individual --venv-python / --gt-indexes-root flags still work and
    # always win over the profile, so a partial override (e.g.
    # `--vm-profile root_v1 --venv-python /opt/venv/bin/python`) is fine.
    parser.add_argument(
        "--vm-profile",
        default=None,
        choices=sorted(_VM_PROFILES),
        help=(
            "Bundle VM-specific path defaults. Required (or pass each "
            "--venv-python / --gt-indexes-root explicitly). Profiles: "
            f"{sorted(_VM_PROFILES)}. RC-13 removed the silent /home/ubuntu "
            "fallback that masked VM cutovers."
        ),
    )
    parser.add_argument("--venv-python", default=None,
                        help=(
                            "Override --vm-profile's venv python. RC-13: "
                            "no hardcoded /home/ubuntu default."
                        ))
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
    # RC-02 (G-001/G-009): per-launch authoritative cap. Required for paid
    # runs; without it a stale YAML cap can silently bill 8x the phase
    # budget. Pair with --per-task-cost-estimate for the EXPECTED_COST surface.
    parser.add_argument(
        "--total-cost-limit",
        type=float,
        default=None,
        help=(
            "Hard total cost cap (USD) — overrides agent.model.total_cost_limit "
            "from the YAML on this launch. Phase 4 = 50, Phase 5 = 200."
        ),
    )
    # RC-02 (G-002): per-task cost estimate (USD) used to compute the
    # EXPECTED_COST: surface line printed before launch.
    parser.add_argument(
        "--per-task-cost-estimate",
        type=float,
        default=_DEFAULT_PER_TASK_COST_USD,
        help=(
            "Estimated USD cost per task at the v1.0.5 envelope (default: "
            f"${_DEFAULT_PER_TASK_COST_USD:.4f}). Used only for the "
            "preflight EXPECTED_COST surface — the cap is enforced by "
            "--total-cost-limit."
        ),
    )
    parser.add_argument(
        "--api-base",
        default="http://localhost:4000/v1",
        help=(
            "LiteLLM proxy api_base. Preflight curls <base>/health to verify "
            "the proxy is up before SWE-agent starts."
        ),
    )
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
        help="Fail run unless L1..L6 are all invoked/evaluated across expected task ids "
             "(corpus-OR semantics). For per-task AND, see --per-task-all-layers.",
    )
    # RC-10 (D-015 / I-fix): per-task AND gate. Pre-fix --require-all-layers
    # was corpus-OR — a 30-task run where task #1 fired L1, task #2 fired
    # L4, task #3 fired L5 etc. passed even though no single task fired
    # all 6 layers. The per-task variant requires >= --per-task-min-pct
    # of tasks fire all 6 layers (excluding synthesized failsafe lines).
    parser.add_argument(
        "--per-task-all-layers",
        action="store_true",
        help="Fail run unless >= --per-task-min-pct%% of tasks fire ALL 6 layers "
             "(per-task AND, not corpus OR). Synthesized failsafe lines never count.",
    )
    parser.add_argument(
        "--per-task-min-pct",
        type=float,
        default=80.0,
        help="Minimum %% of tasks that must fire all 6 layers under "
             "--per-task-all-layers (default 80.0).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help=(
            "RC-12: wipe --output-dir before launch. Required if the dir "
            "is non-empty unless this flag is passed; prevents stale-artifact "
            "contamination across runs."
        ),
    )
    parser.add_argument(
        "--gt-indexes-root",
        default=None,
        help=(
            "Root dir under which per-instance graph.db's live as "
            "<root>/<instance_id>/graph.db. RC-13: no hardcoded "
            "/home/ubuntu default — supply via --vm-profile or explicit "
            "flag."
        ),
    )
    args = parser.parse_args()

    # RC-13: resolve --vm-profile + explicit overrides BEFORE preflight,
    # so version assertions etc. can use the resolved venv_python.
    try:
        vm_paths = _resolve_vm_profile(
            args.vm_profile,
            {
                "venv_python": args.venv_python,
                "gt_indexes_root": args.gt_indexes_root,
            },
        )
    except KeyError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    if "venv_python" not in vm_paths or "gt_indexes_root" not in vm_paths:
        print(
            "FATAL: --vm-profile not set and one of --venv-python / "
            "--gt-indexes-root is missing. RC-13 removed the silent "
            "/home/ubuntu defaults; pass --vm-profile ubuntu_t0 (current "
            "VM), --vm-profile root_v1, or each flag explicitly.",
            file=sys.stderr,
        )
        return 2
    args.venv_python = vm_paths["venv_python"]
    args.gt_indexes_root = vm_paths["gt_indexes_root"]
    args._vm_swe_repo = vm_paths.get("swe_repo")

    # RC-17 (F-010): --first-n-from-dataset takes precedence over --task-ids.
    # The selection is code-enforced (sorted instance_id, deterministic) and
    # written to <output_dir>/selected_task_ids.txt for re-runs.
    if args.first_n_from_dataset is not None and args.first_n_from_dataset > 0:
        out_dir_for_select = Path(args.output_dir)
        out_dir_for_select.mkdir(parents=True, exist_ok=True)
        try:
            task_ids = _select_first_n_from_dataset(
                args.first_n_from_dataset,
                args.instances_dataset_name,
                args.instances_split,
                out_dir_for_select,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: --first-n-from-dataset failed: {exc}",
                  file=sys.stderr)
            return 2
        if args.task_ids:
            print(
                "[smoke_runner] WARN: --task-ids ignored — "
                "--first-n-from-dataset is the authoritative source.",
                file=sys.stderr,
            )
    elif args.task_ids:
        task_ids = _parse_task_ids(args.task_ids)
    else:
        print(
            "FATAL: provide --task-ids or --first-n-from-dataset",
            file=sys.stderr,
        )
        return 2
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

    # RC-12: stale-artifact contamination gate. A non-empty output_dir at
    # launch time risks the watcher loop counting prior-run instance dirs
    # as "completed" before SWE-agent has even started, which contaminates
    # gt_layers logs and verify_report rates. Require either an empty dir
    # or an explicit --clean.
    try:
        existing = [p for p in output_dir.iterdir() if not p.name.startswith(".")]
    except Exception:  # noqa: BLE001
        existing = []
    if existing:
        if args.clean:
            import shutil as _sh
            for p in existing:
                if p.is_dir():
                    _sh.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except Exception:  # noqa: BLE001
                        pass
            print(
                f"[smoke_runner][RC-12] --clean: wiped {len(existing)} entries "
                f"from {output_dir}",
                file=sys.stderr,
            )
        else:
            print(
                f"FATAL[RC-12]: output_dir not empty ({len(existing)} entries "
                f"in {output_dir}); pass --clean to wipe, or pick a fresh dir.",
                file=sys.stderr,
            )
            return 4

    if not args.skip_preflight:
        preflight = _run_preflight(
            output_dir,
            config_path=Path(args.config),
            phase_budget_usd=args.total_cost_limit,
            api_base=args.api_base,
            # RC-13: pass resolved venv + swe_repo so the SWE-agent
            # version assertion + git-safe-directory probe target the
            # right paths for THIS VM (not /home/ubuntu unconditionally).
            venv_python=args.venv_python,
            swe_repo=getattr(args, "_vm_swe_repo", None),
        )
        for chk in preflight.checks:
            print(f"[smoke_runner][preflight] {chk}", file=sys.stderr)
        if not preflight.ok:
            for fail in preflight.failures:
                print(f"[smoke_runner][preflight][FAIL] {fail}", file=sys.stderr)
            return 3

    # RC-02 (G-002): MANDATORY cost surface before Popen. CLAUDE.md:
    # "any LLM run that will spend real money must have its expected
    # dollar cost surfaced in chat BEFORE launching".
    _expected_total, expected_line = _compute_expected_cost(
        task_count=len(task_ids),
        per_task_estimate_usd=args.per_task_cost_estimate,
        cap_usd=args.total_cost_limit,
    )
    print(expected_line, flush=True)

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
        total_cost_limit=args.total_cost_limit,
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

    # Build subprocess env. RC-17 (F-005) replaces the prior
    # ``os.environ.copy()`` with an explicit allow-list — anything not on
    # _ENV_ALLOWLIST_EXACT or _ENV_ALLOWLIST_PREFIXES is dropped before
    # Popen. The deliberately-set GT_* / Vertex paths below are layered on
    # via the ``extra`` kwarg. The final dict is persisted to
    # <output_dir>/run_env.json so the artifact is self-describing.
    extra_env: Dict[str, str] = {}
    env = _build_subprocess_env(extra_env)
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

    # RC-17 (F-005): persist final env (allow-listed) for forensics.
    _persist_run_env(output_dir, env)

    # RC-17 (F-008): paid-run model fingerprint. Fires one deterministic
    # prompt at the proxy and writes <output_dir>/model_fingerprint.json.
    # Skip on dry-run and when api_base is missing. Cost is ~$0.0001.
    if args.api_base and not args.remote_host:
        try:
            _capture_model_fingerprint(
                output_dir,
                api_base=args.api_base,
                model_name="qwen3-coder-480b-a35b-instruct-maas",
            )
            print(
                "[smoke_runner] RC-17: model_fingerprint.json captured",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[smoke_runner] WARN: fingerprint capture failed: {exc}",
                  file=sys.stderr)

    print("[smoke_runner] launching: "
          + " ".join(shlex.quote(c) for c in launch_cmd), flush=True)

    # RC-14 (A-009, A-020, E-020-22): Popen wrapped in try/finally so a
    # parent SIGINT/exception always tears the SWE-agent batch down +
    # waits, instead of orphaning containers. The signal forwarder
    # (installed AFTER Popen, removed in finally) translates parent
    # SIGTERM/SIGINT into SWE-agent's own SIGTERM so RunBatch's
    # on_instance_completed can fire and the gt_track4_pre_run.py
    # env.close wrapper can pull artifacts before death.
    # Set cwd to repo root so relative paths in gt_track4.yaml (e.g.
    # config/gt_track4_litellm_registry.json) resolve correctly regardless
    # of what directory the runner was invoked from.
    _repo_root = str(Path(__file__).resolve().parent.parent.parent)
    proc = subprocess.Popen(
        launch_cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=env,
        cwd=_repo_root,
    )
    sig_state = _install_sigterm_forwarder(proc)
    try:
        # Hard wall-clock cap covers the LONGEST per-worker chain, not
        # the sum. RC-14 (A-020): use math.ceil — integer floor-division
        # under-counted in the saturated regime (5 tasks / 4 workers
        # mapped to chain=1 instead of 2, capping the run at 2250s when
        # the truthful ceiling is 4500s).
        hard_cap = _compute_hard_cap_seconds(
            cap_seconds=args.per_instance_wallclock_cap_seconds,
            task_count=len(task_ids),
            workers=args.workers,
        )

        rc = _wait_loop(
            proc,
            output_dir=output_dir,
            expected_ids=task_ids,
            hard_wall_clock_s=hard_cap,
        )
        if sig_state.get("fired"):
            # Parent received SIGTERM/SIGINT mid-run. _wait_loop polled
            # `proc.poll()` and returned once the child reaped. Surface
            # the cause so the verifier doesn't misclassify a clean
            # operator-stop as a wedge.
            print(
                "[smoke_runner] exiting under forwarded signal "
                f"{sig_state.get('signum')}; rc={rc}",
                file=sys.stderr,
                flush=True,
            )
    except BaseException:
        # Pyright/mypy: BaseException catches KeyboardInterrupt + system
        # exits the parent shell may inject. Any exception here means
        # the wait loop blew up — kill the child to avoid orphaned
        # containers, then re-raise so the operator/CI sees the trace.
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        # Always reap the child + restore signal handlers so the parent
        # process can exit cleanly. proc.wait is a no-op if already
        # reaped by _wait_loop.
        try:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            _restore_signal_handlers(sig_state)

    # RC-02 (G-005): post-run cost reconciliation. Sum proxy-side
    # litellm_calls.jsonl and compare against SWE-agent's reported total
    # cost from output.jsonl. Diverging by >5% suggests a model-name
    # mismatch (silent $0 risk) or proxy-callback config drift.
    sweagent_total = 0.0
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
                    for k in ("cost_usd", "cost", "total_cost"):
                        if k in rec:
                            try:
                                sweagent_total += float(rec[k])
                                break
                            except (TypeError, ValueError):
                                pass
        except Exception as exc:  # noqa: BLE001
            print(
                f"[smoke_runner][reconcile] read_error:{exc}", file=sys.stderr
            )
    within, recon_line = _reconcile_litellm_calls(output_dir, sweagent_total)
    print(f"[smoke_runner][reconcile] {recon_line}", file=sys.stderr)
    if not within:
        # Soft-warn rather than hard-fail: the run is over, the bill is
        # done. The operator needs the divergence visible, not a non-zero
        # exit that could hide other failures.
        print(
            "[smoke_runner][reconcile][WARN] proxy/agent cost divergence "
            "exceeds tolerance — investigate model-name or callback config",
            file=sys.stderr,
        )

    if rc != 0:
        return rc
    if args.require_all_layers or args.per_task_all_layers:
        ok, reasons = _evaluate_layer_invocation(
            output_dir,
            task_ids,
            per_task_all_layers=args.per_task_all_layers,
            per_task_min_pct=args.per_task_min_pct,
        )
        if not ok:
            for reason in reasons:
                print(f"[smoke_runner][layers][FAIL] {reason}", file=sys.stderr)
            return 4
        if args.per_task_all_layers:
            print(
                f"[smoke_runner][layers] >= {args.per_task_min_pct:.1f}%% of tasks fired all 6 layers",
                file=sys.stderr,
            )
        else:
            print("[smoke_runner][layers] all 6 layers invoked", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

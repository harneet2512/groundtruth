#!/usr/bin/env python3
"""Run mini-SWE-agent with GT Hybrid v1 — 4-checkpoint deterministic control plane.

Checkpoints:
  1. Exploration: auto-inject gt_orient output (compact JSON)
  2. Pre-edit:    auto-run gt_impact on risky symbol edits (verified+likely only)
  3. Post-edit:   mandatory gt_check on all edited files (hard/soft blockers)
  4. Ranking:     PatchScorer on host side (observe-only in v1)

Two modes:
  --mode tools-only : Inject GT tools + orient. No hook enforcement.
  --mode hybrid     : All 4 checkpoints active.

Usage:
    python benchmarks/swebench/run_mini_gt_hybrid_v1.py swebench \
        -c benchmarks/swebench/swebench_deepseek_v3_hybrid_v1.yaml \
        --subset lite --split test \
        --instance-ids "astropy__astropy-12907,astropy__astropy-13033" \
        -w 2 --output-dir results/hybrid_smoke/hybrid_v1 \
        --mode hybrid
"""
from __future__ import annotations

import json
import os
import re
import subprocess as sp
import sys
import tempfile
import traceback
from pathlib import Path

from minisweagent.run.benchmarks.swebench import (
    app,
    get_sb_environment,
    get_model,
    ProgressTrackingAgent,
    update_preds_file,
    remove_from_preds_file,
    logger,
)
from minisweagent.run.benchmarks import swebench as swebench_module
from minisweagent.environments.docker import DockerEnvironment

# ── File paths ──────────────────────────────────────────────────────────

def _find_gt_index() -> Path:
    """Find gt-index-static binary: env var > /tmp > repo."""
    env_path = os.environ.get("GT_INDEX_PATH", "")
    candidates = [
        Path(env_path) if env_path else None,
        Path("/tmp/gt-index-static"),
        Path(__file__).parent.parent.parent / "gt-index" / "gt-index-static",
    ]
    for p in candidates:
        if p is not None and p.is_file():
            return p
    return candidates[-1]  # fallback to repo path

GT_INDEX_BINARY = _find_gt_index()
GT_INTEL_SCRIPT = Path(__file__).parent / "gt_intel.py"
GT_SHELL_TOOLS = Path(__file__).parent.parent.parent / "scripts" / "swebench" / "gt_shell_tools.py"

# ── Mode flag ───────────────────────────────────────────────────────────

_HYBRID_MODE: str = "hybrid"  # "tools-only" or "hybrid"

# ── Edit indicators (reused from run_mini_gt_hooked.py) ─────────────────

_EDIT_INDICATORS = (
    "sed -i",
    "sed ", "cat >", "cat <<",
    "echo >", "echo >>",
    "tee ",
    "patch ",
    "git apply",
    "str_replace_editor",
)

_SOURCE_EXTS = r'\.(?:py|js|ts|jsx|tsx|go|rs|java|rb|php|c|cpp|h|hpp|cs|swift|kt)'

# ── Per-container state ─────────────────────────────────────────────────

_container_roots: dict[str, str] = {}
_edit_counts: dict[str, dict[str, int]] = {}
_shown_files: dict[str, dict[str, int]] = {}

# ── Limited-call budget (hybrid-limited mode) ──────────────────────────

_GT_BUDGET = {"orient": 1, "lookup": 2, "impact": 2, "check": 3}
_gt_budgets: dict[str, dict[str, int]] = {}  # container_id → remaining budget per tool
_diff_hashes: dict[str, str] = {}  # container_id → SHA-256 of last git diff
_hash_skip_counts: dict[str, int] = {}  # container_id → times gt_check skipped due to same hash


# ── Helpers ─────────────────────────────────────────────────────────────

def _detect_repo_root(env) -> str:
    """Detect repo root: /app for Pro, /testbed for Lite."""
    try:
        result = sp.run(
            ["docker", "exec", env.container_id, "test", "-d", "/app/.git"],
            capture_output=True, timeout=3,
        )
        if result.returncode == 0:
            return "/app"
    except Exception:
        pass
    return "/testbed"


def _exec(env, cmd: str, timeout: int = 60):
    return env.execute({"command": cmd}, timeout=timeout)


def _is_repo_source(filepath: str) -> bool:
    """Filter out test scripts, temp files, and repro scripts."""
    base = os.path.basename(filepath)
    if base.startswith("test_") or base.startswith("reproduce") or base.startswith("tmp"):
        return False
    if "/test_" in filepath or "reproduce" in filepath:
        return False
    return True


def _detect_modified_file(command: str) -> str | None:
    """Detect which source file a command modifies."""
    ext = _SOURCE_EXTS
    all_source_files = re.findall(rf'(\.?/?[\w/.-]+{ext})\b', command)
    repo_files = [f for f in all_source_files
                  if _is_repo_source(f) and len(f) > 5]
    if not repo_files:
        return None
    return repo_files[-1]


def _detect_edited_symbol(command: str) -> str | None:
    """Try to extract function/method name being edited from command text."""
    # Look for def/class patterns in sed replacement text or cat heredocs
    m = re.search(r'def\s+(\w+)\s*\(', command)
    if m:
        return m.group(1)
    m = re.search(r'class\s+(\w+)\s*[:\(]', command)
    if m:
        return m.group(1)
    return None


# ── Injection ───────────────────────────────────────────────────────────

def _inject_hybrid(env, instance_id: str) -> bool:
    """Inject Go indexer binary + gt_intel.py + gt_shell_tools.py, build graph.db."""
    root = _detect_repo_root(env)
    _container_roots[env.container_id] = root
    container_id = env.container_id

    try:
        _exec(env, "echo gt_ready", timeout=5)  # verify container alive

        # Copy files into container
        # Use /tmp/gt-index-bin to avoid conflict with gt-index/ repo directory
        sp.run(["docker", "cp", str(GT_INDEX_BINARY), f"{container_id}:/tmp/gt-index-bin"],
               timeout=15, check=True, capture_output=True)
        sp.run(["docker", "cp", str(GT_INTEL_SCRIPT), f"{container_id}:/tmp/gt_intel.py"],
               timeout=10, check=True, capture_output=True)
        sp.run(["docker", "cp", str(GT_SHELL_TOOLS), f"{container_id}:/tmp/gt_tools.py"],
               timeout=10, check=True, capture_output=True)
        _exec(env, "chmod +x /tmp/gt-index-bin", timeout=5)

        # Build graph index
        max_files = os.environ.get("GT_MAX_FILES", "5000")
        result = _exec(env,
                       f"/tmp/gt-index-bin --root={root} --output=/tmp/gt_graph.db --max-files={max_files} 2>&1",
                       timeout=30)
        output = result.get("output", "") if isinstance(result, dict) else ""
        last_line = output.strip().split("\n")[-1][:100] if output else "no output"
        logger.info("hybrid inject: %s | %s", instance_id, last_line)

        # Set env vars for gt_shell_tools.py
        _exec(env, f"echo 'export GT_DB=/tmp/gt_graph.db' >> /root/.bashrc && "
              f"echo 'export GT_ROOT={root}' >> /root/.bashrc", timeout=5)

        return True

    except Exception as e:
        logger.warning("GT injection failed for %s: %s", instance_id, e)
        return False


# ── Checkpoint 1: Exploration (auto-orient) ─────────────────────────────

def _checkpoint_1_orient(env) -> str:
    """Run gt_orient and return compact JSON for prepending to task."""
    try:
        result = _original_execute(
            env,
            {"command": "GT_DB=/tmp/gt_graph.db GT_ROOT=/testbed python3 /tmp/gt_tools.py orient 2>/dev/null"},
            timeout=10,
        )
        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if output and len(output) > 20 and output.startswith("{"):
            return f"<gt-orientation>\n{output}\n</gt-orientation>"
    except Exception as e:
        logger.debug("orient failed: %s", e)
    return ""


# ── Checkpoint 2: Pre-Edit (gt_impact on risky symbols) ────────────────

def _checkpoint_2_pre_edit(env, command: str, modified_file: str) -> str:
    """Run gt_impact if the edit touches a risky symbol."""
    symbol = _detect_edited_symbol(command)
    if not symbol:
        return ""

    # Budget enforcement (hybrid-limited mode)
    if _HYBRID_MODE == "hybrid-limited":
        budget = _gt_budgets.setdefault(env.container_id, dict(_GT_BUDGET))
        if budget.get("impact", 0) <= 0:
            return ""  # budget exhausted

    root = _container_roots.get(env.container_id, "/testbed")
    try:
        result = _original_execute(
            env,
            {"command": f"GT_DB=/tmp/gt_graph.db GT_ROOT={root} python3 /tmp/gt_tools.py impact {symbol} 2>/dev/null"},
            timeout=10,
        )
        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if not output or not output.startswith("{"):
            return ""

        data = json.loads(output)
        caller_count = data.get("caller_count", 0)
        risk = data.get("risk", "LOW")

        # Filter: skip low-risk or single-caller symbols
        if risk == "LOW" or caller_count < 2:
            return ""

        # Decrement budget
        if _HYBRID_MODE == "hybrid-limited":
            budget = _gt_budgets.setdefault(env.container_id, dict(_GT_BUDGET))
            budget["impact"] = budget.get("impact", 0) - 1

        return f"\n\n<gt-pre-edit-check>\n{output}\n</gt-pre-edit-check>"

    except Exception as e:
        logger.debug("pre-edit check failed for %s: %s", symbol, e)
    return ""


# ── Checkpoint 3: Post-Edit (mandatory gt_check) ───────────────────────

def _run_incremental_reindex(env, filepath: str) -> None:
    """Incrementally re-index a changed file."""
    root = _container_roots.get(env.container_id, "/testbed")
    rel_path = filepath.lstrip("./")
    if filepath.startswith(root):
        rel_path = filepath[len(root):].lstrip("/")
    try:
        _original_execute(
            env,
            {"command": f"/tmp/gt-index-bin --incremental --files={rel_path} --root={root} --output=/tmp/gt_graph.db 2>&1"},
            timeout=5,
        )
    except Exception:
        pass


def _checkpoint_3_post_edit(env, modified_file: str) -> str:
    """Run gt_check on the modified file. Return blockers/warnings/clean."""
    container_id = env.container_id
    root = _container_roots.get(container_id, "/testbed")

    # Budget enforcement (hybrid-limited mode)
    if _HYBRID_MODE == "hybrid-limited":
        budget = _gt_budgets.setdefault(container_id, dict(_GT_BUDGET))
        if budget.get("check", 0) <= 0:
            return ""  # budget exhausted

        # Diff-hash check: skip if patch hasn't materially changed
        try:
            hash_result = _original_execute(
                env,
                {"command": f"cd {root} && git diff 2>/dev/null | sha256sum | cut -c1-16"},
                timeout=5,
            )
            new_hash = (hash_result.get("output", "") if isinstance(hash_result, dict) else "").strip()
            if new_hash and new_hash == _diff_hashes.get(container_id):
                _hash_skip_counts[container_id] = _hash_skip_counts.get(container_id, 0) + 1
                return ""  # same patch, skip
            if new_hash:
                _diff_hashes[container_id] = new_hash
        except Exception:
            pass  # on error, proceed with check anyway

    rel_path = modified_file.lstrip("./")
    if modified_file.startswith(root):
        rel_path = modified_file[len(root):].lstrip("/")

    try:
        result = _original_execute(
            env,
            {"command": f"GT_DB=/tmp/gt_graph.db GT_ROOT={root} python3 /tmp/gt_tools.py check {rel_path} 2>/dev/null"},
            timeout=10,
        )
        output = result.get("output", "").strip() if isinstance(result, dict) else ""
        if not output or not output.startswith("{"):
            return ""

        data = json.loads(output)
        status = data.get("status", "")

        # Decrement budget on successful check
        if _HYBRID_MODE == "hybrid-limited":
            budget = _gt_budgets.setdefault(container_id, dict(_GT_BUDGET))
            budget["check"] = budget.get("check", 0) - 1

        if status == "blockers":
            parts = []
            for issue in data.get("issues", []):
                severity = issue.get("severity", "medium")
                detail = f"{issue.get('type', '?')}: {issue.get('symbol', '?')} — {issue.get('detail', '')}"
                if severity == "high":
                    parts.append(f"\n\n<gt-hard-blocker>{detail}</gt-hard-blocker>")
                else:
                    parts.append(f"\n\n<gt-soft-warning>{detail}</gt-soft-warning>")
            return "".join(parts)

        elif status == "clean":
            count = data.get("symbols_checked", "?")
            return f"\n<gt-check>clean: {count} symbols verified</gt-check>"

    except Exception as e:
        logger.debug("post-edit check failed for %s: %s", modified_file, e)
    return ""


# ── Checkpoint 4: Candidate Ranking (PatchScorer on host) ──────────────

def _checkpoint_4_rank(env, instance_id: str, patch_diff: str) -> dict:
    """Score the candidate patch using GT PatchScorer. Returns info dict."""
    info: dict = {}
    if not patch_diff or not env:
        return info

    container_id = getattr(env, "container_id", None)
    if not container_id:
        return info

    tmp_db = None
    try:
        # Copy graph.db from container to host
        _, tmp_db = tempfile.mkstemp(suffix=".db")
        sp.run(["docker", "cp", f"{container_id}:/tmp/gt_graph.db", tmp_db],
               timeout=15, check=True, capture_output=True)

        # Import GT verification modules (available on host PYTHONPATH)
        from groundtruth.index.graph_store import GraphStore
        from groundtruth.substrate.graph_reader_impl import GraphStoreReader
        from groundtruth.verification.patch_scorer import PatchScorer
        from groundtruth.substrate.types import ContractRecord

        store = GraphStore(tmp_db)
        store.initialize()
        reader = GraphStoreReader(store)
        scorer = PatchScorer(reader)

        # Extract changed files from diff
        changed_files = re.findall(r'^diff --git a/(.+?) b/', patch_diff, re.MULTILINE)

        # Extract changed symbols from diff (function defs in hunks)
        changed_symbols = re.findall(r'^\+\s*def\s+(\w+)', patch_diff, re.MULTILINE)
        changed_symbols += re.findall(r'^-\s*def\s+(\w+)', patch_diff, re.MULTILINE)
        changed_symbols = list(set(changed_symbols))

        # Mine contracts for changed symbols
        from groundtruth.verification.models import PatchCandidate
        candidate = PatchCandidate(
            task_ref=instance_id,
            candidate_id="primary",
            diff=patch_diff,
            changed_files=tuple(changed_files),
            changed_symbols=tuple(changed_symbols),
        )

        # Extract contracts from graph for each changed symbol
        contracts: list[ContractRecord] = []
        for sym in changed_symbols:
            node = reader.get_node_by_name(sym)
            if node:
                try:
                    from groundtruth.substrate.service import SubstrateService
                    svc = SubstrateService(reader)
                    contracts.extend(svc.extract_contracts(node["id"]))
                except Exception:
                    pass

        verdict = scorer.score(candidate, contracts)
        info["gt_score"] = verdict.overall_score
        info["gt_decision"] = verdict.decision
        info["gt_reason_codes"] = list(verdict.reason_codes)
        info["gt_violations"] = [
            {"type": v.contract_type, "severity": v.severity, "explanation": v.explanation}
            for v in verdict.violations
        ]
        logger.info("ranking %s: score=%.2f decision=%s reasons=%s",
                     instance_id, verdict.overall_score, verdict.decision,
                     verdict.reason_codes)

    except ImportError as e:
        logger.warning("Ranking unavailable (missing module): %s", e)
        info["gt_ranking_error"] = f"import: {e}"
    except Exception as e:
        logger.warning("Ranking failed for %s: %s", instance_id, e)
        info["gt_ranking_error"] = str(e)[:200]
    finally:
        if tmp_db and os.path.exists(tmp_db):
            os.unlink(tmp_db)

    return info


# ── Monkey-patch DockerEnvironment.execute ──────────────────────────────

_original_execute = DockerEnvironment.execute


def _hybrid_hooked_execute(self, action, cwd="", *, timeout=None):
    """Execute command, then run GT checkpoints if in hybrid mode."""
    result = _original_execute(self, action, cwd=cwd, timeout=timeout)

    # Skip if not a hybrid mode
    if _HYBRID_MODE not in ("hybrid", "hybrid-limited"):
        return result

    command = action.get("command", "") if isinstance(action, dict) else ""
    if not isinstance(command, str) or not getattr(self, "container_id", None):
        return result

    has_edit = any(ind in command for ind in _EDIT_INDICATORS)
    if not has_edit:
        return result

    root = _container_roots.get(self.container_id, "/testbed")

    try:
        # Detect which file was edited
        check = _original_execute(
            self,
            {"command": f"cd {root} && git diff --name-only 2>/dev/null | head -5"},
            cwd=root, timeout=5,
        )
        diff_output = check.get("output", "") if isinstance(check, dict) else ""
        if not diff_output.strip():
            return result

        for line in diff_output.strip().split("\n"):
            fpath = line.strip()
            if not fpath:
                continue
            ext = os.path.splitext(fpath)[1]
            source_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
                           ".rb", ".php", ".c", ".cpp", ".h", ".cs"}
            if ext not in source_exts:
                continue
            if not _is_repo_source(fpath):
                continue

            # Track edits for dedup
            container_id = self.container_id
            counts = _edit_counts.setdefault(container_id, {})
            shown = _shown_files.setdefault(container_id, {})
            counts[fpath] = counts.get(fpath, 0) + 1
            if shown.get(fpath, 0) >= counts[fpath]:
                continue  # no new edits since last check
            shown[fpath] = counts[fpath]

            # Checkpoint 2: Pre-edit impact (fires on the command that caused the edit)
            pre_edit_output = _checkpoint_2_pre_edit(self, command, fpath)

            # Checkpoint 3: Post-edit structural check
            _run_incremental_reindex(self, fpath)
            post_edit_output = _checkpoint_3_post_edit(self, fpath)

            combined = (pre_edit_output or "") + (post_edit_output or "")
            if combined:
                result["output"] = result.get("output", "") + combined
            break  # one file per command

    except Exception as e:
        logger.debug("hybrid hook error: %s", e)

    return result


DockerEnvironment.execute = _hybrid_hooked_execute


# ── Process instance ────────────────────────────────────────────────────

def hybrid_process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager,
) -> None:
    """Process instance with GT Hybrid v1 checkpoints."""
    instance_id = instance["instance_id"]
    if "docker_image" not in instance and "dockerhub_tag" in instance:
        instance["docker_image"] = f"jefzda/sweap-images:{instance['dockerhub_tag']}"

    instance_dir = output_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)

    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting environment")

    agent = None
    env = None
    exit_status = None
    result = None
    extra_info: dict = {"gt_mode": _HYBRID_MODE}

    try:
        env = get_sb_environment(config, instance)

        # Inject GT tools + build graph.db
        progress_manager.update_instance_status(instance_id, "GT: injecting tools + building index")
        hook_ok = _inject_hybrid(env, instance_id)
        extra_info["hook_injected"] = hook_ok

        # Checkpoint 1: Auto-orient
        if hook_ok:
            orient_output = _checkpoint_1_orient(env)
            if orient_output:
                task = orient_output + "\n\n" + task
                extra_info["orient_shown"] = True
                extra_info["orient_chars"] = len(orient_output)
            else:
                extra_info["orient_shown"] = False

        # Run agent — checkpoints 2+3 fire automatically via execute hook
        progress_manager.update_instance_status(instance_id, "Step   1")
        agent = ProgressTrackingAgent(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **config.get("agent", {}),
        )
        info = agent.run(task)
        exit_status = info.get("exit_status")
        result = info.get("submission")

        # Checkpoint 4: Candidate ranking (observe-only)
        if result and env and _HYBRID_MODE in ("hybrid", "hybrid-limited"):
            ranking_info = _checkpoint_4_rank(env, instance_id, result)
            extra_info.update(ranking_info)

        # Log budget telemetry for limited mode
        if _HYBRID_MODE == "hybrid-limited" and env:
            cid = getattr(env, "container_id", "")
            budget = _gt_budgets.get(cid, {})
            extra_info["gt_budget_usage"] = {
                tool: _GT_BUDGET[tool] - budget.get(tool, _GT_BUDGET[tool])
                for tool in _GT_BUDGET
            }
            extra_info["gt_check_skipped_same_hash"] = _hash_skip_counts.get(cid, 0)

    except Exception as e:
        logger.error("Error processing %s: %s", instance_id, e, exc_info=True)
        exit_status, result = type(e).__name__, ""
        extra_info["traceback"] = traceback.format_exc()
    finally:
        # Extract GT logs
        if env is not None:
            try:
                log_dir = output_dir / "gt_logs"
                log_dir.mkdir(exist_ok=True)
                ev_result = _exec(env, "cat /tmp/gt_evidence.jsonl 2>/dev/null || echo ''", timeout=10)
                ev_content = ev_result.get("output", "") if isinstance(ev_result, dict) else ""
                if ev_content.strip():
                    (log_dir / f"{instance_id}.evidence.jsonl").write_text(ev_content)
            except Exception:
                pass

            # Clean up per-container state
            cid = getattr(env, "container_id", "")
            _edit_counts.pop(cid, None)
            _shown_files.pop(cid, None)
            _container_roots.pop(cid, None)
            _gt_budgets.pop(cid, None)
            _diff_hashes.pop(cid, None)
            _hash_skip_counts.pop(cid, None)

        if agent is not None:
            traj_path = instance_dir / f"{instance_id}.traj.json"
            agent.save(
                traj_path,
                {
                    "info": {
                        "exit_status": exit_status,
                        "submission": result,
                        "gt_version": "hybrid_v1",
                        "gt_delivery": "4_checkpoint_control_plane",
                        **extra_info,
                    },
                    "instance_id": instance_id,
                },
            )

        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result or "")
        progress_manager.on_instance_end(instance_id, exit_status)


swebench_module.process_instance = hybrid_process_instance


# ── CLI ─────────────────────────────────────────────────────────────────

def _parse_mode():
    """Extract --mode flag before passing remaining args to minisweagent."""
    global _HYBRID_MODE
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 < len(sys.argv):
            _HYBRID_MODE = sys.argv[idx + 1]
            sys.argv.pop(idx)  # remove --mode
            sys.argv.pop(idx)  # remove value
    logger.info("GT Hybrid v1 mode: %s", _HYBRID_MODE)


if __name__ == "__main__":
    _parse_mode()
    app()

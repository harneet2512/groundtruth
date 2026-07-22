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

import hashlib
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


_BRIEF_LEDGER_WRITE_FAILURES = 0
_COMPOUND_LINEAGE_SCHEMA = "gt.compound_feature_lineage.v1"
_REGISTERED_BRIEF_BLOCKS = {
    # (canonical FACT class, shipped evidence type, runtime producer)
    "localization-header": (
        "localization", "brief_localization", "v1r_brief"),
    "obligations": ("obligations", "obligations", "spec"),
}


def _bc(msg: str) -> None:
    """Flushed breadcrumb to stderr — survives a SIGKILL/hang (unbuffered), so the trial log shows
    exactly how far the runner got when it produces no trajectory."""
    sys.stderr.write(f"[GT-RUNNER] {msg}\n")
    sys.stderr.flush()


def _brief_delivery_extra(e: Mapping[str, str], brief_text: str) -> dict:
    """Validate the producer sidecar and type each delivered brief block.

    A task-start brief is compound evidence, so it deliberately has no whole-row
    FACT alias. Unknown legacy block labels remain visible but untyped.
    """
    brief_path = (e.get("GT_BRIEF_FILE") or "/gt_artifacts/brief.txt").strip()
    result_path = os.path.join(os.path.dirname(brief_path), "brief_result.json")
    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
        if (
            result.get("schema") != "gt.brief_result.v1"
            or result.get("brief_text") != brief_text
        ):
            return {}
        receipts = (result.get("metrics") or {}).get("block_receipts")
        if not isinstance(receipts, list):
            return {}
        from groundtruth.runtime.feature_lineage import build_lineage, lineage_to_dict

        blocks = []
        seen: set[str] = set()
        last_end = 0
        for receipt in receipts:
            if not isinstance(receipt, dict):
                return {}
            block_id = receipt.get("block_id")
            candidate_id = receipt.get("candidate_id")
            span = receipt.get("char_span")
            full_hash = receipt.get("content_hash")
            declared = receipt.get("fact_class")
            label = receipt.get("label")
            if (
                not isinstance(block_id, str) or not block_id or block_id in seen
                or not isinstance(candidate_id, str) or not candidate_id
                or not isinstance(span, list) or len(span) != 2
                or not all(isinstance(n, int) and not isinstance(n, bool) for n in span)
                or span[0] < 0 or span[0] >= span[1] or span[1] > len(brief_text)
                or not isinstance(full_hash, str) or len(full_hash) != 64
                or not isinstance(declared, str) or not declared
                or not isinstance(label, str) or not label
            ):
                return {}
            if span[0] < last_end:
                return {}
            block = brief_text[span[0]:span[1]]
            digest = hashlib.sha256(block.encode("utf-8", "surrogatepass")).hexdigest()
            if digest != full_hash:
                return {}
            seen.add(block_id)
            last_end = span[1]
            item = {
                "block_id": block_id,
                "label": label,
                "candidate_id": candidate_id,
                "char_span": span,
                "chars_delivered": len(block),
                "content_sha256_16": digest[:16],
                "declared_fact_class": declared,
                "transport_producer_id": "v1r_brief",
                "opportunity_exempt_reason": "task_start_pre_policy",
            }
            registered = (
                ("localization", "brief_localization", "v1r_brief")
                if label.startswith("file-entry-")
                else _REGISTERED_BRIEF_BLOCKS.get(label)
            )
            if registered is None:
                item["lineage_status"] = "UNREGISTERED_BLOCK_LABEL"
            else:
                expected_fact, evidence_type, producer = registered
                if declared != expected_fact:
                    return {}
                lineage = build_lineage(
                    runtime_producer_id=producer,
                    evidence_type=evidence_type, actual_event="task_start")
                if lineage is None or not lineage.producer_registration_match:
                    return {}
                item["lineage_status"] = "REGISTERED"
                item["lineage"] = lineage_to_dict(lineage)
            blocks.append(item)
        return {
            "compound_lineage_schema": _COMPOUND_LINEAGE_SCHEMA,
            "compound_delivery": True,
            "block_lineage": blocks,
        }
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


# The producer-attestation store root — MIRRORS gt_mini_patch._attestation_output_root()
# (the same GT_C_OUT root the native lane / gateway classes persist to), so the grader's
# load_attestations(task_dir) recursive glob finds brief-block bundles by the SAME mechanism.
def _brief_attestation_root(e: Mapping[str, str]) -> str:
    return os.path.join(
        (e.get("GT_C_OUT") or os.environ.get("GT_C_OUT") or "/gt_out"),
        "producer_attestations",
    )


def _persist_brief_localization_attestations(e: Mapping[str, str], brief_text: str) -> None:
    """Seal each delivered, graph-verified localization brief BLOCK with an immutable
    producer attestation joinable to its per-block delivery seal.

    Reads the SAME sealed ``brief_result.json`` the compound lineage row is built from
    (its ``block_receipts`` give the exact block seal; its ``localization_proof`` gives
    the producer's build-time graph verification), builds a
    :func:`groundtruth.runtime.brief_attestation.finalize_localization_attestation`
    bundle per registered localization block, and atomically persists it to the shared
    attestation store.  HOST-SIDE, correct-or-quiet: any miss (absent proof, unverified
    candidate → UNMEASURED, unreadable result, store I/O error) leaves the delivered
    brief byte-identical and the class honestly unmeasured; it never raises.
    """
    brief_path = (e.get("GT_BRIEF_FILE") or "/gt_artifacts/brief.txt").strip()
    result_path = os.path.join(os.path.dirname(brief_path), "brief_result.json")
    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
        if (
            result.get("schema") != "gt.brief_result.v1"
            or result.get("brief_text") != brief_text
        ):
            return
        metrics = result.get("metrics") or {}
        receipts = metrics.get("block_receipts")
        proofs = metrics.get("localization_proof")
        if not isinstance(receipts, list) or not isinstance(proofs, list):
            return
        from groundtruth.runtime.attestation_store import persist_attestation
        from groundtruth.runtime.brief_attestation import (
            finalize_localization_attestation,
        )
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # B-cluster sweep (2026-07-17): the SETUP failures (unreadable result, schema
        # mismatch, import) were the only silent exits left — the per-receipt loop
        # already breadcrumbs. An empty store must always be distinguishable from
        # "attestation setup never ran" (the 0-bundle diagnosis class).
        _bc(f"brief localization attestations skipped setup ({type(exc).__name__})")
        return

    proof_by_candidate: dict[str, dict[str, object]] = {}
    for proof in proofs:
        if not isinstance(proof, dict):
            continue
        cid = proof.get("candidate_id")
        if isinstance(cid, str) and cid and cid not in proof_by_candidate:
            proof_by_candidate[cid] = proof

    root = _brief_attestation_root(e)
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get("fact_class") != "localization":
            continue
        candidate_id = receipt.get("candidate_id")
        full_hash = receipt.get("content_hash")
        span = receipt.get("char_span")
        if (
            not isinstance(candidate_id, str) or not candidate_id
            or not isinstance(full_hash, str) or len(full_hash) != 64
            or not isinstance(span, list) or len(span) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in span)
            or span[0] < 0 or span[0] >= span[1] or span[1] > len(brief_text)
        ):
            continue
        # Re-derive the seal from the delivered bytes: the attestation may only bind a
        # seal that is exactly the delivered block's hash (defence against a stale receipt).
        block = brief_text[span[0]:span[1]]
        digest = hashlib.sha256(block.encode("utf-8", "surrogatepass")).hexdigest()
        if digest != full_hash:
            continue
        proof = proof_by_candidate.get(candidate_id)
        if proof is None:
            continue
        try:
            rank = proof.get("rank")
            final = finalize_localization_attestation(
                candidate_id=candidate_id,
                delivery_seal=digest[:16],
                block_content_sha256=digest,
                path=str(proof.get("path") or ""),
                rank=rank if type(rank) is int else 0,
                witness=str(proof.get("witness") or ""),
                witness_verified=proof.get("witness_verified") is True,
            )
            if final is None:
                continue
            persist_attestation(final.attestation, final.artifact_mapping(), root)
        except Exception as exc:  # noqa: BLE001 -- correct-or-quiet host sidecar
            _bc(f"brief localization attestation skipped ({type(exc).__name__})")


def _persist_brief_obligations_attestations(e: Mapping[str, str], brief_text: str) -> None:
    """Seal the delivered task-start obligations BLOCK with an immutable producer
    attestation joinable to its per-block delivery seal.

    Reads the SAME sealed ``brief_result.json`` the compound lineage row is built from
    (its ``block_receipts`` give the exact obligations block seal; its
    ``obligations_record`` gives the producer's build-time extraction identity — the exact
    issue source sha/revision + the extracted obligations digest/count), builds a
    :func:`groundtruth.runtime.brief_attestation.finalize_obligations_attestation` bundle,
    and atomically persists it to the shared attestation store. HOST-SIDE,
    correct-or-quiet: any miss (absent record, mismatched seal, unreadable result, store
    I/O error) leaves the delivered brief byte-identical and the class honestly unmeasured;
    it never raises.

    NAMED GAP (fail-closed, never fabricated): the reactive ``obligation_unexercised`` form
    has NO producer-owned, re-verifiable truth artifact (no persisted snapshot analogous to
    ``obligations_record`` / ``newfile_precedent`` snapshot), so it is deliberately NOT
    attested here — its truth stays UNMEASURED rather than inventing evidence.
    """
    brief_path = (e.get("GT_BRIEF_FILE") or "/gt_artifacts/brief.txt").strip()
    result_path = os.path.join(os.path.dirname(brief_path), "brief_result.json")
    try:
        with open(result_path, encoding="utf-8") as fh:
            result = json.load(fh)
        if (
            result.get("schema") != "gt.brief_result.v1"
            or result.get("brief_text") != brief_text
        ):
            return
        metrics = result.get("metrics") or {}
        receipts = metrics.get("block_receipts")
        record = metrics.get("obligations_record")
        if not isinstance(receipts, list) or not isinstance(record, dict) or not record:
            return
        from groundtruth.runtime.attestation_store import persist_attestation
        from groundtruth.runtime.brief_attestation import (
            finalize_obligations_attestation,
        )
    except (ImportError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        _bc(f"brief obligations attestations skipped setup ({type(exc).__name__})")
        return

    if record.get("schema") != "gt.obligations_record.v1":
        return
    receipt = next(
        (r for r in receipts
         if isinstance(r, dict)
         and r.get("fact_class") == "obligations"
         and r.get("label") == "obligations"),
        None,
    )
    if receipt is None:
        return
    candidate_id = receipt.get("candidate_id")
    full_hash = receipt.get("content_hash")
    span = receipt.get("char_span")
    if (
        not isinstance(candidate_id, str) or not candidate_id
        or not isinstance(full_hash, str) or len(full_hash) != 64
        or not isinstance(span, list) or len(span) != 2
        or not all(isinstance(n, int) and not isinstance(n, bool) for n in span)
        or span[0] < 0 or span[0] >= span[1] or span[1] > len(brief_text)
    ):
        return
    # Re-derive the seal from the delivered bytes (defence against a stale receipt) and
    # cross-check the record's own bindings — the attestation may bind ONLY a seal that is
    # exactly the delivered block's hash for the SAME candidate the record names.
    block = brief_text[span[0]:span[1]]
    digest = hashlib.sha256(block.encode("utf-8", "surrogatepass")).hexdigest()
    if (
        digest != full_hash
        or record.get("candidate_id") != candidate_id
        or record.get("block_content_sha256") != full_hash
        or ("<gt-obligations>" not in block
            and "Requirements to satisfy (from the issue):" not in block)
    ):
        return
    try:
        count = record.get("obligation_count")
        final = finalize_obligations_attestation(
            candidate_id=candidate_id,
            delivery_seal=digest[:16],
            block_content_sha256=digest,
            issue_sha256=str(record.get("issue_sha256") or ""),
            issue_revision=str(record.get("issue_revision") or ""),
            obligation_count=count if type(count) is int else 0,
            obligations_digest=str(record.get("obligations_digest") or ""),
        )
        if final is None:
            return
        persist_attestation(
            final.attestation, final.artifact_mapping(), _brief_attestation_root(e))
    except Exception as exc:  # noqa: BLE001 -- correct-or-quiet host sidecar
        _bc(f"brief obligations attestation skipped ({type(exc).__name__})")


def _record_brief_delivery(e: Mapping[str, str], brief_text: str) -> None:
    """Seal the exact step-0 brief at its existing delivery owner.

    Host metadata only: this never changes the task string.  Profile-2's
    existing in-seam metrics flag activates the row; absent/failed ledger I/O
    leaves the delivery intact but its ACQ proof honestly unmeasured.
    """
    if (e.get("GT_INSEAM_METRICS") or "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return
    path = (e.get("GT_RUNTIME_LEDGER") or "").strip()
    if not path:
        return
    row = {
        "layer": "brief.task",
        "event_type": "task_start",
        "file_path": "",
        "outcome": "delivered",
        "reason": "step0_brief_prepend",
        "chars_delivered": len(brief_text),
        "iteration": 0,
        "content_sha256_16": hashlib.sha256(
            brief_text.encode("utf-8", "surrogatepass")
        ).hexdigest()[:16],
        "seal_scope": "block",
        "opportunity_exempt_reason": "task_start_pre_policy",
    }
    row.update(_brief_delivery_extra(e, brief_text))
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        global _BRIEF_LEDGER_WRITE_FAILURES
        _BRIEF_LEDGER_WRITE_FAILURES += 1
        _bc(f"WARN brief delivery seal unavailable ({type(exc).__name__})")
    # Seal each delivered, graph-verified localization block with a joinable producer
    # attestation (sidecar only; never changes the delivered brief bytes, correct-or-quiet).
    _persist_brief_localization_attestations(e, brief_text)
    # Seal the delivered obligations block with a joinable producer attestation bound to the
    # exact issue source + extracted obligations record (sidecar only; correct-or-quiet).
    _persist_brief_obligations_attestations(e, brief_text)


def _resolve_task(e: Mapping[str, str]) -> str:
    """Resolve the agent task text: the guarded GT_RUN_TASK with the STEP-0 GT BRIEF prepended.

    THE B-wire (2026-07-13): the pier path prepended the curated step-0 brief onto the agent
    instruction (gt_agent._prepend_brief, gt_agent.py:1115); the mini HEADLESS path lost that wire —
    the workflow stages the issue into GT_RUN_TASK and this runner called ``agent.run(task)`` with
    the issue ALONE, so every mini run to date ran with the step-0 channel DARK (brief.txt generated
    but never seen by the agent). Restore it here: on the GT arm read the substrate brief (baked by
    the substrate-proof step to the host artifacts dir, bind-mounted READ-ONLY into the container at
    ``GT_BRIEF_FILE`` — default ``/gt_artifacts/brief.txt``) and PREPEND it so the model reads it at
    turn 0 on the observation channel it already consumes.

    Correct-or-quiet: a missing / unreadable / empty brief leaves the task UNCHANGED (a brief-less
    run is degraded, never broken). BASELINE (control) arm NEVER reads the file, so its task text is
    byte-identical to a run with no brief on disk — the paired control is preserved exactly.
    """
    task = (e.get("GT_RUN_TASK") or "").strip() or "Fix the issue in the repository."
    if e.get("GT_BASELINE") == "1":
        _bc("GT_BASELINE=1 -> brief NOT prepended (pure control arm)")
        return task
    brief_path = (e.get("GT_BRIEF_FILE") or "/gt_artifacts/brief.txt").strip()
    try:
        with open(brief_path, "rb") as fh:
            brief_text = fh.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 -- correct-or-quiet: a brief read miss must NOT fail the run
        _bc(f"no brief file -> task unchanged ({type(exc).__name__} @ {brief_path})")
        return task
    if not brief_text.strip():
        _bc(f"empty brief -> task unchanged (@ {brief_path})")
        return task
    _record_brief_delivery(e, brief_text)
    _bc(f"brief prepended ({len(brief_text)} chars @ {brief_path})")
    return brief_text + "\n\n" + task


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
    # STEP-0 BRIEF (2026-07-13): prepend the substrate brief onto the task on the GT arm (see
    # _resolve_task) — restores the pier-path prepend the headless pipeline had lost. Baseline
    # arm returns the guarded issue text byte-identically.
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
    if gt_mini_patch is not None:
        try:
            installed = bool(gt_mini_patch.install_observation_batch_commit(agent))
            batch_attached = bool(
                installed and getattr(agent, "_gt_batch_commit_installed", False)
            )
            batch_result = "installed" if batch_attached else "install_unavailable"
            if not batch_attached:
                _bc("WARN GT batch handshake unavailable")
        except Exception as _exc:  # noqa: BLE001 -- report before the paid call
            batch_result = f"install_error:{type(_exc).__name__}"
            _bc(f"WARN GT batch-commit install failed: {type(_exc).__name__}: {_exc}")
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
            "FATAL: required observation batch hook is unproven; "
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
            attestation = ledger_attestation.write_attestation(
                e.get("GT_RUNTIME_LEDGER") or "/tmp/gt_runtime_ledger.jsonl",
                write_failures=_BRIEF_LEDGER_WRITE_FAILURES + seam_failures,
            )
            _bc(
                "runtime ledger terminal attestation "
                f"rows={attestation['row_count']} bytes={attestation['byte_count']} "
                f"sha256={attestation['sha256']}"
            )
            if attestation["write_failures"] != 0:
                _bc(
                    "FATAL: runtime ledger writer reported "
                    f"{attestation['write_failures']} failure(s)"
                )
                return 2
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            _bc(f"FATAL: runtime ledger attestation failed: {type(exc).__name__}: {exc}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

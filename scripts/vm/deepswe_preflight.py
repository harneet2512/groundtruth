#!/usr/bin/env python3
"""scripts/vm/deepswe_preflight.py — cheap, LOCAL, NO-LLM, NO-LAUNCH preflight.

Run this BEFORE any official DeepSWE benchmark launch. It fails LOUDLY (exit 1) if the
run is not protected against the known contamination classes learned from the 2026-07-07
parity incident:

  1. Unbounded LLM calls  -> a stalled request hangs a run forever (the offunzu freeze).
  2. Mis-classification   -> a killed/timed-out/ungradeable run booked as a valid AGENT 0.0.
  3. No gradeability truth -> an eicrud-class task (gold->0 locally) counted as a regression.
  4. Raw parallel launch  -> a compose/egress burst on one daemon (races + hangs).

It does NOT call the model, pull images, or launch tasks. It only reads files.

Usage:
  python3 scripts/vm/deepswe_preflight.py [--pier-config PATH] [--out-dir DIR] [--strict]
Exit: 0 = GO (no FAILs), 1 = NO-GO (>=1 FAIL). WARNs never fail unless --strict.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
VERIFY_DIR = os.path.join(REPO_ROOT, "scripts", "verify")

_results: list[tuple[str, str, str]] = []  # (level, name, detail)


def _rec(level: str, name: str, detail: str) -> None:
    _results.append((level, name, detail))


# ── model_kwargs extraction (prefer PyYAML; indent-regex fallback) ────────────
def _model_kwargs(cfg_text: str) -> dict:
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(cfg_text) or {}
        mk = ((cfg.get("model") or {}).get("model_kwargs")) or {}
        if isinstance(mk, dict):
            return mk
    except Exception:
        pass
    # Fallback: model_kwargs scalars sit at a deeper indent than `model_kwargs:`. Capture
    # the block from `model_kwargs:` to the next line at the SAME-or-shallower indent, then
    # read `key: value` scalars inside it. Distinguishes model_kwargs.timeout (deep indent)
    # from environment.timeout (shallow).
    lines = cfg_text.splitlines()
    out: dict = {}
    start = None
    base_indent = 0
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)model_kwargs:\s*$", ln)
        if m:
            start = i + 1
            base_indent = len(m.group(1))
            break
    if start is None:
        return out
    for ln in lines[start:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent <= base_indent:
            break  # left the model_kwargs block
        m = re.match(r"^\s*([A-Za-z_][\w]*):\s*(\S.*?)\s*$", ln)
        if m:
            key, val = m.group(1), m.group(2)
            try:
                out[key] = int(val)
            except ValueError:
                out[key] = val
    return out


# ── checks ────────────────────────────────────────────────────────────────────
def check_llm_timeout(pier_config: str) -> None:
    if not os.path.isfile(pier_config):
        _rec("FAIL", "llm_timeout", f"pier config not found: {pier_config}")
        return
    mk = _model_kwargs(open(pier_config, encoding="utf-8").read())
    timeout = mk.get("timeout")
    retries = mk.get("num_retries")
    if not isinstance(timeout, int) or timeout <= 0:
        _rec("FAIL", "llm_timeout",
             f"model_kwargs.timeout is not a positive int in {os.path.basename(pier_config)} "
             f"(got {timeout!r}). An unbounded LLM call hangs forever on a stalled connection. "
             f"Add `timeout: 180` under model.model_kwargs.")
    else:
        _rec("OK", "llm_timeout", f"model_kwargs.timeout={timeout}s in {os.path.basename(pier_config)}")
    if not isinstance(retries, int) or retries <= 0:
        _rec("FAIL", "llm_retries",
             f"model_kwargs.num_retries is not a positive int (got {retries!r}). A timeout must "
             f"retry, not fail the task. Add `num_retries: 3`.")
    else:
        _rec("OK", "llm_retries", f"model_kwargs.num_retries={retries}")


def check_classifier() -> None:
    sys.path.insert(0, VERIFY_DIR)
    try:
        import deepswe_outcome as dso  # type: ignore
    except Exception as e:  # noqa: BLE001
        _rec("FAIL", "classifier_import", f"cannot import scripts/verify/deepswe_outcome.py: {e}")
        return
    need_subtypes = {"INFRA_MODEL_TIMEOUT", "INFRA_KILLED"}
    have = set(getattr(dso, "INFRA_SUBTYPES", ()))
    missing = need_subtypes - have
    if missing:
        _rec("FAIL", "classifier_states",
             f"deepswe_outcome.INFRA_SUBTYPES missing {sorted(missing)} — a killed/timed-out run "
             f"would misclassify as a valid AGENT 0.0.")
    else:
        _rec("OK", "classifier_states", "INFRA_MODEL_TIMEOUT + INFRA_KILLED present")
    excluded = set(getattr(dso, "DENOMINATOR_EXCLUDED", frozenset()))
    if "UNGRADEABLE" not in excluded:
        _rec("FAIL", "classifier_ungradeable",
             "UNGRADEABLE not in DENOMINATOR_EXCLUDED — an ungradeable task would count as a regression.")
    else:
        _rec("OK", "classifier_ungradeable", "UNGRADEABLE excluded from the resolved denominator")
    # The classifier must actually classify a synthetic killed run as INFRA, not AGENT.
    try:
        # Assert the ungradeable_reason param exists AND drives the UNGRADEABLE class.
        rec_ungr = dso.build_signal_record(
            instance_id="synthetic-ungr", reward=0.0, n_agent_steps=42,
            exit_status="Submitted", trial_log="", cert_dir=None,
            ungradeable_reason="known_ungradeable")
        if rec_ungr.get("failure_class") != "UNGRADEABLE":
            _rec("FAIL", "classifier_behavior",
                 f"an ungradeable record classified {rec_ungr.get('failure_class')!r}, expected UNGRADEABLE")
        else:
            _rec("OK", "classifier_behavior", "ungradeable_reason -> UNGRADEABLE (verified)")
    except TypeError as e:
        _rec("FAIL", "classifier_behavior",
             f"build_signal_record does not accept ungradeable_reason: {e}")
    except Exception as e:  # noqa: BLE001
        _rec("WARN", "classifier_behavior", f"synthetic classify raised: {e}")


def check_gradeability_registry() -> None:
    path = (os.environ.get("GT_GRADEABILITY_REGISTRY")
            or os.path.join(VERIFY_DIR, "gradeability.json"))
    if not os.path.isfile(path):
        _rec("WARN", "gradeability_registry",
             f"no registry at {path} — every task is ASSUMED gradeable (an eicrud-class task would "
             f"be counted as a regression). Add scripts/verify/gradeability.json.")
        return
    try:
        reg = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        _rec("FAIL", "gradeability_registry", f"registry does not parse: {e}")
        return
    ku = reg.get("known_ungradeable")
    vg = reg.get("verified_gradeable")
    if not isinstance(ku, dict) or not isinstance(vg, dict):
        _rec("FAIL", "gradeability_registry",
             "registry missing known_ungradeable / verified_gradeable dicts")
        return
    _rec("OK", "gradeability_registry",
         f"{len(ku)} known-ungradeable, {len(vg)} verified-gradeable cached")


def check_launch_guard() -> None:
    guard = os.path.join(REPO_ROOT, "scripts", "vm", "gt_launch_guard.sh")
    if not os.path.isfile(guard):
        _rec("FAIL", "launch_guard",
             "scripts/vm/gt_launch_guard.sh absent — nothing prevents a raw parallel launch burst.")
        return
    text = open(guard, encoding="utf-8").read()
    for fn in ("gt_require_orchestrator", "gt_launch_gate"):
        if f"{fn}()" not in text:
            _rec("FAIL", "launch_guard", f"gt_launch_guard.sh does not define {fn}")
            return
    # The box runner must both mark itself orchestrated AND call the gate.
    runner = os.path.join(REPO_ROOT, "scripts", "vm", "gt_agent_run.sh")
    if os.path.isfile(runner):
        rt = open(runner, encoding="utf-8").read()
        wired = "GT_ORCHESTRATED=1" in rt and "gt_launch_gate" in rt
        if not wired:
            _rec("WARN", "launch_guard",
                 "gt_agent_run.sh does not wire the launch gate (GT_ORCHESTRATED + gt_launch_gate).")
        else:
            _rec("OK", "launch_guard", "guard present + wired into gt_agent_run.sh")
    else:
        _rec("OK", "launch_guard", "gt_launch_guard.sh present (defines both functions)")


def check_out_dir(out_dir: str | None) -> None:
    if not out_dir:
        _rec("WARN", "out_dir",
             "no --out-dir given: cannot verify results won't mix with a prior run. Pass a CLEAN, "
             "run-namespaced OUT_DIR (e.g. .../run_<utc>).")
        return
    if not os.path.isdir(out_dir):
        _rec("OK", "out_dir", f"{out_dir} does not exist yet (clean)")
        return
    rows = []
    for root, _dirs, files in os.walk(out_dir):
        if "row.json" in files:
            rows.append(os.path.join(root, "row.json"))
    if rows:
        _rec("FAIL", "out_dir",
             f"{out_dir} already holds {len(rows)} row.json from a PRIOR run — results would mix. "
             f"Use a fresh run-namespaced dir or clear it (RESUME is intentional; a NEW run must not).")
    else:
        _rec("OK", "out_dir", f"{out_dir} exists but holds no prior rows")


def check_stale_collector() -> None:
    # A leftover launch-gate stamp far in the future, or a prior pidfile, can skew spacing.
    for p in ("/tmp/gt_launch.lock.stamp", "/tmp/mimo2div.pid"):
        if os.path.isfile(p):
            _rec("WARN", "stale_state",
                 f"leftover {p} from a prior run present — ensure no prior collector/orchestrator "
                 f"is still writing into this run's result dir.")
            return
    _rec("OK", "stale_state", "no leftover launch stamp / prior pidfile in /tmp")


def check_env(require_digest: bool) -> None:
    digest = os.environ.get("GT_SUBSTRATE_DIGEST", "")
    if require_digest:
        if "@sha256:" in digest:
            _rec("OK", "env_substrate", "GT_SUBSTRATE_DIGEST is a pinned @sha256 digest")
        else:
            _rec("FAIL", "env_substrate",
                 f"GT_SUBSTRATE_DIGEST is not a pinned @sha256 digest (got {digest!r}).")
    else:
        _rec("OK" if "@sha256:" in digest else "WARN", "env_substrate",
             "GT_SUBSTRATE_DIGEST pinned" if "@sha256:" in digest
             else "GT_SUBSTRATE_DIGEST not set (set it + re-check at launch with --require-digest)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="DeepSWE official-run preflight (no LLM, no launch).")
    ap.add_argument("--pier-config",
                    default=os.path.join(REPO_ROOT, "artifact_deepswe", "gt_integration",
                                         "deepswe_official_probe.yaml"))
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--require-digest", action="store_true",
                    help="require GT_SUBSTRATE_DIGEST to be a pinned @sha256 (launch-time check)")
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure too")
    args = ap.parse_args(argv[1:])

    print("=== DeepSWE PREFLIGHT (cheap/local; NO LLM call, NO task launch) ===")
    print(f"repo_root   = {REPO_ROOT}")
    print(f"pier_config = {args.pier_config}")
    print()

    check_llm_timeout(args.pier_config)
    check_classifier()
    check_gradeability_registry()
    check_launch_guard()
    check_out_dir(args.out_dir)
    check_stale_collector()
    check_env(args.require_digest)

    fails = [r for r in _results if r[0] == "FAIL"]
    warns = [r for r in _results if r[0] == "WARN"]
    for level, name, detail in _results:
        marker = {"OK": "  [OK]  ", "WARN": "  [WARN]", "FAIL": " [FAIL] "}[level]
        print(f"{marker} {name}: {detail}")
    print()
    print(f"summary: {len(fails)} FAIL, {len(warns)} WARN, "
          f"{len([r for r in _results if r[0]=='OK'])} OK")

    no_go = bool(fails) or (args.strict and bool(warns))
    if no_go:
        print("\nNO-GO: the official run is NOT protected against a known contamination class. "
              "Fix the FAIL lines above" + (" (and WARN under --strict)" if args.strict else "") + ".")
        return 1
    print("\nGO: no unguarded contamination class detected"
          + (" (WARNs present - review them)" if warns else "") + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

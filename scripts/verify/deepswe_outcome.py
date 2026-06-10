"""Extract the REAL outcome of a DeepSWE pier run from the jobs/ dir + CLASSIFY it.

Why this exists (learned from the GCP validation run, 2026-06-03):
- The job-level result.json (jobs/<ts>/result.json) is an AGGREGATE — it has no
  n_agent_steps / verifier_result. The per-trial result.json
  (jobs/<ts>/<task>__*/result.json) has them. `find jobs -name result.json | head`
  grabs the wrong one.
- result.json's `step_results` is EMPTY. The per-turn trajectory (commands, gt_hook
  calls, <gt-evidence>) lives in agent/mini-swe-agent.trajectory.json. The pass/fail
  outcome lives in verifier/test-stdout.txt.
- "Brief written to disk" is NOT proof the agent ran. The proof is n_agent_steps>0 +
  exit_status + reward. A run can write the brief and still do 0 useful work (the GHA
  284-empty-reprompt case) or crash before the agent (the cwd / key cases).

This surfaces, in the workflow log: did the AGENT actually run, did it submit, the
reward, the test pass/fail tally, the FAILING tests (the precise correctness gap),
and the GT hook firings from the real trajectory.

FAILURE CLASSIFICATION (audit Stage 5 — added 2026-06-09):
Extraction alone makes a pull/build/harness failure, an agent miss, and a GT-context
miss indistinguishable. For a paid 113-task benchmark every task MUST get a clean,
triageable failure class derived DETERMINISTICALLY from per-task signals — never from
task IDs / gold / per-task exceptions. The four classes (generalized rules, identical
across all 113 tasks):

- INFRA   — image-pull / substrate-pull / adapter-wire / harness failure
            (GT_SUBSTRATE_DIGEST_MISSING, GT_SUBSTRATE_PULL_FAIL, GT_RUN_PROOF_FAIL,
            GT_ARTIFACT_MISSING, TASK_IMAGE_PULL_FAIL, eval_no_report / harness crash).
            EXCLUDED from the resolved-rate denominator (never an agent/GT failure).
- GT      — GT delivered wrong/no/UNPROVEN context (DEEPSWE_ADAPTER_FAIL,
            gt_prebuilt_active=false, hook != post-LSP graph-hash mismatch, any
            embedder/LSP/graph cert with a FAIL verdict, OR the agent ran without
            resolving while the consumption witness is MISSING — witness-absent =
            unproven consumption = GT's problem, never UNKNOWN-excluded).
- AGENT   — GT context was sound (prebuilt active + valid certs) but the model missed:
            n_agent_steps>0 + reward 0.
- RESOLVED — reward 1.0.

The classifier reads ONLY signals the extractor already parses (reward, exit_status,
the [GT_META] witness fields, the cert verdicts, n_agent_steps). Precedence is
INFRA > GT > RESOLVED > AGENT > UNKNOWN: an infra/wiring break that prevents a clean
comparison wins, then a GT delivery break, then the outcome. INFRA is excluded from the
resolved denominator so it is never miscounted as an agent or GT failure.

PAIRED-DELTA SCAFFOLDING: build_paired_delta() keys a GT-on record against a baseline
record by instance_id for a later Wilcoxon signed-rank on per-task delta. STRUCTURE
ONLY — no baseline is fabricated; when no baseline record is supplied the delta fields
are None.

Usage: python3 scripts/verify/deepswe_outcome.py [jobs_dir]
       (optionally GT_TRIAL_LOG / GT_CERT_DIR / GT_DEEPSWE_OUTCOME_JSON env overrides)
"""
from __future__ import annotations
import glob
import json
import os
import re
import sys
from collections import Counter

# ===========================================================================
# Classification primitives — pure, deterministic, no task-specific logic.
# ===========================================================================

#: Markers the workflow prints to stdout/stderr (captured in trial_output.log) on an
#: infra/substrate/harness failure. Matched LINE-ANCHORED (see find_infra_markers) so an
#: infra token EMBEDDED in an adapter-fail message can never be mistaken for the
#: workflow's own fail-closed line. Generalized: these are the run-level fail-closed
#: tokens emitted in deepswe_full.yml §E, identical for every task. (eval_no_report is
#: also detected structurally from the eval report.)
INFRA_LOG_MARKERS: tuple[str, ...] = (
    "GT_SUBSTRATE_DIGEST_MISSING",
    "GT_SUBSTRATE_PULL_FAIL",
    "GT_RUN_PROOF_FAIL",
    "GT_PROOF_OOM",
    "GT_ARTIFACT_MISSING",
    "TASK_IMAGE_PULL_FAIL",
    # The task issue could not be materialized (no instruction.md, no task.toml
    # issue/prompt) — the substrate is never run with an EMPTY issue (fail-closed in
    # deepswe_full.yml). A harness-input failure, not an agent or GT failure.
    "GT_ISSUE_MISSING",
)

#: The adapter-wire failure marker (§E DEEPSWE_ADAPTER_FAIL). This is a GT-side failure
#: (the adapter could not consume / fingerprint the substrate), classified GT — NOT INFRA.
GT_ADAPTER_FAIL_MARKER = "DEEPSWE_ADAPTER_FAIL"


def find_infra_markers(trial_log: str) -> list[str]:
    """§E infra markers found in the trial log, matched PRECISELY (token-collision fix).

    Two rules, both required so INFRA can never eat a GT adapter-consume failure:
      1. LINE-ANCHORED — a marker counts only when it STARTS a log line (modulo leading
         whitespace / a `::error::` GHA prefix). The workflow's own fail-closed echoes
         start the line with the marker; an adapter message that merely EMBEDS the token
         (e.g. `error=DEEPSWE_ADAPTER_FAIL(GT_ARTIFACT_MISSING: brief absent)`) does not.
      2. ADAPTER-LINE EXCLUSION — any line carrying DEEPSWE_ADAPTER_FAIL is the
         adapter's (class GT); it is never scanned for infra markers.
    Returns markers in INFRA_LOG_MARKERS order (deterministic).
    """
    starts: list[str] = []
    for ln in (trial_log or "").splitlines():
        if GT_ADAPTER_FAIL_MARKER in ln:
            continue  # adapter-fail lines belong to class GT, never INFRA
        s = ln.lstrip()
        if s.startswith("::error::"):
            s = s[len("::error::"):].lstrip()
        starts.append(s)
    return [m for m in INFRA_LOG_MARKERS if any(s.startswith(m) for s in starts)]

#: The certs whose verdict gates GT correctness. A FAIL verdict (or pass=False) on any of
#: these means GT delivered an unsound substrate -> class GT.
CERT_FILES: tuple[str, ...] = (
    "graph_certificate.json",
    "lsp_certificate.json",
    "embedder_certificate.json",
)


def _read_trial_log(log_path: str | None) -> str:
    """Return the trial_output.log text (ANSI-stripped), or '' if absent.

    The log is the AGENT-OBSERVATION truth source: it carries both the [GT_META]
    witness lines and the §E infra/adapter fail markers.
    """
    if not log_path or not os.path.isfile(log_path):
        return ""
    try:
        raw = open(log_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""
    return ANSI.sub("", raw)


def _gt_meta_witness(trial_log: str) -> dict:
    """Parse the LAST [GT_META] witness line's `key=value` fields from the trial log.

    The adapter (gt_agent.py) emits `[GT_META] ... gt_prebuilt_active=...;
    hook_graph_hash_matches_post_lsp=...; error=...` to stdout. We read the LAST such
    line (the final adapter state) and split its `; `-separated `k=v` pairs. Returns {}
    if no witness was emitted (itself a signal: no consumption proof).
    """
    lines = [ln for ln in trial_log.splitlines() if "[GT_META]" in ln]
    if not lines:
        return {}
    # Prefer the canonical graph_witness line (carries the full cert/digest suffix); else
    # fall back to the last [GT_META] line of any kind.
    witness_lines = [ln for ln in lines if "gt_prebuilt_active=" in ln] or lines
    line = witness_lines[-1]
    fields: dict[str, str] = {}
    # The witness mixes a free-text `graph_witness host_resolved_graph_db=... | ...`
    # prefix with `; `-separated `k=v` pairs. Scan for ALL `key=value` tokens anywhere
    # on the line (value runs until the next `;`, `|`, or whitespace-then-key), so a
    # field embedded in the prefix segment (e.g. gt_prebuilt_active inside the pipe
    # prefix) is still captured. Later occurrences win (the canonical suffix overrides).
    body = line.split("[GT_META]", 1)[-1]
    for m in re.finditer(r"([A-Za-z_][\w]*)\s*=\s*([^;|\s]+)", body):
        fields[m.group(1)] = m.group(2).strip()
    return fields


def _cert_verdict(cert_dir: str | None, name: str) -> tuple[str | None, bool | None]:
    """Return (verdict, pass) for a single cert JSON, or (None, None) if absent.

    Each cert (graph/lsp/embedder) is stamped with a top-level `verdict` string
    (e.g. GRAPH_VALID / LSP_ACTIVE_VALID / *_FAIL_*) and a `pass` boolean by its
    scripts/metrics classifier. We read both; either signalling FAIL is a GT failure.
    """
    if not cert_dir:
        return (None, None)
    path = os.path.join(cert_dir, name)
    if not os.path.isfile(path):
        return (None, None)
    try:
        with open(path, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return (None, None)
    verdict = c.get("verdict")
    passed = c.get("pass")
    return (verdict if isinstance(verdict, str) else None,
            bool(passed) if isinstance(passed, bool) else None)


def collect_cert_verdicts(cert_dir: str | None) -> dict[str, dict]:
    """Read the three gating certs into {name: {verdict, pass}} (present certs only).

    A cert is a FAIL if pass is False, OR (when pass is absent) the verdict string
    contains 'FAIL'. Missing certs are NOT treated as a cert-FAIL here — the
    GT_ARTIFACT_MISSING infra marker (or the adapter witness) owns missing-substrate.
    """
    out: dict[str, dict] = {}
    for name in CERT_FILES:
        verdict, passed = _cert_verdict(cert_dir, name)
        if verdict is None and passed is None:
            continue
        is_fail = (passed is False) or (verdict is not None and "FAIL" in verdict.upper())
        out[name] = {"verdict": verdict, "pass": passed, "is_fail": is_fail}
    return out


def _any_cert_fail(certs: dict[str, dict]) -> bool:
    return any(c.get("is_fail") for c in certs.values())


def _to_bool(v) -> bool | None:
    """Coerce a [GT_META] string field ('true'/'false') or a real bool to bool|None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


def classify_outcome(rec: dict) -> str:
    """Deterministic failure classifier over a per-task signal record -> class string.

    Precedence (INFRA > GT > RESOLVED > GT-witness-absent > AGENT > UNKNOWN):
      1. INFRA — any §E infra/harness marker fired, OR the eval produced no report
         (eval_no_report). Excluded downstream from the resolved denominator.
      2. GT    — the adapter failed to wire (DEEPSWE_ADAPTER_FAIL), the substrate graph
         was not consumed (gt_prebuilt_active=false), the consumed graph != the
         post-LSP graph (hook_graph_hash_matches_post_lsp=false), or any cert FAILed.
      3. RESOLVED — reward == 1.0.
      3b. GT — the agent RAN (n_agent_steps>0) and did NOT resolve (reward<1) but the
         consumption witness is MISSING (gt_prebuilt_active unknown): witness-absent =
         UNPROVEN consumption = GT's problem. It stays IN the resolved denominator —
         never UNKNOWN-excluded (the old asymmetry let unproven GT delivery vanish
         from the rate).
      4. AGENT — GT context was sound (prebuilt active + no cert FAIL) and the agent
         ran (n_agent_steps>0) but did not resolve (reward 0).
      5. UNKNOWN — no result at all (no reward, no infra marker, nothing attributable)
         — surfaced explicitly, never silently bucketed.

    `rec` fields (all optional; produced by build_signal_record):
      infra_markers: list[str]      eval_no_report: bool
      adapter_fail: bool            gt_prebuilt_active: bool|None
      hook_hash_match: bool|None    cert_fail: bool
      reward: float|None            n_agent_steps: int|None
    """
    # 1. INFRA — wins outright; the comparison was never clean.
    if rec.get("infra_markers") or rec.get("eval_no_report"):
        return "INFRA"

    # 2. GT — delivery break. Adapter-wire fail, no consumption, hash divergence, cert FAIL.
    if rec.get("adapter_fail"):
        return "GT"
    if rec.get("gt_prebuilt_active") is False:
        return "GT"
    if rec.get("hook_hash_match") is False:
        return "GT"
    if rec.get("cert_fail"):
        return "GT"

    # 3. RESOLVED — the win.
    reward = rec.get("reward")
    if reward is not None and float(reward) >= 1.0:
        return "RESOLVED"

    # 3b. GT — agent ran, did not resolve, and the consumption WITNESS IS MISSING
    # (gt_prebuilt_active unknown). Witness-absent = unproven consumption = GT's
    # problem; it counts in the resolved denominator (never UNKNOWN-excluded).
    steps = rec.get("n_agent_steps")
    if (rec.get("gt_prebuilt_active") is None
            and isinstance(steps, int) and steps > 0
            and reward is not None and float(reward) < 1.0):
        return "GT"

    # 4. AGENT — sound GT context, agent ran, did not resolve.
    if (rec.get("gt_prebuilt_active") is True
            and not rec.get("cert_fail")
            and isinstance(steps, int) and steps > 0
            and reward is not None and float(reward) < 1.0):
        return "AGENT"

    # 5. UNKNOWN — not enough signal to attribute.
    return "UNKNOWN"


#: Classes excluded from the resolved-rate denominator. INFRA failures (and harness
#: no-report) are NOT agent or GT failures — counting them would defame both. UNKNOWN is
#: also excluded (un-attributable -> cannot be charged to GT or the agent).
DENOMINATOR_EXCLUDED: frozenset[str] = frozenset({"INFRA", "UNKNOWN"})


def is_in_resolved_denominator(failure_class: str) -> bool:
    """True if this task counts toward the resolved-rate denominator.

    GT, AGENT, and RESOLVED count (each is a clean attributable outcome). INFRA and
    UNKNOWN do not (un-comparable / un-attributable).
    """
    return failure_class not in DENOMINATOR_EXCLUDED


def tally_classes(records: list[dict]) -> dict:
    """Run-level tally over classified per-task records.

    Returns counts per class, the denominator (GT+AGENT+RESOLVED), resolved count, and
    the resolved_rate over that infra-excluded denominator (None when denominator==0).
    """
    counts = Counter(r.get("failure_class", "UNKNOWN") for r in records)
    denominator = sum(1 for r in records
                      if is_in_resolved_denominator(r.get("failure_class", "UNKNOWN")))
    resolved = counts.get("RESOLVED", 0)
    rate = (resolved / denominator) if denominator else None
    return {
        "total_tasks": len(records),
        "counts": dict(counts),
        "resolved": resolved,
        "denominator_excluding_infra": denominator,
        "excluded_from_denominator": sum(counts.get(c, 0) for c in DENOMINATOR_EXCLUDED),
        # 8-dp per the constitution mandate (no rounding/truncation).
        "resolved_rate": (f"{rate:.8f}" if rate is not None else None),
    }


# ===========================================================================
# Paired-delta scaffolding — GT-on vs baseline, keyed by instance_id (STRUCTURE ONLY).
# ===========================================================================

def build_paired_delta(gt_on: dict, baseline: dict | None = None) -> dict:
    """Pair a GT-on per-task record against a baseline record by instance_id.

    STRUCTURE ONLY — no baseline is fabricated. When `baseline` is None the delta
    fields are None (a later Wilcoxon signed-rank consumes only paired, present
    deltas). The pairing key is instance_id; a key mismatch is surfaced, never
    silently averaged. Deltas are GT-on minus baseline at 8-dp.
    """
    iid = gt_on.get("instance_id")

    def _num(rec: dict | None, key: str) -> float | None:
        if not rec:
            return None
        v = rec.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _delta(a: float | None, b: float | None) -> str | None:
        if a is None or b is None:
            return None
        return f"{(a - b):.8f}"

    key_mismatch = bool(baseline) and baseline.get("instance_id") != iid

    gt_reward = _num(gt_on, "reward")
    bl_reward = _num(baseline, "reward")
    gt_steps = _num(gt_on, "n_agent_steps")
    bl_steps = _num(baseline, "n_agent_steps")

    return {
        "instance_id": iid,
        "baseline_present": baseline is not None,
        "key_mismatch": key_mismatch,
        "gt_on": {
            "failure_class": gt_on.get("failure_class"),
            "reward": (f"{gt_reward:.8f}" if gt_reward is not None else None),
            "n_agent_steps": (f"{gt_steps:.8f}" if gt_steps is not None else None),
        },
        "baseline": ({
            "failure_class": baseline.get("failure_class"),
            "reward": (f"{bl_reward:.8f}" if bl_reward is not None else None),
            "n_agent_steps": (f"{bl_steps:.8f}" if bl_steps is not None else None),
        } if baseline is not None else None),
        # GT-on minus baseline (None until a real baseline record is supplied).
        "resolved_delta": _delta(gt_reward, bl_reward),
        "action_count_delta": _delta(gt_steps, bl_steps),
    }


# ===========================================================================
# Signal extraction — build the per-task record the classifier consumes.
# ===========================================================================

def build_signal_record(
    *,
    instance_id: str | None,
    reward: float | None,
    n_agent_steps: int | None,
    exit_status: str | None,
    trial_log: str,
    cert_dir: str | None,
    eval_no_report: bool = False,
) -> dict:
    """Assemble the per-task signal record + classify it.

    Reads ONLY signals the extractor already parses: reward, n_agent_steps,
    exit_status, the [GT_META] witness fields (from trial_log), the cert verdicts
    (from cert_dir), and the §E infra/adapter markers (from trial_log).
    """
    infra_markers = find_infra_markers(trial_log)
    adapter_fail = GT_ADAPTER_FAIL_MARKER in trial_log
    meta = _gt_meta_witness(trial_log)
    certs = collect_cert_verdicts(cert_dir)

    rec = {
        "instance_id": instance_id,
        "reward": reward,
        "n_agent_steps": n_agent_steps,
        "exit_status": exit_status,
        "infra_markers": infra_markers,
        "eval_no_report": bool(eval_no_report),
        "adapter_fail": adapter_fail,
        "gt_prebuilt_active": _to_bool(meta.get("gt_prebuilt_active")),
        "hook_hash_match": _to_bool(meta.get("hook_graph_hash_matches_post_lsp")),
        "cert_verdicts": certs,
        "cert_fail": _any_cert_fail(certs),
        "gt_meta_present": bool(meta),
    }
    rec["failure_class"] = classify_outcome(rec)
    rec["in_resolved_denominator"] = is_in_resolved_denominator(rec["failure_class"])
    return rec


# ===========================================================================
# Live extraction (CLI) — unchanged surface + the new classification block.
# ===========================================================================

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _detect_eval_no_report(jobs: str) -> bool:
    """Detect the harness 'eval_no_report' status from any eval/result report on disk.

    The eval harness stamps `status: eval_no_report` when it crashed before producing a
    report (an INFRA failure). Best-effort over the common report names; absence => False.
    """
    for pat in ("eval_result.json", "report.json", "result.json"):
        for p in glob.glob(os.path.join(jobs, "**", pat), recursive=True):
            try:
                r = json.load(open(p, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(r, dict) and r.get("status") == "eval_no_report":
                return True
    return False


def main(argv: list[str]) -> int:
    jobs = argv[1] if len(argv) > 1 else "jobs"

    print("=== DeepSWE TRIAL OUTCOME (real proof, not brief-written) ===")

    # exit_status + model_stats live in the agent trajectory.json info, NOT the trial
    # result.json — pull them from there.
    traj_info: dict = {}
    _traj = glob.glob(os.path.join(jobs, "*", "*__*", "agent", "mini-swe-agent.trajectory.json"))
    if _traj:
        try:
            traj_info = (json.load(open(_traj[-1], encoding="utf-8")).get("info") or {})
        except Exception:
            traj_info = {}

    # Signals the classifier consumes (filled as we parse the artifacts below).
    reward: float | None = None
    n_agent_steps: int | None = None
    exit_status: str | None = None
    instance_id: str | None = None

    trials = sorted(glob.glob(os.path.join(jobs, "*", "*__*", "result.json")))
    if not trials:
        print("AGENT_RAN_STEPS=UNKNOWN  -- no trial result.json (harness broke before the agent?)")
    else:
        d = json.load(open(trials[-1], encoding="utf-8"))
        info = d.get("info") or {}
        vr = d.get("verifier_result") or {}
        exc = d.get("exception_info")
        n_agent_steps = d.get("n_agent_steps")
        exit_status = traj_info.get("exit_status") or info.get("exit_status")
        reward = (vr.get("rewards") or {}).get("reward")
        instance_id = (d.get("instance_id") or info.get("instance_id")
                       or info.get("instance", {}).get("instance_id")
                       if isinstance(info.get("instance"), dict) else
                       (d.get("instance_id") or info.get("instance_id")))
        print(f"AGENT_RAN_STEPS={n_agent_steps}   (>0 = harness healthy; 0/None = broke before agent)")
        print(f"EXIT_STATUS={exit_status}   (Submitted = agent finished + submitted a patch)")
        print(f"API_CALLS={(traj_info.get('model_stats') or info.get('model_stats') or {}).get('api_calls')}")
        print(f"REWARD={reward}   (1.0 = task resolved)")
        print(f"EXCEPTION={(exc or {}).get('exception_type') if exc else None}")

    # verifier: pass/fail tally + the failing tests (the precise correctness gap)
    vouts = glob.glob(os.path.join(jobs, "*", "*__*", "verifier", "test-stdout.txt"))
    if vouts:
        txt = ANSI.sub("", open(vouts[-1], encoding="utf-8", errors="replace").read())
        tally = [l.strip() for l in txt.splitlines() if re.search(r"\d+ (passing|failing|pending)", l)]
        print("--- verifier tally ---")
        for l in tally[-5:]:
            print("  " + l)
        m = re.search(r"\n\s*\d+ failing", txt)
        if m:
            print("--- failing tests (the correctness gap) ---")
            for l in txt[m.start():m.start() + 3500].splitlines():
                if re.search(r"^\s*\d+\)|Error|expected|Unable to resolve|throw|AssertionError", l):
                    print("  " + l.strip()[:160])
    else:
        print("(no verifier/test-stdout.txt found)")

    # GT hook firings from the REAL per-turn trajectory (NOT result.json, whose step_results is empty)
    trajs = glob.glob(os.path.join(jobs, "*", "*__*", "agent", "mini-swe-agent.trajectory.json"))
    if trajs:
        t = open(trajs[-1], encoding="utf-8", errors="replace").read()
        c = Counter(re.findall(
            r"gt_hook|gt understand|gt verify|<gt-evidence>|behavioral_contract|post_edit|post_view|CONSENSUS",
            t, re.I))
        print("--- GT hook firings (agent trajectory) ---")
        for k, v in c.most_common():
            print(f"  {v:4} {k}")

    # ── FAILURE CLASSIFICATION (audit Stage 5) ──────────────────────────────
    # The trial log (AGENT-OBSERVATION truth) carries the [GT_META] witness + §E markers;
    # the cert dir carries the graph/lsp/embedder verdicts. Locate both from env (the CI
    # handoff) with sensible fallbacks, then classify.
    log_path = (os.environ.get("GT_TRIAL_LOG")
                or ("trial_output.log" if os.path.isfile("trial_output.log") else None))
    cert_dir = (os.environ.get("GT_CERT_DIR")
                or ("/tmp/gt" if os.path.isdir("/tmp/gt") else None)
                or ("trial_results/gt_artifacts" if os.path.isdir("trial_results/gt_artifacts") else None))
    trial_log = _read_trial_log(log_path)
    eval_no_report = _detect_eval_no_report(jobs)

    rec = build_signal_record(
        instance_id=instance_id,
        reward=reward,
        n_agent_steps=n_agent_steps,
        exit_status=exit_status,
        trial_log=trial_log,
        cert_dir=cert_dir,
        eval_no_report=eval_no_report,
    )
    print("--- FAILURE CLASSIFICATION ---")
    print(f"FAILURE_CLASS={rec['failure_class']}   "
          "(INFRA=pull/build/harness | GT=context wrong/absent | AGENT=model missed | RESOLVED=reward 1.0)")
    print(f"IN_RESOLVED_DENOMINATOR={rec['in_resolved_denominator']}   "
          "(INFRA/UNKNOWN are EXCLUDED from the resolved-rate denominator)")
    print(f"  signals: reward={rec['reward']} n_agent_steps={rec['n_agent_steps']} "
          f"gt_prebuilt_active={rec['gt_prebuilt_active']} hook_hash_match={rec['hook_hash_match']} "
          f"adapter_fail={rec['adapter_fail']} cert_fail={rec['cert_fail']} "
          f"infra_markers={rec['infra_markers']} eval_no_report={rec['eval_no_report']}")
    if rec["cert_verdicts"]:
        for name, cv in rec["cert_verdicts"].items():
            print(f"    cert {name}: verdict={cv['verdict']} pass={cv['pass']} is_fail={cv['is_fail']}")

    # Single-task run-level tally + paired-delta scaffold (no baseline supplied here).
    run_tally = tally_classes([rec])
    paired = build_paired_delta(rec, baseline=None)

    out = {
        "schema": "gt.deepswe_outcome.v1",
        "tasks": [rec],
        "run_tally": run_tally,
        "paired_delta": [paired],
    }
    out_path = os.environ.get("GT_DEEPSWE_OUTCOME_JSON")
    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(out, f, indent=2)
            print(f"  classification record -> {out_path}")
        except OSError as e:
            print(f"  WARN: could not persist classification record: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

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

- INFRA   — image-pull / substrate-pull / harness failure (adapter-wire is GT, not INFRA)
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
#: Fine-grained infra subtypes (CP008) — precedence before AGENT/GT when classifying.
INFRA_SUBTYPES: tuple[str, ...] = (
    "INFRA_ENOSPC",
    "INFRA_TRAJECTORY_FALLBACK",
    "INFRA_MISSING_ARTIFACT",
)

#: gt_run_proof PROOF_STAGES split by OWNERSHIP (DEFECT-2). A FAIL at a REAL
#: substrate/infra stage (env/dep_store/source_copy/index/lsp/graph_cert/gates) is a genuine
#: INFRA failure. The brief_emit / artifact_contract stages are GT-OUTPUT-QUALITY: an
#: empty/thin/degraded brief there is a GT-DELIVERY failure (kept in the resolved
#: denominator), NEVER environmental INFRA. Kept in sync with gt_run_proof.PROOF_STAGES.
_INFRA_PROOF_STAGES: frozenset[str] = frozenset({
    "env_validation", "dep_store", "source_copy", "workspace_metadata",
    "index", "lsp_pass", "graph_cert", "gates",
})
_BRIEF_QUALITY_PROOF_STAGES: frozenset[str] = frozenset({"brief_emit", "artifact_contract"})

#: The INFRA tokens a brief-quality outcome masquerades behind: GT_ARTIFACT_MISSING (the
#: empty/missing-brief marker) and the INFRA_MISSING_ARTIFACT subtype (the missing-trajectory
#: stub). When the proof record PROVES the failure is brief-quality (real infra stages all
#: ok), these are stripped and the task classifies GT (a delivery loss, kept in the resolved
#: denominator), never INFRA. A genuine infra marker/subtype still wins outright.
_BRIEF_QUALITY_INFRA_TOKENS: frozenset[str] = frozenset({"GT_ARTIFACT_MISSING"})
_BRIEF_QUALITY_INFRA_SUBTYPES: frozenset[str] = frozenset({"INFRA_MISSING_ARTIFACT"})

INFRA_LOG_MARKERS: tuple[str, ...] = (
    "GT_SUBSTRATE_DIGEST_MISSING",
    "GT_SUBSTRATE_PULL_FAIL",
    "GT_RUN_PROOF_FAIL",
    "GT_PROOF_OOM",
    # L3 (Pro eval): the task patch could not be applied (wrong cwd / non-applying patch).
    # The verifier never ran on patched code, so this is INFRA (no reward recorded, excluded
    # from the resolved denominator) — never a false agent-miss. Surfaced by swebench_pro_full.yml.
    "PATCH_APPLY_FAIL",
    # F4: the AGENT container hit its memory cap (mem_limit/memswap_limit == ${MEMORY},
    # symmetric to the proof container). A capacity kill (rc/exit 137), not an agent-logic
    # or GT-delivery failure — classified INFRA (excluded from the resolved-rate
    # denominator), the same treatment as GT_PROOF_OOM. Uncapped, this was a silent host
    # OOM-SIGKILL that surfaced as a generic failure; now it is a classified diagnostic.
    "GT_AGENT_OOM",
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


def detect_infra_subtype(jobs: str, trial_log: str = "") -> str | None:
    """Deterministic infra subtype from disk artifacts + trial log (CP008)."""
    log_lower = (trial_log or "").lower()
    if "no space left on device" in log_lower or "enospc" in log_lower:
        return "INFRA_ENOSPC"

    trials = sorted(glob.glob(os.path.join(jobs, "*", "*__*", "result.json")))
    mini_trajs = sorted(glob.glob(
        os.path.join(jobs, "*", "*__*", "agent", "mini-swe-agent.trajectory.json")
    ))
    canon_trajs = sorted(glob.glob(
        os.path.join(jobs, "*", "*__*", "agent", "trajectory.json")
    ))

    if not trials and not mini_trajs and not canon_trajs:
        return "INFRA_MISSING_ARTIFACT"

    for p in canon_trajs:
        try:
            if os.path.getsize(p) == 0 and mini_trajs:
                return "INFRA_TRAJECTORY_FALLBACK"
        except OSError:
            continue

    if trials and not mini_trajs and not canon_trajs:
        trial_dir = os.path.dirname(trials[-1])
        agent_dir = os.path.join(trial_dir, "agent")
        if not os.path.isdir(agent_dir):
            return "INFRA_MISSING_ARTIFACT"

    return None


def _infra_stages_ok(stages) -> bool:
    """No real substrate/infra proof stage is recorded as a failure (DEFECT-2)."""
    for row in stages or []:
        if (isinstance(row, dict)
                and row.get("stage") in _INFRA_PROOF_STAGES
                and row.get("status") == "fail"):
            return False
    return True


def detect_brief_quality_failure(cert_dir: str | None) -> bool:
    """True when the proof record PROVES the run's only failure/degrade is a GT
    BRIEF-QUALITY outcome (empty/thin/degraded brief at brief_emit/artifact_contract) while
    EVERY real substrate/infra stage passed (DEFECT-2).

    Such a task is a GT-DELIVERY failure, not an environmental INFRA failure — it stays IN
    the resolved denominator. This keys on the ACTUAL proof stages (proof_failure.json /
    proof_progress.json that gt-run-proof writes into GT_CERT_DIR), NEVER on GT's own brief
    outcome inferred from a missing-artifact glob. Fail-closed: no proof record, or ANY real
    infra stage recorded as failed, returns False (an INFRA classification stands).
    """
    if not cert_dir:
        return False
    # 1. A HARD proof failure whose failing stage is brief-quality, infra stages all ok.
    try:
        with open(os.path.join(cert_dir, "proof_failure.json"), encoding="utf-8") as fh:
            pf = json.load(fh)
    except (OSError, ValueError):
        pf = None
    if isinstance(pf, dict):
        stage = pf.get("stage")
        if stage in _INFRA_PROOF_STAGES:
            return False  # a genuine infra-stage failure — never reclassify as GT.
        if stage in _BRIEF_QUALITY_PROOF_STAGES and _infra_stages_ok(pf.get("stages")):
            return True
    # 2. No hard failure: a RECORDED brief-quality DEGRADE with infra stages all ok.
    try:
        with open(os.path.join(cert_dir, "proof_progress.json"), encoding="utf-8") as fh:
            pp = json.load(fh)
    except (OSError, ValueError):
        pp = None
    if isinstance(pp, dict):
        stages = pp.get("stages") or []
        degraded = any(
            isinstance(r, dict)
            and r.get("stage") in _BRIEF_QUALITY_PROOF_STAGES
            and str(r.get("detail") or "").upper().startswith("DEGRADED")
            for r in stages
        )
        if degraded and _infra_stages_ok(stages):
            return True
    return False


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
        v_upper = verdict.upper() if isinstance(verdict, str) else ""
        is_fail = (
            (passed is False)
            or ("FAIL" in v_upper)
            or v_upper in {"LSP_INSTALL_MISSING", "EMBEDDER_INSTALL_MISSING"}
        )
        out[name] = {"verdict": verdict, "pass": passed, "is_fail": is_fail}
    return out


def _any_cert_fail(certs: dict[str, dict]) -> bool:
    return any(c.get("is_fail") for c in certs.values())


#: §12 witness reconciliation (gt_gt.md:764): cert verdicts that are KNOWN FALSE FAILS
#: when the runtime witness holds. graph_certificate.json is written PRE-AGENT, so it
#: records hook_graph_hash=null / prebuilt_active=null and stamps
#: GRAPH_FAIL_MISSING_HANDOFF even when the handoff later succeeds; the [GT_META]
#: graph_witness (gt_prebuilt_active=true AND hook_graph_hash_matches_post_lsp=true)
#: PROVES the handoff. ONLY this verdict is reconcilable — every other failing verdict
#: (embedder/LSP/graph integrity fails) classifies GT regardless of the witness.
RECONCILABLE_CERT_VERDICTS: frozenset[str] = frozenset({"GRAPH_FAIL_MISSING_HANDOFF"})


def _witness_holds(rec: dict) -> bool:
    """The §12 runtime consumption witness: prebuilt graph consumed AND hash parity."""
    return (rec.get("gt_prebuilt_active") is True
            and rec.get("hook_hash_match") is True)


def _unreconciled_cert_fail(rec: dict) -> bool:
    """True when at least one failing cert is NOT explained away by the §12 witness.

    Fail-closed: if cert_fail is set but no per-cert verdict detail is available, the
    fail cannot be verified as the reconcilable pre-agent verdict -> it stands. If the
    witness does not hold, every cert fail stands. Otherwise only failing certs whose
    verdict is outside RECONCILABLE_CERT_VERDICTS keep the fail.
    """
    fails = [c for c in (rec.get("cert_verdicts") or {}).values() if c.get("is_fail")]
    if not fails:
        return bool(rec.get("cert_fail"))
    if not _witness_holds(rec):
        return True
    return any((c.get("verdict") or "") not in RECONCILABLE_CERT_VERDICTS for c in fails)


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
         post-LSP graph (hook_graph_hash_matches_post_lsp=false), or any cert FAILed —
         EXCEPT the §12-reconcilable GRAPH_FAIL_MISSING_HANDOFF when the runtime
         witness holds (gt_prebuilt_active ∧ hook_hash_match): that cert is written
         pre-agent and is a KNOWN FALSE FAIL (gt_gt.md:764) — fall through.
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
    # 1. INFRA — wins outright; the comparison was never clean. EXCEPT a GT brief-quality
    # outcome (empty/thin/degraded brief) that only *looks* like INFRA via the
    # GT_ARTIFACT_MISSING marker or the INFRA_MISSING_ARTIFACT missing-trajectory stub: when
    # the proof record proves every real infra stage passed (brief_quality_fail), that is a
    # GT-DELIVERY failure, not environmental INFRA. Strip ONLY those brief-quality tokens; a
    # genuine infra marker/subtype (pull/OOM/ENOSPC/env-start/…) still wins outright.
    brief_quality = bool(rec.get("brief_quality_fail"))
    infra_markers = rec.get("infra_markers") or []
    infra_subtype = rec.get("infra_subtype")
    if brief_quality:
        infra_markers = [m for m in infra_markers if m not in _BRIEF_QUALITY_INFRA_TOKENS]
        if infra_subtype in _BRIEF_QUALITY_INFRA_SUBTYPES:
            infra_subtype = None
    if infra_markers or rec.get("eval_no_report") or infra_subtype:
        return "INFRA"

    # 2. GT — delivery break. Adapter-wire fail, no consumption, hash divergence, cert FAIL.
    if rec.get("adapter_fail"):
        return "GT"
    if rec.get("gt_prebuilt_active") is False:
        return "GT"
    if rec.get("hook_hash_match") is False:
        return "GT"
    # §12 reconciliation (gt_gt.md:764): GRAPH_FAIL_MISSING_HANDOFF with the runtime
    # witness true (gt_prebuilt_active ∧ hook_hash_match) is a KNOWN FALSE FAIL — the
    # cert is pre-agent. Skip the GT stamp ONLY for that reconciled verdict and fall
    # through to the real outcome ladder; any unreconciled cert fail still returns GT.
    if rec.get("cert_fail") and _unreconciled_cert_fail(rec):
        return "GT"

    # 3. RESOLVED — the win.
    reward = rec.get("reward")
    if reward is not None and float(reward) >= 1.0:
        return "RESOLVED"

    # 3a. GT — a PROVEN brief-quality failure (empty/thin/degraded brief) that did not
    # resolve. GT produced no usable evidence; it is a GT loss, KEPT in the resolved
    # denominator (never dropped as INFRA/UNKNOWN). RESOLVED above still wins when the agent
    # solved the task despite the degraded brief.
    if brief_quality:
        return "GT"

    # 3b. GT — agent ran, did not resolve, and the consumption WITNESS IS MISSING
    # (gt_prebuilt_active unknown). Witness-absent = unproven consumption = GT's
    # problem; it counts in the resolved denominator (never UNKNOWN-excluded).
    steps = rec.get("n_agent_steps")
    if (rec.get("gt_prebuilt_active") is None
            and isinstance(steps, int) and steps > 0
            and reward is not None and float(reward) < 1.0):
        return "GT"

    # 4. AGENT — sound GT context, agent ran, did not resolve. A §12-reconciled cert
    # false-fail counts as sound context (the witness proved the handoff).
    if (rec.get("gt_prebuilt_active") is True
            and not _unreconciled_cert_fail(rec)
            and isinstance(steps, int) and steps > 0
            and reward is not None and float(reward) < 1.0):
        return "AGENT"

    # 5. UNKNOWN — not enough signal to attribute.
    return "UNKNOWN"


#: Classes excluded from the resolved-rate denominator. INFRA failures (and harness
#: no-report) are NOT agent or GT failures — counting them would defame both. UNKNOWN is
#: also excluded (un-attributable -> cannot be charged to GT or the agent).
DENOMINATOR_EXCLUDED: frozenset[str] = frozenset({"INFRA", "UNKNOWN"})


def unknown_reason(rec: dict) -> str | None:
    """P2-08 — explicit reason when failure_class is UNKNOWN."""
    if rec.get("failure_class") != "UNKNOWN":
        return None
    if rec.get("reward") is None and not rec.get("n_agent_steps"):
        return "no_reward_and_no_agent_steps"
    if rec.get("reward") is None:
        return "missing_reward"
    steps = rec.get("n_agent_steps")
    if not isinstance(steps, int) or steps <= 0:
        return "agent_never_ran"
    if rec.get("gt_prebuilt_active") is None and not rec.get("gt_meta_present"):
        return "witness_absent_but_steps_present"
    return "unclassified_signal_gap"


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

def extract_instance_id(d: dict, info: dict, trial_dir: str | None = None) -> str | None:
    """Resolve the pairing key (instance_id) from whatever the result shape carries.

    Why: the pier/DeepSWE per-trial result.json has NO `instance_id` and NO `info`
    block — the old extractor looked only there and returned null for every task,
    making the paired Wilcoxon (keyed by instance_id) impossible. The identity the
    trial DOES carry: `task_name` ("org/<slug>"), `task_id.path` (".../tasks/<slug>"),
    and the trial dir / `trial_name` ("<slug>__<hash>", slug TRUNCATED by pier — last
    resort only). Priority (first hit wins, deterministic, identical for every task):
      1. explicit `instance_id` (result top-level / info / info.instance) — VERBATIM
         (SWE-bench ids like `astropy__astropy-12907` legitimately contain `__`).
      2. `task_name` -> last path segment (the task slug, the run-set key).
      3. `task_id.path` -> last path segment.
      4. `trial_name` / trial dir name -> trailing `__<attempt-hash>` stripped (the
         hash varies per trial and would break GT-on-vs-baseline pairing).
    """
    for src in (d, info):
        v = src.get("instance_id")
        if isinstance(v, str) and v.strip():
            return v.strip()
    inst = info.get("instance")
    if isinstance(inst, dict):
        v = inst.get("instance_id")
        if isinstance(v, str) and v.strip():
            return v.strip()
    v = d.get("task_name")
    if isinstance(v, str) and v.strip():
        return v.strip().rstrip("/").rsplit("/", 1)[-1]
    tid = d.get("task_id")
    if isinstance(tid, dict):
        p = tid.get("path")
        if isinstance(p, str) and p.strip():
            return p.strip().rstrip("/").rsplit("/", 1)[-1]
    base = os.path.basename(trial_dir.rstrip("/\\")) if trial_dir else None
    for v in (d.get("trial_name"), base):
        if isinstance(v, str) and v.strip():
            v = v.strip()
            return v.rsplit("__", 1)[0] if "__" in v else v
    return None


# ===========================================================================
# Harness-truth binding (run 29236533134 defect-1): on the mini-swe-agent path
# the pier result.json/instance_id is ABSENT, so instance_id came through null on
# 29/29 tasks and every task auto-labelled INFRA_MISSING_ARTIFACT even though a
# report.json + reward + trajectory were present at the TASK-ROOT. These pure
# helpers recover the instance identity from the three durable sources the run
# ALWAYS carries — the run matrix env, the eval report.json key, the task dir
# name — and answer the report-authoritative resolved verdict. Shared with
# task_truth.build_task_truth (one-directional import: task_truth -> here).
# ===========================================================================

#: Report/reward file names probed at each task-root candidate (mini + CI shapes).
_REPORT_NAMES: tuple[str, ...] = ("report.json", os.path.join("trial_results", "report.json"))
_REWARD_NAMES: tuple[str, ...] = ("reward.txt", os.path.join("trial_results", "reward.txt"))


def _root_candidates(jobs: str) -> list[str]:
    """Task-root dirs to probe for report.json/reward.txt: the jobs dir, its parent
    (the mini task root — ``task_truth.py jobs`` runs from the task dir), the cwd, and
    an explicit GT_TASK_DIR override. Deduped, order-preserving."""
    roots: list[str] = []
    for r in (
        jobs,
        os.path.dirname(os.path.abspath(jobs)) if jobs else "",
        os.getcwd(),
        os.environ.get("GT_TASK_DIR", ""),
    ):
        if r and r not in roots:
            roots.append(r)
    return roots


def instance_id_from_env() -> str | None:
    """The run matrix supplies the instance id in env (the workflow reconcile step
    exports GT_INSTANCE_ID / GT_MATRIX_TASK). First non-blank wins."""
    for key in ("GT_INSTANCE_ID", "GT_MATRIX_TASK", "GT_TASK_ID"):
        v = os.environ.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def find_task_root_report(jobs: str) -> tuple[str | None, dict | None]:
    """Locate the eval report.json at a task-root candidate. Returns (path, dict) or
    (None, None). The report is the SWE-bench harness output — a dict keyed by
    instance_id -> {resolved, tests_status, ...}."""
    for root in _root_candidates(jobs):
        for name in _REPORT_NAMES:
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                try:
                    with open(cand, encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict) and data:
                    return cand, data
    return None, None


def instance_id_from_report(report: dict | None, hint: str | None = None) -> str | None:
    """The report is keyed by instance_id. Prefer a key matching the hint; else the
    sole key (unambiguous per-task report)."""
    if not isinstance(report, dict) or not report:
        return None
    keys = [k for k in report if isinstance(k, str) and k]
    if hint and hint in keys:
        return hint
    if len(keys) == 1:
        return keys[0]
    return None


def task_dir_instance_id(jobs: str) -> str | None:
    """The task-root directory name IS the instance id in the captured/CI layout
    (e.g. .../conan-io__conan-17123/jobs). Never returns a bare 'jobs'/'.' stem."""
    for root in _root_candidates(jobs):
        base = os.path.basename(os.path.abspath(root).rstrip("/\\"))
        if base and base not in ("jobs", "trial_results", ".", ""):
            return base
    return None


def report_resolved(report: dict | None, iid: str | None) -> bool | None:
    """The report's resolved verdict for this instance (True/False), or None when the
    report is absent / carries no resolved field (missing-data law — never a false 0)."""
    if not isinstance(report, dict) or not report:
        return None
    entry = None
    if iid and iid in report:
        entry = report[iid]
    elif len(report) == 1:
        entry = next(iter(report.values()))
    if isinstance(entry, dict) and "resolved" in entry:
        return bool(entry.get("resolved"))
    return None


def root_reward(jobs: str) -> float | None:
    """The task-root reward.txt (mini path / CI bridge), parsed as float. None when
    absent/blank (missing-data law)."""
    for root in _root_candidates(jobs):
        for name in _REWARD_NAMES:
            cand = os.path.join(root, name)
            if os.path.isfile(cand):
                try:
                    txt = open(cand, encoding="utf-8").read().strip()
                    return float(txt) if txt else None
                except (OSError, ValueError):
                    return None
    return None


def root_agent_steps(jobs: str) -> int | None:
    """Agent step count from the task-root mini trajectory (info.model_stats.api_calls,
    else the assistant-turn count). The mini path carries NO pier n_agent_steps, so
    without this a report-graded reward=0 run classifies UNKNOWN (denominator-excluded)
    instead of AGENT/GT. None when no trajectory is found (missing-data law)."""
    for root in _root_candidates(jobs):
        cand = os.path.join(root, "mini-swe-agent.trajectory.json")
        if not os.path.isfile(cand):
            continue
        try:
            with open(cand, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        api = ((d.get("info") or {}).get("model_stats") or {}).get("api_calls")
        if isinstance(api, int) and api > 0:
            return api
        msgs = d.get("messages") or []
        assist = sum(
            1 for m in msgs if isinstance(m, dict)
            and (m.get("role") == "assistant"
                 or (m.get("role") is None and isinstance(m.get("output"), list)))
        )
        return assist or None
    return None


def bind_instance_identity(
    jobs: str, *, current: str | None, report: dict | None = None
) -> str | None:
    """Resolve the pairing key from every durable source, most-specific first:
    already-resolved -> report.json key -> matrix env -> task dir name. Pure; never
    fabricates. (result.json/trajectory-info ids are resolved by the caller BEFORE
    this and passed as ``current``.)"""
    if current:
        return current
    from_report = instance_id_from_report(report, current)
    if from_report:
        return from_report
    from_env = instance_id_from_env()
    if from_env:
        return from_env
    return task_dir_instance_id(jobs)


def build_signal_record(
    *,
    instance_id: str | None,
    reward: float | None,
    n_agent_steps: int | None,
    exit_status: str | None,
    trial_log: str,
    cert_dir: str | None,
    eval_no_report: bool = False,
    infra_subtype: str | None = None,
    brief_quality_fail: bool | None = None,
) -> dict:
    """Assemble the per-task signal record + classify it.

    Reads ONLY signals the extractor already parses: reward, n_agent_steps,
    exit_status, the [GT_META] witness fields (from trial_log), the cert verdicts
    (from cert_dir), the §E infra/adapter markers (from trial_log), and — for DEFECT-2 —
    the proof-stage record (from cert_dir) that distinguishes a real infra-stage failure
    from a GT brief-quality outcome. ``brief_quality_fail`` defaults to auto-detection from
    the proof record; pass an explicit bool to override (tests).
    """
    infra_markers = find_infra_markers(trial_log)
    adapter_fail = GT_ADAPTER_FAIL_MARKER in trial_log
    meta = _gt_meta_witness(trial_log)
    certs = collect_cert_verdicts(cert_dir)
    if brief_quality_fail is None:
        brief_quality_fail = detect_brief_quality_failure(cert_dir)

    rec = {
        "instance_id": instance_id,
        "reward": reward,
        "n_agent_steps": n_agent_steps,
        "exit_status": exit_status,
        "infra_markers": infra_markers,
        "infra_subtype": infra_subtype,
        "eval_no_report": bool(eval_no_report),
        "adapter_fail": adapter_fail,
        "gt_prebuilt_active": _to_bool(meta.get("gt_prebuilt_active")),
        "hook_hash_match": _to_bool(meta.get("hook_graph_hash_matches_post_lsp")),
        "cert_verdicts": certs,
        "cert_fail": _any_cert_fail(certs),
        "gt_meta_present": bool(meta),
        "brief_quality_fail": bool(brief_quality_fail),
    }
    # Transparency: was a raw cert_fail reconciled away by the §12 runtime witness?
    rec["cert_fail_reconciled"] = bool(rec["cert_fail"]) and not _unreconciled_cert_fail(rec)
    rec["failure_class"] = classify_outcome(rec)
    rec["in_resolved_denominator"] = is_in_resolved_denominator(rec["failure_class"])
    rec["unknown_reason"] = unknown_reason(rec)
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
    exc_type: str | None = None

    trials = sorted(glob.glob(os.path.join(jobs, "*", "*__*", "result.json")))
    if not trials:
        print("AGENT_RAN_STEPS=UNKNOWN  -- no trial result.json (harness broke before the agent?)")
    else:
        d = json.load(open(trials[-1], encoding="utf-8"))
        info = d.get("info") or {}
        vr = d.get("verifier_result") or {}
        exc = d.get("exception_info")
        exc_type = (exc or {}).get("exception_type")
        n_agent_steps = d.get("n_agent_steps")
        exit_status = traj_info.get("exit_status") or info.get("exit_status")
        reward = (vr.get("rewards") or {}).get("reward")
        instance_id = extract_instance_id(d, info,
                                          trial_dir=os.path.dirname(trials[-1]))
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
    infra_subtype = detect_infra_subtype(jobs, trial_log)
    # A pier environment/container START failure (EnvironmentStartTimeoutError &c) means the eval
    # CONTAINER never came up: the agent never ran and GT never delivered into it. That is a
    # harness/INFRA failure (excluded from the resolved denominator), NOT a GT-delivery break —
    # without this, the pre-agent GRAPH_FAIL_MISSING_HANDOFF cert cannot reconcile (no runtime
    # witness, gt_prebuilt_active=None) and the task is wrongly stamped GT (the goreleaser blip).
    # Under the OOM fix (overlay2 + MemoryHigh throttle) a giant image's extraction can
    # occasionally cross pier's start timeout — a flaky infra event, never a GT/agent fault.
    if not infra_subtype and exc_type and "environmentstart" in exc_type.replace("_", "").lower():
        infra_subtype = "INFRA_ENV_START_TIMEOUT"

    # ── Harness-truth binding (defect-1) ───────────────────────────────────
    # On the mini path there is no pier result.json, so instance_id/reward came
    # through null and the pier-layout globs above stamped INFRA_MISSING_ARTIFACT.
    # Recover identity from the report.json key / matrix env / task dir name, take
    # the reward from report.json/reward.txt, and DROP a MISSING-artifact verdict
    # whenever a report OR a trajectory is present (INFRA requires BOTH absent).
    report_path, report = find_task_root_report(jobs)
    instance_id = bind_instance_identity(jobs, current=instance_id, report=report)
    if reward is None:
        reward = root_reward(jobs)
    _rep_resolved = report_resolved(report, instance_id)
    if reward is None and _rep_resolved is not None:
        reward = 1.0 if _rep_resolved else 0.0
    if n_agent_steps is None:
        n_agent_steps = root_agent_steps(jobs)
    # The eval report.json is the grading authority: when present (or a genuine resolve),
    # grade from it — never INFRA_MISSING. A task with NO report AND reward<1 (no eval
    # verdict, e.g. checkov-6893) stays true INFRA; trajectory presence alone does not
    # manufacture a verdict.
    _genuine_resolve = reward is not None and float(reward) >= 1.0
    if (infra_subtype and "MISSING" in str(infra_subtype).upper()
            and (report is not None or _genuine_resolve)):
        print(f"INFRA_MISSING_ARTIFACT downgraded — graded from the eval report "
              f"(report_present={report is not None}, instance_id={instance_id}, "
              f"reward={reward}, n_agent_steps={n_agent_steps})")
        infra_subtype = None

    rec = build_signal_record(
        instance_id=instance_id,
        reward=reward,
        n_agent_steps=n_agent_steps,
        exit_status=exit_status,
        trial_log=trial_log,
        cert_dir=cert_dir,
        eval_no_report=eval_no_report,
        infra_subtype=infra_subtype,
    )
    print("--- FAILURE CLASSIFICATION ---")
    print(f"FAILURE_CLASS={rec['failure_class']}   "
          "(INFRA=pull/build/harness | GT=context wrong/absent | AGENT=model missed | RESOLVED=reward 1.0)")
    if rec.get("infra_subtype"):
        print(f"INFRA_SUBTYPE={rec['infra_subtype']}")
    print(f"IN_RESOLVED_DENOMINATOR={rec['in_resolved_denominator']}   "
          "(INFRA/UNKNOWN are EXCLUDED from the resolved-rate denominator)")
    print(f"  signals: reward={rec['reward']} n_agent_steps={rec['n_agent_steps']} "
          f"gt_prebuilt_active={rec['gt_prebuilt_active']} hook_hash_match={rec['hook_hash_match']} "
          f"adapter_fail={rec['adapter_fail']} cert_fail={rec['cert_fail']} "
          f"brief_quality_fail={rec['brief_quality_fail']} "
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

    # CP006 — per-task truth ledger beside the trial.
    try:
        import importlib.util

        tt_path = os.path.join(os.path.dirname(__file__), "..", "swebench", "task_truth.py")
        spec = importlib.util.spec_from_file_location("task_truth_do", tt_path)
        tt_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tt_mod)
        truth_path = tt_mod.write_task_truth(
            jobs,
            trial_log=trial_log,
            cert_dir=cert_dir,
        )
        print(f"  task_truth.json -> {truth_path}")
    except Exception as exc:  # noqa: BLE001 — best-effort adjunct
        print(f"  WARN: could not write task_truth.json: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

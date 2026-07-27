#!/usr/bin/env python3
"""gt_run_check.py -- detailed, taxonomy-aware status checker for GroundTruth GHA runs.

WHY THIS EXISTS (vs `gh run view`): a run can be "success" while GT delivered nothing,
and "failure" for reasons that are expected and fine. This checker reports, in one
compact block designed to be re-read every ~7 minutes:

  1. Run identity (id / workflow / status / conclusion / SHA / queued+elapsed / URL).
  2. Per-job status; for anything not successful, the FAILING STEP NAME.
  3. LIVE annotations per failing/in-progress job via the checks API
     (`repos/{repo}/check-runs/{job_id}/annotations`) -- readable DURING the run.
  4. A TAXONOMY verdict per failing job (CLAUDE.md operating law):
        TASK          agent ran, did not solve -- the EXPECTED denominator, never a problem
        INFRA         image pull / env / OOM / registry -- environmental, retry-class
        METRICS/PROOF GT self-measurement gate tripped -- matters only on real breakage
        PRODUCT       GT delivered wrong/late/leaked evidence -- the only real GT bug
        (+ CANCELLED / UNKNOWN when the evidence does not decide)
     Each verdict carries the evidence string that decided it.
  5. With --artifacts: downloads run artifacts to a local cache and reports GT DELIVERY
     EVIDENCE from gt_runtime_ledger_*.jsonl -- outcome counts, real deliveries
     (chars_delivered > 0), layer / fact_class / contracted_boundary breakdowns
     (new fields; absence on older runs is REPORTED, never an error), on-time analysis
     against the contracted boundary, temporal_relation counts, leak check, and the
     HEADLINE: canonical/capsule rows (zero rows = the oracle released nothing, even
     if bytes shipped through legacy layers).
  6. Per-task resolution from outcome.json / task_truth.json when present.

All GitHub API calls go through `bash scripts/ghh.sh` (per-invocation harneet2512
token) -- never bare `gh`. Works mid-run (partial data) and post-run. Never crashes on
missing files/fields: absent data is reported as absent.

Usage:
    python scripts/swebench/gt_run_check.py <run_id> [--repo harneet2512/groundtruth]
        [--artifacts] [--tmp D:/tmp/gt_run_check] [--max-artifact-mb 512]
        [--max-annotation-jobs 20] [--tasks id1,id2]

Artifact zips/extractions are cached under <tmp>/<run_id>/ keyed by artifact id, so a
7-minute polling loop re-downloads nothing that is already on disk.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GHH = "scripts/ghh.sh"

# ---------------------------------------------------------------------------
# ghh.sh subprocess plumbing (Windows-safe: find a bash that can see `gh`)
# ---------------------------------------------------------------------------
_BASH: str | None = None
_API_ERRORS: list[str] = []


def _find_bash() -> str:
    """Locate a bash that (a) runs and (b) has `gh` on its PATH (Git Bash, not WSL)."""
    global _BASH
    if _BASH:
        return _BASH
    candidates = [
        "bash",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    for cand in candidates:
        try:
            probe = subprocess.run(
                [cand, "-c", "command -v gh"],
                capture_output=True, timeout=30, cwd=str(REPO_ROOT),
            )
            if probe.returncode == 0 and probe.stdout.strip():
                _BASH = cand
                return cand
        except Exception:
            continue
    # Last resort: plain bash even if the probe failed; the first API call will
    # surface the real error message instead of dying here.
    _BASH = "bash"
    return _BASH


def ghh(args: list[str], timeout: int = 120) -> tuple[int, bytes, str]:
    """Run `bash scripts/ghh.sh <args>` from the repo root. Returns (rc, stdout, stderr)."""
    cmd = [_find_bash(), GHH, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(REPO_ROOT))
        return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, b"", f"timeout after {timeout}s: {' '.join(args[:4])}"
    except Exception as exc:  # noqa: BLE001 -- a checker must report, not crash
        return 125, b"", f"{type(exc).__name__}: {exc}"


def api_json(path: str, timeout: int = 120):
    """GET one API path via ghh; None on any failure (recorded in _API_ERRORS)."""
    rc, out, err = ghh(["api", path], timeout=timeout)
    if rc != 0:
        _API_ERRORS.append(f"api {path.split('?')[0]} rc={rc} {err.strip()[:200]}")
        return None
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        _API_ERRORS.append(f"api {path.split('?')[0]} unparseable JSON: {exc}")
        return None


def api_paginated(path_fmt: str, key: str, page_cap: int = 12) -> list | None:
    """Collect `key` arrays across ?per_page=100&page=N pages. None = first call failed."""
    items: list = []
    for page in range(1, page_cap + 1):
        sep = "&" if "?" in path_fmt else "?"
        data = api_json(f"{path_fmt}{sep}per_page=100&page={page}")
        if data is None:
            return None if page == 1 else items
        batch = data.get(key) or []
        items.extend(batch)
        if len(batch) < 100:
            break
    return items


# ---------------------------------------------------------------------------
# small utils
# ---------------------------------------------------------------------------
def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _dur(a: datetime | None, b: datetime | None) -> str:
    if not a or not b:
        return "?"
    secs = int((b - a).total_seconds())
    if secs < 0:
        return "?"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _one_line(text: str, cap: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[: cap - 3] + "..." if len(text) > cap else text


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _fmt_counter(c: Counter, cap: int = 14) -> str:
    items = c.most_common()
    body = " ".join(f"{k}={v}" for k, v in items[:cap])
    if len(items) > cap:
        body += f" (+{len(items) - cap} more)"
    return body or "-"


# ---------------------------------------------------------------------------
# taxonomy classification
# ---------------------------------------------------------------------------
TAX_TASK = "TASK"
TAX_INFRA = "INFRA"
TAX_PROOF = "METRICS/PROOF"
TAX_PRODUCT = "PRODUCT"
TAX_CANCELLED = "CANCELLED"
TAX_UNKNOWN = "UNKNOWN"

# Ordered (most-specific first). Scanned against failing-step names + annotations.
_MESSAGE_RULES: list[tuple[str, str]] = [
    # METRICS/PROOF -- GT's own self-measurement / proof gates
    (r"GT_SUBSTRATE_DIGEST_MISSING", TAX_PROOF),
    (r"industrial(ly)?\s+SOTA|industrially INVALID|INDUSTRIAL_GATE_FAILED", TAX_PROOF),
    (r"RUN_SET_DRIFT", TAX_PROOF),
    (r"COMMIT[_ ]PARITY|commit parity|stale-?bake", TAX_PROOF),
    (r"foundational[_ ]gate|GT_RUN_PROOF_FAIL|proof_failure|proof gate", TAX_PROOF),
    (r"dose_lte_one|ss_gate|incompatible lever|rc=78", TAX_PROOF),
    (r"GT_ARTIFACT_MISSING", TAX_PROOF),
    (r"cost guard|unbounded cost forbidden|workflow_lint|workflow lint", TAX_PROOF),
    (r"[Ll]egitimacy (guard|OK)?.*graph\.db|benchmark-shape", TAX_PROOF),
    # INFRA -- environmental / retry-class
    (r"lost communication with the server|shutdown signal|runner .{0,40}unavailab", TAX_INFRA),
    (r"[Nn]o space left on device|disk quota", TAX_INFRA),
    (r"\bOOM\b|[Oo]ut of memory|oom-?kill|exit code 137|Killed\b", TAX_INFRA),
    (r"Cannot connect to the Docker daemon|docker daemon|dockerd", TAX_INFRA),
    (r"(image|manifest).{0,40}(pull|unknown|not found)|pull access denied|toomanyrequests", TAX_INFRA),
    (r"TASK_IMAGE_PULL_FAIL|GT_SUBSTRATE_PULL_FAIL", TAX_INFRA),
    (r"ghcr\.io.{0,60}(denied|fail|error|unauthorized)|registry .{0,30}(error|unavailable)", TAX_INFRA),
    (r"TLS handshake|connection (reset|refused)|ETIMEDOUT|ECONNRESET|Could not resolve host", TAX_INFRA),
    (r"envstart|swerex|env start|environment start", TAX_INFRA),
    (r"eval_no_report|HTTP (429|5\d\d)|rate limit", TAX_INFRA),
    (r"LFS.{0,30}(quota|bandwidth)|failed to fetch some objects", TAX_INFRA),
    (r"[Cc]ache.{0,40}(restore|service).{0,40}(fail|error)", TAX_INFRA),
    # summarize job's systemic verdict: launch failures across the matrix = INFRA
    (r"0 agents ran|launch-fail \d+ *>|INFRA/UNKNOWN \d+ *>", TAX_INFRA),
    # PRODUCT -- GT delivered wrong/late/leaked evidence
    (r"DEEPSWE_ADAPTER_FAIL|adapter[_ ]fail", TAX_PRODUCT),
    (r"leak(ed|age)?\b(?!.{0,12}guard)", TAX_PRODUCT),
    (r"gt_prebuilt_active=false|hook.{0,30}hash.{0,20}mismatch", TAX_PRODUCT),
    # TASK -- agent ran, did not solve (the expected denominator)
    (r"reward=0(\.0+)?\b|REWARD=0", TAX_TASK),
    (r"cost[_ ]limit|LimitsExceeded|step limit (reached|exceeded)", TAX_TASK),
    (r"unresolved|tests? failed|FAIL_TO_PASS", TAX_TASK),
]

# Step-NAME fallbacks when no message matched (weaker evidence, still stated).
_STEPNAME_RULES: list[tuple[str, str]] = [
    (r"gate|proof|precheck|parity|lint|guard|legitimacy|cost guard", TAX_PROOF),
    (r"pull|image|substrate|cache|restore|checkout|setup|install|login|venv|docker|compose|start .*env", TAX_INFRA),
]


def classify_job(job: dict, ann_msgs: list[str], outcome_rec: dict | None) -> tuple[str, str]:
    """(taxonomy, evidence) for one non-successful job."""
    concl = (job.get("conclusion") or "").lower()
    # 1) per-task outcome.json is the authority when we have it
    if outcome_rec:
        fc = outcome_rec.get("failure_class")
        ev = (f"outcome.json failure_class={fc} reward={outcome_rec.get('reward')} "
              f"n_agent_steps={outcome_rec.get('n_agent_steps')} "
              f"infra_subtype={outcome_rec.get('infra_subtype')}")
        if fc == "INFRA":
            return TAX_INFRA, ev
        if fc == "GT":
            return TAX_PRODUCT, ev
        if fc == "AGENT":
            return TAX_TASK, ev
        # RESOLVED/UNKNOWN: the job red is NOT the task outcome -> keep scanning.
    # 2) message rules over failing-step names + annotation texts
    fail_steps = [s.get("name", "") for s in (job.get("steps") or [])
                  if (s.get("conclusion") or "") in ("failure", "timed_out")]
    corpus: list[tuple[str, str]] = (
        [("step", s) for s in fail_steps] + [("annotation", m) for m in ann_msgs]
    )
    for src, text in corpus:
        for pat, tax in _MESSAGE_RULES:
            m = re.search(pat, text)
            if m:
                return tax, f"{src}::{_one_line(text, 160)} [matched /{pat[:40]}/]"
    if concl == "cancelled":
        return TAX_CANCELLED, ("conclusion=cancelled (user stop or runner eviction; "
                               "INFRA-class for retry math, never a GT defect)")
    if concl == "timed_out":
        return TAX_INFRA, "conclusion=timed_out (GHA wall-clock cut = environmental)"
    # 3) step-name fallback
    for s in fail_steps:
        low = s.lower()
        for pat, tax in _STEPNAME_RULES:
            if re.search(pat, low):
                return tax, f"step-name::'{s}' (no message marker; name-class match)"
    hard_msgs = [m for m in ann_msgs if m.startswith("failure:")
                 and "exit code" not in m] or ann_msgs
    quoted = f"; first annotation: {_one_line(hard_msgs[0], 140)}" if hard_msgs else \
        "; no annotations surfaced"
    if fail_steps:
        return TAX_UNKNOWN, (f"failing step '{fail_steps[0]}': no rule matched "
                             f"{len(ann_msgs)} annotation message(s){quoted}")
    return TAX_UNKNOWN, f"job failed with no failing step recorded{quoted}"


# ---------------------------------------------------------------------------
# artifact download + extraction (cached by artifact id)
# ---------------------------------------------------------------------------
def fetch_artifacts(repo: str, arts: list[dict], base: Path, max_mb: int,
                    task_filter: set[str] | None, notes: list[str]) -> list[tuple[str, Path]]:
    """Download+extract each artifact once; returns [(artifact_name, extract_dir)]."""
    zips = base / "zips"
    xroot = base / "x"
    zips.mkdir(parents=True, exist_ok=True)
    xroot.mkdir(parents=True, exist_ok=True)
    got: list[tuple[str, Path]] = []
    for art in arts:
        name = art.get("name") or f"artifact-{art.get('id')}"
        aid = art.get("id")
        if task_filter and not any(t in name for t in task_filter):
            continue
        if art.get("expired"):
            notes.append(f"artifact '{name}': EXPIRED on GitHub -- not downloadable")
            continue
        size = int(art.get("size_in_bytes") or 0)
        if size > max_mb * 1024 * 1024:
            notes.append(f"artifact '{name}': {size / 1048576:.0f} MB > --max-artifact-mb "
                         f"{max_mb} -- skipped")
            continue
        zf = zips / f"{aid}_{_safe_name(name)}.zip"
        if not (zf.exists() and zf.stat().st_size > 0):
            rc, out, err = ghh(
                ["api", f"repos/{repo}/actions/artifacts/{aid}/zip"], timeout=600)
            if rc != 0 or not out:
                notes.append(f"artifact '{name}': download failed rc={rc} "
                             f"{_one_line(err, 120)}")
                continue
            # Never cache an API error body as a fake zip (it would poison every
            # later poll). Real zips start 'PK'; some artifacts legitimately hold
            # other bytes (e.g. buildx .dockerbuild gzip) -- keep those.
            if out[:1] == b"{" and b'"message"' in out[:400]:
                notes.append(f"artifact '{name}': API error body "
                             f"{_one_line(out[:200].decode('utf-8', 'replace'), 120)}")
                continue
            zf.write_bytes(out)
        xdir = xroot / _safe_name(name)
        marker = xdir / f".gt_extracted_{aid}"
        if not marker.exists():
            try:
                xdir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zf) as z:
                    z.extractall(xdir)
                marker.write_text("ok", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"artifact '{name}': unzip failed: {exc}")
                continue
        got.append((name, xdir))
    return got


# ---------------------------------------------------------------------------
# runtime-ledger analysis
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------
# THE VALID ORACLE MEASUREMENT.
#
# Counting canonical/capsule LEDGER rows has NO discriminating power: the oracle's SUCCESS
# path writes nothing to that JSONL, its real record is a RuntimeJournal SQLite the workflow
# did not upload, and nothing anywhere writes a layer containing "capsule". The same zero is
# produced by a perfect oracle and by one that never installed.
#
# A RELEASED capsule is rendered with the header f"[GroundTruth . {decision_context}]" and
# injected as a user message, so it lands in the agent trajectory. Legacy GT bytes carry
# "<gt-" markers in the SAME files, which makes them a POSITIVE CONTROL: if legacy markers
# are present and capsule headers are absent, the sink is demonstrably live and the oracle
# demonstrably released nothing. Without that control an absence would be unreadable.
# ---------------------------------------------------------------------------------------
def analyze_trajectories(root: Path) -> dict:
    files = capsules = legacy = 0
    for path in root.rglob("*trajectory*.json"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        capsules += text.count("[GroundTruth ")
        legacy += text.count("<gt-")
    return {"files": files, "capsule_headers": capsules, "legacy_markers": legacy}


def format_trajectory_verdict(t: dict) -> str:
    if not t["files"]:
        return "TRAJECTORY: no trajectory files in artifacts -- oracle release NOT MEASURABLE"
    if t["capsule_headers"]:
        return (f"TRAJECTORY: {t['capsule_headers']} capsule header(s) across {t['files']} "
                f"file(s) -- THE ORACLE RELEASED (legacy markers {t['legacy_markers']})")
    if not t["legacy_markers"]:
        return (f"TRAJECTORY: 0 capsule headers AND 0 legacy markers across {t['files']} "
                "file(s) -- NO positive control, absence is UNREADABLE")
    return (f"TRAJECTORY: 0 capsule headers vs {t['legacy_markers']} legacy marker(s) across "
            f"{t['files']} file(s) -- sink is LIVE, oracle released NOTHING")


def analyze_ledger(path: Path) -> dict:
    """Parse one gt_runtime_ledger*.jsonl into the delivery-evidence summary."""
    rows: list[dict] = []
    bad = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                    else:
                        bad += 1
                except Exception:  # noqa: BLE001
                    bad += 1
    except Exception as exc:  # noqa: BLE001
        return {"error": f"unreadable: {exc}", "path": str(path)}

    outcomes = Counter(str(r.get("outcome")) for r in rows)
    delivered_any = [r for r in rows if r.get("outcome") == "delivered"]
    real = [r for r in delivered_any if (r.get("chars_delivered") or 0) > 0]
    empty_delivered = len(delivered_any) - len(real)

    has_fact = any("fact_class" in r for r in rows)
    has_boundary = any("contracted_boundary" in r for r in rows)

    layers = Counter(str(r.get("layer")) for r in real)
    fact = Counter(str(r.get("fact_class")) if r.get("fact_class") else "(absent)"
                   for r in real)
    boundary = Counter(str(r.get("contracted_boundary")) if r.get("contracted_boundary")
                       else "(absent)" for r in real)

    # ON-TIME: only rows carrying BOTH identity fields are adjudicable.
    both = [r for r in real if r.get("fact_class") and r.get("contracted_boundary")]
    on_time = 0
    off: list[tuple] = []
    indeterminate = 0
    for r in both:
        actual = r.get("actual_event")
        if not actual:
            indeterminate += 1
        elif str(actual) == str(r.get("contracted_boundary")):
            on_time += 1
        else:
            off.append((r.get("fact_class"), r.get("contracted_boundary"),
                        actual, r.get("layer"), r.get("iteration")))

    temporal = Counter(str(r.get("temporal_relation")) for r in rows
                       if "temporal_relation" in r)

    canonical = [r for r in rows
                 if "canonical" in str(r.get("layer") or "").lower()
                 or "capsule" in str(r.get("layer") or "").lower()]
    canonical_delivered = [r for r in canonical
                           if r.get("outcome") == "delivered"
                           and (r.get("chars_delivered") or 0) > 0]
    canon_detail = Counter(
        f"{r.get('layer')}:{r.get('outcome')}:{_one_line(str(r.get('reason') or ''), 60)}"
        for r in canonical)

    # WHY A CAPSULE COUNT IS THE WRONG HEADLINE (user directive 2026-07-27).
    #
    # select_evidence_coalition selects "one SMALLEST connected, decision-complete evidence
    # coalition" and HOLDS the rest, at most one dose per observation. Silence is therefore
    # the CORRECT default, and a high canonical-row count would be a failure, not a win. The
    # signal is not how often the oracle spoke -- it is WHY it stayed quiet:
    #
    #   no compilation attempt at all   -> no decision was open. Correct-quiet.
    #   DECISION_INCOMPLETE, held > 0   -> THE DEFECT. Evidence existed and no anchor role
    #                                      could carry it, so nothing could compose
    #                                      (anchor starvation).
    #   DECISION_INCOMPLETE, held == 0  -> no evidence at all: an upstream producer gap,
    #                                      a different bug with a different fix.
    #   COMPILED more than once for one observation -> failure the other way (dose>1).
    compilations = [r for r in canonical
                    if "compilation" in str(r.get("layer") or "").lower()]
    compile_reasons = Counter(
        _one_line(str(r.get("reason") or "(none)"), 48) for r in compilations)
    def _held(row: dict) -> int:
        # `held_evidence` is a TOP-LEVEL row key. Reading it only out of ``extra`` (which is
        # None on these rows) silently defaulted every row to 0 and reported "upstream
        # producer gap" for ALL of them -- including the ones that actually held evidence and
        # were the anchor-starvation defect. A zero with no positive control is unreadable;
        # this reader now takes the top-level key and falls back to ``extra`` only if a future
        # writer moves it.
        v = row.get("held_evidence")
        if v is None:
            v = (row.get("extra") or {}).get("held_evidence")
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    incomplete_with_evidence = sum(
        1 for r in compilations
        if "DECISION_INCOMPLETE" in str(r.get("reason") or "") and _held(r) > 0)
    incomplete_no_evidence = sum(
        1 for r in compilations
        if "DECISION_INCOMPLETE" in str(r.get("reason") or "") and _held(r) == 0)
    # dose<=1 is per OBSERVATION, so count observations that saw more than one COMPILED plan.
    compiled_per_obs = Counter(
        str((r.get("observation_binding") or {}).get("candidate_id")
            or r.get("candidate_id") or "(unbound)")
        for r in compilations
        if "COMPILED" in str(r.get("reason") or "").upper()
        or r.get("outcome") == "delivered")
    dose_violations = sum(1 for k, n in compiled_per_obs.items()
                          if n > 1 and k != "(unbound)")

    leak_delivered = [r for r in real if "leak" in str(r.get("reason") or "").lower()]
    leak_guard_drops = sum(
        1 for r in rows
        if r.get("outcome") != "delivered"
        and ("leak" in str(r.get("reason") or "").lower()
             or r.get("outcome") == "suppressed_hidden_only"))

    return {
        "path": str(path), "rows": len(rows), "bad_lines": bad,
        "outcomes": outcomes, "real_delivered": len(real),
        "empty_delivered": empty_delivered, "layers": layers,
        "fact": fact, "boundary": boundary,
        "has_fact_field": has_fact, "has_boundary_field": has_boundary,
        "both_count": len(both), "on_time": on_time, "off": off,
        "indeterminate": indeterminate, "temporal": temporal,
        "canonical_rows": len(canonical),
        "canonical_delivered": len(canonical_delivered),
        "canon_detail": canon_detail,
        "compilations": len(compilations),
        "compile_reasons": compile_reasons,
        "incomplete_with_evidence": incomplete_with_evidence,
        "incomplete_no_evidence": incomplete_no_evidence,
        "dose_violations": dose_violations,
        "leak_delivered": len(leak_delivered),
        "leak_guard_drops": leak_guard_drops,
    }


def read_outcome_records(xdir: Path) -> list[dict]:
    """Per-task records from any outcome.json under one artifact dir."""
    recs: list[dict] = []
    for p in sorted(xdir.rglob("outcome.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            recs.extend(t for t in data["tasks"] if isinstance(t, dict))
        elif isinstance(data, dict) and data.get("instance_id"):
            recs.append(data)
    return recs


def read_task_truth(xdir: Path) -> dict | None:
    for p in sorted(xdir.rglob("task_truth.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="GroundTruth GHA run-status checker")
    ap.add_argument("run_id")
    ap.add_argument("--repo", default="harneet2512/groundtruth")
    ap.add_argument("--artifacts", action="store_true",
                    help="download artifacts + analyze gt_runtime_ledger_*.jsonl")
    ap.add_argument("--tmp", default="D:/tmp/gt_run_check",
                    help="cache dir for artifact zips/extractions")
    ap.add_argument("--max-artifact-mb", type=int, default=512)
    ap.add_argument("--max-annotation-jobs", type=int, default=20)
    ap.add_argument("--tasks", default="",
                    help="comma-separated task-id substrings to limit artifact analysis")
    args = ap.parse_args(argv)

    try:  # never let a cp1252 console kill the report
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    repo, run_id = args.repo, args.run_id
    run = api_json(f"repos/{repo}/actions/runs/{run_id}")
    if run is None:
        print(f"VERDICT: CHECKER-FAIL run {run_id} -- run fetch failed "
              f"({'; '.join(_API_ERRORS) or 'unknown'})")
        return 2

    status = run.get("status") or "?"
    concl = run.get("conclusion")
    wf = Path(run.get("path") or "?").name
    created = _dt(run.get("created_at"))
    started = _dt(run.get("run_started_at"))
    updated = _dt(run.get("updated_at"))
    end = updated if status == "completed" else _now()
    queued_s = _dur(created, started)
    elapsed_s = _dur(started, end)

    # -- jobs (paginated: deepswe_full fans 113+) -----------------------------
    jobs = api_paginated(f"repos/{repo}/actions/runs/{run_id}/jobs", "jobs") or []
    ok_jobs = [j for j in jobs if j.get("conclusion") == "success"]
    skipped = [j for j in jobs if j.get("conclusion") == "skipped"]
    running = [j for j in jobs if j.get("status") in ("in_progress",)]
    queued = [j for j in jobs if j.get("status") in ("queued", "waiting", "pending")]
    failing = [j for j in jobs if j.get("conclusion")
               in ("failure", "cancelled", "timed_out", "action_required")]
    done = [j for j in jobs if j.get("status") == "completed"]

    def _fail_step(j: dict) -> str:
        for s in j.get("steps") or []:
            if (s.get("conclusion") or "") in ("failure", "timed_out"):
                return f"step[{s.get('number')}]='{s.get('name')}'"
        return "no failing step recorded (job-level)"

    def _live_step(j: dict) -> str:
        for s in j.get("steps") or []:
            if s.get("status") == "in_progress":
                return f"step[{s.get('number')}]='{s.get('name')}'"
        return "between steps"

    # -- annotations: failing first, then running (live signal), capped -------
    ann_by_job: dict[int, list[dict]] = {}
    ann_targets = failing + running
    for j in ann_targets[: args.max_annotation_jobs]:
        jid = j.get("id")
        anns = api_json(f"repos/{repo}/check-runs/{jid}/annotations?per_page=100")
        if isinstance(anns, list) and anns:
            ann_by_job[jid] = anns
    ann_skipped = max(0, len(ann_targets) - args.max_annotation_jobs)

    # -- artifacts ------------------------------------------------------------
    art_list = api_paginated(f"repos/{repo}/actions/runs/{run_id}/artifacts",
                             "artifacts") or []
    notes: list[str] = []
    ledger_stats: list[tuple[str, dict]] = []
    outcome_recs: dict[str, dict] = {}
    truth_by_art: dict[str, dict] = {}
    digest_lines: list[str] = []
    analyzed = []
    if args.artifacts:
        base = Path(args.tmp) / str(run_id)
        task_filter = {t.strip() for t in args.tasks.split(",") if t.strip()} or None
        analyzed = fetch_artifacts(repo, art_list, base, args.max_artifact_mb,
                                   task_filter, notes)
        for name, xdir in analyzed:
            for lp in sorted(xdir.rglob("gt_runtime_ledger*.jsonl")):
                ledger_stats.append((name, analyze_ledger(lp)))
            for rec in read_outcome_records(xdir):
                iid = rec.get("instance_id") or name
                outcome_recs[str(iid)] = rec
            truth = read_task_truth(xdir)
            if truth:
                truth_by_art[name] = truth
            for dp in sorted(xdir.rglob("GT_SUBSTRATE_DIGEST.txt")):
                try:
                    digest_lines.append(
                        f"{name}: {_one_line(dp.read_text(encoding='utf-8'), 160)}")
                except Exception:  # noqa: BLE001
                    pass

    # -- taxonomy per failing job --------------------------------------------
    verdicts: list[tuple[dict, str, str]] = []
    for j in failing:
        task_key = str(j.get("name") or "").split(" (")[0]
        orec = outcome_recs.get(task_key)
        msgs = [f"{a.get('annotation_level')}: {a.get('message') or a.get('title') or ''}"
                for a in ann_by_job.get(j.get("id"), [])]
        tax, ev = classify_job(j, msgs, orec)
        verdicts.append((j, tax, ev))
    tax_counts = Counter(t for _, t, _ in verdicts)

    # -- run-level ledger aggregate ------------------------------------------
    agg_delivered = sum(s.get("real_delivered", 0) for _, s in ledger_stats
                        if "error" not in s)
    agg_canonical = sum(s.get("canonical_rows", 0) for _, s in ledger_stats
                        if "error" not in s)
    agg_canon_deliv = sum(s.get("canonical_delivered", 0) for _, s in ledger_stats
                          if "error" not in s)
    agg_leak = sum(s.get("leak_delivered", 0) for _, s in ledger_stats
                   if "error" not in s)

    # -- resolution summary ---------------------------------------------------
    res_rows: list[str] = []
    resolved_n = 0
    outcome_n = 0
    seen_tasks: set[str] = set()
    for iid, rec in sorted(outcome_recs.items()):
        seen_tasks.add(iid)
        outcome_n += 1
        fc = rec.get("failure_class")
        if fc == "RESOLVED":
            resolved_n += 1
        res_rows.append(
            f"  {iid}: {fc} reward={rec.get('reward')} steps={rec.get('n_agent_steps')}"
            f" exit={rec.get('exit_status')}"
            + (f" infra={rec.get('infra_subtype')}" if rec.get("infra_subtype") else ""))
    for name, truth in sorted(truth_by_art.items()):
        iid = str(truth.get("instance_id") or name)
        if iid in seen_tasks:
            continue
        ob = truth.get("outcome") or {}
        outcome_n += 1
        if ob.get("resolved"):
            resolved_n += 1
        res_rows.append(f"  {iid}: {ob.get('failure_class')} reward={ob.get('reward')}"
                        f" (from task_truth.json)")

    # ======================= OUTPUT (verdict line FIRST) =====================
    state = (concl or status or "?").upper()
    jobs_bit = (f"jobs {len(done)}/{len(jobs)} done: {len(ok_jobs)} ok, "
                f"{len(failing)} fail")
    if tax_counts:
        jobs_bit += " [" + " ".join(f"{k}={v}" for k, v in tax_counts.most_common()) + "]"
    if running:
        jobs_bit += f", {len(running)} running"
    if queued:
        jobs_bit += f", {len(queued)} queued"
    if skipped:
        jobs_bit += f", {len(skipped)} skipped"
    if args.artifacts and ledger_stats:
        oracle_bit = (f"ORACLE RELEASED {agg_canon_deliv} capsule-deliveries "
                      f"({agg_canonical} canonical rows)"
                      if agg_canonical
                      else f"ORACLE SILENT: 0 canonical/capsule rows "
                           f"({agg_delivered} legacy-delivered)")
        res_bit = (f"resolved {resolved_n}/{outcome_n}" if outcome_n
                   else "resolved: no outcome.json yet")
        leak_bit = f" LEAK-ROWS={agg_leak}!" if agg_leak else ""
    elif args.artifacts:
        oracle_bit = ("no ledgers in downloadable artifacts yet"
                      if art_list else "no artifacts uploaded yet")
        res_bit = "resolved n/a"
        leak_bit = ""
    else:
        oracle_bit = f"{len(art_list)} artifacts (pass --artifacts to analyze ledgers)"
        res_bit = "resolved n/a"
        leak_bit = ""
    print(f"VERDICT: {state} run {run_id} {wf} | {jobs_bit} | {oracle_bit}"
          f"{leak_bit} | {res_bit}")

    print(f"RUN  id={run_id} wf='{run.get('name')}' status={status} "
          f"conclusion={concl or '-'} attempt={run.get('run_attempt')}")
    print(f"     sha={str(run.get('head_sha') or '?')[:12]} "
          f"branch={run.get('head_branch')} event={run.get('event')} "
          f"queued={queued_s} elapsed={elapsed_s}")
    print(f"     url={run.get('html_url')}")

    print(f"JOBS ({len(jobs)} total: {len(ok_jobs)} ok / {len(failing)} fail / "
          f"{len(running)} running / {len(queued)} queued / {len(skipped)} skipped)")
    for j in failing:
        s2 = _dur(_dt(j.get("started_at")), _dt(j.get("completed_at")))
        print(f"  FAIL  {j.get('name')}  [{j.get('conclusion')}] {_fail_step(j)} "
              f"dur={s2} job_id={j.get('id')}")
    for j in running:
        s2 = _dur(_dt(j.get("started_at")), _now())
        print(f"  RUN   {j.get('name')}  {_live_step(j)} elapsed={s2} "
              f"job_id={j.get('id')}")
    for j in queued:
        print(f"  QUEUE {j.get('name')}")
    if ok_jobs:
        names = ", ".join(str(j.get("name")) for j in ok_jobs[:10])
        more = f" (+{len(ok_jobs) - 10} more)" if len(ok_jobs) > 10 else ""
        print(f"  OK    {names}{more}")

    if ann_by_job:
        print("ANNOTATIONS (live via checks API)")
        for j in ann_targets:
            anns = ann_by_job.get(j.get("id"))
            if not anns:
                continue
            print(f"  {j.get('name')} [job {j.get('id')}]")
            for a in anns[:8]:
                print(f"    {a.get('annotation_level')}: "
                      f"{_one_line(a.get('message') or a.get('title') or '')}")
            if len(anns) > 8:
                print(f"    (+{len(anns) - 8} more annotations)")
    elif failing or running:
        print("ANNOTATIONS: none surfaced yet on checked jobs"
              + (f" ({ann_skipped} jobs beyond --max-annotation-jobs)" if ann_skipped else ""))

    if verdicts:
        print("TAXONOMY (per failing job; TASK=expected INFRA=retry "
              "METRICS/PROOF=gate PRODUCT=real GT bug)")
        for j, tax, ev in verdicts:
            print(f"  {j.get('name')}: {tax} -- {ev}")

    if args.artifacts:
        print(f"ARTIFACTS ({len(art_list)} listed, {len(analyzed)} analyzed)")
        for line in digest_lines:
            print(f"  substrate-digest {line}")
        for name, s in ledger_stats:
            if "error" in s:
                print(f"  ledger {name}: {s['error']}")
                continue
            print(f"  ledger {name}: rows={s['rows']}"
                  + (f" badlines={s['bad_lines']}" if s["bad_lines"] else "")
                  + f" delivered={s['real_delivered']}"
                  + (f" empty-delivered={s['empty_delivered']} (chars<=0: not deliveries)"
                     if s["empty_delivered"] else ""))
            print(f"    outcomes: {_fmt_counter(s['outcomes'])}")
            print(f"    layers(delivered): {_fmt_counter(s['layers'])}")
            if s["has_fact_field"]:
                print(f"    fact_class(delivered): {_fmt_counter(s['fact'])}")
            else:
                print("    fact_class: FIELD ABSENT on every row (older-run ledger, "
                      "pre-identity-stamp)")
            if s["has_boundary_field"]:
                print(f"    contracted_boundary(delivered): {_fmt_counter(s['boundary'])}")
            else:
                print("    contracted_boundary: FIELD ABSENT on every row (older-run "
                      "ledger, pre-boundary-stamp)")
            if s["both_count"]:
                line = (f"    on-time: {s['on_time']}/{s['both_count']} delivered on "
                        f"contracted boundary")
                if s["indeterminate"]:
                    line += f"; {s['indeterminate']} indeterminate (no actual_event)"
                if s["off"]:
                    line += f"; {len(s['off'])} OFF-BOUNDARY"
                print(line)
                for f, b, a, lay, it in s["off"][:5]:
                    print(f"      OFF: {f} contracted={b} actual={a} layer={lay} iter={it}")
            else:
                print("    on-time: not evaluable -- 0 delivered rows carry BOTH "
                      "fact_class + contracted_boundary")
            if s["temporal"]:
                tline = f"    temporal_relation: {_fmt_counter(s['temporal'])}"
                inversions = {k: v for k, v in s["temporal"].items()
                              if k != "CONTROL_PRECEDES_DELIVERY"}
                if inversions:
                    tline += (" [non-CPD present: "
                              + " ".join(f"{k}={v}" for k, v in inversions.items())
                              + " -- declared receipt-mediator relation is legal; "
                                "any UNDECLARED inversion is a finding]")
                print(tline)
            else:
                print("    temporal_relation: FIELD ABSENT on every row")
            canon_bit = (f"{s['canonical_rows']} rows "
                         f"({s['canonical_delivered']} delivered-with-bytes)")
            if s["canonical_rows"]:
                print(f"    canonical/capsule: {canon_bit} :: "
                      f"{_fmt_counter(s['canon_detail'], 6)}")
            else:
                print("    canonical/capsule: 0 rows -- no capsule reached the model on "
                      "this task")
            # The oracle SELECTS the smallest decision-complete coalition and holds the
            # rest, so quiet is correct and a big number would be the failure. Report WHY
            # it was quiet, not how loud it was.
            if s.get("compilations"):
                print(f"    compile attempts: {s['compilations']} :: "
                      f"{_fmt_counter(s['compile_reasons'], 8)}")
                if s.get("incomplete_with_evidence"):
                    print(f"      -> DEFECT: {s['incomplete_with_evidence']} "
                          "DECISION_INCOMPLETE with held_evidence>0 -- evidence existed and "
                          "NO anchor role could carry it (anchor starvation)")
                if s.get("incomplete_no_evidence"):
                    print(f"      -> UPSTREAM: {s['incomplete_no_evidence']} "
                          "DECISION_INCOMPLETE with held_evidence==0 -- no evidence at all "
                          "(producer gap, not an oracle gap)")
                if s.get("dose_violations"):
                    print(f"      -> DOSE VIOLATION: {s['dose_violations']} observation(s) "
                          "compiled MORE THAN ONE capsule -- breaks dose<=1")
                else:
                    print("      -> dose<=1 held: no observation compiled two capsules")
            else:
                print("    compile attempts: 0 -- the oracle never reached a compile "
                      "decision (no decision open, or the runtime never attached)")
            leak_line = f"    leak: delivered-with-leak-reason={s['leak_delivered']}"
            if s["leak_delivered"]:
                leak_line += " << FINDING (delivered despite leak marker)"
            leak_line += f"; guard-suppressed={s['leak_guard_drops']} (guard working)"
            print(leak_line)
        if ledger_stats:
            headline = ("HEADLINE: ORACLE RELEASED "
                        f"{agg_canon_deliv} capsule deliveries "
                        f"({agg_canonical} canonical rows)"
                        if agg_canonical else
                        "HEADLINE: ZERO canonical/capsule rows across ALL ledgers -- "
                        "the oracle released NOTHING (legacy layers delivered "
                        f"{agg_delivered} payloads)")
            print(f"  RUN-TOTAL: delivered={agg_delivered} "
                  f"canonical={agg_canonical} :: {headline}")
        elif analyzed:
            print("  no gt_runtime_ledger*.jsonl found in any analyzed artifact")
        for n in notes:
            print(f"  note: {n}")
    else:
        arts_named = ", ".join(a.get("name", "?") for a in art_list[:6])
        more = f" (+{len(art_list) - 6} more)" if len(art_list) > 6 else ""
        print(f"ARTIFACTS: {len(art_list)} available{': ' + arts_named + more if art_list else ''}"
              f" -- pass --artifacts for ledger/delivery analysis")

    if res_rows:
        print(f"RESOLUTION ({resolved_n}/{outcome_n} resolved)")
        for r in res_rows:
            print(r)
    elif args.artifacts:
        print("RESOLUTION: no outcome.json / task_truth.json in downloadable "
              "artifacts (normal mid-run)")

    if _API_ERRORS:
        print("API-ERRORS (checker-side, not run-side)")
        for e in _API_ERRORS[:6]:
            print(f"  {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

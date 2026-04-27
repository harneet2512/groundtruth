#!/usr/bin/env python3
"""Minimal ablation hook for Qwen FC benchmark.

Behavior controlled by GT_ABLATION_MODE env var:
  inert           → hook runs, always emits nothing
  empty_surface   → compute evidence, suppress all output
  sibling_only    → emit only SIBLING family findings
  import_only     → emit only IMPORT family findings
  sibling_plus_import → emit SIBLING + IMPORT

Constraints (from ablation spec):
  - Plain text only, no XML
  - Max 3 lines per edit, max 500 chars total
  - Only confidence >= 0.7
  - Silence when no findings
  - No "OK no findings"
  - No submit modification
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

STATE_PATH = Path("/root/state.json")
GT_DB = "/tmp/gt_graph.db"
GT_EVENTS_LOG = "/tmp/gt_ablation_events.jsonl"
GT_INTEL = "/tmp/gt_intel_real.py"
MIN_CONFIDENCE = 0.7
MAX_CHARS = 500
MAX_LINES = 3

MODE = os.environ.get("GT_ABLATION_MODE", "inert")
ALLOWED_FAMILIES = {
    "inert": set(),
    "empty_surface": set(),
    "sibling_only": {"SIBLING"},
    "import_only": {"IMPORT"},
    "sibling_plus_import": {"SIBLING", "IMPORT"},
}

_novelty_seen: set[str] = set()
_novelty_file = Path("/tmp/gt_ablation_novelty.json")


def log_event(**kw):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "arm": os.environ.get("GT_ABLATION_ARM", "unknown"),
        "instance_id": os.environ.get("GT_INSTANCE_ID", "unknown"),
        "mode": MODE,
        **kw,
    }
    try:
        with open(GT_EVENTS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def load_novelty():
    global _novelty_seen
    try:
        if _novelty_file.exists():
            _novelty_seen = set(json.loads(_novelty_file.read_text()))
    except Exception:
        _novelty_seen = set()


def save_novelty():
    try:
        _novelty_file.write_text(json.dumps(list(_novelty_seen)))
    except Exception:
        pass


def fingerprint(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


def get_changed_files() -> list[str]:
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd="/testbed", timeout=5,
        )
        return [f.strip() for f in r.stdout.strip().split("\n") if f.strip()]
    except Exception:
        return []


def compute_evidence(changed_files: list[str]) -> list[dict]:
    """Run gt_intel.py and parse evidence nodes."""
    if not os.path.exists(GT_INTEL) or not os.path.exists(GT_DB):
        return []

    results = []
    for fpath in changed_files[:2]:
        try:
            r = subprocess.run(
                [sys.executable, GT_INTEL, f"--db={GT_DB}", f"--root=/testbed",
                 f"--file={fpath}", "--reminder"],
                capture_output=True, text=True, timeout=20, cwd="/testbed",
            )
            if r.stdout.strip():
                for line in r.stdout.strip().split("\n"):
                    line = line.strip()
                    if not line or line.startswith("[OK]") or line.startswith("---"):
                        continue
                    family = "UNKNOWN"
                    if "IMPORT" in line.upper() or "import" in line:
                        family = "IMPORT"
                    elif "sibling" in line.lower() or "SIBLING" in line or "pattern" in line.lower():
                        family = "SIBLING"
                    elif "caller" in line.lower() or "CALLER" in line:
                        family = "CALLER"
                    elif "test" in line.lower() or "TEST" in line or "assert" in line.lower():
                        family = "TEST"
                    elif "impact" in line.lower() or "IMPACT" in line:
                        family = "IMPACT"
                    elif "type" in line.lower() or "TYPE" in line or "return" in line.lower():
                        family = "TYPE"
                    elif "precedent" in line.lower() or "PRECEDENT" in line or "commit" in line.lower():
                        family = "PRECEDENT"

                    conf_str = ""
                    if "(" in line and ")" in line:
                        try:
                            conf_str = line.rsplit("(", 1)[1].split(")")[0]
                            conf = float(conf_str)
                        except (ValueError, IndexError):
                            conf = 0.5
                    else:
                        conf = 0.5

                    results.append({
                        "family": family,
                        "confidence": conf,
                        "text": line,
                        "file": fpath,
                    })
        except Exception as e:
            log_event(event_type="hook_error", error=str(e)[:200])

    return results


def format_evidence(findings: list[dict], allowed: set[str]) -> str:
    """Filter and format findings as plain text. No XML."""
    filtered = []
    for f in findings:
        if f["family"] not in allowed:
            continue
        if f["confidence"] < MIN_CONFIDENCE:
            continue
        fp = fingerprint(f["text"])
        if fp in _novelty_seen:
            log_event(
                event_type="evidence_duplicate_suppressed",
                fingerprint=fp,
                family=f["family"],
            )
            continue
        _novelty_seen.add(fp)
        filtered.append(f)

    if not filtered:
        return ""

    lines = []
    chars = 0
    for f in filtered[:MAX_LINES]:
        text = f["text"][:MAX_CHARS - chars]
        if not text:
            break
        lines.append(text)
        chars += len(text) + 1
        if chars >= MAX_CHARS:
            break

    return "\n".join(lines)


def main():
    load_novelty()

    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except Exception:
            state = {}

    state["working_dir"] = os.getcwd()
    step = state.get("step", 0)

    log_event(event_type="hook_start", step=step)
    t0 = time.time()

    changed = get_changed_files()
    allowed = ALLOWED_FAMILIES.get(MODE, set())

    evidence_text = ""
    evidence_computed = False
    findings_raw = []

    if changed and MODE != "inert":
        findings_raw = compute_evidence(changed)
        evidence_computed = True

        for f in findings_raw:
            log_event(
                event_type="evidence_computed",
                step=step,
                family=f["family"],
                confidence=f["confidence"],
                file=f["file"],
                allowed=f["family"] in allowed,
            )

        if MODE != "empty_surface" and allowed:
            evidence_text = format_evidence(findings_raw, allowed)

    if evidence_text:
        state["gt_evidence"] = evidence_text
        for line in evidence_text.split("\n"):
            log_event(
                event_type="evidence_emitted",
                step=step,
                text=line[:200],
                chars=len(line),
                fingerprint=fingerprint(line),
            )
    elif "gt_evidence" in state:
        del state["gt_evidence"]

    if evidence_computed and not evidence_text:
        log_event(
            event_type="evidence_suppressed",
            step=step,
            reason="no_findings_passed_filter" if findings_raw else "no_material_edit",
            families_computed=[f["family"] for f in findings_raw],
            allowed_families=list(allowed),
        )

    STATE_PATH.write_text(json.dumps(state))
    save_novelty()

    latency = int((time.time() - t0) * 1000)
    log_event(
        event_type="hook_end",
        step=step,
        latency_ms=latency,
        changed_files=changed[:5],
        evidence_computed=evidence_computed,
        evidence_emitted=bool(evidence_text),
        emitted_chars=len(evidence_text),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_event(event_type="hook_error", error=str(e)[:500])

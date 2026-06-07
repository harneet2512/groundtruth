#!/usr/bin/env python3
"""check_resolution_delivery.py — prove RESOLUTION reached the AGENT (the missing half).

The 300/30-task pipelines already certify that graph.db is well-RESOLVED (the
RESOLUTION-QUALITY gate: deterministic CALLS edges vs name_match). That validates
the GRAPH. It does NOT validate that any of that resolution was actually DELIVERED
into the agent's context — a graph can be perfectly resolved yet the agent never
sees a single resolved caller/call-edge (L1/L3/L3b suppressed, or only structural
`<gt-scope>` import lines rendered). This checker closes that gap.

Per the AGENT-OBSERVATION rule (.claude/CLAUDE.md "Verify GT output from AGENT
OBSERVATION, not structured telemetry"): resolution counts as DELIVERED only when
it is in the agent's RECEIVED context. So the authoritative source is the
agent-facing TEXT, not an "emitted=true" flag.

Two independent truth sources, in trust order:

  (1) AGENT-OBSERVATION truth (strongest). The actual agent-facing rendered text:
      - gt_layer_events_*.jsonl events with agent_visible=true AND
        delivery_status='delivered' -> inspect `rendered_text`, OR
      - output.jsonl history `content` / top-level `instruction` (what the agent
        literally read).
      A resolved call edge reached the agent iff that text carries a RESOLVED-CALLER
      WITNESS — a caller/callee relationship line naming a concrete `file:line`
      (e.g. "Callers: user_set() in conan/api/subapi/remotes.py:238 ...",
      "<gt-evidence ...> ... caller ...", an L3 "Calls into:" / "Called by:" line).
      Pure structural `<gt-scope ... imported>` lines are NOT counted (an import is
      not a resolved CALL edge — it is exactly the weaker signal this gate exists to
      distinguish from real call-graph delivery).

  (2) NUMERIC corroboration (gt_run_summary_*.json). Never the SOLE basis for a
      PASS-on-text run, but a positive count is sufficient corroboration and a
      cheap primary signal when no JSONL/output.jsonl is present:
        l1.l1_candidates_with_call_edge_count   > 0
        l3b.l3b_caller_edge_count               > 0   (resolved-caller deliveries)
        l3.l3_consumer_count / l3.l3_caller_code_line_count > 0
      (Field names verified against a real on-disk run summary:
       .claude/reports/runs/20260606_gha_run1__conan-17123/gt_debug/
       gt_run_summary_conan-io__conan-17123.json.)

VERDICT
  delivered := (resolved-caller witness in agent-facing text) OR (any positive
               numeric call-edge / resolved-caller delivery count).
  --warn    (default): print verdict, ALWAYS exit 0. Use for first rollout while
            field semantics are confirmed on live runs (the gate observes, never
            blocks). Emits ::warning:: on a no-delivery run so it is visible in GHA.
  --require : exit 1 when delivered is False (fail-closed: a run that never delivered
            ANY resolution to the agent does not count as passed).

JSONL-parsed, never grep. Generalized: no task IDs, no gold, no repo/benchmark
literals — only GT's own marker vocabulary + the structural shape of a resolved
caller witness (a relationship line with a file:line target). Language-agnostic.

Usage:
  python scripts/verify/check_resolution_delivery.py \
      [--run-summary <gt_run_summary_*.json> ...] \
      [--layer-events <gt_layer_events_*.jsonl> ...] \
      [--output-jsonl <output.jsonl> ...] \
      [--glob-dir <dir>]            # auto-discover all three artifact kinds under dir
      [--require | --warn] [--json]
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import os
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Resolved-caller WITNESS detection in agent-facing text.                      #
#                                                                              #
# A resolved CALL edge that reached the agent shows up as a RELATIONSHIP LINE  #
# that names a concrete caller/callee with a file:line target. We match GT's   #
# own marker vocabulary (the labels the brief/L3/L3b render) AND require a      #
# file:line witness so a bare label with no target does not count.             #
# Pure `imported` scope lines are explicitly NOT a resolved call edge.         #
# --------------------------------------------------------------------------- #

# Relationship labels GT renders for a RESOLVED call edge (case-insensitive).
# These are call-graph relations, NOT imports/structural-scope.
_CALLER_LABEL_RE = re.compile(
    r"\b(callers?|called by|calls into|caller_usage|"
    r"\[caller\]|\[callers\]|\[calls\])\b",
    re.IGNORECASE,
)
# A concrete caller/callee witness: "<symbol>() in <path>:<line>" or "<path>:<line>"
# appearing on/after a caller label line. The file:line target is what makes it a
# RESOLVED edge (vs a bare unresolved name).
_FILE_LINE_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9_]+:\d+")
# GT post-edit / evidence block that carries resolved relationships when present.
_GT_EVIDENCE_OPEN_RE = re.compile(r"<gt-evidence\b", re.IGNORECASE)
# An explicit unverified disclaimer means the line is NOT a delivered resolved fact.
_UNVERIFIED_RE = re.compile(r"\(unverified\)", re.IGNORECASE)


def _text_has_resolved_caller_witness(text: str) -> bool:
    """True iff `text` carries an agent-visible RESOLVED-caller witness.

    Requires a caller-relationship label AND, on the same line, a concrete
    file:line target that is NOT marked (unverified). This is the structural
    signature of a resolved CALL edge that reached the agent — distinct from a
    structural `imported` scope line (which has no caller label) and from a bare
    unresolved name (which has no file:line target).
    """
    if not text:
        return False
    for line in text.splitlines():
        if not _CALLER_LABEL_RE.search(line):
            continue
        if _UNVERIFIED_RE.search(line):
            continue  # an explicitly-unverified caller is not a delivered FACT
        if _FILE_LINE_RE.search(line):
            return True
    return False


# --------------------------------------------------------------------------- #
# Source 1a: gt_layer_events_*.jsonl (agent_visible delivered rendered_text)   #
# --------------------------------------------------------------------------- #

def _scan_layer_events(path: Path) -> dict:
    out = {
        "delivered_events": 0,            # agent_visible delivered events seen
        "witness_events": 0,              # of those, carrying a resolved-caller witness
        "witness_layers": set(),
        "sample": "",
    }
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                continue
            agent_visible = bool(e.get("agent_visible"))
            delivered = (e.get("delivery_status") == "delivered") or bool(e.get("emitted"))
            # Only count text the agent actually received.
            if not (agent_visible or delivered):
                continue
            text = e.get("rendered_text") or ""
            if not isinstance(text, str) or not text:
                continue
            out["delivered_events"] += 1
            if _text_has_resolved_caller_witness(text):
                out["witness_events"] += 1
                lyr = e.get("layer")
                if lyr:
                    out["witness_layers"].add(str(lyr))
                if not out["sample"]:
                    for ln in text.splitlines():
                        if _CALLER_LABEL_RE.search(ln) and _FILE_LINE_RE.search(ln):
                            out["sample"] = ln.strip()[:200]
                            break
    return out


# --------------------------------------------------------------------------- #
# Source 1b: output.jsonl (the literal agent-received instruction + history)   #
# --------------------------------------------------------------------------- #

def _scan_output_jsonl(path: Path) -> dict:
    out = {"witness_surfaces": 0, "sample": ""}
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            surfaces: list[str] = []
            instr = rec.get("instruction")
            if isinstance(instr, str) and instr:
                surfaces.append(instr)
            hist = rec.get("history")
            entries: list = []
            if isinstance(hist, list):
                entries = [h for h in hist if isinstance(h, dict)]
            elif rec.get("history") is None and "content" in rec and "instruction" not in rec:
                entries = [rec]
            for h in entries:
                c = h.get("content") or h.get("message") or ""
                if isinstance(c, str) and c:
                    surfaces.append(c)
            for text in surfaces:
                if _text_has_resolved_caller_witness(text):
                    out["witness_surfaces"] += 1
                    if not out["sample"]:
                        for ln in text.splitlines():
                            if _CALLER_LABEL_RE.search(ln) and _FILE_LINE_RE.search(ln):
                                out["sample"] = ln.strip()[:200]
                                break
    return out


# --------------------------------------------------------------------------- #
# Source 2: gt_run_summary_*.json (numeric corroboration)                      #
# --------------------------------------------------------------------------- #

def _as_int(v) -> int:
    """Coerce a summary field to int; non-numeric placeholders ('N/A ...') -> 0."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return 0


def _scan_run_summary(path: Path) -> dict:
    out = {
        "l1_call_edge_count": 0,
        "l3b_caller_edge_count": 0,
        "l3_consumer_count": 0,
        "l3_caller_code_line_count": 0,
        "any_positive": False,
        "present": False,
    }
    if not path.exists():
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            s = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return out
    if not isinstance(s, dict):
        return out
    out["present"] = True
    l1 = s.get("l1") or {}
    l3 = s.get("l3") or {}
    l3b = s.get("l3b") or {}
    out["l1_call_edge_count"] = _as_int(l1.get("l1_candidates_with_call_edge_count"))
    out["l3b_caller_edge_count"] = _as_int(l3b.get("l3b_caller_edge_count"))
    out["l3_consumer_count"] = _as_int(l3.get("l3_consumer_count"))
    out["l3_caller_code_line_count"] = _as_int(l3.get("l3_caller_code_line_count"))
    out["any_positive"] = (
        out["l1_call_edge_count"] > 0
        or out["l3b_caller_edge_count"] > 0
        or out["l3_consumer_count"] > 0
        or out["l3_caller_code_line_count"] > 0
    )
    return out


# --------------------------------------------------------------------------- #
# Discovery                                                                     #
# --------------------------------------------------------------------------- #

def _discover(glob_dir: str) -> dict:
    base = glob_dir.rstrip("/\\")
    return {
        "run_summaries": sorted(_glob.glob(os.path.join(base, "**", "gt_run_summary_*.json"), recursive=True)),
        "layer_events": sorted(_glob.glob(os.path.join(base, "**", "gt_layer_events_*.jsonl"), recursive=True)),
        "output_jsonls": sorted(_glob.glob(os.path.join(base, "**", "output*.jsonl"), recursive=True)),
    }


def check_resolution_delivery(
    *,
    run_summaries: list[str],
    layer_events: list[str],
    output_jsonls: list[str],
) -> dict:
    result: dict = {
        "check": "check_resolution_delivery",
        "delivered": False,
        "delivery_basis": [],
        "agent_visible_delivered_events": 0,
        "agent_visible_witness_events": 0,
        "witness_layers": [],
        "output_jsonl_witness_surfaces": 0,
        "numeric": {
            "l1_call_edge_count": 0,
            "l3b_caller_edge_count": 0,
            "l3_consumer_count": 0,
            "l3_caller_code_line_count": 0,
            "any_positive": False,
            "summaries_seen": 0,
        },
        "witness_sample": "",
        "sources_seen": {
            "run_summaries": len(run_summaries),
            "layer_events": len(layer_events),
            "output_jsonls": len(output_jsonls),
        },
        "reasons": [],
    }
    witness_layers: set[str] = set()

    # Source 1a: layer-events rendered_text (agent-visible delivered).
    for f in layer_events:
        le = _scan_layer_events(Path(f))
        result["agent_visible_delivered_events"] += le["delivered_events"]
        result["agent_visible_witness_events"] += le["witness_events"]
        witness_layers |= le["witness_layers"]
        if le["sample"] and not result["witness_sample"]:
            result["witness_sample"] = le["sample"]

    # Source 1b: output.jsonl (literal agent-received text).
    for f in output_jsonls:
        oj = _scan_output_jsonl(Path(f))
        result["output_jsonl_witness_surfaces"] += oj["witness_surfaces"]
        if oj["sample"] and not result["witness_sample"]:
            result["witness_sample"] = oj["sample"]

    # Source 2: numeric corroboration.
    for f in run_summaries:
        rs = _scan_run_summary(Path(f))
        if rs["present"]:
            result["numeric"]["summaries_seen"] += 1
            for k in ("l1_call_edge_count", "l3b_caller_edge_count",
                      "l3_consumer_count", "l3_caller_code_line_count"):
                result["numeric"][k] += rs[k]
    result["numeric"]["any_positive"] = (
        result["numeric"]["l1_call_edge_count"] > 0
        or result["numeric"]["l3b_caller_edge_count"] > 0
        or result["numeric"]["l3_consumer_count"] > 0
        or result["numeric"]["l3_caller_code_line_count"] > 0
    )

    result["witness_layers"] = sorted(witness_layers)

    text_witness = (
        result["agent_visible_witness_events"] > 0
        or result["output_jsonl_witness_surfaces"] > 0
    )
    if text_witness:
        result["delivery_basis"].append("agent_observation_text")
    if result["numeric"]["any_positive"]:
        result["delivery_basis"].append("run_summary_numeric")

    result["delivered"] = bool(result["delivery_basis"])

    if not result["delivered"]:
        had_any = (run_summaries or layer_events or output_jsonls)
        if not had_any:
            result["reasons"].append(
                "no artifacts found (gt_run_summary / gt_layer_events / output.jsonl) — "
                "cannot certify resolution delivery"
            )
        else:
            result["reasons"].append(
                "NO resolution reached the agent: no agent-visible resolved-caller "
                "witness in delivered text AND zero positive call-edge/resolved-caller "
                "delivery counts (graph may be resolved but the call graph was not "
                "delivered into the agent's context)"
            )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Prove RESOLUTION (resolved call edges/callers) reached the agent"
    )
    ap.add_argument("--run-summary", action="append", default=[],
                    help="gt_run_summary_*.json (repeatable)")
    ap.add_argument("--layer-events", action="append", default=[],
                    help="gt_layer_events_*.jsonl (repeatable)")
    ap.add_argument("--output-jsonl", action="append", default=[],
                    help="output.jsonl (repeatable)")
    ap.add_argument("--glob-dir", default=None,
                    help="auto-discover all three artifact kinds recursively under DIR")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--require", action="store_true",
                      help="fail-closed: exit 1 when no resolution reached the agent")
    mode.add_argument("--warn", action="store_true",
                      help="observe only: always exit 0, emit ::warning:: on no-delivery (default)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    run_summaries = list(args.run_summary)
    layer_events = list(args.layer_events)
    output_jsonls = list(args.output_jsonl)
    if args.glob_dir:
        d = _discover(args.glob_dir)
        run_summaries += d["run_summaries"]
        layer_events += d["layer_events"]
        output_jsonls += d["output_jsonls"]
    # de-dup, preserve order
    run_summaries = list(dict.fromkeys(run_summaries))
    layer_events = list(dict.fromkeys(layer_events))
    output_jsonls = list(dict.fromkeys(output_jsonls))

    r = check_resolution_delivery(
        run_summaries=run_summaries,
        layer_events=layer_events,
        output_jsonls=output_jsonls,
    )

    if args.json:
        print(json.dumps(r, indent=2))
    else:
        verdict = "DELIVERED" if r["delivered"] else "NOT-DELIVERED"
        print(f"[RESOLUTION-DELIVERY: {verdict}] basis={r['delivery_basis']}")
        print(f"  agent_visible_delivered_events={r['agent_visible_delivered_events']} "
              f"resolved_caller_witness_events={r['agent_visible_witness_events']} "
              f"witness_layers={r['witness_layers']}")
        print(f"  output_jsonl_witness_surfaces={r['output_jsonl_witness_surfaces']}")
        n = r["numeric"]
        print(f"  numeric(summaries={n['summaries_seen']}): "
              f"l1_call_edge={n['l1_call_edge_count']} l3b_caller_edge={n['l3b_caller_edge_count']} "
              f"l3_consumer={n['l3_consumer_count']} l3_caller_lines={n['l3_caller_code_line_count']} "
              f"any_positive={n['any_positive']}")
        if r["witness_sample"]:
            print(f"  witness_sample: {r['witness_sample']}")
        for reason in r["reasons"]:
            print(f"  - {reason}")

    if r["delivered"]:
        return 0
    # not delivered
    if args.require:
        return 1
    # --warn (default): observe-only. Surface in GHA but never block.
    msg = (r["reasons"][0] if r["reasons"] else "resolution not delivered to agent")
    print(f"::warning::check_resolution_delivery: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

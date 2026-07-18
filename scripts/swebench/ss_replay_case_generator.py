#!/usr/bin/env python3
"""ss_replay_case_generator — propose NEW ss_replay candidate cases from a completed run.

WHY (Cluster-5 ITEM 5, 2026-07-18): the trusted replay corpus
``tests/fixtures/ss_replay/cases.json`` (schema ``ss.replay_oracle_cases.v1``) is CURATED — every
entry is a byte-verified, human-reviewed finding. To grow it without polluting it, this generator
reads a completed run's per-delivery records (the ``gt_runtime_ledger_<task>.jsonl`` seam ledgers)
and MECHANICALLY proposes candidate entries in the SAME category shapes, into a SEPARATE file
marked with provenance (source run id + generator version + ``auto_generated: true``). Candidates
are NEVER enabled by default and NEVER written into the trusted ``cases.json`` — a human curator
promotes a reviewed candidate by hand.

Only categories that are DETERMINISTICALLY derivable from the ledger bytes are proposed (no
semantics, no truth claims):

  * ``suppress_provenance``  — a delivered row whose ``file_path`` is a scratch/throwaway path
    (``/tmp`` / ``tmp/`` / ``htmlcov`` / ``.tmp``); the curated rule is "lines filtered or delivery
    suppressed; L6 never reindexes these paths".
  * ``empty_payload``        — a delivered row with ``chars_delivered == 0`` (the hydra
    ga.trace_frame invariant: zero delivered rows with 0 bytes).
  * ``suppress_semantic_dup``— the SAME ``content_sha256_16`` delivered at >1 distinct
    (task, iteration); the curated rule is "byte-distinct/­identical re-deliveries require a
    fact-GROUP dedup".

PURE + deterministic: given the same ledgers + version, the emitted JSON is byte-identical
(sorted, ``ensure_ascii=False``). No I/O beyond reading the named ledgers / writing the named out
file. LLM-free.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Iterable, Mapping

GENERATOR_VERSION = "ss-replay-case-generator-v1-20260718"
CANDIDATES_SCHEMA = "ss.replay_oracle_cases.candidates.v1"
TRUSTED_SCHEMA = "ss.replay_oracle_cases.v1"

# A scratch / throwaway path a real fact must never be sourced from (mirrors the curated
# suppress_provenance examples: tmp/patch_*.py, change_tracer.py (/tmp), htmlcov/...).
_SCRATCH_RE = re.compile(r"(^|/)(tmp|\.tmp)/|/tmp\b|(^|/)htmlcov(/|\b)|/tmp/", re.IGNORECASE)


def _is_delivered(row: Mapping[str, Any]) -> bool:
    return str(row.get("outcome") or "").strip().lower() == "delivered"


def _delivery_label(row: Mapping[str, Any]) -> str:
    """The cases.json-style ``"<layer> m<iteration>"`` delivery label."""
    layer = str(row.get("layer") or row.get("event_type") or "unknown")
    it = row.get("iteration")
    return f"{layer} m{it}" if it is not None else layer


def _scratch_path(file_path: object) -> bool:
    return isinstance(file_path, str) and bool(file_path) and bool(_SCRATCH_RE.search(file_path))


def _chars(row: Mapping[str, Any]) -> int:
    for key in ("chars_delivered", "chars"):
        v = row.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    return 0


def generate_candidates(
    ledgers_by_task: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    source_run: str,
    generator_version: str = GENERATOR_VERSION,
) -> dict[str, Any]:
    """Build the candidate-cases document from per-task delivery ledgers. PURE + deterministic."""
    suppress_provenance: list[dict[str, Any]] = []
    empty_payload: list[dict[str, Any]] = []
    sha_index: dict[str, list[tuple[str, str]]] = defaultdict(list)  # sha -> [(task, delivery)]

    for task in sorted(ledgers_by_task):
        for row in ledgers_by_task[task]:
            if not isinstance(row, Mapping) or not _is_delivered(row):
                continue
            delivery = _delivery_label(row)
            fp = row.get("file_path")
            if _scratch_path(fp):
                suppress_provenance.append({
                    "task": task, "delivery": delivery, "paths": [fp],
                    "assert": "lines filtered or delivery suppressed reason=ss_provenance; "
                              "L6 never reindexes these paths",
                })
            if _chars(row) == 0:
                empty_payload.append({
                    "task": task, "delivery": delivery,
                    "assert": "zero delivered rows with 0 bytes",
                })
            sha = row.get("content_sha256_16")
            if isinstance(sha, str) and sha:
                sha_index[sha].append((task, delivery))

    suppress_semantic_dup: list[dict[str, Any]] = []
    for sha in sorted(sha_index):
        occ = sha_index[sha]
        if len(occ) > 1:
            suppress_semantic_dup.append({
                "content_sha256_16": sha,
                "deliveries": [f"{t}:{d}" for t, d in sorted(occ)],
                "assert": "byte-identical re-deliveries require fact-GROUP dedup reason=ss_semantic_dup",
            })

    provenance = {
        "auto_generated": True,
        "curated": False,
        "generator_version": generator_version,
        "source_run": source_run,
    }
    return {
        "schema": CANDIDATES_SCHEMA,
        "note": "AUTO-GENERATED replay-case CANDIDATES — NOT the trusted corpus. Every entry is a "
                "mechanical projection of the run's delivery ledgers and must be human-reviewed "
                "before promotion into cases.json (schema " + TRUSTED_SCHEMA + "). Enabling these "
                "in the replay oracle without curation is forbidden.",
        "provenance": provenance,
        "source_run": source_run,
        "suppress_provenance": sorted(
            suppress_provenance, key=lambda e: (e["task"], e["delivery"])
        ),
        "empty_payload": sorted(empty_payload, key=lambda e: (e["task"], e["delivery"])),
        "suppress_semantic_dup": suppress_semantic_dup,
        "counts": {
            "suppress_provenance": len(suppress_provenance),
            "empty_payload": len(empty_payload),
            "suppress_semantic_dup": len(suppress_semantic_dup),
        },
    }


def _read_ledgers(run_dir: str) -> dict[str, list[dict[str, Any]]]:
    """Read every ``gt_runtime_ledger_<task>.jsonl`` under ``run_dir`` (recursively). Malformed
    lines are skipped (never crash); a task with no ledger contributes nothing."""
    out: dict[str, list[dict[str, Any]]] = {}
    for root, _dirs, files in os.walk(run_dir):
        for fname in files:
            m = re.fullmatch(r"gt_runtime_ledger_(.+)\.jsonl", fname)
            if not m:
                continue
            task = m.group(1)
            rows: list[dict[str, Any]] = []
            with open(os.path.join(root, fname), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
            out.setdefault(task, []).extend(rows)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", help="run dir holding gt_runtime_ledger_<task>.jsonl records")
    ap.add_argument("--source-run", default=None, help="run id for provenance (default: basename)")
    ap.add_argument(
        "--output", default=None,
        help="candidate file (default: <run_dir>/ss_replay/cases_candidates_<run>.json). "
             "REFUSES to overwrite the trusted cases.json.",
    )
    args = ap.parse_args(argv)
    if not os.path.isdir(args.run_dir):
        print(f"ss_replay_case_generator: run_dir not found: {args.run_dir}", file=sys.stderr)
        return 2
    source_run = args.source_run or os.path.basename(os.path.normpath(args.run_dir))
    out_path = args.output or os.path.join(
        args.run_dir, "ss_replay", f"cases_candidates_{source_run}.json"
    )
    if os.path.basename(out_path) == "cases.json":
        print("ss_replay_case_generator: REFUSING to write the trusted cases.json; candidates go "
              "to a separate file", file=sys.stderr)
        return 3
    ledgers = _read_ledgers(args.run_dir)
    doc = generate_candidates(ledgers, source_run=source_run)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    print(f"ss_replay_case_generator: {sum(len(v) for v in ledgers.values())} ledger rows, "
          f"candidates -> {out_path} ({doc['counts']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

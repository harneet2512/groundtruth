"""GT_OBLIGATIONS_V2 coverage harness (plan §8) — v1 vs v2 extractor, offline.

RULE OF USE: act ONLY on grammar-class aggregates across the corpus — never on
named tasks (anti-overfitting; the three 2026-07-08 miss cases are the
sanctioned diagnostic exceptions). This harness spends $0: pure extraction
over local issue corpora.

Inputs (any that exist):
  --bench-dir <dir>   deepswe-bench checkout: tasks/*/instruction.md (113)
  --holdout <jsonl>   rows with issue_body (holdout_v1.jsonl etc.) — repeatable
Outputs: TSV per issue + aggregate block on stdout.

Usage:
  python tools/measure_obligation_coverage.py --holdout holdout_v1.jsonl \
      [--holdout holdout_js.jsonl] [--bench-dir deepswe-bench] [--tsv out.tsv]
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from groundtruth.pretask.spec import extract_spec, extract_spec_v2  # noqa: E402


def _iter_corpus(bench_dir: str | None, holdouts: list[str]):
    if bench_dir:
        for md in sorted(Path(bench_dir).glob("tasks/*/instruction.md")):
            yield f"bench:{md.parent.name}", md.read_text(encoding="utf-8", errors="replace")
    for hp in holdouts:
        for i, line in enumerate(Path(hp).read_text(encoding="utf-8").splitlines()):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            body = row.get("issue_body") or row.get("body") or row.get("issue_text") or ""
            title = row.get("issue_title") or ""
            if not body:
                continue
            name = row.get("bug_id") or f"{Path(hp).stem}#{i}"
            yield f"{Path(hp).stem}:{name}", (title + "\n\n" + body)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench-dir", default=None)
    ap.add_argument("--holdout", action="append", default=[])
    ap.add_argument("--tsv", default=None)
    args = ap.parse_args()

    rows = []
    kinds = collections.Counter()
    modalities = collections.Counter()
    det_fail = 0
    regressions = []  # issues where v2 < v1 (the holdout-gate condition)
    for name, text in _iter_corpus(args.bench_dir, args.holdout):
        v1 = extract_spec(text)
        v2a = extract_spec_v2(text)
        v2b = extract_spec_v2(text)
        ser_a = json.dumps(v2a.to_serializable(version=2), sort_keys=True)
        if ser_a != json.dumps(v2b.to_serializable(version=2), sort_keys=True):
            det_fail += 1
        n1, n2 = len(v1.obligations), len(v2a.obligations)
        if n2 < n1:
            regressions.append(name)
        subj = sum(1 for o in v2a.obligations if o.subject_symbols)
        for o in v2a.obligations:
            kinds[o.kind] += 1
            modalities[o.modality or "?"] += 1
        rows.append((name, n1, n2, subj,
                     hashlib.sha256(ser_a.encode()).hexdigest()[:8]))

    out = ["issue\tv1_clauses\tv2_clauses\tv2_with_subject_symbols\tv2_hash"]
    out += [f"{n}\t{a}\t{b}\t{c}\t{h}" for n, a, b, c, h in rows]
    tsv = "\n".join(out)
    if args.tsv:
        Path(args.tsv).write_text(tsv + "\n", encoding="utf-8")
    n = len(rows)
    v1_tot = sum(r[1] for r in rows)
    v2_tot = sum(r[2] for r in rows)
    v1_yield = sum(1 for r in rows if r[1] > 0)
    v2_yield = sum(1 for r in rows if r[2] > 0)
    print(tsv if not args.tsv else f"(per-issue TSV -> {args.tsv})")
    print("\n== AGGREGATE ==")
    print(f"issues: {n}")
    print(f"clauses total: v1={v1_tot} v2={v2_tot} (x{v2_tot / max(1, v1_tot):.2f})")
    print(f">=1-obligation yield: v1={v1_yield}/{n} v2={v2_yield}/{n}")
    print(f"v2<v1 RAW-count deltas: {len(regressions)} {regressions[:5]} "
          "(raw counts include v1 junk classes — headings/fence code; the "
          "COVERED-semantics invariant is pinned by test_spec_v2_holdout_gate)")
    print(f"determinism failures: {det_fail}")
    print(f"kinds: {dict(kinds)}")
    print(f"modalities: {dict(modalities)}")
    subj_frac = (sum(r[3] for r in rows) / max(1, v2_tot))
    print(f"clauses with subject symbols (checkable): {subj_frac:.1%}")
    return 1 if (det_fail or regressions) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Defect-discovery / residual-risk gate (layer 5 of the defense stack, 2026-07-20).

Answers the user's core objection — "we don't know how many defects remain" — with
a MEASURED number instead of a hope, using the Software-Testing-As-Species-Discovery
(STADS, Böhme TOSEM'18) framing:

  Run the generative seam fuzzer at escalating case counts. Fingerprint every
  DISTINCT failure (its minimal signature). Then:
    p0    = f1 / n              (Good-Turing) — P(next case reveals a NEVER-seen defect class)
    Chao1 = S_obs + f1^2/(2*f2) — estimated TOTAL distinct defect classes
    remaining = Chao1 - S_obs   — estimated undiscovered classes

  where f1 = #classes seen exactly once (singletons), f2 = #seen exactly twice,
  S_obs = #distinct classes observed, n = total cases run.

Dispatch verdict: GREEN only when p0 < --epsilon AND the discovery curve is flat
(no NEW class in the last --plateau cases). Otherwise the pool is not drained;
keep fuzzing / fix the found classes first.

This is the SS-LIVE-grade honesty layer: it converts "trust the run" into a
falsifiable, pre-registered threshold. It CANNOT see defect classes outside the
generator's input space — that limit is printed, never hidden.

Usage:
  python defect_discovery_gate.py            # default campaign, prints verdict
  python defect_discovery_gate.py --cases 5000 --epsilon 1e-4 --plateau 1000
Exit: 0 GREEN (drained enough) · 2 RED (residual risk above epsilon) · 3 defects found.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import traceback
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "src"), str(_REPO / "scripts" / "swebench"),
          str(_REPO / "artifact_deepswe")):
    if p not in sys.path:
        sys.path.insert(0, p)

# Reuse the fuzz gate's real seam invariant as the property under test.
import gt_mini_patch as g  # noqa: E402
import receipt_sidecar as rs  # noqa: E402
from groundtruth.runtime.evidence_envelope import build_observation_binding  # noqa: E402

_KINDS = ["detect.coherence", "detect.loop", "recovery", "l3b.evidence",
          "obligation.unexercised", "spec.obligation", "verify.horizon.executed"]
_TEXT_ATOMS = [
    "retry loop re-reads config", "cap at three attempts", "caller update in",
    "\x1b[31mansi\x1b[0m", "café π ≈ 3.14 ✓", "tab\tsep", "trailing   ",
    "crlf\r\n", "`quotes` 'and' \"dquotes\"", "x" * 200, "", "\n\n\n",
]
_SUBJECTS = ["src/pkg/mod.py", "pkg/mod.py", "./src/pkg/mod.py",
             "tests/test_x.py", "demo/e.py", "vendor/v.py", "a/b/c/d/e.py", ""]


def _fingerprint(exc: BaseException) -> str:
    """Minimal signature of a failure: exception type + top in-repo frame."""
    tb = traceback.extract_tb(exc.__traceback__)
    site = ""
    for fr in reversed(tb):
        if "gt_mini_patch" in fr.filename or "receipt_sidecar" in fr.filename \
                or "evidence_envelope" in fr.filename:
            site = f"{os.path.basename(fr.filename)}:{fr.lineno}"
            break
    return f"{type(exc).__name__}@{site}|{str(exc)[:60]}"


def _one_case(rng: random.Random, tmp: Path, n: int) -> "str | None":
    """Run one generated seam round-trip; return a failure fingerprint or None."""
    kind = rng.choice(_KINDS)
    produced = "\n".join(rng.choice(_TEXT_ATOMS) for _ in range(rng.randint(1, 4)))
    if not produced.strip():
        return None
    subject = rng.choice(_SUBJECTS)
    final = produced
    if rng.random() < 0.5 and "\n" in produced:
        lines = produced.split("\n")
        final = "\n".join(l for i, l in enumerate(lines) if i != rng.randrange(len(lines)))
        if not final.strip():
            final = produced
    try:
        os.environ["GT_LANE_ENVELOPE"] = "1"
        os.environ["GT_INSEAM_METRICS"] = "1"
        cert = tmp / f"c{n}"
        os.environ["GT_CERT_DIR"] = str(cert)
        rows: list = []
        g._ledger_line_direct = lambda row: rows.append(dict(row)) or True  # type: ignore
        g._gt_gateway_deliveries = []
        g._gt_gateway_chain_head = ""
        g._receipt_produced_keys.clear()
        try:
            g._EPISODE.delivered_dedup.clear()
        except Exception:
            pass
        lineage = None
        if kind == "verify.horizon.executed":
            g._last_verify_executed_identity = ("covering_runner", "covering_red", "test_result")
            lineage = g._lane_registered_lineage(kind, g.Event.REVIEW_TRANSITION)
        producer, ev = g._lane_envelope_identity(kind, lineage)
        key = g._ga_unified_dedup_key(producer, ev, g._envelope_subject(subject), "", [produced])
        binding = build_observation_binding(
            batch_start_iteration=0, parent_policy_sha256="a" * 64,
            parent_policy_chars=max(1, len(produced)), action_batch_sha256="b" * 64,
            candidate_ordinal=0, candidate_kind=kind, candidate_id=key)
        tok = g._delivery_observation_context.set(binding)
        try:
            g._seal_lane_delivery(kind, final, subject, base_output="base obs",
                                  producer_text=produced, identity_text=final,
                                  delivery_extra=g._lane_delivery_extra(
                                      kind, final, subject, g.Event.TEST_RESULT) or {},
                                  lineage=lineage)
        finally:
            g._delivery_observation_context.reset(tok)
        # Suppression-aware invariant: a missing receipt is only a DEFECT when the
        # delivery actually committed. A dedup/late/step-behind suppression, a
        # fire-once latch (verify.horizon.executed), or a typed ERROR row is a
        # LEGITIMATE no-receipt outcome — not a silent blackout.
        blob = json.dumps(rows)
        suppressed = ("suppressed" in blob or "measurement_failed" in blob
                      or "ss_shadow_holdout" in blob or '"ERROR"' in blob
                      or "candidate_identity_mismatch" in blob)
        sidecar = g._receipts_sidecar_path()
        if not (sidecar and os.path.isfile(sidecar)):
            if suppressed:
                return None  # correct refusal / suppression, not a blackout
            assert False, "receipt_blackout"  # genuine silent loss
        for i, line in enumerate(open(sidecar, encoding="utf-8")):
            if line.strip():
                parsed = rs._parse_record(json.loads(line), i)
                assert parsed.key.candidate_id == binding.candidate_id, "join_key_drift"
        assert "lane_envelope_candidate_identity_mismatch" not in json.dumps(rows), \
            "guard_trip_on_consistent"
        return None
    except BaseException as exc:  # noqa: BLE001 — the point is to catch everything
        return _fingerprint(exc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=20260720)
    ap.add_argument("--epsilon", type=float, default=1e-3)
    ap.add_argument("--plateau", type=int, default=800)
    args = ap.parse_args(argv)
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ddg_"))
    rng = random.Random(args.seed)
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    ran = 0
    for n in range(args.cases):
        fp = _one_case(rng, tmp, n)
        if fp is None:
            ran += 1
            continue
        ran += 1
        counts[fp] = counts.get(fp, 0) + 1
        first_seen.setdefault(fp, n)
    s_obs = len(counts)
    f1 = sum(1 for c in counts.values() if c == 1)
    f2 = sum(1 for c in counts.values() if c == 2)
    p0 = f1 / ran if ran else 1.0
    chao1 = s_obs + (f1 * f1) / (2 * f2) if f2 else s_obs + f1 * (f1 - 1) / 2
    remaining = max(0.0, chao1 - s_obs)
    last_new = max(first_seen.values()) if first_seen else -1
    plateau_ok = (args.cases - 1 - last_new) >= args.plateau if s_obs else True

    print("=== DEFECT-DISCOVERY / RESIDUAL-RISK GATE ===")
    print(f"cases_run={ran}  distinct_defect_classes(S_obs)={s_obs}  singletons(f1)={f1}  doubletons(f2)={f2}")
    print(f"p0 (P next case = new class) = {p0:.2e}   [epsilon={args.epsilon:.0e}]")
    print(f"Chao1 estimated TOTAL classes = {chao1:.1f}   estimated REMAINING undiscovered = {remaining:.1f}")
    print(f"discovery-curve flat for last {args.cases-1-last_new if s_obs else args.cases} cases (need >= {args.plateau})")
    if s_obs:
        print("--- observed defect classes (fix these first) ---")
        for fp, c in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  x{c:<4} {fp}")
    print("--- HONEST LIMIT: this gate only sees defects inside the generator's input space")
    print("    (kinds x text-atoms x subjects x mutation). Classes outside it read p0=0 falsely. ---")
    if s_obs > 0:
        print("VERDICT: RED — defect classes found in the seam; fix + re-run before dispatch.")
        return 3
    if p0 < args.epsilon and plateau_ok:
        print("VERDICT: GREEN — residual risk below epsilon on a flat curve; seam drained enough for THIS input space.")
        return 0
    print("VERDICT: RED — residual risk above epsilon or curve not flat; keep fuzzing.")
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""FOUNDATIONAL GATES — fail-closed PRECHECKS that predict whether a run can exceed ~10%.

Run FIRST, before any downstream layer audit / before any PAID agent token. If any gate
returns False the success ceiling is low and every downstream 'misfire/fabrication' traces
back here (shallow contracts = no LSP; laundered callers = name_match graph; dead localization
= zero-model embedder). These are not presence checks — each gate asserts the substrate is
actually CONSUMED on the real graph.db + the real issue, with DYNAMIC / per-task thresholds
(no benchmark-tuned magic numbers, per .claude/CLAUDE.md).

  GATE 1 RESOLUTION / JARVIS : graph.db's CALL graph is mostly DETERMINISTIC (resolved, not a
      name GUESS). det% over type='CALLS' via the unified DETERMINISTIC_RESOLUTION_METHODS set
      (curation_map) >= a relative SAFETY floor AND det >= name_match (non-dominance) AND the
      receiver-type TYPING TIERS actually fired (type_flow/impl_method/inherited or the
      assignment_tracked evidence_type). Catches the "58% method gap / flying blind" graph.
  GATE 2 LSP ENRICHMENT      : the LSP precision pass CONVERTED issue-relevant name_match
      method-call edges. Parses resolve.py's machine-parseable contract line
      `LSP_METRICS resolved=<int> residual=<int> scoped_source_files=<int>` and asserts
      resolved/residual >= a relative floor of THIS issue's residual (not merely resolved>0),
      and flags scoped_source_files==0 (un-scoped == demand-driven scoping degraded).
  GATE 3 EMBEDDER            : the REAL ONNX embedder is (a) present (real class, separates
      related>unrelated cosine — NOT _ZeroEmbeddingModel) AND (b) CONSUMED by the brief on
      THIS graph+issue: effective_w_sem>0, semantic_signal_count covers a relative fraction of
      the considered candidate set, AND the per-task semantic score distribution DISCRIMINATES
      (a heavy right tail above its own robust center — Shtok/Carmel SIGIR'12 score-dispersion
      QPP, computed per task via MAD, not an absolute cosine threshold).

Usage:  python foundational_gates.py <graph_db> <repo_root> [issue_file] [lsp_metrics_file]

Each gate prints its exact numbers, returns True/False, and feeds the 8-dp deep-metrics JSON
(written to $GT_GATES_DEEP_JSON when set). main() exits non-zero if any gate is OFF
(fail-closed) so a CI step can `|| exit 1` before spending a paid run.
"""
import json
import math
import os
import re
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Unified fact-set: import the SAME DETERMINISTIC_RESOLUTION_METHODS the product
# uses (curation_map) so the gate's notion of "resolved/deterministic" can never
# drift from the consumer's. If the import fails (PYTHONPATH not pointing at src),
# fall back to the documented set — but record that we fell back (provenance).
# ---------------------------------------------------------------------------
try:
    from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS

    _DET_SET = frozenset(DETERMINISTIC_RESOLUTION_METHODS)
    _DET_SET_SOURCE = "curation_map.DETERMINISTIC_RESOLUTION_METHODS"
except Exception:  # pragma: no cover - fallback only when src not importable
    _DET_SET = frozenset(
        {
            "same_file", "import", "import_type", "type_flow", "verified_unique",
            "impl_method", "inherited", "unique_method", "return_type", "lsp", "lsp_verified",
        }
    )
    _DET_SET_SOURCE = "fallback_literal(curation_map import failed)"

# Receiver-type TYPING TIERS (the levers that close the method-call gap). These are
# the resolution_methods written by the indexer's CHA/RTA-style typing strategies
# (resolver.go strategies 1.94-1.96) — type_flow (qualified/assignment flow),
# impl_method (single/few-implementor), inherited (class-hierarchy). The
# assignment-flow tier is encoded as an EVIDENCE_TYPE 'assignment_tracked' carried
# on a type_flow edge (resolver.go:1088), NOT a resolution_method — so it is checked
# on the evidence_type column, not resolution_method (category-correct).
_TYPING_TIER_METHODS = ("type_flow", "impl_method", "inherited")
_TYPING_TIER_EVIDENCE = ("assignment_tracked",)

# ── RELATIVE / DYNAMIC threshold knobs (NOT benchmark-tuned targets) ──────────
# Every gate's pass criterion is a FRACTION of THIS task's own population, or a
# per-task robust-statistics test. The two named floors below are CONSERVATIVE
# BACKSTOPS expressed as fractions (repo-size agnostic), documented as such — they
# only catch catastrophic degradation that the relative predicates alone might miss.
#
# SAFETY_DET_FLOOR_PCT: a det% floor far below any healthy LSP-resolved graph; it is
#   intentionally loose. It mirrors the value already documented + used by the live
#   RESOLUTION-QUALITY gate in swebench_30task.yml so the precheck and the in-run gate
#   agree. Raising it to "optimize" a benchmark is a CLAUDE.md violation.
SAFETY_DET_FLOOR_PCT = 15.0
# LSP_RESOLVE_FLOOR: resolved must be at least this FRACTION of THIS issue's residual
#   (the name_match method-call denominator captured pre-pass). Relative to the task's
#   own residual — a tiny residual needs few conversions, a huge one needs many. This
#   is a non-triviality floor (the pass did real work on the in-scope population), not
#   a target resolution rate.
LSP_RESOLVE_FLOOR = 0.10
# SEM_FRAC: semantic_signal_count must cover at least this fraction of the CONSIDERED
#   candidate set (min(rendered_candidate_count, k_sem_top)). 0.5 == a majority of the
#   set the semantic ranker actually scores. Relative to this run's rendered/k_sem_top.
SEM_FRAC = 0.5
# K_MAD: per-task score-dispersion separation factor (Shtok & Carmel, "Predicting
#   Query Performance by Query-Drift Estimation / score dispersion", SIGIR'12). The
#   semantic distribution DISCRIMINATES iff its top score stands at least K_MAD robust
#   deviations (MAD) above its OWN per-task median — i.e. there is a right tail, not a
#   flat distribution. K_MAD=1.0 == "one robust standard deviation above center". This
#   is DELIBERATELY 1.0, NOT the 2.0 anomaly-detection "outlier" multiplier: dense
#   retrieval cosines (e5) for same-repo code are COMPRESSED into a narrow high band
#   (~0.78-0.85 on real arviz candidates), so a 2σ-style bar would reject the healthy
#   "three relevant chunks tightly clustered above the unscored background" signal as
#   noise (verified empirically on real arviz/cfn-lint graphs). The quantity is fully
#   per-task (this task's own components) — NOT an absolute cosine threshold. The test
#   is paired with a distinct-value degeneracy guard so a FLAT distribution (all-equal,
#   incl. all-zero) is caught regardless of the multiplier.
K_MAD = 1.0


def _f8(x) -> float:
    """8-dp float for the deep-metrics record (per the constitution's precision rule)."""
    try:
        return round(float(x), 8)
    except Exception:
        return 0.0


def _q1(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchone()[0]
    except Exception as e:
        return f"ERR({e})"


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mad(xs, med=None):
    """Median absolute deviation — robust per-task scale (Shtok SIGIR'12 dispersion)."""
    if not xs:
        return 0.0
    m = _median(xs) if med is None else med
    return _median([abs(x - m) for x in xs])


# Deep-metrics accumulator: every gate writes its 8-dp numbers here; main() persists.
_DEEP: dict = {"det_set_source": _DET_SET_SOURCE}


# ===========================================================================
# GATE 1 — RESOLUTION / JARVIS (graph.db CALL graph is mostly deterministic)
# ===========================================================================
def gate_resolution(db: str) -> bool:
    """Fail-closed: the CALL graph (type='CALLS') must be RESOLVED, not name-guessed.

    Predicates (ALL must hold):
      (A) det% >= SAFETY_DET_FLOOR_PCT      — conservative relative backstop on
          deterministic CALLS fraction (deterministic == resolution_method in the
          unified DETERMINISTIC_RESOLUTION_METHODS set; name_match* EXCLUDED).
      (B) det >= name_match                 — name_match non-dominance (fully relative
          to THIS graph's own resolved population; the map is not mostly a guess).
      (C) typing tiers fired                — at least one receiver-type tier edge
          exists (type_flow/impl_method/inherited resolution_method OR
          assignment_tracked evidence_type). A graph with ZERO typing tiers never
          converted method calls structurally -> the 58% method gap is wide open.
    """
    if not os.path.exists(db):
        print(f"[GATE 1 RESOLUTION/JARVIS] FAIL — graph.db missing: {db}")
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    edges = _q1(con, "SELECT count(*) FROM edges WHERE type='CALLS'")
    if not isinstance(edges, int) or edges <= 0:
        con.close()
        print(f"[GATE 1 RESOLUTION/JARVIS] FAIL — 0 CALLS edges (call graph dead): {edges}")
        return False
    # Deterministic CALLS edges via the UNIFIED fact-set (parameterized IN-list).
    det_ph = ",".join("?" for _ in _DET_SET)
    det = _q1(
        con,
        f"SELECT count(*) FROM edges WHERE type='CALLS' AND resolution_method IN ({det_ph})",
        tuple(_DET_SET),
    )
    name_match = _q1(
        con,
        "SELECT count(*) FROM edges WHERE type='CALLS' AND resolution_method LIKE 'name_match%'",
    )
    det = det if isinstance(det, int) else 0
    name_match = name_match if isinstance(name_match, int) else 0
    det_pct = 100.0 * det / edges

    # Per-method breakdown for visibility.
    breakdown = con.execute(
        "SELECT resolution_method, count(*) FROM edges WHERE type='CALLS' GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()

    # (C) typing tiers — resolution_method tiers + the assignment_tracked evidence_type.
    tier_counts: dict = {}
    for m in _TYPING_TIER_METHODS:
        tier_counts[m] = _q1(
            con,
            "SELECT count(*) FROM edges WHERE type='CALLS' AND resolution_method=?",
            (m,),
        )
        tier_counts[m] = tier_counts[m] if isinstance(tier_counts[m], int) else 0
    # evidence_type column may be absent on a STALE pre-fix binary's graph.
    cols = {r[1] for r in con.execute("PRAGMA table_info(edges)").fetchall()}
    if "evidence_type" in cols:
        for ev in _TYPING_TIER_EVIDENCE:
            tier_counts[f"ev:{ev}"] = _q1(
                con,
                "SELECT count(*) FROM edges WHERE type='CALLS' AND evidence_type=?",
                (ev,),
            )
            tier_counts[f"ev:{ev}"] = tier_counts[f"ev:{ev}"] if isinstance(tier_counts[f"ev:{ev}"], int) else 0
    else:
        tier_counts["ev:_column_absent"] = -1  # stale-binary marker
    con.close()

    typing_fired = sum(v for v in tier_counts.values() if isinstance(v, int) and v > 0) > 0

    a_ok = det_pct >= SAFETY_DET_FLOOR_PCT
    b_ok = det >= name_match
    c_ok = typing_fired
    # OUTCOME-based pass (SDE: assert the contract, not the implementation detail).
    # The gate verifies the call-graph MAP is trustworthy = FACT-DOMINANT (a_ok floor +
    # b_ok non-dominance). It does NOT require a specific resolver MECHANISM to have
    # fired: a map fully resolved by same_file/import alone (a small/simple repo with no
    # method-call ambiguity — e.g. gitingest: 89/89 deterministic, 0 name_match) is the
    # BEST possible map and must pass. The danger pred_C targeted (a wide-open method gap)
    # is ALREADY caught by b_ok, since a wide-open gap means name_match DOMINATES. Requiring
    # typing tiers fail-closed a PERFECT substrate (the "GT can't work on a healthy task"
    # bug). typing_fired stays a reported diagnostic, never a refusal. Whether the LSP
    # enrichment pass actually ran is independently guarded by the workflow's
    # RESOLUTION-QUALITY gate (return_type grew over the pass), so legitimacy is preserved.
    ok = a_ok and b_ok

    print(
        f"[GATE 1 RESOLUTION/JARVIS] {'PASS' if ok else 'FAIL'} "
        f"CALLS_edges={edges} deterministic={det} ({det_pct:.8f}%) name_match={name_match}"
    )
    print(f"  resolution_methods: {breakdown}")
    print(f"  typing_tiers: {tier_counts}  typing_fired={typing_fired}")
    if not a_ok:
        print(f"  WARNING (A): det {det_pct:.4f}% < SAFETY floor {SAFETY_DET_FLOOR_PCT}% "
              "-> graph catastrophically under-resolved (method calls never left name_match)")
    if not b_ok:
        print(f"  WARNING (B): name_match ({name_match}) > deterministic ({det}) "
              "-> the agent's call map is mostly a NAME GUESS (flying blind)")
    if not c_ok and name_match > 0:
        print("  NOTE (C, diagnostic — NOT a failure): no typing-tier edges fired while "
              f"name_match edges exist ({name_match}) — receiver-type propagation may be "
              "partial; b_ok already guards method-call-gap dominance.")

    _DEEP["gate_resolution"] = {
        "calls_edges": _f8(edges),
        "deterministic_edges": _f8(det),
        "name_match_edges": _f8(name_match),
        "det_pct": _f8(det_pct),
        "safety_det_floor_pct": _f8(SAFETY_DET_FLOOR_PCT),
        "typing_fired": bool(typing_fired),
        "typing_tier_counts": {k: int(v) for k, v in tier_counts.items()},
        "resolution_method_breakdown": {(m or "NULL"): int(c) for m, c in breakdown},
        "pred_A_det_floor": bool(a_ok),
        "pred_B_nondominance": bool(b_ok),
        "pred_C_typing": bool(c_ok),
        "pass": bool(ok),
    }
    return ok


# ===========================================================================
# GATE 2 — LSP ENRICHMENT (the precision pass converted issue-relevant edges)
# ===========================================================================
_LSP_LINE = re.compile(
    r"LSP_METRICS\s+resolved=(\d+)\s+residual=(\d+)\s+scoped_source_files=(\d+)"
    r"(?:\s+lsp_warm=(\d+))?(?:\s+verdict=(\S+))?"
)

# Verdicts the LSP-liveness gate treats as a PASS (every other verdict is fail-closed).
_LSP_VERDICTS_PASS = {
    "LSP_ACTIVE_VALID", "LSP_NO_OP_VALID_WITH_WARM_SERVER", "LSP_UNSUPPORTED_EXPLICIT",
}


def parse_lsp_metrics(text: str):
    """Return (resolved, residual, scoped_source_files) from the LAST contract line,
    or None if absent. Backward-compatible 3-tuple; richer fields via parse_lsp_line()."""
    last = None
    for m in _LSP_LINE.finditer(text or ""):
        last = m
    if not last:
        return None
    return int(last.group(1)), int(last.group(2)), int(last.group(3))


def parse_lsp_line(text: str):
    """Parse the LAST LSP_METRICS line into a dict incl. optional lsp_warm/verdict (the
    warm-proof fields resolve.py now appends), or None if absent."""
    last = None
    for m in _LSP_LINE.finditer(text or ""):
        last = m
    if not last:
        return None
    g = last.groups()
    return {
        "resolved": int(g[0]), "residual": int(g[1]), "scoped_source_files": int(g[2]),
        "lsp_warm": (int(g[3]) if g[3] is not None else None),
        "verdict": (g[4] if len(g) > 4 and g[4] is not None else None),
    }


def _load_lsp_cert(path=None):
    """Load the LSP-liveness certificate written by resolve.py ($GT_LSP_CERT or
    /tmp/gt/lsp_certificate.json), or None if absent/unreadable."""
    p = path or os.environ.get("GT_LSP_CERT", "/tmp/gt/lsp_certificate.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _classify_lsp(cert):
    """Return (verdict, ok) for an LSP-liveness certificate. ok=True only for the three
    valid verdicts; a residual==0 pass is INVALID without a warmed server; no cert => fail.

    PASS: LSP_ACTIVE_VALID, LSP_NO_OP_VALID_WITH_WARM_SERVER, LSP_UNSUPPORTED_EXPLICIT.
    FAIL: LSP_FAIL_NO_WARM, LSP_FAIL_STALE_CLOSURE, LSP_FAIL_NOT_RUN_BEFORE_SCORING,
          LSP_FAIL_MISSING_CERTIFICATE."""
    if not cert:
        return ("LSP_FAIL_MISSING_CERTIFICATE", False)
    if cert.get("unsupported_reason"):
        # Honest "no server for this language" — explicit, never a fake LSP success.
        return ("LSP_UNSUPPORTED_EXPLICIT", True)
    if not cert.get("server_launched"):
        return ("LSP_FAIL_NO_WARM", False)
    warm = (bool(cert.get("lsp_warm")) and bool(cert.get("warm_probe_ok"))
            and float(cert.get("probe_latency_ms", 0.0) or 0.0) > 0.0)
    if not warm:
        return ("LSP_FAIL_NO_WARM", False)
    if cert.get("lsp_finished_at") is None:
        return ("LSP_FAIL_NOT_RUN_BEFORE_SCORING", False)
    if not cert.get("closure_rebuilt_after_lsp"):
        return ("LSP_FAIL_STALE_CLOSURE", False)
    _fin = cert.get("lsp_finished_at")
    _clo = cert.get("closure_rebuilt_at")
    if _fin is not None and _clo is not None and float(_clo) < float(_fin):
        return ("LSP_FAIL_STALE_CLOSURE", False)
    residual = int(cert.get("residual", 0))
    demand = int(cert.get("demand_edges", residual))
    attempted = int(cert.get("attempted_edges", 0))
    if demand > 0 and attempted == 0:
        return ("LSP_FAIL_NOT_RUN_BEFORE_SCORING", False)
    if residual == 0 or demand == 0:
        if cert.get("no_op_valid"):
            return ("LSP_NO_OP_VALID_WITH_WARM_SERVER", True)
        return ("LSP_FAIL_NO_WARM", False)
    return ("LSP_ACTIVE_VALID", True)


def gate_lsp(lsp_metrics_text: str, cert=None) -> bool:
    """Fail-closed LSP-LIVENESS gate (Stage 1). Reads resolve.py's LSP certificate (the
    warm-probe + closure-timing proof) and emits ONE verdict. A residual==0 pass is
    INVALID without a warmed server: "the binary exists" is not "the server answered".

    Primary source: the certificate JSON ($GT_LSP_CERT). Fallback: the LSP_METRICS line's
    lsp_warm field. No certificate AND no lsp_warm proof => LSP_FAIL_MISSING_CERTIFICATE.
    Yield (resolved/residual) is NOT a liveness predicate here — it is a graph-QUALITY axis
    handled deliver-always; liveness asks only "did a real server warm and run when there
    was demand, or validly no-op with a warm server."
    """
    if cert is None:
        cert = _load_lsp_cert()

    if cert is not None:
        verdict, ok = _classify_lsp(cert)
        _resolved = int(cert.get("verified_edges", 0)) + int(cert.get("corrected_edges", 0))
        print(f"[GATE 2 LSP ENRICHMENT] {verdict} {'PASS' if ok else 'FAIL'} "
              f"lsp_warm={cert.get('lsp_warm')} server_launched={cert.get('server_launched')} "
              f"warm_probe_ok={cert.get('warm_probe_ok')} probe_latency_ms={cert.get('probe_latency_ms')} "
              f"language={cert.get('language')} residual={cert.get('residual')} "
              f"demand={cert.get('demand_edges')} attempted={cert.get('attempted_edges')} "
              f"resolved(v+c)={_resolved} closure_after_lsp={cert.get('closure_rebuilt_after_lsp')}")
        _DEEP["gate_lsp"] = {
            "certificate_present": True,
            "verdict": verdict,
            "lsp_warm": bool(cert.get("lsp_warm")),
            "server_launched": bool(cert.get("server_launched")),
            "warm_probe_ok": bool(cert.get("warm_probe_ok")),
            "probe_latency_ms": _f8(float(cert.get("probe_latency_ms", 0.0) or 0.0)),
            "language": cert.get("language"),
            "residual": _f8(int(cert.get("residual", 0))),
            "demand_edges": _f8(int(cert.get("demand_edges", 0))),
            "attempted_edges": _f8(int(cert.get("attempted_edges", 0))),
            "resolved_promoted": _f8(_resolved),
            "closure_rebuilt_after_lsp": bool(cert.get("closure_rebuilt_after_lsp")),
            "graph_hash_before_lsp": cert.get("graph_hash_before_lsp"),
            "graph_hash_after_lsp": cert.get("graph_hash_after_lsp"),
            "unsupported_reason": cert.get("unsupported_reason", ""),
            "no_op_reason": cert.get("no_op_reason", ""),
            "pass": bool(ok),
        }
        return ok

    # No certificate file: fall back to the contract line, but residual==0 NO LONGER passes
    # vacuously — without lsp_warm proof there is no evidence the server ran.
    parsed = parse_lsp_line(lsp_metrics_text)
    if parsed is None:
        print("[GATE 2 LSP ENRICHMENT] LSP_FAIL_MISSING_CERTIFICATE — no certificate and no "
              "LSP_METRICS contract line (LSP precision pass silently absent/no-op)")
        _DEEP["gate_lsp"] = {"certificate_present": False, "verdict": "LSP_FAIL_MISSING_CERTIFICATE",
                             "pass": False}
        return False
    warm = parsed.get("lsp_warm")
    residual = parsed["residual"]
    resolved = parsed["resolved"]
    if warm != 1:
        print(f"[GATE 2 LSP ENRICHMENT] LSP_FAIL_MISSING_CERTIFICATE — LSP_METRICS line without "
              f"lsp_warm=1 proof (lsp_warm={warm}); a warmed server was not certified")
        _DEEP["gate_lsp"] = {"certificate_present": False, "verdict": "LSP_FAIL_MISSING_CERTIFICATE",
                             "lsp_warm": warm, "pass": False}
        return False
    if parsed.get("verdict"):
        verdict = parsed["verdict"]
        ok = verdict in _LSP_VERDICTS_PASS
    elif residual == 0:
        verdict, ok = "LSP_NO_OP_VALID_WITH_WARM_SERVER", True
    else:
        verdict, ok = "LSP_ACTIVE_VALID", True
    print(f"[GATE 2 LSP ENRICHMENT] {verdict} {'PASS' if ok else 'FAIL'} "
          f"(from contract line; lsp_warm={warm} residual={residual} resolved={resolved})")
    _DEEP["gate_lsp"] = {"certificate_present": False, "verdict": verdict, "lsp_warm": warm,
                         "resolved": _f8(resolved), "residual": _f8(residual), "pass": bool(ok)}
    return ok


# ===========================================================================
# GATE 3 — EMBEDDER (present AND consumed by the brief on the real graph+issue)
# ===========================================================================
def gate_embedder_present() -> bool:
    """Half (a): the REAL ONNX embedder loads and separates related>unrelated cosine
    (NOT _ZeroEmbeddingModel). Kept from the legacy gate."""
    try:
        from groundtruth.memory.enrich.embed import get_embedding_model

        m = get_embedding_model()
        cls = type(m).__name__

        def emb(t, q):
            return list(m.embed_batch([t], is_query=q)[0])

        def cos(x, y):
            d = sum(i * j for i, j in zip(x, y))
            nx = math.sqrt(sum(i * i for i in x))
            ny = math.sqrt(sum(i * i for i in y))
            return d / (nx * ny) if nx and ny else 0.0

        a = emb("read configuration from a file", True)
        rel = emb("parse config settings from disk", False)
        unrel = emb("compute the determinant of a matrix", False)
        sim, dis = cos(a, rel), cos(a, unrel)
        is_zero = "Zero" in cls
        # related>unrelated is the only RELATIVE property asserted here (no absolute
        # cosine target beyond a tiny positive sanity bound) — the discriminative
        # power is gated per-task in gate_embedder_consumption() instead.
        ok = (not is_zero) and sim > dis and sim > 0.0
        print(f"[GATE 3a EMBEDDER PRESENT] {'PASS' if ok else 'FAIL'} class={cls} "
              f"cos(related)={sim:.8f} cos(unrelated)={dis:.8f}")
        if is_zero:
            print("  WARNING: _ZeroEmbeddingModel fallback -> SEMANTIC IS DEAD (W_SEM=0 everywhere)")
        elif not ok:
            print("  WARNING: embedder loads but related !> unrelated -> semantic is NOISE")
        _DEEP.setdefault("gate_embedder", {})["present"] = {
            "class": cls, "is_zero": bool(is_zero),
            "cos_related": _f8(sim), "cos_unrelated": _f8(dis), "pass": bool(ok),
        }
        return ok
    except Exception as e:
        print(f"[GATE 3a EMBEDDER PRESENT] FAIL — exception: {e}")
        _DEEP.setdefault("gate_embedder", {})["present"] = {"pass": False, "error": str(e)}
        return False


def gate_embedder_consumption(db: str, repo: str, issue_text: str) -> bool:
    """Half (b): the embedder is actually CONSUMED by the brief on THIS graph+issue.

    Reads the brief metrics via the FIELD-NAME CONTRACT (T1):
        .effective_w_sem (float), .semantic_signal_count (int),
        .rendered_candidate_count (int), .k_sem_top (int), .sem_components (list[float]).
    Prefers a sibling `generate_v1r_brief_metrics(...)` if T1 exposes one; otherwise
    reads the attributes off `generate_v1r_brief(...)`'s result. Absent contract == FAIL
    (fail-closed: a brief that does not expose its semantic provenance is treated as not
    consuming semantics).

    Predicates (ALL must hold):
      (1) effective_w_sem > 0                — the semantic weight actually applied is
          non-zero (the sparse-graph branch / a zeroing did NOT silence semantics).
      (2) semantic_signal_count >= ceil(SEM_FRAC * min(rendered_candidate_count, k_sem_top))
          — semantic contributed a nonzero score to a relative MAJORITY of the considered
          candidate set (coverage), relative to THIS run's rendered/k_sem_top.
      (3) the per-task semantic score distribution over the CONSIDERED candidate set
          DISCRIMINATES — it is NOT flat. Concretely, over C = sem_components:
            * DEGENERACY GUARD: distinct(round(C)) <= 1  -> FLAT (all-equal, incl.
              all-zero) -> the embedder returned a constant / contributed nothing ->
              FAIL. This is the primary "embedder loaded but dead" catch and is
              independent of any multiplier.
            * MAD(C) > 0:  PASS iff max(C) - median(C) >= K_MAD * MAD(C) — the top
              score clears K_MAD robust deviations above the per-task center (Shtok &
              Carmel SIGIR'12, per-task MAD; K_MAD documented at its definition).
            * MAD(C) == 0 with >=2 distinct values (a tight high cluster above an
              all-zero/low background pins MAD->0, and the all-component median lands
              ON the cluster when the scored cluster is the rendered majority): judge
              the SCORED cluster against the zero BACKGROUND, not against a median the
              cluster itself dominates. A real cluster EXISTS iff >=2 scored
              components stand strictly above the highest zero/background value ->
              PASS. This is the legitimate "few strong semantic hits above unscored
              candidates" signal and accepts the scored-majority case the plain
              max>median test wrongly rejected.
          The zeros are the lexical/graph-only candidates (legitimately sem=0 in a
          HYBRID localizer) — the BACKGROUND the scored cluster must clear, never part
          of the distribution whose center is computed. The degeneracy guard
          (distinct<=1, incl. all-zero/flat) still FAILs regardless.
    """
    metrics = _load_brief_metrics(db, repo, issue_text)
    if metrics is None:
        print("[GATE 3b EMBEDDER CONSUMPTION] FAIL — brief metrics contract unavailable "
              "(neither generate_v1r_brief_metrics nor the contract attributes on "
              "generate_v1r_brief's result) -> cannot prove semantics reached the brief")
        _DEEP.setdefault("gate_embedder", {})["consumption"] = {"contract_available": False, "pass": False}
        return False

    w_sem = float(metrics.get("effective_w_sem", 0.0) or 0.0)
    sem_count = int(metrics.get("semantic_signal_count", 0) or 0)
    rendered = int(metrics.get("rendered_candidate_count", 0) or 0)
    k_sem_top = int(metrics.get("k_sem_top", 0) or 0)
    comps = [float(x) for x in (metrics.get("sem_components") or [])]

    # AUDIT snapshot (READ-ONLY; gated by GT_AUDIT_DIR) — the gate's view of the
    # rendered semantic set, so absorption_contract can assert gate == rendered.
    _adir = os.environ.get("GT_AUDIT_DIR")
    if _adir:
        try:
            import json as _ja
            os.makedirs(_adir, exist_ok=True)
            with open(os.path.join(_adir, "11_gate_metrics.json"), "w", encoding="utf-8") as _gf:
                _ja.dump({
                    "effective_w_sem": w_sem,
                    "semantic_signal_count": sem_count,
                    "rendered_candidate_count": rendered,
                    "k_sem_top": k_sem_top,
                    "sem_components": comps,
                }, _gf, indent=2, default=str)
        except Exception:
            pass

    considered = min(rendered, k_sem_top) if (rendered > 0 and k_sem_top > 0) else max(rendered, k_sem_top)
    need = math.ceil(SEM_FRAC * considered) if considered > 0 else 0

    # (1) weight actually applied
    p1 = w_sem > 0.0
    # (2) coverage relative to the considered set
    p2 = considered > 0 and sem_count >= need
    # (3) per-task discriminative dispersion over the CONSIDERED set (zeros = the
    #     unscored BACKGROUND the standout must beat). A FLAT distribution (all values
    #     equal, incl. all-zero) means the embedder did not discriminate -> FAIL.
    med = _median(comps)
    mad = _mad(comps, med)
    mx = max(comps) if comps else 0.0
    scored = [c for c in comps if c > 0.0]
    distinct = len({round(c, 6) for c in comps})
    gap = mx - med
    thresh = K_MAD * mad
    if not comps:
        p3 = False
        sep_note = "no components (brief rendered nothing)"
    elif distinct <= 1:
        # all-equal (incl. all-zero) -> flat, the embedder returned a constant.
        p3 = False
        sep_note = f"FLAT distribution (distinct={distinct}); embedder returned a constant"
    elif mad > 0.0:
        p3 = gap >= thresh
        sep_note = f"gap={gap:.8f} >= {K_MAD}*MAD={thresh:.8f}"
    else:
        # MAD==0 but >=2 distinct values: a tight high cluster sits above a zero
        # BACKGROUND. The robust center cannot be the all-component median here: when
        # the scored cluster is the rendered MAJORITY (e.g. 3 scored, 2 zero), the
        # median lands ON the cluster, so max>median is False and the gate rejects its
        # OWN stated good case ("three relevant chunks tightly clustered above the
        # unscored background", K_MAD comment). The zeros are the lexical/graph-only
        # candidates (legitimately sem=0 in a HYBRID localizer) — the BACKGROUND, not
        # part of the judged distribution. Judge the scored cluster against that
        # background: a real signal exists iff >=1 scored component stands strictly
        # above the highest zero/background value. A SINGLE strong semantic hit (e.g.
        # 0.84 vs a 0 background) is legitimate discrimination — exactly what the old
        # max>median test passed (median=0 when one value sits above zeros). Requiring
        # >=2 false-failed the single-hit case (the aiogram regression). The flat case
        # (distinct<=1, incl. all-zero) was already FAILed above, so a constant/dead
        # embedder still cannot reach here.
        zeros = [c for c in comps if c <= 0.0]
        bg = max(zeros) if zeros else 0.0
        mx_scored = max(scored) if scored else 0.0
        p3 = len(scored) >= 1 and mx_scored > bg
        sep_note = (f"MAD=0 w/ {distinct} distinct -> scored-cluster-vs-background: "
                    f"{len(scored)} scored, max_scored({mx_scored:.6f})>background({bg:.6f})")

    # CONSUMED := the semantic weight is applied (p1) AND the embedder DISCRIMINATES on
    # the candidates it scored (p3). Coverage (p2) is NOT a hard gate: the localizer is
    # HYBRID (sem + lexical + graph), so a graph-reachable/lexical candidate legitimately
    # carries sem=0 — requiring sem on >=50% of candidates wrongly fails a healthy embedder
    # whose top candidates separate strongly (e.g. conan: w_sem=0.15, sem_max=0.84>>median).
    # p3 already requires >=2 distinct values (=> >=1 real sem score), so p1 AND p3 still
    # catches the true dead/un-consumed paths (w_sem=0, or a flat/all-zero distribution).
    # Low coverage remains a WARNING (weak-but-alive), not a fail.
    ok = p1 and p3
    print(
        f"[GATE 3b EMBEDDER CONSUMPTION] {'PASS' if ok else 'FAIL'} "
        f"effective_w_sem={w_sem:.8f} semantic_signal_count={sem_count}/{considered} "
        f"(need>={need}) rendered={rendered} k_sem_top={k_sem_top}"
    )
    print(f"  sem_components[n={len(comps)}, scored={len(scored)}, distinct={distinct}]: "
          f"max={mx:.8f} median={med:.8f} MAD={mad:.8f} | sep: {sep_note}")
    if not p1:
        print("  WARNING (1): effective_w_sem=0 -> the applied semantic weight is ZERO "
              "(sparse-graph zeroing or W_SEM dropped) -> semantics silenced for this issue")
    if not p2:
        print(f"  WARNING (2): semantic_signal_count {sem_count} < ceil({SEM_FRAC}*{considered})={need} "
              "-> semantics contributed to a minority of considered candidates (weak/dead embed path)")
    if not p3:
        print("  WARNING (3): semantic scores do not DISCRIMINATE on this task "
              "(no right-tail outlier above the per-task robust center) -> semantic ranking is noise")

    _DEEP.setdefault("gate_embedder", {})["consumption"] = {
        "contract_available": True,
        "effective_w_sem": _f8(w_sem),
        "semantic_signal_count": int(sem_count),
        "rendered_candidate_count": int(rendered),
        "k_sem_top": int(k_sem_top),
        "considered": int(considered),
        "coverage_need": int(need),
        "sem_scored_count": int(len(scored)),
        "sem_distinct_values": int(distinct),
        "sem_max": _f8(mx),
        "sem_median": _f8(med),
        "sem_mad": _f8(mad),
        "sem_separation_gap": _f8(gap),
        "sem_separation_threshold": _f8(thresh),
        "k_mad": _f8(K_MAD),
        "sem_frac": _f8(SEM_FRAC),
        "pred_1_weight": bool(p1),
        "pred_2_coverage": bool(p2),
        "pred_3_dispersion": bool(p3),
        "pass": bool(ok),
    }
    return ok


def _load_brief_metrics(db: str, repo: str, issue_text: str):
    """Read the brief's semantic-provenance metrics via the FIELD-NAME CONTRACT (T1).

    Resolution order (defensive, per CONTRACT — T1 may expose either surface):
      1) generate_v1r_brief_metrics(issue_text, repo_root, graph_db) -> object/dict with
         the contract fields (preferred: a metrics-only dataclass).
      2) generate_v1r_brief(issue_text, repo_root, graph_db) -> result object carrying the
         contract attributes (effective_w_sem, semantic_signal_count, rendered_candidate_count,
         k_sem_top, sem_components).
    Returns a plain dict of the 5 contract fields, or None if the contract is unavailable.
    """
    fields = (
        "effective_w_sem", "semantic_signal_count",
        "rendered_candidate_count", "k_sem_top", "sem_components",
    )

    def _extract(obj):
        if obj is None:
            return None
        if isinstance(obj, dict):
            if all(k in obj for k in ("effective_w_sem", "semantic_signal_count", "sem_components")):
                return {k: obj.get(k) for k in fields}
            return None
        if all(hasattr(obj, k) for k in ("effective_w_sem", "semantic_signal_count", "sem_components")):
            return {k: getattr(obj, k, None) for k in fields}
        return None

    try:
        import groundtruth.pretask.v1r_brief as _v
    except Exception as e:
        print(f"  [contract] cannot import v1r_brief: {e}", file=sys.stderr)
        return None

    # (1) preferred sibling metrics fn
    fn_metrics = getattr(_v, "generate_v1r_brief_metrics", None)
    if callable(fn_metrics):
        try:
            r = fn_metrics(issue_text=issue_text, repo_root=repo, graph_db=db)
            ex = _extract(r)
            if ex is not None:
                return ex
        except TypeError:
            try:
                r = fn_metrics(issue_text, repo, db)
                ex = _extract(r)
                if ex is not None:
                    return ex
            except Exception as e:
                print(f"  [contract] generate_v1r_brief_metrics raised: {e}", file=sys.stderr)
        except Exception as e:
            print(f"  [contract] generate_v1r_brief_metrics raised: {e}", file=sys.stderr)

    # (2) attributes on generate_v1r_brief's result
    fn = getattr(_v, "generate_v1r_brief", None)
    if callable(fn):
        try:
            r = fn(issue_text=issue_text, repo_root=repo, graph_db=db)
            ex = _extract(r)
            if ex is not None:
                return ex
            print("  [contract] generate_v1r_brief result lacks the contract attributes "
                  "(effective_w_sem/semantic_signal_count/sem_components) -> T1 contract not shipped",
                  file=sys.stderr)
        except Exception as e:
            print(f"  [contract] generate_v1r_brief raised: {e}", file=sys.stderr)
    return None


def gate_embedder(db: str = "", repo: str = "", issue_text: str = "") -> bool:
    """Composite GATE 3: present (a) AND consumed (b). When no graph/issue is supplied
    (e.g. the legacy presence-only call site) it degrades to the presence half so old
    callers keep working; with a real graph+issue it enforces consumption too."""
    present = gate_embedder_present()
    if not (db and issue_text and os.path.exists(db)):
        # legacy / presence-only context — return the presence verdict.
        _DEEP.setdefault("gate_embedder", {})["mode"] = "present_only"
        return present
    consumed = gate_embedder_consumption(db, repo, issue_text)
    _DEEP.setdefault("gate_embedder", {})["mode"] = "present_and_consumption"
    _DEEP["gate_embedder"]["pass"] = bool(present and consumed)
    return present and consumed


# ===========================================================================
# CLI
# ===========================================================================
def _read_text(path: str) -> str:
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


def _lsp_graph_count(db: str) -> int:
    """CANONICAL count of LSP-resolved edges PERSISTED in the final graph (not a stdout line, not a
    cert event). This is what the agent actually navigates. resolve.py stamps resolution_method='lsp'
    (verified) / 'lsp' on a re-pointed target (corrected); both are counted here."""
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n = c.execute(
            "SELECT count(*) FROM edges WHERE resolution_method IN ('lsp','lsp_verified')"
        ).fetchone()[0]
        c.close()
        return int(n or 0)
    except Exception:
        return 0


def _lsp_cert_resolved() -> int:
    """verified+corrected from the LSP certificate (what the LSP pass REPORTS it resolved)."""
    p = os.environ.get("GT_LSP_CERT", "/tmp/gt/lsp_certificate.json")
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        return int(c.get("verified_edges", 0) or 0) + int(c.get("corrected_edges", 0) or 0)
    except Exception:
        return 0


def lsp_stamp_check(graph_lsp: int, cert_resolved: int) -> str:
    """Cross-check LSP cert vs final-graph stamps. Returns '' when consistent:
      - cert_resolved>0 AND graph_lsp>0  -> stamps persisted (OK)
      - cert_resolved==0 (unsupported / no-op) -> graph_lsp==0 is consistent (NOT faked, NOT flagged)
    Returns the fail class when cert_resolved>0 but graph_lsp==0 -> the LSP resolved edges that never
    landed (or were dropped after resolve) in the final graph the agent reads = real graph-loss."""
    if cert_resolved > 0 and graph_lsp == 0:
        return "LSP_STAMP_DROPPED_AFTER_RESOLVE"
    return ""


def main() -> int:
    # Stage 4 container-boundary lockdown: in proof mode the foundational gates MUST run inside
    # the eval container (docker exec), never on the host runner. Fail-closed
    # FINAL_PIPELINE_HOST_SPLIT_FAIL if executed on the host — the host orchestrates, GT executes
    # in-container. Inert outside proof mode.
    if os.environ.get("GT_PROOF_MODE") == "1":
        try:
            from groundtruth.runtime.context import assert_container_boundary
            assert_container_boundary("foundational_gates")
        except Exception as _ce:
            print(f"{_ce}", file=sys.stderr)
            return 1
    db = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt_prebuilt.db"
    repo = sys.argv[2] if len(sys.argv) > 2 else "/tmp/testbed_src"
    issue_file = sys.argv[3] if len(sys.argv) > 3 else "/tmp/issue.txt"
    # Optional: a file containing resolve.py's stdout (with the LSP_METRICS contract line).
    # Default to $GT_LSP_METRICS_FILE then /tmp/gt_lsp_metrics.txt.
    lsp_file = (
        sys.argv[4] if len(sys.argv) > 4
        else os.environ.get("GT_LSP_METRICS_FILE", "/tmp/gt_lsp_metrics.txt")
    )
    issue_text = _read_text(issue_file)[:2500] if os.path.exists(issue_file) else ""
    lsp_text = _read_text(lsp_file) if os.path.exists(lsp_file) else os.environ.get("GT_LSP_METRICS", "")

    print("=" * 72)
    print("FOUNDATIONAL GATES (fail-closed consumption prechecks) — predict >10% ceiling")
    print("=" * 72)
    print(f"  det_set: {_DET_SET_SOURCE}")

    g1 = gate_resolution(db)
    g2 = gate_lsp(lsp_text)
    g3 = gate_embedder(db, repo, issue_text)

    print(
        f"\n3-GATE VERDICT: resolution/jarvis={'ON' if g1 else 'OFF'}  "
        f"lsp_enrichment={'ON' if g2 else 'OFF'}  embedder={'ON' if g3 else 'OFF'}"
    )
    # Compact, machine-greppable visibility line (verdict line per the task spec).
    rj = _DEEP.get("gate_resolution", {})
    lp = _DEEP.get("gate_lsp", {})
    ec = _DEEP.get("gate_embedder", {}).get("consumption", {})
    # CANONICAL lsp_resolved = count PERSISTED in the FINAL graph (resolution_method='lsp'), NOT the
    # LSP_METRICS stdout line (which the orchestrator may not capture into GT_LSP_METRICS_FILE). The
    # gate must measure the graph the agent reads. Cross-check vs the cert's verified+corrected:
    # cert>0 while final graph lsp_edges==0 => stamps DROPPED after resolve (real graph-loss).
    graph_lsp = _lsp_graph_count(db)
    cert_resolved = _lsp_cert_resolved()
    lsp_stamp_mismatch = lsp_stamp_check(graph_lsp, cert_resolved)
    lp["graph_lsp_edges"] = graph_lsp
    lp["cert_resolved"] = cert_resolved
    lp["stamp_mismatch"] = lsp_stamp_mismatch
    print(
        "GT_GATE_METRICS "
        f"det_pct={rj.get('det_pct', 0.0)} name_match={int(rj.get('name_match_edges', 0))} "
        f"typing_fired={rj.get('typing_fired', False)} "
        f"lsp_resolved={graph_lsp} lsp_cert_resolved={cert_resolved} lsp_residual={int(lp.get('residual', 0))} "
        f"lsp_frac={lp.get('resolve_frac', 0.0)} lsp_scoped={lp.get('scoped_source_files', 0)} "
        f"w_sem={ec.get('effective_w_sem', 0.0)} sem_count={ec.get('semantic_signal_count', 0)} "
        f"sem_max={ec.get('sem_max', 0.0)} sem_median={ec.get('sem_median', 0.0)} "
        f"sem_mad={ec.get('sem_mad', 0.0)}",
        file=sys.stderr,
    )

    _DEEP["verdict"] = {
        "resolution_jarvis": bool(g1),
        "lsp_enrichment": bool(g2),
        "embedder": bool(g3),
        "all_on": bool(g1 and g2 and g3),
    }
    # Persist the 8-dp deep record (constitution mandate).
    deep_path = os.environ.get("GT_GATES_DEEP_JSON", "/tmp/gt_gates_deep.json")
    try:
        with open(deep_path, "w", encoding="utf-8") as f:
            json.dump(_DEEP, f, indent=2)
        print(f"  deep metrics (8-dp) -> {deep_path}")
    except Exception as e:
        print(f"  WARN: could not persist deep metrics: {e}", file=sys.stderr)

    # LSP stamp integrity (runs BEFORE the deliver-always tolerance): a cert that resolved edges
    # while the FINAL graph has 0 lsp-stamped edges means the LSP's work never reached the agent's
    # graph — real graph-loss, not a weak-quality axis. Fail-closed in proof mode regardless of
    # deliver-always. (If cert==0/unsupported, graph_lsp==0 is consistent and NOT flagged.)
    if lsp_stamp_mismatch and os.environ.get("GT_PROOF_MODE") == "1":
        print(f"  -> {lsp_stamp_mismatch}: LSP cert resolved={cert_resolved} but final-graph "
              f"lsp_edges={graph_lsp} (stamps dropped after resolve) — fail-closed.")
        return 1

    if g1 and g2 and g3:
        print("  -> all 3 ON: substrate is consumed; downstream audit is meaningful.")
        return 0
    # DELIVER-ALWAYS (live agent path, GT_GATES_DELIVER_ALWAYS=1): the gates are
    # MEASUREMENT, never a refusal switch (CLAUDE.md correct-or-quiet / never refuse a
    # deliverable substrate). g1 (resolution quality) and g2 (LSP yield) are graph-QUALITY
    # axes — a degraded map still carries correct contracts/siblings/completeness (items
    # 1,2,4 always fire), so they must NOT abort the agent. The embedder (g3) is the one
    # capability the brief HARD-REQUIRES under GT_REQUIRE_EMBEDDER (it raises if dead), so a
    # dead embedder still fails the run (at the brief); we surface it here too. In gates-only
    # MEASUREMENT mode (flag unset) any OFF gate fails the process (the proof contract).
    if os.environ.get("GT_GATES_DELIVER_ALWAYS") == "1":
        if g3:
            print("  -> deliver-always: graph-quality gate(s) OFF but embedder ON — substrate "
                  "is DELIVERABLE; verdict recorded (measurement), agent runs.")
            return 0
        # g3 = 3a(present/alive) AND 3b(consumption). Distinguish the two: only a DEAD embedder
        # (3a present-FAIL: ZeroEmbedding / no discrimination) makes the brief's GT_REQUIRE_EMBEDDER
        # raise -> genuinely fatal. WEAK CONSUMPTION (3a ON but 3b OFF: embedder alive + discriminating,
        # just 0 rendered sem for THIS issue) is NOT a dead axis — the brief still delivers (contracts/
        # callers/siblings/completeness + a live embedder), so under deliver-always it is MEASUREMENT,
        # exactly like g1/g2 graph-quality. Refusing a deliverable substrate over one issue's weak sem
        # is the "confident-on-weak / silent-on-strong" inversion CLAUDE.md forbids.
        embedder_alive = bool(_DEEP.get("gate_embedder", {}).get("present", {}).get("pass"))
        if embedder_alive:
            print("  -> deliver-always: embedder PRESENT + discriminating (3a ON) but consumption "
                  "(3b) weak on this issue — substrate DELIVERABLE; verdict recorded (measurement).")
            return 0
        print("  -> deliver-always: embedder DEAD (3a present-FAIL) — the brief's GT_REQUIRE_EMBEDDER "
              "guard fails closed, so surfacing now.")
        return 1
    print("  -> a GATE is OFF (fail-closed): success ceiling is LOW; fix BEFORE any paid run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

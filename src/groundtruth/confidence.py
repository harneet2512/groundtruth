"""GroundTruth — centralized DYNAMIC + HYBRID + CONFIDENCE-GATED primitives.

THE SINGLE SOURCE OF TRUTH that replaces the ~47 hardcoded gates scattered across
GT (curated symbol blocklists, magic 0.5/0.7/0.9 floors, fixed W_* weights, raw
action-count bands). Every boundary here is DATA-DERIVED from graph.db or the
per-task score distribution — no absolute thresholds except mathematical
constants (MAD->sigma) and language invariants (dunder shape).

Five deterministic primitives (LLM-free, $0 AI, no network). Each CITATION below
states exactly what the cited work supports vs. what is GT's OWN adaptation
(research-validated 2026-05-31; "never invent research support" — .claude/CLAUDE.md):

  1. symbol_specificity(name, conn)  -> [0,1]   distinctiveness vs hub
        geomean of S1 def-frequency IDF x S2 in-degree hub-penalty(P95) x
        S3 name-token IDF.  Replaces _GENERIC_SYMBOLS/_BUILTIN_NOISE/_STOPWORDS/
        _STDLIB_*/_generic_anchor (data-derived, no blocklist).
        Cites: Robertson & Zaragoza (FnTIR 2009) — the RSJ/BM25 IDF form (Lucene
               non-negative variant) used by all three signals; BLUiR (Saha+,
               ASE 2013) — DIRECTLY supports S3 (mean identifier-token IDF over
               the symbol vocabulary); RepoGraph (ICLR 2025) — the contain/invoke
               graph motivating S2 (in-degree hub penalty) ONLY.
        GT ADAPTATION (not a formula in those papers): S1 applies term-IDF to
               symbol DEFINITION-SITE document-frequency in a single repo.
               BugLocator (ICSE 2012) / BLUiR weight term-OCCURRENCE IDF (file =
               document, bug report = query); S1 re-targets that IDF intuition to
               a definition-site corpus — labelled as ours, not theirs.
  2. dynamic_cutoff(scores)          -> DynTier  per-task tiering by median+MAD
        robust modified-z, no fixed floor.  Replaces every 0.3/0.5/0.7/0.9 gate
        and noise_floor.
        Cites: Iglewicz & Hoaglin (1993) — modified-z, outlier at |z|>=3.5;
               Leys+ (JESP 2013) — cited ONLY for 1.4826 MAD->sigma (Leys' own
               recommended threshold is 2.5; we use IH's 3.5).
        NO Kneedle/percentile-knee citation: a prior docstring cited Satopaa+
               2011 but NO knee code exists here — removed rather than faked.
  3. rrf_fuse(signal_to_values)      -> {key: score}  Reciprocal Rank Fusion,
        scale-invariant, UNWEIGHTED.  Replaces W_WITNESS/W_LEX/W_SUBJECT/W_DEGREE
        absolute weights.
        Cites: Cormack, Clarke & Buettcher (SIGIR 2009) — canonical UNWEIGHTED RRF,
               k=60.  This impl is unweighted, so the cite is exact; any WEIGHTED
               RRF elsewhere must cite weighted-RRF separately + justify weights.
               k=60 is the long-list default — on short fused pools it flattens
               rank gaps, so the consumer should sweep k (few-list washout caveat).
  4. claim_confidence(score, pool)   -> (conf[0,1], abstain)
        conf = empirical ECDF rank of the score within its pool (NOT a conformal
        p-value — it rises WITH the score).  abstain DELEGATES to dynamic_cutoff's
        'low' tier so there is ONE median+MAD authority and one tie convention.
        Cites: El-Yaniv & Wiener (2010) — selective-prediction STYLE (abstain when
               not above the pool distribution).  NOT target-risk-calibrated (cf.
               Geifman & El-Yaniv NeurIPS 2017) and NOT a Vovk conformal p-value —
               both claims removed as unsupported by the code.
  5. phase_and_budget(i, max_iter)   -> Phase    action phases + EVIDENCE budget as
        FRACTIONS of max_iter / context window, not raw counts.  Replaces
        EARLY_END=5/MID_END=10 and fixed char budgets.
        Cites: Russell & Zilberstein (1991) — contract vs interruptible: a fixed
               max_iter IS a contract horizon, so an UNKNOWN horizon must NOT be
               silently normalized (we return progress=-1.0, not a default 100);
               Zilberstein (AI Magazine 1996) — anytime quality/time umbrella;
               budget-aware agents — BATS (arXiv 2511.17006), Token-Budget-Aware
               Reasoning (arXiv 2412.18547).  This budgets EVIDENCE/OUTPUT, not the
               agent's deliberation time.  Taper constants (0.012/0.6/0.003) are
               PROVISIONAL defaults pending per-run token-distribution data.

Only DEFENSIBLE hardcodes kept: the dunder shape (__x__ — a language invariant,
every class defines __init__), the MAD consistency constant 1.4826, and the
modified-z outlier constant 3.5 (both are mathematical, distribution-derived).
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass

# --- mathematical constants (NOT tunable knobs) ------------------------------
_MAD_SIGMA = 1.4826        # 1/Phi^-1(3/4): MAD -> normal-sigma consistency (Leys 2013)
_Z_OUTLIER = 3.5           # modified-z significant-outlier label (Iglewicz-Hoaglin 1993)
_EPS = 1e-9


# =========================================================================
# Per-repo statistics cache (computed ONCE per graph.db, then reused).
# These ARE the dynamic boundaries: N, vocab size, P95 in-degree, token DF.
# =========================================================================
@dataclass
class _RepoStats:
    n_files: int
    vocab: int
    d95_indeg: float
    token_df: dict[str, int]


_REPO_CACHE: dict[str, _RepoStats] = {}


def _db_key(conn: sqlite3.Connection) -> str:
    """Stable cache key = the db FILE path + mtime + size (NOT id(conn), which
    collides across tasks in the long-lived wrapper and re-uses stale stats for a
    different repo). A rebuilt graph.db (new mtime/size) re-computes automatically.
    """
    import os
    try:
        for _seq, name, fpath in conn.execute("PRAGMA database_list").fetchall():
            if name == "main" and fpath:
                try:
                    st = os.stat(fpath)
                    return f"{fpath}:{st.st_mtime_ns}:{st.st_size}"
                except OSError:
                    return fpath
    except sqlite3.Error:
        pass
    return f"id:{id(conn)}"


def _split_identifier(name: str) -> list[str]:
    """snake_case / camelCase -> lowercased sub-tokens >= 2 chars (BLUiR-style)."""
    n = (name or "").strip()
    if not n:
        return []
    parts = re.split(r"[_\W]+|(?<=[a-z0-9])(?=[A-Z])", n)
    return [p.lower() for p in parts if p and len(p) >= 2]


def _repo_stats(conn: sqlite3.Connection) -> _RepoStats:
    key = _db_key(conn)
    cached = _REPO_CACHE.get(key)
    if cached is not None:
        return cached
    n_files = 1
    vocab = 1
    d95 = 50.0
    token_df: dict[str, int] = {}
    try:
        n_files = conn.execute("SELECT COUNT(DISTINCT file_path) FROM nodes").fetchone()[0] or 1
        names = [r[0] for r in conn.execute(
            "SELECT name FROM nodes WHERE label IN "
            "('Function','Method','Class','Interface') AND name IS NOT NULL"
        ).fetchall()]
        vocab = max(len(names), 1)
        for nm in names:                       # token document-frequency map (BLUiR)
            for t in set(_split_identifier(nm)):
                token_df[t] = token_df.get(t, 0) + 1
        degs = [r[0] for r in conn.execute(
            "SELECT COUNT(e.id) c FROM nodes n JOIN edges e ON e.target_id = n.id "
            "WHERE e.type='CALLS' GROUP BY n.name ORDER BY c"
        ).fetchall() if r and r[0] is not None]
        if degs:
            d95 = float(degs[int(len(degs) * 0.95)]) or 50.0
    except sqlite3.Error:
        pass
    stats = _RepoStats(n_files, vocab, max(d95, 1.0), token_df)
    _REPO_CACHE[key] = stats
    return stats


def _rsj_idf(df: int, total: int) -> float:
    """Robertson-Sparck-Jones / BM25 IDF — provably non-negative (no clamp)."""
    df = max(df, 0)
    return math.log((total - df + 0.5) / (df + 0.5) + 1.0)


# =========================================================================
# 1. symbol_specificity  (BugLocator ICSE 2012 / BLUiR ASE 2013 / RSJ-IDF)
# =========================================================================
def symbol_specificity(name: str, conn: sqlite3.Connection) -> float:
    """[0,1] distinctiveness of `name` as a localization anchor vs a generic hub.

    geomean( S1 def-frequency IDF, S2 in-degree hub-penalty, S3 name-token IDF ).
    A symbol generic on ANY axis (homonymous / massive hub / common tokens)
    collapses toward ~0 (EPS-floored by the geomean guard, not exactly 0 unless
    the dunder/empty short-circuit fires) — the data-derived replacement for
    every blocklist. Dunder shape short-circuits to 0 (Python language invariant).
    """
    s = (name or "").strip()
    if not s or len(s) < 2:
        return 0.0
    if s.startswith("__") and s.endswith("__"):
        return 0.0  # language invariant, not a heuristic list
    st = _repo_stats(conn)
    sl = s.lower()
    try:
        df_def = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE LOWER(name)=? "
            "AND label IN ('Function','Method','Class','Interface')", (sl,)
        ).fetchone()[0] or 0
        indeg = conn.execute(
            "SELECT COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id=n.id "
            "WHERE LOWER(n.name)=? AND e.type='CALLS' AND COALESCE(e.confidence,0.5) >= 0.5",
            (sl,),
        ).fetchone()[0] or 0
    except sqlite3.Error:
        return 0.0
    if df_def <= 0:
        return 0.0  # not a defined symbol here (e.g. an imported name)
    # S1 — definition-frequency IDF, normalized to [0,1] by the per-repo ceiling.
    s1 = _rsj_idf(df_def, st.n_files) / max(_rsj_idf(1, st.n_files), _EPS)
    # S2 — in-degree hub penalty, scaled by the repo's OWN P95 in-degree.
    s2 = 1.0 - (math.log1p(indeg) / math.log1p(st.d95_indeg))
    # S3 — mean name-token IDF (BLUiR identifier weighting).
    toks = _split_identifier(s)
    if toks:
        tok_idfs = [_rsj_idf(st.token_df.get(t, 0), st.vocab) for t in toks]
        s3 = (sum(tok_idfs) / len(tok_idfs)) / max(_rsj_idf(1, st.vocab), _EPS)
    else:
        s3 = 0.0
    s1 = min(max(s1, 0.0), 1.0); s2 = min(max(s2, 0.0), 1.0); s3 = min(max(s3, 0.0), 1.0)
    return round((max(s1, _EPS) * max(s2, _EPS) * max(s3, _EPS)) ** (1.0 / 3.0), 4)


# =========================================================================
# 2. dynamic_cutoff  (Iglewicz-Hoaglin modified-z + Leys MAD + Kneedle)
# =========================================================================
@dataclass
class DynTier:
    kept: list[int]              # indices (into the input order) above the dynamic cutoff
    tiers: list[str]             # per-item tier: "high" | "mid" | "low"
    median: float
    sigma: float


def dynamic_cutoff(scores: list[float]) -> DynTier:
    """Per-task deliver/suppress + tiering from the SCORE DISTRIBUTION, no fixed
    floor. Robust modified-z = 0.6745*(x-med)/MAD (Iglewicz-Hoaglin 1993).

    high = a significant positive outlier (z >= 3.5) -> deliver as a fact
    mid  = above the median                          -> deliver, de-prioritized
    low  = at/below the median                       -> suppress (correct-or-quiet)
    Degenerate guards: empty -> nothing; single -> 'mid' (cannot estimate spread);
    flat (MAD==0) -> all 'mid' (no separation to exploit).
    """
    n = len(scores)
    if n == 0:
        return DynTier([], [], 0.0, 0.0)
    order = sorted(range(n), key=lambda i: -scores[i])
    vals = sorted(scores)
    med = vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    # MAD is the median of |x - med| — average the two central deviations for even
    # n, mirroring the median above (the upper-middle order statistic biased it).
    _devs = sorted(abs(x - med) for x in scores)
    mad = _devs[n // 2] if n % 2 else 0.5 * (_devs[n // 2 - 1] + _devs[n // 2])
    sigma = _MAD_SIGMA * mad
    tiers = ["low"] * n
    kept: list[int] = []
    if n == 1 or sigma < _EPS:
        # cannot separate -> treat all as mid (deliver but not as outlier-facts)
        for i in range(n):
            tiers[i] = "mid"
        return DynTier(list(order), tiers, med, sigma)
    for i in range(n):
        z = 0.6745 * (scores[i] - med) / mad if mad > _EPS else 0.0
        if z >= _Z_OUTLIER:
            tiers[i] = "high"; kept.append(i)
        elif scores[i] > med:
            tiers[i] = "mid"; kept.append(i)
        else:
            tiers[i] = "low"
    kept.sort(key=lambda i: -scores[i])
    return DynTier(kept, tiers, med, sigma)


# =========================================================================
# 3. rrf_fuse  (Cormack & Clarke, RRF, SIGIR 2009)
# =========================================================================
def rrf_fuse(signal_to_values: dict[str, dict[str, float]], *, k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion of >=3 signals into one ranking, no learned weights.

    signal_to_values: {signal_name: {item_key: raw_score}}. Each signal is ranked
    INDEPENDENTLY (dense rank, ties share a rank); the fused score sums 1/(k+rank)
    over signals. Scale-invariant (raw magnitudes discarded), so no W_* weight can
    dominate, and a flat signal (all-equal) contributes a constant that drops out.
    """
    items: set[str] = set()
    for vals in signal_to_values.values():
        items |= set(vals.keys())
    fused: dict[str, float] = {it: 0.0 for it in items}
    for vals in signal_to_values.values():
        if not vals:
            continue
        # dense rank by descending value; ties share a rank (standard competition)
        ordered = sorted(vals.items(), key=lambda kv: -kv[1])
        rank = 0
        prev = None
        for idx, (it, v) in enumerate(ordered):
            if prev is None or v != prev:
                rank = idx + 1
                prev = v
            fused[it] += 1.0 / (k + rank)
        # items absent from this signal get no contribution (rank = infinity)
    return fused


# =========================================================================
# 4. claim_confidence  (selective prediction / conformal-style, El-Yaniv 2010)
# =========================================================================
def claim_confidence(score: float, pool: list[float]) -> tuple[float, bool]:
    """Empirical-percentile confidence in [0,1] for one claim vs its same-kind POOL,
    plus a distribution-based abstain flag (correct-or-quiet).

    conf = ECDF rank: the fraction of the pool the claim is >=. This is the
    empirical CDF position and RISES with the score — it is NOT a conformal
    p-value (a conformal p-value measures atypicality and FALLS as the score
    grows; Vovk 2005). Reported honestly as a percentile rank.

    abstain DELEGATES to `dynamic_cutoff` (the one median+MAD authority): the
    claim abstains iff it lands in the 'low' tier of its own pool (at/below the
    pool median, not a positive outlier). One median+MAD computation, one tie
    convention shared with every other gate — no duplicated, drifting threshold.
    Selective-prediction STYLE (El-Yaniv & Wiener 2010): a silence rule on a
    heuristic score, NOT a target-risk-calibrated guarantee (Geifman & El-Yaniv
    2017).
    """
    if not pool:
        return (0.5, True)
    m = len(pool)
    conf = sum(1 for p in pool if p <= score) / m          # ECDF percentile rank
    abstain = dynamic_cutoff(list(pool) + [score]).tiers[-1] == "low"
    return (round(conf, 4), abstain)


# =========================================================================
# 5. phase_and_budget  (anytime / budget-aware, Zilberstein AI Mag 1996)
# =========================================================================
@dataclass
class Phase:
    phase: str          # "early" | "mid" | "late"  (= orient / work / land)
    progress: float     # i / max_iter in [0,1];  -1.0 = unknown horizon
    near_budget: bool   # within the force-submit window
    char_budget: int    # evidence budget for this turn (scales with remaining)


def phase_and_budget(
    i: int,
    max_iter: int,
    *,
    phi1: float = 1.0 / 3.0,
    phi2: float = 2.0 / 3.0,
    phi_submit: float = 0.95,
    context_chars: int = 400_000,
) -> Phase:
    """Action phase (early/mid/late = orient/work/land) + EVIDENCE budget as
    FRACTIONS of the task horizon, not raw counts. A 30-iteration task and a
    100-iteration task reach 'late' at the same PROGRESS, not the same absolute
    action (the EARLY_END=5/MID_END=10 bug).

    A fixed max_iter is a CONTRACT horizon (Russell & Zilberstein 1991): the
    allocation must be known to normalize against it. If max_iter is unknown
    (<= 0 / missing) we do NOT silently default it — the old `or 100` corrupted
    every harness whose real cap != 100 — but return an explicit unknown-horizon
    marker (progress = -1.0, never near_budget) with the generous early budget,
    leaving the caller to supply a real cap.
    """
    b = int(max_iter or 0)
    if b <= 0:
        # unknown/variable horizon — interruptible mode, no phase/taper claim.
        return Phase("early", -1.0, False, int(0.012 * context_chars))
    p = min(max(i / b, 0.0), 1.0)
    phase = "early" if p < phi1 else ("mid" if p < phi2 else "late")
    # budget ~1% of context, tapering as the task progresses (deliver more early,
    # less late when context is fuller) — fraction of the window, not a fixed 2000.
    char_budget = int(max(0.003, 0.012 * (1.0 - 0.6 * p)) * context_chars)
    return Phase(phase, round(p, 4), p >= phi_submit, char_budget)


def clear_cache() -> None:
    _REPO_CACHE.clear()

"""Behavioral + research-integrity tests for ``groundtruth.confidence`` — the
central dynamic + hybrid + confidence-gated primitives.

These lock the corrections mandated by the primitive research-validation pass
(2026-05-31), each of which is RED on the pre-correction code and GREEN after:

  * ``dynamic_cutoff`` — pure median+MAD modified-z (the Kneedle/"percentile
    knee" citation was deleted because no knee code exists); MAD is a TRUE
    median (averages the two central deviations for even ``n``).
  * ``claim_confidence`` — ``conf`` is an honest ECDF rank (NOT a conformal
    p-value); ``abstain`` DELEGATES to ``dynamic_cutoff`` so there is one
    median+MAD authority and one tie convention.
  * ``phase_and_budget`` — an unknown/zero ``max_iter`` is NOT silently
    normalized to 100; it returns an explicit unknown-horizon marker.

They also regression-lock the data-derived behavior that REPLACES the hardcoded
symbol blocklists (``symbol_specificity``) and fixed weights (``rrf_fuse``).
Runnable under pytest OR as a plain script (``python tests/test_confidence_primitives.py``).
"""
from __future__ import annotations

import sqlite3

from groundtruth.confidence import (
    claim_confidence,
    clear_cache,
    dynamic_cutoff,
    is_seed_pollutant,
    phase_and_budget,
    rrf_fuse,
    symbol_specificity,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mk_graph(defs: list[tuple[str, str, str]], edges: list[tuple[str, str]]):
    """Build a tiny in-memory graph.db. ``defs`` = (name, label, file_path);
    ``edges`` = (caller_name, callee_name) CALLS edges at confidence 1.0."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INT, end_line INT,"
        " signature TEXT, return_type TEXT, is_exported INT, is_test INT,"
        " language TEXT, parent_id INT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INT, target_id INT,"
        " type TEXT, source_line INT, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    ids: dict[str, int] = {}
    for i, (name, label, fpath) in enumerate(defs, 1):
        conn.execute(
            "INSERT INTO nodes(id,label,name,file_path,is_test,language)"
            " VALUES(?,?,?,?,0,'python')",
            (i, label, name, fpath),
        )
        ids.setdefault(name, i)
    for src, dst in edges:
        conn.execute(
            "INSERT INTO edges(source_id,target_id,type,confidence)"
            " VALUES(?,?, 'CALLS', 1.0)",
            (ids.get(src, 0), ids.get(dst, 0)),
        )
    conn.commit()
    return conn


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# 1. symbol_specificity — data-derived replacement for the blocklists
# --------------------------------------------------------------------------- #
def test_specificity_dunder_is_zero():
    clear_cache()
    conn = _mk_graph([("__init__", "Method", "a.py"), ("parse", "Function", "b.py")], [])
    assert symbol_specificity("__init__", conn) == 0.0  # language invariant, not a list


def test_is_seed_pollutant_hub_homonym_rare():
    """Structural pollution gate: drop hubs (>=P95 in-degree) and homonyms (>P95
    def-count), keep uniquely-defined low-degree symbols. Repo-P95 derived, with a
    >=20-sample reliability guard (a P95 needs >= 1/(1-0.95) points to be meaningful)."""
    clear_cache()
    callers = [(f"caller_{i}", "Function", f"c{i}.py") for i in range(22)]
    targets = [(f"target_fn_{i}", "Function", f"t{i}.py") for i in range(1, 23)]  # 22
    homonym = [("New", "Function", f"pkg{i}.py") for i in range(6)]  # 6 files define New
    defs = callers + targets + homonym + [
        ("String", "Class", "s.py"),
        ("rare_unique_fn", "Function", "r.py"),
        ("__init__", "Method", "i.py"),
    ]
    edges: list[tuple[str, str]] = []
    for i in range(1, 23):                        # target_fn_i called by i distinct callers
        for j in range(i):
            edges.append((f"caller_{j}", f"target_fn_{i}"))
    edges += [("caller_0", "String")] * 50        # String = clear hub (indeg 50 >> P95)
    edges += [("caller_1", "rare_unique_fn")]      # rare: indeg 1
    conn = _mk_graph(defs, edges)
    assert is_seed_pollutant("String", conn) is True              # structural hub
    assert is_seed_pollutant("New", conn) is True                 # homonym (6 files)
    assert is_seed_pollutant("rare_unique_fn", conn) is False     # precise low-degree seed
    assert is_seed_pollutant("__init__", conn) is True            # dunder
    assert is_seed_pollutant("NotInGraph", conn) is True          # not defined here


def test_specificity_hub_collapses_below_rare_no_blocklist():
    """A generic hub (many callers) scores below a rare single-file symbol —
    WITHOUT any hardcoded _GENERIC_SYMBOLS list."""
    clear_cache()
    callers = [(f"c{i}", "Function", f"f{i}.py") for i in range(20)]
    defs = [("String", "Class", "s.py"), ("compute_delta_table", "Function", "d.py")] + callers
    edges = [(f"c{i}", "String") for i in range(20)]  # String = 20-caller hub
    conn = _mk_graph(defs, edges)
    s_hub = symbol_specificity("String", conn)
    s_rare = symbol_specificity("compute_delta_table", conn)
    assert s_hub < s_rare, (s_hub, s_rare)
    assert s_hub < 0.2, s_hub  # collapses purely from in-degree, no blocklist


# --------------------------------------------------------------------------- #
# 2. dynamic_cutoff — median+MAD only; TRUE median MAD for even n
# --------------------------------------------------------------------------- #
def test_dynamic_cutoff_even_n_mad_is_true_median():
    """RED before fix: even-n MAD took the upper-middle order statistic
    (deviations [0.5,0.5,1.5,8.5] -> 1.5). GREEN after: average the two central
    deviations (avg(0.5,1.5)=1.0), mirroring the median computation."""
    dt = dynamic_cutoff([0.0, 1.0, 2.0, 10.0])  # median = 1.5
    # sigma = 1.4826 * MAD ; MAD must be the TRUE median of deviations = 1.0
    assert _approx(dt.sigma, 1.4826 * 1.0), dt.sigma


def test_dynamic_cutoff_degenerate_guards():
    assert dynamic_cutoff([]).tiers == []
    assert dynamic_cutoff([5.0]).tiers == ["mid"]          # single -> cannot estimate spread
    assert dynamic_cutoff([3.0, 3.0, 3.0]).tiers == ["mid", "mid", "mid"]  # flat -> all mid


def test_dynamic_cutoff_outlier_high():
    # A spread cluster (MAD > 0) plus one far outlier — the outlier is flagged
    # "high". (A pool that is >50% identical has MAD=0 and CANNOT flag an outlier;
    # that degenerate case is covered by test_dynamic_cutoff_degenerate_guards.)
    dt = dynamic_cutoff([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])
    assert dt.tiers[-1] == "high"  # 100 is a positive outlier (z >= 3.5)


# --------------------------------------------------------------------------- #
# 3. rrf_fuse — rank-only, scale-invariant, no learned weights
# --------------------------------------------------------------------------- #
def test_rrf_scale_invariant():
    a = {"x": 1.0, "y": 2.0, "z": 3.0}
    b = {"x": 1000.0, "y": 2000.0, "z": 3000.0}  # identical RANKS, different scale
    assert rrf_fuse({"s1": a, "s2": a}) == rrf_fuse({"s1": b, "s2": b})


def test_rrf_consensus_beats_single_signal():
    sig = {
        "s1": {"a": 3, "b": 2, "c": 1},
        "s2": {"a": 3, "b": 2, "c": 1},
        "s3": {"c": 3, "a": 2, "b": 1},
    }
    fused = rrf_fuse(sig)
    assert max(fused, key=fused.get) == "a"  # consensus #1 in 2/3 signals wins


# --------------------------------------------------------------------------- #
# 4. claim_confidence — ECDF rank + abstain DELEGATED to dynamic_cutoff
# --------------------------------------------------------------------------- #
def test_claim_confidence_is_ecdf_rank():
    pool = [0.1, 0.2, 0.3, 0.9]
    conf, _ = claim_confidence(0.3, pool)
    assert _approx(conf, 3 / 4)  # fraction of pool the score is >=


def test_claim_confidence_abstain_delegates_to_dynamic_cutoff():
    """RED before fix: claim_confidence recomputed its OWN z<=0 boundary, which
    diverges from dynamic_cutoff on flat/degenerate pools. GREEN after: abstain
    is exactly dynamic_cutoff(pool+[score]).low — one median+MAD authority."""
    for pool, score in [
        ([0.1, 0.2, 0.3, 0.9], 0.95),   # clear top
        ([0.1, 0.2, 0.3, 0.9], 0.05),   # clear bottom
        ([3.0, 3.0, 3.0], 3.0),         # flat pool — the divergence case
        ([3.0, 3.0, 3.0], 2.0),         # below a flat pool
        ([0.1, 0.5, 0.9], 0.5),         # at the median
    ]:
        _, abstain = claim_confidence(score, pool)
        expected = dynamic_cutoff(list(pool) + [score]).tiers[-1] == "low"
        assert abstain == expected, (pool, score, abstain, expected)


# --------------------------------------------------------------------------- #
# 5. phase_and_budget — fractions of max_iter; explicit unknown-horizon
# --------------------------------------------------------------------------- #
def test_phase_fractions_are_horizon_relative():
    assert phase_and_budget(5, 30).phase == "early"
    assert phase_and_budget(15, 30).phase == "mid"
    assert phase_and_budget(28, 30).phase == "late"
    assert phase_and_budget(29, 30).near_budget is True
    # same PROGRESS on a different horizon -> same phase (the EARLY_END=5 bug fix)
    assert phase_and_budget(20, 30).phase == phase_and_budget(67, 100).phase


def test_phase_unknown_horizon_not_silently_100():
    """RED before fix: max_iter=0 fell back to `or 100`, so progress=5/100=0.05
    (a silent, wrong normalization). GREEN after: unknown horizon is explicit."""
    ph = phase_and_budget(5, 0)
    assert ph.progress == -1.0, ph.progress      # explicit unknown marker, not 0.05
    assert ph.near_budget is False               # never force-submit on unknown horizon


# --------------------------------------------------------------------------- #
# plain-script runner (no pytest dependency required)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)

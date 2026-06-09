"""GT proof-mode surface — ONE place every benchmark entrypoint enforces the
fail-closed runtime contract.

Why this module exists (the 14-run lesson, gt_trial.md §0): GT can *provision* a
dependency (graph, FTS5, LSP, embedder) and still operate *partially* — host-split
paths, python-side FTS5 creation, an embedder that loads but is zeroed at scoring
time, a closure rebuilt before LSP ran. Every one of those degrades the run to a
grep+graph baseline wearing a GT label, silently. This module turns each silent
degrade into a LOUD fail-closed stop **in proof mode**, and a logged warning
otherwise — so dev/CI behaviour is byte-identical to before (CLAUDE.md: "Audit
mode disabled produces byte-identical non-audit output, except explicit proof-mode
fail-fast").

ONE surface, reused everywhere (CLAUDE.md ONE PRODUCT RULE): run_v74, localize,
generate_v1r_brief, foundational_gates, resolve.py, and the wrapper all call the
helpers here instead of re-deriving partial-operation checks independently.

Importing/using this NEVER changes ranking/scoring/brief logic. It only raises (in
proof mode) or warns (outside).

Proof mode = ``GT_PROOF_MODE=1``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import sys
import time

_LOG = logging.getLogger("groundtruth.runtime.proof")

# Host-path aliases. Accepting these in proof mode is the split host/container
# root the plan flags (context.from_env falls back to them). In proof mode the
# canonical GT_SOURCE_ROOT / GT_GRAPH_DB (the in-container paths) MUST be used.
# The canonical host->agent handoff: Point A resolves the graph on the host and hands the
# SAME LSP-enriched graph to the agent via these (gt_gt §1). They are LEGITIMATE (freshness
# + schema validated downstream), NOT a forbidden host split — the agent must hook onto the
# same enriched graph the gates measured. They are intentionally NOT in the reject list.
HOST_HANDOFF = ("GT_HOST_SRC_ROOT", "GT_HOST_GRAPH_DB")
# Non-canonical aliases the pipeline never sets — their presence means a MISCONFIGURATION
# (typo / dead var), which proof mode should surface.
HOST_ALIASES = ("GT_HOST_GRAPH", "GT_HOST_SOURCE_ROOT")

_DEFAULT_RUNTIME_ROOT = "/opt/gt"

# Meta table this module owns inside graph.db. Distinct from the Go indexer's
# tables (nodes/edges/properties/assertions) so stamping never collides with the
# indexer's writes. resolve.py is already a legitimate graph.db writer.
_META_DDL = (
    "CREATE TABLE IF NOT EXISTS gt_runtime_meta "
    "(key TEXT PRIMARY KEY, value TEXT, updated_ts REAL)"
)

# Timing keys (one pipeline order: index -> LSP enrich -> closure rebuild).
K_INDEX_BUILD = "index_build_id"
K_LSP_TS = "lsp_enrichment_ts"
K_LSP_METRICS = "lsp_metrics"
K_CLOSURE_TS = "closure_rebuild_ts"
K_CONTEXT_ID = "runtime_context_id"
K_EMBEDDER_ID = "embedder_identity"


class GTProofModeError(RuntimeError):
    """Raised when GT_PROOF_MODE=1 and a runtime contract check fails.

    Accepts either a plain message string, or a list of ``(name, ok, detail)``
    check tuples (the form ``GTRuntimeContext.validate`` raises) — so this is the
    single proof-mode exception type across the whole runtime.
    """

    def __init__(self, arg):
        if isinstance(arg, str):
            self.failures = []
            super().__init__(arg)
        else:
            self.failures = list(arg)
            lines = "\n".join(f"  - {n}: {d}" for n, ok, d in self.failures if not ok)
            super().__init__("GT_PROOF_MODE runtime contract FAILED:\n" + lines)


def is_proof_mode() -> bool:
    return os.environ.get("GT_PROOF_MODE") == "1"


def require_embedder() -> bool:
    return os.environ.get("GT_REQUIRE_EMBEDDER") == "1"


def require(ok: bool, name: str, detail: str = "") -> bool:
    """Fail closed in proof mode; warn-and-continue otherwise.

    Returns ``ok`` so callers can branch outside proof mode. The single gate every
    other helper funnels through, so the proof/non-proof split lives in ONE place.
    """
    if ok:
        return True
    msg = f"[GT_PROOF] {name} FAILED: {detail}"
    if is_proof_mode():
        raise GTProofModeError(msg)
    _LOG.warning(msg)
    print(msg + " (not proof mode; continuing)", file=sys.stderr)
    return False


# ───────────────────────────── path / import contract ─────────────────────────


def reject_host_aliases() -> None:
    """In proof mode, refuse only NON-CANONICAL host aliases (a misconfiguration). The
    canonical GT_HOST_SRC_ROOT / GT_HOST_GRAPH_DB are the LEGITIMATE host->agent graph
    handoff (gt_gt §1) — the agent hooks onto the same LSP-enriched graph the gates
    measured — and are NOT forbidden."""
    if not is_proof_mode():
        return
    present = [k for k in HOST_ALIASES if os.environ.get(k)]
    require(not present, "no_noncanonical_host_aliases",
            f"non-canonical host aliases set in proof mode: {present} — use the canonical "
            f"handoff GT_HOST_SRC_ROOT/GT_HOST_GRAPH_DB, or in-container GT_SOURCE_ROOT/GT_GRAPH_DB")


def runtime_root() -> str:
    return os.environ.get("GT_HOME") or _DEFAULT_RUNTIME_ROOT


def require_import_under_runtime_root() -> None:
    """In proof mode, groundtruth must import from under the runtime root
    (/opt/gt), not a host checkout — else a host code path is executing."""
    if not is_proof_mode():
        return
    root = runtime_root().rstrip("/")
    try:
        import groundtruth as _g
        gf = (getattr(_g, "__file__", "") or "").replace("\\", "/")
    except Exception as e:  # pragma: no cover - import always succeeds here
        require(False, "import_under_runtime_root", f"import error: {e}")
        return
    ok = gf.startswith(root + "/") or gf.startswith(root)
    require(ok, "import_under_runtime_root",
            f"groundtruth imported from {gf!r}, expected under {root!r}")


def forbid_prebuilt_graph() -> None:
    """In proof mode, a prebuilt/injected graph.db is forbidden — the graph must
    be built fresh in-container per task (GT_FORBID_PREBUILT_GRAPH)."""
    if not is_proof_mode():
        return
    prebuilt = os.environ.get("GT_PREBUILT_GRAPH_DB", "")
    require(not prebuilt, "no_prebuilt_graph",
            f"GT_PREBUILT_GRAPH_DB set in proof mode: {prebuilt!r}")


def context_id() -> str:
    """Stable id for the runtime that produced an artifact — sha256[:16] of the
    paths that DEFINE the runtime. Stamped into graph meta, brief result, gate
    result and the run contract so gates-only and live can be proven identical."""
    parts = [
        runtime_root(),
        os.environ.get("GT_SOURCE_ROOT", ""),
        os.environ.get("GT_GRAPH_DB", ""),
        os.environ.get("GT_MODELS_ROOT", "") or os.path.join(runtime_root(), "models"),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# ───────────────────────────── graph.db meta stamp/read ───────────────────────


def _connect(db) -> tuple[sqlite3.Connection, bool]:
    """Return (conn, owned). Accepts a path or an open connection."""
    if isinstance(db, sqlite3.Connection):
        return db, False
    return sqlite3.connect(db), True


def stamp_meta(db, key: str, value: str) -> None:
    conn, owned = _connect(db)
    try:
        conn.execute(_META_DDL)
        conn.execute(
            "INSERT INTO gt_runtime_meta(key, value, updated_ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (key, str(value), time.time()),
        )
        conn.commit()
    finally:
        if owned:
            conn.close()


def read_meta(db, key: str) -> str | None:
    conn, owned = _connect(db)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM gt_runtime_meta WHERE key=?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # meta table absent
        return row[0] if row else None
    finally:
        if owned:
            conn.close()


def stamp_event_ts(db, key: str) -> float:
    """Stamp the current wall-clock under ``key`` and return it. Used for the
    one-pipeline ordering: lsp_enrichment_ts then closure_rebuild_ts."""
    ts = time.time()
    stamp_meta(db, key, repr(ts))
    return ts


def read_ts(db, key: str) -> float | None:
    v = read_meta(db, key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def graph_edges_hash(db) -> str:
    """Canonical content fingerprint of the graph's edges (source,target,type,
    resolution_method,confidence) — proves the SAME graph flows build -> LSP -> gates ->
    hooks (Stage 2). MUST match resolve._graph_edges_hash exactly so cross-stage hashes
    compare; a drift test guards that. Returns '' if the db is unreadable."""
    import hashlib
    import sqlite3 as _sql
    h = hashlib.sha256()
    try:
        c = _sql.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for row in c.execute(
                "SELECT source_id, target_id, type, resolution_method, confidence "
                "FROM edges ORDER BY id"
            ):
                h.update(repr(tuple(row)).encode("utf-8"))
        finally:
            c.close()
    except Exception:
        return ""
    return h.hexdigest()


# ───────────────────────────── FTS5 (Stage 2) ────────────────────────────────


def assert_fts5_native(conn: sqlite3.Connection, *, where: str = "retrieval") -> bool:
    """Proof mode: nodes_fts must be Go-built (already present), populated, and a
    real MATCH must return without error. Python-side creation during retrieval is
    forbidden — it means the indexer was compiled without -tags sqlite_fts5 and the
    run silently degraded (CLAUDE.md). Returns True if FTS5 is usable.
    """
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except sqlite3.Error as e:
        return require(False, "fts5_table_check", f"{where}: {e}")
    if "nodes_fts" not in tables:
        return require(False, "fts5_native_present",
                       f"{where}: nodes_fts missing — Go indexer lacked "
                       f"-tags sqlite_fts5; python-side creation is forbidden in proof mode")
    try:
        n = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
    except sqlite3.Error as e:
        return require(False, "fts5_queryable", f"{where}: COUNT failed: {e}")
    if not require(n > 0, "fts5_populated", f"{where}: nodes_fts has 0 rows"):
        return False
    try:
        conn.execute("SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? LIMIT 1", ("a*",)).fetchall()
    except sqlite3.Error as e:
        return require(False, "fts5_match_works", f"{where}: MATCH raised: {e}")
    return True


# ───────────────────────────── LSP / closure timing (Stage 2) ─────────────────


def stamp_lsp(db, metrics: str = "") -> float:
    """Record that LSP enrichment completed (call from resolve.py after the pass)."""
    if metrics:
        stamp_meta(db, K_LSP_METRICS, metrics)
    return stamp_event_ts(db, K_LSP_TS)


def stamp_closure(db) -> float:
    """Record that the closure rebuild completed (call after _rebuild_closure)."""
    return stamp_event_ts(db, K_CLOSURE_TS)


def assert_lsp_before_scoring(db) -> bool:
    """Proof mode: scoring/render/gates must not start before LSP enriched the
    graph — else the brief ranks over a name_match-garbage map."""
    if not is_proof_mode():
        return True
    return require(read_ts(db, K_LSP_TS) is not None, "lsp_before_scoring",
                   "no lsp_enrichment_ts stamp on graph — LSP did not run before scoring")


def assert_closure_after_lsp(db) -> bool:
    """Proof mode: the closure must be rebuilt AFTER LSP mutated the edges, so the
    closure reflects resolved edges (not the pre-LSP name_match graph)."""
    if not is_proof_mode():
        return True
    lsp = read_ts(db, K_LSP_TS)
    clo = read_ts(db, K_CLOSURE_TS)
    if not require(lsp is not None, "closure_lsp_stamp_present",
                   "no lsp_enrichment_ts — cannot prove closure freshness"):
        return False
    if not require(clo is not None, "closure_rebuilt",
                   "no closure_rebuild_ts stamp — closure not rebuilt after LSP"):
        return False
    if lsp is None or clo is None:  # narrowing (require warned, not raised, outside proof)
        return False
    return require(clo >= lsp, "closure_newer_than_lsp",
                   f"closure_rebuild_ts={clo!r} older than lsp_enrichment_ts={lsp!r} (stale closure)")


# ───────────────────────────── embedder usage (Stage 3) ───────────────────────


def forbid_no_sem_config(ablation: str, rrf_mode: str, effective_w_sem: float) -> None:
    """Proof mode + GT_REQUIRE_EMBEDDER: refuse a configuration that drops the
    semantic signal on the final benchmark path. Availability is enforced
    elsewhere (run_v74 _get_model raises); this enforces USAGE intent up front."""
    if not (is_proof_mode() and require_embedder()):
        return
    rrf = (rrf_mode or "").strip().lower()
    require(ablation not in ("A", "B0", "B1"), "no_sem_ablation_in_proof",
            f"ablation={ablation!r} zeroes the semantic signal; forbidden in proof mode")
    require(rrf not in ("det", "deterministic", "nosem"), "no_sem_rrf_in_proof",
            f"GT_RRF_FUSION={rrf!r} drops the embedding signal; forbidden in proof mode")
    require(effective_w_sem > 0.0, "sem_weight_nonzero",
            f"effective W_SEM={effective_w_sem} — semantic weight zeroed in proof mode")


def assert_semantic_consumed(effective_w_sem: float, sem_components, n_candidates: int) -> bool:
    """Proof mode + GT_REQUIRE_EMBEDDER: a PRESENT embedder must be CONSUMED — when
    there are candidates and the semantic weight is non-zero, the sem components
    over the scored candidate universe cannot be all-zero/flat (the
    "provisioned-but-unconsumed" trap; CLAUDE.md AGENT-OBSERVATION + gt_trial §1.5
    GATE 3b). This is the consumption proof the V74BriefResult.effective_w_sem /
    sem_components_full fields were added for."""
    if not (is_proof_mode() and require_embedder()):
        return True
    if n_candidates <= 0:
        return True
    if not require(effective_w_sem > 0.0, "sem_weight_nonzero",
                   f"effective_w_sem={effective_w_sem} with {n_candidates} candidates"):
        return False
    comps = list(sem_components or [])
    nonzero = sum(1 for s in comps if isinstance(s, (int, float)) and s and s > 0.0)
    return require(nonzero > 0, "semantic_components_consumed",
                   f"effective_w_sem={effective_w_sem} but ALL {len(comps)} sem components "
                   f"zero/flat over {n_candidates} candidates — embedder present but unconsumed")


def embedder_identity() -> dict:
    """The semantic surface identity (models root / class / dim / force-onnx) so
    run_v74 and localize can be proven to use the SAME embedder, not two."""
    ident = {
        "models_root": os.environ.get("GT_MODELS_ROOT", "") or os.path.join(runtime_root(), "models"),
        "force_onnx": os.environ.get("GT_FORCE_ONNX_EMBEDDER", ""),
        "class": "",
        "dim": "",
    }
    try:
        from groundtruth.memory.enrich.embed import get_embedding_model
        m = get_embedding_model()
        ident["class"] = type(m).__name__
        ident["dim"] = str(getattr(m, "dim", ""))
    except Exception as e:
        ident["class"] = f"load_error:{e}"
    return ident


def assert_same_embedder_identity(db, who: str) -> bool:
    """Stamp this caller's embedder identity into graph meta and, if a prior caller
    already stamped one, require they match (run_v74 vs localize must agree)."""
    if not is_proof_mode():
        return True
    ident = embedder_identity()
    key = (ident.get("models_root", ""), ident.get("class", ""), ident.get("dim", ""), ident.get("force_onnx", ""))
    sig = "|".join(key)
    prior = read_meta(db, K_EMBEDDER_ID)
    if prior is None:
        stamp_meta(db, K_EMBEDDER_ID, sig)
        return True
    return require(prior == sig, "same_embedder_identity",
                   f"{who} embedder {sig!r} != prior {prior!r}")


def embedder_model_path() -> str:
    """Path to the baked ONNX model (GT_MODELS_ROOT/<model-dirname>/model.onnx).

    CHANGE 2: the dirname is derived from the CONFIGURED localization model name
    (``model_name.split('/')[-1]`` — gte-modernbert-base by default, or whatever
    GT_EMBED_MODEL_NAME pins) so the proof cert points at the model actually loaded."""
    from groundtruth.memory.enrich.embed import _default_embed_model

    root = os.environ.get("GT_MODELS_ROOT", "") or os.path.join(runtime_root(), "models")
    model_dirname = _default_embed_model().split("/")[-1]
    return os.path.join(root, model_dirname, "model.onnx")


def embedder_model_sha() -> str:
    """Best-effort model SHA — a sidecar `.sha256` or $GT_MODEL_SHA (never hash a ~130MB
    model file per task). '' if unavailable."""
    env = os.environ.get("GT_MODEL_SHA", "")
    if env:
        return env.strip()
    p = embedder_model_path()
    for cand in (p + ".sha256", os.path.join(os.path.dirname(p), "model.onnx.sha256")):
        try:
            with open(cand, encoding="utf-8") as f:
                return f.read().split()[0].strip()
        except Exception:
            continue
    return ""


def build_embedder_certificate(**kw) -> dict:
    """Assemble the Stage-3 embedder-usage certificate (identity + consumption fields).
    Pure data assembly; classification lives in scripts/metrics/embedder_certificate.py."""
    try:
        ident = embedder_identity()
    except Exception:
        ident = {}
    db = kw.get("db")
    stamped = read_meta(db, K_EMBEDDER_ID) if db else None
    return {
        "schema": "gt.embedder_certificate.v1",
        "bug_id": kw.get("bug_id", ""),
        "GT_FORCE_ONNX_EMBEDDER": os.environ.get("GT_FORCE_ONNX_EMBEDDER", ""),
        "GT_REQUIRE_EMBEDDER": os.environ.get("GT_REQUIRE_EMBEDDER", ""),
        "GT_MODELS_ROOT": ident.get("models_root", os.environ.get("GT_MODELS_ROOT", "")),
        "model_path": embedder_model_path(),
        "model_sha": embedder_model_sha(),
        "embedder_class": ident.get("class", ""),
        "embedder_dim": ident.get("dim", ""),
        "runtime_context_id": context_id(),
        "stamped_embedder_identity": stamped,
        "run_v74_embedder_identity": kw.get("run_v74_identity") or ident,
        "localize_embedder_identity": kw.get("localize_identity"),
        "v1r_render_semantic_identity": kw.get("v1r_identity"),
        "semantic_candidate_count": int(kw.get("semantic_candidate_count", 0) or 0),
        "rendered_candidate_count": int(kw.get("rendered_candidate_count", 0) or 0),
        "rendered_semantic_nonzero_count": int(kw.get("rendered_semantic_nonzero_count", 0) or 0),
        "upstream_semantic_nonzero_count": int(kw.get("upstream_semantic_nonzero_count", 0) or 0),
        "effective_w_sem": float(kw.get("effective_w_sem", 0.0) or 0.0),
        "all_zero_semantic_reason": kw.get("all_zero_semantic_reason", "") or "",
        "model_download_attempted": bool(kw.get("model_download_attempted", False)),
    }


def write_embedder_certificate(cert: dict) -> str:
    """Write the embedder certificate to $GT_EMBEDDER_CERT (default
    /tmp/gt/embedder_certificate.json). Best-effort; never raises."""
    import json as _json
    path = os.environ.get("GT_EMBEDDER_CERT", "/tmp/gt/embedder_certificate.json")
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(cert, f, indent=2)
    except Exception:
        pass
    return path

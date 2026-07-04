#!/usr/bin/env python
"""measure_brief.py — the canonical GT localization measurement instrument.

Given (issue_text, graph.db, repo_root, gold_files[]) it calls the AUTHORITATIVE
brief path `generate_v1r_brief` (semantic ON, container ONNX) and reports, per
gold file:
  * the gold's RANK in the brief's delivered candidate list (`.files`), and
    whether it appears at all;
  * the STAGE that dropped it if absent — by re-running the same deterministic
    sub-functions the pipeline uses (run_v74 seeder universe -> exact-name
    guarantee `_exact_issue_named_files` -> localize/RRF candidates -> render
    K-cap = delivered `.files`) and finding the first stage the gold is missing
    from;
  * the full delivered candidate list with scores + the confidence tier;
  * whether `_exact_issue_named_files` fired for the gold (and which symbols).

Semantic MUST be ON (BRIEFING.md): this harness ASSERTS the embedder is the
container ONNX `_OnnxEmbedderAdapter` and ABORTS (or loudly records) otherwise —
never silently falls back to semantic-off. Deterministic / no network.

Usage:
  measure_brief.py --issue-file F --graph G.db --repo R --gold a.py --gold b.py
  measure_brief.py --task-config tasks/<id>.json
  measure_brief.py --testset            # run every task config + emit AGGREGATE
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# --- env: force the container ONNX embedder, point at the baked models ---
os.environ.setdefault("GT_FORCE_ONNX_EMBEDDER", "1")
os.environ.setdefault("GT_REQUIRE_EMBEDDER", "1")
os.environ.setdefault("GT_MODELS_ROOT", "D:/Groundtruth/models")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, "D:/Groundtruth/src")

from groundtruth.pretask.graph_localizer import _normalize as _norm  # noqa: E402

TESTSET_DIR = "D:/gt_runs/localization_testset"


def _assert_semantic_on() -> dict:
    """Verify the embedder is the container ONNX adapter; never run semantic-off.

    Returns a dict describing the embedder. Raises RuntimeError if it is NOT the
    ONNX adapter (e.g. ZeroEmbeddingoModel / sentence_transformers), per BRIEFING
    §0/§5: a half-on pipeline produces worthless numbers."""
    # Block sentence_transformers explicitly (BRIEFING §0): if importable AND
    # selected it would desync the two halves; the force/require flags already
    # skip it, but we record its presence.
    st_present = False
    try:
        import sentence_transformers  # noqa: F401
        st_present = True
    except Exception:
        st_present = False

    from groundtruth.pretask.v7_4_brief import _OnnxEmbedderAdapter, _get_model

    model = _get_model()
    is_onnx = isinstance(model, _OnnxEmbedderAdapter)
    info = {
        "embedder_class": type(model).__name__,
        "is_onnx_adapter": is_onnx,
        "sentence_transformers_importable": st_present,
        "GT_FORCE_ONNX_EMBEDDER": os.environ.get("GT_FORCE_ONNX_EMBEDDER"),
        "GT_REQUIRE_EMBEDDER": os.environ.get("GT_REQUIRE_EMBEDDER"),
        "GT_MODELS_ROOT": os.environ.get("GT_MODELS_ROOT"),
    }
    if not is_onnx:
        raise RuntimeError(
            "SEMANTIC-OFF ABORT: embedder is "
            f"{type(model).__name__}, not the container ONNX _OnnxEmbedderAdapter. "
            "Per BRIEFING.md any number on a half-on pipeline is worthless. "
            "Ensure onnxruntime is installed and the gte-modernbert ONNX model is "
            f"baked under GT_MODELS_ROOT={os.environ.get('GT_MODELS_ROOT')}."
        )
    # Prove the embedder actually produces a nonzero vector.
    try:
        v = model.encode(["semantic on probe"], normalize_embeddings=True)
        dim = len(v[0]) if hasattr(v, "__len__") and len(v) else 0
        info["probe_dim"] = int(dim)
        info["probe_nonzero"] = bool(dim and any(abs(float(x)) > 1e-9 for x in v[0]))
    except Exception as e:  # noqa: BLE001
        info["probe_error"] = repr(e)
        info["probe_nonzero"] = False
    if not info.get("probe_nonzero"):
        raise RuntimeError(f"SEMANTIC-OFF ABORT: embedder produced zero vector: {info}")
    return info


def _rec_path(r: dict) -> str:
    return _norm(r.get("path") or r.get("file") or r.get("file_path") or "")


def _measure_stages(issue_text: str, repo_root: str, graph_db: str, gold_norm: list[str]) -> dict:
    """Re-run the deterministic stages and record, per gold file, the highest stage
    it survived to. This does NOT alter the authoritative brief; it re-invokes the
    same sub-functions on identical (deterministic) inputs to expose the drop-stage.

    Stage order (pipeline order):
      1. seeder_universe : run_v74().ranked_full  (was it retrieved/scored at all?)
      2. exact_name      : _exact_issue_named_files (the L1 issue-symbol guarantee)
      3. localize_rrf    : localize().candidates    (3-ranker RRF surfaced it?)
      4. render_kcap      : the delivered brief .files (handled by caller)
    """
    stages: dict[str, dict] = {}

    # Stage 1: run_v74 seeder/scoring universe.
    from groundtruth.pretask.v7_4_brief import run_v74

    v74 = run_v74(
        issue_text, repo_root, graph_db,
        ablation="C", k_anchor=3, k_sem_top=10, tau_anchor=0.20,
        max_depth=3, min_confidence=0.7, focus_size=5,
    )
    ranked = v74.ranked_full or []
    rank_by = {}
    for i, r in enumerate(ranked):
        p = _rec_path(r)
        if p not in rank_by:
            rank_by[p] = i
    stages["seeder_universe"] = {
        "count": len(ranked),
        "gold_native_rank": {g: rank_by.get(g, None) for g in gold_norm},
        "effective_w_sem": float(getattr(v74, "effective_w_sem", 0.0) or 0.0),
    }

    # Stage 2: exact-name guarantee.
    from groundtruth.pretask.anchors import extract_issue_anchors
    from groundtruth.pretask.v1r_brief import _exact_issue_named_files

    anchors = None
    try:
        anchors = extract_issue_anchors(issue_text, graph_db)
    except Exception:
        anchors = None
    ein = _exact_issue_named_files(issue_text, graph_db, issue_anchors=anchors)
    ein_norm = {_norm(f): syms for f, syms in ein.items()}
    stages["exact_name"] = {
        "fired_files": sorted(ein_norm.keys()),
        "gold_fired": {g: ein_norm.get(g) for g in gold_norm},
    }

    # Stage 3: localize / 3-way RRF.
    from groundtruth.pretask.graph_localizer import localize

    loc_rank = {}
    loc_info = {}
    _agreement: dict[str, int] = {}
    _signals: dict[str, list] = {}
    try:
        loc = localize(issue_text, graph_db, top_k=8, issue_anchors=anchors, repo_root=repo_root)
        for i, c in enumerate(loc.candidates or []):
            p = _norm(c.file_path)
            if p not in loc_rank:
                loc_rank[p] = i
                loc_info[p] = {
                    "score": round(float(c.score), 8),
                    "confidence": round(float(c.confidence), 8),
                    "verified_witness": bool(c.has_verified_witness),
                    "witness": c.render_witness(),
                }
        # Leg-vote attribution (D2a): agreement_by_file COUNTS how many of the 3
        # independent rankers (grep/structural/semantic) put the file in their own
        # top-3; signals_by_file NAMES them. Consumed by the agreement>=2 slice.
        _agreement = dict(getattr(loc, "agreement_by_file", {}) or {})
        _signals = {k: list(v) for k, v in (getattr(loc, "signals_by_file", {}) or {}).items()}
    except Exception as e:  # noqa: BLE001
        loc_info["_error"] = repr(e)
    stages["localize_rrf"] = {
        "count": len(loc_rank),
        "gold_loc_rank": {g: loc_rank.get(g, None) for g in gold_norm},
        "gold_loc_info": {g: loc_info.get(g) for g in gold_norm},
        "agreement_by_file": _agreement,
        "signals_by_file": _signals,
    }
    return stages


def _gold_match(f: str, gold_set: set[str]) -> bool:
    """Suffix-tolerant gold membership (mirrors _hit_at_k)."""
    return f in gold_set or any(f.endswith("/" + g) or g.endswith("/" + f) for g in gold_set)


def _agreement_ge2_slice(agreement_by_file: dict, gold_norm: list[str]) -> dict:
    """The conditional agreement>=2 precision slice (D2a): of the files where >=2 of
    {grep, structural, semantic} put the file in their top-3, what fraction ARE the
    true-gold file. This is the number that bounds how confidently item C (the
    consensus) may speak — a low precision means "N/3 agree" is a weak claim.

    precision = |{f : agreement>=2 and f in gold}| / |{f : agreement>=2}| (8-dp),
    None when no file reaches >=2 agreement (nothing to speak about)."""
    gs = set(gold_norm)
    ge2 = sorted(f for f, n in (agreement_by_file or {}).items() if int(n or 0) >= 2)
    ge2_gold = [f for f in ge2 if _gold_match(f, gs)]
    n = len(ge2)
    return {
        "n_files_agree_ge2": n,
        "n_agree_ge2_gold": len(ge2_gold),
        "precision": round(len(ge2_gold) / n, 8) if n else None,
        "files_agree_ge2": ge2,
        "gold_in_agree_ge2": ge2_gold,
    }


def measure_one(issue_text: str, repo_root: str, graph_db: str, gold_files: list[str]) -> dict:
    """Run the authoritative brief + the stage instrumentation; return a report dict."""
    from groundtruth.pretask.v1r_brief import generate_v1r_brief

    gold_norm = [_norm(g) for g in gold_files]

    # The authoritative delivered brief (semantic ON).
    result = generate_v1r_brief(issue_text, repo_root, graph_db, gold_files=gold_files)
    delivered = result.files or []
    deliv_rank = {}
    candidate_list = []
    for i, e in enumerate(delivered):
        p = _norm(e.path)
        if p not in deliv_rank:
            deliv_rank[p] = i
        candidate_list.append({
            "rank": i,
            "path": e.path,
            "norm": p,
            "score": round(float(e.score), 8),
            "is_gold": p in gold_norm,
            "witness_verified": bool(e.witness_verified),
        })

    stages = _measure_stages(issue_text, repo_root, graph_db, gold_norm)
    seeder = stages["seeder_universe"]
    exact = stages["exact_name"]
    loc = stages["localize_rrf"]

    per_gold = {}
    for g, gn in zip(gold_files, gold_norm):
        delivered_rank = deliv_rank.get(gn, None)
        native_rank = seeder["gold_native_rank"].get(gn)
        loc_r = loc["gold_loc_rank"].get(gn)
        exact_fired = exact["gold_fired"].get(gn)
        # Drop-stage: first pipeline stage (in order) where the gold is absent.
        if delivered_rank is not None:
            drop_stage = "DELIVERED (rank %d)" % delivered_rank
        elif native_rank is None and loc_r is None and not exact_fired:
            drop_stage = "seeder_universe (never retrieved/scored; not in run_v74.ranked_full, not localized, exact-name did not fire)"
        elif loc_r is None and not exact_fired:
            # retrieved by run_v74 but neither localize nor exact-name surfaced it
            drop_stage = "render_kcap (in seeder@%s but not localized, not exact-named -> cut by adaptive-K/render)" % native_rank
        else:
            # it WAS surfaced by localize or exact-name yet still not delivered
            drop_stage = "render_kcap (surfaced by %s but dropped before delivery)" % (
                "localize@%s" % loc_r if loc_r is not None else "exact_name"
            )
        per_gold[g] = {
            "norm": gn,
            "delivered_rank": delivered_rank,
            "delivered": delivered_rank is not None,
            "seeder_native_rank": native_rank,
            "localize_rank": loc_r,
            "localize_info": loc["gold_loc_info"].get(gn),
            "exact_name_fired": bool(exact_fired),
            "exact_name_symbols": exact_fired,
            "drop_stage": drop_stage,
            "tier": result.confidence_tier,
        }

    agreement_slice = _agreement_ge2_slice(loc.get("agreement_by_file", {}), gold_norm)

    return {
        "gold_files": gold_files,
        "confidence_tier": result.confidence_tier,
        "delivered_count": len(delivered),
        "delivered_candidates": candidate_list,
        "effective_w_sem": round(float(result.effective_w_sem), 8),
        "semantic_signal_count": int(result.semantic_signal_count),
        "per_gold": per_gold,
        "agreement_ge2": agreement_slice,
        "stages": stages,
    }


# ============================================================================
# OFFLINE-DELIVERED MODE (Phase-0 D2) — measure what the tape ALREADY delivered.
# No pipeline re-run, no ONNX, no repo checkout: parse <gt-localization> from the
# delivered brief, score vs TRUE gold (deepswe_gold / D1), label the stratum
# (stratum.py / D0), and compare GT's step-0 top-1 to the agent's OWN first
# source view/edit from the canonical trajectory. This is the *what-shipped*
# measurement, and it corrects the #51 first cut (which used the model's own
# submitted patch as the reference — self-referential).
# ============================================================================

import glob as _glob  # noqa: E402
import importlib.util as _ilu  # noqa: E402
import re as _re  # noqa: E402
import sqlite3 as _sqlite3  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from groundtruth.pretask.stratum import classify_stratum  # noqa: E402

# D1 gold resolver lives in scripts/analysis/ (a sibling script, not a package) —
# load it by path so this works regardless of cwd.
_GOLD_MOD_PATH = _Path(__file__).resolve().parent / "analysis" / "deepswe_gold.py"
_gspec = _ilu.spec_from_file_location("deepswe_gold", _GOLD_MOD_PATH)
assert _gspec and _gspec.loader
deepswe_gold = _ilu.module_from_spec(_gspec)
_gspec.loader.exec_module(deepswe_gold)

_SRC_EXT = r"(?:rs|py|go|ts|tsx|js|jsx|java|rb|c|cc|cpp|h|hpp|kt|scala|php|swift|m|mm)"
_LOC_LINE = _re.compile(r"^\s*\d+\.\s*([\w.\-/]+\.%s)\b" % _SRC_EXT)
_EDIT_TARGET_LINE = _re.compile(r"^\s*Edit target:\s*([\w.\-/]+\.%s)\s*::" % _SRC_EXT)
# HIGH head under GT_CONSENSUS_LEDGER (Fable C5): the ledger drops the "Edit target:"
# imperative — the head becomes "<file> :: <func> — N/3 signals agree (...)". Without
# this, every HIGH-tier ledger delivery scored delivered=[] (false top-1 miss + the tape
# leak check went blind on that line). Anchored on the path-then-`::`-then-`signals agree`
# marker so it never false-matches a numbered line or the legacy Edit-target line.
_LEDGER_HIGH_LINE = _re.compile(
    r"^\s*([\w.\-/]+\.%s)\s*::\s*\S.*\bsignals agree\b" % _SRC_EXT)
_VIEW_VERB = _re.compile(r"\b(cat|sed|head|tail|less|more|nl|view|open|awk)\b")
_EDIT_VERB = _re.compile(
    r"(str_replace|apply_patch|new_str|insert_line|\btee\b|sed\s+-i|>>?\s*[\w./]+\.%s)" % _SRC_EXT
)
_PATH_RE = _re.compile(r"(?:\./)?(?:[\w.\-/]+/)?[\w.\-]+\.%s\b" % _SRC_EXT)
# FILENAME-ANCHORED test detection: a test DIRECTORY component, or a basename that
# starts with test_ / ends in _test. / .test. / .spec. — NOT the substring "test_"
# anywhere (which wrongly filtered the product file bandit/core/test_set.py).
_TESTPATH = _re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/"          # a tests/ directory component
    r"|(^|/)test_[^/]*$"                              # basename test_*.ext
    r"|[^/]*_test\.[A-Za-z0-9]+$"                    # basename *_test.ext
    r"|[^/]*\.(test|spec)\.[A-Za-z0-9]+$"            # basename *.test.ext / *.spec.ext
)
# Heredoc / scratch write targets (`cat > /tmp/patch.py`) — the agent's edit VERB
# points at /tmp, but the file it actually edits is named in the heredoc body.
# ANCHORED at path start: `_np` has already stripped the leading slash, so an
# absolute scratch path is `tmp/...`/`dev/...` at position 0, while a repo path
# keeps its component mid-string (`src/dev/server.js`, `core/tmp/cache.go`) and
# must NOT be dropped.
_SCRATCH = _re.compile(r"^(?:tmp|var/tmp|dev)/")
# Harness infra probes the agent runs before touching code — searches for these are
# environment discovery, NOT localization, so they must not set first_search_repo_wide.
_INFRA_PROBE = _re.compile(
    r"gt_root\.txt|/opt/gt|go\.mod|package\.json|pyproject|Cargo\.toml|tsconfig|"
    r"\.git\b|site-packages|node_modules"
)
# A LOCALIZATION search finds where relevant CODE lives: a content grep, or a
# filename search for a file pattern. It is NOT a directory-structure enumeration
# (`find -type d`, `ls`, `tree`) — that is orientation, not localization.
_CONTENT_SEARCH = _re.compile(r"\b(?:grep|rg|ag|ack)\b|\bgit\s+grep\b")
_NAME_SEARCH = _re.compile(r"\b(?:find|fd)\b.*(?:-name|-iname|-path|-regex)\b")
# Write-target extraction for edits (so a heredoc `cat > /tmp/p.py` whose BODY writes
# the real file, or a comment naming another file, doesn't mis-attribute the target):
#   a redirect to a real file, or an `open('<f>', 'w'|'a'|'x')` write in a script body.
_REDIR_TARGET = _re.compile(r">>?\s*([\w.\-/]+\.[A-Za-z0-9]+)")
_OPEN_WRITE = _re.compile(r"""open\(\s*['"]([^'"]+\.[A-Za-z0-9]+)['"]\s*,\s*['"][wax]""")


def _np(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def _hit_at_k(gold_norm: list[str], cands: list[str], k: int) -> bool:
    # suffix-tolerant, mirrors measure_v1r_localization.py:95
    gs = set(gold_norm)
    for c in cands[:k]:
        if c in gs or any(c.endswith("/" + g) or g.endswith("/" + c) for g in gs):
            return True
    return False


def _first_gold_rank(gold_norm: list[str], cands: list[str]) -> int | None:
    # 1-indexed, None if never; mirrors measure_v1r_localization.py:104
    gs = set(gold_norm)
    for i, c in enumerate(cands, 1):
        if c in gs or any(c.endswith("/" + g) or g.endswith("/" + c) for g in gs):
            return i
    return None


def _parse_localization_block(brief_text: str) -> list[str]:
    """Ordered candidate paths from the delivered ``<gt-localization>`` block.

    Matches ALL THREE producer formats emitted by v1r_brief._localization_header:
      * HIGH  (:2914) — a single ``Edit target: <file> :: <func>`` line;
      * MEDIUM(:2973) — ``N. <file> — funcs`` numbered list;
      * LOW   (:2957) — ``Region: .../`` + ``N. <file>`` numbered list.
    The earlier version knew only the numbered list, so it scored the brief's
    HIGHEST-confidence single-target deliveries as empty (top1 miss)."""
    m = _re.search(r"<gt-localization[^>]*>(.*?)</gt-localization>", brief_text or "", _re.S)
    if not m:
        return []
    out: list[str] = []

    def _add(p: str) -> None:
        p = _np(p)
        if p and p not in out:
            out.append(p)

    for line in m.group(1).splitlines():
        # HIGH single-target: "Edit target: <path> :: <func>"
        hi = _EDIT_TARGET_LINE.match(line)
        if hi:
            _add(hi.group(1))
            continue
        # HIGH ledger head (GT_CONSENSUS_LEDGER): "<path> :: <func> — N/3 signals agree"
        hl = _LEDGER_HIGH_LINE.match(line)
        if hl:
            _add(hl.group(1))
            continue
        # MEDIUM/LOW numbered list: "  N. <path> — ..."
        mm = _LOC_LINE.match(line)
        if mm:
            _add(mm.group(1))
    return out


def _primary_language(graph_db: str) -> str | None:
    try:
        c = _sqlite3.connect(graph_db)
        rows = c.execute(
            "SELECT language, COUNT(*) n FROM nodes WHERE language IS NOT NULL "
            "GROUP BY language ORDER BY n DESC, language ASC"
        ).fetchall()
        c.close()
    except Exception:
        return None
    for lang, _ in rows:
        if lang and lang.lower() not in ("markdown", "text", "txt", "json", "yaml", "toml"):
            return lang
    return rows[0][0] if rows else None


def _canonical_trajectory(tape_dir: str) -> str | None:
    """The agent trajectory. TWO writers, TWO schemas: the normalized ``steps[]``
    ``trajectory.json`` (8 tapes) and the mini-swe-agent ``messages[]``
    ``mini-swe-agent.trajectory.json`` (19 tapes) — plus retry attempts. Prefer the
    normalized file, then the mini file, then the latest retry attempt.
    ``_iter_agent_commands`` reads either schema."""
    agent_dirs = sorted(_glob.glob(os.path.join(tape_dir, "jobs", "**", "agent"), recursive=True))
    for name in ("trajectory.json", "mini-swe-agent.trajectory.json"):
        for ad in agent_dirs:
            p = os.path.join(ad, name)
            if os.path.isfile(p):
                return p
    attempts = _glob.glob(
        os.path.join(tape_dir, "jobs", "**", "agent", "mini-swe-agent.trajectory.attempt*.json"),
        recursive=True,
    )
    return sorted(attempts)[-1] if attempts else None


def _iter_agent_commands(traj: object) -> list[str]:
    """Ordered bash commands the agent ran, SHAPE-AWARE:
      * steps[]    : steps[].tool_calls[].arguments.command   (source=='agent')
      * messages[] : messages[].extra.actions[].command       (Responses-API / mini)
    The mini writer nests the command under ``extra.actions`` (role is absent on the
    assistant response objects), so a steps[]-only reader silently sees nothing."""
    cmds: list[str] = []
    if not isinstance(traj, dict):
        return cmds
    steps = traj.get("steps")
    if steps:
        for s in steps:
            if not isinstance(s, dict) or s.get("source") != "agent":
                continue
            for tc in (s.get("tool_calls") or []):
                c = (tc.get("arguments") or {}).get("command")
                if isinstance(c, str):
                    cmds.append(c)
        return cmds
    for m in (traj.get("messages") or []):
        if not isinstance(m, dict):
            continue
        for a in ((m.get("extra") or {}).get("actions") or []):
            c = a.get("command")
            if isinstance(c, str):
                cmds.append(c)
    return cmds


def _is_localization_search(cmd: str) -> bool:
    """A repo search meant to LOCALIZE code: a content grep, or a filename search —
    but NOT a directory-structure enumeration (`find -type d`, orientation) and NOT a
    harness infra probe."""
    if _INFRA_PROBE.search(cmd):
        return False
    if _CONTENT_SEARCH.search(cmd):
        return True
    if _NAME_SEARCH.search(cmd) and "-type d" not in cmd:
        return True
    return False


def _edit_target(cmd: str, src_paths: list[str]) -> str | None:
    """The file an edit command actually writes. Priority: a redirect / edit arg
    pointing at a REAL (non-scratch) file; else an `open('<f>','w')` write inside a
    heredoc script body; else the first non-scratch source path. This stops a comment
    (or a read of a different file) inside a `cat > /tmp/patch.py` body from stealing
    the attribution — the earlier `src_paths[0]` was order-fragile."""
    for m in _REDIR_TARGET.finditer(cmd):
        p = _np(m.group(1))
        if not _SCRATCH.search(p):
            return p
    for m in _OPEN_WRITE.finditer(cmd):
        p = _np(m.group(1))
        if not _SCRATCH.search(p):
            return p
    return src_paths[0] if src_paths else None


def _agent_first_hits(traj_path: str | None, gold_norm: list[str]) -> dict:
    """Agent's OWN localization: first non-test source VIEW, first EDIT, whether each
    hits gold, and whether the first localization search was repo-wide (the GT-blind
    moment). ``has_trajectory`` is False when no commands were read, so the aggregate
    can size the agent denominator instead of silently averaging over a biased
    subsample. Step fields are 0-based indices into the agent-COMMAND list (named
    ``_cmd_index`` — they are NOT the trajectory's own step_id, which the mini writer
    does not carry). Missing/unreadable trajectory -> all null."""
    res: dict[str, object] = {
        "has_trajectory": False,
        "first_view_file": None, "first_view_cmd_index": None, "first_view_hit": None,
        "first_edit_file": None, "first_edit_cmd_index": None, "first_edit_hit": None,
        "first_search_repo_wide": None, "n_search_before_view": None,
    }
    if not traj_path or not os.path.isfile(traj_path):
        return res
    try:
        d = json.load(open(traj_path, encoding="utf-8", errors="replace"))
    except Exception:
        return res
    cmds = _iter_agent_commands(d)
    res["has_trajectory"] = bool(cmds)
    gs = set(gold_norm)

    def _hit(p: str) -> bool:
        return p in gs or any(p.endswith("/" + g) or g.endswith("/" + p) for g in gs)

    n_search = 0
    for idx, cmd in enumerate(cmds):
        # source paths referenced, minus /tmp scratch and minus test files — UNLESS
        # the path IS gold (a gold file that is test-NAMED, e.g. bandit/core/test_set.py,
        # a product registry, is the target and must never be filtered).
        src_paths = []
        for _p in _PATH_RE.findall(cmd):
            _p = _np(_p)
            if _SCRATCH.search(_p):
                continue
            if _TESTPATH.search(_p) and not _hit(_p):
                continue
            src_paths.append(_p)
        is_edit = bool(_EDIT_VERB.search(cmd))
        # a write (`cat > f`) matches the view verb `cat` but is NOT a view.
        is_view = bool(_VIEW_VERB.search(cmd)) and not is_edit
        if _is_localization_search(cmd):
            n_search += 1
            if res["first_search_repo_wide"] is None:
                # repo-wide == the search names no specific source file
                res["first_search_repo_wide"] = not _PATH_RE.findall(cmd)
        if res["first_view_file"] is None and is_view and src_paths:
            res["first_view_file"] = src_paths[0]
            res["first_view_cmd_index"] = idx
            res["first_view_hit"] = _hit(src_paths[0])
            res["n_search_before_view"] = n_search
        if res["first_edit_file"] is None and is_edit:
            tgt = _edit_target(cmd, src_paths)
            if tgt is not None:
                res["first_edit_file"] = tgt
                res["first_edit_cmd_index"] = idx
                res["first_edit_hit"] = _hit(tgt)
    return res


def measure_tape_offline(tape_dir: str) -> dict:
    """Measure ONE tape's delivered localization vs TRUE gold. Fully offline."""
    task = deepswe_gold.task_id_from_tape_dir(os.path.basename(tape_dir))
    ga = os.path.join(tape_dir, "gt_artifacts")
    brief_p = os.path.join(ga, "brief.txt")
    issue_p = os.path.join(ga, "issue.txt")
    graph_db = os.path.join(tape_dir, "graph.db")
    # A missing brief.txt means GT never delivered on this tape (an INFRA-dead
    # run), NOT that GT localized to nothing — it must be EXCLUDED from hit@k, the
    # same as gold=UNKNOWN, never scored as a miss.
    brief_present = os.path.isfile(brief_p)
    brief_text = open(brief_p, encoding="utf-8", errors="replace").read() if brief_present else ""
    issue_text = open(issue_p, encoding="utf-8", errors="replace").read() if os.path.isfile(issue_p) else ""

    delivered = _parse_localization_block(brief_text)
    gold = [_np(g) for g in deepswe_gold.gold_files_for_task(task)]
    gold_known = bool(gold)
    delivered_known = brief_present
    scorable = gold_known and delivered_known
    test_files = [_np(t) for t in deepswe_gold.test_patch_files_for_task(task)]
    meta = deepswe_gold.task_meta(task)
    # LANGUAGE: task.toml is the ground truth (maintainer-declared). The graph
    # node-majority mislabels a task whose repo vendors another language (aiomonitor:
    # 1480 vendored JS nodes vs 179 python) — task.toml says python, so it wins.
    language = meta.get("language") or (_primary_language(graph_db) if os.path.isfile(graph_db) else None)

    # Stratum (D0): re-extract anchors from issue.txt + graph.db (deterministic,
    # equivalent to the tape's persisted gt_issue_anchors.json).
    strat = classify_stratum(issue_text, graph_db if os.path.isfile(graph_db) else None)

    # LEAK: no delivered localization path may be a verifier test file.
    leak = sorted(set(delivered) & set(test_files))

    # Agent's own localization from the trajectory.
    agent = _agent_first_hits(_canonical_trajectory(tape_dir), gold)

    # ITEM-D post_search probe (offline): run item-A's real parser+resolver over the
    # agent's OWN localization searches. Scorable only when gold is known (else the
    # fire-precision has no reference). Guarded — never aborts the tape scoring.
    post_search = None
    if gold_known and os.path.isfile(graph_db):
        try:
            _cmds = _agent_search_cmds(tape_dir)
            post_search = post_search_probe(_cmds, graph_db, "", gold)
        except Exception:  # noqa: BLE001
            post_search = None

    # Reward / resolved (diagnostic pairing, not a gate).
    reward = None
    tt_p = os.path.join(tape_dir, "task_truth.json")
    if os.path.isfile(tt_p):
        try:
            tt = json.load(open(tt_p, encoding="utf-8", errors="replace"))
            reward = (tt.get("outcome") or {}).get("reward")
        except Exception:
            reward = None

    gt_top1 = _hit_at_k(gold, delivered, 1) if scorable else None
    gt_top3 = _hit_at_k(gold, delivered, 3) if scorable else None
    fgr = _first_gold_rank(gold, delivered) if scorable else None

    return {
        "task": task,
        "language": language,
        "stratum": strat.label,
        "stratum_evidence": strat.evidence,
        "gold_known": gold_known,
        "delivered_known": delivered_known,
        "scorable": scorable,
        "gold_files": gold,
        "delivered": delivered,
        "gt_top1_hit": gt_top1,
        "gt_top3_hit": gt_top3,
        "gt_first_gold_rank": fgr,
        "agent": agent,
        "has_agent_trajectory": agent["has_trajectory"],
        # GT delivers at step 0; the agent reaches its first gold view after
        # n_search_before_view searches. Positive = GT was there first.
        "agent_first_view_hit": agent["first_view_hit"],
        "agent_first_edit_hit": agent["first_edit_hit"],
        "first_search_repo_wide": agent["first_search_repo_wide"],
        "leak_test_files": leak,
        "leak_count": len(leak),
        "post_search": post_search,
        "reward": reward,
    }


def _rate(vals: list) -> tuple[float | None, int]:
    xs = [v for v in vals if v is not None]
    return (round(sum(1 for v in xs if v) / len(xs), 8), len(xs)) if xs else (None, 0)


def aggregate_offline(reports: list[dict]) -> dict:
    """Stratify hit@1/@3 + agent deltas by stratum and by language, 8-dp."""
    def _cell(rs: list[dict]) -> dict:
        # GT hit@k excludes non-scorable tapes (gt_top1_hit is None); agent rates
        # carry their OWN denominator (n_agent = tapes with a readable trajectory)
        # so a biased subsample is visible, not hidden inside n.
        g1, n_scorable = _rate([r["gt_top1_hit"] for r in rs])
        g3, _ = _rate([r["gt_top3_hit"] for r in rs])
        av, _ = _rate([r["agent_first_view_hit"] for r in rs])
        ae, n_agent = _rate([r["agent_first_edit_hit"] for r in rs])
        fgrs = [r["gt_first_gold_rank"] for r in rs if r["gt_first_gold_rank"] is not None]
        return {
            "n": len(rs),
            "n_scorable": n_scorable,
            "n_agent": sum(1 for r in rs if r.get("has_agent_trajectory")),
            "n_agent_edit_measured": n_agent,
            "gt_hit_at_1": g1, "gt_hit_at_3": g3,
            "agent_first_view_hit": av, "agent_first_edit_hit": ae,
            "gt_mean_first_gold_rank": round(sum(fgrs) / len(fgrs), 8) if fgrs else None,
            "leak_total": sum(r["leak_count"] for r in rs),
        }

    by_stratum = {}
    for label in ("A", "B", "C", "D"):
        rs = [r for r in reports if r["stratum"] == label]
        if rs:
            by_stratum[label] = _cell(rs)
    lang_groups: dict[str, list] = {}
    for r in reports:
        lang_groups.setdefault(r["language"] or "unknown", []).append(r)
    by_language = {k: _cell(v) for k, v in sorted(lang_groups.items())}

    return {
        "n_tapes": len(reports),
        "n_gold_unknown": sum(1 for r in reports if not r["gold_known"]),
        "n_brief_undelivered": sum(1 for r in reports if not r["delivered_known"]),
        "n_agent_trajectories": sum(1 for r in reports if r.get("has_agent_trajectory")),
        "leak_total": sum(r["leak_count"] for r in reports),
        "overall": _cell(reports),
        "by_stratum": by_stratum,
        "by_language": by_language,
        "post_search": _aggregate_post_search(reports),
        "agreement_ge2": _aggregate_agreement(reports),
    }


# ============================================================================
# ITEM D — post_search abstention/precision probe (OFFLINE, no ONNX).
# On the agent's OWN localization searches, measure item-A (post_search):
#   * ABSTENTION — of the agent's grep/rg searches, how many are NOT a bare
#     symbol (path/glob/regex/multi-token) so _search_pattern correctly stays
#     silent (correct-or-quiet). abstain_rate = abstained / grep_searches.
#   * PRECISION  — of the fires that RESOLVE to a def (unambiguous, non-shadow),
#     how many land the def-site in the gold file. Uses the SHIPPING parser +
#     resolver from gt_mini_patch (no re-implementation). Leak counter must stay 0
#     (a test-path def-site would be a leak). Guarded import: degrades to
#     available=False if artifact_deepswe / gt_mini_patch cannot be imported.
# ============================================================================
_GT_MINI = None
_GT_MINI_TRIED = False


def _gt_mini():
    global _GT_MINI, _GT_MINI_TRIED
    if _GT_MINI_TRIED:
        return _GT_MINI
    _GT_MINI_TRIED = True
    try:
        _ad = _Path(__file__).resolve().parent.parent / "artifact_deepswe"
        if str(_ad) not in sys.path:
            sys.path.insert(0, str(_ad))
        import gt_mini_patch as _g  # noqa: PLC0415
        _GT_MINI = _g
    except Exception:  # noqa: BLE001 — probe is best-effort; never abort the run
        _GT_MINI = None
    return _GT_MINI


def _is_grep_search(g, cmd: str) -> bool:
    try:
        return bool(g._GREP_HEAD_RE.search((cmd or "").split("\n", 1)[0]))
    except Exception:  # noqa: BLE001
        return False


def post_search_probe(cmds: list[str], graph_db: str, repo_root: str,
                      gold_norm: list[str]) -> dict:
    """Run item-A's real parser+resolver over the agent's search commands."""
    g = _gt_mini()
    base = {
        "available": bool(g) and bool(graph_db) and os.path.isfile(graph_db or ""),
        "n_search": len(cmds),
        "n_grep_search": 0, "n_abstain": 0, "abstain_rate": None,
        "n_fire": 0, "n_resolved": 0, "n_fire_gold": 0,
        "fire_precision": None, "leak_count": 0, "fire_symbols": [],
    }
    if not base["available"]:
        return base
    gs = set(gold_norm)
    con = None
    try:
        con = g._connect_ro(graph_db)
    except Exception:  # noqa: BLE001
        con = None
    if con is None:
        base["available"] = False
        return base
    n_grep = n_abstain = n_fire = n_resolved = n_gold = n_leak = 0
    fired: list[str] = []
    try:
        for cmd in cmds:
            is_grep = _is_grep_search(g, cmd)
            if is_grep:
                n_grep += 1
            sym = g._search_pattern(cmd)
            if sym is None:
                if is_grep:
                    n_abstain += 1   # a grep that is not a bare symbol -> silent
                continue
            n_fire += 1
            fired.append(sym)
            try:
                info = g._resolve_symbol_defs(con, sym, repo_root or "")
            except Exception:  # noqa: BLE001
                info = None
            if not info or not info.get("def_sites"):
                continue          # resolved to nothing / ambiguous -> abstain
            n_resolved += 1
            def_files = [_np(fp) for fp, _ln in info["def_sites"]]
            if any(_gold_match(f, gs) for f in def_files):
                n_gold += 1
            for f in def_files:
                try:
                    if g._POST_SEARCH_TESTPATH.search(f.replace("\\", "/")):
                        n_leak += 1
                except Exception:  # noqa: BLE001
                    pass
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    base.update({
        "n_grep_search": n_grep, "n_abstain": n_abstain,
        "abstain_rate": round(n_abstain / n_grep, 8) if n_grep else None,
        "n_fire": n_fire, "n_resolved": n_resolved, "n_fire_gold": n_gold,
        "fire_precision": round(n_gold / n_resolved, 8) if n_resolved else None,
        "leak_count": n_leak, "fire_symbols": sorted(set(fired)),
    })
    return base


def _agent_search_cmds(tape_dir: str) -> list[str]:
    """The agent's LOCALIZATION searches from the canonical trajectory (reuses the
    same shape-aware reader + localization-search filter as the offline scorer)."""
    tp = _canonical_trajectory(tape_dir)
    if not tp or not os.path.isfile(tp):
        return []
    try:
        d = json.load(open(tp, encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []
    return [c for c in _iter_agent_commands(d) if _is_localization_search(c)]


def _aggregate_post_search(reports: list[dict]) -> dict:
    ps = [r.get("post_search") for r in reports if r.get("post_search")]
    ps = [p for p in ps if p and p.get("available")]
    if not ps:
        return {"available": False, "n_tapes": 0}
    n_grep = sum(p["n_grep_search"] for p in ps)
    n_abstain = sum(p["n_abstain"] for p in ps)
    n_fire = sum(p["n_fire"] for p in ps)
    n_res = sum(p["n_resolved"] for p in ps)
    n_gold = sum(p["n_fire_gold"] for p in ps)
    n_leak = sum(p["leak_count"] for p in ps)
    return {
        "available": True,
        "n_tapes": len(ps),
        "n_grep_search": n_grep,
        "n_abstain": n_abstain,
        "abstain_rate": round(n_abstain / n_grep, 8) if n_grep else None,
        "n_fire": n_fire,
        "n_resolved": n_res,
        "n_fire_gold": n_gold,
        "fire_precision": round(n_gold / n_res, 8) if n_res else None,
        "leak_total": n_leak,
    }


def _aggregate_agreement(reports: list[dict]) -> dict:
    """Micro-aggregate the agreement>=2 precision slice across tapes/tasks (8-dp)."""
    slices = [r.get("agreement_ge2") for r in reports if r.get("agreement_ge2")]
    if not slices:
        return {"n_tapes": 0}
    n_files = sum(s["n_files_agree_ge2"] for s in slices)
    n_gold = sum(s["n_agree_ge2_gold"] for s in slices)
    return {
        "n_tapes": len(slices),
        "n_files_agree_ge2": n_files,
        "n_agree_ge2_gold": n_gold,
        "precision": round(n_gold / n_files, 8) if n_files else None,
    }


def run_tapes_offline(tapes_dir: str, out_dir: str | None) -> dict:
    tape_dirs = sorted(_glob.glob(os.path.join(tapes_dir, "deepswe-full-*")))
    reports = []
    for td in tape_dirs:
        if not os.path.isdir(td):
            continue
        rep = measure_tape_offline(td)
        reports.append(rep)
        mark = "LEAK!" if rep["leak_count"] else ("" if rep["scorable"] else "[excl]")
        print(
            f"  {rep['task'][:34]:<34} {str(rep['language']):<10} strat={rep['stratum']} "
            f"gold={'?' if not rep['gold_known'] else len(rep['gold_files'])} "
            f"top1={rep['gt_top1_hit']} top3={rep['gt_top3_hit']} fgr={rep['gt_first_gold_rank']} "
            f"traj={'Y' if rep['has_agent_trajectory'] else '.'} "
            f"agentEdit={rep['agent_first_edit_hit']} {mark}"
        )
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            json.dump(rep, open(os.path.join(out_dir, f"measure_brief_{rep['task']}.json"), "w",
                               encoding="utf-8"), indent=2, default=str)
    agg = aggregate_offline(reports)
    if out_dir:
        json.dump(agg, open(os.path.join(out_dir, "measure_brief_AGGREGATE.json"), "w",
                           encoding="utf-8"), indent=2, default=str)
    print("\n=== AGGREGATE (8-dp) ===")
    print(json.dumps(agg, indent=2, default=str))
    return agg


def _print_table(task_id: str, rep: dict) -> None:
    print(f"\n=== {task_id} === tier={rep['confidence_tier']} "
          f"delivered={rep['delivered_count']} w_sem={rep['effective_w_sem']} "
          f"sem_signal={rep['semantic_signal_count']}")
    print("  delivered candidates:")
    for c in rep["delivered_candidates"]:
        mark = " <== GOLD" if c["is_gold"] else ""
        print(f"    #{c['rank']:<2} {c['score']:.6f}  {c['path']}{mark}")
    print("  per-gold:")
    for g, info in rep["per_gold"].items():
        print(f"    GOLD {g}")
        print(f"       delivered_rank = {info['delivered_rank']}  "
              f"seeder_rank = {info['seeder_native_rank']}  "
              f"localize_rank = {info['localize_rank']}  "
              f"exact_name_fired = {info['exact_name_fired']}")
        print(f"       DROP-STAGE: {info['drop_stage']}")


def _load_task_configs(testset_dir: str = TESTSET_DIR) -> list[dict]:
    tasks_dir = os.path.join(testset_dir, "tasks")
    out = []
    for fn in sorted(os.listdir(tasks_dir)):
        if fn.endswith(".json"):
            out.append(json.load(open(os.path.join(tasks_dir, fn), encoding="utf-8")))
    return out


def run_task(cfg: dict, env_info: dict) -> dict:
    iid = cfg["id"]
    graph_db = cfg.get("graph_db")
    repo_dir = cfg.get("repo_dir")
    gold = cfg.get("gold_files", [])
    issue = cfg.get("issue_text", "")
    if not graph_db or not os.path.exists(graph_db):
        rep = {
            "task_id": iid,
            "category": cfg.get("category"),
            "status": "graph.db unavailable",
            "graph_db": graph_db,
            "gold_files": gold,
        }
        print(f"\n=== {iid} === graph.db UNAVAILABLE — skipped ({cfg.get('index_status')})")
        return rep
    rep = measure_one(issue, repo_dir or "", graph_db, gold)
    rep["task_id"] = iid
    rep["category"] = cfg.get("category")
    rep["status"] = "measured"
    rep["graph_db"] = graph_db
    rep["env"] = env_info
    _print_table(iid, rep)
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-file")
    ap.add_argument("--graph")
    ap.add_argument("--repo")
    ap.add_argument("--gold", action="append", default=[])
    ap.add_argument("--task-config")
    ap.add_argument("--testset", action="store_true")
    ap.add_argument("--tapes", help="OFFLINE-delivered mode: measure every deepswe-full-* tape "
                                    "dir under this path (parses the delivered <gt-localization>, "
                                    "scores vs TRUE gold, no ONNX/repo/pipeline).")
    ap.add_argument("--testset-dir", default=TESTSET_DIR,
                    help="override the testset dir (tasks/ + out) for held-out splits")
    ap.add_argument("--out")
    args = ap.parse_args()

    # OFFLINE-delivered mode reads already-shipped artifacts — no embedder needed,
    # so it must NOT gate on the ONNX semantic assert (which would abort without
    # the baked models). It measures what the agent actually saw on the tapes.
    if args.tapes:
        run_tapes_offline(args.tapes, args.out)
        return

    env_info = _assert_semantic_on()
    print("[ENV] semantic ON:", json.dumps(env_info))

    if args.testset:
        results = []
        for cfg in _load_task_configs(args.testset_dir):
            results.append(run_task(cfg, env_info))
        agreement = _aggregate_agreement(results)
        agg = {"env": env_info, "tasks": results, "agreement_ge2": agreement}
        out = args.out or os.path.join(args.testset_dir, "measure_results.json")
        json.dump(agg, open(out, "w", encoding="utf-8"), indent=2, default=str)
        print("\n=== agreement>=2 precision slice (8-dp) ===")
        print(json.dumps(agreement, indent=2, default=str))
        print(f"\n[WROTE] {out}")
        return

    if args.task_config:
        cfg = json.load(open(args.task_config, encoding="utf-8"))
        rep = run_task(cfg, env_info)
        out = args.out or os.path.join(TESTSET_DIR, f"measure_{cfg['id']}.json")
        json.dump(rep, open(out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\n[WROTE] {out}")
        return

    if not (args.issue_file and args.graph and args.repo):
        ap.error("provide --task-config / --testset, or --issue-file --graph --repo [--gold ...]")
    issue = open(args.issue_file, encoding="utf-8").read()
    rep = measure_one(issue, args.repo, args.graph, args.gold)
    rep["env"] = env_info
    _print_table(os.path.basename(args.graph), rep)
    if args.out:
        json.dump(rep, open(args.out, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\n[WROTE] {args.out}")


if __name__ == "__main__":
    main()

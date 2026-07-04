#!/usr/bin/env python3
"""measure_content_leg_stratumB.py — the content-BM25 stratum-B upside driver (task #84).

QUESTION MEASURED
-----------------
Does the content-BM25 leg (``GT_CONTENT_LEG=1``) improve stratum-B (behaviour-
described, no greppable symbol) gold-file localization vs OFF?

The leg is ``graph_localizer._content_fts_candidates`` — a BM25 query over the
baked ``symbol_content_fts`` table — gated inside ``localize()`` at:

    if (not grep_recalled) and os.getenv("GT_CONTENT_LEG") == "1":
        _cfts = _content_fts_candidates(conn, terms, limit=_grep_limit)

It fires ONLY in the grep-dead lane (no repo grep recalled anything) and reads the
FTS table straight out of graph.db, so it needs NO source repo at query time.

Two lanes, so the leg's effect is visible both isolated and realistic:
  * ``graphdb``  — localize() with repo_root="" (the graph.db-only / MCP lane the
                   leg was built for). For a stratum-B task OFF early-returns empty
                   (no resolving anchor, no repo, flag off) -> the honest "no
                   content surface" baseline; ON fills the vacant lexical slot with
                   the BM25 body-content candidates. This is the headline lane.
  * ``repo``     — localize() with repo_root=<checkout> (grep active). Here the
                   content leg is MUTUALLY EXCLUSIVE with grep, so ON==OFF unless
                   grep recalled literally nothing. A diagnostic on how often the
                   live grep leg masks the content leg.

Only the ``GT_CONTENT_LEG`` env var differs between the two arms of a lane — every
other input (issue text, graph, repo_root, embedder state) is held identical, so a
per-task OFF/ON delta isolates the leg.

LEAK INVARIANT (hard gate)
--------------------------
``localize()`` is fed ONLY (issue_text, graph_db, repo_root) — never the patch,
gold files, FAIL_TO_PASS/PASS_TO_PASS, or test assertions — so a leak is
structurally impossible. This driver still asserts it defensively: no rendered
candidate path or witness string may contain a verifier test name / test file. Any
leak aborts that task with a loud error and is counted; a nonzero global leak total
invalidates the aggregate.

USAGE (codespace full run)
--------------------------
    python scripts/analysis/measure_content_leg_stratumB.py \
        --sample 60 --gt-index ./gt-index/gt-index --out /tmp/b_result.json

    python scripts/analysis/measure_content_leg_stratumB.py \
        --ids /tmp/ids.txt --gt-index ./gt-index/gt-index --lane both \
        --workdir /tmp/gt_bwork --out /tmp/b_result.json

LOCAL SMOKE (no network, uses a baked witness graph)
----------------------------------------------------
    python scripts/analysis/measure_content_leg_stratumB.py --smoke

Deterministic, no LLM, Linux-portable (only stdlib + the groundtruth package).
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

# --- locate the groundtruth package relative to THIS file (codespace-portable) ---
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[2]  # scripts/analysis/<file>.py -> repo root
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from groundtruth.pretask import graph_localizer as gl  # noqa: E402
from groundtruth.pretask.stratum import classify_stratum  # noqa: E402

# ---------------------------------------------------------------------------
# CONTENT-LEG REACHED counter (debug provenance). Monkeypatch the module-global
# `_content_fts_candidates`; localize() resolves it as a bare module name at call
# time, so this wrapper sees every invocation even when the resulting candidates
# are later filtered out of the ranked set. This is the "did the ON path actually
# reach _content_fts_candidates" proof, independent of the ranked output.
# ---------------------------------------------------------------------------
_CFTS_COUNTER = {"calls": 0, "rows": 0}
_ORIG_CFTS = gl._content_fts_candidates


def _counting_cfts(conn, tokens, limit: int = 50):
    rows = _ORIG_CFTS(conn, tokens, limit=limit)
    _CFTS_COUNTER["calls"] += 1
    _CFTS_COUNTER["rows"] += len(rows)
    return rows


gl._content_fts_candidates = _counting_cfts  # install once, process-wide

DEFAULT_CORPUS = str(_REPO_ROOT / "benchmarks" / "data" / "swebench_live_lite.jsonl")
DEFAULT_WITNESS_DIR = "D:/gt_runs/depth_run_28699793199"

# Source-code extensions: the localization target is a source gold file, not a
# changelog/doc (a patch often also touches CHANGES/*.rst, docs/*.md, etc.).
_SRC_EXT = {
    "py", "go", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "rb", "rs",
    "c", "cc", "cpp", "cxx", "h", "hpp", "kt", "kts", "scala", "php", "swift",
    "m", "mm", "cs", "vue", "svelte",
}

# FILENAME-anchored test detection (mirrors measure_brief.py:_TESTPATH) — a test
# DIRECTORY component, or a basename test_*/ *_test / *.test / *.spec.
_TESTPATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/"
    r"|(^|/)test_[^/]*$"
    r"|[^/]*_test\.[A-Za-z0-9]+$"
    r"|[^/]*\.(test|spec)\.[A-Za-z0-9]+$"
)


class LeakError(RuntimeError):
    """Raised when a rendered candidate output contains a verifier test name."""


# ---------------------------------------------------------------------------
# small suffix-tolerant matchers (mirror measure_brief.py:_hit_at_k /
# _first_gold_rank). Reimplemented here rather than importing measure_brief,
# whose module body forces the ONNX embedder env + loads deepswe_gold.py.
# ---------------------------------------------------------------------------
def _norm(p: str) -> str:
    return gl._normalize(str(p or ""))


def _gold_match(cand: str, gold_set: set[str]) -> bool:
    return cand in gold_set or any(
        cand.endswith("/" + g) or g.endswith("/" + cand) for g in gold_set
    )


def _hit_at_k(gold_norm: list[str], cands: list[str], k: int) -> bool:
    gs = set(gold_norm)
    return any(_gold_match(c, gs) for c in cands[:k])


def _first_gold_rank(gold_norm: list[str], cands: list[str]) -> int | None:
    """1-indexed rank of the first gold file in the ranked list; None if absent."""
    gs = set(gold_norm)
    for i, c in enumerate(cands, 1):
        if _gold_match(c, gs):
            return i
    return None


# ---------------------------------------------------------------------------
# corpus + gold/test extraction
# ---------------------------------------------------------------------------
def _load_corpus(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                return [str(x) for x in json.loads(s)]
            except Exception:
                pass
        return [s] if s else []
    return []


_DIFF_TARGET = re.compile(r'^\+\+\+ b/(.+?)\s*$', re.MULTILINE)
_DIFF_GIT = re.compile(r'^diff --git a/(?:.+?) b/(.+?)\s*$', re.MULTILINE)


def _patch_target_files(patch: str) -> list[str]:
    """The b-side files a unified diff edits (deduped, order-preserving), skipping
    /dev/null delete targets."""
    out: list[str] = []
    seen: set[str] = set()
    for rx in (_DIFF_TARGET, _DIFF_GIT):
        for m in rx.finditer(patch or ""):
            f = _norm(m.group(1))
            if f and f != "dev/null" and f not in seen:
                seen.add(f)
                out.append(f)
    return out


def _ext(p: str) -> str:
    b = p.rsplit("/", 1)[-1]
    return b.rsplit(".", 1)[-1].lower() if "." in b else ""


def _extract_gold_and_tests(task: dict) -> dict:
    """gold_all = every file the FIX patch edits; test_files = verifier surface
    (test_patch targets + FAIL_TO_PASS/PASS_TO_PASS file paths + test-named golds).
    localization_gold = source (non-test) golds — the files the localizer should
    surface."""
    gold_all = _patch_target_files(task.get("patch", ""))
    test_patch_files = _patch_target_files(task.get("test_patch", ""))

    ftp = _as_list(task.get("FAIL_TO_PASS")) + _as_list(task.get("PASS_TO_PASS"))
    ftp_files = {_norm(e.split("::", 1)[0]) for e in ftp if "::" in e or "/" in e}
    ftp_names = {e.split("::")[-1] for e in ftp if "::" in e}

    test_files = set(test_patch_files) | ftp_files | {
        g for g in gold_all if _TESTPATH.search(g)
    }
    gold_product = [g for g in gold_all if g not in test_files]
    localization_gold = [g for g in gold_product if _ext(g) in _SRC_EXT]

    return {
        "gold_all": gold_all,
        "test_files": sorted(test_files),
        "gold_product": gold_product,
        "localization_gold": localization_gold,
        "ftp_test_names": sorted(ftp_names),
    }


def _leak_terms(gold_info: dict) -> set[str]:
    """The verifier surface that must NEVER appear in a rendered candidate output.
    Excludes anything that is part of the localization gold (a legitimate gold that
    happens to be test-named must not be flagged as a leak)."""
    gold = set(gold_info["localization_gold"]) | set(gold_info["gold_product"])
    terms: set[str] = set()
    for f in gold_info["test_files"]:
        terms.add(f)                     # full verifier path (distinctive)
        terms.add(f.rsplit("/", 1)[-1])  # basename (test_*.py etc.)
    # Test METHOD names only when they carry the "test" convention — the last
    # "::" component of a FAIL_TO_PASS id is often a generic parametrization
    # fragment (e.g. cfn-lint "Length", "ToJsonString") which, as a common
    # identifier, would false-positive the leak scan against a legitimate witness.
    for n in gold_info["ftp_test_names"]:
        if n and "test" in n.lower():
            terms.add(n)
    # never flag a gold path/name as a leak
    gold_names = gold | {g.rsplit("/", 1)[-1] for g in gold}
    return {t for t in terms if t and t not in gold_names and len(t) >= 5}


def _leak_scan(rendered_text: str, leak_terms: set[str]) -> list[str]:
    return sorted(t for t in leak_terms if t in rendered_text)


# ---------------------------------------------------------------------------
# clone + index
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int, cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        return p.returncode, (p.stdout or "")[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s: {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001
        return 1, f"EXC: {e!r}"


def _clone_at(repo: str, base_commit: str, dest: str, timeout: int) -> tuple[bool, str]:
    """Shallow blob-filtered clone then checkout base_commit. Falls back to a full
    clone if the partial clone fails."""
    url = f"https://github.com/{repo}"
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    rc, log = _run(
        ["git", "clone", "--filter=blob:none", "--quiet", url, dest], timeout
    )
    if rc != 0:
        shutil.rmtree(dest, ignore_errors=True)
        rc, log = _run(["git", "clone", "--quiet", url, dest], timeout)
        if rc != 0:
            return False, f"clone failed: {log}"
    rc, log = _run(
        ["git", "-C", dest, "checkout", "--quiet", base_commit], timeout
    )
    if rc != 0:
        return False, f"checkout {base_commit[:12]} failed: {log}"
    return True, "ok"


def _index(gt_index: str, repo_dir: str, graph_db: str, timeout: int) -> tuple[bool, str]:
    if os.path.isfile(graph_db):
        os.remove(graph_db)
    rc, log = _run([gt_index, "-root", repo_dir, "-output", graph_db], timeout)
    if rc != 0:
        return False, f"gt-index rc={rc}: {log}"
    if not os.path.isfile(graph_db):
        return False, "gt-index produced no graph.db"
    # symbol_content_fts is the leg's data source — a graph built without the
    # sqlite_fts5 tag silently omits it; fail-closed so the run is not measuring a
    # dead leg.
    try:
        c = sqlite3.connect(graph_db)
        has = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'"
        ).fetchone()
        nrows = c.execute("SELECT COUNT(*) FROM symbol_content_fts").fetchone()[0] if has else 0
        c.close()
    except sqlite3.Error as e:
        return False, f"graph.db unreadable: {e}"
    if not has:
        return False, "graph.db has NO symbol_content_fts (gt-index missing -tags sqlite_fts5?)"
    return True, f"ok ({nrows} content rows)"


# ---------------------------------------------------------------------------
# the OFF/ON localize primitive
# ---------------------------------------------------------------------------
def _localize_ranked(issue: str, graph_db: str, repo_root: str, content_leg: bool) -> dict:
    """Run localize() with GT_CONTENT_LEG toggled; return the ranked file list plus
    provenance. Captures stderr so the '[GT L1] content-fts leg' banner is a second,
    independent proof the leg fired."""
    os.environ["GT_CONTENT_LEG"] = "1" if content_leg else "0"
    before = dict(_CFTS_COUNTER)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        loc = gl.localize(issue, graph_db, top_k=8, repo_root=repo_root or "")
    files: list[str] = []
    seen: set[str] = set()
    for c in loc.candidates or []:
        p = _norm(c.file_path)
        if p not in seen:
            seen.add(p)
            files.append(p)
    witnesses = [c.render_witness() for c in (loc.candidates or [])]
    signals = loc.signals_by_file or {}
    err = buf.getvalue()
    return {
        "files": files,
        "witnesses": witnesses,
        "gate": loc.gate_reason,
        "signals_has_content": any("content" in v for v in signals.values()),
        "content_fts_calls": _CFTS_COUNTER["calls"] - before["calls"],
        "content_fts_rows": _CFTS_COUNTER["rows"] - before["rows"],
        "stderr_content_leg": "content-fts leg" in err,
    }


def _rendered_text(arm: dict) -> str:
    return "\n".join(arm["files"]) + "\n" + "\n".join(w for w in arm["witnesses"] if w)


def _score_arm(arm: dict, gold_norm: list[str]) -> dict:
    return {
        "n_candidates": len(arm["files"]),
        "gate": arm["gate"],
        "hit_at_1": _hit_at_k(gold_norm, arm["files"], 1),
        "hit_at_3": _hit_at_k(gold_norm, arm["files"], 3),
        "hit_at_5": _hit_at_k(gold_norm, arm["files"], 5),
        "gold_best_rank": _first_gold_rank(gold_norm, arm["files"]),
        "content_leg_reached": bool(arm["content_fts_calls"]) or arm["stderr_content_leg"],
        "signals_has_content": arm["signals_has_content"],
        "content_fts_rows": arm["content_fts_rows"],
        "top5": arm["files"][:5],
    }


def _measure_lane(issue: str, graph_db: str, repo_root: str, gold_norm: list[str],
                  leak_terms: set[str], lane: str) -> dict:
    off = _localize_ranked(issue, graph_db, repo_root, content_leg=False)
    on = _localize_ranked(issue, graph_db, repo_root, content_leg=True)
    for arm_name, arm in (("off", off), ("on", on)):
        leaks = _leak_scan(_rendered_text(arm), leak_terms)
        if leaks:
            raise LeakError(
                f"LEAK in lane={lane} arm={arm_name}: verifier terms surfaced in "
                f"rendered candidates: {leaks}"
            )
    return {"off": _score_arm(off, gold_norm), "on": _score_arm(on, gold_norm)}


# ---------------------------------------------------------------------------
# per-task driver
# ---------------------------------------------------------------------------
def process_task(task: dict, args, workdir: str) -> dict:
    iid = task.get("instance_id") or task.get("id") or "unknown"
    repo = task.get("repo", "")
    base = task.get("base_commit", "")
    issue = task.get("problem_statement", "") or task.get("all_hints_text", "") or ""
    rec: dict = {"instance_id": iid, "repo": repo, "status": "pending"}

    repo_dir = os.path.join(workdir, "repos", iid)
    graph_db = os.path.join(workdir, "graphs", f"{iid}.db")
    os.makedirs(os.path.dirname(graph_db), exist_ok=True)

    if not os.path.isfile(graph_db):
        ok, msg = _clone_at(repo, base, repo_dir, args.max_clone_seconds)
        if not ok:
            rec.update(status="skip_clone", detail=msg)
            return rec
        ok, msg = _index(args.gt_index, repo_dir, graph_db, args.max_index_seconds)
        if not ok:
            rec.update(status="skip_index", detail=msg)
            shutil.rmtree(repo_dir, ignore_errors=True)
            return rec
        rec["index_detail"] = msg

    # STRATUM (graph-confirmed) — classify EVERY sampled task (the A/B/C/D split is
    # itself a deliverable).
    strat = classify_stratum(issue, graph_db=graph_db, repo_root="")
    rec["stratum"] = strat.label
    rec["stratum_evidence"] = strat.evidence

    gold_info = _extract_gold_and_tests(task)
    rec.update(gold_info)
    loc_gold = [_norm(g) for g in gold_info["localization_gold"]]

    if strat.label != "B":
        rec["status"] = "not_B"
        _cleanup_repo(repo_dir, args)
        return rec
    if not loc_gold:
        rec["status"] = "B_no_source_gold"
        _cleanup_repo(repo_dir, args)
        return rec

    leak_terms = _leak_terms(gold_info)
    lanes = ["graphdb", "repo"] if args.lane == "both" else [args.lane]
    rec["lanes"] = {}
    try:
        for lane in lanes:
            rr = repo_dir if lane == "repo" else ""
            rec["lanes"][lane] = _measure_lane(issue, graph_db, rr, loc_gold, leak_terms, lane)
        # DETERMINISM: OFF twice + ON twice identical (graphdb lane) on this B task.
        det = _determinism_check(issue, graph_db)
        rec["determinism_ok"] = det["ok"]
        rec["determinism_detail"] = det
        rec["status"] = "measured_B"
        rec["leak_count"] = 0
    except LeakError as e:
        print(f"  !!! {e}", file=sys.stderr)
        rec["status"] = "LEAK_ABORT"
        rec["leak_count"] = 1
        rec["detail"] = str(e)
    _cleanup_repo(repo_dir, args)
    return rec


def _cleanup_repo(repo_dir: str, args) -> None:
    if not args.keep_clones:
        shutil.rmtree(repo_dir, ignore_errors=True)


def _determinism_check(issue: str, graph_db: str) -> dict:
    off1 = _localize_ranked(issue, graph_db, "", content_leg=False)["files"]
    off2 = _localize_ranked(issue, graph_db, "", content_leg=False)["files"]
    on1 = _localize_ranked(issue, graph_db, "", content_leg=True)["files"]
    on2 = _localize_ranked(issue, graph_db, "", content_leg=True)["files"]
    return {
        "off_identical": off1 == off2,
        "on_identical": on1 == on2,
        "ok": off1 == off2 and on1 == on2,
        "off_n": len(off1),
        "on_n": len(on1),
    }


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def _rate(bools: list[bool]) -> float | None:
    xs = [b for b in bools if b is not None]
    return round(sum(1 for b in xs if b) / len(xs), 8) if xs else None


def _lane_cell(b_recs: list[dict], lane: str) -> dict | None:
    arms = [r["lanes"][lane] for r in b_recs if lane in r.get("lanes", {})]
    if not arms:
        return None

    def _side(side: str) -> dict:
        ss = [a[side] for a in arms]
        ranks = [s["gold_best_rank"] for s in ss if s["gold_best_rank"] is not None]
        return {
            "n": len(ss),
            "hit_at_1": _rate([s["hit_at_1"] for s in ss]),
            "hit_at_3": _rate([s["hit_at_3"] for s in ss]),
            "hit_at_5": _rate([s["hit_at_5"] for s in ss]),
            "gold_found_rate": round(len(ranks) / len(ss), 8) if ss else None,
            "mean_gold_rank": round(statistics.mean(ranks), 8) if ranks else None,
            "median_gold_rank": round(statistics.median(ranks), 8) if ranks else None,
            "content_leg_reached_rate": _rate([s["content_leg_reached"] for s in ss]),
        }

    off, on = _side("off"), _side("on")

    def _d(a, b):
        return round(b - a, 8) if (a is not None and b is not None) else None

    return {
        "off": off,
        "on": on,
        "delta_hit_at_1": _d(off["hit_at_1"], on["hit_at_1"]),
        "delta_hit_at_3": _d(off["hit_at_3"], on["hit_at_3"]),
        "delta_hit_at_5": _d(off["hit_at_5"], on["hit_at_5"]),
        "delta_mean_gold_rank": _d(off["mean_gold_rank"], on["mean_gold_rank"]),
    }


def aggregate(records: list[dict]) -> dict:
    dist = {"A": 0, "B": 0, "C": 0, "D": 0, "unclassified": 0}
    for r in records:
        s = r.get("stratum")
        dist[s if s in dist else "unclassified"] += 1

    b_recs = [r for r in records if r.get("status") == "measured_B"]
    lanes_present = sorted({lane for r in b_recs for lane in r.get("lanes", {})})
    by_lane = {}
    for lane in lanes_present:
        cell = _lane_cell(b_recs, lane)
        if cell:
            by_lane[lane] = cell

    status_counts: dict[str, int] = {}
    for r in records:
        status_counts[r.get("status", "?")] = status_counts.get(r.get("status", "?"), 0) + 1

    return {
        "n_tasks": len(records),
        "abcd_distribution": dist,
        "n_B_measured": len(b_recs),
        "status_counts": status_counts,
        "leak_total": sum(r.get("leak_count", 0) for r in records),
        "determinism_ok_rate": _rate([r.get("determinism_ok") for r in b_recs]),
        "by_lane": by_lane,
    }


def _print_summary(agg: dict) -> None:
    print("\n" + "=" * 72)
    print("CONTENT-LEG STRATUM-B MEASUREMENT — SUMMARY (8-dp)")
    print("=" * 72)
    print(f"tasks={agg['n_tasks']}  B_measured={agg['n_B_measured']}  "
          f"leak_total={agg['leak_total']}  determinism_ok={agg['determinism_ok_rate']}")
    print(f"A/B/C/D distribution: {agg['abcd_distribution']}")
    print(f"status: {agg['status_counts']}")
    for lane, cell in agg["by_lane"].items():
        print(f"\n--- lane={lane} ---")
        hdr = f"  {'metric':<22}{'OFF':>16}{'ON':>16}{'delta':>16}"
        print(hdr)
        for m in ("hit_at_1", "hit_at_3", "hit_at_5", "mean_gold_rank"):
            off = cell["off"].get(m)
            on = cell["on"].get(m)
            d = cell.get("delta_" + m)
            print(f"  {m:<22}{str(off):>16}{str(on):>16}{str(d):>16}")
        print(f"  {'gold_found_rate':<22}{str(cell['off']['gold_found_rate']):>16}"
              f"{str(cell['on']['gold_found_rate']):>16}")
        print(f"  {'content_leg_reached':<22}{str(cell['off']['content_leg_reached_rate']):>16}"
              f"{str(cell['on']['content_leg_reached_rate']):>16}")


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------
def _find_smoke_graph(args) -> str | None:
    if args.smoke_graph and os.path.isfile(args.smoke_graph):
        return args.smoke_graph
    cands = sorted(glob.glob(os.path.join(args.witness_dir, "deepswe-full-*", "graph.db")))
    for g in cands:
        try:
            c = sqlite3.connect(g)
            has = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'"
            ).fetchone()
            n = c.execute("SELECT COUNT(*) FROM symbol_content_fts").fetchone()[0] if has else 0
            c.close()
            if has and n > 0:
                return g
        except sqlite3.Error:
            continue
    return cands[0] if cands else None


def run_smoke(args) -> int:
    """Local, network-free proof the harness RUNS, toggles the flag, reaches the
    content leg, is deterministic, and leaks nothing — on a baked witness graph."""
    graph = _find_smoke_graph(args)
    print(f"[SMOKE] witness graph: {graph}")
    if not graph or not os.path.isfile(graph):
        print("[SMOKE][FAIL] no witness graph with symbol_content_fts found. "
              "Pass --smoke-graph <graph.db>.")
        return 2

    # A behaviour-described issue with NO greppable/resolving symbol (true stratum B).
    issue = (
        "The deferred module cache flags are not reset when statements are "
        "evaluated; string literal handling drops tokens during install."
    )

    # 0) direct proof the FTS table + query work.
    conn = sqlite3.connect(graph)
    terms = gl._issue_terms(issue)
    direct = _ORIG_CFTS(conn, terms, limit=20)
    conn.close()
    print(f"[SMOKE] _content_fts_candidates direct rows = {len(direct)} "
          f"(issue terms n={len(terms)})")

    # 1) runs + toggles.
    off1 = _localize_ranked(issue, graph, "", content_leg=False)
    off2 = _localize_ranked(issue, graph, "", content_leg=False)
    on1 = _localize_ranked(issue, graph, "", content_leg=True)
    on2 = _localize_ranked(issue, graph, "", content_leg=True)
    print(f"[SMOKE] OFF gate={off1['gate']} ncand={len(off1['files'])}  |  "
          f"ON gate={on1['gate']} ncand={len(on1['files'])}")
    print(f"[SMOKE] ON top5={on1['files'][:5]}")

    # 2) determinism.
    off_det = off1["files"] == off2["files"]
    on_det = on1["files"] == on2["files"]
    print(f"[SMOKE] determinism: OFF identical={off_det}  ON identical={on_det}")

    # 3) content leg reached on ON (counter + stderr banner + signal provenance).
    reached = bool(on1["content_fts_calls"]) and on1["stderr_content_leg"]
    print(f"[SMOKE] content leg reached ON: counter_calls={on1['content_fts_calls']} "
          f"rows={on1['content_fts_rows']} stderr_banner={on1['stderr_content_leg']} "
          f"signals_has_content={on1['signals_has_content']}")
    off_reached = bool(off1["content_fts_calls"])
    print(f"[SMOKE] content leg reached OFF (must be False): {off_reached}")

    # 4) leak-free. Synthetic verifier terms that MUST NOT surface, plus a token we
    #    know is NOT in the rendered set.
    leak_terms = {"test_should_never_leak", "assert_secret_marker_xyz"}
    leaks = _leak_scan(_rendered_text(on1), leak_terms) + _leak_scan(_rendered_text(off1), leak_terms)
    print(f"[SMOKE] leak scan (synthetic verifier terms) hits = {leaks}")

    # assertions
    ok = True
    checks = [
        ("harness ran (ON produced candidates)", len(on1["files"]) > 0),
        ("OFF deterministic", off_det),
        ("ON deterministic", on_det),
        ("content leg reached on ON", reached),
        ("content leg NOT reached on OFF", not off_reached),
        ("signals provenance shows 'content' on ON", on1["signals_has_content"]),
        ("OFF is empty (grep-dead stratum-B baseline)", len(off1["files"]) == 0),
        ("no leak", not leaks),
        ("direct FTS returns rows", len(direct) > 0),
    ]
    print("\n[SMOKE] CHECKS:")
    for name, val in checks:
        mark = "PASS" if val else "FAIL"
        if not val:
            ok = False
        print(f"   [{mark}] {name}")
    print(f"\n[SMOKE] {'ALL PASS' if ok else 'FAILED'}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=DEFAULT_CORPUS,
                    help="Live-Lite jsonl (default: benchmarks/data/swebench_live_lite.jsonl)")
    ap.add_argument("--sample", type=int, default=0,
                    help="measure the first N corpus tasks (0 = all)")
    ap.add_argument("--ids", help="file of instance_ids (one per line) to restrict to")
    ap.add_argument("--workdir", default=None, help="scratch dir for clones + graphs")
    ap.add_argument("--out", help="write the aggregate + per-task JSON here")
    ap.add_argument("--gt-index", dest="gt_index",
                    help="path to the gt-index binary (built with -tags sqlite_fts5)")
    ap.add_argument("--lane", choices=["graphdb", "repo", "both"], default="graphdb",
                    help="graphdb=repo-less (the leg's designed lane, headline); "
                         "repo=grep-active diagnostic; both=run both (default graphdb)")
    ap.add_argument("--keep-clones", action="store_true",
                    help="keep source checkouts after indexing (default: delete, keep graph.db)")
    ap.add_argument("--max-clone-seconds", type=int, default=600)
    ap.add_argument("--max-index-seconds", type=int, default=1800)
    ap.add_argument("--smoke", action="store_true",
                    help="local network-free smoke on a baked witness graph")
    ap.add_argument("--smoke-graph", help="explicit witness graph.db for --smoke")
    ap.add_argument("--witness-dir", default=DEFAULT_WITNESS_DIR,
                    help="dir holding deepswe-full-*/graph.db witness graphs")
    args = ap.parse_args()

    if args.smoke:
        return run_smoke(args)

    if not args.gt_index or not os.path.isfile(args.gt_index):
        ap.error("--gt-index <path to gt-index binary> is required for the full run "
                 "(build: cd gt-index && CGO_ENABLED=1 go build -tags sqlite_fts5 "
                 "-o gt-index ./cmd/gt-index/)")

    workdir = args.workdir or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"gt_content_leg_{int(time.time())}"
    )
    os.makedirs(workdir, exist_ok=True)
    print(f"[RUN] workdir={workdir}  lane={args.lane}  gt_index={args.gt_index}")

    tasks = _load_corpus(args.corpus)
    if args.ids:
        want = {l.strip() for l in open(args.ids, encoding="utf-8") if l.strip()}
        tasks = [t for t in tasks if (t.get("instance_id") or t.get("id")) in want]
    if args.sample and args.sample > 0:
        tasks = tasks[: args.sample]
    print(f"[RUN] {len(tasks)} tasks to process")

    records = []
    for i, t in enumerate(tasks, 1):
        iid = t.get("instance_id") or t.get("id") or "unknown"
        t0 = time.time()
        try:
            rec = process_task(t, args, workdir)
        except Exception as e:  # noqa: BLE001 — one bad task must not kill the run
            rec = {"instance_id": iid, "status": "EXC", "detail": repr(e)}
        rec["seconds"] = round(time.time() - t0, 2)
        records.append(rec)
        print(f"  [{i}/{len(tasks)}] {iid:<40} status={rec.get('status'):<16} "
              f"stratum={rec.get('stratum','?')} ({rec['seconds']}s)")

    agg = aggregate(records)
    out = {"env": {
        "gt_index": args.gt_index, "lane": args.lane, "corpus": args.corpus,
        "n_requested": len(tasks), "workdir": workdir,
    }, "aggregate": agg, "tasks": records}

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"\n[WROTE] {args.out}")
    _print_summary(agg)

    if agg["leak_total"] > 0:
        print(f"\n[INVALID] leak_total={agg['leak_total']} — aggregate is NOT trustworthy.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

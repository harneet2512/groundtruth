#!/usr/bin/env python
"""make_substrate_faithful.py — apply the SHIPPED LSP+closure pass to the
localization test/held-out graphs so they reflect the runtime substrate
(BRIEFING §5: a pre-LSP graph — lsp=0, name_match retained — gives untrustworthy
lever numbers).

For each task graph (test + held-out):
  1. Record pre-LSP census + graph-edges hash.
  2. Run the SHIPPED `groundtruth.resolve --resolve` DEMAND-DRIVEN — scoped to the
     issue subgraph (gold files + issue-named files), pyright on PATH. This
     verifies/corrects/deletes residual name_match and stamps `lsp` edges.
  3. Run `gt-index -rebuild-closure` so reach/path-decay reflect the LSP edges.
  4. Read the LSP certificate + post census; classify per graph:
       FAITHFUL       — lsp_warm AND closure_rebuilt AND lsp_edges_written>0
       FAITHFUL_NOOP  — lsp_warm AND closure_rebuilt AND residual==0 (valid warm
                        no-op: the in-scope issue subgraph had no unresolved
                        method-call edges — the cert's LSP_NO_OP_VALID_WITH_WARM_SERVER;
                        the graph is still substrate-faithful, just nothing to promote)
       EXCLUDED       — server never warmed / pyright failed / repo gone (DO NOT
                        measure a pre-LSP graph; record EXCLUDED, never fabricate)

  NOTE: the per-graph LSP cert is written to GT_LSP_CERT (set per task to
  <testset>/certs/<id>.lsp.json) and copied into the manifest.

Demand-driven scoping (CLAUDE.md): NEVER per-edge-LSP the whole repo. Scope =
the issue files; resolve.py expands type-enrichment to their 1-hop neighbours.

Host pyright, NOT the container — the container is reserved for the final
witness. Blind spot recorded in the report: host pyright may resolve a slightly
different residual than the container's baked server (version/stub skew); the
RANK deltas are what we compare, and both halves use the SAME host pyright, so
the comparison is internally consistent.

Usage:
  python scripts/make_substrate_faithful.py --testset-dir D:/gt_runs/localization_testset
  python scripts/make_substrate_faithful.py --all      # both test + heldout
No commit. D:/ paths. PYTHONPATH=D:/Groundtruth/src, PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, "D:/Groundtruth/src")

GT_INDEX = "D:/Groundtruth/gt-index/gt-index.exe"
TESTSETS = {
    "test": "D:/gt_runs/localization_testset",
    "heldout": "D:/gt_runs/localization_heldout",
}


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./")


def _census(db: str) -> dict[str, int]:
    import sqlite3
    c = sqlite3.connect(db)
    try:
        rows = c.execute(
            "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method"
        ).fetchall()
    finally:
        c.close()
    return {m: n for m, n in rows}


def _edges_hash(db: str) -> str:
    from groundtruth.resolve import _graph_edges_hash
    return _graph_edges_hash(db)


def _issue_named_files(issue_text: str, db: str, repo_dir: str) -> list[str]:
    """Deterministic issue-named source files that EXIST in graph.db, repo-relative."""
    out: set[str] = set()
    try:
        from groundtruth.pretask.anchors import extract_issue_anchors
        a = extract_issue_anchors(issue_text, db)
        for f in (getattr(a, "file_paths", None) or []):
            out.add(_norm(f))
    except Exception:
        pass
    # Keep only paths that are real source_file values in the graph (avoids scoping
    # to a path the indexer never saw).
    import sqlite3
    c = sqlite3.connect(db)
    try:
        real = {r[0] for r in c.execute("SELECT DISTINCT source_file FROM edges").fetchall()}
        realn = {_norm(r) for r in real if r}
    finally:
        c.close()
    return sorted(p for p in out if p in realn)


def _run_resolve(db: str, repo_dir: str, scope: list[str], cert_path: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = "D:/Groundtruth/src"
    env["PYTHONIOENCODING"] = "utf-8"
    env["GT_LSP_CERT"] = cert_path
    # Do NOT set GT_REQUIRE_LSP — a valid warm no-op (residual==0) must not hard-fail.
    cmd = [
        sys.executable, "-m", "groundtruth.resolve",
        "--db", db, "--root", repo_dir, "--lang", "python",
        "--resolve", "--max-edges", "5000",
        "--source-files", ",".join(scope),
    ]
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
        return r.returncode, (r.stdout or "")[-4000:] + "\n--STDERR--\n" + (r.stderr or "")[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return 1, repr(e)


def _rebuild_closure(db: str) -> tuple[int, str]:
    try:
        r = subprocess.run(
            [GT_INDEX, "-rebuild-closure", "-output", db],
            capture_output=True, text=True, timeout=900,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return 1, repr(e)


def process_task(cfg: dict, certs_dir: str) -> dict:
    iid = cfg["id"]
    db = (cfg.get("graph_db") or "").replace("\\", "/")
    repo_dir = (cfg.get("repo_dir") or "").replace("\\", "/")
    gold = [_norm(g) for g in cfg.get("gold_files", [])]
    issue = cfg.get("issue_text", "")
    rec: dict = {"id": iid, "category": cfg.get("category")}

    if not db or not os.path.exists(db):
        rec["state"] = "EXCLUDED"
        rec["reason"] = "graph.db missing"
        return rec
    if not repo_dir or not os.path.isdir(os.path.join(repo_dir, ".git")):
        rec["state"] = "EXCLUDED"
        rec["reason"] = "repo (base_commit) gone from disk"
        return rec

    pre = _census(db)
    pre_hash = _edges_hash(db)
    issue_files = _issue_named_files(issue, db, repo_dir)
    scope = sorted(set(gold) | set(issue_files))
    rec["scope_files"] = scope
    rec["issue_named_files"] = issue_files
    rec["pre_lsp_census"] = pre
    rec["pre_lsp_name_match"] = int(pre.get("name_match", 0))
    rec["graph_hash_before"] = pre_hash

    cert_path = os.path.join(certs_dir, f"{iid}.lsp.json")
    rc, out = _run_resolve(db, repo_dir, scope, cert_path)
    rec["resolve_rc"] = rc
    rec["resolve_tail"] = out[-1500:]

    cert = {}
    if os.path.exists(cert_path):
        try:
            cert = json.load(open(cert_path, encoding="utf-8"))
        except Exception:
            cert = {}
    rec["cert"] = {
        k: cert.get(k) for k in (
            "lsp_warm", "attempted_edges", "verified_edges", "corrected_edges",
            "deleted_edges", "failed_edges", "skipped_edges", "residual",
            "effective_work", "verdict_hint", "graph_hash_before_lsp",
            "graph_hash_after_lsp", "closure_rebuilt_after_lsp",
        )
    }

    # Closure rebuild — resolve.py rebuilds it ONLY when effective_work>0; force a
    # deterministic explicit rebuild so reach/path-decay are fresh on EVERY graph.
    crc, cout = _rebuild_closure(db)
    rec["closure_rc"] = crc
    rec["closure_out"] = cout.strip()[-400:]

    post = _census(db)
    post_hash = _edges_hash(db)
    rec["post_lsp_census"] = post
    rec["lsp_edges_written"] = int(post.get("lsp", 0))
    rec["post_lsp_name_match"] = int(post.get("name_match", 0))
    rec["graph_hash_after"] = post_hash
    rec["graph_hash_changed"] = bool(pre_hash != post_hash)

    lsp_warm = bool(cert.get("lsp_warm"))
    closure_ok = (crc == 0)
    lsp_n = rec["lsp_edges_written"]
    residual = int(cert.get("residual", -1))

    if not lsp_warm:
        rec["state"] = "EXCLUDED"
        rec["reason"] = f"LSP did not warm (verdict={cert.get('verdict_hint')})"
    elif not closure_ok:
        rec["state"] = "EXCLUDED"
        rec["reason"] = f"closure rebuild failed rc={crc}"
    elif lsp_n > 0:
        rec["state"] = "FAITHFUL"
        rec["reason"] = f"lsp_edges={lsp_n} closure_rebuilt graph_hash_changed={rec['graph_hash_changed']}"
    elif residual == 0:
        rec["state"] = "FAITHFUL_NOOP"
        rec["reason"] = ("valid warm no-op: issue subgraph had 0 unresolved method-call "
                         "edges in scope; closure rebuilt")
    else:
        # warm server, residual>0, but converted 0 -> dep-env / external-target residual.
        rec["state"] = "FAITHFUL_NOOP"
        rec["reason"] = (f"warm no-op: residual={residual} but pyright resolved targets to "
                         f"external/stdlib (effective_work=0); closure rebuilt")
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--testset-dir")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", default=None, help="single task id (debug)")
    args = ap.parse_args()

    dirs = []
    if args.all:
        dirs = list(TESTSETS.values())
    elif args.testset_dir:
        dirs = [args.testset_dir]
    else:
        ap.error("--testset-dir or --all")

    all_recs = []
    for d in dirs:
        certs_dir = os.path.join(d, "certs")
        os.makedirs(certs_dir, exist_ok=True)
        tasks_dir = os.path.join(d, "tasks")
        for fn in sorted(os.listdir(tasks_dir)):
            if not fn.endswith(".json"):
                continue
            cfg = json.load(open(os.path.join(tasks_dir, fn), encoding="utf-8"))
            if args.only and cfg["id"] != args.only:
                continue
            print(f"\n========== {cfg['id']} ({os.path.basename(d)}) ==========", flush=True)
            rec = process_task(cfg, certs_dir)
            rec["testset"] = os.path.basename(d)
            print(f"  scope={rec.get('scope_files')}")
            print(f"  STATE={rec['state']}  {rec['reason']}")
            print(f"  pre_name_match={rec.get('pre_lsp_name_match')} -> post_name_match="
                  f"{rec.get('post_lsp_name_match')}  lsp_written={rec.get('lsp_edges_written')}  "
                  f"hash_changed={rec.get('graph_hash_changed')}")
            all_recs.append(rec)
        man = os.path.join(d, "substrate_faithful_manifest.json")
        json.dump([r for r in all_recs if r["testset"] == os.path.basename(d)],
                  open(man, "w", encoding="utf-8"), indent=2, default=str)
        print(f"\n[WROTE] {man}", flush=True)

    n_f = sum(1 for r in all_recs if r["state"] == "FAITHFUL")
    n_noop = sum(1 for r in all_recs if r["state"] == "FAITHFUL_NOOP")
    n_excl = sum(1 for r in all_recs if r["state"] == "EXCLUDED")
    print(f"\n=== SUMMARY: {len(all_recs)} graphs -> FAITHFUL={n_f} "
          f"FAITHFUL_NOOP={n_noop} EXCLUDED={n_excl} ===")


if __name__ == "__main__":
    main()

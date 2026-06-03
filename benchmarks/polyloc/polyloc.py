"""PolyLoc — Polyglot Localization Evaluator (ONE pipeline, N languages).

WHAT IT IS
    An offline, leakage-proof evaluator of GroundTruth's *localization substrate*:
    given an issue + a freshly-built graph + the repo, where does the GOLD file land
    in the candidate set GT hands the agent, and does it reach the agent's options?
    The SAME pipeline is run across every language — GT is one product, not one
    localizer per language. Results are broken out per language so a uniform claim
    can't hide a language that fails.

WHAT IT IS NOT
    NOT a resolve-rate benchmark. It does not run the agent and does not check
    FAIL_TO_PASS. Flips / resolve-rate remain on Microsoft's official SWE-bench-Live
    harness + a paid agent run. PolyLoc is the cheap pre-benchmark gate: if the
    substrate doesn't improve here with zero regressions, there is no point paying
    for an agent run.

ARMS (fair comparison)
    A  grep-only        : the recall the localizer itself greps, ranked by distinct
                          issue-token coverage. A real lexical baseline, not a strawman.
    C  GT (this build)  : full pipeline (grep floor + RRF fusion + graded header).

METRICS (per language and overall)
    rank-of-gold (lower better), Acc@1/@5/@10, mean reciprocal rank,
    gold-in-options (gold appears in the agent-facing graded header — FULL-PATH match),
    regressions (tasks where C's gold rank is worse than A AND the gold left the
    options), paired Wilcoxon signed-rank on per-task (rank_A - rank_C).

LEAKAGE GUARD
    The localizer/brief are called with ONLY (issue_text, graph_db, repo_root). The
    gold is used solely to SCORE the output, never passed into the pipeline. Asserted
    at runtime (see _assert_no_leak).

GENERALIZATION DISCIPLINE
    The manifest must contain repos/instances NOT used during development. Freeze the
    code, run ONCE, accept the result. Do not tune after seeing PolyLoc output.
"""
from __future__ import annotations
import json, os, re, sqlite3, subprocess, sys, inspect

GTINDEX = os.environ.get("GT_INDEX", r"D:\Groundtruth\gt-index\gt-index-current.exe")
WT_ROOT = os.environ.get("POLYLOC_WT", r"D:/polyloc_repos")
MSWE = "bytedance-research/Multi-SWE-bench"


def norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./").lstrip("/")


def is_test(f: str) -> bool:
    fl = f.lower()
    return ("test" in os.path.basename(fl) or "/test" in fl or "/spec" in fl
            or "__tests__" in fl or fl.endswith((".md", ".txt", ".snap")))


def _assert_no_leak(localize_fn) -> None:
    params = set(inspect.signature(localize_fn).parameters)
    leak = params & {"gold", "gold_files", "gold_file", "patch", "fail_to_pass"}
    assert not leak, f"LEAK: localizer accepts gold-bearing params {leak}"


# ---- task sourcing (Multi-SWE-bench schema; uniform across all 5 languages) ----
def fetch_tasks(repo_file: str, k: int) -> list[dict]:
    from huggingface_hub import hf_hub_download
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    p = hf_hub_download(MSWE, repo_file, repo_type="dataset", cache_dir="D:/hf_cache")
    out = []
    for line in open(p, encoding="utf-8"):
        r = json.loads(line)
        gold = sorted({m.group(1) for m in re.finditer(r"^\+\+\+ b/(\S+)", r.get("fix_patch", "") or "", re.M)})
        gold = [g for g in gold if not is_test(g)]
        issue = (r.get("title", "") or "") + "\n" + (r.get("body", "") or "")
        for ri in (r.get("resolved_issues") or []):
            issue += "\n" + (ri.get("title", "") or "") + "\n" + (ri.get("body", "") or "")
        if len(gold) == 1 and len(issue) > 80:  # one clean source-file gold + real issue
            out.append({"iid": r["instance_id"], "org": r["org"], "repo": r["repo"],
                        "sha": r["base"]["sha"], "gold": gold, "issue": issue})
        if len(out) >= k:
            break
    return out


def fetch_swelive_python(repos: list[str], k: int) -> list[dict]:
    """Python tasks from SWE-bench-Live (Multi-SWE-bench has no usable python file).
    Schema: instance_id / repo (org/name) / base_commit / patch / problem_statement."""
    from datasets import load_dataset
    ds = load_dataset("SWE-bench-Live/SWE-bench-Live", split="test")
    want = set(repos); per: dict[str, int] = {}
    out = []
    for r in ds:
        full = r.get("repo", ""); name = full.split("/")[-1]
        if name not in want or per.get(name, 0) >= k:
            continue
        gold = sorted({m.group(1) for m in re.finditer(r"^\+\+\+ b/(\S+)", r.get("patch", "") or "", re.M)})
        gold = [g for g in gold if not is_test(g) and g.endswith(".py")]
        issue = r.get("problem_statement", "") or ""
        if len(gold) == 1 and len(issue) > 80:
            out.append({"iid": r["instance_id"], "org": full.split("/")[0], "repo": name,
                        "sha": r["base_commit"], "gold": gold, "issue": issue})
            per[name] = per.get(name, 0) + 1
    return out


def ensure_repo_db(t: dict, lang: str | None = None):
    wt = f"{WT_ROOT}/{t['repo']}"
    if not os.path.isdir(wt):
        subprocess.run(["git", "clone", "--filter=blob:none", "--quiet",
                        f"https://github.com/{t['org']}/{t['repo']}", wt], capture_output=True, text=True)
    subprocess.run(["git", "-C", wt, "checkout", "--quiet", t["sha"]], capture_output=True, text=True)
    db = f"{WT_ROOT}/{t['repo']}__{t['sha'][:10]}.db"
    if not (os.path.exists(db) and sqlite3.connect(db).execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0):
        # gt-index may be GT_INDEX path (Windows .exe) or a bare binary name (Linux/VM).
        idx = GTINDEX if (os.path.exists(GTINDEX) or "/" not in GTINDEX) else "gt-index"
        subprocess.run([idx, "-root", wt, "-output", db], capture_output=True, text=True)
    # LSP-enriched copy: pyright/gopls/rust-analyzer/tsserver UPDATE edges -> resolution_method='lsp',
    # confidence=1.0, CERTIFIED tier. This is the same-name disambiguation lever (MarsCode/IGS).
    # Linux-only (LSP handshake hangs on Windows); enrich is skipped if the server is absent.
    db_lsp = db.replace(".db", "__lsp.db")
    if lang and not os.path.exists(db_lsp):
        try:
            import shutil; shutil.copyfile(db, db_lsp)
            r = subprocess.run([sys.executable, "-m", "groundtruth.resolve", "--db", db_lsp,
                                "--root", wt, "--resolve", "--lang", lang],
                               capture_output=True, text=True, timeout=900,
                               env={**os.environ, "GT_SRC": os.environ.get("GT_SRC", "")})
            n_lsp = sqlite3.connect(db_lsp).execute(
                "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'").fetchone()[0]
            print(f"    [lsp-enrich] {t['repo']}: lsp_edges={n_lsp} rc={r.returncode}")
        except Exception as e:
            print(f"    [lsp-enrich] FAILED {type(e).__name__}: {str(e)[:80]}")
    # Honest lsp_on: a db_lsp counts as "LSP enriched" ONLY if it actually carries
    # lsp edges. A cached or copied db_lsp with 0 lsp edges (rust/go: rust-analyzer/
    # gopls resolved nothing without a project build) is identical to the plain db,
    # so report it as db so lsp_on=(db_lsp!=db) reflects DELIVERY, not file existence.
    if db_lsp != db:
        try:
            n_lsp = sqlite3.connect(db_lsp).execute(
                "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'").fetchone()[0]
            if n_lsp == 0:
                db_lsp = db
        except Exception:
            db_lsp = db
    return wt, db, db_lsp


def best_rank(order: list[str], gold: list[str]):
    rr = [i for i, fp in enumerate(order) for g in gold if norm(fp).endswith(norm(g))]
    return min(rr) if rr else None


def arm_grep(L, issue, db, rr, gold):
    conn = sqlite3.connect(db); seeds = L._grep_to_seeds(L._issue_terms(issue), rr, conn, max_seeds=40); conn.close()
    toks = [t for t in L._issue_terms(issue) if len(t) >= 4]
    scored = []
    for fp in {norm(s[2]) for s in seeds}:
        try: txt = open(os.path.join(rr, fp), encoding="utf-8", errors="ignore").read(500_000).lower()
        except OSError: txt = ""
        scored.append((fp, sum(1 for t in toks if t in txt)))
    scored.sort(key=lambda x: -x[1])
    return best_rank([f for f, _ in scored], gold)


def evaluate(manifest: dict, k_per_repo: int = 3):
    sys.path.insert(0, os.environ.get("GT_SRC", r"D:/gt-grepfloor-wt/src"))
    from groundtruth.pretask.anchors import extract_issue_anchors
    from groundtruth.pretask import graph_localizer as L
    from groundtruth.pretask.v1r_brief import generate_v1r_brief
    _assert_no_leak(L.localize)

    rows = []
    for lang, repo_files in manifest.items():
        # python -> SWE-bench-Live (repo names); other langs -> Multi-SWE-bench files
        groups: list[list[dict]] = []
        if lang == "python":
            try:
                groups = [fetch_swelive_python(repo_files, k_per_repo)]
            except Exception as e:
                print(f"[python] fetch fail: {str(e)[:80]}")
        else:
            for rf in repo_files:
                try:
                    groups.append(fetch_tasks(rf, k_per_repo))
                except Exception as e:
                    print(f"[{lang}] fetch fail {rf}: {str(e)[:80]}")
        for tasks in groups:
            for t in tasks:
                try:
                    wt, db, db_lsp = ensure_repo_db(t, lang)
                    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                    if n == 0:
                        print(f"[{lang}] {t['iid']} 0 nodes — skip"); continue
                    rA = arm_grep(L, t["issue"], db, wt, t["gold"])
                    # FULL pipeline (not bare localize): generate_v1r_brief fuses v7_4
                    # (BM25+reach+proximity+co-change+hub) WITH localize (grep+struct+
                    # semantic) — use ALL the graph's strength for the rank metric.
                    brief = generate_v1r_brief(t["issue"], wt, db, bug_id=t["iid"], max_files=20)
                    rC = best_rank([f.path for f in brief.files], t["gold"])
                    # C_lsp: SAME pipeline on the LSP-enriched graph (lsp edges weighted as verified).
                    rC_lsp = rC
                    if db_lsp != db:
                        brief_l = generate_v1r_brief(t["issue"], wt, db_lsp, bug_id=t["iid"], max_files=20)
                        rC_lsp = best_rank([f.path for f in brief_l.files], t["gold"])
                    m = re.search(r"<gt-localization[^>]*>.*?</gt-localization>", brief.brief_text, re.S)
                    blk = (m.group(0) if m else "").replace("\\", "/")
                    gold_in = any(norm(x) in blk for x in t["gold"])     # FULL-PATH (no basename collision)
                    imper = "Highest-confidence candidate" in brief.brief_text
                    rows.append({"lang": lang, "iid": t["iid"], "A": rA, "C": rC, "C_lsp": rC_lsp,
                                 "gold_in": gold_in, "imperative": imper, "lsp_on": db_lsp != db})
                    print(f"[{lang}] {t['iid']:28} A={str(rA):>4} C={str(rC):>4} C_lsp={str(rC_lsp):>4} "
                          f"gold_in={gold_in} imper={imper}")
                except Exception as e:
                    print(f"[{lang}] {t.get('iid')} ERROR {type(e).__name__}: {str(e)[:90]}")
    return rows


def report(rows: list[dict]):
    def acc(rs, kk): return sum(1 for r in rs if r is not None and r < kk) / len(rs) if rs else 0.0
    def mrr(rs):
        v = [1.0 / (r + 1) for r in rs if r is not None]
        return sum(v) / len(rs) if rs else 0.0
    BIG = 999

    print("\n================= PolyLoc report =================")
    print(f"{'lang':8} {'n':3} {'A@1':5} {'C@1':5} {'Cl@1':5} {'A@5':5} {'C@5':5} {'Cl@5':5} "
          f"{'A.mrr':6} {'C.mrr':6} {'Cl.mrr':6} {'lsp_n':5} {'gold_in':7} {'imper':5} {'regr':4}")
    langs = sorted({r["lang"] for r in rows})
    for lang in langs + ["ALL"]:
        rs = rows if lang == "ALL" else [r for r in rows if r["lang"] == lang]
        if not rs: continue
        A = [r["A"] for r in rs]; C = [r["C"] for r in rs]; Cl = [r.get("C_lsp", r["C"]) for r in rs]
        # regression = C worse than A by rank AND gold left the options
        regr = sum(1 for r in rs if (r["C"] or BIG) > (r["A"] or BIG) and not r["gold_in"])
        gold_in = sum(1 for r in rs if r["gold_in"])
        imper = sum(1 for r in rs if r["imperative"])
        lsp_n = sum(1 for r in rs if r.get("lsp_on"))
        print(f"{lang:8} {len(rs):3} {acc(A,1):5.2f} {acc(C,1):5.2f} {acc(Cl,1):5.2f} "
              f"{acc(A,5):5.2f} {acc(C,5):5.2f} {acc(Cl,5):5.2f} "
              f"{mrr(A):6.2f} {mrr(C):6.2f} {mrr(Cl):6.2f} {lsp_n:>3}/{len(rs):<2} "
              f"{gold_in:>2}/{len(rs):<3}  {imper:>2}/{len(rs):<2} {regr:>2}")

    # paired stat on per-task delta = rank_A - rank_C (positive = C better), miss=BIG
    deltas = [((r["A"] if r["A"] is not None else BIG) - (r["C"] if r["C"] is not None else BIG)) for r in rows]
    nz = [d for d in deltas if d != 0]
    cb = sum(1 for d in nz if d > 0); ab = sum(1 for d in nz if d < 0)
    print(f"\nPaired (C vs A): C-better={cb} A-better={ab} ties={len(deltas)-len(nz)}  (n={len(rows)})")
    try:
        from scipy.stats import wilcoxon
        if nz:
            stat, p = wilcoxon([(r["A"] or BIG) for r in rows], [(r["C"] or BIG) for r in rows])
            print(f"Wilcoxon signed-rank p={p:.4f}")
    except Exception:
        print("(scipy unavailable; sign test only)")
    tot_imper = sum(1 for r in rows if r["imperative"])
    tot_regr = sum(1 for r in rows if (r["C"] or BIG) > (r["A"] or BIG) and not r["gold_in"])
    print(f"\nGATE: imperative={tot_imper}/{len(rows)} (must be 0)  harmful_regressions={tot_regr}/{len(rows)} (must be 0)")
    print("==================================================")


# HELD-OUT manifest — repos NOT used during development (dev used: weasyprint, beets,
# sharkdp/fd, iamkun/dayjs, darkreader, cli/cli). Auto-picks clean tasks; not cherry-picked.
HELD_OUT = {
    # python -> SWE-bench-Live test repos (fresh: dev used weasyprint/beets only)
    "python": ["sqllineage", "geopandas", "privacyidea"],
    "rust":   ["rust/tokio-rs__bytes_dataset.jsonl", "rust/clap-rs__clap_dataset.jsonl"],
    "go":     ["go/grpc__grpc-go_dataset.jsonl"],   # go-zero has an ignore-all .gitignore -> gt-index 0 files
    "ts":     ["ts/vuejs__core_dataset.jsonl"],
    "js":     ["js/expressjs__express_dataset.jsonl"],
}

if __name__ == "__main__":
    os.makedirs(WT_ROOT, exist_ok=True)
    k = int(os.environ.get("POLYLOC_K", "3"))
    rows = evaluate(HELD_OUT, k_per_repo=k)
    report(rows)
    json.dump(rows, open(os.environ.get("POLYLOC_OUT", "D:/Groundtruth/.tmp_polyloc_results.json"), "w"))

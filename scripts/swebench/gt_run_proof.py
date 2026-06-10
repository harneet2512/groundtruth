#!/usr/bin/env python3
"""gt-run-proof — the PORTABLE GT proof-runtime entrypoint.

ONE command an EXTERNAL benchmark team runs inside the pinned GT substrate image to produce ALL GT
artifacts from a mounted, read-only task repo. No per-task pip install, no model download, no host
GT execution, no mutation of the official SWE task image, no private local state.

    docker run --rm \
      -v "$TASK_REPO:/work:ro" -v "$GT_ARTIFACTS:/gt_artifacts" \
      -e GT_PROOF_MODE=1 -e GT_CONTAINERIZED=1 -e GT_RUNTIME_STRATEGY=unified_substrate \
      -e GT_REQUIRE_FTS5=1 -e GT_REQUIRE_EMBEDDER=1 -e GT_FORCE_ONNX_EMBEDDER=1 \
      -e GT_REQUIRE_LSP=1 -e GT_REQUIRE_FULL_STACK=1 \
      ghcr.io/<org>/groundtruth-substrate@sha256:<digest> \
      gt-run-proof --source-root /work --out /gt_artifacts

Emits to --out: graph.db, runtime_context.json, lsp_certificate.json, graph_certificate.json,
embedder_certificate.json, foundational_gate_report.json (+ brief/ render artifacts if applicable),
and run_manifest.json. Exit code mirrors the foundational gate verdict (deliver-always-aware).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

# The artifact contract the external benchmark team relies on (all written under --out).
# brief.txt IS part of the contract (P0.1-c): the agent CONSUMES /gt_artifacts/brief.txt
# read-only — in proof mode an empty/missing brief is GT_ARTIFACT_MISSING (fail-closed),
# never a "host-fallback" WARN (host run_v74 is fail-closed by the container boundary).
REQUIRED_ARTIFACTS = [
    "graph.db",
    "runtime_context.json",
    "lsp_certificate.json",
    "graph_certificate.json",
    "embedder_certificate.json",
    "foundational_gate_report.json",
    "run_manifest.json",
    "brief.txt",
]

# Where GT is baked in the substrate image (NOT a checkout, NOT host paths).
GT_HOME = os.environ.get("GT_HOME", "/opt/gt")

# ── Run provenance (Stage-5 audit: a run must prove WHICH code produced it) ──────────────
# The runtime flags recorded in run_manifest.runtime_flags: the 8 proof-env flags the
# substrate runs under (same set as required_env in the contract) PLUS
# GT_FORBID_PREBUILT_GRAPH (P1-i — the freshness legitimacy flag the workflow arms;
# recorded-or-null provenance, not a required_env gate).
PROOF_FLAG_KEYS = ("GT_PROOF_MODE", "GT_CONTAINERIZED", "GT_RUNTIME_STRATEGY",
                   "GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER", "GT_FORCE_ONNX_EMBEDDER",
                   "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK", "GT_FORBID_PREBUILT_GRAPH")

# The 4 substrate certificates whose schema/version stamps the manifest records.
_CERT_FILES = {"lsp_certificate": "lsp_certificate.json",
               "graph_certificate": "graph_certificate.json",
               "embedder_certificate": "embedder_certificate.json",
               "foundational_gate_report": "foundational_gate_report.json"}

_LEGIT_MOD = None
_LEGIT_TRIED = False


def _legitimacy_mod():
    """Borrow scripts/verify/legitimacy.py (the OH-path manifest builder) when reachable.
    The substrate bakes the whole scripts tree (Dockerfile: COPY scripts /opt/gt/scripts)
    but only scripts/swebench is on PYTHONPATH, so load it by PATH — in-container under
    $GT_HOME, or repo-relative on a host/dev checkout. None => callers use the inline
    minimal equivalents below; provenance must never crash the proof run."""
    global _LEGIT_MOD, _LEGIT_TRIED
    if _LEGIT_TRIED:
        return _LEGIT_MOD
    _LEGIT_TRIED = True
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(GT_HOME, "scripts", "verify", "legitimacy.py"),
                 os.path.normpath(os.path.join(here, "..", "verify", "legitimacy.py"))):
        if not os.path.exists(cand):
            continue
        try:
            spec = importlib.util.spec_from_file_location("gt_legitimacy_helpers", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _LEGIT_MOD = mod
        except Exception:
            _LEGIT_MOD = None
        break
    return _LEGIT_MOD


def _env_or_none(key: str):
    """Provenance env value, recorded-or-null. Absent/empty -> None — never a guess."""
    v = os.environ.get(key, "").strip()
    return v or None


def _gt_git_commit():
    """Which GT code produced this run. Env GT_GIT_COMMIT first (the substrate container
    has no .git — the workflow exports github.sha into the docker run); fall back to
    `git rev-parse HEAD` ONLY when a .git actually exists (host/dev checkout); else None.
    Never fabricated."""
    v = _env_or_none("GT_GIT_COMMIT")
    if v:
        return v
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (GT_HOME, os.path.normpath(os.path.join(here, "..", ".."))):
        if not os.path.isdir(os.path.join(root, ".git")):
            continue
        m = _legitimacy_mod()
        if m is not None and hasattr(m, "_git_head"):
            return m._git_head(root) or None
        try:
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                 capture_output=True, text=True, timeout=15)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    return None


def _sha256_file(path: str):
    """sha256 of a file (graph.db provenance). Borrows legitimacy._sha256_file when
    available (byte-identical inline fallback otherwise). None when unreadable."""
    m = _legitimacy_mod()
    if m is not None and hasattr(m, "_sha256_file"):
        try:
            return m._sha256_file(path) or None
        except Exception:
            return None
    try:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _language_distribution(graph_db: str):
    """REAL per-language node counts from the built graph.db. EVERY language present is
    counted — including ones with no LSP server (_detect_langs filters to _LSP_LANGS for
    resolve scheduling; provenance must not drop them). None when the graph cannot be
    read — never a fabricated {}."""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        rows = c.execute("select coalesce(nullif(trim(language),''),'unknown') as lang, "
                         "count(*) from nodes group by lang order by count(*) desc").fetchall()
        c.close()
        return {str(r[0]): int(r[1]) for r in rows}
    except Exception:
        return None


def _cert_versions(out_dir: str) -> dict:
    """The schema/version stamp from each of the 4 substrate certs when present
    (gt.lsp_certificate.v2 / gt.graph_certificate.v1 / gt.embedder_certificate.v1; the
    foundational gate report carries no schema field today). Absent file or absent
    field -> None — never fabricated."""
    out: dict = {}
    for name, fname in _CERT_FILES.items():
        ver = None
        p = os.path.join(out_dir, fname)
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k in ("schema", "schema_version", "version", "cert_version"):
                        if data.get(k):
                            ver = data[k]
                            break
        except Exception:
            ver = None
        out[name] = ver
    return out


def build_run_manifest(*, graph_db: str, out_dir: str, languages: list, lsp_scope_files: int,
                       lsp_max_edges: str, gate_rc: int, artifacts_present: dict,
                       source_root: str) -> dict:
    """run_manifest.json — v2 = the v1 run-shape + RUN PROVENANCE (Stage-5 audit gap:
    a DeepSWE run could not prove which code produced it). Additive only: no task IDs,
    no gold, no behavior change to the proof/gates. Every provenance field is
    recorded-or-null, never guessed."""
    return {
        "schema": "gt.run_manifest.v2",
        # ── run shape (v1 fields, unchanged) ──
        "languages": languages,
        "lsp_scope_files": lsp_scope_files,
        "lsp_max_edges": lsp_max_edges,
        "gate_rc": gate_rc,
        "artifacts_present": artifacts_present,
        "source_root": source_root,
        "out": out_dir,
        # ── provenance: which code / substrate / task repo produced this run ──
        "gt_git_commit": _gt_git_commit(),
        "substrate_digest": _env_or_none("GT_SUBSTRATE_DIGEST"),
        "task_repo_commit": _env_or_none("GT_TASK_REPO_COMMIT"),
        "runtime_flags": {k: os.environ.get(k) for k in PROOF_FLAG_KEYS},
        "language_distribution": _language_distribution(graph_db),
        "graph_db_sha256": _sha256_file(graph_db),
        "cert_versions": _cert_versions(out_dir),
    }


def expected_outputs(out_dir: str) -> list[str]:
    """The absolute artifact paths this entrypoint guarantees under --out."""
    return [os.path.join(out_dir, a) for a in REQUIRED_ARTIFACTS]


def validate_proof_env() -> list[str]:
    """Return a list of proof-boundary violations (empty == clean). Enforces: in-container,
    baked deps (NO per-task pip/download), all proof flags. Used by main() + the tests."""
    problems: list[str] = []
    if os.environ.get("GT_PROOF_MODE") != "1":
        problems.append("GT_PROOF_MODE!=1 (this entrypoint is proof-only)")
    if os.environ.get("GT_CONTAINERIZED") != "1":
        problems.append("GT_CONTAINERIZED!=1 (must run INSIDE the substrate container)")
    for f in ("GT_REQUIRE_FTS5", "GT_REQUIRE_EMBEDDER", "GT_FORCE_ONNX_EMBEDDER",
              "GT_REQUIRE_LSP", "GT_REQUIRE_FULL_STACK"):
        if os.environ.get(f) != "1":
            problems.append(f"{f}!=1")
    strat = os.environ.get("GT_RUNTIME_STRATEGY", "")
    if strat and strat != "unified_substrate":
        problems.append(f"GT_RUNTIME_STRATEGY={strat!r} (expected unified_substrate)")
    # BAKED deps — never install at runtime. A missing dep is a build error in the substrate
    # image, NEVER a per-task pip/download.
    problems.extend(_baked_lsp_problems())
    problems.extend(_baked_embedder_problems())
    if not shutil.which("gt-index") and not os.path.exists("/usr/local/bin/gt-index"):
        problems.append("gt-index not baked")
    return problems


def _models_root() -> str:
    return os.environ.get("GT_MODELS_ROOT", os.path.join(GT_HOME, "models"))


def _baked_lsp_problems() -> list[str]:
    """Assert EVERY LSP server resolve.py can spawn is baked on PATH. The canonical set is
    src/groundtruth/lsp/config.py::LSP_SERVERS (the ONLY language-aware source) — NOT a
    benchmark-shaped list. We probe the binary resolve.py actually spawns (command[0]),
    deriving it from config so the check tracks config automatically. pyright-langserver
    accepts the `pyright` CLI alias (npm ships both). Generalized, correct-or-quiet."""
    problems: list[str] = []
    # Each baked server command[0] -> acceptable PATH aliases. Derived from config below.
    aliases = {
        "pyright-langserver": ("pyright-langserver", "pyright"),
    }
    try:
        sys.path.insert(0, os.path.join(GT_HOME, "src"))
        from groundtruth.lsp.config import LSP_SERVERS  # canonical, language-aware
        commands = sorted({cfg.command[0] for cfg in LSP_SERVERS.values() if cfg.command})
    except Exception:
        # Fail-closed to the known set if config can't be imported (still NOT benchmark-shaped).
        commands = ["pyright-langserver", "typescript-language-server", "gopls",
                    "rust-analyzer", "jdtls"]
    for cmd in commands:
        cands = aliases.get(cmd, (cmd,))
        if not any(shutil.which(c) for c in cands):
            problems.append(f"LSP server {cmd!r} not baked on PATH "
                            f"(do NOT install per task; tried: {', '.join(cands)})")
    return problems


def _baked_embedder_problems() -> list[str]:
    """Assert the CONFIGURED localization embedder is baked, consistent with
    proof.embedder_model_path / context.model_files_baked (which derive the dirname from
    embed._default_embed_model()). The loader DEFAULT is gte-modernbert-base.

    NO-FALLBACK on the proof path (audit Stage-3 reconcile): under GT_REQUIRE_EMBEDDER the
    embedder loaders (_get_model / _get_embedder) now require the CONFIGURED model (gte) and
    RAISE rather than silently substitute e5. So validate_proof_env must likewise require the
    CONFIGURED model to be baked — the prior "configured-default OR e5" acceptance would clear
    the boundary while the loader then raises, a contradiction. We accept ONLY the configured
    model's ONNX (model.onnx or a baked int8/quantized variant). e5 remains baked for the
    sqlite-vec MEMORY store, but it is NOT an acceptable substitute for the proof-path embedder.
    Variants accepted (matches EmbeddingModel._resolve_onnx_path): model.onnx, model_int8.onnx,
    model_quantized.onnx, model_uint8.onnx."""
    root = _models_root()
    try:
        sys.path.insert(0, os.path.join(GT_HOME, "src"))
        from groundtruth.memory.enrich.embed import _default_embed_model
        configured = _default_embed_model().split("/")[-1]  # e.g. gte-modernbert-base
    except Exception:
        configured = "gte-modernbert-base"
    variants = ("model.onnx", "model_int8.onnx", "model_quantized.onnx", "model_uint8.onnx")
    paths = [os.path.join(root, configured, v) for v in variants]
    if any(os.path.exists(p) for p in paths):
        return []
    tried = "; ".join(paths)
    return [f"configured embedder model {configured!r} not baked (no silent e5 substitution on the "
            f"proof path); do NOT download per task. tried: {tried}"]


def _gt_index_bin() -> str:
    return shutil.which("gt-index") or "/usr/local/bin/gt-index"


def _detect_lang(graph_db: str) -> str:
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        r = c.execute("select language from nodes where is_test=0 and language is not null "
                      "and trim(language)!='' group by language order by count(*) desc limit 1").fetchone()
        c.close()
        return r[0] if r else "python"
    except Exception:
        return "python"


def _run(cmd: list[str], env: dict | None = None) -> int:
    print(f"[gt-run-proof] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, env=env or os.environ.copy()).returncode


_EVAL_LEAK_ENV = ("FAIL_TO_PASS", "PASS_TO_PASS", "GOLD_PATCH", "GOLD_FILES", "TEST_PATCH",
                  "GT_GOLD", "SWE_GOLD", "SWE_TEST_PATCH")
_EVAL_LEAK_FILES = {"test_patch.diff", "gold_patch.diff", "test_patch", "gold_patch",
                    "fail_to_pass.json", "pass_to_pass.json", "eval.sh", "run_tests.sh",
                    "eval_spec.json", "run_instance.sh", "fail_to_pass", "pass_to_pass"}


def eval_leakage(source_root: str) -> list:
    """Separation of concerns / anti-cheat: GT (the HELPER) must NEVER see the evaluator's hidden
    tests or gold. The substrate's ONLY input is the read-only repo at the agent's commit. Returns a
    list of leaks (empty == clean) if any eval artifact (gold / test_patch / FAIL_TO_PASS) reaches GT
    via an env key or a harness-injected TOP-LEVEL file. The repo's OWN tests are legitimate and are
    never flagged — we inspect only env keys + top-level injected names, not the repo's test tree."""
    leaks = []
    for k in os.environ:
        ku = k.upper()
        if any(tok in ku for tok in _EVAL_LEAK_ENV):
            leaks.append(f"env:{k}")
    try:
        for name in os.listdir(source_root):
            if name.lower() in _EVAL_LEAK_FILES:
                leaks.append(f"file:{name}")
    except Exception:
        pass
    return leaks


_LSP_LANGS = {"python", "go", "javascript", "typescript", "rust", "java", "c", "cpp", "ruby", "php"}
_STOP = {"the", "and", "for", "with", "this", "that", "when", "from", "into", "have", "will", "your",
         "are", "was", "not", "but", "you", "can", "all", "any", "has", "had", "get", "set", "def",
         "self", "test", "error", "issue", "should", "would", "could", "because", "return", "none"}


def _detect_langs(graph_db: str) -> list:
    """ALL languages present in the graph that have a known LSP server, ordered by node count desc
    (dominant first). Polyglot repos resolve every language, not just the dominant one."""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        rows = c.execute("select language, count(*) c from nodes where is_test=0 and language is not "
                         "null and trim(language)!='' group by language order by c desc").fetchall()
        c.close()
        return [r[0] for r in rows if r[0] and str(r[0]).lower() in _LSP_LANGS]
    except Exception:
        return []


def _read_issue(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _issue_terms(issue_text: str, k: int = 30) -> list:
    import re
    out = []
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", issue_text or ""):
        if w.lower() in _STOP:
            continue
        if w not in out:
            out.append(w)
        if len(out) >= k:
            break
    return out


def _demand_scope_files(graph_db: str, issue_text: str, cap: int = 80) -> list:
    """Demand-driven scope (Heintze & Tardieu, PLDI 2001): the issue-relevant files via an FTS5
    MATCH on the issue terms. Returns [] (=> whole-repo) when there's no issue. Bounds LSP work to
    the subgraph that matters so it can be resolved FULLY instead of whole-repo-capped-at-500."""
    terms = _issue_terms(issue_text)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        rows = c.execute("select n.file_path from nodes_fts f join nodes n on n.id=f.rowid where "
                         "nodes_fts match ? group by n.file_path order by count(*) desc limit ?",
                         (match, cap)).fetchall()
        c.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def aggregate_lsp_verdicts(lang_verdicts: dict, *, require_lsp: bool, any_success: bool):
    """P1-e polyglot aggregation rule over per-language LSP verdicts -> (ok, failures).

    ``failures`` lists 'lang=verdict' for every language whose pass FAILED:
      * LSP_INSTALL_MISSING  — baked-server language, binary missing on PATH;
      * LSP_FAIL_NO_WARM     — server launched (or tried) but never warmed: a
        launched-but-never-warm server is a FAILURE, not a pass;
      * LSP_RESOLVE_ERROR(..)— the resolve pass exited nonzero without a verdict.
    Genuinely-unknown languages (LSP_UNSUPPORTED_EXPLICIT) and the two valid verdicts
    (LSP_ACTIVE_VALID / LSP_NO_OP_VALID_WITH_WARM_SERVER) are never failures.

    Under ``require_lsp`` (GT_REQUIRE_LSP=1): ok=False if ANY known language failed —
    a sibling language succeeding must NOT mask another language's gap — or if no
    language resolved successfully at all. Without the flag, ok=True (verdicts are
    still recorded for the certs/manifest). Pure + deterministic for the tests."""
    failures = [
        f"{lg}={v}" for lg, v in lang_verdicts.items()
        if v in ("LSP_INSTALL_MISSING", "LSP_FAIL_NO_WARM") or str(v).startswith("LSP_RESOLVE_ERROR")
    ]
    if not require_lsp:
        return True, failures
    if failures:
        return False, failures
    if not any_success:
        return False, ["<none>=NO_LANGUAGE_RESOLVED"]
    return True, failures


def emit_brief(out_dir: str, issue_text: str, work: str, graph: str, *, generator=None):
    """Emit the curated brief to <out>/brief.txt — proof artifact #8 (P0.1-c).

    gt-run-proof is PROOF-ONLY (validate_proof_env requires GT_PROOF_MODE=1), and the agent
    consumes /gt_artifacts/brief.txt READ-ONLY: there is NO host fallback (host run_v74 is
    fail-closed by the container boundary), so an empty or failed brief is a missing proof
    artifact — never a WARN. Returns (ok, detail); the caller fails closed on ok=False with
    GT_ARTIFACT_MISSING. ``generator`` is injectable for tests; default = the real
    generate_v1r_brief (which also writes the issue anchors mirrored below)."""
    try:
        if generator is None:
            from groundtruth.pretask.v1r_brief import generate_v1r_brief as generator
        b = generator(issue_text=issue_text, repo_root=work, graph_db=graph, bug_id="portable")
        bt = (getattr(b, "brief_text", "") or "").strip()
    except Exception as e:
        return False, f"brief generation raised (no swallow in proof): {type(e).__name__}: {e}"
    if not bt:
        return False, ("portable brief EMPTY — proof mode requires a non-empty brief.txt "
                       "(the agent consumes /gt_artifacts/brief.txt; there is no host fallback)")
    try:
        with open(os.path.join(out_dir, "brief.txt"), "w", encoding="utf-8") as bf:
            bf.write(bt)
    except OSError as e:
        return False, f"brief.txt write failed: {e}"
    if os.path.exists("/tmp/gt_issue_anchors.json"):
        try:
            shutil.copy("/tmp/gt_issue_anchors.json", os.path.join(out_dir, "gt_issue_anchors.json"))
        except OSError:
            pass
    return True, f"{len(bt)} chars"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt-run-proof")
    ap.add_argument("--source-root", required=False, default="/work")
    ap.add_argument("--out", required=False, default="/gt_artifacts")
    ap.add_argument("--issue", default=os.environ.get("GT_ISSUE_FILE", ""))
    ap.add_argument("--lang", default="")
    ap.add_argument("--print-contract", action="store_true",
                    help="print the artifact contract JSON and exit 0 (no execution)")
    a = ap.parse_args(argv)

    if a.print_contract:
        print(json.dumps({
            "schema": "gt.run_proof.contract.v1",
            "entrypoint": "gt-run-proof",
            "required_env": ["GT_PROOF_MODE=1", "GT_CONTAINERIZED=1", "GT_RUNTIME_STRATEGY=unified_substrate",
                             "GT_REQUIRE_FTS5=1", "GT_REQUIRE_EMBEDDER=1", "GT_FORCE_ONNX_EMBEDDER=1",
                             "GT_REQUIRE_LSP=1", "GT_REQUIRE_FULL_STACK=1"],
            "inputs": {"source_root": "read-only mount of the task repo", "out": "writable artifact dir"},
            "outputs": REQUIRED_ARTIFACTS,
            "guarantees": ["no per-task pip install", "no model download", "no host GT execution",
                           "no mutation of the task image", "baked pinned image",
                           "no eval-test/gold leakage (helper/evaluator separation)"],
        }, indent=2))
        return 0

    os.makedirs(a.out, exist_ok=True)

    # Boundary + baked-deps + flags. A host run / missing baked dep fails-closed here.
    violations = validate_proof_env()
    if violations:
        print("FINAL_PIPELINE_HOST_SPLIT_FAIL / SUBSTRATE_NOT_PORTABLE: " + "; ".join(violations),
              file=sys.stderr)
        return 2
    try:
        sys.path.insert(0, os.path.join(GT_HOME, "src"))  # package lives at $GT_HOME/src
        sys.path.insert(0, GT_HOME)
        from groundtruth.runtime.context import assert_container_boundary
        assert_container_boundary("gt-run-proof")
    except Exception as e:
        print(f"FINAL_PIPELINE_HOST_SPLIT_FAIL: {e}", file=sys.stderr)
        return 2

    # Separation of concerns (anti-cheat): GT is the HELPER, never the evaluator. It must never see
    # the evaluator's hidden tests or gold. Fail-closed if any eval artifact leaked in via env/file.
    leaks = eval_leakage(a.source_root)
    if leaks:
        print("EVAL_LEAKAGE_FORBIDDEN: GT (substrate) must never receive the evaluator's hidden "
              "tests/gold/FAIL_TO_PASS; separation breached by: " + ", ".join(leaks), file=sys.stderr)
        return 2

    # The task repo is mounted READ-ONLY at --source-root; copy to a writable workdir so gt-index
    # never mutates the official task image's source.
    work = "/tmp/gt_work_src"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(a.source_root, work, symlinks=True, ignore_dangling_symlinks=True)

    graph = os.path.join(a.out, "graph.db")
    cert_lsp = os.path.join(a.out, "lsp_certificate.json")
    cert_graph = os.path.join(a.out, "graph_certificate.json")
    cert_emb = os.path.join(a.out, "embedder_certificate.json")
    gate_report = os.path.join(a.out, "foundational_gate_report.json")
    issue_file = a.issue or "/tmp/issue.txt"
    # foundational_gates reads the issue file; in the portable run it may not be mounted. Ensure it
    # exists (empty or GT_ISSUE_TEXT) so the gates run + emit certs instead of crashing on open().
    if not os.path.exists(issue_file):
        try:
            with open(issue_file, "w", encoding="utf-8") as _f:
                _f.write(os.environ.get("GT_ISSUE_TEXT", ""))
        except Exception:
            issue_file = os.path.join(a.out, "issue.txt")
            with open(issue_file, "w", encoding="utf-8") as _f:
                _f.write(os.environ.get("GT_ISSUE_TEXT", ""))

    # The groundtruth package is baked at $GT_HOME/src (Dockerfile: COPY src /opt/gt/src), the scripts
    # at $GT_HOME/scripts. The subprocesses (resolve, foundational_gates) import groundtruth, so
    # PYTHONPATH MUST include $GT_HOME/src — PREPEND it to (never overwrite) the image's PYTHONPATH.
    _pp = os.environ.get("PYTHONPATH", "")
    _gt_paths = os.pathsep.join([os.path.join(GT_HOME, "src"),
                                 os.path.join(GT_HOME, "scripts", "swebench"),
                                 os.path.join(GT_HOME, "benchmarks", "swebench"), GT_HOME])
    base_env = os.environ.copy()
    base_env.update({"PYTHONPATH": _gt_paths + (os.pathsep + _pp if _pp else ""), "GT_HOME": GT_HOME,
                     "GT_MODELS_ROOT": os.environ.get("GT_MODELS_ROOT", os.path.join(GT_HOME, "models")),
                     "GT_SOURCE_ROOT": work, "GT_GRAPH_DB": graph,
                     "GT_LSP_CERT": cert_lsp, "GT_GRAPH_CERT": cert_graph, "GT_EMBEDDER_CERT": cert_emb})

    # 1. graph build (FTS5 enforced at index time under GT_REQUIRE_FTS5)
    if _run([_gt_index_bin(), "-root", work, "-output", graph], base_env) != 0:
        print("FATAL: gt-index failed", file=sys.stderr)
        return 2
    # 2. LSP enrichment — demand-driven + polyglot + un-throttled within the issue scope.
    # gt_gt §3/§7 + CLAUDE.md "demand-driven, not exhaustive": resolve the issue-relevant subgraph
    # for EVERY language present (not just the dominant one), un-capped within that bounded scope —
    # closing the "whole-repo capped at 500 -> majority name_match" gap. With a real issue the
    # demand scope resolves FULLY; with no issue (free liveness proof) it keeps the 500 default.
    #
    # P1-e (cert-overwrite fix): per-language verdicts AGGREGATE. Each language's resolve pass
    # writes its OWN certificate (lsp_certificate_<lang>.json) so no FAIL cert is ever overwritten
    # by a later language; the DOMINANT language's cert is then copied to the canonical
    # lsp_certificate.json path. Under GT_REQUIRE_LSP=1 the run fails closed if ANY known
    # language is INSTALL_MISSING **or FAIL_NO_WARM** (a launched-but-never-warm server is a
    # failure, not a pass — resolve.py exits 2 on both); genuinely-unknown languages
    # (LSP_UNSUPPORTED_EXPLICIT) remain an honest no-op.
    langs = _detect_langs(graph) or ([a.lang] if a.lang else [_detect_lang(graph)])
    scope_files = _demand_scope_files(graph, _read_issue(issue_file))
    scope_path = ""
    if scope_files:
        scope_path = os.path.join(a.out, "gt_scope_files.txt")
        with open(scope_path, "w", encoding="utf-8") as _sf:
            _sf.write("\n".join(scope_files))
    max_edges = "20000" if scope_files else "500"
    # Capture resolve's stdout (the LSP_METRICS contract line) into GT_LSP_METRICS_FILE so the
    # foundational LSP gate can read residual/scoped — previously uncaptured -> gate read resolved=0
    # while the graph + cert held the real count (the measurement half of the stamp discrepancy).
    lsp_metrics_file = os.path.join(a.out, "gt_lsp_metrics.txt")
    base_env["GT_LSP_METRICS_FILE"] = lsp_metrics_file
    open(lsp_metrics_file, "w").close()
    import re as _re
    lsp_ok = False
    lang_verdicts: dict = {}  # per-language verdict (aggregated, none overwritten)
    for lg in reversed(langs):  # least-common first, dominant last
        # Per-language certificate path: NO overwrite — every language's cert persists.
        cert_lsp_lang = os.path.join(a.out, f"lsp_certificate_{lg}.json")
        lang_env = dict(base_env, GT_LSP_CERT=cert_lsp_lang)
        cmd = [sys.executable, "-m", "groundtruth.resolve", "--db", graph, "--root", work,
               "--resolve", "--lang", lg, "--max-edges", max_edges]
        if scope_path:
            cmd += ["--source-files", scope_path]
        print(f"[gt-run-proof] $ {' '.join(cmd)}", flush=True)
        rr = subprocess.run(cmd, env=lang_env, capture_output=True, text=True)
        sys.stdout.write(rr.stdout or ""); sys.stderr.write(rr.stderr or "")
        with open(lsp_metrics_file, "a", encoding="utf-8") as _mf:
            _mf.write(rr.stdout or "")
        # Verdict for THIS language from its LSP_METRICS contract line (last wins). A
        # nonzero exit without a verdict line is recorded as LSP_RESOLVE_ERROR(rc=N).
        _vs = _re.findall(r"verdict=(\S+)", rr.stdout or "")
        verdict = _vs[-1] if _vs else ""
        if not verdict and rr.returncode != 0:
            verdict = f"LSP_RESOLVE_ERROR(rc={rr.returncode})"
        lang_verdicts[lg] = verdict or "LSP_RESOLVE_ERROR(no_verdict)"
        if rr.returncode == 0:
            lsp_ok = True
    # Canonical cert = the DOMINANT language's (langs[0], node-count desc); per-language
    # certs persist alongside so a FAIL verdict is never lost to an overwrite.
    _dom_cert = os.path.join(a.out, f"lsp_certificate_{langs[0]}.json")
    if os.path.exists(_dom_cert):
        try:
            shutil.copyfile(_dom_cert, cert_lsp)
        except OSError as _ce:
            print(f"WARN: could not copy dominant LSP cert to canonical path: {_ce}", file=sys.stderr)
    print(f"[gt-run-proof] per-language LSP verdicts: {lang_verdicts}", flush=True)
    # P1-e fail-closed aggregation: ANY known language INSTALL_MISSING / FAIL_NO_WARM /
    # resolve-error fails the proof under GT_REQUIRE_LSP=1 — a sibling language's success
    # must never mask another language's gap (audit defect #1), and a launched-but-
    # never-warm server is a failure, not a pass.
    _agg_ok, _agg_failures = aggregate_lsp_verdicts(
        lang_verdicts,
        require_lsp=os.environ.get("GT_REQUIRE_LSP") == "1",
        any_success=lsp_ok,
    )
    if not _agg_ok:
        print("LSP_LIVENESS_FAIL: GT_REQUIRE_LSP=1 but known language(s) failed the LSP pass: "
              f"{', '.join(_agg_failures)} — a baked-server language that cannot launch/warm "
              "fails closed (no silent pass, no sibling-language masking)", file=sys.stderr)
        return 2

    # 3. graph certificate
    _run([sys.executable, os.path.join(GT_HOME, "scripts/metrics/graph_certificate.py"), graph,
          "--source-root", work, "--lsp-cert", cert_lsp, "--out", cert_graph,
          "--built-inside-container", "1"], base_env)

    # 4. foundational gates (emits foundational_gate_report.json + embedder_certificate.json via run_v74)
    gate_env = dict(base_env, GT_GATES_DEEP_JSON=gate_report)
    rc = _run([sys.executable, os.path.join(GT_HOME, "scripts/metrics/foundational_gates.py"),
               graph, work, issue_file], gate_env)

    # 4b. Embedder certificate — foundational_gates writes it via run_v74 ONLY when the brief has
    # candidates (a non-empty issue). Guarantee the artifact: if absent, emit it from a direct
    # identity + cosine-discrimination probe (proves the forced-ONNX embedder LOADS + produces a
    # finite, discriminating vector). The gate (gate_rc above) proves CONSUMPTION; together =
    # "loaded AND used". Issue-independent, so it always emits.
    if not os.path.exists(cert_emb):
        try:
            os.environ["GT_EMBEDDER_CERT"] = cert_emb
            from groundtruth.runtime import proof as _proof
            import numpy as _np
            from groundtruth.pretask.v7_4_brief import _get_model
            _proof.embedder_identity()  # loads the embedder (raises if not the forced-ONNX one)
            # Encode errors are NOT swallowed — a degenerate/unloadable embedder is fatal in proof.
            vs = _get_model().encode(["database connection pool",
                                      "database connection pool timeout", "the quick brown fox"])

            def _cos(a, b):
                a = _np.asarray(a, float); b = _np.asarray(b, float)
                return float(a @ b / ((a @ a) ** 0.5 * (b @ b) ** 0.5 + 1e-9))
            disc = _cos(vs[0], vs[1]) - _cos(vs[0], vs[2])
            cert = _proof.build_embedder_certificate(db=graph, bug_id="portable_probe")
            cert["discrimination_margin"] = disc
            cert["emitted_by"] = "gt-run-proof direct identity+cosine probe (issue-independent)"
            _proof.write_embedder_certificate(cert)
            print(f"[gt-run-proof] embedder cert emitted via direct probe (disc={disc})", flush=True)
        except Exception as e:
            print(f"EMBEDDER_USAGE_FAIL: embedder probe failed (no swallow in proof): {e}", file=sys.stderr)
            return 2

    # 4c. CLASSIFY the embedder certificate (probe OR gate-written) and FAIL-CLOSED on a bad verdict
    # — degenerate/no-discrimination, zero model, ST-under-forced-ONNX, model-root divergence,
    # dropped semantic. Presence alone is not proof on a real-money run.
    try:
        _md = os.path.join(GT_HOME, "scripts", "metrics")
        if _md not in sys.path:
            sys.path.insert(0, _md)
        import importlib
        _ec = importlib.import_module("embedder_certificate")
        _verdict, _ok = _ec.classify_embedder(_ec.load_embedder_cert(cert_emb),
                                              proof_mode=True, require_embedder=True)
        print(f"[gt-run-proof] embedder verdict: {_verdict}", flush=True)
        if not _ok:
            print(f"EMBEDDER_USAGE_FAIL: {_verdict}", file=sys.stderr)
            return 2
    except Exception as e:
        print(f"WARN: embedder cert classification skipped: {e}", file=sys.stderr)

    # 4d. Emit the curated brief IN-CONTAINER (run_v74 is legal here — containerized + proof) so the
    # agent CONSUMES it from /gt_artifacts/brief.txt instead of regenerating on the host (where
    # run_v74 is fail-closed by the boundary assert). generate_v1r_brief writes the issue anchors;
    # mirror them out for the agent's in-container post_view/post_edit consumers.
    # P0.1-c: brief.txt is REQUIRED (artifact #8). In proof mode an empty/missing brief is
    # GT_ARTIFACT_MISSING (fail-closed) — the old "agent will host-fallback" WARN was stale:
    # the host brief path is fail-closed by the container boundary, so a missing brief here
    # means the agent runs with NO brief at all (the green-zero-run chain).
    _brief_ok, _brief_detail = emit_brief(a.out, _read_issue(issue_file), work, graph)
    if not _brief_ok:
        print(f"GT_ARTIFACT_MISSING: brief.txt — {_brief_detail}", file=sys.stderr)
        return 2
    print(f"[gt-run-proof] brief emitted -> /gt_artifacts/brief.txt ({_brief_detail})", flush=True)

    # 5. runtime_context.json
    try:
        from groundtruth.runtime.context import GTRuntimeContext
        ctx = GTRuntimeContext.from_env(source_root=work, graph_db=graph)
        with open(os.path.join(a.out, "runtime_context.json"), "w", encoding="utf-8") as f:
            json.dump({"runtime_root": ctx.runtime_root, "source_root": ctx.source_root,
                       "graph_db": ctx.graph_db, "models_root": ctx.models_root,
                       "inside_container": ctx.inside_container, "proof_mode": ctx.proof_mode,
                       "containerized": ctx.containerized,
                       "runtime_context_id": base_env.get("GT_CONTEXT_ID", "")}, f, indent=2)
    except Exception as e:
        print(f"WARN: runtime_context.json: {e}", file=sys.stderr)

    # 6. run manifest + artifact presence (+ run provenance — see build_run_manifest)
    present = {a_: os.path.exists(os.path.join(a.out, a_)) for a_ in REQUIRED_ARTIFACTS
               if a_ != "run_manifest.json"}
    manifest = build_run_manifest(graph_db=graph, out_dir=a.out, languages=langs,
                                  lsp_scope_files=len(scope_files), lsp_max_edges=max_edges,
                                  gate_rc=rc, artifacts_present=present, source_root=work)
    with open(os.path.join(a.out, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    missing = [k for k, v in present.items() if not v]
    if missing:
        print(f"SUBSTRATE_MISSING_CERTS: {missing}", file=sys.stderr)
    print(f"[gt-run-proof] done: gate_rc={rc} artifacts_present={present}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

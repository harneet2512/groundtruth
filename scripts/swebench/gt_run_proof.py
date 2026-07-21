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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

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

PROOF_STAGES = (
    "env_validation",
    "dep_store",
    "source_copy",
    "workspace_metadata",
    "index",
    "lsp_pass",
    "graph_cert",
    "gates",
    "brief_emit",
    "artifact_contract",
)


class _ProofTracker:
    """Persist proof_progress.json plus one canonical proof_verdict.json."""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = out_dir
        self.stages: list[dict] = []
        self._flush()

    @staticmethod
    def _memory_heartbeat() -> dict:
        """Best-effort RSS snapshot for OOM triage (P1-04)."""
        rss_kb: int | None = None
        try:
            import resource

            raw = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            # Linux reports KiB; macOS reports bytes.
            rss_kb = raw if raw < 10_000_000 else raw // 1024
        except Exception:
            try:
                import psutil

                rss_kb = int(psutil.Process().memory_info().rss // 1024)
            except Exception:
                pass
        return {"rss_kb": rss_kb}

    def _flush(self) -> None:
        path = os.path.join(self.out_dir, "proof_progress.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema": "gt.proof_progress.v1", "stages": self.stages}, fh, indent=2)
            fh.write("\n")

    def complete(self, stage: str, **extra) -> None:
        row = {"stage": stage, "status": "ok"}
        row.update(self._memory_heartbeat())
        row.update(extra)
        self.stages.append(row)
        self._flush()

    def verdict(self, state: str, stage: str, **extra) -> None:
        payload = {
            "schema": "gt.proof_verdict.v1",
            "state": state,
            "stage": stage,
            "stages": self.stages,
        }
        payload.update(extra)
        path = os.path.join(self.out_dir, "proof_verdict.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")

    def fail(self, stage: str, code: str, message: str, **extra) -> int:
        row = {"stage": stage, "status": "fail", "code": code, "message": message}
        row.update(self._memory_heartbeat())
        row.update(extra)
        self.stages.append(row)
        self._flush()
        failure = {
            "schema": "gt.proof_failure.v1",
            "stage": stage,
            "code": code,
            "message": message,
            "stages": self.stages,
        }
        failure.update(extra)
        fpath = os.path.join(self.out_dir, "proof_failure.json")
        with open(fpath, "w", encoding="utf-8") as fh:
            json.dump(failure, fh, indent=2)
            fh.write("\n")
        self.verdict("fail", stage, code=code, message=message, **extra)
        print(f"{code}: {message}", file=sys.stderr)
        return 2

# Where GT is baked in the substrate image (NOT a checkout, NOT host paths).
GT_HOME = os.environ.get("GT_HOME", "/opt/gt")

# ── Deterministic float-reduction pins (FIX 1) ───────────────────────────────────────────
# The brief-determinism proof compares the PRIMARY brief (generated in the foundational_gates
# SUBPROCESS) against a WITNESS brief (generated IN-PROCESS at emit_brief). Both reduce the
# per-file embedding cosines with numpy/BLAS. ONNX inference is already single-threaded
# (embed._deterministic_session_options), but the numpy/BLAS pooling + L2-normalisation that
# follow read OMP/MKL/OpenBLAS/NumExpr thread counts at process start — and those are NOT set
# anywhere in the live env. With >1 thread the two processes sum the reduction in different
# orders, so a near-tie semantic cosine flips top-k membership -> the two briefs differ ->
# DETERMINISM_MISMATCH (correctly caught by the Stage-1 gate). Pin every backend to ONE thread
# so the reduction ORDER is fixed and both sides are byte-identical. setdefault (never clobber)
# so a shell/Dockerfile pin or an explicit operator/test override still wins; applied at module
# import — before numpy is first imported in this process — so the in-process witness is pinned.
DETERMINISTIC_THREAD_PINS = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
for _pin_k, _pin_v in DETERMINISTIC_THREAD_PINS.items():
    os.environ.setdefault(_pin_k, _pin_v)

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


def commit_parity_status() -> dict:
    """Commit parity between the code the SUBSTRATE was BUILT from
    (GT_SUBSTRATE_BUILD_COMMIT, baked at docker build) and the code this RUN claims
    (GT_GIT_COMMIT, the workflow checkout sha). Integration runs the baked /opt/gt/src,
    so a divergence means the result is attributed to a commit whose code did not run.
    Recorded-or-null, never fabricated. status: 'match' | 'mismatch' | 'unknown'."""
    build = _env_or_none("GT_SUBSTRATE_BUILD_COMMIT")
    run = _env_or_none("GT_GIT_COMMIT")
    if not build or not run or build in ("dev", "unknown"):
        status = "unknown"
    elif build == run:
        status = "match"
    else:
        status = "mismatch"
    return {"substrate_build_commit": build, "run_commit": run, "status": status}


def assert_commit_parity() -> tuple[bool, str]:
    """Fail-closed commit-parity gate. Under GT_REQUIRE_COMMIT_PARITY=1 a substrate built
    from a DIFFERENT commit than the run claims is a stale-substrate legitimacy violation
    -> (False, detail). Default (flag unset) is RECORD-ONLY: the manifest still carries
    commit_parity so drift is never SILENT, but a pinned substrate that legitimately lags
    during iteration does not abort. Returns (ok, detail)."""
    st = commit_parity_status()
    if os.environ.get("GT_REQUIRE_COMMIT_PARITY") != "1":
        return True, f"commit_parity={st['status']} (gate off; record-only)"
    if st["status"] != "match":
        return False, (
            f"GT_COMMIT_PARITY_MISMATCH: substrate built from {st['substrate_build_commit']} "
            f"but the run claims {st['run_commit']} — a stale substrate cannot run under "
            "GT_REQUIRE_COMMIT_PARITY=1; rebuild + repin the substrate at the run commit."
        )
    return True, f"commit_parity={st['status']}"


def artifact_hashes(out_dir: str, names: list[str]) -> dict:
    """SHA-256 bind proof artifacts into run_manifest; absent artifacts are omitted."""
    out: dict[str, str] = {}
    for name in names:
        p = os.path.join(out_dir, name)
        if not os.path.isfile(p):
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        out[name] = h.hexdigest()
    return out


def build_run_manifest(*, graph_db: str, out_dir: str, languages: list, lsp_scope_files: int,
                       lsp_max_edges: str, lsp_ready_budgets: dict, gate_rc: int, artifacts_present: dict,
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
        "lsp_ready_budgets": lsp_ready_budgets,
        "gate_rc": gate_rc,
        "artifacts_present": artifacts_present,
        "artifact_sha256": artifact_hashes(out_dir, sorted(artifacts_present)),
        "proof_verdict_schema": "gt.proof_verdict.v1",
        "source_root": source_root,
        "out": out_dir,
        # ── provenance: which code / substrate / task repo produced this run ──
        "gt_git_commit": _gt_git_commit(),
        # The commit the SUBSTRATE was BUILT from (baked ENV) — the code Integration
        # actually runs is this /opt/gt/src, not the workflow checkout. Divergence from
        # gt_git_commit = a stale substrate (see commit_parity_status / GT_REQUIRE_COMMIT_PARITY).
        "substrate_build_commit": _env_or_none("GT_SUBSTRATE_BUILD_COMMIT"),
        "commit_parity": commit_parity_status(),
        "substrate_digest": _env_or_none("GT_SUBSTRATE_DIGEST"),
        "task_repo_commit": _env_or_none("GT_TASK_REPO_COMMIT"),
        "runtime_flags": {k: os.environ.get(k) for k in PROOF_FLAG_KEYS},
        "language_distribution": _language_distribution(graph_db),
        "graph_db_sha256": _sha256_file(graph_db),
        "cert_versions": _cert_versions(out_dir),
        "brief_sha256": _sha256_file(os.path.join(out_dir, "brief.txt")),
        "issue_sha256": _sha256_file(os.path.join(out_dir, "issue.txt")),
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
    # A substrate MAY deliberately omit a language's server (jdtls/Java was dropped from
    # docker/Dockerfile.gt-substrate 2026-06-20 — zero Java in the 113 tasks, ~500MB saved;
    # config.py keeps the entry because the PRODUCT still supports Java when jdtls is present).
    # The image declares its omissions via GT_PROOF_OMIT_LSP so the portability gate stays
    # honest: assert only the servers THIS image bakes, not every language config can dispatch.
    # Generalized (any server name, env-driven); default mirrors the Dockerfile's documented
    # removal so the gate matches the standard build. resolve.py dispatches by extension, so an
    # omitted server is correct-or-quiet at spawn time — never a wrong-context risk.
    _omit = {s.strip() for s in os.environ.get("GT_PROOF_OMIT_LSP", "jdtls").split(",") if s.strip()}
    for cmd in commands:
        if cmd in _omit:
            continue
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
    return subprocess.run(cmd, env=env or clean_env_projection(os.environ)).returncode


_EVAL_LEAK_ENV = ("FAIL_TO_PASS", "PASS_TO_PASS", "GOLD_PATCH", "GOLD_FILES", "TEST_PATCH",
                  "GT_GOLD", "SWE_GOLD", "SWE_TEST_PATCH")
# GT-owned diagnostic env vars could be allowlisted here, but the proof path currently
# treats every gold/test/evaluator-shaped env key as tainted, including GT-prefixed keys.
_EVAL_LEAK_ENV_ALLOW = frozenset()
_EVAL_LEAK_FILES = {"test_patch.diff", "gold_patch.diff", "test_patch", "gold_patch",
                    "fail_to_pass.json", "pass_to_pass.json", "eval.sh", "run_tests.sh",
                    "eval_spec.json", "run_instance.sh", "fail_to_pass", "pass_to_pass"}


def _env_is_eval_leak(key: str) -> bool:
    """True iff an env key carries an evaluator-artifact token as a WHOLE underscore-
    delimited word-run. ``GOLD_FILES`` / ``SWE_GOLD_FILES`` leak, and GT-prefixed
    keys with the same token sequence are blocked too."""
    ku = key.upper()
    if ku in _EVAL_LEAK_ENV_ALLOW:
        return False
    segs = ku.split("_")
    for tok in _EVAL_LEAK_ENV:
        tks = tok.split("_")
        n = len(tks)
        for i in range(len(segs) - n + 1):
            if segs[i:i + n] == tks:  # token present as a contiguous whole-word run
                return True
    return False


def eval_leakage(source_root: str) -> list:
    """Separation of concerns / anti-cheat: GT (the HELPER) must NEVER see the evaluator's hidden
    tests or gold. The substrate's ONLY input is the read-only repo at the agent's commit. Returns a
    list of leaks (empty == clean) if any eval artifact (gold / test_patch / FAIL_TO_PASS) reaches GT
    via an env key or a harness-injected TOP-LEVEL file. The repo's OWN tests are legitimate and are
    never flagged — we inspect only env keys + top-level injected names, not the repo's test tree."""
    leaks = []
    for k in os.environ:
        if _env_is_eval_leak(k):
            leaks.append(f"env:{k}")
    try:
        for name in os.listdir(source_root):
            if name.lower() in _EVAL_LEAK_FILES:
                leaks.append(f"file:{name}")
    except Exception:
        pass
    return leaks


def clean_env_projection(src: dict) -> dict:
    """Environment passed to proof subprocesses: preserve runtime knobs, drop eval/gold taint."""
    out: dict[str, str] = {}
    allowed_prefixes = (
        "GT_", "PYTHON", "PATH", "HOME", "LANG", "LC_", "TZ", "TMP", "TEMP",
        "XDG_", "GOCACHE", "GOMODCACHE", "GOPATH", "GOTOOLCHAIN", "CARGO_",
        "RUST", "NODE_", "NPM_", "YARN_", "HF_", "TRANSFORMERS_",
        # P1-4 fix (Fable 2026-07-02): the workflow injects the OFFLINE seal (GOPROXY=off,
        # GOFLAGS=-mod=mod, GOSUMDB=off, GONOSUMCHECK=1) into the proof container, but these were
        # stripped here -> the resolve subprocess and the gopls it spawns ran UN-sealed -> per-task
        # network reach (breaks the "no per-task download" proof guarantee) / proxy timeouts that
        # burn gopls's 30s ready-budget -> LSP_WARN_NOT_READY / ZERO_CONVERSION on go. None are
        # eval-tainted. probe_workspace_metadata already re-adds them, proving the intent.
        "GOPROXY", "GOFLAGS", "GOSUMDB", "GONOSUMCHECK", "GOPRIVATE", "GOWORK", "GONOSUMDB",
    )
    for k, v in src.items():
        if _env_is_eval_leak(k):
            continue
        # FIX 1: the deterministic thread pins (OMP/MKL/OpenBLAS/NumExpr) match none of the
        # allowed prefixes, so without this the gate SUBPROCESS (which writes the PRIMARY brief)
        # would run UN-pinned while the in-process witness is pinned -> the two reduce in
        # different thread-pool orders -> DETERMINISM_MISMATCH. Pass them through so BOTH sides
        # of the compare reduce under identical single-thread pins.
        if (
            k in {"PATH", "HOME", "USER", "USERNAME", "SHELL"}
            or k in DETERMINISTIC_THREAD_PINS
            or any(k.startswith(p) for p in allowed_prefixes)
        ):
            out[k] = v
    return out


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


# ── GATE-1-aware dynamic LSP attempt budget (fix: 113-task sweep run 27249519544) ────────
# NOTE (delivered-partition, foundational_gates a2d9501f): gate_resolution's pred_B DECISION
# now dominance-checks the DELIVERED population (non-test source, name_match conf>=0.5 — the
# edges the localizer actually navigates). This LSP budget deliberately sizes to the RAW
# all-CALLS dominance gap below (pred_B_nondominance_RAW), NOT the delivered gap: the raw gap
# is an UPPER BOUND on the delivered gap, so budgeting to it can only OVER-provision LSP
# attempts (more residual resolution than the decision strictly needs) — harmless, capped by
# LSP_MAX_EDGES_CEILING, and it feeds a budget only, never a pass/fail. Shrinking it to the
# delivered gap would risk UNDER-provisioning and leaving the gap uncleared, so keep it raw.
# GATE 1 pred_B (raw) requires det >= name_match over CALLS edges. Each LSP promotion
# (verify/correct: name_match -> 'lsp', a deterministic method) closes the dominance gap
# (name_match - det) by 2; each delete closes it by 1. Flipping pred_B therefore needs
# 2*Promoted + Deleted > gap. The old FIXED un-scoped cap of 500 was structurally below
# that requirement on the failing TS repos (dynamodb-toolbox: gap = 3154 - 2485 = 669 —
# 500 attempts can clear it only at a >67% pure-promotion rate, far above any realistic
# mixed promote/delete/fail outcome). The budget must SCALE with the measured gap.
#
# Worst-case wall clock is BOUNDED by the ceiling: 20000 edges at the measured ~24
# edges/s ≈ 14 min per language; the dynamodb-class gap (669 -> budget 870) is ~36s, and
# the full 3154-edge TS residual would be ≈ 2.2 min. Repo-size-agnostic, no benchmark
# shapes: the gap is read from THIS graph, the floors/ceiling are fixed bounds.
LSP_MAX_EDGES_FLOOR = 500            # historical un-scoped default — kept as the floor
LSP_MAX_EDGES_SCOPED_FLOOR = 20000   # historical issue-scoped budget — kept as a floor
LSP_MAX_EDGES_CEILING = 20000        # bounded worst case (~14 min/lang at ~24 edges/s)
LSP_GAP_HEADROOM = 0.30              # sub-perfect promote/delete success margin


def _gate1_det_set() -> frozenset:
    """The gates' DETERMINISTIC resolution-method set — the SAME unified fact-set
    foundational_gates.gate_resolution counts (curation_map), same fallback literal, so
    this budget's det count matches the gate's. The budget's GAP targets pred_B's RAW
    (all-CALLS) dominance, an upper bound on the delivered-partition decision (see the
    dynamic-budget note above) — deliberately raw to never under-provision LSP."""
    try:
        sys.path.insert(0, os.path.join(GT_HOME, "src"))
        from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
        return frozenset(DETERMINISTIC_RESOLUTION_METHODS)
    except Exception:
        return frozenset({
            "same_file", "import", "import_type", "type_flow", "verified_unique",
            "impl_method", "inherited", "unique_method", "return_type", "lsp", "lsp_verified",
        })


def gate1_dominance_gap(graph_db: str) -> int:
    """GATE 1 pred_B's RAW dominance gap on this graph: name_match% CALLS edges minus
    deterministic CALLS edges over ALL CALLS (det via the unified set, name_match via
    LIKE 'name_match%') — i.e. pred_B_nondominance_RAW, the upper bound on the delivered
    decision (gate_resolution now decides pred_B on the delivered non-test/conf>=0.5
    partition; this budget stays raw to never under-provision — see the note above the
    constants). >=0; 0 on a det-dominant or unreadable graph (fail-safe — the floor
    budget still runs the pass)."""
    try:
        import sqlite3
        det_set = sorted(_gate1_det_set())
        ph = ",".join("?" for _ in det_set)
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        det = c.execute(
            f"SELECT count(*) FROM edges WHERE type='CALLS' AND resolution_method IN ({ph})",
            det_set,
        ).fetchone()[0]
        nm = c.execute(
            "SELECT count(*) FROM edges WHERE type='CALLS' "
            "AND resolution_method LIKE 'name_match%'"
        ).fetchone()[0]
        c.close()
        return max(0, int(nm) - int(det))
    except Exception:
        return 0


def compute_lsp_max_edges(graph_db: str, *, scoped: bool, env=None) -> int:
    """Dynamic --max-edges for the LSP residual pass.

    ``min(CEILING, max(floor, ceil(gap * (1 + HEADROOM))))`` where ``gap`` is GATE 1's
    dominance gap on this graph and ``floor`` preserves the historical budgets (500
    un-scoped / 20000 issue-scoped) — the dynamic computation may only RAISE budgets,
    never shrink a pass that worked before. ``GT_LSP_MAX_EDGES`` (positive int) is an
    explicit operator override and wins outright; invalid values are ignored. Pure +
    deterministic for the tests; never raises."""
    env = os.environ if env is None else env
    override = str(env.get("GT_LSP_MAX_EDGES", "") or "").strip()
    if override:
        try:
            v = int(override)
            if v > 0:
                return v
        except ValueError:
            pass
    import math
    gap = gate1_dominance_gap(graph_db)
    floor = LSP_MAX_EDGES_SCOPED_FLOOR if scoped else LSP_MAX_EDGES_FLOOR
    dynamic = int(math.ceil(gap * (1.0 + LSP_GAP_HEADROOM)))
    return min(LSP_MAX_EDGES_CEILING, max(floor, dynamic))


def _canonical_cert_source(out_dir: str, declared_lang, dominant_lang) -> tuple[str, str]:
    """(source_cert_path, chosen_lang) for the canonical LSP cert copy (G4).

    Prefer the DECLARED task language's per-language cert when it exists on disk;
    fall back to the graph-DOMINANT language only when the declared language has no
    cert. The old code always copied ``langs[0]`` (graph-dominant), so on a python
    task whose graph is vendored-JS-dominant the canonical cert became the JS no-op
    — masking a real python LSP failure or under-reporting real python work. Pure +
    deterministic for the tests."""
    declared = (str(declared_lang or "")).strip().lower()
    dom = (str(dominant_lang or "")).strip().lower()
    if declared:
        dc = os.path.join(out_dir, f"lsp_certificate_{declared}.json")
        if os.path.exists(dc):
            return dc, declared
    return os.path.join(out_dir, f"lsp_certificate_{dom}.json"), dom


def _finalize_after_lsp_hash(graph_db: str, cert_lsp: str):
    """WHOLE-GRAPH FINALIZE — refresh ``graph_hash_after_lsp`` in the canonical LSP cert to
    the hash of the FINAL graph, after ALL per-language LSP passes.

    ``graph_hash_after_lsp`` is a whole-graph invariant (the graph AFTER every language's LSP
    pass). But ``resolve.py`` snapshots it PER LANGUAGE (right after that one language's closure
    rebuild), and ``_canonical_cert_source`` keys the canonical cert on the DECLARED language —
    which need NOT be the LAST language to mutate the shared ``graph.db``. When a non-declared
    language resolves AFTER the declared one (measured: katex declared=js [corrected 0, deleted 1]
    then typescript [verified 26, corrected 12] rebuilt closure 2555->2852), the canonical cert
    froze a STALE mid-pipeline hash. The runtime hook (``gt_agent._emit_gt_meta_witness``) and
    ``graph_certificate`` then compare that stale authority against the FINAL graph and FALSE-FAIL
    ``GRAPH_FAIL_HASH_MISMATCH`` — charged failure_class=GT — on a perfectly good (more-complete)
    graph. Re-snapshotting the authority ONCE over the final ``graph.db`` fixes it.

    Uses the SAME edge hash the witness + graph_certificate use, so all three agree by
    construction. GENERALIZED (any language count/order, zero task/lang keys). DETERMINISTIC (the
    final edge set is order-independent — languages resolve disjoint, extension-partitioned files;
    only the snapshot moment differed). STRICT NO-OP (cert left byte-identical) for single-language
    repos or when the declared language WAS the last to mutate. Still lets the witness catch REAL
    handoff divergence (the hook consuming a different/corrupt db than the final certified one).

    Returns ``(stale_hash, final_hash)`` when a refresh was written, else ``None`` (no-op).
    """
    try:
        from groundtruth.runtime import proof as _proof_fin
        final = _proof_fin.graph_edges_hash(graph_db)
    except Exception:
        return None
    if not final or not os.path.exists(cert_lsp):
        return None
    try:
        with open(cert_lsp, encoding="utf-8") as fh:
            cc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cc, dict):
        return None
    stale = cc.get("graph_hash_after_lsp")
    if stale == final:
        return None  # already the final graph -> byte-identical no-op (determinism preserved)
    cc["graph_hash_after_lsp"] = final
    cc["closure_hash_after_rebuild"] = final
    cc["graph_hash_after_lsp_refreshed_from"] = stale  # honesty trail
    try:
        with open(cert_lsp, "w", encoding="utf-8") as fh:
            json.dump(cc, fh, indent=2)
    except OSError:
        return None
    return (stale, final)


def _count_lsp_stamped_edges(graph_db: str) -> int:
    """COUNT of edges the resolver stamped as LSP-resolved. For the G4 inverse
    stamp check only (graph has lsp edges but the canonical cert reports no-op)."""
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        n = c.execute(
            "SELECT count(*) FROM edges WHERE resolution_method IN ('lsp','lsp_verified')"
        ).fetchone()[0]
        c.close()
        return int(n or 0)
    except Exception:
        return 0


def lsp_ready_budget_seconds(language: str, env=None) -> int:
    """Per-language LSP readiness budget — SOURCED FROM resolve._READY_BUDGET_S_BY_SERVER
    (the single source of truth), NOT a second, contradictory table.

    LSP4 (Fable): this used to hardcode go=30 / rust=45 / else=20 and then EXPORT it as
    ``GT_LSP_READY_BUDGET_S``, which OVERRIDES resolve.py's per-server budgets (rust-analyzer=180,
    jdtls=180, gopls=60, typescript-language-server=150). rust/java/ts were thereby
    under-provisioned to 45/20/20s — the servers never finished indexing → 0 edges converted,
    the exact 0-conversion failure resolve.py's per-server table was tuned (with live witnesses:
    boa-*, drizzle-orm) to PREVENT. There must be ONE table: map language → server binary
    (resolve._KNOWN_SERVERS) → resolve._READY_BUDGET_S_BY_SERVER. Raising a budget never slows a
    warm server (the readiness barrier early-exits on the first real answer); it only lets a slow
    indexer finish.

    Precedence unchanged: ``GT_LSP_READY_BUDGET_S_OVERRIDE`` still wins if set. The per-run env
    ``GT_LSP_READY_BUDGET_S`` remains the concrete value consumed by ``groundtruth.resolve``.
    """
    env = os.environ if env is None else env
    override = str(env.get("GT_LSP_READY_BUDGET_S_OVERRIDE", "") or "").strip()
    if override:
        try:
            v = int(float(override))
            if v > 0:
                return v
        except ValueError:
            pass
    lang = (language or "").strip().lower()
    # Single source of truth: resolve.py owns the per-server readiness budgets. Map the language
    # to its server binary, then read that table — so the value we export as
    # GT_LSP_READY_BUDGET_S is EXACTLY what resolve.py would pick on its own, never a shorter
    # contradiction.
    try:
        from groundtruth.resolve import (
            _KNOWN_SERVERS,
            _READY_BUDGET_S_BY_SERVER,
            _READY_BUDGET_S_DEFAULT,
        )

        server_bin = os.path.basename(_KNOWN_SERVERS.get(lang, "") or "")
        budget = _READY_BUDGET_S_BY_SERVER.get(server_bin, _READY_BUDGET_S_DEFAULT)
        return int(round(float(budget)))
    except Exception:
        # resolve.py not importable (proof running without the package on PYTHONPATH): fall back
        # to a floor that still EXCEEDS the old under-provisioned values for the slow indexers, so
        # a fallback can never RE-introduce the 0-conversion bug it is meant to fix.
        return {
            "rust": 180, "java": 180, "kotlin": 180,
            "typescript": 150, "javascript": 150, "ts": 150, "js": 150, "tsx": 150, "jsx": 150,
            "go": 60,
        }.get(lang, 20)


def _derive_primary_langs(langs, dom_lang, declared_lang):
    """Fail-close primary scope = the languages that MUST resolve for the proof to be valid.

    The DECLARED task language (dep_store_manifest 'language', else --lang) is ground truth and,
    when known, is the ONLY primary: a vendored/docs corpus in another language is not the task's
    code and its LSP outcome is irrelevant (Fable P0-2 — a python task with 2378 vendored JS nodes
    must not be scoped to javascript, which both FALSE-KILLS on a JS-server crash AND fails OPEN on a
    real python resolve-error, protecting the wrong language). Fall back to the graph-dominant
    langs[0] (+ dom) ONLY when the declared language is unavailable — this subsumes BUG-D
    (`_detect_lang` can default python and drop the real go/rust primary) since the fallback keys off
    the LSP-filtered verdict set. Scoping to the declared language additionally neutralizes the
    omitted-server corner (a vendored java corpus with jdtls omitted is never the DECLARED language,
    so it can never be a fatal INSTALL_MISSING). Pure + deterministic for the tests.

    Fallback precedence when the declared language is unavailable: the LSP-FILTERED verdict-key
    dominant `langs[0]` (reliable), THEN the unfiltered `_detect_lang` dom ONLY if no filtered
    language exists. dom is deliberately NOT unioned with langs[0] — that would re-add the
    unreliable default (`_detect_lang` returns "python" on any exception), the exact BUG-D miss."""
    if declared_lang:
        return {str(declared_lang).lower()}
    if langs:
        return {str(langs[0]).lower()}
    if dom_lang:
        return {str(dom_lang).lower()}
    return set()


def aggregate_lsp_verdicts(lang_verdicts: dict, *, require_lsp: bool, any_success: bool, primary_langs=None):
    """P1-e polyglot aggregation rule over per-language LSP verdicts -> (ok, failures).

    ``failures`` lists 'lang=verdict' for every language whose pass FAILED:
      * LSP_INSTALL_MISSING  — baked-server language, binary missing on PATH;
      * LSP_FAIL_NO_WARM     — server launched (or tried) but never warmed: a
        launched-but-never-warm server is a FAILURE, not a pass;
      * LSP_RESOLVE_ERROR(..)— the resolve pass exited nonzero without a verdict.
    Genuinely-unknown languages (LSP_UNSUPPORTED_EXPLICIT) and the valid verdicts
    (LSP_ACTIVE_VALID / LSP_NO_OP_VALID_WITH_WARM_SERVER) are never failures.

    FIX-A: any LSP_WARN_* verdict (NOT_READY / ZERO_CONVERSION / NOT_ATTEMPTED) is a
    graph-QUALITY shortfall on a LIVE, warm-or-launched server (Go/Rust dep-env
    incomplete offline), NOT a liveness failure. These do NOT match the failure
    predicate below, so a Go/Rust task with a warm-but-unproductive server reaches
    the agent with its tree-sitter graph instead of dying at lsp_pass. Only a
    NEVER-LAUNCHED server (LSP_FAIL_NO_WARM) or a missing binary (LSP_INSTALL_MISSING)
    is a hard fail.

    Under ``require_lsp`` (GT_REQUIRE_LSP=1): ok=False if a PRIMARY-language pass failed —
    a sibling language succeeding must NOT mask the task's OWN language gap — or if no
    language resolved successfully at all. Without the flag, ok=True (verdicts are
    still recorded for the certs/manifest). Pure + deterministic for the tests.

    B-INFRA (2026-07-02): ``primary_langs`` scopes the fail-closed gate to the repo's
    PRIMARY/dominant language(s). An INCIDENTAL language (a handful of files, not the
    task's own language) whose server this substrate does not ship — e.g. a TS repo
    (tutanota) carrying a few Java files when jdtls is not baked — must NOT fail-close
    the whole task: its primary (TS) LSP resolved, the graph IS enriched, killing it
    is a false INFRA death. So an incidental-language failure is RECORDED but non-fatal;
    only a primary-language failure (the substrate genuinely can't serve the task's own
    language) or NO language resolving at all is fatal. Correct-or-quiet, generalized to
    ANY incidental unsupported language (c/cpp/ruby/...), not a java/jdtls special-case.
    ``primary_langs=None`` keeps the original strict behavior (every failure fatal) so
    the pure unit tests are unchanged."""
    failures = [
        f"{lg}={v}" for lg, v in lang_verdicts.items()
        if v in ("LSP_INSTALL_MISSING", "LSP_FAIL_NO_WARM")
        or str(v).startswith("LSP_RESOLVE_ERROR") or str(v).startswith("LSP_FAIL_")
    ]
    if not require_lsp:
        return True, failures
    if primary_langs:
        _prim = {str(lg).lower() for lg in primary_langs}
        fatal = [f for f in failures if f.split("=", 1)[0].lower() in _prim]
    else:
        fatal = failures  # back-compat: no primary info -> original strict gate
    if fatal:
        return False, fatal
    if not any_success:
        # FIX-A COMPLETION (2026-07-02, live go-only INFRA death `abs-module-cache-flags`):
        # a PRIMARY language whose server is warm-but-unproductive (LSP_WARN_*: warmed +
        # project_ready, but the tiny scoped residual had nothing convertible offline —
        # gopls w/o module cache, rust-analyzer w/o cargo) is LIVE per FIX-A and MUST reach
        # the agent with its tree-sitter graph. The resolve subprocess exits nonzero on that
        # verdict, so any_success (= rc==0) is False; without this a single-primary go/rust
        # repo fail-closes at lsp_pass despite a demonstrably warm server — the exact defeat
        # of FIX-A's own intent (lines above). A warm WARN on a primary language counts live;
        # a NEVER-warm/missing/error server is already in `fatal` and returned above.
        _prim = {str(p).lower() for p in primary_langs} if primary_langs else None
        _warm_primary = [
            lg for lg, v in lang_verdicts.items()
            if str(v).startswith("LSP_WARN_") and (_prim is None or str(lg).lower() in _prim)
        ]
        if not _warm_primary:
            return False, ["<none>=NO_LANGUAGE_RESOLVED"]
    return True, failures  # warm-primary WARN or a real success -> live; incidental recorded


# Sidecar artifacts the brief-gen step writes to its cwd (/tmp fallback) that must be
# MIRRORED into out_dir so they cross the cert-dir handoff to the agent. Enumerated so a
# new sidecar can't be silently forgotten (the 2026-07-08 GT_OBLIGATIONS_V2 witness gap:
# the T2 files were written but never mirrored, so T2/T3 never reached the agent).
_CERT_SIDECARS = ("gt_issue_anchors.json", "gt_obligations_v2.json", "gt_obligations.md")


class SidecarIdentityError(ValueError):
    """A cert sidecar is not bound to the issue being proved."""


def _mirror_cert_sidecars(
    out_dir: str,
    *,
    expected_issue_sha256: str,
    src_dir: str = "/tmp",
) -> list[str]:
    """Mirror only sidecars bound to the expected issue.

    All present sources are validated before any destination is changed. The
    obligations JSON binds the exact Markdown checklist bytes by digest. Missing
    inputs are a flag-off no-op; partial, malformed, stale, or mismatched bundles
    fail closed. Each destination file is published by atomic replacement after
    validation; this function does not claim a multi-file atomic filesystem commit.
    """
    if (
        len(expected_issue_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_issue_sha256)
    ):
        raise SidecarIdentityError("expected issue identity is not lowercase SHA-256")
    paths = {name: os.path.join(src_dir, name) for name in _CERT_SIDECARS}
    present = {name for name, path in paths.items() if os.path.isfile(path)}
    if not present:
        return []

    raw: dict[str, bytes] = {}
    for name in present:
        try:
            with open(paths[name], "rb") as fh:
                raw[name] = fh.read()
        except OSError as exc:
            raise SidecarIdentityError(f"cannot read cert sidecar {name}: {exc}") from exc

    parsed: dict[str, dict] = {}
    for name in ("gt_issue_anchors.json", "gt_obligations_v2.json"):
        if name not in present:
            continue
        try:
            value = json.loads(raw[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SidecarIdentityError(f"malformed cert sidecar {name}") from exc
        if not isinstance(value, dict):
            raise SidecarIdentityError(f"cert sidecar {name} is not a JSON object")
        if value.get("issue_sha256") != expected_issue_sha256:
            raise SidecarIdentityError(f"cert sidecar {name} has wrong issue identity")
        parsed[name] = value

    obligation_names = {"gt_obligations_v2.json", "gt_obligations.md"}
    obligation_present = present & obligation_names
    if obligation_present and obligation_present != obligation_names:
        raise SidecarIdentityError("incomplete obligations sidecar bundle")
    if obligation_present:
        manifest = parsed["gt_obligations_v2.json"]
        if manifest.get("obligations_version") != 2:
            raise SidecarIdentityError("unsupported obligations sidecar version")
        actual = hashlib.sha256(raw["gt_obligations.md"]).hexdigest()
        if manifest.get("checklist_sha256") != actual:
            raise SidecarIdentityError("obligations checklist digest mismatch")

    mirrored: list[str] = []
    staged: list[tuple[str, str]] = []
    try:
        for name in _CERT_SIDECARS:
            if name not in present:
                continue
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=out_dir)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw[name])
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            staged.append((tmp, os.path.join(out_dir, name)))
        for tmp, destination in staged:
            os.replace(tmp, destination)
            mirrored.append(os.path.basename(destination))
        return mirrored
    finally:
        for tmp, _destination in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _degrade_empty_brief(out_dir: str, detail: str):
    """Shared GT-QUALITY degrade path for emit_brief (gates.md BETTER-GATES §1).

    A GT-QUALITY brief issue (empty/thin brief, determinism, localization diagnostic) must
    DEGRADE, not kill the paid task: the agent self-localizes and runs GT-off-for-this-task.
    Write a 0-byte brief.txt FIRST so build_run_manifest + the artifact-presence loop + every
    downstream identity/PROOF_STATE gate has its artifact and the run reaches the agent, then
    return (ok=False, detail, degraded=True). If the placeholder write ITSELF raises OSError
    that is a genuine artifact-IO failure (not GT quality) → (False, io_detail, False), and the
    caller hard-fails with GT_ARTIFACT_MISSING."""
    try:
        with open(os.path.join(out_dir, "brief.txt"), "w", encoding="utf-8") as bf:
            bf.write("")
    except OSError as e:
        return False, f"brief.txt write failed (degrade path): {e}", False
    return False, detail, True


def emit_brief(out_dir: str, issue_text: str, work: str, graph: str, *, generator=None):
    """Emit the curated brief to <out>/brief.txt — proof artifact #8 (P0.1-c).

    gt-run-proof is PROOF-ONLY (validate_proof_env requires GT_PROOF_MODE=1), and the agent
    consumes /gt_artifacts/brief.txt READ-ONLY: there is NO host fallback (host run_v74 is
    fail-closed by the container boundary). A GT-QUALITY brief issue (empty/thin brief,
    determinism, localization diagnostic) DEGRADES rather than hard-failing the paid task —
    the agent self-localizes and runs GT-off-for-this-task (gates.md BETTER-GATES §1).
    Returns (ok, detail, degraded): a GT-QUALITY issue writes an EMPTY brief.txt and returns
    (False, detail, True) so the caller RECORDS + CONTINUES (build_run_manifest + all
    downstream identity/artifact-presence/PROOF_STATE gates still run); only VALIDITY (sidecar
    identity) or genuine artifact IO (brief.txt write) returns (False, detail, False) and the
    caller fails closed with GT_ARTIFACT_MISSING; success returns (True, detail, False).
    ``generator`` is injectable for tests; default = the real
    generate_v1r_brief (which also writes the issue anchors mirrored below)."""
    # gate3b persists the authoritative V1RBriefResult. Load it, independently
    # execute the same acquisition once, and attach a repeat witness only when
    # canonical identities match. The second result never replaces gate bytes.
    try:
        from groundtruth.runtime.brief_cache import (
            capture_request_identity,
            get_or_generate,
            validate_request_identity,
            verify_independent_generation,
        )
        _identity_handoff = capture_request_identity(issue_text, graph)
        result = get_or_generate(
            out_dir, issue_text, work, graph, generator=generator,
            identity_handoff=_identity_handoff,
        )
        _reused_gate = not result.get("generated", True)
        bt = (result.get("brief_text") or "").strip()
    except Exception as e:
        return _degrade_empty_brief(
            out_dir, f"brief generation raised (no swallow in proof): {type(e).__name__}: {e}")
    if not bt:
        return _degrade_empty_brief(out_dir, (
            "portable brief EMPTY — proof mode requires a non-empty brief.txt "
            "(the agent consumes /gt_artifacts/brief.txt; there is no host fallback)"))
    # The gate/cache acquisition above is the sole delivery authority. Its generator writes
    # support sidecars into the configured source (default /tmp), so capture those exact
    # bytes BEFORE the independent witness executes. Execution 2 proves repeat identity only;
    # it may write divergent sidecars and must never replace model-visible/support artifacts.
    try:
        _mirror_cert_sidecars(
            out_dir,
            expected_issue_sha256=hashlib.sha256(issue_text.encode("utf-8")).hexdigest(),
            src_dir=os.environ.get("GT_CERT_SIDECAR_SOURCE_DIR", "/tmp"),
        )
    except (SidecarIdentityError, OSError) as e:
        # VALIDITY/integrity failure (not GT-quality) → hard-fail, no degrade.
        return False, f"brief sidecar identity validation failed: {e}", False
    try:
        if generator is None:
            from groundtruth.pretask.v1r_brief import generate_v1r_brief as _generator
        else:
            _generator = generator

        def _independent_generation():
            return _generator(
                issue_text=issue_text,
                repo_root=work,
                graph_db=graph,
                bug_id="portable",
            )

        _determinism = verify_independent_generation(
            out_dir,
            result,
            _independent_generation,
            expect_identity=_identity_handoff.value,
        )
        if _determinism.get("matched") is not True:
            return _degrade_empty_brief(out_dir, (
                "DETERMINISM_MISMATCH: independent same-input brief acquisition "
                "produced different canonical identities"
            ))
        result = get_or_generate(
            out_dir, issue_text, work, graph, generator=generator,
            identity_handoff=_identity_handoff,
        )
        bt = (result.get("brief_text") or "").strip()
        validate_request_identity(_identity_handoff)
    except Exception as e:
        return _degrade_empty_brief(
            out_dir, f"brief determinism proof raised: {type(e).__name__}: {e}")
    try:
        from groundtruth.runtime.localization_diagnostic import validate_brief_payload

        _gold_files: list[str] = []
        _full_potential = os.environ.get("GT_FULL_POTENTIAL", "0") == "1"
        _strict_diag = (
            _full_potential
            or os.environ.get("GT_LOCALIZATION_DIAGNOSTIC_STRICT", "0") == "1"
        )
        _require_semantic = (
            _full_potential
            or os.environ.get("GT_LOCALIZATION_REQUIRE_SEMANTIC", "0") == "1"
        )
        _require_gold = (
            bool(_gold_files)
            and (
                _full_potential
                or os.environ.get("GT_LOCALIZATION_REQUIRE_GOLD", "0") == "1"
            )
        )
        _diag = validate_brief_payload(
            result,
            gold_files=_gold_files,
            require_gold=_require_gold,
            require_semantic=_require_semantic,
        )
        with open(os.path.join(out_dir, "localization_diagnostic.json"), "w", encoding="utf-8") as _df:
            json.dump(_diag, _df, indent=2, sort_keys=True)
        if _strict_diag and not _diag.get("ok", False):
            return _degrade_empty_brief(out_dir, (
                "live localization diagnostic HALT before brief delivery: "
                + ",".join(str(v) for v in _diag.get("violations", []))
            ))
    except Exception as e:
        if (
            os.environ.get("GT_LOCALIZATION_DIAGNOSTIC_STRICT", "0") == "1"
            or os.environ.get("GT_FULL_POTENTIAL", "0") == "1"
        ):
            return _degrade_empty_brief(
                out_dir, f"live localization diagnostic raised: {type(e).__name__}: {e}")
        try:
            with open(os.path.join(out_dir, "localization_diagnostic_error.txt"), "w", encoding="utf-8") as _ef:
                _ef.write(f"{type(e).__name__}: {e}")
        except OSError:
            pass
    try:
        with open(os.path.join(out_dir, "brief.txt"), "w", encoding="utf-8") as bf:
            bf.write(bt)
    except OSError as e:
        # Genuine artifact IO failure (not GT-quality) → hard-fail, no degrade.
        return False, f"brief.txt write failed: {e}", False
    _sha = result.get("brief_sha256", "")
    _reused = _reused_gate
    return True, (
        f"{len(bt)} chars sha256={_sha[:12]} reused_gate_brief={_reused} "
        "determinism_matched=True"
    ), False


def probe_workspace_metadata(language: str, source_root: str, env: dict[str, str]) -> dict[str, object]:
    """Probe offline workspace/package metadata for languages whose LSPs depend on it.

    This is product truth for Go/Rust readiness: dep-store presence is only evidence.
    The actual question is whether the substrate can load workspace metadata offline.
    """
    lang = (language or "").strip().lower()
    if lang not in {"go", "rust"}:
        return {
            "applicable": False,
            "language": lang,
            "status": "skip",
            "reason": "language_not_metadata_bound",
        }

    if lang == "go":
        # -e: don't fail on build-constraint errors (syscall/js, platform-specific).
        # OFFLINE by default: the sealed proof substrate ships GOPROXY=off + GOTOOLCHAIN=local
        # (docker/Dockerfile.gt-substrate) and the module cache is mounted from the dep store —
        # a proof run must NOT reach the network (the "no per-task download" contract). We keep
        # GOFLAGS=-mod=mod (use the cache, don't require a complete go.sum) but force GOPROXY=off
        # so a missing transitive dep degrades to a diagnostic WARN, never a network fetch. The
        # network proxy is used ONLY when an operator explicitly opts in via
        # GT_PROOF_ALLOW_NETWORK=1 (e.g. a non-sealed dev probe).
        cmd = ["go", "list", "-e", "./..."]
        _goproxy = ("https://proxy.golang.org,direct"
                    if os.environ.get("GT_PROOF_ALLOW_NETWORK") == "1" else "off")
        env = dict(env, GOFLAGS="-mod=mod", GOPROXY=_goproxy)
        code = "GO_WORKSPACE_METADATA_FAIL"
    else:
        cmd = ["cargo", "metadata", "--format-version=1", "--no-deps"]
        code = "RUST_WORKSPACE_METADATA_FAIL"

    try:
        cp = subprocess.run(
            cmd,
            cwd=source_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as e:
        return {
            "applicable": True,
            "language": lang,
            "status": "fail",
            "code": code,
            "message": f"{lang} workspace metadata probe raised: {type(e).__name__}: {e}",
            "command": cmd,
        }

    stdout = (cp.stdout or "").strip()
    stderr = (cp.stderr or "").strip()
    if cp.returncode != 0:
        msg_bits = [f"{lang} workspace metadata probe failed (rc={cp.returncode})"]
        if stderr:
            msg_bits.append(f"stderr={stderr[:300]}")
        elif stdout:
            msg_bits.append(f"stdout={stdout[:300]}")
        return {
            "applicable": True,
            "language": lang,
            "status": "fail",
            "code": code,
            "message": "; ".join(msg_bits),
            "command": cmd,
            "returncode": cp.returncode,
            "stdout_excerpt": stdout[:300],
            "stderr_excerpt": stderr[:300],
        }

    package_count = 0
    if lang == "go":
        package_count = len([ln for ln in stdout.splitlines() if ln.strip()])

    return {
        "applicable": True,
        "language": lang,
        "status": "ok",
        "command": cmd,
        "returncode": cp.returncode,
        "package_count": package_count,
        "stdout_excerpt": stdout[:300],
        "stderr_excerpt": stderr[:300],
    }


def _strict_broken_gate_signals(gate_report_path: str):
    """Under STRICT (GT_GATES_DELIVER_ALWAYS=0), the GENUINELY-broken foundational signals
    that must fail-close the paid run — EXCLUDING the benign name_match-dominance carve-out.

    A name_match-heavy CALL map (GATE 1 pred_B off) is EXPECTED on JS/TS/Go repos (CLAUDE.md
    Known Limitations #1: 70-80% name_match on large repos) and is DELIBERATELY not a refusal
    (the deliver-always doctrine) — so GATE 1 being OFF is never returned here. Genuinely
    broken = the LSP work was lost after resolve (LSP_STAMP_DROPPED_AFTER_RESOLVE), the LSP is
    dark (GATE 2 OFF: no warm server / missing cert), or the embedder is DEAD (GATE 3a
    present-FAIL — semantic ranking cannot run). WEAK per-issue semantic consumption (3a ON,
    3b OFF) is DELIVERABLE (measurement), matching the deliver-always split in
    foundational_gates.main.

    Returns the list of broken-signal strings (empty == deliverable), or None when the report
    cannot be read (caller falls back to the raw gate rc — conservative STRICT posture)."""
    try:
        with open(gate_report_path, encoding="utf-8") as fh:
            rep = json.load(fh)
    except Exception:
        return None
    broken: list[str] = []
    verdict = (rep.get("verdict") or {})
    # B-1: fail-close ONLY on the GENUINE catastrophic-under-resolution signal — pred_A
    # (det% below the SAFETY floor: method calls never left name_match) — NOT on the benign
    # name_match-DOMINANCE carve-out (pred_B: name_match > det), which CLAUDE.md documents as
    # the EXPECTED state of large JS/TS/Go repos (Known Limitation #1) and which the
    # deliver-always doctrine treats as deliverable. resolution_jarvis = pred_A AND pred_B, so
    # keying on it here refused big repos at proof — the exact scale class GT targets (this
    # function's own docstring says GATE-1-OFF must never be returned here). Read the granular
    # pred_A from gate_resolution; fall back to the combined signal only when the detail is
    # absent (older report → conservative STRICT posture).
    gres = (rep.get("gate_resolution") or {})
    if gres.get("pred_A_det_floor") is False:
        broken.append("RESOLUTION_GRAPH_FAIL (GATE 1 pred_A: det% below SAFETY floor — catastrophic under-resolution)")
    elif "pred_A_det_floor" not in gres and verdict.get("resolution_jarvis") is False:
        broken.append("RESOLUTION_GRAPH_FAIL (GATE 1 resolution OFF; granular pred_A unavailable)")
    lp = (rep.get("gate_lsp") or {})
    if lp.get("stamp_mismatch"):
        broken.append(f"LSP_STAMP_DROPPED_AFTER_RESOLVE ({lp.get('stamp_mismatch')})")
    if verdict.get("lsp_enrichment") is False:
        broken.append("LSP_DARK (GATE 2 LSP-liveness OFF)")
    present = ((rep.get("gate_embedder") or {}).get("present") or {})
    if present.get("pass") is False:
        broken.append("EMBEDDER_DEAD (GATE 3a present-FAIL)")
    return broken


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
    tracker = _ProofTracker(a.out)

    # Boundary + baked-deps + flags. A host run / missing baked dep fails-closed here.
    violations = validate_proof_env()
    if violations:
        return tracker.fail(
            "env_validation",
            "SUBSTRATE_NOT_PORTABLE",
            "FINAL_PIPELINE_HOST_SPLIT_FAIL / SUBSTRATE_NOT_PORTABLE: " + "; ".join(violations),
        )
    try:
        sys.path.insert(0, os.path.join(GT_HOME, "src"))  # package lives at $GT_HOME/src
        sys.path.insert(0, GT_HOME)
        from groundtruth.runtime.context import assert_container_boundary
        assert_container_boundary("gt-run-proof")
    except Exception as e:
        return tracker.fail("env_validation", "FINAL_PIPELINE_HOST_SPLIT_FAIL", str(e))
    # D (2026-06-13): commit-parity gate. Integration runs the BAKED /opt/gt/src, so a
    # substrate built from a different commit than the run claims is a stale-substrate
    # legitimacy violation. Default is RECORD-ONLY (run_manifest.commit_parity carries the
    # status — drift is never silent); GT_REQUIRE_COMMIT_PARITY=1 makes a mismatch fail closed.
    _cp_ok, _cp_detail = assert_commit_parity()
    if not _cp_ok:
        return tracker.fail("env_validation", "GT_COMMIT_PARITY_MISMATCH", _cp_detail)

    # EARLY IDENTITY MANIFEST (2026-07-20, gate redesign — gates.md). The Run-identity gate
    # (R01-R04) validates run_manifest.json's commit_parity + substrate_build_commit — the
    # IDENTITY binding, which is VALID regardless of GT quality. Historically the manifest was
    # written only at the END (build_run_manifest, ~line 1919), AFTER the GT-QUALITY gates
    # (index/graph_cert/embedder/emit_brief); a quality-fail returned before it, so the manifest
    # was MISSING and the identity gate cascaded to IDENTITY_CONTENT_INVALID (a SYMPTOM, not a
    # real identity fault). Write the identity binding NOW — right after the legitimacy/commit-
    # parity checks, before any GT-QUALITY gate — so it SURVIVES a quality-fail. The full manifest
    # OVERWRITES this on success; on a quality-degraded run this minimal identity manifest persists
    # so validity is still strictly enforced while GT-quality no longer destroys the binding. NEW
    # RULE: identity/validity is always recorded (may refuse); GT-quality is recorded, never gates.
    try:
        with open(os.path.join(a.out, "run_manifest.json"), "w", encoding="utf-8") as _idf:
            json.dump({
                "schema": "gt.run_manifest.v2",
                "gt_git_commit": _gt_git_commit(),
                "substrate_build_commit": _env_or_none("GT_SUBSTRATE_BUILD_COMMIT"),
                "commit_parity": commit_parity_status(),
                "identity_manifest_early": True,
            }, _idf, indent=2)
    except Exception:  # noqa: BLE001 — never let identity-manifest IO abort the proof
        pass

    # Fail-closed dead-surface guard (runtime teeth for dead_path_registry). On the proof/
    # substrate path a retired DEAD_PATHS module must NEVER be loaded — if one is in
    # sys.modules here (before indexing), abort naming the dead module + its live replacement.
    # No-op outside proof mode, so OH/MCP/CLI harnesses are unaffected. sys.path already has
    # $GT_HOME/src (set above), so the registry import resolves to the baked live src.
    try:
        from groundtruth.runtime.dead_path_registry import assert_no_dead_surface_loaded
        assert_no_dead_surface_loaded()
    except Exception as e:
        return tracker.fail("env_validation", "GT_DEAD_SURFACE_LOADED", str(e))
    tracker.complete("env_validation")

    dep_manifest = os.path.join(a.out, "dep_store_manifest.json")
    proof_language = (a.lang or "").strip().lower()
    if os.path.exists(dep_manifest):
        try:
            with open(dep_manifest, encoding="utf-8") as fh:
                dm = json.load(fh)
            proof_language = ((dm.get("language") or proof_language or "")).strip().lower()
            try:
                import dep_store_manifest as _dsm
            except ImportError:
                sys.path.insert(0, os.path.join(GT_HOME, "scripts", "swebench"))
                import dep_store_manifest as _dsm
            dep_problems = _dsm.validate_manifest(dm)
            if dep_problems:
                return tracker.fail(
                    "dep_store",
                    "DEP_STORE_EMPTY",
                    dep_problems[0],
                    language=dm.get("language"),
                    manifest=dep_manifest,
                )
            tracker.complete("dep_store", manifest=dep_manifest)
        except Exception as e:
            return tracker.fail("dep_store", "DEP_STORE_MANIFEST_READ_FAIL", str(e))
    else:
        tracker.complete("dep_store", manifest="absent")

    # Separation of concerns (anti-cheat): GT is the HELPER, never the evaluator. It must never see
    # the evaluator's hidden tests or gold. Fail-closed if any eval artifact leaked in via env/file.
    leaks = eval_leakage(a.source_root)
    if leaks:
        return tracker.fail(
            "env_validation",
            "EVAL_LEAKAGE_FORBIDDEN",
            "GT (substrate) must never receive the evaluator's hidden tests/gold/FAIL_TO_PASS; "
            "separation breached by: " + ", ".join(leaks),
        )

    # The task repo is mounted READ-ONLY at --source-root; copy to a writable workdir so gt-index
    # never mutates the official task image's source.
    work = "/tmp/gt_work_src"
    shutil.rmtree(work, ignore_errors=True)
    try:
        shutil.copytree(a.source_root, work, symlinks=True, ignore_dangling_symlinks=True)
    except Exception as e:
        return tracker.fail("source_copy", "SOURCE_COPY_FAIL", str(e))
    tracker.complete("source_copy", workdir=work)

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
    base_env = clean_env_projection(os.environ)
    base_env.update({"PYTHONPATH": _gt_paths + (os.pathsep + _pp if _pp else ""), "GT_HOME": GT_HOME,
                     "GT_MODELS_ROOT": os.environ.get("GT_MODELS_ROOT", os.path.join(GT_HOME, "models")),
                     "GT_SOURCE_ROOT": work, "GT_GRAPH_DB": graph,
                     "GT_LSP_CERT": cert_lsp, "GT_GRAPH_CERT": cert_graph, "GT_EMBEDDER_CERT": cert_emb})

    metadata_probe = probe_workspace_metadata(proof_language, work, base_env)
    if metadata_probe.get("applicable"):
        if metadata_probe.get("status") != "ok":
            # FIX-A/RC-4: the Go/Rust-only workspace-metadata probe is DIAGNOSTIC,
            # not a hard gate. A `go list` / `cargo metadata` failure offline is a
            # dep-env limitation (the same one that makes the LSP pass WARN), not a
            # substrate defect — it must NOT kill the task before the agent runs.
            # Record it as a completed stage with a warn status so the tree-sitter
            # graph + brief still reach the agent (consistent with the LSP-pass
            # WARN-not-fail doctrine). Python never hits this gate; Go/Rust no longer
            # die on it.
            tracker.complete(
                "workspace_metadata",
                language=proof_language,
                status_detail="warn",
                code=str(metadata_probe.get("code") or "WORKSPACE_METADATA_WARN"),
                message=str(metadata_probe.get("message") or "workspace metadata probe non-ok (dep-env incomplete offline)"),
                command=metadata_probe.get("command"),
                returncode=metadata_probe.get("returncode"),
            )
        else:
            tracker.complete(
                "workspace_metadata",
                language=proof_language,
                command=metadata_probe.get("command"),
                package_count=metadata_probe.get("package_count"),
            )
    else:
        tracker.complete(
            "workspace_metadata",
            language=proof_language or None,
            skipped_reason=metadata_probe.get("reason"),
        )

    # 1. graph build — RETRY + VERIFY (industrial hardening). A transient index failure
    # (OOM, disk, flaky parse) or a silently-thin graph must NOT sink the task on the first
    # try the way a single fail-on-rc did. Retry up to N, VERIFY nodes>0 (a 0-node graph is
    # a failure even at rc=0), LOG every attempt + its cause, wipe the partial db between
    # tries so a corrupt WAL can't poison the retry, and fail closed with a clear diagnostic.
    # gt-index itself now fail-closes loud on 0-files/0-nodes/parse-rate, so rc!=0 carries the
    # cause; this loop adds the recover-and-identify layer around it. (FTS5 enforced in-index
    # under GT_REQUIRE_FTS5.)
    _gt_index_retries = int(os.environ.get("GT_INDEX_RETRIES", "3") or "3")
    _index_ok = False
    _last = ""
    _rc, _nodes = None, 0  # last-attempt outcome (defined even if retries==0) for the classify below
    for _attempt in range(1, _gt_index_retries + 1):
        _rc = _run([_gt_index_bin(), "-root", work, "-output", graph], base_env)
        _nodes = 0
        if _rc == 0 and os.path.exists(graph):
            try:
                import sqlite3 as _sq
                _c = _sq.connect(graph)
                _nodes = _c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                _c.close()
            except Exception as _e:  # noqa: BLE001
                _last = f"graph verify error: {_e}"
        if _rc == 0 and _nodes > 0:
            _index_ok = True
            if _attempt > 1:
                print(f"[gt-index] recovered on attempt {_attempt}/{_gt_index_retries} ({_nodes} nodes)", flush=True)
            break
        _last = f"attempt {_attempt}/{_gt_index_retries}: rc={_rc} nodes={_nodes}" + (f" ({_last})" if _last else "")
        _tag = "WARN" if _attempt < _gt_index_retries else "FAIL"
        print(f"[gt-index][{_tag}] {_last}", flush=True)
        if _attempt < _gt_index_retries:  # wipe partial db BETWEEN retries so the next try is clean;
            for _ext in ("", "-shm", "-wal"):  # KEEP the final attempt's db so a clean rc==0/0-node
                try:                            # empty graph survives for the degrade path below
                    os.remove(graph + _ext)     # (REQUIRED_ARTIFACTS presence + build_run_manifest need it)
                except OSError:
                    pass
    if not _index_ok:
        # INFRA-vs-QUALITY classification (gates.md PHASE-3). A CLEAN EMPTY graph — gt-index ran to
        # completion (rc==0) but genuinely found nothing to index (0 nodes; _index_ok requires
        # rc==0 AND nodes>0, so `not _index_ok` with rc==0 means nodes==0) — is GT-QUALITY, not a
        # substrate break: GT simply has no map for this repo, so the agent self-localizes and runs
        # GT-off-for-this-task. DEGRADE + CONTINUE (record, never block); the final attempt's
        # schema-correct empty graph.db was spared the wipe so downstream stages + the presence gate
        # + the manifest all have it. OOM (rc 137/-9 SIGKILL), disk-full, or ANY other nonzero
        # gt-index exit is INFRA (retry-class, environmental — says nothing about GT) and STAYS
        # fail-closed exactly as before.
        _empty_graph = (_rc == 0 and _nodes == 0)
        if not _empty_graph:
            _oom = _rc in (137, -9)
            return tracker.fail(
                "index", "GT_INDEX_FAIL",
                f"gt-index failed after {_gt_index_retries} attempts "
                f"({'OOM/SIGKILL' if _oom else 'INFRA disk/missing-binary/crash'}, rc={_rc}): {_last}")
        tracker.complete("index", graph_db=graph, status_detail="degraded",
                         degraded_reason="GT_INDEX_EMPTY_GRAPH", detail=_last)
        print(f"[gt-index][DEGRADED] empty graph (rc=0, 0 nodes) after {_gt_index_retries} "
              f"attempts -> GT-off-for-this-task; continuing to build_run_manifest ({_last})",
              flush=True)
    else:
        tracker.complete("index", graph_db=graph)
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
    # Deliverable-path filter (parity with the brief's scope rendering): a vendored/
    # test/demo fixture (examples/qunit.js, third_party/, node_modules/) is neither an
    # edit target nor worth LSP budget — drop it from the delivered scope artifact AND
    # the demand-scoped LSP pass. Correct-or-quiet, fail-safe. (js-Consistency hygiene.)
    try:
        from groundtruth.delivery.path_policy import (
            is_test_or_demo as _itd,
            is_vendored_path as _ivp,
        )
        scope_files = [f for f in scope_files if not _itd(f) and not _ivp(f)]
    except Exception:
        pass
    scope_path = ""
    if scope_files:
        scope_path = os.path.join(a.out, "gt_scope_files.txt")
        with open(scope_path, "w", encoding="utf-8") as _sf:
            _sf.write("\n".join(scope_files))
    # Dynamic --max-edges (fix 27249519544-a): the old fixed un-scoped 500 was structurally
    # below GATE 1's dominance gap (pred_B needs 2*Promoted + Deleted > gap; dynamodb-toolbox
    # gap=669). Budget = gap + 30% headroom, floor-preserving (500 un-scoped / 20000 scoped),
    # ceiling-capped at 20000 (worst case ≈ 14 min/lang at the measured ~24 edges/s),
    # GT_LSP_MAX_EDGES env-overridable. See compute_lsp_max_edges.
    _gate1_gap = gate1_dominance_gap(graph)
    max_edges = str(compute_lsp_max_edges(graph, scoped=bool(scope_files)))
    print(f"[gt-run-proof] LSP attempt budget: gate1_dominance_gap={_gate1_gap} "
          f"(name_match - deterministic, CALLS), scoped={bool(scope_files)}, "
          f"headroom={LSP_GAP_HEADROOM:.2f} -> --max-edges {max_edges}"
          + (" (GT_LSP_MAX_EDGES override)"
             if (os.environ.get("GT_LSP_MAX_EDGES") or "").strip() else ""),
          flush=True)
    # Capture resolve's stdout (the LSP_METRICS contract line) into GT_LSP_METRICS_FILE so the
    # foundational LSP gate can read residual/scoped — previously uncaptured -> gate read resolved=0
    # while the graph + cert held the real count (the measurement half of the stamp discrepancy).
    lsp_metrics_file = os.path.join(a.out, "gt_lsp_metrics.txt")
    base_env["GT_LSP_METRICS_FILE"] = lsp_metrics_file
    open(lsp_metrics_file, "w").close()
    import re as _re
    lsp_ok = False
    lsp_ready_budgets: dict[str, int] = {}
    lang_verdicts: dict = {}  # per-language verdict (aggregated, none overwritten)
    # TIER-2 (multi-language build perf): defer each per-language closure rebuild to ONE
    # orchestrator-owned rebuild after the loop. The transitive-closure sidecar is a whole-graph
    # property over the FINAL edge set — rebuilding it once per language wastes K-1 rebuilds (only
    # the last survives; it is NOT part of graph_edges_hash). Single-language repos do NOT defer
    # (no env var → resolve.py rebuilds inline + asserts, byte-identical to before).
    _multi_lang_closure_defer = len(langs) >= 2
    _total_effective_lsp = 0  # Σ verified+corrected+deleted across langs → drives the one rebuild
    for lg in reversed(langs):  # least-common first, dominant last
        # Per-language certificate path: NO overwrite — every language's cert persists.
        cert_lsp_lang = os.path.join(a.out, f"lsp_certificate_{lg}.json")
        budget_s = lsp_ready_budget_seconds(lg, base_env)
        lsp_ready_budgets[lg] = budget_s
        lang_env = dict(
            base_env,
            GT_LSP_CERT=cert_lsp_lang,
            GT_LSP_READY_BUDGET_S=str(budget_s),
        )
        if _multi_lang_closure_defer:
            lang_env["GT_DEFER_CLOSURE_REBUILD"] = "1"
        cmd = [sys.executable, "-m", "groundtruth.resolve", "--db", graph, "--root", work,
               "--resolve", "--lang", lg, "--max-edges", max_edges]
        if scope_path:
            cmd += ["--source-files", scope_path]
        print(
            f"[gt-run-proof] LSP ready budget for {lg}: {budget_s}s "
            "(owned by gt-run-proof; override via GT_LSP_READY_BUDGET_S_OVERRIDE)",
            flush=True,
        )
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
        # TIER-2: tally this language's edge mutations (from its own cert) so the single
        # post-loop rebuild fires iff ANY language changed edges — mirroring resolve.py's
        # per-pass "rebuild iff changed, else stamp" exactly, once instead of K times.
        if _multi_lang_closure_defer and os.path.exists(cert_lsp_lang):
            try:
                _lc_l = json.load(open(cert_lsp_lang, encoding="utf-8"))
                _total_effective_lsp += (int(_lc_l.get("verified_edges", 0) or 0)
                                         + int(_lc_l.get("corrected_edges", 0) or 0)
                                         + int(_lc_l.get("deleted_edges", 0) or 0))
            except (OSError, ValueError):
                pass
    # Canonical cert (G4) = the DECLARED task language's cert when present, else the
    # graph-DOMINANT language's (langs[0], node-count desc). Per-language certs persist
    # alongside so a FAIL verdict is never lost to an overwrite. proof_language is the
    # dep_store_manifest 'language' (declared, ground truth); the graph-dominant fallback
    # covers --lang-absent / cert-missing.
    _canon_src, _canon_lang = _canonical_cert_source(
        a.out, (proof_language or "").strip().lower(), langs[0])
    if os.path.exists(_canon_src):
        try:
            shutil.copyfile(_canon_src, cert_lsp)
            print(f"[gt-run-proof] canonical LSP cert <- {_canon_lang} "
                  f"(declared={(proof_language or 'n/a')}, dominant={langs[0]})", flush=True)
        except OSError as _ce:
            print(f"WARN: could not copy {_canon_lang} LSP cert to canonical path: {_ce}", file=sys.stderr)
    # TIER-2: ONE orchestrator-owned closure rebuild over the FINAL edge set (the per-language
    # passes deferred it via GT_DEFER_CLOSURE_REBUILD). Mirrors resolve.py's per-pass contract
    # EXACTLY — rebuild iff any language mutated edges, else stamp — reusing _closure_action, and
    # runs the SAME proof-mode freshness assertion (clo_ts >= lsp_ts), moved from per-language to
    # once-after-loop. In proof mode a missing binary / rc!=0 fail-closes inside _rebuild_closure,
    # preserving the closure-legitimacy gate. Then stamps the canonical cert honestly. The final
    # closure table is IDENTICAL to the baseline's last per-language rebuild (same final edges);
    # only K-1 redundant rebuilds are removed. No-op unless we deferred (single-language untouched).
    if _multi_lang_closure_defer:
        from groundtruth.resolve import _closure_action as _clo_act
        from groundtruth.resolve import _rebuild_closure as _rebuild_closure_once
        from groundtruth.runtime import proof as _proof_clo
        if _clo_act(False, _total_effective_lsp) == "rebuild":
            _final_closure_ok = _rebuild_closure_once(graph)
        else:  # no language changed edges → closure already valid; refresh freshness ts only
            _proof_clo.stamp_closure(graph)
            _final_closure_ok = True
        _proof_clo.assert_closure_after_lsp(graph)  # SAME fail-close, once (proof mode)
        print(f"[gt-run-proof] closure rebuilt ONCE over final graph "
              f"(Σeffective={_total_effective_lsp}, ok={_final_closure_ok}) — "
              f"{len(langs)} langs, K-1 redundant rebuilds skipped (tier-2)", flush=True)
        if os.path.exists(cert_lsp):
            try:
                _cc_clo = json.load(open(cert_lsp, encoding="utf-8"))
                _cc_clo["closure_rebuilt_after_lsp"] = bool(_final_closure_ok)
                with open(cert_lsp, "w", encoding="utf-8") as _fh_clo:
                    json.dump(_cc_clo, _fh_clo, indent=2)
            except (OSError, ValueError):
                pass
    # WHOLE-GRAPH FINALIZE (multi-language correctness): re-snapshot graph_hash_after_lsp
    # ONCE over the FINAL graph.db and stamp it into the canonical cert — the per-language
    # snapshot picked by DECLARED language can be stale when a non-declared language mutated
    # the shared graph AFTER it. See _finalize_after_lsp_hash. No-op for single-language repos.
    _fin = _finalize_after_lsp_hash(graph, cert_lsp)
    if _fin:
        print(f"[gt-run-proof] graph_hash_after_lsp refreshed to FINAL graph "
              f"({(_fin[0] or '(absent)')[:12]} -> {_fin[1][:12]}) — canonical lang={_canon_lang!r} "
              f"was not the last to mutate the shared graph.db (whole-graph finalize)", flush=True)
    # G4 inverse stamp check: the GRAPH carries lsp-stamped edges but the canonical cert
    # reports zero effective LSP work -> the cert under-reports real work (wrong-language
    # or stale cert masking the task language). Diagnostic flag, generalized (no task keys).
    _lsp_stamped = _count_lsp_stamped_edges(graph)
    if _lsp_stamped > 0 and os.path.exists(cert_lsp):
        try:
            _cc = json.load(open(cert_lsp, encoding="utf-8"))
            _work = (int(_cc.get("verified_edges", 0) or 0)
                     + int(_cc.get("corrected_edges", 0) or 0)
                     + int(_cc.get("deleted_edges", 0) or 0))
            if _work == 0:
                print(f"[gt-run-proof] WARN LSP STAMP MISMATCH: graph has {_lsp_stamped} "
                      f"lsp-stamped edge(s) but canonical cert (lang={_cc.get('language')!r}) "
                      f"reports 0 effective work — possible wrong-language/stale cert", flush=True)
        except (OSError, ValueError):
            pass
    print(f"[gt-run-proof] per-language LSP verdicts: {lang_verdicts}", flush=True)
    # P1-e fail-closed aggregation: ANY known language INSTALL_MISSING / FAIL_NO_WARM /
    # resolve-error fails the proof under GT_REQUIRE_LSP=1 — a sibling language's success
    # must never mask another language's gap (audit defect #1), and a launched-but-
    # never-warm server is a failure, not a pass.
    # B-INFRA: scope the fail-closed gate to the repo's PRIMARY/dominant language(s) so an
    # incidental language whose server isn't baked (e.g. a few Java files in a TS repo, jdtls
    # absent) does NOT falsely INFRA-kill a task whose own language resolved. Dominant lang from
    # the graph + the declared task lang; both are already computed above the same way as `langs`.
    try:
        _dom = _detect_lang(graph)
    except Exception:
        _dom = None
    # P0-2 fix (Fable 2026-07-02): feed the DECLARED task language (proof_language, from
    # dep_store_manifest) as the fail-close primary. Production never passes --lang, and the
    # graph-dominant heuristic (_detect_lang / langs[0]) is owned by whatever language has the most
    # nodes — which on a vendored/docs-heavy repo is NOT the task's language (aiomonitor: a python
    # task with 2378 vendored JS nodes scoped to javascript). langs[0]/_dom remain the FALLBACK when
    # the declared language is unavailable (subsumes BUG-D). See _derive_primary_langs.
    _primary_langs = _derive_primary_langs(langs, _dom, proof_language or getattr(a, "lang", None))
    _agg_ok, _agg_failures = aggregate_lsp_verdicts(
        lang_verdicts,
        require_lsp=os.environ.get("GT_REQUIRE_LSP") == "1",
        any_success=lsp_ok,
        primary_langs=_primary_langs or None,
    )
    if not _agg_ok:
        # FAIL-CLOSED (gt_gt §7 + the P1-e comment above): _agg_ok is only False when
        # GT_REQUIRE_LSP=1 AND a known language is INSTALL_MISSING / FAIL_NO_WARM
        # (crash-on-start) / resolve-error — a genuine substrate liveness break. Do NOT run
        # the paid agent on a dark LSP (the old WARN-and-continue silently discarded the
        # computed fail verdict). WARN verdicts (Go/Rust dep-env incomplete offline) are NOT
        # in _agg_failures, so a warm-but-unproductive server still reaches the agent
        # (correct-or-quiet preserved); INSTALL_MISSING is additionally guarded earlier by
        # _baked_lsp_problems at env_validation.
        return tracker.fail(
            "lsp_pass",
            "LSP_LIVENESS_FAIL",
            "GT_REQUIRE_LSP=1 but known language(s) failed the LSP pass: "
            f"{', '.join(_agg_failures)}",
            lang_verdicts=lang_verdicts,
        )
    tracker.complete("lsp_pass", lang_verdicts=lang_verdicts)

    # 3. graph certificate
    _gc_rc = _run([sys.executable, os.path.join(GT_HOME, "scripts/metrics/graph_certificate.py"), graph,
          "--source-root", work, "--lsp-cert", cert_lsp, "--out", cert_graph,
          "--built-inside-container", "1"], base_env)
    # FAIL-CLOSED on a CONTENT-QUALITY graph failure under GT_REQUIRE_GRAPH_VALID=1 (mirrors the
    # embedder gate at step 4c). Scope is the "graph is thin/broken/missing" class — exactly the
    # element-web flying-blind concern — which is STABLE at this proof-time eval point:
    #   GRAPH_FAIL_EMPTY / GRAPH_FAIL_FTS5 / GRAPH_FAIL_BASES_INCOMPLETE / GRAPH_FAIL_DEPTH_INCOMPLETE.
    # Deliberately NOT gated here: the handoff/closure/hash legitimacy verdicts
    # (MISSING_HANDOFF / HANDOFF_INACTIVE / STALE_CLOSURE / HASH_MISMATCH / HOOK_MISMATCH /
    # BUILT_ON_HOST) — those depend on the agent-consume plumbing set up AFTER this point, so gating
    # on them here would FALSE-FAIL good tasks in any proof-only context (proof-sweep showed exactly
    # this: 6 good tasks -> MISSING_HANDOFF because no host handoff). They keep their existing handling.
    # Default (flag unset) stays INFORMATIONAL so iteration is not blocked; the full-run workflows arm
    # the flag so a thin/broken graph STOPS the paid agent instead of running blind on a false map.
    _CONTENT_FAIL = {"GRAPH_FAIL_EMPTY", "GRAPH_FAIL_FTS5",
                     "GRAPH_FAIL_BASES_INCOMPLETE", "GRAPH_FAIL_DEPTH_INCOMPLETE"}
    # Genuine VALIDITY sub-conditions (gates.md PHASE-3 "keep genuine VALIDITY fail-closed"): a cert
    # proving the AGENT would consume a graph that is NOT the in-container proven graph — built on
    # the host (host-split, mirrors FINAL_PIPELINE_HOST_SPLIT_FAIL), or a different/absent graph
    # IDENTITY hash than proven (classify_graph's own comment: a missing/mismatched graph_hash is "a
    # LEGITIMACY failure ... NOT a quality issue"), or the hook wired to a different graph. These
    # corrupt result attribution and STAY fail-closed. Every OTHER non-GRAPH_VALID verdict (thin/
    # broken/undelivered content: EMPTY/FTS5/BASES/DEPTH/STALE_CLOSURE/MISSING_HANDOFF/
    # HANDOFF_INACTIVE) is GT-QUALITY -> degrade + continue so an index-degraded run does not die here.
    _GRAPH_VALIDITY_FAIL = {"GRAPH_FAIL_BUILT_ON_HOST",
                            "GRAPH_FAIL_HASH_MISMATCH", "GRAPH_FAIL_HOOK_MISMATCH"}
    if os.environ.get("GT_REQUIRE_GRAPH_VALID") == "1":
        _gv = "unknown"
        try:
            with open(cert_graph, encoding="utf-8") as _gf:
                _gv = (json.load(_gf) or {}).get("verdict", "unknown")
        except Exception:
            pass
        # Context-aware enforcement (the discriminator is whether a real agent handoff is configured):
        #  - REAL handoff (GT_HOST_GRAPH_DB set — the paid deepswe/pro runs ALWAYS set it): enforce the
        #    FULL verdict. A genuine MISSING_HANDOFF / HANDOFF_INACTIVE / STALE_CLOSURE / HASH_MISMATCH
        #    here means the agent will NOT receive a valid graph -> MUST fail-close. Good tasks reach
        #    GRAPH_VALID in this context (smoke: 17/18), so no false-fail.
        #  - PROOF-ONLY (no GT_HOST_GRAPH_DB, e.g. the $0 proof-sweep): the handoff verdicts are expected
        #    artifacts (no agent to hand off to) -> enforce only the content-quality class, so the sweep
        #    does not false-fail. A thin/broken graph (DEPTH_INCOMPLETE etc.) fails closed in BOTH modes.
        _real_handoff = bool(os.environ.get("GT_HOST_GRAPH_DB", "").strip())
        _fail = (_gv != "GRAPH_VALID") if _real_handoff else (_gv in _CONTENT_FAIL)
        if _fail and _gv in _GRAPH_VALIDITY_FAIL:
            # VALIDITY: the agent would consume a wrong/host-built/mismatched-identity graph ->
            # results would not be attributable to the proven GT -> refuse the paid run.
            return tracker.fail("graph_cert", "GRAPH_CERT_INVALID",
                                f"graph_certificate verdict={_gv} under GT_REQUIRE_GRAPH_VALID=1 "
                                f"(real_handoff={_real_handoff}) — VALIDITY: refusing the paid agent "
                                "run on a wrong/host-built/mismatched graph")
        if _fail:
            # GT-QUALITY thin/broken/undelivered graph -> DEGRADE + CONTINUE (record, never block);
            # the agent self-localizes and runs GT-off-for-this-task. Reaching build_run_manifest is
            # what keeps the identity binding + artifact presence intact for the downstream gates.
            tracker.complete("graph_cert", path=cert_graph, status_detail="degraded",
                             degraded_reason=str(_gv))
            print(f"[gt-run-proof] graph_cert DEGRADED (verdict={_gv}, real_handoff={_real_handoff}) "
                  f"-> GT-off-for-this-task; continuing to build_run_manifest", flush=True)
        else:
            tracker.complete("graph_cert", path=cert_graph)
    else:
        tracker.complete("graph_cert", path=cert_graph)

    # 4. foundational gates (emits foundational_gate_report.json + embedder_certificate.json via run_v74)
    # A1: GT_BRIEF_CACHE_DIR = the proof out dir, so gate3b PERSISTS its generated brief there;
    # emit_brief (same a.out) then READS it instead of regenerating (single brief per proof).
    gate_env = dict(base_env, GT_GATES_DEEP_JSON=gate_report, GT_BRIEF_CACHE_DIR=a.out)
    rc = _run([sys.executable, os.path.join(GT_HOME, "scripts/metrics/foundational_gates.py"),
               graph, work, issue_file], gate_env)

    # 4b. Embedder certificate — foundational_gates writes it via run_v74 ONLY when the brief has
    # candidates (a non-empty issue). Guarantee the artifact: if absent, emit it from a direct
    # identity + cosine-discrimination probe (proves the forced-ONNX embedder LOADS + produces a
    # finite, discriminating vector). The gate (gate_rc above) proves CONSUMPTION; together =
    # "loaded AND used". Issue-independent, so it always emits.
    #
    # GT-QUALITY DEGRADE (gates.md PHASE-3): a degenerate/unloadable embedder must NOT kill the paid
    # task. It is a GT-quality issue -> SUPPRESS THE SEMANTIC LEG ONLY (FTS5 + graph + brief still
    # ship) and CONTINUE, recording the real verdict. The sanctioned semantic-OFF lever (v7_4_brief
    # _get_model docstring: "or unset GT_REQUIRE_EMBEDDER") is to drop GT_REQUIRE_EMBEDDER so the
    # downstream in-process brief regen zeroes W_SEM (_ZeroEmbeddingModel: "BM25 + graph signals
    # will drive ranking") or falls to the graceful e5 path instead of RAISING. We also guarantee
    # embedder_certificate.json EXISTS (degraded stub if the probe never wrote one) so the
    # REQUIRED_ARTIFACTS presence gate + build_run_manifest do not SUBSTRATE_MISSING_CERTS.
    _embedder_degraded = False
    _embedder_degrade_reason = ""

    def _degrade_embedder(reason: str) -> None:
        nonlocal _embedder_degraded, _embedder_degrade_reason
        _embedder_degraded = True
        _embedder_degrade_reason = reason
        os.environ["GT_REQUIRE_EMBEDDER"] = "0"  # semantic-OFF for the downstream brief (sanctioned)
        if not os.path.exists(cert_emb):
            try:
                with open(cert_emb, "w", encoding="utf-8") as _cf:
                    json.dump({"schema": "gt.embedder_certificate.degraded.v1",
                               "verdict": "EMBEDDER_DEGRADED", "ok": False, "degraded": True,
                               "reason": reason,
                               "emitted_by": "gt-run-proof GT-QUALITY degrade (semantic leg suppressed)"},
                              _cf, indent=2)
            except OSError:
                pass
        print("[gt-run-proof] embedder DEGRADED -> semantic leg suppressed (FTS5+graph+brief still "
              f"ship, GT_REQUIRE_EMBEDDER=0); continuing to build_run_manifest. reason={reason}",
              flush=True)

    if not os.path.exists(cert_emb):
        try:
            os.environ["GT_EMBEDDER_CERT"] = cert_emb
            from groundtruth.runtime import proof as _proof
            import numpy as _np
            from groundtruth.pretask.v7_4_brief import _get_model
            _proof.embedder_identity()  # loads the embedder (raises if not the forced-ONNX one)
            # A raise here (unloadable/degenerate embedder) is caught below and DEGRADED (GT-QUALITY:
            # semantic leg suppressed, FTS5+graph+brief still ship), no longer fatal to the paid task.
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
            # GT-QUALITY: the forced-ONNX embedder could not load/encode -> degrade, don't fail.
            _degrade_embedder(f"probe raised (unloadable/degenerate embedder): {type(e).__name__}: {e}")

    # 4c. CLASSIFY the embedder certificate (probe OR gate-written). A bad verdict — degenerate/
    # no-discrimination, zero model, ST-under-forced-ONNX, model-root divergence, dropped semantic —
    # is GT-QUALITY (embedder degraded), so DEGRADE (suppress the semantic leg, continue), not fail.
    # Skipped when 4b already degraded (the stub cert would only re-derive the same verdict).
    if not _embedder_degraded:
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
                # GT-QUALITY: embedder present but degenerate/unusable -> degrade, don't fail.
                _degrade_embedder(f"classify verdict={_verdict}")
        except Exception as e:
            # A corrupt/unreadable embedder cert on a REQUIRED run is still GT-QUALITY (the embedder
            # is degraded/unprovable), not a substrate break -> degrade + suppress semantic, continue.
            # When not required, keep the original warn-and-continue (semantic already optional).
            if os.environ.get("GT_REQUIRE_EMBEDDER") == "1":
                _degrade_embedder(f"classification error: {type(e).__name__}: {e}")
            else:
                print(f"WARN: embedder cert classification skipped: {e}", file=sys.stderr)
    tracker.complete("gates", gate_rc=rc, embedder_degraded=_embedder_degraded,
                     embedder_degrade_reason=(_embedder_degrade_reason or None))

    # 4d. Emit the curated brief IN-CONTAINER (run_v74 is legal here — containerized + proof) so the
    # agent CONSUMES it from /gt_artifacts/brief.txt instead of regenerating on the host (where
    # run_v74 is fail-closed by the boundary assert). generate_v1r_brief writes the issue anchors;
    # mirror them out for the agent's in-container post_view/post_edit consumers.
    # P0.1-c + gates.md BETTER-GATES §1: brief.txt is artifact #8, but a GT-QUALITY brief issue
    # (empty/thin brief, determinism, localization diagnostic) DEGRADES rather than killing the
    # paid task — the agent self-localizes and runs GT-off-for-this-task. emit_brief writes an
    # EMPTY brief.txt on that path and returns degraded=True, so build_run_manifest + the
    # artifact-presence loop + every downstream identity/PROOF_STATE gate has its artifact and
    # the run reaches the agent. Only VALIDITY (sidecar identity) or genuine artifact IO
    # (brief.txt write) hard-fails with GT_ARTIFACT_MISSING.
    _brief_ok, _brief_detail, _brief_degraded = emit_brief(a.out, _read_issue(issue_file), work, graph)
    if not _brief_ok and not _brief_degraded:
        return tracker.fail("brief_emit", "GT_ARTIFACT_MISSING", f"brief.txt — {_brief_detail}")
    if _brief_degraded:
        # RECORD, never block. The tracker has no degraded state, so record via the existing
        # complete() with a DEGRADED detail string (do not invent new infra), then CONTINUE.
        tracker.complete("brief_emit", detail=f"DEGRADED: {_brief_detail}")
        print(f"[gt-run-proof] brief DEGRADED -> empty /gt_artifacts/brief.txt "
              f"(GT-off-for-this-task; {_brief_detail})", flush=True)
    else:
        tracker.complete("brief_emit", detail=_brief_detail)
        print(f"[gt-run-proof] brief emitted -> /gt_artifacts/brief.txt ({_brief_detail})", flush=True)

    # 5. runtime_context.json
    try:
        from groundtruth.runtime.context import GTRuntimeContext
        ctx = GTRuntimeContext.from_env(source_root=work, graph_db=graph)
        # BUG-F fix (LIPI+Fable 2026-07-02): stamp the SAME runtime_context_id the cert uses.
        # _proof.context_id() reads GT_SOURCE_ROOT/GT_GRAPH_DB from os.environ — but THIS parent
        # process does not set them (they are injected only into the cert SUBPROCESS via base_env,
        # graph_certificate.py:137). Calling context_id() in-process would stamp a DIFFERENT, wrong
        # id (an honest "" turned into a confident-wrong token). Use the shared pure helper with the
        # SAME paths base_env gave the cert subprocess (work, graph, os.environ GT_MODELS_ROOT) so
        # the two binding tokens are byte-identical.
        try:
            from groundtruth.runtime import proof as _proof
            _rcid = _proof.context_id_from(_proof.runtime_root(), work, graph,
                                           os.environ.get("GT_MODELS_ROOT", ""))
        except Exception:
            _rcid = base_env.get("GT_CONTEXT_ID", "")
        with open(os.path.join(a.out, "runtime_context.json"), "w", encoding="utf-8") as f:
            json.dump({"runtime_root": ctx.runtime_root, "source_root": ctx.source_root,
                       "graph_db": ctx.graph_db, "models_root": ctx.models_root,
                       "inside_container": ctx.inside_container, "proof_mode": ctx.proof_mode,
                       "containerized": ctx.containerized,
                       "runtime_context_id": _rcid}, f, indent=2)
    except Exception as e:
        print(f"WARN: runtime_context.json: {e}", file=sys.stderr)

    # 6. run manifest + artifact presence (+ run provenance — see build_run_manifest)
    present = {a_: os.path.exists(os.path.join(a.out, a_)) for a_ in REQUIRED_ARTIFACTS
               if a_ != "run_manifest.json"}
    manifest = build_run_manifest(graph_db=graph, out_dir=a.out, languages=langs,
                                  lsp_scope_files=len(scope_files), lsp_max_edges=max_edges,
                                  lsp_ready_budgets=lsp_ready_budgets, gate_rc=rc,
                                  artifacts_present=present, source_root=work)
    with open(os.path.join(a.out, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    missing = [k for k, v in present.items() if not v]
    if missing:
        return tracker.fail(
            "artifact_contract",
            "SUBSTRATE_MISSING_CERTS",
            f"missing artifacts: {missing}",
            artifacts_present=present,
        )
    tracker.complete("artifact_contract", artifacts_present=present, gate_rc=rc)
    print(f"[gt-run-proof] done: gate_rc={rc} artifacts_present={present}", flush=True)
    # EXIT CODE (P0 fix) — HONOR the wired GT_GATES_DELIVER_ALWAYS pin (previously the
    # foundational-gate rc was captured but the proof ALWAYS `return 0`, so the STRICT pin
    # deepswe_full.yml sets was a no-op and the LSP stamp-drop verdict was inert):
    #
    #   STRICT ("0", the deepswe_full.yml pin): fail-closed on the gate verdict — BUT keep
    #   the deliberate name_match-dominance carve-out. Honoring the RAW gate rc here would
    #   spuriously fail JS/TS/Go tasks, because foundational_gates.main returns rc=1 whenever
    #   ANY gate is OFF and GATE 1 pred_B (det >= name_match) is EXPECTED-off on name_match-
    #   heavy repos (CLAUDE.md Known Limitations #1) — that is the documented reason the old
    #   code returned 0. So under STRICT we fail-close ONLY on the GENUINELY-broken signals
    #   (_strict_broken_gate_signals: LSP stamp-drop / LSP-dark / dead embedder) and let a
    #   pure-GATE-1 name_match rc through (carve-out, DELIVERABLE). Dead embedder + empty
    #   brief + missing artifacts already fail-closed above; this arms the stamp-drop/LSP-dark
    #   teeth the STRICT pin was meant to add.
    #
    #   LENIENT ("1"/unset, default): gate is INFORMATIONAL (recorded in
    #   foundational_gate_report.json); a gate_rc=1 never blocks the paid agent trial.
    if os.environ.get("GT_GATES_DELIVER_ALWAYS") == "0":
        if rc == 0:
            tracker.verdict("ok", "artifact_contract", gate_rc=rc, artifacts_present=present)
            return 0
        broken = _strict_broken_gate_signals(gate_report)
        if broken is None:
            return tracker.fail(
                "gates", "FOUNDATIONAL_GATE_STRICT_FAIL",
                f"GT_GATES_DELIVER_ALWAYS=0 (STRICT) - gate report unreadable; "
                f"honoring raw gate rc={rc} (conservative).",
                gate_rc=rc,
            )
        if broken:
            return tracker.fail(
                "gates", "FOUNDATIONAL_GATE_STRICT_FAIL",
                "GT_GATES_DELIVER_ALWAYS=0 (STRICT) — genuinely-broken foundational "
                "signal(s): " + "; ".join(broken),
                gate_rc=rc,
            )
        # B-1: broken == [] with gate rc != 0 means the ONLY gate failures were the DELIVERABLE
        # carve-outs — benign name_match-DOMINANCE (pred_B) and/or weak per-issue semantic
        # (GATE 3b) — which _strict_broken_gate_signals intentionally excludes and the
        # deliver-always doctrine + this path's docstring ("empty == deliverable") treat as
        # non-refusals (the consumer fact-gates already suppress name_match). Every GENUINE
        # signal (pred_A det-floor, LSP stamp-drop/dark, embedder-dead) is captured above and
        # fails there, so an empty `broken` is a DELIVERABLE gate rc, not a refusal. Refusing
        # here (the old behavior) killed large name_matchy repos at proof — the exact scale
        # class GT targets, and the bug this path's own docstring warns against.
        tracker.verdict("ok", "artifact_contract_name_match_carveout", gate_rc=rc, artifacts_present=present)
        return 0
    tracker.verdict("ok", "artifact_contract", gate_rc=rc, artifacts_present=present)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
REQUIRED_ARTIFACTS = [
    "graph.db",
    "runtime_context.json",
    "lsp_certificate.json",
    "graph_certificate.json",
    "embedder_certificate.json",
    "foundational_gate_report.json",
    "run_manifest.json",
]

# Where GT is baked in the substrate image (NOT a checkout, NOT host paths).
GT_HOME = os.environ.get("GT_HOME", "/opt/gt")


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
    if not (shutil.which("pyright-langserver") or shutil.which("pyright")):
        problems.append("pyright-langserver not baked (do NOT pip-install per task)")
    model = os.path.join(os.environ.get("GT_MODELS_ROOT", os.path.join(GT_HOME, "models")),
                         "e5-small-v2", "model.onnx")
    if not os.path.exists(model):
        problems.append(f"e5 model not baked at {model} (do NOT download per task)")
    if not shutil.which("gt-index") and not os.path.exists("/usr/local/bin/gt-index"):
        problems.append("gt-index not baked")
    return problems


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
    # Dominant language is resolved LAST so its LSP certificate is the one that persists.
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
    lsp_ok = False
    for lg in reversed(langs):  # least-common first, dominant last (its cert persists)
        cmd = [sys.executable, "-m", "groundtruth.resolve", "--db", graph, "--root", work,
               "--resolve", "--lang", lg, "--max-edges", max_edges]
        if scope_path:
            cmd += ["--source-files", scope_path]
        print(f"[gt-run-proof] $ {' '.join(cmd)}", flush=True)
        rr = subprocess.run(cmd, env=base_env, capture_output=True, text=True)
        sys.stdout.write(rr.stdout or ""); sys.stderr.write(rr.stderr or "")
        with open(lsp_metrics_file, "a", encoding="utf-8") as _mf:
            _mf.write(rr.stdout or "")
        if rr.returncode == 0:
            lsp_ok = True
    if os.environ.get("GT_REQUIRE_LSP") == "1" and not lsp_ok:
        print("LSP_LIVENESS_FAIL: GT_REQUIRE_LSP=1 but LSP resolved no language successfully",
              file=sys.stderr)
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
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        _b = generate_v1r_brief(issue_text=_read_issue(issue_file), repo_root=work,
                                graph_db=graph, bug_id="portable")
        _bt = (getattr(_b, "brief_text", "") or "").strip()
        if _bt:
            with open(os.path.join(a.out, "brief.txt"), "w", encoding="utf-8") as _bf:
                _bf.write(_bt)
            if os.path.exists("/tmp/gt_issue_anchors.json"):
                shutil.copy("/tmp/gt_issue_anchors.json", os.path.join(a.out, "gt_issue_anchors.json"))
            print(f"[gt-run-proof] brief emitted -> /gt_artifacts/brief.txt ({len(_bt)} chars)", flush=True)
        else:
            print("WARN: portable brief empty — not written (agent will host-fallback)", file=sys.stderr)
    except Exception as e:
        print(f"WARN: brief emission failed: {e}", file=sys.stderr)

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

    # 6. run manifest + artifact presence
    present = {a_: os.path.exists(os.path.join(a.out, a_)) for a_ in REQUIRED_ARTIFACTS
               if a_ != "run_manifest.json"}
    manifest = {"schema": "gt.run_manifest.v1", "languages": langs, "lsp_scope_files": len(scope_files),
                "lsp_max_edges": max_edges, "gate_rc": rc,
                "artifacts_present": present, "source_root": work, "out": a.out}
    with open(os.path.join(a.out, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    missing = [k for k, v in present.items() if not v]
    if missing:
        print(f"SUBSTRATE_MISSING_CERTS: {missing}", file=sys.stderr)
    print(f"[gt-run-proof] done: gate_rc={rc} artifacts_present={present}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

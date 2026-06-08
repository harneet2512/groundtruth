"""Container preflight — fail-fast runtime contract before any benchmark work.

Runs INSIDE the eval container. Two modes:
  --mode runtime  (pre-index): validates the GTRuntimeContext runtime/env/embedder/
                  LSP-available contract. Fails before GT builds anything.
  --mode graph    (post-resolve): validates the graph dimensions by REUSING the
                  existing scripts/verify/preflight_pipeline.py --census (graph/fts5/
                  edge_quality/data_flow/lsp_edges/embedder/prebuilt), plus the
                  context's graph checks (built-in-container, not-prebuilt).

Writes runtime_preflight.json (the context + the check results + the verdict) to
--out, and exits non-zero (fail-closed) when GT_PROOF_MODE=1 and any check fails.
Changes no GT product logic.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from groundtruth.runtime.context import GTRuntimeContext


def _run_census(runtime_root: str, graph_db: str, source_root: str) -> tuple[bool, str]:
    """Reuse the existing comprehensive preflight (graph dimensions). Returns
    (passed, tail-of-output). Tries the in-container path first, then a checkout."""
    candidates = [
        os.path.join(runtime_root, "scripts", "verify", "preflight_pipeline.py"),
        os.path.join(os.environ.get("GITHUB_WORKSPACE", ""), "scripts", "verify", "preflight_pipeline.py"),
        "scripts/verify/preflight_pipeline.py",
    ]
    script = next((p for p in candidates if p and os.path.exists(p)), "")
    if not script:
        return (False, "preflight_pipeline.py not found")
    try:
        cp = subprocess.run(
            [sys.executable, script, "--db", graph_db, "--root", source_root, "--census"],
            capture_output=True, text=True, timeout=600)
        tail = "\n".join((cp.stdout + cp.stderr).strip().splitlines()[-12:])
        return (cp.returncode == 0, tail)
    except Exception as e:
        return (False, f"census error: {e}")


def run_preflight(mode: str, source_root: str, graph_db: str, audit_dir: str,
                  out: str, census: bool) -> int:
    ctx = GTRuntimeContext.from_env(source_root=source_root, graph_db=graph_db, audit_dir=audit_dir)
    require_graph = mode == "graph"
    results = ctx.checks(require_graph=require_graph)

    census_pass = None
    census_tail = ""
    if require_graph and census and ctx.graph_db:
        census_pass, census_tail = _run_census(ctx.runtime_root, ctx.graph_db, ctx.source_root)
        results.append(("graph_dimension_census", bool(census_pass), census_tail.replace("\n", " | ")[:300]))

    failures = [(n, ok, d) for n, ok, d in results if not ok]
    verdict = {
        "contract": "runtime_preflight",
        "mode": mode,
        "context": ctx.as_dict(),
        "proof_mode": ctx.proof_mode,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results],
        "failures": [n for n, ok, _ in results if not ok],
        "passed": not failures,
    }
    if out:
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(verdict, f, indent=2, default=str)
        except Exception:
            pass

    for n, ok, d in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {n}: {d}")
    if ctx.proof_mode and failures:
        print(f"GT_PROOF_MODE=1 PREFLIGHT FAILED ({mode}): " + ", ".join(n for n, _, _ in failures))
        return 1
    if failures:
        print(f"PREFLIGHT WARNINGS ({mode}; not proof mode, not fatal): "
              + ", ".join(n for n, _, _ in failures))
    else:
        print(f"PREFLIGHT OK ({mode})")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["runtime", "graph"], default="runtime")
    ap.add_argument("--source", default="")
    ap.add_argument("--graph-db", default="")
    ap.add_argument("--audit", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--census", action="store_true")
    a = ap.parse_args(argv)
    return run_preflight(a.mode, a.source, a.graph_db, a.audit, a.out, a.census)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

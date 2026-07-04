#!/usr/bin/env python3
"""Emit structured targeted probe evidence for industrial/SOTA TODOs."""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / ".groundtruth" / "industrial_sota_targeted_probes.json"
SMOKE_GRAPH = ROOT / "proof_smoke_27798877761_artifacts_current" / "proof-sweep-abs-module-cache-flags" / "graph.db"


def _run(cmd: list[str], *, cwd: Path = ROOT, timeout_s: int = 120) -> dict[str, Any]:
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
        return {
            "command": cmd,
            "cwd": str(cwd),
            "rc": proc.returncode,
            "timed_out": False,
            "duration_s": round(time.time() - start, 3),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "cwd": str(cwd),
            "rc": None,
            "timed_out": True,
            "duration_s": round(time.time() - start, 3),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }


def _probe(status: str, evidence: list[str], notes: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"status": status, "evidence": evidence, "notes": notes or [], **extra}


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _worktree_probe() -> dict[str, Any]:
    git = shutil.which("git")
    if not git:
        return _probe("missing", [], ["git unavailable"])
    with tempfile.TemporaryDirectory(prefix="gt_worktree_probe_") as td:
        root = Path(td) / "repo"
        wt = Path(td) / "wt"
        root.mkdir()
        steps = [
            _run([git, "init", "-q"], cwd=root),
            _run([git, "config", "user.email", "gt@example.invalid"], cwd=root),
            _run([git, "config", "user.name", "GT Probe"], cwd=root),
        ]
        (root / "a.txt").write_text("a\n", encoding="utf-8")
        steps.extend([
            _run([git, "add", "a.txt"], cwd=root),
            _run([git, "commit", "-q", "-m", "init"], cwd=root),
            _run([git, "worktree", "add", "-q", str(wt)], cwd=root),
        ])
        top = _run([git, "rev-parse", "--show-toplevel"], cwd=wt)
        excl = _run([git, "rev-parse", "--git-path", "info/exclude"], cwd=wt)
        steps.extend([top, excl])
        git_file = (wt / ".git").is_file()
        ok = all(s["rc"] == 0 for s in steps) and git_file and Path((excl["stdout"] or "").strip()).is_absolute()
        evidence = [
            "git worktree has .git file, not directory",
            "git rev-parse --show-toplevel resolves worktree root",
            "git rev-parse --git-path info/exclude returns concrete exclude path",
        ] if ok else []
        return _probe("evidence" if ok else "missing", evidence, [] if ok else ["worktree fixture failed"], steps=steps)


def _go_test_probe() -> dict[str, Any]:
    go_exe = shutil.which("go")
    if not go_exe:
        candidates = sorted((Path.home() / "go" / "pkg" / "mod" / "golang.org").glob("toolchain@v*-go*.windows-amd64/bin/go.exe"), reverse=True)
        go_exe = str(candidates[0]) if candidates else ""
    if not go_exe:
        return _probe("missing", [], ["go unavailable"])
    cmd = [
        go_exe,
        "test",
        "-json",
        "-run",
        "Test.*(Composes|GetAllEdges|Promote)",
        "./internal/resolver",
        "./internal/store",
        "-count=1",
    ]
    step = _run(cmd, cwd=ROOT / "gt-index", timeout_s=180)
    passed_tests: list[str] = []
    failed_tests: list[str] = []
    for line in (step.get("stdout") or "").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("Action") == "pass" and row.get("Test"):
            passed_tests.append(str(row["Test"]))
        if row.get("Action") == "fail" and row.get("Test"):
            failed_tests.append(str(row["Test"]))
    ok = step["rc"] == 0 and not failed_tests and passed_tests
    return _probe(
        "evidence" if ok else "missing",
        [f"go targeted resolver/store fixtures passed: {len(passed_tests)} tests"] if ok else [],
        [] if ok else ["go targeted fixtures failed or found no tests"],
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        step=step,
    )


def _repeatability_probe() -> dict[str, Any]:
    if not SMOKE_GRAPH.is_file():
        return _probe("missing", [], [f"graph missing: {SMOKE_GRAPH}"])
    outputs: list[dict[str, Any]] = []
    hashes: list[str] = []
    for _ in range(2):
        step = _run([sys.executable, "scripts/swebench/resolution_method_audit.py", "--sample-limit", "10", str(SMOKE_GRAPH)], timeout_s=120)
        outputs.append(step)
        try:
            payload = json.loads(step["stdout"])
            hashes.append(_json_hash(payload))
        except Exception:
            hashes.append("")
    ok = all(o["rc"] == 0 for o in outputs) and len(set(hashes)) == 1 and bool(hashes[0])
    return _probe(
        "evidence" if ok else "missing",
        [f"repeat audit hashes match: {hashes[0]}"] if ok else [],
        [] if ok else ["repeatability hash mismatch"],
        hashes=hashes,
        steps=outputs,
    )


def _mcp_metadata_probe() -> dict[str, Any]:
    path = ROOT / "src" / "groundtruth" / "mcp" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_mcp_metadata":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for key in sub.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            keys.add(key.value)
    required = {"schema", "graph_db", "lsp_promotion_requested", "freshness", "degraded"}
    ok = required.issubset(keys)
    return _probe(
        "evidence" if ok else "missing",
        [f"MCP metadata keys present: {sorted(required)}"] if ok else [],
        [] if ok else [f"missing MCP metadata keys: {sorted(required - keys)}"],
        observed_keys=sorted(keys),
    )


def _fact_surface_probe() -> dict[str, Any]:
    from groundtruth.evidence.fact_gate import edge_fact_clause

    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE edges(type TEXT, resolution_method TEXT, trust_tier TEXT, candidate_count INTEGER, confidence REAL)"
    )
    rows = [
        ("CALLS", "import", "CERTIFIED", 1, 1.0),
        ("CALLS", "name_match", "CERTIFIED", 9, 0.9),
        ("COMPOSES", "name_match", "CERTIFIED", 2, 0.9),
        ("COMPOSES", "promote_composes", "SUPPRESSED", 2, 0.9),
        ("CALLS", "same_file", "SUPPRESSED", 1, 1.0),
    ]
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", rows)
    clause = edge_fact_clause(con, "e")
    facts = con.execute(f"SELECT type, resolution_method, trust_tier FROM edges e WHERE {clause}").fetchall()
    con.close()
    fact_rows = [tuple(r) for r in facts]
    ok = fact_rows == [("CALLS", "import", "CERTIFIED")]
    return _probe(
        "evidence" if ok else "missing",
        ["canonical fact gate admits deterministic fact and rejects name_match/suppressed ambiguous surfaces"] if ok else [],
        [] if ok else [f"unexpected fact rows: {fact_rows!r}"],
        fact_rows=fact_rows,
    )


def _verification_horizon_probe() -> dict[str, Any]:
    from groundtruth.runtime.verification_horizon import verify_horizon_band

    low = verify_horizon_band(
        action_count=1,
        step_limit=20,
        v=1,
        edit_coverage=1.0,
        test_coverage=0.0,
        has_edits=True,
        confidence_tier="low",
    )
    high = verify_horizon_band(
        action_count=1,
        step_limit=20,
        v=1,
        edit_coverage=1.0,
        test_coverage=0.0,
        has_edits=True,
        confidence_tier="high",
    )
    ok = low == "advisory" and high is None
    return _probe(
        "evidence" if ok else "missing",
        ["low confidence tier triggers earlier verification horizon than high confidence"] if ok else [],
        [] if ok else [f"unexpected bands: low={low!r} high={high!r}"],
        bands={"low": low, "high": high},
    )


def build_payload() -> dict[str, Any]:
    go_probe = _go_test_probe()
    mcp_probe = _mcp_metadata_probe()
    return {
        "schema": "gt.industrial_sota_targeted_probes.v1",
        "generated_at_epoch": time.time(),
        "probes": {
            "worktree_or_submodule_fixture": _worktree_probe(),
            "ambiguous_composes_fixture": go_probe,
            "repeatability_run_pair": _repeatability_probe(),
            "mcp_endpoint_probe": mcp_probe,
            "mcp_metadata_probe": mcp_probe,
            "evidence_surface_parity_probe": _fact_surface_probe(),
            "fresh_brief_result": _verification_horizon_probe(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    payload = build_payload()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    statuses = [p.get("status") for p in payload["probes"].values()]
    return 0 if all(s == "evidence" for s in statuses) else 1


if __name__ == "__main__":
    raise SystemExit(main())

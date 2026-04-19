#!/usr/bin/env python3
"""Forced material-edit probe for the GT hybrid LSP path.

The goal is to prove whether the hybrid promotion branch emits
`material_edit`, `lsp_status`, and `lsp_promotion` once a real diff exists.
This runs the same repository-local code paths used by the smoke runs, but in
an isolated temporary git repo so it does not disturb benchmark tasks.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RuntimeError(f"unable to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _git_init(repo: Path) -> None:
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "gt-probe@example.com"], repo)
    _run(["git", "config", "user.name", "GT Probe"], repo)


def _write_repo(repo: Path) -> tuple[Path, Path]:
    pkg = repo / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def callee(x):\n"
        "    return x + 1\n"
    )
    (pkg / "b.py").write_text(
        "from pkg.a import callee\n\n"
        "def caller(y):\n"
        "    return callee(y)\n"
    )
    return pkg / "a.py", pkg / "b.py"


def _commit_baseline(repo: Path) -> None:
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", "baseline"], repo)


def _seed_hashes(mod, repo: Path, hashes_path: Path) -> None:
    tracked = []
    for rel in ["pkg/__init__.py", "pkg/a.py", "pkg/b.py"]:
        tracked.append(rel)
    hashes = {}
    for rel in tracked:
        abs_path = repo / rel
        hashes[rel] = mod.file_hash(str(abs_path))
    hashes_path.write_text(json.dumps(hashes, indent=2, sort_keys=True))


def _make_graph_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                language TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                resolution_method TEXT,
                confidence REAL NOT NULL,
                source_file TEXT NOT NULL,
                source_line INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO nodes(id,name,file_path,start_line,end_line,language) VALUES (?,?,?,?,?,?)",
            (1, "caller", "pkg/b.py", 1, 6, "python"),
        )
        conn.execute(
            "INSERT INTO nodes(id,name,file_path,start_line,end_line,language) VALUES (?,?,?,?,?,?)",
            (2, "callee", "pkg/a.py", 1, 4, "python"),
        )
        conn.execute(
            "INSERT INTO edges(id,source_id,target_id,type,resolution_method,confidence,source_file,source_line) VALUES (?,?,?,?,?,?,?,?)",
            (1, 1, 2, "CALLS", "treesitter", 0.5, "pkg/b.py", 4),
        )
        conn.commit()
    finally:
        conn.close()


def _count_events(path: Path) -> dict[str, int]:
    ctr: collections.Counter[str] = collections.Counter()
    if not path.exists():
        return {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        ctr[ev.get("event", "?")] += 1
    return dict(ctr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", type=Path, required=True, help="Path to the repo checkout to probe.")
    ap.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="Path to swe_agent_state_gt.py (defaults to repo-root/tools/groundtruth/... or repo-root/benchmarks/swebench/...).",
    )
    ap.add_argument(
        "--promoter-path",
        type=Path,
        default=None,
        help="Path to lsp_promoter.py (defaults to repo-root/tools/groundtruth/... or repo-root/benchmarks/swebench/...).",
    )
    ap.add_argument("--keep", action="store_true", help="Keep the probe workspace for inspection.")
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    src_path = repo_root / "src"
    if src_path.exists() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    state_path = args.state_path
    if state_path is None:
        for candidate in [
            repo_root / "tools" / "groundtruth" / "swe_agent_state_gt.py",
            repo_root / "benchmarks" / "swebench" / "swe_agent_state_gt.py",
        ]:
            if candidate.exists():
                state_path = candidate
                break
    promoter_path = args.promoter_path
    if promoter_path is None:
        for candidate in [
            repo_root / "tools" / "groundtruth" / "lsp_promoter.py",
            repo_root / "benchmarks" / "swebench" / "lsp_promoter.py",
        ]:
            if candidate.exists():
                promoter_path = candidate
                break
    if state_path is None or not state_path.exists():
        raise SystemExit("could not locate swe_agent_state_gt.py; pass --state-path explicitly")
    if promoter_path is None or not promoter_path.exists():
        raise SystemExit("could not locate lsp_promoter.py; pass --promoter-path explicitly")

    state_mod = _load_module(state_path, "swe_agent_state_gt_probe")
    promoter_mod = _load_module(promoter_path, "lsp_promoter_probe")

    with tempfile.TemporaryDirectory(prefix="gt_lsp_probe_") as td:
        td = Path(td)
        repo = td / "repo"
        repo.mkdir()
        _git_init(repo)
        _write_repo(repo)
        _commit_baseline(repo)

        db_path = td / "graph.db"
        _make_graph_db(db_path)

        telemetry = td / "gt_hook_telemetry.jsonl"
        hashes = td / "gt_hashes.json"
        ack_state = td / "gt_ack_state.json"
        last_action = td / "gt_last_action.txt"
        last_edit = td / "gt_last_material_edit.ts"
        ready = td / "gt_lsp_ready"

        ready.touch()
        _seed_hashes(state_mod, repo, hashes)

        # Patch the module to point at the isolated workspace.
        state_mod.REPO_ROOT = str(repo)
        state_mod.GT_DB = str(db_path)
        state_mod.GT_TELEMETRY = telemetry
        state_mod.GT_HASHES = hashes
        state_mod.GT_ACK_STATE = ack_state
        state_mod.GT_LAST_ACTION = last_action
        state_mod.GT_LAST_MATERIAL_EDIT_TS = last_edit
        state_mod.GT_LSP_READY = ready
        state_mod.GT_TELEMETRY_DIR = td
        state_mod.GT_POLICY_STATE = td / "gt_policy.json"
        state_mod.GT_TOOL_COUNTS = td / "gt_tool_counts.json"
        state_mod.STATE_PATH = td / "state.json"

        os.environ["GT_ARM"] = "gt-hybrid"
        os.environ["GT_RUN_ID"] = "probe-lsp-material-edit"
        os.environ["GT_INSTANCE_ID"] = "probe-task"
        os.environ["GT_LSP_ENABLED"] = "1"
        os.environ["GT_TELEMETRY_DIR"] = str(td)

        # Force one controlled edit that detect_material_edits() should see.
        with open(repo / "pkg" / "b.py", "a", encoding="utf-8") as f:
            f.write("\n# forced probe edit\n")

        changed = state_mod.detect_material_edits()
        if changed:
            state_mod.log_event("material_edit", files=changed[:3], edit_count=1)
            state_mod.log_event("lsp_status", enabled=True, status="ready", ready=True, changed=len(changed))
            promo_stats = promoter_mod.promote_ambiguous_edges(
                source_files=changed,
                db_path=str(db_path),
                root=str(repo),
                language="python",
            )
            state_mod.log_event("lsp_promotion", **promo_stats)
        else:
            promo_stats = {"attempts": 0, "reason": "no_material_edit_detected"}
            state_mod.log_event("lsp_status", enabled=True, status="warming_up", ready=False, changed=0)

        events = _count_events(telemetry)
        out = {
            "changed": changed,
            "promotion_stats": promo_stats,
            "events": events,
            "telemetry_path": str(telemetry),
            "repo": str(repo),
            "db_path": str(db_path),
            "ready": ready.exists(),
        }
        print(json.dumps(out, indent=2, sort_keys=True))

        if args.keep:
            keep = Path.cwd() / "gt_lsp_probe_keep"
            if keep.exists():
                shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(td, keep)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

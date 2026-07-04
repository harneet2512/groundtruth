#!/usr/bin/env python3
"""Run local industrial/SOTA checks and record a JSONL trajectory.

This is a hygiene runner, not a TODO closer. It records enough command output to
inspect what happened without relying on chat scrollback or ad hoc grep.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / ".groundtruth" / "industrial_sota_local_trajectory.jsonl"


def _go_bin_dir() -> str | None:
    candidates = sorted(
        (ROOT.parent / "Lenovo" / "go" / "pkg" / "mod" / "golang.org").glob(
            "toolchain@v*-go*.windows-amd64/bin"
        ),
        reverse=True,
    )
    for path in candidates:
        if (path / "go.exe").is_file():
            return str(path)
    fallback = Path.home() / "go" / "pkg" / "mod" / "golang.org"
    candidates = sorted(fallback.glob("toolchain@v*-go*.windows-amd64/bin"), reverse=True)
    for path in candidates:
        if (path / "go.exe").is_file():
            return str(path)
    return None


def _go_exe() -> str:
    go_dir = _go_bin_dir()
    if go_dir:
        return str(Path(go_dir) / "go.exe")
    return "go"


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    go_dir = _go_bin_dir()
    if go_dir:
        env["PATH"] = go_dir + os.pathsep + env.get("PATH", "")
    return env


def _cmd(
    check_id: str,
    purpose: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    expect_rc: int | None = 0,
    timeout_s: int = 120,
) -> dict[str, Any]:
    start = time.time()
    env = _base_env()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
        )
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc = None
        timed_out = True
        proc = exc  # type: ignore[assignment]
    except FileNotFoundError as exc:
        rc = None
        timed_out = False
        duration = time.time() - start
        return {
            "schema": "gt.local_validation_step.v1",
            "check_id": check_id,
            "purpose": purpose,
            "cwd": str(cwd),
            "command": command,
            "expect_rc": expect_rc,
            "rc": rc,
            "timed_out": timed_out,
            "ok": False,
            "duration_s": round(duration, 3),
            "stdout": "",
            "stderr": f"FileNotFoundError: {exc}",
        }
    duration = time.time() - start
    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    ok = (not timed_out) and (expect_rc is None or rc == expect_rc)
    return {
        "schema": "gt.local_validation_step.v1",
        "check_id": check_id,
        "purpose": purpose,
        "cwd": str(cwd),
        "command": command,
        "expect_rc": expect_rc,
        "rc": rc,
        "timed_out": timed_out,
        "ok": ok,
        "duration_s": round(duration, 3),
        "stdout": stdout,
        "stderr": stderr,
    }


def _python_inline(check_id: str, purpose: str, code: str, *, expect_rc: int = 0) -> dict[str, Any]:
    return _cmd(check_id, purpose, [sys.executable, "-c", code], expect_rc=expect_rc)


def _checks() -> list[dict[str, Any]]:
    return [
        _cmd(
            "python_compile_core",
            "Compile touched Python validation, proof, evidence, adapter, and runtime paths.",
            [
                sys.executable,
                "-m",
                "py_compile",
                "scripts/swebench/gt_run_proof.py",
                "scripts/swebench/artifact_resolver.py",
                "scripts/swebench/gt_intel_lean.py",
                "scripts/swebench/task_truth.py",
                "scripts/swebench/industrial_sota_validation_gate.py",
                "scripts/swebench/run_industrial_sota_targeted_probes.py",
                "scripts/swebench/select_industrial_sota_targets.py",
                "scripts/swebench/paired_industrial_sota_summary.py",
                "scripts/swebench/print_industrial_sota_dispatch.py",
                "scripts/swebench/resolution_method_audit.py",
                "artifact_deepswe/gt_agent.py",
                "artifact_deepswe/gt_mini_patch.py",
                "src/groundtruth/evidence/fact_gate.py",
                "src/groundtruth/evidence/format_contract.py",
                "src/groundtruth/evidence/mismatch.py",
                "src/groundtruth/mcp/server.py",
                "src/groundtruth/pretask/v1r_brief.py",
                "src/groundtruth/runtime/verification_horizon.py",
                "tests/test_artifact_resolver.py",
            ],
            timeout_s=180,
        ),
        _cmd(
            "artifact_resolver_pytest",
            "Verify artifact resolver constructor/API changes.",
            [sys.executable, "-m", "pytest", "tests/test_artifact_resolver.py", "-q"],
            timeout_s=180,
        ),
        _cmd(
            "go_targeted_tests",
            "Run targeted Go tests around resolver promotion and store edge ordering.",
            [
                _go_exe(),
                "test",
                "-run",
                "Test.*(Promote|Composes|Edge|Store|SQLite|GetAllEdges)",
                "./internal/resolver",
                "./internal/store",
                "-count=1",
            ],
            cwd=ROOT / "gt-index",
            timeout_s=180,
        ),
        _cmd(
            "iteration_selector_multilingual",
            "Default selector must use open-source multilingual iteration surface.",
            [
                sys.executable,
                "scripts/swebench/select_industrial_sota_targets.py",
                "--per-language",
                "1",
                "--max-total",
                "5",
                "--json",
            ],
        ),
        _cmd(
            "deepswe_selector_explicit",
            "DeepSWE held-out selector works only when explicitly requested.",
            [
                sys.executable,
                "scripts/swebench/select_industrial_sota_targets.py",
                "--surface",
                "deepswe",
                "--per-language",
                "1",
                "--max-total",
                "5",
                "--json",
            ],
        ),
        _cmd(
            "iteration_dispatch_refuses_deepswe_workflow",
            "Open-source iteration IDs must not be dispatched to deepswe_full.yml.",
            [
                sys.executable,
                "scripts/swebench/print_industrial_sota_dispatch.py",
                "--per-language",
                "1",
                "--max-total",
                "5",
            ],
            expect_rc=1,
        ),
        _cmd(
            "deepswe_dispatch_explicit",
            "Held-out DeepSWE dispatch emits paired per-language GT/baseline commands only when explicit.",
            [
                sys.executable,
                "scripts/swebench/print_industrial_sota_dispatch.py",
                "--surface",
                "deepswe",
                "--per-language",
                "1",
                "--max-total",
                "5",
                "--step-limit",
                "20",
                "--max-parallel",
                "2",
                "--split-by-language",
            ],
        ),
        _python_inline(
            "all_five_language_coverage",
            "Fail unless iteration and explicit DeepSWE surfaces both cover Go, JavaScript, Python, Rust, and TypeScript.",
            "\n".join(
                [
                    "import json, subprocess, sys",
                    "required={'go','javascript','python','rust','typescript'}",
                    "def langs(args):",
                    "    out=subprocess.check_output([sys.executable, 'scripts/swebench/select_industrial_sota_targets.py', *args, '--per-language', '1', '--max-total', '5', '--json'], text=True)",
                    "    data=json.loads(out)",
                    "    return {t.get('language') for t in data.get('tasks', [])}",
                    "iteration=langs([])",
                    "deepswe=langs(['--surface','deepswe'])",
                    "assert iteration == required, {'iteration': sorted(iteration), 'missing': sorted(required-iteration)}",
                    "assert deepswe == required, {'deepswe': sorted(deepswe), 'missing': sorted(required-deepswe)}",
                    "print({'iteration': sorted(iteration), 'deepswe': sorted(deepswe)})",
                ]
            ),
        ),
        _cmd(
            "resolution_method_audit_smoke",
            "Audit existing graph artifact for resolution-method risk fields.",
            [
                sys.executable,
                "scripts/swebench/resolution_method_audit.py",
                "--sample-limit",
                "5",
                "proof_smoke_27798877761_artifacts_current/proof-sweep-abs-module-cache-flags/graph.db",
            ],
        ),
        _cmd(
            "targeted_probe_evidence",
            "Refresh structured targeted probe evidence for fixture-level industrial/SOTA checkpoints.",
            [
                sys.executable,
                "scripts/swebench/run_industrial_sota_targeted_probes.py",
                "--out",
                ".groundtruth/industrial_sota_targeted_probes.json",
            ],
            timeout_s=240,
        ),
        _cmd(
            "industrial_validation_gate_smoke",
            "Run evidence-only ledger gate on old smoke artifacts plus structured targeted probes.",
            [
                sys.executable,
                "scripts/swebench/industrial_sota_validation_gate.py",
                "proof_smoke_27798877761_artifacts_current",
            ],
        ),
        _python_inline(
            "fact_gate_sqlite",
            "Verify fact gate admits categorical facts, confidence legacy facts, and fails closed on unknown schema.",
            "\n".join(
                [
                    "import sqlite3",
                    "from groundtruth.evidence.fact_gate import edge_fact_clause",
                    "def count(schema, rows):",
                    "    con=sqlite3.connect(':memory:'); con.execute(schema)",
                    "    for row in rows: con.execute('INSERT INTO edges VALUES ('+','.join('?' for _ in row)+')', row)",
                    "    clause=edge_fact_clause(con, 'e')",
                    "    got=con.execute(f'SELECT COUNT(*) FROM edges e WHERE {clause}').fetchone()[0]",
                    "    con.close(); return got",
                    "assert count('CREATE TABLE edges(type TEXT, resolution_method TEXT, trust_tier TEXT, candidate_count INTEGER)', [('CALLS',' SAME_FILE ','certified',1),('CALLS','name_match','CERTIFIED',9),('CALLS','unknown','SUPPRESSED',1)]) == 1",
                    "assert count('CREATE TABLE edges(type TEXT, confidence REAL)', [('CALLS',0.7),('CALLS',0.1)]) == 1",
                    "assert count('CREATE TABLE edges(type TEXT)', [('CALLS',),('CALLS',)]) == 0",
                    "print('fact_gate_sqlite_ok')",
                ]
            ),
        ),
        _python_inline(
            "ledger_count",
            "Verify local ledger still tracks all 27 items as open/in_progress.",
            "\n".join(
                [
                    "import json",
                    "from collections import Counter",
                    "from pathlib import Path",
                    "data=json.loads(Path('.groundtruth/industrial_sota_ledger.json').read_text())",
                    "statuses=Counter(i.get('status') for i in data.get('items', []))",
                    "assert data.get('remaining') == 27, data.get('remaining')",
                    "assert statuses == {'in_progress': 27}, statuses",
                    "assert not [i.get('id') for i in data.get('items', []) if not i.get('changed_files')]",
                    "print({'remaining': data.get('remaining'), 'statuses': dict(statuses)})",
                ]
            ),
        ),
        _cmd(
            "diff_check",
            "Check whitespace/errors on touched code files.",
            [
                "git",
                "diff",
                "--check",
                "--",
                ".github/workflows/deepswe_full.yml",
                "artifact_deepswe/gt_agent.py",
                "artifact_deepswe/gt_mini_patch.py",
                "gt-index/internal/resolver/promote.go",
                "gt-index/internal/store/sqlite.go",
                "scripts/swebench/artifact_resolver.py",
                "scripts/swebench/gt_intel_lean.py",
                "scripts/swebench/gt_run_proof.py",
                "scripts/swebench/task_truth.py",
                "scripts/swebench/industrial_sota_validation_gate.py",
                "scripts/swebench/run_industrial_sota_targeted_probes.py",
                "scripts/swebench/select_industrial_sota_targets.py",
                "scripts/swebench/paired_industrial_sota_summary.py",
                "scripts/swebench/print_industrial_sota_dispatch.py",
                "scripts/swebench/resolution_method_audit.py",
                "scripts/vm/gt_agent_run.sh",
                "src/groundtruth/evidence/fact_gate.py",
                "src/groundtruth/evidence/format_contract.py",
                "src/groundtruth/evidence/mismatch.py",
                "src/groundtruth/mcp/server.py",
                "src/groundtruth/pretask/v1r_brief.py",
                "src/groundtruth/runtime/verification_horizon.py",
                "tests/test_artifact_resolver.py",
            ],
        ),
    ]


def _summarize(steps: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [s for s in steps if not s["ok"]]
    return {
        "schema": "gt.local_validation_trajectory_summary.v1",
        "step_count": len(steps),
        "ok_count": len(steps) - len(failed),
        "failed_count": len(failed),
        "failed": [
            {
                "check_id": s["check_id"],
                "rc": s["rc"],
                "timed_out": s["timed_out"],
                "purpose": s["purpose"],
            }
            for s in failed
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    steps = _checks()
    summary = _summarize(steps)
    with out.open("w", encoding="utf-8") as fh:
        for step in steps:
            fh.write(json.dumps(step, sort_keys=True) + "\n")
        fh.write(json.dumps(summary, sort_keys=True) + "\n")
    print(json.dumps({"trajectory": str(out), **summary}, indent=2, sort_keys=True))
    return 0 if summary["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

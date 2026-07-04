"""Run open-source Stage-1 localization proof cases.

This runner is intentionally outside the Pro benchmark path. It exercises the
same product brief entrypoint on ordinary OSS repos with explicit gold files.
"""

from __future__ import annotations

import argparse
import json
import os
import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEXER = REPO_ROOT / "gt-index" / "gt-index.exe"


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").lstrip("/")


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _gold_rank(paths: list[str], gold_files: list[str]) -> int | None:
    gold = {_norm(g) for g in gold_files}
    for idx, path in enumerate(paths, start=1):
        if _norm(path) in gold:
            return idx
    return None


def _build_graph(indexer: Path, repo_root: Path, graph_db: Path, max_files: int) -> dict[str, Any]:
    graph_db.parent.mkdir(parents=True, exist_ok=True)
    if graph_db.exists():
        graph_db.unlink()
    started = time.time()
    cp = _run(
        [
            str(indexer),
            "-root",
            str(repo_root),
            "-output",
            str(graph_db),
            "-max-files",
            str(max_files),
        ],
        cwd=REPO_ROOT,
        timeout=900,
    )
    return {
        "returncode": cp.returncode,
        "elapsed_sec": round(time.time() - started, 3),
        "graph_db": str(graph_db),
        "stdout_tail": cp.stdout[-4000:],
        "stderr_tail": cp.stderr[-4000:],
        "exists": graph_db.exists(),
        "size": graph_db.stat().st_size if graph_db.exists() else 0,
    }


def _brief(case: dict[str, Any], graph_db: Path, out_dir: Path, timeout: int = 480) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = out_dir / "case.json"
    payload.write_text(json.dumps(case, indent=2), encoding="utf-8")
    code = r'''
import json
import os
import sys
import traceback
from pathlib import Path

repo_root = Path(sys.argv[1])
payload_path = Path(sys.argv[2])
graph_db = Path(sys.argv[3])
out_dir = Path(sys.argv[4])
sys.path.insert(0, str(repo_root / "src"))

out = {"ok": False}
try:
    from groundtruth.pretask.v1r_brief import generate_v1r_brief
    from groundtruth.runtime.brief_cache import persist_brief
    from groundtruth.runtime.localization_diagnostic import validate_brief_payload
    case = json.loads(payload_path.read_text(encoding="utf-8"))
    result = generate_v1r_brief(
        issue_text=str(case["issue_text"]),
        repo_root=str(case["repo_root"]),
        graph_db=str(graph_db),
        max_files=8,
    )
    persisted = persist_brief(str(out_dir), result.brief_text, result, identity=str(case["id"]))
    full_potential = os.environ.get("GT_FULL_POTENTIAL", "0") == "1"
    strict_diag = full_potential or os.environ.get("GT_LOCALIZATION_DIAGNOSTIC_STRICT", "0") == "1"
    require_semantic = full_potential or os.environ.get("GT_LOCALIZATION_REQUIRE_SEMANTIC", "0") == "1"
    require_gold = bool(case.get("gold_files")) and (
        full_potential or os.environ.get("GT_LOCALIZATION_REQUIRE_GOLD", "0") == "1"
    )
    diagnostic = validate_brief_payload(
        persisted,
        gold_files=[str(x) for x in case.get("gold_files", [])],
        require_gold=require_gold,
        require_semantic=require_semantic,
    )
    (out_dir / "localization_diagnostic.json").write_text(
        json.dumps(diagnostic, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if strict_diag and not diagnostic.get("ok", False):
        raise RuntimeError(
            "live localization diagnostic HALT before brief delivery: "
            + ",".join(str(v) for v in diagnostic.get("violations", []))
        )
    (out_dir / "brief.txt").write_text((result.brief_text or "").strip(), encoding="utf-8")
    paths = [getattr(f, "path", "") for f in result.files]
    out = {
        "ok": True,
        "paths": paths,
        "brief_len": len(result.brief_text or ""),
        "metrics": persisted.get("metrics", {}),
        "localization_diagnostic": str(out_dir / "localization_diagnostic.json"),
        "brief_result": str(out_dir / "brief_result.json"),
        "brief_txt": str(out_dir / "brief.txt"),
    }
except Exception:
    out = {"ok": False, "error": traceback.format_exc()}
print("__GT_OSS_JSON__" + json.dumps(out, sort_keys=True))
'''
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("GT_MODELS_ROOT", str(REPO_ROOT / "models"))
    started = time.time()
    try:
        cp = subprocess.run(
            [sys.executable, "-c", code, str(REPO_ROOT), str(payload), str(graph_db), str(out_dir)],
            cwd=str(REPO_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "elapsed_sec": round(time.time() - started, 3),
            "error": f"timeout after {timeout}s",
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
        }
    parsed: dict[str, Any] | None = None
    for line in reversed(cp.stdout.splitlines()):
        if line.startswith("__GT_OSS_JSON__"):
            parsed = json.loads(line[len("__GT_OSS_JSON__") :])
            break
    if parsed is None:
        parsed = {"ok": False, "error": "missing proof json marker"}
    paths = [str(p) for p in parsed.get("paths") or []]
    parsed.update(
        {
            "elapsed_sec": round(time.time() - started, 3),
            "returncode": cp.returncode,
            "gold_rank": _gold_rank(paths, list(case.get("gold_files") or [])),
            "stdout_tail": cp.stdout[-4000:],
            "stderr_tail": cp.stderr[-4000:],
        }
    )
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=r"D:\gt_runs\oss_stage1_manifest.json")
    parser.add_argument("--out", default=r"D:\gt_runs\oss_stage1_results")
    parser.add_argument("--indexer", default=str(DEFAULT_INDEXER))
    parser.add_argument("--max-files", type=int, default=12000)
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--skip-index", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    cases = list(manifest.get("cases") or [])
    if args.max_cases > 0:
        cases = cases[: args.max_cases]
    out_root = Path(args.out)
    graph_root = out_root / "graphs"
    case_root = out_root / "cases"
    out_root.mkdir(parents=True, exist_ok=True)

    rows = []
    for case in cases:
        cid = str(case["id"])
        repo_root = Path(case["repo_root"])
        repo_key = hashlib.sha1(str(repo_root.resolve()).encode("utf-8", "replace")).hexdigest()[:12]
        graph_db = graph_root / f"{repo_root.name}_{repo_key}" / "graph.db"
        row: dict[str, Any] = {"id": cid, "language": case.get("language"), "gold_files": case.get("gold_files", [])}
        try:
            if graph_db.exists():
                row["index"] = {"skipped": True, "graph_db": str(graph_db), "exists": True, "size": graph_db.stat().st_size}
            else:
                row["index"] = _build_graph(Path(args.indexer), repo_root, graph_db, args.max_files)
            if not row["index"].get("exists") or row["index"].get("returncode", 0) not in (0, None):
                row["brief"] = {"ok": False, "error": "index_failed"}
            else:
                row["brief"] = _brief(case, graph_db, case_root / cid)
        except Exception as exc:
            row["brief"] = {"ok": False, "error": repr(exc)}
        rows.append(row)
        print(json.dumps({"id": cid, "gold_rank": (row.get("brief") or {}).get("gold_rank"), "ok": (row.get("brief") or {}).get("ok")}))

    summary = {
        "schema": "gt.oss_stage1_results.v1",
        "manifest": str(args.manifest),
        "rows": rows,
        "counts": {
            "cases": len(rows),
            "brief_ok": sum(1 for r in rows if (r.get("brief") or {}).get("ok")),
            "gold_at_1": sum(1 for r in rows if (r.get("brief") or {}).get("gold_rank") == 1),
            "gold_in_8": sum(1 for r in rows if (r.get("brief") or {}).get("gold_rank") is not None),
        },
    }
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary["counts"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

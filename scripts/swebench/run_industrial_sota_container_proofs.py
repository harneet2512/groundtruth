#!/usr/bin/env python3
"""Run parallel containerized proof lanes for the industrial/SOTA evidence table.

Default source = SWE-bench Pro task images via the hbali-stack GHCR mirror
(ghcr.io/hbali-stack/sweap-images:<dockerhub_tag>) — reliable pulls, unlike the
open-source lanes that hit image_pull_failed. Pro covers go/js/python/ts (NO rust).
Default selection = 2 tasks per language x 4 languages = 8 lanes. Never the held-out
DeepSWE set; no benchmark labels are used (repos are only a multilingual source).
Produces structured per-lane artifacts under .groundtruth/ so the validator reads
facts from JSON/files, not ad hoc text search.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "artifact_multilingual" / "repo_manifest.json"
DEFAULT_OUT = ROOT / ".groundtruth" / "industrial_sota_container_proofs"
DEFAULT_SUBSTRATE = "7a9fd6a44e4e"
LANGS = ("go", "javascript", "python", "rust", "typescript")

# SWE-bench Pro targets via the hbali-stack GHCR mirror. All 731 Pro images are cached
# public at ghcr.io/hbali-stack/sweap-images:<dockerhub_tag> (see CLAUDE.md operational
# wiring), so this path does NOT hit the open-source image_pull_failed lanes.
# Pro covers go/js/python/ts ONLY — NO rust. repo_language codes are normalized to LANGS.
PRO_TAGS = ROOT / "benchmarks" / "data" / "swebench_pro_public_tags.jsonl"
GHCR_MIRROR = "ghcr.io/hbali-stack/sweap-images"
_PRO_LANG_MAP = {"go": "go", "js": "javascript", "python": "python", "ts": "typescript"}
PRO_LANGS = ("go", "javascript", "python", "typescript")  # rust absent from SWE-bench Pro


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    timeout_s: int = 600,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict[str, Any]:
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
        timed_out = False
        rc: int | None = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = None
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    duration = round(time.time() - start, 3)
    if stdout_path:
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    if stderr_path:
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
    return {
        "schema": "gt.command_result.v1",
        "command": cmd,
        "cwd": str(cwd),
        "rc": rc,
        "timed_out": timed_out,
        "duration_s": duration,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stderr_path": str(stderr_path) if stderr_path else None,
    }


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        raise ValueError(f"manifest has no tasks list: {path}")
    return [t for t in tasks if isinstance(t, dict)]


def _load_pro_targets(path: Path = PRO_TAGS) -> list[dict[str, Any]]:
    """SWE-bench Pro targets via the hbali-stack GHCR mirror.

    All 731 Pro images are cached public at ghcr.io/hbali-stack/sweap-images:<tag>,
    so these lanes do NOT hit the open-source image_pull_failed path. Pro has no rust.
    """
    tasks: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            tag = rec.get("dockerhub_tag")
            lang = _PRO_LANG_MAP.get(str(rec.get("repo_language") or "").lower())
            if not tag or not lang:
                continue
            tasks.append({
                "language": lang,
                "instance_id": rec.get("instance_id"),
                "repo": rec.get("repo"),
                "docker_image": f"{GHCR_MIRROR}:{tag}",
                "commit_hash": "",
                "source": "swebench_pro_ghcr_mirror",
            })
    return tasks


def _select_per_language(
    tasks: list[dict[str, Any]], langs: tuple[str, ...], per: int = 1
) -> list[dict[str, Any]]:
    """Deterministically pick `per` tasks for each language (sorted by instance_id)."""
    by_lang: dict[str, list[dict[str, Any]]] = {}
    for task in sorted(tasks, key=lambda t: str(t.get("instance_id") or "")):
        lang = str(task.get("language") or "").lower()
        if lang in langs:
            by_lang.setdefault(lang, []).append(task)
    short = [l for l in langs if len(by_lang.get(l, [])) < per]
    if short:
        raise ValueError(f"targets lack >={per} tasks for languages: {short}")
    out: list[dict[str, Any]] = []
    for lang in langs:
        out.extend(by_lang[lang][:per])
    return out


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:120]


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _docker_repo_root(container: str) -> str:
    probe = (
        "for d in /home/user /testbed /workspace /app /repo; do "
        "[ -e \"$d/.git\" ] && echo \"$d\" && break; done"
    )
    cp = subprocess.run(
        ["docker", "exec", container, "sh", "-lc", probe],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    root = (cp.stdout or "").strip().splitlines()
    if cp.returncode != 0 or not root:
        raise RuntimeError(f"repo root probe failed rc={cp.returncode}: {cp.stderr[-500:]}")
    return root[0]


def _lang_arg(language: str) -> str:
    return {"javascript": "js", "typescript": "ts"}.get(language, language)


def _run_lane(task: dict[str, Any], out_root: Path, substrate_image: str, proof_timeout_s: int, no_pull: bool = True) -> dict[str, Any]:
    language = str(task.get("language") or "unknown").lower()
    instance_id = str(task.get("instance_id") or task.get("display_title") or language)
    image = str(task.get("docker_image") or "")
    lane = out_root / f"{language}_{_safe_name(instance_id)}"
    src = lane / "src"
    artifacts = lane / "trial_results" / "gt_artifacts"
    lane.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    _write_json(lane / "target.json", {"schema": "gt.container_proof_target.v1", **task})

    result: dict[str, Any] = {
        "schema": "gt.container_proof_lane.v1",
        "language": language,
        "instance_id": instance_id,
        "docker_image": image,
        "lane_dir": str(lane),
        "status": "running",
        "steps": [],
    }

    cname = "gt_src_" + _safe_name(instance_id + "_" + str(os.getpid()))
    proof_name = "gt_proof_" + _safe_name(instance_id + "_" + str(os.getpid()))
    try:
        # Images are expected to be present locally (SWE-bench Pro / prior pulls). Do NOT
        # force-pull — that is what falsely marked present images as image_pull_failed.
        # Use the local image; only pull if it is genuinely missing AND pulling is allowed.
        inspect = _run(["docker", "image", "inspect", image], timeout_s=60)
        if inspect["rc"] == 0:
            result["image_source"] = "local"
        elif no_pull:
            result["status"] = "image_missing_local_no_pull"
            return result
        else:
            pull = _run(["docker", "pull", image], timeout_s=900, stdout_path=lane / "pull.out.log", stderr_path=lane / "pull.err.log")
            result["steps"].append(pull)
            if pull["rc"] != 0:
                result["status"] = "image_pull_failed"
                return result
            result["image_source"] = "pulled"

        _run(["docker", "rm", "-f", cname], timeout_s=30)
        create = _run(
            ["docker", "run", "-d", "--entrypoint", "sh", "--name", cname, image, "-lc", "sleep 1800"],
            timeout_s=120,
            stdout_path=lane / "src_container.out.log",
            stderr_path=lane / "src_container.err.log",
        )
        result["steps"].append(create)
        if create["rc"] != 0:
            result["status"] = "source_container_failed"
            return result

        repo_root = _docker_repo_root(cname)
        result["repo_root_in_image"] = repo_root
        if src.exists():
            shutil.rmtree(src)
        src.mkdir(parents=True, exist_ok=True)
        copy = _run(["docker", "cp", f"{cname}:{repo_root}/.", str(src)], timeout_s=900, stdout_path=lane / "copy.out.log", stderr_path=lane / "copy.err.log")
        result["steps"].append(copy)
        if copy["rc"] != 0:
            result["status"] = "source_copy_failed"
            return result

        issue = lane / "issue.txt"
        issue.write_text(
            f"Industrial SOTA multilingual proof lane for {instance_id}. "
            "Build a strict GT proof bundle without benchmark labels.",
            encoding="utf-8",
        )
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True, stdout=subprocess.PIPE).stdout.strip()
        docker_src = str(src.resolve())
        docker_artifacts = str(artifacts.resolve())
        docker_issue = str(issue.resolve())
        proof_cmd = [
            "docker", "run", "--rm", "--name", proof_name,
            "--memory=8g", "--memory-swap=18g",
            "-v", f"{docker_src}:/work:ro",
            "-v", f"{docker_artifacts}:/gt_artifacts",
            "-v", f"{docker_issue}:/work_issue.txt:ro",
            "-e", "GT_PROOF_MODE=1",
            "-e", "GT_CONTAINERIZED=1",
            "-e", "GT_RUNTIME_STRATEGY=unified_substrate",
            "-e", "GT_HOST_GRAPH_DB=/gt_artifacts/graph.db",
            "-e", "GT_REQUIRE_FTS5=1",
            "-e", "GT_REQUIRE_EMBEDDER=1",
            "-e", "GT_FORCE_ONNX_EMBEDDER=1",
            "-e", "GT_REQUIRE_LSP=1",
            "-e", "GT_REQUIRE_FULL_STACK=1",
            "-e", "GT_ISSUE_FILE=/work_issue.txt",
            "-e", "GT_RRF_FUSION=on",
            "-e", "GT_LOC_FUSION_V2=1",
            "-e", "GT_PROACTIVE_TOPN=1",
            "-e", "GT_GATES_DELIVER_ALWAYS=0",
            "-e", f"GT_GIT_COMMIT={commit}",
            "-e", "GT_REQUIRE_COMMIT_PARITY=0",
            "-e", "GT_REQUIRE_GRAPH_VALID=0",
            "-e", f"GT_SUBSTRATE_DIGEST=local-{substrate_image}",
            "-e", f"GT_TASK_REPO_COMMIT={task.get('commit_hash') or ''}",
            "-e", "HF_HUB_OFFLINE=1",
            "-e", "TRANSFORMERS_OFFLINE=1",
            "-e", "HF_DATASETS_OFFLINE=1",
            "-e", "PATH=/opt/gt/bin:/opt/gt/node/bin:/opt/gt/python/bin:/opt/gt/jre/bin:/opt/gt/go/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            substrate_image,
            "gt-run-proof",
            "--source-root", "/work",
            "--out", "/gt_artifacts",
            "--lang", _lang_arg(language),
        ]
        proof = _run(proof_cmd, timeout_s=proof_timeout_s, stdout_path=lane / "proof.out.log", stderr_path=lane / "proof.err.log")
        result["steps"].append(proof)
        if proof["timed_out"]:
            _run(["docker", "rm", "-f", proof_name], timeout_s=30)

        if (artifacts / "graph.db").is_file():
            audit = _run(
                [sys.executable, "scripts/swebench/resolution_method_audit.py", "--sample-limit", "10", str(artifacts)],
                timeout_s=180,
                stdout_path=artifacts / "resolution_method_audit.json",
                stderr_path=lane / "resolution_audit.err.log",
            )
            result["steps"].append(audit)

        result["artifact_sha256"] = {
            name: _sha256(artifacts / name)
            for name in (
                "proof_verdict.json",
                "run_manifest.json",
                "brief_result.json",
                "graph.db",
                "resolution_method_audit.json",
            )
        }
        result["status"] = "proof_passed" if proof["rc"] == 0 else ("proof_timed_out" if proof["timed_out"] else "proof_failed")
        return result
    except Exception as exc:
        result["status"] = "exception"
        result["exception"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        _run(["docker", "rm", "-f", cname], timeout_s=30)
        _write_json(lane / "container_proof_result.json", result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("pro", "manifest"), default="pro",
                        help="pro = SWE-bench Pro via hbali-stack GHCR mirror (go/js/python/ts, NO rust); "
                             "manifest = open-source repo_manifest.json")
    parser.add_argument("--per-lang", type=int, default=2,
                        help="tasks per language (pro default 2 => 4 langs x 2 = 8 lanes)")
    parser.add_argument("--langs", default="", help="comma override of languages (default per source)")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--substrate-image", default=DEFAULT_SUBSTRATE)
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--proof-timeout-s", type=int, default=1800)
    parser.add_argument("--no-pull", dest="no_pull", action="store_true", default=True,
                        help="(default) never pull; use only local images, fail a lane if its image is missing")
    parser.add_argument("--allow-pull", dest="no_pull", action="store_false",
                        help="permit a docker pull when an image is missing locally")
    args = parser.parse_args(argv)

    if args.source == "pro":
        all_tasks = _load_pro_targets()
        default_langs = PRO_LANGS
        surface = "swebench_pro_ghcr_mirror"
    else:
        all_tasks = _load_manifest(Path(args.manifest))
        default_langs = LANGS
        surface = "artifact_multilingual"
    langs = tuple(x.strip() for x in args.langs.split(",") if x.strip()) or default_langs
    targets = _select_per_language(all_tasks, langs, per=max(1, args.per_lang))

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_root = Path(args.out) / run_id
    out_root.mkdir(parents=True, exist_ok=True)
    _write_json(out_root / "run_manifest.json", {
        "schema": "gt.container_proof_run.v1",
        "run_id": run_id,
        "surface": surface,
        "source": args.source,
        "languages": list(langs),
        "per_lang": max(1, args.per_lang),
        "substrate_image": args.substrate_image,
        "targets": targets,
        "max_workers": args.max_workers,
        "proof_timeout_s": args.proof_timeout_s,
    })

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
        futs = [pool.submit(_run_lane, task, out_root, args.substrate_image, args.proof_timeout_s, args.no_pull) for task in targets]
        for fut in concurrent.futures.as_completed(futs):
            result = fut.result()
            results.append(result)
            _write_json(out_root / "summary.partial.json", {
                "schema": "gt.container_proof_summary.v1",
                "run_id": run_id,
                "completed": len(results),
                "total": len(targets),
                "results": sorted(results, key=lambda r: str(r.get("language"))),
            })

    counts: dict[str, int] = {}
    for result in results:
        status = str(result.get("status"))
        counts[status] = counts.get(status, 0) + 1
    summary = {
        "schema": "gt.container_proof_summary.v1",
        "run_id": run_id,
        "out_root": str(out_root),
        "counts": counts,
        "results": sorted(results, key=lambda r: str(r.get("language"))),
    }
    _write_json(out_root / "summary.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if counts.get("proof_passed", 0) == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prerequisite gate for Groundtruth benchmark launches.

This is a pre-launch audit, not a benchmark and not a proof sweep. It verifies
that the substrate/pipeline wiring is launch-safe before a large DeepSWE or Pro
run is allowed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DIGEST_RE = re.compile(r"^ghcr\.io/[^/]+/gt-substrate@sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

DEEPSWE_CANARIES = {
    "arcane-drift-detection-baselines",
    "awilix-async-container-initialization",
    "boa-hierarchical-evaluation-cancellation",
    "dasel-html-document-format",
}

PRO_CANARIES = {
    "instance_flipt-io__flipt-02e21636c58e86c51119b63e0fb5ca7b813b07b1",
    "instance_navidrome__navidrome-89b12b34bea5687c70e4de2109fd1e7330bb2ba2",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ok(checks: list[dict], name: str, detail: str = "") -> None:
    checks.append({"ok": True, "name": name, "detail": detail})


def _fail(checks: list[dict], name: str, detail: str) -> None:
    checks.append({"ok": False, "name": name, "detail": detail})


def audit(args: argparse.Namespace) -> tuple[list[dict], dict]:
    checks: list[dict] = []

    if DIGEST_RE.match(args.gt_substrate_digest or ""):
        _ok(checks, "immutable_substrate_digest", args.gt_substrate_digest)
    else:
        _fail(checks, "immutable_substrate_digest", "gt_substrate_digest must be ghcr.io/...@sha256:<64 hex>")

    if SHA_RE.match(args.gt_code_commit or ""):
        _ok(checks, "gt_code_commit_shape", args.gt_code_commit)
    else:
        _fail(checks, "gt_code_commit_shape", "gt_code_commit must be a 40-char SHA")
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        head = ""
    if head and head == args.gt_code_commit:
        _ok(checks, "checkout_commit_parity", head)
    else:
        _fail(checks, "checkout_commit_parity", f"checkout={head or '<unknown>'} input={args.gt_code_commit}")

    deepswe_manifest = _json(ROOT / "artifact_deepswe" / "repo_manifest.json")
    deepswe_tasks = deepswe_manifest.get("tasks") or []
    deepswe_ids = {t.get("instance_id") for t in deepswe_tasks}
    if len(deepswe_ids) == 113 and len(deepswe_tasks) == 113:
        _ok(checks, "deepswe_manifest_count", "113 unique tasks")
    else:
        _fail(checks, "deepswe_manifest_count", f"found rows={len(deepswe_tasks)} unique={len(deepswe_ids)}")
    missing = sorted(DEEPSWE_CANARIES - deepswe_ids)
    if not missing:
        _ok(checks, "deepswe_canaries_present", ",".join(sorted(DEEPSWE_CANARIES)))
    else:
        _fail(checks, "deepswe_canaries_present", f"missing {missing}")

    pro_rows = _jsonl(ROOT / "benchmarks" / "data" / "swebench_pro_public_tags.jsonl")
    pro_ids = {r.get("instance_id") for r in pro_rows}
    langs = sorted({r.get("repo_language") for r in pro_rows})
    if len(pro_ids) == 731 and len(pro_rows) == 731:
        _ok(checks, "pro_manifest_count", "731 unique tasks")
    else:
        _fail(checks, "pro_manifest_count", f"found rows={len(pro_rows)} unique={len(pro_ids)}")
    if langs == ["go", "js", "python", "ts"]:
        _ok(checks, "pro_manifest_languages", ",".join(langs))
    else:
        _fail(checks, "pro_manifest_languages", f"found {langs}")
    missing = sorted(PRO_CANARIES - pro_ids)
    if not missing:
        _ok(checks, "pro_canaries_present", ",".join(sorted(PRO_CANARIES)))
    else:
        _fail(checks, "pro_canaries_present", f"missing {missing}")

    substrate = _read(ROOT / "scripts" / "ci" / "substrate_proof.sh")
    if 'GT_TASK_LANGUAGE="${GT_MATRIX_LANGUAGE}"' in substrate:
        _ok(checks, "task_language_passed_into_substrate", "GT_TASK_LANGUAGE is exported to gt-run-proof")
    else:
        _fail(checks, "task_language_passed_into_substrate", "substrate proof must pass GT_TASK_LANGUAGE")
    if "GOMODCACHE=/tmp/gomodcache" in substrate:
        _ok(checks, "go_mod_cache_exported", "mounted Go module cache is exported into proof containers")
    else:
        _fail(checks, "go_mod_cache_exported", "substrate proof must export GOMODCACHE=/tmp/gomodcache")
    if "GOPROXY=https://proxy.golang.org,direct" in substrate and "GOPROXY=off" not in substrate:
        _ok(checks, "go_lsp_live_proxy_matches_metadata_probe", "proof gopls path can load Go package metadata")
    else:
        _fail(
            checks,
            "go_lsp_live_proxy_matches_metadata_probe",
            "substrate proof must not force GOPROXY=off while metadata probe uses live GOPROXY",
        )
    if 'GT_REQUIRE_COMMIT_PARITY="${GT_REQUIRE_COMMIT_PARITY:-1}"' in substrate:
        _ok(checks, "proof_container_commit_parity_enforced", "proof containers receive GT_REQUIRE_COMMIT_PARITY")
    else:
        _fail(checks, "proof_container_commit_parity_enforced", "proof docker run must pass GT_REQUIRE_COMMIT_PARITY")
    for forbidden in ("pip install", "rustup component add rust-src", "load_dataset("):
        if forbidden in substrate:
            _fail(checks, f"substrate_forbidden_{forbidden}", f"found {forbidden!r}")
        else:
            _ok(checks, f"substrate_forbidden_{forbidden}", "absent")
    if "GOFLAGS=-mod=mod" in substrate:
        _fail(checks, "substrate_forbidden_global_go_mod_flag", "global GOFLAGS=-mod=mod breaks Go workspace-mode repos")
    else:
        _ok(checks, "substrate_forbidden_global_go_mod_flag", "absent")

    dockerfile = _read(ROOT / "docker" / "Dockerfile.gt-substrate")
    go_version_match = re.search(r"(?m)^ARG GO_VERSION=(\d+)\.(\d+)(?:\.(\d+))?$", dockerfile)
    if go_version_match and tuple(map(int, go_version_match.groups(default="0")[:2])) >= (1, 25):
        _ok(checks, "substrate_go_toolchain_minimum", f"GO_VERSION={go_version_match.group(0).split('=', 1)[1]}")
    else:
        found = go_version_match.group(0).split("=", 1)[1] if go_version_match else "<missing>"
        _fail(checks, "substrate_go_toolchain_minimum", f"Dockerfile.gt-substrate must bake Go >=1.25, found {found}")
    if "GT_GO_TOOLCHAIN_MIN_OK" in dockerfile and "(1,25)" in dockerfile.replace(" ", ""):
        _ok(checks, "substrate_go_toolchain_build_self_test", "Docker build fails closed when Go <1.25")
    else:
        _fail(checks, "substrate_go_toolchain_build_self_test", "Dockerfile.gt-substrate must self-test the baked Go minimum")
    gopls_version_match = re.search(r"(?m)^ARG GOPLS_VERSION=v(\d+)\.(\d+)\.(\d+)$", dockerfile)
    if gopls_version_match and tuple(map(int, gopls_version_match.groups())) >= (0, 21, 1):
        _ok(checks, "substrate_gopls_go125_compatible", f"GOPLS_VERSION=v{'.'.join(gopls_version_match.groups())}")
    else:
        found = f"v{'.'.join(gopls_version_match.groups())}" if gopls_version_match else "<missing>"
        _fail(checks, "substrate_gopls_go125_compatible", f"Dockerfile.gt-substrate must build gopls compatible with Go 1.25, found {found}")

    proof_py = _read(ROOT / "scripts" / "swebench" / "gt_run_proof.py")
    if "_required_lsp_languages" in proof_py and "required_languages=_required_langs" in proof_py:
        _ok(checks, "primary_language_lsp_scope", "declared task language is hard fail-closed")
    else:
        _fail(checks, "primary_language_lsp_scope", "gt-run-proof must aggregate LSP verdicts by declared task language")
    if "_go_metadata_probe_env" in proof_py and "_go_workspace_mode" in proof_py and "_drop_go_mod_flag" in proof_py:
        _ok(checks, "go_workspace_metadata_probe", "Go workspace-mode probes do not force -mod=mod")
    else:
        _fail(checks, "go_workspace_metadata_probe", "gt-run-proof must avoid GOFLAGS=-mod=mod in Go workspace mode")

    pro_full = _read(ROOT / ".github" / "workflows" / "swebench_pro_full.yml")
    if "load_dataset" not in pro_full and "huggingface" not in pro_full.lower():
        _ok(checks, "pro_full_no_huggingface_runtime", "no HF runtime dependency markers")
    else:
        _fail(checks, "pro_full_no_huggingface_runtime", "Pro full workflow still references HF runtime")
    if "Build offline Pro dataset row" in pro_full and "build_pro_local_dataset.py" in pro_full:
        _ok(checks, "pro_offline_dataset_builder", "uses local Pro-OS helper jsonl")
    else:
        _fail(checks, "pro_offline_dataset_builder", "missing offline Pro dataset row builder")

    wrapper = ROOT / ".github" / "workflows" / "deepswe_substrate_proof_wrapper_sweep.yml"
    if wrapper.exists() and "scripts/ci/substrate_proof.sh" in _read(wrapper):
        _ok(checks, "deepswe_wrapper_proof_uses_production_path", str(wrapper))
    else:
        _fail(checks, "deepswe_wrapper_proof_uses_production_path", "missing production-wrapper DeepSWE proof gate")

    pro_proof = ROOT / ".github" / "workflows" / "swebench_pro_proof_sweep.yml"
    if pro_proof.exists() and "scripts/ci/substrate_proof.sh" in _read(pro_proof):
        _ok(checks, "pro_proof_uses_production_path", str(pro_proof))
    else:
        _fail(checks, "pro_proof_uses_production_path", "missing production-wrapper Pro proof gate")

    prereq_workflow = _read(ROOT / ".github" / "workflows" / "groundtruth_prereq_gate.yml")
    if "proof_canaries:" in prereq_workflow and "scripts/ci/substrate_proof.sh" in prereq_workflow:
        _ok(checks, "prereq_executes_production_proof_canaries", "manual gate runs bounded production proof canaries")
    else:
        _fail(
            checks,
            "prereq_executes_production_proof_canaries",
            "manual prereq gate must execute production substrate_proof.sh canaries before launch",
        )

    meta = {
        "deepswe_canaries": sorted(DEEPSWE_CANARIES),
        "pro_canaries": sorted(PRO_CANARIES),
        "substrate_digest": args.gt_substrate_digest,
        "gt_code_commit": args.gt_code_commit,
    }
    return checks, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gt-substrate-digest", required=True)
    parser.add_argument("--gt-code-commit", required=True)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    checks, meta = audit(args)
    report = {"schema": "gt.prereq_benchmark_gate.v1", "checks": checks, "meta": meta}
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for check in checks:
        tag = "OK" if check["ok"] else "FAIL"
        print(f"{tag:4} {check['name']}: {check.get('detail', '')}")
    failed = [c for c in checks if not c["ok"]]
    if failed:
        print(f"PREREQ_GATE_FAIL: {len(failed)} failed checks", file=sys.stderr)
        return 1
    print("PREREQ_GATE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

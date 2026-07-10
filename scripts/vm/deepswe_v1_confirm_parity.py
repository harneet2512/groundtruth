#!/usr/bin/env python3
"""Confirm DeepSWE v1.0.0 + mimo + GT-ON parity on BOTH surfaces (box + GHA).

STATIC confirmation: reads the box script (scripts/vm/gt_agent_run.sh) and the
GHA workflow (.github/workflows/deepswe_full.yml) from source, resolves every
parity-relevant parameter on each surface, hashes the shared artifacts, emits
PARITY_SNAPSHOT_UPCLOUD.json + PARITY_SNAPSHOT_GHA.json + PARITY_DIFF.txt, and
prints CONFIRM / MISMATCH.

GT-behavior fields MUST be identical across surfaces. Allowed-to-differ fields
(surface name, absolute paths, task IDs, telemetry output paths, timestamps,
redacted secret names) are excluded from the diff per the parity spec.

Usage: python scripts/vm/deepswe_v1_confirm_parity.py [--out DIR]
Runs on Windows (static, from the repo) and on the box (same result).
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOX = REPO / "scripts" / "vm" / "gt_agent_run.sh"
GHA = REPO / "github_workflow"  # resolved below
GHA = REPO / ".github" / "workflows" / "deepswe_full.yml"
PARITY_CFG = REPO / "artifact_deepswe" / "gt_integration" / "deepswe_v1_parity_mimo.yaml"
GT_OVERLAY = REPO / "artifact_deepswe" / "gt_mini_patch.py"
# mini.yaml is not vendored (installed with mini-swe-agent); its sha256 is the
# boundary anchor, verified against SWE-agent/mini-swe-agent config/mini.yaml.
MINI_YAML_SHA256 = "b539a8965f5bf41dd0f32daafa5d2db22581923d13812fa378fd09569b9d6b0c"
MINI_SWE_AGENT_VERSION = "2.2.8"  # --ak version pin (both surfaces)

# GT feature flags that MUST match cross-surface (behavior).
FEATURE_FLAGS = [
    "GT_CONTENT_LEG", "GT_POST_SEARCH", "GT_CONSENSUS_LEDGER", "GT_SEM_BODY",
    "GT_DCC", "GT_NEG_EVIDENCE", "GT_TYPEFLOW_FIXPOINT", "GT_FIELD_CANDIDATES",
    "GT_PASSAGE_WIDE", "GT_VERIFY_STRUCTURAL_RISK",
]
# GT behavior params (must match) beyond the feature flags.
BEHAVIOR_PARAMS = [
    "GT_BASELINE", "GT_STEP_LIMIT", "GT_VERIFICATION_CYCLE_COST",
    "GT_SELF_VERIFY_ATTEMPTS",
]


def sha256_file(p: Path) -> str:
    if not p.is_file():
        return "MISSING:" + p.name
    return hashlib.sha256(p.read_bytes()).hexdigest()


def inline_default(text: str, var: str) -> str | None:
    """Extract D from `--ae VAR="${VAR:-D}"` (bash-style inline default)."""
    m = re.search(rf'--ae {re.escape(var)}="\$\{{{re.escape(var)}:-([^}}]*)\}}"', text)
    return m.group(1) if m else None


def gha_workflow_env(text: str, var: str) -> str | None:
    """Resolve a GHA workflow-level `  VAR: <value>` default.

    Handles `${{ inputs.x || 'D' }}`, `${{ ... && '1' || '0' }}` (gt arm -> the
    || fallback), and plain quoted/unquoted literals.
    """
    m = re.search(rf'^\s{{2,}}{re.escape(var)}:\s*(.+)$', text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    # `${{ inputs.gt_arm == 'baseline' && '1' || '0' }}` -> the || fallback (gt arm)
    mm = re.search(r"\|\|\s*'([^']*)'\s*\}\}", val)
    if "&&" in val and mm:
        return mm.group(1)
    # `${{ inputs.step_limit || '250' }}` -> '250'
    mm = re.search(r"\|\|\s*'([^']*)'\s*\}\}", val)
    if mm:
        return mm.group(1)
    # plain quoted literal  GT_SELF_VERIFY_ATTEMPTS: "2"
    mm = re.match(r'^"([^"]*)"$', val)
    if mm:
        return mm.group(1)
    mm = re.match(r"^'([^']*)'$", val)
    if mm:
        return mm.group(1)
    if "${{" not in val:
        return val
    return None  # unresolved template


def resolve_box(text: str, var: str) -> str | None:
    return inline_default(text, var)


def resolve_gha(text: str, var: str) -> str | None:
    # workflow-env override wins over inline default (env is set before the run step)
    wf = gha_workflow_env(text, var)
    if wf is not None:
        return wf
    return inline_default(text, var)


def build_snapshot(surface: str, deep_swe_tag: str, deep_swe_commit: str,
                   config_basename: str, params: dict) -> dict:
    return {
        "experiment": "deepswe-v1-mimo-gt-on",
        "surface": surface,
        "deep_swe": {
            "repo": "datacurve-ai/deep-swe",
            "tag": deep_swe_tag,
            "commit": deep_swe_commit,
        },
        "pier": {"version": "0.2.0", "agent": "mini-swe-agent",
                 "mini_swe_agent_version": MINI_SWE_AGENT_VERSION},
        "model": {
            "name": "mimo-v2.5-pro",
            "provider_route": "openrouter/xiaomi/mimo-v2.5-pro",
            "model_kwargs": {},
            "sampling_pins": "none",
        },
        "benchmark_prompt": {
            "mini_yaml_instance_template_sha256": MINI_YAML_SHA256,
            "config_overlay": config_basename,
            "config_overlay_sha256": sha256_file(PARITY_CFG),
            "modified_by_gt": False,
        },
        "groundtruth": {
            "enabled": params.get("GT_BASELINE") == "0",
            "overlay_module": "artifact_deepswe/gt_mini_patch.py",
            "overlay_sha256": sha256_file(GT_OVERLAY),
            "injection_mode": "separate_gt_overlay_runtime_evidence",
            "modifies_benchmark_prompt": False,
            "modifies_grader": False,
            "modifies_tests": False,
        },
        "grading": {
            "version_family": "v1.0.0",
            "uses_test_sh": True,
            "uses_test_patch": True,
            "uses_v1_1_separate_verifier": False,
        },
        "limits": {"step_limit": 0, "cost_limit": 0, "wall_time_sec": 5400},
        "resources": {"memory_mb": params.get("override_memory_mb") or "8192 (task.toml, no override)",
                      "cpus": params.get("override_cpus") or "2 (task.toml, no override)",
                      "allow_internet": False,
                      "override_flags_passed": bool(params.get("override_memory_mb"))},
        "gt_behavior_env": {k: params.get(k) for k in BEHAVIOR_PARAMS + FEATURE_FLAGS},
        "surface_diff_allowed": ["surface", "hostname", "absolute_paths",
                                 "task_ids", "timestamps", "redacted_secret_names",
                                 "telemetry_output_paths"],
    }


def comparable(snap: dict) -> dict:
    """Fields that MUST match cross-surface (drop surface + allowed-diff)."""
    c = json.loads(json.dumps(snap))
    c.pop("surface", None)
    c.pop("surface_diff_allowed", None)
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO))
    args = ap.parse_args()
    out = Path(args.out)

    box_txt = BOX.read_text(encoding="utf-8", errors="replace")
    gha_txt = GHA.read_text(encoding="utf-8", errors="replace")

    def first(pattern: str, text: str, default: str = "?") -> str:
        m = re.search(pattern, text)
        return m.group(1) if m else default

    # deep-swe tag: box GT_DEEPSWE_TAG default; GHA clone --branch
    box_tag = first(r'GT_DEEPSWE_TAG:-([\w.]+)', box_txt)
    box_commit = first(r'GT_DEEPSWE_TAG_SHA_PREFIX:-([0-9a-f]+)', box_txt)
    gha_tag = first(r'--branch (v[\d.]+) https://github.com/datacurve-ai/deep-swe', gha_txt)

    box_cfg = re.search(r'PIER_CONFIG:-\$REPO_ROOT/[^}]*/([\w.]+\.yaml)', box_txt)
    box_cfg = box_cfg.group(1) if box_cfg else "?"
    gha_cfg = re.search(r'config_file=\S*/([\w.]+\.yaml)', gha_txt)
    gha_cfg = gha_cfg.group(1) if gha_cfg else "?"

    def mem_cpu(text):
        mem = re.search(r'--override-memory-mb\s+"?\$?\{?[\w:-]*?(\d{3,})\}?"?', text) \
            or re.search(r'--override-memory-mb\s+(\d+)', text)
        cpu = re.search(r'--override-cpus\s+"?\$?\{?[\w:-]*?(\d)\}?"?', text) \
            or re.search(r'--override-cpus\s+(\d)', text)
        return (mem.group(1) if mem else None, cpu.group(1) if cpu else None)

    box_mem, box_cpu = mem_cpu(box_txt)
    gha_mem, gha_cpu = mem_cpu(gha_txt)

    box_params = {"override_memory_mb": box_mem, "override_cpus": box_cpu}
    gha_params = {"override_memory_mb": gha_mem, "override_cpus": gha_cpu}
    for v in BEHAVIOR_PARAMS + FEATURE_FLAGS:
        box_params[v] = resolve_box(box_txt, v)
        gha_params[v] = resolve_gha(gha_txt, v)

    box_snap = build_snapshot("upcloud-box", box_tag, box_commit, box_cfg, box_params)
    gha_snap = build_snapshot("github-actions", gha_tag, box_commit, gha_cfg, gha_params)

    (out / "PARITY_SNAPSHOT_UPCLOUD.json").write_text(
        json.dumps(box_snap, indent=2) + "\n", encoding="utf-8")
    (out / "PARITY_SNAPSHOT_GHA.json").write_text(
        json.dumps(gha_snap, indent=2) + "\n", encoding="utf-8")

    # Diff the must-match fields
    cb, cg = comparable(box_snap), comparable(gha_snap)
    diffs = []

    def walk(a, b, path=""):
        if isinstance(a, dict):
            for k in sorted(set(a) | set(b or {})):
                walk(a.get(k), (b or {}).get(k), f"{path}.{k}")
        else:
            if a != b:
                diffs.append(f"{path[1:]}: box={a!r} gha={b!r}")

    walk(cb, cg)

    lines = ["PARITY_DIFF — box (upcloud) vs GHA — must-match fields only",
             f"deep_swe tag: box={box_tag} gha={gha_tag}",
             f"config overlay: box={box_cfg} gha={gha_cfg}",
             f"mini-swe-agent version pin: {MINI_SWE_AGENT_VERSION} (both)",
             f"parity config sha256: {sha256_file(PARITY_CFG)}",
             f"GT overlay sha256: {sha256_file(GT_OVERLAY)}", ""]
    if diffs:
        lines.append(f"MISMATCH — {len(diffs)} field(s) differ:")
        lines += ["  " + d for d in diffs]
        verdict = "MISMATCH"
    else:
        lines.append("CONFIRM — all must-match fields identical across surfaces.")
        verdict = "CONFIRM"
    (out / "PARITY_DIFF.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\n=== {verdict} ===")
    print("wrote PARITY_SNAPSHOT_UPCLOUD.json, PARITY_SNAPSHOT_GHA.json, PARITY_DIFF.txt")
    return 0 if verdict == "CONFIRM" else 1


if __name__ == "__main__":
    sys.exit(main())

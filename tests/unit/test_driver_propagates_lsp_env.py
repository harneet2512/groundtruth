"""Behavior test: single_run_arm.sh must propagate GT_LSP_ENABLED into the
sweagent yaml so it actually reaches the container.

Why this test exists: a prior version of the driver did
`export GT_LSP_ENABLED=1` at host process level only. sweagent only
propagates yaml env_variables into the container, so the lsp-hybrid arm
silently ran as a nolsp twin — the LSP code path never executed, no
lsp_promotion events were emitted, and the hybrid readiness gate stayed
dormant. The bug survived multiple smokes because every test we wrote
only asserted what the driver SAID it would do, not what it actually did.

This test runs the real yaml-patcher (the same entry point the driver
invokes) against a synthetic cfg and asserts the produced yaml has the
load-bearing env variable set. It cannot be made to pass by changing the
test — if the driver stops writing GT_LSP_ENABLED, this test fails.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_SCRIPT = REPO_ROOT / "scripts" / "swebench" / "_patch_sweagent_cfg.py"


def _minimal_cfg() -> str:
    return """agent:
  tools:
    env_variables: {}
    bundles:
      - path: tools/registry
      - path: tools/groundtruth
"""


def _invoke_patcher(tmp_path: Path, arm: str) -> dict:
    src = tmp_path / "in.yaml"
    dst = tmp_path / "out.yaml"
    src.write_text(_minimal_cfg())
    subprocess.run(
        [
            sys.executable, str(PATCH_SCRIPT),
            str(src), str(dst),
            arm, "run-test", "iid-test",
            str(tmp_path / "telem"), str(tmp_path / "bundle"),
        ],
        check=True,
    )
    return yaml.safe_load(dst.read_text())


class TestDriverPropagatesLspEnv:
    def test_lsp_hybrid_arm_sets_gt_lsp_enabled_to_one(self, tmp_path):
        cfg = _invoke_patcher(tmp_path, "gt-lsp-hybrid")
        env = cfg["agent"]["tools"]["env_variables"]
        assert env.get("GT_LSP_ENABLED") == "1", (
            "lsp-hybrid arm must set GT_LSP_ENABLED=1 in yaml env_variables "
            "so sweagent propagates it into the container. Current env: "
            f"{env!r}"
        )

    def test_nolsp_arm_sets_gt_lsp_enabled_to_zero(self, tmp_path):
        cfg = _invoke_patcher(tmp_path, "gt-nolsp")
        env = cfg["agent"]["tools"]["env_variables"]
        assert env.get("GT_LSP_ENABLED") == "0", (
            "nolsp arm must set GT_LSP_ENABLED=0 explicitly so the hook "
            f"treats this as a nolsp run. Current env: {env!r}"
        )

    def test_arm_specific_env_does_not_leak_other_fields(self, tmp_path):
        """Changing arm only flips GT_ARM + GT_LSP_ENABLED — other env keys are identical.

        Uses identical telem/bundle/run args for both invocations so the only
        difference is the arm string.
        """
        def run(arm, out_name):
            src = tmp_path / f"{out_name}_in.yaml"
            dst = tmp_path / f"{out_name}_out.yaml"
            src.write_text(_minimal_cfg())
            subprocess.run(
                [
                    sys.executable, str(PATCH_SCRIPT),
                    str(src), str(dst),
                    arm, "run-shared", "iid-shared",
                    str(tmp_path / "telem-shared"), str(tmp_path / "bundle-shared"),
                ],
                check=True,
            )
            return yaml.safe_load(dst.read_text())
        lsp = run("gt-lsp-hybrid", "lsp")
        nolsp = run("gt-nolsp", "nolsp")
        lsp_env = lsp["agent"]["tools"]["env_variables"]
        nolsp_env = nolsp["agent"]["tools"]["env_variables"]
        diff = {k for k in set(lsp_env) | set(nolsp_env) if lsp_env.get(k) != nolsp_env.get(k)}
        assert diff == {"GT_ARM", "GT_LSP_ENABLED"}, (
            f"arm flip should only change GT_ARM and GT_LSP_ENABLED; got {diff}"
        )

    def test_bundle_path_swap_preserves_groundtruth_bundle(self, tmp_path):
        """Behavior: the groundtruth bundle path is rewritten; other bundles are untouched."""
        bundle = tmp_path / "custom_bundle"
        src = tmp_path / "in.yaml"
        dst = tmp_path / "out.yaml"
        src.write_text(_minimal_cfg())
        subprocess.run(
            [sys.executable, str(PATCH_SCRIPT), str(src), str(dst),
             "gt-nolsp", "r", "i", str(tmp_path / "t"), str(bundle)],
            check=True,
        )
        cfg = yaml.safe_load(dst.read_text())
        paths = [b["path"] for b in cfg["agent"]["tools"]["bundles"]]
        assert str(bundle) in paths, (
            f"groundtruth bundle path must be swapped to the per-task bundle. "
            f"Paths: {paths!r}"
        )
        assert "tools/registry" in paths, (
            "other bundles must be preserved unchanged"
        )

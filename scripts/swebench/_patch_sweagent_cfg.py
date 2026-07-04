#!/usr/bin/env python3
"""Patch a sweagent yaml with per-task env overrides + bundle path swap.

Invoked by single_run_arm.sh. Extracted to its own file so the behavior
is unit-testable — the tests in tests/unit/test_driver_propagates_lsp_env.py
run this script against synthetic yamls and assert the output shape.

Usage:
  python3 _patch_sweagent_cfg.py <src_yaml> <dst_yaml> <arm> <run_id> <iid> <telemetry_dir> <bundle_path>
"""
import sys

import yaml


def patch_cfg(src: str, dst: str, arm: str, run_id: str, iid: str, tdir: str, bundle_path: str) -> None:
    with open(src) as f:
        cfg = yaml.safe_load(f)
    env = cfg["agent"]["tools"].setdefault("env_variables", {})
    env["GT_ARM"] = arm
    env["GT_RUN_ID"] = run_id
    env["GT_INSTANCE_ID"] = iid
    env["GT_TELEMETRY_DIR"] = tdir
    env["GT_ARM_ON_MATERIAL_EDIT"] = "1"
    # Load-bearing: without this, the lsp-hybrid arm silently runs as a
    # nolsp twin because sweagent only propagates yaml env_variables into
    # the container, not host-process env.
    env["GT_LSP_ENABLED"] = "1" if arm == "gt-lsp-hybrid" else "0"
    for bundle in cfg["agent"]["tools"].get("bundles", []):
        if isinstance(bundle, dict) and bundle.get("path", "").endswith("groundtruth"):
            bundle["path"] = bundle_path
    with open(dst, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


if __name__ == "__main__":
    if len(sys.argv) < 8:
        print("usage: _patch_sweagent_cfg.py <src> <dst> <arm> <run_id> <iid> <telemetry_dir> <bundle_path>", file=sys.stderr)
        sys.exit(2)
    patch_cfg(*sys.argv[1:8])

#!/usr/bin/env python3
"""Legacy-CLI -> canonical hook shim.

The OH run_infer.py V2 wrap (post-edit) hard-codes a *legacy* CLI shape:

    python3 /tmp/gt_hook.py verify --root=<...> --db=/tmp/graph.db --quiet --max-items=N

That CLI was provided by scripts/swebench/gt_intel_lean.py. The new canonical
post-edit/post-view hooks live under groundtruth.hooks.* and use a *different*
CLI:

    python3 -m groundtruth.hooks.post_edit --root=<...> --db=<...> --quiet --max-items=N
    python3 -m groundtruth.hooks.post_view --root=<...> --db=<...> --file=<path>

This shim:
  * accepts the legacy invocation (`verify [...]` or `enrich --file=...`)
  * forwards to the canonical module via `python3 -m groundtruth.hooks.<name>`

It is intentionally tiny so the canonical hooks need no legacy-compat code.

Usage in run_infer.py wrap:

    python3 /tmp/gt_canonical_hook_shim.py verify --root=/testbed \\
            --db=/tmp/graph.db --quiet --max-items=3

    python3 /tmp/gt_canonical_hook_shim.py enrich --root=/testbed \\
            --db=/tmp/graph.db --file=src/foo.py

Env:
    GT_CANONICAL_PYTHON   override the python binary used (default: sys.executable)
    GT_CANONICAL_PYTHONPATH  prepended to PYTHONPATH so groundtruth is importable
                             (default: /tmp/groundtruth/src)

Exit code: forwards subprocess return code; on internal error returns 0 so a
broken shim never breaks the agent's tool result.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


_SUBCMD_TO_MODULE = {
    "verify": "groundtruth.hooks.post_edit",
    "edit": "groundtruth.hooks.post_edit",
    "post-edit": "groundtruth.hooks.post_edit",
    "enrich": "groundtruth.hooks.post_view",
    "view": "groundtruth.hooks.post_view",
    "read": "groundtruth.hooks.post_view",
    "post-view": "groundtruth.hooks.post_view",
}

# Legacy flags accepted but ignored (they were no-ops or covered by the
# canonical CLI under different names). Stripping them here keeps the
# canonical argparse from failing.
_LEGACY_DROP_FLAGS = {
    "--briefing",
    "--reminder",
    "--lean",
    "--repo-map",
    "--findings-json",
}
_LEGACY_DROP_KV_PREFIXES = (
    "--function=",
    "--max-lines=",
    "--log=",
    "--issue-text=",
    "--surface=",
)


def _strip_legacy(args: list[str]) -> list[str]:
    out: list[str] = []
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in _LEGACY_DROP_FLAGS:
            continue
        if any(a.startswith(p) for p in _LEGACY_DROP_KV_PREFIXES):
            continue
        # Bare-form e.g. `--function foo`: drop the value too.
        if a in ("--function", "--max-lines", "--log", "--issue-text", "--surface"):
            skip_next = True
            continue
        out.append(a)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # Bare invocation: nothing to do but exit clean.
        return 0

    subcmd = argv[0]
    if subcmd.startswith("-"):
        # Legacy callers sometimes omit the verb. Default to verify.
        forward_args = argv
        module = "groundtruth.hooks.post_edit"
    else:
        module = _SUBCMD_TO_MODULE.get(subcmd)
        if module is None:
            # Unknown verb; treat as edit-verify and forward the rest.
            module = "groundtruth.hooks.post_edit"
            forward_args = argv
        else:
            forward_args = argv[1:]

    forward_args = _strip_legacy(forward_args)

    py = os.environ.get("GT_CANONICAL_PYTHON") or sys.executable or "python3"
    pythonpath = os.environ.get("GT_CANONICAL_PYTHONPATH", "/tmp/groundtruth/src")
    env = dict(os.environ)
    if pythonpath:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = pythonpath if not existing else pythonpath + os.pathsep + existing

    cmd = [py, "-m", module] + forward_args
    try:
        proc = subprocess.run(cmd, env=env, check=False)
        return int(proc.returncode or 0)
    except Exception:
        # Never let the shim break the agent's tool result.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

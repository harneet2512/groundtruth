#!/usr/bin/env python3
"""Idempotent installer for the GT_PATCH_V6_GT_SRC block in run_infer.py.

The V6_GT_SRC block ships the groundtruth/ python package source tree into
the OH runtime container at /tmp/groundtruth/src/. Without it, the canonical
hook shim's `python3 -m groundtruth.hooks.post_edit / post_view` invocation
ImportErrors because GT_CANONICAL_PYTHONPATH=/tmp/groundtruth/src is empty.

This script reads scripts/swebench/run_infer_v6_gt_src.patch (the canonical
diff), extracts the inserted block, and inserts it into a target run_infer.py
immediately after the GT_PATCH_V3_INDEXER closing marker. Re-running is a
no-op.

Usage:
    python3 scripts/swebench/install_run_infer_gt_src_patch.py /path/to/run_infer.py

Exit codes:
    0  patch already installed OR successfully installed
    1  target file missing, or insertion anchor not found
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
PATCH_FILE = REPO_DIR / "scripts" / "swebench" / "run_infer_v6_gt_src.patch"

START_MARKER = "# >>> GT_PATCH_V6_GT_SRC"
END_MARKER = "# <<< GT_PATCH_V6_GT_SRC"
ANCHOR_AFTER = "# <<< GT_PATCH_V3_INDEXER"


def extract_block_from_patch(patch_path: Path) -> str:
    """Pull the inserted block out of the unified diff.

    The patch contains lines starting with '+'; this strips the leading '+'
    so the result is the literal Python the target file should contain.
    """
    if not patch_path.is_file():
        print(f"FATAL: patch file not found at {patch_path}", file=sys.stderr)
        sys.exit(1)

    in_block = False
    out: list[str] = []
    for line in patch_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("+"):
            continue
        body = line[1:]
        if START_MARKER in body:
            in_block = True
        if in_block:
            out.append(body)
        if in_block and END_MARKER in body:
            break
    if not out:
        print("FATAL: could not extract V6_GT_SRC block from patch file", file=sys.stderr)
        sys.exit(1)
    return "\n".join(out) + "\n"


def already_installed(target_text: str) -> bool:
    return START_MARKER in target_text and END_MARKER in target_text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="Path to run_infer.py to patch")
    ap.add_argument("--dry-run", action="store_true", help="Don't write, just report")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not target.is_file():
        print(f"FATAL: target {target} does not exist", file=sys.stderr)
        return 1

    text = target.read_text(encoding="utf-8")
    if already_installed(text):
        print(f"OK: V6_GT_SRC block already present in {target}; no change")
        return 0

    if ANCHOR_AFTER not in text:
        print(
            f"FATAL: anchor {ANCHOR_AFTER!r} not found in {target}; "
            "this run_infer.py is missing the GT_PATCH_V3_INDEXER block — "
            "install that first via install_run_infer_patch.py",
            file=sys.stderr,
        )
        return 1

    block = extract_block_from_patch(PATCH_FILE)

    anchor_idx = text.index(ANCHOR_AFTER)
    line_end = text.index("\n", anchor_idx) + 1
    new_text = text[:line_end] + block + text[line_end:]

    if args.dry_run:
        print(f"DRY-RUN: would insert V6_GT_SRC block into {target}")
        print(f"  insertion point: byte {line_end} (just after first {ANCHOR_AFTER})")
        print(f"  block size: {len(block)} bytes / {block.count(chr(10))} lines")
        return 0

    backup = target.with_suffix(target.suffix + f".bak_pre_v6gs_{int(time.time())}")
    shutil.copyfile(target, backup)
    target.write_text(new_text, encoding="utf-8")

    verify_text = target.read_text(encoding="utf-8")
    if START_MARKER not in verify_text or END_MARKER not in verify_text:
        print(f"FATAL: post-write verification failed; markers not present", file=sys.stderr)
        return 1

    print(f"OK: V6_GT_SRC block installed in {target}")
    print(f"  backup: {backup}")
    print(f"VERIFY: both markers present ({START_MARKER}, {END_MARKER})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

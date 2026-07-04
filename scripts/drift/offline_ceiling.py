"""Offline drift-ceiling harness — runs the contract-drift lever over a real task's
pre/post state and reports whether material drift fires, WITHOUT a paid agent run.

For each of the 9 NO trajectories: build the base-commit graph (pre-edit), freeze it,
apply the agent's patch, reindex the changed files, and run drift. Records whether
drift fired and what it said. The human/codespace then compares against the gold patch
to judge the ceiling: did drift flag a change that was actually a breaking error?

Modes:
  # build from base + apply the agent patch (most self-contained):
  python offline_ceiling.py --root <repo_at_base> --binary <gt-index> --patch <agent.diff> --name <task>

  # patch already applied on disk + you have the pre-patch graph frozen:
  python offline_ceiling.py --root <repo> --binary <gt-index> --orig-db <base.db> --applied --name <task>

Prints the <gt-drift> block (or "(no material drift)") and a one-line VERDICT.
Always advisory; exit 0 unless setup failed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def _sh(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="offline drift ceiling")
    p.add_argument("--root", required=True, help="repo checkout (base commit, or patched if --applied)")
    p.add_argument("--binary", required=True, help="gt-index binary path")
    p.add_argument("--patch", default="", help="agent patch (.diff) to apply (non-applied mode)")
    p.add_argument("--orig-db", default="", help="pre-edit graph.db (applied mode)")
    p.add_argument("--applied", action="store_true", help="patch already applied on disk")
    p.add_argument("--name", default="task")
    a = p.parse_args(argv)

    os.environ["GT_INDEX_BINARY"] = a.binary
    from groundtruth.hooks.drift_hook import (
        drift_advisory,
        freeze_original,
        original_db_path,
    )

    db = f"/tmp/gt_ceiling_{a.name}.db"

    if not a.applied:
        r = _sh([a.binary, "-root", a.root, "-output", db])
        if r.returncode != 0:
            print(f"[{a.name}] INDEX FAILED: {r.stderr[:200]}")
            return 2
        freeze_original(db, force=True)
        if a.patch:
            ap = _sh(["git", "-C", a.root, "apply", "--whitespace=nowarn", a.patch])
            if ap.returncode != 0:
                ap = _sh(["bash", "-lc", f"cd {a.root!r} && patch -p1 < {a.patch!r}"])
            if ap.returncode != 0:
                print(f"[{a.name}] PATCH APPLY FAILED: {ap.stderr[:200]}")
                return 2
    else:
        if not a.orig_db:
            print(f"[{a.name}] --applied requires --orig-db")
            return 2
        shutil.copyfile(a.orig_db, original_db_path(db))
        shutil.copyfile(a.orig_db, db)  # working starts from base; drift reindexes the edits

    diff = _sh(["git", "-C", a.root, "diff", "--name-only", "HEAD"])
    files = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
    if not files:
        print(f"[{a.name}] NO CHANGED FILES")
        return 0

    out = drift_advisory(a.root, db, files)
    print(f"=== [{a.name}] changed_files={len(files)} ===")
    print(out if out else "(no material drift)")
    print(f"[{a.name}] VERDICT: {'DRIFT_FIRED' if out else 'QUIET'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

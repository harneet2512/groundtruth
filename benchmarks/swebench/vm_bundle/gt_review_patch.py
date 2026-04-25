#!/usr/bin/env python3
"""GT vNext review_patch — runs at submit time inside the bash wrapper.

Called by the GT presubmit wrapper in install.sh, before exec submit.real.
Prints findings to stdout (agent sees them). Exits 0 with findings to
pause submit. Exits 1 to signal "proceed to real submit".

Env: GT_VNEXT=1 required. GT_REVIEW_PATCH_FORCE_SHOW=1 bypasses novelty.
"""
import json
import os
import subprocess
import sys

GT_INTEL_REAL = "/tmp/gt_intel_real.py"
GT_DB = "/tmp/gt_graph.db"
REPO_ROOT = "/testbed"
NOVELTY_PATH = "/tmp/gt_vnext_novelty.json"
META_PATH = "/tmp/gt_vnext_meta.json"
REVIEW_DONE = "/tmp/gt_vnext_review_done"


def main():
    if os.environ.get("GT_VNEXT") != "1":
        sys.exit(1)  # not enabled, proceed to submit

    if os.path.exists(REVIEW_DONE):
        sys.exit(1)  # already ran, proceed to submit

    # Check prerequisites
    if not os.path.exists(GT_INTEL_REAL) or not os.path.exists(GT_DB):
        sys.exit(1)

    # Get changed files
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=5,
        )
        changed = [l for l in r.stdout.strip().split("\n") if l.strip()]
    except Exception:
        changed = []

    if not changed:
        sys.exit(1)  # no changes, proceed

    # Mark done (one-shot)
    open(REVIEW_DONE, "w").close()

    force_show = os.environ.get("GT_REVIEW_PATCH_FORCE_SHOW") == "1"

    # Run gt_intel on changed files
    all_findings = []
    for fpath in changed[:3]:
        try:
            r = subprocess.run(
                ["python3", GT_INTEL_REAL, "--db=" + GT_DB, "--file=" + fpath,
                 "--root=" + REPO_ROOT, "--findings-json", "--surface=review_patch"],
                capture_output=True, text=True, timeout=15, cwd=REPO_ROOT,
            )
            if r.stdout.strip().startswith("["):
                all_findings.extend(json.loads(r.stdout.strip()))
        except Exception:
            pass

    # Novelty filter
    if force_show:
        novel = all_findings
        suppressed = 0
    else:
        seen = set()
        try:
            seen = set(json.loads(open(NOVELTY_PATH).read()))
        except Exception:
            pass
        novel = []
        suppressed = 0
        for f in all_findings:
            loc = f.get("location", {})
            fp = "%s|%s|%s|%s" % (
                f.get("kind", ""), loc.get("file", ""),
                loc.get("line", ""), loc.get("symbol", ""))
            if fp not in seen:
                seen.add(fp)
                novel.append(f)
            else:
                suppressed += 1
        with open(NOVELTY_PATH, "w") as sf:
            sf.write(json.dumps(list(seen)))

    # Write metadata
    meta = {}
    try:
        meta = json.loads(open(META_PATH).read())
    except Exception:
        pass
    meta["review_patch_called_pre_submit"] = True
    meta["review_patch_force_show"] = force_show
    meta["submit_paused_for_review"] = bool(novel)
    meta["review_findings_count"] = len(novel)
    meta["review_all_findings_count"] = len(all_findings)
    meta["review_high_confidence_count"] = sum(
        1 for f in novel if f.get("confidence", 0) >= 0.85)
    meta["review_duplicate_suppressed"] = suppressed
    meta["agent_had_chance_to_respond_to_review_patch"] = bool(novel)
    with open(META_PATH, "w") as mf:
        mf.write(json.dumps(meta))

    if novel:
        lines = ['<gt-evidence surface="review_patch">']
        fix_count = 0
        for f in novel:
            tier = f.get("tier", "INFO")
            kind = f.get("kind", "")
            msg = f.get("message", "")
            loc = f.get("location", {})
            loc_s = ("%s:%s" % (loc.get("file", ""), loc.get("line", ""))
                     if loc.get("line") else loc.get("file", ""))
            conf = f.get("confidence", 0)
            action = f.get("agent_action", "verify").upper().replace("_", " ")
            lines.append("[%s] [%s] %s @ %s (%.2f) -- %s" % (
                tier, kind, msg, loc_s, conf, action))
            if conf >= 0.85:
                fix_count += 1
        if fix_count > 0:
            lines.append("---")
            lines.append("BINDING: %d finding(s) require explicit fix or ACK." % fix_count)
        lines.append("</gt-evidence>")
        if force_show:
            lines.append("[diagnostic: GT_REVIEW_PATCH_FORCE_SHOW=1]")
        lines.append("Review the findings above, then submit again to confirm.")
        print("\n".join(lines))
        sys.exit(0)  # pause submit — agent sees findings
    else:
        sys.exit(1)  # no novel findings, proceed to submit


if __name__ == "__main__":
    main()

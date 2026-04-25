#!/usr/bin/env python3
"""Inject GT vNext review_patch into SWE-agent's submit tool.

v4: Uses the submit tool's existing review stage system.
The review_patch runs on the first submit call and returns findings
as a review message. The agent must submit again to confirm.
"""
import sys

SUBMIT_PATH = "/tmp/SWE-agent/tools/review_on_submit_m/bin/submit"

# This block runs BEFORE the review stage check.
# It adds a GT review stage on the first submit call if GT_VNEXT=1.
GT_REVIEW_BLOCK = r'''
    # ── GT vNext review_patch (v4: uses review stage system) ──
    _gt_review_done = Path("/tmp/gt_vnext_review_done")
    if os.environ.get("GT_VNEXT") == "1" and patch.strip() and not _gt_review_done.exists():
        _gt_review_done.touch()
        gt_real = "/tmp/gt_intel_real.py"
        gt_db = "/tmp/gt_graph.db"
        if os.path.exists(gt_real) and os.path.exists(gt_db):
            import json as _json
            changed_files = [l for l in subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True, text=True, cwd=repo_root,
            ).stdout.strip().split("\n") if l.strip()]
            all_findings = []
            for fpath in changed_files[:3]:
                try:
                    r = subprocess.run(
                        ["python3", gt_real, "--db=" + gt_db, "--file=" + fpath,
                         "--root=" + str(repo_root), "--findings-json",
                         "--surface=review_patch"],
                        capture_output=True, text=True, timeout=15, cwd=str(repo_root),
                    )
                    if r.stdout.strip().startswith("["):
                        all_findings.extend(_json.loads(r.stdout.strip()))
                except Exception:
                    pass
            # Novelty filter
            seen_path = "/tmp/gt_vnext_novelty.json"
            seen = set()
            try:
                seen = set(_json.loads(open(seen_path).read()))
            except Exception:
                pass
            novel = []
            suppressed = 0
            for f in all_findings:
                loc = f.get("location", {})
                fp = "%s|%s|%s|%s" % (
                    f.get("kind",""), loc.get("file",""),
                    loc.get("line",""), loc.get("symbol",""))
                if fp not in seen:
                    seen.add(fp)
                    novel.append(f)
                else:
                    suppressed += 1
            with open(seen_path, "w") as sf:
                sf.write(_json.dumps(list(seen)))
            # Write metadata
            meta_path = "/tmp/gt_vnext_meta.json"
            meta = {}
            try:
                meta = _json.loads(open(meta_path).read())
            except Exception:
                pass
            meta["review_patch_called_pre_submit"] = True
            meta["submit_paused_for_review"] = bool(novel)
            meta["review_findings_count"] = len(novel)
            meta["review_high_confidence_count"] = sum(
                1 for f in novel if f.get("confidence", 0) >= 0.85)
            meta["review_duplicate_suppressed"] = suppressed
            meta["agent_had_chance_to_respond_to_review_patch"] = True
            with open(meta_path, "w") as mf:
                mf.write(_json.dumps(meta))
            # If novel findings exist, show them and exit (agent must submit again)
            if novel:
                lines = ['<gt-evidence surface="review_patch">']
                fix_count = 0
                for f in novel:
                    tier = f.get("tier", "INFO")
                    kind = f.get("kind", "")
                    msg = f.get("message", "")
                    loc = f.get("location", {})
                    loc_s = ("%s:%s" % (loc.get("file",""), loc.get("line",""))
                             if loc.get("line") else loc.get("file",""))
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
                lines.append("Review the findings above. Fix issues or submit again to confirm.")
                print("\n".join(lines))
                sys.exit(0)
            else:
                print("GT review_patch: clean pass (0 novel findings). Proceeding to submit.")

'''

def main():
    with open(SUBMIT_PATH) as f:
        content = f.read()

    marker = '    submit_review_messages = registry.get("SUBMIT_REVIEW_MESSAGES", [])'
    if marker not in content:
        print("ERROR: registry line not found in", SUBMIT_PATH)
        sys.exit(1)

    if "GT vNext review_patch" in content:
        start = content.index("    # ── GT vNext review_patch")
        end = content.index(marker, start)
        content = content[:start] + content[end:]
        print("REMOVED old patch")

    idx = content.index(marker)
    patched = content[:idx] + GT_REVIEW_BLOCK + "\n" + content[idx:]

    with open(SUBMIT_PATH, "w") as f:
        f.write(patched)

    print("PATCHED v4: review_patch as pre-stage gate with sys.exit(0)")

if __name__ == "__main__":
    main()

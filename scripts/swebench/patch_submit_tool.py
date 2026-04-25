#!/usr/bin/env python3
"""Inject GT vNext review_patch into SWE-agent's submit tool.

Adds a review_patch call before the final <<SWE_AGENT_SUBMISSION>> output.
The agent sees review findings and can fix or ACK before the patch is recorded.

v2: Full diagnostics — no silent errors. All debug output visible to agent.
"""
import sys

SUBMIT_PATH = "/tmp/SWE-agent/tools/review_on_submit_m/bin/submit"

GT_REVIEW_BLOCK = '''
    # ── GT vNext review_patch (injected by patch_submit_tool.py v2) ──
    if os.environ.get("GT_VNEXT") == "1" and patch.strip():
        import sys as _sys
        _sys.stderr.write("GT_REVIEW_DEBUG_START\\n")
        gt_real = "/tmp/gt_intel_real.py"
        gt_db = "/tmp/gt_graph.db"
        _sys.stderr.write("cwd: %s\\n" % os.getcwd())
        _sys.stderr.write("repo_root: %s\\n" % repo_root)
        _sys.stderr.write("exists(gt_intel_real.py): %s\\n" % os.path.exists(gt_real))
        _sys.stderr.write("exists(gt_graph.db): %s\\n" % os.path.exists(gt_db))
        _sys.stderr.write("exists(/root/tools/groundtruth): %s\\n" % os.path.isdir("/root/tools/groundtruth"))
        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=repo_root,
        )
        changed_files = [l for l in diff_result.stdout.strip().split("\\n") if l.strip()]
        _sys.stderr.write("changed_files: %s\\n" % changed_files[:5])
        if os.path.exists(gt_real) and os.path.exists(gt_db) and changed_files:
            import json as _json
            all_findings = []
            for fpath in changed_files[:3]:
                cmd = ["python3", gt_real, "--db=" + gt_db, "--file=" + fpath,
                       "--root=" + str(repo_root), "--findings-json",
                       "--surface=review_patch"]
                _sys.stderr.write("cmd: %s\\n" % " ".join(cmd))
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15,
                    cwd=str(repo_root),
                )
                _sys.stderr.write("returncode: %d\\n" % r.returncode)
                _sys.stderr.write("stdout[:2000]: %s\\n" % r.stdout[:2000])
                _sys.stderr.write("stderr[:2000]: %s\\n" % r.stderr[:2000])
                if r.stdout.strip().startswith("["):
                    try:
                        all_findings.extend(_json.loads(r.stdout.strip()))
                    except Exception as je:
                        _sys.stderr.write("JSON parse error: %s\\n" % str(je))
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
            meta["agent_had_chance_to_respond_to_review_patch"] = bool(novel)
            with open(meta_path, "w") as mf:
                mf.write(_json.dumps(meta))
            _sys.stderr.write("review_patch: %d findings, %d novel, %d suppressed\\n" % (
                len(all_findings), len(novel), suppressed))
            # Format and print if findings exist — agent sees this
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
                print("\\n".join(lines))
            else:
                _sys.stderr.write("review_patch: clean pass (0 novel findings)\\n")
        else:
            _sys.stderr.write("review_patch: skipped (missing files or no changes)\\n")
        _sys.stderr.write("GT_REVIEW_DEBUG_END\\n")

'''

def main():
    with open(SUBMIT_PATH) as f:
        content = f.read()

    marker = '    print("<<SWE_AGENT_SUBMISSION>>")'
    if marker not in content:
        print("ERROR: submission marker not found in", SUBMIT_PATH)
        sys.exit(1)

    # Remove old patch if present
    if "GT vNext review_patch" in content:
        # Strip old block
        start = content.index("    # ── GT vNext review_patch")
        end_marker = '    print("<<SWE_AGENT_SUBMISSION>>")'
        end = content.index(end_marker, start)
        content = content[:start] + content[end:]
        print("REMOVED old patch")

    idx = content.index(marker)
    patched = content[:idx] + GT_REVIEW_BLOCK + "\n" + content[idx:]

    with open(SUBMIT_PATH, "w") as f:
        f.write(patched)

    print("PATCHED v2: review_patch with full diagnostics injected into submit tool")

if __name__ == "__main__":
    main()

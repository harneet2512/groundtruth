#!/usr/bin/env python3
"""Patch run_infer.py to use Go gt-index for fast graph.db build + verify with all 7 families.

Replaces the analyze subcommand (slow Python AST index) with:
1. Copy gt-index binary into container
2. Build graph.db (~2-5s via Go + tree-sitter)
3. Wrapper calls verify --db=/tmp/graph.db (all 7 evidence families)
"""
import sys
import py_compile

def main():
    fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"
    with open(fpath) as f:
        src = f.read()

    # 1. Add gt-index binary injection after gt_hook.py injection
    # Find the "GT runtime wrapper installed" log line and inject before it
    marker = 'logger.info("GT runtime wrapper installed")'
    if marker not in src:
        print("ERROR: cannot find runtime wrapper marker")
        return 1

    indexer_inject = '''
                    # Inject gt-index binary and build graph.db
                    _gt_indexer_path = os.environ.get("GT_INDEXER_PATH", "")
                    if _gt_indexer_path and os.path.isfile(_gt_indexer_path):
                        try:
                            runtime.copy_to(_gt_indexer_path, "/tmp/")
                            _idx_act = CmdRunAction(command="chmod +x /tmp/gt-index-linux && /tmp/gt-index-linux -root /workspace/*/ -output /tmp/graph.db 2>&1 | tail -3 && echo INDEX_BUILT")
                            _idx_act.set_hard_timeout(60)
                            _idx_obs = _orig_run_action(_idx_act)
                            if "INDEX_BUILT" in getattr(_idx_obs, "content", ""):
                                logger.info("GT graph.db built via Go indexer")
                            else:
                                logger.warning("GT indexer: %s", getattr(_idx_obs, "content", "")[:200])
                        except Exception as _idx_e:
                            logger.warning("GT indexer injection failed: %s", _idx_e)

'''
    src = src.replace(marker, indexer_inject + "                    " + marker)

    # 2. Change wrapper command from analyze to verify with --db
    old_cmd = 'gt_cmd = "/opt/miniconda3/bin/python3 /tmp/gt_hook.py analyze " + fpath + " --root=/workspace/*/ --quiet"'
    new_cmd = 'gt_cmd = "/opt/miniconda3/bin/python3 /tmp/gt_hook.py verify --root=/workspace/*/ --db=/tmp/graph.db --quiet --max-items=3"'

    if old_cmd in src:
        src = src.replace(old_cmd, new_cmd)
        print("CHANGED: analyze -> verify with --db=/tmp/graph.db")
    else:
        print("WARNING: analyze command not found, searching...")
        if "analyze" in src and "gt_cmd" in src:
            # Find the line
            for i, line in enumerate(src.split("\n")):
                if "gt_cmd" in line and "analyze" in line:
                    print(f"  Line {i}: {line.strip()}")
        else:
            print("  No analyze gt_cmd found at all")

    with open(fpath, "w") as f:
        f.write(src)

    py_compile.compile(fpath, doraise=True)
    print("SYNTAX OK")
    print("PATCHED: Go indexer injection + verify with graph.db")
    return 0

if __name__ == "__main__":
    sys.exit(main())

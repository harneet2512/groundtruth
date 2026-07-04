#!/usr/bin/env python3
"""Apply ALL GT patches to run_infer.py in one shot.

Patches:
1. Hook injection in initialize_runtime (gt_hook.py + watcher)
2. Log extraction in complete_runtime
3. Runtime wrapper for evidence delivery to agent observations
"""
import os
import sys
import py_compile

def main():
    fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"
    bak = fpath + ".bak"

    # Restore from backup if exists
    if os.path.exists(bak):
        with open(bak) as f:
            src = f.read()
        print(f"Restored from {bak}")
    else:
        with open(fpath) as f:
            src = f.read()
        with open(bak, "w") as f:
            f.write(src)
        print(f"Backed up to {bak}")

    # ==========================================
    # PATCH 1: Hook injection in initialize_runtime
    # ==========================================
    init_marker = "# >>> GT_PATCH_V2: initialize_runtime"
    if init_marker not in src:
        target = "    logger.info('END Runtime Initialization Fn')"
        idx = src.find(target)
        if idx < 0:
            print("ERROR: cannot find END Runtime Initialization Fn")
            return 1
        # Find the preceding logger.info('-' * 30)
        block_start = src.rfind("    logger.info('-' * 30)", 0, idx)

        inject = """
    # >>> GT_PATCH_V2: initialize_runtime
    _gt_hook_path = os.environ.get("GT_HOOK_PATH", "")
    if _gt_hook_path and os.path.isfile(_gt_hook_path):
        try:
            import base64 as _b64
            with open(_gt_hook_path, "rb") as _fh:
                _hook_b64 = _b64.b64encode(_fh.read()).decode("ascii")
            _CHUNK = 8000
            _chunks = [_hook_b64[i:i+_CHUNK] for i in range(0, len(_hook_b64), _CHUNK)]
            _ok = True
            for _i, _chunk in enumerate(_chunks):
                _op = ">" if _i == 0 else ">>"
                _act = CmdRunAction(command="echo -n '" + _chunk + "' " + _op + " /tmp/gt_hook.b64")
                _act.set_hard_timeout(30)
                _obs = runtime.run_action(_act)
                if not isinstance(_obs, CmdOutputObservation) or _obs.exit_code != 0:
                    _ok = False
                    break
            if _ok:
                _act = CmdRunAction(command="base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && chmod +x /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64 && echo GT_HOOK_READY")
                _act.set_hard_timeout(30)
                _obs = runtime.run_action(_act)
                if "GT_HOOK_READY" in getattr(_obs, "content", ""):
                    logger.info("GT hook injected: %s", instance.instance_id)

                    # --- Runtime wrapper: evidence in observations ---
                    _orig_run_action = runtime.run_action
                    _gt_n = [0]
                    def _gt_wrapped(action, _orig=_orig_run_action, _counter=_gt_n):
                        obs = _orig(action)
                        try:
                            atype = getattr(action, "action", "")
                            fpath = getattr(action, "path", "")
                            if atype in ("edit", "write") and fpath and fpath.endswith(".py"):
                                _counter[0] += 1
                                # Make path relative to workspace root
                                if "/workspace/" in fpath:
                                    parts = fpath.split("/workspace/", 1)[1]
                                    # Skip the repo dir name (e.g. scikit-learn__scikit-learn__0.21/)
                                    if "/" in parts:
                                        rel = parts.split("/", 1)[1]
                                    else:
                                        rel = parts
                                else:
                                    rel = fpath
                                root_cmd = "git -C /workspace/*/ rev-parse --show-toplevel 2>/dev/null || echo /workspace/*/"
                                gt_cmd = "cd $(" + root_cmd + ") && python3 /tmp/gt_hook.py analyze " + rel + " --root=. --quiet 2>/dev/null || true"
                                ga = CmdRunAction(command=gt_cmd)
                                ga.set_hard_timeout(35 if _counter[0] == 1 else 8)
                                go = _orig(ga)
                                gt_out = getattr(go, "content", "")
                                if gt_out and len(gt_out.strip()) > 20:
                                    existing = getattr(obs, "content", "")
                                    obs.content = existing + "\\n\\n" + gt_out.strip()
                        except Exception:
                            pass
                        return obs
                    runtime.run_action = _gt_wrapped
                    logger.info("GT runtime wrapper installed")
        except Exception as _e:
            logger.warning("GT hook injection error: %s", _e)
    # <<< GT_PATCH_V2: initialize_runtime

"""
        src = src[:block_start] + inject + src[block_start:]
        print("PATCHED: initialize_runtime (hook + wrapper)")
    else:
        print("SKIP: initialize_runtime already patched")

    # ==========================================
    # PATCH 2: Log extraction in complete_runtime
    # ==========================================
    comp_marker = "# >>> GT_PATCH_V2: complete_runtime"
    if comp_marker not in src:
        target = "    logger.info('END Runtime Completion Fn')"
        idx = src.find(target)
        if idx < 0:
            print("ERROR: cannot find END Runtime Completion Fn")
            return 1
        block_start = src.rfind("    logger.info('-' * 30)", 0, idx)

        inject = """
    # >>> GT_PATCH_V2: complete_runtime
    _gt_log_dir = os.environ.get("GT_LOG_DIR", "")
    if _gt_log_dir:
        os.makedirs(_gt_log_dir, exist_ok=True)
        _iid = str(getattr(instance, "instance_id", "unknown"))
        try:
            _act = CmdRunAction(command="cat /tmp/gt_hook_log.jsonl 2>/dev/null || echo EMPTY")
            _act.set_hard_timeout(10)
            _obs = runtime.run_action(_act)
            _out = getattr(_obs, "content", "")
            if _out and _out.strip() and _out.strip() != "EMPTY":
                with open(os.path.join(_gt_log_dir, _iid + ".jsonl"), "w") as _fh:
                    _fh.write(_out)
                logger.info("GT hook log: %s (%d bytes)", _iid, len(_out))
            _act2 = CmdRunAction(command="cat /tmp/gt_hook_stdout.log 2>/dev/null || echo EMPTY")
            _act2.set_hard_timeout(10)
            _obs2 = runtime.run_action(_act2)
            _out2 = getattr(_obs2, "content", "")
            if _out2 and _out2.strip() and _out2.strip() != "EMPTY":
                with open(os.path.join(_gt_log_dir, _iid + "_stdout.log"), "w") as _fh:
                    _fh.write(_out2)
                logger.info("GT stdout log: %s (%d bytes)", _iid, len(_out2))
        except Exception as _e:
            logger.warning("GT log extraction failed: %s: %s", _iid, _e)
    # <<< GT_PATCH_V2: complete_runtime

"""
        src = src[:block_start] + inject + src[block_start:]
        print("PATCHED: complete_runtime (log extraction)")
    else:
        print("SKIP: complete_runtime already patched")

    # Write and validate
    with open(fpath, "w") as f:
        f.write(src)

    py_compile.compile(fpath, doraise=True)
    print("SYNTAX OK")
    print("ALL PATCHES APPLIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

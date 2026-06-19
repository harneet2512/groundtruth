#!/usr/bin/env python3
"""Fix the watcher injection: replace copy_to with base64 echo."""
import sys, py_compile

fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"
with open(fpath) as f:
    src = f.read()

old = '''                    with _tmpmod.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as _wtf:
                        _wtf.write(_watcher_src.replace("\\n", "\\n"))
                        _watcher_path = _wtf.name
                    try:
                        runtime.copy_to(_watcher_path, "/tmp/gt_watcher.py")
                        _act2 = CmdRunAction(command="nohup python3 /tmp/gt_watcher.py > /dev/null 2>&1 & echo WATCHER_PID=$!")
                        _act2.set_hard_timeout(10)
                        _obs2 = runtime.run_action(_act2)
                        if "WATCHER_PID=" in getattr(_obs2, "content", ""):
                            logger.info("GT watcher started")
                    finally:
                        os.unlink(_watcher_path)'''

new = '''                    import base64 as _wb64
                    _watcher_real = _watcher_src.replace("\\n", "\\n")
                    _watcher_b64_str = _wb64.b64encode(_watcher_real.encode()).decode("ascii")
                    _wact = CmdRunAction(command="echo '" + _watcher_b64_str + "' | base64 -d > /tmp/gt_watcher_run.py && chmod +x /tmp/gt_watcher_run.py && nohup python3 /tmp/gt_watcher_run.py > /dev/null 2>&1 & echo WATCHER_PID=$!")
                    _wact.set_hard_timeout(10)
                    _obs2 = runtime.run_action(_wact)
                    if "WATCHER_PID=" in getattr(_obs2, "content", ""):
                        logger.info("GT watcher started")'''

if old in src:
    src = src.replace(old, new)
    with open(fpath, "w") as f:
        f.write(src)
    py_compile.compile(fpath, doraise=True)
    print("FIXED: watcher now uses base64 injection")
else:
    print("Pattern not found - checking if already fixed")
    if "gt_watcher_run.py" in src:
        print("Already fixed")
    else:
        print("ERROR: unexpected state")
        sys.exit(1)

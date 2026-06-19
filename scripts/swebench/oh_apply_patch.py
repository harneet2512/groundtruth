#!/usr/bin/env python3
"""Apply GT hook injection patch to OpenHands run_infer.py.

Usage: python oh_apply_patch.py /path/to/run_infer.py
"""
import sys
import os
import py_compile

INIT_MARKER = "# >>> GT_HOOK_INJECT: initialize_runtime"
COMP_MARKER = "# >>> GT_HOOK_INJECT: complete_runtime"

INIT_BLOCK = '''
    # >>> GT_HOOK_INJECT: initialize_runtime
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
                    logger.info("GT hook injected: %s (%d bytes)", instance.instance_id, len(_hook_b64))
                    # Start watcher via copy_to
                    import tempfile as _tmpmod
                    _watcher_src = (
                        "import os,time,subprocess,json,hashlib,sys\\n"
                        "ROOT='/testbed'\\n"
                        "HOOK='/tmp/gt_hook.py'\\n"
                        "LOG='/tmp/gt_hook_stdout.log'\\n"
                        "JL='/tmp/gt_hook_log.jsonl'\\n"
                        "def md5(p):\\n"
                        " try:\\n"
                        "  h=hashlib.md5()\\n"
                        "  with open(p,'rb') as f:\\n"
                        "   for c in iter(lambda:f.read(8192),b''):\\n"
                        "    h.update(c)\\n"
                        "  return h.hexdigest()\\n"
                        " except: return ''\\n"
                        "def scan(r):\\n"
                        " d={}\\n"
                        " for dp,dns,fns in os.walk(r):\\n"
                        "  dns[:]=[x for x in dns if x not in ('__pycache__','.git','.tox')]\\n"
                        "  for fn in fns:\\n"
                        "   if fn.endswith('.py'):\\n"
                        "    fp=os.path.join(dp,fn); d[fp]=md5(fp)\\n"
                        " return d\\n"
                        "with open('/tmp/gt_watcher.pid','w') as pf: pf.write(str(os.getpid()))\\n"
                        "prev=scan(ROOT)\\n"
                        "with open(LOG,'a') as lf: lf.write('watcher: %d py files\\\\n' % len(prev))\\n"
                        "while True:\\n"
                        " time.sleep(2)\\n"
                        " cur=scan(ROOT)\\n"
                        " ch=[f for f in cur if f not in prev or cur[f]!=prev.get(f)]\\n"
                        " if ch:\\n"
                        "  with open(LOG,'a') as lf: lf.write('changed: %s\\\\n' % str(ch[:3]))\\n"
                        "  for fp in ch[:3]:\\n"
                        "   try:\\n"
                        "    r=subprocess.run([sys.executable,HOOK,'--root='+ROOT,'--file='+fp,'--quiet','--max-items=5'],capture_output=True,text=True,timeout=30)\\n"
                        "    with open(LOG,'a') as lf:\\n"
                        "     if r.stdout: lf.write(r.stdout)\\n"
                        "    with open(JL,'a') as jf: jf.write(json.dumps({'ts':time.time(),'file':fp,'rc':r.returncode,'out_len':len(r.stdout or '')})+'\\\\n')\\n"
                        "   except Exception as e:\\n"
                        "    with open(LOG,'a') as lf: lf.write('err: %s\\\\n' % e)\\n"
                        " prev=cur\\n"
                    )
                    import base64 as _wb64
                    _watcher_bytes = _watcher_src.replace("\\n", "\n").encode()
                    _watcher_b64 = _wb64.b64encode(_watcher_bytes).decode("ascii")
                    _wact = CmdRunAction(command="echo '" + _watcher_b64 + "' | base64 -d > /tmp/gt_watcher_script.py && chmod +x /tmp/gt_watcher_script.py && nohup python3 /tmp/gt_watcher_script.py > /dev/null 2>&1 & echo WATCHER_PID=$!")
                    _wact.set_hard_timeout(10)
                    _obs2 = runtime.run_action(_wact)
                    if "WATCHER_PID=" in getattr(_obs2, "content", ""):
                        logger.info("GT watcher started")
        except Exception as _e:
            logger.warning("GT hook injection error: %s", _e)
    # <<< GT_HOOK_INJECT: initialize_runtime
'''

COMP_BLOCK = '''
    # >>> GT_HOOK_INJECT: complete_runtime
    _gt_log_dir = os.environ.get("GT_LOG_DIR", "")
    if _gt_log_dir:
        os.makedirs(_gt_log_dir, exist_ok=True)
        _iid = str(getattr(instance, "instance_id", "unknown"))
        try:
            _act = CmdRunAction(command="kill $(cat /tmp/gt_watcher.pid 2>/dev/null) 2>/dev/null; sleep 1; cat /tmp/gt_hook_log.jsonl 2>/dev/null || true")
            _act.set_hard_timeout(15)
            _obs = runtime.run_action(_act)
            _out = getattr(_obs, "content", "")
            if _out and _out.strip() and _out.strip() != "true":
                with open(os.path.join(_gt_log_dir, _iid + ".jsonl"), "w") as _fh:
                    _fh.write(_out)
                logger.info("GT hook log: %s (%d bytes)", _iid, len(_out))
            _act2 = CmdRunAction(command="cat /tmp/gt_hook_stdout.log 2>/dev/null || true")
            _act2.set_hard_timeout(15)
            _obs2 = runtime.run_action(_act2)
            _out2 = getattr(_obs2, "content", "")
            if _out2 and _out2.strip() and _out2.strip() != "true":
                with open(os.path.join(_gt_log_dir, _iid + "_stdout.log"), "w") as _fh:
                    _fh.write(_out2)
                logger.info("GT stdout log: %s (%d bytes)", _iid, len(_out2))
        except Exception as _e:
            logger.warning("GT log extraction failed: %s: %s", _iid, _e)
    # <<< GT_HOOK_INJECT: complete_runtime
'''


def main():
    if len(sys.argv) < 2:
        print("Usage: python oh_apply_patch.py /path/to/run_infer.py")
        return 1

    fpath = sys.argv[1]
    with open(fpath) as f:
        source = f.read()

    if INIT_MARKER in source:
        print("SKIP: init patch already applied")
    else:
        lines = source.splitlines(True)
        for i, line in enumerate(lines):
            if "END Runtime Initialization Fn" in line and "logger.info" in line:
                lines.insert(i - 1, INIT_BLOCK + "\n")
                print(f"PATCHED: initialize_runtime at line {i}")
                break
        source = "".join(lines)

    if COMP_MARKER in source:
        print("SKIP: complete patch already applied")
    else:
        lines = source.splitlines(True)
        for i, line in enumerate(lines):
            if "END Runtime Completion Fn" in line and "logger.info" in line:
                lines.insert(i - 1, COMP_BLOCK + "\n")
                print(f"PATCHED: complete_runtime at line {i}")
                break
        source = "".join(lines)

    # Write
    with open(fpath, "w") as f:
        f.write(source)

    # Validate
    try:
        py_compile.compile(fpath, doraise=True)
        print("SYNTAX OK")
    except py_compile.PyCompileError as e:
        print(f"SYNTAX ERROR: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

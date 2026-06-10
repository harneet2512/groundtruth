#!/usr/bin/env python3
"""Fix the _iid ordering bug in the complete_runtime GT patch."""
import sys, py_compile, os, signal

# Kill running eval processes first
os.system("pkill -9 -f run_infer 2>/dev/null")

fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"
with open(fpath) as f:
    src = f.read()

# Fix: move _iid assignment BEFORE the debug line that uses it
old = '        logger.info("GT_EXTRACT_V3: will extract for iid=%s to dir=%s", _iid, _gt_log_dir)\n        _iid = str(getattr(instance, "instance_id", "unknown"))'
new = '        _iid = str(getattr(instance, "instance_id", "unknown"))\n        logger.info("GT_EXTRACT_V3: will extract for iid=%s to dir=%s", _iid, _gt_log_dir)'

if old in src:
    src = src.replace(old, new)
    print("Fixed _iid ordering")
else:
    print("Pattern not found - checking alternatives")
    if '_iid = str(getattr(instance' in src and 'GT_EXTRACT_V3: will extract' in src:
        print("Already fixed or different format")
    else:
        print("WARNING: could not find pattern to fix")

with open(fpath, "w") as f:
    f.write(src)

py_compile.compile(fpath, doraise=True)
print("SYNTAX OK")

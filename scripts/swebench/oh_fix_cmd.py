#!/usr/bin/env python3
"""Fix the wrapper command: use direct path, no cd/git/redirect."""
import sys
import py_compile

fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"
with open(fpath) as f:
    src = f.read()

# Find and replace the complex command construction
# Old: cd $(git...) && python3 /tmp/gt_hook.py analyze <rel> --root=. --quiet 2>/dev/null || true
# New: python3 /tmp/gt_hook.py analyze <fpath> --root=/workspace/*/ --quiet

old_lines = [
    '                                root_cmd = "git -C /workspace/*/ rev-parse --show-toplevel 2>/dev/null || echo /workspace/*/"',
    '                                gt_cmd = "cd $(" + root_cmd + ") && python3 /tmp/gt_hook.py analyze " + rel + " --root=. --quiet 2>/dev/null || true"',
]
old_block = "\n".join(old_lines)

# Simple: pass the full path directly to analyze, let it figure out the root
new_block = '                                gt_cmd = "python3 /tmp/gt_hook.py analyze " + fpath + " --root=/workspace/*/ --quiet"'

if old_block in src:
    src = src.replace(old_block, new_block)
    print("FIXED: simplified command")
else:
    # Try to find what's actually there
    for line in src.split("\n"):
        if "gt_cmd" in line and "analyze" in line:
            print(f"Found gt_cmd line: {line.strip()}")
    print("Pattern not found exactly, checking alternatives...")

    # Alternative: just replace any line that constructs gt_cmd with analyze
    lines = src.split("\n")
    for i, line in enumerate(lines):
        if "gt_cmd" in line and "analyze" in line and "root_cmd" not in line:
            print(f"  Line {i}: {line.strip()}")
        if "root_cmd" in line and "git -C" in line:
            print(f"  Root cmd at line {i}: {line.strip()}")
            # Replace this and next line
            lines[i] = "                                # simplified: direct path"
            if i + 1 < len(lines) and "gt_cmd" in lines[i + 1]:
                lines[i + 1] = '                                gt_cmd = "python3 /tmp/gt_hook.py analyze " + fpath + " --root=/workspace/*/ --quiet"'
                print("FIXED via line replacement")
                src = "\n".join(lines)
                break

with open(fpath, "w") as f:
    f.write(src)

py_compile.compile(fpath, doraise=True)
print("SYNTAX OK")

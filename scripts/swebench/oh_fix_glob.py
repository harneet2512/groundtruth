#!/usr/bin/env python3
"""Fix glob expansion: /workspace/*/ doesn't expand in CmdRunAction.
Use $(ls -d /workspace/*/ | head -1) instead."""
import sys
import py_compile

fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"
with open(fpath) as f:
    src = f.read()

# Replace all /workspace/*/ with $(ls -d /workspace/*/ | head -1)
# But only in command strings, not in Python code
count = 0

# Fix in the indexer injection (initialize_runtime)
old1 = "-root=/workspace/*/ -output=/tmp/graph.db"
new1 = "-root=$(ls -d /workspace/*/ | head -1) -output=/tmp/graph.db"
if old1 in src:
    src = src.replace(old1, new1)
    count += 1

# Fix in the wrapper verify command
old2 = "--root=/workspace/*/ --db=/tmp/graph.db"
new2 = "--root=$(ls -d /workspace/*/ | head -1) --db=/tmp/graph.db"
if old2 in src:
    src = src.replace(old2, new2)
    count += 1

# Also: suppress gt-index stdout so only verify output is captured
# Change: gt-index ...; python3 verify
# To: gt-index ... >/dev/null; python3 verify
old3 = "-output=/tmp/graph.db 2>/dev/null;"
new3 = "-output=/tmp/graph.db >/dev/null 2>&1;"
if old3 in src:
    src = src.replace(old3, new3)
    count += 1

with open(fpath, "w") as f:
    f.write(src)

py_compile.compile(fpath, doraise=True)
print(f"FIXED: {count} replacements, SYNTAX OK")

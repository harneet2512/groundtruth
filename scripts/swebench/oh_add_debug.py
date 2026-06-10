#!/usr/bin/env python3
"""Add heavy debug logging to the complete_runtime GT patch."""
import sys

fpath = sys.argv[1] if len(sys.argv) > 1 else "evaluation/benchmarks/swe_bench/run_infer.py"

with open(fpath) as f:
    src = f.read()

if "GT_EXTRACT_V3" in src:
    print("Debug already applied")
    sys.exit(0)

# Add debug right after the patch marker
src = src.replace(
    "# >>> GT_HOOK_INJECT: complete_runtime\n",
    '# >>> GT_HOOK_INJECT: complete_runtime\n'
    '    logger.info("GT_EXTRACT_V3: entering extraction, GT_LOG_DIR=%s", os.environ.get("GT_LOG_DIR", "<UNSET>"))\n'
)

# Add debug inside the if block, right after makedirs
src = src.replace(
    "os.makedirs(_gt_log_dir, exist_ok=True)\n",
    'os.makedirs(_gt_log_dir, exist_ok=True)\n'
    '        logger.info("GT_EXTRACT_V3: will extract for iid=%s to dir=%s", _iid, _gt_log_dir)\n'
)

# Add debug after the first cat command
src = src.replace(
    '_out = getattr(_obs, "content", "")\n',
    '_out = getattr(_obs, "content", "")\n'
    '            logger.info("GT_EXTRACT_V3: cat hook_log result: len=%d first100=%s", len(_out), repr(_out[:100]))\n',
    1  # only first occurrence
)

# Add debug after second cat
src = src.replace(
    '_out2 = getattr(_obs2, "content", "")\n',
    '_out2 = getattr(_obs2, "content", "")\n'
    '            logger.info("GT_EXTRACT_V3: cat stdout_log result: len=%d first100=%s", len(_out2), repr(_out2[:100]))\n',
    1
)

# Add debug in except block
src = src.replace(
    'logger.warning("GT log extraction failed: %s: %s", _iid, _e)',
    'logger.warning("GT_EXTRACT_V3 FAILED: %s: %s", _iid, _e)\n'
    '            import traceback; logger.warning("GT_EXTRACT_V3 traceback: %s", traceback.format_exc())'
)

with open(fpath, "w") as f:
    f.write(src)

import py_compile
py_compile.compile(fpath, doraise=True)
print("Debug V3 applied, syntax OK")

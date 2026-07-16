"""Process-level determinism pin for interface_preservation._warned_files.

Real defect (smoke30_ss128_20260716, 12/30 tasks failed the workflow parity gate):
``gt_deep_metrics`` (the deep-metrics embedder) and ``gt_performance_metrics`` (the
standalone performance writer) each call ``compute_performance_metrics`` in a SEPARATE
process. ``interface_preservation._warned_files`` was emitted as ``list(<set>)`` whose
iteration order is ``PYTHONHASHSEED``-dependent, so the two writers embedded the SAME
file set in DIFFERENT orders and the byte-exact parity gate (deep.performance == perf)
failed. The fix canonicalizes the order (``sorted``) at the PRODUCER so every downstream
serialization inherits it.

This reconstructs the minimal producer input (the internal timeline shape) and pins that
the producer yields identical, sorted output across hash seeds -- reliably RED on
``list(set)``, GREEN on ``sorted()``. Generic file paths only: the invariant must hold
for ANY file set (no repo/task names).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SWE = ROOT / "scripts" / "swebench"

# Seven generic warned files. >=5 distinct strings make the hash-seeded set-iteration
# order differ across processes pre-fix (empirically verified). NOT sorted as written.
_WARNED_FILES = [
    "src/zeta.py",
    "src/alpha.py",
    "src/mike.py",
    "src/bravo.py",
    "src/yankee.py",
    "src/delta.py",
    "src/oscar.py",
]


def _run_producer_warned_files(hash_seed: int) -> list[str]:
    """Run the interface-preservation producer in a fresh process at ``hash_seed``.

    Builds the minimal trajectory that drives the CALLERS-warning timeline path:
    each observation carries a ``<gt-contract>[CALLERS] ...</gt-contract>`` block and is
    immediately followed by an assistant edit to the warned file. ``consumption=None``
    forces the deterministic timeline path (no runtime ledger present).
    """
    script = r"""
import json, os, sys
sys.path.insert(0, os.environ["GT_TEST_SWE"])
import gt_performance_metrics as pm

files = json.loads(os.environ["GT_TEST_FILES"])
messages = []
for f in files:
    messages.append({"role": "tool",
                     "content": "<gt-contract>[CALLERS] 3 callers in 2 files for " + f + "</gt-contract>"})
    args = json.dumps({"command": "str_replace", "path": f})
    messages.append({"role": "assistant", "content": "edit",
                     "tool_calls": [{"function": {"arguments": args}}]})

timeline = pm._parse_timeline(messages)
result = pm._compute_interface_preservation(timeline, None)
sys.stdout.buffer.write(json.dumps(result["_warned_files"]).encode("utf-8"))
"""
    env = os.environ.copy()
    env.update({
        "PYTHONHASHSEED": str(hash_seed),
        "GT_TEST_SWE": str(SWE),
        "GT_TEST_FILES": json.dumps(_WARNED_FILES),
    })
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    return json.loads(out)


def test_warned_files_identical_and_sorted_across_two_hash_seeds() -> None:
    """The two-writer defect, exactly: two processes, two hash seeds, one file set.

    Pre-fix (``list(set)``) the two orders diverge -> RED. Post-fix (``sorted``) both
    are the canonical sorted list -> GREEN.
    """
    seed_a = _run_producer_warned_files(hash_seed=1)
    seed_b = _run_producer_warned_files(hash_seed=2)

    assert seed_a == seed_b, (
        "interface_preservation._warned_files diverged across processes: "
        f"{seed_a!r} != {seed_b!r} (set-iteration-order leak)"
    )
    assert seed_a == sorted(_WARNED_FILES)


def test_warned_files_stable_across_many_hash_seeds() -> None:
    """Stronger pin: every hash seed yields the identical canonical sorted order."""
    expected = sorted(_WARNED_FILES)
    for seed in (0, 1, 7, 42, 1000):
        assert _run_producer_warned_files(hash_seed=seed) == expected

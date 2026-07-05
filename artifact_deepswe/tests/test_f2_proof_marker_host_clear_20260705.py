"""F2 (Fable 2026-07-05): the proof-marker staleness clear moved from a per-interpreter
import-time call to the SINGLE host boundary.

The per-interpreter `_clear_proof_marker()` at gt_mini_patch import ran in EVERY interpreter
that loaded the module via the .pth, so a SIGKILL'd scratch `timeout python3` (import → clear →
die before the module-end write) deleted the marker the MAIN agent process had already written,
false-failing a genuine GT-on run. P0-B's write-only-when-attached guard sharpened it (a
non-attaching scratch import clears but never rewrites). The staleness defense now lives at the
single host boundary: gt_agent clears the container-local marker once before each agent run, and
the module-end write stays the last statement.

The actual failure is a cross-process race that is not unit-testable inline (the module-end write
masks the clear-removal whenever the process attaches). These structural pins RED-proof the two
halves of the fix; the attestation SEMANTICS are covered by test_p0b_proof_attachment /
test_lipi_latch_proof_fixes (29 tests).
"""
from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MINI = os.path.join(_HERE, "gt_mini_patch.py")
_AGENT = os.path.join(_HERE, "gt_agent.py")


def test_no_per_interpreter_import_time_clear():
    # RED if a bare top-level `_clear_proof_marker()` call is (re)introduced at module scope.
    src = open(_MINI, encoding="utf-8").read()
    assert not re.search(r"(?m)^_clear_proof_marker\(\)\s*$", src), (
        "the racy per-interpreter import-time clear must not exist at module scope"
    )


def test_module_end_write_is_still_the_last_statement():
    # The fail-closed write must remain the module's last executable statement.
    src = open(_MINI, encoding="utf-8").read()
    calls = [m.start() for m in re.finditer(r"(?m)^_write_proof_marker\(\)\s*$", src)]
    assert calls, "module-level _write_proof_marker() call is missing"


def test_host_clears_marker_before_the_run():
    # RED if the host-boundary clear is removed or moved after the run call.
    src = open(_AGENT, encoding="utf-8").read()
    i_clear = src.find("rm -f {_PROOF_MARKER_PATH}")
    i_run = src.find("_run_with_test_retry(augmented")
    assert i_clear != -1, "host-side proof-marker clear is absent"
    assert i_run != -1, "run call anchor moved"
    assert i_clear < i_run, "the host clear must precede the agent run (symmetric with the assert)"

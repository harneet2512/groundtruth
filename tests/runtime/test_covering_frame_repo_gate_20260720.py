"""D-K (run6 audit): the covering-RED 'where-to-fix' frame must be the agent's
own repo source, never a third-party dep frame or pseudo-file.

Live kill: privacyidea covering RED delivered `pyasn1/.../encoder.py:952` +
`argon2.py:716` (site-packages deps) as the fix site; dynaconf delivered a
`<stdin>`/heredoc-class frame. Those mislead the agent to a file it cannot edit.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from groundtruth.runtime.native_render import (  # noqa: E402
    render_covering_failure_native,
    _frame_under_repo,
)

_RESULT = {
    "stdout_tail": "",
    "stderr_tail": (
        "E   TypeError: bad kwarg\n"
        '  File "pyasn1/codec/ber/encoder.py", line 952, in encode\n'
        "pyasn1/codec/ber/encoder.py:952: in encode\n"
        "privacyidea/lib/tokenclass.py:88: in update\n"
    ),
}


def _repo(tmp_path):
    (tmp_path / "privacyidea" / "lib").mkdir(parents=True)
    (tmp_path / "privacyidea" / "lib" / "tokenclass.py").write_text("x=1\n")
    return str(tmp_path)


def test_third_party_frame_dropped_from_covering_red(tmp_path):
    root = _repo(tmp_path)
    block = render_covering_failure_native(_RESULT, edited_symbol="update", repo_root=root)
    assert "pyasn1" not in block, "third-party dep frame must not be the where-to-fix"
    assert "tokenclass.py" in block, "the real in-repo frame must survive"


def test_legacy_no_repo_root_unchanged(tmp_path):
    # backward-compat: without repo_root the frame is kept (legacy behavior)
    block = render_covering_failure_native(_RESULT, edited_symbol="update")
    assert "pyasn1" in block


def test_frame_under_repo_predicate(tmp_path):
    root = _repo(tmp_path)
    assert _frame_under_repo("privacyidea/lib/tokenclass.py", root) is True
    assert _frame_under_repo("pyasn1/codec/ber/encoder.py", root) is False
    assert _frame_under_repo("<stdin>", root) is False
    assert _frame_under_repo("privacyidea/lib/tokenclass.py", None) is True  # legacy

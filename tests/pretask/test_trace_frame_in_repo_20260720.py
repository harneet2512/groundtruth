"""D-2 (run6 audit, hydra): a THIRD-PARTY dep frame stripped of its install
prefix must NOT be delivered as an in-repo stack frame.

Live kill: fixing hydra, GT delivered `omegaconf/base.py` (site-packages, 0 rows
in graph.db) labeled "deepest in-repo frame" @conf 0.7. The package-being-fixed
IS the repo (loguru/_datetime.py exists under a loguru testbed); a third-party
dep is NOT vendored (omegaconf/ absent under a hydra testbed). Discriminator =
existence under repo_root.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from groundtruth.pretask.traces import parse_stack_traces, _is_in_repo  # noqa: E402


_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/opt/conda/lib/python3.11/site-packages/omegaconf/base.py", line 662, in _resolve\n'
    "    raise\n"
    '  File "/opt/conda/lib/python3.11/site-packages/thepkg/core.py", line 12, in run\n'
    "    boom()\n"
    "ValueError: x\n"
)


def _make_repo(tmp_path, present: list[str]):
    for rel in present:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    return str(tmp_path)


def test_third_party_dep_frame_dropped(tmp_path):
    # repo is 'thepkg' -> thepkg/core.py exists; omegaconf is a third-party dep, absent
    root = _make_repo(tmp_path, ["thepkg/core.py"])
    frames = parse_stack_traces(_TRACE, root)
    files = [f.file for f in frames]
    assert "thepkg/core.py" in files, "the repo's own stripped frame must survive"
    assert "omegaconf/base.py" not in files, (
        "a third-party dep frame (absent under repo_root) must be dropped, not "
        "delivered as in-repo")


def test_package_being_fixed_frame_kept(tmp_path):
    # fixing loguru: loguru/_datetime.py IS the repo source -> kept
    root = _make_repo(tmp_path, ["loguru/_datetime.py"])
    tb = ('Traceback (most recent call last):\n'
          '  File "/x/site-packages/loguru/_datetime.py", line 9, in aware_now\n'
          '    raise ValueError("x")\n')
    frames = parse_stack_traces(tb, root)
    assert any(f.file == "loguru/_datetime.py" for f in frames)


def test_is_in_repo_predicate(tmp_path):
    root = _make_repo(tmp_path, ["pkg/real.py"])
    assert _is_in_repo("pkg/real.py", root) is True
    assert _is_in_repo("omegaconf/base.py", root) is False  # absent -> not in repo

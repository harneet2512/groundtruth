"""P0-B (2026-07-05): the proof marker must attest hook ATTACHMENT, not module load.

`_install()` can patch ZERO env classes (import-shape drift / a harness whose env class
isn't in `_ENV_CLASSES`) and return silently — the swallow-all `except: pass`. The OLD
`_write_proof_marker` then wrote the sentinel ANYWAY, so a silently GT-off run was graded
GT-on. Fix: `_install` records `_PATCHED_CLASSES`; the marker is written iff >=1 class
attached and its content carries `patched=<n>:<classes>`; the host verdict probe requires
`patched=[1-9]` (not mere presence).

RED on the pre-fix writer (remove the `if not _PATCHED_CLASSES: return` guard):
`test_p0b_zero_attachment_writes_no_marker` fails because the 0-attach process writes the
marker. Proven by reverting that one line.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
_ARTIFACT_DIR = os.path.join(_HERE, "..")
sys.path.insert(0, _ARTIFACT_DIR)

import gt_mini_patch as g  # noqa: E402


def test_p0b_zero_attachment_writes_no_marker(monkeypatch, tmp_path):
    """proof mode + GT-on arm, but NO env class attached -> marker NOT written."""
    marker = str(tmp_path / "gt_proof_active")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_PATCHED_CLASSES", [])  # zero hooks attached this process
    g._write_proof_marker(marker)
    assert not os.path.exists(marker), "a 0-attach (GT-off) process must not attest GT-on"


def test_p0b_attachment_writes_marker_with_count(monkeypatch, tmp_path):
    """>=1 class attached -> marker written, content carries patched=<n>:<classes>."""
    marker = str(tmp_path / "gt_proof_active")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(
        g, "_PATCHED_CLASSES",
        ["minisweagent.environments.local.LocalEnvironment"],
    )
    g._write_proof_marker(marker)
    assert os.path.exists(marker)
    content = open(marker, encoding="utf-8").read()
    assert "patched=1:" in content, content
    assert "LocalEnvironment" in content, content


def test_p0b_install_records_attachment():
    """`_install()` (ran at import) populates `_PATCHED_CLASSES` with the classes it
    patched. minisweagent is installed in the test env, so >=1 env class attached."""
    assert isinstance(g._PATCHED_CLASSES, list)
    assert any("Environment" in c for c in g._PATCHED_CLASSES), \
        f"expected >=1 patched env class, got {g._PATCHED_CLASSES!r}"


def test_p0b_host_verdict_requires_patched_count():
    """The host-side `_assert_proof_marker` probe requires `patched=[1-9]` (attachment),
    not mere file presence — a bare/`patched=0` marker fails closed."""
    ga_src = open(os.path.join(_ARTIFACT_DIR, "gt_agent.py"), encoding="utf-8").read()
    assert "patched=[1-9]" in ga_src, "host marker probe must assert patched>=1"

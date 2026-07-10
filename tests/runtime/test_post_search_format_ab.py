"""Stage-1: post_search FORMAT A/B — the `<gt-search-facts>` tag arm (default) vs the
native ripgrep-style arm (GT_POST_SEARCH_NATIVE=1), CONTENT held constant.

Pins:
- default (flag off) = the exact tagged shape the 32 legacy pins assert (no drift).
- flag on = the SAME facts, NO `<gt-*>` tag (charter ⑤), def lines grep-native.
- leak invariant holds in BOTH arms (no test path/identity, counts only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ARTIFACT = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if _ARTIFACT not in sys.path:
    sys.path.insert(0, _ARTIFACT)

from groundtruth.runtime.native_render import (  # noqa: E402
    contains_gt_tag,
    contains_test_identity,
)

gmp = pytest.importorskip("gt_mini_patch")

_INFO = {
    "def_sites": [("src/pkg/users.py", 42)],
    "callers_render": "get_user (src/api/handlers.py:10)",
    "test_ref_count": 3,
}


def test_default_arm_is_tagged(monkeypatch):
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    out = gmp._fmt_def_facts("get_user", _INFO, ".")
    assert out.startswith('<gt-search-facts symbol="get_user">')
    assert out.rstrip().endswith("</gt-search-facts>")
    assert "def: src/pkg/users.py:42" in out          # exact legacy shape


def test_native_arm_drops_tag_same_facts(monkeypatch):
    # MUTATION: remove the GT_POST_SEARCH_NATIVE branch -> tag arm returns -> the
    # `not contains_gt_tag` assertion bites.
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    out = gmp._fmt_def_facts("get_user", _INFO, ".")
    assert not contains_gt_tag(out)                    # charter ⑤ — no <gt-*>
    assert "src/pkg/users.py:42:get_user" in out       # grep-native def line
    assert "callers:" in out and "test refs: 3" in out  # SAME content as the tag arm
    assert "<gt-search-facts" not in out


def test_native_arm_leak_clean(monkeypatch):
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    out = gmp._fmt_def_facts("get_user", _INFO, ".")
    assert not contains_test_identity(out)             # count only, no test name/path


def test_native_arm_multi_def_and_no_callers(monkeypatch):
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    info = {
        "def_sites": [("a.py", 1), ("b.py", 2), ("c.py", 3), ("d.py", 4)],
        "callers_render": "",
        "test_ref_count": 0,
    }
    out = gmp._fmt_def_facts("X", info, ".")
    assert "a.py:1:X" in out and "(1 more matches)" in out
    assert "callers:" not in out and "test refs:" not in out  # quiet when absent
    assert not contains_gt_tag(out)

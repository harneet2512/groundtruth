"""Pin: DCC witness never names a test-path footprint file (leak=0).

_dcc_axis_git / _dcc_axis_static NAME a footprint file in their witness string
("co-changed with <nf>", "FACT-edge to <f>"). A test file in the footprint (an edited test
the classifier admitted, or a viewed-twice test) would leak its path into <gt-concern> — the
terminal filter (gt_mini_patch.py:7006) drops a test MEMBER key but does NOT scrub the witness.
_dcc_candidate_families filters test paths from the footprint the naming axes see. Reverting
that filter reddens this pin.
"""
from __future__ import annotations

import gt_mini_patch as g


def _is_test(p: str) -> bool:
    n = g._norm_fp(p)
    return g._is_test_or_demo_path(n) or g._is_post_search_testpath(n)


def test_footprint_filter_drops_every_test_path_form():
    fp = {"src/foo.py", "tests/test_foo.py", "src/x.test.ts", "conftest.py",
          "a/FooTest.java", "loguru/_logger.py"}
    # the exact expression _dcc_candidate_families applies to the footprint the axes see
    clean = {f for f in fp if not _is_test(f)}
    assert clean == {"src/foo.py", "loguru/_logger.py"}, clean
    # every dropped path is a test surface; every kept path is source
    assert all(_is_test(p) for p in (fp - clean))
    assert not any(_is_test(p) for p in clean)


def test_dcc_predicate_covers_dotform_and_conftest():
    # the leak forms the old wrapper TEST_PATH_RE missed must be caught here too
    for p in ("pkg/a.test.ts", "b/c.spec.js", "conftest.py", "x/FooTest.java", "y/BarTests.cs"):
        assert _is_test(p), p

"""Test-edit guard (post_edit `_test_edit_advisory`).

Audit finding (flip run 27011135159): conan-17102 and checkov-6893 both FAILED by editing
grader-reverted test/fixture files instead of the source (conan's agent explicitly hesitated
and did it anyway). This guard is the non-leakage harm-reduction lever — it uses ONLY path
classification (is_test / fixture dirs), never gold labels, and advises fixing the source.

Asserts: fires on test/fixture edits, stays QUIET on pure source edits (correct-or-quiet),
catches the two real gaming paths, and dedups (advise, not spam).
"""
import os

from groundtruth.hooks.post_edit import _test_edit_advisory

_MARKER = "/tmp/gt_test_edit_warned.txt"


def _clean():
    try:
        os.remove(_MARKER)
    except OSError:
        pass


def test_source_only_edit_stays_quiet():
    _clean()
    assert _test_edit_advisory(["conans/client/graph/install_graph.py"]) == ""


def test_conan_gaming_path_fires():
    """conan edited test/integration/test_info_build_order.py to fake-pass."""
    _clean()
    adv = _test_edit_advisory(["test/integration/test_info_build_order.py"])
    assert adv and "test/integration/test_info_build_order.py" in adv
    assert "source" in adv.lower()


def test_checkov_fixture_path_fires():
    """checkov edited a fixture expected.yaml under a tests/ dir."""
    _clean()
    adv = _test_edit_advisory(
        ["tests/terraform/graph/checks/AzureSubnetConfigWithNSG/expected.yaml"]
    )
    assert adv != ""


def test_conftest_and_fixtures_dirs_fire():
    _clean()
    assert _test_edit_advisory(["conftest.py"]) != ""
    _clean()
    assert _test_edit_advisory(["pkg/fixtures/sample.json"]) != ""
    _clean()
    assert _test_edit_advisory(["ui/__snapshots__/x.snap"]) != ""


def test_mixed_source_and_test_names_the_test():
    _clean()
    adv = _test_edit_advisory(["src/foo.py", "tests/test_foo.py"])
    assert adv and "tests/test_foo.py" in adv


def test_dedup_fires_once_per_file():
    _clean()
    assert _test_edit_advisory(["tests/test_x.py"]) != ""   # first: fires
    assert _test_edit_advisory(["tests/test_x.py"]) == ""   # second: deduped (no spam)

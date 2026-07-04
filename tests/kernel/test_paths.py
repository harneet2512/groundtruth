"""Stress tests for ``control.paths.normalize``.

Layers per locked decision 6: happy / boundary / adversarial / mutation.
Mutation pins live alongside the asserts; flipping the constants in
``control/paths.py`` (the prefix regex, the leading-slash strip) must break
at least one of these.
"""

from __future__ import annotations

import pytest

from groundtruth.control.paths import normalize


# happy
def test_happy_relative_path_unchanged() -> None:
    assert normalize("src/foo.py") == "src/foo.py"


def test_happy_strips_workspace_prefix() -> None:
    assert normalize("workspace/src/foo.py") == "src/foo.py"


def test_happy_strips_testbed_prefix() -> None:
    assert normalize("testbed/src/foo.py") == "src/foo.py"


# boundary
def test_boundary_strips_leading_slash_then_prefix() -> None:
    assert normalize("/testbed/src/x.py") == "src/x.py"


def test_boundary_backslash_to_forward() -> None:
    assert normalize("src\\foo\\bar.py") == "src/foo/bar.py"


def test_boundary_empty_string() -> None:
    assert normalize("") == ""


# adversarial -- the bugs the old _norm shipped
def test_adversarial_leading_dots_preserved() -> None:
    """``lstrip('./')`` would strip leading dots; we must not."""
    assert normalize("..foo.py") == "..foo.py"
    assert normalize(".hidden") == ".hidden"


def test_adversarial_workspaces_not_a_prefix() -> None:
    """``workspaces/`` (with trailing s) is not the ``workspace/`` prefix."""
    assert normalize("workspaces/x.py") == "workspaces/x.py"


def test_adversarial_only_strips_one_prefix() -> None:
    """Nested ``workspace/testbed/...`` strips only the first component."""
    assert normalize("workspace/testbed/x.py") == "testbed/x.py"


def test_adversarial_double_leading_slash_strips_one() -> None:
    """Only one leading slash is stripped -- preserves the rest."""
    assert normalize("//testbed/x.py") == "/testbed/x.py"


# mutation pins -- if the regex anchor is removed (^ dropped) or made greedy
# this test breaks
def test_mutation_pin_prefix_must_be_at_start() -> None:
    assert normalize("src/workspace/x.py") == "src/workspace/x.py"


# parametrized adversarial sweep -- protects against future drift
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("workspace/", ""),
        ("testbed/", ""),
        ("workspace", "workspace"),  # no slash -> no strip
        ("testbed", "testbed"),
        ("./foo.py", "./foo.py"),  # leading "./" preserved (was eaten by lstrip)
    ],
)
def test_parametrized_edges(raw: str, expected: str) -> None:
    assert normalize(raw) == expected

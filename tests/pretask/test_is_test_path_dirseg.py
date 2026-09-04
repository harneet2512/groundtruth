"""Pin: v1r_brief._is_test_path must catch a test file by its DIRECTORY segment, not
just the basename. The csstree witness (2026-06-15) leaked `test/lexer.js` into the
brief's "Also changes:" cochange line because the old `"/test/" in p` substring required
a LEADING slash and a relative top-level `test/lexer.js` (basename `lexer.js`, no test
marker) slipped through. These paths are swap-invariant: never surfaced to the agent."""

from groundtruth.pretask.v1r_brief import _is_test_path


def test_relative_top_level_test_dir_is_caught():
    # the exact csstree leak: relative, no leading slash, basename has no test marker
    assert _is_test_path("test/lexer.js") is True


def test_nested_and_jest_test_dirs_caught():
    assert _is_test_path("__tests__/awilix.test.ts") is True
    assert _is_test_path("a/b/__tests__/c.js") is True
    assert _is_test_path("tests/foo.py") is True
    assert _is_test_path("pkg/spec/bar.rb") is True


def test_basename_markers_still_caught():
    assert _is_test_path("foo/bar_test.go") is True
    assert _is_test_path("x/y.test.ts") is True
    assert _is_test_path("a/b.spec.js") is True


def test_real_source_files_are_not_test():
    # the gold/source files from the two witnesses — must NOT be filtered out
    assert _is_test_path("lib/lexer/Lexer.js") is False
    assert _is_test_path("src/container.ts") is False
    assert _is_test_path("lib/utils/List.js") is False
    # a source dir that merely CONTAINS the substring 'test' is not a test dir
    assert _is_test_path("src/test_helpers/x.go") is False
    assert _is_test_path("src/latest/build.py") is False

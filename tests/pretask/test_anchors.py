"""Stage A unit tests for Module 1 (anchors)."""

from __future__ import annotations

from groundtruth.pretask.anchors import extract_issue_anchors


def test_anchors_extract_camelcase() -> None:
    """CamelCase identifiers in prose are recognized as symbol candidates."""
    text = "FSMContext is broken when get_data is called twice."
    out = extract_issue_anchors(text, graph_db_path=None)
    # Without a DB, we fall through to symbols_raw; ``symbols`` returns
    # the raw set unchanged when graph_db_path is None.
    assert "FSMContext" in out.symbols
    assert "get_data" in out.symbols


def test_anchors_drop_natural_language() -> None:
    """All-lower short prose words must NOT be treated as symbols."""
    text = "the issue is broken and the fix did not work"
    out = extract_issue_anchors(text, graph_db_path=None)
    assert out.symbols == set()


def test_anchors_resolve_against_graph(tiny_graph_db: str) -> None:
    """Unknown identifier filtered when not in nodes.name."""
    text = "SafeWatchdog._fd is bad. NotARealSymbol is also bad."
    out = extract_issue_anchors(text, graph_db_path=tiny_graph_db)
    assert "SafeWatchdog" in out.symbols
    assert "_fd" in out.symbols
    assert "NotARealSymbol" not in out.symbols
    # Sanity: the dotted form resolved its tail too.
    assert "SafeWatchdog._fd" not in out.symbols  # not a node name


def test_anchors_extract_paths() -> None:
    """Backtick-wrapped and bare ``*.py`` paths are extracted."""
    text = "see `patroni/watchdog.py` and also tests/test_x.py"
    out = extract_issue_anchors(text, graph_db_path=None)
    assert "patroni/watchdog.py" in out.paths
    assert "tests/test_x.py" in out.paths


def test_anchors_extract_test_names() -> None:
    """Pytest-style test_* names are pulled out."""
    text = "regression in test_storage_persists; also see test_login_v2."
    out = extract_issue_anchors(text, graph_db_path=None)
    assert "test_storage_persists" in out.test_names
    assert "test_login_v2" in out.test_names

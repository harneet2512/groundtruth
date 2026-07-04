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
    """English closed-class FUNCTION words are dropped even without a DB. DOMAIN /
    prose words (issue, broken, fix, work) are NO LONGER dropped by a blocklist —
    the graph cross-check removes them in production. The old 190-word _STOPWORDS
    that dropped them was poison: it also dropped real short/domain SYMBOLS (run,
    get, set). See test_anchors_resolve_against_graph for the graph filter and
    tests/test_anchors_specificity.py for the per-repo generic-hub drop."""
    text = "the issue is broken and the fix did not work"
    out = extract_issue_anchors(text, graph_db_path=None)
    # function words gone; any survivor is a DOMAIN token the GRAPH (not a list) filters.
    for fw in ("the", "and", "did", "not", "was", "are", "with"):
        assert fw not in out.symbols


def test_anchors_resolve_against_graph(tiny_graph_db: str) -> None:
    """Unknown identifier filtered when not in nodes.name."""
    text = "SafeWatchdog._fd is bad. NotARealSymbol is also bad."
    out = extract_issue_anchors(text, graph_db_path=tiny_graph_db)
    assert "SafeWatchdog" in out.symbols
    assert "_fd" in out.symbols
    assert "NotARealSymbol" not in out.symbols
    # CONTRACT UPDATED (fix 2026-06-10, §4 anchor-extraction defect): the dotted
    # form is now KEPT when the graph CONFIRMS the qualified pair (SafeWatchdog
    # defines _fd in this fixture). The old assertion (`not in symbols — not a
    # node name`) encoded the bug where the issue's most specific anchor was
    # dropped and W_CODE_DEF never engaged. Unconfirmable dotted tokens are
    # still dropped — see test_rerank_localization_fixes.py::
    # test_unconfirmed_dotted_anchor_stays_dropped.
    assert "SafeWatchdog._fd" in out.symbols


def test_anchors_extract_paths() -> None:
    """Backtick-wrapped and bare ``*.py`` paths are extracted."""
    text = "see `patroni/watchdog.py` and also tests/test_x.py"
    out = extract_issue_anchors(text, graph_db_path=None)
    assert "patroni/watchdog.py" in out.paths
    assert "tests/test_x.py" in out.paths


def test_anchors_normalize_github_blob_url() -> None:
    """Forge blob/raw URLs are reduced to the repo-relative path so the path
    anchor matches a graph ``file_path`` (repo-relative). Without normalization
    the raw URL is kept and never matches, killing the path anchor (KINK #4).

    Covers both URL forms (scheme-prefixed and protocol-relative) plus GitLab
    (``-/blob/``), Bitbucket (``/src/``), and ``raw.githubusercontent.com`` —
    repo-agnostic, no task-specific keys.
    """
    cases = {
        "https://github.com/arviz-devs/arviz/blob/main/arviz/plots/hdiplot.py":
            "arviz/plots/hdiplot.py",
        "//github.com/arviz-devs/arviz/blob/abc123def456/arviz/plots/hdiplot.py":
            "arviz/plots/hdiplot.py",
        "http://github.com/o/r/blob/v1.2.3/pkg/sub/file.go":
            "pkg/sub/file.go",
        "https://github.com/o/r/blob/main/a/b.py#L42":
            "a/b.py",
        "https://gitlab.com/group/proj/-/blob/master/src/app/main.rs":
            "src/app/main.rs",
        "https://bitbucket.org/team/repo/src/main/lib/util.ts":
            "lib/util.ts",
        "https://raw.githubusercontent.com/o/r/main/x/y/z.js":
            "x/y/z.js",
    }
    for url, expected in cases.items():
        out = extract_issue_anchors(url, graph_db_path=None)
        assert expected in out.paths, f"{url!r} did not normalize to {expected!r}; got {out.paths!r}"
        # The raw host-prefixed string must NOT survive as a path.
        assert not any(p.startswith("//") or "github.com" in p for p in out.paths), (
            f"raw URL leaked into paths for {url!r}: {out.paths!r}"
        )


def test_anchors_plain_path_untouched_by_url_normalizer() -> None:
    """Repo-relative paths (incl. ones containing a ``src/`` segment) are NOT
    altered by the forge-URL normalizer — it fires only on real ``//host/`` URLs.
    """
    text = "see `patroni/watchdog.py` and src/app/core/main.go and tests/test_x.py"
    out = extract_issue_anchors(text, graph_db_path=None)
    assert "patroni/watchdog.py" in out.paths
    assert "src/app/core/main.go" in out.paths
    assert "tests/test_x.py" in out.paths


def test_anchors_extract_test_names() -> None:
    """Pytest-style test_* names are pulled out."""
    text = "regression in test_storage_persists; also see test_login_v2."
    out = extract_issue_anchors(text, graph_db_path=None)
    assert "test_storage_persists" in out.test_names
    assert "test_login_v2" in out.test_names

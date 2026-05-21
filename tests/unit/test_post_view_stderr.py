"""BUG-C4 proof: graph_navigation_error must go to stdout, not stderr."""
import os
import tempfile


def test_graph_navigation_error_goes_to_stdout(capsys, tmp_path):
    """Pre-fix: error goes to stderr (invisible inside container).
    Post-fix: error goes to stdout (captured by wrapper's _run_internal)."""
    from groundtruth.hooks.post_view import graph_navigation

    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_text("NOT A SQLITE DATABASE")

    lines, count = graph_navigation("fake/file.py", str(corrupt_db))
    captured = capsys.readouterr()

    assert "[GT_META] graph_navigation_error" in captured.out, (
        "BUG-C4: graph_navigation_error goes to stderr, not stdout"
    )
    assert "[GT_META] graph_navigation_error" not in captured.err
    assert lines == []
    assert count == 0

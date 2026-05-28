"""Tests for cross-file scope in L1 brief."""
from dataclasses import dataclass, field

from groundtruth.pretask.v1r_brief import render_brief, FileEntry


def _make_entry(path: str, **kwargs) -> FileEntry:
    return FileEntry(
        path=path,
        score=kwargs.get("score", 0.5),
        functions=kwargs.get("functions", []),
        test_mappings=kwargs.get("test_mappings", []),
        callees=kwargs.get("callees", []),
        co_changes=kwargs.get("co_changes", []),
        contract=kwargs.get("contract", ""),
        pattern=kwargs.get("pattern", ""),
        spec=kwargs.get("spec", ""),
    )


class TestScopeInBrief:
    def test_high_confidence_scope(self):
        entries = [_make_entry("src/app.py", functions=["main"])]
        result = render_brief(
            entries,
            scope_files=["src/utils.py", "src/config.py"],
            scope_confidence="high",
        )
        assert "Likely multi-file scope: utils.py, config.py" in result

    def test_medium_confidence_scope(self):
        entries = [_make_entry("src/app.py")]
        result = render_brief(
            entries,
            scope_files=["src/utils.py"],
            scope_confidence="medium",
        )
        assert "Related files to inspect: utils.py" in result

    def test_low_confidence_suppressed(self):
        entries = [_make_entry("src/app.py")]
        result = render_brief(
            entries,
            scope_files=["src/utils.py"],
            scope_confidence="low",
        )
        assert "multi-file scope" not in result
        assert "Related files" not in result

    def test_no_scope_files(self):
        entries = [_make_entry("src/app.py")]
        result = render_brief(entries, scope_files=[], scope_confidence="high")
        assert "multi-file scope" not in result

    def test_scope_before_closing_tag(self):
        entries = [_make_entry("src/app.py")]
        result = render_brief(
            entries,
            scope_files=["src/b.py", "src/c.py"],
            scope_confidence="high",
        )
        lines = result.split("\n")
        closing_idx = next(i for i, l in enumerate(lines) if "</gt-task-brief>" in l)
        scope_idx = next(i for i, l in enumerate(lines) if "multi-file scope" in l)
        assert scope_idx < closing_idx

    def test_scope_max_3_files(self):
        entries = [_make_entry("src/app.py")]
        result = render_brief(
            entries,
            scope_files=["a.py", "b.py", "c.py", "d.py", "e.py"],
            scope_confidence="high",
        )
        # Should show only basenames of first 3
        assert "a.py, b.py, c.py" in result
        assert "d.py" not in result

    def test_scope_with_directive(self):
        """Scope and directive can coexist when top entry is [VERIFIED]."""
        # Per Cursor-style philosophy, directive only fires on [VERIFIED] top
        # entry. Add a function-name contract so it qualifies.
        entries = [_make_entry(
            "src/app.py",
            test_mappings=["tests/test_app.py"],
            contract="run() in src/main.py:10 `app.run()`",
        )]
        scores = [0.9, 0.3]  # big gap → high confidence directive
        result = render_brief(
            entries,
            scores=scores,
            scope_files=["src/utils.py"],
            scope_confidence="high",
        )
        assert "multi-file scope" in result
        assert "Edit src/app.py first" in result

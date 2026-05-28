"""Tests for v1r_brief per-entry confidence tier — DOC_OF_HONOR §2.1.

Verifies that:
- [VERIFIED] tag is used when graph backing is strong
- [WARNING] tag is used when backing is mid
- [INFO] tag is used when only lexical/semantic retrieval signal exists
- Honest fallback note appears when all entries are [INFO]
- Directive ending only fires on [VERIFIED] top entry
"""
from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _entry_confidence_tier,
    render_brief,
)


def test_verified_when_contract_has_function_names():
    """contract with 'foo() in bar.py:42' format → [VERIFIED]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["bar"],
        contract="parse_query() in src/api.py:55 `query = parse_query(input)`",
    )
    assert _entry_confidence_tier(entry, "issue text") == "[VERIFIED]"


def test_verified_when_issue_match_with_contract():
    """Issue-text function name match + any contract → [VERIFIED]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["parse_query"],
        contract="src/other.py:55",
    )
    assert _entry_confidence_tier(entry, "fix the parse_query bug") == "[VERIFIED]"


def test_warning_when_contract_only_file_paths():
    """Contract with file:line only (no 'in' marker) → [WARNING]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["bar"],
        contract="src/other.py:55",
    )
    assert _entry_confidence_tier(entry, "unrelated issue") == "[WARNING]"


def test_warning_when_test_mapping_only():
    """No contract but test mapping present → [WARNING]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["bar"],
        test_mappings=["tests/test_foo.py"],
    )
    assert _entry_confidence_tier(entry, "issue") == "[WARNING]"


def test_warning_when_issue_match_no_contract():
    """Issue-text symbol match but no caller contract → [WARNING]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["parse_query"],
        contract="",
    )
    assert _entry_confidence_tier(entry, "fix parse_query bug") == "[WARNING]"


def test_info_when_no_graph_evidence():
    """Pure retrieval-score match, no callers/tests/issue-match → [INFO]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["bar"],
    )
    assert _entry_confidence_tier(entry, "unrelated issue") == "[INFO]"


def test_render_brief_includes_tier_tags():
    """render_brief output should prefix each entry with [VERIFIED]/[WARNING]/[INFO]."""
    files = [
        FileEntry(
            path="src/foo.py",
            score=0.9,
            functions=["bar"],
            contract="caller() in other.py:1 `bar()`",
        ),
        FileEntry(path="src/baz.py", score=0.3, functions=["qux"]),
    ]
    out = render_brief(files, scores=[0.9, 0.3])
    assert "[VERIFIED] 1. src/foo.py" in out
    assert "[INFO] 2. src/baz.py" in out


def test_render_brief_all_info_emits_honest_note():
    """When all entries lack graph backing, honest note appears."""
    files = [
        FileEntry(path="src/a.py", score=0.5),
        FileEntry(path="src/b.py", score=0.4),
        FileEntry(path="src/c.py", score=0.3),
    ]
    out = render_brief(files)
    assert "GT could not anchor any candidate" in out
    assert "[INFO] 1." in out


def test_render_brief_directive_only_on_verified_top():
    """'Edit X first' only when top entry is [VERIFIED] AND score gap large."""
    files = [
        FileEntry(path="src/a.py", score=0.9, functions=["x"]),
        FileEntry(path="src/b.py", score=0.3, functions=["y"]),
    ]
    out = render_brief(files, scores=[0.9, 0.3])
    assert "Edit src/a.py first" not in out


def test_render_brief_directive_fires_when_verified_and_gap():
    """Directive fires when top is [VERIFIED] AND gap > 30%."""
    files = [
        FileEntry(
            path="src/a.py",
            score=0.9,
            functions=["x"],
            contract="caller() in other.py:1 `x()`",
        ),
        FileEntry(path="src/b.py", score=0.3, functions=["y"]),
    ]
    out = render_brief(files, scores=[0.9, 0.3])
    assert "Edit src/a.py first" in out


def test_render_brief_no_directive_low_score_gap():
    """Even with [VERIFIED] top, no directive when scores are close."""
    files = [
        FileEntry(
            path="src/a.py",
            score=0.5,
            functions=["x"],
            contract="caller() in other.py:1 `x()`",
        ),
        FileEntry(path="src/b.py", score=0.45, functions=["y"]),
    ]
    out = render_brief(files, scores=[0.5, 0.45])
    assert "Edit" not in out or "Edit src/a.py first" not in out


def test_warning_when_contract_path_contains_in_substring():
    """BUG 2 regression: path with 'in' substring should not falsely tag [VERIFIED]."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["bar"],
        contract="src/built in widget.py:42",
    )
    assert _entry_confidence_tier(entry, "") == "[WARNING]"


def test_issue_match_uses_function_names_not_signatures():
    """BUG 1 regression: entry.functions stores signatures in production.
    function_names must be used for issue matching."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["def parse_query(input: str) -> User:"],
        function_names=["parse_query"],
        contract="src/x.py:1",
    )
    assert _entry_confidence_tier(entry, "fix parse_query bug") == "[VERIFIED]"


def test_issue_match_short_function_name():
    """Three-char names like 'cli' should still match issue text."""
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["cli"],
        function_names=["cli"],
        contract="",
    )
    assert _entry_confidence_tier(entry, "fix cli bug") == "[WARNING]"


def test_no_issue_match_when_issue_text_empty():
    entry = FileEntry(
        path="src/foo.py",
        score=0.5,
        functions=["parse_query"],
        function_names=["parse_query"],
        contract="",
    )
    assert _entry_confidence_tier(entry, "") == "[INFO]"


def test_honest_note_appears_before_entries():
    out = render_brief([FileEntry(path="src/a.py", score=0.5)])
    assert out.index("GT could not anchor") < out.index("[INFO] 1.")

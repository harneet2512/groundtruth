"""Tests for enforce_budget token truncation."""

from groundtruth.schema.finding import enforce_budget


class TestEnforceBudget:
    def test_under_budget_unchanged(self) -> None:
        text = "<gt-evidence>\nshort line\n</gt-evidence>"
        assert enforce_budget(text, 400) == text

    def test_empty_string_passthrough(self) -> None:
        assert enforce_budget("", 400) == ""

    def test_over_budget_truncates(self) -> None:
        lines = ['<gt-evidence surface="test">']
        for i in range(50):
            lines.append(f"[VERIFIED] finding {i} with some padding text to fill tokens @ file.py:{i}")
        lines.append("</gt-evidence>")
        text = "\n".join(lines)
        assert len(text) // 4 > 400

        result = enforce_budget(text, 400)
        assert len(result) // 4 <= 400
        assert result.startswith('<gt-evidence surface="test">')
        assert result.endswith("</gt-evidence>")
        assert "+{" not in result or "more suppressed" in result

    def test_preserves_header_footer(self) -> None:
        lines = ['<gt-evidence surface="check">']
        for i in range(30):
            lines.append(f"[VERIFIED] long finding number {i} with lots of text padding " * 3)
        lines.append("</gt-evidence>")
        text = "\n".join(lines)

        result = enforce_budget(text, 100)
        assert result.startswith('<gt-evidence surface="check">')
        assert result.endswith("</gt-evidence>")

    def test_suppression_notice(self) -> None:
        lines = ['<gt-evidence surface="test">']
        for i in range(40):
            lines.append(f"[VERIFIED] finding {i} padding " * 5)
        lines.append("</gt-evidence>")
        text = "\n".join(lines)

        result = enforce_budget(text, 200)
        assert "more suppressed]" in result

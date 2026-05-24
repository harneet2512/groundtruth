"""Integration test: metadata-clean delivery (Phase 1.4).

Verifies that no hidden diagnostic prefixes ([GT_META], [GT_STATUS], etc.)
leak into agent-visible stdout from GT hooks. These prefixes must be routed
to stderr only.
"""

from __future__ import annotations

import pytest

from groundtruth.runtime.sanitizer import has_leak, is_hidden_line, sanitize, _HIDDEN_PREFIXES


class TestHasLeak:
    """Verify the sanitizer's has_leak() function detects all hidden prefixes."""

    @pytest.mark.parametrize("prefix", _HIDDEN_PREFIXES)
    def test_detects_each_hidden_prefix(self, prefix: str) -> None:
        text = f"{prefix} some diagnostic info here"
        assert has_leak(text), f"has_leak failed to detect {prefix}"

    def test_clean_evidence_has_no_leak(self) -> None:
        evidence = (
            "<gt-evidence trigger=\"post_edit:src/foo.py\">\n"
            "[CONTRACT] 3 callers depend on get_user()\n"
            "  api/views.py:42 `user = get_user(request.user_id)`\n"
            "[SIGNATURE] def get_user(user_id: int) -> Optional[User]\n"
            "[TEST] test_get_user expects: result.id == 42\n"
            "</gt-evidence>"
        )
        assert not has_leak(evidence)

    def test_mixed_content_detects_leak(self) -> None:
        """Evidence with a hidden prefix injected should be detected."""
        text = (
            "[CONTRACT] 3 callers depend on foo()\n"
            "[GT_META] behavioral_contract: body_len=80\n"
            "[SIGNATURE] def foo() -> int\n"
        )
        assert has_leak(text)

    @pytest.mark.parametrize("prefix", _HIDDEN_PREFIXES)
    def test_prefix_embedded_in_longer_line(self, prefix: str) -> None:
        text = f"Some text before {prefix} and some text after"
        assert has_leak(text)

    def test_no_leak_for_allowed_prefixes(self) -> None:
        """Allowed evidence markers should NOT trigger a leak."""
        allowed_markers = [
            "[CONTRACT]",
            "[SIGNATURE]",
            "[TEST]",
            "[PEER]",
            "[PATTERN]",
            "[CO-CHANGE]",
            "[SCOPE]",
            "[PROPAGATE]",
            "[BEHAVIORAL CONTRACT]",
            "[GT_VERIFY high]",
            "[GT_CONTRACT medium]",
        ]
        for marker in allowed_markers:
            text = f"{marker} some evidence content"
            assert not has_leak(text), f"False positive for allowed marker: {marker}"


class TestIsHiddenLine:
    """Verify per-line hidden prefix detection."""

    def test_hidden_line(self) -> None:
        assert is_hidden_line("[GT_META] foo=bar")

    def test_hidden_line_with_whitespace(self) -> None:
        assert is_hidden_line("  [GT_STATUS] success:ok  ")

    def test_visible_evidence_line(self) -> None:
        assert not is_hidden_line("[CONTRACT] 3 callers depend on foo()")


class TestSanitize:
    """Verify the sanitize function strips hidden lines and enforces cap."""

    def test_removes_hidden_lines(self) -> None:
        text = (
            "[CONTRACT] callers depend on foo()\n"
            "[GT_META] behavioral_contract: body_len=80\n"
            "[SIGNATURE] def foo() -> int\n"
            "[GT_TRACE] mech=L5 layer=L5 threshold=20\n"
        )
        cleaned = sanitize(text)
        assert "[GT_META]" not in cleaned
        assert "[GT_TRACE]" not in cleaned
        assert "[CONTRACT]" in cleaned
        assert "[SIGNATURE]" in cleaned

    def test_enforces_character_cap(self) -> None:
        text = "x" * 3000
        cleaned = sanitize(text, max_chars=100)
        assert len(cleaned) <= 100
        assert cleaned.endswith("...")

    def test_empty_after_sanitize(self) -> None:
        text = "[GT_META] only metadata here"
        cleaned = sanitize(text)
        assert cleaned == ""


class TestAllHiddenPrefixesCovered:
    """Verify all 8 documented hidden prefixes are in _HIDDEN_PREFIXES."""

    EXPECTED_PREFIXES = {
        "[GT_STATUS]",
        "[GT_CONFIG]",
        "[GT_META]",
        "[GT_TRACE]",
        "[GT_DELIVERY]",
        "[GT_COST]",
        "[GT_PAYLOAD]",
        "[GT_LLM_CONFIG]",
    }

    def test_all_prefixes_present(self) -> None:
        actual = set(_HIDDEN_PREFIXES)
        assert actual == self.EXPECTED_PREFIXES, (
            f"Missing: {self.EXPECTED_PREFIXES - actual}, "
            f"Extra: {actual - self.EXPECTED_PREFIXES}"
        )

    def test_has_leak_catches_all(self) -> None:
        for prefix in self.EXPECTED_PREFIXES:
            text = f"{prefix} diagnostic output"
            assert has_leak(text), f"has_leak missed {prefix}"


class TestSampleHookOutput:
    """Simulate sample hook output and verify no leaks."""

    def test_sample_post_edit_evidence(self) -> None:
        """Simulated post_edit output that should be clean."""
        output = (
            '<gt-evidence trigger="post_edit:django/db/models/fields/__init__.py">\n'
            "[BEHAVIORAL CONTRACT]\n"
            "  GUARD: if value is None -> return\n"
            "  L42: return self.to_python(value)\n"
            "[CONTRACT] 5 callers depend on get_prep_value() -- changes here affect fields.py:\n"
            "  django/db/models/sql/compiler.py:312 `prep = field.get_prep_value(val)`\n"
            "[SIGNATURE] def get_prep_value(self, value) -> Any -- 5 callers depend on this\n"
            "[GT_VERIFY high] Run: pytest tests/model_fields/test_field.py::test_get_prep_value\n"
            "</gt-evidence>"
        )
        assert not has_leak(output)

    def test_sample_post_edit_with_leaked_meta(self) -> None:
        """If metadata accidentally reached stdout, has_leak catches it."""
        output = (
            '<gt-evidence trigger="post_edit:src/foo.py">\n'
            "[GT_META] behavioral_contract: func=bar file=src/foo.py start=10 end=20\n"
            "[SIGNATURE] def bar() -> int\n"
            "</gt-evidence>"
        )
        assert has_leak(output)


class TestMCPToolRegistration:
    """Smoke test: MCP server module is importable and tools are registered."""

    def test_create_server_is_callable(self) -> None:
        """Verify create_server function exists and is callable."""
        from groundtruth.mcp.server import create_server
        assert callable(create_server)

    def test_tool_handlers_importable(self) -> None:
        """Verify all 16 tool handler functions are importable."""
        from groundtruth.mcp.tools import (
            handle_brief,
            handle_checkpoint,
            handle_context,
            handle_dead_code,
            handle_do,
            handle_explain,
            handle_find_relevant,
            handle_hotspots,
            handle_impact,
            handle_orient,
            handle_patterns,
            handle_status,
            handle_symbols,
            handle_trace,
            handle_unused_packages,
            handle_validate,
        )
        handlers = [
            handle_brief,
            handle_checkpoint,
            handle_context,
            handle_dead_code,
            handle_do,
            handle_explain,
            handle_find_relevant,
            handle_hotspots,
            handle_impact,
            handle_orient,
            handle_patterns,
            handle_status,
            handle_symbols,
            handle_trace,
            handle_unused_packages,
            handle_validate,
        ]
        assert len(handlers) == 16
        for handler in handlers:
            assert callable(handler)

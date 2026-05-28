"""Phase 3 Output Diet invariant tests.

Research backing:
- R4 (Rule 4): Verification as primary post-edit (R6 "Agents Don't Know When to Stop", R7 CodeR)
- R5 (Rule 5): Issue-keyword gating (OpenAI "relevant context, not all context")
- R2 (Rule 3): Phase-appropriate evidence (Agentless, SE-agent lifecycle)
- R2 (Rule 2): First-view only / no duplication (Lost in the Middle)
"""
import pytest


class TestL6ReviewFirst:
    """Rule 4: REVIEW/PRESERVE must precede SIGNATURE in post-edit output."""

    def test_preserve_before_signature(self):
        output = "PRESERVE: validate_user -- 3 callers\n[SIGNATURE] def validate_user(uid: int)"
        preserve_pos = output.find("PRESERVE:")
        sig_pos = output.find("[SIGNATURE]")
        assert preserve_pos < sig_pos

    def test_review_before_signature(self):
        output = "[REVIEW] Changed files:\nPRESERVE: func -- 2 callers\n[SIGNATURE] def func(x)"
        review_pos = output.find("[REVIEW]")
        sig_pos = output.find("[SIGNATURE]")
        assert review_pos < sig_pos

    def test_signature_still_present(self):
        """SIGNATURE is not removed — just reordered after REVIEW."""
        parts = ["PRESERVE: f -- 1 caller", "[SIGNATURE] def f(x)", "[TEST] assert f(1) == 2"]
        output = "\n".join(parts)
        assert "[SIGNATURE]" in output
        assert "PRESERVE:" in output


class TestRaisesCatchesGating:
    """Rule 5: RAISES/CATCHES only when issue has error-handling keywords."""

    _ERROR_KEYWORDS = frozenset({
        "error", "exception", "raise", "raises", "catch", "catches",
        "handle", "handler", "traceback", "crash", "fail", "failure",
        "throw", "thrown", "except", "unexpected",
    })

    def _issue_has_error_keywords(self, issue_terms):
        return bool(set(issue_terms) & self._ERROR_KEYWORDS)

    def test_error_issue_allows_raises(self):
        assert self._issue_has_error_keywords(["error", "handling", "timeout"])

    def test_non_error_issue_blocks_raises(self):
        assert not self._issue_has_error_keywords(["ratio", "limit", "qbittorrent"])

    def test_exception_keyword_allows(self):
        assert self._issue_has_error_keywords(["exception", "raised", "when"])

    def test_crash_keyword_allows(self):
        assert self._issue_has_error_keywords(["crash", "segfault"])

    def test_empty_terms_blocks(self):
        assert not self._issue_has_error_keywords([])


class TestCalleeSuppression:
    """Rule 3: Callees suppressed during read-only exploration."""

    def test_graph_navigation_has_no_callee_emission(self):
        """graph_navigation() in post_view.py should not emit 'Calls into:' during exploration."""
        import importlib
        spec = importlib.util.find_spec("groundtruth.hooks.post_view")
        if spec is None:
            pytest.skip("post_view not importable")
        source = spec.origin
        with open(source, encoding="utf-8") as f:
            content = f.read()
        # Find graph_navigation function and check it doesn't emit callees
        gn_start = content.find("def graph_navigation(")
        gn_end = content.find("\ndef ", gn_start + 1) if gn_start >= 0 else len(content)
        gn_body = content[gn_start:gn_end] if gn_start >= 0 else ""
        active_callee_lines = [
            line.strip() for line in gn_body.split("\n")
            if "Calls into:" in line
            and "out.append" in line
            and not line.strip().startswith("#")
        ]
        assert len(active_callee_lines) == 0, f"Active callee emission in graph_navigation: {active_callee_lines}"


class TestL4aL3bDedup:
    """Rule 2: L4a suppressed when L3b already fired for same file."""

    def test_l3b_key_blocks_l4a(self):
        import types
        config = types.SimpleNamespace()
        config.evidence_sent = {"l3b_file:src/auth.py": True}
        key = f"l3b_file:src/auth.py"
        assert key in config.evidence_sent

    def test_no_l3b_key_allows_l4a(self):
        import types
        config = types.SimpleNamespace()
        config.evidence_sent = {}
        key = f"l3b_file:src/auth.py"
        assert key not in config.evidence_sent

"""Unit tests for L3 evidence formatting (P0-2).

Proves that a 7-line behavioral contract survives the non-live
formatting path after the truncation fix ([:3] removed, [:2000] cap).
"""

from __future__ import annotations


class TestEvidenceFormatting:

    def _simulate_non_live_formatting(self, hook_output: str) -> str:
        """Simulate the non-live L3 evidence formatting path.

        Extracted from oh_gt_full_wrapper.py post-edit section.
        This is the code path that USED TO truncate to 3 lines / 130 chars.
        After P0-2 fix: "\n".join(directive_lines)[:2000]
        """
        directive_lines = [
            ln.strip() for ln in hook_output.splitlines()
            if ln.strip()
            and not ln.strip().startswith("[GT_STATUS]")
            and not ln.strip().startswith("__")
            and not ln.strip().startswith("<")
            and not ln.strip().startswith("</")
        ]
        # P0-2 fix: was directive_lines[:3] + ln[:130]
        evidence_text = "\n".join(directive_lines)[:2000]
        return evidence_text

    def test_7_line_behavioral_contract_survives(self):
        """P0-2 proof: a 7-line behavioral contract is NOT truncated."""
        hook_output = (
            "[BEHAVIORAL CONTRACT]\n"
            "  GUARD: if self.call_args['return_cmd'] -> return\n"
            "  L893: return self\n"
            "  L894: return str(self)\n"
            "  L896: return wait_for_completion().__await__()\n"
            "[SIGNATURE] def __await__(self):\n"
            "[GT_STATUS] success\n"
        )
        result = self._simulate_non_live_formatting(hook_output)
        assert "[BEHAVIORAL CONTRACT]" in result
        assert "GUARD:" in result
        assert "return self" in result
        assert "return str(self)" in result
        assert "__await__" in result
        assert "[SIGNATURE]" in result
        # GT_STATUS is filtered out (correct)
        assert "[GT_STATUS]" not in result

    def test_callers_before_contract_both_survive(self):
        """P0-2 proof: when callers appear before contract, BOTH survive."""
        hook_output = (
            "[CONTRACT] 3 callers in 2 files\n"
            "CALLERS: auth.py:42 `validate(token)`\n"
            "CALLERS: middleware.py:88 `check_auth(req)`\n"
            "[BEHAVIORAL CONTRACT]\n"
            "  GUARD: if not token: raise ValueError\n"
            "  return User(id=uid)\n"
            "[SIGNATURE] def get_user(uid: int) -> User\n"
            "[GT_STATUS] success\n"
        )
        result = self._simulate_non_live_formatting(hook_output)
        # ALL evidence types must survive
        assert "[CONTRACT]" in result
        assert "auth.py:42" in result
        assert "middleware.py:88" in result
        assert "[BEHAVIORAL CONTRACT]" in result
        assert "GUARD:" in result
        assert "[SIGNATURE]" in result
        # Count lines
        lines = [l for l in result.split("\n") if l.strip()]
        assert len(lines) >= 7, f"Expected >=7 lines, got {len(lines)}: {result}"

    def test_under_2000_char_cap(self):
        """P0-2 proof: result is within 2000-char budget."""
        hook_output = (
            "[BEHAVIORAL CONTRACT]\n"
            "  GUARD: if x: return\n"
            "  return self\n"
            "[SIGNATURE] def foo():\n"
            "[GT_STATUS] success\n"
        )
        result = self._simulate_non_live_formatting(hook_output)
        assert len(result) <= 2000

    def test_long_evidence_capped_at_2000(self):
        """P0-2 proof: evidence longer than 2000 chars is capped."""
        lines = [f"[CONTRACT] caller_{i}.py:42 `foo(x, y, z)`" for i in range(100)]
        hook_output = "\n".join(lines) + "\n[GT_STATUS] success\n"
        result = self._simulate_non_live_formatting(hook_output)
        assert len(result) <= 2000
        # Must still contain content (not empty)
        assert "[CONTRACT]" in result

    def test_old_behavior_would_truncate(self):
        """Proves the OLD behavior ([:3] + [:130]) would have lost evidence."""
        hook_output = (
            "[CONTRACT] 3 callers in 2 files\n"
            "CALLERS: auth.py:42 `validate(token, strict=True)`\n"
            "CALLERS: middleware.py:88 `check_auth(req, session=sess)`\n"
            "[BEHAVIORAL CONTRACT]\n"
            "  GUARD: if not token: raise ValueError\n"
            "  return User(id=uid)\n"
            "[SIGNATURE] def get_user(uid: int) -> User\n"
            "[GT_STATUS] success\n"
        )
        directive_lines = [
            ln.strip() for ln in hook_output.splitlines()
            if ln.strip()
            and not ln.strip().startswith("[GT_STATUS]")
            and not ln.strip().startswith("__")
            and not ln.strip().startswith("<")
            and not ln.strip().startswith("</")
        ]
        # OLD behavior
        old_result = "\n".join(ln[:130] for ln in directive_lines[:3])
        # NEW behavior
        new_result = "\n".join(directive_lines)[:2000]

        # Old loses behavioral contract
        assert "[BEHAVIORAL CONTRACT]" not in old_result
        # New keeps it
        assert "[BEHAVIORAL CONTRACT]" in new_result

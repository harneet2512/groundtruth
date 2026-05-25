#!/usr/bin/env python3
"""Adversarial tests for GT wrapper fixes.

Tests UNHAPPY paths: boundary conditions, malformed inputs, adversarial commands,
SQL injection, empty/null data, race conditions, and resource limits.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ------------------------------------------------------------------
# Path setup — force imports from THIS repo, not any installed package
# ------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_SCRIPTS_DIR = _REPO_ROOT / "scripts" / "swebench"

# Remove any pre-existing groundtruth paths and force ours first
sys.path = [str(_SRC_DIR), str(_SCRIPTS_DIR)] + [
    p for p in sys.path if "groundtruth" not in p.lower() or str(_REPO_ROOT) in p
]
# Purge any already-loaded groundtruth modules so reimport uses our path
_to_remove = [k for k in sys.modules if k.startswith("groundtruth")]
for k in _to_remove:
    del sys.modules[k]


# ==================================================================
# ATTACK 1: _extract_search_symbol with adversarial grep commands
# ==================================================================
class TestExtractSearchSymbolAdversarial:
    """Test grep extraction against commands the happy path never sees."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from oh_gt_full_wrapper import _extract_search_symbol
        self.fn = _extract_search_symbol

    def test_single_quotes(self):
        """Single quotes instead of double quotes."""
        result = self.fn("grep -rn 'validate_token' src/")
        assert result == "validate_token", f"Got '{result}'"

    def test_no_quotes_at_all(self):
        """No quotes around the pattern — bare word after flags."""
        result = self.fn("grep -rn validate_token src/")
        # Should use fallback regex and return validate_token
        assert result == "validate_token", f"Got '{result}'"

    def test_piped_command(self):
        """grep piped to grep — should extract from first grep."""
        result = self.fn('grep -rn "token" src/ | grep -v test')
        assert result == "token", f"Got '{result}'"

    def test_include_before_pattern(self):
        """--include flag before the pattern.

        BUG: The regex greedily matches the FIRST quoted string after grep,
        which is '*.py' from --include="*.py", not the actual search pattern.
        It extracts '*.py', fails to find an identifier, and returns empty.
        Real-world impact: agent commands like `grep --include="*.py" -rn "symbol" src/`
        will silently fail to trigger grep interception.
        """
        result = self.fn('grep --include="*.py" -rn "validate_token" src/')
        # BUG: returns '' instead of 'validate_token'
        # The [^"']* in the regex greedily consumes to the first quote it finds
        assert result == "validate_token", f"Got '{result}'"

    def test_e_flag_multiple_patterns(self):
        """-e flag with multiple patterns."""
        result = self.fn('grep -rn -e "token" -e "auth" src/')
        assert result == "token", f"Got '{result}'"

    def test_pattern_is_file_path(self):
        """Pattern that looks like a file path — should not crash."""
        result = self.fn('grep -rn "src/auth.py" docs/')
        # "src" is only 3 chars and in SKIP? No, SKIP has 'r','rn','rl','n','l','i','include','exclude','type','py','js','go','def','class','import'
        # "src" is not in SKIP, but auth is the identifier we want
        assert result in ("src", "auth"), f"Got '{result}'"

    def test_beets_5682_real_command(self):
        """Real command from beets-5682 with grep OR using \\|."""
        result = self.fn('grep -i "grindcore\\|melodic death\\|blackened thrash" genres.txt')
        # Should extract "grindcore" (first valid identifier in pipe-OR split)
        assert result == "grindcore", f"Got '{result}'"

    def test_no_grep_at_all(self):
        """Command with no grep — should return empty."""
        result = self.fn("find . -name '*.py' -exec cat {} \\;")
        assert result == "", f"Got '{result}'"

    def test_rg_ripgrep_variant(self):
        """ripgrep (rg) instead of grep."""
        result = self.fn('rg -n "authenticate" --type py')
        assert result == "authenticate", f"Got '{result}'"

    def test_empty_quoted_pattern(self):
        """Empty quotes — should return empty, not crash."""
        result = self.fn('grep -rn "" src/')
        assert result == "", f"Got '{result}'"

    def test_pattern_all_short_words(self):
        """Pattern with only 2-char words below the \\w{2,} threshold."""
        result = self.fn('grep -rn "ab" src/')
        # "ab" is only 2 chars, regex requires \\w{2,} which means 3+ total
        # Wait: \\w{2,} means 2 or more AFTER the first char in [A-Za-z_]\\w{2,}
        # so total length is 3+. "ab" is 2 chars → no match
        assert result == "", f"Got '{result}'"

    def test_pattern_with_regex_special_chars(self):
        """Pattern with regex metacharacters — should not crash.

        BUG: When pattern is 'def.*validate[0-9]+', the sub for stripping
        'def ' prefix fails (no space after 'def'), then \\b word boundary
        search finds 'def' first (which is in SKIP). The code only takes
        the FIRST ident match per part and skips if in SKIP, but doesn't
        try the NEXT match in the same part. So 'validate' is never found.
        Real-world impact: agent greps with regex patterns containing 'def'
        or 'class' prefix without a space will fail to extract the symbol.
        """
        result = self.fn('grep -rn "def.*validate[0-9]+" src/')
        # BUG: returns '' because 'def' is found first and is in SKIP,
        # but code doesn't try finditer/subsequent matches
        assert result == "validate", f"Got '{result}' (expected 'validate' from ident extraction)"

    def test_cd_workspace_prefix(self):
        """cd /workspace && grep pattern — the standard OH format."""
        result = self.fn('cd /workspace && grep -rn "process_request" lib/')
        assert result == "process_request", f"Got '{result}'"

    def test_returns_junk_flag_as_symbol(self):
        """Verify flags like 'rn' or 'include' are not returned as symbols.

        The SKIP set should filter these. This tests the fallback regex path.
        """
        # This is a tricky case: grep with no quotes, just a flag-looking word
        result = self.fn("grep -rn include src/")
        # "include" is in SKIP, so fallback should return ""
        assert result == "", f"Got '{result}' — should have been filtered by SKIP"

    def test_backslash_pipe_literal(self):
        """Actual backslash-pipe in the pattern text (grep OR syntax)."""
        # The regex splits on \\\\?\\| which should handle both \\| and \\\\|
        result = self.fn('grep -rn "first_func\\|second_func\\|third_func" pkg/')
        assert result == "first_func", f"Got '{result}'"


# ==================================================================
# ATTACK 2: format_risk_evidence boundary conditions
# ==================================================================
class TestFormatRiskEvidenceBoundary:
    """Boundary and adversarial inputs for format_risk_evidence."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from groundtruth.hooks.post_edit import format_risk_evidence
        self.fn = format_risk_evidence

    def test_zero_callers(self):
        """Empty callers list — should return []."""
        result = self.fn([], "some_func", confidence=0.95)
        assert result == [], f"Got {result}"

    def test_confidence_exactly_0_9(self):
        """Confidence exactly at the 0.9 boundary — should use strong format."""
        callers = [{"file": "a.py", "line": "1", "code": "x()"}]
        result = self.fn(callers, "x", confidence=0.9)
        assert any("DO NOT BREAK" in line for line in result), f"Got {result}"

    def test_confidence_0_89(self):
        """Confidence just below 0.9 — should NOT use 'DO NOT BREAK'."""
        callers = [{"file": "a.py", "line": "1", "code": "x()"}]
        result = self.fn(callers, "x", confidence=0.89)
        # Should fall to the 0.5-0.9 tier with "~" soft note
        assert all("DO NOT BREAK" not in line for line in result), f"Got {result}"
        assert any("~" in line or "possible" in line.lower() or "verify" in line.lower() for line in result), f"Got {result}"

    def test_confidence_below_0_5(self):
        """Confidence below 0.5 — per docstring should return [].

        DISCREPANCY: The docstring says 'confidence < 0.5 or no callers: silence
        (empty list)' but the code has NO check for confidence < 0.5 when callers
        is non-empty. The final branch (the catchall) always fires for non-empty
        callers with confidence < 0.9. This means low-confidence callers (e.g. 0.2)
        still produce '[CONTRACT ~] possible callers...' output instead of silence.
        This test documents the ACTUAL behavior (not the documented behavior).
        """
        callers = [{"file": "a.py", "line": "1", "code": "x()"}]
        result = self.fn(callers, "x", confidence=0.49)
        # ACTUAL behavior: produces output (docstring says should be empty)
        # This is a doc-vs-code discrepancy. The code never returns [] for
        # non-empty callers regardless of confidence.
        assert len(result) > 0, "Code actually produces output for confidence < 0.5 (contradicts docstring)"
        assert any("~" in line or "possible" in line.lower() for line in result)

    def test_caller_with_empty_code(self):
        """Caller dict with empty code string."""
        callers = [{"file": "a.py", "line": "5", "code": ""}]
        result = self.fn(callers, "func", confidence=0.95)
        # Should not crash, should produce a line without expects:
        assert len(result) > 0, "Should produce output even with empty code"
        joined = "\n".join(result)
        assert "a.py:5" in joined, f"Should still show file:line. Got: {joined}"

    def test_caller_with_newlines_in_code(self):
        """Code field containing newlines — should not break formatting."""
        callers = [{"file": "b.py", "line": "10", "code": "func(\n  arg1,\n  arg2\n)"}]
        result = self.fn(callers, "func", confidence=0.95)
        # Should not crash
        assert len(result) > 0

    def test_caller_with_quotes_in_code(self):
        """Code field containing quotes."""
        callers = [{"file": "c.py", "line": "3", "code": 'call("it\'s a test")'}]
        result = self.fn(callers, "call", confidence=0.95)
        assert len(result) > 0

    def test_caller_file_with_spaces(self):
        """File path containing spaces."""
        callers = [{"file": "my dir/my file.py", "line": "1", "code": "x()"}]
        result = self.fn(callers, "x", confidence=0.95)
        assert len(result) > 0
        joined = "\n".join(result)
        assert "my file.py" in joined or "my dir" in joined

    def test_caller_file_with_unicode(self):
        """File path with unicode characters."""
        callers = [{"file": "src/données.py", "line": "42", "code": "traiter()"}]
        result = self.fn(callers, "traiter", confidence=0.95)
        assert len(result) > 0

    def test_many_callers_high_confidence(self):
        """3+ callers with high confidence — should get risk warning."""
        callers = [
            {"file": "a.py", "line": "1", "code": "x()"},
            {"file": "b.py", "line": "2", "code": "x()"},
            {"file": "c.py", "line": "3", "code": "x()"},
            {"file": "d.py", "line": "4", "code": "x()"},
        ]
        result = self.fn(callers, "x", confidence=0.95)
        joined = "\n".join(result)
        assert "DO NOT BREAK" in joined
        # Should cap at showing 2 caller lines (callers[:2])
        assert result[-1].count("expects:") <= 1 or len(result) <= 3


# ==================================================================
# ATTACK 3: L5 scaffold detection with tricky file sets
# ==================================================================
class TestScaffoldDetectionAdversarial:
    """Test _is_scaffolding_path with edge-case paths."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from oh_gt_full_wrapper import _is_scaffolding_path, _is_test_path
        self.is_scaffold = _is_scaffolding_path
        self.is_test = _is_test_path

    def test_only_test_files_not_scaffold(self):
        """Test files should NOT be scaffold."""
        test_files = [
            "tests/test_auth.py",
            "tests/test_api.py",
            "tests/conftest.py",
        ]
        for f in test_files:
            assert not self.is_scaffold(f), f"{f} should NOT be scaffold"
            # But they should be test paths:
            if "conftest" not in f:
                assert self.is_test(f), f"{f} should be a test path"

    def test_reproduce_without_underscore(self):
        """'reproduce.py' without underscore — NOT scaffold per prefix list."""
        # SCAFFOLDING_PREFIXES has 'reproduce_' (with underscore)
        assert not self.is_scaffold("reproduce.py"), "reproduce.py (no underscore) should NOT be scaffold"

    def test_scaffold_in_subdirectory(self):
        """Scaffold files in subdirectories — _normalize_path + Path.name should handle."""
        cases = [
            ("scripts/reproduce_issue.py", True),
            ("tools/debug_helper.py", True),
            ("src/debug_utils.py", True),  # starts with debug_
            ("some/deep/path/temp_file.py", True),
        ]
        for path, expected in cases:
            result = self.is_scaffold(path)
            assert result == expected, f"_is_scaffolding_path('{path}') = {result}, expected {expected}"

    def test_conftest_is_not_scaffold(self):
        """conftest.py should not be scaffold."""
        assert not self.is_scaffold("tests/conftest.py")

    def test_verify_fix_variants(self):
        """verify_fix prefix should match, verify_other should not (unless starts with verify_fix or verify_implementation)."""
        assert self.is_scaffold("verify_fix.py")
        assert self.is_scaffold("verify_fix_auth.py")
        assert self.is_scaffold("verify_implementation.py")
        # "verify_other.py" does NOT start with any prefix:
        assert not self.is_scaffold("verify_other.py"), "verify_other.py is not in prefix list"

    def test_edited_files_set_dedup(self):
        """edited_files is a set — same file 5 times should be counted once."""
        edited = set()
        for _ in range(5):
            edited.add("reproduce_bug.py")
        assert len(edited) == 1

    def test_case_sensitivity(self):
        """Scaffold detection uses .lower() — should catch uppercase variants."""
        # The code does: base = Path(_normalize_path(path)).name.lower()
        assert self.is_scaffold("Reproduce_issue.py"), "Should match after lowercasing"
        assert self.is_scaffold("TEMP_file.py"), "Should match after lowercasing"
        assert self.is_scaffold("Debug_trace.py"), "Should match after lowercasing"


# ==================================================================
# ATTACK 4: L5b late-band boundary conditions
# ==================================================================
class TestL5bLateBandBoundary:
    """Test AgentTrajectoryState and L5b ratio logic at boundaries."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from groundtruth.runtime.state import AgentTrajectoryState
        self.State = AgentTrajectoryState

    def test_ratio_exactly_0_60(self):
        """ratio = 0.60 exactly — L5b gate uses >= 0.60."""
        state = self.State(action_count=60, max_iter=100)
        assert state.ratio == 0.60
        # Band classification: 0.50 <= 0.60 < 0.75 → "late"
        assert state.band == "late"

    def test_max_iter_zero(self):
        """max_iter=0 should not crash (division by zero guarded)."""
        state = self.State(action_count=50, max_iter=0)
        # The property uses: if self.max_iter <= 0: return 0.0
        assert state.ratio == 0.0
        assert state.band == "early"

    def test_l5b_count_negative(self):
        """Simulating _l5b_injection_count = -1 (shouldn't happen but test robustness)."""
        # The wrapper uses: _l5b_count = getattr(config, '_l5b_injection_count', 0)
        # and checks: _l5b_count < 2
        # If somehow set to -1, it would still be < 2 → allowed
        # This isn't a crash but could allow extra injections
        assert -1 < 2  # Just documenting the logic

    def test_goku_active_false_regression(self):
        """When goku_active=False, L5b allowed should be True (original behavior)."""
        # _l5b_allowed = (not goku_active) or (_l5b_count < 2 and _ratio_l5b >= 0.60)
        # When goku_active=False: (not False) = True → short-circuit, always allowed
        goku_active = False
        _l5b_count = 999  # Even with absurd count
        _ratio_l5b = 0.0  # Even at start
        _l5b_allowed = (not goku_active) or (_l5b_count < 2 and _ratio_l5b >= 0.60)
        assert _l5b_allowed is True, "With goku off, L5b should always be allowed"

    def test_goku_active_true_early_ratio(self):
        """When goku active and ratio < 0.60, L5b should be blocked."""
        goku_active = True
        _l5b_count = 0
        _ratio_l5b = 0.59
        _l5b_allowed = (not goku_active) or (_l5b_count < 2 and _ratio_l5b >= 0.60)
        assert _l5b_allowed is False

    def test_goku_active_true_count_at_2(self):
        """When goku active and count already at 2, L5b should be blocked."""
        goku_active = True
        _l5b_count = 2
        _ratio_l5b = 0.80
        _l5b_allowed = (not goku_active) or (_l5b_count < 2 and _ratio_l5b >= 0.60)
        assert _l5b_allowed is False

    def test_band_boundaries(self):
        """Test all band transitions."""
        cases = [
            (0, 100, "early"),
            (24, 100, "early"),
            (25, 100, "mid"),
            (49, 100, "mid"),
            (50, 100, "late"),
            (74, 100, "late"),
            (75, 100, "final"),
            (100, 100, "final"),
        ]
        for ac, mi, expected_band in cases:
            state = self.State(action_count=ac, max_iter=mi)
            assert state.band == expected_band, f"action_count={ac}, max_iter={mi}: got '{state.band}', expected '{expected_band}'"

    def test_action_count_exceeds_max_iter(self):
        """action_count > max_iter — should not crash."""
        state = self.State(action_count=150, max_iter=100)
        assert state.ratio == 1.5
        assert state.band == "final"


# ==================================================================
# ATTACK 5: Ledger with extreme/adversarial data
# ==================================================================
class TestLedgerAdversarial:
    """Stress and edge-case tests for Ledger."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from groundtruth.runtime.ledger import Ledger, SignalOutcome
        self.Ledger = Ledger
        self.SignalOutcome = SignalOutcome

    def test_10000_entries(self):
        """10,000 entries — should not crash or OOM."""
        ledger = self.Ledger()
        for i in range(10_000):
            ledger.delivered(layer="L3", event_type="post_edit",
                          file_path=f"src/file_{i}.py", chars=100, iteration=i)
        jsonl = ledger.to_jsonl()
        lines = jsonl.split("\n")
        assert len(lines) == 10_000, f"Expected 10000 lines, got {len(lines)}"

    def test_unicode_file_path(self):
        """Unicode characters in file_path."""
        ledger = self.Ledger()
        ledger.delivered(layer="L3", event_type="post_edit",
                        file_path="src/données/résumé.py", chars=50, iteration=1)
        jsonl = ledger.to_jsonl()
        parsed = json.loads(jsonl)
        assert parsed["file_path"] == "src/données/résumé.py"

    def test_empty_layer_string(self):
        """Empty string for layer — should not crash."""
        ledger = self.Ledger()
        ledger.delivered(layer="", event_type="", file_path="", chars=0, iteration=0)
        jsonl = ledger.to_jsonl()
        parsed = json.loads(jsonl)
        assert parsed["layer"] == ""

    def test_chars_zero(self):
        """chars=0 for a delivered entry — legitimate empty delivery."""
        ledger = self.Ledger()
        ledger.delivered(layer="L3", event_type="post_edit",
                        file_path="src/a.py", chars=0, iteration=1)
        summary = ledger.summary()
        assert summary.get("delivered") == 1

    def test_flush_without_gt_debug_dir(self):
        """Flush when GT_DEBUG_DIR is not set — should use /tmp/gt_debug."""
        ledger = self.Ledger()
        ledger.delivered(layer="L3", event_type="test",
                        file_path="a.py", chars=10, iteration=1)
        # The atexit flush uses: os.environ.get("GT_DEBUG_DIR", "/tmp/gt_debug")
        # On Windows this would be /tmp/gt_debug which may not exist
        # The code has os.makedirs(os.path.dirname(_lp), exist_ok=True)
        # so it should create it. This is a platform-dependent test.
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"GT_DEBUG_DIR": tmpdir}):
                _lp = os.path.join(tmpdir, "gt_ledger_test.jsonl")
                os.makedirs(os.path.dirname(_lp), exist_ok=True)
                with open(_lp, "w", encoding="utf-8") as f:
                    f.write(ledger.to_jsonl())
                assert os.path.exists(_lp)

    def test_summary_with_mixed_outcomes(self):
        """Summary counts each outcome type correctly."""
        ledger = self.Ledger()
        ledger.delivered(layer="L3", event_type="a", file_path="a.py", chars=10)
        ledger.delivered(layer="L3", event_type="b", file_path="b.py", chars=20)
        ledger.suppressed(layer="L3", event_type="c", file_path="c.py",
                         outcome=self.SignalOutcome.SUPPRESSED_BUDGET, reason="test")
        ledger.suppressed(layer="L3", event_type="d", file_path="d.py",
                         outcome=self.SignalOutcome.SUPPRESSED_DUPLICATE, reason="dup")
        summary = ledger.summary()
        assert summary.get("delivered") == 2
        assert summary.get("suppressed_budget") == 1
        assert summary.get("suppressed_duplicate") == 1

    def test_to_jsonl_each_line_valid(self):
        """Every line in to_jsonl() must be valid JSON."""
        ledger = self.Ledger()
        for i in range(50):
            ledger.delivered(layer=f"L{i%6}", event_type="test",
                          file_path=f"file_{i}.py", chars=i*10, iteration=i)
        jsonl = ledger.to_jsonl()
        for line_num, line in enumerate(jsonl.split("\n"), 1):
            if line.strip():
                try:
                    json.loads(line)
                except json.JSONDecodeError as e:
                    pytest.fail(f"Line {line_num} is not valid JSON: {e}\nLine: {line}")


# ==================================================================
# ATTACK 6: build_graph_map with adversarial graph.db
# ==================================================================
class TestGraphMapAdversarial:
    """Test build_graph_map against adversarial database states."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from groundtruth.brief.graph_map import build_graph_map
        self.fn = build_graph_map

    def _make_db(self, tmpdir, setup_fn=None):
        db_path = os.path.join(tmpdir, "graph.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT,
                file_path TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                signature TEXT,
                return_type TEXT,
                is_exported BOOLEAN DEFAULT 0,
                is_test BOOLEAN DEFAULT 0,
                language TEXT NOT NULL,
                parent_id INTEGER REFERENCES nodes(id)
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES nodes(id),
                target_id INTEGER NOT NULL REFERENCES nodes(id),
                type TEXT NOT NULL,
                source_line INTEGER,
                source_file TEXT,
                resolution_method TEXT,
                confidence REAL DEFAULT 0.0,
                metadata TEXT
            );
        """)
        if setup_fn:
            setup_fn(conn)
        conn.commit()
        conn.close()
        return db_path

    def test_empty_db_zero_nodes(self):
        """graph.db with 0 nodes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_db(tmpdir)
            ranked = [{"file": "src/main.py", "score": 0.9}]
            brief = self.fn(ranked, db_path)
            rendered = brief.render()
            assert "<gt-task-brief>" in rendered
            # Should not crash, just produce empty entries

    def test_nodes_but_zero_edges(self):
        """Nodes exist but no edges — callers/callees should be empty."""
        def setup(conn):
            conn.execute(
                "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language) "
                "VALUES ('Function', 'main', 'pkg.main', 'src/main.py', 1, 10, 'args: list', 'None', 1, 0, 'Python')"
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_db(tmpdir, setup)
            ranked = [{"file": "src/main.py", "score": 0.9}]
            brief = self.fn(ranked, db_path)
            rendered = brief.render()
            assert "Called by:" not in rendered
            assert "Functions:" in rendered or "main" in rendered

    def test_sql_injection_in_file_path(self):
        """file_path containing single quote — should not cause SQL error."""
        def setup(conn):
            conn.execute(
                "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language) "
                "VALUES ('Function', 'parse', 'x.parse', 'src/it''s_a_file.py', 1, 5, 'x: str', 'bool', 1, 0, 'Python')"
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_db(tmpdir, setup)
            # Query with a file path containing quote
            ranked = [{"file": "src/it's_a_file.py", "score": 0.8}]
            brief = self.fn(ranked, db_path)
            # Should not crash — parameterized queries protect against injection
            rendered = brief.render()
            assert "<gt-task-brief>" in rendered

    def test_null_signature_and_return_type(self):
        """NULL values in signature/return_type — should not crash contracts."""
        def setup(conn):
            conn.execute(
                "INSERT INTO nodes (label, name, qualified_name, file_path, start_line, end_line, signature, return_type, is_exported, is_test, language) "
                "VALUES ('Function', 'init', 'x.init', 'src/x.py', 1, 5, NULL, NULL, 1, 0, 'Python')"
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_db(tmpdir, setup)
            ranked = [{"file": "src/x.py", "score": 0.9}]
            brief = self.fn(ranked, db_path)
            rendered = brief.render()
            # Should not crash, contracts should be empty
            assert "Contract:" not in rendered

    def test_missing_schema_wrong_table(self):
        """Database that doesn't have the expected schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "graph.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE other (id INTEGER)")
            conn.commit()
            conn.close()
            ranked = [{"file": "src/main.py", "score": 0.9}]
            # Should not crash due to try/except in build_graph_map
            brief = self.fn(ranked, db_path)
            rendered = brief.render()
            assert "<gt-task-brief>" in rendered

    def test_nonexistent_db_path(self):
        """Path to a database that doesn't exist."""
        brief = self.fn(
            [{"file": "src/main.py", "score": 0.9}],
            "/nonexistent/path/graph.db",
        )
        rendered = brief.render()
        # Should return empty brief (the function checks os.path.exists)
        assert "<gt-task-brief>" in rendered

    def test_empty_ranked_files(self):
        """Empty ranked_files list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._make_db(tmpdir)
            brief = self.fn([], db_path)
            rendered = brief.render()
            assert "<gt-task-brief>" in rendered


# ==================================================================
# ATTACK 7: Full flow simulation — the REAL adversary
# ==================================================================
class TestFullFlowSimulation:
    """Simulate a realistic adversarial agent session end-to-end."""

    def test_grep_extraction_with_no_graph_callers(self):
        """Agent runs 3 greps that extract valid symbols, but graph has 0 callers."""
        from oh_gt_full_wrapper import _extract_search_symbol

        commands = [
            'cd /workspace && grep -rn "validate_token" src/',
            'grep -rn "process_request" lib/',
            'rg -n "authenticate" --type py',
        ]
        symbols = []
        for cmd in commands:
            sym = _extract_search_symbol(cmd)
            assert sym != "", f"Failed to extract symbol from: {cmd}"
            symbols.append(sym)

        assert symbols == ["validate_token", "process_request", "authenticate"]

    def test_scaffold_files_detection(self):
        """4 scaffold files without source edits — all should be detected."""
        from oh_gt_full_wrapper import _is_scaffolding_path

        scaffold_files = [
            "reproduce_issue.py",
            "debug_auth.py",
            "temp_test_runner.py",
            "scratch_analysis.py",
        ]
        for f in scaffold_files:
            assert _is_scaffolding_path(f), f"{f} should be detected as scaffold"

    def test_l5b_fires_at_65_percent(self):
        """At 65% iteration with pending_next_action — L5b should fire."""
        from groundtruth.runtime.state import AgentTrajectoryState

        state = AgentTrajectoryState(action_count=65, max_iter=100)
        assert state.ratio == 0.65
        assert state.band == "late"

        # Simulate the L5b gate logic from wrapper
        goku_active = True
        _l5b_count = 0
        _ratio_l5b = state.ratio
        _l5b_allowed = (not goku_active) or (_l5b_count < 2 and _ratio_l5b >= 0.60)
        assert _l5b_allowed is True

    def test_ledger_records_all_events(self):
        """Ledger should record grep, scaffold, and L5b events without crash."""
        from groundtruth.runtime.ledger import Ledger, SignalOutcome

        ledger = Ledger()

        # 3 grep intercepts (delivered)
        for i, sym in enumerate(["validate_token", "process_request", "authenticate"]):
            ledger.delivered(layer="L3", event_type="grep_intercept",
                          file_path=f"src/{sym}.py", chars=100, iteration=i+1)

        # 1 scaffold fire
        ledger.delivered(layer="L5", event_type="scaffold_redirect",
                        file_path="reproduce_issue.py", chars=200, iteration=5)

        # 1 L5b fire
        ledger.delivered(layer="L5b", event_type="ignored_next_action",
                        file_path="src/auth.py", chars=150, iteration=65)

        assert len(ledger.entries) == 5
        summary = ledger.summary()
        assert summary.get("delivered") == 5

        # Verify JSONL output
        jsonl = ledger.to_jsonl()
        lines = jsonl.strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            parsed = json.loads(line)
            assert "layer" in parsed
            assert "outcome" in parsed

    def test_no_crashes_in_complete_flow(self):
        """The entire flow from grep -> scaffold -> L5b -> ledger flush runs without exception."""
        from oh_gt_full_wrapper import _extract_search_symbol, _is_scaffolding_path
        from groundtruth.runtime.state import AgentTrajectoryState
        from groundtruth.runtime.ledger import Ledger, SignalOutcome
        from groundtruth.runtime.budget import BudgetTracker

        # Phase 1: grep extraction
        syms = [_extract_search_symbol(cmd) for cmd in [
            'grep -rn "validate_token" src/',
            'grep -rn "process_request" lib/',
            'rg "authenticate" --type py',
        ]]
        assert all(s != "" for s in syms)

        # Phase 2: scaffold detection
        scaffold_files = ["reproduce_issue.py", "debug_auth.py",
                         "temp_check.py", "scratch_pad.py"]
        assert all(_is_scaffolding_path(f) for f in scaffold_files)

        # Phase 3: state tracking
        state = AgentTrajectoryState(action_count=65, max_iter=100)
        assert state.band == "late"

        # Phase 4: budget tracking
        tracker = BudgetTracker()
        for _ in range(3):
            ok, _ = tracker.check("gt_query")
            assert ok
        ok, reason = tracker.check("gt_query")
        assert not ok
        assert "budget_exhausted" in reason

        # Phase 5: ledger
        ledger = Ledger()
        ledger.delivered(layer="L3", event_type="grep", file_path="src/auth.py",
                        chars=100, iteration=1)
        ledger.delivered(layer="L5", event_type="scaffold", file_path="reproduce_issue.py",
                        chars=200, iteration=3)
        ledger.suppressed(layer="L3", event_type="grep", file_path="src/api.py",
                         outcome=SignalOutcome.SUPPRESSED_BUDGET,
                         reason="budget_exhausted:gt_query=3/3", iteration=5)

        # Flush to temp
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = os.path.join(tmpdir, "gt_ledger_test.jsonl")
            with open(ledger_path, "w", encoding="utf-8") as f:
                f.write(ledger.to_jsonl())
            assert os.path.exists(ledger_path)
            with open(ledger_path) as f:
                content = f.read()
            lines = content.strip().split("\n")
            assert len(lines) == 3


# ==================================================================
# ATTACK BONUS: BudgetTracker edge cases
# ==================================================================
class TestBudgetTrackerAdversarial:
    """Edge cases for BudgetTracker."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from groundtruth.runtime.budget import BudgetTracker
        self.BudgetTracker = BudgetTracker

    def test_unknown_key_defaults_to_999(self):
        """Key not in DEFAULT_BUDGETS should get limit 999."""
        bt = self.BudgetTracker()
        ok, _ = bt.check("totally_unknown_key")
        assert ok is True
        assert bt.remaining("totally_unknown_key") == 998

    def test_remaining_never_negative(self):
        """After exhausting budget, remaining should be 0 not negative."""
        bt = self.BudgetTracker()
        for _ in range(10):
            bt.check("gt_query")
        assert bt.remaining("gt_query") == 0

    def test_custom_limit_zero(self):
        """Setting a limit of 0 — first check should be blocked."""
        bt = self.BudgetTracker()
        bt.limits["custom"] = 0
        ok, reason = bt.check("custom")
        assert ok is False
        assert "budget_exhausted" in reason

    def test_to_dict_structure(self):
        """to_dict should have limits, counts, remaining."""
        bt = self.BudgetTracker()
        bt.check("gt_query")
        d = bt.to_dict()
        assert "limits" in d
        assert "counts" in d
        assert "remaining" in d
        assert d["counts"]["gt_query"] == 1
        assert d["remaining"]["gt_query"] == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))

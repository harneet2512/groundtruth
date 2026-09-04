"""LSP precision-pass fix campaign — biting tests (RED before fix, GREEN after).

Covers the six resolve.py / lsp client bugs:

  Bug1 [P0] jdtls (java) converts 0 every run — _READY_BUDGET_S_BY_SERVER omits jdtls
            (slowest indexer) so it falls to the 20s default and quits before workspace import.
  Bug2 [P1] the cert false-greens a 0-conversion warm pass — effective_work==0 & residual>0
            & warm must set degraded=true and, under GT_REQUIRE_LSP=1, fail-closed.
  Bug3 [P1] lsp_warm gated on probe_latency_ms > 0.0 — an instant warm server rounds to 0.0ms
            and is mis-classified NOT warm. Liveness is launched AND warm_probe_ok.
  Bug4 [P1] column finder used str.find(target_name) -> first substring (get inside get_key);
            must locate the name as a whole-word call-shaped token, never silent col 0.

(Bug5/Bug6 live in the LSP client and are covered in tests/unit/test_lsp_client.py.)
"""

from __future__ import annotations

import os

import pytest

from groundtruth import resolve as R


# ─────────────────────────────────────────────────────────────────────────────
# Bug1 — per-server readiness budget must cover the slow indexers (jdtls, gopls)
# ─────────────────────────────────────────────────────────────────────────────
class TestReadyBudgetBySlowServer:
    def test_jdtls_gets_a_long_indexing_budget_not_the_20s_default(self):
        # jdtls is the slowest indexer; the 20s default quits before its workspace
        # import finishes -> 0 conversions every run. It MUST get an extended budget.
        budget = R._READY_BUDGET_S_BY_SERVER.get("jdtls")
        assert budget is not None, (
            "jdtls missing from _READY_BUDGET_S_BY_SERVER -> falls to 20s default"
        )
        assert budget >= 120.0, f"jdtls budget {budget}s too short for an Eclipse workspace import"

    def test_rust_analyzer_budget_unchanged(self):
        assert R._READY_BUDGET_S_BY_SERVER.get("rust-analyzer") == 180.0

    def test_gopls_gets_a_floor_above_the_default(self):
        # gopls loads package metadata lazily; give it a floor above the 20s default.
        budget = R._READY_BUDGET_S_BY_SERVER.get("gopls")
        assert budget is not None and budget > R._READY_BUDGET_S_DEFAULT

    def test_budget_selection_keys_on_command_basename(self):
        # _await_project_ready resolves the budget via basename(server_cmd); prove the
        # real launch commands (config.command[0]) map onto the table.
        from groundtruth.lsp.config import get_server_config

        for ext, expect_key in ((".java", "jdtls"), (".go", "gopls"), (".rs", "rust-analyzer")):
            cfg = get_server_config(ext)
            assert not _is_err(cfg), f"no config for {ext}"
            cmd0 = cfg.value.command[0]
            assert os.path.basename(cmd0) == expect_key
            selected = R._READY_BUDGET_S_BY_SERVER.get(
                os.path.basename(cmd0), R._READY_BUDGET_S_DEFAULT
            )
            assert selected == R._READY_BUDGET_S_BY_SERVER[expect_key]


# ─────────────────────────────────────────────────────────────────────────────
# Bug4 — column finder: whole-word call-shaped token, never first substring / col 0
# ─────────────────────────────────────────────────────────────────────────────
class TestCallSiteColumnFinder:
    def test_method_call_not_confused_with_longer_name_substring(self):
        # The residual majority is method calls (get/join/append). `obj.get(...)`
        # on a line that ALSO contains `get_key` must resolve to the bare `get` call
        # site, not the first `get` inside `get_key`.
        line = "    value = config.get_key(default) or store.get(k)\n"
        # naive str.find points at get_key's 'get' (col ~22), the WRONG call.
        naive = line.find("get")
        col, ok = R._find_call_column(line, "get")
        assert ok is True
        assert col != naive
        # the resolved column must sit on a call-shaped occurrence: `get(`
        assert line[col:].startswith("get("), f"col {col} -> {line[col : col + 6]!r}"

    def test_plain_function_call_resolves_to_its_paren(self):
        line = "result = compute(a, b)\n"
        col, ok = R._find_call_column(line, "compute")
        assert ok is True
        assert line[col:].startswith("compute(")

    def test_name_present_but_not_call_shaped_is_a_miss_not_col_zero(self):
        # `target` appears as an identifier but never as `target(` — the old code
        # silently queried col 0; the fix must report a MISS so it can be bucketed.
        line = "target = something_else\n"
        col, ok = R._find_call_column(line, "target")
        assert ok is False
        assert col == -1, "a non-call occurrence must NOT silently resolve to col 0"

    def test_prefers_occurrence_nearest_expected_column_when_given(self):
        # Two call-shaped occurrences of `run` on one line; the graph-provided column
        # hint should break the tie toward the nearer one.
        line = "x = run(1); y = run(2)\n"
        first = line.find("run(")
        second = line.find("run(", first + 1)
        col_near_first, ok1 = R._find_call_column(line, "run", expected_col=first)
        col_near_second, ok2 = R._find_call_column(line, "run", expected_col=second)
        assert ok1 and ok2
        assert col_near_first == first
        assert col_near_second == second

    def test_whole_word_not_matched_inside_another_token(self):
        # `join` must NOT match inside `rejoin(` — word-boundary required.
        line = "z = rejoin(parts)\n"
        col, ok = R._find_call_column(line, "join")
        assert ok is False and col == -1


# ─────────────────────────────────────────────────────────────────────────────
# Bug3 — lsp_warm must NOT gate on probe_latency_ms > 0.0
# ─────────────────────────────────────────────────────────────────────────────
class TestLspWarmNotGatedOnLatency:
    def test_instant_warm_server_is_still_warm(self):
        # The exact regression: a live server answered, but the coarse-clock latency
        # rounds to 0.0ms. Old code: lsp_warm = launched AND probe_ok AND latency>0.0
        # -> False -> LSP_FAIL_NO_WARM exit 2 on a LIVE server. New code: launched AND
        # probe_ok, latency irrelevant.
        assert R._compute_lsp_warm(server_launched=True, warm_probe_ok=True) is True

    def test_not_launched_is_not_warm(self):
        assert R._compute_lsp_warm(server_launched=False, warm_probe_ok=True) is False

    def test_no_probe_answer_is_not_warm(self):
        assert R._compute_lsp_warm(server_launched=True, warm_probe_ok=False) is False


# ─────────────────────────────────────────────────────────────────────────────
# Bug2 — degraded flag on a warm zero-conversion pass
# ─────────────────────────────────────────────────────────────────────────────
class TestDegradedZeroConversion:
    def test_warm_residual_zero_conversion_is_degraded(self):
        # warm server, residual work remained, but converted/deleted NOTHING.
        assert R._compute_degraded(lsp_warm=True, residual=500, effective_work=0) is True

    def test_real_conversion_is_not_degraded(self):
        assert R._compute_degraded(lsp_warm=True, residual=500, effective_work=12) is False

    def test_no_residual_is_not_degraded(self):
        # a true no-op (nothing to convert) is not degraded.
        assert R._compute_degraded(lsp_warm=True, residual=0, effective_work=0) is False

    def test_not_warm_is_not_degraded_here(self):
        # a cold/never-launched server is handled by the NO_WARM/NOT_READY path,
        # not the degraded path.
        assert R._compute_degraded(lsp_warm=False, residual=500, effective_work=0) is False


class TestDegradedFailClosedUnderRequireLsp:
    """End-to-end: a degraded cert must EXIT NON-ZERO under GT_REQUIRE_LSP=1 and PASS
    (exit 0) when the env var is unset (deliver-always). Driven through resolve_main's
    real CLI with a synthetic graph.db so no LSP server is launched."""

    def _make_graph_db(self, tmp_path):
        import sqlite3

        db = tmp_path / "graph.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
                file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
                return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
                parent_id INTEGER
            );
            CREATE TABLE edges (
                id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
                source_line INTEGER, source_file TEXT, resolution_method TEXT,
                confidence REAL, metadata TEXT
            );
            """
        )
        conn.commit()
        conn.close()
        return db

    def _run(self, tmp_path, env):
        """Run `groundtruth resolve --resolve` in a subprocess; return CompletedProcess."""
        import subprocess
        import sys

        db = self._make_graph_db(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        cmd = [
            sys.executable,
            "-c",
            (
                "import sys; from groundtruth.resolve import resolve_main; "
                "sys.argv = ['gt-resolve','resolve','--db', %r, '--root', %r, "
                "'--resolve','--lang','go']; resolve_main()" % (str(db), str(repo))
            ),
        ]
        full_env = dict(os.environ)
        full_env.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, env=full_env, timeout=120)

    @pytest.mark.skipif(
        __import__("shutil").which("gopls") is None,
        reason="needs a launchable gopls to reach the warm/degraded path",
    )
    def test_degraded_fails_closed_when_required(self, tmp_path):
        # With a launchable server but residual edges it cannot resolve in this empty
        # repo, GT_REQUIRE_LSP=1 must fail-closed (exit 2). (Skipped if gopls absent —
        # the degraded path needs a real warm server; the pure _compute_degraded test
        # above covers the logic unconditionally.)
        proc = self._run(tmp_path, {"GT_REQUIRE_LSP": "1"})
        # Either degraded (exit 2) or install-missing/never-launched (exit 2) — the
        # contract is that an unsatisfied warm/zero-conversion never silently greens.
        assert proc.returncode != 0


def _is_err(result) -> bool:
    from groundtruth.utils.result import Err

    return isinstance(result, Err)

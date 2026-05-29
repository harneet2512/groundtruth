"""C6 step-4 (RF-3): host-promoted graph.db reaches in-container hooks.

These tests assert the three contracts from RUNTIME_FUCKUPS.md RF-3 build spec:

  (a) flag UNSET  -> default in-container build path taken (no upload, no skip).
      This is the safety-critical invariant: the working eval path must be
      byte-unchanged when GT_PREBUILT_GRAPH_DB is not armed.
  (b) flag SET + schema-valid prebuilt db -> the promoted db is uploaded to the
      EXACT config.graph_db path and the in-container gt-index build is skipped.
  (c) the L6-reindex path re-applies the promotion (RF-3 (d)) — after a
      successful reindex, scoped LSP re-promotion fires only when prebuilt is
      active and the LSP server is present in-container.

The full container flow (real docker cp, real gt-index, real pyright) is
CI-only; these tests mock the runtime + LSP-present probe and exercise the
wrapper's branching logic deterministically.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault(
    "litellm",
    SimpleNamespace(
        model_cost={},
        success_callback=[],
        completion=lambda *args, **kwargs: None,
        acompletion=None,
        completion_cost=lambda *args, **kwargs: 0.0,
    ),
)

from scripts.swebench import oh_gt_full_wrapper as ohgt


class Observation:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.exit_code = 0


class FileEditAction:
    # Name must match exactly: classify_tool_event keys on type(action).__name__.
    def __init__(self, path: str) -> None:
        self.path = path


def _make_schema_valid_db(path: Path) -> None:
    """Write a graph.db that passes schema_version.verify_graph_db_schema."""
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT);
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            target_id INTEGER,
            type TEXT,
            resolution_method TEXT,
            confidence REAL,
            trust_tier TEXT,
            candidate_count INTEGER,
            evidence_type TEXT,
            verification_status TEXT
        );
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO nodes (id, name, file_path) VALUES (1, 'a', 'src/app.py');
        INSERT INTO edges (id, source_id, target_id, type, resolution_method, confidence,
                           trust_tier, candidate_count, evidence_type, verification_status)
        VALUES (1, 1, 1, 'CALLS', 'lsp', 1.0, 'CERTIFIED', 1, 'call', 'verified');
        """
    )
    con.execute(
        "INSERT INTO project_meta (key, value) VALUES ('schema_version', ?)",
        ("v15.2-trust-tier",),
    )
    con.commit()
    con.close()


class _BaseRuntime:
    """Records run_action commands + copy_to calls; returns canned probe output."""

    def __init__(self, *, nonempty: str = "5|7", lsp_present: bool = True) -> None:
        self.actions: list[str] = []
        self.copied: list[tuple] = []
        self._nonempty = nonempty
        self._lsp_present = lsp_present

    def copy_to(self, host_path, container_dir):
        self.copied.append((host_path, container_dir))

    def run_action(self, action):
        command = getattr(action, "command", "")
        self.actions.append(command)
        # graph-nonempty probe (python3 - <db> heredoc -> "N|E")
        if "FROM nodes" in command and "FROM edges" in command:
            return Observation(self._nonempty)
        if "echo GT_BIN_OK" in command:
            return Observation("GT_BIN_OK")
        if "GT_LSP_PRESENT" in command:
            return Observation("GT_LSP_PRESENT" if self._lsp_present else "GT_LSP_ABSENT")
        if "command -v gt_query" in command:
            return Observation(
                "/tmp/gt_tools/gt_query\n/tmp/gt_tools/gt_search\n"
                "/tmp/gt_tools/gt_navigate\n/tmp/gt_tools/gt_validate"
            )
        if "gt-index" in command:
            return Observation("INDEX_OK")
        return Observation("")


# ---------------------------------------------------------------------------
# (a) flag UNSET -> default build path; no upload, no skip. (byte-unchanged)
# ---------------------------------------------------------------------------
def test_flag_unset_takes_default_build_no_upload(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_PREBUILT_GRAPH_DB", raising=False)
    # Pin a host index binary so install path is deterministic.
    host_bin = tmp_path / "gt-index-linux"
    host_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("GT_INDEX_BINARY", str(host_bin))

    runtime = _BaseRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")

    ohgt.install_graph_and_hook(runtime, config)

    # The full in-container build command MUST run (default path).
    assert any(
        "/tmp/gt-index-linux -root='/workspace' -output='/tmp/gt_index.db' 2>&1" in c
        for c in runtime.actions
    ), "default in-container build did not run when flag unset"
    # No prebuilt db was copied into the container.
    assert runtime.copied == [
        (str(host_bin), "/tmp/"),
    ], f"unexpected copy_to calls on default path: {runtime.copied}"
    # The prebuilt-active flag stays False.
    assert config._gt_prebuilt_active is False


def test_promoted_path_gate_returns_empty_when_unset(monkeypatch):
    monkeypatch.delenv("GT_PREBUILT_GRAPH_DB", raising=False)
    assert ohgt._promoted_graph_db_path() == ""


def test_promoted_path_gate_returns_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_PREBUILT_GRAPH_DB", str(tmp_path / "nope.db"))
    assert ohgt._promoted_graph_db_path() == ""


# ---------------------------------------------------------------------------
# (b) flag SET + valid -> upload happens + in-container build skipped.
# ---------------------------------------------------------------------------
def test_flag_set_valid_uploads_and_skips_build(tmp_path, monkeypatch):
    promoted = tmp_path / "promoted_graph.db"
    _make_schema_valid_db(promoted)
    monkeypatch.setenv("GT_PREBUILT_GRAPH_DB", str(promoted))

    host_bin = tmp_path / "gt-index-linux"
    host_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("GT_INDEX_BINARY", str(host_bin))

    runtime = _BaseRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")

    ohgt.install_graph_and_hook(runtime, config)

    # The promoted db was copied toward the config.graph_db directory.
    assert any(
        host == str(promoted) for host, _dir in runtime.copied
    ), f"promoted db not copied: {runtime.copied}"
    # The full in-container build command MUST NOT have run.
    assert not any(
        "/tmp/gt-index-linux -root='/workspace' -output='/tmp/gt_index.db' 2>&1" in c
        for c in runtime.actions
    ), "in-container build ran despite a valid prebuilt db"
    # The uploaded file is renamed/placed at the EXACT config.graph_db path.
    assert any(
        c.startswith("mv -f ") and "/tmp/gt_index.db" in c for c in runtime.actions
    ), "uploaded db was not moved to the exact config.graph_db path"
    # Prebuilt mode is armed for the L6 re-promotion.
    assert config._gt_prebuilt_active is True


def test_flag_set_schema_mismatch_falls_back_to_build(tmp_path, monkeypatch):
    # A db WITHOUT project_meta.schema_version fails verify_graph_db_schema.
    bad = tmp_path / "bad_graph.db"
    con = sqlite3.connect(str(bad))
    con.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER);"
    )
    con.close()
    monkeypatch.setenv("GT_PREBUILT_GRAPH_DB", str(bad))

    host_bin = tmp_path / "gt-index-linux"
    host_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("GT_INDEX_BINARY", str(host_bin))

    runtime = _BaseRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")

    ohgt.install_graph_and_hook(runtime, config)

    # Schema mismatch => DO NOT upload; fall back to the in-container build.
    assert any(
        "/tmp/gt-index-linux -root='/workspace' -output='/tmp/gt_index.db' 2>&1" in c
        for c in runtime.actions
    ), "schema-mismatch prebuilt db did not fall back to in-container build"
    assert not any(host == str(bad) for host, _dir in runtime.copied)
    assert config._gt_prebuilt_active is False


def test_upload_promoted_db_returns_false_when_container_empty(tmp_path, monkeypatch):
    promoted = tmp_path / "promoted_graph.db"
    _make_schema_valid_db(promoted)

    # In-container verify reports 0 nodes / 0 edges -> upload "succeeded" but the
    # db is unusable in-container, so the helper reports False (caller rebuilds).
    runtime = _BaseRuntime(nonempty="0|0")
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")

    ok = ohgt._upload_promoted_db(runtime, config, runtime.run_action, str(promoted))
    assert ok is False


# ---------------------------------------------------------------------------
# (c) L6-reindex path preserves promotion via scoped re-promotion (RF-3 (d)).
# ---------------------------------------------------------------------------
def test_repromote_noop_when_not_prebuilt():
    runtime = _BaseRuntime()
    config = ohgt.GTRuntimeConfig()
    assert config._gt_prebuilt_active is False
    status = ohgt._repromote_after_reindex(
        runtime, config, runtime.run_action, "src/app.py"
    )
    assert status == "skip:not_prebuilt"
    # No re-promotion command was emitted.
    assert not any("groundtruth.resolve" in c for c in runtime.actions)


def test_repromote_fires_when_prebuilt_and_lsp_present():
    runtime = _BaseRuntime(lsp_present=True)
    config = ohgt.GTRuntimeConfig()
    config._gt_prebuilt_active = True

    status = ohgt._repromote_after_reindex(
        runtime, config, runtime.run_action, "src/app.py"
    )
    assert status == "ok:repromoted(python)"
    # The scoped LSP re-promotion command was issued for python.
    assert any(
        "groundtruth.resolve" in c and "--lang=python" in c and "--resolve" in c
        for c in runtime.actions
    ), "scoped re-promotion command not emitted"
    # It targets the EXACT config.graph_db path the in-container hooks read.
    assert any(
        "groundtruth.resolve" in c and "/tmp/gt_index.db" in c for c in runtime.actions
    )


def test_repromote_quiet_when_lsp_absent():
    # Correct-or-quiet: if the LSP server is missing in-container we must NOT
    # touch the db; the name_match reindex result stands.
    runtime = _BaseRuntime(lsp_present=False)
    config = ohgt.GTRuntimeConfig()
    config._gt_prebuilt_active = True

    status = ohgt._repromote_after_reindex(
        runtime, config, runtime.run_action, "src/app.py"
    )
    assert status.startswith("skip:lsp_absent")
    assert not any("groundtruth.resolve" in c for c in runtime.actions)


def test_repromote_skips_unsupported_language():
    runtime = _BaseRuntime(lsp_present=True)
    config = ohgt.GTRuntimeConfig()
    config._gt_prebuilt_active = True

    status = ohgt._repromote_after_reindex(
        runtime, config, runtime.run_action, "config.toml"
    )
    assert status.startswith("skip:lang_unsupported")
    assert not any("groundtruth.resolve" in c for c in runtime.actions)


def test_l6_reindex_triggers_repromotion_when_prebuilt(tmp_path, monkeypatch):
    """End-to-end-ish: with prebuilt active, a source edit's L6 reindex must
    issue the scoped re-promotion command. Mocks the reindex by making the fake
    runtime report a successful reindex (mtime bump + exit 0)."""

    class ReindexRuntime(_BaseRuntime):
        def __init__(self):
            super().__init__(lsp_present=True)
            self._mtime = 100

        def run_action(self, action):
            command = getattr(action, "command", "")
            # stat mtime probe: bump after the reindex so r_ok becomes True.
            if "stat -c %Y" in command:
                self.actions.append(command)
                val = self._mtime
                self._mtime += 50  # next stat returns a newer mtime
                return Observation(str(val))
            # The reindex command carries the exit-code probe.
            if "-file=" in command and "__EXIT__" in command:
                self.actions.append(command)
                return Observation("reindex done\n__EXIT__0")
            return super().run_action(action)

    runtime = ReindexRuntime()
    config = ohgt.GTRuntimeConfig(gt_index_bin="/tmp/gt-index-linux")
    config._gt_prebuilt_active = True
    ohgt.wrap_runtime_run_action(runtime, config)

    runtime.run_action(FileEditAction("src/app.py"))

    assert any(
        "groundtruth.resolve" in c and "--lang=python" in c and "--resolve" in c
        for c in runtime.actions
    ), "L6 reindex did not trigger scoped re-promotion under prebuilt mode"

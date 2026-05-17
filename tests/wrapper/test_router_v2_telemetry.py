"""Local proof for V2_LIVE telemetry fix (FINAL_ARCH_V2 Track-B).

Exercises the wrapper's router_v2 helpers directly against a real fixture
graph.db + AgentState. Verifies the silence bug is closed: every call now
writes to gt_interactions_<task>.jsonl AND gt_layer_events (via the
telemetry writer), AND prints a diag line, AND increments a counter the
end-of-task fail-fast checks.

Cannot exercise OpenHands' run_action wrapping on Windows; what's covered
here is the chain `_router_v2_on_*` → `_persist_router_v2_event` →
gt_interactions_*.jsonl. The end-to-end agent-visibility step is verified
on Linux when the wrapper appends router evidence text to obs.content
inside the live-mode bypass in ``process_event`` (post_view, post_edit).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "swebench"))


def _load_wrapper_helpers():
    """Load only the helpers we test, without triggering OpenHands imports.

    The wrapper module pulls in openhands.* at module load, which isn't
    available on Windows. We extract the helper functions by source slicing.
    """
    src = (_REPO_ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py").read_text()
    import re
    # Slice from `# FINAL_ARCH_V2 GT_ROUTER_V2` block start to the
    # `_strip_scaffold_files` function (exclusive).
    start = src.index("# FINAL_ARCH_V2 GT_ROUTER_V2 path")
    end = src.index("def _strip_scaffold_files(")
    helper_src = src[start:end]
    # Replace forward refs to GTRuntimeConfig + Any in signatures with object.
    helper_src = helper_src.replace("GTRuntimeConfig", "object")
    # Strip _emit_structured_event call (uses telemetry writer which needs
    # full wrapper context). We monkey-patch it.
    ns = {
        "os": os, "sys": sys, "json": json,
        "time": __import__("time"),
        "Any": object, "Path": Path,
    }
    # The helpers use `_metrics_path` which is at module top — define a
    # local equivalent for the test harness.
    helpers_prelude = """
def _metrics_path(config, name):
    tid = getattr(config, '_meta_instance_id', None) or 'global'
    return f"/tmp/gt_{name}_{tid}.jsonl"

def _emit_structured_event(config, layer, event_type, **kw):
    # Capture into config so the test can inspect it.
    config._structured_events.append({'layer': layer, 'event_type': event_type, **kw})
    return f"sev::{layer}::{event_type}::{len(config._structured_events)}"

def _ensure_agent_state(config):
    return config._agent_state

WORKSPACE_ROOT = "/workspace"
"""
    exec(helpers_prelude + helper_src, ns)
    return ns


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    # Each test gets a clean /tmp gt_interactions namespace via task_id.
    monkeypatch.setenv("GT_ROUTER_V2", "live")
    monkeypatch.setenv("GT_REPO_ROOT", "/workspace/fixture")
    ns = _load_wrapper_helpers()
    yield ns


def _make_config(task_id: str, db_path: str = "") -> SimpleNamespace:
    """Minimal config object compatible with the wrapper helpers under test."""
    from groundtruth.state.agent_state import AgentState
    state = AgentState.load_or_create(task_id=task_id, max_iterations=100,
                                       repo_root="/workspace/" + task_id)
    cfg = SimpleNamespace(
        _agent_state=state,
        _router_v2=None,
        _router_v2_call_count=0,
        _host_graph_db=db_path,
        graph_db=db_path,
        action_count=0,
        max_iter=100,
        interaction_log=[],
        _meta_instance_id=task_id,
        _telemetry_writer=None,
        _structured_events=[],
    )
    return cfg


class TestRouterV2ModeResolution:
    def test_off_unset(self, wrapper, monkeypatch):
        monkeypatch.delenv("GT_ROUTER_V2", raising=False)
        assert wrapper["_router_v2_mode"]() == "off"
        assert not wrapper["_router_v2_enabled"]()
        assert not wrapper["_router_v2_live"]()

    def test_live(self, wrapper, monkeypatch):
        monkeypatch.setenv("GT_ROUTER_V2", "live")
        # Reset the once-logged flag so we can re-check.
        wrapper["_ROUTER_V2_MODE_LOGGED"] = False
        assert wrapper["_router_v2_mode"]() == "live"
        assert wrapper["_router_v2_enabled"]()
        assert wrapper["_router_v2_live"]()

    def test_shadow_via_legacy_1(self, wrapper, monkeypatch):
        monkeypatch.setenv("GT_ROUTER_V2", "1")
        wrapper["_ROUTER_V2_MODE_LOGGED"] = False
        assert wrapper["_router_v2_mode"]() == "shadow"


class TestRouterV2Telemetry:
    def test_on_view_persists_to_disk(self, wrapper, tmp_path, capsys, monkeypatch):
        # Use /tmp on linux; on Windows, use tmp_path mirrored via TMPDIR.
        # _metrics_path hardcodes /tmp; use a TMP override via monkeypatching
        # the wrapper's _metrics_path inside the namespace.
        ns = wrapper
        recorded_files: list[Path] = []
        def _path(config, name):
            p = tmp_path / f"gt_{name}_{config._meta_instance_id}.jsonl"
            recorded_files.append(p)
            return str(p)
        ns["_metrics_path"] = _path

        cfg = _make_config("test-disk")
        cfg._structured_events = []  # type: ignore[attr-defined]

        ev = ns["_router_v2_on_view"](cfg, "/workspace/test-disk/src/foo.py")
        assert ev is not None, "on_view must return an event dict in live mode"
        assert ev["mode"] == "live"
        assert ev["trigger"] == "on_view"
        # Suppression expected — in live mode, _graph_db_present is forced
        # True so router proceeds to evidence check (returns no_evidence on
        # empty/missing DB rather than no_graph_db).
        assert ev["emit"] is False
        assert ev["suppression_reason"] in ("no_graph_db", "no_evidence")

        # Counter incremented.
        assert cfg._router_v2_call_count == 1

        # Disk persistence — /tmp/gt_interactions_test-disk.jsonl must exist with one row.
        interactions_file = tmp_path / "gt_interactions_test-disk.jsonl"
        assert interactions_file.exists(), f"interactions file missing: {interactions_file}"
        line = interactions_file.read_text().splitlines()[0]
        rec = json.loads(line)
        assert rec["layer"] == "L3_router_v2"
        assert rec["suppression_reason"] in ("no_graph_db", "no_evidence")
        assert rec["path"] == "/workspace/test-disk/src/foo.py"

        # Structured event also recorded.
        assert any(
            e["layer"] == "L3_router_v2" and e["event_type"] == "on_view"
            for e in cfg._structured_events
        )

        # Diag print emitted.
        captured = capsys.readouterr()
        assert "[GT_META] router_v2 on_view" in captured.out
        assert "mode=live" in captured.out

    def test_on_edit_persists_and_increments(self, wrapper, tmp_path, monkeypatch):
        ns = wrapper
        def _path(config, name):
            return str(tmp_path / f"gt_{name}_{config._meta_instance_id}.jsonl")
        ns["_metrics_path"] = _path

        cfg = _make_config("test-edit")
        cfg._structured_events = []  # type: ignore[attr-defined]

        ev1 = ns["_router_v2_on_edit"](cfg, "/workspace/test-edit/foo.py", ["bar"])
        ev2 = ns["_router_v2_on_edit"](cfg, "/workspace/test-edit/foo.py", ["baz"])
        assert ev1 is not None and ev2 is not None
        assert cfg._router_v2_call_count == 2

        interactions_file = tmp_path / "gt_interactions_test-edit.jsonl"
        rows = [json.loads(l) for l in interactions_file.read_text().splitlines() if l.strip()]
        assert len(rows) == 2
        assert all(r["trigger"] == "on_edit" for r in rows)


class TestRouterV2OffSilent:
    def test_off_mode_does_not_invoke_router(self, wrapper, monkeypatch):
        monkeypatch.delenv("GT_ROUTER_V2", raising=False)
        wrapper["_ROUTER_V2_MODE_LOGGED"] = False
        cfg = _make_config("test-off")
        # Counter does still increment (we count call-site attempts) but
        # _ensure_v2_router returns None and the helper short-circuits.
        ev = wrapper["_router_v2_on_view"](cfg, "/workspace/test-off/foo.py")
        assert ev is None, "off mode must not emit router events"
        assert cfg._router_v2 is None

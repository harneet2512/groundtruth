"""Step-3 strangler: ledger.py is THE canonical delivery-ledger, with an optional
SIGKILL-safe append-line file sink (the independent home of the semantics the mini
seam's ``_ledger_line_direct`` provides; the mini adopts THIS in W2).

Characterization tests pin that the EXISTING Ledger API is byte-identical when no
sink is configured (the default), plus RED-first tests for the new durable sink.
"""
from __future__ import annotations

import json

from groundtruth.runtime.ledger import (
    Ledger,
    SignalOutcome,
    append_ledger_line,
    resolve_sink_path,
)


# --------------------------------------------------------------------------- #
# characterization: EXISTING API is preserved (no sink == today's behaviour)
# --------------------------------------------------------------------------- #
def test_default_ledger_has_no_sink_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "should_not_exist.jsonl"))
    led = Ledger()                                   # default: no sink
    led.delivered("l3b", "post_edit", "src/a.py", chars=10, iteration=2)
    led.suppressed("l3", "post_view", "src/b.py",
                   SignalOutcome.SUPPRESSED_DUPLICATE, reason="dup", iteration=3)
    assert not (tmp_path / "should_not_exist.jsonl").exists()  # env alone does NOT arm
    assert len(led.entries) == 2
    assert led.summary() == {"delivered": 1, "suppressed_duplicate": 1}
    assert led.has_silent_drops() is False
    lines = led.to_jsonl().splitlines()
    assert json.loads(lines[0])["outcome"] == "delivered"
    assert json.loads(lines[1])["outcome"] == "suppressed_duplicate"


# --------------------------------------------------------------------------- #
# new: SIGKILL-safe append-line file sink
# --------------------------------------------------------------------------- #
def test_ledger_sink_writes_one_durable_line_per_record(tmp_path):
    sink = tmp_path / "sub" / "ledger.jsonl"          # parent dir does not exist yet
    led = Ledger(sink_path=str(sink))
    led.delivered("l3b", "post_edit", "src/a.py", chars=7, iteration=1)
    led.suppressed("l5", "test_result", "src/b.py",
                   SignalOutcome.SUPPRESSED_WRONG_PHASE, reason="phase", iteration=4)
    # durable the instant it is written (no explicit flush/close call by the test)
    assert sink.exists()
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    r0 = json.loads(lines[0])
    # schema-identical to LedgerEntry.to_dict / to_jsonl
    assert set(r0) == {"layer", "event_type", "file_path", "outcome", "reason",
                       "chars_delivered", "timestamp_ms", "iteration"}
    assert r0["outcome"] == "delivered" and r0["file_path"] == "src/a.py"
    assert json.loads(lines[1])["outcome"] == "suppressed_wrong_phase"
    # the in-memory entries are ALSO retained (sink is additive)
    assert len(led.entries) == 2


def test_ledger_sink_appends_across_ledger_instances(tmp_path):
    """SIGKILL-safety comes from append-mode (never truncate-rewrite): a second
    Ledger on the same path APPENDS, it does not clobber the prior run's lines."""
    sink = str(tmp_path / "ledger.jsonl")
    Ledger(sink_path=sink).delivered("a", "e", "f1", chars=1)
    Ledger(sink_path=sink).delivered("b", "e", "f2", chars=1)
    lines = open(sink, encoding="utf-8").read().splitlines()
    assert len(lines) == 2
    assert [json.loads(x)["file_path"] for x in lines] == ["f1", "f2"]


def test_append_ledger_line_standalone_is_durable_and_injects_timestamp(tmp_path):
    sink = str(tmp_path / "deep" / "nested" / "l.jsonl")
    append_ledger_line({"layer": "root", "event_type": "", "file_path": "x",
                        "outcome": "provider_failed", "reason": "boom",
                        "chars_delivered": 0, "iteration": 0}, sink)
    rec = json.loads(open(sink, encoding="utf-8").read().splitlines()[0])
    assert rec["outcome"] == "provider_failed"
    assert "timestamp_ms" in rec and isinstance(rec["timestamp_ms"], int)


def test_append_ledger_line_is_best_effort_never_raises():
    # a path that cannot be created (empty string) must NOT raise — telemetry must
    # never break the agent loop.
    append_ledger_line({"outcome": "delivered"}, "")


def test_resolve_sink_path_env_and_default(monkeypatch):
    monkeypatch.delenv("GT_RUNTIME_LEDGER", raising=False)
    assert resolve_sink_path().endswith("gt_runtime_ledger.jsonl")
    assert resolve_sink_path("/x/y.jsonl") == "/x/y.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", "/custom/led.jsonl")
    assert resolve_sink_path() == "/custom/led.jsonl"
    assert resolve_sink_path("/explicit.jsonl") == "/explicit.jsonl"  # explicit wins

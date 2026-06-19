"""Tests for JSONL writer."""
from __future__ import annotations

import json
import os
from groundtruth.telemetry.schemas import GTLayerEvent, GTAgentReactionEvent, GTBeliefEvent
from groundtruth.telemetry.writer import GTTelemetryWriter


class TestGTTelemetryWriter:
    def test_creates_files_on_write(self, tmp_path):
        w = GTTelemetryWriter("run1", "task1", str(tmp_path))
        event = GTLayerEvent(layer="L3", event_type="evidence", eligible=True, emitted=True, suppressed=False)
        w.emit_layer_event(event)
        w.close()
        assert os.path.exists(w.layer_events_path)
        with open(w.layer_events_path) as f:
            line = json.loads(f.readline())
            assert line["layer"] == "L3"
            assert line["run_id"] == "run1"
            assert line["task_id"] == "task1"

    def test_append_only(self, tmp_path):
        w = GTTelemetryWriter("run1", "task1", str(tmp_path))
        for i in range(3):
            event = GTLayerEvent(layer="L1", event_type=f"ev{i}", eligible=True, emitted=True, suppressed=False)
            w.emit_layer_event(event)
        w.close()
        with open(w.layer_events_path) as f:
            lines = f.readlines()
            assert len(lines) == 3

    def test_belief_event(self, tmp_path):
        w = GTTelemetryWriter("run1", "task1", str(tmp_path))
        belief = GTBeliefEvent(file_path="src/foo.py", new_status="candidate", reason="L1", source_event_id="abc")
        w.emit_belief_event(belief)
        w.close()
        assert os.path.exists(w.belief_ledger_path)
        with open(w.belief_ledger_path) as f:
            line = json.loads(f.readline())
            assert line["file_path"] == "src/foo.py"

    def test_reaction_event(self, tmp_path):
        w = GTTelemetryWriter("run1", "task1", str(tmp_path))
        reaction = GTAgentReactionEvent(gt_event_id="evt1", gt_layer="L3", gt_iter=5, follow_type="IGNORED")
        w.emit_agent_reaction(reaction)
        w.close()
        assert os.path.exists(w.agent_reactions_path)

    def test_close_prevents_writes(self, tmp_path):
        w = GTTelemetryWriter("run1", "task1", str(tmp_path))
        w.close()
        event = GTLayerEvent(layer="L1", event_type="x", eligible=True, emitted=True, suppressed=False)
        eid = w.emit_layer_event(event)
        assert eid
        assert not os.path.exists(w.layer_events_path)

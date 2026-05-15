"""Thread-safe JSONL writer for GT telemetry streams."""

from __future__ import annotations

import json
import os
import threading
from typing import IO

from .schemas import GTLayerEvent, GTAgentReactionEvent, GTBeliefEvent, GTAgentEvent


class GTTelemetryWriter:
    """Writes 4 append-only JSONL files + run summary. Thread-safe, flush-on-write."""

    def __init__(self, run_id: str, task_id: str, output_dir: str = "/tmp") -> None:
        self.run_id = run_id
        self.task_id = task_id
        self._lock = threading.Lock()
        self._closed = False

        os.makedirs(output_dir, exist_ok=True)
        self._layer_path = os.path.join(output_dir, f"gt_layer_events_{task_id}.jsonl")
        self._reaction_path = os.path.join(output_dir, f"gt_agent_reactions_{task_id}.jsonl")
        self._belief_path = os.path.join(output_dir, f"gt_belief_ledger_{task_id}.jsonl")
        self._agent_path = os.path.join(output_dir, f"gt_agent_events_{task_id}.jsonl")
        self._summary_path = os.path.join(output_dir, f"gt_run_summary_{task_id}.json")

        self._layer_fh: IO[str] | None = None
        self._reaction_fh: IO[str] | None = None
        self._belief_fh: IO[str] | None = None
        self._agent_fh: IO[str] | None = None

    def _ensure_open(self, attr: str, path: str) -> IO[str]:
        fh = getattr(self, attr)
        if fh is None:
            fh = open(path, "a", encoding="utf-8")
            setattr(self, attr, fh)
        return fh

    def _write_line(self, fh: IO[str], data: dict) -> None:
        line = json.dumps(data, default=str, ensure_ascii=False)
        fh.write(line + "\n")
        fh.flush()

    def emit_layer_event(self, event: GTLayerEvent) -> str:
        if self._closed:
            return event.event_id
        event.run_id = self.run_id
        event.task_id = self.task_id
        d = event.to_dict()
        with self._lock:
            fh = self._ensure_open("_layer_fh", self._layer_path)
            self._write_line(fh, d)
        return event.event_id

    def emit_agent_reaction(self, event: GTAgentReactionEvent) -> str:
        if self._closed:
            return event.gt_event_id
        event.run_id = self.run_id
        event.task_id = self.task_id
        d = event.to_dict()
        with self._lock:
            fh = self._ensure_open("_reaction_fh", self._reaction_path)
            self._write_line(fh, d)
        return event.gt_event_id

    def emit_belief_event(self, event: GTBeliefEvent) -> str:
        if self._closed:
            return event.event_id
        event.run_id = self.run_id
        event.task_id = self.task_id
        d = event.to_dict()
        with self._lock:
            fh = self._ensure_open("_belief_fh", self._belief_path)
            self._write_line(fh, d)
        return event.event_id

    def emit_agent_event(self, event: GTAgentEvent) -> str:
        if self._closed:
            return event.agent_action_id
        event.run_id = self.run_id
        event.task_id = self.task_id
        d = event.to_dict()
        with self._lock:
            fh = self._ensure_open("_agent_fh", self._agent_path)
            self._write_line(fh, d)
        return event.agent_action_id

    def write_run_summary(self, summary: dict) -> None:
        """Write aggregated run summary as single JSON file."""
        try:
            with open(self._summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, default=str, ensure_ascii=False)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for attr in ("_layer_fh", "_reaction_fh", "_belief_fh", "_agent_fh"):
                fh = getattr(self, attr)
                if fh is not None:
                    try:
                        fh.flush()
                        fh.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)

    @property
    def layer_events_path(self) -> str:
        return self._layer_path

    @property
    def agent_reactions_path(self) -> str:
        return self._reaction_path

    @property
    def belief_ledger_path(self) -> str:
        return self._belief_path

    @property
    def agent_events_path(self) -> str:
        return self._agent_path

    @property
    def run_summary_path(self) -> str:
        return self._summary_path

#!/usr/bin/env python3
"""Controlled ack probe for live GCP environments.

This uses the real swe_agent_state_gt.py module and the same file-based
observation path as the smoke runs, but with minimal synthetic windows so we
can verify whether ack_followed / ack_ignored / ack_not_observed are
observable end-to-end.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "swe_agent_state_gt_probe",
        Path(__file__).resolve().with_name("swe_agent_state_gt.py"),
    )
    if not spec or not spec.loader:
        raise RuntimeError("unable to load swe_agent_state_gt.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _arm(mod, cycle: int, focus_file: str):
    mod.GT_ACK_STATE.write_text(json.dumps({
        "cycle": cycle,
        "channel": "probe",
        "tier": "likely",
        "intervention_id": "probe-123",
        "expected_next_action": "submit or repair",
        "confidence_tier": "likely",
        "file": focus_file,
        "file_key": list(mod._file_suffix_key(focus_file)),
        "symbol": "",
        "pre_emit_action": "",
        "pre_emit_changed": [],
        "pre_emit_file_refs": [],
        "pre_emit_symbol_refs": [],
        "expires_at_cycle": cycle + mod.NEXT_WINDOW_SIZE,
    }))


def run_case(case: str) -> int:
    mod = _load_module()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        os.environ["GT_ARM"] = "gt-probe"
        os.environ["GT_RUN_ID"] = "probe-run"
        os.environ["GT_INSTANCE_ID"] = "probe-task"
        os.environ["GT_TELEMETRY_DIR"] = str(td / "host")
        (td / "host").mkdir()

        mod.GT_ACK_STATE = td / "ack.json"
        mod.GT_LAST_ACTION = td / "last_action.txt"
        mod.GT_BUDGET_EVENTS = td / "budget_events.jsonl"
        mod.GT_BUDGET_EVENTS_OFFSET = td / "budget_events.offset"
        mod.GT_TELEMETRY = td / "telemetry.jsonl"
        mod.GT_POLICY_STATE = td / "policy.json"
        mod.GT_TOOL_COUNTS = td / "tool_counts.json"
        mod.GT_HASHES = td / "hashes.json"
        mod.STATE_PATH = td / "state.json"

        focus = "astropy/io/fits/hdu/table.py"
        _arm(mod, 5, focus)
        if case == "follow":
            mod.GT_BUDGET_EVENTS.write_text(
                '{"event":"submit_observed","status":"allowed","file":"astropy/io/fits/hdu/table.py","ts":1}\n'
            )
            mod._check_ack(6, "", [])
        elif case == "ignore":
            mod.GT_BUDGET_EVENTS.write_text(
                '{"event":"submit_observed","status":"blocked","file":"astropy/io/fits/hdu/table.py","ts":1}\n'
            )
            mod._check_ack(6, "", [])
        elif case == "expire":
            for c in range(6, 6 + mod.NEXT_WINDOW_SIZE + 2):
                mod._check_ack(c, "", [])
        else:
            raise SystemExit(f"unknown case: {case}")

        events = _read_events(mod.GT_TELEMETRY)
        print(json.dumps({"case": case, "events": events}, indent=2))
        return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: gt_ack_probe.py <follow|ignore|expire>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run_case(sys.argv[1]))

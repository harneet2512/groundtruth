"""CP007 — consumption ledger tests."""
import importlib.util
import os

_CL_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "swebench", "consumption_ledger.py")
_spec = importlib.util.spec_from_file_location("consumption_ledger_t", _CL_PATH)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)


def test_block_consumed_when_agent_opens_same_file_next_turn():
    traj = [
        {"observation": "<gt-evidence>foo.py::bar invariant</gt-evidence>", "action": ""},
        {"observation": "", "action": "cat src/foo.py"},
        {"observation": "", "action": "echo done"},
    ]
    ledger = cl.build_consumption_ledger(traj, window=3)
    assert ledger["gt_blocks_delivered"] == 1
    assert ledger["gt_blocks_consumed"] == 1
    assert ledger["entries"][0]["consumed"] is True


def test_no_delivery_no_blocks():
    traj = [{"observation": "plain output", "action": "ls"}]
    ledger = cl.build_consumption_ledger(traj)
    assert ledger["gt_blocks_delivered"] == 0

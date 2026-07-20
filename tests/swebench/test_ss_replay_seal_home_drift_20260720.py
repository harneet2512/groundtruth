"""D-N (run6, loguru iters 10/14 fell to full-scan): reconstruct_task homed each
delivered seal with the calibrated index `home = 2*iter + 1`, which hard-codes a
2-message prelude AND strict assistant/tool alternation. Any interstitial message
(a reasoning turn with no command, a format re-prompt) shifts later raw indices
without advancing the iteration counter, so the calibrated home drifts and the
delivery falls to the O(M) full-scan — which takes the FIRST global occurrence of
the seal bytes. When GT delivered byte-identical deltas at two iterations, the
later delivery is mis-homed onto the earlier message (corrupting home_msg, which
feeds proximity/consumption grading).

Fix: derive `home` from the actual tool-message index map (tool_msg_indices[it-1])
— reduces to 2*iter+1 on the canonical shape, stays exact across interstitials —
and, in the fallback, prefer the match NEAREST the expected index over the first
global occurrence.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORACLE = ROOT / "scripts" / "swebench" / "ss_replay_oracle.py"


def _load():
    spec = importlib.util.spec_from_file_location("ss_replay_oracle_dn", ORACLE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses/InitVar resolution needs this registered
    spec.loader.exec_module(mod)
    return mod


_DELTA = "GTX__DUPLICATE_DELTA_PAYLOAD__ZZ"   # ASCII -> bytes == chars
_SHA16 = hashlib.sha256(_DELTA.encode("utf-8")).hexdigest()[:16]
_N = len(_DELTA)


def _assistant(cmd: str | None):
    m = {"role": "assistant", "content": f"thinking about {cmd or 'nothing'}"}
    if cmd is not None:
        m["tool_calls"] = [{"function": {"arguments": json.dumps({"command": cmd})}}]
    return m


def _tool(text: str):
    return {"role": "tool", "content": text}


def _write_fixture(tmp_path: Path, task: str) -> Path:
    d = tmp_path / task
    d.mkdir(parents=True)
    # indices:      0        1
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    #  2,3   iter1
    msgs += [_assistant("cmd1"), _tool("obs one plain")]
    #  4,5   iter2  <- carries the delta
    msgs += [_assistant("cmd2"), _tool("obs two " + _DELTA)]
    #  6,7   iter3
    msgs += [_assistant("cmd3"), _tool("obs three plain")]
    #  8     INTERSTITIAL assistant with NO tool call (shifts later raw indices)
    msgs += [_assistant(None)]
    #  9,10  iter4
    msgs += [_assistant("cmd4"), _tool("obs four plain")]
    # 11,12  iter5  <- carries the SAME delta; true home is raw index 12, not 2*5+1=11
    msgs += [_assistant("cmd5"), _tool("obs five " + _DELTA)]

    traj = {"trajectory_format": "mini-swe-agent", "info": {}, "messages": msgs}
    (d / "mini-swe-agent.trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    def _row(it):
        return {"layer": "l3b.evidence", "event_type": "post_edit", "outcome": "delivered",
                "reason": "", "file_path": "", "iteration": it, "chars_delivered": _N,
                "content_sha256_16": _SHA16}
    # LEDGER ORDER: the drifting iter-5 row FIRST, so its full-scan runs while the
    # byte-identical iter-2 copy is still present -> current code mis-homes it.
    (d / f"gt_runtime_ledger_{task}.jsonl").write_text(
        json.dumps(_row(5)) + "\n" + json.dumps(_row(2)) + "\n", encoding="utf-8")
    return tmp_path


def test_duplicate_seal_homes_to_its_own_iteration(tmp_path):
    mod = _load()
    task = "loguru-dup"
    _write_fixture(tmp_path, task)
    recon = mod.reconstruct_task(task, tmp_path)
    homes = {dl.iteration: dl.home_msg for dl in recon.recorded_deliveries}
    # iteration 5's bytes live in the 5th tool message = raw index 12 (NOT 2*5+1=11)
    assert homes[5] == 12, f"iter-5 seal mis-homed to m{homes[5]} (drift + first-occurrence)"
    # iteration 2's bytes live in the 2nd tool message = raw index 5
    assert homes[2] == 5, f"iter-2 seal mis-homed to m{homes[2]}"
    # stripping still complete -> no residual leak
    assert recon.residual_leaks == [], recon.residual_leaks


def test_canonical_shape_unchanged(tmp_path):
    # no interstitial -> tool_msg_indices[it-1] must equal the old 2*it+1
    mod = _load()
    task = "canon"
    d = tmp_path / task
    d.mkdir(parents=True)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    msgs += [_assistant("c1"), _tool("one " + _DELTA)]     # iter1 -> raw index 3
    msgs += [_assistant("c2"), _tool("two plain")]         # iter2 -> raw index 5
    (d / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": msgs, "info": {}}), encoding="utf-8")
    (d / f"gt_runtime_ledger_{task}.jsonl").write_text(
        json.dumps({"layer": "l", "event_type": "e", "outcome": "delivered", "reason": "",
                    "file_path": "", "iteration": 1, "chars_delivered": _N,
                    "content_sha256_16": _SHA16}) + "\n", encoding="utf-8")
    recon = mod.reconstruct_task(task, tmp_path)
    assert recon.recorded_deliveries[0].home_msg == 3   # == 2*1+1 on canonical shape
    assert recon.residual_leaks == []

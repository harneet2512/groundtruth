"""EpisodeState — the canonical consolidated runtime state (W1a, strangler).

TTD: every test names the invariant it pins. EpisodeState is the ONE owner of the
per-run mutable state that was fragmented across GatewayState / trajectory_state /
state.py. These tests pin: field surface + defaults, EXACT to_dict/from_dict round
trip (sorted, no wallclock), reset_attempt semantics (which fields clear vs persist),
the delivered-dedup single chain, the probe-stem ledger shape, 2-process determinism,
and the read-view adapters (from_l5 / from_trajectory_state / from_state).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.ledger import Ledger


# --------------------------------------------------------------------------- #
# field surface + defaults
# --------------------------------------------------------------------------- #
def test_fresh_defaults_are_empty_and_deterministic():
    e = EpisodeState()
    assert e.action_index == 0
    assert e.step_limit is None
    assert e.phase == "" and e.band == ""
    assert e.edited_files == {}
    assert e.edited_tokens == set() and e.tested_tokens == set()
    assert e.edited_lines_by_file == {}
    assert e.test_count == 0 and e.test_evidence_seen is False
    assert e.nonedit_streak == 0
    assert e.failure_fingerprints == set() and e.last_failure_record == {}
    assert e.delivered_dedup == set()
    assert e.probe_ledger == {}
    assert e.edit_events == []
    assert e.horizon_latches == {}
    assert e.submit_bounce_count == 0
    assert e.hypothesis_slots == []
    assert isinstance(e.delivery_ledger, Ledger)   # opaque handle, canonical home
    assert e.obligations is None                    # opaque ref, not owned


def test_two_instances_have_independent_containers():
    """No shared mutable default — each EpisodeState owns its own stores."""
    a, b = EpisodeState(), EpisodeState()
    a.delivered_dedup.add("k")
    a.probe_ledger["s"] = {"probed_forms": set(), "probe_indices": [], "outcomes": []}
    a.edit_events.append({"index": 1, "blob": "x"})
    assert b.delivered_dedup == set() and b.probe_ledger == {} and b.edit_events == []
    assert a.delivery_ledger is not b.delivery_ledger


# --------------------------------------------------------------------------- #
# to_dict / from_dict — EXACT round trip, sorted, no wallclock
# --------------------------------------------------------------------------- #
def _populated() -> EpisodeState:
    e = EpisodeState(episode_id="task-42", action_index=7, step_limit=150,
                     phase="edit", band="mid")
    e.edited_files.update({"src/z.py": 5, "src/a.py": 3})
    e.edited_tokens.update({"foo", "bar"})
    e.tested_tokens.update({"baz"})
    e.edited_lines_by_file["src/a.py"] = [[3, 4], [7, 9]]
    e.test_count = 2
    e.test_evidence_seen = True
    e.nonedit_streak = 4
    e.failure_fingerprints.update({"h2", "h1"})
    e.last_failure_record = {"iter": 6, "kind": "assert"}
    e.delivered_dedup.update({"kb", "ka"})
    e.probe_ledger["getuser"] = {"probed_forms": {"getUser", "get_user"},
                                 "probe_indices": [3, 5], "outcomes": ["zero", "hit"]}
    e.edit_events.append({"index": 5, "blob": "def foo"})
    e.horizon_latches.update({"urgent_fired": True, "gate_count": 2})
    e.submit_bounce_count = 1
    return e


def test_to_dict_from_dict_exact_round_trip():
    e = _populated()
    d = e.to_dict()
    e2 = EpisodeState.from_dict(d)
    assert e2 == e                       # value equality (handles excluded from compare)
    assert e2.to_dict() == d             # exact dict round trip
    # probed_forms must come back as a SET (gateway does .add() on it)
    assert isinstance(e2.probe_ledger["getuser"]["probed_forms"], set)
    assert e2.edited_tokens == {"foo", "bar"}


def test_to_dict_is_sorted_and_wallclock_free():
    e = _populated()
    d = e.to_dict()
    # sets serialize to SORTED lists (deterministic)
    assert d["edited_tokens"] == ["bar", "foo"]
    assert d["failure_fingerprints"] == ["h1", "h2"]
    assert d["delivered_dedup"] == ["ka", "kb"]
    assert d["probe_ledger"]["getuser"]["probed_forms"] == ["getUser", "get_user"]
    # no timestamp / wallclock key anywhere in the serialized value state
    blob = json.dumps(d)
    assert "timestamp" not in blob and "time" not in blob
    # two calls byte-identical
    assert json.dumps(e.to_dict(), sort_keys=True) == json.dumps(e.to_dict(), sort_keys=True)


def test_to_dict_excludes_opaque_handles():
    e = _populated()
    d = e.to_dict()
    assert "delivery_ledger" not in d      # opaque handle, not value identity
    assert "obligations" not in d
    # from_dict yields a FRESH ledger handle (not reconstructed from a summary)
    assert isinstance(EpisodeState.from_dict(d).delivery_ledger, Ledger)


def test_tuple_rows_via_note_edited_lines_round_trip_exactly():
    """F1 (micro-bounce): W2 sources emit TUPLE line ranges
    (gt_mini_patch._edited_line_ranges). The supported ingress
    ``note_edited_lines`` normalizes rows to lists at entry, so the EXACT
    round-trip contract ``from_dict(to_dict(e)) == e`` stays TRUE even for
    tuple-shaped input rows."""
    e = EpisodeState()
    e.note_edited_lines("src/a.py", [(3, 4), (7, 9)])   # tuples, as W2 emits
    e.note_edited_lines("src/a.py", [(12, 12)])          # appends, not replaces
    e.note_edited_lines("src/b.py", [[1, 2]])            # lists pass through
    assert e.edited_lines_by_file["src/a.py"] == [[3, 4], [7, 9], [12, 12]]
    assert e.edited_lines_by_file["src/b.py"] == [[1, 2]]
    d = e.to_dict()
    e2 = EpisodeState.from_dict(d)
    assert e2 == e                       # the round-trip equality claim, now TRUE
    assert e2.to_dict() == d


def test_direct_tuple_assignment_round_trips_at_dict_level():
    """A tuple row assigned DIRECTLY (bypassing the supported ingress) is
    normalized in serialization: the dict-level round trip is exact even though
    object-level tuple identity is not preserved (that is why note_edited_lines
    is the documented ingress)."""
    e = EpisodeState()
    e.edited_lines_by_file["src/x.py"] = [(1, 2)]        # unsupported direct ingress
    d = e.to_dict()
    assert d["edited_lines_by_file"]["src/x.py"] == [[1, 2]]
    assert EpisodeState.from_dict(d).to_dict() == d      # to_dict-level round trip


# --------------------------------------------------------------------------- #
# reset_attempt — which fields clear (per-attempt accumulators) vs persist
# --------------------------------------------------------------------------- #
def test_reset_attempt_clears_owned_accumulators():
    e = _populated()
    e.reset_attempt()
    # cleared per-attempt accumulators (mirror _reset_oracle_state coverage)
    assert e.action_index == 0
    assert e.edited_files == {} and e.edited_tokens == set() and e.tested_tokens == set()
    assert e.edited_lines_by_file == {}
    assert e.test_count == 0 and e.test_evidence_seen is False and e.nonedit_streak == 0
    assert e.failure_fingerprints == set() and e.last_failure_record == {}
    assert e.probe_ledger == {}
    assert e.edit_events == []
    assert e.horizon_latches == {}
    assert e.submit_bounce_count == 0
    assert e.hypothesis_slots == []
    assert e.phase == "" and e.band == ""


def test_reset_attempt_persists_identity_and_ledger_handle():
    e = _populated()
    ledger_before = e.delivery_ledger
    e.reset_attempt()
    # persists across attempts: the BASE run identity + step budget + durable ledger.
    # D-11: reset_attempt advances the attempt counter and SUFFIXES the id so two
    # attempts get distinct chain-scoped identities; the base "task-42" is preserved.
    assert e.episode_id == "task-42#a1"
    assert e.attempt == 1
    assert e.step_limit == 150
    assert e.delivery_ledger is ledger_before   # durable handle survives the attempt
    # a second reset advances to #a2 (the prior suffix is stripped, never stacked)
    e.reset_attempt()
    assert e.episode_id == "task-42#a2" and e.attempt == 2


# ---- M2 MUTATION target: break reset -> leave the delivered chain uncleared ----
def test_m2_reset_attempt_clears_delivered_dedup_chain():
    """MUTATION M2 bites here: if reset_attempt fails to clear delivered_dedup, a
    stale dedup key from attempt-1 silently suppresses a legitimate re-delivery on
    attempt-2 (the exact _oracle_delivered_hashes.clear() semantic)."""
    e = EpisodeState()
    e.delivered_dedup.update({"a", "b", "c"})
    e.reset_attempt()
    assert e.delivered_dedup == set(), "reset MUST clear the delivered-dedup chain"


# --------------------------------------------------------------------------- #
# read-view adapters (read-only projections; never mutate the source store)
# --------------------------------------------------------------------------- #
def test_from_l5_read_view_projection():
    from groundtruth.state.agent_state import L5TrajectoryState
    l5 = L5TrajectoryState(instance_id="t9", current_iter=12, max_iter=100)
    l5.edited_source_files = ["src/a.py", "src/b.py"]
    l5.last_edit_iter = 11
    l5.verification_commands_run = 3
    l5.unresolved_failure_hashes = ["hh1", "hh2"]
    l5.failure_records = [{"iter": 8}, {"iter": 10, "last": True}]
    es = EpisodeState.from_l5(l5)
    assert es.episode_id == "t9"
    assert es.action_index == 12 and es.step_limit == 100
    assert set(es.edited_files) == {"src/a.py", "src/b.py"}
    assert es.test_count == 3 and es.test_evidence_seen is True
    assert es.failure_fingerprints == {"hh1", "hh2"}
    assert es.last_failure_record == {"iter": 10, "last": True}
    # read-only: the source L5 object is not mutated
    assert l5.current_iter == 12 and l5.edited_source_files == ["src/a.py", "src/b.py"]


def test_from_trajectory_state_read_view():
    from groundtruth.runtime.trajectory_state import derive_state, Turn
    ts = derive_state([
        Turn(command="sed -n '1,20p' src/app.py"),
        Turn(command="python -c \"open('src/app.py','w').write('def widget(): pass')\""),
        Turn(command="pytest -q", observation="1 passed"),
    ], step_limit=100)
    es = EpisodeState.from_trajectory_state(ts)
    assert es.action_index == ts.action_count
    assert es.step_limit == 100
    assert "src/app.py" in es.edited_files
    assert es.test_count == ts.test_count
    assert es.edited_tokens == ts.edited_tokens


def test_from_state_read_view():
    from groundtruth.runtime.state import AgentTrajectoryState
    ats = AgentTrajectoryState(action_count=9, max_iter=120)
    ats.record_edit("src/x.py", is_source=True)
    ats.record_test()
    es = EpisodeState.from_state(ats)
    assert es.action_index == 9 and es.step_limit == 120
    assert "src/x.py" in es.edited_files
    assert es.test_count == 1 and es.test_evidence_seen is True


# --------------------------------------------------------------------------- #
# DETERMINISM — 2-process byte-identity of a serialized EpisodeState after a
# scripted event sequence, under DIFFERENT PYTHONHASHSEED.
# --------------------------------------------------------------------------- #
def _repo_src() -> str:
    return str(Path(__file__).resolve().parents[2] / "src")


def test_determinism_two_processes(tmp_path):
    runner = tmp_path / "runner.py"
    runner.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {json.dumps(_repo_src())})\n"
        "from groundtruth.runtime.episode_state import EpisodeState\n"
        "e = EpisodeState(episode_id='x', step_limit=150)\n"
        "for i, tok in enumerate(['zoo','alpha','mid','beta','yak']):\n"
        "    e.action_index = i + 1\n"
        "    e.edited_tokens.add(tok)\n"
        "    e.edited_files[f'src/{tok}.py'] = i + 1\n"
        "    e.delivered_dedup.add(f'key-{tok}')\n"
        "    e.probe_ledger.setdefault('stem', {'probed_forms': set(),"
        " 'probe_indices': [], 'outcomes': []})['probed_forms'].add(tok)\n"
        "print(json.dumps(e.to_dict(), sort_keys=True))\n"
    )
    env0 = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
    env1 = {**os.environ, "PYTHONHASHSEED": "1", "PYTHONIOENCODING": "utf-8"}
    r0 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env0)
    r1 = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, env=env1)
    assert r0.returncode == 0, r0.stderr
    assert r1.returncode == 0, r1.stderr
    assert r0.stdout == r1.stdout
    assert json.loads(r0.stdout)["delivered_dedup"] == [
        "key-alpha", "key-beta", "key-mid", "key-yak", "key-zoo"]

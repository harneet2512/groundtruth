"""RED-first tests for direction_arrival — the DIRECTION-AND-ARRIVAL instrument.

GT is deterministic, so its contribution is a DIRECTION (a concrete target: file, symbol
contract, clause, pivot). The trajectory answers only "did the agent arrive, and how
fast". This suite pins the six behaviours that make that claim falsifiable:

  (a) AHEAD    — GT delivered the direction BEFORE the agent's state satisfied it.
  (b) BEHIND   — the agent was already there at-or-before the delivery.
  (c) NEVER    — the agent never arrived; censored at the trajectory end, never a number.
  (d) PAIRED   — the same arrival scan over a GT-OFF trajectory yields
                 arrival_delta = baseline_arrival - gton_arrival.
  (e) CENSORED — when either side never arrives the delta is None + a NAMED reason,
                 never a fabricated maximum.
  (f) DETERMINISM — identical inputs produce a byte-identical report.

BITING MUTATIONS (each applied to direction_arrival.py, observed RED, then reverted):
  M1 — ``_verdict`` returns AHEAD on ``delivery_step <= arrival_step`` (instead of ``<``):
       the BEHIND fixture, whose arrival lands on the delivery step itself, flips to
       AHEAD. ``test_behind_when_agent_was_already_there`` goes RED.
  M2 — ``adjudicate`` falls back to ``delta = max_step`` when a side is censored: the
       NEVER fixture yields a number instead of ``None`` and
       ``test_censoring_yields_none_and_a_named_reason`` goes RED.
  M3 — ``_scan_one`` for KIND_FILE also matches on the raw command text rather than
       ``_parse_timeline``'s viewed_file/edited_file: the NEVER fixture (whose agent
       greps the *name* of the ranked file but never opens it) reports an arrival and
       ``test_never_when_the_agent_does_not_arrive`` goes RED.
  M4 — ``_build_directions`` degrades a CONTRACT target to the row's file_path when the
       payload names no symbol: the skip-reason test loses ``no_target_extractable`` and
       ``test_unextractable_target_is_skipped_with_a_named_reason`` goes RED.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.swebench.direction_arrival import (  # noqa: E402
    AHEAD,
    BEHIND,
    DIRECTION_ARRIVAL_SCHEMA,
    KIND_CONTRACT,
    KIND_FILE,
    NEVER,
    adjudicate,
    arrival_scan,
    directions_from_ledger,
)


# --------------------------------------------------------------------------- #
# fixture builders (same message shapes the grader's parsers read)
# --------------------------------------------------------------------------- #
def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _assistant(command: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "bash", "arguments": json.dumps({"command": command})}}
        ],
    }


def _tool(content: str) -> dict:
    return {"role": "tool", "content": content}


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _delivered_row(payload: str, *, evidence_type: str, event_type: str,
                   file_path: str, iteration: int) -> dict:
    return {
        "layer": "gateway." + evidence_type,
        "evidence_type": evidence_type,
        "event_type": event_type,
        "file_path": file_path,
        "iteration": iteration,
        "outcome": "delivered",
        "reason": "delivery",
        "chars_delivered": len(payload),
        "content_sha256_16": _seal(payload),
    }


# A localization payload naming the ranked file; a caller-contract payload naming a symbol.
_PAYLOAD_LOC = "Ranked edit target: src/foo.py"
_PAYLOAD_CONTRACT = "callers: compute_widget\ndef compute_widget(x, y):"


def _write_task_dir(tmp_path: Path, trajectory: dict, rows: list[dict]) -> Path:
    task_dir = tmp_path / "ll-full-demo"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(trajectory), encoding="utf-8"
    )
    with (task_dir / "gt_runtime_ledger_demo.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return task_dir


# --------------------------------------------------------------------------- #
# (a) AHEAD — GT points at src/foo.py at step 1; the agent first opens it at step 2.
# --------------------------------------------------------------------------- #
def _ahead_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Fix the widget bug."),               # 0
        _assistant("grep -rn 'widget' src/"),       # 1  search (no arrival)
        _tool(_PAYLOAD_LOC),                        # 2  DELIVERY (step 1 observation)
        _assistant("cat src/foo.py"),               # 3  step 2 -> ARRIVAL (viewed)
        _tool("...file..."),                        # 4
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_LOC, evidence_type="trace_frame", event_type="search_result",
            file_path="src/foo.py", iteration=1,
        )
    ]
    return {"messages": messages}, rows


def test_ahead_when_gt_points_before_the_agent_arrives(tmp_path):
    trajectory, rows = _ahead_fixture()
    task_dir = _write_task_dir(tmp_path, trajectory, rows)
    report = adjudicate(str(task_dir))

    assert report["schema"] == DIRECTION_ARRIVAL_SCHEMA
    rows_out = [r for r in report["per_direction"] if r["fact_class"] == "localization"]
    assert len(rows_out) == 1, report["skipped_rows"]
    row = rows_out[0]
    assert row["kind"] == KIND_FILE
    assert row["target"] == "src/foo.py"
    assert row["delivery_step"] == 1
    assert row["arrival_step"] == 2
    assert row["arrival_evidence"] == "viewed"
    assert row["verdict"] == AHEAD
    assert report["summary"]["verdicts"][AHEAD] == 1


# --------------------------------------------------------------------------- #
# (b) BEHIND — the agent already opened src/foo.py at the step GT delivered into.
# --------------------------------------------------------------------------- #
def _behind_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Fix the widget bug."),               # 0
        _assistant("cat src/foo.py"),               # 1  step 1 -> the agent is ALREADY there
        _tool(_PAYLOAD_LOC),                        # 2  DELIVERY rides that same step-1 obs
        _assistant("apply_patch src/foo.py"),       # 3
        _tool("edit applied"),                      # 4
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_LOC, evidence_type="trace_frame", event_type="file_view",
            file_path="src/foo.py", iteration=1,
        )
    ]
    return {"messages": messages}, rows


def test_behind_when_agent_was_already_there(tmp_path):
    trajectory, rows = _behind_fixture()
    report = adjudicate(str(_write_task_dir(tmp_path, trajectory, rows)))
    row = next(r for r in report["per_direction"] if r["fact_class"] == "localization")
    assert row["delivery_step"] == 1
    assert row["arrival_step"] == 1
    assert row["verdict"] == BEHIND, "arrival <= delivery must NEVER read as AHEAD"
    assert report["summary"]["verdicts"][BEHIND] == 1
    assert report["summary"]["verdicts"][AHEAD] == 0


# --------------------------------------------------------------------------- #
# (c) NEVER — the agent greps the file's NAME but never opens or edits it.
# --------------------------------------------------------------------------- #
def _never_fixture() -> tuple[dict, list[dict]]:
    messages = [
        _user("Fix the widget bug."),               # 0
        _assistant("grep -rn widget src/"),         # 1
        _tool(_PAYLOAD_LOC),                        # 2  DELIVERY: go to src/foo.py
        # NAMES src/foo.py in a command but never opens or edits it — a history query is
        # not an arrival. This is the exact case a raw command-text matcher would misread.
        _assistant("git log --oneline -- src/foo.py"),   # 3
        _tool("a1b2c3 fix widget"),                 # 4
        _assistant("echo done"),                    # 5
        _tool("done"),                              # 6
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_LOC, evidence_type="trace_frame", event_type="search_result",
            file_path="src/foo.py", iteration=1,
        )
    ]
    return {"messages": messages}, rows


def test_never_when_the_agent_does_not_arrive(tmp_path):
    trajectory, rows = _never_fixture()
    report = adjudicate(str(_write_task_dir(tmp_path, trajectory, rows)))
    row = next(r for r in report["per_direction"] if r["fact_class"] == "localization")
    assert row["arrival_step"] is None, "no open/edit of the target = no arrival"
    assert row["verdict"] == NEVER
    assert row["arrival_delta"] is None
    assert report["summary"]["verdicts"][NEVER] == 1


# --------------------------------------------------------------------------- #
# (d) PAIRED — the SAME arrival scan on a GT-OFF trajectory for the SAME target.
# --------------------------------------------------------------------------- #
def _baseline_trajectory() -> dict:
    """GT-off: the agent wanders two extra steps before opening src/foo.py at step 4."""
    return {
        "messages": [
            _user("Fix the widget bug."),
            _assistant("grep -rn widget src/"),     # step 1
            _tool("many hits"),
            _assistant("ls src/"),                  # step 2
            _tool("bar.py foo.py"),
            _assistant("cat src/bar.py"),           # step 3
            _tool("...bar..."),
            _assistant("cat src/foo.py"),           # step 4 -> baseline arrival
            _tool("...foo..."),
        ]
    }


def test_paired_delta_against_a_baseline_trajectory(tmp_path):
    trajectory, rows = _ahead_fixture()
    task_dir = _write_task_dir(tmp_path, trajectory, rows)
    baseline_path = tmp_path / "baseline.trajectory.json"
    baseline_path.write_text(json.dumps(_baseline_trajectory()), encoding="utf-8")

    report = adjudicate(str(task_dir), str(baseline_path))
    row = next(r for r in report["per_direction"] if r["fact_class"] == "localization")
    assert row["arrival_step"] == 2
    assert row["baseline_arrival_step"] == 4
    assert row["arrival_delta"] == 2, "baseline_arrival - gton_arrival"
    assert row["arrival_delta_censor_reason"] is None

    summary = report["summary"]
    assert summary["n_measured_pairs"] == 1
    assert summary["median_arrival_delta"] == 2.0
    assert summary["n_censored"] == 0


# --------------------------------------------------------------------------- #
# (e) CENSORING — None + a NAMED reason on both sides, never a fabricated number.
# --------------------------------------------------------------------------- #
def test_censoring_yields_none_and_a_named_reason(tmp_path):
    # gton never arrives -> gton_never_arrived (even though the baseline DID arrive).
    trajectory, rows = _never_fixture()
    task_dir = _write_task_dir(tmp_path, trajectory, rows)
    baseline_path = tmp_path / "baseline.trajectory.json"
    baseline_path.write_text(json.dumps(_baseline_trajectory()), encoding="utf-8")

    report = adjudicate(str(task_dir), str(baseline_path))
    row = next(r for r in report["per_direction"] if r["fact_class"] == "localization")
    assert row["baseline_arrival_step"] == 4
    assert row["arrival_delta"] is None
    assert row["arrival_delta_censor_reason"] == "gton_never_arrived"
    assert report["summary"]["median_arrival_delta"] is None
    assert report["summary"]["n_measured_pairs"] == 0
    assert report["summary"]["censor_reasons"] == {"gton_never_arrived": 1}

    # the mirror case: gton arrives, the baseline never does.
    trajectory2, rows2 = _ahead_fixture()
    task_dir2 = _write_task_dir(tmp_path / "b", trajectory2, rows2)
    flat = tmp_path / "flat.trajectory.json"
    flat.write_text(
        json.dumps({"messages": [_user("go"), _assistant("echo hi"), _tool("hi")]}),
        encoding="utf-8",
    )
    report2 = adjudicate(str(task_dir2), str(flat))
    row2 = next(r for r in report2["per_direction"] if r["fact_class"] == "localization")
    assert row2["arrival_step"] == 2
    assert row2["baseline_arrival_step"] is None
    assert row2["arrival_delta"] is None
    assert row2["arrival_delta_censor_reason"] == "baseline_never_arrived"

    # and with no baseline at all, the reason is explicit rather than silent.
    report3 = adjudicate(str(task_dir2))
    row3 = next(r for r in report3["per_direction"] if r["fact_class"] == "localization")
    assert row3["arrival_delta"] is None
    assert row3["arrival_delta_censor_reason"] == "no_baseline_trajectory"


# --------------------------------------------------------------------------- #
# (f) DETERMINISM
# --------------------------------------------------------------------------- #
def test_determinism(tmp_path):
    trajectory, rows = _ahead_fixture()
    task_dir = _write_task_dir(tmp_path, trajectory, rows)
    baseline_path = tmp_path / "baseline.trajectory.json"
    baseline_path.write_text(json.dumps(_baseline_trajectory()), encoding="utf-8")

    first = json.dumps(adjudicate(str(task_dir), str(baseline_path)), sort_keys=True)
    second = json.dumps(adjudicate(str(task_dir), str(baseline_path)), sort_keys=True)
    assert first == second


# --------------------------------------------------------------------------- #
# fail-closed surfaces
# --------------------------------------------------------------------------- #
def test_no_delivered_rows_yields_empty_directions_with_a_reason(tmp_path):
    trajectory, _rows = _ahead_fixture()
    task_dir = _write_task_dir(
        tmp_path, trajectory,
        [{"layer": "gateway.trace_frame", "outcome": "suppressed", "chars_delivered": 0}],
    )
    report = adjudicate(str(task_dir))
    assert report["per_direction"] == []
    assert report["summary"]["directions"] == 0
    assert "no_delivered_rows" in report["summary"]["empty_reasons"]


def test_unregistered_fact_class_is_skipped_with_a_named_reason(tmp_path):
    trajectory, rows = _ahead_fixture()
    rows = rows + [
        _delivered_row(
            "some legacy scope map bytes", evidence_type="", event_type="post_view",
            file_path="src/foo.py", iteration=1,
        )
    ]
    rows[-1]["layer"] = "consensus.scope_map"  # a real, unregistered legacy layer
    report = adjudicate(str(_write_task_dir(tmp_path, trajectory, rows)))
    assert report["summary"]["skipped_reasons"].get("unregistered_fact_class") == 1
    assert all(s["reason"] for s in report["skipped_rows"]), "every skip must be named"


def test_unextractable_target_is_skipped_with_a_named_reason(tmp_path):
    """A caller_contract direction is a SYMBOL contract. When the delivered bytes name no
    symbol the direction cannot be formed at that granularity — it is skipped with a named
    reason, never silently degraded to the row's file_path."""
    payload = "callers: (none recorded)"
    messages = [
        _user("Fix it."),
        _assistant("cat src/foo.py"),
        _tool(payload),
        _assistant("echo done"),
        _tool("done"),
    ]
    rows = [
        _delivered_row(
            payload, evidence_type="caller_contract", event_type="file_view",
            file_path="src/foo.py", iteration=1,
        )
    ]
    report = adjudicate(str(_write_task_dir(tmp_path, {"messages": messages}, rows)))
    assert report["per_direction"] == []
    assert report["summary"]["skipped_reasons"].get("no_target_extractable") == 1


def test_contract_direction_targets_the_symbol_not_the_file(tmp_path):
    messages = [
        _user("Fix it."),
        _assistant("grep -rn compute src/"),          # step 1
        _tool(_PAYLOAD_CONTRACT),                     # DELIVERY at step 1
        _assistant("sed -i 's/x/y/' src/foo.py # compute_widget"),  # step 2 -> arrival
        _tool("ok"),
    ]
    rows = [
        _delivered_row(
            _PAYLOAD_CONTRACT, evidence_type="caller_contract",
            event_type="search_result", file_path="src/foo.py", iteration=1,
        )
    ]
    report = adjudicate(str(_write_task_dir(tmp_path, {"messages": messages}, rows)))
    row = next(r for r in report["per_direction"] if r["fact_class"] == "caller_contract")
    assert row["kind"] == KIND_CONTRACT
    assert row["target"] == "compute_widget"
    assert row["arrival_evidence"] == "named_in_command"
    assert row["verdict"] == AHEAD


def test_directions_from_ledger_without_a_trajectory_has_no_delivery_step(tmp_path):
    """The pure-module surface: with no trajectory only ledger file subjects are
    recoverable and delivery_step is honestly None — never guessed from ``iteration``."""
    _trajectory, rows = _ahead_fixture()
    directions = directions_from_ledger(rows)
    assert [d.target for d in directions] == ["src/foo.py"]
    assert directions[0].delivery_step is None
    assert directions[0].fact_class == "localization"


def test_arrival_scan_is_pure_and_trajectory_side(tmp_path):
    """arrival_scan never consults the delivery: the SAME directions scanned over the
    GT-OFF trajectory give the baseline arrival (this is what makes pairing possible)."""
    trajectory, rows = _ahead_fixture()
    directions = directions_from_ledger(rows, trajectory=trajectory)
    scan = arrival_scan(_baseline_trajectory(), directions)
    assert len(scan) == 1
    assert next(iter(scan.values()))["arrival_step"] == 4

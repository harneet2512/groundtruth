"""F17: dispatch-layer skip rows.

When a producer's trigger event fires but a gate keeps it out, the audit must
distinguish that from both "entered + abstained" and "event never matched".
These tests drive produce_raw with the gates OFF and assert the
``producer.dispatch`` / ``not_entered`` row carries the right skip reason.
"""

from groundtruth.runtime.gateway import (
    GatewayState,
    ToolEvent,
    produce_raw,
)


def _skip_rows(recorder, producer):
    return [
        r
        for r in recorder
        if r.get("layer") == "producer.dispatch"
        and r.get("outcome") == "not_entered"
        and r.get("producer") == producer
    ]


def test_patch_delta_kill_switch_records_not_entered(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "0")  # default is ON; literal 0 disables
    rows = []
    state = GatewayState(
        repo_root=str(tmp_path),
        graph_db=str(tmp_path / "graph.db"),
        producer_recorder=rows.append,
    )
    event = ToolEvent(
        kind="edit",
        command="apply_patch",
        semantic_events=("edit_result",),
        semantics_authoritative=True,
        action_index=7,
    )
    produce_raw(event, state)
    skips = _skip_rows(rows, "patch_delta")
    assert len(skips) == 1
    assert skips[0]["skip_reason"] == "kill_switch_off"
    assert skips[0]["invocation_site"] == "gateway.edit.patch_delta"
    assert skips[0]["action_index"] == 7
    assert sorted(skips[0]["evidence_types"]) == ["patch_delta", "signature_delta"]


def test_change_surface_edit_skip_reason_is_specific(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")  # producer on…
    monkeypatch.delenv("GT_CS_EDIT_TRIGGER", raising=False)  # …edit trigger off
    rows = []
    state = GatewayState(
        repo_root=str(tmp_path),
        graph_db=str(tmp_path / "graph.db"),
        producer_recorder=rows.append,
    )
    event = ToolEvent(
        kind="edit",
        command="apply_patch",
        semantic_events=("edit_result",),
        semantics_authoritative=True,
    )
    produce_raw(event, state)
    skips = _skip_rows(rows, "change_surface")
    assert len(skips) == 1
    assert skips[0]["skip_reason"] == "cs_edit_trigger_off"


def test_ranked_localization_flag_off_records_not_entered(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_LOC_RESLOT", raising=False)
    rows = []
    state = GatewayState(
        repo_root=str(tmp_path),
        graph_db=str(tmp_path / "graph.db"),
        producer_recorder=rows.append,
    )
    event = ToolEvent(
        kind="search",
        command="grep -rn foo",
        semantic_events=("search_result",),
        semantics_authoritative=True,
    )
    produce_raw(event, state)
    skips = _skip_rows(rows, "ranked_localization")
    assert len(skips) == 1
    assert skips[0]["skip_reason"] == "loc_reslot_off"


def test_no_skip_rows_when_gates_open(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    rows = []
    state = GatewayState(
        repo_root=str(tmp_path),
        graph_db=str(tmp_path / "graph.db"),
        producer_recorder=rows.append,
    )
    event = ToolEvent(
        kind="edit",
        command="apply_patch",
        semantic_events=("edit_result",),
        semantics_authoritative=True,
    )
    produce_raw(event, state)
    assert _skip_rows(rows, "patch_delta") == []

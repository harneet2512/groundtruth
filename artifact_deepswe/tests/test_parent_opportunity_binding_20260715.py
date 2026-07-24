"""RED contract for host-only policy-parent and feature-opportunity binding.

The instrumentation described here is audit metadata only.  It must never alter
the observation returned by ``format_observation_messages`` and must never copy
model-authored prose into the runtime ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

import gt_mini_patch as g
from groundtruth.runtime.evidence_envelope import observation_candidate_id
from groundtruth.runtime.feature_lineage import build_lineage


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _action_batch_sha256(actions: list[dict]) -> str:
    """Full-width counterpart of the seam's existing ``_batch_identity``."""
    keys = tuple(
        json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
        for action in actions
    )
    raw = json.dumps(keys, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(raw)


def _observation_id(
    *, start_iteration: int, parent_sha256: str, action_sha256: str,
) -> str:
    """Normative, unambiguous v1 framing for one policy observation."""
    framed = (
        b"gt.observation.v1\x00"
        + int(start_iteration).to_bytes(8, "big", signed=False)
        + bytes.fromhex(parent_sha256)
        + bytes.fromhex(action_sha256)
    )
    return hashlib.sha256(framed).hexdigest()


def _opportunity_id(
    *, observation_id: str, ordinal: int, kind: str, dedup_key: str,
) -> str:
    framed = (
        b"gt.opportunity.v1\x00"
        + bytes.fromhex(observation_id)
        + int(ordinal).to_bytes(8, "big", signed=False)
        + bytes.fromhex(_sha256_text(kind))
        + bytes.fromhex(_sha256_text(dedup_key))
    )
    return hashlib.sha256(framed).hexdigest()


class _Model:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises

    def format_observation_messages(self, message, outputs, template_vars=None):
        if self.raises:
            raise RuntimeError("format failed")
        return [{"role": "tool", "content": output["output"]} for output in outputs]


class _Environment:
    def execute(self, action):
        output = {"output": "base:" + action["command"], "returncode": 0}
        g._augment_output(action, output)
        return output


class _Agent:
    def __init__(self, model: _Model | None = None) -> None:
        self.model = model or _Model()
        self.env = _Environment()

    def execute_actions(self, message):
        outputs = [
            self.env.execute(action) for action in message["extra"]["actions"]
        ]
        return self.model.format_observation_messages(message, outputs, {})


def _wire_lineage_free_candidates(
    monkeypatch, ledger_rows: list[dict], *, metrics_on: bool = True,
) -> None:
    monkeypatch.setenv("GT_INSEAM_METRICS", "1" if metrics_on else "0")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_action_count", 0)
    monkeypatch.setattr(g, "_global_arbiter_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: ledger_rows.append(dict(row)))
    monkeypatch.setattr(g, "_ledger_judge_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(g, "_ss_scan_acks", lambda *args, **kwargs: None)
    monkeypatch.setattr(g, "_gt_gateway_caller_contract_ready", lambda *args, **kwargs: False)
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda text: False)
    monkeypatch.setattr(g, "_ss_content_decision", lambda *args, **kwargs: (False, ""))
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_novelty_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: False)
    monkeypatch.setattr(g, "_lane_envelope_on", lambda: False)
    monkeypatch.setattr(g, "_ss_shadow_would_withhold", lambda *args, **kwargs: False)

    def gateway(action, output, command, original_output, *, pool=None, lattice_produced=None):  # lattice_produced: match prod signature (2026-07-22)
        candidate = SimpleNamespace(
            kind="unknown." + command,
            plane="fact",
            dedup_key="candidate:" + command,
            symbol="",
            lineage=None,
        )
        def thunk():
            shipped = "\nGT:" + command
            output["output"] = output["output"] + shipped
            g._runtime_ledger_record(
                kind="gateway." + command,
                outcome=g._ProductSignalOutcome.DELIVERED,
                chars=len(shipped),
                content=shipped,
                extra={"candidate_id": candidate.dedup_key},
            )
        g._append_batch_candidate(
            pool, candidate, thunk, output, "GT:" + command, join=True
        )

    def plan(pool):
        winner = pool[-1][0]
        losers = [(candidate, "outranked") for candidate, _thunk in pool[:-1]]
        return SimpleNamespace(repair_support=False, losers=losers), winner

    monkeypatch.setattr(g, "_gt_gateway_deliver", gateway)
    monkeypatch.setattr(g, "_global_pool_plan", plan)


def test_parallel_candidates_bind_exact_parent_without_persisting_prose(monkeypatch):
    rows: list[dict] = []
    _wire_lineage_free_candidates(monkeypatch, rows)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)

    parent = (
        "Now I need to rebuild the extension. LEAK_CANARY_TEST_NAME must never "
        "be copied to audit metadata."
    )
    actions = [{"command": "inspect-build"}, {"command": "run-build"}]
    message = {"role": "assistant", "content": parent, "extra": {"actions": actions}}
    rendered = agent.execute_actions(message)

    assert ["GT:" in item["content"] for item in rendered] == [False, True]
    opportunities = [row for row in rows if row.get("layer") == "feature.opportunity"]
    assert len(opportunities) == 2

    expected_parent = _sha256_text(parent)
    expected_actions = _action_batch_sha256(actions)
    expected_observation = _observation_id(
        start_iteration=0,
        parent_sha256=expected_parent,
        action_sha256=expected_actions,
    )
    assert {row["parent_policy_sha256"] for row in opportunities} == {expected_parent}
    assert {row["parent_policy_chars"] for row in opportunities} == {len(parent)}
    assert {row["action_batch_sha256"] for row in opportunities} == {expected_actions}
    assert {row["observation_id"] for row in opportunities} == {expected_observation}
    expected_opportunities = [
        _opportunity_id(
            observation_id=expected_observation,
            ordinal=index,
            kind="unknown." + action["command"],
            dedup_key="candidate:" + action["command"],
        )
        for index, action in enumerate(actions)
    ]
    assert [row["opportunity_id"] for row in opportunities] == expected_opportunities
    assert all(_HEX64.fullmatch(value) for value in expected_opportunities)
    assert [row["candidate_ordinal"] for row in opportunities] == [0, 1]

    # Unknown ownership is an explicit hole.  A layer name or enabled GT flag may
    # never manufacture a canonical feature attribution.
    assert all(row["feature_refs"] == [] for row in opportunities)
    assert all(row["attribution_status"] == "UNMEASURED" for row in opportunities)
    assert all(row["attribution_reason"] == "typed_lineage_absent" for row in opportunities)

    serialized = json.dumps(opportunities, sort_keys=True)
    assert parent not in serialized
    assert "LEAK_CANARY_TEST_NAME" not in serialized
    assert "inspect-build" not in serialized
    assert "run-build" not in serialized


def test_each_delivered_fire_carries_its_exact_observation_and_opportunity(monkeypatch):
    rows: list[dict] = []
    _wire_lineage_free_candidates(monkeypatch, rows)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)

    messages = [
        {
            "role": "assistant",
            "content": "first parent",
            "extra": {"actions": [
                {"command": "first-a"}, {"command": "first-b"},
            ]},
        },
        {
            "role": "assistant",
            "content": "second parent",
            "extra": {"actions": [{"command": "second-a"}]},
        },
    ]
    for message in messages:
        agent.execute_actions(message)

    opportunities = [row for row in rows if row.get("layer") == "feature.opportunity"]
    deliveries = [
        row for row in rows
        if row.get("outcome") == "delivered" and row.get("chars_delivered", 0) > 0
    ]
    assert len(opportunities) == 3
    assert len(deliveries) == 2
    selected = {row["opportunity_id"]: row for row in opportunities if row["selected"]}
    assert len(selected) == 2
    for delivery in deliveries:
        binding = delivery.get("observation_binding")
        assert isinstance(binding, dict)
        origin = selected[binding["opportunity_id"]]
        assert binding["observation_id"] == origin["observation_id"]
        assert binding["candidate_id"] == delivery["candidate_id"]
        assert binding["candidate_ordinal"] == origin["candidate_ordinal"]


def test_inseam_metrics_flag_changes_no_model_visible_byte(monkeypatch):
    message = {
        "role": "assistant",
        "content": "I need to decide whether verification is necessary.",
        "extra": {"actions": [
            {"command": "inspect-build"}, {"command": "run-build"},
        ]},
    }

    def run(metrics_on: bool):
        with monkeypatch.context() as isolated:
            rows: list[dict] = []
            _wire_lineage_free_candidates(
                isolated, rows, metrics_on=metrics_on)
            agent = _Agent()
            assert g.install_observation_batch_commit(agent)
            rendered = agent.execute_actions(message)
            return json.dumps(rendered, sort_keys=True).encode(), rows

    off_bytes, off_rows = run(False)
    on_bytes, on_rows = run(True)

    assert on_bytes == off_bytes
    assert not [row for row in off_rows if row.get("layer") == "feature.opportunity"]
    assert len([
        row for row in on_rows if row.get("layer") == "feature.opportunity"
    ]) == 2


def test_opportunity_instrumentation_fault_cannot_change_visible_bytes(monkeypatch):
    rows: list[dict] = []
    _wire_lineage_free_candidates(monkeypatch, rows)
    monkeypatch.setattr(
        g,
        "_record_batch_opportunities",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit fault")),
    )
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions({
        "role": "assistant",
        "content": "parent",
        "extra": {"actions": [{"command": "one"}]},
    })
    assert rendered == [{"role": "tool", "content": "base:one\nGT:one"}]


def test_registered_typed_lineage_persists_exact_feature_refs(monkeypatch):
    lineage = build_lineage(
        runtime_producer_id="change_surface",
        evidence_type="new_file_destination",
        actual_event="post_edit",
        cap_feature_ids=("GT_CHANGE_SURFACE",),
    )
    assert lineage is not None and lineage.producer_registration_match is True
    candidate = SimpleNamespace(
        kind="gateway.new_file_destination",
        plane="gateway",
        dedup_key="typed",
        lineage=lineage,
    )
    rows: list[dict] = []
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    state = {
        "observation_id": "1" * 64,
        "parent_policy_sha256": "2" * 64,
        "parent_policy_chars": 7,
        "action_batch_sha256": "3" * 64,
        "batch_start_iteration": 4,
        "pool": [(candidate, lambda: None)],
    }
    plans = {id(candidate): SimpleNamespace(disposition="deliver")}

    g._record_batch_opportunities(state, plans, candidate)

    assert rows[0]["feature_refs"] == [
        {"category": "CAP", "feature_id": "GT_CHANGE_SURFACE", "role": "byte_owner"},
        {"category": "FACT", "feature_id": "newfile_precedent", "role": "fact"},
    ]
    assert rows[0]["attribution_status"] == "BOUND"
    assert rows[0]["attribution_reason"] == "typed_lineage"


def test_eligibility_ruling_binds_same_typed_candidate_before_delivery(monkeypatch):
    rows: list[dict] = []
    original_content_decision = g._ss_content_decision
    _wire_lineage_free_candidates(monkeypatch, rows)
    monkeypatch.setattr(g, "_ss_content_decision", original_content_decision)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: True)
    monkeypatch.setattr(g, "_ss_dedup2_suppresses", lambda *args, **kwargs: False)
    monkeypatch.setattr(g, "_ss_novelty_on", lambda: False)

    lineage = build_lineage(
        runtime_producer_id="change_surface",
        evidence_type="new_file_destination",
        actual_event="post_edit",
        cap_feature_ids=("GT_CHANGE_SURFACE",),
    )
    assert lineage is not None and lineage.producer_registration_match is True

    def gateway(action, output, command, original_output, *, pool=None, lattice_produced=None):  # lattice_produced: match prod signature (2026-07-22)
        candidate = SimpleNamespace(
            kind="gateway.new_file_destination",
            plane="gateway",
            dedup_key="typed-candidate",
            symbol="src/new.py",
            lineage=lineage,
        )

        def thunk():
            shipped = "\nGT:" + command
            output["output"] = output["output"] + shipped
            extra = g._feature_lineage_extra(lineage)
            extra["candidate_id"] = candidate.dedup_key
            g._runtime_ledger_record(
                kind="gateway.new_file_destination",
                outcome=g._ProductSignalOutcome.DELIVERED,
                chars=len(shipped),
                content=shipped,
                extra=extra,
            )

        g._append_batch_candidate(
            pool, candidate, thunk, output, "GT:" + command, join=True,
        )

    monkeypatch.setattr(g, "_gt_gateway_deliver", gateway)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions({
        "role": "assistant",
        "content": "inspect the new-file destination",
        "extra": {"actions": [{"command": "inspect-build"}]},
    })
    assert rendered == [{"role": "tool", "content": "base:inspect-build\nGT:inspect-build"}]

    ruling = next(
        row for row in rows
        if row.get("layer") == "control.participation"
        and row.get("control_ref", {}).get("feature_id") == "GT_SS_DEDUP2"
    )
    delivery = next(
        row for row in rows
        if row.get("outcome") == "delivered"
        and row.get("fact_class") == "newfile_precedent"
    )
    canonical_candidate_id = observation_candidate_id("typed-candidate")
    opportunity = next(
        row for row in rows
        if row.get("layer") == "feature.opportunity"
        and row.get("candidate_id") == canonical_candidate_id
    )

    assert ruling["participation_decision"] == "NO_EFFECT"
    assert ruling["fact_class"] == "newfile_precedent"
    assert ruling["candidate_id"] == canonical_candidate_id
    assert ruling["observation_binding"] == delivery["observation_binding"]
    assert ruling["observation_binding"] == opportunity["observation_binding"]
    assert ruling["candidate_sha256_16"] == delivery["content_sha256_16"]
    assert ruling["candidate_chars"] == delivery["chars_delivered"]


def test_typed_lineage_producer_mismatch_fails_closed(monkeypatch):
    lineage = build_lineage(
        runtime_producer_id="wrong_producer",
        evidence_type="new_file_destination",
        actual_event="post_edit",
    )
    assert lineage is not None and lineage.producer_registration_match is False
    candidate = SimpleNamespace(
        kind="gateway.new_file_destination",
        plane="gateway",
        dedup_key="mismatch",
        lineage=lineage,
    )
    rows: list[dict] = []
    monkeypatch.setattr(g, "_inseam_metrics_on", lambda: True)
    monkeypatch.setattr(g, "_ledger_line_direct", lambda row: rows.append(dict(row)))
    state = {
        "observation_id": "4" * 64,
        "parent_policy_sha256": "5" * 64,
        "parent_policy_chars": 9,
        "action_batch_sha256": "6" * 64,
        "batch_start_iteration": 10,
        "pool": [(candidate, lambda: None)],
    }
    plans = {id(candidate): SimpleNamespace(disposition="deliver")}

    g._record_batch_opportunities(state, plans, candidate)

    assert rows[0]["feature_refs"] == []
    assert rows[0]["attribution_status"] == "UNMEASURED"
    assert rows[0]["attribution_reason"] == "producer_registration_mismatch"


def test_formatter_failure_creates_no_observed_eligible_opportunity(monkeypatch):
    rows: list[dict] = []
    _wire_lineage_free_candidates(monkeypatch, rows)
    agent = _Agent(_Model(raises=True))
    assert g.install_observation_batch_commit(agent)

    message = {
        "role": "assistant",
        "content": "I might verify later.",
        "extra": {"actions": [{"command": "inspect-build"}]},
    }
    with pytest.raises(RuntimeError, match="format failed"):
        agent.execute_actions(message)

    assert not [
        row for row in rows
        if row.get("layer") == "feature.opportunity"
        and row.get("outcome") == "eligible"
    ]


def test_policy_observation_id_matches_shared_authority():
    """The seam's local ``_policy_observation_id`` (its in-container degradation copy)
    must be BYTE-IDENTICAL to the shared ``evidence_envelope.policy_observation_id`` over
    many inputs — the two framed-identity implementations can never drift (LIPI, the
    reader/writer-drift class this cluster closes)."""
    from groundtruth.runtime.evidence_envelope import policy_observation_id as shared

    cases = [
        (0, _sha256_text("parent-a"), _sha256_text("[]")),
        (1, _sha256_text("parent-b"), _sha256_text('[{"command":"x"}]')),
        (18, _sha256_text(""), _sha256_text('["noop"]')),
        (65535, _sha256_text("a" * 4000), _sha256_text("b" * 4000)),
        (2**40, _sha256_text("café ☃"), _sha256_text("Δ")),
    ]
    for start_iteration, parent_sha, action_sha in cases:
        assert g._policy_observation_id(start_iteration, parent_sha, action_sha) == shared(
            start_iteration, parent_sha, action_sha
        )


def test_join_suffix_nonempty_for_nonempty_text():
    """Invariant relied on by the shadow-gate reorder (2026-07-18): _join_lane_output
    NEVER dedups — it only manages the newline boundary — so the shipped suffix
    ``_join_lane_output(base, text)[len(base):]`` is non-empty iff ``text`` is non-empty.
    Therefore passing the JOINED suffix to _ss_shadow_would_withhold has an equivalent
    empty-gate to the pre-reorder text argument, and assignment outcomes are byte-for-byte
    unchanged (assignment hashes task/kind/dedup_key only, never text)."""
    newline = chr(10)
    bases = ["", "base", "base" + newline, "obs line", "a" + newline + "b" + newline,
             "x" * 4000]
    texts = ["GT:hint", newline + "GT:hint", "already" + newline + "present", "x" * 999]
    for base in bases:
        for text in texts:
            joined = g._join_lane_output(base, text)
            suffix = joined[len(base):]
            assert suffix != ""
            assert suffix in (newline + text, text)
    for base in bases:
        assert g._join_lane_output(base, "")[len(base):] == ""

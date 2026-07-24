"""One GT arbitration commit per complete mini policy observation."""

from types import SimpleNamespace

import pytest

import gt_mini_patch as g


class _Model:
    def __init__(self, *, raises=False):
        self.raises = raises

    def format_observation_messages(self, message, outputs, template_vars=None):
        self.last_outputs = outputs
        if self.raises:
            raise RuntimeError("format failed")
        return [{"role": "tool", "content": row["output"]} for row in outputs]


class _Env:
    def execute(self, action):
        out = {"output": "base:" + action["command"], "returncode": 0}
        self.last_out = out
        g._augment_output(action, out)
        return out


class _Agent:
    def __init__(self, model=None):
        self.model = model or _Model()
        self.env = _Env()

    def execute_actions(self, message):
        actions = message["extra"]["actions"]
        outputs = [self.env.execute(action) for action in actions]
        return self.model.format_observation_messages(message, outputs, {})


def _wire_fake_candidates(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_global_arbiter_on", lambda: True)
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kwargs: None)
    monkeypatch.setattr(g, "_ledger_judge_pending", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ss_scan_acks", lambda *a, **k: None)
    monkeypatch.setattr(g, "_gt_gateway_caller_contract_ready", lambda *a, **k: False)

    def gateway(action, out, cmd, orig_out, *, pool=None, lattice_produced=None):  # lattice_produced: match prod signature (2026-07-22)
        candidate = SimpleNamespace(kind="fake." + cmd, plane="fact")
        thunk = lambda: out.__setitem__("output", out["output"] + "\nGT:" + cmd)
        if pool is None:
            thunk()
        else:
            g._append_batch_candidate(
                pool, candidate, thunk, out, "GT:" + cmd, join=True)

    monkeypatch.setattr(g, "_gt_gateway_deliver", gateway)


def test_real_augment_to_formatter_mixed_actions_one_flush(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    flushes = []

    contexts = []

    original_commit = g._commit_batch_arbitration
    def commit(state, result, winner, plans):
        flushes.append(len(plans))
        fake_ids = {key for key, plan in plans.items()
                    if plan.candidate.kind.startswith("fake.")}
        contexts.append({key: value for key, value in state["contexts"].items()
                         if key in fake_ids})
        return original_commit(state, result, winner, plans)

    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (
            SimpleNamespace(repair_support=False, losers=[]),
            next(row[0] for row in pool if row[0].kind == "fake.two")))
    monkeypatch.setattr(g, "_commit_batch_arbitration", commit)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    message = {"extra": {"actions": [
        {"command": "one", "tool_call_id": "a"},
        {"command": "two"},
        {"command": "three", "tool_call_id": "c"},
    ]}}

    rendered = agent.execute_actions(message)

    assert len(flushes) == 1 and flushes[0] >= 3
    frozen = sorted(contexts[0].values(), key=lambda row: row["action_index"])
    assert [row["action_index"] for row in frozen] == [0, 1, 2]
    assert len({row["producer_iteration"] for row in frozen}) == 3
    assert ["GT:" in row["content"] for row in rendered] == [False, True, False]
    assert g._batch_context.get() is None


def test_install_failure_fails_closed_instead_of_per_action_fallback(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    flushes = []
    monkeypatch.setattr(
        g, "_global_pool_flush",
        lambda pool, **kwargs: (flushes.append(len(pool)), pool[0][1]())[1])
    agent = _Agent()
    agent.model.format_observation_messages = None

    assert not g.install_observation_batch_commit(agent)
    outputs = [agent.env.execute({"command": value}) for value in ("one", "two")]
    assert flushes == []
    assert all("GT:" not in out["output"] for out in outputs)
    assert g._batch_context.get() is None


def test_install_failure_discards_preexisting_pending_state(monkeypatch):
    rolled_back = []
    owner = SimpleNamespace()
    state = g._begin_observation_batch(owner, SimpleNamespace(), [{"command": "one"}])
    candidate = SimpleNamespace(kind="pending", plane="fact")
    state["pool"].append((candidate, lambda: None))
    state["rollbacks"][id(candidate)] = lambda: rolled_back.append("pending")
    agent = _Agent()
    agent.model.format_observation_messages = None

    assert not g.install_observation_batch_commit(agent)
    assert rolled_back == ["pending"]
    assert g._batch_context.get() is None


def test_formatter_exception_never_commits_output_or_dedup(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    before = set(g._EPISODE.delivered_dedup)

    commits = []
    def flush(pool, **kwargs):
        commits.append(True)
        pool[0][1]()
        g._EPISODE.delivered_dedup.add("transient-delivery")
        return pool[0][0]

    monkeypatch.setattr(g, "_global_pool_flush", flush)
    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (SimpleNamespace(repair_support=False), pool[0][0]))
    agent = _Agent(_Model(raises=True))
    assert g.install_observation_batch_commit(agent)

    with pytest.raises(RuntimeError, match="format failed"):
        agent.execute_actions({"extra": {"actions": [{"command": "one"}]}})

    assert agent.model.last_outputs[0]["output"] == "base:one\nGT:one"
    assert commits == []
    assert set(g._EPISODE.delivered_dedup) == before
    assert g._batch_context.get() is None


def test_identity_mismatch_discards_without_delivery(monkeypatch):
    _wire_fake_candidates(monkeypatch)

    class _MismatchAgent(_Agent):
        def execute_actions(self, message):
            outputs = [self.env.execute(action) for action in message["extra"]["actions"]]
            changed = {"extra": {"actions": [{"command": "different"}]}}
            return self.model.format_observation_messages(changed, outputs, {})

    agent = _MismatchAgent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions(
        {"extra": {"actions": [{"command": "original"}]}})
    assert rendered[0]["content"] == "base:original"
    assert g._batch_context.get() is None


def test_attempt_reset_discards_pending_batch(monkeypatch):
    _wire_fake_candidates(monkeypatch)

    class _ResetAgent(_Agent):
        def execute_actions(self, message):
            outputs = [self.env.execute(action) for action in message["extra"]["actions"]]
            g._reset_oracle_state()
            return self.model.format_observation_messages(message, outputs, {})

    agent = _ResetAgent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions({"extra": {"actions": [{"command": "one"}]}})
    assert rendered[0]["content"] == "base:one"
    assert g._batch_context.get() is None


def test_interleaved_handshake_fails_closed_without_reusing_first_batch():
    first = g._begin_observation_batch(
        SimpleNamespace(), SimpleNamespace(), [{"command": "one"}])
    second = g._begin_observation_batch(
        SimpleNamespace(), SimpleNamespace(), [{"command": "two"}])
    assert first is not None and second is not None
    assert first["invalid"] and second["invalid"]
    g._discard_tool_observation_batch("test_overlap", second)
    assert g._batch_context.get() is first
    g._discard_tool_observation_batch("test_overlap", first)
    assert g._batch_context.get() is None


def test_atomic_append_removes_unowned_candidate_on_attachment_fault(monkeypatch):
    pool = []
    candidate = SimpleNamespace(kind="candidate")
    rollbacks = []
    monkeypatch.setattr(
        g, "_attach_batch_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("attach")))

    with pytest.raises(RuntimeError, match="attach"):
        g._append_batch_candidate(
            pool, candidate, lambda: None, {"output": "base"}, "GT", join=True,
            rollback=lambda: rollbacks.append("once"))

    assert pool == []
    assert rollbacks == ["once"]


def test_postformat_commit_fault_preserves_validated_visible_dose(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (SimpleNamespace(repair_support=False, losers=[]), pool[0][0]))
    monkeypatch.setattr(
        g, "_commit_batch_arbitration",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("commit")))
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)

    rendered = agent.execute_actions(
        {"extra": {"actions": [{"command": "one"}]}})

    assert rendered[0]["content"] == "base:one\nGT:one"
    assert g._batch_context.get() is None


def test_overlapping_agent_executions_discard_both_complete_observations(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (SimpleNamespace(repair_support=False), pool[0][0]))
    second = _Agent()
    assert g.install_observation_batch_commit(second)

    class _OverlapModel(_Model):
        def format_observation_messages(self, message, outputs, template_vars=None):
            if not hasattr(self, "second_rendered"):
                self.second_rendered = second.execute_actions(
                    {"extra": {"actions": [{"command": "second"}]}})
            return super().format_observation_messages(message, outputs, template_vars)

    first = _Agent(_OverlapModel())
    assert g.install_observation_batch_commit(first)
    first_rendered = first.execute_actions(
        {"extra": {"actions": [{"command": "first"}]}})

    assert first_rendered[0]["content"] == "base:first"
    assert first.model.second_rendered[0]["content"] == "base:second"
    assert g._batch_context.get() is None


def _pure_plan(monkeypatch, *, plane, text="payload", kind="fact"):
    monkeypatch.setattr(g, "_ss_content_decision", lambda *a, **k: (False, ""))
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_novelty_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: False)
    candidate = SimpleNamespace(
        kind=kind, plane=plane, dedup_key="unified", suppressible_if_acquired=False)
    return g._prepare_batch_delivery(
        candidate, lambda: None,
        {"out": {"output": "base"}, "payload": text, "join": True,
         "winner_hash": "legacy"})


def test_finalizer_suppresses_leak_before_render(monkeypatch):
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda text: True)
    plan = _pure_plan(monkeypatch, plane=g._GA_PLANE_LANE_A)
    assert plan.disposition == "suppressed" and plan.suffix == ""


def test_search_metadata_reaches_finalizer_and_sibling_acquisition_suppresses(
        monkeypatch):
    subject = "src/widget.py"
    payload = "src/widget.py:10:class Widget"
    action = {"command": "rg Widget src"}
    out = {"output": "base", "returncode": 0}

    monkeypatch.setattr(g, "_ss_novelty_on", lambda: True)
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: False)
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda text: False)
    monkeypatch.setattr(g, "_ss_payload_has_content", lambda text: True)
    state = g._begin_observation_batch(
        SimpleNamespace(), SimpleNamespace(), [action])
    try:
        assert g._register_batch_execute(state, action, out)
        g._global_pool_add_lane_a(
            state["pool"], out, action["command"],
            [("post_search.localize", payload, subject)],
            krel="", event=None, kkind="post_search")

        assert len(state["pool"]) == 1
        candidate, thunk = state["pool"][0]
        prepared = state["prepared"][id(candidate)]
        assert candidate.current_ordinal == 1
        assert prepared["krel"] == subject
        assert prepared["event"] == "post_search"

        # A sibling result in the same policy observation body-acquired the
        # target after production but before final observation arbitration.
        g._ss_acquired_files.add(subject)
        plan = g._prepare_batch_delivery(candidate, thunk, prepared)
    finally:
        g._ss_acquired_files.discard(subject)
        g._clear_tool_observation_batch(state)

    assert plan.disposition == "suppressed"
    assert plan.suffix == ""
    assert plan.decision["reason"] == "ss_step_behind"


def test_finalizer_screens_claim_against_its_own_native_result(monkeypatch):
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    monkeypatch.setattr(g, "_ss_provenance_on", lambda: False)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: False)
    monkeypatch.setattr(g, "_ss_dedup2_on", lambda: False)
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda text: False)
    monkeypatch.setattr(g, "_ss_payload_has_content", lambda text: True)
    candidate = SimpleNamespace(kind="l3b.evidence", plane=g._GA_PLANE_LANE_A)
    plan = g._prepare_batch_delivery(
        candidate, lambda: None,
        {"out": {"output": "src/widget.py parse_config()"},
         "payload": "src/widget.py parse_config()", "join": True},
    )

    assert plan.disposition == "suppressed"
    assert plan.decision["reason"] == "ss_step_behind"


@pytest.mark.parametrize("plane", [g._GA_PLANE_LANE_A, g._GA_PLANE_STEER,
                                    g._GA_PLANE_GATEWAY])
def test_finalizer_holdout_is_winning_zero_byte_plan(monkeypatch, plane):
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda text: False)
    monkeypatch.setattr(g, "_ss_shadow_would_withhold", lambda *a: True)
    monkeypatch.setattr(g, "_lane_envelope_on", lambda: False)
    plan = _pure_plan(monkeypatch, plane=plane)
    assert plan.disposition == "holdout"
    assert plan.suffix == "" and plan.decision["payload"] == "payload"
    assert plan.decision["shipped_suffix"] == "\npayload"


def test_finalizer_suppresses_legacy_or_content_dedup(monkeypatch):
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda text: False)
    monkeypatch.setattr(g, "_oracle_content_hash", lambda text: "state-hash")
    monkeypatch.setattr(g, "_lane_envelope_on", lambda: False)
    monkeypatch.setattr(g, "_ss_shadow_would_withhold", lambda *a: False)
    g._oracle_delivered_hashes.add("state-hash")
    try:
        plan = _pure_plan(monkeypatch, plane=g._GA_PLANE_LANE_A)
        assert plan.disposition == "suppressed"
        assert plan.decision["reason"] == "delivered"
    finally:
        g._oracle_delivered_hashes.discard("state-hash")


@pytest.mark.parametrize("text,reason", [("", "empty"), ("payload", "lane_budget")])
def test_finalizer_suppresses_empty_or_byte_over_budget(monkeypatch, text, reason):
    monkeypatch.setattr(g, "_payload_leaks_test_identity", lambda value: False)
    monkeypatch.setattr(g, "_lane_envelope_on", lambda: True)
    monkeypatch.setattr(g, "_lane_fits_budget", lambda value: False)
    monkeypatch.setattr(g, "_ss_shadow_would_withhold", lambda *a: False)
    plan = _pure_plan(monkeypatch, plane=g._GA_PLANE_LANE_A, text=text)
    assert plan.disposition == "suppressed"
    assert plan.decision["reason"] == reason


def test_lane_budget_counts_utf8_bytes_not_codepoints(monkeypatch):
    monkeypatch.setattr(g, "_GT_LANE_MAX_DELTA", 4)
    assert not g._lane_fits_budget("ééé")  # 3 codepoints, 6 UTF-8 bytes


def test_formatter_truncation_falls_back_to_base_observation(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    payload = "G" * 12000

    def gateway(action, out, cmd, orig_out, *, pool=None, lattice_produced=None):  # lattice_produced: match prod signature (2026-07-22)
        candidate = SimpleNamespace(kind="large", plane="fact", dedup_key="")
        thunk = lambda: out.__setitem__(
            "output", g._join_lane_output(out["output"], payload))
        g._append_batch_candidate(pool, candidate, thunk, out, payload, join=True)

    class _TruncatingModel(_Model):
        def format_observation_messages(self, message, outputs, template_vars=None):
            rows = []
            for output in outputs:
                text = output["output"]
                if len(text) >= 10000:
                    text = text[:5000] + text[-5000:]
                rows.append({"role": "tool", "content": text})
            return rows

    monkeypatch.setattr(g, "_gt_gateway_deliver", gateway)
    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (SimpleNamespace(repair_support=False, losers=[]), pool[0][0]))
    agent = _Agent(_TruncatingModel())
    class _LongEnv(_Env):
        def execute(self, action):
            out = {"output": "B" * 12000, "returncode": 0}
            self.last_out = out
            g._augment_output(action, out)
            return out
    agent.env = _LongEnv()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions(
        {"extra": {"actions": [{"command": "one"}]}})
    assert rendered[0]["content"] == ("B" * 5000 + "B" * 5000)


def test_suffix_in_wrong_rendered_message_is_not_byte_proof():
    rendered = [
        {"role": "tool", "content": "target was clipped"},
        {"role": "tool", "content": "unrelated\nGT_SUFFIX"},
    ]
    assert not g._rendered_contains_exact_suffix(rendered, 0, "\nGT_SUFFIX")


def test_trajectory_raw_output_is_not_model_byte_proof():
    rendered = [{
        "role": "tool",
        "content": "<output>clipped</output>",
        "extra": {"raw_output": "base\nGT_SUFFIX"},
    }]
    assert not g._rendered_contains_exact_suffix(rendered, 0, "\nGT_SUFFIX")


def test_wrapped_and_multimodal_api_content_are_model_byte_proof():
    wrapped = [{"role": "tool", "content": "<output>base\nGT_SUFFIX</output>"}]
    multimodal = [{"role": "tool", "content": [
        {"type": "image_url", "image_url": {"url": "GT_SUFFIX"}},
        {"type": "text", "text": "base\nGT_SUFFIX"},
    ]}]
    assert g._rendered_contains_exact_suffix(wrapped, 0, "\nGT_SUFFIX")
    assert g._rendered_contains_exact_suffix(multimodal, 0, "\nGT_SUFFIX")


def test_multimodal_nontext_metadata_is_not_model_byte_proof():
    rendered = [{"role": "tool", "content": [
        {"type": "image_url", "image_url": {"url": "base/GT_SUFFIX"}},
    ]}]
    assert not g._rendered_contains_exact_suffix(rendered, 0, "GT_SUFFIX")


def test_suffix_proof_rejects_preexisting_identical_suffix():
    """A clipped augmented copy must not pass on bytes already in the base."""
    base = [{"role": "tool", "content": "base\nGT_SUFFIX"}]
    augmented = [{"role": "tool", "content": "base\nGT_SUFFIX"}]
    assert not g._rendered_contains_exact_suffix(
        augmented, 0, "\nGT_SUFFIX", base)


def test_suffix_proof_requires_structural_base_to_augmented_append():
    base = [{"role": "tool", "content": "base"}]
    augmented = [{"role": "tool", "content": "base\nGT_SUFFIX"}]
    assert g._rendered_contains_exact_suffix(
        augmented, 0, "\nGT_SUFFIX", base)


def test_suffix_proof_rejects_reordered_suffix_even_when_present():
    base = [{"role": "tool", "content": "base"}]
    reordered = [{"role": "tool", "content": "\nGT_SUFFIXbase"}]
    assert not g._rendered_contains_exact_suffix(
        reordered, 0, "\nGT_SUFFIX", base)


def test_partial_commit_exception_reconciles_visible_output_and_memory(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    before_hashes = set(g._oracle_delivered_hashes)
    before_dedup = set(g._EPISODE.delivered_dedup)
    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (SimpleNamespace(repair_support=False, losers=[]), pool[0][0]))

    def broken_commit(state, result, winner, plans):
        plans[id(winner)].output["output"] += "\nPARTIAL"
        g._oracle_delivered_hashes.add("partial")
        g._EPISODE.delivered_dedup.add("partial")
        raise RuntimeError("partial commit")

    monkeypatch.setattr(g, "_commit_batch_arbitration", broken_commit)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions(
        {"extra": {"actions": [{"command": "one"}]}})
    assert rendered[0]["content"] == "base:one\nGT:one"
    assert agent.env.last_out["output"] == "base:one\nGT:one"
    assert set(g._oracle_delivered_hashes) == before_hashes
    assert set(g._EPISODE.delivered_dedup) == before_dedup


def test_phase2_keyboard_interrupt_restores_then_reraises(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    before_hashes = set(g._oracle_delivered_hashes)
    monkeypatch.setattr(
        g, "_global_pool_plan",
        lambda pool: (SimpleNamespace(repair_support=False, losers=[]), pool[0][0]))

    def interrupted(state, result, winner, plans):
        plans[id(winner)].output["output"] += "\nPARTIAL"
        g._oracle_delivered_hashes.add("interrupt-partial")
        raise KeyboardInterrupt()

    monkeypatch.setattr(g, "_commit_batch_arbitration", interrupted)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    with pytest.raises(KeyboardInterrupt):
        agent.execute_actions({"extra": {"actions": [{"command": "one"}]}})
    assert agent.env.last_out["output"] == "base:one"
    assert set(g._oracle_delivered_hashes) == before_hashes


def test_holdout_ledger_hashes_exact_would_ship_suffix(monkeypatch):
    records = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **row: records.append(row))
    candidate = SimpleNamespace(
        kind="lane.fact", plane=g._GA_PLANE_LANE_A, dedup_key="unified",
        symbol="", lineage=None)
    decision = {
        "payload": "payload", "shipped_suffix": "\npayload",
        "shadow_key": "content", "content_state_hash": "state",
        "content_hash": "content"}
    plan = g._BatchDeliveryPlan(
        candidate, lambda: None, {"output": "base"}, "", False,
        "holdout", decision)
    state = {
        "contexts": {id(candidate): {"producer_iteration": 7, "krel": "x.py"}},
        "prepared": {id(candidate): {}}, "rollbacks": {}}
    result = SimpleNamespace(losers=[], repair_support=False)
    g._commit_batch_arbitration(state, result, candidate, {id(candidate): plan})
    row = next(record for record in records if record.get("outcome") == "shadow_holdout")
    assert row["content"] == "\npayload"
    assert row["extra"]["chars_would"] == len("\npayload")
    assert plan.output["output"] == "base"
    g._oracle_delivered_hashes.discard("state")
    g._oracle_delivered_hashes.discard("content")
    g._EPISODE.delivered_dedup.discard("unified")


@pytest.mark.parametrize("profile", ["", "1", "2", "custom"])
def test_global_arbiter_without_installed_handshake_falls_open_to_inline(monkeypatch, profile):
    # CONTRACT UPDATE (2026-07-23): this test formerly asserted ZERO dose when the batch-commit
    # handshake is not installed. That encoded the pre-2026-07-22 fail-CLOSED behavior. The
    # deliberate FAIL-OPEN fix (gt_mini_patch _augment_output ~18984: `_ga_on and not
    # _batch_commit_installed` -> `_ga_on = False`) now degrades to INLINE per-plane delivery for
    # single-action scaffolds (mini-swe) that never install the batch formatter — otherwise EVERY
    # reactive FACT would be silently dropped on every observation. So the correct contract is now:
    # the reactive dose still ships INLINE (the byte-identical `_ga_on==False` path), NOT zero.
    _wire_fake_candidates(monkeypatch)
    monkeypatch.setenv("GT_RL_PROFILE", profile)
    monkeypatch.setattr(g, "_batch_commit_installed", False)
    monkeypatch.setattr(g, "_batch_install_failed", False)
    out = _Env().execute({"command": "one"})
    assert out["output"] == "base:one\nGT:one"


def test_final_stepbehind_commits_known_fact_only_after_formatter(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    remembered = []
    rows = []
    monkeypatch.setattr(
        g, "_ss_content_decision",
        lambda *a, **k: (True, "ss_step_behind"))
    monkeypatch.setattr(
        g, "_ss_remember_known",
        lambda kind, text, root="", **kwargs: remembered.append(
            (kind, text, kwargs)))
    monkeypatch.setattr(
        g, "_runtime_ledger_record", lambda **row: rows.append(row))
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)

    rendered = agent.execute_actions(
        {"extra": {"actions": [{"command": "one"}]}})

    assert rendered[0]["content"] == "base:one"
    assert remembered == [("fake.one", "GT:one", {
        "is_loc": False, "knowledge_authority": True})]
    assert any(row.get("reason") == "ss_step_behind" for row in rows)


def test_final_stepbehind_ledger_preserves_subject_and_event(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    rows = []
    monkeypatch.setattr(
        g, "_ss_content_decision",
        lambda *args, **kwargs: (True, "ss_step_behind"))
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **row: rows.append(row))

    def gateway(action, out, cmd, orig_out, *, pool=None, lattice_produced=None):  # lattice_produced: match prod signature (2026-07-22)
        candidate = SimpleNamespace(
            kind="post_search.localize", plane=g._GA_PLANE_LANE_A,
            dedup_key="preview", symbol="", lineage=None)
        g._append_batch_candidate(
            pool, candidate, lambda: None, out, "preview", join=True,
            krel="src/widget.py",
            prepared_extra={
                "krel": "src/widget.py", "event": "post_search"})

    monkeypatch.setattr(g, "_gt_gateway_deliver", gateway)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions(
        {"extra": {"actions": [{"command": "rg Widget src"}]}})

    row = next(row for row in rows if row.get("reason") == "ss_step_behind")
    assert rendered[0]["content"] == "base:rg Widget src"
    assert row["file_path"] == "src/widget.py"
    assert row["event"] == "post_search"


def test_gateway_ledger_seals_exact_boundary_joined_suffix(monkeypatch):
    records = []
    out = {"output": "base", "returncode": 0}
    winner = SimpleNamespace(evidence_type="fact", target="src/x.py")
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **row: records.append(row))
    monkeypatch.setattr(g, "_persist_receipt", lambda *a, **k: None)
    monkeypatch.setattr(g, "_xsession_flush", lambda: None)
    monkeypatch.setattr(g, "_gateway_delivery_extra", lambda value: None)

    def commit_inline(pool, envelope, native, thunk, **kwargs):
        thunk()
        return True

    monkeypatch.setattr(g, "_global_pool_add_gateway", commit_inline)
    sealed = SimpleNamespace()
    g._gt_gateway_pool_envelope(
        winner, pool=[], out=out, ev=SimpleNamespace(kind="search"), native=True,
        render_envelope=lambda *a, **k: "FACT",
        fits_budget=lambda *a, **k: True,
        seal_delivery=lambda *a, **k: (sealed, "new-chain"),
        contains_gt_tag=lambda text: False,
        contains_test_identity=lambda text: False,
    )

    row = next(record for record in records if record.get("outcome") == "delivered")
    assert out["output"] == "base\nFACT"
    assert row["chars"] == len("\nFACT")
    assert row["content"] == "\nFACT"


@pytest.mark.parametrize("commands", [("submit", "sibling"),
                                       ("sibling", "submit")])
def test_submit_refusal_is_sole_dose_first_or_last(monkeypatch, commands):
    _wire_fake_candidates(monkeypatch)
    rolled_back = []

    def gateway(action, out, cmd, orig_out, *, pool=None, lattice_produced=None):  # lattice_produced: match prod signature (2026-07-22)
        is_submit = cmd == "submit"
        candidate = SimpleNamespace(
            kind="submit_gate" if is_submit else "sibling",
            plane="fact", dedup_key="", symbol="", lineage=None)
        payload = "REFUSE" if is_submit else "SIBLING_GT"
        thunk = lambda: out.__setitem__(
            "output", g._join_lane_output(out["output"], payload))
        g._append_batch_candidate(
            pool, candidate, thunk, out, payload, join=True,
            rollback=(None if is_submit
                      else lambda: rolled_back.append(cmd)))

    def arbitrate(pool):
        winner = next(candidate for candidate, _thunk in pool
                      if candidate.kind == "submit_gate")
        losers = [(candidate, "outranked") for candidate, _thunk in pool
                  if candidate is not winner]
        return SimpleNamespace(repair_support=False, losers=losers), winner

    monkeypatch.setattr(g, "_gt_gateway_deliver", gateway)
    monkeypatch.setattr(g, "_global_pool_plan", arbitrate)
    agent = _Agent()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions({"extra": {"actions": [
        {"command": command} for command in commands]}})
    contents = [row["content"] for row in rendered]
    assert sum("REFUSE" in content for content in contents) == 1
    assert all("SIBLING_GT" not in content for content in contents)
    assert rolled_back == ["sibling"]


@pytest.mark.parametrize("commands", [("submit", "sibling"),
                                       ("sibling", "submit")])
def test_native_precommitted_submit_refusal_owns_observation(monkeypatch, commands):
    _wire_fake_candidates(monkeypatch)

    class _SubmitEnv(_Env):
        def execute(self, action):
            if action["command"] == "submit":
                out = {"output": "NATIVE_REFUSAL", "returncode": 1}
                assert g._register_precommitted_batch_dose(
                    g._batch_context.get(), action, out)
                return out
            return super().execute(action)

    agent = _Agent()
    agent.env = _SubmitEnv()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions({"extra": {"actions": [
        {"command": command} for command in commands]}})

    contents = [row["content"] for row in rendered]
    assert sum("NATIVE_REFUSAL" in content for content in contents) == 1
    assert all("GT:sibling" not in content for content in contents)


def test_submit_refusal_delivery_commits_only_after_exact_formatter_visibility(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    rows = []
    content_hash = "c:" + "a" * 16
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **row: rows.append(row))
    monkeypatch.setattr(g, "_gt_submit_record", lambda *a, **k: None)
    monkeypatch.setattr(g, "_gt_submit_bounce_count", 0, raising=False)
    g._oracle_delivered_hashes.discard(content_hash)

    class _SubmitEnv(_Env):
        def execute(self, action):
            out = {
                "output": "NATIVE_REFUSAL", "returncode": 1,
                "_gt_pending_delivery": {
                    "content_hash": content_hash,
                    "verdict": SimpleNamespace(reason="hygiene"),
                    "extra": {"fact_class": "submit_refusal"},
                },
            }
            assert g._register_precommitted_batch_dose(
                g._batch_context.get(), action, out)
            assert not rows
            assert content_hash not in g._oracle_delivered_hashes
            assert g._gt_submit_bounce_count == 0
            return out

    agent = _Agent()
    agent.env = _SubmitEnv()
    assert g.install_observation_batch_commit(agent)
    rendered = agent.execute_actions({
        "extra": {"actions": [{"command": "submit"}]},
    })

    assert rendered == [{"role": "tool", "content": "NATIVE_REFUSAL"}]
    delivered = [row for row in rows if row.get("outcome") == "delivered"]
    assert len(delivered) == 1
    assert delivered[0]["content"] == "NATIVE_REFUSAL"
    assert delivered[0]["chars"] == len("NATIVE_REFUSAL")
    assert content_hash in g._oracle_delivered_hashes
    assert g._gt_submit_bounce_count == 1
    g._commit_precommitted_batch_dose({
        "payload": "NATIVE_REFUSAL",
        "pending_delivery": {
            "content_hash": content_hash,
            "verdict": SimpleNamespace(reason="hygiene"),
            "extra": {"fact_class": "submit_refusal"},
        },
    })
    assert len([row for row in rows if row.get("outcome") == "delivered"]) == 1
    assert g._gt_submit_bounce_count == 1


def test_submit_refusal_formatter_failure_never_commits_delivery(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    rows = []
    content_hash = "c:" + "b" * 16
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **row: rows.append(row))
    monkeypatch.setattr(g, "_gt_submit_record", lambda *a, **k: None)
    monkeypatch.setattr(g, "_gt_submit_bounce_count", 0, raising=False)
    g._oracle_delivered_hashes.discard(content_hash)

    class _SubmitEnv(_Env):
        def execute(self, action):
            out = {
                "output": "NATIVE_REFUSAL", "returncode": 1,
                "_gt_pending_delivery": {
                    "content_hash": content_hash,
                    "verdict": SimpleNamespace(reason="hygiene"),
                    "extra": {"fact_class": "submit_refusal"},
                },
            }
            assert g._register_precommitted_batch_dose(
                g._batch_context.get(), action, out)
            return out

    agent = _Agent(_Model(raises=True))
    agent.env = _SubmitEnv()
    assert g.install_observation_batch_commit(agent)
    with pytest.raises(RuntimeError, match="format failed"):
        agent.execute_actions({"extra": {"actions": [{"command": "submit"}]}})

    assert not [row for row in rows if row.get("outcome") == "delivered"]
    assert content_hash not in g._oracle_delivered_hashes
    assert g._gt_submit_bounce_count == 0


def test_second_precommitted_refusal_fails_open_instead_of_deadlocking(monkeypatch):
    _wire_fake_candidates(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_batch_install_failed", False)
    monkeypatch.setattr(g, "_gt_submit_bounce_count", 0, raising=False)

    class Submitted(Exception):
        pass

    counter = {"n": 0}

    def prepare(_env, _action, _exc):
        counter["n"] += 1
        return {
            "output": "REFUSAL", "returncode": 1,
            "_gt_pending_delivery": {
                "content_hash": "c:" + str(counter["n"]) * 16,
                "verdict": SimpleNamespace(reason="hygiene"),
                "extra": {"fact_class": "submit_refusal"},
            },
        }

    monkeypatch.setattr(g, "_gt_gate_submit_exception", prepare)

    def submit(_env, _action, *args, **kwargs):
        raise Submitted()

    class _SubmitEnv:
        execute = g._wrap_execute(submit)

    agent = _Agent()
    agent.env = _SubmitEnv()
    assert g.install_observation_batch_commit(agent)

    with pytest.raises(Submitted):
        agent.execute_actions({"extra": {"actions": [
            {"command": "submit-one"}, {"command": "submit-two"},
        ]}})

    assert counter["n"] == 2
    assert g._gt_submit_bounce_count == 0
    assert g._batch_context.get() is None

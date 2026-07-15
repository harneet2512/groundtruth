"""SS-0 features 1 (GT_SS_NOVELTY) + 2 (GT_SS_DEDUP2) — step-behind + entity-set dedup.

Causal-audit context (run 29236533134): ~210/296 GT deliveries were STEP-BEHIND (the
agent already held the cited entity), and the SAME caller-fact cluster shipped 3x as
byte-distinct variants whose entity sets were subsets of the first (conan-17092
migrations cluster m13/m49/m53 — byte-dedup let the semantic repeats pass).

RED-first: with the flag OFF (or the gate reverted) the step-behind / semantic-dup block
DELIVERS. Post-fix it is SUPPRESSED. Parameter-free (membership / containment). Leak
invariant preserved (SS only ever suppresses an already-leak-screened block).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.native_render import contains_test_identity  # noqa: E402


def _base(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: "")
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *a, **k: None, raising=False)
    for k in ("GT_SS_NOVELTY", "GT_SS_DEDUP2", "GT_SS_PROVENANCE", "GT_SS_LATE_DROP",
              "GT_SS_ACK_METRICS"):
        monkeypatch.delenv(k, raising=False)
    g._reset_oracle_state()


def _capture(monkeypatch):
    recs: list = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **k: recs.append(k))
    return recs


# --------------------------------------------------------------------------- #
# Feature 1 — NOVELTY / step-behind gate
# --------------------------------------------------------------------------- #
def test_novelty_pure_predicate_exempts_nudge_classes():
    g._ss_acquired_files.clear()
    g._ss_acquired_files.add("src/foo.py")
    # timing/salience nudge classes are NEVER gated (recovery / edit.syntax / detect.*)
    for kind in ("recovery", "edit.syntax", "detect.coherence", "l5.failure",
                 "verify.horizon.executed"):
        assert g._ss_novelty_suppresses(kind, "src/foo.py get_user()", "") is False
    # a factual class whose cited file is acquired -> step-behind
    assert g._ss_novelty_suppresses("l3.contract", "src/foo.py get_user()", "") is True


def test_novelty_suppresses_when_all_entities_acquired(monkeypatch):
    _base(monkeypatch)
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    g._ss_acquired_files.add("src/foo.py")
    block = ("l3.contract", "\n<gt-contract>\nsrc/foo.py: def get_user(uid)\n</gt-contract>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="src/foo.py", event=None)
    assert (out.get("output") or "") == ""  # SUPPRESSED (the file was already opened)
    assert any(r.get("reason") == "ss_step_behind" for r in recs)


def test_novelty_delivers_when_entity_not_acquired(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    # the agent has NOT opened src/other.py -> the contract about it is novel
    g._ss_acquired_files.add("src/foo.py")
    block = ("l3.contract", "\n<gt-contract>\nsrc/other.py: def handler(uid)\n</gt-contract>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="src/other.py", event=None)
    assert "<gt-contract>" in (out.get("output") or "")  # DELIVERED (novel file)


@pytest.mark.parametrize("event", ["post_view", "post_edit"])
def test_closed_subject_boundary_preserves_mixed_novel_caller_fact(
        monkeypatch, event):
    """A downstream caller decision stays open when the fact adds a novel path."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    payload = "src/subject.py run()\nsrc/novel.py helper()"

    suppressed, reason = g._ss_screen_delivery(
        "l3b.evidence", payload, "", subject_path="src/subject.py", event=event)

    assert suppressed is False
    assert reason == ""


def test_post_view_subject_only_claim_suppresses_when_acquired(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    g._ss_acquired_files.add("src/subject.py")

    suppressed, reason = g._ss_screen_delivery(
        "l3b.evidence", "src/subject.py run()", "",
        subject_path="src/subject.py", event="post_view",
    )

    assert suppressed is True
    assert reason == "ss_step_behind"


def test_open_search_boundary_preserves_mixed_novel_caller_fact(monkeypatch):
    """The same caller fact remains eligible when emitted at a still-open search
    boundary before the agent has selected and body-read the subject file."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    payload = "src/subject.py run()\nsrc/novel.py helper()"

    suppressed, reason = g._ss_screen_delivery(
        "l3b.evidence", payload, "", subject_path="src/subject.py", event="post_search")

    assert suppressed is False
    assert reason == ""


def test_same_native_observation_suppresses_complete_actionable_claim(monkeypatch):
    """A fact adds nothing when its exact entities are already in this result."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")

    suppressed, reason = g._ss_content_decision(
        "l3b.evidence", "src/widget.py parse_config()", "",
        native_text="Traceback:\nsrc/widget.py parse_config()\nfailed",
    )

    assert suppressed is True
    assert reason == "ss_step_behind"


def test_same_native_observation_preserves_claim_with_new_entity(monkeypatch):
    """Containment is exact: one additional entity keeps the GT fact eligible."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")

    suppressed, reason = g._ss_content_decision(
        "l3b.evidence", "src/widget.py parse_config() repair_schema()", "",
        native_text="Traceback in src/widget.py while calling parse_config()",
    )

    assert suppressed is False
    assert reason == ""


def test_same_entities_with_new_relation_remain_eligible(monkeypatch):
    """Entity coincidence cannot erase a novel actionable relation."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    native = "src/a.py foo() failed; src/b.py bar() exists"
    claim = "src/b.py bar() calls src/a.py foo()"
    assert g._ss_entity_set(native) == g._ss_entity_set(claim)

    suppressed, reason = g._ss_content_decision(
        "l3b.evidence", claim, "", native_text=native,
    )

    assert suppressed is False
    assert reason == ""


def test_native_claim_comparison_preserves_semantic_indentation(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    claim = "if ready:\n    src/widget.py parse_config()"
    native = "if ready:\nsrc/widget.py parse_config()"

    suppressed, reason = g._ss_content_decision(
        "l3b.evidence", claim, "", native_text=native,
    )

    assert suppressed is False
    assert reason == ""


def test_same_native_observation_does_not_suppress_timing_nudge(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")

    suppressed, reason = g._ss_content_decision(
        "recovery", "src/widget.py parse_config()", "",
        native_text="src/widget.py parse_config()",
    )

    assert suppressed is False
    assert reason == ""


def test_novel_gateway_caller_relation_survives_edit_boundary(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")

    suppressed, reason = g._ss_content_decision(
        "caller_contract", "src/caller.py invoke() calls src/widget.py parse_config()", "",
        subject_path="src/widget.py", event="post_edit",
        native_text="edited src/widget.py parse_config()",
    )

    assert suppressed is False
    assert reason == ""


def test_native_equivalent_gateway_claim_is_step_behind_at_edit(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    claim = "src/caller.py invoke() calls src/widget.py parse_config()"

    suppressed, reason = g._ss_content_decision(
        "caller_contract", claim, "", subject_path="src/widget.py",
        event="post_edit", native_text=f"edit result:\n{claim}",
    )

    assert suppressed is True
    assert reason == "ss_step_behind"


def test_search_preview_is_suppressed_if_same_observation_acquired_target(
        monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    g._ss_acquired_files.add("src/subject.py")

    suppressed, reason = g._ss_screen_delivery(
        "post_search.localize", "src/subject.py:10:run", "",
        subject_path="src/subject.py", event="post_search")

    assert suppressed is True
    assert reason == "ss_step_behind"


def test_novelty_off_delivers_stepbehind_block(monkeypatch):
    """RED anchor: flag OFF -> the step-behind block MUST deliver (byte-identical pre-SS)."""
    _base(monkeypatch)  # GT_SS_NOVELTY not set
    g._ss_acquired_files.add("src/foo.py")
    block = ("l3.contract", "\n<gt-contract>\nsrc/foo.py: def get_user(uid)\n</gt-contract>")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="src/foo.py", event=None)
    assert "<gt-contract>" in (out.get("output") or "")


def test_novelty_pathless_block_uses_symbol_acquisition():
    g._ss_acquired_files.clear()
    g._ss_acquired_symbols.clear()
    g._ss_acquired_symbols.add("frobnicate")
    # a path-less block naming only an acquired greped symbol -> step-behind
    assert g._ss_novelty_suppresses("l3b.evidence", "frobnicate() is called here", "") is True
    # a path-less block naming an UN-greped symbol -> deliver
    assert g._ss_novelty_suppresses("l3b.evidence", "brandnew() is called here", "") is False


# --------------------------------------------------------------------------- #
# Feature 2 — ENTITY-SET dedup (containment on top of byte dedup)
# --------------------------------------------------------------------------- #
def test_dedup2_kills_bytedistinct_subset(monkeypatch):
    """The conan-17092 shape: 3 byte-distinct payloads, same/subset entity set."""
    _base(monkeypatch)
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    b1 = ("l3.contract", "\n<gt-contract>\nmigrations.py migrate() rollback()\n</gt-contract>")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [b1], krel="migrations.py", event=None)
    assert "migrate" in (o1.get("output") or "")  # first delivers + records the entity set
    # a byte-DISTINCT variant whose entity set {migrations.py, migrate} ⊆ prior
    b2 = ("l3.contract", "\n<gt-contract>\nthe caller of migrations.py migrate() lives here\n</gt-contract>")
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [b2], krel="migrations.py", event=None)
    assert (o2.get("output") or "") == ""  # SUPPRESSED as ss_semantic_dup
    assert any(r.get("reason") == "ss_semantic_dup" for r in recs)


def test_dedup2_group_crosses_classes(monkeypatch):
    """Spec refinement: the conan-17092 cluster CROSSED classes — m13 l3b.evidence citing
    migrations.py:48, m49 l3.contract re-delivering a SUBSET. Group ``caller_facts``
    dedups across l3b.evidence + l3.contract."""
    _base(monkeypatch)
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    # m13: l3b.evidence citing migrations.py callers
    b1 = ("l3b.evidence", "\n<gt-evidence>\nmigrations.py migrate() rollback() apply()\n</gt-evidence>")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [b1], krel="migrations.py", event=None)
    assert "migrate" in (o1.get("output") or "")
    # m49: a DIFFERENT class (l3.contract) whose entity set ⊆ the prior l3b.evidence
    b2 = ("l3.contract", "\n<gt-contract>\nmigrations.py migrate()\n</gt-contract>")
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [b2], krel="migrations.py", event=None)
    assert (o2.get("output") or "") == ""  # cross-class containment suppresses
    assert any(r.get("reason") == "ss_semantic_dup" for r in recs)


def test_stepbehind_fact_becomes_known_for_later_cross_class_subset(monkeypatch):
    """A fact already known through native acquisition is a semantic prior even when GT
    correctly withheld its first rendering as step-behind."""
    _base(monkeypatch)
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    g._ss_acquired_files.add("conans/client/migrations.py")

    first = ("l3b.evidence", "conans/client/migrations.py migrate_settings_file() update_file()")
    g._lane_a_deliver({}, "cmd", [first], krel="conans/client/migrations.py", event=None)
    second = ("l3.contract", "conans/client/migrations.py migrate_settings_file()")
    g._lane_a_deliver({}, "cmd", [second], krel="conans/client/migrations.py", event=None)

    assert [r.get("reason") for r in recs] == ["ss_step_behind", "ss_semantic_dup"]


def test_known_fact_decision_has_lane_gateway_parity_and_reset(monkeypatch):
    _base(monkeypatch)
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    g._ss_acquired_files.add("src/known.py")
    commits: list[str] = []
    winner = SimpleNamespace(
        evidence_type="def_ref_partition", target="src/known.py",
        tier="VERIFIED", confidence=1.0, provenance=(("src/known.py", 1),),
    )

    g._global_pool_add_gateway(
        [], winner, True, lambda: commits.append("first"), ev_kind="search",
        rendered_text="src/known.py run() helper()",
    )
    g._global_pool_add_gateway(
        [], winner, True, lambda: commits.append("second"), ev_kind="search",
        rendered_text="src/known.py run()",
    )

    assert commits == []
    assert [r.get("reason") for r in recs] == ["ss_step_behind", "ss_semantic_dup"]
    assert g._ss_known_entsets
    g._ss_reset()
    assert g._ss_known_entsets == {}


def test_unverified_gateway_stepbehind_never_seeds_semantic_known(monkeypatch):
    """A path hit proves acquisition, not the truth of a low-authority envelope."""
    _base(monkeypatch)
    recs = _capture(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    g._ss_acquired_files.add("src/known.py")
    winner = SimpleNamespace(
        evidence_type="def_ref_partition", target="src/known.py",
        tier="INFO", confidence=0.0, provenance=(("src/known.py", 1),),
    )

    g._global_pool_add_gateway(
        [], winner, True, lambda: None, ev_kind="search",
        rendered_text="src/known.py run() helper()",
    )
    g._global_pool_add_gateway(
        [], winner, True, lambda: None, ev_kind="search",
        rendered_text="src/known.py run()",
    )

    assert [r.get("reason") for r in recs] == ["ss_step_behind", "ss_step_behind"]
    assert g._ss_known_entsets == {}


def test_unverified_gateway_delivery_never_seeds_semantic_known(monkeypatch):
    """Receipt of an advisory envelope does not upgrade it into dedup authority."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    committed: list[str] = []
    winner = SimpleNamespace(
        evidence_type="def_ref_partition", target="src/novel.py",
        tier="INFO", confidence=0.0, provenance=(("src/novel.py", 1),),
    )

    pool: list = []
    g._global_pool_add_gateway(
        pool, winner, True, lambda: committed.append("delivered"), ev_kind="search",
        rendered_text="src/novel.py helper()",
    )
    if pool:
        pool[0][1]()

    assert committed == ["delivered"]
    assert g._ss_known_entsets == {}


@pytest.mark.parametrize(("tier", "confidence", "provenance"), [
    ("INFO", 1.0, (("src/known.py", 1),)),
    ("VERIFIED", 0.69, (("src/known.py", 1),)),
    ("VERIFIED", 1.0, ()),
])
def test_gateway_knowledge_authority_fails_closed_each_contract_dimension(
        monkeypatch, tier, confidence, provenance):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    g._ss_acquired_files.add("src/known.py")
    winner = SimpleNamespace(
        evidence_type="def_ref_partition", target="src/known.py",
        tier=tier, confidence=confidence, provenance=provenance,
    )

    g._global_pool_add_gateway(
        [], winner, True, lambda: None, ev_kind="search",
        rendered_text="src/known.py run()",
    )

    assert g._ss_known_entsets == {}


def test_novel_cross_file_fact_survives_and_does_not_precommit_known_state(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_NOVELTY", "1")
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    g._ss_acquired_files.add("src/subject.py")
    block = ("l3b.evidence", "src/subject.py run()\nsrc/novel.py helper()")
    out: dict = {}
    g._lane_a_deliver(out, "cmd", [block], krel="src/subject.py", event=None)
    assert "src/novel.py" in (out.get("output") or "")
    # The delivered commit, not the preflight decision, owns known-state mutation.
    assert len(g._ss_known_entsets.get("caller_facts", ())) == 1


def test_nonknowledge_suppressions_and_arbiter_losers_never_seed_known(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    assert g._ss_screen_delivery("l3.contract", "/tmp/bad.py helper()", "")[1] == "ss_provenance"
    assert g._ss_known_entsets == {}

    monkeypatch.delenv("GT_SS_PROVENANCE")
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    monkeypatch.setattr(g, "_ss_late_drop_suppresses", lambda *a, **k: True)
    assert g._ss_screen_delivery("l3.contract", "src/a.py helper()", "")[1] == "ss_late"
    assert g._ss_known_entsets == {}

    monkeypatch.delenv("GT_SS_LATE_DROP")
    winner = SimpleNamespace(
        evidence_type="def_ref_partition", target="src/novel.py",
        dedup_key="novel-key", fact_id="fact", tier="VERIFIED",
        confidence=1.0, lineage=None,
    )
    pool: list = []
    g._global_pool_add_gateway(
        pool, winner, True, lambda: None, ev_kind="search",
        rendered_text="src/novel.py helper()",
    )
    assert len(pool) == 1  # eligible but not committed: model knows nothing yet
    assert g._ss_known_entsets == {}


def test_content_flags_off_do_not_mutate_known_state(monkeypatch):
    _base(monkeypatch)
    g._ss_acquired_files.add("src/a.py")
    out: dict = {}
    g._lane_a_deliver(
        out, "cmd", [("l3.contract", "src/a.py helper()")],
        krel="src/a.py", event=None,
    )
    assert "src/a.py" in (out.get("output") or "")
    assert g._ss_known_entsets == {}


def test_dedup2_obligation_outside_group(monkeypatch):
    """Nudge/obligation classes stay OUTSIDE dedup groups (value = timing/salience)."""
    assert g._ss_dedup_group("obligation.unexercised") is None
    assert g._ss_dedup_group("recovery") is None
    assert g._ss_dedup_group("consensus.scope") is None
    assert g._ss_dedup2_suppresses("obligation.unexercised", "migrations.py migrate()", "") is False


def test_dedup2_superset_still_delivers(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_DEDUP2", "1")
    b1 = ("l3.contract", "\n<gt-contract>\nmigrations.py migrate()\n</gt-contract>")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [b1], krel="migrations.py", event=None)
    assert "migrate" in (o1.get("output") or "")
    # a SUPERSET (adds a new symbol the agent hasn't been told) is NOT a repeat -> delivers
    b2 = ("l3.contract", "\n<gt-contract>\nmigrations.py migrate() and new_helper()\n</gt-contract>")
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [b2], krel="migrations.py", event=None)
    assert "new_helper" in (o2.get("output") or "")


def test_dedup2_off_delivers_repeat(monkeypatch):
    """RED anchor: flag OFF -> the semantic repeat DELIVERS (byte dedup can't catch it)."""
    _base(monkeypatch)  # GT_SS_DEDUP2 not set
    b1 = ("l3.contract", "\n<gt-contract>\nmigrations.py migrate() rollback()\n</gt-contract>")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [b1], krel="migrations.py", event=None)
    b2 = ("l3.contract", "\n<gt-contract>\ncaller of migrations.py migrate() here\n</gt-contract>")
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [b2], krel="migrations.py", event=None)
    assert "<gt-contract>" in (o2.get("output") or "")  # byte-distinct -> delivers when off


def test_ss_suppression_never_leaks():
    # SS emits ZERO model bytes; the reasons it stamps carry no test identity.
    for r in ("ss_step_behind", "ss_semantic_dup", "ss_provenance", "ss_late"):
        assert not contains_test_identity(r)

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

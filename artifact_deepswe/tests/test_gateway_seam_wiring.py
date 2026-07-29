"""W2 — the GT_GATEWAY seam wiring (gt_mini_patch._augment_output / _gt_gateway_deliver).

The ONE gateway.augment() call is wired into the mini seam behind the master flag
GT_GATEWAY (default OFF = byte-identical). These pins prove, on the REAL seam:
  * flag-OFF is a no-op (byte-identical splice);
  * flag-ON appends ONE deterministic FACT, BYTE-PRESERVING (TITO laws 1/2);
  * the message-list conformance diff = ONLY appended gateway deltas;
  * flag-ON parity: the seam delta == the pure adapter pipeline on the SAME event
    (incl. the whole D0 search corpus);
  * W1b single-owner: `_search_seen`/`_oracle_delivered_hashes` ARE the episode's
    containers (mandate b); the episode-boundary re-sees facts (mandate c);
  * TITO ledger + receipts recorded on the sealed envelope copies (mandates d/g);
  * M1 (byte-preservation) and M2 (second dedup owner) mutations BITE.

Hermetic: a synthetic graph.db, no ONNX / no repo checkout, GT_POST_SEARCH off so
the ONLY flag-ON delta is the gateway's.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import gt_mini_patch as g
from groundtruth.runtime import gateway as gw
from groundtruth.runtime import global_arbiter as ga
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import (
    RECEIPT_ACTED,
    RECEIPT_DELIVERED,
    VERIFIED,
    EvidenceEnvelope,
)

_CORPUS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "gateway_corpus"


def _mk_graph(tmp_path, nodes):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5), n.get("is_test", 0),
             n.get("language", "python")))
    con.commit()
    con.close()
    return db


@pytest.fixture
def seam(tmp_path, monkeypatch):
    """A synthetic 2-file 'run' graph (AMBIGUOUS_HIT -> def_ref_partition) wired into
    the mini seam; GT_POST_SEARCH off so only the gateway can add a delta."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    g._reset_oracle_state()
    yield db
    g._reset_oracle_state()


def _turn(cmd, output, rc=0):
    return {"command": cmd}, {"output": output, "returncode": rc}


def _run_output(cmd, output, *, rc=0):
    """Run one turn through the REAL seam and return the resulting observation text."""
    action, out = _turn(cmd, output, rc)
    g._augment_output(action, out)
    return out["output"]


def _gateway_env(kind, *, target, fact_id, payload):
    return EvidenceEnvelope.build(
        producer="gateway-regression",
        fact_id=fact_id,
        target=target,
        evidence_type=kind,
        payload=(payload,),
        provenance=(),
        confidence=0.9,
        tier=VERIFIED,
    )


def _drive_fake_gateway_pool(monkeypatch, envelopes):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setattr(gw, "augment", lambda _event, _state: list(envelopes))
    out = {"output": "base observation", "returncode": 0}
    pool = []
    g._gt_gateway_deliver(
        {"command": "grep -rn run ."}, out, "grep -rn run .",
        "base observation", pool=pool,
    )
    g._global_pool_flush(pool, kkind="post_search", kf="", krel="")
    return out, pool


# --------------------------------------------------------------------------- #
# flag-OFF is a byte-identical no-op splice
# --------------------------------------------------------------------------- #
def test_flag_off_no_op(seam, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "0")
    out = {"output": "a/x.py:10: run\nb/y.py:20: run", "returncode": 0}
    action = {"command": "grep -rn run ."}
    before = out["output"]
    g._augment_output(action, out)
    assert out["output"] == before          # gateway added nothing
    assert not g._gt_gateway_deliveries      # nothing recorded


# --------------------------------------------------------------------------- #
# Global-pool gateway fallback: local preselection must not destroy candidates.
# --------------------------------------------------------------------------- #
def test_global_pool_keeps_fallback_when_local_winner_is_globally_internal(
    seam, monkeypatch,
):
    high = _gateway_env(
        "trace_frame", target="a/x.py", fact_id="run",
        payload="HIGH local-priority candidate",
    )
    fallback = _gateway_env(
        "cochange_partner", target="b/y.py", fact_id="helper",
        payload="USEFUL global fallback",
    )
    real_rank = ga.rank_of
    monkeypatch.setattr(
        ga, "rank_of", lambda kind: None if kind == "trace_frame" else real_rank(kind)
    )

    out, _pool = _drive_fake_gateway_pool(monkeypatch, [high, fallback])

    assert "USEFUL global fallback" in out["output"]
    assert "HIGH local-priority candidate" not in out["output"]


def test_global_pool_keeps_fallback_when_local_winner_is_already_acquired(
    seam, monkeypatch,
):
    high = _gateway_env(
        "def_ref_partition", target="a/x.py", fact_id="run",
        payload="HIGH acquired localization",
    )
    fallback = _gateway_env(
        "cochange_partner", target="b/y.py", fact_id="helper",
        payload="USEFUL acquired fallback",
    )
    g._EPISODE.read_targets.add("a/x.py")

    out, _pool = _drive_fake_gateway_pool(monkeypatch, [high, fallback])

    assert "USEFUL acquired fallback" in out["output"]
    assert "HIGH acquired localization" not in out["output"]


def test_global_pool_keeps_fallback_when_local_winner_is_redundant(
    seam, monkeypatch,
):
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    g._search_seen["run"] = {"answered": True}
    high = _gateway_env(
        "def_ref_partition", target="a/x.py", fact_id="run",
        payload="HIGH redundant localization",
    )
    fallback = _gateway_env(
        "cochange_partner", target="b/y.py", fact_id="helper",
        payload="USEFUL redundant fallback",
    )

    out, pool = _drive_fake_gateway_pool(monkeypatch, [high, fallback])

    assert pool[0][0].redundant_with_delivered is True
    # Acquired suppression and delivered-fact redundancy have distinct identities:
    # the former is path-scoped, while the latter is keyed by the producer's symbol.
    assert pool[0][0].symbol == "a/x.py"
    assert pool[0][0].fact_id == "run"
    assert "USEFUL redundant fallback" in out["output"]
    assert "HIGH redundant localization" not in out["output"]


def test_literal_localization_is_an_external_global_class():
    assert ga.class_of_kind("localization") == "localization"
    assert ga.rank_of("localization") == ga.RANK_LADDER["localization"]


def test_global_off_keeps_local_gateway_arbitration_bytes(seam, monkeypatch):
    high = _gateway_env(
        "trace_frame", target="a/x.py", fact_id="run",
        payload="LOCAL WINNER BYTES",
    )
    fallback = _gateway_env(
        "cochange_partner", target="b/y.py", fact_id="helper",
        payload="LOWER BYTES",
    )
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")
    monkeypatch.setattr(gw, "augment", lambda _event, _state: [high, fallback])
    out = {"output": "base observation", "returncode": 0}
    expected_delta = ad.render_envelope(
        high, native=False, def_facts_renderer=g._gateway_def_render
    )

    g._gt_gateway_deliver(
        {"command": "grep -rn run ."}, out, "grep -rn run .",
        "base observation", pool=None,
    )

    assert out["output"] == g._join_lane_output(
        "base observation", expected_delta
    )


# --------------------------------------------------------------------------- #
# flag-ON appends ONE fact, BYTE-PRESERVING
# --------------------------------------------------------------------------- #
def test_flag_on_appends_byte_preserving(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"

    monkeypatch.setenv("GT_GATEWAY", "0")
    off = _run_output("grep -rn run .", grep_out)
    g._reset_oracle_state()

    monkeypatch.setenv("GT_GATEWAY", "1")
    on = _run_output("grep -rn run .", grep_out)

    assert on != off
    assert on.startswith(off)                # law 1/2: pure suffix, prefix untouched
    delta = on[len(off):]
    assert delta and "a/x.py:10" in delta    # the def/ref partition reached the model
    # a delivery copy was sealed (TITO level-1 receipt)
    assert g._gt_gateway_deliveries
    d = g._gt_gateway_deliveries[-1]
    assert d.receipt_state == RECEIPT_DELIVERED and d.rendered_bytes_hash
    assert d.parent_observation_hash == ""   # first delivery -> genesis parent
    assert g._gt_gateway_chain_head          # chain advanced


# --------------------------------------------------------------------------- #
# message-list conformance: flag-on == flag-off + appended gateway deltas ONLY
# --------------------------------------------------------------------------- #
_EPISODE_SCRIPT = [
    ("ls -la", "a/x.py\nb/y.py", 0),                       # other -> no delta
    ("grep -rn run .", "a/x.py:10: run\nb/y.py:20: run", 0),  # search -> delta
    ("cat a/x.py", "def run(): pass", 0),                  # view -> no delta (no graph fact)
    ("grep -rn run .", "a/x.py:10: run\nb/y.py:20: run", 0),  # repeat -> dedup, no new delta
]


def _replay(monkeypatch, gateway_on):
    g._reset_oracle_state()
    monkeypatch.setenv("GT_GATEWAY", "1" if gateway_on else "0")
    return [_run_output(cmd, out, rc=rc) for cmd, out, rc in _EPISODE_SCRIPT]


def test_message_list_conformance_diff(seam, monkeypatch):
    off = _replay(monkeypatch, gateway_on=False)
    on = _replay(monkeypatch, gateway_on=True)
    assert len(on) == len(off)
    deltas = []
    for o_on, o_off in zip(on, off):
        assert o_on.startswith(o_off), "each observation is off + a pure suffix"
        deltas.append(o_on[len(o_off):])
    # exactly the search turn (index 1) carried a delta; the repeat (index 3) is
    # dedup-suppressed within the SAME episode.
    assert deltas[0] == "" and deltas[2] == "" and deltas[3] == ""
    assert deltas[1] and "run" in deltas[1]


# --------------------------------------------------------------------------- #
# flag-ON parity: seam delta == the pure adapter pipeline on the SAME event
# --------------------------------------------------------------------------- #
def test_flag_on_parity_vs_adapter_pipeline(seam, monkeypatch, tmp_path):
    db = seam
    grep_out = "a/x.py:10: run\nb/y.py:20: run"

    # the seam's delta (flag-on minus flag-off)
    monkeypatch.setenv("GT_GATEWAY", "0")
    off = _run_output("grep -rn run .", grep_out)
    g._reset_oracle_state()
    monkeypatch.setenv("GT_GATEWAY", "1")
    on = _run_output("grep -rn run .", grep_out)
    seam_delta = on[len(off):]

    # the DIRECT pre-wired path: normalize -> augment -> arbitrate -> render, using the
    # SAME injected def renderer the seam uses.
    g._reset_oracle_state()
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path), episode=g._EPISODE)
    ev = ad.normalize_event("grep -rn run .", grep_out, 0, g._action_count)
    winner = ad.arbitrate(gw.augment(ev, st))
    direct = ad.render_envelope(winner, native=False, def_facts_renderer=g._gateway_def_render)

    # Seam-F6 (bounce 2026-07-10): the seam now inserts the L-1a one-newline boundary so a
    # delta that does not open with `\n` (the tagged `<gt-fact`/`<gt-search-facts` and the
    # native body forms) cannot JAM onto a non-newline-terminated observation. The CORRECT
    # ladder is the boundary-JOINED adapter render (byte-identical to `direct` whenever the
    # observation already ends in `\n`, i.e. the production command-output case). Pre-F6 this
    # pin asserted a raw suffix `seam_delta == direct`, which jammed on this fixture.
    assert seam_delta == g._join_lane_output(off, direct)[len(off):] and direct != ""


def test_d0_corpus_seam_mirrors_adapter(seam, monkeypatch):
    """D0 flag-ON parity: for EVERY real search-corpus event, the seam delta equals
    the pure adapter pipeline on the same event (both empty without a matching graph
    -> the seam faithfully mirrors the adapter; no spurious delivery)."""
    rows = [json.loads(ln) for ln in (_CORPUS / "search.jsonl").read_text(
        encoding="utf-8").splitlines() if ln.strip() and "__meta__" not in ln]
    assert rows
    for r in rows:
        cmd, output = r.get("command", ""), r.get("output", "")
        g._reset_oracle_state()
        monkeypatch.setenv("GT_GATEWAY", "0")
        off = _run_output(cmd, output, rc=r.get("returncode", 0) or 0)
        g._reset_oracle_state()
        monkeypatch.setenv("GT_GATEWAY", "1")
        on = _run_output(cmd, output, rc=r.get("returncode", 0) or 0)
        assert on.startswith(off)
        seam_delta = on[len(off):]

        g._reset_oracle_state()
        st = gw.GatewayState(graph_db=g._db_path(), repo_root=g._root(), episode=g._EPISODE)
        ev = ad.normalize_event(cmd, output, r.get("returncode", 0) or 0, g._action_count)
        winner = ad.arbitrate(gw.augment(ev, st))
        direct = "" if winner is None else ad.render_envelope(
            winner, native=False, def_facts_renderer=g._gateway_def_render)
        assert seam_delta == direct


# --------------------------------------------------------------------------- #
# GT_POST_SEARCH_NATIVE: the seam reuses _fmt_def_facts_native (tag-free)
# --------------------------------------------------------------------------- #
def test_native_flag_is_tagfree_reuses_fmt_def_facts_native(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    off_len = len(grep_out)
    on = _run_output("grep -rn run .", grep_out)
    delta = on[off_len:]
    assert "<gt-" not in delta                 # world-fact voice: ZERO tags
    assert "a/x.py:10:run" in delta            # native ripgrep-style def line
    # equals the mini's OWN native formatter on the same symbol (reuse, not re-impl)
    import sqlite3  # noqa: F401  (graph already built by the seam fixture)
    con = g._connect_ro(g._db_path())
    try:
        info = g._resolve_symbol_defs(con, "run", g._root())
    finally:
        con.close()
    # Seam-F6: the CORRECT ladder is the boundary-joined native render (grep_out has no
    # trailing `\n` here, so the seam prepends exactly one `\n` boundary). Content + voice
    # are unchanged — still the mini's OWN native formatter, tag-free (asserted above).
    expected = g._fmt_def_facts_native("run", info, g._root())
    assert delta == g._join_lane_output(grep_out, expected)[len(grep_out):]


# --------------------------------------------------------------------------- #
# W1b single-owner (mandate b) + episode-boundary (mandate c) THROUGH the seam
# --------------------------------------------------------------------------- #
def test_w1b_single_owner_identity():
    assert g._search_seen is g._EPISODE.probe_ledger
    assert g._oracle_delivered_hashes is g._EPISODE.delivered_dedup


def test_episode_boundary_reseen_through_seam(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    d1 = _run_output("grep -rn run .", grep_out)
    assert d1.count("a/x.py:10") >= 1
    # same episode -> suppressed (no NEW delivery recorded)
    n_before = len(g._gt_gateway_deliveries)
    _run_output("grep -rn run .", grep_out)
    assert len(g._gt_gateway_deliveries) == n_before   # dedup within the episode
    # a NEW attempt re-sees the fact (per-episode chain resets at genesis)
    g._reset_oracle_state()
    monkeypatch.setenv("GT_GATEWAY", "1")
    _run_output("grep -rn run .", grep_out)
    assert len(g._gt_gateway_deliveries) == 1
    assert g._gt_gateway_deliveries[-1].parent_observation_hash == ""  # genesis again


# --------------------------------------------------------------------------- #
# receipts (mandate g) levels 1->2->3 recorded through the seam
# --------------------------------------------------------------------------- #
def test_receipt_ladder_is_capped_at_delivered_through_seam(seam, monkeypatch):
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    _run_output("grep -rn run .", grep_out)             # level 1 DELIVERED (target a/x.py)
    assert g._gt_gateway_deliveries[-1].receipt_state == RECEIPT_DELIVERED

    # B-13: a later ENV OUTPUT naming the delivered file is NOT a reference — environment
    # text is not the agent referencing the fact (the `git status` command does not target
    # a/x.py; only its output mentions it). The receipt STAYS DELIVERED. REFERENCED (a
    # policy MESSAGE naming the fact) is not observable at the seam and is computed offline
    # by the grader; the seam must never falsely promote from environment output.
    _run_output("git status", "  modified:   a/x.py")
    assert g._gt_gateway_deliveries[-1].receipt_state == RECEIPT_DELIVERED

    # RE-POINTED 2026-07-28 (Wave 1 Step 5). This asserted that a command TARGETING the
    # delivered file promotes the receipt to ACTED. That assertion was a TAUTOLOGY:
    # `update_receipts` (adapters/miniswe.py:1483) decides `acted` as
    # `any(_entity_matches(k, e, cmd) ...)` over an `ents` that always contains
    # ("path", env.target) -- and env.target IS the file GT delivered evidence ABOUT,
    # i.e. the file the agent was already working on. It could not fail.
    #
    # Worse, the field was causally INVERTED for half its rows: lane-sealed deliveries
    # were promoted by the very command that CAUSED them (the seal at
    # gt_mini_patch.py:14578 precedes the promotion loop run against THIS turn's cmd),
    # while gateway rows were promoted honestly on a later turn. One field, two opposite
    # meanings, no discriminating column. The promotion block is deleted; the seam's
    # ladder is now CAPPED at `delivered` and `acted` is defined OFFLINE only, by
    # fair_probe_result._treatment_acted (delivery_index < action_index <=
    # decision_commit_index), which is the definition this seam could never express.
    #
    # This still bites: reinstating the promotion loop reddens both assertions.
    _run_output("sed -i 's/a/b/' a/x.py", "")
    assert g._gt_gateway_deliveries[-1].receipt_state == RECEIPT_DELIVERED
    from groundtruth.runtime.evidence_envelope import RUNTIME_EMITTABLE_RECEIPT_STATES
    assert RECEIPT_ACTED not in RUNTIME_EMITTABLE_RECEIPT_STATES


def test_b33_seal_fault_leaves_zero_bytes(seam, monkeypatch):
    # B-33: the SEAL is the delivery commit and now runs BEFORE the append. If it faults,
    # NO model-facing bytes are appended and NO orphan delivery is recorded (bytes must
    # never ship with no dedup/receipt/chain record). Pre-reorder (append-before-seal) a
    # seal fault left the delta appended with no receipt — this pin bites that ordering.
    monkeypatch.setenv("GT_GATEWAY", "1")

    def _boom(*a, **k):
        raise RuntimeError("forced seal fault")

    monkeypatch.setattr(ad, "seal_delivery", _boom)
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    obs = _run_output("grep -rn run .", grep_out)
    assert obs == grep_out, "seal fault must leave the observation byte-identical (0 GT bytes)"
    assert g._gt_gateway_deliveries == [], "no orphan delivery recorded on a seal fault"


# --------------------------------------------------------------------------- #
# F1 (bounce 2026-07-10): dedup is stamped at the SEAL (delivery commit), never at
# production — a dropped/undelivered fact is DEFERRED, not destroyed (the seam's
# own :8189 deferral law).
# --------------------------------------------------------------------------- #
def test_f1_budget_dropped_fact_reoffered_same_episode(seam, monkeypatch):
    """T2b: a law-8 budget drop must NOT burn the fact's dedup key — the same-stem
    probe later in the SAME episode re-offers and delivers it."""
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    # 1) budget too small -> delta dropped WHOLE, nothing sealed
    monkeypatch.setattr(g, "_GT_GATEWAY_MAX_DELTA", 5)
    dropped = _run_output("grep -rn run .", grep_out)
    assert dropped == grep_out                      # law 8: byte-identical obs
    assert g._gt_gateway_deliveries == []           # nothing sealed
    # 2) budget restored, SAME episode -> the fact is re-offered and delivers
    monkeypatch.setattr(g, "_GT_GATEWAY_MAX_DELTA", 4000)
    redelivered = _run_output("grep -rn run .", grep_out)
    assert redelivered != grep_out, "dropped fact must be re-offered (deferred, not destroyed)"
    assert len(g._gt_gateway_deliveries) == 1       # sealed on the re-offer
    # 3) NOW delivered -> a further repeat in the same episode stays suppressed
    n = len(g._gt_gateway_deliveries)
    again = _run_output("grep -rn run .", grep_out)
    assert again == grep_out and len(g._gt_gateway_deliveries) == n


def test_f1_mutation_production_stamping_bites(seam, monkeypatch):
    """MUTATION: re-introduce production-stamping (stamp every produced key inside
    augment) -> the T2b re-offer invariant BITES. (The seam re-imports gateway.augment
    per call, so patching the module attribute reaches it.)"""
    import groundtruth.runtime.gateway as gwmod
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")

    real_augment = gwmod.augment

    def stamping_augment(event, state):  # the OLD (buggy) production-stamping
        out = real_augment(event, state)
        for a in out:
            state.delivered_keys.add(a.dedup_key)
        return out

    monkeypatch.setattr(gwmod, "augment", stamping_augment)
    monkeypatch.setattr(g, "_GT_GATEWAY_MAX_DELTA", 5)
    _run_output("grep -rn run .", grep_out)         # budget-dropped, but key burned
    monkeypatch.setattr(g, "_GT_GATEWAY_MAX_DELTA", 4000)
    redelivered = _run_output("grep -rn run .", grep_out)
    assert redelivered == grep_out, "mutation reproduces destroy-undelivered (bites T2b)"
    assert g._gt_gateway_deliveries == []


# --------------------------------------------------------------------------- #
# MUTATIONS — proven-biting
# --------------------------------------------------------------------------- #
def test_m1_byte_preservation_mutation_bites(seam, monkeypatch):
    """M1: an append that REWRITES the prior message (prepends the delta) violates
    laws 1/2 — the conformance invariant `on.startswith(off)` then FAILS."""
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "0")
    off = _run_output("grep -rn run .", grep_out)
    g._reset_oracle_state()

    # MUTATION: rewrite the prefix instead of appending a pure suffix.
    def _rewriting_append(out, delta):
        out["output"] = delta + (out.get("output") or "")   # prepend => prefix altered

    monkeypatch.setattr(g, "_gt_gateway_append", _rewriting_append)
    monkeypatch.setenv("GT_GATEWAY", "1")
    on = _run_output("grep -rn run .", grep_out)
    assert on != off
    assert not on.startswith(off)   # the conformance invariant BITES the mutation


def test_m2_second_dedup_owner_mutation_bites(seam, monkeypatch):
    """M2: a SECOND dedup chain at the seam (a local set alongside the episode) breaks
    the single-owner identity AND the episode-boundary — the invariant BITES."""
    # single-owner holds pre-mutation
    assert g._oracle_delivered_hashes is g._EPISODE.delivered_dedup

    # MUTATION: rebind the module alias to a fresh second owner (a local set).
    real = g._oracle_delivered_hashes
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    assert g._oracle_delivered_hashes is not g._EPISODE.delivered_dedup   # single-owner BITES
    # restore is automatic (monkeypatch), the object itself is untouched
    assert real is g._EPISODE.delivered_dedup

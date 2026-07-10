"""W2 seam adapter (groundtruth.runtime.adapters.miniswe) — pure-unit contracts.

Covers mandate (a) normalize/arbitrate/render, (c) episode-boundary dedup law,
(d) TITO ledger (chain + law 8 budget), (g) receipt levels 1-3. The adapter is
pure, so these run with no mini-swe-agent import.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from groundtruth.runtime import gateway as gw
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.evidence_envelope import (
    INFO,
    RECEIPT_ACTED,
    RECEIPT_DELIVERED,
    RECEIPT_REFERENCED,
    VERIFIED,
    WARNING,
    EvidenceEnvelope,
    chain_hash,
)
from groundtruth.runtime.evidence_envelope import validate as validate_env


@pytest.fixture(autouse=True)
def _gateway_on(monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    yield


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


def _env(**kw):
    base = dict(producer="p", fact_id="sym", target="a/x.py",
                evidence_type="def_ref_partition", payload=("def: a/x.py:1",),
                provenance=(("a/x.py", 1),), confidence=0.9, tier=VERIFIED)
    base.update(kw)
    return EvidenceEnvelope.build(**base)


# --------------------------------------------------------------------------- #
# normalize_event
# --------------------------------------------------------------------------- #
def test_normalize_event_classifies_kind():
    ev = ad.normalize_event("grep -rn run .", "a/x.py:1: run", 0, 3)
    assert ev.kind == gw.KIND_SEARCH
    assert ev.command == "grep -rn run ." and ev.action_index == 3
    assert ev.exit_status == 0

    ev2 = ad.normalize_event("pytest tests/x.py", "1 passed", 0, 4)
    assert ev2.kind == gw.KIND_TEST
    assert ad.normalize_event("", "", None, 0).exit_status is None


# --------------------------------------------------------------------------- #
# arbitrate — the <=1 dose, deterministic
# --------------------------------------------------------------------------- #
def test_arbitrate_empty_is_none():
    assert ad.arbitrate([]) is None


def test_arbitrate_picks_highest_severity():
    low = _env(evidence_type="cochange_partner", target="a/c.py", tier=INFO,
               confidence=0.5, payload=("co",), provenance=())
    high = _env(evidence_type="covering_verdict", target="a/v.py", tier=VERIFIED,
                confidence=0.9)
    mid = _env(evidence_type="trace_frame", target="a/t.py", tier=WARNING,
               confidence=0.7)
    assert ad.arbitrate([low, high, mid]) is high
    # deterministic: same set, any order -> same winner
    assert ad.arbitrate([high, mid, low]) is high


def test_arbitrate_ties_break_deterministically():
    a = _env(evidence_type="def_ref_partition", target="a/a.py", fact_id="aa")
    b = _env(evidence_type="def_ref_partition", target="a/b.py", fact_id="bb")
    w1 = ad.arbitrate([a, b])
    w2 = ad.arbitrate([b, a])
    assert w1 is w2  # order-independent tiebreak (dedup_key)


# --------------------------------------------------------------------------- #
# render_envelope — generic + injected def renderer
# --------------------------------------------------------------------------- #
def test_render_generic_tagged_vs_native():
    e = _env(evidence_type="trace_frame", target="a/x.py",
             payload=("deepest in-repo frame: a/x.py:7 in f",),
             provenance=(("a/x.py", 7),))
    tagged = ad.render_envelope(e, native=False)
    assert tagged.startswith('<gt-fact kind="trace_frame">') and tagged.endswith("\n")
    assert "</gt-fact>" in tagged
    native = ad.render_envelope(e, native=True)
    assert "<gt-" not in native  # world-fact voice: tag-free
    assert "deepest in-repo frame" in native


def test_render_reuses_injected_def_renderer():
    e = _env(evidence_type="def_ref_partition")
    calls = {}

    def fake(env):
        calls["seen"] = env
        return "NATIVE_DEF_BYTES"

    out = ad.render_envelope(e, native=True, def_facts_renderer=fake)
    assert out == "NATIVE_DEF_BYTES" and calls["seen"] is e
    # a non-def kind never routes through the injected def renderer
    t = _env(evidence_type="trace_frame")
    assert ad.render_envelope(t, native=True, def_facts_renderer=fake) != "NATIVE_DEF_BYTES"


def test_render_injected_renderer_fault_falls_back():
    e = _env(evidence_type="def_ref_partition")

    def boom(_env):
        raise RuntimeError("x")

    out = ad.render_envelope(e, native=False, def_facts_renderer=boom)
    assert out and "<gt-fact" in out  # generic fallback, never crashes


# --------------------------------------------------------------------------- #
# fits_budget — TITO law 8
# --------------------------------------------------------------------------- #
def test_fits_budget_law8():
    assert ad.fits_budget("abc", max_delta_chars=3) is True
    assert ad.fits_budget("abcd", max_delta_chars=3) is False   # over -> drop whole
    assert ad.fits_budget("", max_delta_chars=100) is False     # empty is not a delivery


# --------------------------------------------------------------------------- #
# seal_delivery — chain + rendered-hash + still-valid copy (TITO law 5/6)
# --------------------------------------------------------------------------- #
def test_seal_delivery_stamps_and_chains():
    e = _env()
    rendered = b"the bytes appended\n"
    sealed, head = ad.seal_delivery(
        e, episode_id="task-1", event_id="7", parent_hash="",
        rendered_bytes=rendered, renderer_id="native")
    assert sealed.receipt_state == RECEIPT_DELIVERED
    assert sealed.rendered_bytes_hash == hashlib.sha256(rendered).hexdigest()
    assert sealed.parent_observation_hash == ""            # genesis
    assert head == chain_hash("", rendered)                # chain advanced
    assert sealed.episode_id == "task-1" and sealed.event_id == "7"
    # the sealed copy is still a law-abiding envelope (identity untouched)
    assert validate_env(sealed) == []
    # dedup identity preserved (metadata does not feed the key)
    assert sealed.dedup_key == e.dedup_key


def test_seal_chain_threads_across_deliveries():
    e1, e2 = _env(target="a/1.py"), _env(target="a/2.py")
    s1, h1 = ad.seal_delivery(e1, episode_id="t", event_id="1", parent_hash="",
                              rendered_bytes=b"one", renderer_id="native")
    s2, h2 = ad.seal_delivery(e2, episode_id="t", event_id="2", parent_hash=h1,
                              rendered_bytes=b"two", renderer_id="native")
    assert s2.parent_observation_hash == h1               # links to prior head
    assert h2 == chain_hash(h1, b"two") and h2 != h1
    assert validate_env(s2) == []


# --------------------------------------------------------------------------- #
# update_receipts — TITO law 7 levels 1->2->3
# --------------------------------------------------------------------------- #
def test_receipt_levels_1_2_3():
    e = _env(target="pkg/users.py", fact_id="get_user")
    s, _ = ad.seal_delivery(e, episode_id="t", event_id="1", parent_hash="",
                            rendered_bytes=b"x", renderer_id="native")
    assert s.receipt_state == RECEIPT_DELIVERED           # level 1 on append

    # level 2: a later message NAMES the delivered entity
    ref = ad.update_receipts([s], observed_text="opening pkg/users.py to read")
    assert ref[0].receipt_state == RECEIPT_REFERENCED

    # level 3: the next action TARGETS it (dominates referenced)
    act = ad.update_receipts(ref, next_action_cmd="sed -i 's/a/b/' pkg/users.py")
    assert act[0].receipt_state == RECEIPT_ACTED

    # monotone: neutral turn never downgrades
    still = ad.update_receipts(act, observed_text="unrelated", next_action_cmd="ls")
    assert still[0].receipt_state == RECEIPT_ACTED


def test_receipt_no_promotion_without_reference():
    e = _env(target="pkg/users.py", fact_id="get_user")
    s, _ = ad.seal_delivery(e, episode_id="t", event_id="1", parent_hash="",
                            rendered_bytes=b"x", renderer_id="native")
    out = ad.update_receipts([s], observed_text="nothing relevant", next_action_cmd="cat foo")
    assert out[0].receipt_state == RECEIPT_DELIVERED      # stays level 1


# --------------------------------------------------------------------------- #
# F2 (bounce 2026-07-10): receipt promotion uses word-boundary tokens + path-aware
# compare — the three executed FP classes must stay DELIVERED.
# --------------------------------------------------------------------------- #
def _sealed(fact_id, target):
    e = _env(fact_id=fact_id, target=target)
    s, _ = ad.seal_delivery(e, episode_id="t", event_id="1", parent_hash="",
                            rendered_bytes=b"x", renderer_id="native")
    return s


def test_f2_no_substring_symbol_inflation_acted():
    """'get' inside 'target' must NOT promote to ACTED (word boundary)."""
    s = _sealed("get", "src/main.py")
    out = ad.update_receipts([s], next_action_cmd="grep -rn target .")
    assert out[0].receipt_state == RECEIPT_DELIVERED


def test_f2_no_basename_collision_acted():
    """Acting on a DIFFERENT util.py (other directory) must NOT promote — path-aware
    compare matches the full relpath, never a bare basename of a pathed target."""
    s = _sealed("helper", "pkg/util.py")
    out = ad.update_receipts([s], next_action_cmd="cat vendor/other/util.py")
    assert out[0].receipt_state == RECEIPT_DELIVERED


def test_f2_no_partial_word_referenced():
    """'run' inside 'running' must NOT promote to REFERENCED (word boundary)."""
    s = _sealed("run", "src/core.py")
    out = ad.update_receipts([s], observed_text="running 3 tests...")
    assert out[0].receipt_state == RECEIPT_DELIVERED


def test_f2_r1_unicode_boundary_no_inflation():
    """R1 (micro-bounce 2026-07-10): the boundary must be unicode-aware — 'café'
    inside the LONGER identifier 'écafé' (and a CJK symbol inside a longer CJK run)
    must NOT promote; an exact unicode-identifier token still does."""
    s = _sealed("café", "src/main.py")
    out = ad.update_receipts([s], observed_text="the écafé helper")
    assert out[0].receipt_state == RECEIPT_DELIVERED      # no unicode inflation
    out = ad.update_receipts([s], observed_text="the café helper")
    assert out[0].receipt_state == RECEIPT_REFERENCED     # exact token still promotes
    s2 = _sealed("变量", "src/main.py")
    out2 = ad.update_receipts([s2], observed_text="新变量 = 1")
    assert out2[0].receipt_state == RECEIPT_DELIVERED     # CJK prefix rejected


def test_f2_r2_relative_prefix_suffix_no_inflation():
    """R2 (micro-bounce 2026-07-10): the '/'-suffix rule holds ONLY for ABSOLUTE
    tokens — a sibling-relative clone (backup/src/a/util.py) must NOT promote; the
    container-absolute true positive (/testbed/...) still does."""
    s = _sealed("helper", "src/a/util.py")
    out = ad.update_receipts([s], next_action_cmd="cat backup/src/a/util.py")
    assert out[0].receipt_state == RECEIPT_DELIVERED      # sibling clone rejected
    out = ad.update_receipts([s], next_action_cmd="cat /testbed/src/a/util.py")
    assert out[0].receipt_state == RECEIPT_ACTED          # absolute TP preserved


def test_f2_true_positives_still_promote():
    """The genuine cases keep promoting: full relpath (or a container-prefixed form)
    in the text -> REFERENCED; a standalone symbol token / relpath in the command
    -> ACTED; a basename-ONLY target may match by basename."""
    # full relpath referenced
    s = _sealed("get_user", "pkg/users.py")
    out = ad.update_receipts([s], observed_text="modified: pkg/users.py")
    assert out[0].receipt_state == RECEIPT_REFERENCED
    # container-prefixed path acted
    out = ad.update_receipts(out, next_action_cmd="sed -i 's/a/b/' /testbed/pkg/users.py")
    assert out[0].receipt_state == RECEIPT_ACTED
    # standalone symbol token referenced
    s2 = _sealed("run", "src/core.py")
    out2 = ad.update_receipts([s2], observed_text="the run helper fails")
    assert out2[0].receipt_state == RECEIPT_REFERENCED
    # basename-only delivered target matches by basename
    s3 = _sealed("helper", "util.py")
    out3 = ad.update_receipts([s3], next_action_cmd="cat lib/util.py")
    assert out3[0].receipt_state == RECEIPT_ACTED


# --------------------------------------------------------------------------- #
# F1 (bounce 2026-07-10): augment READS the dedup chain, never stamps it — the
# SEAL (delivery commit) stamps. Undelivered facts are deferred, not destroyed.
# --------------------------------------------------------------------------- #
def test_f1_augment_reads_never_stamps(tmp_path):
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    ep = EpisodeState(episode_id="ep-1")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path), episode=ep)
    out_txt = "a/x.py:10: run\nb/y.py:20: run"
    first = gw.augment(ad.normalize_event("grep -rn run .", out_txt, 0, 1), st)
    assert first
    assert st.delivered_keys == set()              # production did NOT stamp
    # not yet sealed -> the fact is RE-OFFERED on the next identical probe
    second = gw.augment(ad.normalize_event("grep -rn run .", out_txt, 0, 2), st)
    assert second and second[0].dedup_key == first[0].dedup_key
    # SEAL = the delivery commit -> stamps the chain -> now suppressed
    ad.seal_delivery(second[0], episode_id="ep-1", event_id="2", parent_hash="",
                     rendered_bytes=b"delta", renderer_id="native",
                     dedup_chain=st.delivered_keys)
    assert second[0].dedup_key in st.delivered_keys
    assert gw.augment(ad.normalize_event("grep -rn run .", out_txt, 0, 3), st) == []


# --------------------------------------------------------------------------- #
# episode-boundary law (mandate c): same fact re-delivers in a NEW episode,
# suppressed within the SAME episode.
# ADJUSTED (stamp-at-seal, bounce 2026-07-10): augment no longer stamps at
# production, so the test performs the DELIVERY COMMIT (seal with dedup_chain)
# before asserting same-episode suppression — the real pipeline's semantics.
# --------------------------------------------------------------------------- #
def test_episode_boundary_dedup(tmp_path):
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    ep = EpisodeState(episode_id="ep-1")
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path), episode=ep)
    ev = ad.normalize_event("grep -rn run .", "a/x.py:10: run\nb/y.py:20: run", 0, 1)
    first = gw.augment(ev, st)
    assert first                                          # produced once
    # the delivery COMMIT stamps the chain (stamp-at-seal)
    ad.seal_delivery(first[0], episode_id="ep-1", event_id="1", parent_hash="",
                     rendered_bytes=b"delta", renderer_id="native",
                     dedup_chain=st.delivered_keys)
    assert gw.augment(ad.normalize_event("grep -rn run .", ev.output, 0, 2), st) == []  # same episode -> suppressed

    # a NEW episode/attempt re-sees the fact (dedup key is episode-invariant, the
    # chain/dedup set is per-episode).
    ep.reset_attempt()
    again = gw.augment(ad.normalize_event("grep -rn run .", ev.output, 0, 1), st)
    assert again and again[0].dedup_key == first[0].dedup_key

r"""SM-9c (2026-07-11) — the CROSS-SESSION learned-delivery policy.

The durable per-repo causal-consumption ledger (``xsession_memory``) is wired into the
Gateway: at task-start it is loaded as an EXPLICIT, VERSIONED input (threaded onto
``EpisodeState.xsession_policy``); the Gateway then SUPPRESSES a fact-class this repo
delivered ``>= MIN_SAMPLES`` times and NEVER once consumed, and RANKS-UP a class it
historically acted on. These pins prove, deterministically:

  (A) the store is PURE + DETERMINISTIC + leak-safe (dump byte-stable; merge idempotent;
      poisoned keys dropped on load / merge — leak=0 by construction);
  (B) the learned policy is a deterministic FUNCTION of the store — same store -> same
      output, a DIFFERENT store -> a deterministic change (suppress winner-change);
  (C) FLAG-OFF / EMPTY-POLICY is byte-identical (the same list object is returned untouched);
  (D) end-to-end through ``gateway.augment`` an inert store SUPPRESSES the caller_break dose;
  (E) ``EpisodeState.reset_attempt`` PRESERVES the loaded policy (a per-run input) and it is
      excluded from value identity;
  (F) two required BITING mutations: dropping the determinism-given-inputs guard (ignore the
      passed store) and dropping the leak scrub (skip ``is_registered``) both change behavior.

Windows: run with ``PYTHONIOENCODING=utf-8`` (payloads may carry an em-dash).
Deterministic: synthetic graph.db, no ONNX, no network, no task IDs.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from groundtruth.runtime import episode_state as es
from groundtruth.runtime import gateway as gw
from groundtruth.runtime import xsession_memory as xm
from groundtruth.runtime.evidence_envelope import WARNING, EvidenceEnvelope
from groundtruth.runtime.native_render import contains_test_identity


# --------------------------------------------------------------------------- #
# fixtures — envelopes + a caller graph (reused shape from the SM-2b suite)
# --------------------------------------------------------------------------- #
def _env(evidence_type: str, *, target: str = "svc/users.py", sym: str = "get_user",
         tier: str = WARNING, conf: float = 0.5) -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="p", fact_id=sym, target=target, evidence_type=evidence_type,
        payload=("body line",), provenance=(("app/main.py", 3),),
        confidence=conf, tier=tier)


def _mk_graph(tmp_path, nodes, edges):
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
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
            "confidence) VALUES(?,?,?,'CALLS',?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("source_line", 3),
             e["resolution_method"], e["confidence"]))
    con.commit()
    con.close()
    return db


def _py_caller_graph(tmp_path):
    return _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "svc/users.py"},
        {"id": 2, "name": "caller", "file_path": "app/main.py"},
    ], [{"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import",
         "confidence": 1.0}])


def _edit_ev(rel, before, after):
    return gw.ToolEvent(kind="edit", command=f"str_replace {rel}", output="",
                        changed_files=(rel,), action_index=1,
                        edit_before_after={rel: (before, after)})


def _state(policy=None, **kw):
    return gw.GatewayState(episode=es.EpisodeState(xsession_policy=dict(policy or {})), **kw)


# =========================================================================== #
# (A) the store — pure, deterministic, leak-safe
# =========================================================================== #
def test_sanitize_class_only_admits_registered_classes():
    assert xm.sanitize_class("caller_contract") == "caller_contract"
    assert xm.sanitize_class("def_partition") == "def_partition"
    # a test identity / path / assertion echo is NOT a registered class -> dropped
    assert xm.sanitize_class("tests/test_x.py::test_leak") is None
    assert xm.sanitize_class("def test_secret") is None
    assert xm.sanitize_class("app/main.py") is None
    assert xm.sanitize_class("") is None


def test_canonical_class_bridges_shipped_evidence_types():
    assert xm.canonical_class("caller_break") == "caller_contract"
    assert xm.canonical_class("covering_verdict") == "covering_red"
    assert xm.canonical_class("def_ref_partition") == "def_partition"
    assert xm.canonical_class("bogus_type") is None


def test_repo_key_is_stable_and_prefers_remote():
    a = xm.repo_key({"remote": "https://github.com/o/r.git", "name": "r", "root_name": "r"})
    b = xm.repo_key({"remote": "https://github.com/o/r.git", "name": "x", "root_name": "y"})
    assert a == b and len(a) == 16          # remote wins; deterministic
    assert xm.repo_key({"remote": "", "name": "n", "root_name": "d"}) \
        != xm.repo_key({"remote": "", "name": "m", "root_name": "d"})   # falls back to name
    assert xm.repo_key({}) == ""            # empty identity -> no key (feature disabled)


def test_dump_is_byte_deterministic(tmp_path):
    s = xm.XSessionStore(repo_key="k", class_stats={"def_partition": (4, 3),
                                                    "caller_contract": (5, 0)})
    p = str(tmp_path / "s.json")
    assert xm.dump(s, p)
    b1 = open(p, "rb").read()
    assert xm.dump(s, p)
    b2 = open(p, "rb").read()
    assert b1 == b2                          # same store -> byte-identical file
    # keys sorted, integer counts, NO timestamp/session field in the value identity
    obj = json.loads(b1)
    assert obj == {"schema_version": xm.SCHEMA_VERSION, "repo_key": "k",
                   "class_stats": {"caller_contract": [5, 0], "def_partition": [4, 3]}}


def test_dump_rereads_under_lock_and_preserves_peer_class_update(tmp_path):
    """MUTATION W07-A: replacing without reread loses a concurrent class update."""
    p = str(tmp_path / "s.json")
    first = xm.XSessionStore(repo_key="k", class_stats={"caller_contract": (1, 1)})
    second = xm.XSessionStore(repo_key="k", class_stats={"def_partition": (1, 0)})
    assert xm.dump(first, p)
    assert xm.dump(second, p)
    assert dict(xm.load(p).class_stats) == {
        "caller_contract": (1, 1), "def_partition": (1, 0)
    }


def test_dump_same_snapshot_is_idempotent_after_reread(tmp_path):
    """MUTATION W07-B: summing the reread snapshot double-counts repeated flushes."""
    p = str(tmp_path / "s.json")
    state = xm.XSessionStore(repo_key="k", class_stats={"caller_contract": (1, 1)})
    assert xm.dump(state, p)
    before = open(p, "rb").read()
    assert xm.dump(state, p)
    assert open(p, "rb").read() == before
    assert dict(xm.load(p).class_stats) == {"caller_contract": (1, 1)}


def test_dump_atomic_replace_failure_keeps_previous_bytes(tmp_path, monkeypatch):
    """MUTATION W07-B: writing directly to the target can destroy the last good store."""
    p = str(tmp_path / "s.json")
    old = xm.XSessionStore(repo_key="k", class_stats={"caller_contract": (1, 0)})
    new = xm.XSessionStore(repo_key="k", class_stats={"def_partition": (1, 0)})
    assert xm.dump(old, p)
    before = open(p, "rb").read()
    monkeypatch.setattr(xm.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    assert xm.dump(new, p) is False
    assert open(p, "rb").read() == before


def test_load_round_trips_and_wrong_schema_is_fresh(tmp_path):
    s = xm.XSessionStore(repo_key="k", class_stats={"caller_contract": (5, 0)})
    p = str(tmp_path / "s.json")
    xm.dump(s, p)
    got = xm.load(p)
    assert dict(got.class_stats) == {"caller_contract": (5, 0)} and got.repo_key == "k"
    # unknown schema -> a FRESH empty store (never mis-adapt on a version we do not know)
    (tmp_path / "old.json").write_text(json.dumps(
        {"schema_version": "gt_xsession.v0", "repo_key": "k",
         "class_stats": {"caller_contract": [9, 9]}}))
    assert dict(xm.load(str(tmp_path / "old.json"), repo_key="k").class_stats) == {}
    # missing file -> empty store keyed to the caller's repo_key
    assert xm.load(str(tmp_path / "nope.json"), repo_key="k").repo_key == "k"


def test_merge_is_idempotent_from_a_frozen_base():
    base = xm.XSessionStore(repo_key="k", class_stats={"caller_contract": (5, 0)})
    m1 = xm.merge(base, {"caller_contract": (1, 1)})
    m2 = xm.merge(base, {"caller_contract": (1, 1)})
    assert dict(m1.class_stats) == dict(m2.class_stats) == {"caller_contract": (6, 1)}
    assert dict(base.class_stats) == {"caller_contract": (5, 0)}   # base untouched (pure)


def test_load_and_merge_drop_poisoned_and_malformed_rows(tmp_path):
    poison = {"schema_version": xm.SCHEMA_VERSION, "repo_key": "k", "class_stats": {
        "tests/test_x.py::test_leak": [9, 9],       # test identity -> dropped
        "def test_secret": [9, 9],                  # assertion echo -> dropped
        "caller_contract": [5, "x"],                # malformed value -> dropped
        "def_partition": [4, 3],                    # valid -> kept
        "cochange_prior": [2, 9],                   # consumed>delivered -> clamped to (2,2)
    }}
    p = str(tmp_path / "poison.json")
    open(p, "w").write(json.dumps(poison))
    store = xm.load(p)
    assert dict(store.class_stats) == {"def_partition": (4, 3), "cochange_prior": (2, 2)}
    # merge applies the SAME leak+integrity sanitizer to a session batch
    merged = xm.merge(xm.XSessionStore(repo_key="k"),
                      {"tests/test_x.py::test_leak": (9, 9), "caller_contract": (3, 1)})
    assert dict(merged.class_stats) == {"caller_contract": (3, 1)}


def test_policy_thresholds_are_conservative():
    inert = {"caller_contract": (5, 0)}
    consumed = {"caller_contract": (5, 3)}
    under = {"caller_contract": (2, 0)}          # below MIN_SAMPLES -> no opinion
    assert xm.is_inert(inert, "caller_contract") is True
    assert xm.is_consumed(inert, "caller_contract") is False
    assert xm.is_consumed(consumed, "caller_contract") is True
    assert xm.is_inert(consumed, "caller_contract") is False
    assert xm.is_inert(under, "caller_contract") is False          # not enough evidence
    assert xm.consumption_rate(under, "caller_contract") is None
    assert xm.consumption_rate(consumed, "caller_contract") == 3 / 5
    assert xm.policy_rank(consumed, "caller_contract") == 1
    assert xm.policy_rank(inert, "caller_contract") == 0
    assert xm.is_inert({}, None) is False                          # None-safe


# =========================================================================== #
# (B)/(C) the learned policy — deterministic function of the store; off = no-op
# =========================================================================== #
def test_flag_off_returns_the_same_list_object(monkeypatch):
    monkeypatch.delenv("GT_XSESSION_MEMORY", raising=False)
    st = _state({"caller_contract": (5, 0)})
    out = [_env("caller_break")]
    assert gw._apply_xsession_policy(out, st) is out        # untouched: byte-identical off


def test_empty_policy_returns_the_same_list_object(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    st = _state({})
    out = [_env("caller_break")]
    assert gw._apply_xsession_policy(out, st) is out        # no learning yet -> no-op


def test_inert_class_is_suppressed(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    st = _state({"caller_contract": (5, 0)})
    out = [_env("caller_break"), _env("def_ref_partition", target="a/x.py", sym="run")]
    kept = [e.evidence_type for e in gw._apply_xsession_policy(list(out), st)]
    assert kept == ["def_ref_partition"]                   # caller_break suppressed (inert)


def test_non_policy_class_never_suppressed(monkeypatch):
    """The first slice acts on ONE class (caller_contract). An inert def_partition record is
    NOT in the allow-list -> def_ref_partition survives untouched (bounded blast radius)."""
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    st = _state({"def_partition": (9, 0)})                  # inert, but NOT allow-listed
    out = [_env("def_ref_partition", target="a/x.py", sym="run")]
    assert [e.evidence_type for e in gw._apply_xsession_policy(list(out), st)] \
        == ["def_ref_partition"]


def test_consumed_class_is_ranked_up_not_suppressed(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    st = _state({"caller_contract": (5, 3)})
    out = [_env("def_ref_partition", target="a/x.py", sym="run"), _env("caller_break")]
    kept = [e.evidence_type for e in gw._apply_xsession_policy(list(out), st)]
    assert kept[0] == "caller_break"                       # ranked to the front (consumed)
    assert set(kept) == {"caller_break", "def_ref_partition"}   # nothing suppressed


def test_same_store_is_byte_deterministic(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    pol = {"caller_contract": (5, 0)}
    def _run():
        st = _state(pol)
        out = [_env("caller_break"), _env("def_ref_partition", target="a/x.py", sym="run")]
        return [e.dedup_key for e in gw._apply_xsession_policy(out, st)]
    assert _run() == _run()                                # same (candidates, store) -> same


def test_different_store_changes_delivery_deterministically(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    def _run(pol):
        st = _state(pol)
        out = [_env("caller_break"), _env("def_ref_partition", target="a/x.py", sym="run")]
        return [e.evidence_type for e in gw._apply_xsession_policy(out, st)]
    inert = _run({"caller_contract": (5, 0)})
    consumed = _run({"caller_contract": (5, 3)})
    assert inert != consumed                               # the store CHANGES the delivery
    assert "caller_break" not in inert and "caller_break" in consumed
    assert _run({"caller_contract": (5, 0)}) == inert      # ...deterministically


# =========================================================================== #
# (D) end-to-end through gateway.augment — the winner-change lever
# =========================================================================== #
def test_augment_suppresses_inert_caller_break_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    db = _py_caller_graph(tmp_path)
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a, b):\n    return a\n")
    # baseline (no store) -> caller_break delivered
    base = gw.GatewayState(graph_db=db, repo_root=str(tmp_path), episode=es.EpisodeState())
    assert any(a.evidence_type == "caller_break" for a in gw.augment(ev, base))
    # inert store -> augment SUPPRESSES it (a different dose, or none, now wins)
    inert = gw.GatewayState(graph_db=db, repo_root=str(tmp_path),
                            episode=es.EpisodeState(xsession_policy={"caller_contract": (5, 0)}))
    assert not any(a.evidence_type == "caller_break" for a in gw.augment(ev, inert))
    # consumed store -> delivered (rank-up path keeps it)
    consumed = gw.GatewayState(graph_db=db, repo_root=str(tmp_path),
                               episode=es.EpisodeState(xsession_policy={"caller_contract": (5, 3)}))
    assert any(a.evidence_type == "caller_break" for a in gw.augment(ev, consumed))


def test_augment_flag_off_ignores_the_store_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_XSESSION_MEMORY", raising=False)
    db = _py_caller_graph(tmp_path)
    ev = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                  "def get_user(a, b):\n    return a\n")
    base = [a.evidence_type for a in gw.augment(
        ev, gw.GatewayState(graph_db=db, repo_root=str(tmp_path), episode=es.EpisodeState()))]
    # an inert store present but the FLAG is OFF -> the store is ignored (byte-identical)
    with_store = [a.evidence_type for a in gw.augment(
        ev, gw.GatewayState(graph_db=db, repo_root=str(tmp_path),
                            episode=es.EpisodeState(xsession_policy={"caller_contract": (5, 0)})))]
    assert with_store == base and "caller_break" in base


# =========================================================================== #
# (E) EpisodeState — reset preserves the loaded policy; value identity unchanged
# =========================================================================== #
def test_reset_attempt_preserves_xsession_policy():
    e = es.EpisodeState(episode_id="run", xsession_policy={"caller_contract": (5, 0)})
    e.action_index = 7
    e.probe_ledger["s"] = {"probed_forms": set(), "probe_indices": [], "outcomes": []}
    e.reset_attempt()
    assert e.action_index == 0 and e.probe_ledger == {}        # accumulators cleared
    assert e.xsession_policy == {"caller_contract": (5, 0)}    # the store input PRESERVED
    assert e.episode_id == "run#a1"                            # identity persists (D-11)


def test_xsession_policy_excluded_from_value_identity():
    e1 = es.EpisodeState(episode_id="r", xsession_policy={"caller_contract": (5, 0)})
    e2 = es.EpisodeState(episode_id="r", xsession_policy={"def_partition": (9, 1)})
    assert e1 == e2                                            # compare=False -> not identity
    assert "xsession_policy" not in e1.to_dict()               # not serialized (replay-safe)
    assert es.EpisodeState.from_dict(e1.to_dict()) == e1


# =========================================================================== #
# (F) LEAK PROBE (== 0) + the two required BITING mutations
# =========================================================================== #
def test_leak_probe_poison_never_influences_delivery(tmp_path, monkeypatch):
    """A poisoned prior-session store carries a test identity. It is DROPPED on load, so it
    can never suppress/rank a delivery, and NO delivered byte carries a test identity."""
    poison = {"schema_version": xm.SCHEMA_VERSION, "repo_key": "k", "class_stats": {
        "tests/test_x.py::test_leak": [9, 0], "def test_secret": [9, 0],
        "caller_contract": [5, 3]}}
    p = str(tmp_path / "xsession_k.json")
    open(p, "w").write(json.dumps(poison))
    store = xm.load(p)
    assert set(store.class_stats) == {"caller_contract"}       # poison dropped on load
    assert [k for k in store.class_stats if xm.sanitize_class(k) is None] == []  # leak == 0
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    st = _state(dict(store.class_stats))
    kept = gw._apply_xsession_policy([_env("caller_break")], st)
    leaks = sum(1 for e in kept for line in e.payload if contains_test_identity(line))
    assert leaks == 0                                          # ZERO leak on the delivered surface


def test_mutation_ignoring_the_store_bites(monkeypatch):
    """MUTATION on the DETERMINISM-GIVEN-INPUTS guard: read an EMPTY policy instead of
    ``state.episode.xsession_policy``. Then the output is no longer a function of the store —
    the inert class is NOT suppressed. The real function suppresses; the mutant does not."""
    monkeypatch.setenv("GT_XSESSION_MEMORY", "1")
    st = _state({"caller_contract": (5, 0)})
    out = [_env("caller_break")]
    # REAL: reads the passed store -> suppresses the inert class
    assert gw._apply_xsession_policy(list(out), st) == []

    def _mutant(o):
        # the mutation: ignore state.episode.xsession_policy, evaluate against {} instead
        empty: dict = {}
        keep = []
        for a in o:
            canon = xm.canonical_class(a.evidence_type)
            if canon in gw._XSESSION_POLICY_CLASSES and xm.is_inert(empty, canon):
                continue
            keep.append(a)
        return keep
    assert _mutant(list(out)) == out                          # mutant keeps it -> BITES


def test_mutation_removing_the_leak_scrub_bites():
    """MUTATION on the LEAK SCRUB: drop the ``is_registered`` guard in ``sanitize_class`` so
    it returns the name as-is. Then a poisoned key SURVIVES into the store and could steer a
    delivery. The real scrub drops it; the mutant keeps it."""
    poison = "tests/test_x.py::test_leak"
    assert xm.sanitize_class(poison) is None                  # REAL scrub drops the leak
    mutant = lambda n: (n or "").strip() or None              # is_registered removed
    assert mutant(poison) == poison                           # mutant keeps it -> BITES
    # and the sanitizer used by load/merge drops it while keeping the registered class
    assert xm._sanitize_stats({"caller_contract": [3, 0], poison: [9, 9]}) \
        == {"caller_contract": (3, 0)}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

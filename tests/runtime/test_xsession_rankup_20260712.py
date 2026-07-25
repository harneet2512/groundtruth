r"""SM-9c rank-up -> WINNER (2026-07-12) — the cross-session ladder promotes a delivered dose.

Before this: the SM-9c memory store was SUPPRESS-ONLY at the winner level. A remembered
consumed fact-class could be stable-SORTED to the front of the Gateway's candidate list, but
the adapter's arbiter picks by a TOTAL ORDER (``_priority``), not list position — so a
memory-consumed class could never WIN the ``<= 1``-dose arbitration from the sort alone
(``gateway._apply_xsession_policy`` docstring: "winner promotion ... OWED to the adapter").

This wave closes that: ``xsession_memory.ladder_boost`` derives a tiered boost from the
consumed count; ``gateway._apply_xsession_rankup`` (behind ``GT_XSESSION_RANKUP``) stamps it
onto the already-eligible envelope's ``native_args`` side-car; the adapter's ``_priority`` ADDS
it to the class's severity rank so the consumed class can WIN the dose.

Pins, deterministically (synthetic, no ONNX / network / task IDs):
  (A) ``ladder_boost`` is a MONOTONE consumed-count tier, 0 when under-sampled / inert, and
      CAPPED below the executed-covering-RED severity gap;
  (B) RED-first: two eligible facts where the memory-laddered one LOSES today -> WINS with the
      flag on; MUTATION: zero the ladder (consumed 0) -> reverts;
  (C) BYTE-IDENTICAL-OFF: flag off / empty policy / non-policy class -> the SAME list object,
      an unstamped ``_priority`` unchanged;
  (D) ``<= 1``-dose invariant: arbitrate returns exactly ONE (the boost changes WHICH, never
      adds a dose); the cap keeps a learned fact from out-dosing the executed covering RED;
  (E) two BITING mutations: drop the ``_priority`` boost-add (adapter) and ignore the store
      (gateway) both erase the flip.

Windows: run with ``PYTHONIOENCODING=utf-8``.
"""
from __future__ import annotations

import pytest

from groundtruth.runtime import episode_state as es
from groundtruth.runtime import gateway as gw
from groundtruth.runtime import xsession_memory as xm
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import WARNING, VERIFIED, EvidenceEnvelope


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _env(evidence_type: str, *, target: str = "svc/users.py", sym: str = "get_user",
         tier: str = WARNING, conf: float = 0.5) -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="p", fact_id=sym, target=target, evidence_type=evidence_type,
        payload=("body line",), provenance=(("app/main.py", 3),),
        confidence=conf, tier=tier)


def _state(policy=None):
    return gw.GatewayState(episode=es.EpisodeState(xsession_policy=dict(policy or {})))


def _boosted(policy):
    """caller_break stamped by the REAL gateway rank-up path against ``policy`` (flag ON)."""
    st = _state(policy)
    return gw._apply_xsession_rankup([_env("caller_break")], st)[0]


# =========================================================================== #
# (A) ladder_boost — monotone consumed-count tier, 0 on weak evidence, capped
# =========================================================================== #
def test_ladder_boost_is_a_monotone_consumed_count_tier():
    # under-sampled (delivered < MIN_SAMPLES) or inert (consumed 0) -> NO boost.
    assert xm.ladder_boost({"caller_contract": (2, 2)}, "caller_contract") == 0   # < MIN_SAMPLES
    assert xm.ladder_boost({"caller_contract": (5, 0)}, "caller_contract") == 0   # inert
    # tiers rise with the consumed count, monotone.
    b_lo = xm.ladder_boost({"caller_contract": (5, 1)}, "caller_contract")        # 1-2
    b_mid = xm.ladder_boost({"caller_contract": (5, 3)}, "caller_contract")       # 3-5
    b_hi = xm.ladder_boost({"caller_contract": (9, 6)}, "caller_contract")        # 6+
    assert 0 < b_lo < b_mid < b_hi
    assert xm.ladder_boost({}, None) == 0                                         # None-safe


def test_ladder_boost_capped_below_executed_covering_red_gap():
    """The max boost must be strictly LESS than the adapter's severity gap from caller_break
    to the executed covering RED, so a learned rank-up never out-doses the repo's own RED."""
    gap = ad._EVIDENCE_TYPE_RANK["covering_verdict"] - ad._EVIDENCE_TYPE_RANK["caller_break"]
    huge = xm.ladder_boost({"caller_contract": (100, 100)}, "caller_contract")
    assert 0 < huge < gap                                                        # 11 < 12


# =========================================================================== #
# (B) RED-first: the memory-laddered fact LOSES today, WINS with the flag on
# =========================================================================== #
def test_consumed_caller_break_wins_over_higher_severity_with_flag(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    # signature_mismatch (severity 50) out-doses caller_break (48) by default.
    sig = _env("signature_mismatch", target="app/main.py", sym="get_user")
    # consumed store -> caller_break earns a boost > 2, so 48 + boost > 50.
    caller = _boosted({"caller_contract": (5, 3)})
    assert ad.arbitrate([sig, caller]) is caller                                 # ladder flips it


def test_baseline_without_boost_the_higher_severity_wins(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    sig = _env("signature_mismatch", target="app/main.py", sym="get_user")
    # INERT store (consumed 0) -> ladder_boost 0 -> no stamp -> baseline arbitration.
    caller = _boosted({"caller_contract": (5, 0)})
    assert ad.arbitrate([sig, caller]) is sig                                    # 48 < 50


def test_mutation_zeroing_the_ladder_reverts_the_winner(monkeypatch):
    """MUTATION: the SAME two facts, but the store shows 0 consumptions -> boost 0 -> the
    higher-severity fact wins again. Proves the boost magnitude is load-bearing (ladder-derived)."""
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    sig = _env("signature_mismatch", target="app/main.py", sym="get_user")
    assert ad.arbitrate([sig, _boosted({"caller_contract": (5, 3)})]).evidence_type == "caller_break"
    assert ad.arbitrate([sig, _boosted({"caller_contract": (5, 0)})]).evidence_type == "signature_mismatch"


# =========================================================================== #
# (C) BYTE-IDENTICAL-OFF
# =========================================================================== #
def test_rankup_flag_off_returns_same_list_object(monkeypatch):
    monkeypatch.delenv("GT_XSESSION_RANKUP", raising=False)
    st = _state({"caller_contract": (5, 3)})
    out = [_env("caller_break")]
    assert gw._apply_xsession_rankup(out, st) is out                            # untouched


def test_rankup_empty_policy_returns_same_list_object(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    out = [_env("caller_break")]
    assert gw._apply_xsession_rankup(out, _state({})) is out                    # no learning -> no-op


def test_rankup_non_policy_class_not_stamped_same_object(monkeypatch):
    """The first slice acts on ONE class (caller_contract). A consumed record for it does NOT
    stamp an unrelated def_ref_partition candidate -> the list is returned UNCHANGED."""
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    st = _state({"caller_contract": (5, 3)})
    out = [_env("def_ref_partition", target="a/x.py", sym="run")]
    assert gw._apply_xsession_rankup(out, st) is out                            # nothing to boost


def test_priority_is_byte_identical_without_native_args():
    """An unstamped envelope (native_args None) -> _xsession_boost 0 -> _priority unchanged."""
    e = _env("caller_break")
    assert e.native_args is None
    assert ad._xsession_boost(e) == 0
    # 2026-07-25: _priority gained a LEADING boundary-specificity element (GT #29). It is
    # constantly 0 without an ``observed_event``, so this test's invariant — an unstamped envelope
    # ranks exactly as before — is UNCHANGED; only the tuple arity moved. Asserting the full tuple
    # (rather than slicing it) is deliberate: it keeps this a byte-identity check, so any future
    # element added to the ordering must be justified here too.
    assert ad._priority(e) == (0,
                               ad._EVIDENCE_TYPE_RANK["caller_break"],
                               ad._TIER_RANK[WARNING], 0.5, e.dedup_key)


def test_stamp_is_identity_neutral(monkeypatch):
    """The boost rides native_args (compare=False, unserialized): dedup_key / == unchanged."""
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    base = _env("caller_break")
    boosted = _boosted({"caller_contract": (5, 3)})
    assert boosted.native_args == {"_xsession_boost": 7}
    assert boosted.dedup_key == base.dedup_key                                   # identity unmoved
    assert boosted == base                                                       # compare=False


# =========================================================================== #
# (D) <=1-dose invariant + the executed-RED cap
# =========================================================================== #
def test_arbitrate_still_returns_exactly_one_dose(monkeypatch):
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    winner = ad.arbitrate([_env("signature_mismatch", target="app/main.py"),
                           _boosted({"caller_contract": (9, 6)})])
    assert isinstance(winner, EvidenceEnvelope)                                  # ONE, not a list
    assert ad.arbitrate([]) is None                                             # empty -> None


def test_boosted_caller_still_loses_to_executed_covering_red(monkeypatch):
    """Even a maximally-consumed caller_break must NOT out-dose the repo's OWN executed RED
    (covering_verdict severity 60). The cap enforces the doctrine that the executed world-fact
    wins."""
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    covering = _env("covering_verdict", target="svc/users.py", sym="get_user", tier=VERIFIED,
                    conf=0.9)
    caller = _boosted({"caller_contract": (100, 100)})
    assert ad.arbitrate([covering, caller]) is covering


# =========================================================================== #
# (E) two BITING mutations — the adapter add + the gateway store read
# =========================================================================== #
def test_mutation_dropping_priority_boost_add_bites(monkeypatch):
    """MUTATION on the ADAPTER: _priority ignores the stamped boost (as before this wave). Then
    the boosted caller_break no longer wins -> the flip disappears. The real add promotes it."""
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    sig = _env("signature_mismatch", target="app/main.py", sym="get_user")
    caller = _boosted({"caller_contract": (5, 3)})
    assert ad.arbitrate([sig, caller]) is caller                                # REAL: caller wins
    monkeypatch.setattr(ad, "_xsession_boost", lambda env: 0)                   # drop the add
    assert ad.arbitrate([sig, caller]) is sig                                   # MUTANT: reverts


def test_mutation_gateway_ignoring_the_store_bites(monkeypatch):
    """MUTATION on the GATEWAY: read an EMPTY policy instead of state.episode.xsession_policy.
    Then no boost is stamped -> the caller_break envelope has native_args None -> no flip."""
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    st = _state({"caller_contract": (5, 3)})
    real = gw._apply_xsession_rankup([_env("caller_break")], st)[0]
    assert real.native_args == {"_xsession_boost": 7}                          # REAL: stamped

    def _mutant(o):
        # the mutation: ignore state.episode.xsession_policy, evaluate against {} instead.
        import dataclasses
        empty: dict = {}
        out = []
        for a in o:
            canon = xm.canonical_class(a.evidence_type)
            boost = xm.ladder_boost(empty, canon) if canon in gw._XSESSION_POLICY_CLASSES else 0
            if boost > 0:
                na = dict(a.native_args or {})
                na["_xsession_boost"] = int(boost)
                out.append(dataclasses.replace(a, native_args=na))
            else:
                out.append(a)
        return out
    assert _mutant([_env("caller_break")])[0].native_args is None              # MUTANT: no stamp


# =========================================================================== #
# (F) end-to-end through gateway.augment — the winner-change lever, live
# =========================================================================== #
def _py_two_caller_graph(tmp_path):
    import sqlite3
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,"
        " file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,"
        " is_exported INTEGER, is_test INTEGER, language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(1,'Function','get_user','svc/users.py',1,5,0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
                " VALUES(2,'Function','caller','app/main.py',1,5,0,'python')")
    con.execute("INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(1,2,1,'CALLS',3,'import',1.0)")
    con.commit()
    con.close()
    return db


def test_augment_end_to_end_stamps_boost_on_consumed_caller_break(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_XSESSION_RANKUP", "1")
    db = _py_two_caller_graph(tmp_path)
    ev = gw.ToolEvent(kind="edit", command="str_replace svc/users.py", output="",
                      changed_files=("svc/users.py",), action_index=1,
                      edit_before_after={"svc/users.py": (
                          "def get_user(a):\n    return a\n",
                          "def get_user(a, b):\n    return a\n")})
    consumed = gw.GatewayState(
        graph_db=db, repo_root=str(tmp_path),
        episode=es.EpisodeState(xsession_policy={"caller_contract": (5, 3)}))
    cbs = [a for a in gw.augment(ev, consumed) if a.evidence_type == "caller_break"]
    assert cbs and cbs[0].native_args["_xsession_boost"] == 7                  # live stamp
    assert cbs[0].native_args["before_parameters"] == ("a",)
    assert cbs[0].native_args["after_parameters"] == ("a", "b")


def test_augment_flag_off_no_boost_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_XSESSION_RANKUP", raising=False)
    db = _py_two_caller_graph(tmp_path)
    ev = gw.ToolEvent(kind="edit", command="str_replace svc/users.py", output="",
                      changed_files=("svc/users.py",), action_index=1,
                      edit_before_after={"svc/users.py": (
                          "def get_user(a):\n    return a\n",
                          "def get_user(a, b):\n    return a\n")})
    st = gw.GatewayState(
        graph_db=db, repo_root=str(tmp_path),
        episode=es.EpisodeState(xsession_policy={"caller_contract": (5, 3)}))
    cbs = [a for a in gw.augment(ev, st) if a.evidence_type == "caller_break"]
    assert cbs and "_xsession_boost" not in cbs[0].native_args                 # flag off -> unstamped


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

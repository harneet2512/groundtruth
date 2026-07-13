"""SS-8 — the shadow-holdout chokepoint consult at the mini seam (gt-math E10).

The seam helper ``gt_mini_patch._ss_shadow_withheld`` is consulted AFTER a fact won arbitration
and passed every SS screen (it WOULD deliver). On HOLDOUT it delivers ZERO model bytes and writes
a ``shadow_holdout`` runtime-ledger row carrying the withheld render's HASH (never the payload).

RED-first: on a tree without the helper / without ``shadow_holdout`` these tests error. Biting
mutations are named per test. Nothing task-specific — synthetic kinds/text only.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def _base(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_action_count", 7)
    for k in ("GT_SS_SHADOW", "GT_SS_SHADOW_RATE", "GT_SS_SHADOW_SEED"):
        monkeypatch.delenv(k, raising=False)
    ledger = tmp_path / "gt_runtime_ledger_repo__demo-1.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    return ledger


def _capture(monkeypatch):
    recs: list = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **k: recs.append(k))
    return recs


# --------------------------------------------------------------------------- #
# flag-off / rate-0 byte-identity: the helper never withholds and never records
# --------------------------------------------------------------------------- #
def test_lane_a_withheld_arm_spends_content_hash_exactly_one_shadow_row(monkeypatch, tmp_path):
    """ORCHESTRATOR LIPI PIN (chokepoint-level, not helper-level): the Lane-A withheld arm must
    spend the SAME content hashes as the deliver arm, so a withheld fact (a) never re-competes on
    a later turn even across a behavioral-state change, and (b) yields exactly ONE shadow row per
    distinct content — the analyzer pairs one withheld instance against one delivered instance;
    a duplicate draw double-counts the holdout arm and skews every per-class E10 contrast.
    MUTATION[drop the _oracle_delivered_hashes.add(h)/.add(hc) spend at the Lane-A shadow
    chokepoint] -> the second send re-reaches the consult -> a second shadow row -> RED."""
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *a, **k: None, raising=False)
    recs = _capture(monkeypatch)
    block = ("l3.contract", "<gt-contract>def f(x: int) -> int</gt-contract>")

    monkeypatch.setattr(g, "_oracle_bstate", lambda: "turn=1|edits=1")
    o1: dict = {}
    g._lane_a_deliver(o1, "cmd", [block], krel="f.py", event=None)
    assert (o1.get("output") or "") == ""  # withheld: zero model bytes

    # state changed — the withheld fact must NOT re-compete (content-only hash was spent)
    monkeypatch.setattr(g, "_oracle_bstate", lambda: "turn=9|edits=9")
    o2: dict = {}
    g._lane_a_deliver(o2, "cmd", [block], krel="f.py", event=None)
    assert (o2.get("output") or "") == ""  # still zero model bytes

    shadow = [r for r in recs if r.get("outcome") == "shadow_holdout"]
    assert len(shadow) == 1, f"expected exactly ONE shadow row, got {len(shadow)}"


def test_flag_off_never_withholds(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    recs = _capture(monkeypatch)
    # GT_SS_SHADOW unset -> deliver, no shadow row (byte-identical path).
    assert g._ss_shadow_withheld("l3.contract", "c:deadbeef", "SOME CONTRACT TEXT") is False
    assert recs == []
    # MUTATION[make _ss_shadow_on() default true] -> a row is recorded off-flag -> RED.


def test_rate_zero_never_withholds_even_with_flag_on(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    # GT_SS_SHADOW_RATE unset -> defaults "0" -> never withhold (production-safe).
    recs = _capture(monkeypatch)
    for k in ("k1", "k2", "k3", "k4", "k5"):
        assert g._ss_shadow_withheld("l3.contract", k, "TEXT") is False
    assert recs == []
    # MUTATION[make _ss_shadow_rate() default "1"] -> rows recorded at rate 0 default -> RED.


def test_empty_text_is_never_withheld(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    recs = _capture(monkeypatch)
    assert g._ss_shadow_withheld("l3.contract", "k", "") is False
    assert recs == []


# --------------------------------------------------------------------------- #
# rate=1 + participating class -> WITHHELD (True) with a correct captured envelope
# --------------------------------------------------------------------------- #
def test_participating_class_rate1_withheld_with_metadata(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    recs = _capture(monkeypatch)
    text = "MUST preserve caller contract of write()"
    assert g._ss_shadow_withheld("l3.contract", "c:abc123", text,
                                 file_path="src/mod.py", event=None) is True
    assert len(recs) == 1
    row = recs[0]
    assert row["outcome"] == "shadow_holdout"
    assert row["kind"] == "l3.contract"
    assert row["chars"] == 0                       # ZERO model bytes delivered
    assert row["content"] == text                  # sealed as a HASH downstream (see leak test)
    assert row["file_path"] == "src/mod.py"
    extra = row["extra"]
    assert extra["chars_would"] == len(text)
    assert extra["dedup_key"] == "c:abc123"
    assert extra["fact_class"] == "caller_contract"   # canonicalized from the seam kind
    assert extra["shadow_rate"] == "1"
    # MUTATION[record chars=len(text) instead of 0] -> chars!=0 -> RED (a holdout ships 0 bytes).


def test_safety_class_never_withheld_at_rate1(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    recs = _capture(monkeypatch)
    # edit.syntax -> syntax_result (cardinal); submit_gate -> submit_refusal (cardinal).
    assert g._ss_shadow_withheld("edit.syntax", "k", "syntax verdict text") is False
    assert g._ss_shadow_withheld("submit_gate", "k", "submit refusal text") is False
    assert recs == []
    # MUTATION[allowlist -> denylist in shadow_holdout.assign] -> a safety fact is withheld -> RED.


def test_determinism_same_inputs_same_decision(monkeypatch, tmp_path):
    _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "0.5")
    _capture(monkeypatch)
    first = g._ss_shadow_withheld("l3.contract", "c:stable", "text")
    for _ in range(50):
        assert g._ss_shadow_withheld("l3.contract", "c:stable", "text") == first


# --------------------------------------------------------------------------- #
# durable ledger row: leak-safe (hash only), correct schema, chars_delivered=0
# --------------------------------------------------------------------------- #
def test_durable_shadow_row_is_leak_free_and_sealed(monkeypatch, tmp_path):
    ledger = _base(monkeypatch, tmp_path)
    monkeypatch.setenv("GT_SS_SHADOW", "1")
    monkeypatch.setenv("GT_SS_SHADOW_RATE", "1")
    # use the REAL _runtime_ledger_record (no capture) so the durable line is exercised.
    secret = "WITHHELD-RENDER-caller-line-42-should-never-appear-verbatim"
    assert g._ss_shadow_withheld("l3b.evidence", "c:zz", secret, file_path="p/q.py") is True
    raw = ledger.read_text(encoding="utf-8")
    # leak-safe: the withheld render's bytes are NEVER in the durable row.
    assert secret not in raw
    row = json.loads([l for l in raw.splitlines() if l.strip()][-1])
    assert row["outcome"] == "shadow_holdout"
    assert row["chars_delivered"] == 0
    assert row["iteration"] == 7                     # from _action_count
    # the seal is the sha256[:16] of the withheld render (host-side audit join), hash ONLY.
    assert row["content_sha256_16"] == hashlib.sha256(
        secret.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    assert row["fact_class"] == "caller_contract"    # l3b.evidence canonicalizes here
    assert row["chars_would"] == len(secret)
    assert row["dedup_key"] == "c:zz"
    # MUTATION[render content=... into out['output'] / put `secret` in the row] -> leak -> RED.


def test_runtime_ledger_record_extra_is_byte_identical_when_absent(monkeypatch, tmp_path):
    ledger = _base(monkeypatch, tmp_path)
    # a normal delivered row (no extra) must be schema-identical to the pre-SS-8 shape:
    g._runtime_ledger_record(kind="l3.contract", outcome=g._ProductSignalOutcome.DELIVERED,
                             chars=10, file_path="x.py", content="hello")
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    # no SS-8 side-car keys leak into a non-shadow row.
    for k in ("chars_would", "dedup_key", "fact_class", "shadow_rate"):
        assert k not in row
    assert row["outcome"] == "delivered" and row["chars_delivered"] == 10
    # MUTATION[merge extra unconditionally / not None-guarded] -> stray keys appear -> RED.


def test_extra_cannot_clobber_canonical_schema_keys(monkeypatch, tmp_path):
    ledger = _base(monkeypatch, tmp_path)
    # a hostile extra tries to overwrite canonical fields -> setdefault must protect them.
    g._runtime_ledger_record(kind="l3.contract", outcome="delivered", chars=5,
                             extra={"outcome": "HACKED", "chars_delivered": 999,
                                    "new_side_car": "ok"})
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert row["outcome"] == "delivered"       # NOT clobbered
    assert row["chars_delivered"] == 5         # NOT clobbered
    assert row["new_side_car"] == "ok"         # a genuinely-new key is added
    # MUTATION[use dict.update instead of setdefault] -> outcome=='HACKED' -> RED.

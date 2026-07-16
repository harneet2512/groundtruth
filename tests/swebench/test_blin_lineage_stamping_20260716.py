"""B-LIN (2026-07-16, REV2): typed feature-lineage on the l3.contract / l3b.evidence
DELIVERED runtime-ledger rows — with PER-PAYLOAD classification for the polymorphic
l3b.evidence lane.

DEFECT (census byteproof_mediator_census.md): the legacy caller-contract data plane emitted
sealed DELIVERED rows with NO typed lineage, so `gt_feature_metrics._fact_delivery_byte_proven`
could never accept them.

FIX:
* l3.contract is PURE caller_contract (`_graph_contract_block`) -> always stamp
  `("contract_map","caller_contract")`.
* l3b.evidence is POLYMORPHIC (`_evidence_body`: post_edit leads with CALLEE contracts;
  post_view can carry [SIBLINGS] (no registered class) + callee-direction [WITNESS]). It is
  stamped `caller_contract` ONLY when the payload is classified at COMPOSITION time as
  EXCLUSIVELY caller-direction contract evidence (`_l3b_last_pure_caller` /
  `_l3b_pure_caller_hashes`); mixed/callee/sibling payloads stay honestly untyped
  (correct-or-quiet). The classifier reads WHICH builders contributed, never the rendered bytes.

Tests drive the REAL classifier (`_evidence_body`), the REAL producer (`_evidence`), the REAL
delivery writer (`_lane_a_deliver`), and the REAL reader (`_fact_delivery_byte_proven`).
"""

from __future__ import annotations

import glob
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench", ROOT / "artifact_deepswe"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gt_mini_patch as gmp  # noqa: E402
import gt_feature_metrics as metrics  # noqa: E402

LINEAGE_SCHEMA = "gt.feature_lineage.v1"
_SAVED_LEDGER = (
    "D:/gt_runs/smoke30_ss128_20260716/ll-full-aiogram__aiogram-1594/"
    "gt_runtime_ledger_aiogram__aiogram-1594.jsonl"
)

_L3_CONTRACT_TEXT = (
    "\n[SIGNATURE] compute(a, b)\n"
    "[CALLERS] 3 verified caller(s) in 2 files - preserve this interface"
)


def _witness(direction):
    return {
        "direction": direction, "target": "compute", "symbol": "consumer",
        "line": 42, "file_path": "src/consumer.py", "code": "compute(a, b)",
    }


# The session may run the full Profile-2 flag stack. B-LIN lineage stamping is host-side and
# orthogonal to the delivery-FORM / SS-gate flags; disable those so the delivery path is
# deterministic (tag form, no shadow-holdout / provenance / novelty withholding). The lineage
# classifier + gate under test are unaffected by these flags.
def _isolate_delivery_flags(monkeypatch):
    for flag in (
        "GT_EVIDENCE_NATIVE", "GT_SS_SHADOW", "GT_SS_PROVENANCE", "GT_SS_NOVELTY",
        "GT_SS_DEDUP2", "GT_SS_LATE_DROP", "GT_LANE_ENVELOPE",
    ):
        monkeypatch.setenv(flag, "0")


# ---------------------------------------------------------------------------
# harness A: drive the REAL composition classifier (_evidence_body) with stubbed
# builders, exercising the exact flag-tracking control flow.
# ---------------------------------------------------------------------------
def _run_classifier(
    monkeypatch, kind, *, callee_contracts=(), witnesses=(), callers="", siblings=""
):
    dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    dbf.write(b"x")
    dbf.close()
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_db_path", lambda: dbf.name)
    monkeypatch.setattr(gmp, "_connect_ro", lambda db: type("C", (), {"close": lambda s: None})())
    monkeypatch.setattr(gmp, "_resolve_frame", lambda con, rel, root: (rel, root))
    monkeypatch.setattr(gmp, "_is_delivery_excluded", lambda rel, root: False)
    monkeypatch.setattr(gmp, "_is_test_or_demo_path", lambda rel: False)
    monkeypatch.setattr(gmp, "_top_func_names", lambda con, f, limit=3: ["compute"])
    monkeypatch.setattr(gmp, "_funcs_enclosing_edited", lambda con, f, cmd: ["compute"])
    monkeypatch.setattr(
        gmp, "_edit_target_callee_contracts",
        lambda con, f, fn, repo_root="": list(callee_contracts),
    )
    monkeypatch.setattr(
        gmp, "_resolved_witnesses_for_file",
        lambda con, f, cr, max_each=2: list(witnesses),
    )
    monkeypatch.setattr(gmp, "_caller_contract_for_file", lambda con, f, cr, fn: callers)
    monkeypatch.setattr(gmp, "_sibling_context", lambda con, f, fn: siblings)
    body = gmp._evidence_body(kind, "src/foo.py", "/root", "sed -i s/a/b/ src/foo.py")
    return gmp._l3b_last_pure_caller, body


# ---------------------------------------------------------------------------
# harness B: drive the REAL producer (_evidence, which records the verdict) then the
# REAL delivery writer (_lane_a_deliver); return the delivered row + the payload text.
# ---------------------------------------------------------------------------
def _drive_evidence_and_deliver(monkeypatch, tmp_path, kind, event, **composition):
    _isolate_delivery_flags(monkeypatch)
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_action_count", 1)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_to_repo_rel", lambda f, root: f)
    # _classify returns the OBSERVATION kind (post_view/post_edit == ``event``), which drives
    # _evidence_body's composition. The lane layer is always "l3b.evidence".
    monkeypatch.setattr(gmp, "_classify", lambda cmd: (event, "src/foo.py"))
    monkeypatch.setattr(gmp, "_seen", set())
    monkeypatch.setattr(gmp, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(gmp, "_contract_seen", set())
    monkeypatch.setattr(gmp, "_l3b_pure_caller_hashes", set())
    # Neutralize the stateful cross-turn budget dedup (not under test; its module-global
    # _PRODUCT_BUDGETER otherwise trims lines shared across isolated cases to empty).
    monkeypatch.setattr(gmp, "_budget_trim", lambda x: x)
    dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    dbf.write(b"x")
    dbf.close()
    monkeypatch.setattr(gmp, "_db_path", lambda: dbf.name)
    monkeypatch.setattr(gmp, "_connect_ro", lambda db: type("C", (), {"close": lambda s: None})())
    monkeypatch.setattr(gmp, "_resolve_frame", lambda con, rel, root: (rel, root))
    monkeypatch.setattr(gmp, "_is_delivery_excluded", lambda rel, root: False)
    monkeypatch.setattr(gmp, "_is_test_or_demo_path", lambda rel: False)
    monkeypatch.setattr(gmp, "_top_func_names", lambda con, f, limit=3: ["compute"])
    monkeypatch.setattr(gmp, "_funcs_enclosing_edited", lambda con, f, cmd: ["compute"])
    monkeypatch.setattr(
        gmp, "_edit_target_callee_contracts",
        lambda con, f, fn, repo_root="": list(composition.get("callee_contracts", ())),
    )
    monkeypatch.setattr(
        gmp, "_resolved_witnesses_for_file",
        lambda con, f, cr, max_each=2: list(composition.get("witnesses", ())),
    )
    monkeypatch.setattr(
        gmp, "_caller_contract_for_file",
        lambda con, f, cr, fn: composition.get("callers", ""),
    )
    monkeypatch.setattr(gmp, "_sibling_context", lambda con, f, fn: composition.get("siblings", ""))

    text = gmp._evidence("sed -i s/a/b/ src/foo.py")
    assert text, "producer emitted nothing"

    ledger = tmp_path / "led.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    out = {"output": ""}
    gmp._lane_a_deliver(
        out, "sed -i s/a/b/ src/foo.py",
        [(kind, text, "src/foo.py")], krel="src/foo.py", event=event,
    )
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    delivered = [r for r in rows if r.get("layer") == kind and r.get("outcome") == "delivered"]
    assert delivered, f"no delivered {kind} row: {rows}"
    return delivered[-1], text


def _seal_join_ledger(row):
    return {
        "entries": [
            {
                "source": "trajectory", "joined": True, "join_method": "seal",
                "content_sha256_16": row.get("content_sha256_16"),
                "ledger_chars": row.get("chars_delivered"),
                "ledger_layer": row.get("layer"),
                "msg_index": 7, "receipt": 1,
            }
        ]
    }


# ---------------------------------------------------------------------------
# harness C: drive the REAL delivery writer directly for l3.contract (pure lane).
# ---------------------------------------------------------------------------
def _drive_lane_delivery(kind, text, subject, event, tmp_path, monkeypatch):
    _isolate_delivery_flags(monkeypatch)
    ledger = tmp_path / f"gt_runtime_ledger_{kind}.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_action_count", 1)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(gmp, "_seen", set())
    monkeypatch.setattr(gmp, "_contract_seen", set())
    out = {"output": ""}
    gmp._lane_a_deliver(
        out, "sed -i s/a/b/ src/foo.py",
        [(kind, text, subject)], krel=subject, event=event,
    )
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    delivered = [r for r in rows if r.get("layer") == kind and r.get("outcome") == "delivered"]
    assert delivered, f"no delivered {kind} row: {rows}"
    return delivered[-1], out["output"]


# ===========================================================================
# GROUP 1 — the REAL composition classifier (correct-or-quiet)
# ===========================================================================
def test_classifier_pure_caller_is_pure(monkeypatch):
    pure, body = _run_classifier(
        monkeypatch, "post_view",
        witnesses=[_witness("caller")], callers="compute() in src/consumer.py:42",
    )
    assert pure is True and body


def test_classifier_callee_primary_postedit_not_pure(monkeypatch):
    pure, _ = _run_classifier(
        monkeypatch, "post_edit",
        callee_contracts=["[CALLEE] helper() src/h.py:3 def helper(x)"],
    )
    assert pure is False


def test_classifier_siblings_not_pure(monkeypatch):
    pure, _ = _run_classifier(
        monkeypatch, "post_view",
        witnesses=[_witness("caller")], callers="compute() in x:1", siblings="foo(), bar()",
    )
    assert pure is False


def test_classifier_mixed_caller_callee_not_pure(monkeypatch):
    pure, _ = _run_classifier(
        monkeypatch, "post_view",
        witnesses=[_witness("caller"), _witness("callee")], callers="compute() in x:1",
    )
    assert pure is False


def test_classifier_callee_witness_only_not_pure(monkeypatch):
    pure, _ = _run_classifier(monkeypatch, "post_view", witnesses=[_witness("callee")])
    assert pure is False


# ===========================================================================
# GROUP 2 — end-to-end: producer -> delivery gate -> reader byte-proof
# ===========================================================================
def test_pure_l3b_payload_byte_proves_caller_contract(tmp_path, monkeypatch):
    row, text = _drive_evidence_and_deliver(
        monkeypatch, tmp_path, "l3b.evidence", "post_view",
        witnesses=[_witness("caller")], callers="compute() in src/consumer.py:42",
    )
    assert row.get("lineage_schema") == LINEAGE_SCHEMA
    assert row.get("fact_class") == "caller_contract"
    assert row.get("runtime_producer_id") == "contract_map"
    assert row.get("producer_registration_match") is True
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is True


@pytest.mark.parametrize(
    "label, kind, event, composition",
    [
        # post_edit carrying a CALLEE-direction witness (def/usage fact, not caller_contract)
        ("callee_direction_postedit", "l3b.evidence", "post_edit",
         {"witnesses": [_witness("callee")]}),
        # post_view carrying [SIBLINGS] (no registered fact class)
        ("siblings", "l3b.evidence", "post_view",
         {"witnesses": [_witness("caller")], "callers": "compute() in x:1",
          "siblings": "foo(), bar()"}),
        # post_view mixing caller + callee witnesses
        ("mixed", "l3b.evidence", "post_view",
         {"witnesses": [_witness("caller"), _witness("callee")], "callers": "compute() in x:1"}),
    ],
)
def test_nonpure_l3b_payload_stays_untyped(label, kind, event, composition, tmp_path, monkeypatch):
    row, text = _drive_evidence_and_deliver(monkeypatch, tmp_path, kind, event, **composition)
    assert row.get("lineage_schema") is None, f"{label} was wrongly typed"
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is False


def test_mutation_blanket_stamp_would_falsely_byte_prove(tmp_path, monkeypatch):
    """MUT (biting): flipping the classifier to blanket-stamp mixed/sibling/callee content as
    pure would make those rows falsely byte-prove as caller_contract. The per-payload gate is
    exactly what prevents it. Force the blanket verdict (add the payload's hash) and assert the
    WRONG typed outcome results — proving the classifier verdict is load-bearing."""
    row, text = _drive_evidence_and_deliver(
        monkeypatch, tmp_path, "l3b.evidence", "post_view",
        witnesses=[_witness("caller")], callers="compute() in x:1", siblings="foo(), bar()",
    )
    assert row.get("lineage_schema") is None  # honest path: siblings -> untyped
    # simulate a blanket classifier: add the delivered payload's hash, redeliver
    monkeypatch.setattr(gmp, "_seen", set())
    monkeypatch.setattr(gmp, "_oracle_delivered_hashes", set())
    gmp._l3b_pure_caller_hashes.add(gmp._l3b_content_key(text))
    out = {"output": ""}
    gmp._lane_a_deliver(
        out, "sed -i s/a/b/ src/foo.py",
        [("l3b.evidence", text, "src/foo.py")], krel="src/foo.py", event="post_view",
    )
    ledger = Path(tmp_path / "led.jsonl")
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    forced = [r for r in rows if r.get("layer") == "l3b.evidence"
              and r.get("outcome") == "delivered" and r.get("lineage_schema") == LINEAGE_SCHEMA]
    assert forced, "blanket stamp did not produce a typed row (gate wiring broken)"
    cons = _seal_join_ledger(forced[-1])
    assert metrics._fact_delivery_byte_proven("caller_contract", [forced[-1]], cons) is True


# ===========================================================================
# l3.contract — the ACCEPTED pure lane (unchanged from REV1)
# ===========================================================================
def test_l3_contract_delivered_row_byte_proves(tmp_path, monkeypatch):
    row, shipped = _drive_lane_delivery(
        "l3.contract", _L3_CONTRACT_TEXT, "src/foo.py", "post_edit", tmp_path, monkeypatch
    )
    assert row.get("lineage_schema") == LINEAGE_SCHEMA
    assert row.get("fact_class") == "caller_contract"
    assert row.get("evidence_type") == "caller_contract"
    assert row.get("runtime_producer_id") == "contract_map"
    assert row.get("registered_producer_id") == "contract_map"
    assert row.get("producer_registration_match") is True
    assert shipped and row.get("content_sha256_16")
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is True


def test_l3_contract_prefix_untyped_does_not_byte_prove(tmp_path, monkeypatch):
    pre_fix = dict(gmp._LANE_REGISTERED_PRODUCERS)
    pre_fix.pop("l3.contract", None)
    pre_fix.pop("l3b.evidence", None)
    monkeypatch.setattr(gmp, "_LANE_REGISTERED_PRODUCERS", pre_fix)
    row, _ = _drive_lane_delivery(
        "l3.contract", _L3_CONTRACT_TEXT, "src/foo.py", "post_edit", tmp_path, monkeypatch
    )
    assert row.get("lineage_schema") is None
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is False


def test_mutation_wrong_producer_stays_quiet(tmp_path, monkeypatch):
    mutated = dict(gmp._LANE_REGISTERED_PRODUCERS)
    mutated["l3.contract"] = ("patch_delta", "caller_contract")  # not authoritative
    monkeypatch.setattr(gmp, "_LANE_REGISTERED_PRODUCERS", mutated)
    row, _ = _drive_lane_delivery(
        "l3.contract", _L3_CONTRACT_TEXT, "src/foo.py", "post_edit", tmp_path, monkeypatch
    )
    assert row.get("lineage_schema") is None
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is False


def test_mutation_wrong_class_fails_caller_contract_proof(tmp_path, monkeypatch):
    mutated = dict(gmp._LANE_REGISTERED_PRODUCERS)
    mutated["l3.contract"] = ("patch_delta", "signature_mismatch")  # -> signature_delta
    monkeypatch.setattr(gmp, "_LANE_REGISTERED_PRODUCERS", mutated)
    row, _ = _drive_lane_delivery(
        "l3.contract", _L3_CONTRACT_TEXT, "src/foo.py", "post_edit", tmp_path, monkeypatch
    )
    assert row.get("lineage_schema") == LINEAGE_SCHEMA
    assert row.get("fact_class") == "signature_delta"
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is False
    assert metrics._fact_delivery_byte_proven("signature_delta", [row], cons) is True


def test_forged_registration_match_still_rejected():
    forged = {
        "layer": "l3.contract", "outcome": "delivered", "chars_delivered": 120,
        "content_sha256_16": "deadbeefdeadbeef", "lineage_schema": LINEAGE_SCHEMA,
        "fact_class": "caller_contract", "evidence_type": "caller_contract",
        "runtime_producer_id": "patch_delta", "registered_producer_id": "contract_map",
        "producer_registration_match": True,  # the lie
    }
    cons = _seal_join_ledger(forged)
    assert metrics._fact_delivery_byte_proven("caller_contract", [forged], cons) is False


# ===========================================================================
# Offline saved-ledger proof (read-only). REV2: a saved l3b row cannot be retro-classified
# (its extra carries no composition signal), so it stays untyped and rejected; the new typed
# shape is proven on a SYNTHETIC pure caller_contract lineage via the real writer helper.
# ===========================================================================
def test_offline_saved_l3b_stays_untyped_and_rule_proven_synthetically():
    matches = glob.glob(_SAVED_LEDGER)
    if not matches:
        pytest.skip(f"saved ledger not present: {_SAVED_LEDGER}")
    rows = [
        json.loads(ln)
        for ln in Path(matches[0]).read_text(encoding="utf-8").splitlines() if ln
    ]
    saved = next(
        (r for r in rows if r.get("layer") == "l3b.evidence"
         and r.get("outcome") == "delivered" and r.get("content_sha256_16")),
        None,
    )
    assert saved is not None, "no delivered l3b row in the saved ledger"
    assert saved.get("lineage_schema") is None  # not retro-classifiable -> untyped
    cons = _seal_join_ledger(saved)
    assert metrics._fact_delivery_byte_proven("caller_contract", [saved], cons) is False

    from groundtruth.runtime.feature_lineage import build_lineage, lineage_ledger_extra
    lineage = build_lineage(
        runtime_producer_id="contract_map", evidence_type="caller_contract",
        actual_event="post_view",
    )
    assert lineage is not None and lineage.producer_registration_match
    synthetic = dict(saved)
    synthetic.update(lineage_ledger_extra(lineage))
    assert metrics._fact_delivery_byte_proven("caller_contract", [synthetic], cons) is True

"""B-LINH (2026-07-16): two hardenings on the landed B-LIN per-payload l3b.evidence
caller_contract typing, proving typing is ALIVE (not fail-closed-DEAD) under the production
SS profile.

H1 — FAIL-CLOSED classifier. The REV2 verdict used two booleans and failed OPEN: a FUTURE
     _evidence_body builder that appends a line without setting a class flag would ride an
     existing caller block's `pure` verdict. Hardened to a COVERAGE GUARD
     (`caller_blocks + noncaller_blocks == n_lines`): an unclassified contributor -> NON-pure.

C1 — SS_PROVENANCE re-key. Under GT_SS_PROVENANCE the delivered text is line-filtered BEFORE the
     typing gate (gt_mini_patch _lane_a_deliver ~:12101 / the arbiter preparer ~:14267), so the
     gate's content-hash lookup (keyed on the FINAL bytes) missed the build-time key and a
     genuinely-pure payload silently never typed (fail-closed but DEAD). Fixed by re-keying the
     pure verdict onto the final bytes at the true final-bytes point. Invariant preserved: only an
     already-pure ORIGINAL propagates, so a non-pure payload can never be false-typed.

OBSERVABILITY — the DELIVERED l3b runtime-ledger row now carries `lineage_composition`
     ("typed" | "pure_gate_missed" | "non_pure") so an offline reader can tell the three apart
     (wave-2 hole: 3 delivered l3b rows, 0 typed, cause indistinguishable).

Tests drive the REAL verdict fn (`_l3b_pure_verdict`), the REAL classifier/producer (`_evidence`),
the REAL delivery writer (`_lane_a_deliver`) WITH GT_SS_PROVENANCE live, and the REAL reader
(`_fact_delivery_byte_proven`).
"""

from __future__ import annotations

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


def _caller_witness(path="src/consumer.py", line=42):
    return {
        "direction": "caller", "target": "compute", "symbol": "consumer",
        "line": line, "file_path": path, "code": "compute(a, b)",
    }


def _callee_witness(path="src/h.py", line=3):
    return {
        "direction": "callee", "target": "helper", "symbol": "helper",
        "line": line, "file_path": path, "code": "helper(x)",
    }


# ===========================================================================
# H1 — the FAIL-CLOSED composition verdict (`_l3b_pure_verdict`), driven directly.
# ===========================================================================
def test_h1_all_classified_caller_is_pure():
    # 1 caller block, 0 non-caller, 1 appended line -> every line classified -> PURE.
    assert gmp._l3b_pure_verdict(1, 0, 1) is True
    assert gmp._l3b_pure_verdict(3, 0, 3) is True


def test_h1_unclassified_future_builder_fails_closed():
    """RED before H1: a future builder appends a line WITHOUT classifying it — one caller block
    plus one unclassified line (caller=1, noncaller=0, n_lines=2). The OLD boolean verdict
    (`caller and not noncaller`) said PURE; the coverage guard says NON-pure."""
    # the exact fail-open input REV1 bounced:
    assert gmp._l3b_pure_verdict(1, 0, 2) is False
    # document that the pre-H1 (boolean) logic WOULD have wrongly typed it -> the guard bites:
    old_boolean_verdict = (1 >= 1) and (0 == 0)
    assert old_boolean_verdict is True
    assert gmp._l3b_pure_verdict(1, 0, 2) != old_boolean_verdict


def test_h1_noncaller_present_not_pure():
    assert gmp._l3b_pure_verdict(1, 1, 2) is False
    assert gmp._l3b_pure_verdict(0, 1, 1) is False


def test_h1_empty_not_pure():
    assert gmp._l3b_pure_verdict(0, 0, 0) is False


def test_h1_real_classifier_pure_payload_maintains_coverage(monkeypatch):
    """The real `_evidence_body` on a pure caller payload keeps the verdict True (regression:
    the refactor to `_l3b_pure_verdict` did not change the accepted pure case)."""
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
    monkeypatch.setattr(gmp, "_edit_target_callee_contracts", lambda con, f, fn, repo_root="": [])
    monkeypatch.setattr(
        gmp, "_resolved_witnesses_for_file",
        lambda con, f, cr, max_each=2: [_caller_witness()])
    monkeypatch.setattr(
        gmp, "_caller_contract_for_file", lambda con, f, cr, fn: "compute() in src/consumer.py:42")
    monkeypatch.setattr(gmp, "_sibling_context", lambda con, f, fn: "")
    body = gmp._evidence_body("post_view", "src/foo.py", "/root", "cat src/foo.py")
    assert body and gmp._l3b_last_pure_caller is True


# ===========================================================================
# Delivery harness with LIVE per-feature flags (C1 exercises GT_SS_PROVENANCE ON).
# ===========================================================================
def _deliver(monkeypatch, tmp_path, *, event, flags=None, translate_identity=True, **composition):
    for f in (
        "GT_EVIDENCE_NATIVE", "GT_SS_SHADOW", "GT_SS_PROVENANCE", "GT_SS_NOVELTY",
        "GT_SS_DEDUP2", "GT_SS_LATE_DROP", "GT_LANE_ENVELOPE",
    ):
        monkeypatch.setenv(f, "0")
    for k, v in (flags or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(gmp, "_GT_BASELINE", False)
    monkeypatch.setattr(gmp, "_action_count", 1)
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_to_repo_rel", lambda f, root: f)
    monkeypatch.setattr(gmp, "_classify", lambda cmd: (event, "src/foo.py"))
    monkeypatch.setattr(gmp, "_seen", set())
    monkeypatch.setattr(gmp, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(gmp, "_contract_seen", set())
    monkeypatch.setattr(gmp, "_l3b_pure_caller_hashes", set())
    monkeypatch.setattr(gmp, "_budget_trim", lambda x: x)
    if translate_identity:
        # isolate the provenance<->gate interaction from action-phrasing reformat, so the
        # [WITNESS] path token survives verbatim for the provenance line-drop to bite.
        monkeypatch.setattr(gmp, "_translate_to_action", lambda ev, *a, **k: ev)
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
        lambda con, f, fn, repo_root="": list(composition.get("callee_contracts", ())))
    monkeypatch.setattr(
        gmp, "_resolved_witnesses_for_file",
        lambda con, f, cr, max_each=2: list(composition.get("witnesses", ())))
    monkeypatch.setattr(
        gmp, "_caller_contract_for_file", lambda con, f, cr, fn: composition.get("callers", ""))
    monkeypatch.setattr(gmp, "_sibling_context", lambda con, f, fn: composition.get("siblings", ""))

    text = gmp._evidence("sed -i s/a/b/ src/foo.py")
    assert text, "producer emitted nothing"
    ledger = tmp_path / "led.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    out = {"output": ""}
    gmp._lane_a_deliver(
        out, "sed -i s/a/b/ src/foo.py",
        [("l3b.evidence", text, "src/foo.py")], krel="src/foo.py", event=event)
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln]
    delivered = [r for r in rows if r.get("layer") == "l3b.evidence"
                 and r.get("outcome") == "delivered"]
    assert delivered, f"no delivered l3b row: {rows}"
    return delivered[-1], text, out["output"]


def _seal_join_ledger(row):
    return {"entries": [{
        "source": "trajectory", "joined": True, "join_method": "seal",
        "content_sha256_16": row.get("content_sha256_16"),
        "ledger_chars": row.get("chars_delivered"), "ledger_layer": row.get("layer"),
        "msg_index": 7, "receipt": 1,
    }]}


# ===========================================================================
# C1 — a PURE payload MUTATED by GT_SS_PROVENANCE still types (RED before C1).
# ===========================================================================
def test_c1_provenance_mutated_pure_payload_still_types(tmp_path, monkeypatch):
    # two caller-direction witnesses -> PURE composition; one cites a generated `build/` path that
    # provenance DROPS, so the delivered bytes differ from the build-time recorded key.
    row, text, shipped = _deliver(
        monkeypatch, tmp_path, event="post_view",
        flags={"GT_SS_PROVENANCE": "1"},
        witnesses=[_caller_witness("src/consumer.py", 42), _caller_witness("build/gen.py", 9)],
    )
    # provenance actually mutated the bytes (the build/ line is gone), proving the gate faced a
    # NEW content hash — the exact condition that left the pure row untyped before C1.
    assert "build/gen.py" not in shipped
    assert "src/consumer.py" in shipped
    assert row.get("lineage_schema") == LINEAGE_SCHEMA, "C1: mutated-but-pure payload failed to type"
    assert row.get("fact_class") == "caller_contract"
    assert row.get("runtime_producer_id") == "contract_map"
    assert row.get("producer_registration_match") is True
    assert row.get("lineage_composition") == "typed"
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is True


def test_c1_provenance_biting_mutation_original_key_would_miss(tmp_path, monkeypatch):
    """Biting: without the C1 re-key the gate keys on the MUTATED bytes while the pure verdict is
    recorded under the ORIGINAL bytes — a miss. Prove the two keys genuinely differ (so the
    re-key is load-bearing), then prove the delivered row IS typed (the re-key fired)."""
    row, text, shipped = _deliver(
        monkeypatch, tmp_path, event="post_view",
        flags={"GT_SS_PROVENANCE": "1"},
        witnesses=[_caller_witness("src/consumer.py", 42), _caller_witness("build/gen.py", 9)],
    )
    final_bytes = shipped[shipped.index("\n<gt-evidence"):] if "<gt-evidence" in shipped else shipped
    # the recorded ORIGINAL key (pre-mutation `text`) differs from the final delivered key ->
    # a hash lookup keyed on either alone-without-rekey would have missed one of them.
    assert gmp._l3b_content_key(text) != gmp._l3b_content_key(final_bytes)
    # both keys are now present (build-time original + C1 re-keyed final) -> gate hits -> typed.
    assert gmp._l3b_content_key(text) in gmp._l3b_pure_caller_hashes
    assert gmp._l3b_content_key(final_bytes) in gmp._l3b_pure_caller_hashes
    assert row.get("lineage_schema") == LINEAGE_SCHEMA


# ===========================================================================
# INVARIANT — a NON-pure payload never types, under provenance ON or OFF.
# ===========================================================================
@pytest.mark.parametrize("prov", ["0", "1"])
def test_c_nonpure_never_types_under_any_flags(prov, tmp_path, monkeypatch):
    # [SIBLINGS] present -> non-caller -> NON-pure; add a provenance-bad caller line so provenance
    # still mutates the payload under prov=1 (proving mutation cannot manufacture a type).
    row, text, shipped = _deliver(
        monkeypatch, tmp_path, event="post_view",
        flags={"GT_SS_PROVENANCE": prov},
        witnesses=[_caller_witness("src/consumer.py", 42), _caller_witness("build/gen.py", 9)],
        callers="compute() in src/consumer.py:1", siblings="foo(), bar()",
    )
    assert row.get("lineage_schema") is None, "non-pure payload was wrongly typed"
    assert row.get("lineage_composition") == "non_pure"
    cons = _seal_join_ledger(row)
    assert metrics._fact_delivery_byte_proven("caller_contract", [row], cons) is False


# ===========================================================================
# OBSERVABILITY — the three distinguishable states on the DELIVERED l3b row.
# ===========================================================================
def test_marker_typed_on_pure_payload(tmp_path, monkeypatch):
    row, _text, _shipped = _deliver(
        monkeypatch, tmp_path, event="post_view",
        witnesses=[_caller_witness()], callers="compute() in src/consumer.py:42",
    )
    assert row.get("lineage_schema") == LINEAGE_SCHEMA
    assert row.get("lineage_composition") == "typed"


def test_marker_non_pure_on_callee_primary(tmp_path, monkeypatch):
    row, _text, _shipped = _deliver(
        monkeypatch, tmp_path, event="post_edit",
        callee_contracts=["[CALLEE] helper() src/h.py:3 def helper(x)"],
    )
    assert row.get("lineage_schema") is None
    assert row.get("lineage_composition") == "non_pure"


def test_marker_pure_gate_missed_when_lineage_engine_absent(tmp_path, monkeypatch):
    """The DEAD case the wave-2 hole hid: the payload IS pure (its key is in the set) but the
    lineage engine did not attach a stamp (here: the gate returns None). The marker must read
    "pure_gate_missed" — distinct from "non_pure" — so an offline reader can localize the miss."""
    # force a gate miss on an otherwise-pure payload.
    monkeypatch.setattr(gmp, "_lane_registered_lineage", lambda *a, **k: None)
    row, text, _shipped = _deliver(
        monkeypatch, tmp_path, event="post_view",
        witnesses=[_caller_witness()], callers="compute() in src/consumer.py:42",
    )
    assert gmp._l3b_content_key(text) in gmp._l3b_pure_caller_hashes  # pure at composition
    assert row.get("lineage_schema") is None                         # but not typed
    assert row.get("lineage_composition") == "pure_gate_missed"


# ===========================================================================
# BYTE-IDENTITY — the marker/re-key touch only host-side ledger metadata, never model bytes.
# ===========================================================================
def test_model_bytes_unchanged_by_marker(tmp_path, monkeypatch):
    row, _text, shipped = _deliver(
        monkeypatch, tmp_path, event="post_view",
        witnesses=[_caller_witness()], callers="compute() in src/consumer.py:42",
    )
    # the shipped observation is exactly the <gt-evidence> block (marker lives in the ledger row,
    # not the model-facing bytes); the seal is over those bytes.
    assert shipped.startswith("\n<gt-evidence")
    assert "lineage_composition" not in shipped
    assert row.get("content_sha256_16")

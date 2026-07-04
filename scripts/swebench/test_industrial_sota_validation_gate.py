#!/usr/bin/env python3
"""B15 regression tests for industrial_sota_validation_gate.py.

The gate must be able to FAIL (fail-closed #1; --strict) and must not pass HOLLOW
(#27 structured-only; #23/#24 exercise-floor). Self-contained — no pytest required:
    python scripts/swebench/test_industrial_sota_validation_gate.py
(Also pytest-discoverable via the test_* functions.)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import industrial_sota_validation_gate as gate  # noqa: E402  # type: ignore[import]

_NOPROBES = Path("this_probes_file_does_not_exist.json")


def _mkroot(tmp: str, *, gate_rc: object = ..., proof_ok=True, adapter=None, resolution=None,
            stage=None, graph_db_at=None) -> Path:
    root = Path(tmp)
    art = root / "gt_artifacts"
    art.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"artifact_sha256": {}}
    if gate_rc is not ...:
        manifest["gate_rc"] = gate_rc
    # graph_db_at in {"root","gt_artifacts"}: write a real graph.db there + bind its hash so
    # the artifact-hash items (3/14) can be exercised (BUG-B: root placement must still verify).
    if graph_db_at is not None:
        import hashlib
        dest = (root if graph_db_at == "root" else art) / "graph.db"
        dest.write_bytes(b"SQLite format 3\x00 fixture graph bytes")
        manifest["artifact_sha256"] = {"graph.db": hashlib.sha256(dest.read_bytes()).hexdigest()}
    (art / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    verdict: dict = {"state": "ok" if proof_ok else "fail", "schema": "gt.proof_verdict.v1"}
    if stage is not None:
        verdict["stage"] = stage
    (art / "proof_verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    if adapter is not None:
        (root / "adapter_witness.json").write_text(json.dumps(adapter), encoding="utf-8")
    if resolution is not None:
        (art / "resolution_method_audit.json").write_text(json.dumps(resolution), encoding="utf-8")
    return root


def _item(payload, i):
    return next(it for it in payload["items"] if it["id"] == i)


def test_item1_fail_open_fixed():
    with tempfile.TemporaryDirectory() as tmp:  # missing gate_rc → NOT evidence (was fail-open)
        p = gate.validate(_mkroot(tmp), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] != "evidence", "item1 passed with missing gate_rc (fail-open)"
    with tempfile.TemporaryDirectory() as tmp:  # gate_rc=0 + proof ok → evidence
        p = gate.validate(_mkroot(tmp, gate_rc=0), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] == "evidence", "item1 should pass with gate_rc=0"
    with tempfile.TemporaryDirectory() as tmp:  # gate_rc=1 → NOT evidence
        p = gate.validate(_mkroot(tmp, gate_rc=1), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] != "evidence", "item1 should fail with gate_rc=1"


def test_item1_name_match_carveout():
    # BUG-A red->green (LIPI 2026-07-02): the proof blesses a name_match-dominant repo as
    # DELIVERABLE (state=ok, stage=…name_match_carveout) with gate_rc=1. Item 1 must accept it,
    # else every TS/JS/Go/Rust name_match-heavy task fails the strict gate.
    CARVE = "artifact_contract_name_match_carveout"
    with tempfile.TemporaryDirectory() as tmp:
        p = gate.validate(_mkroot(tmp, gate_rc=1, proof_ok=True, stage=CARVE), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] == "evidence", "item1 must honor the deliverable name_match carveout"
    # mutation guards:
    with tempfile.TemporaryDirectory() as tmp:  # gate_rc=1 + NON-carveout stage -> still fails
        p = gate.validate(_mkroot(tmp, gate_rc=1, proof_ok=True, stage="artifact_contract"), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] != "evidence", "non-carveout gate_rc=1 must still fail item1"
    with tempfile.TemporaryDirectory() as tmp:  # carveout stage but state=fail -> fails
        p = gate.validate(_mkroot(tmp, gate_rc=1, proof_ok=False, stage=CARVE), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] != "evidence", "carveout with proof state=fail must fail item1"
    with tempfile.TemporaryDirectory() as tmp:  # missing gate_rc + carveout -> B15 fail-open preserved
        p = gate.validate(_mkroot(tmp, proof_ok=True, stage=CARVE), probes_path=_NOPROBES)
        assert _item(p, 1)["status"] != "evidence", "missing gate_rc must stay unclean (B15 fail-open)"


def test_graph_db_at_root_hash_verified():
    # BUG-B red->green (LIPI 2026-07-02): graph.db is collected to the TASK ROOT (trial_results/)
    # while the certs live in gt_artifacts/. The gate must still find + hash-verify it (items 3/14),
    # not report graph.db:missing.
    with tempfile.TemporaryDirectory() as tmp:
        root = _mkroot(tmp, gate_rc=0, proof_ok=True, graph_db_at="root")
        p = gate.validate(root, probes_path=_NOPROBES)
        assert _item(p, 3)["status"] == "evidence", f"item3 must verify graph.db at task root: {_item(p,3)}"
        assert _item(p, 14)["status"] == "evidence", f"item14 must verify graph.db at task root: {_item(p,14)}"
    # sanity: also works when co-located in gt_artifacts/
    with tempfile.TemporaryDirectory() as tmp:
        root = _mkroot(tmp, gate_rc=0, proof_ok=True, graph_db_at="gt_artifacts")
        p = gate.validate(root, probes_path=_NOPROBES)
        assert _item(p, 14)["status"] == "evidence"
    # MUTATION GUARD: wrong bytes at root -> hash check still bites (NOT a mere presence check)
    with tempfile.TemporaryDirectory() as tmp:
        root = _mkroot(tmp, gate_rc=0, proof_ok=True, graph_db_at="root")
        (root / "graph.db").write_bytes(b"tampered bytes not matching the bound hash")
        p = gate.validate(root, probes_path=_NOPROBES)
        assert _item(p, 14)["status"] != "evidence", "tampered graph.db must fail the hash check"


def test_item27_structured_only():
    with tempfile.TemporaryDirectory() as tmp:  # adapter present but NO structured receipt
        p = gate.validate(_mkroot(tmp, gate_rc=0, adapter={"brief_match": True}), probes_path=_NOPROBES)
        assert _item(p, 27)["status"] == "missing", "item27 passed on mere adapter presence (dead code)"


def test_item23_24_exercise_floor():
    with tempfile.TemporaryDirectory() as tmp:  # trivial graph: empty/0 → unexercised
        res = {"graphs": [{"demand_lsp_residuals": [], "receiver_type_proven_call_edges": 0, "receiver_unproven_call_edges": 0}]}
        p = gate.validate(_mkroot(tmp, gate_rc=0, resolution=res), probes_path=_NOPROBES)
        assert _item(p, 23)["status"] == "unexercised", f"item23={_item(p, 23)['status']}"
        assert _item(p, 24)["status"] == "unexercised", f"item24={_item(p, 24)['status']}"
    with tempfile.TemporaryDirectory() as tmp:  # exercised graph
        res = {"graphs": [{"demand_lsp_residuals": [{"edge": 1}], "receiver_type_proven_call_edges": 5, "receiver_unproven_call_edges": 2}]}
        p = gate.validate(_mkroot(tmp, gate_rc=0, resolution=res), probes_path=_NOPROBES)
        # B-6: item 23 enumerates UNRESOLVED residual candidates — demand-driven LSP is NOT
        # built, so residual candidates green as "unexercised" (a diagnostic), never "evidence".
        assert _item(p, 23)["status"] == "unexercised", _item(p, 23)
        # item 24 is REAL resolution (receiver_type_proven edges) -> evidence stands.
        assert _item(p, 24)["status"] == "evidence"


def test_strict_can_fail():
    with tempfile.TemporaryDirectory() as tmp:  # required integrity items unmet → strict rc=1
        root = _mkroot(tmp)  # no gate_rc, no hashes → items 1/3/14 not evidence
        assert gate.main([str(root), "--strict", "--probes", str(_NOPROBES)]) == 1, "strict must fail on unmet required items"
        assert gate.main([str(root), "--probes", str(_NOPROBES)]) == 0, "non-strict must preserve rc=0"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

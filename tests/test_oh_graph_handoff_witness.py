"""OH-path graph-handoff attestation parity with mini (gt_gt §12) — TTD.

Artifact-first red (run 28768411100, OH substrate path): every task shipped the
PRE-AGENT cert verdict ``GRAPH_FAIL_MISSING_HANDOFF`` even though the run log's
witness line carried a REAL ``hook_graph_hash`` equal to the substrate's
``graph_hash_after_lsp`` (both 40a4ab0b… on cfn-lint-3749). The DESIGNED
witness-override (scripts/swebench/reconcile.py) never fired because:

  (a) the OH wrapper's witness line ends ``_gt_prebuilt_active=True`` — the
      parser (deepswe_outcome._gt_meta_witness) extracts the key
      ``_gt_prebuilt_active``, never ``gt_prebuilt_active``, and the line has no
      ``hook_graph_hash_matches_post_lsp=`` field at all, so
      witness_holds is False by construction;
  (b) swebench_300task.yml never captures the witness nor runs
      reconcile_graph_handoff (mini's deepswe_full.yml does both).

These tests drive the port and its fail-closed legs. They extract
``_emit_graph_handoff_witness`` from the wrapper source (the established
pattern — tests/fail_closed/test_fail_closed_gates.py:467) so no OpenHands
dependency is imported.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_WRAP = ROOT / "scripts" / "swebench" / "oh_gt_full_wrapper.py"
_WF = ROOT / ".github" / "workflows" / "swebench_300task.yml"
_DEEPSWE_OUTCOME = ROOT / "scripts" / "verify" / "deepswe_outcome.py"
_RECONCILE = ROOT / "scripts" / "swebench" / "reconcile.py"
_TASK_TRUTH = ROOT / "scripts" / "swebench" / "task_truth.py"

# The EXACT witness line the pre-fix wrapper emitted on run 28768411100
# (task-aws-cloudformation__cfn-lint-3749/gt_debug/full_run.log) — frozen artifact.
_OBSERVED_LEGACY_LINE = (
    "[GT_META] graph_witness host_resolved_graph_db=/tmp/gt/graph.db "
    "hook_graph_db=/tmp/gt_index.db "
    "hook_graph_hash=40a4ab0b860c52501da2b409df2bbef03da094c7b173adc390e1e29a8cc72cec "
    "_gt_prebuilt_active=True"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _do():
    return _load_module("deepswe_outcome_ohw", _DEEPSWE_OUTCOME)


def _reconcile_mod():
    return _load_module("reconcile_ohw", _RECONCILE)


def _extract_witness_fn():
    """Extract _emit_graph_handoff_witness from the wrapper WITHOUT importing the
    whole wrapper (OpenHands deps). Same shape as test_fail_closed_gates.py:467."""
    src = _WRAP.read_text(encoding="utf-8")
    m = re.search(
        r"^def _emit_graph_handoff_witness\(.*?\n(?=^def |\Z)",
        src,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert m, (
        "RED: _emit_graph_handoff_witness not found in oh_gt_full_wrapper.py — the "
        "OH witness emit has not been ported to mini parity yet"
    )
    ns: dict = {
        "os": os,
        "sys": sys,
        "json": json,
        "Path": Path,
        "Any": Any,
        "__file__": str(_WRAP),
    }
    exec(compile(m.group(0), str(_WRAP), "exec"), ns)
    return ns["_emit_graph_handoff_witness"]


def _make_graph_db(path: Path) -> str:
    """Tiny graph.db with the exact edges schema graph_edges_hash fingerprints."""
    c = sqlite3.connect(str(path))
    c.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
    )
    c.execute(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (1, 2, 'CALLS', 'import', 1.0)"
    )
    c.execute(
        "INSERT INTO edges (source_id, target_id, type, resolution_method, confidence) "
        "VALUES (2, 3, 'CALLS', 'same_file', 1.0)"
    )
    c.commit()
    c.close()
    # Forward slashes: proof.graph_edges_hash opens file:{db}?mode=ro URIs.
    return str(path).replace("\\", "/")


def _real_hash(db: str) -> str:
    from groundtruth.runtime import proof

    h = proof.graph_edges_hash(db)
    assert h, f"fixture graph.db unreadable at {db}"
    return h


def _write_certs(cert_dir: Path, *, post_lsp_hash: str) -> str:
    """Substrate-shaped certs: the PRE-AGENT graph cert (host env unset in-container
    -> GRAPH_FAIL_MISSING_HANDOFF, hook fields null) + the LSP cert with the
    authority hash. Mirrors the observed run-28768411100 gate artifacts."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    (cert_dir / "lsp_certificate.json").write_text(
        json.dumps({"graph_hash_after_lsp": post_lsp_hash}), encoding="utf-8"
    )
    (cert_dir / "graph_certificate.json").write_text(
        json.dumps(
            {
                "schema": "gt.graph_certificate.v1",
                "verdict": "GRAPH_FAIL_MISSING_HANDOFF",
                "graph_hash": post_lsp_hash,
                "graph_hash_after_lsp": post_lsp_hash,
                "host_resolved_graph_db": "",
                "hook_graph_hash": None,
                "prebuilt_active": None,
            }
        ),
        encoding="utf-8",
    )
    return str(cert_dir).replace("\\", "/")


def _signal_and_reconcile(trial_log: str, cert_dir: str | None):
    do = _do()
    rec = do.build_signal_record(
        instance_id="synthetic__task-1",
        reward=None,
        n_agent_steps=None,
        exit_status=None,
        trial_log=trial_log,
        cert_dir=cert_dir,
    )
    return rec, _reconcile_mod().reconcile_graph_handoff(rec)


def _clean_gt_env(monkeypatch):
    for k in (
        "GT_HOST_GRAPH_DB",
        "GT_CERT_DIR",
        "GT_LSP_CERT",
        "GT_GRAPH_CERT",
        "GT_PROOF_MODE",
        "GT_PORTABLE_SUBSTRATE",
        "GT_SUBSTRATE_DIGEST",
        "GT_RUNTIME_STRATEGY",
        "GT_L6_FRESH",
        "GT_TRIAL_LOG",
        "GT_JOB_STARTED",
    ):
        monkeypatch.delenv(k, raising=False)


def _stub_config(*, prebuilt: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _gt_prebuilt_active=prebuilt, graph_db="/tmp/gt_index.db"
    )


# ────────────────────────────────────────────────────────────────────────────
# The shipped gap (frozen artifact) — the legacy line MUST NOT reconcile.
# Guards the parser's fail-closed semantics: an unproven witness never overrides.
# ────────────────────────────────────────────────────────────────────────────


def test_observed_legacy_witness_line_does_not_reconcile(tmp_path, monkeypatch):
    _clean_gt_env(monkeypatch)
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash="40a4ab0b860c52501da2b409df2bbef03da094c7b173adc390e1e29a8cc72cec")
    rec, out = _signal_and_reconcile(_OBSERVED_LEGACY_LINE + "\n", cert_dir)
    # The parser never sees a parseable gt_prebuilt_active / match field.
    assert rec["gt_prebuilt_active"] is None
    assert rec["hook_hash_match"] is None
    assert out["witness_holds"] is False
    assert out["graph_handoff"] == "fail"
    assert "GRAPH_FAIL_MISSING_HANDOFF without runtime witness" in out["contradictions"]


# ────────────────────────────────────────────────────────────────────────────
# The port: the wrapper's emitted witness must reconcile to GREEN.
# ────────────────────────────────────────────────────────────────────────────


def test_emitted_witness_reconciles_to_witness_overrides(tmp_path, monkeypatch, capsys):
    _clean_gt_env(monkeypatch)
    fn = _extract_witness_fn()
    db = _make_graph_db(tmp_path / "graph.db")
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash=_real_hash(db))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setenv("GT_CERT_DIR", cert_dir)
    monkeypatch.setenv("GT_PROOF_MODE", "1")

    fn(_stub_config(prebuilt=True))
    log = capsys.readouterr().out

    # The suffix fields the parser + the pipeline verify-step grep for:
    assert "gt_prebuilt_active=true" in log, log
    assert "hook_graph_hash_matches_post_lsp=True" in log, log
    assert "hook_graph_hash_matches_post_lsp=False" not in log
    assert "error=DEEPSWE_ADAPTER_FAIL" not in log
    assert f"hook_graph_hash={_real_hash(db)}" in log

    rec, out = _signal_and_reconcile(log, cert_dir)
    assert rec["gt_prebuilt_active"] is True
    assert rec["hook_hash_match"] is True
    assert out["witness_holds"] is True
    assert out["graph_handoff"] == "witness_overrides", (rec, out)


# ────────────────────────────────────────────────────────────────────────────
# Fail-closed legs (mutation checks — break the witness, the chain must bite).
# ────────────────────────────────────────────────────────────────────────────


def test_hash_mismatch_fails_closed_and_stays_fail(tmp_path, monkeypatch, capsys):
    _clean_gt_env(monkeypatch)
    fn = _extract_witness_fn()
    db = _make_graph_db(tmp_path / "graph.db")
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash="deadbeef" * 8)
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setenv("GT_CERT_DIR", cert_dir)
    monkeypatch.setenv("GT_PROOF_MODE", "1")

    with pytest.raises(RuntimeError, match="GRAPH_FAIL_HASH_MISMATCH"):
        fn(_stub_config(prebuilt=True))
    log = capsys.readouterr().out
    assert "hook_graph_hash_matches_post_lsp=False" in log
    assert "error=DEEPSWE_ADAPTER_FAIL" in log

    rec, out = _signal_and_reconcile(log, cert_dir)
    assert rec["hook_hash_match"] is not True
    assert out["witness_holds"] is False
    assert out["graph_handoff"] == "fail"


def test_absent_post_lsp_hash_fails_closed_under_proof(tmp_path, monkeypatch, capsys):
    _clean_gt_env(monkeypatch)
    fn = _extract_witness_fn()
    db = _make_graph_db(tmp_path / "graph.db")
    cert_dir = tmp_path / "gt"
    cert_dir.mkdir()
    (cert_dir / "graph_certificate.json").write_text(
        json.dumps({"verdict": "GRAPH_FAIL_MISSING_HANDOFF"}), encoding="utf-8"
    )
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setenv("GT_CERT_DIR", str(cert_dir).replace("\\", "/"))
    monkeypatch.setenv("GT_PROOF_MODE", "1")

    with pytest.raises(RuntimeError, match="post_lsp_hash_absent"):
        fn(_stub_config(prebuilt=True))
    log = capsys.readouterr().out
    assert "error=DEEPSWE_ADAPTER_FAIL" in log


def test_witness_absent_stays_fail(tmp_path, monkeypatch):
    _clean_gt_env(monkeypatch)
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash="ab" * 32)
    rec, out = _signal_and_reconcile("no witness in this log\n", cert_dir)
    assert rec["gt_meta_present"] is False
    assert out["graph_handoff"] == "fail"
    assert "GRAPH_FAIL_MISSING_HANDOFF without runtime witness" in out["contradictions"]


def test_unarmed_path_emits_legacy_line_and_does_not_raise(tmp_path, monkeypatch, capsys):
    """OH keeps its designed in-container-rebuild fallback: prebuilt inactive emits the
    byte-identical legacy bare line (no suffix, no raise) and reconcile keeps the cert
    FAIL — fail-closed at the attestation level, not by killing the fallback."""
    _clean_gt_env(monkeypatch)
    fn = _extract_witness_fn()
    db = _make_graph_db(tmp_path / "graph.db")
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash=_real_hash(db))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setenv("GT_CERT_DIR", cert_dir)
    monkeypatch.setenv("GT_PROOF_MODE", "1")

    fn(_stub_config(prebuilt=False))  # must NOT raise
    log = capsys.readouterr().out
    assert (
        f"[GT_META] graph_witness host_resolved_graph_db={db} "
        f"hook_graph_db=/tmp/gt_index.db hook_graph_hash= "
        f"_gt_prebuilt_active=False" in log
    ), log
    assert "gt_prebuilt_active=true" not in log
    assert "hook_graph_hash_matches_post_lsp" not in log

    rec, out = _signal_and_reconcile(log, cert_dir)
    assert out["graph_handoff"] == "fail"


# ────────────────────────────────────────────────────────────────────────────
# The pipeline seam: the EXACT invocation the yml reconcile step runs.
# ────────────────────────────────────────────────────────────────────────────


def test_pipeline_reconcile_step_writes_reconciled_verdict(tmp_path, monkeypatch, capsys):
    _clean_gt_env(monkeypatch)
    fn = _extract_witness_fn()
    db = _make_graph_db(tmp_path / "graph.db")
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash=_real_hash(db))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setenv("GT_CERT_DIR", cert_dir)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    fn(_stub_config(prebuilt=True))
    log = capsys.readouterr().out

    results = tmp_path / "results"  # OH results dir (no pier-shaped artifacts)
    results.mkdir()
    debug = tmp_path / "gt_debug"
    debug.mkdir()
    (debug / "full_run.log").write_text(log, encoding="utf-8")
    monkeypatch.setenv("GT_TRIAL_LOG", str(debug / "full_run.log"))

    tt = _load_module("task_truth_ohw", _TASK_TRUTH)
    out_path = tt.write_task_truth(
        str(results),
        out_path=str(debug / "task_truth.json"),
        instance_id="synthetic__task-1",
        cert_dir=cert_dir,
    )
    assert os.path.isfile(out_path)
    verdict = json.loads(
        (debug / "reconciled_substrate_verdict.json").read_text(encoding="utf-8")
    )
    assert verdict["graph_handoff"] == "witness_overrides", verdict
    assert verdict["witness_holds"] is True
    assert verdict["graph_certificate_verdict"] == "GRAPH_FAIL_MISSING_HANDOFF"
    assert verdict["instance_id"] == "synthetic__task-1"


# ────────────────────────────────────────────────────────────────────────────
# Outcome-authority separation (run 28772993900): write_task_truth reuses the
# DeepSWE-pier task-truth writer for its graph-handoff reconcile leg, but that
# writer ALSO runs the DeepSWE outcome classifier (detect_infra_subtype), which
# globs jobs/*/*__*/agent/*.trajectory.json — absent on an OH results dir — and
# fails closed to INFRA_MISSING_ARTIFACT. The four real tasks on 28772993900 were
# stamped a FALSE INFRA (agents wrote real source patches; the substrate handoff
# witnessed clean). grade_outcome=False grades ONLY the substrate.
# ────────────────────────────────────────────────────────────────────────────


def _reconcile_fixture(tmp_path, monkeypatch, capsys):
    """Shared OH fixture: emit a real witness, an OH results dir (no pier artifacts),
    a GRAPH_FAIL_MISSING_HANDOFF cert, and the trial log. Returns (results, debug, cert)."""
    _clean_gt_env(monkeypatch)
    fn = _extract_witness_fn()
    db = _make_graph_db(tmp_path / "graph.db")
    cert_dir = _write_certs(tmp_path / "gt", post_lsp_hash=_real_hash(db))
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    monkeypatch.setenv("GT_CERT_DIR", cert_dir)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    fn(_stub_config(prebuilt=True))
    log = capsys.readouterr().out
    results = tmp_path / "results"
    results.mkdir()  # OH shape — NO jobs/*/*__*/agent/*.trajectory.json
    debug = tmp_path / "gt_debug"
    debug.mkdir()
    (debug / "full_run.log").write_text(log, encoding="utf-8")
    monkeypatch.setenv("GT_TRIAL_LOG", str(debug / "full_run.log"))
    return results, debug, cert_dir


def test_oh_default_grade_outcome_true_stamps_false_infra(tmp_path, monkeypatch, capsys):
    """RED — pins the bug: grade_outcome=True (the pre-fix yml default) runs the
    DeepSWE outcome classifier on an OH results dir and stamps a FALSE
    INFRA_MISSING_ARTIFACT, even though the substrate handoff reconciles clean.
    This documents the exact misclassification the fix removes."""
    results, debug, cert_dir = _reconcile_fixture(tmp_path, monkeypatch, capsys)
    tt = _load_module("task_truth_ohw_infra", _TASK_TRUTH)
    tt.write_task_truth(
        str(results),
        out_path=str(debug / "task_truth.json"),
        instance_id="synthetic__task-1",
        cert_dir=cert_dir,
        grade_outcome=True,  # pre-fix behavior
    )
    truth = json.loads((debug / "task_truth.json").read_text(encoding="utf-8"))
    # The spurious INFRA — driven purely by artifact-shape mismatch, not a real failure.
    assert truth["outcome"]["failure_class"] == "INFRA", truth["outcome"]
    assert truth["outcome"]["infra_subtype"] == "INFRA_MISSING_ARTIFACT"
    assert truth["outcome"]["in_resolved_denominator"] is False
    # ...while the substrate is provably fine — so the INFRA cannot be a real infra fail.
    verdict = json.loads(
        (debug / "reconciled_substrate_verdict.json").read_text(encoding="utf-8")
    )
    assert verdict["graph_handoff"] == "witness_overrides"


def test_oh_grade_outcome_false_defers_outcome_keeps_substrate(tmp_path, monkeypatch, capsys):
    """GREEN — the fix: grade_outcome=False never runs the DeepSWE outcome classifier,
    so the false INFRA is gone; outcome is explicitly deferred to eval_result.json; the
    graph handoff still reconciles to witness_overrides (substrate grading is unaffected
    because reconcile_graph_handoff reads only the runtime-witness fields)."""
    results, debug, cert_dir = _reconcile_fixture(tmp_path, monkeypatch, capsys)
    tt = _load_module("task_truth_ohw_defer", _TASK_TRUTH)
    tt.write_task_truth(
        str(results),
        out_path=str(debug / "task_truth.json"),
        instance_id="synthetic__task-1",
        cert_dir=cert_dir,
        grade_outcome=False,  # the fix
    )
    truth = json.loads((debug / "task_truth.json").read_text(encoding="utf-8"))
    # No DeepSWE outcome stamped — failure_class + infra_subtype are None (deferred).
    assert truth["outcome"]["failure_class"] is None, truth["outcome"]
    assert truth["outcome"]["infra_subtype"] is None
    assert truth["outcome"]["in_resolved_denominator"] is None
    assert "eval_result.json" in (truth["outcome"].get("note") or "")
    assert truth["patch_hygiene"]["classification"] == "deferred_to_eval"
    # The substrate is STILL graded and correct — the leg we actually want from this step.
    verdict = json.loads(
        (debug / "reconciled_substrate_verdict.json").read_text(encoding="utf-8")
    )
    assert verdict["graph_handoff"] == "witness_overrides", verdict
    assert verdict["witness_holds"] is True
    assert verdict["outcome_failure_class"] is None  # NOT a false INFRA
    assert verdict["in_resolved_denominator"] is None


# ────────────────────────────────────────────────────────────────────────────
# The workflow wiring (text-level, same pattern as test_portable_substrate.py).
# ────────────────────────────────────────────────────────────────────────────


def test_300task_yml_captures_witness_and_reconciles():
    wf = _WF.read_text(encoding="utf-8")
    # Witness-verify step (parity with deepswe_full.yml "Verify DeepSWE adapter witness"):
    assert 'grep -q "gt_prebuilt_active=true" /tmp/gt_debug/full_run.log' in wf
    assert 'grep -q "hook_graph_hash_matches_post_lsp=False" /tmp/gt_debug/full_run.log' in wf
    assert 'grep -q "error=DEEPSWE_ADAPTER_FAIL" /tmp/gt_debug/full_run.log' in wf
    # Reconcile step: task_truth writes the reconciled verdict from the run log + certs.
    assert "GT_TRIAL_LOG" in wf and "/tmp/gt_debug/full_run.log" in wf
    assert "reconciled_substrate_verdict.json" in wf
    assert "write_task_truth" in wf or "task_truth.py" in wf
    # OH path grades ONLY the substrate — outcome deferred to eval_result.json.
    # Without this the DeepSWE outcome classifier stamps a false INFRA on every OH task.
    assert "grade_outcome=False" in wf

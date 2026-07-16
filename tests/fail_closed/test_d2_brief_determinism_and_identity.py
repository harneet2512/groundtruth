"""D2 defect — brief determinism pins + membership quantization + honest identity propagation.

Covers the three fixes for the DETERMINISM_MISMATCH class (4/30 smoke tasks):
  FIX 1 — deterministic float-reduction pins reach BOTH sides of the brief-determinism compare
          (the gate subprocess primary AND the in-process witness). Verified structurally:
          the module pins, clean_env_projection passes them through, and the proof shell +
          Dockerfile export them. (A live embed reduction is not reliably reproducible on this
          host, so the behavior is pinned structurally per the task's allowance.)
  FIX 2 — the semantic top-k MEMBERSHIP cut is quantized, so a sub-epsilon (reduction-noise)
          cosine difference no longer flips which file lands in the seed set.
  FIX 3 — the Run-identity gate PROPAGATES an upstream proof failure (e.g. DETERMINISM_MISMATCH)
          instead of overwriting it with the misleading IDENTITY_CONTENT_INVALID.

Documented biting mutations (each turns a test RED):
  M1 (FIX 1): drop ``k in DETERMINISTIC_THREAD_PINS`` from clean_env_projection's allowlist
              -> the gate subprocess primary runs un-pinned -> test_fix1_* fails.
  M2 (FIX 2): remove ``round(item[1], _SEM_MEMBERSHIP_QUANT_DP)`` from the membership sort key
              -> the two noisy runs pick different seed members -> test_fix2_* fails.
  M3 (FIX 3): make resolve_proof_status always return ``identity_code``
              -> the upstream code is lost -> test_fix3_* fails.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np
import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(ROOT, rel_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grp = _load("gt_run_proof_d2", "scripts/swebench/gt_run_proof.py")
prop = _load("propagate_identity_failure_d2", "scripts/ci/propagate_identity_failure.py")


# ─────────────────────────────── FIX 1 ────────────────────────────────────────

_PINS = {"OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"}


def test_fix1_determinism_pins_declared_as_one_thread():
    assert set(grp.DETERMINISTIC_THREAD_PINS) == _PINS
    assert all(v == "1" for v in grp.DETERMINISTIC_THREAD_PINS.values())


def test_fix1_clean_env_projection_passes_pins_to_the_gate_subprocess():
    # The gate subprocess (which writes the PRIMARY brief) is launched with
    # clean_env_projection(os.environ). If the pins are stripped here, the primary reduces
    # un-pinned while the in-process witness is pinned -> DETERMINISM_MISMATCH. (M1 bites here.)
    src = {p: "1" for p in _PINS}
    src["FAIL_TO_PASS"] = "leak"  # eval taint must still be dropped
    projected = grp.clean_env_projection(src)
    for p in _PINS:
        assert projected.get(p) == "1", f"{p} was stripped from the subprocess env"
    assert "FAIL_TO_PASS" not in projected  # leak guard unaffected


def test_fix1_pins_applied_to_process_env_at_import():
    # setdefault at module import pins the in-process witness before numpy is first imported.
    for p in _PINS:
        assert os.environ.get(p) == "1"


def test_fix1_proof_shell_exports_and_forwards_pins_without_rebake():
    sh = open(os.path.join(ROOT, "scripts/ci/substrate_proof.sh"), encoding="utf-8").read()
    # exported before the docker run so the container process starts pinned (no rebake needed)
    for p in _PINS:
        assert f"export {p}=" in sh, f"{p} not exported in substrate_proof.sh"
    # forwarded into BOTH docker run invocations (deepswe + pro branches)
    assert sh.count("-e OMP_NUM_THREADS=") == 2
    assert sh.count("-e NUMEXPR_NUM_THREADS=") == 2


def test_fix1_dockerfile_env_belt_present():
    df = open(os.path.join(ROOT, "docker/Dockerfile.gt-substrate"), encoding="utf-8").read()
    for p in _PINS:
        assert f"{p}=1" in df, f"{p} missing from Dockerfile ENV belt"


# ─────────────────────────────── FIX 2 ────────────────────────────────────────


def _cosine_row(cos: float) -> list[float]:
    """A unit 2-vector whose dot with issue_emb [1,0] equals ``cos``."""
    return [float(cos), float((1.0 - cos * cos) ** 0.5)]


def _run_semantic_top_k(monkeypatch, cos_by_file: dict[str, float], k_sem_top: int):
    from groundtruth.pretask import anchor_select as A

    paths = list(cos_by_file)
    matrix = {fp: np.array([_cosine_row(cos_by_file[fp])], dtype=np.float32) for fp in paths}

    monkeypatch.setattr(A, "_get_file_embeddings", lambda *a, **k: (paths, matrix))
    monkeypatch.setattr(A, "_embed", lambda *a, **k: [np.array([1.0, 0.0], dtype=np.float32)])
    # single-symbol rows => aggregate_symbol_cosines([c]) == c, so score == cosine.
    return A.semantic_top_k("issue", "/repo", "/graph.db", object(), k_sem_top=k_sem_top)


def test_fix2_membership_stable_across_subepsilon_reduction_noise(monkeypatch):
    # B and C straddle the k=2 cut, separated only by reduction noise (2e-5 < 1e-4). Two runs
    # whose noise flips B<->C must still select the SAME seed member. (M2 bites here.)
    k = 2
    run1 = _run_semantic_top_k(monkeypatch, {"a.py": 0.6, "b.py": 0.50001, "c.py": 0.49999}, k)
    run2 = _run_semantic_top_k(monkeypatch, {"a.py": 0.6, "b.py": 0.49999, "c.py": 0.50001}, k)

    assert len(run1) == k and len(run2) == k
    assert set(run1) == set(run2), (
        "sub-epsilon cosine noise flipped top-k membership: "
        f"{sorted(run1)} vs {sorted(run2)}"
    )
    # deterministic path tiebreak keeps the alphabetically-first of the tied pair
    assert set(run1) == {"a.py", "b.py"}


def test_fix2_genuinely_distinct_scores_keep_their_order(monkeypatch):
    # Quantization must NOT merge scores that differ beyond the epsilon: 0.60 vs 0.50 stay
    # distinct, top-1 selects the real winner (ranking semantics unchanged beyond ties).
    res = _run_semantic_top_k(monkeypatch, {"a.py": 0.60, "b.py": 0.50}, k_sem_top=1)
    assert set(res) == {"a.py"}


# ─────────────────────────────── FIX 3 ────────────────────────────────────────

_IDENTITY_CODE = "IDENTITY_CONTENT_INVALID"
_IDENTITY_DETAIL = "gt_run_identity.json failed schema/hash/checkout/digest validation"


def test_fix3_propagates_upstream_determinism_failure_not_identity_symptom():
    # The real D2 chain: brief_emit failed (proof_failure.json), run_manifest absent, identity
    # gate then computes IDENTITY_CONTENT_INVALID. The honest label is the UPSTREAM code. (M3.)
    failure = {
        "schema": "gt.proof_failure.v1",
        "stage": "brief_emit",
        "code": "GT_ARTIFACT_MISSING",
        "message": "brief.txt — DETERMINISM_MISMATCH: independent same-input brief acquisition "
                   "produced different canonical identities",
    }
    status = {"schema": "gt.proof_status.v1", "state": "failed", "code": "GT_RUN_PROOF_FAIL",
              "detail": "gt-run-proof rc=2"}
    out = prop.resolve_proof_status(_IDENTITY_CODE, _IDENTITY_DETAIL, status, failure, ts=0.0)

    assert out["state"] == "failed"  # still fail-closed, still refuses to spend
    assert out["code"] == "GT_ARTIFACT_MISSING"  # the TRUE upstream code, not the symptom
    assert "DETERMINISM_MISMATCH" in out["detail"]
    assert out["identity_gate_code"] == _IDENTITY_CODE  # symptom recorded, not primary
    assert out["propagated_from"] == "proof_failure.json"


def test_fix3_propagates_proof_status_code_when_no_failure_json():
    status = {"schema": "gt.proof_status.v1", "state": "failed", "code": "GT_RUN_PROOF_FAIL",
              "detail": "gt-run-proof rc=2"}
    out = prop.resolve_proof_status(_IDENTITY_CODE, _IDENTITY_DETAIL, status, None, ts=0.0)
    assert out["code"] == "GT_RUN_PROOF_FAIL"
    assert out["identity_gate_code"] == _IDENTITY_CODE


def test_fix3_identity_is_primary_when_no_upstream_failure():
    # No upstream failure -> the identity gate IS the first failure; its own code is the label.
    out = prop.resolve_proof_status(_IDENTITY_CODE, _IDENTITY_DETAIL, None, None, ts=0.0)
    assert out["code"] == _IDENTITY_CODE
    assert out["state"] == "failed"
    assert "identity_gate_code" not in out

"""Stage 3 — embedder-usage gate tests.

Prove the embedder is CONSUMED by every semantic path, not merely loaded. Drives
embedder_certificate.classify_embedder (synthetic certs), proof.assert_same_embedder_identity,
the proof build/write/load roundtrip, and the real localize encode-guard. No SWE-bench tasks,
no gold, no per-repo logic, no ranking-weight changes.
"""
import importlib.util
import os
import sqlite3
import sys

import pytest

_EC_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "metrics", "embedder_certificate.py")
_spec = importlib.util.spec_from_file_location("embedder_certificate_t", _EC_PATH)
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


def _base_embedder_cert(**kw):
    c = {
        "embedder_class": "EmbeddingModel", "embedder_dim": "384",
        "GT_FORCE_ONNX_EMBEDDER": "1", "GT_REQUIRE_EMBEDDER": "1",
        "GT_MODELS_ROOT": "/opt/gt/models",
        "run_v74_embedder_identity": {"models_root": "/opt/gt/models"},
        "localize_embedder_identity": {"models_root": "/opt/gt/models"},
        "semantic_candidate_count": 5, "rendered_candidate_count": 5,
        "rendered_semantic_nonzero_count": 4, "upstream_semantic_nonzero_count": 4,
        "effective_w_sem": 0.3, "all_zero_semantic_reason": "", "model_download_attempted": False,
    }
    c.update(kw)
    return c


# ── classify matrix (the required hard gates) ────────────────────────────────

def test_valid():
    assert ec.classify_embedder(_base_embedder_cert(), proof_mode=True, require_embedder=True) == ("EMBEDDER_USAGE_VALID", True)


def test_zero_model_rejected():
    assert ec.classify_embedder(_base_embedder_cert(embedder_class="_ZeroEmbeddingModel"),
                                proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_ZERO_MODEL", False)


def test_load_error_rejected():
    assert ec.classify_embedder(_base_embedder_cert(embedder_class="load_error:FileNotFoundError"),
                                proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_LOAD_ERROR", False)


def test_sentence_transformers_under_forced_onnx_rejected():
    assert ec.classify_embedder(_base_embedder_cert(embedder_class="SentenceTransformer", GT_FORCE_ONNX_EMBEDDER="1"),
                                proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_ST_UNDER_FORCED_ONNX", False)


def test_model_root_divergence_rejected():
    cert = _base_embedder_cert(localize_embedder_identity={"models_root": "/some/other/root"})
    assert ec.classify_embedder(cert, proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_MODEL_ROOT_DIVERGENCE", False)


def test_model_download_rejected():
    assert ec.classify_embedder(_base_embedder_cert(model_download_attempted=True),
                                proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_MODEL_DOWNLOAD", False)


def test_all_zero_for_nonempty_candidates_fails():
    # embedder loaded but produced all-zero rendered semantics with candidates present
    cert = _base_embedder_cert(rendered_semantic_nonzero_count=0, upstream_semantic_nonzero_count=0)
    assert ec.classify_embedder(cert, proof_mode=True, require_embedder=True) == ("EMBEDDER_USAGE_FAIL", False)


def test_dropped_semantic_fails():
    # upstream had semantic signal but rendered shows zero -> dropped before render
    cert = _base_embedder_cert(rendered_semantic_nonzero_count=0, upstream_semantic_nonzero_count=3)
    assert ec.classify_embedder(cert, proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_DROPPED_SEMANTIC", False)


def test_no_candidates_is_noop_valid():
    cert = _base_embedder_cert(semantic_candidate_count=0, rendered_semantic_nonzero_count=0, upstream_semantic_nonzero_count=0)
    assert ec.classify_embedder(cert, proof_mode=True, require_embedder=True) == ("EMBEDDER_USAGE_VALID_NOOP", True)


def test_no_cert_fails():
    assert ec.classify_embedder(None, proof_mode=True, require_embedder=True) == ("EMBEDDER_FAIL_NO_CERT", False)


def test_all_zero_outside_proof_is_valid():
    # correct-or-quiet: outside proof mode, all-zero is NOT a fail (no broad escape, but no abort)
    cert = _base_embedder_cert(rendered_semantic_nonzero_count=0, upstream_semantic_nonzero_count=0)
    assert ec.classify_embedder(cert, proof_mode=False, require_embedder=False) == ("EMBEDDER_USAGE_VALID", True)


def test_all_zero_without_require_embedder_is_valid():
    cert = _base_embedder_cert(rendered_semantic_nonzero_count=0, upstream_semantic_nonzero_count=0)
    assert ec.classify_embedder(cert, proof_mode=True, require_embedder=False) == ("EMBEDDER_USAGE_VALID", True)


# ── proof identity assert (model roots/identity must agree across paths) ──────

def test_assert_same_embedder_identity_match_then_mismatch(tmp_path, monkeypatch):
    from groundtruth.runtime import proof as _proof
    from groundtruth.runtime.proof import GTProofModeError
    db = str(tmp_path / "g.db")
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setattr(_proof, "embedder_identity",
                        lambda: {"models_root": "rootA", "class": "EmbeddingModel", "dim": "384", "force_onnx": "1"})
    assert _proof.assert_same_embedder_identity(db, "run_v74") is True   # first stamps
    assert _proof.assert_same_embedder_identity(db, "localize") is True  # matches
    monkeypatch.setattr(_proof, "embedder_identity",
                        lambda: {"models_root": "rootB", "class": "Other", "dim": "384", "force_onnx": "1"})
    with pytest.raises(GTProofModeError):
        _proof.assert_same_embedder_identity(db, "localize_divergent")


# ── build/write/load roundtrip (proof emitter -> classifier) ─────────────────

def test_build_write_load_roundtrip(tmp_path, monkeypatch):
    from groundtruth.runtime import proof as _proof
    monkeypatch.setenv("GT_EMBEDDER_CERT", str(tmp_path / "ec.json"))
    monkeypatch.setenv("GT_FORCE_ONNX_EMBEDDER", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    monkeypatch.setattr(_proof, "embedder_identity",
                        lambda: {"models_root": "/opt/gt/models", "class": "EmbeddingModel", "dim": "384", "force_onnx": "1"})
    cert = _proof.build_embedder_certificate(
        db=None, bug_id="x", semantic_candidate_count=5, rendered_candidate_count=5,
        rendered_semantic_nonzero_count=4, upstream_semantic_nonzero_count=4, effective_w_sem=0.3)
    p = _proof.write_embedder_certificate(cert)
    loaded = ec.load_embedder_cert(p)
    assert loaded["embedder_class"] == "EmbeddingModel"
    assert ec.classify_embedder(loaded, proof_mode=True, require_embedder=True) == ("EMBEDDER_USAGE_VALID", True)


# ── real localize encode-guard: a swallowed encode error => raise in proof ────

def test_localize_encode_exception_raises_in_proof(tmp_path, monkeypatch):
    try:
        from groundtruth.pretask import graph_localizer as gl
        from groundtruth.runtime.proof import GTProofModeError
    except Exception:
        pytest.skip("graph_localizer not importable in this environment")
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, signature TEXT, "
                 "file_path TEXT, is_test INTEGER)")
    conn.execute("INSERT INTO nodes (name,signature,file_path,is_test) VALUES ('foo','()','f.py',0)")
    conn.commit()
    conn.close()

    class _BadModel:
        def encode(self, *a, **k):
            raise RuntimeError("encode boom")

    monkeypatch.setattr(gl, "_get_embedder", lambda: _BadModel())
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    with pytest.raises(GTProofModeError):
        gl._semantic_score_by_file("an issue about foo behavior", db, {"f.py"})

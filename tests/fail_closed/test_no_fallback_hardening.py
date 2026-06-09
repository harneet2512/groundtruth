"""NO-FALLBACK / NO-SILENT-PASS hardening tests (audit defects #1 + #2).

Two fail-closed contracts, both env/proof-flag-driven and language-generic (no task IDs,
no gold, no per-repo logic):

  (1) Unsupported-language exit-0 false-green. resolve.py must distinguish:
        (a) a KNOWN LSP language (in config.LSP_SERVERS) whose server BINARY is missing on
            PATH  -> LSP_INSTALL_MISSING, FAIL-CLOSED under GT_REQUIRE_LSP=1 (nonzero exit);
        (b) a GENUINELY-UNKNOWN language (no LSP_SERVERS entry at all)
            -> LSP_UNSUPPORTED_EXPLICIT, honest no-op (exit 0).
      foundational_gates._classify_lsp must classify LSP_INSTALL_MISSING as a FAIL.

  (2) Embedder silent-substitution on the proof path. Under GT_REQUIRE_EMBEDDER=1 the CONFIGURED
      model (gte-modernbert) must LOAD or the loaders (_get_model / _get_embedder) RAISE — NO
      silent e5 substitution. With the flag OFF, the e5 fallback is preserved (graceful).
      gt_run_proof._baked_embedder_problems must require the CONFIGURED model baked (no "OR e5").
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

# foundational_gates (load by path — it lives under scripts/metrics, not the package)
_FG_PATH = os.path.join(ROOT, "scripts", "metrics", "foundational_gates.py")
_fg_spec = importlib.util.spec_from_file_location("foundational_gates_nf", _FG_PATH)
fg = importlib.util.module_from_spec(_fg_spec)
_fg_spec.loader.exec_module(fg)

# gt_run_proof (load by path)
_GRP_PATH = os.path.join(ROOT, "scripts", "swebench", "gt_run_proof.py")
_grp_spec = importlib.util.spec_from_file_location("gt_run_proof_nf", _GRP_PATH)
grp = importlib.util.module_from_spec(_grp_spec)
_grp_spec.loader.exec_module(grp)


# ─────────────────────────────────────────────────────────────────────────────
# DEFECT #1 — LSP: known-language-missing-server vs genuinely-unknown language
# ─────────────────────────────────────────────────────────────────────────────

def test_known_lsp_languages_are_recognized():
    from groundtruth import resolve
    # Every language config.LSP_SERVERS can serve is KNOWN (name, short ext, dotted ext).
    for lang in ("python", "go", "rust", "typescript", "javascript", "java"):
        assert resolve._is_known_lsp_language(lang), lang
    for ext in ("py", "go", "rs", "ts", "js", "java"):
        assert resolve._is_known_lsp_language(ext), ext
    for dotted in (".py", ".go", ".rs"):
        assert resolve._is_known_lsp_language(dotted), dotted


def test_unknown_languages_are_not_known():
    from groundtruth import resolve
    # No LSP_SERVERS entry exists for these -> genuinely unsupported (legitimate no-op).
    for lang in ("ruby", "c", "cpp", "php", "haskell", "cobol", ""):
        assert not resolve._is_known_lsp_language(lang), lang


def test_classify_lsp_install_missing_fails_closed():
    """(b) KNOWN language, binary missing -> verdict_hint=LSP_INSTALL_MISSING must FAIL."""
    cert = {
        "schema": "gt.lsp_certificate.v1", "language": "go",
        "server_command": "gopls", "server_launched": False, "warm_probe_ok": False,
        "lsp_warm": False, "probe_latency_ms": 0.0,
        "unsupported_reason": "",  # NOT a genuine no-server case
        "install_missing_reason": "LSP server for known language 'go' (command 'gopls') is not on PATH",
        "verdict_hint": "LSP_INSTALL_MISSING",
        "residual": 0, "demand_edges": 0, "attempted_edges": 0,
    }
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_INSTALL_MISSING" and not ok


def test_classify_lsp_install_missing_not_masked_by_empty_unsupported():
    """Even if only verdict_hint is set (install_missing_reason blank), it must still FAIL."""
    cert = {
        "language": "rust", "server_launched": False, "lsp_warm": False,
        "warm_probe_ok": False, "probe_latency_ms": 0.0, "unsupported_reason": "",
        "install_missing_reason": "", "verdict_hint": "LSP_INSTALL_MISSING",
        "residual": 0, "demand_edges": 0, "attempted_edges": 0,
    }
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_INSTALL_MISSING" and not ok


def test_classify_lsp_genuinely_unsupported_still_passes():
    """(a) genuinely-unknown language (unsupported_reason set, no install_missing) -> PASS no-op."""
    cert = {
        "language": "ruby", "server_launched": False, "lsp_warm": False,
        "warm_probe_ok": False, "probe_latency_ms": 0.0,
        "unsupported_reason": "no LSP server configured for language 'ruby'",
        "install_missing_reason": "", "verdict_hint": "LSP_UNSUPPORTED_EXPLICIT",
        "residual": 0, "demand_edges": 0, "attempted_edges": 0,
    }
    v, ok = fg._classify_lsp(cert)
    assert v == "LSP_UNSUPPORTED_EXPLICIT" and ok


def _tiny_graph_db(path: str) -> None:
    """Minimal graph.db with the columns resolve_main reads (edges + nodes + confidence)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
                 "file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, "
                 "return_type TEXT, language TEXT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
                 "type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, "
                 "confidence REAL)")
    conn.commit()
    conn.close()


def _run_resolve(tmp_path, lang, env_extra):
    """Invoke `python -m groundtruth.resolve --resolve --lang <lang>` as a subprocess against a
    tiny graph.db; returns the CompletedProcess. Subprocess because resolve_main uses sys.exit."""
    db = str(tmp_path / "graph.db")
    _tiny_graph_db(db)
    env = os.environ.copy()
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    # Point GT_LSP_CERT / GT proof dirs into tmp so we never touch /tmp/gt on the host.
    env["GT_LSP_CERT"] = str(tmp_path / "lsp_certificate.json")
    env.pop("GT_PROOF_MODE", None)  # stamp_meta must not hard-fail in proof mode here
    env.update(env_extra)
    cmd = [sys.executable, "-m", "groundtruth.resolve", "--db", db, "--root", str(tmp_path),
           "--resolve", "--lang", lang]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(__import__("shutil").which("gopls") is not None,
                    reason="gopls IS installed; the install-missing path cannot be exercised here")
def test_known_language_missing_server_fails_closed_under_require_lsp(tmp_path):
    """(a) KNOWN language (go) with gopls absent + GT_REQUIRE_LSP=1 -> NONZERO exit (fail-closed)."""
    r = _run_resolve(tmp_path, "go", {"GT_REQUIRE_LSP": "1"})
    assert r.returncode != 0, f"expected nonzero exit; stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "LSP_INSTALL_MISSING" in (r.stdout + r.stderr)
    assert "verdict=LSP_INSTALL_MISSING" in r.stdout


@pytest.mark.skipif(__import__("shutil").which("gopls") is not None,
                    reason="gopls IS installed; the install-missing path cannot be exercised here")
def test_known_language_missing_server_no_require_lsp_does_not_hard_fail(tmp_path):
    """(d) Same known-language-missing-server but WITHOUT GT_REQUIRE_LSP -> exit 0 (graceful),
    still surfaces the LSP_INSTALL_MISSING verdict (never silently green as 'unsupported')."""
    r = _run_resolve(tmp_path, "go", {})
    assert r.returncode == 0, f"expected exit 0 off the flag; stderr={r.stderr!r}"
    assert "verdict=LSP_INSTALL_MISSING" in r.stdout
    assert "LSP_UNSUPPORTED_EXPLICIT" not in r.stdout  # NOT laundered as unsupported


def test_genuinely_unknown_language_no_ops_exit_0(tmp_path):
    """(b) GENUINELY-UNKNOWN language (ruby — no LSP_SERVERS entry) -> exit 0 + UNSUPPORTED_EXPLICIT,
    EVEN under GT_REQUIRE_LSP=1 (there is no server to install — honest no-op)."""
    r = _run_resolve(tmp_path, "ruby", {"GT_REQUIRE_LSP": "1"})
    assert r.returncode == 0, f"expected exit 0 for genuinely-unknown lang; stderr={r.stderr!r}"
    assert "verdict=LSP_UNSUPPORTED_EXPLICIT" in r.stdout
    assert "LSP_INSTALL_MISSING" not in r.stdout


# ─────────────────────────────────────────────────────────────────────────────
# DEFECT #2 — embedder: no silent e5 substitution under GT_REQUIRE_EMBEDDER
# ─────────────────────────────────────────────────────────────────────────────

class _FakeLoadable:
    """A minimal embed.EmbeddingModel stand-in whose _ensure_loaded() succeeds."""
    def __init__(self, dim=384):
        self.dim = dim

    def _ensure_loaded(self):
        return (object(), object())

    def embed(self, text, is_query=False):
        return [0.0] * self.dim

    def embed_batch(self, texts, is_query=False):
        return [[0.0] * self.dim for _ in texts]


def _patch_gte_fails_e5_loads(monkeypatch, embed_mod):
    """Monkeypatch get_embedding_model so the CONFIGURED (no-arg) call RAISES (gte absent) but the
    explicit e5 call LOADS — isolating the substitution path."""
    from groundtruth.memory.enrich import embed as real_embed

    def _fake_get(model_name=None, dim=None):
        if model_name is None:  # the configured/default (gte) call
            raise FileNotFoundError("gte ONNX not baked (simulated)")
        if model_name == real_embed.E5_MODEL:  # the e5 fallback call
            return _FakeLoadable(dim=real_embed.E5_DIM)
        return _FakeLoadable()

    monkeypatch.setattr(embed_mod, "get_embedding_model", _fake_get)


def _reset_v74_cache():
    from groundtruth.pretask import v7_4_brief as v74
    v74._CACHED_MODEL = None
    v74._SEMANTIC_AVAILABLE = None
    return v74


def _reset_localizer_cache():
    from groundtruth.pretask import graph_localizer as gl
    gl._EMBEDDER = None
    gl._EMBEDDER_TRIED = False
    return gl


def test_v74_get_model_raises_under_require_no_e5_substitution(monkeypatch):
    """(c) gte-absent + GT_REQUIRE_EMBEDDER=1 -> _get_model RAISES (no silent e5)."""
    from groundtruth.memory.enrich import embed as real_embed
    v74 = _reset_v74_cache()
    _patch_gte_fails_e5_loads(monkeypatch, real_embed)
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    monkeypatch.setenv("GT_FORCE_ONNX_EMBEDDER", "1")  # skip sentence-transformers
    with pytest.raises(RuntimeError) as ei:
        v74._get_model()
    # The message must name the CONFIGURED model and disclaim silent e5 substitution.
    assert "GT_REQUIRE_EMBEDDER=1" in str(ei.value)
    assert "no silent e5 substitution" in str(ei.value)


def test_localizer_get_embedder_raises_under_require_no_e5_substitution(monkeypatch):
    """(c) gte-absent + GT_REQUIRE_EMBEDDER=1 -> _get_embedder RAISES (no silent e5)."""
    from groundtruth.memory.enrich import embed as real_embed
    gl = _reset_localizer_cache()
    _patch_gte_fails_e5_loads(monkeypatch, real_embed)
    monkeypatch.setenv("GT_REQUIRE_EMBEDDER", "1")
    monkeypatch.setenv("GT_FORCE_ONNX_EMBEDDER", "1")
    with pytest.raises(RuntimeError) as ei:
        gl._get_embedder()
    assert "GT_REQUIRE_EMBEDDER=1" in str(ei.value)
    assert "no silent e5 substitution" in str(ei.value)


def test_v74_get_model_uses_e5_fallback_when_flag_off(monkeypatch):
    """(d) gte-absent + GT_REQUIRE_EMBEDDER UNSET -> graceful e5 fallback (NOT raised)."""
    from groundtruth.memory.enrich import embed as real_embed
    v74 = _reset_v74_cache()
    _patch_gte_fails_e5_loads(monkeypatch, real_embed)
    monkeypatch.delenv("GT_REQUIRE_EMBEDDER", raising=False)
    monkeypatch.setenv("GT_FORCE_ONNX_EMBEDDER", "1")
    m = v74._get_model()  # must NOT raise
    # The e5 fallback loaded -> a real adapter (not the zero model).
    assert m is not None
    assert not isinstance(m, v74._ZeroEmbeddingModel)
    assert getattr(m, "dim", None) == real_embed.E5_DIM


def test_localizer_get_embedder_uses_e5_fallback_when_flag_off(monkeypatch):
    """(d) gte-absent + GT_REQUIRE_EMBEDDER UNSET -> graceful e5 fallback (NOT None, NOT raised)."""
    from groundtruth.memory.enrich import embed as real_embed
    gl = _reset_localizer_cache()
    _patch_gte_fails_e5_loads(monkeypatch, real_embed)
    monkeypatch.delenv("GT_REQUIRE_EMBEDDER", raising=False)
    monkeypatch.setenv("GT_FORCE_ONNX_EMBEDDER", "1")
    e = gl._get_embedder()  # must NOT raise
    assert e is not None
    assert getattr(e, "dim", None) == real_embed.E5_DIM


# ── gt_run_proof._baked_embedder_problems: configured-only (no "OR e5") ───────

def test_baked_embedder_requires_configured_model_not_e5(monkeypatch, tmp_path):
    """Only e5 baked (configured gte ABSENT) -> validate must report 'not baked' (no e5 escape)."""
    root = tmp_path / "models"
    (root / "e5-small-v2").mkdir(parents=True)
    (root / "e5-small-v2" / "model.onnx").write_bytes(b"\x00")  # e5 present...
    # ...but the configured gte model dir is absent.
    monkeypatch.setenv("GT_MODELS_ROOT", str(root))
    problems = grp._baked_embedder_problems()
    assert problems, "e5-only must NOT clear the proof boundary"
    assert any("not baked" in p for p in problems)


def test_baked_embedder_accepts_configured_model(monkeypatch, tmp_path):
    """Configured model baked -> clean (no problems)."""
    root = tmp_path / "models"
    # Derive the configured dirname the same way the function does.
    sys.path.insert(0, SRC)
    from groundtruth.memory.enrich.embed import _default_embed_model
    configured = _default_embed_model().split("/")[-1]
    (root / configured).mkdir(parents=True)
    (root / configured / "model.onnx").write_bytes(b"\x00")
    monkeypatch.setenv("GT_MODELS_ROOT", str(root))
    assert grp._baked_embedder_problems() == []


def test_baked_embedder_accepts_int8_variant(monkeypatch, tmp_path):
    """A baked int8/quantized variant of the configured model is acceptable (matches loader)."""
    root = tmp_path / "models"
    sys.path.insert(0, SRC)
    from groundtruth.memory.enrich.embed import _default_embed_model
    configured = _default_embed_model().split("/")[-1]
    (root / configured).mkdir(parents=True)
    (root / configured / "model_int8.onnx").write_bytes(b"\x00")  # no model.onnx, only int8
    monkeypatch.setenv("GT_MODELS_ROOT", str(root))
    assert grp._baked_embedder_problems() == []

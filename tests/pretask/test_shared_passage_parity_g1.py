"""G1 — the two semantic halves must build the SAME per-symbol passage under GT_SEM_BODY.

Before this fix, ``graph_localizer._assemble_symbol_passages`` (the localize half) read
the index-time body channels under GT_SEM_BODY while
``anchor_select._get_file_embeddings`` (the run_v74/anchor half) read only the OFF
docstring props with no channels and no flag gate. With the flag ON the two halves ranked
on DIFFERENT passage text AND produced two different ``passage_hash`` for the SAME symbol,
so the shared ``embed._PASSAGE_VEC_CACHE`` double-encoded it (breaking the encode-once /
OOM contract).

Fix: both halves read the per-symbol body through the ONE shared ``_symbol_body_map``.
This test drives BOTH real code paths against the SAME hand-built graph.db and asserts the
passage TEXT and the ``passage_hash`` for a given symbol are identical under GT_SEM_BODY=1.

RED (pre-fix): anchor's passage == symbol_passage(name, sig, docstring-only) while the
localizer's == symbol_passage(name, sig, <channel template>) -> texts differ -> assert
fails. GREEN (post-fix): identical.

The anchor half embeds internally, so we capture its passages with a fake embedder that
records every text handed to ``embed_batch`` — no ONNX / torch needed.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from groundtruth.pretask import anchor_select
from groundtruth.pretask.graph_localizer import _assemble_symbol_passages, _normalize
from groundtruth.memory.enrich.embed import PASSAGE_CACHE_VERSION, passage_hash

GTE = "Alibaba-NLP/gte-modernbert-base"
DIM = 768

# ONE non-test symbol with docstring + all three body channels, so the ON template and the
# OFF props are demonstrably different passages.
_NODES = [(1, "svc/redis.py", "connect", "(host)", 0)]
_PROPS = [
    (1, "docstring", "open the connection"),
    (1, "string_literals", "redis://localhost:6379"),
    (1, "calls", "verify_certificate open_socket"),
    (1, "body_terms", "host url verify_certificate handshake tls establish"),
]


def _make_graph(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, file_path TEXT, name TEXT, "
        "signature TEXT, start_line INTEGER, end_line INTEGER, is_test INTEGER)"
    )
    conn.execute(
        "CREATE TABLE properties (node_id INTEGER, kind TEXT, value TEXT, line INTEGER, confidence REAL)"
    )
    for nid, fp, nm, sig, is_test in _NODES:
        conn.execute(
            "INSERT INTO nodes (id, file_path, name, signature, start_line, end_line, is_test) "
            "VALUES (?,?,?,?,?,?,?)", (nid, fp, nm, sig, 1, 20, is_test),
        )
    for node_id, kind, value in _PROPS:
        conn.execute(
            "INSERT INTO properties (node_id, kind, value, line, confidence) VALUES (?,?,?,?,?)",
            (node_id, kind, value, 1, 1.0),
        )
    conn.commit()
    conn.close()
    return db


class _CapturingModel:
    """Fake embedder: records every passage handed to embed_batch; returns zero vectors.
    Exposes model_name/dim so embed.model_identity resolves the shared cache key, and
    ONLY embed_batch (no .encode) so anchor_select._embed takes the embed_batch path."""

    model_name = GTE
    dim = DIM

    def __init__(self):
        self.captured: list[str] = []

    def embed_batch(self, texts, is_query=False):
        self.captured.extend(list(texts))
        return np.zeros((len(texts), DIM), dtype=np.float32)


def _anchor_half_passages(tmp_path, db, monkeypatch) -> list[str]:
    """Run the REAL anchor half (_get_file_embeddings) and return the passages it built."""
    monkeypatch.setenv("GT_SEM_BODY", "1")
    # Force fresh encode: clear both the per-graph matrix cache and the shared vector cache.
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    model = _CapturingModel()
    anchor_select._get_file_embeddings(db, str(tmp_path), model, issue_text="")
    return model.captured


def test_both_halves_identical_passage_text_and_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_SEM_BODY", "1")
    db = _make_graph(tmp_path)

    # Localizer half — returns passages directly.
    loc_fp, _ = _assemble_symbol_passages(db, {_normalize("svc/redis.py")}, body_on=True)
    loc_passages = loc_fp[_normalize("svc/redis.py")]
    assert len(loc_passages) == 1, f"expected 1 symbol passage, got {loc_passages!r}"
    loc = loc_passages[0]

    # Anchor half — capture the passage it hands the embedder.
    anc_passages = _anchor_half_passages(tmp_path, db, monkeypatch)
    assert len(anc_passages) == 1, f"expected 1 anchor passage, got {anc_passages!r}"
    anc = anc_passages[0]

    # (a) identical passage TEXT.
    assert loc == anc, (
        "G1: the two halves built DIFFERENT passage text for the same symbol\n"
        f"--- localizer ---\n{loc!r}\n--- anchor ---\n{anc!r}"
    )
    # (b) identical passage_hash => ONE shared-cache encode (both halves key it the same).
    assert passage_hash(loc, GTE, DIM, PASSAGE_CACHE_VERSION) == passage_hash(
        anc, GTE, DIM, PASSAGE_CACHE_VERSION
    )
    # Teeth: the ON channel vocabulary MUST be present (guards a silent revert to OFF props,
    # which would ALSO make both halves equal — but on the wrong text).
    for tok in ("redis://localhost:6379", "handshake", "tls"):
        assert tok in loc, f"ON channel vocabulary {tok!r} absent — assembler fell back to OFF: {loc!r}"


def test_off_both_halves_identical_and_props_only(tmp_path, monkeypatch):
    """OFF must ALSO agree, and be the docstring-only (byte-identical) passage."""
    monkeypatch.delenv("GT_SEM_BODY", raising=False)
    db = _make_graph(tmp_path)

    loc_fp, _ = _assemble_symbol_passages(db, {_normalize("svc/redis.py")}, body_on=False)
    loc = loc_fp[_normalize("svc/redis.py")][0]

    # ONE fresh anchor encode (a second call on the same db would hit the disk .embed_cache
    # and skip the encode, capturing nothing). GT_SEM_BODY is unset -> OFF path.
    anchor_select._EMBED_CACHE.clear()
    anchor_select._SYMVEC_CACHE.clear()
    model = _CapturingModel()
    anchor_select._get_file_embeddings(db, str(tmp_path), model, issue_text="")
    assert model.captured, "anchor half encoded nothing (unexpected cache hit)"
    anc_off = model.captured[0]

    assert loc == anc_off, f"OFF halves differ:\n{loc!r}\n{anc_off!r}"
    # OFF passage carries NONE of the channel vocabulary (docstring props only).
    for leaked in ("redis://localhost:6379", "handshake", "verify_certificate"):
        assert leaked not in loc, f"OFF passage leaked channel token {leaked!r}: {loc!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

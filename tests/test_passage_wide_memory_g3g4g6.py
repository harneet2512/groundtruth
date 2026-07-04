"""G3/G4/G6 — GT_PASSAGE_WIDE memory + desync + e5-preservation fixes (offline).

G3  encode-batch is HALVED under the flag so a 256-token batch's ~2x activation stays
    within the exit-137 budget (16*256 == 32*128). OFF -> 32 (byte-identical).
G4  padding is re-asserted per-encode (== that encode's window) so a flag flipped AFTER
    load can never desync padding (128) from truncation (256) into a ragged np.array.
G6  the per-symbol char cap widens ONLY for the window-widening (non-e5) family, so e5's
    passage stays byte-identical under the flag (its window stays 128).

All three drive the REAL functions with fakes — no ONNX / torch.
"""
from __future__ import annotations

import numpy as np
import pytest

from groundtruth.memory.enrich import embed
from groundtruth.memory.enrich.embed import (
    EmbeddingModel,
    _encode_batch_size,
    _symbol_passage_char_cap,
    passage_hash,
    symbol_passage,
)

GTE = "Alibaba-NLP/gte-modernbert-base"
E5 = "intfloat/e5-small-v2"


# ── G3: encode-batch halving ─────────────────────────────────────────────────
def test_g3_encode_batch_off_is_32(monkeypatch):
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    monkeypatch.delenv("GT_EMBED_ENCODE_BATCH", raising=False)
    assert _encode_batch_size() == 32  # byte-identical to the pre-change literal


def test_g3_encode_batch_on_is_16(monkeypatch):
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    monkeypatch.delenv("GT_EMBED_ENCODE_BATCH", raising=False)
    assert _encode_batch_size() == 16  # halved -> peak activation preserved


def test_g3_explicit_override_wins_both_states(monkeypatch):
    monkeypatch.setenv("GT_EMBED_ENCODE_BATCH", "8")
    for flag in (None, "1"):
        if flag is None:
            monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
        else:
            monkeypatch.setenv("GT_PASSAGE_WIDE", flag)
        assert _encode_batch_size() == 8


def test_g3_embed_prefixed_actually_uses_halved_batch(monkeypatch):
    """_embed_prefixed must chunk by the flag-aware batch size (drives the REAL method)."""
    m = EmbeddingModel(GTE, 768)
    sizes: list[int] = []
    monkeypatch.setattr(
        m, "_embed_chunk",
        lambda texts, is_query=False: (sizes.append(len(texts)) or [[0.0]] * len(texts)),
    )
    monkeypatch.delenv("GT_EMBED_ENCODE_BATCH", raising=False)

    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    sizes.clear()
    m._embed_prefixed(["p"] * 48, is_query=False)
    assert sizes == [32, 16]  # OFF: 32 then remainder 16

    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    sizes.clear()
    m._embed_prefixed(["p"] * 48, is_query=False)
    assert sizes == [16, 16, 16]  # ON: three 16-batches (halved)


# ── G4: padding/truncation desync ────────────────────────────────────────────
class _FakeEncoded:
    def __init__(self, n: int):
        self.ids = [1] * n
        self.attention_mask = [1] * n


class _FakeTokenizer:
    """Faithful padding/truncation model: truncation caps the length; fixed-length
    padding pads SHORTER sequences up to the length but never truncates longer ones."""

    def __init__(self):
        self.pad_len: int | None = None
        self.trunc_len: int | None = None

    def enable_padding(self, length=None, **_):
        self.pad_len = length

    def enable_truncation(self, max_length=None, **_):
        self.trunc_len = max_length

    def encode_batch(self, texts):
        out = []
        for t in texts:
            n = len(t.split())
            if self.trunc_len is not None:
                n = min(n, self.trunc_len)
            if self.pad_len is not None and n < self.pad_len:
                n = self.pad_len
            out.append(_FakeEncoded(n))
        return out


class _FakeSession:
    def get_inputs(self):
        return []

    def run(self, _none, feed):
        ids = feed["input_ids"]
        b, s = ids.shape
        return [np.ones((b, s, 8), dtype=np.float32)]


def _loaded_model(name: str, pad_at_load: int):
    m = EmbeddingModel(name, 768 if name == GTE else 384)
    m._session = _FakeSession()
    tok = _FakeTokenizer()
    tok.enable_padding(length=pad_at_load)  # simulate the load-time padding
    m._tokenizer = tok
    m._input_names = ["input_ids", "attention_mask"]
    return m, tok


def test_g4_no_ragged_after_load_off_then_flag_on(monkeypatch):
    """Load OFF (padding fixed 128), flip the flag ON, then encode mixed-length passages.
    The per-encode padding fix keeps the batch rectangular (no ragged np.array)."""
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    m, tok = _loaded_model(GTE, 128)
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")  # window now 256 for passages

    out = m._embed_chunk(["w " * 10, "w " * 200], is_query=False)
    assert len(out) == 2  # rectangular -> np.array built, 2 vectors, no raise
    # mechanism: padding was re-asserted == truncation window (256) at encode time.
    assert tok.pad_len == tok.trunc_len == 256


def test_g4_mutation_desync_would_be_ragged():
    """The guarded failure is REAL: padding fixed 128 + truncation 256 (the pre-fix
    desync) yields rows of DIFFERENT length — the ragged batch np.array chokes on."""
    tok = _FakeTokenizer()
    tok.enable_padding(length=128)          # pre-fix: fixed at load
    tok.enable_truncation(max_length=256)   # per-encode passage window under the flag
    enc = tok.encode_batch(["w " * 10, "w " * 200])
    lengths = {len(e.ids) for e in enc}
    assert len(lengths) > 1  # {128 (padded short), 200 (long, unpadded)} -> ragged


def test_g4_off_passage_padding_is_128(monkeypatch):
    """OFF: the passage encode still pads/truncates at 128 (byte-identical window)."""
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    m, tok = _loaded_model(GTE, 128)
    m._embed_chunk(["w " * 5, "w " * 9], is_query=False)
    assert tok.pad_len == tok.trunc_len == 128


def test_f12_query_padding_stays_at_passage_window(monkeypatch):
    """F12 (RED→GREEN): a QUERY encode must NOT pad to the query window. Pre-change
    (and pre-F12) padding followed `_window`, so a flag-OFF query padded to 1024 —
    a flag-off behavior change (~8x padded ONNX compute on a short query, and
    bit-exactness left to the export's masking) with zero benefit: a query is a
    SINGLE row, always rectangular. Padding must stay at the PASSAGE window (128
    OFF — byte-identical to the pre-change fixed-128 padding) while truncation uses
    the query window. RED pre-F12: pad seen at encode == 1024."""
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    m, tok = _loaded_model(GTE, 128)
    seen: list[tuple[int | None, int | None]] = []
    orig = tok.encode_batch

    def spy(texts):
        seen.append((tok.pad_len, tok.trunc_len))
        return orig(texts)

    tok.encode_batch = spy
    m._embed_chunk(["query about redis tls"], is_query=True)
    assert seen == [(128, 1024)]  # truncation at the query window; padding stays 128

    # Flag ON: the passage window widens to 256 — the query pad follows the PASSAGE
    # window (256), never the 1024 query window.
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    seen.clear()
    m._embed_chunk(["query about redis tls"], is_query=True)
    assert seen == [(256, 1024)]


# ── G6: e5 char cap under the flag ───────────────────────────────────────────
def test_g6_e5_char_cap_not_widened_under_flag(monkeypatch):
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    monkeypatch.setenv("GT_EMBED_MODEL_NAME", E5)
    assert _symbol_passage_char_cap() == 400  # e5 window stays 128 -> cap stays 400

    long_body = "term " * 1000
    p_on = symbol_passage("name", "(sig)", long_body)
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    p_off = symbol_passage("name", "(sig)", long_body)
    assert p_on == p_off and len(p_on) == 400  # byte-identical text under the flag

    # ... and byte-identical hash (e5 is never salted, so the cache slot is shared).
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    h_on = passage_hash(p_on, E5, 384, "sym2-fn", is_query=False)
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    h_off = passage_hash(p_off, E5, 384, "sym2-fn", is_query=False)
    assert h_on == h_off


def test_g6_gte_char_cap_still_widens_under_flag(monkeypatch):
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    monkeypatch.delenv("GT_EMBED_MODEL_NAME", raising=False)  # default -> gte (non-e5)
    assert _symbol_passage_char_cap() == 800  # non-e5 widens (window 256)


def test_g6_char_cap_off_is_400_regardless_of_model(monkeypatch):
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    for name in (GTE, E5):
        monkeypatch.setenv("GT_EMBED_MODEL_NAME", name)
        assert _symbol_passage_char_cap() == 400


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

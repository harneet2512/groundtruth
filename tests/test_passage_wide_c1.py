"""C1 — GT_PASSAGE_WIDE: widen the per-symbol semantic PASSAGE window (offline).

Offline-provable surface ONLY (per the build spec): the passage-WINDOW selection,
the char-cap selection, the cache-key salt, and — the hard guarantee — that with the
flag OFF every one of these is BYTE-IDENTICAL to the pre-change code. The activation-
MEMORY proof (exit-137) and the cos-to-gold lift need the ONNX embed env => Phase-4,
NOT asserted here.

Each byte-identical-off assertion drives the REAL function (no stub) with the flag off
and compares to the value the code produced BEFORE this change, recomputed inline from
the old formula. The MUTATION companions show the exact neutering that reddens each.
"""

import hashlib

import pytest

from groundtruth.memory.enrich import embed
from groundtruth.memory.enrich.embed import (
    EmbeddingModel,
    _passage_token_window,
    _symbol_passage_char_cap,
    passage_hash,
    symbol_passage,
)

GTE = "Alibaba-NLP/gte-modernbert-base"
E5 = "intfloat/e5-small-v2"


def _model(name: str) -> EmbeddingModel:
    # Constructor does NOT load ONNX; _passage_token_window only reads .model_name.
    return EmbeddingModel(model_name=name, dim=768 if name == GTE else 384)


# ── window selection ────────────────────────────────────────────────────────
def test_passage_window_off_is_128_both_families(monkeypatch):
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    assert _passage_token_window(_model(GTE)) == 128
    assert _passage_token_window(_model(E5)) == 128


def test_passage_window_on_gte_256_e5_stays_128(monkeypatch):
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    assert _passage_token_window(_model(GTE)) == 256  # gte/ModernBERT widens
    assert _passage_token_window(_model(E5)) == 128   # e5 window preserved


def test_passage_window_off_falsey_values_are_off(monkeypatch):
    for v in ("", "0", "false", "no"):
        monkeypatch.setenv("GT_PASSAGE_WIDE", v)
        assert _passage_token_window(_model(GTE)) == 128


# ── char cap ────────────────────────────────────────────────────────────────
def test_char_cap_off_is_400_byte_identical(monkeypatch):
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    assert _symbol_passage_char_cap() == 80 * 5  # the pre-change literal
    # drive the REAL symbol_passage with a long body -> capped at exactly 400
    long_body = "term " * 1000
    p = symbol_passage("name", "(sig)", long_body)
    expected = f"name (sig)\n{long_body.strip()}"[:400]  # old code: [:_SYMBOL_PASSAGE_CHAR_CAP]
    assert p == expected
    assert len(p) == 400


def test_char_cap_on_is_800(monkeypatch):
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    assert _symbol_passage_char_cap() == 800
    long_body = "term " * 1000
    p = symbol_passage("name", "(sig)", long_body)
    assert len(p) == 800


# ── cache-key salt ──────────────────────────────────────────────────────────
def _old_hash(passage, model_name, dim, version, is_query):
    role = "q" if is_query else "p"
    sig = f"{version}:{model_name}:{dim}:{role}:{passage}"  # pre-change formula (no salt)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()


def test_passage_hash_off_byte_identical_to_old_formula(monkeypatch):
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    for name in (GTE, E5):
        for is_q in (False, True):
            got = passage_hash("foo (x)", name, 768, "sym2-fn", is_query=is_q)
            assert got == _old_hash("foo (x)", name, 768, "sym2-fn", is_q)


def test_passage_hash_on_salts_only_gte_passages(monkeypatch):
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    old_gte_p = _old_hash("foo (x)", GTE, 768, "sym2-fn", False)
    old_gte_q = _old_hash("foo (x)", GTE, 768, "sym2-fn", True)
    old_e5_p = _old_hash("foo (x)", E5, 384, "sym2-fn", False)

    # gte PASSAGE gets its own (salted) slot — must NOT collide with the 128 entry.
    assert passage_hash("foo (x)", GTE, 768, "sym2-fn", is_query=False) != old_gte_p
    # gte QUERY window is unchanged by the flag -> key stays identical (shares cache).
    assert passage_hash("foo (x)", GTE, 768, "sym2-fn", is_query=True) == old_gte_q
    # e5 window stays 128 even under the flag -> key stays identical (shares cache).
    assert passage_hash("foo (x)", E5, 384, "sym2-fn", is_query=False) == old_e5_p


# ── determinism ─────────────────────────────────────────────────────────────
def test_determinism_same_input_twice(monkeypatch):
    for flag in (None, "1"):
        if flag is None:
            monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
        else:
            monkeypatch.setenv("GT_PASSAGE_WIDE", flag)
        a = symbol_passage("f", "(a, b)", "compute redis tls handshake")
        b = symbol_passage("f", "(a, b)", "compute redis tls handshake")
        assert a == b
        assert passage_hash(a, GTE, 768, "sym2-fn") == passage_hash(b, GTE, 768, "sym2-fn")


# ── MUTATION companions (documented; each reddens a specific neuter) ──────────
def test_mutation_off_window_must_be_128_not_wide(monkeypatch):
    """If _passage_token_window dropped its _passage_wide() gate (always returned
    _PASSAGE_TOKEN_WINDOW_WIDE), this OFF assertion would red. Guards byte-identical-off."""
    monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    assert _passage_token_window(_model(GTE)) == embed._PASSAGE_TOKEN_WINDOW == 128


def test_mutation_e5_never_widens_even_on(monkeypatch):
    """If the e5 guard (`not _is_e5_model`) were neutered, e5 would widen to 256 and be
    salted — this reddens. Guards 'leave e5 at its window'."""
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    assert _passage_token_window(_model(E5)) == 128
    assert passage_hash("foo (x)", E5, 384, "sym2-fn", is_query=False) == _old_hash(
        "foo (x)", E5, 384, "sym2-fn", False
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

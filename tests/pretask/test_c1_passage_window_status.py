"""C1 (GT_PASSAGE_WIDE) audit witness — `embed.passage_window_status`.

The 5-task audit (run 28886910434) rendered gt_math row 40 as a blank `—`. That was a
MISLABEL: C1 is the per-symbol passage WIDTH knob (128->256 for gte/ModernBERT), default
OFF, and until now it emitted nothing an auditor could read — so the cell had no witness.
`passage_window_status()` is that witness: deterministic, runs no encoder, off-safe. With
the flag off it reports the 128/32 default (the honest "off — stated reason" the row now
carries); with it on, `active=True` proves the wide window actually ran.

SCOPE PIN: C1 is the width knob only. The broad-body passage CONTENT (source bodies,
docstrings, string literals, calls) is mined at index time (C2) and read by the semantic
leg (B2) — rows 41/30, ON independently of this flag. The status note records that so the
row is never conflated with "no body retrieval".
"""
from __future__ import annotations

import importlib

import groundtruth.memory.enrich.embed as embed


def _status(monkeypatch, *, wide):
    if wide:
        monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    else:
        monkeypatch.delenv("GT_PASSAGE_WIDE", raising=False)
    monkeypatch.delenv("GT_EMBED_ENCODE_BATCH", raising=False)
    return embed.passage_window_status()


def test_off_is_safe_default_128_32(monkeypatch):
    """Flag OFF (the default): the 128 window + 32 batch — byte-identical to pre-C1, and
    a STATED status ('off'), not a blank unmeasured cell."""
    s = _status(monkeypatch, wide=False)
    assert s["gt_passage_wide"] is False
    assert s["active"] is False
    assert s["passage_window"] == 128
    assert s["encode_batch"] == 32


def test_on_witnesses_wide_256_16(monkeypatch):
    """Flag ON: the wide 256 window + halved 16 batch (16*256 == 32*128, same exit-137
    budget). `active` True is the witness that C1 actually ran."""
    s = _status(monkeypatch, wide=True)
    assert s["gt_passage_wide"] is True
    assert s["active"] is True
    assert s["passage_window"] == 256
    assert s["encode_batch"] == 16


def test_scope_note_names_c2_b2(monkeypatch):
    """The status carries the C1-vs-C2/B2 distinction so row 40 is never read as
    'no body retrieval' (which is C2/B2, rows 41/30, independent of this flag)."""
    s = _status(monkeypatch, wide=False)
    assert "C1" in s["note"] and "C2/B2" in s["note"]


def test_deterministic(monkeypatch):
    """Same env, twice => identical status (no clock, no RNG)."""
    a = _status(monkeypatch, wide=True)
    b = _status(monkeypatch, wide=True)
    assert a == b


def test_e5_preserved_when_model_given(monkeypatch):
    """With a model argument the e5-vs-non-e5 split is honoured: e5 stays 128 even with
    the flag ON (its conservative window is preserved); the default gte family widens."""
    monkeypatch.setenv("GT_PASSAGE_WIDE", "1")
    importlib.reload(embed)  # ensure a clean module read of the flag helpers
    e5 = embed.EmbeddingModel(model_name=embed.E5_MODEL, dim=embed.E5_DIM)
    gte = embed.EmbeddingModel(model_name=embed.DEFAULT_EMBED_MODEL, dim=embed.DEFAULT_EMBED_DIM)
    assert embed.passage_window_status(e5)["passage_window"] == 128
    assert embed.passage_window_status(e5)["is_e5_preserved"] is True
    assert embed.passage_window_status(gte)["passage_window"] == 256

"""B-31 — the brief's "token" count must be a REAL tokenizer count, not char/4.

Finding (GT_BUG_JOURNEY_BRIEF B-31): the reported token count was
``len(text)//4 + 1`` — a character heuristic mislabeled as tokens. Dose
enforcement and reported efficiency vary materially by code / punctuation /
Unicode / tokenizer, so the char heuristic is dishonest.

Fix: ``_count_tokens`` prefers the baked HF ``tokenizers`` BPE vocabulary (the
gte tokenizer.json under ``GT_MODELS_ROOT``/``GT_TOKENIZER_JSON``) and counts
real BPE tokens; it falls back to the char/4 ESTIMATE only when the tokenizer is
unavailable. Deterministic + offline (the tokenizer.json is loaded from disk;
no network).

RED on the pre-fix tree: ``_count_tokens`` does not exist (ImportError). MUTATION
proof: forcing ``_count_tokens`` to ignore the tokenizer (return the estimate)
makes ``test_count_tokens_uses_real_tokenizer`` RED — the real BPE count no
longer matches the independently-loaded tokenizer.

Hermetic: the fallback test deletes the tokenizer env vars; the real-tokenizer
test builds a tiny tokenizer.json in a per-test tmp dir and points
``GT_TOKENIZER_JSON`` at it (the path-keyed cache keeps tests isolated).
"""

from __future__ import annotations

import pytest

from groundtruth.pretask.v1r_brief import _count_tokens, _estimate_tokens


_SAMPLE = "def foo(x):\n    return x + 1  # comment, with! punctuation??? and Unicode → ✓"
# Whitespace-split token count (16) is provably NOT the char/4 estimate (8) — used
# to prove the real tokenizer is consulted rather than the character heuristic.
_DIFFERS = " ".join("abcdefghijklmnop")


def _clear_tokenizer_env(monkeypatch) -> None:
    monkeypatch.delenv("GT_TOKENIZER_JSON", raising=False)
    monkeypatch.delenv("GT_MODELS_ROOT", raising=False)


def test_count_tokens_fallback_is_estimate(monkeypatch) -> None:
    """No tokenizer configured → _count_tokens == the char/4 ESTIMATE (honest
    fallback; never a hard failure)."""
    _clear_tokenizer_env(monkeypatch)
    assert _count_tokens(_SAMPLE) == _estimate_tokens(_SAMPLE)
    assert _count_tokens("") == _estimate_tokens("")


def _build_tiny_tokenizer(path: str) -> None:
    from tokenizers import Tokenizer, models, pre_tokenizers

    tk = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.Whitespace()
    tk.save(path)


def test_count_tokens_uses_real_tokenizer(tmp_path, monkeypatch) -> None:
    """A configured tokenizer is actually CONSULTED: _count_tokens equals the
    independently-loaded tokenizer's id count — and (for this sample) DIFFERS from
    the char/4 estimate, proving it is not the heuristic. This is the mutation
    target: making _count_tokens ignore the tokenizer breaks the first assert."""
    tokenizers = pytest.importorskip("tokenizers")
    tok_path = str(tmp_path / "tokenizer.json")
    _build_tiny_tokenizer(tok_path)
    monkeypatch.delenv("GT_MODELS_ROOT", raising=False)
    monkeypatch.setenv("GT_TOKENIZER_JSON", tok_path)

    independent = len(tokenizers.Tokenizer.from_file(tok_path).encode(_SAMPLE).ids)
    assert _count_tokens(_SAMPLE) == independent
    # A real tokenizer count is NOT the char/4 heuristic: on _DIFFERS the BPE count
    # (16 whitespace tokens) diverges sharply from the char/4 estimate (8).
    real = _count_tokens(_DIFFERS)
    assert real == len(tokenizers.Tokenizer.from_file(tok_path).encode(_DIFFERS).ids)
    assert real != _estimate_tokens(_DIFFERS)


def test_count_tokens_prefers_models_root(tmp_path, monkeypatch) -> None:
    """GT_MODELS_ROOT/gte-modernbert-base/tokenizer.json is the baked-substrate
    default location the count resolves when GT_TOKENIZER_JSON is unset."""
    pytest.importorskip("tokenizers")
    from tokenizers import Tokenizer

    root = tmp_path / "models"
    tdir = root / "gte-modernbert-base"
    tdir.mkdir(parents=True)
    _build_tiny_tokenizer(str(tdir / "tokenizer.json"))
    monkeypatch.delenv("GT_TOKENIZER_JSON", raising=False)
    monkeypatch.setenv("GT_MODELS_ROOT", str(root))

    tj = str(tdir / "tokenizer.json")
    assert _count_tokens(_SAMPLE) == len(Tokenizer.from_file(tj).encode(_SAMPLE).ids)
    # _DIFFERS proves the real tokenizer (16) is used, not the char/4 estimate (8).
    assert _count_tokens(_DIFFERS) == len(Tokenizer.from_file(tj).encode(_DIFFERS).ids)
    assert _count_tokens(_DIFFERS) != _estimate_tokens(_DIFFERS)


def test_estimate_is_still_char_over_four(monkeypatch) -> None:
    """The ESTIMATE helper is unchanged (char/4 + 1) — it is the honest fallback,
    only relabeled; existing byte-for-byte behavior of the estimate is preserved."""
    assert _estimate_tokens("abcd") == 2  # 4//4 + 1
    assert _estimate_tokens("a" * 400) == 101

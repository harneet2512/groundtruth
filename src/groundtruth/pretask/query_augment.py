"""v2.2 — graph-aware QueryObject augmentation.

Soft natural-word filter (SweRank §3.2): re-admit prose tokens filtered as
English by query_preprocessor when they match a real file basename or
symbol name in the indexed graph. Adds matched tokens to file_hints (if
file-basename match) or function_hints (if symbol match).
"""
from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path

from groundtruth.pretask.anchors import (
    _STOPWORDS,
    _extract_raw_identifiers,
)
from groundtruth.pretask.v2_types import HighSignalToken, QueryObject, TokenSource


def _load_graph_views(
    db_path: str,
) -> tuple[set[str], dict[str, str], set[str], dict[str, str], dict[str, int], dict[str, int]] | None:
    """Return (file_stems, stem_to_path, symbol_names, name_to_label, stem_df, sym_df) or None.

    stem_df: count of distinct file_paths sharing each stem (rarity gate).
    sym_df: count of distinct (file_path, name) rows for each symbol name.
    Returns None when the DB is missing or the read fails.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute("SELECT DISTINCT file_path FROM nodes")
            file_paths = sorted({row[0] for row in cur.fetchall() if row[0]})

            cur = conn.execute(
                "SELECT name, label, file_path FROM nodes "
                "WHERE label IN ('Function','Method','Class')"
            )
            sym_rows_full = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    stem_to_path: dict[str, str] = {}
    file_stems: set[str] = set()
    stem_df: dict[str, int] = {}
    for p in file_paths:
        stem = Path(p).stem
        if not stem:
            continue
        file_stems.add(stem)
        stem_to_path.setdefault(stem, p)
        stem_df[stem] = stem_df.get(stem, 0) + 1

    symbol_names: set[str] = set()
    name_to_label: dict[str, str] = {}
    sym_df: dict[str, int] = {}
    seen_pair: set[tuple[str, str]] = set()
    for row in sym_rows_full:
        name, label, fpath = row[0], row[1], row[2]
        if not name:
            continue
        symbol_names.add(name)
        prev = name_to_label.get(name)
        if prev is None or (prev != "Class" and label == "Class"):
            name_to_label[name] = label
        key = (name, fpath or "")
        if key not in seen_pair:
            seen_pair.add(key)
            sym_df[name] = sym_df.get(name, 0) + 1

    return file_stems, stem_to_path, symbol_names, name_to_label, stem_df, sym_df


def _existing_token_set(query: QueryObject) -> set[str]:
    return {
        *query.file_hints,
        *query.function_hints,
        *query.class_hints,
    }


def _has_high_signal_token(query: QueryObject, token: str) -> bool:
    return any(hst.token == token for hst in query.high_signal_tokens)


def _is_prose_shaped(token: str) -> bool:
    """True if token would be dropped by the preprocessor's symbol-shape gate.

    Preprocessor admits snake_case (has '_' and islower), camel_case (mixed
    case), and stack-trace / backtick / path tokens. A bare lowercase word
    with no underscore and no digits never enters file/function/class hints
    or high_signal_tokens via paths 5–6, regardless of length.
    """
    if "_" in token:
        return False
    if any(c.isdigit() for c in token):
        return False
    if not token.islower():
        return False
    return True


def _idf_weight(df: int, n_total: int) -> float:
    """Standard add-1 smoothed IDF in [0, log(n_total+1)]. Returns 0 if df<=0."""
    if df <= 0 or n_total <= 0:
        return 0.0
    return math.log((n_total + 1) / (df + 1)) + 1.0


def _normalized_weight(df: int, n_total: int) -> float:
    """IDF normalized so the rarest term scores ~1.0 and very common ~0.

    Common stems (df ≈ n_total) score near 0; unique stems (df=1) score near 1.
    No magic threshold: it's a continuous gate.
    """
    if df <= 0 or n_total <= 0:
        return 0.0
    max_idf = math.log((n_total + 1) / 2.0) + 1.0
    if max_idf <= 0:
        return 0.0
    return min(1.0, _idf_weight(df, n_total) / max_idf)


def _candidate_tokens(issue_text: str, existing: set[str]) -> list[str]:
    raw = _extract_raw_identifiers(issue_text)
    out: list[str] = []
    for tok in sorted(raw):
        head = tok.split(".")[-1] if "." in tok else tok
        if head in existing or tok in existing:
            continue
        if head.lower() in _STOPWORDS:
            continue
        if not _is_prose_shaped(head):
            continue
        out.append(head)
    return list(dict.fromkeys(out))


def augment_query_with_graph(
    query: QueryObject,
    issue_text: str,
    graph_db_path: str,
) -> QueryObject:
    """Return a NEW QueryObject augmented with graph-cross-checked prose tokens.

    Pure function; does not mutate input. If graph_db_path is unreadable or
    has no nodes, returns input unchanged.
    """
    views = _load_graph_views(graph_db_path)
    if views is None:
        return query

    file_stems, stem_to_path, symbol_names, name_to_label, stem_df, sym_df = views
    if not file_stems and not symbol_names:
        return query

    existing = _existing_token_set(query)
    candidates = _candidate_tokens(issue_text, existing)
    if not candidates:
        return query

    new_file_hints = list(query.file_hints)
    new_function_hints = list(query.function_hints)
    new_class_hints = list(query.class_hints)
    new_tokens = list(query.high_signal_tokens)

    n_files = max(len(file_stems), 1)
    n_symbols = max(len(symbol_names), 1)

    def _maybe_add_hst(token: str, weight: float, source: TokenSource) -> None:
        if not any(t.token == token and t.source == source for t in new_tokens):
            new_tokens.append(HighSignalToken(token=token, weight=weight, source=source))

    for tok in candidates:
        source: TokenSource = "snake_case" if "_" in tok else "camel_case"

        if tok in file_stems:
            w = _normalized_weight(stem_df.get(tok, 0), n_files)
            if w > 0.0:
                _maybe_add_hst(tok, 2.0 * w, source)
            continue

        if tok in symbol_names:
            w = _normalized_weight(sym_df.get(tok, 0), n_symbols)
            if w > 0.0:
                _maybe_add_hst(tok, 2.0 * w, source)
            continue

    return QueryObject(
        file_hints=new_file_hints,
        function_hints=new_function_hints,
        class_hints=new_class_hints,
        high_signal_tokens=new_tokens,
        code_blocks=list(query.code_blocks),
        raw_text=query.raw_text,
    )

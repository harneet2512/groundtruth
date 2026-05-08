"""Track A — Deterministic preprocessor for SWE-Bench Lite issue text.

Pure regex extraction. No LLM, no embeddings, no graph DB lookup. Track B
consumes the QueryObject and performs the cross-check against graph.db.
"""
from __future__ import annotations

import os
import re

from groundtruth.pretask.anchors import (
    _IDENT_RE,
    _PATH_RE,
    _STOPWORDS,
    _extract_paths,
    _extract_raw_identifiers,
    _looks_like_natural_word,
)
from groundtruth.pretask.traces import parse_stack_traces
from groundtruth.pretask.v2_types import (
    HighSignalToken,
    QueryObject,
    TokenSource,
)

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FENCED_BLOCK_RE = re.compile(r"```(?:\w+)?\n?(.*?)```", re.DOTALL)
_ERROR_SUFFIXES = ("Error", "Exception", "Warning")


def _is_camel_case(token: str) -> bool:
    if token.isupper() or token.islower():
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    return has_upper and has_lower


def _is_error_class_name(token: str) -> bool:
    if not _is_camel_case(token):
        return False
    return any(token.endswith(s) for s in _ERROR_SUFFIXES)


def _is_identifier_shaped(token: str) -> bool:
    return bool(_IDENT_RE.fullmatch(token))


def _is_path_shaped(token: str) -> bool:
    return bool(_PATH_RE.fullmatch(f"`{token}`") or _PATH_RE.fullmatch(token))


def _dedup_preserve_order(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


class _TokenAccumulator:
    """Accumulates HighSignalToken entries, keeping max weight per (token, source)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, TokenSource], float] = {}
        self._order: list[tuple[str, TokenSource]] = []

    def add(self, token: str, weight: float, source: TokenSource) -> None:
        if not token:
            return
        key = (token, source)
        prev = self._by_key.get(key)
        if prev is None:
            self._by_key[key] = weight
            self._order.append(key)
        elif weight > prev:
            self._by_key[key] = weight

    def finalize(self) -> list[HighSignalToken]:
        return [
            HighSignalToken(token=tok, weight=self._by_key[(tok, src)], source=src)
            for (tok, src) in self._order
        ]


def preprocess(issue_text: str) -> QueryObject:
    if not issue_text:
        return QueryObject(raw_text="")

    file_hints: list[str] = []
    function_hints: list[str] = []
    class_hints: list[str] = []
    code_blocks: list[str] = []
    tokens = _TokenAccumulator()
    seen_in_traces_or_backtick: set[str] = set()

    # 1. Stack traces. Use "." as repo_root so the relative-path fallback in
    # traces._is_in_repo accepts repo-relative paths like "src/foo.py".
    frames = parse_stack_traces(issue_text, repo_root=".")
    for frame in frames:
        if frame.file:
            file_hints.append(frame.file)
        if frame.func:
            function_hints.append(frame.func)
            tokens.add(frame.func, 4.0, "stack_trace")
            seen_in_traces_or_backtick.add(frame.func)
        if frame.file:
            base = os.path.basename(frame.file)
            stem, _ = os.path.splitext(base)
            if stem:
                tokens.add(stem, 4.0, "stack_trace")
                seen_in_traces_or_backtick.add(stem)

    # 2. Backtick spans.
    for span in _BACKTICK_RE.findall(issue_text):
        span = span.strip()
        if not span:
            continue
        if _is_path_shaped(span):
            file_hints.append(span)
            continue
        if _is_identifier_shaped(span):
            if span.lower() not in _STOPWORDS:
                tokens.add(span, 3.0, "backtick")
                seen_in_traces_or_backtick.add(span)
                if _is_error_class_name(span):
                    class_hints.append(span)
            if "." in span:
                for part in span.split("."):
                    if not part:
                        continue
                    if not (len(part) >= 3 or part.startswith("_")):
                        continue
                    if part.lower() in _STOPWORDS:
                        continue
                    tokens.add(part, 3.0, "backtick")
                    seen_in_traces_or_backtick.add(part)
                    if _is_error_class_name(part):
                        class_hints.append(part)

    # 3. Fenced code blocks.
    for block in _FENCED_BLOCK_RE.findall(issue_text):
        code_blocks.append(block)
        for tok in _extract_raw_identifiers(block):
            if tok in seen_in_traces_or_backtick:
                continue
            head = tok.split(".")[-1] if "." in tok else tok
            if head.lower() in _STOPWORDS:
                continue
            # Code blocks routinely contain generic exception names
            # (ValueError, RuntimeError) in tracebacks. Adding those to
            # class_hints pollutes the hint-prefix that rank_files passes
            # to v7.4. Keep token-level signal via high_signal_tokens only.
            tokens.add(tok, 3.5, "backtick")
            seen_in_traces_or_backtick.add(tok)

    # 4. Title.
    title = ""
    for line in issue_text.splitlines():
        stripped = line.strip()
        if stripped:
            title = stripped[:200]
            break
    if title:
        for tok in _extract_raw_identifiers(title):
            head = tok.split(".")[-1] if "." in tok else tok
            if head.lower() in _STOPWORDS:
                continue
            if _looks_like_natural_word(head):
                continue
            tokens.add(tok, 1.5, "title")

    # 5 + 6. snake_case / camel_case / error_class from full text.
    all_idents = _extract_raw_identifiers(issue_text)
    for tok in all_idents:
        if tok in seen_in_traces_or_backtick:
            continue
        head = tok.split(".")[-1] if "." in tok else tok
        if head.lower() in _STOPWORDS:
            continue
        if _looks_like_natural_word(head):
            continue
        if "_" in tok and tok.islower():
            tokens.add(tok, 2.0, "snake_case")
            continue
        if _is_camel_case(tok):
            if _is_error_class_name(tok):
                class_hints.append(tok)
                tokens.add(tok, 3.0, "error_class")
            else:
                tokens.add(tok, 2.0, "camel_case")

    # 7. Paths from prose.
    for path in _extract_paths(issue_text):
        file_hints.append(path)

    return QueryObject(
        file_hints=_dedup_preserve_order(file_hints),
        function_hints=_dedup_preserve_order(function_hints),
        class_hints=_dedup_preserve_order(class_hints),
        high_signal_tokens=tokens.finalize(),
        code_blocks=code_blocks,
        raw_text=issue_text,
    )

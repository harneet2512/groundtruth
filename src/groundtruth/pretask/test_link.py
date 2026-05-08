"""v2.2 — test name to source-file linkage.

For each pytest-style or test_*-style name extracted from issue text, score
candidate source files by the likelihood that they're the file-under-test.
Returns per-file 0-1 score for use as a multiplicative file-rank boost.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from groundtruth.pretask.v2_types import QueryObject

_TEST_PREFIX_RE = re.compile(r"^[Tt]est[_]?")
_TEST_SUFFIX_RE = re.compile(r"[_]?[Tt]est$")
_CAMEL_SPLIT_RE = re.compile(r"(?<!^)(?=[A-Z])")
_TEST_TOKEN_RE = re.compile(r"\b(?:test_[A-Za-z_][A-Za-z0-9_]*|[A-Za-z][A-Za-z0-9_]*_test|test[A-Z][A-Za-z0-9]*|Test[A-Z][A-Za-z0-9]*)\b")


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/")


def _is_test_file(file_path: str) -> bool:
    norm = _normalize_path(file_path).lower()
    parts = [seg for seg in norm.split("/") if seg]
    if not parts:
        return False
    stem = PurePosixPath(norm).stem
    if stem.startswith("test_") or stem.endswith("_test"):
        return True
    if stem == "test" or stem == "tests":
        return True
    for seg in parts[:-1]:
        if seg in {"test", "tests", "testing", "__tests__"}:
            return True
    return False


def _looks_like_test_name(name: str) -> bool:
    if not name:
        return False
    if name.startswith("test_") or name.endswith("_test"):
        return True
    if name.startswith("test") and len(name) > 4 and name[4].isupper():
        return True
    if name.startswith("Test") and len(name) > 4 and name[4].isupper():
        return True
    return False


def extract_test_names_from_query(query: QueryObject) -> set[str]:
    """Return all test-shaped names from query (pytest test_X, X_test, etc.)."""
    names: set[str] = set()

    for fn in query.function_hints:
        if _looks_like_test_name(fn):
            names.add(fn)

    for fh in query.file_hints:
        norm = _normalize_path(fh)
        stem = PurePosixPath(norm).stem
        if _looks_like_test_name(stem):
            names.add(stem)

    for tok in query.high_signal_tokens:
        if _looks_like_test_name(tok.token):
            names.add(tok.token)

    if query.raw_text:
        for match in _TEST_TOKEN_RE.findall(query.raw_text):
            if _looks_like_test_name(match):
                names.add(match)

    for cls in query.class_hints:
        if _looks_like_test_name(cls):
            names.add(cls)

    return names


def _camel_to_snake(name: str) -> str:
    return _CAMEL_SPLIT_RE.sub("_", name).lower()


def candidate_source_stems(test_name: str) -> set[str]:
    """For a single test name, return possible source-file stems (lowercased)."""
    if not test_name:
        return set()

    stems: set[str] = set()
    core = test_name

    if core.startswith("test_"):
        core = core[len("test_"):]
    elif core.endswith("_test"):
        core = core[: -len("_test")]
    elif core.startswith("Test") and len(core) > 4 and core[4].isupper():
        core = core[len("Test"):]
    elif core.startswith("test") and len(core) > 4 and core[4].isupper():
        core = core[len("test"):]

    if not core:
        return set()

    if "_" in core:
        stems.add(core.lower())
        parts = [p for p in core.split("_") if p]
        if parts:
            stems.add(parts[0].lower())
            if len(parts) > 1:
                stems.add(parts[-1].lower())
                # full compound is already added above
    else:
        if any(c.isupper() for c in core[1:]):
            snake = _camel_to_snake(core)
            stems.add(snake)
            stems.add(core.lower())
            stems.add(core.lower().replace("_", ""))
            # also first camel chunk
            chunks = _CAMEL_SPLIT_RE.split(core)
            if chunks:
                stems.add(chunks[0].lower())
        else:
            stems.add(core.lower())

    return {s for s in stems if s}


def score_test_to_source(
    candidate_files: list[str],
    query: QueryObject,
) -> dict[str, float]:
    """Return {file_path: 0-1 score} based on test-to-source linkage."""
    test_names = extract_test_names_from_query(query)
    if not test_names:
        return {}

    all_stems: set[str] = set()
    for tn in test_names:
        all_stems.update(candidate_source_stems(tn))
    if not all_stems:
        return {}

    raw: dict[str, float] = {}
    for f in candidate_files:
        if not f:
            continue
        if _is_test_file(f):
            continue

        norm = _normalize_path(f)
        stem = PurePosixPath(norm).stem.lower()
        segments = [seg.lower() for seg in PurePosixPath(norm).parts if seg]
        # exclude trailing filename from segment match (we already check stem)
        path_segments = segments[:-1] if segments else []

        best = 0.0
        for cand in all_stems:
            if not cand:
                continue
            if stem == cand:
                score = 1.0
            elif cand in stem or stem in cand:
                score = 0.7
            elif any(cand == seg or cand in seg for seg in path_segments):
                score = 0.5
            else:
                score = 0.0
            if score > best:
                best = score

        if best > 0.0:
            raw[f] = best

    if not raw:
        return {}

    peak = max(raw.values())
    if peak <= 0.0:
        return {}
    return {k: v / peak for k, v in raw.items()}

"""Module 1 — Issue-text anchor extraction.

Extracts symbol names, file paths, and test names from an issue body using
deterministic regex patterns. Symbols are then cross-checked against
``nodes.name`` in graph.db so that natural-language false positives
(e.g. ``broken``, ``implementation``) are dropped before they leak into the
PPR seed set.

Pure regex + sqlite. No LLM, no tree-sitter dependency at runtime — fenced
code blocks are scanned with the same identifier regex as prose, which is
sufficient for symbol surface forms (CamelCase, snake_case, dotted).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

# ----------------------------------------------------------------- regex set
# Identifier surface forms we care about: CamelCase, snake_case, dotted (a.b.c).
# Min length 3 to drop "is", "to", etc. Keeps leading underscore for dunder
# attrs (``_fd``, ``__init__``).
_IDENT_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]+)*)\b"
)

# Backtick-wrapped paths OR bare paths with known source extensions.
_PATH_EXTS = (
    r"py|pyi|js|jsx|ts|tsx|go|rs|java|kt|kts|c|h|cc|hh|cpp|hpp|"
    r"rb|php|cs|swift|m|mm|scala|clj|ex|exs|lua|sh"
)
_PATH_RE = re.compile(
    rf"(?:`([^`\n]+\.(?:{_PATH_EXTS}))`"
    rf"|(?<![\w/])([\w./\\-]+\.(?:{_PATH_EXTS}))\b)"
)

# Pytest-style test names (test_*, *_test).
_TEST_NAME_RE = re.compile(r"\b(test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)\b")

# English/common-word stopwords — also: programming-language keywords that
# look identifier-shaped but never resolve to graph nodes.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # English filler
        "the", "and", "for", "this", "that", "with", "from", "into", "have",
        "has", "had", "was", "were", "will", "would", "should", "could",
        "does", "did", "are", "but", "not", "can", "may", "might", "must",
        "use", "used", "uses", "using", "see", "any", "all", "some", "one",
        "two", "three", "four", "five", "ten", "now", "new", "old", "yes",
        "off", "out", "via", "per", "non", "yet", "say", "set", "get",
        "put", "let", "got", "make", "made", "want", "need", "give", "find",
        "back", "down", "over", "such", "then", "than", "very", "much",
        "more", "less", "well", "long", "high", "low", "left", "right",
        "same", "different", "still", "even", "thus", "also", "again",
        # issue/bug filler
        "fix", "fixed", "fixing", "bug", "bugs", "issue", "issues",
        "error", "errors", "fail", "fails", "failed", "failure", "failures",
        "broken", "break", "breaks", "expected", "actual", "result",
        "results", "value", "values", "implementation", "behavior",
        "behaviour", "problem", "problems", "regression", "regressions",
        "crash", "crashes", "wrong", "incorrect", "correct", "correctly",
        "since", "before", "after", "while", "when", "where", "why", "how",
        "what", "which", "whose", "whom",
        # generic noun-ish
        "test", "tests", "testing", "code", "codes", "file", "files",
        "function", "functions", "class", "classes", "method", "methods",
        "type", "types", "object", "objects", "exception", "exceptions",
        "raise", "raises", "raised", "return", "returns", "returned",
        "import", "imports", "imported", "module", "modules", "package",
        "packages", "library", "libraries", "version", "versions",
        # python keywords / builtins seen in prose
        "true", "false", "none", "null", "self", "cls", "args", "kwargs",
        "python", "java", "javascript", "typescript", "rust", "golang",
        # boilerplate verbs
        "called", "called", "calling", "called", "ran", "run", "running",
        "found", "see", "look", "looking", "looked", "show", "shows",
        "showed", "follow", "follows", "followed", "throw", "throws",
        "thrown", "catch", "caught", "log", "logs", "logged", "print",
        "prints", "printed",
    }
)


@dataclass
class IssueAnchors:
    """Concrete anchors extracted from an issue body.

    Attributes:
        symbols: Symbol names that ALSO exist as ``nodes.name`` in the
            indexed graph. Natural-language false positives are dropped here.
        paths: Repository-relative or backtick-wrapped paths mentioned
            verbatim in the issue body. Returned as strings (not resolved
            to graph file_paths) so the renderer can still surface a
            user-mentioned path even if no symbol from it ranked.
        test_names: Pytest-style test names referenced in the body
            (e.g. ``test_storage_persists``).
        symbols_raw: Pre-cross-check raw identifier candidates after the
            stopword filter. Telemetry only — the orchestrator must use
            ``symbols`` for downstream seeding.
        symbols_pre_stopword: Identifier candidates BEFORE stopwording.
            Telemetry only.
    """

    symbols: set[str] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    test_names: set[str] = field(default_factory=set)
    symbols_raw: set[str] = field(default_factory=set)
    symbols_pre_stopword: set[str] = field(default_factory=set)


def _looks_like_natural_word(token: str) -> bool:
    """True if a token is almost certainly an English word, not a symbol.

    Heuristics: all-lower, no underscore, no digits, length < 5. This errs
    toward removing words like ``data``, ``user``, ``size`` from the seed
    pool unless they reappear as actual graph nodes — at which point the
    cross-check restores them.
    """
    if "_" in token:
        return False
    if any(c.isdigit() for c in token):
        return False
    if not token.islower():
        return False
    return len(token) < 5


def _extract_raw_identifiers(text: str) -> set[str]:
    """Pull every identifier-shaped token from the issue body.

    For dotted paths (``module.Class.method``) the LAST component is added
    in addition to the full dotted form, since the graph stores symbols by
    their bare name.
    """
    out: set[str] = set()
    for match in _IDENT_RE.finditer(text):
        token = match.group(1)
        out.add(token)
        if "." in token:
            # Add every dotted segment that on its own looks like an
            # identifier (length >= 3 OR begins with underscore so dunder
            # attrs like ``_fd`` survive).
            for part in token.split("."):
                if not part:
                    continue
                if len(part) >= 3 or part.startswith("_"):
                    out.add(part)
    return out


def _extract_paths(text: str) -> set[str]:
    """Pull file-path mentions from the issue body."""
    out: set[str] = set()
    for match in _PATH_RE.finditer(text):
        path = match.group(1) or match.group(2)
        if path:
            out.add(path.strip())
    return out


def _extract_test_names(text: str) -> set[str]:
    """Pull pytest-style test function names from the issue body."""
    return {m.group(1) for m in _TEST_NAME_RE.finditer(text)}


def _cross_check_against_graph(
    candidates: set[str],
    db_path: str | None,
) -> set[str]:
    """Filter candidates to only those present in graph.db ``nodes.name``.

    If ``db_path`` is None or unreadable, returns the input unchanged so
    that the pipeline degrades gracefully on missing-DB tasks. Telemetry
    will record ``graph_node_count = 0`` separately.
    """
    if not db_path or not candidates:
        return set(candidates)
    try:
        conn = sqlite3.connect(db_path)
        try:
            placeholders = ",".join("?" for _ in candidates)
            cursor = conn.execute(
                f"SELECT DISTINCT name FROM nodes WHERE name IN ({placeholders})",
                tuple(candidates),
            )
            return {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def extract_issue_anchors(
    issue_text: str,
    graph_db_path: str | None = None,
) -> IssueAnchors:
    """Extract symbols, file paths, and test names from issue text.

    Args:
        issue_text: Raw issue body (markdown / plaintext).
        graph_db_path: Path to graph.db. If provided, symbols are
            cross-checked against ``nodes.name`` and only matches survive.
            If ``None``, no cross-check is performed (used in unit tests
            that don't need a DB).

    Returns:
        IssueAnchors with both filtered (``symbols``) and pre-filter
        (``symbols_raw``, ``symbols_pre_stopword``) views, for telemetry.
    """
    if not issue_text:
        return IssueAnchors()

    raw_idents = _extract_raw_identifiers(issue_text)

    after_stopword: set[str] = set()
    for tok in raw_idents:
        # Compare lowercased dotted-tail against stopwords too.
        head = tok.split(".")[-1] if "." in tok else tok
        if head.lower() in _STOPWORDS:
            continue
        if _looks_like_natural_word(head):
            continue
        after_stopword.add(tok)

    resolved = _cross_check_against_graph(after_stopword, graph_db_path)

    return IssueAnchors(
        symbols=resolved,
        paths=_extract_paths(issue_text),
        test_names=_extract_test_names(issue_text),
        symbols_raw=after_stopword,
        symbols_pre_stopword=raw_idents,
    )

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

from groundtruth.confidence import is_seed_pollutant

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

# English closed-class FUNCTION words (articles, conjunctions, prepositions,
# pronouns, auxiliaries). Language invariants that are never useful localization
# anchors even when they collide with a node name — a cheap pre-filter before the
# graph cross-check (and the ONLY filter on the no-DB unit path). DOMAIN words that
# look like code (run/get/set/new/type/value/error/test/class/...) are deliberately
# NOT here: whether they are anchors is decided by the graph cross-check + per-repo
# symbol_specificity (see _drop_generic_hubs), not by a static blocklist. The old
# 190-word _STOPWORDS dropped real short/domain symbols — the false-negative poison.
_NL_FUNCTION_WORDS: frozenset[str] = frozenset({
    "the", "and", "for", "nor", "but", "yet",
    "this", "that", "these", "those", "there", "here",
    "with", "without", "within", "from", "into", "onto", "over", "under",
    "about", "after", "before", "between", "through", "during", "against",
    "are", "was", "were", "been", "being", "has", "have", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "does", "did", "not", "yes",
    "then", "than", "when", "where", "why", "how", "which", "what",
    "who", "whom", "whose", "they", "them", "their", "its", "our", "your",
    "very", "just", "only", "also", "too", "more", "most", "less",
    "some", "any", "all", "each", "both", "few", "many", "such", "same",
})


# ---------------------------------------------------------------------------
# LEXICAL-QUERY stopwords — RETAINED for query_preprocessor / query_augment,
# where stripping English stopwords from a BM25/FTS *query* is standard, cited IR
# practice (every IR engine ships a default stopword list). This is NOT used for
# SYMBOL/anchor filtering any more: extract_issue_anchors uses _NL_FUNCTION_WORDS
# + per-repo symbol_specificity (_drop_generic_hubs), because a broad list used
# as a symbol blocklist dropped real short/domain symbols. Do NOT route anchor
# extraction back through _STOPWORDS / _looks_like_natural_word.
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


def _looks_like_natural_word(token: str) -> bool:
    """LEXICAL-QUERY heuristic (query_preprocessor only) — True if a token is
    almost certainly an English word, not a symbol. NOT used for anchor extraction
    (see _drop_generic_hubs). Heuristics: all-lower, no underscore, no digits,
    length < 5.
    """
    if "_" in token:
        return False
    if any(c.isdigit() for c in token):
        return False
    if not token.islower():
        return False
    return len(token) < 5


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
    # PROVENANCE tier — the subset of ``symbols`` that appears in the issue
    # TITLE / markdown headings (the report summary), NOT only in a fenced code
    # block or traceback. Research: BugLocator (Zhou et al., ICSE 2012) weights
    # the bug-report summary above the description; Schröter et al. (MSR 2010) +
    # Moreno et al. (ICSME 2014) show the fix site is in the stack trace only
    # ~60% of the time and rarely the top frame — so stack-frame symbols are a
    # WEAK localization signal. Consumers rank ``title_symbols`` above the rest
    # so a titled symbol (e.g. ``set_fields``) beats stack-frame noise
    # (``main``/``import_asis`` from a pasted traceback).
    title_symbols: set[str] = field(default_factory=set)
    # CODE provenance — symbols found inside backtick-wrapped regions (inline
    # ``code`` or fenced ``` blocks) in the issue body. The reporter explicitly
    # marked these as code, so they are HIGH-confidence anchors. Symbols found
    # ONLY in prose (never in backticks) that are short common words (≤5 chars,
    # all-lowercase, no underscore) are likely English verbs coinciding with
    # function names (``check``, ``set``, ``run``, ``add``, ``log``) — the flask-
    # 5637 false-positive class where "check" the verb → ``check()`` in
    # ``json/tag.py`` misdirected the ranker. Research: Reformulate, Retrieve,
    # Localize (arXiv:2512.07022, 2025) distinguishes code mentions from prose;
    # Query Reduction for Bug Localization (Mejia-Bernal et al., JSS 2025) shows
    # raw keywords are noisy queries — reducing/reweighting improves precision.
    code_symbols: set[str] = field(default_factory=set)


# Backtick-wrapped inline code: `symbol` or `module.symbol`
_BACKTICK_CODE_RE = re.compile(r"`([^`\n]+)`")


def _extract_code_region_identifiers(text: str) -> set[str]:
    """Extract identifiers from backtick-wrapped and fenced-code regions only.

    These are the symbols the reporter explicitly marked as code — highest-
    confidence localization anchors. Prose words that happen to match function
    names (``check``, ``set``) are NOT in this set unless backtick-wrapped.
    """
    out: set[str] = set()
    # Inline backtick code: `request.trusted_hosts`, `check()`
    for m in _BACKTICK_CODE_RE.finditer(text):
        snippet = m.group(1).strip()
        for ident_m in _IDENT_RE.finditer(snippet):
            tok = ident_m.group(1)
            out.add(tok)
            if "." in tok:
                for part in tok.split("."):
                    if part and (len(part) >= 3 or part.startswith("_")):
                        out.add(part)
    # Fenced code blocks (``` ... ```)
    for fence_m in _CODE_FENCE_RE.finditer(text):
        block = fence_m.group(0)
        for ident_m in _IDENT_RE.finditer(block):
            tok = ident_m.group(1)
            out.add(tok)
            if "." in tok:
                for part in tok.split("."):
                    if part and (len(part) >= 3 or part.startswith("_")):
                        out.add(part)
    return out


def _is_prose_only_common_word(sym: str, code_idents: set[str]) -> bool:
    """True if a symbol is likely an English verb coinciding with a function name.

    Gate: appears ONLY in prose (never in backtick/code), AND is short (≤5),
    all-lowercase, no underscore. Catches: check, set, get, run, add, log, call,
    send, load, dump, copy, move, find, sort, read, open, close, write, parse,
    match, split, strip, join, start, stop, flush, clear, reset, apply, build.
    """
    if sym in code_idents:
        return False  # explicitly marked as code — trust it
    s = sym.lower()
    if s != sym:
        return False  # has uppercase — likely a real symbol (CamelCase)
    if "_" in sym:
        return False  # has underscore — likely a real symbol (snake_case)
    if len(sym) > 5:
        return False  # long enough to be distinctive
    return True


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


# Fenced ``` code blocks (and ~~~). Stripped from the title region so a snippet
# pasted right under the title can't leak stack frames into the high-signal tier.
_CODE_FENCE_RE = re.compile(r"(```|~~~).*?(```|~~~)", re.DOTALL)


def _extract_title_region(text: str) -> str:
    """Return the high-signal TITLE / heading region of an issue.

    The first non-empty line (the report summary/title) plus any markdown ATX
    headings (``## Section``), with fenced code blocks removed. Generalised —
    every issue has a first line, and titles/headings never contain stack
    frames. Research: BugLocator ICSE 2012 (summary >> description), Schröter
    MSR 2010 / Moreno ICSME 2014 (stack frames are weak fix-locators).
    """
    no_code = _CODE_FENCE_RE.sub(" ", text)
    lines = no_code.splitlines()
    region: list[str] = []
    for ln in lines:
        if ln.strip():
            region.append(ln.strip().lstrip("#").strip())  # title = first non-empty line
            break
    for ln in lines:  # markdown ATX headings anywhere are section titles
        s = ln.strip()
        if s.startswith("#"):
            region.append(s.lstrip("#").strip())
    return "\n".join(region)


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


def _drop_generic_hubs(symbols: set[str], db_path: str | None) -> set[str]:
    """Suppress graph-resolved anchors that would POLLUTE seeding — the dynamic +
    confidence-gated replacement for the static symbol blocklist.

    DROP a resolved symbol iff ``is_seed_pollutant`` (confidence.py): it is a HOMONYM
    (defined in more files than the repo's P95 definition count) or a dunder. The
    bound is the repo's OWN 95th percentile (data-derived, no magic threshold), so
    each repo gets its own bar. We gate on DEFINITION-frequency (Aider's production
    genericness signal), NOT in-degree: a uniquely-defined symbol is a precise seed
    even when highly called (in-degree conflates importance with genericness — Step-2
    finding #1) and is merely deprioritized by symbol_specificity in RANKING. Short /
    domain-shaped names the old _STOPWORDS / _looks_like_natural_word wrongly dropped
    are kept.

    Correct-or-quiet: no DB / <=1 symbol -> keep unchanged; never let the gate empty
    the anchor set (fall back to the full resolved set so downstream ranking can
    re-weight rather than the agent being blinded).
    """
    if not db_path or len(symbols) <= 1:
        return set(symbols)
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return set(symbols)
    try:
        kept = {s for s in symbols if not is_seed_pollutant(s, conn)}
        return kept or set(symbols)
    except sqlite3.Error:
        return set(symbols)
    finally:
        conn.close()


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

    after_filter: set[str] = set()
    for tok in raw_idents:
        # Drop ONLY English closed-class function words (dotted-tail aware). Domain /
        # short / code-shaped tokens pass through to the graph cross-check + per-repo
        # specificity gate, which decide by data — not by a blocklist.
        head = tok.split(".")[-1] if "." in tok else tok
        if head.lower() in _NL_FUNCTION_WORDS:
            continue
        after_filter.add(tok)

    resolved = _cross_check_against_graph(after_filter, graph_db_path)
    resolved = _drop_generic_hubs(resolved, graph_db_path)

    # PROVENANCE tier (BugLocator ICSE 2012; Schröter MSR 2010): the resolved
    # symbols that also occur in the TITLE / heading region are the high-signal
    # localization anchors; everything else (body + pasted traceback) is the
    # weak tier. Consumers rank title_symbols first so stack-frame pollution
    # (main/import_asis/apply_choice…) no longer ties with the titled target.
    _title_idents = _extract_raw_identifiers(_extract_title_region(issue_text))
    title_symbols = {s for s in resolved if s in _title_idents}

    # CODE provenance (Reformulate, Retrieve, Localize arXiv:2512.07022, 2025;
    # Query Reduction for Bug Localization Mejia-Bernal et al. JSS 2025):
    # symbols the reporter explicitly backtick-wrapped are code references.
    # Symbols found ONLY in prose that are short common words (≤5, lowercase,
    # no underscore) are likely English verbs — downweight them so a verb like
    # "check" in "configure and check trusted_hosts" doesn't seed a false graph
    # witness to json/tag.py::check() (the flask-5637 mislocalization).
    _code_idents = _extract_code_region_identifiers(issue_text)
    code_symbols = {s for s in resolved if s in _code_idents}
    # Remove prose-only common words from the main anchor set. They stay in
    # symbols_raw for telemetry but don't seed the graph localizer. This is
    # STRONGER than downweighting — a false seed produces a false witness that
    # the ranker amplifies. Correct-or-quiet: remove the seed, don't let it
    # propagate. The code_symbols set preserves any backtick-wrapped short word
    # (if the reporter wrote `check()` explicitly, it stays).
    _prose_demoted: set[str] = set()
    for s in list(resolved):
        if _is_prose_only_common_word(s, _code_idents):
            _prose_demoted.add(s)
            resolved.discard(s)

    return IssueAnchors(
        symbols=resolved,
        paths=_extract_paths(issue_text),
        test_names=_extract_test_names(issue_text),
        symbols_raw=after_filter,
        symbols_pre_stopword=raw_idents,
        title_symbols=title_symbols,
        code_symbols=code_symbols,
    )

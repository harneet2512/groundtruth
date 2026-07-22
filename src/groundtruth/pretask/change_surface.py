"""W-A — the ABSENCE / ARCHITECTURAL-HOLE engine.

Today GroundTruth can only RANK files that already exist. For a feature-add
issue ("add an azure provider") the gold is frequently a file that does **not**
exist yet — grep and the call graph both return nothing at the exact moment the
agent needs to know *where the change goes*. This engine closes that hole.

Given the issue text + the repo tree (+ optional ``graph.db``), it emits
HYPOTHESIS-tier facts about the change surfaces a new capability is MISSING,
derived entirely from the repo's OWN conventions:

  * :class:`MissingRole` — a change surface the sibling convention says should
    exist for the new entity but does not (implementation, config/schema,
    registration/export, test), each carrying the concrete sibling evidence.
  * :class:`NewFileDestination` — where the new file should live (directory,
    nearest sibling template, the registration integration point).

Method (all deterministic, LLM-free, no network, stdlib + sqlite3 only):

1. **Entity extraction** — the capability tokens the issue names that either
   resolve NOWHERE in the repo (the new-file signal) or name an existing sibling
   with a partial wiring hole.
2. **Sibling-group mining** — parallel siblings are files whose path token
   sequences are identical *except one position* (``providers/aws.py`` vs
   ``providers/gcp.py``; ``config/aws_config.py`` vs ``config/gcp_config.py``;
   ``plugins/aws/__init__.py`` vs ``plugins/gcp/__init__.py``). No hardcoded
   framework list — the group emerges from the tree.
3. **Role classification** — implementation (source file defining graph nodes),
   config/schema (path/name tokens like ``config``/``schema`` or a data file),
   registration/export (a file whose CONTENT cross-references >=2 sibling entity
   names — a registry/factory/``__init__`` detected by counting, not by name),
   test (test-shaped paths).
4. **Role template** = roles present for >=2 existing siblings; diff the new
   entity's presence against it -> a :class:`MissingRole` per absent role.
5. **Destination ranking** — the sibling directory, the richest sibling file as
   the template, the registration file as the integration point.

CORRECT-OR-QUIET is enforced throughout: abstain (empty result) when there is no
admissible entity token, no sibling group with >=2 members, an ambiguous
(conflicting) destination, or fewer than 2 independent evidence signals for a
fact. The entity itself obeys the two-signal law: adjacency to the category noun
("azure provider") is ONE signal and never mints alone — the token must also be
either novel (resolves nowhere in the repo's code identifiers, and not a generic
English descriptor like "additional"/"custom") or an existing sibling member
(the partial-hole case). Wrong evidence is worse than none.

LEAK LAW: role-level facts may quote sibling FILE PATHS and registry (non-test)
lines as convention evidence, but never test bodies / assertions / anything that
resembles a benchmark's graded tests.

Honored behind the ``GT_CHANGE_SURFACE`` env flag: when unset / ``0`` / ``false``
/ ``no`` / ``off`` the public entry point returns an empty result immediately and
does no work.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from groundtruth.pretask.anchors import (
    _LANG_KEYWORD_TOKENS,
    _NL_FUNCTION_WORDS,
    _STOPWORDS,
    extract_issue_anchors,
)
from groundtruth.pretask.stratum import _FEATURE_VERBS

__all__ = [
    "TRUST_HYPOTHESIS",
    "ROLE_IMPLEMENTATION",
    "ROLE_CONFIG",
    "ROLE_REGISTRATION",
    "ROLE_TEST",
    "MissingRole",
    "NewFileDestination",
    "ChangeSurfaceResult",
    "detect_change_surface",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
TRUST_HYPOTHESIS = "HYPOTHESIS"  # nothing here is compiler-verified — never CERTIFIED

ROLE_IMPLEMENTATION = "implementation"
ROLE_CONFIG = "config_schema"
ROLE_REGISTRATION = "registration"
ROLE_TEST = "test_shape"

# Fixed template order so output is deterministic regardless of dict iteration.
_ROLE_ORDER = (ROLE_IMPLEMENTATION, ROLE_CONFIG, ROLE_REGISTRATION, ROLE_TEST)

# Non-negotiable gates (the mutation-check targets).
_MIN_SIBLINGS = 2  # a "convention" needs >=2 parallel siblings
_MIN_SIGNALS = 2   # every emitted fact needs >=2 independent evidence signals

_MIN_ENTITY_LEN = 3
_MAX_WALK_FILES = 20000    # safety cap on the tree walk
_MAX_REGISTRY_SCAN = 800   # bound registry content scanning
_MAX_REGISTRY_SCAN_CHARS = 16 * 1024 * 1024  # aggregate decoded-content budget
_MAX_FILE_BYTES = 262144   # 256 KiB read cap per file
_MAX_EVIDENCE_LINES = 6

# Source-language extensions -> an implementation surface. Deliberately broad and
# language-agnostic (mirrors the anchors path-extension set).
_SOURCE_EXTS: frozenset[str] = frozenset({
    "py", "pyi", "js", "jsx", "ts", "tsx", "go", "rs", "java", "kt", "kts",
    "rb", "php", "cs", "swift", "scala", "clj", "ex", "exs", "lua", "m", "mm",
    "c", "h", "cc", "hh", "cpp", "hpp", "cxx",
})
# Data/config extensions -> a config/schema surface.
_DATA_EXTS: frozenset[str] = frozenset({
    "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "properties", "xml", "env",
})
_TEXT_EXTS = _SOURCE_EXTS | _DATA_EXTS

# Generic structural path tokens -> a config/schema role when they are the FIXED
# (non-varying) part of a slot. Conventions, not framework names.
_CONFIG_TOKENS: frozenset[str] = frozenset({
    "config", "configs", "conf", "settings", "setting", "schema", "schemas",
    "defaults", "default",
})
# Test-shaped path tokens.
_TEST_TOKENS: frozenset[str] = frozenset({
    "test", "tests", "spec", "specs", "testing", "__tests__",
})
# Structural filename tokens that are never the feature ENTITY (registry/module
# scaffolding). Dropped from member sets so they never masquerade as a sibling.
_NON_ENTITY_TOKENS: frozenset[str] = frozenset({
    "init", "__init__", "index", "main", "mod", "lib", "base", "common",
    "utils", "util", "core", "package", "registry", "factory", "__main__",
})
# Directories never worth walking.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "vendor", "target", ".eggs", "site-packages",
})

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBSEP_RE = re.compile(r"[_\-.]+")
_EXPLICIT_COMPOUND_RE = re.compile(
    r"`([A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+)`"
)
_DECLARATION_WORDS: frozenset[str] = frozenset({
    "named", "called", "kind", "section", "feature", "rule", "api",
})
_CALLABLE_FAMILY_WORDS: frozenset[str] = frozenset({
    "method", "methods", "function", "functions", "func", "api", "apis",
})
_NEGATION_WORDS: frozenset[str] = frozenset({
    "not", "never", "avoid", "without", "cannot", "neither", "nor",
})

# Analogy/comparison words introduce EXISTING exemplars; they are grammatical
# relations, never the requested family member.  They also form a hard governor
# boundary so an earlier "add/support" verb cannot incorrectly govern a sibling
# named after "like"/"analogous to".
_ANALOGY_WORDS: frozenset[str] = frozenset({
    "like", "unlike", "analogous", "akin", "similar", "similarly", "such",
    "mirroring", "resembling",
})

# Tokens that are never admissible new-capability entities (English filler,
# language keywords, feature verbs). Built once at import.
_ENTITY_BLOCKLIST: frozenset[str] = (
    _NL_FUNCTION_WORDS | _STOPWORDS | _LANG_KEYWORD_TOKENS | _FEATURE_VERBS
    | _ANALOGY_WORDS
)

# English modifiers of QUANTITY / KIND / QUALITY that describe HOW a member
# relates to the family, never WHICH member it is ("an additional provider" =
# one more provider; "a custom adapter" = an adapter of your own). These are
# language-invariant English (the same closed-class principle as
# ``_NL_FUNCTION_WORDS`` in anchors.py), NOT framework or domain knowledge.
# They are consulted ONLY on the novel-token leg of the entity gate: a token
# that actually EXISTS as a sibling member (a repo genuinely shipping a
# ``custom``/``mock``/``default`` member) is admitted through the
# ``existing_sibling_member`` signal instead — data always beats the blocklist.
_GENERIC_DESCRIPTORS: frozenset[str] = frozenset({
    "additional", "another", "extra", "further",
    "custom", "customized", "customizable", "dedicated", "separate",
    "alternative", "alternate", "different", "similar", "corresponding",
    "equivalent", "matching", "related", "generic", "special", "specific",
    "particular", "proper", "appropriate", "suitable", "arbitrary",
    "optional", "configurable", "pluggable", "extensible", "reusable",
    "flexible", "better", "improved", "enhanced", "simple", "simpler",
    "basic", "minimal", "lightweight", "modern", "legacy", "internal",
    "external", "initial", "final", "temporary", "permanent", "second",
    "third", "single", "multiple", "shared", "standalone", "unified",
    "updated", "upgraded", "default", "standard", "dummy", "fake", "mock",
    "stub", "sample", "experimental", "native", "builtin",
})


# --------------------------------------------------------------------------- #
# public dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class MissingRole:
    """A change surface the sibling convention expects but the new entity lacks.

    Every field is evidence: which siblings define the role, the registry file +
    the exact lines that register the *siblings* (never the entity), the issue
    span that named the entity, and the independent signals that justified the
    fact. ``trust_tier`` is always HYPOTHESIS — nothing here is compiler-verified.
    """

    role: str
    entity: str
    sibling_files: list[str] = field(default_factory=list)
    registration_file: str | None = None
    registration_lines: list[tuple[int, str]] = field(default_factory=list)
    issue_span: str = ""
    signals: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    trust_tier: str = TRUST_HYPOTHESIS


@dataclass
class NewFileDestination:
    """Where a new-entity file should be created and how to integrate it."""

    entity: str
    suggested_path: str
    directory: str
    template_file: str
    registration_file: str | None = None
    sibling_files: list[str] = field(default_factory=list)
    issue_span: str = ""
    evidence: list[str] = field(default_factory=list)
    trust_tier: str = TRUST_HYPOTHESIS


@dataclass
class ChangeSurfaceResult:
    """The engine's output. ``abstained`` + ``abstain_reason`` explain a quiet."""

    entities: list[str] = field(default_factory=list)
    missing_roles: list[MissingRole] = field(default_factory=list)
    destinations: list[NewFileDestination] = field(default_factory=list)
    sibling_groups: list[dict[str, object]] = field(default_factory=list)
    abstained: bool = True
    abstain_reason: str = ""


# --------------------------------------------------------------------------- #
# internal structures
# --------------------------------------------------------------------------- #
@dataclass
class _Slot:
    """A parallel-sibling slot: files identical except one token position."""

    ext: str
    position: int
    constants: tuple[str, ...]          # the fixed (non-member) tokens
    members: dict[str, str]             # member token -> rel path (sorted-first wins)
    role: str = ""

    @property
    def dirs(self) -> set[str]:
        return {_posix_dir(p) for p in self.members.values()}


@dataclass
class _Group:
    slots: list[_Slot]
    members: set[str] = field(default_factory=set)
    fixed_tokens: set[str] = field(default_factory=set)
    registry_file: str | None = None
    registry_refs: dict[str, list[tuple[int, str]]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# flag
# --------------------------------------------------------------------------- #
def _flag_enabled() -> bool:
    return os.environ.get("GT_CHANGE_SURFACE", "").strip().lower() not in (
        "", "0", "false", "no", "off",
    )


# --------------------------------------------------------------------------- #
# path helpers
# --------------------------------------------------------------------------- #
def _posix(rel: str) -> str:
    return rel.replace("\\", "/")


def _posix_dir(rel: str) -> str:
    p = _posix(rel)
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _split_ext(fname: str) -> tuple[str, str]:
    if "." in fname:
        stem, ext = fname.rsplit(".", 1)
        return stem, ext.lower()
    return fname, ""


def _tokenize(rel_path: str) -> tuple[list[str], str]:
    """Return (token-sequence, ext). Directory names and the filename stem are
    each split on ``_ - .`` into lowercased subtokens; purely structural tokens
    (``__init__``/``index``/...) are dropped so a registry never poses as a
    member. Position alignment across two files exposes the varying (entity)
    token."""
    p = _posix(rel_path).lstrip("./")
    segs = [s for s in p.split("/") if s and s != "."]
    if not segs:
        return [], ""
    *dirs, fname = segs
    stem, ext = _split_ext(fname)
    tokens: list[str] = []
    for seg in [*dirs, stem]:
        for t in _SUBSEP_RE.split(seg.lower()):
            if t and t not in _NON_ENTITY_TOKENS:
                tokens.append(t)
    return tokens, ext


def _normalize(tok: str) -> str:
    """Light singularization so ``providers`` matches ``provider``."""
    t = tok.lower()
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


# --------------------------------------------------------------------------- #
# repo walk + graph facts
# --------------------------------------------------------------------------- #
def _walk_repo(repo_root: str) -> list[str]:
    """Sorted repo-relative posix paths of text-ish files (bounded, noise-pruned)."""
    root = Path(repo_root)
    if not root.is_dir():
        return []
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".git"))
        for fn in sorted(filenames):
            _, ext = _split_ext(fn)
            if ext not in _TEXT_EXTS:
                continue
            rel = _posix(os.path.relpath(os.path.join(dirpath, fn), root))
            out.append(rel)
            if len(out) >= _MAX_WALK_FILES:
                return sorted(set(out))
    return sorted(set(out))


def _graph_facts(graph_db: str | None) -> tuple[set[str], set[str], set[str]]:
    """(node_names_lower, impl_files, test_files) from graph.db, empty on error."""
    names: set[str] = set()
    impl_files: set[str] = set()
    test_files: set[str] = set()
    if not graph_db:
        return names, impl_files, test_files
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return names, impl_files, test_files
    try:
        try:
            for row in conn.execute("SELECT name, file_path, is_test FROM nodes"):
                nm, fp, is_test = row[0], row[1], row[2]
                if nm:
                    names.add(str(nm).lower())
                if fp:
                    fpp = _posix(str(fp)).lstrip("./")
                    if is_test:
                        test_files.add(fpp)
                    else:
                        impl_files.add(fpp)
        except sqlite3.Error:
            return set(), set(), set()
    finally:
        conn.close()
    return names, impl_files, test_files


# --------------------------------------------------------------------------- #
# sibling-group mining
# --------------------------------------------------------------------------- #
def _build_slots(files: list[str]) -> list[_Slot]:
    """Bucket files that agree after replacing one *member token* -> slots.

    A member may occupy more than one path position.  ``methods/parse/parse.ts``
    and ``methods/pipe/pipe.ts`` are one convention just as surely as flat
    ``rule_action.go`` and ``rule_if.go`` are.  Blank every occurrence of the
    candidate token together; blanking only one position makes the former
    convention invisible and sends new members to unrelated flat directories.
    """
    buckets: dict[tuple[str, int, tuple[str | None, ...]], dict[str, str]] = {}
    for rel in files:
        tokens, ext = _tokenize(rel)
        if not tokens:
            continue
        for tok in dict.fromkeys(tokens):
            positions = [i for i, value in enumerate(tokens) if value == tok]
            i = positions[0]
            blanked = tuple(None if value == tok else value for value in tokens)
            key = (ext, i, blanked)
            member_map = buckets.setdefault(key, {})
            # deterministic: first path wins for a given (slot, member) pair
            if tok not in member_map or rel < member_map[tok]:
                member_map[tok] = rel
    slots: list[_Slot] = []
    for (ext, i, blanked), member_map in buckets.items():
        if len(member_map) < _MIN_SIBLINGS:
            continue
        constants = tuple(t for t in blanked if t is not None)
        slots.append(_Slot(ext=ext, position=i, constants=constants, members=dict(member_map)))
    # stable order
    slots.sort(key=lambda s: (s.ext, s.position, s.constants, sorted(s.members)))
    return slots


def _classify_slot(slot: _Slot, impl_files: set[str], test_files: set[str],
                   have_graph: bool) -> str | None:
    """test_shape > config_schema > implementation. None if not a change surface."""
    const_set = set(slot.constants)
    member_files = set(slot.members.values())
    # test shape: constant token is test-ish OR graph marks the files as tests
    if const_set & _TEST_TOKENS or (have_graph and member_files and member_files <= test_files):
        return ROLE_TEST
    # config/schema: constant token is config-ish OR the files are data files
    if const_set & _CONFIG_TOKENS or slot.ext in _DATA_EXTS:
        return ROLE_CONFIG
    # implementation: a source file that defines symbols (when a graph is present)
    if slot.ext in _SOURCE_EXTS:
        if not have_graph:
            return ROLE_IMPLEMENTATION
        if member_files & impl_files:
            return ROLE_IMPLEMENTATION
    return None


def _build_groups(slots: list[_Slot]) -> list[_Group]:
    """Connect slots that share >=2 member tokens (same feature family)."""
    n = len(slots)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for a in range(n):
        for b in range(a + 1, n):
            if len(set(slots[a].members) & set(slots[b].members)) >= _MIN_SIBLINGS:
                union(a, b)

    comps: dict[int, list[_Slot]] = {}
    for idx in range(n):
        comps.setdefault(find(idx), []).append(slots[idx])

    groups: list[_Group] = []
    for members_slots in comps.values():
        members: set[str] = set()
        fixed: set[str] = set()
        for s in members_slots:
            members.update(s.members)
            fixed.update(s.constants)
        groups.append(_Group(slots=list(members_slots), members=members, fixed_tokens=fixed))
    groups.sort(key=lambda g: (sorted(g.fixed_tokens), sorted(g.members)))
    return groups


# --------------------------------------------------------------------------- #
# registration/export detection (content cross-reference counting)
# --------------------------------------------------------------------------- #
def _ref_pattern(token: str) -> re.Pattern[str]:
    """Match a member token as a standalone code reference across the casings
    real code uses, with case-SENSITIVE boundaries per casing branch:

      * bare lowercase ``aws``  — ``aws_config``, ``'aws':``, ``providers/aws``
        (a continuing lowercase run blocks it: ``awesome``, ``flaws``)
      * Capitalized ``Aws``     — a camelCase segment: ``AwsProvider``,
        ``handleAwsRequest`` (left boundary allows the lower→Upper camel
        transition; a continuing lowercase run blocks it: ``Awsome``)
      * ALL-CAPS ``AWS``        — ``AWS_REGION`` (strict boundaries both sides,
        so an uppercase run never leaks: ``AWSOME``, ``CATALOG`` for ``cat``)

    No ``re.IGNORECASE``: under IGNORECASE the boundary character classes turn
    case-insensitive too, which silently blocked every camelCase reference
    (the F3 defect — ``Aws`` in ``AwsProvider`` never matched).
    """
    low = re.escape(token.lower())
    cap = re.escape(token[:1].upper() + token[1:].lower())
    up = re.escape(token.upper())
    branches = [
        rf"(?<![A-Za-z0-9]){low}(?![a-z0-9])",    # aws | aws_config | 'aws':
        rf"(?<![A-Z0-9]){cap}(?![a-z0-9])",       # AwsProvider | handleAwsRequest
        rf"(?<![A-Za-z0-9]){up}(?![A-Za-z0-9])",  # AWS_REGION (strict)
    ]
    return re.compile("|".join(dict.fromkeys(branches)))


def _read_text(repo_root: str, rel: str) -> str:
    try:
        p = Path(repo_root) / rel
        with open(p, "rb") as fh:
            raw = fh.read(_MAX_FILE_BYTES)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


# F1 line-level filters: a registry cross-reference must sit on a CODE-shaped
# line. Conservative heuristics (losing an evidence line is fine; counting a
# prose line is not — correct-or-quiet):
#   * lines inside (or containing) triple-quoted blocks are skipped entirely;
#   * ``#`` and ``//`` comment tails are stripped (``://`` in URLs preserved);
#   * inline ``/* ... */`` spans are removed; ``*``-led continuation lines skipped;
#   * the remainder must carry code punctuation (assignment / call / dict-key /
#     collection / quote) or an import/export keyword — pure prose never counts.
_TRIPLE_QUOTE_RE = re.compile(r"'''|\"\"\"")
_HASH_COMMENT_RE = re.compile(r"(?:^|\s)#")
_SLASH_COMMENT_RE = re.compile(r"(?:^|[^:])//")
_BLOCK_COMMENT_INLINE_RE = re.compile(r"/\*.*?\*/")
_CODE_SHAPE_RE = re.compile(
    r"[\"'=(){}\[\],;:]|\b(?:import|export|from|require|include|register)\b"
)


def _strip_comment_tail(line: str) -> str:
    """Drop ``#`` / ``//`` comment tails and inline ``/*...*/`` spans."""
    line = _BLOCK_COMMENT_INLINE_RE.sub(" ", line)
    m = _HASH_COMMENT_RE.search(line)
    if m:
        line = line[: line.index("#", m.start())]
    m2 = _SLASH_COMMENT_RE.search(line)
    if m2:
        line = line[: line.index("//", m2.start())]
    return line


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(lineno, stripped-line) for the CODE-shaped lines of a file — the only
    lines a registry cross-reference may be counted on (see filter notes above)."""
    out: list[tuple[int, str]] = []
    in_triple = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        n_triple = len(_TRIPLE_QUOTE_RE.findall(raw))
        if in_triple:
            if n_triple % 2 == 1:
                in_triple = False
            continue
        if n_triple:
            if n_triple % 2 == 1:
                in_triple = True
            continue  # conservative: a triple-quote line is string-dominated
        line = _strip_comment_tail(raw)
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue  # empty after strip / block-comment continuation
        if not _CODE_SHAPE_RE.search(line):
            continue  # pure prose — no code shape
        out.append((lineno, stripped))
    return out


def _detect_registry(group: _Group, files: list[str], repo_root: str) -> None:
    """Populate group.registry_file / registry_refs: the file whose CODE lines
    cross-reference the MOST distinct group members (>=2). Test-shaped files
    (incl. ``conftest``) are excluded; docstring/comment/prose mentions never
    count (F1). Deterministic ranking: most distinct members, then the FEWEST
    duplicate reference lines, then lexicographic path. A registry normally
    names each member once; rewarding repetition lets unrelated corpora or
    generated data outrank the actual integration point."""
    members = sorted(group.members)
    if len(members) < _MIN_SIBLINGS:
        return
    patterns = {m: _ref_pattern(m) for m in members}
    sibling_files = {
        _posix(path) for slot in group.slots for path in slot.members.values()
    }
    best: tuple[int, int, str] | None = None
    best_refs: dict[str, list[tuple[int, str]]] = {}
    best_file: str | None = None
    scanned = 0
    scanned_chars = 0
    for rel in files:
        if scanned >= _MAX_REGISTRY_SCAN:
            break
        toks, _ext = _tokenize(rel)
        if set(toks) & _TEST_TOKENS or _is_test_path(rel):
            continue
        if _posix(rel) in sibling_files:
            continue  # an implementation sibling is not its own registry
        text = _read_text(repo_root, rel)
        if not text:
            continue
        # The per-file cap alone permits 800 * 256 KiB (>200 MiB) of content
        # work on a synchronous observation hook.  Bound the aggregate too.
        # Reaching the budget is a correct-or-quiet abstention boundary: facts
        # derived before it remain exact, and unexamined files mint nothing.
        if scanned_chars + len(text) > _MAX_REGISTRY_SCAN_CHARS:
            break
        scanned_chars += len(text)
        scanned += 1
        # A registry must reference at least two distinct family members.  Use
        # C-level literal searches to derive a per-file candidate set before
        # splitting/filtering lines or invoking any exact regex.  This retains
        # `_ref_pattern` as the authority (substring hits can only add cheap
        # work, never evidence) and prevents large families from multiplying
        # Python regex work across every unrelated code line.
        folded_text = text.lower()
        file_candidates = [m for m in members if m in folded_text]
        if len(file_candidates) < _MIN_SIBLINGS:
            continue
        refs: dict[str, list[tuple[int, str]]] = {}
        for lineno, line in _code_lines(text):
            folded_line = line.lower()
            for m in file_candidates:
                if m not in folded_line:
                    continue
                if patterns[m].search(line) or any(
                    ident.lower() == m.lower() for ident in _IDENT_RE.findall(line)
                ):
                    refs.setdefault(m, []).append((lineno, line))
        distinct = len(refs)
        if distinct < _MIN_SIBLINGS:
            continue
        n_ref_lines = sum(len(v) for v in refs.values())
        if (
            best is None
            or distinct > best[0]
            or (distinct == best[0] and n_ref_lines < best[1])
            or ((distinct, n_ref_lines) == (best[0], best[1]) and rel < best[2])
        ):
            best = (distinct, n_ref_lines, rel)
            best_refs = refs
            best_file = rel
    if best_file is not None:
        group.registry_file = best_file
        group.registry_refs = best_refs


# --------------------------------------------------------------------------- #
# issue-side helpers
# --------------------------------------------------------------------------- #
def _is_test_path(rel: str) -> bool:
    toks, _ = _tokenize(rel)
    if set(toks) & _TEST_TOKENS:
        return True
    base = _posix(rel).rsplit("/", 1)[-1].lower()
    stem, _ext = _split_ext(base)
    return (
        base.startswith("test_") or base.endswith("_test")
        or ".test." in base or ".spec." in base
        or stem == "conftest"  # pytest plumbing imports many siblings — never a registry
    )


def _issue_word_seq(issue_text: str) -> list[str]:
    return [m.group(0).lower() for m in _IDENT_RE.finditer(issue_text or "")]


def _clause_negates_feature_addition(words: list[str]) -> bool:
    """Whether a local issue clause explicitly forbids the proposed addition."""
    if set(words) & _NEGATION_WORDS:
        return True
    # Apostrophes split under ``_IDENT_RE``: don't -> [don, t].
    return any(
        words[i] in {"don", "doesn", "shouldn", "mustn", "isn", "aren"}
        and words[i + 1] == "t"
        for i in range(len(words) - 1)
    )


def _declared_code_identifier(entity: str, issue_text: str) -> bool:
    """Whether a code-spelled identifier is introduced as a feature identity.

    Backticks also quote config values, enum literals, and field names.  Those
    are not independent new-file entities.  A quoted identifier is admissible
    only when nearby language declares/names it (or a feature-add verb governs
    it).  Function-call syntax remains an explicit public API declaration.
    """
    for match in re.finditer(
        rf"(?<![A-Za-z0-9_]){re.escape(entity)}\(",
        issue_text or "",
        re.IGNORECASE,
    ):
        prefix = (issue_text or "")[max(0, match.start() - 128):match.start()]
        prefix = re.split(r"[.!?;\n]", prefix)[-1]
        if not _clause_negates_feature_addition(_issue_word_seq(prefix)):
            return True
    for match in re.finditer(rf"`{re.escape(entity)}`", issue_text or "", re.IGNORECASE):
        prefix = (issue_text or "")[max(0, match.start() - 96):match.start()]
        # Do not let declaration evidence leak across sentence boundaries.
        prefix = re.split(r"[.!?;\n]", prefix)[-1]
        words = _issue_word_seq(prefix)[-10:]
        if (not _clause_negates_feature_addition(words)
                and (set(words[-4:]) & _DECLARATION_WORDS
                     or set(words[-3:]) & _FEATURE_VERBS)):
            return True
    return False


def _entity_candidates(issue_text: str, graph_db: str | None,
                       known_tokens: set[str]) -> list[str]:
    """Issue tokens admissible as a new-capability entity.

    Admitted iff identifier-shaped, >= _MIN_ENTITY_LEN, not English/keyword/verb
    filler. ``unresolvable`` (absent from every repo token / graph node) is the
    strong new-file signal, but an existing sibling token is also admitted so the
    PARTIAL-hole case (impl exists, registration missing) is reachable — the
    later role diff decides whether anything is actually absent.
    """
    words = _issue_word_seq(issue_text)
    cands: list[str] = []
    seen: set[str] = set()
    # Preserve explicitly named compound identifiers as ONE candidate.  Splitting
    # ``action-pinning`` into ``action`` + ``pinning`` loses the fact that it is
    # a new variant of the existing ``action`` member and can fabricate a nearby
    # adjective (for example ``lint``) as the destination instead.
    for match in _EXPLICIT_COMPOUND_RE.finditer(issue_text or ""):
        compound = match.group(1).lower()
        if compound not in seen and _declared_code_identifier(compound, issue_text):
            seen.add(compound)
            cands.append(compound)
    for w in words:
        if len(w) < _MIN_ENTITY_LEN or w in _ENTITY_BLOCKLIST:
            continue
        if w in seen:
            continue
        seen.add(w)
        cands.append(w)
    # reuse anchors' code-provenance greenfield tier when a graph is available
    if graph_db:
        try:
            anchors = extract_issue_anchors(issue_text, graph_db)
            for t in anchors.unresolved_code_symbols:
                tl = t.lower()
                if len(tl) >= _MIN_ENTITY_LEN and tl not in _ENTITY_BLOCKLIST and tl not in seen:
                    seen.add(tl)
                    cands.append(tl)
        except sqlite3.Error:
            pass
    known = known_tokens
    # deterministic order: unresolvable first (strong signal), then existing, lex
    # Async companions conventionally live beside their sync base rather than
    # defining another feature-family directory.  When both names are requested,
    # let the base own destination inference; the companion remains downstream
    # implementation detail.  An async-only request is retained.
    cand_set = set(cands)
    cands = [
        w for w in cands
        if not (w.endswith("async") and len(w) > 5 and w[:-5] in cand_set)
    ]
    cands.sort(key=lambda w: (w in known, w))
    return cands


def _entity_links_group(entity: str, word_seq: list[str], group: _Group,
                        issue_text: str = "") -> bool:
    """The new entity must IMMEDIATELY PRECEDE its category noun — the universal
    adjective-noun construction that names a variant ("azure provider", "sqlite
    adapter", "yaml handler"). This is what separates the real new-capability
    token from prose that merely shares the sentence: in "an azure provider
    analogous to the existing aws and gcp providers", only ``azure`` precedes a
    category noun — ``analogous`` precedes "to", ``existing`` precedes the member
    "aws". Category = the group's FIXED tokens only (normalized); member names
    do not count as anchors, so a descriptive word sitting next to a member is
    not admitted. Correct-or-quiet: a real entity named far from its category
    noun is a tolerated miss, never a false emission.
    """
    category = {
        _normalize(t) for t in group.fixed_tokens
        if t not in _ENTITY_BLOCKLIST and t not in _GENERIC_DESCRIPTORS
    }
    for i, w in enumerate(word_seq):
        if w != entity:
            continue
        # A novel word next to a repo-path category is not sufficient: issue text
        # contains audit/verification instructions such as "trace provider calls"
        # that are not requests for a new provider.  Require the entity phrase to
        # be governed by a feature-add verb (allowing an article between them).
        governor_window = word_seq[max(0, i - 4):i]
        governed = (
            any(word in _FEATURE_VERBS for word in governor_window)
            and not _clause_negates_feature_addition(governor_window)
            and not (set(governor_window) & _ANALOGY_WORDS)
        )
        if (
            governed
            and i + 1 < len(word_seq)
            and _normalize(word_seq[i + 1]) in category
        ):
            return True
    # Public API names are often introduced as code (``foo(...)`` or a quoted
    # compound identifier) while the family noun appears elsewhere in the same
    # request ("available from the public methods surface").  That is stronger
    # than arbitrary word co-occurrence: require both an explicit code spelling
    # and an issue-mentioned category derived from this repo group.
    explicit = _declared_code_identifier(entity, issue_text)
    structural_category = {
        _normalize(t) for t in group.fixed_tokens
        if t not in _GENERIC_DESCRIPTORS
    }
    category_named = any(
        _normalize(w) in structural_category for w in word_seq
    )
    return explicit and category_named


def _slot_member_repetitions(slot: _Slot) -> int:
    """How many structural positions the varying member owns in this slot."""
    best = 0
    for member, rel in slot.members.items():
        tokens, _ext = _tokenize(rel)
        best = max(best, sum(1 for token in tokens if token == member))
    return best


def _group_shape_strength(group: _Group) -> int:
    """Prefer a coupled directory/file family over a flat lexical distractor."""
    return max((_slot_member_repetitions(slot) for slot in group.slots), default=0)


def _focus_group(group: _Group, entity: str, norm_issue_words: set[str],
                 issue_text: str) -> _Group:
    """Select one coherent path family from a transitive member component.

    Connected-component grouping intentionally joins roles that share members,
    but common names can bridge unrelated families (actions -> methods ->
    schemas -> codemods).  Destination inference must be owned by one concrete
    slot prefix, not by that transitive mega-component.  Rank slots using only
    general evidence: explicit compound extension, callable API shape, issue
    overlap, repeated member structure, then shortest convention prefix.
    """
    is_call = bool(re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(entity)}\(",
        issue_text or "", re.IGNORECASE,
    ))

    def score(slot: _Slot) -> tuple[int, int, int, int, int, int, int]:
        constants = {_normalize(t) for t in slot.constants}
        parts = {_normalize(p) for p in _SUBSEP_RE.split(entity) if p}
        members = {_normalize(m) for m in slot.members}
        compound_extension = int(bool(parts & members) and len(parts) > 1)
        callable_family = int(is_call and bool(constants & _CALLABLE_FAMILY_WORDS))
        overlap = len(constants & norm_issue_words)
        # Plain adjective+noun requests ("azure provider") must select a slot
        # whose OWN convention names that category. Explicit API/compound names
        # instead use their stronger code-shape evidence and shortest prefix.
        category_ownership = overlap if not (compound_extension or is_call) else 0
        return (
            compound_extension,
            callable_family,
            int(slot.role == ROLE_IMPLEMENTATION),
            category_ownership,
            -len(slot.constants),
            _slot_member_repetitions(slot),
            overlap,
        )

    anchor = sorted(
        group.slots,
        key=lambda slot: (tuple(-v for v in score(slot)), slot.constants),
    )[0]
    anchor_constants = set(anchor.constants)
    anchor_members = set(anchor.members)
    # Role variants add suffix constants such as test/types; keep those, while
    # excluding unrelated prefixes reached only through transitive member names.
    focused_slots = [
        slot for slot in group.slots
        if anchor_constants <= set(slot.constants)
        or (
            len(anchor_members & set(slot.members)) >= _MIN_SIBLINGS
            and len(anchor_members & set(slot.members)) >=
            max(len(anchor_members), len(slot.members)) - 1
        )
    ]
    if not focused_slots:
        focused_slots = [anchor]
    members = {member for slot in focused_slots for member in slot.members}
    fixed = {token for slot in focused_slots for token in slot.constants}
    return _Group(slots=focused_slots, members=members, fixed_tokens=fixed)


def _compound_extends_group(entity: str, group: _Group) -> bool:
    parts = {_normalize(p) for p in _SUBSEP_RE.split(entity) if p}
    members = {_normalize(m) for m in group.members}
    return bool(parts & members) and bool(_SUBSEP_RE.search(entity))


def _entity_second_signal(entity: str, group: _Group,
                          known_tokens: set[str]) -> str | None:
    """The INDEPENDENT second signal an entity needs on top of issue adjacency
    (the F2 two-signal law — adjacency alone is ONE signal and never mints):

      * ``existing_sibling_member`` — the token IS a member of the sibling group
        (the partial-hole case: impl exists, wiring absent). Checked first so
        repo data always beats the descriptor blocklist.
      * ``novel_token`` — the token resolves NOWHERE in the repo's code
        identifiers (graph node names + path tokens + sibling slot values) AND
        is not a generic English descriptor ("additional", "custom", ...). An
        unresolvable named capability is the strong new-file signal; a bare
        adjective before the category noun is neither novel-code nor a member.

    Returns ``None`` when no independent signal holds -> the entity abstains.
    """
    if entity in group.members:
        return "existing_sibling_member"
    if entity not in known_tokens and entity not in _GENERIC_DESCRIPTORS:
        return "novel_token"
    return None


def _group_overlap(group: _Group, norm_issue_words: set[str]) -> int:
    return sum(1 for ft in group.fixed_tokens if _normalize(ft) in norm_issue_words)


def _issue_span(issue_text: str, entity: str) -> str:
    m = re.search(r"(?<![A-Za-z0-9])" + re.escape(entity) + r"(?![A-Za-z0-9])",
                  issue_text or "", re.IGNORECASE)
    if not m:
        return ""
    lo, hi = max(0, m.start() - 24), min(len(issue_text), m.end() + 24)
    return " ".join(issue_text[lo:hi].split())


def _fill_pattern(template_rel: str, member_token: str, entity: str) -> str:
    template = _posix(template_rel)
    parts = [p for p in _SUBSEP_RE.split(entity) if p]
    replacement = entity
    if len(parts) > 1:
        # Adopt the repository path's separator convention.  The conceptual API
        # name may be kebab-case while the source file family is snake_case.
        if "_" in template:
            replacement = "_".join(parts)
        elif "-" in template:
            replacement = "-".join(parts)
    # Coupled member slots repeat the token in directory and filename; fill all
    # occurrences so ``methods/parse/parse.ts`` becomes
    # ``methods/recursive/recursive.ts`` atomically.
    return _ref_pattern(member_token).sub(replacement, template)


# --------------------------------------------------------------------------- #
# template assembly per group
# --------------------------------------------------------------------------- #
def _role_slots(group: _Group) -> dict[str, list[_Slot]]:
    out: dict[str, list[_Slot]] = {}
    for s in group.slots:
        if s.role:
            out.setdefault(s.role, []).append(s)
    return out


def _role_members(slots: list[_Slot], registry_file: str | None) -> tuple[set[str], list[str]]:
    """(member tokens, sibling files) for a role, excluding the registry file."""
    members: set[str] = set()
    files: list[str] = []
    for s in slots:
        for m, f in s.members.items():
            if registry_file and _posix(f) == _posix(registry_file):
                continue
            members.add(m)
            files.append(_posix(f))
    return members, sorted(set(files))


def _pick_template(impl_slots: list[_Slot], registry_file: str | None,
                   impl_files: set[str]) -> tuple[str, str, str] | None:
    """Choose (directory, template_file, member_token) for the destination.

    Directory = the impl dir with the most members; a TIE between directories is
    a conflicting convention -> caller abstains. Template = the richest member
    file in that dir (most graph symbols is unknown here, so: prefer a file that
    is a known impl symbol source, then lexicographic)."""
    ranked_slots: list[tuple[tuple[int, int], _Slot, dict[str, str]]] = []
    for slot in impl_slots:
        members = {
            member: _posix(path) for member, path in slot.members.items()
            if not registry_file or _posix(path) != _posix(registry_file)
        }
        if members:
            ranked_slots.append((
                (_slot_member_repetitions(slot), len(members)), slot, members,
            ))
    if not ranked_slots:
        return None
    ranked_slots.sort(key=lambda row: (-row[0][0], -row[0][1], row[1].constants))
    top_score, _slot, members = ranked_slots[0]
    if len(ranked_slots) >= 2 and ranked_slots[1][0] == top_score:
        first_patterns = {
            _ref_pattern(m).sub("{member}", f) for m, f in members.items()
        }
        other_patterns = {
            _ref_pattern(m).sub("{member}", f)
            for m, f in ranked_slots[1][2].items()
        }
        if first_patterns != other_patterns:
            return None  # equally strong, genuinely conflicting conventions
    # richest template: prefer a file the graph confirms defines symbols, then lex
    def tkey(item: tuple[str, str]) -> tuple[int, str]:
        _m, f = item
        return (0 if f in impl_files else 1, f)
    member_token, template_file = sorted(members.items(), key=tkey)[0]
    directory = _posix_dir(template_file)
    return directory, template_file, member_token


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def detect_change_surface(
    issue_text: str,
    repo_root: str,
    graph_db: str | None = None,
) -> ChangeSurfaceResult:
    """Detect MISSING change surfaces + new-file destinations for an issue.

    Returns an empty (abstained) result immediately when ``GT_CHANGE_SURFACE`` is
    unset/0, and abstains (correct-or-quiet) whenever the evidence bar is not
    met. ``graph_db`` is optional: without it the engine works from the tree
    alone (implementation = source-extension file), which is exactly the
    new-file case the graph cannot contain.
    """
    if not _flag_enabled():
        return ChangeSurfaceResult(abstained=True, abstain_reason="flag_disabled")
    if not issue_text or not repo_root:
        return ChangeSurfaceResult(abstained=True, abstain_reason="empty_input")

    files = _walk_repo(repo_root)
    if not files:
        return ChangeSurfaceResult(abstained=True, abstain_reason="no_files")

    graph_names, impl_files, test_files = _graph_facts(graph_db)
    have_graph = bool(graph_db) and (bool(graph_names) or bool(impl_files) or bool(test_files))

    # known tokens: every path token + graph node name (lowercased)
    known_tokens: set[str] = set(graph_names)
    for rel in files:
        toks, _ext = _tokenize(rel)
        known_tokens.update(toks)

    slots = _build_slots(files)
    for s in slots:
        s.role = _classify_slot(s, impl_files, test_files, have_graph) or ""
    slots = [s for s in slots if s.role]
    if not slots:
        return ChangeSurfaceResult(abstained=True, abstain_reason="no_sibling_group")

    groups = _build_groups(slots)

    word_seq = _issue_word_seq(issue_text)
    norm_issue_words = {_normalize(w) for w in word_seq}
    entities = _entity_candidates(issue_text, graph_db, known_tokens)

    # A quoted compound name that extends an existing family member is the most
    # specific issue-side identity for that family.  Do not also mint a nearby
    # category modifier as a second destination (``lint rule`` must not compete
    # with explicit ``action-pinning`` extending the existing ``action`` rule).
    preferred_compounds: dict[int, set[str]] = {}
    for group in groups:
        preferred = {
            entity for entity in entities
            if _compound_extends_group(entity, group)
            and _entity_links_group(entity, word_seq, group, issue_text)
        }
        if preferred:
            preferred_compounds[id(group)] = preferred

    result = ChangeSurfaceResult()
    registry_cache: dict[
        tuple[int, ...], tuple[str | None, dict[str, list[tuple[int, str]]]]
    ] = {}
    derived_registry_groups: dict[tuple[int, ...], _Group] = {}
    emitted_entities: set[str] = set()
    conflict = False
    for entity in entities:
        # which groups does this entity plausibly belong to?
        matched: list[tuple[tuple[int, int, int, int, int, int], _Group]] = []
        for g in groups:
            if _normalize(entity) in {_normalize(ft) for ft in g.fixed_tokens}:
                continue  # entity IS the category noun, not a new member
            ov = _group_overlap(g, norm_issue_words)
            if ov == 0:
                continue  # group category not named in the issue -> irrelevant
            if not _entity_links_group(entity, word_seq, g, issue_text):
                continue  # entity not adjacent to this group's category
            preferred = preferred_compounds.get(id(g))
            if preferred and entity not in preferred:
                continue
            focused = _focus_group(g, entity, norm_issue_words, issue_text)
            fixed_norm = {_normalize(t) for t in focused.fixed_tokens}
            callable_family = int(
                _declared_code_identifier(entity, issue_text)
                and bool(fixed_norm & _CALLABLE_FAMILY_WORDS)
            )
            focused_overlap = len(fixed_norm & norm_issue_words)
            min_prefix = min((len(s.constants) for s in focused.slots), default=999)
            rank = (
                int(_compound_extends_group(entity, focused)),
                callable_family,
                -min_prefix,
                _group_shape_strength(focused),
                focused_overlap,
                len(focused.members),
            )
            matched.append((rank, focused))
        if not matched:
            continue

        # pick the dominant group; a tie on (overlap, member-count) is a conflict
        matched.sort(key=lambda t: (
            tuple(-value for value in t[0]), sorted(t[1].fixed_tokens)
        ))
        if len(matched) >= 2:
            (rank0, g0), (rank1, g1) = matched[0], matched[1]
            if rank0 == rank1 and (
                sorted(g0.fixed_tokens) != sorted(g1.fixed_tokens)
            ):
                conflict = True
                continue
        group = matched[0][1]

        # Registry mining is the expensive content phase: it reads up to
        # ``_MAX_REGISTRY_SCAN`` files and checks their code lines against every
        # member in a sibling family.  Running it for every tree-derived group
        # before the issue has selected a family makes cost proportional to all
        # repository conventions, including hundreds unrelated to the request.
        # Registration evidence is only consumed by ``_entity_holes`` for the
        # dominant issue-matched group, so derive it lazily and at most once per
        # selected group.  This preserves exact evidence for every emitted fact
        # while keeping unrelated groups correct-and-quiet.
        # `_focus_group` returns a fresh view for every entity.  Key the cache
        # by its underlying slot identities so two entities selecting the same
        # convention reuse one content derivation while genuinely different
        # focused families remain independent.
        group_identity = tuple(id(slot) for slot in group.slots)
        cached_registry = registry_cache.get(group_identity)
        if cached_registry is None:
            _detect_registry(group, files, repo_root)
            registry_cache[group_identity] = (
                group.registry_file,
                {member: list(lines) for member, lines in group.registry_refs.items()},
            )
        else:
            group.registry_file = cached_registry[0]
            group.registry_refs = {
                member: list(lines) for member, lines in cached_registry[1].items()
            }
        derived_registry_groups[group_identity] = group

        # F2 two-signal law (REAL, derived — the mutation target): adjacency to
        # the category noun is ONE signal; the entity needs an independent
        # second (novel token / existing member) or it abstains. This is what
        # keeps "an additional provider" / "a custom provider" from minting.
        ent_signals: list[str] = ["issue_adjacency"]
        second = _entity_second_signal(entity, group, known_tokens)
        if second is not None:
            ent_signals.append(second)
        if len(ent_signals) < _MIN_SIGNALS:
            continue

        mrs, dest = _entity_holes(entity, group, issue_text, ent_signals,
                                  impl_files)
        if dest is None and _needs_destination_but_conflicted(entity, group, impl_files):
            conflict = True
            continue
        if mrs:
            result.missing_roles.extend(mrs)
            emitted_entities.add(entity)
        if dest is not None:
            result.destinations.append(dest)
            emitted_entities.add(entity)

    # Serialize diagnostics only after lazy registry derivation so an emitted
    # group's registration mechanism remains visible to the attestation join.
    # Unmatched groups intentionally carry no registry_file: no issue-grounded
    # fact consumed (or paid for) that content scan.
    for g in [*groups, *derived_registry_groups.values()]:
        rs = _role_slots(g)
        tmpl_dirs = sorted({_posix_dir(f) for s in g.slots for f in s.members.values()})
        result.sibling_groups.append({
            "members": sorted(g.members),
            "fixed_tokens": sorted(g.fixed_tokens),
            "roles": sorted(rs.keys()) + ([ROLE_REGISTRATION] if g.registry_file else []),
            "directories": tmpl_dirs,
            "registry_file": g.registry_file,
        })

    result.entities = sorted(emitted_entities)
    result.missing_roles.sort(key=lambda m: (m.entity, _ROLE_ORDER.index(m.role)
                                             if m.role in _ROLE_ORDER else 99))
    result.destinations.sort(key=lambda d: (d.entity, d.suggested_path))

    if result.missing_roles or result.destinations:
        result.abstained = False
        result.abstain_reason = ""
    else:
        result.abstained = True
        result.abstain_reason = "conflicting_conventions" if conflict else "no_change_surface"
    return result


def _needs_destination_but_conflicted(entity: str, group: _Group,
                                      impl_files: set[str]) -> bool:
    """True when the entity lacks an implementation (so a destination is owed) but
    the destination directory is ambiguous (conflicting convention)."""
    rs = _role_slots(group)
    impl_slots = rs.get(ROLE_IMPLEMENTATION, [])
    if not impl_slots:
        return False
    impl_members, _ = _role_members(impl_slots, group.registry_file)
    if entity in impl_members:
        return False  # impl already present -> no destination owed
    return _pick_template(impl_slots, group.registry_file, impl_files) is None


def _entity_holes(entity: str, group: _Group, issue_text: str,
                  ent_signals: list[str], impl_files: set[str],
                  ) -> tuple[list[MissingRole], NewFileDestination | None]:
    """Diff the entity against the group's role template -> MissingRoles (+dest).

    ``ent_signals`` are the caller-DERIVED entity admission signals (adjacency +
    the independent novel-token / existing-member signal). Every emitted fact
    carries them plus its role-specific structural signal — no hard-coded signal
    literals, so ``_MIN_SIGNALS`` genuinely gates emission.
    """
    rs = _role_slots(group)
    span = _issue_span(issue_text, entity)
    out: list[MissingRole] = []

    role_present: dict[str, bool] = {}
    role_sibs: dict[str, list[str]] = {}
    for role in (ROLE_IMPLEMENTATION, ROLE_CONFIG, ROLE_TEST):
        slots = rs.get(role, [])
        if not slots:
            continue
        members, sib_files = _role_members(slots, group.registry_file)
        if len(members) < _MIN_SIBLINGS:
            continue  # convention needs >=2 siblings
        role_present[role] = entity in members
        role_sibs[role] = sib_files

    # implementation / config / test — entity signals + the sibling convention
    for role in (ROLE_IMPLEMENTATION, ROLE_CONFIG, ROLE_TEST):
        if role not in role_sibs or role_present.get(role, False):
            continue
        signals = [*ent_signals, "sibling_convention"]
        if len(signals) < _MIN_SIGNALS:
            continue
        sibs = role_sibs[role]
        ev = [
            f"{len(sibs)} siblings define {role}: " + ", ".join(sibs),
            f"issue names new entity '{entity}' in this family: '{span}'",
        ]
        out.append(MissingRole(
            role=role, entity=entity, sibling_files=sibs, issue_span=span,
            signals=signals, evidence=ev, trust_tier=TRUST_HYPOTHESIS,
        ))

    # registration — REQUIRES the detected mechanism on top of the entity
    # signals: a registry file whose CODE lines cross-reference >=2 siblings.
    # Sibling naming convention alone (no mechanism) never mints this fact.
    has_mechanism = bool(group.registry_file) and len(group.registry_refs) >= _MIN_SIBLINGS
    if has_mechanism:
        entity_registered = any(
            _ref_pattern(entity).search(t)
            or any(ident.lower() == entity.lower() for ident in _IDENT_RE.findall(t))
            for lines in group.registry_refs.values() for _, t in lines
        )
        if not entity_registered:
            signals = [*ent_signals, "registration_mechanism"]
            if len(signals) >= _MIN_SIGNALS:
                reg_lines: list[tuple[int, str]] = []
                for m in sorted(group.registry_refs):
                    reg_lines.extend(group.registry_refs[m][:1])
                reg_lines = sorted(set(reg_lines))[:_MAX_EVIDENCE_LINES]
                reg_file = _posix(group.registry_file) if group.registry_file else ""
                ev = [
                    f"registry {reg_file} registers "
                    f"{len(group.registry_refs)} siblings but not '{entity}'",
                ]
                ev += [f"line {ln}: {txt}" for ln, txt in reg_lines]
                out.append(MissingRole(
                    role=ROLE_REGISTRATION, entity=entity,
                    sibling_files=[reg_file],
                    registration_file=reg_file,
                    registration_lines=reg_lines, issue_span=span,
                    signals=signals, evidence=ev, trust_tier=TRUST_HYPOTHESIS,
                ))

    # destination — only when the implementation surface is the hole
    dest: NewFileDestination | None = None
    impl_slots = rs.get(ROLE_IMPLEMENTATION, [])
    if impl_slots:
        impl_members, impl_sibs = _role_members(impl_slots, group.registry_file)
        if len(impl_members) >= _MIN_SIBLINGS and entity not in impl_members:
            picked = _pick_template(impl_slots, group.registry_file, impl_files)
            if picked is not None:
                directory, template_file, member_token = picked
                suggested = _fill_pattern(template_file, member_token, entity)
                ev = [
                    f"sibling directory '{directory}' holds {len(impl_sibs)} "
                    f"parallel implementations: " + ", ".join(impl_sibs),
                    f"nearest template: {template_file}",
                ]
                if group.registry_file:
                    ev.append(f"integrate at registry: {_posix(group.registry_file)}")
                dest = NewFileDestination(
                    entity=entity, suggested_path=_posix(suggested),
                    directory=directory, template_file=_posix(template_file),
                    registration_file=_posix(group.registry_file) if group.registry_file else None,
                    sibling_files=impl_sibs, issue_span=span, evidence=ev,
                    trust_tier=TRUST_HYPOTHESIS,
                )

    return out, dest

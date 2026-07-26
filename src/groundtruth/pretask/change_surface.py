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
    "RepositoryWitness",
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

# Tokens that are never admissible new-capability entities (English filler,
# language keywords, feature verbs). Built once at import.
_ENTITY_BLOCKLIST: frozenset[str] = (
    _NL_FUNCTION_WORDS | _STOPWORDS | _LANG_KEYWORD_TOKENS | _FEATURE_VERBS
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
@dataclass(frozen=True, order=True)
class RepositoryWitness:
    """One exact repository location supporting a destination inference.

    ``kind`` states what the location proves.  The detector currently admits
    only production implementation definitions, code-shaped lexical template
    references, and code-shaped registration references; it never manufactures
    a line number for a path-only sibling.
    """

    file: str
    line: int
    kind: str
    identity: str


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
    repository_witnesses: list[RepositoryWitness] = field(default_factory=list)


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


def _graph_facts(
    graph_db: str | None,
) -> tuple[
    set[str],
    set[str],
    set[str],
    dict[str, tuple[RepositoryWitness, ...]],
]:
    """Graph names/files plus exact production definition witnesses.

    Definition witnesses require a positive ``start_line``.  A graph row with
    no source line can still classify an implementation file, but it cannot
    support canonical provenance and is deliberately omitted from the witness
    map.
    """
    names: set[str] = set()
    impl_files: set[str] = set()
    test_files: set[str] = set()
    impl_witness_rows: dict[str, list[RepositoryWitness]] = {}
    if not graph_db:
        return names, impl_files, test_files, {}
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return names, impl_files, test_files, {}
    try:
        try:
            for row in conn.execute(
                "SELECT name, file_path, start_line, is_test FROM nodes "
                "ORDER BY file_path, start_line, name"
            ):
                nm, fp, start_line, is_test = row[0], row[1], row[2], row[3]
                if nm:
                    names.add(str(nm).lower())
                if fp:
                    fpp = _posix(str(fp)).lstrip("./")
                    if is_test:
                        test_files.add(fpp)
                    else:
                        impl_files.add(fpp)
                        if (
                            isinstance(start_line, int)
                            and not isinstance(start_line, bool)
                            and start_line > 0
                            and nm
                        ):
                            impl_witness_rows.setdefault(fpp, []).append(
                                RepositoryWitness(
                                    file=fpp,
                                    line=start_line,
                                    kind="template_definition",
                                    identity=str(nm),
                                )
                            )
        except sqlite3.Error:
            return set(), set(), set(), {}
    finally:
        conn.close()
    witnesses = {
        file_path: tuple(sorted(set(rows), key=lambda row: (
            row.line, row.identity, row.kind
        )))
        for file_path, rows in impl_witness_rows.items()
    }
    return names, impl_files, test_files, witnesses


def _cochange_coupling(
    graph_db: "str | None",
    target_rel: str,
    member_files: "set[str] | frozenset[str]",
) -> int:
    """WS-4b (2026-07-23) co-change RANKING input (Zimmermann ICSE'04 logical coupling): the
    historical coupling strength between a candidate registry file and a change-surface group's
    members, from the pre-mined ``cochanges`` table (``file_a, file_b, count``). Used ONLY as a
    TIE-BREAK when the static content-cross-reference ranking ties — DOCTRINE: co-change is an
    INTERNAL ranking prior, NEVER a delivered fact; the delivered output stays the change_surface
    registration. Returns the summed pair count over members; 0 on any error / absent table / no
    coupling (correct-or-quiet)."""
    if not graph_db or not target_rel or not member_files:
        return 0
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return 0
    try:
        try:
            tbls = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "cochanges" not in tbls:
                return 0
            t = _posix(str(target_rel)).lstrip("./")
            members = {_posix(str(m)).lstrip("./") for m in member_files if m}
            members.discard(t)
            if not members:
                return 0
            total = 0
            for m in sorted(members):
                for row in conn.execute(
                    "SELECT count FROM cochanges "
                    "WHERE (file_a = ? AND file_b = ?) OR (file_a = ? AND file_b = ?)",
                    (t, m, m, t),
                ):
                    total += int(row[0] or 0)
            return total
        except sqlite3.Error:
            return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# sibling-group mining
# --------------------------------------------------------------------------- #
def _build_slots(files: list[str]) -> list[_Slot]:
    """Bucket files that agree on every token position except one -> slots."""
    buckets: dict[tuple[str, int, tuple[str | None, ...]], dict[str, str]] = {}
    for rel in files:
        tokens, ext = _tokenize(rel)
        if not tokens:
            continue
        for i, tok in enumerate(tokens):
            blanked = tuple(tokens[:i]) + (None,) + tuple(tokens[i + 1:])
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


def _detect_registry(group: _Group, files: list[str], repo_root: str,
                     graph_db: "str | None" = None) -> None:
    """Populate group.registry_file / registry_refs: the file whose CODE lines
    cross-reference the MOST distinct group members (>=2). Test-shaped files
    (incl. ``conftest``) are excluded; docstring/comment/prose mentions never
    count (F1). Deterministic ranking: most distinct members, then most
    code-shaped reference lines (density), then — WS-4b, ONLY when the static pair ties and
    GT_CHANGE_SURFACE_COCHANGE is on — historical co-change coupling to the members (Zimmermann),
    then lexicographic path — NEVER path length (the tie-break a shorter decoy path could win).
    ``graph_db`` feeds the co-change tie-break only; flag-off => cc=0 for all => byte-identical."""
    members = sorted(group.members)
    if len(members) < _MIN_SIBLINGS:
        return
    patterns = {m: _ref_pattern(m) for m in members}
    # WS-4b co-change tie-break (doctrine: internal ranking prior, never a delivered fact).
    _cc_on = os.environ.get(
        "GT_CHANGE_SURFACE_COCHANGE", "").strip().lower() not in (
        "", "0", "false", "no", "off")
    _member_paths = {f for s in group.slots for f in s.members.values()} if _cc_on else set()
    best: "tuple[int, int, int, str] | None" = None  # (distinct, n_ref_lines, cc, rel)
    best_refs: dict[str, list[tuple[int, str]]] = {}
    best_file: str | None = None
    scanned = 0
    for rel in files:
        if scanned >= _MAX_REGISTRY_SCAN:
            break
        toks, _ext = _tokenize(rel)
        if set(toks) & _TEST_TOKENS or _is_test_path(rel):
            continue
        text = _read_text(repo_root, rel)
        if not text:
            continue
        scanned += 1
        refs: dict[str, list[tuple[int, str]]] = {}
        for lineno, line in _code_lines(text):
            for m in members:
                if patterns[m].search(line):
                    refs.setdefault(m, []).append((lineno, line))
        distinct = len(refs)
        if distinct < _MIN_SIBLINGS:
            continue
        n_ref_lines = sum(len(v) for v in refs.values())
        # WS-4b: co-change coupling is a TIE-BREAK only (0 when the flag is off => the tuple
        # comparison collapses to (distinct, n_ref_lines) + lexicographic == byte-identical).
        cc = _cochange_coupling(graph_db, rel, _member_paths) if _cc_on else 0
        if (
            best is None
            or (distinct, n_ref_lines, cc) > (best[0], best[1], best[2])
            or ((distinct, n_ref_lines, cc) == (best[0], best[1], best[2]) and rel < best[3])
        ):
            best = (distinct, n_ref_lines, cc, rel)
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
    cands.sort(key=lambda w: (w in known, w))
    return cands


def _entity_links_group(entity: str, word_seq: list[str], group: _Group) -> bool:
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
    category = {_normalize(t) for t in group.fixed_tokens}
    for i, w in enumerate(word_seq):
        if w != entity:
            continue
        if i + 1 < len(word_seq) and _normalize(word_seq[i + 1]) in category:
            return True
    return False


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
    return _ref_pattern(member_token).sub(entity, _posix(template_rel), count=1)


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
    dir_members: dict[str, dict[str, str]] = {}
    for s in impl_slots:
        for m, f in s.members.items():
            if registry_file and _posix(f) == _posix(registry_file):
                continue
            dir_members.setdefault(_posix_dir(f), {})[m] = _posix(f)
    if not dir_members:
        return None
    ranked = sorted(dir_members.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if len(ranked) >= 2 and len(ranked[0][1]) == len(ranked[1][1]):
        return None  # conflicting destination directories -> abstain
    directory, members = ranked[0]
    # richest template: prefer a file the graph confirms defines symbols, then lex
    def tkey(item: tuple[str, str]) -> tuple[int, str]:
        _m, f = item
        return (0 if f in impl_files else 1, f)
    member_token, template_file = sorted(members.items(), key=tkey)[0]
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

    (
        graph_names,
        impl_files,
        test_files,
        impl_witness_rows,
    ) = _graph_facts(graph_db)
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
    for g in groups:
        _detect_registry(g, files, repo_root, graph_db)

    word_seq = _issue_word_seq(issue_text)
    norm_issue_words = {_normalize(w) for w in word_seq}
    entities = _entity_candidates(issue_text, graph_db, known_tokens)

    result = ChangeSurfaceResult()
    for g in groups:
        rs = _role_slots(g)
        tmpl_dirs = sorted({_posix_dir(f) for s in g.slots for f in s.members.values()})
        result.sibling_groups.append({
            "members": sorted(g.members),
            "fixed_tokens": sorted(g.fixed_tokens),
            "roles": sorted(rs.keys()) + ([ROLE_REGISTRATION] if g.registry_file else []),
            "directories": tmpl_dirs,
            "registry_file": g.registry_file,
        })

    emitted_entities: set[str] = set()
    conflict = False
    for entity in entities:
        # which groups does this entity plausibly belong to?
        matched: list[tuple[int, _Group]] = []
        for g in groups:
            if _normalize(entity) in {_normalize(ft) for ft in g.fixed_tokens}:
                continue  # entity IS the category noun, not a new member
            ov = _group_overlap(g, norm_issue_words)
            if ov == 0:
                continue  # group category not named in the issue -> irrelevant
            if not _entity_links_group(entity, word_seq, g):
                continue  # entity not adjacent to this group's category
            matched.append((ov, g))
        if not matched:
            continue

        # pick the dominant group; a tie on (overlap, member-count) is a conflict
        matched.sort(key=lambda t: (-t[0], -len(t[1].members), sorted(t[1].fixed_tokens)))
        if len(matched) >= 2:
            (o0, g0), (o1, g1) = matched[0], matched[1]
            if o0 == o1 and len(g0.members) == len(g1.members) and (
                sorted(g0.fixed_tokens) != sorted(g1.fixed_tokens)
            ):
                conflict = True
                continue
        group = matched[0][1]

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

        mrs, dest = _entity_holes(
            entity,
            group,
            issue_text,
            ent_signals,
            impl_files,
            impl_witness_rows,
            repo_root,
        )
        if dest is None and _needs_destination_but_conflicted(entity, group, impl_files):
            conflict = True
            continue
        if mrs:
            result.missing_roles.extend(mrs)
            emitted_entities.add(entity)
        if dest is not None:
            result.destinations.append(dest)
            emitted_entities.add(entity)

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
                  impl_witness_rows: dict[str, tuple[RepositoryWitness, ...]],
                  repo_root: str,
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
                witnesses: list[RepositoryWitness] = []
                template_rel = _posix(template_file)
                template_rows = impl_witness_rows.get(template_rel, ())
                if template_rows:
                    # One exact production definition is enough to prove that
                    # the selected template exists as an implementation
                    # surface.  Additional definitions in the same file add no
                    # destination value.
                    witnesses.append(template_rows[0])
                else:
                    # Tree-only repositories have no definition graph.  Retain
                    # the first code-shaped template line that actually names
                    # the selected sibling member.  A path by itself never
                    # becomes a fake ``:1`` citation; if the member is absent
                    # from source, this substrate stays quiet.
                    member_pattern = _ref_pattern(member_token)
                    for line, source_text in _code_lines(
                        _read_text(repo_root, template_rel)
                    ):
                        if member_pattern.search(source_text):
                            witnesses.append(RepositoryWitness(
                                file=template_rel,
                                line=line,
                                kind="template_lexical_reference",
                                identity=member_token,
                            ))
                            break
                if group.registry_file:
                    registry_rel = _posix(group.registry_file)
                    for member in sorted(group.registry_refs):
                        rows = group.registry_refs[member]
                        if not rows:
                            continue
                        line, _text = rows[0]
                        if (
                            isinstance(line, int)
                            and not isinstance(line, bool)
                            and line > 0
                        ):
                            witnesses.append(RepositoryWitness(
                                file=registry_rel,
                                line=line,
                                kind="registration_reference",
                                identity=member,
                            ))
                witnesses = sorted(set(witnesses))
                dest = NewFileDestination(
                    entity=entity, suggested_path=_posix(suggested),
                    directory=directory, template_file=_posix(template_file),
                    registration_file=_posix(group.registry_file) if group.registry_file else None,
                    sibling_files=impl_sibs, issue_span=span, evidence=ev,
                    repository_witnesses=witnesses,
                    trust_tier=TRUST_HYPOTHESIS,
                )

    return out, dest

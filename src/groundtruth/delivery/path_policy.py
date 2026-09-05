"""Centralized delivery path policy — vendored/generated/minified exclusions."""

from __future__ import annotations

import os

_VENDOR_DIR_MARKERS: tuple[str, ...] = (
    "/extern/",
    "/externals/",
    "/vendor/",
    "/vendored/",
    "/third_party/",
    "/thirdparty/",
    "/node_modules/",
    "/bower_components/",
    "/dist/",
    "/_generated/",
    "/generated/",
    "/site-packages/",
    "/static/",
    "/assets/",
)
_MINIFIED_SUFFIXES: tuple[str, ...] = (".min.js", ".min.css", ".min.mjs", ".min.map")
_GENERATED_FILE_MARKERS: tuple[str, ...] = (
    "zz_generated",
    ".pb.go",
    ".pb.gw.go",
    "_pb2.py",
    "_pb2_grpc.py",
    ".generated.",
    "/generated/",
    "_generated.go",
    ".g.dart",
    ".freezed.dart",
)
# post_view legacy segment patterns (subset; path_policy is superset)
_VENDOR_SEGMENT_PATTERNS: tuple[str, ...] = (
    "static/",
    "vendor/",
    "node_modules/",
    "dist/",
    ".min.",
    "assets/",
)


def _norm(fp: str) -> str:
    return "/" + (fp or "").replace("\\", "/").lstrip("./").lstrip("/").lower()


def is_vendored_path(fp: str) -> bool:
    """Path-class filter: vendored / minified / generated — never a DELIVERED fact."""
    f = _norm(fp)
    if any(m in f for m in _VENDOR_DIR_MARKERS):
        return True
    base = f.rsplit("/", 1)[-1]
    if base.endswith(_MINIFIED_SUFFIXES):
        return True
    if any(m in base for m in _GENERATED_FILE_MARKERS):
        return True
    norm = fp.replace("\\", "/")
    for p in _VENDOR_SEGMENT_PATTERNS:
        if p == ".min.":
            if ".min." in norm:
                return True
        elif f"/{p}" in norm or norm.startswith(p):
            return True
    return False


# BUG-3 (2026-06-15): the RANKING demote must key on UNAMBIGUOUS machine-generated
# file-SUFFIX forms only. The bare directory substrings ``/generated/`` and
# ``.generated.`` (carried in _GENERATED_FILE_MARKERS for the delivery-exclusion
# path, where they match a basename, never the full path) wrongly demoted a
# HANDWRITTEN source file that merely lives under a ``generated/`` directory — it ate
# the -0.5 ranking penalty in graph_localizer. A dir named "generated" is not proof a
# file IN it is codegen. These suffix markers are: protobuf (.pb.go/_pb2.py/...),
# kubernetes deepcopy (zz_generated), dart codegen (.g.dart/.freezed.dart).
_GENERATED_RANK_DEMOTE_SUFFIXES: tuple[str, ...] = (
    "zz_generated",
    ".pb.go",
    ".pb.gw.go",
    "_pb2.py",
    "_pb2_grpc.py",
    "_generated.go",
    ".g.dart",
    ".freezed.dart",
)


def is_generated(fp: str) -> bool:
    """Machine-generated files (protobuf, codegen) — RANKING demote, not hard drop.

    Keys ONLY on unambiguous file-suffix markers (BUG-3): a handwritten file under a
    ``generated/`` directory is NOT machine-generated and must not eat the ranking
    demote. Path is normalized the way is_vendored_path normalizes (leading-slash +
    lowercase) so the suffix test is separator-agnostic."""
    base = _norm(fp).rsplit("/", 1)[-1]
    return any(m in base for m in _GENERATED_RANK_DEMOTE_SUFFIXES)


# ---------------------------------------------------------------------------
# TEST / DEMO path predicate — the SINGLE SOURCE for the test+demo dir filter.
# De-dup seam (2026-06-15): v1r_brief._is_test_path, the nested _is_test_file in
# _localization_header, graph_localizer._is_test_file, and gt_mini_patch.
# _is_test_or_demo_path were 4 divergent copies — the brief ones MISSED the demo
# half, leaking docs_src/ tutorial files as candidate edit targets (fastapi). All
# consumers now delegate here. DIRECTORY-SEGMENT match, never substring, so
# `contest/`/`latest/` are NOT test dirs (the load-bearing invariant the parity
# test pins). gt_mini_patch imports this with a local fallback (in-container the
# groundtruth import may fail — same pattern as the fact set).
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        # Fable LSP11: `__tests` (no trailing __) is csstree's real layout `lib/__tests/`;
        # a segment-exact entry, so it never false-positives on `contest`/`latest`.
        # Fable P11: `testing` REMOVED — it is production-ambiguous (Django `django/test`-style
        # shipped test UTILITIES, Go `testing` helpers) and wrongly demoted/excluded real source.
        # Mirrors walker.go nonSourceDirSegments (kept in sync).
        "test",
        "tests",
        "__tests__",
        "__test__",
        "__tests",
        "spec",
        "specs",
        "e2e",
    }
)
_DEMO_NONSOURCE_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        "examples",
        "example",
        "demo",
        "demos",
        "sample",
        "samples",
        "fixtures",
        "fixture",
        "docs",
        "doc",
        "docs_src",
        "doc_src",
        "documentation",
        "tutorial",
        "tutorials",
        "benchmark",
        "benchmarks",
        "benches",
        "bench",
        "vendor",
        "node_modules",
        "dist",
        "build",
        # fuzz / property-based / mutation test dirs (mirrors walker.go nonSourceDirSegments —
        # DUPLICATION TRAP: keep these two sets in sync; the wasmi leak was fuzz/ missing here)
        "fuzz",
        "fuzzing",
        "fuzz_targets",
        "corpus",
        "testcases",
        # Fable P11: `conformance` + `compat` REMOVED — production-ambiguous (pandas/numpy
        # `compat` shims, protobuf `conformance` are real source). Mirrors walker.go.
        "integration_tests",
        "e2e_tests",
    }
)
_TEST_TOOLING_ROOT_SEGMENTS: frozenset[str] = frozenset(
    set(_TEST_DIR_SEGMENTS)
    | set(_DEMO_NONSOURCE_DIR_SEGMENTS)
    | {
        "support",
        "supports",
        "helper",
        "helpers",
        "mock",
        "mocks",
        "stub",
        "stubs",
        "fake",
        "fakes",
        "testdata",
        "testutils",
        "testutil",
    }
)


def _path_segments(path: str) -> tuple[list[str], str]:
    p = (path or "").replace("\\", "/").lower()
    if p.startswith("./"):
        p = p[2:]
    segs = [s for s in p.split("/") if s]
    return segs, (segs[-1] if segs else "")


# Exact test-module basenames the pattern markers below MISS: pytest ``conftest.py``
# (fixtures that call product symbols — near-universal in Python repos) and the
# Django/convention ``tests.py`` / ``test.py`` test modules. RATIONALE (corrected
# 2026-07-05, Fable spec-review reconciliation): walker.go DOES now flag these at index
# time (``walker.go:218`` sets is_test=1 on conftest.py/tests.py/test.py/*_tests.py,
# Fable 2026-07-03), so a FRESHLY-BUILT graph is already correct. This Python guard is
# defense-in-depth for FROZEN / pre-existing graphs indexed by an OLDER binary that
# predates that rule (is_test=0 on their nodes) — without it a delivered CALLER line
# could surface a graded-test function NAME + location (proven leak). Exact match (never
# substring), so ``latest.py`` / ``mytest.py`` / ``attestation.py`` stay clean.
_TEST_FILE_BASENAMES = frozenset({"conftest.py", "tests.py", "test.py"})


def is_test_path(path: str) -> bool:
    """True iff a TEST file — by DIRECTORY SEGMENT (a relative top-level ``test/x.js``
    is caught; ``contest/``/``latest/`` are NOT — substring is never enough) OR a
    basename marker. FULL PARITY with the Go indexer's ``walker.IsTestFile``
    (``gt-index/internal/walker/walker.go:210-271``) so this defense-in-depth guard
    for FROZEN / stale-``is_test`` graphs catches EVERY test convention the walker
    flags at index time — not just the Python/JS subset (Fable E2: the under-covering
    set leaked ``*_tests.py`` / ``*.tests.ts`` / ``*_spec.rb`` / ``tests.rs`` /
    ``FooTest.java`` bodies through witness/caller renders). Stem+ext rules are
    EXT-GATED and CASE-SENSITIVE for the CamelCase forms, so ``latest.py`` /
    ``attestation.py`` / ``mytest.py`` stay clean."""
    segs, bn = _path_segments(path)
    if not segs:
        return False
    if any(s in _TEST_DIR_SEGMENTS for s in segs[:-1]):
        return True
    if bn in _TEST_FILE_BASENAMES:
        return True
    # boundary-delimited basename markers (incl. the JS/TS plural .tests./.specs.)
    if (
        bn.startswith("test_")
        or "_test." in bn
        or ".test." in bn
        or ".spec." in bn
        or ".tests." in bn
        or ".specs." in bn
    ):
        return True
    # walker.IsTestFile stem/ext parity (defense-in-depth on stale-is_test graphs).
    # Use the ORIGINAL-CASE basename: bn is lowercased by _path_segments, which would
    # break the CASE-sensitive CamelCase forms (FooTest.java) AND make lowercase
    # "latest"/"attestation" ambiguous with a "test" suffix. ext is matched lowercased.
    _ob = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    dot = _ob.rfind(".")
    ext = (_ob[dot:] if dot > 0 else "").lower()
    stem = _ob[:dot] if dot > 0 else _ob
    if ext == ".py" and stem.endswith("_tests"):  # pytest plural module
        return True
    if ext in (".java", ".kt", ".kts", ".scala", ".groovy") and (  # JVM (CASE-sensitive)
        stem.endswith("Test") or stem.endswith("Tests") or stem.endswith("Spec")
    ):
        return True
    if ext == ".cs" and (stem.endswith("Test") or stem.endswith("Tests")):  # C#
        return True
    if ext == ".php" and stem.endswith("Test"):  # PHPUnit
        return True
    if ext == ".swift" and (stem.endswith("Tests") or stem.endswith("Test")):
        return True
    if ext == ".rb" and stem.endswith("_spec"):  # RSpec
        return True
    if ext == ".rs" and (  # Rust module test files
        bn in ("tests.rs", "test.rs") or stem.endswith("_tests")
    ):
        return True
    return False


def is_test_or_demo(path: str) -> bool:
    """``is_test_path`` OR a DEMO/non-source dir segment (examples/demo/docs/docs_src/
    tutorial/fixtures/vendor/node_modules/dist/build). The candidate-emit + scope +
    witness + cochange surfaces use THIS; the brief copies missing the demo half were
    the docs_src/ leak. Never a delivered scope/witness/candidate."""
    segs, _ = _path_segments(path)
    if any(s in _DEMO_NONSOURCE_DIR_SEGMENTS for s in segs[:-1]):
        return True
    return is_test_path(path)


def _has_test_tooling_root_marker(path: str) -> bool:
    """True when a candidate root's own path indicates test/support/tooling.

    Import topology alone is not a source-role proof: normal production roots can
    be imported mostly or only by tests in sparse graphs. Hard test-tooling
    exclusion therefore needs an independent path-role marker before the graph
    import fixpoint may classify the root.
    """
    segs, _ = _path_segments(path)
    return any(s in _TEST_TOOLING_ROOT_SEGMENTS for s in segs)


def is_deliverable(path: str) -> bool:
    """THE single "may this path be shown to the agent" predicate (Class-A chokepoint,
    2026-06-17). True iff ``path`` is genuine source the agent may be pointed at as a
    Caller / scope / witness / candidate — i.e. NOT a test/demo/docs path AND NOT a
    vendored/minified/generated path. Composes the two existing path-class predicates so
    every render surface has ONE entry: no surface re-implements "is this non-source."
    Correct-or-quiet (excludes on either class); generalized (dir-segment + file-suffix
    markers, no per-repo/benchmark logic)."""
    return not (is_test_or_demo(path) or is_vendored_path(path))


def test_tooling_roots(graph_db: str) -> frozenset[str]:
    """Directories that are TEST-TOOLING — every importer OUTSIDE the directory is a
    test file. These are vendored assertion/debug/diff libraries (testify, spew,
    difflib, …) copied under ``internal/`` / ``pkg/`` where the ``vendor/`` markers
    miss them: they lexically match an issue's error/panic vocabulary and out-rank the
    real gold, yet are NEVER a feature edit target. Graph-derived from IMPORTS edges —
    no library names, no benchmark shape, language-agnostic (Tip&Palsberg dependency
    analysis). A file under any such root reuses the EXISTING test-file demote (no new
    weight/threshold). Conservative: a root needs >=1 external importer and ALL of them
    must be tests; ``require``→``assert`` (both non-test, same vendored tree) does NOT
    disqualify the ``internal/testify`` root because that importer is INSIDE it."""
    import sqlite3

    pairs: list[tuple[str, str]] = []
    try:
        conn = sqlite3.connect(graph_db)
        try:
            pairs = [
                (str(a).replace("\\", "/"), str(b).replace("\\", "/"))
                for a, b in conn.execute(
                    """
                    SELECT DISTINCT tgt.file_path, src.file_path
                    FROM edges e
                    JOIN nodes tgt ON tgt.id = e.target_id
                    JOIN nodes src ON src.id = e.source_id
                    WHERE e.type = 'IMPORTS'
                      AND tgt.file_path IS NOT NULL AND src.file_path IS NOT NULL
                    """
                ).fetchall()
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return frozenset()
    # For each ancestor dir D of an imported file, record importers that are OUTSIDE D.
    ext_importers: dict[str, set[str]] = {}
    for imported, importer in pairs:
        segs = [s for s in imported.split("/") if s][:-1]  # dirs of the imported file
        d = ""
        for seg in segs:
            d = f"{d}/{seg}" if d else seg
            if not (importer == d or importer.startswith(d + "/")):  # importer outside D
                ext_importers.setdefault(d, set()).add(importer)
    # Fixpoint: a dir is test-tooling if every external importer is a test file OR is
    # itself already inside a test-tooling dir. Catches TRANSITIVE vendoring — testify
    # (round 1: all importers are tests) pulls in spew (round 2: imported only by
    # testify + tests), the chain a single pass misses. Monotone over a finite dir set
    # => terminates.
    roots: set[str] = set()
    changed = True
    while changed:
        changed = False
        for d, imps in ext_importers.items():
            if d in roots:
                continue
            if not _has_test_tooling_root_marker(d):
                continue
            if imps and all(
                is_test_path(i) or any(i == r or i.startswith(r + "/") for r in roots) for i in imps
            ):
                roots.add(d)
                changed = True
    return frozenset(roots)


def is_test_tooling(fp: str, roots: frozenset[str]) -> bool:
    """True iff ``fp`` lives under any test-tooling root (see ``test_tooling_roots``)."""
    f = (fp or "").replace("\\", "/").lstrip("./").lstrip("/")
    return any(f == r or f.startswith(r + "/") for r in roots)


def is_delivery_excluded(fp: str, repo_root: str = "") -> bool:
    """True when path must never appear in a DELIVERED fact."""
    if is_vendored_path(fp):
        return True
    if repo_root:
        return is_minified_file(repo_root, fp)
    return False


_minified_cache: dict[str, bool] = {}
_MINIFIED_MEAN_LINE_LEN = 200


def is_minified_file(repo_root: str, rel: str) -> bool:
    if rel in _minified_cache:
        return _minified_cache[rel]
    verdict = False
    try:
        with open(os.path.join(repo_root or "", rel), encoding="utf-8", errors="ignore") as fh:
            head = fh.read(16384)
        lines = [ln for ln in head.splitlines() if ln.strip()]
        if lines:
            verdict = (sum(len(ln) for ln in lines) / len(lines)) > _MINIFIED_MEAN_LINE_LEN
    except OSError:
        verdict = False
    _minified_cache[rel] = verdict
    return verdict

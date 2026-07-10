#!/usr/bin/env python3
"""build_eval_corpus.py — the stratified EVALUATION-task corpus builder.

PURPOSE (calibration/eval data ONLY — explicitly NOT training data). Every calibration
decision GT makes (abstention thresholds, hit@k at scale, per-stratum performance) needs a
task corpus larger and more honest than the ~6-task testset, with SEVERE splits so
"generalized" is falsifiable. This builds that corpus from two leak-safe sources:

  (a) MINED  — the LOCAL git history of a repo. The tasks are issue-less, so each is
     constructed by INVERSE PATCH: a commit that FIXES something -> task = the PARENT
     snapshot (base_sha = commit^) + a problem statement reconstructed FROM THE COMMIT
     MESSAGE (never from the diff itself). Gold paths / test ids in the message are redacted.
  (b) LIVELITE — the existing public benchmark dataset (swebench_live_lite.jsonl). We REUSE
     its real issue ``problem_statement`` + repo/base_commit anchors. We copy NO gold patch,
     NO test_patch, NO FAIL_TO_PASS/PASS_TO_PASS into any row (leak law).

Row schema (exactly):
  {task_id, source, repo, base_sha, date, problem_statement, stratum, split, provenance}

STRATA (deterministic): reuse ``groundtruth.pretask.stratum.classify_stratum`` (A/B/C/D ->
symbol_anchored / behavioral / traceback / new_file_feature) and EXTEND — in this script only,
never editing the module — with two conservative keyword+shape rules: ``config_dependency``
and ``insufficient_evidence`` (documented in ``classify_extended``). Offline (no graph.db),
``classify_stratum`` is correct-or-quiet: A/D degrade to B, so symbol_anchored/new_file_feature
appear only when a ``--graph-db`` is supplied. That degradation is reported honestly in
PROVENANCE, never papered over.

SPLITS: repo-disjoint with repo-level time ordering. The REPO IDENTITY (normalized via the
origin remote URL, see ``_repo_identity``) is the atomic unit — no repo appears in both
splits. Repos are ordered by their EARLIEST task date; the older half becomes ``calibration``,
the newer half ``eval``. HONESTY: this is repo-granular time ordering, NOT task-level
time-disjointness — individual task dates inside a repo may straddle the boundary date
(``rule_granularity: "repo-earliest, task dates may straddle"``, recorded in PROVENANCE).

LEAK LAW: no gold file paths, no test names, no FAIL_TO_PASS, no patch content in any row.
Public live-lite prose is kept verbatim EXCEPT targeted redaction of the leak-shaped tokens
(test identifiers, test-FILE names of all classes — .spec.js / FooTest.java / _spec.rb /
test_*.py — pytest node-ids, patch-hunk markers, the FAIL_TO_PASS/PASS_TO_PASS literals).
Mined statements additionally redact slashed source paths AND bare source-file basenames
("fix ... in syncer_csa.go") — a commit message routinely names its own gold files.
TWIN GUARD: a mined row whose fix commit (or squash-PR ref) IS a livelite task's gold commit
is DROPPED (recorded in PROVENANCE.dropped_twins) — otherwise the corpus would carry the same
task twice, once with its gold pointer minable from the local history.

LAWS: deterministic (two runs -> byte-identical outputs; everything sorted; NO wallclock in
rows), LLM-free, no network. git subprocesses run with PYTHONIOENCODING=utf-8.
``write_outputs`` FAILS CLOSED (raises) if the leak scan or the repo-disjoint invariant does
not hold on the rows actually being written.

Usage:
  build_eval_corpus.py --from-livelite benchmarks/data/swebench_live_lite.jsonl \\
                       --from-history . --out-dir D:/gt_runs/eval_corpus_20260710/
  build_eval_corpus.py --from-livelite <jsonl>            # print corpus JSON to stdout
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess

from groundtruth.pretask.stratum import classify_stratum

# Rule-set version — bump when any classification/redaction rule below changes. Recorded in
# PROVENANCE so a downstream calibration run knows exactly which rules produced the labels.
# p7s.2.0: LIPI-review fixes F1-F6 (bare-basename + test-file redaction, code-signal override,
# repo-identity normalization, livelite-twin drop, fail-closed writes).
# p7s.2.1: R2 — extensionless >=2-segment SPECIFIER redaction (conventional-commit scopes /
# URL paths, e.g. "fix(utils/mime):" -> gold src/utils/mime.ts), mined statements only;
# R1 — write_outputs recomputes the split overlap from the rows being written.
CLASSIFICATION_RULES_VERSION = "p7s.2.1"

STRATA = (
    "symbol_anchored",      # base A — issue names a symbol that resolves in the graph
    "behavioral",           # base B — prose-only, no resolving anchor (offline default)
    "traceback",            # base C — a stack trace is present (most precise localizer)
    "new_file_feature",     # base D — code that resolves nowhere (feature to be built)
    "config_dependency",    # local rule — config/manifest/dependency is the subject
    "insufficient_evidence",  # local rule — statement too thin to localize from
)

# base classify_stratum label -> our stratum name.
_BASE_MAP = {"A": "symbol_anchored", "B": "behavioral",
             "C": "traceback", "D": "new_file_feature"}

# ---------------------------------------------------------------------------
# insufficient_evidence — a conservative STRUCTURAL floor (not tuned to any bench).
# A statement a human could not localize from: too few non-space chars OR too few word
# tokens OR an explicit placeholder phrase. Deliberately small so it fires only on the
# genuinely empty/placeholder cases (the Live-Lite 42-char placeholder, empty strings).
# ---------------------------------------------------------------------------
MIN_NONSPACE_CHARS = 40
MIN_WORD_TOKENS = 5
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:n/?a|tbd|todo|placeholder|no\s+(?:problem|issue|description)\b|none)\s*\.?\s*$",
    re.I,
)
# F6(ii) — a CONCRETE-CODE signal overrides the brevity floors: a short title that names a
# real identifier ("Support `unicode-range`") is localizable, not insufficient. Conservative
# shapes only: a backticked token, snake_case, camelCase, or a call-parens token. The
# placeholder rule stays absolute (a placeholder never carries a code token).
_BACKTICK_RE = re.compile(r"`[^`\n]{2,}`")
_CODE_SHAPE_RE = re.compile(r"\b[A-Za-z]\w*_\w+\b|\b[a-z]+[A-Z]\w*\b|\b\w+\(\)")


def _has_code_signal(text: str) -> bool:
    t = text or ""
    return bool(_BACKTICK_RE.search(t) or _CODE_SHAPE_RE.search(t))

# ---------------------------------------------------------------------------
# config_dependency — conservative keyword+shape. Fires only when NOT a traceback and NOT a
# resolved-symbol task (base != A) AND one of:
#   (a) a recognized manifest/lockfile filename is named, OR
#   (b) a dependency phrase appears in the TITLE region (first non-empty line), OR
#   (c) >= 2 DISTINCT dependency phrases appear anywhere.
# This keeps a lone casual "version" from stealing an ordinary bug into config_dependency.
# ---------------------------------------------------------------------------
_MANIFEST_FILES = (
    "requirements.txt", "setup.py", "setup.cfg", "pyproject.toml", "pipfile", "poetry.lock",
    "package.json", "package-lock.json", "yarn.lock", "go.mod", "go.sum", "cargo.toml",
    "cargo.lock", "pom.xml", "build.gradle", "gemfile", "composer.json", "tsconfig.json",
    "dockerfile", "docker-compose", "tox.ini", "makefile", ".pre-commit-config",
)
_DEP_PHRASES = (
    "dependency", "dependencies", "pip install", "npm install", "version constraint",
    "requirement", "incompatible version", "pinned version", "pin the version",
    "version pin", "upgrade to", "downgrade", "lockfile", "lock file",
    "environment variable", "config option", "configuration file", "build config",
)
_MANIFEST_RE = re.compile(r"(?<![\w./])(" + "|".join(re.escape(m) for m in _MANIFEST_FILES)
                          + r")(?![\w])", re.I)


def _title_region(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s.lower()
    return ""


def _is_insufficient(text: str) -> bool:
    t = text or ""
    if _PLACEHOLDER_RE.match(t):
        return True
    if _has_code_signal(t):
        # F6(ii): a concrete-symbol signal overrides the brevity floors — the statement
        # names code and is localizable even when very short (weasyprint-2405 class).
        return False
    if len(re.sub(r"\s+", "", t)) < MIN_NONSPACE_CHARS:
        return True
    if len(_WORD_RE.findall(t)) < MIN_WORD_TOKENS:
        return True
    return False


def _is_config_dependency(text: str) -> bool:
    body = (text or "").lower()
    if _MANIFEST_RE.search(text or ""):
        return True
    title = _title_region(text)
    if any(p in title for p in _DEP_PHRASES):
        return True
    hits = {p for p in _DEP_PHRASES if p in body}
    return len(hits) >= 2


def classify_extended(problem_statement: str, graph_db: str | None = None) -> str:
    """Classify a problem statement into one of ``STRATA`` (deterministic, LLM-free).

    Precedence: insufficient_evidence > traceback (base C) > config_dependency >
    {symbol_anchored | new_file_feature | behavioral} (base A/D/B). A traceback wins over
    config because a stack trace is the more precise localizer; config wins over a plain
    behavioral/new-file bug when a manifest/dependency is the actual subject, but never over
    a resolved-symbol task (base A). Classification runs on the RAW statement; the STORED
    statement is redacted separately.
    """
    if _is_insufficient(problem_statement):
        return "insufficient_evidence"
    base = classify_stratum(problem_statement, graph_db=graph_db).label
    if base == "C":
        return "traceback"
    if base != "A" and _is_config_dependency(problem_statement):
        return "config_dependency"
    return _BASE_MAP.get(base, "behavioral")


# ===========================================================================
# REDACTION (deterministic, leak-safe) — mirrors gt_observation_corpus discipline:
# keep the prose, scrub the leak-shaped tokens to a bracketed marker.
# ===========================================================================
_SRC_EXT = (r"(?:py|pyi|go|ts|tsx|js|jsx|mjs|cjs|java|rb|rs|c|cc|cpp|cxx|h|hpp|"
            r"kt|kts|scala|php|swift|m|mm|cs)")
# a pytest/JS node-id: path.ext::Thing[::thing] — scrub whole thing (carries file + tests).
_NODEID_RE = re.compile(r"(?<![\w])[\w./\-]+\.%s::[\w:.\-]+" % _SRC_EXT)
_TEST_ID_RE = re.compile(r"(?<![\w])test_[A-Za-z0-9_]+")
_TEST_SUFFIX_RE = re.compile(r"(?<![\w])[A-Za-z0-9]+_test(?![\w])")
_FAILPASS_RE = re.compile(r"\b(?:FAIL_TO_PASS|PASS_TO_PASS)\b")
_DIFFGIT_RE = re.compile(r"diff --git[^\n]*")
_HUNK_RE = re.compile(r"^@@.*?@@.*$", re.M)
_DIFF_FILE_RE = re.compile(r"^(?:\+\+\+|---)\s.*$", re.M)
# a slashed path with an extension (gold-code / infra file) — mined statements only.
_PATH_RE = re.compile(r"(?<![\w])(?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]+")
# F1 — bare (slashless) source-file basenames leak the gold file from mined commit messages
# ("fix ... in syncer_csa.go"). EXPLICIT conservative extension list (review F1) so ordinary
# prose is never nuked; extensions like .txt are deliberately excluded. Extend the list, never
# loosen it to "any extension". Applied only under redact_paths (mined statements).
_BARE_SRC_EXTS = (
    "py|pyi|go|js|jsx|mjs|cjs|ts|tsx|rs|java|rb|c|h|cc|cpp|cxx|hpp|kt|kts|scala|php|"
    "swift|cs|yml|yaml|toml|json|md|rst|sh|bash|ps1|cfg|ini|lock|mod|sum|gradle"
)
_BARE_BASENAME_RE = re.compile(r"(?<![\w/])[\w.\-]+\.(?:%s)\b" % _BARE_SRC_EXTS)
# F1 — test-FILE name classes beyond bare test_/_test identifiers: foo.spec.js / foo.test.tsx
# / foo_spec.rb / storages_test.go / FooTest.java / test_x.py. Test names are leak-law targets
# in BOTH sources, so this applies regardless of redact_paths.
_TEST_FILE_RE = re.compile(
    r"(?<![\w])(?:"
    r"[\w.\-]*\.(?:spec|test)\.[A-Za-z0-9]+"     # basicAuth.spec.js, foo.test.tsx
    r"|test_[\w\-]+\.[A-Za-z0-9]+"                # test_utils.py
    r"|[\w.\-]*_(?:test|spec)s?\.[A-Za-z0-9]+"    # storages_test.go, parser_spec.rb
    r"|[A-Z]\w*Tests?\.java"                       # FooTest.java / FooTests.java
    r")(?![\w])")
# R2 — extensionless SPECIFIER paths leak the gold file in conventional-commit scopes and
# URL paths ("fix(utils/mime):" -> gold src/utils/mime.ts). Conservative by construction:
# >= 2 slash-separated segments (single words NEVER match — the bare stem/module class is
# stratum-A task input by design and stays), and every segment must START with a letter or
# underscore (dates/versions like 2026/07/09 never match). Common English slash-pairs are
# kept via the documented stoplist. Applied only under redact_paths (mined statements).
_SPECIFIER_PATH_RE = re.compile(
    r"(?<![\w./\-])[A-Za-z_][\w.\-]*(?:/[A-Za-z_][\w.\-]*)+(?![\w/])")
_SPECIFIER_STOP = frozenset({
    "and/or", "either/or", "yes/no", "y/n", "n/a", "w/o", "i/o", "a/b", "ci/cd", "tcp/ip",
})


def _specifier_sub(m: re.Match) -> str:
    tok = m.group(0)
    return tok if tok.lower() in _SPECIFIER_STOP else "[path]"


def redact_statement(text: str, redact_paths: bool = False) -> str:
    """Scrub leak-shaped tokens from a statement. ALWAYS scrubbed (both sources): patch/hunk
    markers, pytest node-ids, test-FILE names (.spec.js / FooTest.java / _spec.rb / test_*.py),
    bare test identifiers, and the FAIL_TO_PASS/PASS_TO_PASS literals. Scrubbed only when
    ``redact_paths`` (mined statements — a commit message routinely names its own gold files):
    slashed source paths, bare source-file basenames (explicit extension list,
    ``_BARE_SRC_EXTS``), and extensionless >=2-segment SPECIFIER paths (commit scopes / URL
    paths, ``_SPECIFIER_PATH_RE``). Public live-lite prose keeps its non-test path mentions
    verbatim. Ordering matters: widest shapes first (diff markers, node-ids, extensioned
    slashed paths), then test-file names, then bare basenames, then extensionless specifiers,
    then bare test identifiers — so a later narrower rule never leaves a fragment of a wider
    one."""
    if not text:
        return text or ""
    s = text
    s = _DIFFGIT_RE.sub("[patch-hunk]", s)
    s = _HUNK_RE.sub("[patch-hunk]", s)
    s = _DIFF_FILE_RE.sub("[patch-hunk]", s)
    s = _NODEID_RE.sub("[test-id]", s)
    s = _FAILPASS_RE.sub("[test-metadata]", s)
    if redact_paths:
        s = _PATH_RE.sub("[path]", s)
    s = _TEST_FILE_RE.sub("[test-id]", s)
    if redact_paths:
        s = _BARE_BASENAME_RE.sub("[path]", s)
        s = _SPECIFIER_PATH_RE.sub(_specifier_sub, s)
    s = _TEST_ID_RE.sub("[test-id]", s)
    s = _TEST_SUFFIX_RE.sub("[test-id]", s)
    return s


# ===========================================================================
# DATE NORMALIZATION (for splitting only — the ROW keeps the original date string)
# ===========================================================================
def _norm_date(raw: str) -> _dt.datetime:
    """Parse an ISO-8601 date to a naive-UTC datetime for deterministic ordering. Handles
    both tz-aware (`...-04:00`) and naive (`...T10:00:00.000`) forms. Unparseable -> epoch."""
    s = (raw or "").strip()
    try:
        d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return _dt.datetime(1970, 1, 1)
    if d.tzinfo is not None:
        d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return d


# ===========================================================================
# SOURCE (a): MINED — inverse-patch tasks from local git history
# ===========================================================================
_FIX_SUBJECT_RE = re.compile(r"\b(?:fix|fixes|fixed|bug|bugfix|resolve[sd]?|"
                             r"correct(?:s|ed)?|repair|patch(?:es|ed)?)\b", re.I)
# a changed file is "code-ish" (a real task surface) if it has one of these extensions or is
# a well-known infra/build file. Doc-only commits (.md/.rst/.txt) are not tasks.
_CODE_EXT = {
    "py", "pyi", "go", "ts", "tsx", "js", "jsx", "mjs", "cjs", "java", "rb", "rs", "c", "cc",
    "cpp", "cxx", "h", "hpp", "kt", "kts", "scala", "php", "swift", "m", "mm", "cs",
    "yml", "yaml", "sh", "toml", "cfg", "ini", "mk", "gradle", "json",
}
_CODE_BASENAMES = {"dockerfile", "makefile", "gemfile", "rakefile"}
# git trailers / boilerplate — dropped from the reconstructed statement (they are not problem
# description, and Signed-off-by/Co-Authored-By carry contributor emails = noise). NO trailing
# \b: these prefixes end in ':' (a non-word char), so a \b after them would never match.
_TRAILER_RE = re.compile(
    r"^\s*(?:co-authored-by:|signed-off-by:|acked-by:|reviewed-by:|reported-by:|"
    r"tested-by:|cc:|.*generated with)", re.I)


def _git(repo: str, *args: str) -> str:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", env=env)
    return out.stdout


def _is_code_change(files: list[str]) -> bool:
    for f in files:
        base = f.rsplit("/", 1)[-1].lower()
        ext = base.rsplit(".", 1)[-1] if "." in base else ""
        if ext in _CODE_EXT or base in _CODE_BASENAMES:
            return True
    return False


def _clean_message(msg: str) -> str:
    """Drop commit trailers (Co-Authored-By, Signed-off-by, 'Generated with ...') — they are
    boilerplate, never problem description."""
    kept = [ln for ln in (msg or "").splitlines() if not _TRAILER_RE.match(ln)]
    return "\n".join(kept).strip()


# ---------------------------------------------------------------------------
# F3 — repo IDENTITY normalization. Two checkouts of the same upstream (a ".tmp_holdout_wp/
# weasyprint" clone vs livelite "Kozea/WeasyPrint") MUST be ONE atomic unit in the split, or
# repo-disjointness holds only by luck. Primary: the origin remote URL's owner/name.
# Fallbacks, in order: an explicit --repo-alias map entry (for remoteless checkouts), then
# the directory basename. assign_splits compares identities CASEFOLDED.
# ---------------------------------------------------------------------------
_REMOTE_OWNER_NAME_RE = re.compile(r"[:/]([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")
_PR_REF_RE = re.compile(r"\(#(\d+)\)")


def parse_owner_name(url: str) -> str:
    """'https://github.com/acme/x.git' or 'git@github.com:acme/x.git' -> 'acme/x'."""
    m = _REMOTE_OWNER_NAME_RE.search((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _repo_identity(repo_path: str, alias: dict[str, str] | None = None) -> str:
    label = os.path.basename(os.path.abspath(repo_path).rstrip("/\\")) or "repo"
    if alias and label in alias:
        return alias[label]
    try:
        url = _git(repo_path, "remote", "get-url", "origin").strip()
    except (subprocess.CalledProcessError, OSError):
        url = ""
    return (parse_owner_name(url) if url else "") or label


def extract_pr_refs(message: str) -> list[str]:
    """Distinct '(#N)' squash-merge PR refs in a commit message, sorted numerically.
    Deliberately requires the parenthesized form — a bare '#N' is usually an issue ref."""
    return sorted({m.group(1) for m in _PR_REF_RE.finditer(message or "")}, key=int)


def iter_mined_rows(repo_path: str, graph_db: str | None = None,
                    max_mined: int = 0, identity: str | None = None) -> list[dict]:
    """Build inverse-patch rows from a local git repo. A qualifying commit: subject matches a
    fix verb, has a parent (base snapshot), and touches >=1 code-ish file. base_sha = parent;
    problem_statement = redacted commit message; the fix DIFF is never read into the row.
    ``repo`` is the NORMALIZED identity (``_repo_identity``: origin remote owner/name, alias,
    or directory basename); ``identity`` may be passed in to skip the git remote lookup."""
    repo_path = os.path.abspath(repo_path)
    repo_label = os.path.basename(repo_path.rstrip("/\\")) or "repo"
    repo_ident = identity if identity else _repo_identity(repo_path)
    sep, fsep = "\x1e", "\x1f"
    fmt = fsep.join(["%H", "%P", "%aI", "%B"]) + sep
    log_args = ["log", "--no-merges", "--format=" + fmt]
    if max_mined:
        # git log is newest-first & deterministic for a fixed repo state; bound the scan
        # window so we never diff-tree an entire 26k-commit history to find a few fixes.
        log_args += ["-n", str(max_mined * 50)]
    try:
        raw = _git(repo_path, *log_args)
    except (subprocess.CalledProcessError, OSError):
        return []
    rows: list[dict] = []
    for rec in raw.split(sep):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(fsep)
        if len(parts) < 4:
            continue
        sha, parents, adate, message = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3]
        subject = (message.splitlines() or [""])[0]
        if not _FIX_SUBJECT_RE.search(subject):
            continue
        parent = parents.split()[0] if parents.split() else ""
        if not parent:  # root commit: no base snapshot to reconstruct from
            continue
        try:
            changed = [ln.strip() for ln in _git(
                repo_path, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
            ).splitlines() if ln.strip()]
        except (subprocess.CalledProcessError, OSError):
            changed = []
        if not _is_code_change(changed):
            continue
        clean = _clean_message(message)
        stratum = classify_extended(clean, graph_db=graph_db)
        rows.append({
            "task_id": f"mined__{repo_label}__{sha[:12]}",
            "source": "mined",
            "repo": repo_ident,
            "base_sha": parent,
            "date": adate,
            "problem_statement": redact_statement(clean, redact_paths=True),
            "stratum": stratum,
            "split": "",  # assigned later
            "provenance": {
                "source_kind": "git_history",
                "repo_label": repo_label,
                "repo_identity": repo_ident,
                "fix_commit": sha,
                "parent_commit": parent,
                "pr_refs": extract_pr_refs(message),
                "statement_origin": "commit_message",
                "rules_version": CLASSIFICATION_RULES_VERSION,
            },
        })
        if max_mined and len(rows) >= max_mined:
            break  # newest-first log => the first max_mined qualifying rows are the newest
    rows.sort(key=lambda r: r["task_id"])
    return rows


# ===========================================================================
# SOURCE (b): LIVELITE — reuse public issue statements + anchors
# ===========================================================================
def iter_livelite_rows(jsonl_path: str, graph_db: str | None = None) -> list[dict]:
    """Build rows from a swebench-live-lite-style jsonl. Reuses problem_statement + repo +
    base_commit + created_at. NEVER copies patch / test_patch / FAIL_TO_PASS / PASS_TO_PASS."""
    rows: list[dict] = []
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        iid = str(d.get("instance_id") or "").strip()
        if not iid:
            continue
        ps = d.get("problem_statement") or ""
        stratum = classify_extended(ps, graph_db=graph_db)
        rows.append({
            "task_id": iid,
            "source": "livelite",
            "repo": str(d.get("repo") or "").strip(),
            "base_sha": str(d.get("base_commit") or "").strip(),
            "date": str(d.get("created_at") or "").strip(),
            "problem_statement": redact_statement(ps, redact_paths=False),
            "stratum": stratum,
            "split": "",
            "provenance": {
                "source_kind": "benchmark_dataset",
                "dataset": os.path.basename(jsonl_path),
                "instance_id": iid,
                "public": True,
                "rules_version": CLASSIFICATION_RULES_VERSION,
            },
        })
    rows.sort(key=lambda r: r["task_id"])
    return rows


def _livelite_gold_index(jsonl_path: str) -> dict:
    """Gold pointers of the livelite dataset, used ONLY for the mined-TWIN intersection
    check (F3): fix-commit SHAs from ``commit_urls`` and (repo, pull_number) pairs. These
    values are NEVER copied into any corpus row."""
    sha_to_iid: dict[str, str] = {}
    repo_pr_to_iid: dict[tuple[str, str], str] = {}
    try:
        fh = open(jsonl_path, encoding="utf-8", errors="replace")
    except OSError:
        return {"sha_to_iid": sha_to_iid, "repo_pr_to_iid": repo_pr_to_iid}
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            iid = str(d.get("instance_id") or "").strip()
            if not iid:
                continue
            for u in (d.get("commit_urls") or []):
                m = re.search(r"/commit/([0-9a-fA-F]{7,40})", str(u))
                if m:
                    sha_to_iid[m.group(1).lower()] = iid
            pn = str(d.get("pull_number") or "").strip()
            rp = str(d.get("repo") or "").strip().casefold()
            if pn and rp:
                repo_pr_to_iid[(rp, pn)] = iid
    return {"sha_to_iid": sha_to_iid, "repo_pr_to_iid": repo_pr_to_iid}


def _sha_match(a: str, b: str) -> bool:
    """Prefix-tolerant commit-SHA equality (min overlap 12 hex chars, conservative)."""
    a, b = (a or "").lower(), (b or "").lower()
    n = min(len(a), len(b))
    return n >= 12 and a[:n] == b[:n]


# ===========================================================================
# SPLIT — repo-disjoint, repo-level time ordering (calibration=older repos)
# ===========================================================================
def assign_splits(rows: list[dict], enforce_repo_disjoint: bool = True) -> dict:
    """Assign each row a ``split`` in {calibration, eval}. With the guard on (default), the
    REPO IDENTITY (casefolded) is atomic: repos are ordered by their EARLIEST task date and
    the older half -> calibration, the newer half -> eval, so NO repo appears in both splits.
    HONESTY (F5): this is repo-granular time ordering, NOT task-level time-disjointness —
    individual task dates inside a repo may straddle the boundary date. Recorded as
    ``rule_granularity`` in the boundary dict.

    ``enforce_repo_disjoint=False`` is the MUTATION handle: it splits per-TASK at the global
    median date, so a repo whose tasks straddle the median lands in BOTH splits — the
    disjointness invariant then breaks and the test bites."""
    boundary: dict = {"rule": "", "n_repos": 0, "boundary_date": None}
    if not rows:
        return {"rows": rows, "calibration_repos": [], "eval_repos": [],
                "repo_overlap": [], "split_boundary": boundary}

    def _rkey(r: dict) -> str:
        return (r.get("repo") or "").casefold()

    if enforce_repo_disjoint:
        rep: dict[str, _dt.datetime] = {}  # casefolded identity -> earliest task date
        for r in rows:
            d = _norm_date(r["date"])
            cur = rep.get(_rkey(r))
            if cur is None or d < cur:
                rep[_rkey(r)] = d
        ordered = sorted(rep, key=lambda k: (rep[k], k))
        n = len(ordered)
        k = (n + 1) // 2  # older majority -> calibration; single-repo -> all calibration
        cal_set = set(ordered[:k])
        for r in rows:
            r["split"] = "calibration" if _rkey(r) in cal_set else "eval"
        boundary = {
            "rule": "repo-atomic; repos ordered by earliest task date; older half=calibration",
            "rule_granularity": "repo-earliest, task dates may straddle",
            "n_repos": n,
            "n_calibration_repos": len(cal_set),
            "n_eval_repos": n - len(cal_set),
            "boundary_date": (rep[ordered[k]].isoformat() if k < n else None),
        }
    else:
        alld = sorted(_norm_date(r["date"]) for r in rows)
        mid = alld[len(alld) // 2]
        for r in rows:
            r["split"] = "calibration" if _norm_date(r["date"]) < mid else "eval"
        boundary = {"rule": "per-task median (MUTATION: NOT repo-disjoint)",
                    "n_repos": len({_rkey(r) for r in rows}),
                    "boundary_date": mid.isoformat()}

    # overlap by casefolded identity, reported with the original-case names.
    key_splits: dict[str, set] = {}
    for r in rows:
        key_splits.setdefault(_rkey(r), set()).add(r["split"])
    overlap_keys = {k for k, s in key_splits.items() if len(s) > 1}
    cal_repos = sorted({r["repo"] for r in rows if r["split"] == "calibration"})
    eval_repos = sorted({r["repo"] for r in rows if r["split"] == "eval"})
    overlap = sorted({r["repo"] for r in rows if _rkey(r) in overlap_keys})
    return {"rows": rows, "calibration_repos": cal_repos, "eval_repos": eval_repos,
            "repo_overlap": overlap, "split_boundary": boundary}


# ===========================================================================
# ORCHESTRATION
# ===========================================================================
# PRECISE gold-leak invariant. The naive substring "test_" is a poor proxy — it appears inside
# legitimate words ("latest_", "greatest_") and public identifiers ("pytest_runtest_setup"), so
# banning it wholesale corrupts prose. The REAL leak targets are: pytest/JS node-ids (carry a
# gold file + a gold test), the FAIL_TO_PASS/PASS_TO_PASS metadata literals, patch/diff content,
# and a BARE word-boundary gold-test identifier (\btest_\w+). Those must be ZERO in every row.
_GOLDLEAK_RES = {
    "node_id": _NODEID_RE,
    "failpass_literal": _FAILPASS_RE,
    "diff_git": _DIFFGIT_RE,
    "patch_hunk": re.compile(r"@@.*?@@"),
    "bare_test_ident": re.compile(r"\btest_[A-Za-z0-9]+"),
    "test_file_name": _TEST_FILE_RE,
}
# MINED-only path rules (F1): a mined statement is a redacted commit message, so ANY surviving
# source path — slashed or bare basename — is a gold-file leak. Livelite prose keeps its own
# public path mentions verbatim by design, so these two are scanned on mined rows only.
_MINED_PATH_RES = {
    "mined_bare_src_basename": _BARE_BASENAME_RE,
    "mined_slashed_src_path": _PATH_RE,
}
_TEST_SUBSTR_RE = re.compile(r"test_")


def _leak_scan(rows: list[dict]) -> dict:
    """Scan every row's problem_statement + provenance for gold-leak patterns. ALL
    ``enforced_zero_counts`` MUST be 0 (the write gate refuses otherwise). The mined-only
    path rules run on the statement text of mined rows. ``residual_test_substring_rows`` is
    INFORMATIONAL: rows where 'test_' survives as a substring inside a larger, non-gold
    identifier (documented as intentionally preserved, not a leak)."""
    counts = {k: 0 for k in _GOLDLEAK_RES} | {k: 0 for k in _MINED_PATH_RES}
    counts["mined_specifier_path"] = 0
    residual = 0
    for r in rows:
        blob = r.get("problem_statement", "") + "\n" + json.dumps(r.get("provenance", {}))
        for k, rx in _GOLDLEAK_RES.items():
            if rx.search(blob):
                counts[k] += 1
        if r.get("source") == "mined":
            ps = r.get("problem_statement", "")
            for k, rx in _MINED_PATH_RES.items():
                if rx.search(ps):
                    counts[k] += 1
            # R2 — stoplist-aware (a kept "and/or" is intentional, not a leak).
            if any(m.group(0).lower() not in _SPECIFIER_STOP
                   for m in _SPECIFIER_PATH_RE.finditer(ps)):
                counts["mined_specifier_path"] += 1
        if _TEST_SUBSTR_RE.search(blob):
            residual += 1
    return {
        "enforced_zero_counts": counts,
        "invariant_holds": all(v == 0 for v in counts.values()),
        "residual_test_substring_rows": residual,
        "residual_note": ("'test_' preserved only as a substring of a larger non-gold "
                          "identifier (e.g. pytest_runtest_setup, _is_test_file, the glob "
                          "test_*); never a bare gold-test id or a node-id"),
    }


def _count_per_split_per_stratum(rows: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["split"], {})
        out[r["split"]][r["stratum"]] = out[r["split"]].get(r["stratum"], 0) + 1
    return {sp: dict(sorted(v.items())) for sp, v in sorted(out.items())}


def _count_per_source_per_stratum(rows: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["source"], {})
        out[r["source"]][r["stratum"]] = out[r["source"]].get(r["stratum"], 0) + 1
    return {sp: dict(sorted(v.items())) for sp, v in sorted(out.items())}


def build_corpus(livelite_path: str | None = None, history_repos: list[str] | None = None,
                 graph_db: str | None = None, max_mined: int = 0,
                 repo_alias: dict[str, str] | None = None) -> dict:
    """Build the full corpus from BOTH sources: normalize repo identities, drop livelite
    TWINS from the mined rows, assign the repo-disjoint splits, produce PROVENANCE."""
    rows: list[dict] = []
    livelite_sha: str | None = None
    gold = {"sha_to_iid": {}, "repo_pr_to_iid": {}}
    if livelite_path:
        rows += iter_livelite_rows(livelite_path, graph_db=graph_db)
        gold = _livelite_gold_index(livelite_path)
        try:
            with open(livelite_path, "rb") as fh:
                livelite_sha = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            livelite_sha = None

    mined_meta: list[dict] = []
    for repo in (history_repos or []):
        ap = os.path.abspath(repo)
        ident = _repo_identity(ap, repo_alias)
        mrows = iter_mined_rows(ap, graph_db=graph_db, max_mined=max_mined, identity=ident)
        rows += mrows
        try:
            head = _git(ap, "rev-parse", "HEAD").strip()
        except (subprocess.CalledProcessError, OSError):
            head = ""
        mined_meta.append({"path": ap.replace("\\", "/"),
                           "label": os.path.basename(ap.rstrip("/\\")),
                           "identity": ident, "head_sha": head,
                           "mined_rows": len(mrows)})
    mined_meta.sort(key=lambda m: m["path"])

    # F3 TWIN GUARD — drop any mined row that IS a livelite task: its fix commit matches a
    # livelite gold commit (commit_urls), or its squash-PR ref matches the livelite
    # pull_number on the same normalized repo.
    kept: list[dict] = []
    dropped_twins: list[dict] = []
    for r in rows:
        if r["source"] != "mined":
            kept.append(r)
            continue
        fc = r["provenance"].get("fix_commit", "")
        twin: tuple[str, str] | None = None
        for sha in sorted(gold["sha_to_iid"]):
            if _sha_match(fc, sha):
                twin = ("fix_commit_matches_livelite_gold_commit", gold["sha_to_iid"][sha])
                break
        if twin is None:
            for pr in r["provenance"].get("pr_refs", []):
                iid = gold["repo_pr_to_iid"].get(((r["repo"] or "").casefold(), pr))
                if iid:
                    twin = ("pr_ref_matches_livelite_pull_number", iid)
                    break
        if twin:
            dropped_twins.append({"task_id": r["task_id"], "repo": r["repo"],
                                  "reason": twin[0], "matched_instance_id": twin[1]})
        else:
            kept.append(r)
    rows = kept
    dropped_twins.sort(key=lambda d: d["task_id"])

    # global deterministic order (source then id) BEFORE splitting.
    rows.sort(key=lambda r: (r["source"], r["task_id"]))
    split = assign_splits(rows, enforce_repo_disjoint=True)
    rows = split["rows"]
    rows.sort(key=lambda r: (r["source"], r["task_id"]))

    provenance = {
        "purpose": "evaluation/calibration ONLY — NOT training data",
        "classification_rules_version": CLASSIFICATION_RULES_VERSION,
        "strata": list(STRATA),
        "sources": {
            "livelite": os.path.basename(livelite_path) if livelite_path else None,
            "livelite_path": (os.path.abspath(livelite_path).replace("\\", "/")
                              if livelite_path else None),
            "livelite_sha256": livelite_sha,
            "history_repos": mined_meta,
            "max_mined": max_mined,
        },
        "repo_identity_rule": ("origin remote URL owner/name; fallback --repo-alias entry, "
                               "then directory basename; casefold-compared in the split"),
        "dropped_twins": dropped_twins,
        "graph_db_used": bool(graph_db),
        "offline_degradation_note": (
            "no graph.db -> classify_stratum is correct-or-quiet: A/D degrade to B, so "
            "symbol_anchored/new_file_feature can appear ONLY when --graph-db is supplied."
            if not graph_db else "graph.db supplied: A/D resolvable"),
        "leak_law": ("no gold file paths / test names / FAIL_TO_PASS / patch content in any "
                     "row; live-lite prose verbatim except leak-token + test-file redaction; "
                     "mined statements from commit MESSAGE (never the diff) with slashed "
                     "paths AND bare source basenames redacted; livelite twins dropped"),
        "insufficient_evidence_retention": (
            "INTENTIONAL: retained as abstention-calibration data — thin statements are "
            "exactly the population abstention thresholds must be calibrated against; noise "
            "subjects (e.g. 'fix: lint', 'Fix typo') are kept and labeled, never silently "
            "filtered. A concrete-code signal (backtick/snake/camel/call token) overrides "
            "the brevity floors (F6ii)."),
        "task_facing_export_hazard": (
            "mined rows carry gold pointers OUTSIDE the task surface: provenance.fix_commit "
            "and the sha12 suffix of task_id identify the gold fix commit. Acceptable for "
            "OFFLINE calibration only. Any task-facing export MUST strip "
            "provenance.fix_commit and re-key task_id. No export mode is built (no consumer "
            "exists yet); this corpus is not agent-facing."),
        "total_rows": len(rows),
        "counts_per_source": {s: sum(1 for r in rows if r["source"] == s)
                              for s in sorted({r["source"] for r in rows})},
        "counts_per_stratum": {st: sum(1 for r in rows if r["stratum"] == st)
                               for st in STRATA},
        "counts_per_split": {sp: sum(1 for r in rows if r["split"] == sp)
                             for sp in sorted({r["split"] for r in rows})},
        "counts_per_split_per_stratum": _count_per_split_per_stratum(rows),
        "counts_per_source_per_stratum": _count_per_source_per_stratum(rows),
        "split_boundary": split["split_boundary"],
        "calibration_repos": split["calibration_repos"],
        "eval_repos": split["eval_repos"],
        "repo_overlap": split["repo_overlap"],
        "leak_scan": _leak_scan(rows),
        "min_nonspace_chars": MIN_NONSPACE_CHARS,
        "min_word_tokens": MIN_WORD_TOKENS,
    }
    return {"rows": rows, "provenance": provenance}


def write_outputs(corpus: dict, out_dir: str) -> dict:
    """Write corpus.jsonl (one row per line, sorted) + PROVENANCE.json. Byte-deterministic.

    FAIL-CLOSED (F2/R1): BOTH gates recompute from the rows ACTUALLY being written — the
    leak scan AND the repo-disjoint overlap (per-casefolded-identity split sets) — never
    from a possibly-stale provenance field. A gate that only records (or trusts a field
    computed before a post-build edit) was proven mutable-past — this one bites."""
    rows = sorted(corpus["rows"], key=lambda r: (r["source"], r["task_id"]))
    scan = _leak_scan(rows)
    if not scan["invariant_holds"]:
        raise ValueError("leak invariant violated — refusing to write: %s"
                         % json.dumps(scan["enforced_zero_counts"], sort_keys=True))
    key_splits: dict[str, set] = {}
    for r in rows:
        key_splits.setdefault((r.get("repo") or "").casefold(), set()).add(r.get("split"))
    overlap = sorted(k for k, s in key_splits.items() if len(s) > 1)
    if overlap:
        raise ValueError("repo-disjoint split violated — refusing to write: overlap=%s"
                         % overlap)
    os.makedirs(out_dir, exist_ok=True)
    cpath = os.path.join(out_dir, "corpus.jsonl")
    with open(cpath, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
    ppath = os.path.join(out_dir, "PROVENANCE.json")
    with open(ppath, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(corpus["provenance"], indent=2, sort_keys=True, ensure_ascii=False))
    return {"corpus_jsonl": cpath, "provenance_json": ppath, "rows_written": len(rows)}


# ===========================================================================
# CLI
# ===========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-livelite", help="path to a swebench_live_lite.jsonl")
    ap.add_argument("--from-history", action="append", default=[],
                    help="a local git repo to mine (repeatable)")
    ap.add_argument("--graph-db", help="optional graph.db to resolve A/D strata (offline "
                                       "without it, A/D degrade to behavioral)")
    ap.add_argument("--max-mined", type=int, default=0,
                    help="cap mined rows per repo (0 = unlimited; keeps the newest)")
    ap.add_argument("--repo-alias", action="append", default=[],
                    help="label=owner/name identity for a remoteless checkout (repeatable; "
                         "F3 fallback when 'git remote get-url origin' is unavailable)")
    ap.add_argument("--out-dir", default="D:/gt_runs/eval_corpus_20260710",
                    help="write corpus.jsonl + PROVENANCE.json here")
    ap.add_argument("--stdout", action="store_true", help="print corpus JSON, do not write")
    args = ap.parse_args()

    if not args.from_livelite and not args.from_history:
        ap.error("provide --from-livelite and/or --from-history")

    alias: dict[str, str] = {}
    for spec in args.repo_alias:
        if "=" in spec:
            k, v = spec.split("=", 1)
            alias[k.strip()] = v.strip()

    corpus = build_corpus(livelite_path=args.from_livelite, history_repos=args.from_history,
                          graph_db=args.graph_db, max_mined=args.max_mined,
                          repo_alias=alias or None)
    if args.stdout:
        print(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False))
        return
    res = write_outputs(corpus, args.out_dir)
    prov = corpus["provenance"]
    print(f"[WROTE] {res['corpus_jsonl']}  ({res['rows_written']} rows)")
    print(f"[WROTE] {res['provenance_json']}")
    print(f"per-source: {prov['counts_per_source']}")
    print(f"per-stratum: {prov['counts_per_stratum']}")
    print(f"per-split: {prov['counts_per_split']}")
    print(f"split boundary: {prov['split_boundary']}")
    print(f"dropped twins: {len(prov['dropped_twins'])} "
          f"{[d['task_id'] for d in prov['dropped_twins']]}")


if __name__ == "__main__":
    main()

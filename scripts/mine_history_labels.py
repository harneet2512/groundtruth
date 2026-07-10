#!/usr/bin/env python3
"""Mine git history into FREE evaluation labels for GT's edit-intelligence engines.

Merged commits are ground truth. This tool reads a repo's own history and emits three
families of labels — WITHOUT any agent run — that measure the precision/recall of the
static edit-intelligence engines (``runtime/patch_delta``: signature-mismatch +
companion advisories; ``change_surface``: sibling-role holes; the syntax floor):

  1. SIGNATURE labels — a Python function/method whose POSITIONAL signature changed in
     a commit; the label is the OTHER files changed in the SAME commit that contain a
     call site of that symbol (the caller-fix ground truth). Parsed with the SAME
     conservative ``ast`` engine as ``patch_delta`` (imported, not re-implemented).
  2. COMPANION labels — a commit that ADDS a new file into an existing sibling family
     (>=2 same-dir/same-ext siblings at the parent commit); the label is the other
     files changed in the same commit (registry / config / test companions).
  3. SYNTAX labels — a file whose PARENT version fails ``ast.parse`` and whose CHILD
     version parses clean (a broken->fixed pair for the syntax floor).

NOISE CONTROLS (label honesty, never invented determinism):
  * merge commits skipped; commits touching > ``MAX_FILES_MULTI_INTENT`` files skipped
    (multi-intent); vendored paths filtered everywhere and test/demo paths filtered
    PER FAMILY via ``groundtruth.delivery.path_policy`` — a signature label's edited
    file, a companion label's added file, and a syntax label's file must all be
    non-test source (test-file caller-fixes are still recorded, but split into
    ``test_callers_fixed`` / ``test_companions``, never mixed into the source truth);
  * every label row carries a ``confidence`` field — ``clean_single_intent`` vs
    ``multi_file_commit`` — and the commit's file count;
  * ALL outputs are sorted; the ONLY timestamps recorded are deterministic git commit
    dates (provenance), never a wall-clock run time.

A ``--score patch_delta`` mode replays SIGNATURE labels through
``patch_delta.analyze_patch_delta`` (``GT_PATCH_DELTA=1``, before/after content from
git, the caller files materialized in their PARENT/unfixed state via ``git worktree``)
and reports precision/recall at 8dp over CALLER-FILE PAIRS. TWO truth bases are
reported: "as-designed" (every scored positive label) and "engine-contract"
(WIDENING labels excluded — when the new positional range CONTAINS the old one,
``valid_before AND NOT valid_after`` is unsatisfiable, so the engine's silence is its
contract, not a miss; containment survives the method call-site self-shift and the
``max(0, .)`` clamp in ``patch_delta._variant``). SILENCE-truth labels (signature
changed, NO non-test caller fixed) are replayed too — any engine prediction there is
a FP — yielding a FULL-POPULATION precision (deterministic stride sample when the
silence population exceeds ``--silence-sample``; the rule is recorded in PROVENANCE).

``__init__`` caller-truth is CLASS-AWARE: attribute matching of ``x.__init__()`` is
class-blind (every class defines one), which fabricated false caller-fix truth
(proven on beets 0c227e94). Of the review's two options, (a) class-name resolution
was chosen over (b) dropping ``__init__`` labels: the single-def-per-file gate makes
the owning class unambiguous, and (b) would discard all constructor-arity ground
truth. A caller-fix for ``C.__init__`` counts ONLY when the file constructs
``C(...)``, references ``C.__init__`` explicitly, or defines a subclass of ``C`` that
calls ``super().__init__``.

CLI QUIRK: pass ``--rev=--all`` (with the equals sign) — argparse eats a bare
``--all`` as an unknown flag value.

LLM-free, deterministic, stdlib + git subprocess only (the optional score mode also
shells the deterministic ``gt-index`` binary and reads ``graph.db`` via ``sqlite3``).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# --- bootstrap: make the in-repo groundtruth package importable when run directly ---
_HERE = Path(__file__).resolve()
_SRC = _HERE.parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from groundtruth.delivery.path_policy import is_test_or_demo, is_vendored_path  # noqa: E402
from groundtruth.runtime.patch_delta import (  # noqa: E402
    _py_defs,
    _py_signature,
    analyze_patch_delta,
)

# ---------------------------------------------------------------------------
# Constants / noise-control knobs
# ---------------------------------------------------------------------------
MAX_FILES_MULTI_INTENT = 20   # a commit touching more files is multi-intent -> skip
CLEAN_SINGLE_INTENT_MAX = 5   # <= this many changed files => a clean single-intent commit
MIN_SIBLINGS = 2              # a family needs >=2 pre-existing siblings
_PY_EXTS = (".py", ".pyi")
_GIT_TIMEOUT = 60


def too_many_files(n_files: int) -> bool:
    """Multi-intent gate: a commit touching more than ``MAX_FILES_MULTI_INTENT`` files
    mixes intents and its per-symbol labels are unreliable -> skip the whole commit."""
    return n_files > MAX_FILES_MULTI_INTENT


def commit_confidence(n_files: int) -> str:
    """Per-label confidence class from the commit's total changed-file count."""
    return "clean_single_intent" if n_files <= CLEAN_SINGLE_INTENT_MAX else "multi_file_commit"


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class MineResult:
    signature: list[dict] = field(default_factory=list)
    companion: list[dict] = field(default_factory=list)
    syntax: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    head_sha: str = ""
    date_range: tuple[str, str] = ("", "")


# ---------------------------------------------------------------------------
# git plumbing (utf-8, deterministic, never prompts)
# ---------------------------------------------------------------------------
def _git(repo: str, *args: str, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout, check=False,
    )


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").strip().lstrip("./")


def _dirname(path: str) -> str:
    p = _norm(path)
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _ext(path: str) -> str:
    base = _norm(path).rsplit("/", 1)[-1]
    return base[base.rfind("."):].lower() if "." in base else ""


class _BatchCat:
    """One persistent ``git cat-file --batch`` process that streams many blobs.

    Per-blob ``git show`` spawns a subprocess for EVERY file version — thousands of
    spawns on a big repo, unusably slow on Windows. ``cat-file --batch`` reads
    ``<rev>:<path>`` requests on stdin and writes ``<oid> <type> <size>\\n<bytes>\\n``,
    so the whole mine costs ONE process instead of O(blobs). Any protocol fault falls
    back to ``git show`` (correct, just slower)."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, bufsize=0,
        )

    def read(self, sha: str, path: str) -> "tuple[str, str | None]":
        """Returns (``"ok"``, content) | (``"missing"``, None) | (``"error"``, None).
        Only ``"error"`` (a protocol fault) should trigger a one-shot fallback."""
        spec = f"{sha}:{path}"
        if "\n" in spec or "\r" in spec:
            return ("error", None)  # a newline in a path would desync the protocol
        p = self.proc
        if p.stdin is None or p.stdout is None:
            return ("error", None)
        try:
            p.stdin.write((spec + "\n").encode("utf-8"))
            p.stdin.flush()
            header = p.stdout.readline().decode("utf-8", "replace").strip()
            if not header:
                return ("error", None)
            if header.endswith(("missing", "ambiguous")):
                return ("missing", None)
            size = int(header.rsplit(" ", 1)[-1])
            buf = bytearray()
            while len(buf) < size:
                chunk = p.stdout.read(size - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
            if len(buf) < size:
                # F6: mid-blob EOF — a truncated stream must NEVER read as a
                # complete blob (a partial file would silently poison every
                # downstream parse). Error -> the caller falls back to git show.
                return ("error", None)
            p.stdout.read(1)  # trailing newline
            return ("ok", buf.decode("utf-8", "replace"))
        except (OSError, ValueError, BrokenPipeError):
            return ("error", None)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass


def _blob(repo: str, sha: str, path: str, cache: dict) -> "str | None":
    """Content of ``path`` at ``sha``, or None if absent. Uses a per-repo persistent
    ``git cat-file --batch`` reader (created lazily, stored in ``cache``); falls back to
    a one-shot ``git show`` on any batch fault."""
    key = (sha, path)
    if key in cache:
        return cache[key]
    batch = cache.get(("_batch_", repo))
    if batch is None:
        batch = _BatchCat(repo)
        cache[("_batch_", repo)] = batch
    status, content = batch.read(sha, path)
    if status == "ok":
        val: "str | None" = content
    elif status == "missing":
        val = None
    else:  # protocol error -> one-shot fallback (correct, just slower)
        proc = _git(repo, "show", f"{sha}:{path}")
        val = proc.stdout if proc.returncode == 0 else None
    cache[key] = val
    return val


def _close_blob_cache(cache: dict) -> None:
    for k in [k for k in cache if isinstance(k, tuple) and len(k) == 2 and k[0] == "_batch_"]:
        b = cache.pop(k)
        if isinstance(b, _BatchCat):
            b.close()


def _ls_tree_dir(repo: str, sha: str, directory: str, cache: dict) -> list[str]:
    """Immediate child paths of ``directory`` at ``sha`` (non-recursive)."""
    key = (sha, directory)
    if key in cache:
        return cache[key]
    pathspec = f"{directory}/" if directory else ""
    args = ["ls-tree", "--name-only", sha]
    if pathspec:
        args += ["--", pathspec]
    proc = _git(repo, *args)
    out = [_norm(ln) for ln in proc.stdout.splitlines() if ln.strip()] if proc.returncode == 0 else []
    cache[key] = out
    return out


@dataclass(frozen=True)
class _Commit:
    sha: str
    parents: tuple[str, ...]
    date: str
    subject: str
    files: tuple[tuple[str, str, str], ...]  # (status, path, old_path_for_rename)


# A TEXT marker (never a literal NUL in the argv — Windows CreateProcess rejects an
# embedded NUL in an argument). Git's ``%x1f`` escape is literal text in the arg and
# only expands to the 0x1f byte in the OUTPUT, which is what we split on.
_MARK = "__GT_COMMIT__"
_US = "\x1f"


def iter_commits(repo: str, rev_tokens: list[str], max_commits: int) -> list[_Commit]:
    """Parse ``git log`` (name-status) into commits, newest first, merges dropped."""
    proc = _git(
        repo, "-c", "core.quotepath=false", "log", *rev_tokens,
        "--no-merges", f"-n{max_commits}", "--name-status",
        f"--pretty=format:{_MARK}%H%x1f%P%x1f%cI%x1f%s",
        timeout=180,
    )
    if proc.returncode != 0:
        return []
    commits: list[_Commit] = []
    cur: "dict | None" = None
    for raw in proc.stdout.splitlines():
        if raw.startswith(_MARK):
            if cur is not None:
                commits.append(_finish(cur))
            payload = raw[len(_MARK):]
            parts = payload.split(_US)
            while len(parts) < 4:
                parts.append("")
            sha, parents, date, subject = parts[0], parts[1], parts[2], parts[3]
            cur = {"sha": sha, "parents": parents.split() if parents else [],
                   "date": date, "subject": subject, "files": []}
            continue
        if cur is None or not raw.strip():
            continue
        cols = raw.split("\t")
        status = cols[0].strip()
        if status[:1] == "R" and len(cols) >= 3:      # rename: R<score>\told\tnew
            cur["files"].append(("R", _norm(cols[2]), _norm(cols[1])))
        elif status[:1] == "C" and len(cols) >= 3:    # copy: C<score>\tsrc\tnew
            cur["files"].append(("C", _norm(cols[2]), _norm(cols[1])))
        elif len(cols) >= 2:
            cur["files"].append((status[:1], _norm(cols[1]), ""))
    if cur is not None:
        commits.append(_finish(cur))
    return commits


def _finish(d: dict) -> _Commit:
    return _Commit(
        sha=d["sha"], parents=tuple(d["parents"]), date=d["date"],
        subject=d["subject"], files=tuple(d["files"]),
    )


# ---------------------------------------------------------------------------
# call-site detection (AST first, conservative regex fallback)
# ---------------------------------------------------------------------------
def _calls_symbol(content: str, symbol: str) -> bool:
    """True iff ``content`` contains a CALL to ``symbol`` (``symbol(...)`` or
    ``x.symbol(...)``). AST-based; falls back to a word-boundary ``symbol(`` regex only
    when the file is not parseable by the host Python."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return bool(re.search(r"(?<![\w.])" + re.escape(symbol) + r"\s*\(", content)) or bool(
            re.search(r"\." + re.escape(symbol) + r"\s*\(", content))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == symbol:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == symbol:
                return True
    return False


def is_widening(old_bounds: "list[int]", new_bounds: "list[int]") -> bool:
    """F1: True iff the NEW positional range CONTAINS the old one. On such a label
    ``patch_delta``'s firing condition (``valid_before AND NOT valid_after``) is
    unsatisfiable — every arity valid before stays valid after — so the engine's
    silence is its CONTRACT. Containment is preserved under the method call-site
    self-shift (uniform -1 on both ends) and the ``max(0, .)`` clamp, so the
    def-level stored bounds are sufficient."""
    return new_bounds[0] <= old_bounds[0] and new_bounds[1] >= old_bounds[1]


def _init_class_names(content: str) -> "list[str]":
    """Names of classes whose body (including ``if``/``try`` wrappers at class level)
    defines ``__init__``. Mirrors ``_py_defs``'s context walk; used to resolve the
    owning class of a mined ``__init__`` label (F2)."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    names: list[str] = []

    def _walk(node, cls: "str | None") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name == "__init__" and cls is not None:
                    names.append(cls)
                _walk(child, None)  # a def nested in a function leaves class context
            elif isinstance(child, ast.ClassDef):
                _walk(child, child.name)
            else:
                _walk(child, cls)

    _walk(tree, None)
    return names


def _calls_init_of_class(content: str, class_name: str) -> bool:
    """F2: class-aware ``__init__`` caller test. ``x.__init__()`` attribute matching
    is class-blind (every class has an ``__init__``), so a caller-fix for
    ``C.__init__`` counts ONLY on one of three class-anchored shapes:
      (1) a constructor call ``C(...)`` / ``mod.C(...)``,
      (2) an explicit ``C.__init__(...)`` / ``mod.C.__init__(...)`` reference,
      (3) a class SUBCLASSING ``C`` (``class X(C)`` / ``class X(mod.C)``) that calls
          ``super().__init__`` anywhere in its body.
    Unparseable content degrades to a conservative ``C(`` regex (shape 1 only)."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return bool(re.search(r"(?<![\w.])" + re.escape(class_name) + r"\s*\(", content))

    def _is_class_ref(node) -> bool:
        return (isinstance(node, ast.Name) and node.id == class_name) or (
            isinstance(node, ast.Attribute) and node.attr == class_name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if _is_class_ref(fn):
                return True  # (1) constructor form
            if isinstance(fn, ast.Attribute) and fn.attr == "__init__" and _is_class_ref(fn.value):
                return True  # (2) explicit C.__init__
        elif isinstance(node, ast.ClassDef):
            if any(_is_class_ref(b) for b in node.bases):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "__init__"):
                        return True  # (3) subclass super().__init__
    return False


def _syntax_error_class(content: str) -> "str | None":
    """The SyntaxError subclass name if ``content`` fails to parse for a SYNTAX reason,
    ``"OTHER"`` for a non-syntax parse failure (null bytes / recursion), else None."""
    try:
        ast.parse(content)
        return None
    except SyntaxError as exc:
        return type(exc).__name__
    except (ValueError, RecursionError, MemoryError):
        return "OTHER"


def _parses_clean(content: str) -> bool:
    try:
        ast.parse(content)
        return True
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False


# ---------------------------------------------------------------------------
# per-commit miners
# ---------------------------------------------------------------------------
def _before_path(status: str, path: str, old: str) -> str:
    return old if (status in ("R", "C") and old) else path


def _sig_labels(repo_name: str, repo: str, c: _Commit, blob_cache: dict, stats: dict) -> list[dict]:
    conf = commit_confidence(len(c.files))
    parent = c.sha + "^"
    # (a) modified python SOURCE files whose prior signature could break
    py_mods: list[tuple[str, str, str]] = []
    for status, path, old in c.files:
        if not path.endswith(_PY_EXTS):
            continue
        if status in ("A", "D"):
            continue
        if is_vendored_path(path):
            stats["vendored_paths_skipped"] += 1
            continue
        if is_test_or_demo(path):
            stats["test_paths_skipped"] += 1
            continue
        before = _blob(repo, parent, _before_path(status, path, old), blob_cache)
        after = _blob(repo, c.sha, path, blob_cache)
        if before is None or after is None:
            continue
        py_mods.append((path, before, after))
    if not py_mods:
        return []
    # every changed python file (incl tests) is a candidate caller-fix surface
    other_after: dict[str, str] = {}
    for status, path, _old in c.files:
        if status == "D" or not path.endswith(_PY_EXTS):
            continue
        after = _blob(repo, c.sha, path, blob_cache)
        if after is not None:
            other_after[path] = after

    out: list[dict] = []
    for path, before, after in py_mods:
        bdefs = _py_defs(before)
        adefs = _py_defs(after)
        if bdefs is None or adefs is None:
            stats["unparseable_py_skipped"] += 1
            continue
        for name in sorted(set(bdefs) & set(adefs)):
            b_nodes, a_nodes = bdefs[name], adefs[name]
            if len(b_nodes) != 1 or len(a_nodes) != 1:
                continue  # duplicate/overloaded name -> ambiguous -> abstain
            b_fn, b_in_class = b_nodes[0]
            a_fn, a_in_class = a_nodes[0]
            old_sig = _py_signature(b_fn, b_in_class)
            new_sig = _py_signature(a_fn, a_in_class)
            if old_sig is None or new_sig is None:
                continue
            if old_sig.is_method != new_sig.is_method:
                continue
            if (old_sig.min_pos, old_sig.max_pos) == (new_sig.min_pos, new_sig.max_pos):
                continue  # positional bounds unchanged -> nothing to break
            # F2: ``__init__`` caller-truth must be CLASS-AWARE — resolve the owning
            # class (unambiguous under the single-def gate) and require a
            # class-anchored caller shape; a bare ``x.__init__()`` match is
            # class-blind and fabricates false truth.
            class_name = ""
            if name == "__init__":
                classes = _init_class_names(after)
                if len(classes) != 1:
                    stats["init_class_ambiguous_skipped"] += 1
                    continue  # owning class unprovable -> no honest truth
                class_name = classes[0]
            callers: list[str] = []
            test_callers: list[str] = []
            for opath, ocontent in other_after.items():
                if opath == path:
                    continue
                if class_name:
                    if not _calls_init_of_class(ocontent, class_name):
                        continue
                elif not _calls_symbol(ocontent, name):
                    continue
                if is_vendored_path(opath):
                    continue
                if is_test_or_demo(opath):
                    test_callers.append(opath)
                else:
                    callers.append(opath)
            old_bounds = [old_sig.min_pos, old_sig.max_pos]
            new_bounds = [new_sig.min_pos, new_sig.max_pos]
            out.append({
                "repo": repo_name, "sha": c.sha, "date": c.date,
                "symbol": name, "file": path,
                "class_name": class_name,
                "is_method": bool(old_sig.is_method),
                "old_bounds": old_bounds,
                "new_bounds": new_bounds,
                "is_widening": is_widening(old_bounds, new_bounds),
                "callers_fixed": sorted(callers),
                "test_callers_fixed": sorted(test_callers),
                "confidence": conf, "n_files_in_commit": len(c.files),
            })
    return out


def _companion_labels(
    repo_name: str, repo: str, c: _Commit, ls_cache: dict, stats: dict
) -> list[dict]:
    conf = commit_confidence(len(c.files))
    parent = c.sha + "^"
    changed_paths = [p for _s, p, _o in c.files]
    out: list[dict] = []
    for status, path, _old in c.files:
        if status != "A":
            continue
        if is_vendored_path(path):
            continue
        # F5: a new TEST/demo-dir file is never a companion label (the engines this
        # family gates — change_surface / VerificationPlan — advise on SOURCE adds).
        if is_test_or_demo(path):
            stats["companion_test_new_file_skipped"] += 1
            continue
        ext = _ext(path)
        if ext not in _PY_EXTS:
            continue  # sibling families are keyed on python source
        directory = _dirname(path)
        base = _norm(path).rsplit("/", 1)[-1]
        siblings = [
            child.rsplit("/", 1)[-1]
            for child in _ls_tree_dir(repo, parent, directory, ls_cache)
            if _dirname(child) == directory and _ext(child) == ext
            and child.rsplit("/", 1)[-1] != base
        ]
        if len(siblings) < MIN_SIBLINGS:
            continue
        others = sorted(p for p in changed_paths if p != path)
        if not others:
            continue  # no other file changed -> no companion label to learn from
        # F5: split the truth into source vs test buckets (a test companion is real
        # signal for VerificationPlan but must never mix into the source truth).
        companions = [p for p in others if not is_test_or_demo(p)]
        test_companions = [p for p in others if is_test_or_demo(p)]
        out.append({
            "repo": repo_name, "sha": c.sha, "date": c.date,
            "entity": directory or ".", "new_file": path,
            "siblings": sorted(siblings), "companions": companions,
            "test_companions": test_companions,
            "confidence": conf, "n_files_in_commit": len(c.files),
        })
    return out


def _syntax_labels(
    repo_name: str, repo: str, c: _Commit, blob_cache: dict, stats: dict
) -> list[dict]:
    conf = commit_confidence(len(c.files))
    parent = c.sha + "^"
    out: list[dict] = []
    for status, path, old in c.files:
        if not path.endswith(_PY_EXTS) or status in ("A", "D"):
            continue
        if is_vendored_path(path):
            continue
        # F5: the syntax floor guards agent edits to SOURCE; a broken->fixed pair in
        # a test/demo path is skipped and counted, never a label.
        if is_test_or_demo(path):
            stats["syntax_test_paths_skipped"] += 1
            continue
        before = _blob(repo, parent, _before_path(status, path, old), blob_cache)
        after = _blob(repo, c.sha, path, blob_cache)
        if before is None or after is None:
            continue
        err = _syntax_error_class(before)
        if err is None or err == "OTHER":
            continue  # parent already parsed (or a non-syntax failure) -> not a fix pair
        if not _parses_clean(after):
            continue  # child still broken -> not fixed here
        out.append({
            "repo": repo_name, "sha": c.sha, "date": c.date,
            "file": path, "error_class": err,
            "confidence": conf, "n_files_in_commit": len(c.files),
        })
    return out


# ---------------------------------------------------------------------------
# repo-level orchestration
# ---------------------------------------------------------------------------
def mine_repo(repo_name: str, repo_path: str, rev: str = "HEAD", max_commits: int = 5000) -> MineResult:
    """Mine one repo's history into the three label families + noise-control stats."""
    stats = {
        "commits_examined": 0,
        "merge_commits_skipped": 0,
        "multi_intent_skipped": 0,
        "vendored_paths_skipped": 0,
        "test_paths_skipped": 0,
        "unparseable_py_skipped": 0,
        "init_class_ambiguous_skipped": 0,
        "syntax_test_paths_skipped": 0,
        "companion_test_new_file_skipped": 0,
        "confidence_breakdown": {
            fam: {"clean_single_intent": 0, "multi_file_commit": 0}
            for fam in ("signature", "companion", "syntax")
        },
    }
    rev_tokens = rev.split() if rev else ["HEAD"]
    commits = iter_commits(repo_path, rev_tokens, max_commits)
    blob_cache: dict = {}
    ls_cache: dict = {}
    sig_rows: list[dict] = []
    comp_rows: list[dict] = []
    syn_rows: list[dict] = []
    dates: list[str] = []
    head_sha = commits[0].sha if commits else ""
    for c in commits:
        dates.append(c.date)
        if len(c.parents) > 1:  # belt-and-braces (git log --no-merges already drops these)
            stats["merge_commits_skipped"] += 1
            continue
        if too_many_files(len(c.files)):
            stats["multi_intent_skipped"] += 1
            continue
        stats["commits_examined"] += 1
        s = _sig_labels(repo_name, repo_path, c, blob_cache, stats)
        m = _companion_labels(repo_name, repo_path, c, ls_cache, stats)
        y = _syntax_labels(repo_name, repo_path, c, blob_cache, stats)
        for row in s:
            stats["confidence_breakdown"]["signature"][row["confidence"]] += 1
        for row in m:
            stats["confidence_breakdown"]["companion"][row["confidence"]] += 1
        for row in y:
            stats["confidence_breakdown"]["syntax"][row["confidence"]] += 1
        sig_rows.extend(s)
        comp_rows.extend(m)
        syn_rows.extend(y)
    _close_blob_cache(blob_cache)

    sig_rows.sort(key=lambda r: (r["repo"], r["sha"], r["file"], r["symbol"]))
    comp_rows.sort(key=lambda r: (r["repo"], r["sha"], r["new_file"]))
    syn_rows.sort(key=lambda r: (r["repo"], r["sha"], r["file"]))
    date_range = (min(dates), max(dates)) if dates else ("", "")
    return MineResult(
        signature=sig_rows, companion=comp_rows, syntax=syn_rows,
        stats=stats, head_sha=head_sha, date_range=date_range,
    )


# ---------------------------------------------------------------------------
# --score patch_delta : replay SIGNATURE labels through the real engine
# ---------------------------------------------------------------------------
def _build_graph_at(repo: str, sha: str, worktree: str, gt_index: str) -> "str | None":
    """Build a graph.db over ``worktree`` (checked out at ``sha``) via the gt-index
    binary. Returns the db path or None on any failure."""
    db = os.path.join(worktree, ".gt_history_graph.db")
    try:
        proc = subprocess.run(
            [gt_index, "-root", worktree, "-output", db],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=600, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return db if (proc.returncode == 0 and os.path.isfile(db)) else None


def _pr8(tp: int, fp: int, fn: int) -> "tuple[float, float]":
    p = round(tp / (tp + fp), 8) if (tp + fp) > 0 else 0.0
    r = round(tp / (tp + fn), 8) if (tp + fn) > 0 else 0.0
    return p, r


def score_patch_delta(
    labels, repo_path, *, graph_db=None, gt_index=None, limit=25, silence_limit=30
) -> dict:
    """Replay SIGNATURE labels through ``analyze_patch_delta``; TP/FP/FN + 8dp P/R
    over CALLER-FILE PAIRS, on TWO truth bases plus the full population.

    * POSITIVE labels (>=1 non-test caller-fix): a predicted caller-fixed file = TP,
      a predicted untouched file = FP, silence on a caller-fixed file = FN.
    * F1 — "as-designed" counts every scored positive; "engine-contract" excludes
      WIDENING labels (see ``is_widening``: the engine's firing condition is
      unsatisfiable there, so its silence is the contract, not a miss).
    * F3 — SILENCE labels (empty non-test truth) are replayed too: any prediction
      is a FP. When the silence population exceeds ``silence_limit`` a deterministic
      stride sample over the sorted labels is scored; the rule is reported.

    Caller files are materialized in their PARENT (unfixed) state via a detached
    ``git worktree`` at ``sha^`` (one worktree+graph per unique sha, reused across
    that commit's labels). ``graph_db`` pins an aligned graph (test fixtures);
    ``gt_index`` builds a fresh graph at the parent. A label is scored only when a
    graph is available (skips are counted, never guessed).
    """
    positives = [row for row in labels if row.get("callers_fixed")]
    silence_all = [row for row in labels if not row.get("callers_fixed")]
    positives_capped = positives[:limit]
    if silence_limit and len(silence_all) > silence_limit:
        stride = -(-len(silence_all) // silence_limit)  # ceil
        silence_sel = [silence_all[i] for i in range(0, len(silence_all), stride)][:silence_limit]
        sampling = (f"deterministic stride sample: every {stride}th of "
                    f"{len(silence_all)} sorted silence labels -> {len(silence_sel)}")
    else:
        silence_sel = list(silence_all)
        sampling = f"all {len(silence_all)} silence labels"

    work = [(row, "positive") for row in positives_capped] + [
        (row, "silence") for row in silence_sel]
    # group by sha so one worktree+graph serves every label of that commit
    work.sort(key=lambda t: (t[0]["sha"], t[0]["file"], t[0]["symbol"], t[1]))

    a_tp = a_fp = a_fn = 0          # positives, as-designed
    e_tp = e_fp = e_fn = 0          # positives, engine-contract (non-widening)
    sil_fp_as = sil_fp_ec = 0       # silence FPs per base
    pos_scored = sil_scored = 0
    pos_skipped = sil_skipped = 0
    ec_excluded = sil_ec_excluded = 0
    details: list[dict] = []

    prev_flag = os.environ.get("GT_PATCH_DELTA")
    os.environ["GT_PATCH_DELTA"] = "1"
    blob_cache: dict = {}
    cur_sha: "str | None" = None
    cur_wt: "str | None" = None
    cur_db: "str | None" = None

    def _teardown() -> None:
        nonlocal cur_wt
        if cur_wt:
            _git(repo_path, "worktree", "remove", "--force", cur_wt)
            _safe_rmtree(cur_wt)
            cur_wt = None

    try:
        for row, kind in work:
            sha, path, symbol = row["sha"], row["file"], row["symbol"]
            truth = {_norm(p) for p in (row.get("callers_fixed") or [])}
            widening = row.get("is_widening")
            if widening is None:  # legacy rows: derive from the stored bounds
                widening = is_widening(row["old_bounds"], row["new_bounds"])
            before = _blob(repo_path, sha + "^", path, blob_cache)
            after = _blob(repo_path, sha, path, blob_cache)
            if before is None or after is None:
                if kind == "positive":
                    pos_skipped += 1
                else:
                    sil_skipped += 1
                continue
            if sha != cur_sha:
                _teardown()
                cur_sha, cur_db = sha, None
                wt = tempfile.mkdtemp(prefix="gt_hist_wt_")
                add = _git(repo_path, "worktree", "add", "--detach", "-f", wt,
                           sha + "^", timeout=180)
                if add.returncode == 0:
                    cur_wt = wt
                    cur_db = graph_db
                    if cur_db is None and gt_index:
                        cur_db = _build_graph_at(repo_path, sha + "^", wt, gt_index)
                else:
                    _safe_rmtree(wt)
            if not cur_wt or not cur_db or not os.path.isfile(cur_db):
                if kind == "positive":
                    pos_skipped += 1
                else:
                    sil_skipped += 1
                continue
            res = analyze_patch_delta({path: (before, after)}, cur_wt, cur_db)
            predicted = {
                _norm(mm.caller_file)
                for mm in res.signature_mismatches if mm.symbol == symbol
            }
            l_tp = len(predicted & truth)
            l_fp = len(predicted - truth)
            l_fn = len(truth - predicted)
            if kind == "positive":
                pos_scored += 1
                a_tp += l_tp
                a_fp += l_fp
                a_fn += l_fn
                if widening:
                    ec_excluded += 1
                else:
                    e_tp += l_tp
                    e_fp += l_fp
                    e_fn += l_fn
            else:
                sil_scored += 1
                sil_fp_as += l_fp
                if widening:
                    sil_ec_excluded += 1
                else:
                    sil_fp_ec += l_fp
            details.append({"sha": sha, "symbol": symbol, "file": path,
                            "kind": kind, "is_widening": bool(widening),
                            "predicted": sorted(predicted), "truth": sorted(truth),
                            "tp": l_tp, "fp": l_fp, "fn": l_fn})
    finally:
        _teardown()
        _close_blob_cache(blob_cache)
        if prev_flag is None:
            os.environ.pop("GT_PATCH_DELTA", None)
        else:
            os.environ["GT_PATCH_DELTA"] = prev_flag

    a_p, a_r = _pr8(a_tp, a_fp, a_fn)
    e_p, e_r = _pr8(e_tp, e_fp, e_fn)
    full_as_den = a_tp + a_fp + sil_fp_as
    full_ec_den = e_tp + e_fp + sil_fp_ec
    return {
        "engine": "patch_delta",
        "unit": "caller-file pairs",
        "scorable_labels": len(positives),
        "n_scored": pos_scored,
        "skipped_no_graph": pos_skipped,
        "tp": a_tp, "fp": a_fp, "fn": a_fn,          # as-designed (compat keys)
        "precision": a_p, "recall": a_r,
        "as_designed": {"tp": a_tp, "fp": a_fp, "fn": a_fn,
                        "precision": a_p, "recall": a_r, "n_scored": pos_scored},
        "engine_contract": {"tp": e_tp, "fp": e_fp, "fn": e_fn,
                            "precision": e_p, "recall": e_r,
                            "n_scored": pos_scored - ec_excluded,
                            "excluded_widening": ec_excluded},
        "full_population": {
            "silence_population": len(silence_all),
            "silence_scored": sil_scored,
            "silence_skipped_no_graph": sil_skipped,
            "silence_sampling": sampling,
            "silence_fp_as_designed": sil_fp_as,
            "silence_fp_engine_contract": sil_fp_ec,
            "silence_excluded_widening": sil_ec_excluded,
            "as_designed_precision": round(a_tp / full_as_den, 8) if full_as_den else 0.0,
            "engine_contract_precision": round(e_tp / full_ec_den, 8) if full_ec_den else 0.0,
        },
        "details": details,
    }


def _safe_rmtree(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=True))
            fh.write("\n")


def _sha256_of(path: str) -> "str | None":
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mine git history into GT evaluation labels.",
        epilog="NOTE: to mine all refs pass --rev=--all (with '='); "
               "argparse eats a bare --all.")
    ap.add_argument("--repo", action="append", default=[],
                    help="repo path (repeatable); name = basename. Default: current dir.")
    ap.add_argument("--rev", default="HEAD",
                    help="git revision/range/flags (e.g. HEAD, or --rev=--all).")
    ap.add_argument("--max-commits", type=int, default=5000)
    ap.add_argument("--out", default=str(Path("D:/gt_runs/history_labels_20260710")))
    ap.add_argument("--score", choices=["patch_delta"], default=None)
    ap.add_argument("--graph-db", default=None, help="pinned graph.db for --score (aligned to sha^).")
    ap.add_argument("--gt-index", default=None, help="gt-index binary to build a graph per parent.")
    ap.add_argument("--score-limit", type=int, default=25)
    ap.add_argument("--silence-sample", type=int, default=30,
                    help="max SILENCE labels replayed per repo for full-population "
                         "precision (deterministic stride sample; 0 = all).")
    args = ap.parse_args(argv)

    repos = args.repo or ["."]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_sig: list[dict] = []
    all_comp: list[dict] = []
    all_syn: list[dict] = []
    prov_repos: list[dict] = []
    agg_stats: dict = {}
    per_repo_results: list[tuple[str, str, MineResult]] = []

    for repo in repos:
        repo_path = str(Path(repo).resolve())
        repo_name = Path(repo_path).name or "repo"
        res = mine_repo(repo_name, repo_path, rev=args.rev, max_commits=args.max_commits)
        per_repo_results.append((repo_name, repo_path, res))
        all_sig.extend(res.signature)
        all_comp.extend(res.companion)
        all_syn.extend(res.syntax)
        prov_repos.append({
            "name": repo_name, "path": repo_path, "rev": args.rev,
            # F4: this is the newest commit REACHED BY --rev (e.g. --all), which is
            # not necessarily the checkout HEAD — named accordingly.
            "newest_mined_sha": res.head_sha, "date_range": list(res.date_range),
            "commits_examined": res.stats["commits_examined"],
            "stats": res.stats,
        })
        _merge_stats(agg_stats, res.stats)

    all_sig.sort(key=lambda r: (r["repo"], r["sha"], r["file"], r["symbol"]))
    all_comp.sort(key=lambda r: (r["repo"], r["sha"], r["new_file"]))
    all_syn.sort(key=lambda r: (r["repo"], r["sha"], r["file"]))

    _write_jsonl(out_dir / "signature_labels.jsonl", all_sig)
    _write_jsonl(out_dir / "companion_labels.jsonl", all_comp)
    _write_jsonl(out_dir / "syntax_labels.jsonl", all_syn)

    counts = {"signature": len(all_sig), "companion": len(all_comp), "syntax": len(all_syn)}
    small_n = {fam: (n < 10) for fam, n in counts.items()}

    score_block = None
    if args.score == "patch_delta":
        if len(all_sig) < 10:
            score_block = {"engine": "patch_delta", "status": "small_n",
                           "signature_labels": len(all_sig),
                           "note": "fewer than 10 signature labels; scoring not statistically meaningful"}
        elif not (args.graph_db or args.gt_index):
            score_block = {"engine": "patch_delta", "status": "no_graph_source",
                           "signature_labels": len(all_sig),
                           "note": "pass --graph-db (aligned) or --gt-index to score"}
        else:
            # score each repo's own labels against that repo (worktree at parent,
            # one worktree+graph per unique sha)
            merged = {"engine": "patch_delta", "unit": "caller-file pairs",
                      "scorable_labels": 0, "n_scored": 0, "skipped_no_graph": 0,
                      "tp": 0, "fp": 0, "fn": 0,
                      "engine_contract": {"tp": 0, "fp": 0, "fn": 0,
                                          "n_scored": 0, "excluded_widening": 0},
                      "full_population": {"silence_population": 0, "silence_scored": 0,
                                          "silence_skipped_no_graph": 0,
                                          "silence_fp_as_designed": 0,
                                          "silence_fp_engine_contract": 0,
                                          "silence_excluded_widening": 0,
                                          "silence_sampling": []},
                      "details": []}
            for repo_name, repo_path, res in per_repo_results:
                sub = score_patch_delta(res.signature, repo_path, graph_db=args.graph_db,
                                        gt_index=args.gt_index, limit=args.score_limit,
                                        silence_limit=args.silence_sample)
                for k in ("scorable_labels", "n_scored", "skipped_no_graph",
                          "tp", "fp", "fn"):
                    merged[k] += sub[k]
                for k in ("tp", "fp", "fn", "n_scored", "excluded_widening"):
                    merged["engine_contract"][k] += sub["engine_contract"][k]
                for k in ("silence_population", "silence_scored",
                          "silence_skipped_no_graph", "silence_fp_as_designed",
                          "silence_fp_engine_contract", "silence_excluded_widening"):
                    merged["full_population"][k] += sub["full_population"][k]
                merged["full_population"]["silence_sampling"].append(
                    f"{repo_name}: {sub['full_population']['silence_sampling']}")
                merged["details"].extend(sub["details"])
            merged["precision"], merged["recall"] = _pr8(
                merged["tp"], merged["fp"], merged["fn"])
            ec = merged["engine_contract"]
            ec["precision"], ec["recall"] = _pr8(ec["tp"], ec["fp"], ec["fn"])
            fullp = merged["full_population"]
            fad = merged["tp"] + merged["fp"] + fullp["silence_fp_as_designed"]
            fed = ec["tp"] + ec["fp"] + fullp["silence_fp_engine_contract"]
            fullp["as_designed_precision"] = round(merged["tp"] / fad, 8) if fad else 0.0
            fullp["engine_contract_precision"] = round(ec["tp"] / fed, 8) if fed else 0.0
            merged["as_designed"] = {"tp": merged["tp"], "fp": merged["fp"],
                                     "fn": merged["fn"], "precision": merged["precision"],
                                     "recall": merged["recall"],
                                     "n_scored": merged["n_scored"]}
            merged["status"] = "scored"
            merged["gt_index_path"] = args.gt_index
            merged["gt_index_sha256"] = _sha256_of(args.gt_index) if args.gt_index else None
            merged["graph_db"] = args.graph_db
            score_block = merged

    provenance = {
        "generated_by": "scripts/mine_history_labels.py",
        "schema_version": 2,
        "determinism_note": "deterministic; no wall-clock time recorded (git commit dates only)",
        # F4: full reproducibility record for THIS run's parameters.
        "run_parameters": {
            "rev": args.rev,
            "max_commits": args.max_commits,
            "score": args.score,
            "score_limit": args.score_limit,
            "silence_sample": args.silence_sample,
            "graph_db": args.graph_db,
            "gt_index": args.gt_index,
            "cli_note": "pass --rev=--all (with '='); argparse eats a bare --all",
        },
        "repos": prov_repos,
        "counts": counts,
        "small_n_flags": small_n,
        "noise_controls": agg_stats,
        "score_patch_delta": score_block,
    }
    with open(out_dir / "PROVENANCE.json", "w", encoding="utf-8", newline="\n") as fh:
        json.dump(provenance, fh, indent=2, sort_keys=True, ensure_ascii=True)
        fh.write("\n")

    print(f"signature={counts['signature']} companion={counts['companion']} "
          f"syntax={counts['syntax']} -> {out_dir}")
    for fam, flag in small_n.items():
        if flag:
            print(f"  small-n: {fam} has {counts[fam]} labels (<10) -- reported, not inflated")
    if score_block:
        if score_block.get("status") == "scored":
            ec = score_block["engine_contract"]
            fullp = score_block["full_population"]
            print(f"  patch_delta [as-designed truth]      P={score_block['precision']:.8f} "
                  f"R={score_block['recall']:.8f} "
                  f"(tp={score_block['tp']} fp={score_block['fp']} fn={score_block['fn']}; "
                  f"unit=caller-file pairs across {score_block['n_scored']} labels)")
            print(f"  patch_delta [engine-contract truth]  P={ec['precision']:.8f} "
                  f"R={ec['recall']:.8f} "
                  f"(tp={ec['tp']} fp={ec['fp']} fn={ec['fn']}; widenings excluded="
                  f"{ec['excluded_widening']} of {score_block['n_scored']})")
            print(f"  patch_delta [full population]        "
                  f"P_as={fullp['as_designed_precision']:.8f} "
                  f"P_contract={fullp['engine_contract_precision']:.8f} "
                  f"(silence scored={fullp['silence_scored']}/"
                  f"{fullp['silence_population']}, "
                  f"silence_fp={fullp['silence_fp_as_designed']})")
        else:
            print(f"  patch_delta score: {score_block['status']} -- {score_block.get('note', '')}")
    return 0


def _merge_stats(agg: dict, s: dict) -> None:
    for k, v in s.items():
        if isinstance(v, int):
            agg[k] = agg.get(k, 0) + v
        elif isinstance(v, dict):
            sub = agg.setdefault(k, {})
            for fam, counts in v.items():
                dst = sub.setdefault(fam, {})
                for ck, cv in counts.items():
                    dst[ck] = dst.get(ck, 0) + cv


if __name__ == "__main__":
    raise SystemExit(main())

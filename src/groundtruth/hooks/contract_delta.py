"""Contract-DELTA — what the agent's edit CHANGED in a function's behavioral contract.

Lives at the L3 contract seam (post_edit.generate_improved_evidence). L3 already renders the
*current* contract ("PRESERVE this"); this adds the missing operation — the before/after DIFF
("you CHANGED this") — and attaches the dependency consequence (verified callers + how they use
it + structural twin) so the agent sees not just "X changed" but "X changed AND N callers depend
on it / its twin wasn't updated".

Correctness — same-path before/after indexing:
  old and current content are BOTH indexed via the SAME single-file build path, so an UNEDITED
  function's properties are byte-identical and never diff. This removes phantom drift at the root
  (the bug came from diffing a full-build baseline against an incremental reindex). No line-range
  scoping needed for correctness — identical extraction ⇒ only genuinely-changed functions surface.

Language-agnostic (tree-sitter → all property kinds, not Python-ast). Zero test contact (callers
are is_test=0 by call-graph construction; assertions table never read). No execution; graph reads
only; LLM-free.

Research: 30-category failure taxonomy — "local correctness without global awareness: agents write
locally correct code that breaks callers, contracts, cross-file invariants" (ENRICHED_HANDOFF);
Smith et al. FSE 2015 (repair overfitting — patches pass given tests yet regress); CodePlan FSE 2024
(propagate edits to dependents); The Distracting Effect arXiv:2505.06914 (correct-or-quiet); Lost in
the Middle NeurIPS/TACL 2024 (delta first / primacy).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from groundtruth._binary import find_binary
from groundtruth.pretask.curation_map import _open_ro, build_function_map

# Contract-bearing property kinds we diff (the depth, not 4 fields). Ordered by how
# decisively a change breaks a dependent.
# We diff ONLY the STABLE, high-signal contract dimensions:
#   - exception_type: raised exception TYPES (identifiers like ValueError/TypeError).
#     Re-indentation / wrapping in `if smooth:` cannot churn an identifier; a dropped
#     raise = an exception callers catch is gone (high signal), a new raise = an
#     exception callers don't expect.
#   - return_shape: the return contract callers consume.
# Guard/boundary/conditional/side-effect VALUES are deliberately EXCLUDED: they are
# full-expression STRINGS that churn under benign restructuring (the arviz run5 LIPI:
# the agent wrapped code in `if smooth:`, the indexer re-extracted the SAME guards as
# different strings -> false "dropped/new guard"). Their real high-signal content (a
# removed precondition that RAISES) already surfaces as a dropped raise.
_DELTA_KINDS = (
    "return_shape",
    "exception_type",
)

# Human label per kind for the removed/added/changed rendering.
_KIND_LABEL = {
    "return_shape": "return shape",
    "exception_type": "raise",
    "guard_clause": "guard",
    "boundary_condition": "boundary check",
    "conditional_return": "branch return",
    "exception_handler": "exception handler",
    "side_effect": "side effect",
    "resource_pattern": "resource/context",
    "field_read": "state read",
    "call_order": "call order",
}

_MARKER = "[CONTRACT-DELTA]"


# Last compute_delta outcome — read by post_edit and surfaced into the HOST-visible
# gt_layer_events (the hook's stderr is in-container and lost). Lets us see WHY the delta
# was quiet in-container ("no_old_content" / "old_equals_current" / "index_failed" / ...).
LAST_REASON: str = ""


def _quiet(reason: str) -> list[str]:
    """Return [] but LOG why — so "correctly quiet" vs "silently broken" are
    distinguishable. The arviz live run returned [] silently (old-content recovery
    failed) with zero signal; every early-return now states its reason."""
    global LAST_REASON
    LAST_REASON = reason
    print(f"[GT_META] contract_delta_empty: reason={reason}", file=sys.stderr, flush=True)
    return []


def _git_env() -> dict:
    """Git env for in-container use. MUST set safe.directory=* — the SWE-bench testbed
    repo is owned by a different uid than the hook process, so without it git refuses
    with 'detected dubious ownership', returncode!=0, and old-content recovery silently
    returns empty (the run3 silent-fail). Mirrors post_edit._git_env."""
    e = dict(os.environ)
    e.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    e["GIT_CONFIG_COUNT"] = "1"
    e["GIT_CONFIG_KEY_0"] = "safe.directory"
    e["GIT_CONFIG_VALUE_0"] = "*"
    return e


def _path_candidates(file_rel: str) -> list[str]:
    """git-relative path candidates. SWE-bench prefixes file paths with the instance
    dir (`arviz-devs__arviz-2413/arviz/plots/x.py`) while the git root is that dir, so
    also try the path with the leading segment(s) stripped."""
    rel = file_rel.replace("\\", "/").lstrip("/")
    cands = [rel]
    parts = rel.split("/")
    for i in range(1, len(parts)):
        cands.append("/".join(parts[i:]))
    return cands


def _old_content(repo_root: str, file_rel: str) -> str:
    """The FULL pre-edit file from the IMMUTABLE task base commit — never a fragment.

    Anchors "old" to GT_BASE_COMMIT (the task's base SHA, captured BEFORE the agent ran),
    not live HEAD: the agent runs its own git commands (checkout/commit), which can move
    HEAD so that old==current or the diff is wrong — the in-container silence observed in
    run7. We try the base ref first, then HEAD as a fallback. Full file only (a fragment
    makes the whole pre-existing contract read as "new" — the run4 bug). "" if unavailable."""
    base_ref = os.environ.get("GT_BASE_COMMIT", "").strip()
    refs = [base_ref, "HEAD"] if base_ref else ["HEAD"]
    for ref in refs:
        for rel in _path_candidates(file_rel):
            try:
                r = subprocess.run(
                    ["git", "-C", repo_root, "show", f"{ref}:{rel}"],
                    capture_output=True, text=True, timeout=10, env=_git_env(),
                )
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout
            except (subprocess.SubprocessError, OSError):
                continue
    return ""


def _read_current(repo_root: str, file_rel: str) -> str | None:
    """Current (post-edit) file content from disk, trying the task-dir-prefixed path
    AND stripped variants (the same prefix ambiguity as git). None if not found."""
    for rel in _path_candidates(file_rel):
        path = os.path.join(repo_root, rel) if repo_root else rel
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def _index_one(content: str, file_rel: str) -> str | None:
    """Full-build index a single file (in an isolated 1-file dir) → scratch db path, or None.

    Full-build (not incremental) and identical for both sides ⇒ same extraction path."""
    try:
        binary = find_binary()
    except Exception:
        return None
    d = tempfile.mkdtemp(prefix="gtdelta_")
    base = os.path.basename(file_rel) or "f"
    fp = os.path.join(d, base)
    try:
        with open(fp, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
    except OSError:
        shutil.rmtree(d, ignore_errors=True)
        return None
    db = os.path.join(d, "g.db")
    try:
        r = subprocess.run([binary, "-root", d, "-output", db],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(db):
            return None
    except (subprocess.SubprocessError, OSError):
        return None
    return db  # caller is responsible for cleanup of the parent dir


# Disk cache for the OLD side. The old content is git HEAD — IMMUTABLE for the whole run —
# so the agent's repeated edits to the same file re-index identical old content every time.
# Cache the extracted props keyed by (base, sha256(old)). The cache holds props produced by
# the SAME _index_one path, so same-path correctness (no phantom drift) is preserved; the
# binary is deterministic (determinism_rate=1.0), so a hit is byte-identical to a fresh index.
# Each post_edit invocation is a fresh process, so the cache must live on disk (not in-memory).
_DELTA_CACHE_DIR = os.path.join(tempfile.gettempdir(), "gt_delta_oldcache")


def _old_props_cached(old: str, base: str) -> dict[str, dict[str, set]] | None:
    key = hashlib.sha256(f"{base}\0{old}".encode("utf-8", "replace")).hexdigest()
    try:
        with open(os.path.join(_DELTA_CACHE_DIR, key + ".json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        return {fn: {k: set(v) for k, v in kinds.items()} for fn, kinds in raw.items()}
    except (OSError, ValueError, TypeError):
        return None


def _old_props_store(old: str, base: str, pre: dict[str, dict[str, set]]) -> None:
    key = hashlib.sha256(f"{base}\0{old}".encode("utf-8", "replace")).hexdigest()
    try:
        os.makedirs(_DELTA_CACHE_DIR, exist_ok=True)
        raw = {fn: {k: sorted(v) for k, v in kinds.items()} for fn, kinds in pre.items()}
        with open(os.path.join(_DELTA_CACHE_DIR, key + ".json"), "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
    except OSError:
        pass


def _props_by_func(db: str, base_name: str) -> dict[str, dict[str, set]]:
    """{func_name: {kind: {values}}} for contract kinds, from a single-file scratch db."""
    conn = _open_ro(db)
    if conn is None:
        return {}
    out: dict[str, dict[str, set]] = {}
    try:
        kph = ",".join("?" for _ in _DELTA_KINDS)
        rows = conn.execute(
            f"SELECT n.name, p.kind, p.value FROM nodes n JOIN properties p ON p.node_id=n.id "
            f"WHERE n.file_path = ? AND n.label IN ('Function','Method') AND p.kind IN ({kph})",
            (base_name, *_DELTA_KINDS),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    for name, kind, value in rows:
        if not name or not value:
            continue
        out.setdefault(name, {}).setdefault(kind, set()).add(str(value).strip())
    return out


def _verified_caller_count(graph_db: str, file_rel: str, name: str) -> int:
    maps = build_function_map(graph_db, [(file_rel, name)], dynamic=False)
    if not maps:
        return 0
    return sum(1 for e in maps[0].callers if e.is_fact)


def _twin_not_updated(graph_db: str, file_rel: str, name: str, changed: set[str]) -> str:
    """If the changed function has a structural_twin / serde partner that was NOT itself
    changed, name it (completeness — CodePlan FSE 2024)."""
    conn = _open_ro(graph_db)
    if conn is None:
        return ""
    try:
        rows = conn.execute(
            "SELECT p.value FROM nodes n JOIN properties p ON p.node_id=n.id "
            "WHERE n.file_path=? AND n.name=? AND p.kind IN ('structural_twin','serialization_pair')",
            (file_rel, name),
        ).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    for (val,) in rows:
        # values look like "twin: set_metadata (get_/set_ pair)" or "partner:parse_x@file:..|sig:.."
        m = re.search(r"(?:twin:|partner:)\s*([A-Za-z_][A-Za-z0-9_]*)", str(val))
        if m and m.group(1) not in changed:
            return m.group(1)
    return ""


def _diff_func(pre: dict[str, set], post: dict[str, set]) -> list[str]:
    """High-signal, restructuring-stable contract changes for one function.

    Diffs ONLY return shape + raised exception TYPES (see _DELTA_KINDS rationale).
    A removed/changed return or a dropped/added raise is a real change callers depend
    on; both are identifier/shape-level so benign re-indentation cannot produce them.
    """
    lines: list[str] = []
    # Return shape — diffed at the CATEGORY level (value / collection / none / ...),
    # not the raw expression. The indexer stores "<category>|<expr>"; comparing the
    # full string flags a benign rename (value|val -> value|result) as a change. Only
    # a category shift (value -> none, collection -> value) is a real return-contract change.
    a = {s.split("|", 1)[0] for s in pre.get("return_shape", set())}
    b = {s.split("|", 1)[0] for s in post.get("return_shape", set())}
    if a != b and (a or b):
        lines.append(
            f"  return shape: {', '.join(sorted(a)) or 'none'} -> "
            f"{', '.join(sorted(b)) or 'none'}"
        )
    # Raised exception TYPES — dropped (removed a check callers catch) / new.
    a = pre.get("exception_type", set())
    b = post.get("exception_type", set())
    for removed in sorted(a - b):
        lines.append(f"  dropped raise: {removed[:60]}")
    for added in sorted(b - a):
        lines.append(f"  new raise: {added[:60]}")
    return lines


def compute_delta(
    graph_db: str,
    file_rel: str,
    *,
    repo_root: str,
    diff_text: str = "",
    current_content: str | None = None,
    old_content: str | None = None,
) -> list[str]:
    """Return the [CONTRACT-DELTA] lines for what the edit changed, or [] (correct-or-quiet).

    graph_db: the main (current) graph, used ONLY for the dependency consequence.
    repo_root + diff_text: recover the pre-edit content. current_content: post-edit content
    (read from disk if None). Never raises.
    """
    file_rel = file_rel.replace("\\", "/").lstrip("/")
    try:
        # OLD must be the FULL pre-edit file. A caller-supplied full file (tests /
        # explicit callers) is honored; otherwise recover from git HEAD. We do NOT
        # use diff-fragment reconstruction or OH's str_replace window — a fragment
        # makes the whole pre-existing contract read as "new" (arviz run4 bug).
        old = old_content if old_content else _old_content(repo_root, file_rel)
        if current_content is None:
            current_content = _read_current(repo_root, file_rel)
            if current_content is None:
                return _quiet("no_current_content")
        if not old:
            return _quiet("no_old_content")
        if not current_content:
            return _quiet("no_current_content")
        if old == current_content:
            return _quiet("old_equals_current")

        base = os.path.basename(file_rel)
        # OLD side is git HEAD — immutable for the run — so cache its props across the
        # agent's repeated edits to this file. Only the CURRENT side is indexed every
        # edit (it changed). Same-path correctness holds: cached props came from the same
        # _index_one path, and the binary is deterministic.
        pre = _old_props_cached(old, base)
        if pre is None:
            old_db = _index_one(old, file_rel)
            if not old_db:
                return _quiet("index_failed:old")
            try:
                pre = _props_by_func(old_db, base)
            finally:
                shutil.rmtree(os.path.dirname(old_db), ignore_errors=True)
            _old_props_store(old, base, pre)
        new_db = _index_one(current_content, file_rel)
        if not new_db:
            return _quiet("index_failed:new")
        try:
            post = _props_by_func(new_db, base)
        finally:
            shutil.rmtree(os.path.dirname(new_db), ignore_errors=True)

        changed_names = sorted(n for n in (set(pre) | set(post)) if pre.get(n) != post.get(n))
        if not changed_names:
            return _quiet("no_changed_funcs")

        blocks: list[str] = []
        for name in changed_names:
            pre_props = pre.get(name, {})
            post_props = post.get(name, {})
            # Degenerate-old guard: old has ZERO properties for a function the post
            # shows as fully-formed => old-content recovery degraded to a fragment
            # (or the func is brand-new). Either way, reporting the entire post
            # contract as "new" is the run4 false-positive flood. Stay quiet.
            if not pre_props and post_props:
                print(f"[GT_META] contract_delta_skip: degenerate_old func={name}",
                      file=sys.stderr, flush=True)
                continue
            dlines = _diff_func(pre_props, post_props)
            if not dlines:
                continue
            cnt = _verified_caller_count(graph_db, file_rel, name)
            head = f"{_MARKER} {name}"
            if cnt > 0:
                s = "s" if cnt != 1 else ""
                head += f"  ({cnt} verified caller{s} depend on this)"
            block = [head, *dlines]
            twin = _twin_not_updated(graph_db, file_rel, name, set(changed_names))
            if twin:
                block.append(f"  twin not updated: {twin}")
            blocks.extend(block)
        global LAST_REASON
        LAST_REASON = f"delivered:{len(changed_names)}func" if blocks else "no_material_diff"
        return blocks
    except Exception as e:
        return _quiet(f"exception:{type(e).__name__}:{str(e)[:80]}")

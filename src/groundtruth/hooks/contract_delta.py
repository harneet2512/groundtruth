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
_DELTA_KINDS = (
    "return_shape",
    "exception_type",
    "guard_clause",
    "boundary_condition",
    "conditional_return",
    "exception_handler",
    "side_effect",
    "resource_pattern",
    "field_read",
    "call_order",
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


def _quiet(reason: str) -> list[str]:
    """Return [] but LOG why — so "correctly quiet" vs "silently broken" are
    distinguishable. The arviz live run returned [] silently (old-content recovery
    failed) with zero signal; every early-return now states its reason."""
    print(f"[GT_META] contract_delta_empty: reason={reason}", file=sys.stderr, flush=True)
    return []


def _git_env() -> dict:
    e = dict(os.environ)
    e.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    return e


def _old_content(repo_root: str, file_rel: str, diff_text: str) -> str:
    """Pre-edit file content: git HEAD first, then reconstruct from the unified diff."""
    try:
        r = subprocess.run(
            ["git", "show", f"HEAD:{file_rel}"],
            capture_output=True, text=True, cwd=repo_root, timeout=10, env=_git_env(),
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    # fallback: rebuild old side from the diff (deleted + context lines)
    if not diff_text:
        return ""
    target = file_rel.replace("\\", "/").lstrip("/")
    out: list[str] = []
    in_file = False
    for ln in diff_text.splitlines():
        # Match the +++ new-side header for ANY path form: `b/<rel>`, a bare rel
        # path, OR an ABSOLUTE container path (`/workspace/<task>/<rel>`). OpenHands
        # emits absolute paths, so the old `+++ b/` check never matched (the arviz
        # silent-empty bug). Suffix-match the target rel path.
        if ln.startswith("+++ "):
            p = ln[4:].strip().replace("\\", "/")
            if p.startswith("b/"):
                p = p[2:]
            p = p.lstrip("/")
            # exact, or path-boundary suffix (absolute container path). NOT a bare
            # endswith — that would false-match foo_hdiplot.py for target hdiplot.py.
            in_file = p == target or p.endswith("/" + target)
            continue
        if ln.startswith("--- ") or ln.startswith("diff "):
            continue
        if not in_file:
            continue
        if ln.startswith("-") and not ln.startswith("---"):
            out.append(ln[1:])
        elif ln.startswith(" "):
            out.append(ln[1:])
    return "\n".join(out)


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
    """Material contract changes for one function, rendered as compact lines."""
    lines: list[str] = []
    kinds = [k for k in _DELTA_KINDS if k in pre or k in post]
    for kind in kinds:
        a = pre.get(kind, set())
        b = post.get(kind, set())
        if a == b:
            continue
        label = _KIND_LABEL.get(kind, kind)
        if kind == "return_shape":
            # scalar-ish: show old -> new compactly
            lines.append(f"  return shape: {', '.join(sorted(a)) or 'none'} -> {', '.join(sorted(b)) or 'none'}")
            continue
        for removed in sorted(a - b):
            lines.append(f"  dropped {label}: {removed[:80]}")
        for added in sorted(b - a):
            lines.append(f"  new {label}: {added[:80]}")
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
        # Prefer the caller-supplied pre-edit content (post_edit.main already recovered
        # it with 3 fallbacks incl git HEAD); fall back to our own recovery only if not
        # passed. This is the arviz fix: the hook now threads old_content_text through.
        old = (old_content or "") if old_content else _old_content(repo_root, file_rel, diff_text)
        if current_content is None:
            cur_path = os.path.join(repo_root, file_rel) if repo_root else file_rel
            try:
                with open(cur_path, encoding="utf-8", errors="replace") as fh:
                    current_content = fh.read()
            except OSError:
                return _quiet("no_current_content")
        if not old:
            return _quiet("no_old_content")
        if not current_content:
            return _quiet("no_current_content")
        if old == current_content:
            return _quiet("old_equals_current")

        base = os.path.basename(file_rel)
        old_db = _index_one(old, file_rel)
        new_db = _index_one(current_content, file_rel)
        try:
            if not old_db or not new_db:
                return _quiet(f"index_failed:old={bool(old_db)},new={bool(new_db)}")
            pre = _props_by_func(old_db, base)
            post = _props_by_func(new_db, base)
        finally:
            for db in (old_db, new_db):
                if db:
                    shutil.rmtree(os.path.dirname(db), ignore_errors=True)

        changed_names = sorted(n for n in (set(pre) | set(post)) if pre.get(n) != post.get(n))
        if not changed_names:
            return _quiet("no_changed_funcs")

        blocks: list[str] = []
        for name in changed_names:
            dlines = _diff_func(pre.get(name, {}), post.get(name, {}))
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
        return blocks
    except Exception as e:
        return _quiet(f"exception:{type(e).__name__}:{str(e)[:80]}")

"""Preflight delivery-invariant tests for the L1 brief assembly.

These run the REAL code from scripts/swebench/oh_gt_full_wrapper.py — not a fake.
Because that module patches OpenHands at import time and cannot be imported
standalone, we extract the actual source of `_brief_max_tokens` and the
instruction-wrap block via `ast`/source-slicing and exec them in isolation. The
extracted bytes ARE the shipped logic.

What they guard (the bugs that reached the agent in canary 26651360055):
  1. `_brief_max_tokens` must NOT reorder — the old `merged = ranked + rest`
     pulled path-bearing lines to the front and stranded the <gt-graph-map>
     tags together, emptying the map body. A local `_old_brief_max_tokens`
     reproduces that and is the red-before-green negative control: the invariant
     checker MUST fail on it.
  2. The instruction wrap must not double-wrap an already-wrapped brief -> the
     final instruction must contain exactly one <gt-task-brief> open + one close.
  3. No hidden [GT_*] diagnostic prefix may survive into a delivered brief.

NOTE: invariant 3's full form (no [GT_*] in the real agent instruction) is the
job of the runtime verifier (scripts/verify/check_brief_delivery.py) on a real
output.jsonl; here we only assert _brief_max_tokens introduces no pollution.
"""
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

# The wrap block now routes the brief through the Safe Renderer (C1); the
# extracted block references it, so it must be in the exec namespace.
from groundtruth.runtime.sanitizer import sanitize_evidence_block as _core_sanitize_block

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "swebench" / "oh_gt_full_wrapper.py"

# A structured v1r brief: <gt-graph-map> is a sibling block AFTER </gt-task-brief>,
# and its BODY lines contain file paths (so the old reorder pulls them to the front
# and leaves the tag lines adjacent -> empty map). Small enough to be < any budget.
STRUCTURED_BRIEF = "\n".join([
    "<gt-task-brief>",
    "1. app/core.py (def run(self, documents):)",
    "   Contract: raises ValueError, TypeError | preserve not documents | returns value",
    "</gt-task-brief>",
    "<gt-graph-map>",
    "app/core.py :: run",
    "  calls: helper (app/util.py), validate (app/util.py)",
    "  called by: main (app/main.py)",
    "</gt-graph-map>",
])


def _read_source() -> str:
    assert WRAPPER.exists(), f"wrapper not found: {WRAPPER}"
    return WRAPPER.read_text(encoding="utf-8")


def _load_func(name: str):
    """Extract a module-level function's real source and exec it in isolation."""
    src = _read_source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            ns: dict = {"re": re}
            exec(compile(seg, str(WRAPPER), "exec"), ns)
            return ns[name]
    raise AssertionError(f"function {name} not found in {WRAPPER}")


def _load_wrap_block() -> str:
    """Slice the real instruction-wrap conditional block (dedented) for exec."""
    src = _read_source().splitlines()
    marker = "_wrapped = _core_sanitize_block(brief.strip())"
    start = next((i for i, ln in enumerate(src) if ln.strip() == marker), None)
    assert start is not None, f"wrap block marker `{marker}` not found"
    block = "\n".join(src[start:start + 6])
    return textwrap.dedent(block)


def _graph_map_body(s: str) -> str | None:
    m = re.search(r"<gt-graph-map>(.*?)</gt-graph-map>", s, re.S)
    return None if not m else m.group(1).strip()


def _old_brief_max_tokens(text: str, max_tokens: int = 2000) -> str:
    """The ORIGINAL buggy behavior (reorder). Negative control only."""
    if not text:
        return ""
    max_chars = max_tokens * 4
    lines = text.strip().split("\n")
    path_line_re = re.compile(r"[a-zA-Z0-9_./\\-]+\.[a-zA-Z0-9]{1,4}\b")
    ranked = [ln for ln in lines if path_line_re.search(ln)]
    rest = [ln for ln in lines if not path_line_re.search(ln)]
    merged = ranked + rest
    out, n = [], 0
    for ln in merged:
        if n + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        n += len(ln) + 1
    return "\n".join(out)


# ---------- _brief_max_tokens structure preservation ----------

def test_brief_max_tokens_preserves_graph_map_body():
    fn = _load_func("_brief_max_tokens")
    out = fn(STRUCTURED_BRIEF, max_tokens=2000)  # 8000-char budget; brief is ~250c
    assert out.lstrip().startswith("<gt-task-brief>"), "task-brief must remain first/outer"
    body = _graph_map_body(out)
    assert body is not None, "graph-map tag must survive"
    assert len(body) > 0, "graph-map BODY must be non-empty (the reorder bug emptied it)"
    assert "app/util.py" in body, "graph-map body content must be preserved, not stranded"


def test_brief_max_tokens_does_not_reorder():
    fn = _load_func("_brief_max_tokens")
    out = fn(STRUCTURED_BRIEF, max_tokens=2000)
    # under budget -> identical content, original order
    assert out.strip() == STRUCTURED_BRIEF.strip(), "in-order cap must not change a sub-budget brief"


def test_brief_max_tokens_introduces_no_pollution():
    fn = _load_func("_brief_max_tokens")
    out = fn(STRUCTURED_BRIEF, max_tokens=2000)
    for bad in ("[GT_META]", "[GT_BRIEF_DIAG]", "[GT_RANK_DIAG]"):
        assert bad not in out


# ---------- red-before-green negative control ----------

def test_negative_control_old_reorder_empties_map():
    """Proves the invariant catches the ORIGINAL bug class.

    The old reorder MUST empty the graph-map body; if this ever stops failing,
    the negative control (and thus the whole test's value) is broken.
    """
    bad = _old_brief_max_tokens(STRUCTURED_BRIEF, max_tokens=2000)
    bad_body = _graph_map_body(bad)
    assert bad_body == "", "old reorder is expected to EMPTY the graph-map body (red-before-green)"
    # And confirm the REAL function does NOT exhibit the bug on the same input.
    good = _load_func("_brief_max_tokens")(STRUCTURED_BRIEF, max_tokens=2000)
    assert (_graph_map_body(good) or "") != "", "real _brief_max_tokens must preserve the body"


# ---------- single-wrap invariant (real wrap block) ----------

def test_wrap_does_not_double_wrap_already_wrapped_brief():
    block = _load_wrap_block()
    brief = _load_func("_brief_max_tokens")(STRUCTURED_BRIEF, max_tokens=2000)  # starts with <gt-task-brief>
    ns = {"brief": brief, "content": "<uploaded_files>\nrepo\n</uploaded_files>\nissue text",
          "tools_hint": "", "_demo": "", "_core_sanitize_block": _core_sanitize_block}
    exec(block, ns)
    final = ns["content"]
    assert final.count("<gt-task-brief>") == 1, "exactly one <gt-task-brief> open tag"
    assert final.count("</gt-task-brief>") == 1, "exactly one </gt-task-brief> close tag"
    assert (_graph_map_body(final) or "") != "", "graph-map body must survive into the instruction"


def test_wrap_still_wraps_an_unwrapped_brief():
    block = _load_wrap_block()
    ns = {"brief": "plain brief with no tags", "content": "issue", "tools_hint": "", "_demo": "",
          "_core_sanitize_block": _core_sanitize_block}
    exec(block, ns)
    final = ns["content"]
    assert final.count("<gt-task-brief>") == 1
    assert final.count("</gt-task-brief>") == 1


def test_source_has_no_unconditional_double_wrap():
    """Regression lock: the old unconditional double-wrap line must be gone."""
    src = _read_source()
    assert 'content = f"<gt-task-brief>\\n{brief}\\n</gt-task-brief>' not in src, \
        "the unconditional double-wrap must not return"
    # BUG 3 (e86151d6): the guard was upgraded from startswith() to presence (`in`)
    # because the prepended <gt-localization> header means the block no longer STARTS
    # with <gt-task-brief>. Lock the improved presence-based guard.
    assert '"<gt-task-brief>" in _wrapped' in src, \
        "the conditional single-wrap guard (presence-based) must be present"


def test_source_has_no_reorder():
    """Regression lock: the reorder must not be reintroduced as EXECUTABLE code.

    (Comment lines are excluded — the fix's docstring legitimately documents the
    old `merged = ranked + rest` behavior; only a real code statement is a regression.)
    """
    code_lines = [ln for ln in _read_source().splitlines() if not ln.lstrip().startswith("#")]
    offending = [ln.strip() for ln in code_lines if "merged = ranked + rest" in ln]
    assert not offending, f"reorder reintroduced as code: {offending}"

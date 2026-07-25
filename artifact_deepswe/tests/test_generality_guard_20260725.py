"""GENERALIZATION / ANTI-BENCHMAXX GUARD — enforced, not promised.

CLAUDE.md's non-negotiable boundary: GT must not be a benchmark trick, a gold-label router, a
task-ID router, or a system that only works because of SWE-bench structure. That rule has been
stated in prose for months; this makes it EXECUTABLE, so a violation fails a test instead of
surviving until someone notices it in review.

Scope note (honest): this is a STATIC guard over GT's runtime surface. It cannot prove generality —
only a held-out run can. What it CAN do is make the specific, mechanical ways benchmaxxing enters a
codebase impossible to add silently: keying on instance ids, reading gold labels, or hardcoding a
benchmark's repo names into decision logic.
"""
from __future__ import annotations
import os
import re

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# The RUNTIME decision surface — the code that chooses what the model sees.
_RUNTIME = [
    os.path.join(_ROOT, "artifact_deepswe", "gt_mini_patch.py"),
    os.path.join(_ROOT, "src", "groundtruth", "runtime", "gateway.py"),
    os.path.join(_ROOT, "src", "groundtruth", "runtime", "covering_runner.py"),
    os.path.join(_ROOT, "src", "groundtruth", "runtime", "context_policy.py"),
    os.path.join(_ROOT, "src", "groundtruth", "runtime", "fact_registry.py"),
    os.path.join(_ROOT, "src", "groundtruth", "runtime", "adapters", "miniswe.py"),
]


def _code_lines(path):
    """Source lines with comments/docstring-ish prose excluded — a rule may be DISCUSSED in a
    comment (and several are, as recorded rationale) without being ENFORCED in code."""
    out = []
    for n, raw in enumerate(open(path, encoding="utf-8", errors="replace").read().splitlines(), 1):
        s = raw.strip()
        if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
            continue
        out.append((n, raw.split("#", 1)[0]))
    return out


@pytest.mark.parametrize("path", [p for p in _RUNTIME if os.path.exists(p)])
def test_no_gold_label_consumption(path):
    """GT must never READ the benchmark's answer key. Every FAIL_TO_PASS/PASS_TO_PASS occurrence in
    GT source must be a LEAK GUARD that scrubs it, never a value the code branches on."""
    bad = []
    for n, line in _code_lines(path):
        if re.search(r"\b(FAIL_TO_PASS|PASS_TO_PASS)\b", line):
            # Permitted: this is a LEAK GUARD, not a consumer. Two accepted shapes —
            #   (a) the scrub call itself on this line (re.sub / scrub / redact / _LEAK / _RE), or
            #   (b) a bare regex LITERAL continuing a re.compile( on the preceding line, which is
            #       how the canonical scrub pattern is written (gt_mini_patch ~L8290).
            # A branch on the VALUE (== / in / .get) is never permitted and still fails below.
            if re.search(r"re\.|sub\(|scrub|_LEAK|redact|pattern|_RE\b", line, re.I):
                continue
            if re.search(r'^\s*r["\']', line) or r"\b(?:" in line:
                continue
            # A MENTION is not a CONSUMPTION. Only flag a line that actually READS the value —
            # compares it, indexes it, or .get()s it. Prose inside a docstring naming the token
            # ("no assertions, no FAIL_TO_PASS") documents the leak law rather than using it.
            if not re.search(r"==|!=|\bin\b|\[|\.get\(", line):
                continue
            bad.append((n, line.strip()[:90]))
    assert not bad, f"{os.path.basename(path)} branches on gold labels:\n" + "\n".join(
        f"  L{n}: {t}" for n, t in bad)


@pytest.mark.parametrize("path", [p for p in _RUNTIME if os.path.exists(p)])
def test_no_instance_id_keying(path):
    """A SWE-bench instance id in decision logic is a task router by definition."""
    pat = re.compile(r"['\"][a-z0-9_.-]+__[a-z0-9_.-]+-\d+['\"]", re.I)
    bad = [(n, l.strip()[:90]) for n, l in _code_lines(path) if pat.search(l)]
    assert not bad, f"{os.path.basename(path)} keys on task instance ids:\n" + "\n".join(
        f"  L{n}: {t}" for n, t in bad)


@pytest.mark.parametrize("path", [p for p in _RUNTIME if os.path.exists(p)])
def test_no_benchmark_repo_names_in_decision_logic(path):
    """Repo names may appear in RATIONALE comments (they are evidence of where a bug was found),
    but branching on them makes behaviour repo-specific."""
    repos = ("swebench", "swe_bench", "swe-bench")
    bad = [(n, l.strip()[:90]) for n, l in _code_lines(path)
           if any(r in l.lower() for r in repos) and ("==" in l or " in " in l)]
    assert not bad, f"{os.path.basename(path)} branches on benchmark identity:\n" + "\n".join(
        f"  L{n}: {t}" for n, t in bad)


def test_selection_is_deterministic_by_construction():
    """SOTA/reproducibility floor: selection must be a pure function of the input. A set fed into
    SQL takes per-process hash order; LIMIT without ORDER BY lets the engine return any rows.
    Both were REAL defects here (ss_gate flaked 4/6 RED until fixed on 2026-07-25)."""
    for path in (os.path.join(_ROOT, "artifact_deepswe", "gt_mini_patch.py"),
                 os.path.join(_ROOT, "src", "groundtruth", "runtime", "covering_runner.py")):
        raw = open(path, encoding="utf-8", errors="replace").read()
        # strip comments: a comment may DISCUSS "LIMIT 2" (several record why an ambiguity probe
        # is order-independent) without being SQL the engine ever sees.
        src = chr(10).join(l.split("#", 1)[0] for l in raw.splitlines())
        # every multi-row LIMIT that selects a SET must carry an ORDER BY in the same statement
        for m in re.finditer(r"LIMIT\s+(\d+)", src):
            if int(m.group(1)) <= 1:
                continue  # existence probe — order cannot change the answer
            start = src.rfind("SELECT", 0, m.start())
            stmt = src[start:m.end()]
            assert "ORDER BY" in stmt, (
                f"{os.path.basename(path)}: LIMIT {m.group(1)} without ORDER BY at "
                f"line {src[:m.start()].count(chr(10)) + 1} — selection is not a pure function "
                "of the input, so no offline proof transfers to an online run"
            )

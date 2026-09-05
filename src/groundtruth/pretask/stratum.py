"""Deterministic issue-shape classifier — the localization stratum of an issue.

An issue falls into exactly one stratum, and the stratum decides which retrieval
leg should LEAD and how GT localization compares to the agent's own grep loop:

  A  symbol-anchored   — the issue names a code symbol that RESOLVES to a graph
                         node. The agent self-localizes well here; GT's win is
                         first-hit (def-vs-ref-vs-test known at step 0), not
                         out-ranking the converged loop.
  B  behavior-described — prose only, no resolving anchor ("Redis TLS fails").
                         The ranking fight: name-derived surfaces all fail
                         together, so content/semantic legs must lead.
  C  traceback-bearing  — a stack trace is present. The deepest in-repo frame is
                         a strong localizer; deep-frame bugs are a GT win.
  D  new-file/feature   — the issue names code that resolves NOWHERE (the API to
                         be built). The gold is often a file the graph cannot
                         contain; GT delivers a directory/sibling neighborhood.

Precedence: **C > D > A > B**.
  - A traceback is the most precise localizer, so C wins even when symbols also
    resolve (a pasted traceback usually also mentions real symbols).
  - A dominant feature-add's gold is frequently a NEW file, so D must beat A or a
    lone context-symbol would steal a feature-add into graph ranking of existing
    files. The `>=` dominance guard prevents that.

ROOT-CAUSE NOTE (why this exists): an earlier crude classifier keyed stratum A on
token *shape* (a backtick / snake_case / camelCase / ``foo()`` present in the
issue). Virtually every issue contains such a token, so it flagged everything as
A (measured: 12/12 tapes). The fix is graph *resolution*: A requires
``IssueAnchors.symbols`` — identifiers cross-checked against ``nodes.name`` — to
be non-empty, not mere ``symbols_pre_stopword`` shape. The label is advisory; the
``evidence`` dict carries every raw signal so a consumer (measure_brief, the shape
router, newfile) can re-decide or log.

Deterministic + no LLM: pure functions of issue text + the graph's ``nodes.name``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.traces import parse_stack_traces

__all__ = ["StratumResult", "classify_stratum"]

# Explicit traceback markers per language — used ONLY as the offline fallback when
# no ``repo_root`` is available for ``parse_stack_traces`` (which repo-filters
# frames). Kept deliberately narrow so C stays precise. Covers all five DeepSWE
# languages: an earlier version was Python/JS-only and would mislabel a Go panic or
# Rust ``panicked at`` backtrace as B on a multi-language bench.
_TRACEBACK_RES = (
    re.compile(r"^\s*Traceback \(most recent call last\):", re.MULTILINE),  # Python
    re.compile(r"\n\s+at\s+.+\(.+:\d+:\d+\)"),                                # JS/TS
    re.compile(r"^panic:\s|\ngoroutine \d+ \[", re.MULTILINE),               # Go
    re.compile(r"thread '.*' panicked at|panicked at .+:\d+:\d+"),           # Rust
    re.compile(r"^\s*at .+\.java:\d+\)", re.MULTILINE),                       # Java
)

# Feature-add verbs in the issue title region. EVIDENCE ONLY — never the sole
# trigger for stratum D (D is gated structurally on unresolved-symbol dominance);
# a brittle verb list must not decide routing on its own.
_FEATURE_VERBS = frozenset(
    {"add", "support", "implement", "introduce", "allow", "enable", "expose", "provide"}
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _has_traceback(issue_text: str, repo_root: str) -> tuple[bool, int]:
    """(has_traceback, in_repo_frame_count). Uses ``parse_stack_traces`` when a
    ``repo_root`` is given (exact, repo-filtered); else a narrow textual fallback
    (offline tapes have no checkout). Best-effort offline, exact live."""
    if repo_root:
        frames = parse_stack_traces(issue_text, repo_root)
        if frames:
            return True, len(frames)
    if not issue_text:
        return False, 0
    if any(rx.search(issue_text) for rx in _TRACEBACK_RES):
        return True, 0
    return False, 0


def _title_region(issue_text: str) -> str:
    """The first non-empty line (the report summary), lowercased. BugLocator
    (Zhou et al., ICSE 2012) weights the summary above the body."""
    for line in (issue_text or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s.lower()
    return ""


def _feature_verb_hit(issue_text: str) -> bool:
    return bool(_FEATURE_VERBS & set(_WORD_RE.findall(_title_region(issue_text))))


@dataclass(frozen=True)
class StratumResult:
    label: str  # "A" | "B" | "C" | "D"
    evidence: dict = field(default_factory=dict)


def classify_stratum(
    issue_text: str,
    graph_db: str | None = None,
    repo_root: str = "",
    issue_anchors: IssueAnchors | None = None,
) -> StratumResult:
    """Classify an issue into stratum A/B/C/D. Precedence C > D > A > B.

    ``issue_anchors`` may be passed in to avoid a second extraction (the brief
    hoists an ``IssueAnchors`` once; measure_brief/the router should reuse it so
    the classifier and the brief agree on the anchor set). When absent, anchors
    are extracted here (graph-cross-checked iff ``graph_db`` is given).
    correct-or-quiet: a missing graph means ``symbols``/``unresolved`` cannot be
    known, so A/D degrade to B rather than guessing.
    """
    anchors = issue_anchors if issue_anchors is not None else extract_issue_anchors(
        issue_text, graph_db
    )

    # correct-or-quiet: A and D both assert something about GRAPH RESOLUTION
    # (a token resolves / provably does NOT resolve to a node). Without a graph,
    # ``extract_issue_anchors`` does NO cross-check and fills ``symbols`` with raw
    # tokens (measured: n_sym=3-5 on prose) — so A would fire on unresolved
    # tokens, the very #51 failure through a side door. A passed-in ``issue_anchors``
    # is trusted graph-resolved (the brief hoists it WITH a graph). When neither a
    # graph nor pre-resolved anchors are available, A/D cannot be claimed → degrade
    # to B (or C on a textual traceback, which needs no graph).
    graph_resolved = bool(graph_db) or issue_anchors is not None

    n_sym = len(anchors.symbols)
    n_unres = len(anchors.unresolved_code_symbols)
    has_tb, n_frames = _has_traceback(issue_text, repo_root)
    verb_hit = _feature_verb_hit(issue_text)

    # D is STRUCTURAL: unresolved code tokens dominate the resolving ones. G5:
    # when RESOLVED anchors exist (n_sym>0) the issue's gold is very likely an
    # EXISTING graph-contained file, so D requires POSITIVE new-file evidence (a
    # feature-add verb in the title) on top of unresolved dominance — otherwise a
    # few prose/error tokens that slipped past the anchor filter would mislabel an
    # existing-file bug as new-file. With no resolved anchors, unresolved dominance
    # alone still routes to D (the pure greenfield case).
    d_dominant = graph_resolved and n_unres > 0 and (
        n_sym == 0 or (n_unres >= n_sym and verb_hit)
    )
    a_anchored = graph_resolved and n_sym > 0

    if has_tb:
        label = "C"
    elif d_dominant:
        label = "D"
    elif a_anchored:
        label = "A"
    else:
        label = "B"

    evidence = {
        "resolved_anchors": sorted(anchors.symbols),
        "unresolved_symbols": sorted(anchors.unresolved_code_symbols),
        "title_symbols": sorted(anchors.title_symbols),
        "n_resolved": n_sym,
        "n_unresolved": n_unres,
        "in_repo_frames": n_frames,
        "has_traceback": has_tb,
        "feature_verb_hit": verb_hit,
        "graph_available": bool(graph_db) or issue_anchors is not None,
    }
    return StratumResult(label=label, evidence=evidence)

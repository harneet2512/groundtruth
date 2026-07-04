"""v8.0 bounded localization governor.

This module is intentionally separate from the frozen v7.5 scorer.  It consumes
v7.5-style ranked records plus early agent behavioral evidence and deterministically
selects a small active working set.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Literal

BehaviorEvidence = Literal[
    "generic_open",
    "generic_search",
    "explicit_nomination",
    "stack_trace",
    "failing_test_output",
    "tool_trace",
    "material_edit",
    "diff_header",
    "command_output",
]

STRONG_AGENT_EVIDENCE: set[str] = {
    "stack_trace",
    "failing_test_output",
    "tool_trace",
    "material_edit",
    "diff_header",
    "command_output",
}
GENERIC_AGENT_EVIDENCE: set[str] = {
    "generic_open",
    "generic_search",
    "explicit_nomination",
}

SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".clj",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".m",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
}
IMPLEMENTATION_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/.*\.(py|js|jsx|ts|tsx|java|go|rb|rs)$"),
    re.compile(r"(^|/)test_.*\.(py|js|jsx|ts|tsx|java|go|rb|rs)$"),
    re.compile(r"(^|/).*\.(test|spec)\.(js|jsx|ts|tsx)$"),
)
DOC_CONFIG_DATA_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csv",
    ".ini",
    ".json",
    ".jsonl",
    ".lock",
    ".md",
    ".rst",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
DOC_CONFIG_DATA_NAMES = {
    "dockerfile",
    "makefile",
    "requirements.txt",
    "setup.cfg",
}

PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?(?:/?[\w.-]+/)+[\w.@+-]+\.[A-Za-z0-9_+-]+)"
)


@dataclass(frozen=True)
class AgentCandidate:
    path: str
    score: float
    evidence: BehaviorEvidence


@dataclass(frozen=True)
class GovernorCandidate:
    path: str
    score: float
    gt_score: float
    agent_score: float
    source: str
    entered_via: str = ""
    min_path_length_from_anchor: int = 999
    components: dict[str, float] = field(default_factory=dict)
    expansion_source: str | None = None


@dataclass(frozen=True)
class GovernorResult:
    active_set: list[GovernorCandidate]
    expanded: bool
    expansion_reason: str | None
    expansion_added: list[str]
    dumb_bounded_union: list[str]


def normalize_path(path: str) -> str:
    p = path.replace("\\", "/").strip().strip("`'\"),;:")
    p = re.sub(r"^[A-Za-z]:", "", p)
    p = p.removeprefix("/testbed/").removeprefix("testbed/")
    p = p.removeprefix("./").lstrip("/")
    return p


def is_implementation_test_file(path: str) -> bool:
    p = normalize_path(path).lower()
    return any(pattern.search(p) for pattern in IMPLEMENTATION_TEST_PATTERNS)


def is_source_code_file(path: str) -> bool:
    p = normalize_path(path).lower()
    return PurePosixPath(p).suffix in SOURCE_EXTENSIONS


def is_docs_config_data_or_fixture(path: str) -> bool:
    p = normalize_path(path).lower()
    name = PurePosixPath(p).name
    if name in DOC_CONFIG_DATA_NAMES:
        return True
    if "/fixtures/" in f"/{p}/" or "/testdata/" in f"/{p}/":
        return True
    return PurePosixPath(p).suffix in DOC_CONFIG_DATA_EXTENSIONS


def agent_path_allowed(path: str, evidence: str) -> bool:
    """Apply the corrected code-first behavioral extraction rule."""
    path = normalize_path(path)
    if not path:
        return False
    if evidence in STRONG_AGENT_EVIDENCE:
        return is_source_code_file(path) or is_docs_config_data_or_fixture(path)
    if evidence in GENERIC_AGENT_EVIDENCE:
        return is_source_code_file(path) or is_implementation_test_file(path)
    return False


def extract_agent_candidates(
    text: str,
    *,
    evidence: BehaviorEvidence,
    score: float = 1.0,
) -> list[AgentCandidate]:
    """Extract behavioral candidates from one evidence-bearing text block.

    Loose path extraction is deliberately narrow.  Docs/config/data/fixture files
    are emitted only for strong evidence types.
    """
    out: list[AgentCandidate] = []
    seen: set[str] = set()
    for match in PATH_RE.finditer(text or ""):
        path = normalize_path(match.group("path"))
        if path in seen or not agent_path_allowed(path, evidence):
            continue
        seen.add(path)
        out.append(AgentCandidate(path=path, score=max(0.0, min(1.0, score)), evidence=evidence))
    return out


def _gt_component(record: dict[str, Any], name: str) -> float:
    comps = record.get("components") or {}
    try:
        return float(comps.get(name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _gt_score(record: dict[str, Any]) -> float:
    try:
        return float(record.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _structurally_eligible(record: dict[str, Any]) -> bool:
    """Shared GT rescue eligibility used for initial rescue and expansion."""
    entered_via = str(record.get("entered_via") or "")
    reach = _gt_component(record, "reach")
    anchor_prox = _gt_component(record, "anchor_prox")
    try:
        path_len = int(record.get("min_path_length_from_anchor", 999) or 999)
    except (TypeError, ValueError):
        path_len = 999

    if not (entered_via in {"graph_rescue", "both"} or anchor_prox >= 0.333):
        return False
    if path_len > 2:
        return False
    if not (reach >= 0.35 or anchor_prox >= 0.333):
        return False
    if reach == 0 and anchor_prox == 0 and entered_via == "semantic_seed":
        return False
    return True


def _agent_scores(agent_candidates: Iterable[AgentCandidate]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for cand in agent_candidates:
        path = normalize_path(cand.path)
        if not agent_path_allowed(path, cand.evidence):
            continue
        scores[path] = max(scores.get(path, 0.0), max(0.0, min(1.0, cand.score)))
    return scores


def _rank_candidates(
    gt_ranked: list[dict[str, Any]],
    agent_candidates: Iterable[AgentCandidate],
    *,
    limit: int,
) -> list[GovernorCandidate]:
    gt_by_path = {normalize_path(str(rec.get("path", ""))): rec for rec in gt_ranked}
    raw_gt_scores = {
        path: max(0.0, min(1.0, _gt_score(rec))) for path, rec in gt_by_path.items() if path
    }
    agent = _agent_scores(agent_candidates)
    paths = set(raw_gt_scores) | set(agent)

    ranked: list[GovernorCandidate] = []
    for path in paths:
        rec = gt_by_path.get(path, {})
        g = raw_gt_scores.get(path, 0.0)
        a = agent.get(path, 0.0)
        source = "both" if g and a else ("gt" if g else "agent")
        blended = (0.55 * g) + (0.45 * a)
        score = max(g, a, blended)
        ranked.append(
            GovernorCandidate(
                path=path,
                score=round(score, 6),
                gt_score=round(g, 6),
                agent_score=round(a, 6),
                source=source,
                entered_via=str(rec.get("entered_via") or ""),
                min_path_length_from_anchor=int(rec.get("min_path_length_from_anchor", 999) or 999),
                components=dict(rec.get("components") or {}),
            )
        )
    ranked.sort(key=lambda c: (c.score, c.gt_score, c.agent_score, c.path), reverse=True)
    return ranked[:limit]


def _should_expand(
    active: list[GovernorCandidate],
    *,
    gt_initial: set[str],
) -> tuple[bool, str | None]:
    if not active:
        return False, None
    top_scores = [c.score for c in active[:3]]
    if max(top_scores) < 0.60:
        return True, "low_top_score"
    if len(top_scores) >= 3 and top_scores[0] - top_scores[2] <= 0.08:
        return True, "top3_close"
    top_agent = {c.path for c in active[:3] if c.agent_score > 0}
    if (
        top_agent
        and gt_initial
        and top_agent.isdisjoint(gt_initial)
        and max(c.agent_score for c in active[:3]) >= 0.7
        and max(c.gt_score for c in active[:3]) >= 0.7
    ):
        return True, "high_support_zero_overlap"
    return False, None


def _graph_neighbors(path: str, graph_db: str) -> list[tuple[str, float]]:
    if not graph_db:
        return []
    conn = sqlite3.connect(graph_db)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT n2.file_path, MAX(COALESCE(e.confidence, 0.5))
            FROM edges e
            JOIN nodes n1 ON e.source_id = n1.id
            JOIN nodes n2 ON e.target_id = n2.id
            WHERE n1.file_path = ?
              AND n2.file_path IS NOT NULL
              AND n1.file_path != n2.file_path
            GROUP BY n2.file_path
            """,
            (path,),
        ).fetchall()
    finally:
        conn.close()
    return [(normalize_path(dst), float(conf)) for dst, conf in rows]


def _trace_probe_candidates(early_trace_text: str, active_top3: set[str], slots: int) -> list[str]:
    if slots <= 0 or not early_trace_text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    strong = extract_agent_candidates(
        early_trace_text,
        evidence="failing_test_output",
        score=1.0,
    )
    for cand in strong:
        if cand.path in seen or cand.path in active_top3:
            continue
        seen.add(cand.path)
        out.append(cand.path)
        if len(out) >= slots:
            break
    return out


def dumb_bounded_union(
    gt_ranked: list[dict[str, Any]],
    agent_candidates: Iterable[AgentCandidate],
    *,
    gt_k: int = 3,
    agent_k: int = 3,
    ceiling: int = 7,
) -> list[str]:
    paths: list[str] = []
    for rec in gt_ranked[:gt_k]:
        path = normalize_path(str(rec.get("path", "")))
        if path and path not in paths:
            paths.append(path)
    for path, _score in sorted(_agent_scores(agent_candidates).items(), key=lambda x: x[1], reverse=True):
        if path not in paths:
            paths.append(path)
        if len(paths) >= gt_k + agent_k:
            break
    return paths[:ceiling]


def govern(
    gt_ranked: list[dict[str, Any]],
    agent_candidates: Iterable[AgentCandidate],
    *,
    graph_db: str | None = None,
    early_trace_text: str = "",
    preferred_max: int = 5,
    hard_ceiling: int = 7,
) -> GovernorResult:
    """Run one bounded v8.0 arbitration pass, optional expansion, then stop."""
    agent_list = list(agent_candidates)
    preferred_max = min(preferred_max, hard_ceiling)
    initial = _rank_candidates(gt_ranked, agent_list, limit=preferred_max)
    gt_initial = {normalize_path(str(rec.get("path", ""))) for rec in gt_ranked[:3]}
    should_expand, reason = _should_expand(initial, gt_initial=gt_initial)
    additions: list[GovernorCandidate] = []

    gt_by_path = {normalize_path(str(rec.get("path", ""))): rec for rec in gt_ranked}
    active_paths = {c.path for c in initial}

    if should_expand:
        slots = min(2, hard_ceiling - len(initial))
        for rec in gt_ranked:
            if slots <= 0:
                break
            path = normalize_path(str(rec.get("path", "")))
            if not path or path in active_paths or not _structurally_eligible(rec):
                continue
            additions.append(
                GovernorCandidate(
                    path=path,
                    score=round(_gt_score(rec), 6),
                    gt_score=1.0,
                    agent_score=0.0,
                    source="gt",
                    entered_via=str(rec.get("entered_via") or ""),
                    min_path_length_from_anchor=int(rec.get("min_path_length_from_anchor", 999) or 999),
                    components=dict(rec.get("components") or {}),
                    expansion_source="unused_gt_structural_rescue",
                )
            )
            active_paths.add(path)
            slots -= 1

        if slots > 0 and initial and graph_db:
            top_path = initial[0].path
            neighbors = []
            for dst, conf in _graph_neighbors(top_path, graph_db):
                rec = gt_by_path.get(dst)
                if conf < 0.5 or dst in active_paths:
                    continue
                if rec:
                    if not _structurally_eligible(rec):
                        continue
                    candidate_rec = rec
                else:
                    candidate_rec = {
                        "path": dst,
                        "score": conf,
                        "entered_via": "graph_rescue",
                        "min_path_length_from_anchor": 1,
                        "components": {"reach": conf, "anchor_prox": 0.0},
                    }
                neighbors.append((dst, conf, candidate_rec))
            neighbors.sort(key=lambda item: (_gt_score(item[2]), item[1]), reverse=True)
            for dst, _conf, rec in neighbors[:slots]:
                additions.append(
                    GovernorCandidate(
                        path=dst,
                        score=round(_gt_score(rec), 6),
                        gt_score=1.0,
                        agent_score=0.0,
                        source="gt",
                        entered_via=str(rec.get("entered_via") or ""),
                        min_path_length_from_anchor=1,
                        components=dict(rec.get("components") or {}),
                        expansion_source="graph_ring",
                    )
                )
                active_paths.add(dst)
                slots -= 1

        if not additions:
            top3 = {c.path for c in initial[:3]}
            for path in _trace_probe_candidates(early_trace_text, top3, min(2, hard_ceiling - len(initial))):
                additions.append(
                    GovernorCandidate(
                        path=path,
                        score=0.0,
                        gt_score=0.0,
                        agent_score=1.0,
                        source="agent",
                        expansion_source="targeted_trace_probe",
                    )
                )

    reranked = _rank_candidates(
        gt_ranked,
        [
            *agent_list,
            *(AgentCandidate(path=c.path, score=max(c.agent_score, 0.01), evidence="tool_trace") for c in additions),
        ],
        limit=hard_ceiling,
    )
    expansion_by_path = {c.path: c for c in additions}
    final: list[GovernorCandidate] = []
    for cand in reranked:
        final.append(expansion_by_path.get(cand.path, cand))
    final = final[:hard_ceiling]

    return GovernorResult(
        active_set=final,
        expanded=bool(additions),
        expansion_reason=reason if additions else None,
        expansion_added=[c.path for c in additions],
        dumb_bounded_union=dumb_bounded_union(gt_ranked, agent_list, ceiling=hard_ceiling),
    )


def includes_gold(paths: Iterable[str], gold_files: Iterable[str]) -> bool:
    active = {normalize_path(p) for p in paths}
    return bool(active & {normalize_path(g) for g in gold_files})


def pilot_stop_condition(
    task_rows: list[dict[str, Any]],
    *,
    max_worse_tasks: int = 1,
    min_mean_file_count_advantage: float = 0.5,
) -> dict[str, Any]:
    """Evaluate the corrected dumb-union stop condition for the 10-task pilot."""
    n = len(task_rows)
    worse = 0
    strict_better = 0
    gov_counts: list[int] = []
    dumb_counts: list[int] = []
    for row in task_rows:
        gold = row.get("gold_files") or []
        gov_paths = row.get("governor_files") or row.get("active_set") or []
        dumb_paths = row.get("dumb_union_files") or row.get("dumb_bounded_union") or []
        gov_hit = includes_gold(gov_paths, gold)
        dumb_hit = includes_gold(dumb_paths, gold)
        worse += int(dumb_hit and not gov_hit)
        strict_better += int(gov_hit and not dumb_hit)
        gov_counts.append(len(gov_paths))
        dumb_counts.append(len(dumb_paths))

    mean_gov = sum(gov_counts) / n if n else 0.0
    mean_dumb = sum(dumb_counts) / n if n else 0.0
    file_advantage = mean_dumb - mean_gov
    equal_gold_inclusion = worse == 0 and strict_better == 0
    fail = worse > max_worse_tasks or (
        equal_gold_inclusion
        and file_advantage < min_mean_file_count_advantage
        and strict_better == 0
    )
    return {
        "PASS": not fail,
        "worse_than_dumb_count": worse,
        "strictly_better_count": strict_better,
        "mean_governor_file_count": round(mean_gov, 4),
        "mean_dumb_union_file_count": round(mean_dumb, 4),
        "mean_file_count_advantage": round(file_advantage, 4),
        "n": n,
    }

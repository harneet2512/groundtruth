"""GT pre-task brief v5 — orchestrator.

Single public entry point: :func:`generate_brief`. Wires the five
deterministic modules in sequence, computes the candidate set, renders
the brief, and writes the per-task telemetry record.

No LLM calls. No HTTP. Graph + tree-sitter (via gt-index, upstream) +
regex + git only.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from groundtruth.pretask.anchors import IssueAnchors, extract_issue_anchors
from groundtruth.pretask.hybrid import (
    direct_path_hits,
    lexical_file_search,
    ppr_hits,
    reciprocal_rank_fusion,
    repository_memory_search,
    stack_frame_hits,
)
from groundtruth.pretask.ppr import (
    PPRResult,
    aggregate_scores_by_file,
    personalized_pagerank,
)
from groundtruth.pretask.recency import recent_commit_weight
from groundtruth.pretask.render import Candidate, collect_rationale_tags, render_brief
from groundtruth.pretask.telemetry import (
    TelemetryRecord,
    empty_anchors_block,
    empty_hybrid_block,
    empty_input_block,
    empty_ppr_block,
    empty_recent_block,
    empty_render_block,
    empty_v7_cochange_block,
    empty_v7_constraints_block,
    empty_v7_contract_block,
    empty_traces_block,
    utc_timestamp,
    write_record,
)
from groundtruth.pretask.traces import StackFrame, parse_stack_traces

# Hard caps from arch_update.md §10 open-decisions (defaults).
DEFAULT_MAX_FILES = 5
EDGE_CONF_MIN = 0.5
RECENCY_BOOST_MAX = 1.5  # multiplicative cap


@dataclass
class BriefResult:
    """Returned by :func:`generate_brief` when ``return_telemetry=True``.

    Attributes:
        brief: The rendered ``<gt-task-brief>`` block.
        telemetry: Telemetry record (already written to disk, also
            returned for tests / verifying agents).
        telemetry_path: Where the record was written, or ``None`` if the
            writer failed (best effort).
    """

    brief: str
    telemetry: TelemetryRecord
    telemetry_path: str | None = None
    candidates: list[Candidate] = field(default_factory=list)


# ----------------------------------------------------------------- helpers
def _seed_node_ids(
    graph_db: str | None,
    symbol_names: set[str],
    frames: list[StackFrame],
) -> tuple[set[int], list[str]]:
    """Resolve symbol names + stack-frame functions into node ids.

    Returns:
        (seed_ids, resolved_names) where ``resolved_names`` is the set of
        symbol surface forms that successfully mapped to at least one
        node id.
    """
    if not graph_db or not os.path.exists(graph_db):
        return set(), []

    candidates_names: set[str] = set(symbol_names)
    for fr in frames:
        if fr.func:
            candidates_names.add(fr.func)
    if not candidates_names:
        return set(), []

    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return set(), []

    seeds: set[int] = set()
    resolved: list[str] = []
    try:
        placeholders = ",".join("?" for _ in candidates_names)
        cursor = conn.execute(
            f"SELECT id, name FROM nodes WHERE name IN ({placeholders})",
            tuple(candidates_names),
        )
        for nid, name in cursor.fetchall():
            seeds.add(nid)
            if name not in resolved:
                resolved.append(name)
    except sqlite3.Error:
        pass
    finally:
        conn.close()

    return seeds, resolved


def _build_provenance(
    file_scores: dict[str, tuple[float, int]],
    anchors: IssueAnchors,
    frames: list[StackFrame],
    recency_map: dict[str, float],
    graph_db: str | None,
) -> dict[str, list[tuple[str, str]]]:
    """Assign rationale tags to each candidate file.

    Tag rules (from arch_update.md §2 Module 5):
        - ``issue-symbol`` if the file contains a node whose name is in
          ``anchors.symbols``.
        - ``stack-trace-frame`` if a parsed frame's path matches the
          file (suffix match).
        - ``graph-neighbor`` if the file came through PPR but did not
          earn one of the two stronger tags above.
        - ``test-of-affected-class`` if the file is a test AND it
          imports an anchor symbol — this is a heuristic check.
        - ``recent-edit`` if the file appears in the recency map AND it
          earned no other tag.
    """
    provenance: dict[str, list[tuple[str, str]]] = defaultdict(list)

    # Tag 1: issue-symbol (per file, list nodes by name).
    if graph_db and anchors.symbols and file_scores:
        try:
            conn = sqlite3.connect(graph_db)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                placeholders = ",".join("?" for _ in anchors.symbols)
                cursor = conn.execute(
                    f"SELECT DISTINCT file_path, name FROM nodes "
                    f"WHERE name IN ({placeholders})",
                    tuple(anchors.symbols),
                )
                for fpath, name in cursor.fetchall():
                    if fpath in file_scores:
                        provenance[fpath].append(("issue-symbol", name))
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    # Tag 2: stack-trace-frame (suffix match against frame.file).
    for fr in frames:
        f_norm = fr.file.replace("\\", "/")
        for fpath in file_scores:
            fp_norm = fpath.replace("\\", "/")
            if fp_norm.endswith(f_norm) or f_norm.endswith(fp_norm):
                provenance[fpath].append(
                    ("stack-trace-frame", f"line {fr.line}")
                )
                break

    # Tag 3: graph-neighbor — applied to files that earned NO stronger tag.
    if anchors.symbols:
        seed_label = ", ".join(sorted(anchors.symbols)[:2])
        for fpath in file_scores:
            if not provenance[fpath]:
                provenance[fpath].append(("graph-neighbor", seed_label))

    # Tag 4: test-of-affected-class — a test file that pulls in an anchor.
    # Heuristic: file_path contains "test" AND graph has an edge from the
    # file's nodes pointing to an anchor symbol.
    if anchors.symbols and graph_db:
        try:
            conn = sqlite3.connect(graph_db)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                placeholders = ",".join("?" for _ in anchors.symbols)
                cursor = conn.execute(
                    f"SELECT DISTINCT e.source_file FROM edges e "
                    f"JOIN nodes n ON e.target_id = n.id "
                    f"WHERE n.name IN ({placeholders}) "
                    f"AND e.source_file IS NOT NULL",
                    tuple(anchors.symbols),
                )
                rows = [r[0] for r in cursor.fetchall()]
                for fpath in rows:
                    if not fpath:
                        continue
                    fp_norm = fpath.replace("\\", "/").lower()
                    if (
                        ("test" in fp_norm or fp_norm.endswith("_test.go"))
                        and fpath in file_scores
                    ):
                        provenance[fpath].append(
                            ("test-of-affected-class", "")
                        )
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    # Tag 5: recent-edit — only if file has no other tag yet.
    for fpath, weight in recency_map.items():
        if fpath in file_scores and not provenance[fpath]:
            provenance[fpath].append(
                ("recent-edit", f"weight {weight:.2f}")
            )

    return dict(provenance)


def _is_test_file(file_path: str) -> bool:
    """Cheap test-file heuristic — file path contains ``test``."""
    fp = file_path.replace("\\", "/").lower()
    return (
        "/tests/" in fp
        or "/test/" in fp
        or fp.startswith("tests/")
        or fp.startswith("test/")
        or "test_" in os.path.basename(fp)
        or fp.endswith("_test.py")
        or fp.endswith("_test.go")
    )


def _graph_db_stats(graph_db: str) -> dict[str, int]:
    """Cheap node/edge count + DB size for the input telemetry block."""
    out = {"graph_db_size_kb": 0, "graph_node_count": 0, "graph_edge_count": 0}
    try:
        out["graph_db_size_kb"] = max(0, os.path.getsize(graph_db) // 1024)
    except OSError:
        return out
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return out
    try:
        for sql, key in (
            ("SELECT COUNT(*) FROM nodes", "graph_node_count"),
            ("SELECT COUNT(*) FROM edges", "graph_edge_count"),
        ):
            try:
                row = conn.execute(sql).fetchone()
                out[key] = int(row[0]) if row else 0
            except sqlite3.Error:
                pass
    finally:
        conn.close()
    return out


# ----------------------------------------------------------------- entrypoint
def generate_brief(
    issue_text: str,
    repo_root: str,
    graph_db: str | None,
    *,
    task_id: str = "unknown",
    max_files: int = DEFAULT_MAX_FILES,
    log_dir: str | None = None,
    return_telemetry: bool = False,
    write_telemetry: bool = True,
) -> str | BriefResult:
    """Run the v5 deterministic localization pipeline end-to-end.

    Args:
        issue_text: Raw issue body.
        repo_root: Filesystem path to the target repository (for stack
            trace in-repo filtering and ``git log``).
        graph_db: Path to graph.db. ``None`` or missing → pipeline still
            runs but PPR / cross-check stages will short-circuit.
        task_id: Stable id used in the telemetry filename.
        max_files: Max candidate files in the rendered brief (5 default).
        log_dir: Override ``$GT_LOG_DIR`` for this call.
        return_telemetry: When True, return a :class:`BriefResult`
            instead of just the brief string. The brief string return
            shape preserves backwards compatibility with v4 callers.
        write_telemetry: When False, skip the JSONL write. v7 uses this
            to call the v6 localizer internally and write one v7 record.

    Returns:
        The ``<gt-task-brief>`` block, or a :class:`BriefResult` when
        ``return_telemetry=True``.
    """
    t_total_start = time.perf_counter()

    record = TelemetryRecord(
        task_id=task_id,
        timestamp=utc_timestamp(),
        input=empty_input_block(issue_text, repo_root, graph_db or ""),
        module_1_anchors=empty_anchors_block(),
        module_2_traces=empty_traces_block(),
        module_3_ppr=empty_ppr_block(),
        module_4_recent=empty_recent_block(),
        module_6_hybrid=empty_hybrid_block(),
        module_7_cochange=empty_v7_cochange_block(),
        module_7_contract=empty_v7_contract_block(),
        module_7_constraints=empty_v7_constraints_block(),
        module_5_render=empty_render_block(),
    )

    if graph_db and os.path.exists(graph_db):
        record.input.update(_graph_db_stats(graph_db))

    # ------------- Module 1: anchors -------------
    t0 = time.perf_counter()
    anchors = extract_issue_anchors(issue_text, graph_db)
    m1_ms = int((time.perf_counter() - t0) * 1000)
    record.module_1_anchors = {
        "wall_ms": m1_ms,
        "symbols_extracted_raw": sorted(anchors.symbols_pre_stopword),
        "symbols_after_stopword": sorted(anchors.symbols_raw),
        "symbols_resolved_in_graph": sorted(anchors.symbols),
        "paths_extracted": sorted(anchors.paths),
        "test_names_extracted": sorted(anchors.test_names),
    }

    # ------------- Module 2: stack traces -------------
    t0 = time.perf_counter()
    raw_frames_count_box = {"n": 0}
    # We need raw count for telemetry but parse_stack_traces returns
    # only in-repo frames; do a quick pre-count by re-running each regex.
    # Cheaper: peek by counting unique matches via a proxy — but we'll
    # just re-import the registry.
    from groundtruth.pretask.traces import _frames_from_text  # type: ignore

    raw_all = _frames_from_text(issue_text or "")
    raw_frames_count_box["n"] = len(raw_all)
    frames = parse_stack_traces(issue_text or "", repo_root)
    m2_ms = int((time.perf_counter() - t0) * 1000)
    record.module_2_traces = {
        "wall_ms": m2_ms,
        "raw_frames_found": raw_frames_count_box["n"],
        "in_repo_frames": [
            {"file": fr.file, "line": fr.line, "func": fr.func, "lang": fr.lang}
            for fr in frames
        ],
        "deepest_frame": (
            {
                "file": frames[0].file,
                "line": frames[0].line,
                "func": frames[0].func,
                "lang": frames[0].lang,
            }
            if frames
            else None
        ),
    }

    # ------------- Module 3: PPR -------------
    seed_ids, resolved_names = _seed_node_ids(graph_db, anchors.symbols, frames)

    t0 = time.perf_counter()
    if graph_db and os.path.exists(graph_db):
        ppr_result: PPRResult = personalized_pagerank(
            graph_db,
            seed_ids,
            min_confidence=EDGE_CONF_MIN,
        )
        file_scores = aggregate_scores_by_file(ppr_result.node_scores, graph_db)
    else:
        ppr_result = PPRResult(node_scores={}, iterations_run=0, converged=True)
        file_scores = {}
    m3_ms = int((time.perf_counter() - t0) * 1000)

    top_10 = sorted(
        file_scores.items(), key=lambda kv: kv[1][0], reverse=True
    )[:10]
    record.module_3_ppr = {
        "wall_ms": m3_ms,
        "seed_node_count": len(seed_ids),
        "seed_node_names": sorted(resolved_names),
        "iterations_to_convergence": ppr_result.iterations_run,
        "top_10_files": [
            {"file": fpath, "score": round(score, 6), "node_count": n}
            for fpath, (score, n) in top_10
        ],
    }

    # ------------- Module 4: recency -------------
    t0 = time.perf_counter()
    recency_map, recency_total_lines = recent_commit_weight(repo_root)
    m4_ms = int((time.perf_counter() - t0) * 1000)

    # Apply boost (multiplicative, capped) — only files already in scores.
    boosts_applied: list[dict[str, Any]] = []
    boosted_scores: dict[str, tuple[float, int]] = {}
    for fpath, (score, n_nodes) in file_scores.items():
        weight = recency_map.get(fpath, 0.0)
        if weight > 0.0:
            multiplier = 1.0 + min(weight, 1.0) * (RECENCY_BOOST_MAX - 1.0)
            new_score = score * multiplier
            boosts_applied.append(
                {"file": fpath, "boost": round(multiplier, 3)}
            )
        else:
            new_score = score
        boosted_scores[fpath] = (new_score, n_nodes)

    record.module_4_recent = {
        "wall_ms": m4_ms,
        "git_log_entries": recency_total_lines,
        "files_with_recent_edits": len(recency_map),
        "boosts_applied": boosts_applied,
    }

    # ------------- Module 6: deterministic hybrid fusion -------------
    t0 = time.perf_counter()
    lexical_hits = lexical_file_search(
        issue_text or "", repo_root, graph_db, anchors, max_files=30
    )
    memory_hits, memory_stats = repository_memory_search(
        issue_text or "", repo_root, anchors, max_files=30
    )
    signal_lists = {
        "path-mention": direct_path_hits(anchors.paths),
        "stack-trace-frame": stack_frame_hits(frames),
        "graph-ppr": ppr_hits(boosted_scores),
        "lexical-match": lexical_hits,
        "repo-memory": memory_hits,
    }
    fused = reciprocal_rank_fusion(signal_lists, max_files=max(50, max_files))
    m6_ms = int((time.perf_counter() - t0) * 1000)

    fused_scores: dict[str, tuple[float, int]] = {
        hit.file: (hit.score, 1) for hit in fused
    }
    if not fused_scores:
        fused_scores = dict(boosted_scores)

    confidence_counts: defaultdict[str, int] = defaultdict(int)
    for hit in fused:
        confidence_counts[hit.confidence] += 1
    record.module_6_hybrid = {
        "wall_ms": m6_ms,
        "signal_counts": {name: len(hits) for name, hits in signal_lists.items()},
        "commits_examined": memory_stats.get("commits_examined", 0),
        "matching_commits": memory_stats.get("matching_commits", 0),
        "fused_candidates": [
            {
                "file": hit.file,
                "score": round(hit.score, 6),
                "confidence": hit.confidence,
                "signals": [
                    {"type": name, "detail": detail}
                    for name, detail in hit.signals
                ],
            }
            for hit in fused[:20]
        ],
        "confidence_counts": dict(confidence_counts),
    }

    # ------------- Module 5: render -------------
    t0 = time.perf_counter()
    provenance = _build_provenance(
        fused_scores, anchors, frames, recency_map, graph_db
    )
    for hit in fused:
        tags = provenance.setdefault(hit.file, [])
        tags.append(("confidence", hit.confidence))
        for signal, detail in hit.signals:
            if signal in {"path-mention", "stack-trace-frame"}:
                continue
            tags.append((signal, detail))

    candidates = [
        Candidate(
            file=fpath,
            score=score,
            tags=provenance.get(fpath, []),
            is_test=_is_test_file(fpath),
        )
        for fpath, (score, _n) in fused_scores.items()
    ]
    candidates.sort(key=lambda c: c.score, reverse=True)
    candidates_pre_filter = len(candidates)

    # Special handling: if we have NO graph signal at all but we DO have
    # explicit path mentions in the issue body, surface them. This
    # protects against tasks where the agent dropped the gold path in
    # backticks but no symbol resolved.
    if not candidates and anchors.paths:
        for p in sorted(anchors.paths):
            candidates.append(
                Candidate(
                    file=p,
                    score=0.0,
                    tags=[("issue-symbol", "path-mention")],
                    is_test=_is_test_file(p),
                )
            )

    # Same fallback: stack-trace frames if PPR is empty.
    if not candidates and frames:
        for fr in frames[:max_files]:
            candidates.append(
                Candidate(
                    file=fr.file,
                    score=0.0,
                    tags=[("stack-trace-frame", f"line {fr.line}")],
                    is_test=_is_test_file(fr.file),
                )
            )

    rendered = render_brief(
        candidates, anchors=anchors, frames=frames, max_files=max_files
    )
    m5_ms = int((time.perf_counter() - t0) * 1000)

    in_brief = candidates[:max_files] if candidates else []
    record.module_5_render = {
        "wall_ms": m5_ms,
        "candidates_pre_filter": candidates_pre_filter,
        "candidates_in_brief": len(in_brief),
        "rationale_tags": collect_rationale_tags(in_brief),
        "brief_chars": len(rendered),
        "abstained": not bool(in_brief),
    }

    record.brief_text = rendered
    record.total_wall_ms = int((time.perf_counter() - t_total_start) * 1000)

    # ------------- Telemetry write (after render — clean timings) -------------
    written = write_record(record, log_dir=log_dir) if write_telemetry else None

    if return_telemetry:
        return BriefResult(
            brief=rendered,
            telemetry=record,
            telemetry_path=written,
            candidates=candidates,
        )
    return rendered

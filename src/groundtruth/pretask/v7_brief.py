"""GroundTruth v7 pre-task edit brief.

v6 localizes files. v7 turns those localized files into an edit plan:
candidate cluster, contract, and constraints. The pipeline remains fully
deterministic: no HTTP, no embeddings, no model calls.
"""

from __future__ import annotations

import time
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from groundtruth.pretask.anchors import extract_issue_anchors
from groundtruth.pretask.brief_v5 import BriefResult as V6BriefResult
from groundtruth.pretask.brief_v5 import generate_brief as generate_v6_brief
from groundtruth.pretask.cochange import (
    CochangeResult,
    cochange_cluster,
    cochange_telemetry,
)
from groundtruth.pretask.contract import (
    ContractResult,
    contract_telemetry,
    detect_test_layout,
    extract_contract,
)
from groundtruth.pretask.project_instructions import (
    ProjectInstructions,
    extract_project_instructions,
    project_instructions_telemetry,
)
from groundtruth.pretask.render import Candidate
from groundtruth.pretask.telemetry import TelemetryRecord, utc_timestamp, write_record
from groundtruth.pretask.v7_layers import (
    CallerEvidenceEntry,
    ContractFingerprint,
    FocusFunction,
    RecentEdit,
    collect_v7_layers,
)

VERSION = "v7.0"
DEFAULT_MAX_CLUSTER_FILES = 8
DEFAULT_MAX_AGENT_FILES = 3
MAX_AGENT_BRIEF_CHARS = 3500
HIGH_CONFIDENCE_MIN = 0.6


@dataclass
class V7BriefResult:
    """Returned by :func:`generate_brief` when ``return_telemetry=True``."""

    brief: str
    telemetry: TelemetryRecord
    telemetry_path: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    cluster_files: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    plan_path: str | None = None


def _candidate_files(base: V6BriefResult, max_files: int) -> list[str]:
    files: list[str] = []
    fused = base.telemetry.module_6_hybrid.get("fused_candidates", [])
    if isinstance(fused, list):
        for item in fused:
            if isinstance(item, dict) and item.get("file"):
                file_path = _normalize_path(str(item["file"]))
                if file_path and file_path not in files:
                    files.append(file_path)
            if len(files) >= max_files:
                break
    if files:
        return files
    for cand in base.candidates:
        cand_path = _normalize_path(cand.file or "")
        if cand_path and cand_path not in files:
            files.append(cand_path)
        if len(files) >= max_files:
            break
    return files


def _constraint_lines(
    test_layout: list[str],
    project_instructions: ProjectInstructions | None = None,
) -> list[str]:
    project_constraints = (
        list(project_instructions.rendered_constraints) if project_instructions is not None else []
    )
    lines = [
        "Edit existing files in CANDIDATE CLUSTER first.",
        (
            "Do not add throwaway scaffolding at the repo root: "
            "*_test.py / test_*.py / repro*.py, *_test.go / repro*.go, "
            "*.spec.{js,ts} / *.test.{js,ts}, *Test.java / Repro*.java, "
            "repro*.rs."
        ),
    ]
    lines.extend(project_constraints[:2])
    lines.append(
        "Do not edit vendor/, node_modules/, generated files, or lock files unless the issue explicitly asks."
    )
    if test_layout:
        lines.append(f"If adding tests, use existing layout: {', '.join(test_layout[:3])}.")
    else:
        lines.append("If adding tests, follow the nearest existing test layout.")
    return lines


def _constraints_telemetry(
    test_layout: list[str],
    wall_ms: int,
    project_instructions: ProjectInstructions | None = None,
) -> dict[str, Any]:
    from groundtruth.runtime.patch_auditor import ROOT_SCAFFOLD_PATTERNS

    return {
        "wall_ms": wall_ms,
        "enabled": True,
        "constraints": _constraint_lines(test_layout, project_instructions),
        "detected_test_layout": test_layout,
        "project_instruction_constraints": (
            list(project_instructions.rendered_constraints)
            if project_instructions is not None
            else []
        ),
        "project_instruction_sources": (
            list(project_instructions.selected_sources) if project_instructions is not None else []
        ),
        "scaffold_patterns": list(ROOT_SCAFFOLD_PATTERNS),
        "negative_space_patterns": [
            "vendor/",
            "node_modules/",
            "*.lock",
            "AUTO-GENERATED",
        ],
        "hook_warning_fired": False,
    }


_URL_SHAPED_RE = re.compile(r"^/?(?:https?:)?//|://")


def _is_url_shaped(path: str) -> bool:
    return bool(_URL_SHAPED_RE.search(path or ""))


def _norm(path: str) -> str:
    # Use prefix strip not lstrip("./") -- character-set strip collapses
    # paths like ../../auth.py to auth.py (basename collapse).
    cleaned = path.replace("\\", "/").strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _normalize_path(path: str) -> str | None:
    """Return repo-relative path or None if URL-shaped / empty."""
    if not path:
        return None
    if _is_url_shaped(path):
        return None
    norm = _norm(path)
    if not norm or _is_url_shaped(norm):
        return None
    return norm


def _expected_side_files(contract: ContractResult, repo_root: str) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = [
        {"path": _norm(path), "kind": "contract_test", "required": False}
        for path in contract.selected_test_files
    ]
    root = Path(repo_root)
    for name in (
        "CHANGELOG.md",
        "CHANGES.rst",
        "docs/changelog.rst",
        "CHANGELOG.rst",
        "HISTORY.md",
        "RELEASE_NOTES.md",
    ):
        if (root / name).exists():
            expected.append({"path": name, "kind": "changelog", "required": False})
            break
    for name in (
        "__init__.py",
        "src/__init__.py",
        "index.ts",
        "src/index.ts",
        "index.js",
        "src/index.js",
        "mod.rs",
        "src/lib.rs",
        "src/mod.rs",
    ):
        if (root / name).exists():
            expected.append({"path": name, "kind": "export_surface", "required": False})
    for name in ("py.typed", "types", "typings", "types.d.ts", "src/types.d.ts"):
        if (root / name).exists():
            expected.append({"path": name, "kind": "typing_surface", "required": False})
    for name in (
        "go.mod",
        "Cargo.toml",
        "package.json",
        "pom.xml",
        "build.gradle",
    ):
        if (root / name).exists():
            expected.append({"path": name, "kind": "manifest", "required": False})
            break
    return expected


def _implementation_pattern(cluster: CochangeResult, base_files: list[str]) -> list[str]:
    lines: list[str] = []
    sibling_dirs = sorted({_norm(Path(path).parent.as_posix()) for path in base_files if "/" in _norm(path)})
    if sibling_dirs:
        lines.append(f"Follow sibling implementations under {', '.join(sibling_dirs[:3])}.")
    cochanged = [hit for hit in cluster.hits if hit.file not in base_files]
    if cochanged:
        examples = ", ".join(hit.file for hit in cochanged[:3])
        lines.append(f"Recent bug-fix co-change points at {examples}.")
    if not lines:
        lines.append("Mirror the nearest existing implementation and test style.")
    return lines


def _is_test_path(path: str) -> bool:
    norm = _norm(path).lower()
    name = Path(norm).name
    return (
        norm.startswith("tests/")
        or "/tests/" in norm
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
    )


def _is_low_value_surface(path: str) -> bool:
    name = Path(_norm(path)).name
    return name in {"__init__.py", "index.ts", "index.js", "mod.rs", "lib.rs"}


def _agent_focus_files(
    cluster: CochangeResult,
    base_files: list[str],
    contract: ContractResult,
    *,
    max_files: int = DEFAULT_MAX_AGENT_FILES,
) -> list[dict[str, Any]]:
    """Return the compact ranked edit target list shown to the agent.

    The full candidate cluster remains in JSON telemetry. This surface is
    deliberately small because benchmark data showed broad v7 clusters diluted
    brief-to-edit overlap.
    """
    focus: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: str, reason: str) -> None:
        norm = _normalize_path(path)
        if not norm or norm in seen or len(focus) >= max_files:
            return
        seen.add(norm)
        focus.append({"file": norm, "rank": len(focus) + 1, "reason": reason})

    for path in base_files:
        if not _is_test_path(path) and not _is_low_value_surface(path):
            add(path, "primary source")

    for path in contract.selected_test_files:
        add(path, "contract test")

    for hit in cluster.hits:
        if hit.file in seen or _is_test_path(hit.file) or _is_low_value_surface(hit.file):
            continue
        if hit.file not in {_norm(p) for p in base_files}:
            add(hit.file, f"co-change {hit.count}x")

    if not focus:
        for path in base_files:
            add(path, "primary")
        for hit in cluster.hits:
            add(hit.file, "cluster")

    return focus


def _confidence(
    cluster: CochangeResult,
    contract: ContractResult,
    base_files: list[str],
    focus_files: list[dict[str, Any]] | None = None,
    top1_score: float = 0.0,
) -> float:
    # Continuous score in [0,1]: each signal contributes proportionally to its
    # strength (focus count, contract lines, cluster size, top-1 retrieval
    # score) instead of a fixed boolean step. Old formula saturated at 1.0
    # whenever all four flags fired -> hardcoded-feel.
    focus_count = len(focus_files or [])
    focus_term = min(focus_count / 3.0, 1.0) * 0.30
    contract_term = min(len(contract.contract_lines) / 3.0, 1.0) * 0.20
    cluster_term = min(len(cluster.hits) / 5.0, 1.0) * 0.20
    test_term = 0.10 if contract.selected_test_files else 0.0
    base_term = 0.05 if base_files else 0.0
    score_term = max(0.0, min(top1_score, 1.0)) * 0.15
    return round(min(focus_term + contract_term + cluster_term + test_term + base_term + score_term, 1.0), 2)


def _cluster_files(cluster: CochangeResult, base_files: list[str]) -> list[str]:
    files: list[str] = []
    for hit in cluster.hits:
        norm = _normalize_path(hit.file)
        if norm and norm not in files:
            files.append(norm)
    for file_path in base_files:
        norm = _normalize_path(file_path)
        if norm and norm not in files:
            files.append(norm)
    return files


def _write_plan_json(plan: dict[str, Any], log_dir: str | None, task_id: str) -> str | None:
    target_dir = Path(log_dir) if log_dir else Path("/tmp/gt_logs")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_id = task_id.replace("/", "_").replace("\\", "_") or "unknown"
        target = target_dir / f"{safe_id}_v7_plan.json"
        target.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
        return str(target)
    except (OSError, TypeError, ValueError):
        return None


def _render_contract_block(
    fingerprints: list[ContractFingerprint],
    *,
    max_per_fn: int = 3,
) -> list[str]:
    if not fingerprints:
        return []
    out = ["", "BEHAVIORAL CONTRACT:"]
    for fp in fingerprints:
        lines = fp.lines(max_lines=max_per_fn)
        if not lines:
            continue
        header = fp.function.name or _norm(fp.function.file_path)
        out.append(f"  {header}:")
        for line in lines:
            out.append(f"    - {line}")
    if len(out) == 2:
        return []
    return out


def _render_caller_block(entries: list[CallerEvidenceEntry]) -> list[str]:
    if not entries:
        return []
    out = ["", "CALLER EVIDENCE:"]
    for entry in entries:
        if not entry.callers:
            continue
        out.append(f"  {entry.function.name}:")
        for hit in entry.callers:
            location = f"{hit.caller_file}:{hit.call_line}" if hit.call_line else hit.caller_file
            if hit.call_text:
                out.append(f"    - {hit.caller_name} at {location}: {hit.call_text}")
            else:
                out.append(f"    - {hit.caller_name} at {location}")
    if len(out) == 2:
        return []
    return out


def _render_recent_edits_block(edits: list[RecentEdit]) -> list[str]:
    if not edits:
        return []
    out = ["", "RECENT EDITS:"]
    for edit in edits:
        header = edit.function.name or _norm(edit.function.file_path)
        out.append(f"  {header} ({edit.commit_hash}): {edit.commit_msg}")
        for line in edit.before:
            out.append(f"    before: {line}")
        for line in edit.after:
            out.append(f"    after:  {line}")
    if len(out) == 2:
        return []
    return out


def _render_v7(
    cluster: CochangeResult,
    contract: ContractResult,
    constraints: list[str],
    focus_files: list[dict[str, Any]],
    implementation_pattern: list[str],
    expected_side_files: list[dict[str, Any]],
    confidence: float = 1.0,
    contract_fingerprints: list[ContractFingerprint] | None = None,
    caller_evidence: list[CallerEvidenceEntry] | None = None,
    recent_edits: list[RecentEdit] | None = None,
) -> str:
    if not focus_files and not cluster.hits:
        return (
            "GT could not deterministically localize this issue. "
            "Recommend exploring from issue text directly."
        )

    visible_focus = focus_files[:DEFAULT_MAX_AGENT_FILES]
    allowed_paths = {
        _norm(str(item.get("file") or item.get("path") or ""))
        for item in visible_focus
        if item.get("file") or item.get("path")
    }
    is_high_confidence = confidence >= HIGH_CONFIDENCE_MIN
    if is_high_confidence:
        header = "GT deterministic edit plan (ranked):"
    else:
        header = f"GT low-confidence hints (conf={confidence:.2f}):"

    lines = [header]
    for item in visible_focus:
        lines.append(f"  {item['rank']}. {item['file']} [{item['reason']}]")

    hidden_count = max(0, len(cluster.hits) - len(visible_focus))
    if hidden_count:
        lines.append(f"  - {hidden_count} more candidates in telemetry")

    if contract.contract_lines or contract.selected_test_files:
        visible_tests = [_norm(path) for path in contract.selected_test_files if _norm(path) in allowed_paths]
        for test_file in visible_tests[:1]:
            lines.append(f"  - Test target: {test_file}")
        for contract_line in contract.contract_lines[:2]:
            lines.append(f"  - {_sanitize_brief_line(contract_line, allowed_paths)}")

    # Add other signals without headers if they exist
    for blk in (_render_contract_block(contract_fingerprints or []),
                _render_caller_block(caller_evidence or []),
                _render_recent_edits_block(recent_edits or [])):
        for ln in blk:
            if ln.strip(): lines.append(ln)

    if implementation_pattern:
        lines.append("  - Pattern: Mirror nearest style around ranked targets.")
    else:
        lines.append("  - Pattern: Mirror nearest implementation and test style.")

    visible_side_files = [
        item for item in expected_side_files if _norm(str(item.get("path") or "")) in allowed_paths
    ]
    if visible_side_files:
        for item in visible_side_files[:1]:
            required = "required" if item.get("required") else "if affected"
            lines.append(f"  - Side file: {item.get('path')} [{item.get('kind', 'side_file')}, {required}]")

    for line in constraints[:2]:
        lines.append(f"  - Constraint: {_sanitize_brief_line(line, allowed_paths)}")

    # Final density pass: remove empty lines and redundant whitespace
    dense = [ln.strip() for ln in lines if ln.strip()]
    rendered = "\n".join(dense)

    if len(rendered) <= MAX_AGENT_BRIEF_CHARS:
        return rendered

    # Curated compact: keep files (start) + contract + constraint (end)
    # per Lost-in-the-Middle (Liu et al. 2024) — LLMs attend to start+end
    compact = [
        header,
        *[f"  {item['rank']}. {item['file']} [{item['reason']}]" for item in visible_focus],
        *[f"  - {_sanitize_brief_line(line, allowed_paths)}" for line in contract.contract_lines[:2]],
        "  - Constraint: Edit existing ranked files first; do not create root-level repro/scaffold files.",
    ]
    return "\n".join(ln for ln in compact if ln.strip())


_FILE_MENTION_RE = re.compile(
    r"\b[\w./-]+\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|c|h|cc|cpp|hpp|rb|php|cs|md|rst|toml|json|yaml|yml)\b"
)


def _sanitize_brief_line(line: str, allowed_paths: set[str]) -> str:
    """Strip non-focus file paths while preserving the content around them.

    Replaces ``from <path>:``, ``in <path>:``, ``at <path>:`` and bare
    ``<path>:`` prefixes outright; falls back to deleting the path token alone.
    Glob-like patterns (``*``/``?`` -- including a glob char immediately before
    the match, e.g. ``*_test.py``) and entries already in ``allowed_paths`` are
    left intact.
    """
    text = str(line)
    for match in _FILE_MENTION_RE.findall(text):
        if "*" in match or "?" in match:
            continue
        if _norm(match) in allowed_paths:
            continue
        escaped = re.escape(match)
        # Skip if the match is preceded by a glob char (e.g. "*_test.py" /
        # "?est.py"). Without this guard, the inner regex eats "_test.py" /
        # "Test.java" out of "*_test.py" / "*Test.java" and leaves a bare "*".
        if re.search(rf"[*?]{escaped}", text):
            continue
        text = re.sub(rf"\s*\bfrom\s+{escaped}\b\s*:?\s*", " ", text)
        text = re.sub(rf"\s*\bin\s+{escaped}\b\s*:?\s*", " ", text)
        text = re.sub(rf"\s*\bat\s+{escaped}\b\s*:?\s*", " ", text)
        text = re.sub(rf"\b{escaped}\b\s*:\s*", "", text)
        text = re.sub(rf"\b{escaped}\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _copy_base_record(base: V6BriefResult, task_id: str) -> TelemetryRecord:
    old = base.telemetry
    return TelemetryRecord(
        task_id=task_id,
        timestamp=utc_timestamp(),
        version=VERSION,
        input=dict(old.input),
        module_1_anchors=dict(old.module_1_anchors),
        module_2_traces=dict(old.module_2_traces),
        module_3_ppr=dict(old.module_3_ppr),
        module_4_recent=dict(old.module_4_recent),
        module_6_hybrid=dict(old.module_6_hybrid),
        module_5_render=dict(old.module_5_render),
        total_wall_ms=old.total_wall_ms,
    )


def generate_brief(
    issue_text: str,
    repo_root: str,
    graph_db: str | None,
    *,
    task_id: str = "unknown",
    max_files: int = 5,
    max_cluster_files: int = DEFAULT_MAX_CLUSTER_FILES,
    log_dir: str | None = None,
    return_telemetry: bool = False,
) -> str | V7BriefResult:
    """Generate the v7 deterministic edit plan brief."""
    t_total_start = time.perf_counter()
    base = generate_v6_brief(
        issue_text,
        repo_root,
        graph_db,
        task_id=task_id,
        max_files=max_files,
        log_dir=log_dir,
        return_telemetry=True,
        write_telemetry=False,
    )
    assert isinstance(base, V6BriefResult)

    candidate_files = _candidate_files(base, max_files=max_files)

    t0 = time.perf_counter()
    cluster = cochange_cluster(
        repo_root,
        candidate_files,
        max_files=max_cluster_files,
    )
    cochange_ms = int((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    anchors = extract_issue_anchors(issue_text, graph_db)
    contract = extract_contract(
        issue_text,
        repo_root,
        graph_db,
        anchors,
        candidate_files,
    )
    contract_ms = int((time.perf_counter() - t0) * 1000)

    implementation_pattern = _implementation_pattern(cluster, candidate_files)
    expected_side_files = _expected_side_files(contract, repo_root)
    cluster_files = _cluster_files(cluster, candidate_files)
    focus_files = _agent_focus_files(cluster, candidate_files, contract)

    t0 = time.perf_counter()
    layer_focus_files = [
        _norm(str(item.get("file") or item.get("path") or ""))
        for item in focus_files[:DEFAULT_MAX_AGENT_FILES]
        if item.get("file") or item.get("path")
    ]
    layers = collect_v7_layers(graph_db, repo_root, layer_focus_files)
    layers_ms = int((time.perf_counter() - t0) * 1000)
    contract_fingerprints: list[ContractFingerprint] = layers.get("contract", [])
    caller_evidence: list[CallerEvidenceEntry] = layers.get("callers", [])
    recent_edits: list[RecentEdit] = layers.get("recent_edits", [])

    t0 = time.perf_counter()
    test_layout = detect_test_layout(repo_root)
    project_instructions = extract_project_instructions(
        repo_root,
        focus_files=focus_files,
        candidate_files=candidate_files,
    )
    constraints = _constraint_lines(test_layout, project_instructions)
    constraints_ms = int((time.perf_counter() - t0) * 1000)
    fused = base.telemetry.module_6_hybrid.get("fused_candidates", []) or []
    top1_score = 0.0
    if isinstance(fused, list) and fused and isinstance(fused[0], dict):
        try:
            top1_score = float(fused[0].get("score") or 0.0)
        except (TypeError, ValueError):
            top1_score = 0.0
    plan = {
        "version": VERSION,
        "task_id": task_id,
        "cluster_files": cluster_files,
        "agent_focus_files": focus_files,
        "contract_lines": list(contract.contract_lines),
        "constraints": constraints,
        "implementation_pattern": implementation_pattern,
        "expected_side_files": expected_side_files,
        "confidence": _confidence(
            cluster, contract, candidate_files,
            focus_files=focus_files, top1_score=top1_score,
        ),
        "abstain_reason": "" if cluster_files else "no_candidate_cluster",
    }

    rendered = _render_v7(
        cluster,
        contract,
        constraints,
        focus_files,
        implementation_pattern,
        expected_side_files,
        confidence=plan["confidence"],
        contract_fingerprints=contract_fingerprints,
        caller_evidence=caller_evidence,
        recent_edits=recent_edits,
    )
    record = _copy_base_record(base, task_id)
    record.module_7_cochange = cochange_telemetry(
        cluster, candidate_files, cochange_ms
    )
    record.module_7_contract = contract_telemetry(contract, contract_ms)
    record.module_7_constraints = _constraints_telemetry(
        test_layout, constraints_ms, project_instructions
    )
    record.module_5_render = dict(record.module_5_render)
    sections = [
        "candidate_cluster",
        "contract" if contract.contract_lines else "contract_empty",
    ]
    if contract_fingerprints:
        sections.append("behavioral_contract")
    if caller_evidence:
        sections.append("caller_evidence")
    if recent_edits:
        sections.append("recent_edits")
    sections.extend(["implementation_pattern", "expected_side_files", "constraints"])

    record.module_5_render.update(
        {
            "brief_chars": len(rendered),
            "candidates_in_brief": len(cluster.hits) or len(candidate_files),
            "agent_focus_count": len(focus_files),
            "full_cluster_count": len(cluster_files),
            "v7_sections": sections,
            "v7_layers_ms": layers_ms,
            "v7_layer_counts": {
                "behavioral_contract": len(contract_fingerprints),
                "caller_evidence": len(caller_evidence),
                "recent_edits": len(recent_edits),
            },
        }
    )
    record.gt_plan = plan
    record.brief_text = rendered
    record.total_wall_ms = int((time.perf_counter() - t_total_start) * 1000)
    plan_path = _write_plan_json(plan, log_dir, task_id)
    if log_dir is not None:
        from groundtruth.runtime.telemetry import append_block
        from groundtruth.runtime.plan_surface import usable_delivery_record

        append_block("gt_plan", plan, log_dir=log_dir, task_id=task_id)
        append_block(
            "gt_project_instructions",
            project_instructions_telemetry(project_instructions, constraints_ms),
            log_dir=log_dir,
            task_id=task_id,
        )
        append_block(
            "gt_usable_delivery",
            usable_delivery_record(
                transport_delivered=True,
                brief_chars=len(rendered),
                agent_focus_files=focus_files,
                brief_text=rendered,
                broad_full_plan_default=False,
            ),
            log_dir=log_dir,
            task_id=task_id,
        )
    written = write_record(record, log_dir=log_dir)

    if return_telemetry:
        return V7BriefResult(
            brief=rendered,
            telemetry=record,
            telemetry_path=written,
            candidates=base.candidates,
            cluster_files=cluster_files,
            plan=plan,
            plan_path=plan_path,
        )
    return rendered

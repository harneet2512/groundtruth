"""Deterministic project-instruction extraction for v7 briefs.

The extractor is intentionally language-neutral. It collects full evidence from
repo instruction files for telemetry, then returns only a tiny ranked constraint
set for agent-facing briefs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


INSTRUCTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
    "CONTRIBUTING.md",
)
README_FILES = ("README.md", "README.rst", "README.txt")
MAX_FILE_CHARS = 80_000


@dataclass(frozen=True)
class InstructionEvidence:
    path: str
    scope: str
    precedence: int
    source_kind: str
    constraints: tuple[str, ...] = field(default_factory=tuple)
    test_commands: tuple[str, ...] = field(default_factory=tuple)
    matched_terms: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProjectInstructions:
    evidence: tuple[InstructionEvidence, ...] = field(default_factory=tuple)
    rendered_constraints: tuple[str, ...] = field(default_factory=tuple)
    selected_sources: tuple[str, ...] = field(default_factory=tuple)
    extraction_mode: str = "none"
    abstain_reason: str = ""


def extract_project_instructions(
    repo_root: str,
    *,
    focus_files: list[dict[str, Any]] | None = None,
    candidate_files: list[str] | None = None,
    max_rendered_constraints: int = 2,
) -> ProjectInstructions:
    """Extract repo instructions with scoped precedence.

    Precedence:
    - nearest scoped instruction file for a focus/candidate path
    - repo-root instruction files
    - generic README test sections
    """
    root = Path(repo_root)
    if not root.exists():
        return ProjectInstructions(abstain_reason="repo_root_missing")

    relevant_paths = _relevant_paths(focus_files or [], candidate_files or [])
    evidence = _instruction_file_evidence(root, relevant_paths)
    evidence.extend(_readme_evidence(root))
    evidence = _dedupe_evidence(evidence)
    evidence.sort(key=lambda item: (-item.precedence, item.path))

    rendered: list[str] = []
    selected_sources: list[str] = []
    for item in evidence:
        for command in item.test_commands:
            line = f"Repo validation hint from {item.path}: {command}"
            if line not in rendered:
                rendered.append(line)
                selected_sources.append(item.path)
            if len(rendered) >= max_rendered_constraints:
                break
        if len(rendered) >= max_rendered_constraints:
            break
        for constraint in item.constraints:
            line = f"Repo instruction from {item.path}: {constraint}"
            if line not in rendered:
                rendered.append(line)
                selected_sources.append(item.path)
            if len(rendered) >= max_rendered_constraints:
                break
        if len(rendered) >= max_rendered_constraints:
            break

    if not evidence:
        return ProjectInstructions(abstain_reason="no_instruction_files")
    return ProjectInstructions(
        evidence=tuple(evidence),
        rendered_constraints=tuple(rendered),
        selected_sources=tuple(dict.fromkeys(selected_sources)),
        extraction_mode="scoped+root+readme",
        abstain_reason="" if rendered else "no_relevant_constraints",
    )


def project_instructions_telemetry(result: ProjectInstructions, wall_ms: int) -> dict[str, Any]:
    return {
        "wall_ms": wall_ms,
        "enabled": True,
        "extraction_mode": result.extraction_mode,
        "abstain_reason": result.abstain_reason,
        "selected_sources": list(result.selected_sources),
        "rendered_constraints": list(result.rendered_constraints),
        "evidence": [
            {
                "path": item.path,
                "scope": item.scope,
                "precedence": item.precedence,
                "source_kind": item.source_kind,
                "constraints": list(item.constraints),
                "test_commands": list(item.test_commands),
                "matched_terms": list(item.matched_terms),
            }
            for item in result.evidence
        ],
    }


def _relevant_paths(focus_files: list[dict[str, Any]], candidate_files: list[str]) -> list[str]:
    out: list[str] = []
    for item in focus_files:
        path = str(item.get("file") or item.get("path") or "").replace("\\", "/").lstrip("./")
        if path:
            out.append(path)
    for path in candidate_files:
        norm = str(path).replace("\\", "/").lstrip("./")
        if norm:
            out.append(norm)
    return list(dict.fromkeys(out))


def _instruction_file_evidence(root: Path, relevant_paths: list[str]) -> list[InstructionEvidence]:
    out: list[InstructionEvidence] = []
    candidate_dirs = _candidate_dirs(relevant_paths)
    for rel_dir in candidate_dirs:
        for name in INSTRUCTION_FILES:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            path = root / rel
            if not path.is_file():
                continue
            text = _read_text(path)
            constraints, commands, terms = _extract_lines(text)
            if constraints or commands:
                out.append(
                    InstructionEvidence(
                        path=rel.replace("\\", "/"),
                        scope=rel_dir or ".",
                        precedence=_precedence(rel_dir, name),
                        source_kind="instruction",
                        constraints=tuple(constraints),
                        test_commands=tuple(commands),
                        matched_terms=tuple(terms),
                    )
                )
    return out


def _readme_evidence(root: Path) -> list[InstructionEvidence]:
    out: list[InstructionEvidence] = []
    for name in README_FILES:
        path = root / name
        if not path.is_file():
            continue
        sections = _readme_test_sections(_read_text(path))
        constraints, commands, terms = _extract_lines(sections)
        if constraints or commands:
            out.append(
                InstructionEvidence(
                    path=name,
                    scope=".",
                    precedence=5,
                    source_kind="readme",
                    constraints=tuple(constraints),
                    test_commands=tuple(commands),
                    matched_terms=tuple(terms),
                )
            )
    return out


def _candidate_dirs(relevant_paths: list[str]) -> list[str]:
    dirs = {""}
    for rel in relevant_paths:
        parts = Path(rel).parts[:-1]
        for idx in range(len(parts)):
            dirs.add("/".join(parts[: idx + 1]))
    return sorted(dirs, key=lambda item: (-(item.count("/") + (1 if item else 0)), item))


def _precedence(rel_dir: str, name: str) -> int:
    depth = rel_dir.count("/") + (1 if rel_dir else 0)
    file_bonus = 4 if name == "AGENTS.md" else 3 if name == "CLAUDE.md" else 2
    return 20 + depth * 10 + file_bonus


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="ignore")[:MAX_FILE_CHARS]
    except OSError:
        return ""


def _extract_lines(text: str) -> tuple[list[str], list[str], list[str]]:
    constraints: list[str] = []
    commands: list[str] = []
    terms: list[str] = []
    for raw in text.splitlines():
        line = _clean_line(raw)
        if not line or len(line) > 220:
            continue
        low = line.lower()
        if _looks_like_command(line, low):
            commands.append(line)
            terms.append("test-command")
        elif _looks_like_constraint(low):
            constraints.append(line)
            terms.append("constraint")
        if len(constraints) >= 12 and len(commands) >= 8:
            break
    return _dedupe(constraints)[:12], _dedupe(commands)[:8], _dedupe(terms)


def _readme_test_sections(text: str) -> str:
    lines = text.splitlines()
    selected: list[str] = []
    active = False
    for raw in lines:
        stripped = raw.strip()
        if re.match(r"^#{1,6}\s+", stripped) or re.match(r"^[A-Za-z].+\n?[=-]+$", stripped):
            heading = stripped.lower()
            active = any(term in heading for term in ("test", "develop", "contribut", "run"))
            continue
        if active:
            selected.append(raw)
            if len(selected) > 120:
                break
    return "\n".join(selected)


def _clean_line(raw: str) -> str:
    line = raw.strip().strip("`")
    line = re.sub(r"^[-*+]\s+", "", line)
    line = re.sub(r"^\d+[.)]\s+", "", line)
    return " ".join(line.split())


def _looks_like_command(line: str, low: str) -> bool:
    command_terms = (
        "pytest",
        "tox",
        "nox",
        "npm test",
        "pnpm test",
        "yarn test",
        "go test",
        "cargo test",
        "mvn test",
        "gradle test",
        "ruff",
        "mypy",
    )
    return any(term in low for term in command_terms) and not low.startswith("do not")


def _looks_like_constraint(low: str) -> bool:
    starts = ("do not ", "don't ", "must ", "always ", "never ", "prefer ", "avoid ", "use ")
    contains = (
        "before submitting",
        "run tests",
        "style",
        "format",
        "lint",
        "generated",
        "do not edit",
        "do not modify",
        "contribution",
    )
    return low.startswith(starts) or any(term in low for term in contains)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _dedupe_evidence(evidence: list[InstructionEvidence]) -> list[InstructionEvidence]:
    seen: set[str] = set()
    out: list[InstructionEvidence] = []
    for item in evidence:
        if item.path in seen:
            continue
        seen.add(item.path)
        out.append(item)
    return out

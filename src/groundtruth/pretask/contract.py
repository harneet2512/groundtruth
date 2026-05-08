"""Deterministic test and issue contract extraction for v7 briefs."""

from __future__ import annotations

import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from groundtruth.pretask.anchors import IssueAnchors
from groundtruth.pretask.hybrid import query_terms


@dataclass(frozen=True)
class ContractResult:
    """Compact contract clues for a v7 edit brief."""

    selected_test_files: tuple[str, ...] = field(default_factory=tuple)
    test_files_considered: tuple[str, ...] = field(default_factory=tuple)
    contract_lines: tuple[str, ...] = field(default_factory=tuple)
    issue_calls: tuple[str, ...] = field(default_factory=tuple)
    extraction_mode: str = "none"
    abstain_reason: str = ""


_TEST_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java"}
_ASSERT_PATTERNS = (
    re.compile(r"^\s*assert\b.+"),
    re.compile(r".*\bpytest\.raises\b.+"),
    re.compile(r".*\bwith\s+pytest\.raises\b.+"),
    re.compile(r".*\bexpect\s*\(.+"),
    re.compile(r".*\brequire\..+"),
    re.compile(r".*\bassert\..+"),
    re.compile(r".*\bt\.Errorf\s*\(.+"),
)
_CODE_BLOCK_RE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.DOTALL)
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_\.]{2,})\s*\(")


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def is_test_file(path: str) -> bool:
    """Return True for common cross-language test file layouts."""
    p = _norm(path).lower()
    name = os.path.basename(p)
    return (
        p.startswith("tests/")
        or "/tests/" in p
        or p.startswith("test/")
        or "/test/" in p
        or "/__tests__/" in p
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.go")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
    )


def detect_test_layout(repo_root: str, *, max_dirs: int = 6) -> list[str]:
    """Detect where this repository usually stores tests."""
    if not repo_root:
        return []
    root = Path(repo_root)
    counts: Counter[str] = Counter()
    try:
        files = root.rglob("*")
        for path in files:
            if not path.is_file() or path.suffix.lower() not in _TEST_EXTS:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if not is_test_file(rel):
                continue
            parts = rel.split("/")
            if parts[0] in {"tests", "test"}:
                key = parts[0] if len(parts) <= 2 else "/".join(parts[:2])
            elif "__tests__" in parts:
                key = "/".join(parts[: parts.index("__tests__") + 1])
            else:
                key = os.path.dirname(rel) or "."
            counts[key] += 1
    except OSError:
        return []
    return [path for path, _count in counts.most_common(max_dirs)]


def _path_convention_tests(repo_root: str, candidate_files: list[str]) -> list[str]:
    root = Path(repo_root)
    out: list[str] = []
    for file_path in candidate_files:
        norm = _norm(file_path)
        stem = Path(norm).stem
        suffix = Path(norm).suffix
        guesses = [
            f"tests/test_{stem}{suffix}",
            f"test/test_{stem}{suffix}",
            f"tests/{stem}_test{suffix}",
            f"{Path(norm).parent.as_posix()}/test_{stem}{suffix}",
            f"{Path(norm).parent.as_posix()}/{stem}_test{suffix}",
        ]
        for guess in guesses:
            if guess not in out and (root / guess).exists():
                out.append(guess)
    return out


def _graph_test_files(graph_db: str | None, candidate_files: list[str]) -> list[str]:
    if not graph_db or not os.path.exists(graph_db) or not candidate_files:
        return []
    try:
        conn = sqlite3.connect(graph_db)
    except sqlite3.Error:
        return []
    try:
        placeholders = ",".join("?" for _ in candidate_files)
        sql = (
            "SELECT DISTINCT e.source_file FROM edges e "
            "JOIN nodes n ON e.target_id = n.id "
            f"WHERE n.file_path IN ({placeholders}) "
            "AND e.source_file IS NOT NULL "
            "AND e.confidence >= 0.5"
        )
        rows = conn.execute(sql, tuple(candidate_files)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [_norm(str(row[0])) for row in rows if row and row[0] and is_test_file(str(row[0]))]


def _all_tests(repo_root: str, *, max_files: int = 300) -> list[str]:
    root = Path(repo_root)
    out: list[str] = []
    try:
        for path in root.rglob("*"):
            if len(out) >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in _TEST_EXTS:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if is_test_file(rel):
                out.append(rel)
    except OSError:
        return []
    return sorted(out)


def _lexical_tests(
    issue_text: str,
    anchors: IssueAnchors,
    repo_root: str,
    candidate_files: list[str],
    *,
    max_files: int = 4,
) -> list[str]:
    tests = _all_tests(repo_root)
    if not tests:
        return []
    terms = set(query_terms(issue_text, anchors))
    for file_path in candidate_files:
        terms.add(Path(file_path).stem.lower())
    if not terms:
        return []
    scored: list[tuple[float, str]] = []
    root = Path(repo_root)
    for rel in tests:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")[:120_000]
        except OSError:
            continue
        low = text.lower()
        score = sum(1.0 for term in terms if term in low)
        if score:
            scored.append((score, rel))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [rel for _score, rel in scored[:max_files]]


def _extract_contract_lines(repo_root: str, test_files: list[str], *, max_lines: int) -> list[str]:
    lines: list[str] = []
    root = Path(repo_root)
    for rel in test_files:
        try:
            text_lines = (root / rel).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in text_lines:
            stripped = line.strip()
            if not stripped or len(stripped) > 180:
                continue
            if any(pattern.match(line) for pattern in _ASSERT_PATTERNS):
                rendered = f"{rel}: {stripped}"
                if rendered not in lines:
                    lines.append(rendered)
            if len(lines) >= max_lines:
                return lines
    return lines


def extract_issue_calls(issue_text: str, *, max_calls: int = 8) -> list[str]:
    """Extract function calls from fenced issue reproduction blocks."""
    seen: set[str] = set()
    out: list[str] = []
    for block in _CODE_BLOCK_RE.findall(issue_text or ""):
        for match in _CALL_RE.finditer(block):
            call = match.group(1)
            if call in seen or call.split(".")[-1] in {"print", "assert", "range"}:
                continue
            seen.add(call)
            out.append(f"{call}(...)")
            if len(out) >= max_calls:
                return out
    return out


def extract_contract(
    issue_text: str,
    repo_root: str,
    graph_db: str | None,
    anchors: IssueAnchors,
    candidate_files: list[str],
    *,
    max_tests: int = 4,
    max_contract_lines: int = 6,
) -> ContractResult:
    """Find nearby tests and render assertions/calls as a compact contract."""
    candidates = [_norm(p) for p in candidate_files if p]
    considered: list[str] = []
    modes: list[str] = []

    path_hits = _path_convention_tests(repo_root, candidates)
    if path_hits:
        considered.extend(path_hits)
        modes.append("path-convention")

    graph_hits = _graph_test_files(graph_db, candidates)
    if graph_hits:
        considered.extend(graph_hits)
        modes.append("graph-import")

    lexical_hits = _lexical_tests(issue_text, anchors, repo_root, candidates)
    if lexical_hits:
        considered.extend(lexical_hits)
        modes.append("lexical")

    selected: list[str] = []
    for rel in considered:
        if rel not in selected:
            selected.append(rel)
        if len(selected) >= max_tests:
            break

    issue_calls = extract_issue_calls(issue_text)
    contract_lines = _extract_contract_lines(
        repo_root, selected, max_lines=max_contract_lines
    )
    if issue_calls:
        contract_lines = [f"issue calls: {call}" for call in issue_calls[:2]] + contract_lines
        contract_lines = contract_lines[:max_contract_lines]

    if selected or issue_calls:
        return ContractResult(
            selected_test_files=tuple(selected),
            test_files_considered=tuple(dict.fromkeys(considered)),
            contract_lines=tuple(contract_lines),
            issue_calls=tuple(issue_calls),
            extraction_mode="+".join(modes) if modes else "issue-only",
            abstain_reason="",
        )
    return ContractResult(
        test_files_considered=tuple(dict.fromkeys(considered)),
        extraction_mode="none",
        abstain_reason="no_test_or_issue_contract",
    )


def contract_telemetry(result: ContractResult, wall_ms: int) -> dict[str, object]:
    """Convert a contract result to the module_7_contract telemetry block."""
    return {
        "wall_ms": wall_ms,
        "enabled": True,
        "test_files_considered": list(result.test_files_considered),
        "selected_test_files": list(result.selected_test_files),
        "contract_lines": list(result.contract_lines),
        "issue_calls": list(result.issue_calls),
        "extraction_mode": result.extraction_mode,
        "abstain_reason": result.abstain_reason,
    }

"""Deterministic producers for typed observation-compiler requests.

The entry point in this module executes only an already-selected typed action.  It
does not interpret shell commands, predict planner intent, render model-facing
text, or choose an interception mode.  Every result is returned as the canonical
``EvidenceArtifact`` defined by :mod:`groundtruth.runtime.observation_compiler`.

Filesystem searches are exact over an explicitly declared scope only when a
complete, revision-bound file manifest proves that the captured bytes belong to
the requested worktree. Syntax queries similarly parse the exact bytes they
hash. Any missing authority is retained as useful but ``incomplete`` evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from .edit_check import check_edit_syntax_bytes
from .evidence_envelope import EvidenceEnvelope, INFO
from .observation_compiler import (
    ActionKind,
    ActionRequest,
    Coverage,
    EvidenceArtifact,
    EvidenceSemantics,
    artifact_from_envelope,
    canonical_bytes,
    canonical_sha256,
)
from .diff_impact import impact_for_ranges
from .patch_delta import analyze_patch_delta
from .processes import detect_processes
from .verification_plan import Check, CheckResult, VerificationPlan, green


PRODUCER_VERSION = "gt.deterministic_queries.v1"
_ALLOWED_ARGUMENTS: Mapping[ActionKind, frozenset[str]] = {
    ActionKind.EXACT_LITERAL_SEARCH: frozenset({"literal", "paths"}),
    ActionKind.SYNTAX_QUERY: frozenset({"path"}),
    ActionKind.PATCH_IMPACT: frozenset({"edited_files"}),
    ActionKind.VERIFICATION_STATUS: frozenset({"plan", "result"}),
    ActionKind.DEFINITION: frozenset({"symbol", "path", "language"}),
    ActionKind.REFERENCES: frozenset({"symbol", "path", "language"}),
    ActionKind.CALLERS: frozenset({"symbol", "path", "language", "depth"}),
    ActionKind.SYMBOL_CONTEXT: frozenset({"symbol", "path", "language"}),
    ActionKind.PROCESSES: frozenset({"concept", "limit"}),
    ActionKind.ROUTE_MAP: frozenset({"path"}),
    ActionKind.API_IMPACT: frozenset({"route", "handler"}),
    ActionKind.TAINT: frozenset(
        {"source", "sink", "path", "language", "depth", "include_name_matched"}
    ),
    ActionKind.RENAME: frozenset({"symbol", "new_name", "path", "language"}),
    ActionKind.SHAPE_CHECK: frozenset({"symbol", "path", "language"}),
    ActionKind.TOOL_MAP: frozenset({"path", "language"}),
    ActionKind.SLICE: frozenset({"symbol", "line", "direction", "path", "language", "variables", "interprocedural", "max_hops", "max_depth"}),
}


@dataclass(frozen=True)
class DeterministicQueryContext:
    """Local authorities available to deterministic query producers."""

    repository_root: Path
    graph_db: Path | None = None
    repository_content_revision: str = ""
    working_tree_sha256: str = ""
    snapshot_files: tuple[tuple[str, str], ...] = ()
    snapshot_complete: bool = False

    def __post_init__(self) -> None:
        root = Path(self.repository_root).resolve()
        object.__setattr__(self, "repository_root", root)
        if self.graph_db is not None:
            graph = Path(self.graph_db)
            if not graph.is_absolute():
                graph = root / graph
            object.__setattr__(self, "graph_db", graph.resolve())
        files: list[tuple[str, str]] = []
        for raw_path, raw_digest in self.snapshot_files:
            path = str(raw_path).replace("\\", "/")
            digest = str(raw_digest)
            parts = tuple(part for part in path.split("/") if part)
            canonical_path = "/".join(parts)
            if (
                not path
                or path.startswith("/")
                or (len(path) >= 2 and path[1] == ":")
                or path != canonical_path
                or any(part in {".", ".."} for part in parts)
            ):
                raise ValueError("snapshot_files contains an unsafe path")
            if (
                len(digest) != 64
                or digest != digest.lower()
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("snapshot_files contains an invalid SHA-256")
            files.append((canonical_path, digest))
        files_tuple = tuple(sorted(files))
        if len({path for path, _digest in files_tuple}) != len(files_tuple):
            raise ValueError("snapshot_files contains duplicate paths")
        object.__setattr__(self, "snapshot_files", files_tuple)


@dataclass(frozen=True)
class _Produced:
    answer: Any
    semantics: EvidenceSemantics
    coverage: Coverage
    anchors: tuple[tuple[str, int], ...] = ()
    witnesses: tuple[str, ...] = ()
    ambiguity: tuple[str, ...] = ()
    omissions: tuple[str, ...] = ()
    raw_fallback: bytes = b""
    revision: str = ""


_GRAPH_BACKED_KINDS = frozenset(
    {
        ActionKind.DEFINITION,
        ActionKind.REFERENCES,
        ActionKind.CALLERS,
        ActionKind.SYMBOL_CONTEXT,
        ActionKind.PROCESSES,
        ActionKind.PATCH_IMPACT,
        ActionKind.ROUTE_MAP,
        ActionKind.API_IMPACT,
        ActionKind.TAINT,
        ActionKind.RENAME,
        ActionKind.SHAPE_CHECK,
        ActionKind.TOOL_MAP,
        ActionKind.SLICE,
    }
)


def _producer_revision(request: ActionRequest) -> str:
    revisions = request.repository_snapshot.revisions
    if request.kind is ActionKind.VERIFICATION_STATUS:
        return revisions.runtime_evidence
    if request.kind in _GRAPH_BACKED_KINDS:
        return revisions.graph
    return revisions.repository_content


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _snapshot_authority_omissions(
    request: ActionRequest, context: DeterministicQueryContext
) -> list[str]:
    if not context.snapshot_complete:
        return ["snapshot_authority_unavailable"]
    omissions: list[str] = []
    if context.repository_content_revision != (
        request.repository_snapshot.revisions.repository_content
    ):
        omissions.append("repository_revision_mismatch")
    if context.working_tree_sha256 != request.repository_snapshot.working_tree_sha256:
        omissions.append("working_tree_sha256_mismatch")
    return omissions


def _safe_scope(root: Path, raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _iter_scope(root: Path, scopes: Sequence[Path]) -> tuple[list[Path], list[str]]:
    files: dict[str, Path] = {}
    omissions: set[str] = set()
    for scope in sorted(set(scopes), key=lambda item: item.as_posix()):
        if not scope.exists():
            omissions.add(f"missing_scope:{_rel(root, scope)}")
            continue
        if scope.is_symlink():
            omissions.add(f"symlink:{_rel(root, scope)}")
            continue
        if scope.is_file():
            files[_rel(root, scope)] = scope
            continue
        for dirpath, dirnames, filenames in os.walk(scope, followlinks=False):
            directory = Path(dirpath)
            kept_dirs: list[str] = []
            for name in sorted(dirnames):
                child = directory / name
                if child.is_symlink():
                    omissions.add(f"symlink:{_rel(root, child)}")
                else:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                child = directory / name
                rel = _rel(root, child)
                if child.is_symlink():
                    omissions.add(f"symlink:{rel}")
                else:
                    files[rel] = child
    return [files[key] for key in sorted(files)], sorted(omissions)


def _literal_search(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    args = request.arguments
    literal = args.get("literal")
    unknown_arguments = sorted(set(args) - {"literal", "paths"})
    invalid_literal = (
        not isinstance(literal, str)
        or not literal
        or "\x00" in literal
        or "\r" in literal
        or "\n" in literal
    )
    if invalid_literal or unknown_arguments:
        omissions = (["invalid_literal"] if invalid_literal else []) + [
            f"unsupported_argument:{name}" for name in unknown_arguments
        ]
        return _Produced(
            {"matches": [], "scope": []},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=tuple(omissions),
            revision=_producer_revision(request),
        )
    raw_scopes = args.get("paths", ["."])
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raw_scopes = []
    scopes: list[Path] = []
    invalid: list[str] = []
    for item in raw_scopes:
        resolved = _safe_scope(context.repository_root, item)
        if resolved is None:
            invalid.append(f"invalid_scope:{item!s}")
        else:
            scopes.append(resolved)
    files, omissions = _iter_scope(context.repository_root, scopes)
    omissions.extend(_snapshot_authority_omissions(request, context))
    omissions.extend(invalid)
    needle = literal.encode("utf-8")
    matches: list[dict[str, Any]] = []
    observed_files: list[dict[str, Any]] = []
    anchors: list[tuple[str, int]] = []
    for path in files:
        rel = _rel(context.repository_root, path)
        try:
            data = path.read_bytes()
        except (OSError, PermissionError):
            omissions.append(f"unreadable:{rel}")
            continue
        observed_files.append({"path": rel, "sha256": _sha256(data), "bytes": len(data)})
        offset = 0
        for line_no, line in enumerate(data.splitlines(keepends=True), start=1):
            start = 0
            while True:
                index = line.find(needle, start)
                if index < 0:
                    break
                preview = line.rstrip(b"\r\n").decode("utf-8", "backslashreplace")
                matches.append(
                    {
                        "path": rel,
                        "line": line_no,
                        "column": index + 1,
                        "column_unit": "utf8_byte_1_based",
                        "byte_offset": offset + index,
                        "line_text": preview,
                    }
                )
                anchors.append((rel, line_no))
                start = index + len(needle)
            offset += len(line)
    observed_by_path = {row["path"]: row["sha256"] for row in observed_files}
    snapshot_by_path = dict(context.snapshot_files)
    expected_in_scope = {
        rel: digest
        for rel, digest in snapshot_by_path.items()
        if any(
            (context.repository_root / rel).resolve(strict=False) == scope
            or scope in (context.repository_root / rel).resolve(strict=False).parents
            for scope in scopes
        )
    }
    if observed_by_path != expected_in_scope:
        omissions.append("snapshot_scope_content_mismatch")
    omissions = sorted(set(omissions))
    answer = {
        "literal": literal,
        "scope": sorted(_rel(context.repository_root, item) for item in scopes),
        "scope_sha256": canonical_sha256(observed_files),
        "files_observed": observed_files,
        "matches": matches,
    }
    exact = not omissions and bool(scopes)
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=tuple(anchors),
        witnesses=tuple(f"file:{row['path']}:{row['sha256']}" for row in observed_files),
        omissions=tuple(omissions or (() if scopes else ("no_valid_scope",))),
        revision=request.repository_snapshot.revisions.repository_content,
    )


def _syntax(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    raw_path = request.arguments.get("path")
    path = _safe_scope(context.repository_root, raw_path)
    if path is None or not path.is_file():
        return _Produced(
            {"verdict": "unavailable", "reason": "invalid_or_missing_path"},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=("invalid_or_missing_path",),
            revision=_producer_revision(request),
        )
    rel = _rel(context.repository_root, path)
    try:
        source = path.read_bytes()
    except OSError:
        return _Produced(
            {"path": rel, "verdict": "unavailable", "reason": "unreadable"},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=(f"unreadable:{rel}",),
            revision=_producer_revision(request),
        )
    result = check_edit_syntax_bytes(rel, source, str(context.repository_root))
    answer = {"path": rel, "source_sha256": _sha256(source), "source_bytes": len(source), **result}
    omissions = _snapshot_authority_omissions(request, context)
    expected_sha256 = dict(context.snapshot_files).get(rel)
    if expected_sha256 != _sha256(source):
        omissions.append("snapshot_source_mismatch")
    available = result.get("verdict") in {"ok", "syntax_error", "name_error"}
    if not available:
        omissions.append(str(result.get("reason") or "syntax_checker_unavailable"))
    line = 1
    diagnostic = str(result.get("diagnostic") or "")
    for token in diagnostic.replace(",", " ").split():
        if token.isdigit():
            line = max(1, int(token))
            break
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if available and not omissions else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if available and not omissions else Coverage.UNKNOWN,
        anchors=((rel, line),),
        witnesses=(f"source:{_sha256(source)}",),
        omissions=tuple(sorted(set(omissions))),
        raw_fallback=diagnostic.encode("utf-8", "surrogatepass"),
        revision=request.repository_snapshot.revisions.repository_content,
    )


def _patch_impact(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    raw = request.arguments.get("edited_files")
    if not isinstance(raw, Mapping) or not raw:
        return _Produced(
            {"files": [], "analysis": {}},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=("invalid_edited_files",),
            revision=_producer_revision(request),
        )
    edited: dict[str, tuple[str | None, str]] = {}
    exact_files: list[dict[str, Any]] = []
    omissions: list[str] = []
    for name in sorted(raw, key=str):
        value = raw[name]
        if not isinstance(name, str) or not isinstance(value, Mapping):
            omissions.append(f"invalid_edit:{name!s}")
            continue
        before = value.get("before")
        after = value.get("after")
        if before is not None and not isinstance(before, str) or not isinstance(after, str):
            omissions.append(f"invalid_edit:{name}")
            continue
        edited[name] = (before, after)
        exact_files.append(
            {
                "path": name,
                "before_sha256": _sha256(before.encode("utf-8")) if before is not None else None,
                "after_sha256": _sha256(after.encode("utf-8")),
            }
        )
    result = analyze_patch_delta(edited, str(context.repository_root), str(context.graph_db or ""))
    analysis = asdict(result)
    if result.reason:
        omissions.append(f"patch_analyzer:{result.reason}")
    if result.is_empty and os.environ.get("GT_PATCH_DELTA", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        omissions.append("patch_analyzer_disabled")

    # Graph impact: diff -> changed symbols -> upstream callers -> affected
    # flows. A missing graph or files that map to no Function/Method node are
    # named omissions, never silently dropped.
    impact_answer: dict[str, Any] = {
        "changed_symbols": [],
        "callers_by_depth": {},
        "affected_flows": [],
        "statement_slices": [],
    }
    conn = _open_graph(context)
    if conn is None:
        omissions.append("graph_unavailable")
    else:
        try:
            omissions.extend(_graph_omissions(request, context, conn))
            sides = {
                name: _graph_side(conn, name, edited[name], omissions)
                for name in sorted(edited)
            }
            ranges = {
                name: _changed_ranges(edited[name][0] or "", edited[name][1], sides[name])
                for name in sorted(edited)
            }
            impact = impact_for_ranges(
                context.graph_db,
                {name: r for name, r in ranges.items() if r},
                processes=detect_processes(context.graph_db).processes,
            )
            mapped = {s.file_path for s in impact.changed_symbols}
            for name in sorted(edited):
                if name not in mapped:
                    omissions.append(f"changed_symbols_unmapped:{name}")
            impact_answer = {
                "changed_symbols": [
                    {
                        "name": s.name,
                        "file_path": s.file_path,
                        "start_line": s.start_line,
                    }
                    for s in impact.changed_symbols
                ],
                "line_mapping": {name: sides[name] for name in sorted(edited)},
                "callers_by_depth": {
                    str(d): [
                        {
                            "name": c.name,
                            "qualified_name": c.qualified_name,
                            "location": c.location,
                            "trust_tier": c.trust_tier,
                            "confidence": c.confidence,
                        }
                        for c in callers
                    ]
                    for d, callers in impact.caller_details_by_depth.items()
                },
                "affected_flows": [
                    {
                        "label": p.display_label,
                        "entry_kind": p.entry_kind,
                        "step_count": p.step_count,
                        "certified_ratio": round(p.certified_ratio, 4),
                    }
                    for p in impact.affected_flows
                ],
                "statement_slices": _statement_slices(edited, conn, omissions, sides),
            }
        except sqlite3.Error:
            omissions.append("graph_unreadable")
        finally:
            conn.close()

    # Exact patch identities are retained, but the semantic impact analyzer is
    # deliberately conservative and therefore cannot certify complete impact.
    omissions.append("semantic_impact_not_complete")
    return _Produced(
        {"files": exact_files, "analysis": analysis, "impact": impact_answer},
        EvidenceSemantics.INCOMPLETE,
        Coverage.PARTIAL,
        witnesses=tuple(f"patch:{row['path']}:{row['after_sha256']}" for row in exact_files),
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _text_digests(text: str) -> set[str]:
    """SHA-256 of the text as given and with LF/CRLF line endings — the
    producer hashes the raw file bytes, the caller may hold either form."""
    lf = text.replace("\r\n", "\n")
    return {
        _sha256(text.encode("utf-8")),
        _sha256(lf.encode("utf-8")),
        _sha256(lf.replace("\n", "\r\n").encode("utf-8")),
    }


def _graph_side(
    conn: sqlite3.Connection,
    name: str,
    edit: tuple[str | None, str],
    omissions: list[str],
) -> str:
    """Which version of ``name`` the graph's line numbers describe.

    ``"old"`` when the graph's recorded content hash equals ``before``,
    ``"new"`` when it equals ``after``.  When neither can be proven the
    pre-image is assumed if ``before`` is known (a typed patch_impact is
    normally asked before the graph is amended) and the mapping is named
    ``line_mapping_approximate:<file>``.
    """
    before, after = edit
    try:
        row = conn.execute(
            "SELECT content_hash FROM file_hashes WHERE file_path = ?", (name,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    recorded = str(row[0]) if row and row[0] else ""
    if recorded and before is not None and recorded in _text_digests(before):
        return "old"
    if recorded and recorded in _text_digests(after):
        return "new"
    try:
        indexed = conn.execute(
            "SELECT 1 FROM nodes WHERE file_path = ? LIMIT 1", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        indexed = False
    if indexed:
        omissions.append(f"line_mapping_approximate:{name}")
    return "old" if before is not None else "new"


def _changed_ranges(before: str, after: str, side: str) -> list[tuple[int, int]]:
    """Changed line ranges ``(start, length)`` in the ``side`` numbering.

    Computed from line opcodes, not from a unified diff: diff hunks carry
    context lines, and a post-image range laid over a pre-edit graph lands
    on whatever the shifted lines used to be.  A pure insertion (old side)
    or pure deletion (new side) is attributed to the two lines around the
    joint — conservative: the joint belongs to whichever symbol spans it.
    """
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines(), autojunk=False
    )
    ranges: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        lo, hi = (i1, i2) if side == "old" else (j1, j2)
        if hi > lo:
            ranges.append((lo + 1, hi - lo))
        else:
            ranges.append((max(lo, 1), 2 if lo >= 1 else 1))
    return ranges


def _changed_after_lines(before: str, after: str) -> list[tuple[int, int]]:
    """``(after_line, old_line)`` for each replaced/inserted post-edit line;
    ``old_line`` is the pre-image line the change sits at."""
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines(), autojunk=False
    )
    out: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"replace", "insert"}:
            continue
        for j in range(j1, j2):
            if tag == "replace":
                old = i1 + min(j - j1, i2 - i1 - 1) + 1
            else:
                old = max(i1, 1)
            out.append((j + 1, old))
    return out


def _check_from_dict(data: Mapping[str, Any]) -> Check:
    return Check(
        kind=str(data.get("kind") or ""),
        command=tuple(data["command"]) if isinstance(data.get("command"), list) else None,
        selection_basis=str(data.get("selection_basis") or ""),
        covered_entities=tuple(data.get("covered_entities") or ()),
        covered_obligations=tuple(data.get("covered_obligations") or ()),
        expected_cost=str(data.get("expected_cost") or "unknown"),
        confidence=str(data.get("confidence") or "unknown"),
        attribution_requirement=str(data.get("attribution_requirement") or "none"),
        targets=tuple(data.get("targets") or ()),
        reason=str(data.get("reason") or ""),
    )


def _verification(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    del context
    plan_data = request.arguments.get("plan")
    result_data = request.arguments.get("result")
    if not isinstance(plan_data, Mapping) or not isinstance(result_data, Mapping):
        return _Produced(
            {"green": False, "status": "unavailable"},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=("invalid_verification_input",),
            revision=request.repository_snapshot.revisions.runtime_evidence,
        )
    try:
        plan = VerificationPlan(
            patch_revision=str(plan_data.get("patch_revision") or ""),
            graph_revision=str(plan_data.get("graph_revision") or ""),
            changed_entities=tuple(plan_data.get("changed_entities") or ()),
            obligations=tuple(plan_data.get("obligations") or ()),
            checks=tuple(_check_from_dict(item) for item in plan_data.get("checks") or ()),
            edited_files=tuple(plan_data.get("edited_files") or ()),
        )
        result = CheckResult(
            kind=str(result_data.get("kind") or ""),
            selection_basis=str(result_data.get("selection_basis") or ""),
            executed=bool(result_data.get("executed")),
            verdict=str(result_data.get("verdict") or ""),
            graph_revision=str(result_data.get("graph_revision") or ""),
            patch_revision=str(result_data.get("patch_revision") or ""),
            covered_entities=tuple(result_data.get("covered_entities") or ()),
            covered_obligations=tuple(result_data.get("covered_obligations") or ()),
            attribution_requirement=str(result_data.get("attribution_requirement") or "none"),
            attribution_satisfied=bool(result_data.get("attribution_satisfied")),
            detail=dict(result_data.get("detail") or {}),
        )
        verdict = green(result, plan)
    except (TypeError, ValueError, KeyError) as exc:
        return _Produced(
            {"green": False, "status": "unavailable"},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=(f"invalid_verification_input:{type(exc).__name__}",),
            revision=request.repository_snapshot.revisions.runtime_evidence,
        )
    live = request.repository_snapshot.revisions
    omissions: list[str] = []
    if plan.graph_revision != live.graph or result.graph_revision != live.graph:
        omissions.append("graph_revision_mismatch")
    if (
        plan.patch_revision != live.repository_content
        or result.patch_revision != live.repository_content
    ):
        omissions.append("patch_revision_mismatch")
    answer = {**asdict(verdict), "plan": plan.to_dict(), "result": asdict(result)}
    return _Produced(
        answer,
        EvidenceSemantics.EXECUTION_SPECIFIC if not omissions else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if not omissions else Coverage.PARTIAL,
        witnesses=(f"verification:{canonical_sha256(answer)}",),
        omissions=tuple(omissions),
        raw_fallback=canonical_bytes(result.detail),
        revision=live.runtime_evidence,
    )


# ---------------------------------------------------------------------------
# Graph-backed producers.
#
# Every producer below reads the precomputed call graph at context.graph_db
# and binds to the graph's recorded revision. Honest-abstention contract
# (same discipline as _literal_search): an EXACT answer requires the graph
# to be present, its revision to match the request snapshot, and the symbol
# to resolve unambiguously as recorded; anything else is INCOMPLETE with a
# named omission — never fabricated coverage.
# ---------------------------------------------------------------------------

_NODE_COLS = (
    "id, name, COALESCE(qualified_name,''), COALESCE(label,''), file_path,"
    " COALESCE(start_line,0), COALESCE(end_line,0), COALESCE(signature,''),"
    " COALESCE(return_type,''), COALESCE(language,''), COALESCE(is_test,0),"
    " COALESCE(is_exported,0)"
)
_DEF_LABELS = ("Function", "Method", "Class", "Interface", "Struct", "Enum", "Module")
# Source-code symbol labels — the parser's declaration taxonomy, mirroring
# gt-index ``resolver.IsCodeSymbolLabel`` (plus the Rust ``ImplBlock``
# parent).  Everything else in ``nodes`` is either a file anchor or an
# analysis-layer row (Callsite, *Fact, QueryPolicyVersion, ...) published by
# the resolution substrate; those share symbol *names* with real code and
# must never be resolved as a symbol, a definition, or a reference source.
_SOURCE_SYMBOL_LABELS = frozenset(
    {
        "Function", "Method", "Class", "Interface",
        "Struct", "Enum", "EnumMember", "Trait", "Impl", "ImplBlock",
        "TypeAlias", "Namespace", "Module", "Union", "Macro", "Constant",
        "Record", "Annotation", "Constructor", "Accessor", "Protocol",
    }
)
# Incoming-edge sources that count as code references: source symbols plus
# the per-file anchor node (module-level imports/decorations hang off it).
_REFERENCE_SOURCE_LABELS = _SOURCE_SYMBOL_LABELS | {"File"}
_SOURCE_LABEL_SQL = ",".join(f"'{label}'" for label in sorted(_SOURCE_SYMBOL_LABELS))
_REFERENCE_LABEL_SQL = ",".join(f"'{label}'" for label in sorted(_REFERENCE_SOURCE_LABELS))
# Result caps.  Every cap that drops rows records a named omission, and the
# reported counts are always the pre-truncation totals.
_MAX_RESOLVED_NODES = 5
_MAX_ROWS_PER_GROUP = 20
_MAX_CONTEXT_NEIGHBORS = 10
_MAX_CONTEXT_FLOWS = 5
_TIER_ORDER = {"CERTIFIED": 0, "CANDIDATE": 1, "SPECULATIVE": 2}
# Languages the Go indexer emits per-function CFG sidecar rows for
# (cfg_blocks/cfg_edges/cfg_defs) — mirrored by cfgLangs in
# gt-index/internal/parser/cfg.go.  ``slice`` serves these through the
# cfg_store substrate when rows exist; everything else still abstains.
_STORED_CFG_LANGUAGES = frozenset({"javascript", "typescript", "java", "go"})


def _open_graph(context: DeterministicQueryContext) -> sqlite3.Connection | None:
    graph = context.graph_db
    if graph is None or not Path(graph).is_file():
        return None
    try:
        return sqlite3.connect(str(graph))
    except sqlite3.Error:
        return None


def _graph_revision(conn: sqlite3.Connection) -> str:
    """The indexed source revision the graph was built from.

    HAR-90 contract: gt-index writes ``project_meta.source_revision`` (flag
    ``-source-revision``) on full builds, amends and ``-file`` increments.
    ``git_commit`` is the producer *build* commit — provenance, not the
    indexed tree — so it is never read here; comparing it against a value
    the host copied from the same row made the freshness check tautological.
    Absent -> "" -> ``graph_revision_unavailable``.
    """
    try:
        row = conn.execute(
            "SELECT value FROM project_meta WHERE key='source_revision'"
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


def _graph_omissions(
    request: ActionRequest,
    context: DeterministicQueryContext,
    conn: sqlite3.Connection,
) -> list[str]:
    omissions = _snapshot_authority_omissions(request, context)
    graph_revision = _graph_revision(conn)
    live_graph = request.repository_snapshot.revisions.graph
    if not graph_revision:
        omissions.append("graph_revision_unavailable")
    elif live_graph and graph_revision != live_graph:
        omissions.append("graph_revision_mismatch")
    return sorted(set(omissions))


def _node_dict(row: tuple) -> dict[str, Any]:
    (
        node_id, name, qualified, label, file_path, start_line, end_line,
        signature, return_type, language, is_test, is_exported,
    ) = row
    return {
        "id": int(node_id),
        "name": str(name or ""),
        "qualified_name": str(qualified or ""),
        "kind": str(label or ""),
        "file_path": str(file_path or ""),
        "start_line": int(start_line or 0),
        "end_line": int(end_line or 0),
        "signature": str(signature or ""),
        "return_type": str(return_type or ""),
        "language": str(language or ""),
        "is_test": bool(is_test),
        "is_exported": bool(is_exported),
    }


def _resolve_symbol_nodes(
    conn: sqlite3.Connection,
    symbol: object,
    path_hint: object = "",
    language_hint: object = "",
    omissions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Exact name/qualified_name lookup over source-symbol nodes only.

    Analysis-layer rows (``Callsite`` and the resolution-substrate fact
    nodes) are excluded by label.  The language hint is a filter: a hint
    that matches nothing yields no nodes (never a silent fallback to other
    languages) and records ``language_hint_unmatched``.  The path hint
    narrows when it matches; a non-matching path hint keeps the set and
    records ``path_hint_unmatched`` so the answer cannot claim EXACT.
    """
    if not isinstance(symbol, str) or not symbol.strip() or "\x00" in symbol:
        return []
    symbol = symbol.strip()
    rows = conn.execute(
        f"SELECT {_NODE_COLS} FROM nodes WHERE (name = ? OR qualified_name = ?)"
        f" AND label IN ({_SOURCE_LABEL_SQL})",
        (symbol, symbol),
    ).fetchall()
    nodes = [_node_dict(r) for r in rows]
    if isinstance(language_hint, str) and language_hint.strip():
        lang = language_hint.strip().lower()
        narrowed = [n for n in nodes if n["language"].lower() == lang]
        if not narrowed and nodes and omissions is not None:
            omissions.append(f"language_hint_unmatched:{lang}")
        nodes = narrowed
    if isinstance(path_hint, str) and path_hint.strip():
        hint = path_hint.strip().replace("\\", "/")
        narrowed = [n for n in nodes if n["file_path"].endswith(hint)]
        if narrowed:
            nodes = narrowed
        elif nodes and omissions is not None:
            omissions.append("path_hint_unmatched")
    # Preferred order: non-test defs, exported first, deterministic by site.
    nodes.sort(
        key=lambda n: (n["is_test"], -int(n["is_exported"]), n["file_path"], n["start_line"])
    )
    return nodes


def _symbol_sites(nodes: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Ambiguity witnesses: one ``file:line`` per distinct resolved symbol.

    More than one site means the name denotes several symbols (different
    files, classes or languages); merging their evidence silently is what
    this surfaces."""
    sites = sorted({f"{n['file_path']}:{n['start_line']}" for n in nodes})
    return tuple(sites) if len(sites) > 1 else ()


def _resolved_symbols(nodes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": n["name"],
            "qualified_name": n["qualified_name"],
            "kind": n["kind"],
            "file_path": n["file_path"],
            "start_line": n["start_line"],
            "language": n["language"],
        }
        for n in nodes[:_MAX_ROWS_PER_GROUP]
    ]


def _tier_key(row: Mapping[str, Any]) -> tuple[int, str, int]:
    return (
        _TIER_ORDER.get(str(row.get("trust_tier") or ""), 3),
        str(row.get("file_path") or ""),
        int(row.get("line") or 0),
    )


def _unavailable_graph(request: ActionRequest, kind: str) -> _Produced:
    return _Produced(
        {"symbol": request.arguments.get("symbol"), "answer": None},
        EvidenceSemantics.INCOMPLETE,
        Coverage.UNKNOWN,
        omissions=("graph_unavailable",),
        revision=_producer_revision(request),
    )


def _definition(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "definition")
    args = request.arguments
    with conn:
        omissions = _graph_omissions(request, context, conn)
        nodes = _resolve_symbol_nodes(
            conn, args.get("symbol"), args.get("path"), args.get("language"), omissions
        )
    if not nodes:
        omissions.append("symbol_not_found")
    defs = [n for n in nodes if n["kind"] in _DEF_LABELS] or nodes
    if len(defs) > _MAX_ROWS_PER_GROUP:
        omissions.append("definitions_truncated")
    answer = {
        "symbol": args.get("symbol"),
        "definitions": [
            {
                "name": n["name"],
                "qualified_name": n["qualified_name"],
                "kind": n["kind"],
                "file_path": n["file_path"],
                "start_line": n["start_line"],
                "end_line": n["end_line"],
                "signature": n["signature"],
                "return_type": n["return_type"],
                "language": n["language"],
                "is_test": n["is_test"],
            }
            for n in defs[:_MAX_ROWS_PER_GROUP]
        ],
        "definition_count": len(defs),
    }
    ambiguity = _symbol_sites(defs)
    exact = not omissions and not ambiguity
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact and defs else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact and defs else Coverage.PARTIAL,
        anchors=tuple(
            (n["file_path"], n["start_line"]) for n in defs[:_MAX_ROWS_PER_GROUP]
            if n["start_line"] >= 1
        ),
        ambiguity=ambiguity,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _neighbor_row(
    name: object,
    qualified: object,
    file_path: object,
    line: object,
    call_line: object,
    language: object,
    tier: object,
    confidence: object,
) -> dict[str, Any]:
    return {
        "name": str(name or ""),
        "qualified_name": str(qualified or ""),
        "file_path": str(file_path or ""),
        "line": int(line or 0),
        "call_line": int(call_line or 0),
        "language": str(language or ""),
        "trust_tier": str(tier or ""),
        "confidence": float(confidence or 0.0),
    }


def _row_anchors(rows: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, int], ...]:
    return tuple(
        (str(r["file_path"]), int(r["line"]))
        for r in rows
        if r.get("file_path") and int(r.get("line") or 0) >= 1
    )[:_MAX_ROWS_PER_GROUP]


def _references(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Incoming graph edges of every type into the resolved symbol(s).

    Reference sources are source symbols or file anchors only — the
    resolution substrate's Callsite/fact rows are not code references.
    Rows carry the referencing symbol's language and the edge's own
    ``reference_line``; ``line`` stays the referencing symbol's start line.
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "references")
    args = request.arguments
    with conn:
        omissions = _graph_omissions(request, context, conn)
        nodes = _resolve_symbol_nodes(
            conn, args.get("symbol"), args.get("path"), args.get("language"), omissions
        )
        if not nodes:
            omissions.append("symbol_not_found")
        if len(nodes) > _MAX_RESOLVED_NODES:
            omissions.append("resolved_nodes_truncated")
        by_type: dict[str, list[dict[str, Any]]] = {}
        for node in nodes[:_MAX_RESOLVED_NODES]:
            for row in conn.execute(
                "SELECT e.type, n.name, COALESCE(n.qualified_name,''), n.file_path,"
                " n.start_line, COALESCE(e.source_line,0), COALESCE(n.language,''),"
                " COALESCE(e.trust_tier,''), COALESCE(e.confidence,0.0)"
                " FROM edges e JOIN nodes n ON n.id = e.source_id"
                f" WHERE e.target_id = ? AND n.label IN ({_REFERENCE_LABEL_SQL})",
                (node["id"],),
            ):
                etype, name, qualified, fp, line, ref_line, lang, tier, conf = row
                entry = _neighbor_row(name, qualified, fp, line, ref_line, lang, tier, conf)
                entry["reference_line"] = entry.pop("call_line")
                entry["target"] = f"{node['file_path']}:{node['start_line']}"
                by_type.setdefault(str(etype), []).append(entry)
        counts = {etype: len(rows) for etype, rows in sorted(by_type.items())}
        for etype, rows in by_type.items():
            rows.sort(key=_tier_key)
            if len(rows) > _MAX_ROWS_PER_GROUP:
                omissions.append(f"references_truncated:{etype}")
                del rows[_MAX_ROWS_PER_GROUP:]
    answer = {
        "symbol": args.get("symbol"),
        "resolved_nodes": len(nodes),
        "resolved_symbols": _resolved_symbols(nodes),
        "references_by_type": by_type,
        "reference_count": sum(counts.values()),
        "reference_count_by_type": counts,
        "returned_count": sum(len(rows) for rows in by_type.values()),
    }
    ambiguity = _symbol_sites(nodes)
    exact = not omissions and bool(nodes) and not ambiguity
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=_row_anchors([r for rows in by_type.values() for r in rows]),
        ambiguity=ambiguity,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _callers(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Transitive incoming CALLS, banded by hop distance.

    ``caller_count`` is the number of distinct callers found before any
    per-band cap; ``returned_count`` is what the answer carries.  A dropped
    row is the omission ``callers_truncated``; several same-named symbols
    are reported as ambiguity, never merged into one EXACT answer.
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "callers")
    raw_depth = request.arguments.get("depth", 3)
    try:
        depth = max(1, min(int(raw_depth), 6))
    except (TypeError, ValueError):
        depth = 3
    args = request.arguments
    with conn:
        omissions = _graph_omissions(request, context, conn)
        nodes = _resolve_symbol_nodes(
            conn, args.get("symbol"), args.get("path"), args.get("language"), omissions
        )
        if not nodes:
            omissions.append("symbol_not_found")
        visited = {n["id"] for n in nodes}
        bands: dict[int, list[dict[str, Any]]] = {}
        frontier = [(n["id"], 0) for n in nodes]
        while frontier:
            node_id, dist = frontier.pop(0)
            if dist >= depth:
                continue
            for row in conn.execute(
                "SELECT e.source_id, n.name, COALESCE(n.qualified_name,''), n.file_path,"
                " n.start_line, COALESCE(e.source_line,0), COALESCE(n.language,''),"
                " COALESCE(e.trust_tier,''), COALESCE(e.confidence,0.0)"
                " FROM edges e JOIN nodes n ON n.id = e.source_id"
                " WHERE e.target_id = ? AND e.type='CALLS'"
                f" AND n.label IN ({_SOURCE_LABEL_SQL})",
                (node_id,),
            ):
                src = row[0]
                if src in visited:
                    continue
                visited.add(src)
                bands.setdefault(dist + 1, []).append(_neighbor_row(*row[1:]))
                frontier.append((src, dist + 1))
        caller_count = sum(len(rows) for rows in bands.values())
        for rows in bands.values():
            rows.sort(
                key=lambda r: (_TIER_ORDER.get(r["trust_tier"], 3), r["name"], r["file_path"], r["line"])
            )
            if len(rows) > _MAX_ROWS_PER_GROUP:
                omissions.append("callers_truncated")
                del rows[_MAX_ROWS_PER_GROUP:]
    answer = {
        "symbol": args.get("symbol"),
        "resolved_nodes": len(nodes),
        "resolved_symbols": _resolved_symbols(nodes),
        "max_depth": depth,
        "callers_by_depth": {str(d): rows for d, rows in sorted(bands.items())},
        "caller_count": caller_count,
        "returned_count": sum(len(r) for r in bands.values()),
    }
    ambiguity = _symbol_sites(nodes)
    exact = not omissions and bool(nodes) and not ambiguity
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=_row_anchors([r for _d, rows in sorted(bands.items()) for r in rows]),
        ambiguity=ambiguity,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _call_neighbors(
    conn: sqlite3.Connection, node_id: int, direction: str
) -> list[dict[str, Any]]:
    other, own = ("source_id", "target_id") if direction == "in" else ("target_id", "source_id")
    rows = [
        _neighbor_row(*row)
        for row in conn.execute(
            "SELECT n.name, COALESCE(n.qualified_name,''), n.file_path, n.start_line,"
            " COALESCE(e.source_line,0), COALESCE(n.language,''),"
            " COALESCE(e.trust_tier,''), COALESCE(e.confidence,0.0)"
            f" FROM edges e JOIN nodes n ON n.id = e.{other}"
            f" WHERE e.{own} = ? AND e.type='CALLS'"
            f" AND n.label IN ({_SOURCE_LABEL_SQL})",
            (node_id,),
        )
    ]
    rows.sort(key=lambda r: (_TIER_ORDER.get(r["trust_tier"], 3), r["name"], r["file_path"]))
    return rows


def _symbol_context(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """One-shot 360° view: definition + callers + callees + flow membership.

    When the name denotes several symbols the answer lists every candidate,
    records the ambiguity (``ambiguous_symbol``) and states how the shown
    ``definition`` was chosen — it never presents the first match as the
    symbol.  Neighbor caps and flow caps are named omissions.
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "symbol_context")
    args = request.arguments
    with conn:
        omissions = _graph_omissions(request, context, conn)
        nodes = _resolve_symbol_nodes(
            conn, args.get("symbol"), args.get("path"), args.get("language"), omissions
        )
        if not nodes:
            omissions.append("symbol_not_found")
        ambiguity = _symbol_sites(nodes)
        if ambiguity:
            omissions.append("ambiguous_symbol")
        primary = nodes[0] if nodes else None
        callers: list[dict[str, Any]] = []
        callees: list[dict[str, Any]] = []
        if primary:
            callers = _call_neighbors(conn, primary["id"], "in")
            callees = _call_neighbors(conn, primary["id"], "out")
    caller_count, callee_count = len(callers), len(callees)
    if caller_count > _MAX_CONTEXT_NEIGHBORS:
        omissions.append("callers_truncated")
    if callee_count > _MAX_CONTEXT_NEIGHBORS:
        omissions.append("callees_truncated")
    flows: list[str] = []
    if primary:
        try:
            from .processes import build_process_index, detect_processes

            index = build_process_index(detect_processes(context.graph_db).processes)
            member_of = index.get(primary["id"], ())
            flows = [p.display_label for p in member_of[:_MAX_CONTEXT_FLOWS]]
            if len(member_of) > _MAX_CONTEXT_FLOWS:
                omissions.append("flows_truncated")
        except Exception:  # noqa: BLE001 - flow membership is additive
            omissions.append("process_index_unavailable")
    answer = {
        "symbol": args.get("symbol"),
        "definition": primary,
        "primary_selection": (
            "ambiguous_preferred_order" if ambiguity else "unique"
        ) if primary else None,
        "candidates": _resolved_symbols(nodes),
        "additional_definitions": len(nodes) - 1 if nodes else 0,
        "callers": callers[:_MAX_CONTEXT_NEIGHBORS],
        "callees": callees[:_MAX_CONTEXT_NEIGHBORS],
        "caller_count": caller_count,
        "callee_count": callee_count,
        "flows": flows,
    }
    exact = not omissions and primary is not None and not ambiguity
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=(
            ((primary["file_path"], primary["start_line"]),)
            if primary and primary["start_line"] >= 1
            else ()
        ),
        ambiguity=ambiguity,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _processes(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """The repo's detected entry→terminal execution-flow library."""
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "processes")
    with conn:
        omissions = _graph_omissions(request, context, conn)
    from .processes import detect_processes

    result = detect_processes(context.graph_db)
    args = request.arguments
    concept = args.get("concept")
    procs = result.processes
    if isinstance(concept, str) and concept.strip():
        terms = {t.lower() for t in concept.split() if len(t) >= 3}
        procs = [
            p for p in procs
            if terms & {
                n.name.lower() for n in p.nodes
            } or any(
                term in n.file_path.lower() for n in p.nodes for term in terms
            )
        ]
        if not procs:
            omissions.append("no_matching_processes")
    try:
        limit = max(1, min(int(args.get("limit", 10) or 10), 25))
    except (TypeError, ValueError):
        limit = 10
    answer = {
        "process_count": len(result.processes),
        "matched": len(procs),
        "truncated": result.stats.truncated,
        "processes": [
            {
                "label": p.display_label,
                "entry": p.entry.rendered,
                "terminal": p.terminal.rendered,
                "step_count": p.step_count,
                "certified_ratio": round(p.certified_ratio, 4),
                "entry_kind": p.entry_kind,
                "witnessed": p.witnessed,
                "steps": [n.rendered for n in p.nodes],
            }
            for p in procs[:limit]
        ],
    }
    if result.stats.truncated:
        omissions.append("process_library_truncated")
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if not omissions else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if not omissions else Coverage.PARTIAL,
        anchors=tuple(
            (p.entry.file_path, p.entry.start_line) for p in procs[:limit]
        ),
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _route_anchor(route: Mapping[str, Any]) -> tuple[str, int] | None:
    """Where a route row points: the handler definition when bound, else
    the first client call site (an API_CALL-only route has no handler
    line).  ``None`` when neither is a real line — never line 0."""
    handler_file = str(route.get("handler_file") or "")
    handler_line = int(route.get("handler_line") or 0)
    if handler_file and handler_line >= 1:
        return handler_file, handler_line
    for consumer in route.get("consumers") or ():
        cfile = str(consumer.get("file") or "")
        cline = int(consumer.get("line") or 0)
        if cfile and cline >= 1:
            return cfile, cline
    return None


def _route_omissions(
    routes: Sequence[Mapping[str, Any]], result: Mapping[str, Any]
) -> tuple[list[str], tuple[tuple[str, int], ...]]:
    """Named unknowns for a route answer, plus its anchors.  Anything the
    answer could not bind or had to cut is an omission, so EXACT means
    every route is fully known."""
    omissions: list[str] = []
    anchors: list[tuple[str, int]] = []
    for route in routes:
        name = str(route.get("name") or route.get("route") or "")
        if not route.get("handler"):
            omissions.append(f"route_handler_unresolved:{name}")
        if name == "unknown":
            omissions.append("route_path_unknown")
        if route.get("flows_truncated"):
            omissions.append("route_flows_truncated")
        if route.get("consumers_truncated"):
            omissions.append("route_consumers_truncated")
        anchor = _route_anchor(route)
        if anchor is None:
            omissions.append(f"route_anchor_unavailable:{name}")
        else:
            anchors.append(anchor)
    if result.get("truncated"):
        omissions.append("route_map_truncated")
    if result.get("unattached_middleware"):
        omissions.append("middleware_unattached")
    return omissions, tuple(anchors[:20])


def _route_map(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Route → handler → downstream-call topology from producer edges."""
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "route_map")
    from groundtruth.mcp.endpoints.route_map import run_route_map

    conn.row_factory = sqlite3.Row  # endpoint cores use row["col"] access
    with conn:
        omissions = _graph_omissions(request, context, conn)
        result = run_route_map(conn, str(context.repository_root))
    if result.get("status") != "ok":
        omissions.append("route_tables_absent")
    path_arg = request.arguments.get("path")
    routes = result.get("routes") or []
    if isinstance(path_arg, str) and path_arg.strip():
        prefix = path_arg.strip().replace("\\", "/").rstrip("/")
        routes = [
            r for r in routes
            if str(r.get("handler_file") or "").replace("\\", "/").startswith(prefix)
            or prefix in str(r.get("handler_file") or "").replace("\\", "/")
        ]
    route_omissions, anchors = _route_omissions(routes, result)
    omissions.extend(route_omissions)
    answer = {
        "route_count": len(routes),
        "truncated": bool(result.get("truncated")),
        "routes": [
            {
                "route": r.get("name"),
                "method": r.get("method"),
                "handler": r.get("handler"),
                "handler_file": r.get("handler_file"),
                "handler_line": r.get("handler_line"),
                "route_source": r.get("route_source"),
                "discovered_via": r.get("discovered_via"),
                "confidence": r.get("confidence"),
                "middleware": r.get("middleware") or [],
                "injections": r.get("injections") or [],
                "consumers": r.get("consumers") or [],
                "downstream_calls": r.get("flows") or [],
            }
            for r in routes
        ],
        "unattached_middleware": result.get("unattached_middleware") or [],
    }
    exact = not omissions and result.get("status") == "ok"
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=anchors,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _api_impact(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Changed route/handler blast radius: consumers + affected files."""
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "api_impact")
    from groundtruth.mcp.endpoints.route_map import run_api_impact

    conn.row_factory = sqlite3.Row  # endpoint cores use row["col"] access
    args = request.arguments
    route = args.get("route")
    handler = args.get("handler")
    with conn:
        omissions = _graph_omissions(request, context, conn)
        result = run_api_impact(
            conn,
            str(context.repository_root),
            route=str(route) if isinstance(route, str) and route.strip() else None,
            handler=str(handler) if isinstance(handler, str) and handler.strip() else None,
        )
    if result.get("status") == "not_found":
        omissions.append("route_not_found")
    elif result.get("status") != "ok":
        omissions.append("route_tables_absent")
    routes = result.get("routes") or []
    route_omissions, anchors = _route_omissions(routes, result)
    omissions.extend(route_omissions)
    answer = {
        "route": route,
        "handler": handler,
        "route_count": len(routes),
        "routes": routes,
    }
    exact = not omissions and result.get("status") == "ok"
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=anchors,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


_SINK_NAME_MARKERS = frozenset(
    {
        "eval", "exec", "execute", "system", "popen", "spawn", "run",
        "subprocess", "call", "check_output", "query", "raw", "executemany",
        "send", "write", "redirect", "render", "loads", "deserialize",
        "pickle", "yaml", "shell", "command", "open",
    }
)

_CLASS_FIELD_MODS = frozenset(
    {
        "private", "public", "protected", "static", "final", "readonly",
        "abstract", "override", "virtual", "const", "let", "var", "val",
        "mut", "transient", "volatile", "lateinit", "internal", "open",
        "sealed", "inline", "weak", "lazy", "pub", "export", "declare",
    }
)

_FIELD_ANNOTATION_RE = re.compile(r"^@[\w.]+(?:\s*\([^)]*\))?\s*")
_FIELD_COLON_RE = re.compile(
    r"^([A-Za-z_][\w$]*)\s*:\s*([\w.*<>\[\]]+)"
)


def _class_field_name_type(decl: str) -> tuple[str, str] | None:
    """Extract ``(field_name, declared_type)`` from a persisted
    ``class_field`` property value — the producer keeps the declaration
    text verbatim (``private ItemService itemService;``,
    ``@Autowired\\n private Repo repo;``, ``state: int = 0``,
    ``items []Item``).  Returns ``None`` when no type is provable — a
    name without a type cannot bind a receiver chain.

    Disambiguation for the two-token ``A B`` shape: Java/TS put the
    type first (``ItemService itemService``), Go/C the name first
    (``items []Item``, ``r *Repo``).  Casing decides: a leading
    punctuation/uppercase second token marks Go order; otherwise the
    first token is the type.
    """
    text = decl.strip().splitlines()[-1].strip()
    while True:
        m = _FIELD_ANNOTATION_RE.match(text)
        if m is None:
            break
        text = text[m.end():].strip()
    words = [
        w for w in text.replace("=", " = ").split()
        if w not in _CLASS_FIELD_MODS and not w.startswith("@")
    ]
    if "=" in words:
        words = words[: words.index("=")]
    if not words:
        return None
    # Colon-typed: ``name: Type`` (Python/TS/Kotlin after modifier strip —
    # the colon may share a token with the name).
    joined = " ".join(words).rstrip(";")
    m = _FIELD_COLON_RE.match(joined)
    if m is not None:
        return m.group(1), m.group(2)
    # Two-token ``A B`` shapes.
    words = [w.rstrip(";") for w in words if w.rstrip(";")]
    if len(words) == 2:
        a, b = words
        ident_a = re.match(r"^[A-Za-z_][\w$]*$", a)
        ident_b = re.match(r"^[A-Za-z_][\w$]*$", b)
        if b.startswith(("[", "*", "map[")):
            # Unambiguous name-first — ``items []Item``, ``r *Repo``.
            return (a, b) if ident_a else None
        if a[:1].isupper() and ident_b:
            # Type-first — ``ItemService itemService``, ``String NAME``,
            # ``List<Item> items``, ``Repo* repo``.
            return b, a
        if b[:1].isupper() and ident_a:
            # Name-first by casing — ``r Repo``.
            return a, b
        if ident_a and ident_b:
            # Both lowercase — ``int count`` / ``count int`` bind the same
            # (name, type) pair either way.
            return b, a
    return None


def _taint(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Symbol-level source→sink reachability over resolved CALLS.

    Honest scope: this is call-graph reachability, not statement-level
    dataflow — a path exists means a call chain connects source to sink,
    not that attacker-controlled data provably flows. Always INCOMPLETE.

    Trust tiers are kept apart: ``paths`` are chains whose every hop is a
    CERTIFIED edge; ``uncertain_paths`` need at least one CANDIDATE /
    SPECULATIVE hop.  Low-confidence ``name_match`` edges (< 0.5: a call
    bound to a same-named symbol with no receiver/import proof, e.g. a
    stdlib/external qualified call landing on a local method) are not
    followed unless ``include_name_matched`` is true.  Field channels are
    scoped to the query: writers the sources reach, readers that reach the
    sinks (readers are unscoped — and say so — when no sink is given).
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "taint")
    with conn:
        args = request.arguments
        omissions = _graph_omissions(request, context, conn)
        omissions.extend(
            [
                "symbol_level_reachability_only",
                "statement_level_dataflow_unavailable",
            ]
        )
        source_nodes = _resolve_symbol_nodes(
            conn, args.get("source"), args.get("path"), args.get("language")
        )
        if not source_nodes:
            omissions.append("symbol_not_found")
        try:
            depth = max(1, min(int(args.get("depth", 6) or 6), 10))
        except (TypeError, ValueError):
            depth = 6

        sink_arg = args.get("sink")
        sink_nodes = _resolve_symbol_nodes(
            conn, sink_arg, args.get("path"), args.get("language")
        ) if isinstance(sink_arg, str) and sink_arg.strip() else []
        if isinstance(sink_arg, str) and sink_arg.strip() and not sink_nodes:
            omissions.append("sink_not_found")

        include_name_matched = args.get("include_name_matched") is True
        edge_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(edges)")}
        method_expr = (
            "COALESCE(e.resolution_method,'')"
            if "resolution_method" in edge_cols
            else "''"
        )
        # Forward CALLS adjacency: one hop per (source, target), keeping the
        # strongest tier and the first call line of that edge.
        hop_tier: dict[tuple[int, int], str] = {}
        hop_line: dict[tuple[int, int], int] = {}
        excluded_from: set[int] = set()
        for row in conn.execute(
            "SELECT e.source_id, e.target_id, COALESCE(e.trust_tier,''),"
            f" COALESCE(e.confidence,0.0), {method_expr}, COALESCE(e.source_line,0)"
            " FROM edges e WHERE e.type='CALLS'"
            " ORDER BY e.source_id, e.target_id, e.source_line"
        ):
            src, tgt = int(row[0]), int(row[1])
            tier, conf, method = str(row[2] or "").upper(), float(row[3] or 0.0), str(row[4] or "")
            if method == "name_match" and conf < 0.5 and not include_name_matched:
                excluded_from.add(src)
                continue
            key = (src, tgt)
            prior = hop_tier.get(key)
            if prior is None or _TIER_ORDER.get(tier, 3) < _TIER_ORDER.get(prior, 3):
                hop_tier[key] = tier
            if int(row[5] or 0) > 0:
                hop_line.setdefault(key, int(row[5]))
        forward: dict[int, list[int]] = {}
        backward: dict[int, list[int]] = {}
        for src, tgt in sorted(hop_tier):
            forward.setdefault(src, []).append(tgt)
            backward.setdefault(tgt, []).append(src)
        node_label = {
            int(r[0]): (str(r[1] or ""), str(r[2] or ""), int(r[3] or 0))
            for r in conn.execute(
                "SELECT id, name, file_path, COALESCE(start_line,0) FROM nodes"
            )
        }

        def _bfs(
            start: int, adjacency: dict[int, list[int]], certified_only: bool
        ) -> tuple[dict[int, int], set[int]]:
            prev: dict[int, int] = {}
            visited = {start}
            queue: list[tuple[int, int]] = [(start, 0)]
            while queue:
                current, dist = queue.pop(0)
                if dist >= depth:
                    continue
                for nxt in adjacency.get(current, ()):
                    if nxt in visited:
                        continue
                    key = (current, nxt) if adjacency is forward else (nxt, current)
                    if certified_only and hop_tier.get(key) != "CERTIFIED":
                        continue
                    visited.add(nxt)
                    prev[nxt] = current
                    queue.append((nxt, dist + 1))
            return prev, visited

        def _chain(prev: dict[int, int], start: int, end: int) -> list[int]:
            chain = [end]
            while chain[-1] != start:
                chain.append(prev[chain[-1]])
            chain.reverse()
            return chain

        def _path_record(chain: list[int]) -> dict[str, Any]:
            tiers = [hop_tier.get((a, b), "") for a, b in zip(chain, chain[1:])]
            return {
                "source": node_label.get(chain[0], ("",))[0],
                "sink": node_label.get(chain[-1], ("",))[0],
                "hops": len(chain) - 1,
                "path": [node_label.get(n, (str(n),))[0] for n in chain],
                "path_sites": [
                    f"{node_label[n][1]}:{node_label[n][2]}" for n in chain if n in node_label
                ],
                "hop_tiers": tiers,
                "trust": "certified" if all(t == "CERTIFIED" for t in tiers) else "uncertain",
                "_ids": chain,
            }

        paths: list[dict[str, Any]] = []
        uncertain_paths: list[dict[str, Any]] = []
        sink_ids = {n["id"] for n in sink_nodes}
        reachable: set[int] = set()
        reachable_certified: set[int] = set()
        writer_scope: set[int] = set()
        for source in source_nodes[:10]:
            sid = source["id"]
            prev_c, seen_c = _bfs(sid, forward, True)
            prev_a, seen_a = _bfs(sid, forward, False)
            writer_scope |= seen_a
            reachable |= seen_a - {sid}
            reachable_certified |= seen_c - {sid}
            if excluded_from & seen_a:
                omissions.append("name_match_edges_excluded")
            certified_chains = set()
            for sink_id in sorted(sink_ids & seen_c):
                chain = _chain(prev_c, sid, sink_id)
                certified_chains.add(tuple(chain))
                paths.append(_path_record(chain))
            for sink_id in sorted(sink_ids & seen_a):
                chain = _chain(prev_a, sid, sink_id)
                if tuple(chain) in certified_chains:
                    continue
                record = _path_record(chain)
                if record["trust"] == "certified":
                    continue  # a longer certified chain is already reported
                uncertain_paths.append(record)
        for bucket, label in ((paths, "certified"), (uncertain_paths, "uncertain")):
            bucket.sort(key=lambda p: (p["hops"], p["source"], p["path"]))
            if len(bucket) > 25:
                omissions.append(f"taint_{label}_paths_truncated")
                del bucket[25:]
        reachable_names = {n: node_label.get(n, ("",))[0] for n in reachable}
        if sink_ids:
            reader_scope: set[int] | None = set()
            for sink_id in sink_ids:
                reader_scope |= _bfs(sink_id, backward, False)[1]
        else:
            reader_scope = None
            omissions.append("field_channel_readers_unscoped")

        heuristic_sinks: list[str] = []
        if not sink_ids:
            heuristic_sinks = sorted(
                {
                    name
                    for name in reachable_names.values()
                    if name.lower() in _SINK_NAME_MARKERS
                }
            )
            if heuristic_sinks:
                omissions.append("sink_pattern_heuristic")
            else:
                omissions.append("no_sink_specified")
        heuristic_certified = sorted(
            {
                node_label.get(n, ("",))[0]
                for n in reachable_certified
                if node_label.get(n, ("",))[0].lower() in _SINK_NAME_MARKERS
            }
        ) if not sink_ids else []

        # Field-mediated channels: a WRITES(f) → READS(f) pair is a data
        # channel CALLS cannot see (e.g. source writes request field, sink
        # reads it). Field identity binds on (field, owner class) — the
        # enclosing type resolved through parent_id — so same-name fields on
        # different classes and cross-file collisions never form channels.
        # Sites whose owner cannot be resolved join weaker (field+file) and
        # keep the name-matched flag.
        nodes_by_id = {
            int(r[0]): {"name": str(r[1] or ""), "file_path": str(r[2] or ""),
                        "language": str(r[3] or ""), "start_line": int(r[4] or 0),
                        "end_line": int(r[5] or 0), "label": str(r[6] or ""),
                        "parent_id": (int(r[7]) if r[7] is not None else None),
                        "qualified_name": str(r[8] or "")}
            for r in conn.execute(
                "SELECT id, name, file_path, COALESCE(language,''),"
                " COALESCE(start_line,0), COALESCE(end_line,0),"
                " COALESCE(label,''), parent_id, COALESCE(qualified_name,'')"
                " FROM nodes"
            )
        }
        # Receiver/type evidence persisted by the producer, loaded once:
        # field_read/side_effect props carry ``reads: <recv>.<field>`` /
        # ``mutates: <recv>.<field>``; param props carry ``name[:type]``;
        # class_field props carry declared field text per class.
        _RECV_PROP_RE = re.compile(
            r"^(?:reads|mutates):\s*([A-Za-z_][\w.]*)\.(\w+)"
        )
        recv_by_scope_field: dict[tuple[int, str], str] = {}
        param_types: dict[int, dict[str, str]] = {}
        class_field_types: dict[int, dict[str, str]] = {}
        try:
            prop_rows = conn.execute(
                "SELECT node_id, kind, value FROM properties"
                " WHERE kind IN"
                " ('field_read','side_effect','param','class_field')"
            ).fetchall()
        except sqlite3.Error:
            prop_rows = []
        for prow in prop_rows:
            node_i, kind_p, value_p = int(prow[0]), str(prow[1]), str(prow[2])
            if kind_p in ("field_read", "side_effect"):
                m = _RECV_PROP_RE.match(value_p.strip())
                if m is not None:
                    recv_by_scope_field.setdefault(
                        (node_i, m.group(2)), m.group(1)
                    )
            elif kind_p == "param":
                m = re.match(
                    r"^(?:\*{1,2}|\.{3})?([A-Za-z_][\w$]*)"
                    r"(?::+(.*?))?"
                    r"(?:\s+\[required\]|\s+opt(?:=.*)?)?$",
                    value_p.strip(),
                )
                if m is not None and m.group(2):
                    param_types.setdefault(node_i, {})[m.group(1)] = m.group(
                        2
                    ).strip()
            else:
                parsed_cf = _class_field_name_type(value_p)
                if parsed_cf is not None:
                    class_field_types.setdefault(node_i, {})[
                        parsed_cf[0]
                    ] = parsed_cf[1]

        def _class_key(node_id: int) -> str:
            n = nodes_by_id[node_id]
            return n["qualified_name"] or f"{n['file_path']}::{n['name']}"

        class_by_simple: dict[str, int] = {}
        for nid, n in nodes_by_id.items():
            if n["label"] == "Class":
                class_by_simple.setdefault(n["name"], nid)

        def _enclosing_class(scope_node_id: int | None, fallback_id: int) -> int | None:
            cur = scope_node_id if scope_node_id in nodes_by_id else fallback_id
            depth = 0
            while cur in nodes_by_id and depth < 12:
                if nodes_by_id[cur]["label"] == "Class":
                    return cur
                cur = nodes_by_id[cur]["parent_id"]
                depth += 1
            return None

        def _type_name_to_class(type_name: str) -> int | None:
            """Resolve a declared type name to a Class node id — simple or
            qualified-name match, decorations stripped."""
            t = (
                type_name.strip()
                .lstrip("*")
                .strip("[]")
                .split("<")[0]
                .split("[")[0]
            )
            if not t:
                return None
            simple = t.rsplit(".", 1)[-1]
            for nid, n in nodes_by_id.items():
                if n["label"] != "Class":
                    continue
                if n["qualified_name"] == t or n["qualified_name"].endswith(
                    "." + t
                ):
                    return nid
            return class_by_simple.get(simple)

        def _owner_key(
            scope_node_id: int | None,
            fallback_id: int,
            field: str = "",
            persisted_receiver: str = "",
        ) -> str | None:
            """Field owner identity — the stack-graph paused-lookup rule:
            receiver name resolves first, then the field belongs to that
            binding.  ``self``/``this``/``cls`` and ``self.<f>`` bind to the
            enclosing class (or its declared field type); named receivers
            resolve through param annotations then class name-match.
            ``None`` = unresolvable — those channels join weaker."""
            cls_id = _enclosing_class(scope_node_id, fallback_id)
            scope = scope_node_id if scope_node_id in nodes_by_id else fallback_id
            # Producer-persisted receiver (access_sites v2) beats property-
            # text correlation; the correlation map is the v1 fallback.
            recv = persisted_receiver or recv_by_scope_field.get(
                (scope, field), ""
            )
            if recv:
                parts = recv.split(".")
                head = parts[0]
                if head in ("self", "this", "cls"):
                    if len(parts) == 1:
                        return _class_key(cls_id) if cls_id is not None else None
                    # self.<a>.<b>.field — walk declared field types from
                    # the enclosing class; unresolved hops are unknown,
                    # never silently the enclosing class.
                    cur_cls = cls_id
                    for seg in parts[1:]:
                        if cur_cls is None:
                            return None
                        ftype = class_field_types.get(cur_cls, {}).get(seg)
                        cur_cls = _type_name_to_class(ftype) if ftype else None
                    return _class_key(cur_cls) if cur_cls is not None else None
                # Named receiver: param annotation on the scope, then
                # class name-match on the receiver itself.  Unresolved is
                # unknown — binding it to the enclosing class would
                # fabricate a cross-class join.
                ptype = param_types.get(scope, {}).get(head)
                tgt = _type_name_to_class(ptype) if ptype else None
                if tgt is not None:
                    return _class_key(tgt)
                tgt = _type_name_to_class(head)
                return _class_key(tgt) if tgt is not None else None
            if cls_id is not None:
                return _class_key(cls_id)
            return None

        field_writers: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        field_readers: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT e.type, e.source_id, e.access_sites FROM edges e"
            " WHERE e.type IN ('READS','WRITES')"
            " AND e.access_sites IS NOT NULL AND e.access_sites != ''"
        ):
            try:
                site = json.loads(str(row[2]))
            except (TypeError, ValueError):
                continue
            # v2 payloads carry a ``sites`` array — one entry per field
            # access on the edge, each with its own receiver.  v1 payloads
            # have only the top-level field/line; fall back to those.
            sub_sites = site.get("sites")
            if isinstance(sub_sites, list) and sub_sites:
                accesses = [
                    (
                        str(s.get("field") or ""),
                        s.get("line"),
                        str(s.get("receiver") or site.get("receiver") or ""),
                    )
                    for s in sub_sites
                    if isinstance(s, dict)
                ]
            else:
                accesses = [
                    (
                        str(site.get("field") or ""),
                        site.get("line"),
                        str(site.get("receiver") or ""),
                    )
                ]
            scope_raw = site.get("scope_node_id")
            scope_id = int(scope_raw) if isinstance(scope_raw, int) else None
            accessor = int(row[1])
            # Query scoping: a writer must be reachable from a source, a
            # reader must reach a sink.  Repository-wide joins were noise.
            if row[0] == "WRITES" and accessor not in writer_scope:
                continue
            if row[0] == "READS" and reader_scope is not None and accessor not in reader_scope:
                continue
            node = nodes_by_id.get(accessor, {})
            seen_access: set[tuple[str, object]] = set()
            for field, aline, arecv in accesses:
                if not field or (field, aline) in seen_access:
                    continue
                seen_access.add((field, aline))
                rec = {
                    "symbol": node.get("name", "?"),
                    "file_path": node.get("file_path", "?"),
                    "line": aline,
                }
                owner = _owner_key(
                    scope_id,
                    int(row[1]),
                    field,
                    persisted_receiver=arecv,
                )
                (field_writers if row[0] == "WRITES" else field_readers).setdefault(
                    (field, owner), []
                ).append(rec)
        data_channels: list[dict[str, Any]] = []
        # Join rule: two resolved owners must match (same-name fields on
        # different classes never form a channel); an unresolved side joins
        # weaker so cross-file flows still surface, flagged owner_unknown.
        for (field, owner_w), ws in field_writers.items():
            for (field_r, owner_r), rs in field_readers.items():
                if field_r != field:
                    continue
                if owner_w is not None and owner_r is not None and owner_w != owner_r:
                    continue
                bound = owner_w is not None and owner_w == owner_r
                if len(ws) > 5 or len(rs) > 5:
                    omissions.append("field_channels_truncated")
                for w in ws[:5]:
                    for r in rs[:5]:
                        data_channels.append(
                            {
                                "field": field,
                                "owner": owner_w if bound else None,
                                "identity": "owner_bound" if bound else "owner_unknown",
                                "writer": w,
                                "reader": r,
                                "same_file": w["file_path"] == r["file_path"],
                            }
                        )
        data_channels.sort(
            key=lambda c: (str(c["field"]), str(c["owner"]),
                           c["writer"]["symbol"], c["reader"]["symbol"])
        )
        if len(data_channels) > 25:
            omissions.append("field_channels_truncated")
            del data_channels[25:]
        if any(c["identity"] == "owner_bound" for c in data_channels):
            omissions.append("field_flow_owner_bound")
        if any(c["identity"] == "owner_unknown" for c in data_channels):
            omissions.append("field_flow_name_matched")

        # Per-hop statement evidence: the CALLS edge's source_line is a real
        # call site — for Python hops report which variables reach it via the
        # CFG substrate's backward slice (bounded; per-hop abstains).  Hops
        # are node identities, so same-named symbols never borrow lines.
        hop_detail_budget = 8
        for path in (*paths, *uncertain_paths):
            chain_ids = path.pop("_ids")
            details: list[dict[str, Any]] = []
            for a, b in zip(chain_ids, chain_ids[1:]):
                hop: dict[str, Any] = {
                    "from": node_label.get(a, ("",))[0],
                    "to": node_label.get(b, ("",))[0],
                    "trust_tier": hop_tier.get((a, b), ""),
                }
                call_line = hop_line.get((a, b), 0)
                if call_line:
                    hop["call_line"] = call_line
                    node = nodes_by_id.get(a)
                    if node and node["language"] == "python" and hop_detail_budget > 0:
                        src_file = context.repository_root / node["file_path"]
                        if src_file.is_file():
                            try:
                                from .cfg_analysis import slice_at_line as _sal

                                hop_slice = _sal(
                                    src_file.read_text(encoding="utf-8", errors="replace"),
                                    node["name"],
                                    call_line,
                                    "backward",
                                )
                                hop["variables_at_call"] = hop_slice["variables"]
                                hop_detail_budget -= 1
                            except Exception:
                                hop["slice_error"] = True
                details.append(hop)
            if details:
                path["hop_detail"] = details
        if hop_detail_budget <= 0:
            omissions.append("hop_detail_truncated")

    answer = {
        "source": args.get("source"),
        "sink": sink_arg,
        "paths_found": len(paths) + len(uncertain_paths),
        "certified_paths_found": len(paths),
        "paths": paths,
        "uncertain_paths": uncertain_paths,
        "heuristic_sinks_reached": heuristic_sinks[:20],
        "heuristic_sinks_reached_certified": heuristic_certified[:20],
        "reachable_symbol_count": len(reachable_names),
        "field_channels": data_channels,
    }
    return _Produced(
        answer,
        EvidenceSemantics.INCOMPLETE,
        Coverage.PARTIAL,
        anchors=tuple(
            (n["file_path"], n["start_line"]) for n in source_nodes[:10]
        ),
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _rename(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Graph-aware rename preview: every proven edit site for a symbol.

    Enumerates definition sites plus all incoming-edge source locations
    (call sites, imports, decorators, type uses). Text occurrences inside
    strings/comments/docstrings cannot be proven from the graph — always
    INCOMPLETE so the agent knows to sweep text too.
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "rename")
    with conn:
        args = request.arguments
        omissions = _graph_omissions(request, context, conn)
        nodes = _resolve_symbol_nodes(
            conn, args.get("symbol"), args.get("path"), args.get("language"), omissions
        )
        omissions.append("text_references_not_enumerated")
        if not nodes:
            omissions.append("symbol_not_found")
        if len(nodes) > 10:
            omissions.append("rename_definitions_truncated")
        definitions = [
            {
                "name": n["name"],
                "file_path": n["file_path"],
                "line": n["start_line"],
                "kind": n["kind"],
            }
            for n in nodes[:10]
        ]
        sites_by_type: dict[str, list[dict[str, Any]]] = {}
        files: set[str] = set()
        total = 0
        for node in nodes[:10]:
            for row in conn.execute(
                "SELECT e.type, n.name, n.file_path, e.source_line,"
                " COALESCE(e.trust_tier,''), COALESCE(e.confidence,0.0)"
                " FROM edges e JOIN nodes n ON n.id = e.source_id"
                f" WHERE e.target_id = ? AND n.label IN ({_REFERENCE_LABEL_SQL})",
                (node["id"],),
            ):
                etype, name, fp, line, tier, conf = row
                sites_by_type.setdefault(str(etype), []).append(
                    {
                        "referencing_symbol": str(name or ""),
                        "file_path": str(fp or ""),
                        "line": int(line or 0),
                        "trust_tier": str(tier or ""),
                        "confidence": float(conf or 0.0),
                    }
                )
                if fp:
                    files.add(str(fp))
                total += 1
        for etype, rows in sites_by_type.items():
            rows.sort(key=lambda r: (r["file_path"], r["line"]))
            if len(rows) > 50:
                omissions.append(f"edit_sites_truncated:{etype}")
                del rows[50:]

    answer = {
        "symbol": args.get("symbol"),
        "new_name": args.get("new_name"),
        "definitions": definitions,
        "edit_sites_by_type": sites_by_type,
        "edit_site_count": total,
        "files_to_touch": sorted(files),
    }
    remaining_omissions = [o for o in omissions if o != "text_references_not_enumerated"]
    ambiguity = _symbol_sites(nodes)
    return _Produced(
        answer,
        # Text references can never be proven absent from the graph — the
        # graph surface is complete, the rename itself is always INCOMPLETE.
        EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE
        if nodes and not remaining_omissions and not ambiguity
        else Coverage.PARTIAL,
        anchors=tuple(
            (n["file_path"], n["start_line"]) for n in nodes[:10] if n["start_line"] >= 1
        ),
        ambiguity=ambiguity,
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _shape_check(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Contract/shape consistency checks provable from persisted edges.

    Checks: (a) interface conformance — a class with outgoing
    IMPLEMENTS/DECLARED_IMPLEMENTS edges must own each method the interface
    declares; (b) override arity — METHOD_OVERRIDES/OVERRIDES-linked symbols
    must keep compatible positional arity (parsed from signatures).
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "shape_check")
    with conn:
        args = request.arguments
        nodes = _resolve_symbol_nodes(
            conn, args.get("symbol"), args.get("path"), args.get("language")
        )
        omissions = _graph_omissions(request, context, conn)
        if not nodes:
            omissions.append("symbol_not_found")
        checks: list[dict[str, Any]] = []
        for node in nodes[:5]:
            node_id = node["id"]
            if node["kind"] == "Class":
                # Interface conformance: methods the interface declares that
                # this class does not contain.
                for row in conn.execute(
                    "SELECT e.target_id, n.name FROM edges e"
                    " JOIN nodes n ON n.id = e.target_id"
                    " WHERE e.source_id = ?"
                    " AND e.type IN ('IMPLEMENTS','DECLARED_IMPLEMENTS')",
                    (node_id,),
                ):
                    iface_id, iface_name = int(row[0]), str(row[1] or "")
                    required = {
                        str(r[0])
                        for r in conn.execute(
                            "SELECT name FROM nodes WHERE parent_id = ?"
                            " AND label IN ('Method','Function')",
                            (iface_id,),
                        )
                    }
                    own = {
                        str(r[0])
                        for r in conn.execute(
                            "SELECT name FROM nodes WHERE parent_id = ?"
                            " AND label IN ('Method','Function')",
                            (node_id,),
                        )
                    }
                    # Inherited methods satisfy the contract — walk EXTENDS
                    # hops only; an IMPLEMENTS target's members are the
                    # requirement, not the implementation.
                    for r in conn.execute(
                        "SELECT target_id FROM edges WHERE source_id = ?"
                        " AND type = 'EXTENDS'",
                        (node_id,),
                    ):
                        own |= {
                            str(m[0])
                            for m in conn.execute(
                                "SELECT name FROM nodes WHERE parent_id = ?"
                                " AND label IN ('Method','Function')",
                                (int(r[0]),),
                            )
                        }
                    missing = sorted(required - own)
                    checks.append(
                        {
                            "check": "interface_conformance",
                            "interface": iface_name,
                            "status": "fail" if missing else "pass",
                            "missing_methods": missing,
                            "required_count": len(required),
                            "implemented_count": len(required & own),
                        }
                    )
            # Override arity: this symbol's overridden siblings must keep
            # compatible positional parameter counts.
            sig = str(node.get("signature") or "")
            own_arity = _signature_arity(sig)
            if own_arity is not None:
                for row in conn.execute(
                    "SELECT n.name, n.signature, n.file_path, n.start_line"
                    " FROM edges e JOIN nodes n ON n.id = e.target_id"
                    " WHERE e.source_id = ?"
                    " AND e.type IN ('METHOD_OVERRIDES','OVERRIDES')",
                    (node_id,),
                ):
                    other_sig = str(row[1] or "")
                    other_arity = _signature_arity(other_sig)
                    if other_arity is None:
                        continue
                    checks.append(
                        {
                            "check": "override_arity",
                            "overrides": str(row[0] or ""),
                            "overrides_file": str(row[2] or ""),
                            "status": "pass" if own_arity == other_arity else "fail",
                            "detail": (
                                f"{node['name']}{sig} provides {own_arity} positional "
                                f"param(s); overridden signature accepts {other_arity}"
                            ),
                        }
                    )
        if not checks:
            omissions.append("no_verifiable_contracts")
        else:
            omissions.append("shape_check_scope_limited")

    failed = [c for c in checks if c["status"] == "fail"]
    answer = {
        "symbol": args.get("symbol"),
        "check_count": len(checks),
        "failed": len(failed),
        "checks": checks,
    }
    exact = (
        bool(checks)
        and not failed
        and not [o for o in omissions if o != "shape_check_scope_limited"]
    )
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if checks else Coverage.PARTIAL,
        anchors=tuple(
            (n["file_path"], n["start_line"]) for n in nodes[:5]
        ),
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _signature_arity(signature: str) -> int | None:
    """Positional parameter count from a signature string, or None."""
    if "(" not in signature or ")" not in signature:
        return None
    inner = signature.split("(", 1)[1].rsplit(")", 1)[0].strip()
    if not inner:
        return 0
    params = [p.strip() for p in inner.split(",") if p.strip()]
    params = [
        p for p in params
        if p not in {"self", "cls", "*", "/"} and not p.startswith(("*", "**"))
    ]
    return len(params)


_TOOL_DECORATOR_NAMES = frozenset(
    {
        "tool", "command", "action", "register_tool", "function_tool",
        "app.tool", "mcp.tool", "server.tool", "add_tool",
    }
)


def _tool_map(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Agent/MCP-style exposed tools, detected via DECORATES edges.

    A tool surface is claimed only when a decorator whose name marks a tool
    registry (``tool``/``command``/``register_tool``/…) decorates the symbol.
    Undecorated registration sites (``server.add_tool(f)`` calls) are not
    provable from the graph — reported as an omission, never guessed.
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "tool_map")
    with conn:
        args = request.arguments
        omissions = _graph_omissions(request, context, conn)
        omissions.append("registration_sites_untracked")
        path_arg = args.get("path")
        lang_arg = args.get("language")
        tools: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT n.name, n.file_path, n.start_line, n.signature,"
            " n.language, dec.name AS decorator"
            " FROM edges e JOIN nodes n ON n.id = e.target_id"
            " JOIN nodes dec ON dec.id = e.source_id"
            " WHERE e.type='DECORATES'"
            " ORDER BY n.file_path, n.start_line"
        ):
            name, fp, line, sig, lang, decorator = row
            dec_name = str(decorator or "")
            dec_tail = dec_name.rsplit(".", 1)[-1].lower()
            if dec_name.lower() not in _TOOL_DECORATOR_NAMES and dec_tail not in _TOOL_DECORATOR_NAMES:
                continue
            if isinstance(path_arg, str) and path_arg.strip():
                prefix = path_arg.strip().replace("\\", "/").rstrip("/")
                if prefix not in str(fp or "").replace("\\", "/"):
                    continue
            if isinstance(lang_arg, str) and lang_arg.strip():
                if str(lang or "").lower() != lang_arg.strip().lower():
                    continue
            tools.append(
                {
                    "tool": str(name or ""),
                    "decorator": dec_name,
                    "file_path": str(fp or ""),
                    "line": int(line or 0),
                    "signature": str(sig or ""),
                    "language": str(lang or ""),
                }
            )
            if len(tools) >= 50:
                omissions.append("tool_map_truncated")
                break
        if not tools:
            omissions.append("no_tools_detected")

    answer = {
        "tool_count": len(tools),
        "tools": tools,
    }
    return _Produced(
        answer,
        EvidenceSemantics.INCOMPLETE,
        Coverage.PARTIAL,
        anchors=tuple((t["file_path"], t["line"]) for t in tools[:20]),
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _class_name_for(conn: sqlite3.Connection, node_id: int) -> str | None:
    """The enclosing class name when the node's parent is a Class."""
    row = conn.execute(
        "SELECT n2.name FROM nodes n JOIN nodes n2 ON n2.id = n.parent_id"
        " WHERE n.id = ? AND n2.label = 'Class'",
        (node_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _slice(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    """Statement-level backward/forward slice over one function.

    Python runs the real CFG/PDG substrate (cfg_analysis) on the current file
    text — not a call-graph approximation.  javascript/typescript/java/go run
    the persisted-CFG substrate (cfg_store): the Go indexer's cfg_blocks /
    cfg_edges / cfg_defs rows composed into the same dominator /
    control-dependence / reaching-definition machinery.  Uses come from the
    persisted ``cfg_uses`` rows when the function has any
    (``approximate_use_coverage``) and from a lexical source scan otherwise
    (``approximate_use_detection``) — either way always INCOMPLETE.
    ``call_sites`` marks the interprocedural boundary honestly;
    ``limitations`` lists every unmodeled construct so an exact claim is only
    made when the analysis is complete.

    With ``interprocedural: true`` the slice composes through graph.db CALLS
    edges (bounded: max_depth 3, 25 cross-hops).  Python composes over the
    AST substrate; the persisted-CFG languages compose over cfg_* rows via
    ``cfg_store.interprocedural_slice_stored`` — keyword/positional/variadic
    actual→formal binding from persisted ``param`` properties (else entry
    ``cfg_defs``), with actuals from ``edges.actual_args`` when persisted
    (v15.4) and text-extracted otherwise (``approximate_actual_extraction``).
    Argument mapping is
    name/positional — never type-proven — so interprocedural results are
    always INCOMPLETE (``interprocedural_name_matched``).
    """
    conn = _open_graph(context)
    if conn is None:
        return _unavailable_graph(request, "slice")
    from .cfg_analysis import (
        CFGAnalysisError,
        interprocedural_slice,
        slice_at_line,
    )
    from .cfg_store import (
        has_persisted_cfg,
        interprocedural_slice_stored,
        slice_stored,
    )

    with conn:
        args = request.arguments
        omissions = _graph_omissions(request, context, conn)
        nodes = [
            n for n in _resolve_symbol_nodes(
                conn, args.get("symbol"), args.get("path"), args.get("language")
            )
            if n["kind"] in ("Function", "Method")
        ]
        if not nodes:
            omissions.append("symbol_not_found")
        try:
            line = int(args.get("line"))
        except (TypeError, ValueError):
            line = 0
            omissions.append("invalid_line")
        direction = str(args.get("direction") or "backward").lower()
        if direction not in {"backward", "forward"}:
            omissions.append(f"invalid_direction:{direction}")
            direction = "backward"
        variables = args.get("variables")
        if not isinstance(variables, list):
            variables = None
        interprocedural = bool(args.get("interprocedural"))
        try:
            max_hops = int(args.get("max_hops"))
            if max_hops < 0:
                max_hops = 25
        except (TypeError, ValueError):
            max_hops = 25
        try:
            max_depth = int(args.get("max_depth"))
            if max_depth < 1:
                max_depth = 3
        except (TypeError, ValueError):
            max_depth = 3

        def _resolve_callees(
            file: str, function: str, call_line: int
        ) -> list[tuple[str, str, int, int]]:
            """graph.db CALLS targets for the call at ``call_line`` inside
            ``(file, function)`` — the CalleeResolver the CFG substrate
            composes against."""
            src_ids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT id FROM nodes WHERE file_path = ? AND name = ?"
                    " AND label IN ('Function','Method')",
                    (file, function),
                )
            ]
            if not src_ids:
                return []
            marks = ",".join("?" for _ in src_ids)
            out: list[tuple[str, str, int, int]] = []
            seen: set[tuple[str, str, int, int]] = set()
            for row in conn.execute(
                "SELECT n.file_path, n.name, n.start_line, n.end_line"
                " FROM edges e JOIN nodes n ON n.id = e.target_id"
                f" WHERE e.source_id IN ({marks}) AND e.type = 'CALLS'"
                " AND e.source_line = ?"
                " ORDER BY n.file_path, n.start_line, n.name",
                (*src_ids, int(call_line)),
            ):
                target = (
                    str(row[0]), str(row[1]), int(row[2] or 0), int(row[3] or 0)
                )
                if target not in seen:
                    seen.add(target)
                    out.append(target)
            return out

        class _RepoSources(Mapping):
            """Lazy path -> text view over the worktree.

            ``interprocedural_slice`` reads callee sources through this
            mapping; reads are confined to the repository root and cached so
            repeat lookups within one composition are byte-identical.
            """

            def __init__(self, root: Path) -> None:
                self._root = root
                self._cache: dict[str, str] = {}

            def __getitem__(self, rel: str) -> str:
                if rel not in self._cache:
                    path = _safe_scope(self._root, rel)
                    if path is None or not path.is_file():
                        raise KeyError(rel)
                    self._cache[rel] = path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                return self._cache[rel]

            def __iter__(self) -> Any:
                return iter(())

            def __len__(self) -> int:
                return 0

        slices: list[dict[str, Any]] = []
        for node in nodes[:3]:
            lang = str(node["language"] or "").strip().lower()
            if lang != "python" and lang not in _STORED_CFG_LANGUAGES:
                omissions.append(f"unsupported_language:{node['language']}")
                continue
            if lang in _STORED_CFG_LANGUAGES and not has_persisted_cfg(
                conn, node["id"]
            ):
                omissions.append("no_persisted_cfg")
                continue
            source_path = context.repository_root / node["file_path"]
            if not source_path.is_file():
                omissions.append(f"source_unavailable:{node['file_path']}")
                continue
            if line and not (node["start_line"] <= line <= node["end_line"]):
                omissions.append(f"line_outside_function:{node['name']}")
                continue
            try:
                if lang == "python":
                    result = slice_at_line(
                        source_path.read_text(encoding="utf-8", errors="replace"),
                        node["name"],
                        line,
                        direction,
                        class_name=_class_name_for(conn, node["id"]),
                        variables=variables,
                    )
                else:
                    result = slice_stored(
                        conn,
                        node["id"],
                        line,
                        direction,
                        source=source_path.read_text(
                            encoding="utf-8", errors="replace"
                        ),
                        function_name=node["name"],
                        language=lang,
                        variables=variables,
                    )
            except CFGAnalysisError as exc:
                omissions.append(f"analysis_failed:{node['name']}:{exc}")
                continue
            record: dict[str, Any] = {
                "function": node["name"],
                "file_path": node["file_path"],
                "direction": direction,
                "criterion_line": line,
                "slice_lines": sorted(result["lines"]),
                "variables": result["variables"],
                "call_sites": result["call_sites"],
                "limitations": result["limitations"],
            }
            if lang != "python":
                record["substrate"] = "persisted_cfg"
            if interprocedural and lang != "python":
                try:
                    ip = interprocedural_slice_stored(
                        conn,
                        _RepoSources(context.repository_root),
                        node["id"],
                        line,
                        direction,
                        source=source_path.read_text(
                            encoding="utf-8", errors="replace"
                        ),
                        function_name=node["name"],
                        language=lang,
                        max_depth=max_depth,
                        max_hops=max_hops,
                    )
                except CFGAnalysisError as exc:
                    omissions.append(f"analysis_failed:{node['name']}:{exc}")
                    continue
            elif interprocedural:
                try:
                    ip = interprocedural_slice(
                        _RepoSources(context.repository_root),
                        _resolve_callees,
                        node["file_path"],
                        node["name"],
                        line,
                        direction,
                        max_hops=max_hops,
                        max_depth=max_depth,
                        entry_def_line=node["start_line"],
                    )
                except CFGAnalysisError as exc:
                    omissions.append(f"analysis_failed:{node['name']}:{exc}")
                    continue
            if interprocedural:
                record["interprocedural"] = True
                record["per_file"] = ip["per_file"]
                record["cross_function"] = ip["cross_function"]
                record["limitations"] = sorted(
                    set(record["limitations"]) | set(ip["limitations"])
                )
                # Argument mapping is name/positional, never type-proven —
                # always an honest omission.
                omissions.append("interprocedural_name_matched")
                if ip.get("truncated"):
                    omissions.append("interprocedural_budget")
            slices.append(record)
            if record["limitations"]:
                omissions.append("slice_limitations_present")

    answer = {
        "symbol": args.get("symbol"),
        "line": line,
        "direction": direction,
        "interprocedural": interprocedural,
        "slice_count": len(slices),
        "slices": slices,
    }
    exact = bool(slices) and not omissions and not interprocedural
    return _Produced(
        answer,
        EvidenceSemantics.EXACT if exact else EvidenceSemantics.INCOMPLETE,
        Coverage.COMPLETE if exact else Coverage.PARTIAL,
        anchors=tuple(
            (s["file_path"], s["criterion_line"]) for s in slices
        ),
        omissions=tuple(sorted(set(omissions))),
        revision=_producer_revision(request),
    )


def _statement_slices(
    edited: dict[str, tuple[str | None, str]],
    conn: sqlite3.Connection,
    omissions: list[str],
    sides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Forward statement-level slices for changed lines in Python files.

    For each ``+``/replaced line in the post-edit text, find the innermost
    enclosing Function/Method node in the graph and forward-slice from that
    line — what the edited statement can affect downstream inside the same
    function.  The enclosing node is looked up in the graph's own line
    numbering (the pre-image line when the graph predates the edit); the
    slice itself runs on the post-edit text.  Non-Python files and
    unresolvable spans are named omissions.
    """
    from .cfg_analysis import CFGAnalysisError, slice_at_line

    out: list[dict[str, Any]] = []
    for name in sorted(edited):
        before, after = edited[name]
        if not name.endswith((".py", ".pyi")):
            continue  # CFG substrate is Python-only; other langs omitted below
        use_old = (sides or {}).get(name) == "old"
        for line, old_line in _changed_after_lines(before or "", after)[:3]:
            graph_line = old_line if use_old else line
            row = conn.execute(
                "SELECT id, name, label FROM nodes"
                " WHERE file_path = ? AND start_line <= ? AND end_line >= ?"
                " AND label IN ('Function','Method')"
                " ORDER BY (end_line - start_line) ASC LIMIT 1",
                (name, graph_line, graph_line),
            ).fetchone()
            if row is None:
                omissions.append(f"slice_scope_unresolved:{name}:{line}")
                continue
            try:
                result = slice_at_line(
                    after,
                    str(row[1]),
                    line,
                    "forward",
                    class_name=_class_name_for(conn, int(row[0])),
                )
            except CFGAnalysisError as exc:
                omissions.append(f"slice_analysis_failed:{name}:{exc}")
                continue
            out.append(
                {
                    "file_path": name,
                    "line": line,
                    "function": str(row[1]),
                    "affected_lines": sorted(result["lines"]),
                    "variables": result["variables"],
                    "call_sites": result["call_sites"],
                    "limitations": result["limitations"],
                }
            )
        if len(out) >= 8:
            omissions.append("statement_slices_truncated")
            break
    return out


_PRODUCERS: Mapping[ActionKind, Callable[[ActionRequest, DeterministicQueryContext], _Produced]] = {
    ActionKind.EXACT_LITERAL_SEARCH: _literal_search,
    ActionKind.SYNTAX_QUERY: _syntax,
    ActionKind.PATCH_IMPACT: _patch_impact,
    ActionKind.VERIFICATION_STATUS: _verification,
    ActionKind.DEFINITION: _definition,
    ActionKind.REFERENCES: _references,
    ActionKind.CALLERS: _callers,
    ActionKind.SYMBOL_CONTEXT: _symbol_context,
    ActionKind.PROCESSES: _processes,
    ActionKind.ROUTE_MAP: _route_map,
    ActionKind.API_IMPACT: _api_impact,
    ActionKind.TAINT: _taint,
    ActionKind.RENAME: _rename,
    ActionKind.SHAPE_CHECK: _shape_check,
    ActionKind.TOOL_MAP: _tool_map,
    ActionKind.SLICE: _slice,
}


def execute_query(request: ActionRequest, context: DeterministicQueryContext) -> EvidenceArtifact:
    """Execute one supported typed request and return a canonical artifact.

    Unsupported kinds are rejected rather than reinterpreted.  Producer failures
    become an explicit incomplete result, preserving the host's raw fallback path.
    """

    if not isinstance(request, ActionRequest):
        raise TypeError("request must be an ActionRequest")
    producer = _PRODUCERS.get(request.kind)
    if producer is None:
        raise ValueError(f"unsupported deterministic query kind: {request.kind.value}")
    unknown = sorted(set(request.arguments) - _ALLOWED_ARGUMENTS[request.kind])
    if unknown:
        produced = _Produced(
            {"error": "unsupported_arguments", "kind": request.kind.value},
            EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN,
            omissions=tuple(f"unsupported_argument:{name}" for name in unknown),
            revision=_producer_revision(request),
        )
    else:
        try:
            produced = producer(request, context)
        except Exception as exc:  # noqa: BLE001 - producer boundary fails honest
            produced = _Produced(
                {"error": "producer_failed", "kind": request.kind.value},
                EvidenceSemantics.INCOMPLETE,
                Coverage.UNKNOWN,
                omissions=(f"producer_failed:{type(exc).__name__}",),
                revision=_producer_revision(request),
            )
    payload = canonical_bytes(produced.answer).decode("utf-8")
    envelope = EvidenceEnvelope.build(
        producer=f"deterministic_query.{request.kind.value}",
        fact_id=request.action_id,
        target=str(
            request.arguments.get("symbol") or request.arguments.get("path") or request.kind.value
        ),
        evidence_type=request.kind.value,
        payload=(payload,),
        provenance=produced.anchors,
        confidence=1.0 if produced.semantics is EvidenceSemantics.EXACT else 0.8,
        tier=INFO,
        graph_revision=produced.revision,
        valid_until=produced.revision,
    )
    return artifact_from_envelope(
        request=request,
        envelope=envelope,
        producer_version=PRODUCER_VERSION,
        semantics=produced.semantics,
        direct_answer=produced.answer,
        coverage=produced.coverage,
        witnesses=produced.witnesses,
        ambiguity=produced.ambiguity,
        omissions=produced.omissions,
        raw_fallback=produced.raw_fallback,
    )


__all__ = ["DeterministicQueryContext", "PRODUCER_VERSION", "execute_query"]

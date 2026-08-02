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
import hashlib
import os
from pathlib import Path
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
from .patch_delta import analyze_patch_delta
from .verification_plan import Check, CheckResult, VerificationPlan, green


PRODUCER_VERSION = "gt.deterministic_queries.v1"
_ALLOWED_ARGUMENTS: Mapping[ActionKind, frozenset[str]] = {
    ActionKind.EXACT_LITERAL_SEARCH: frozenset({"literal", "paths"}),
    ActionKind.SYNTAX_QUERY: frozenset({"path"}),
    ActionKind.PATCH_IMPACT: frozenset({"edited_files"}),
    ActionKind.VERIFICATION_STATUS: frozenset({"plan", "result"}),
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


def _producer_revision(request: ActionRequest) -> str:
    revisions = request.repository_snapshot.revisions
    if request.kind is ActionKind.VERIFICATION_STATUS:
        return revisions.runtime_evidence
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
            {"matches": [], "scope": []}, EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN, omissions=tuple(omissions),
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
            EvidenceSemantics.INCOMPLETE, Coverage.UNKNOWN,
            omissions=("invalid_or_missing_path",),
            revision=_producer_revision(request),
        )
    rel = _rel(context.repository_root, path)
    try:
        source = path.read_bytes()
    except OSError:
        return _Produced(
            {"path": rel, "verdict": "unavailable", "reason": "unreadable"},
            EvidenceSemantics.INCOMPLETE, Coverage.UNKNOWN,
            omissions=(f"unreadable:{rel}",),
            revision=_producer_revision(request),
        )
    result = check_edit_syntax_bytes(rel, source, str(context.repository_root))
    answer = {
        "path": rel, "source_sha256": _sha256(source), "source_bytes": len(source), **result
    }
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
        anchors=((rel, line),), witnesses=(f"source:{_sha256(source)}",),
        omissions=tuple(sorted(set(omissions))),
        raw_fallback=diagnostic.encode("utf-8", "surrogatepass"),
        revision=request.repository_snapshot.revisions.repository_content,
    )


def _patch_impact(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    raw = request.arguments.get("edited_files")
    if not isinstance(raw, Mapping) or not raw:
        return _Produced(
            {"files": [], "analysis": {}}, EvidenceSemantics.INCOMPLETE, Coverage.UNKNOWN,
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
        "1", "true", "yes", "on"
    }:
        omissions.append("patch_analyzer_disabled")
    # Exact patch identities are retained, but the semantic impact analyzer is
    # deliberately conservative and therefore cannot certify complete impact.
    omissions.append("semantic_impact_not_complete")
    return _Produced(
        {"files": exact_files, "analysis": analysis}, EvidenceSemantics.INCOMPLETE,
        Coverage.PARTIAL, witnesses=tuple(f"patch:{row['path']}:{row['after_sha256']}" for row in exact_files),
        omissions=tuple(sorted(set(omissions))),
        revision=request.repository_snapshot.revisions.repository_content,
    )


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
        targets=tuple(data.get("targets") or ()), reason=str(data.get("reason") or ""),
    )


def _verification(request: ActionRequest, context: DeterministicQueryContext) -> _Produced:
    del context
    plan_data = request.arguments.get("plan")
    result_data = request.arguments.get("result")
    if not isinstance(plan_data, Mapping) or not isinstance(result_data, Mapping):
        return _Produced(
            {"green": False, "status": "unavailable"}, EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN, omissions=("invalid_verification_input",),
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
            executed=bool(result_data.get("executed")), verdict=str(result_data.get("verdict") or ""),
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
            {"green": False, "status": "unavailable"}, EvidenceSemantics.INCOMPLETE,
            Coverage.UNKNOWN, omissions=(f"invalid_verification_input:{type(exc).__name__}",),
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
        witnesses=(f"verification:{canonical_sha256(answer)}",), omissions=tuple(omissions),
        raw_fallback=canonical_bytes(result.detail),
        revision=live.runtime_evidence,
    )


_PRODUCERS: Mapping[ActionKind, Callable[[ActionRequest, DeterministicQueryContext], _Produced]] = {
    ActionKind.EXACT_LITERAL_SEARCH: _literal_search,
    ActionKind.SYNTAX_QUERY: _syntax,
    ActionKind.PATCH_IMPACT: _patch_impact,
    ActionKind.VERIFICATION_STATUS: _verification,
}


def execute_query(
    request: ActionRequest, context: DeterministicQueryContext
) -> EvidenceArtifact:
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
            EvidenceSemantics.INCOMPLETE, Coverage.UNKNOWN,
            omissions=tuple(f"unsupported_argument:{name}" for name in unknown),
            revision=_producer_revision(request),
        )
    else:
        try:
            produced = producer(request, context)
        except Exception as exc:  # noqa: BLE001 - producer boundary fails honest
            produced = _Produced(
                {"error": "producer_failed", "kind": request.kind.value},
                EvidenceSemantics.INCOMPLETE, Coverage.UNKNOWN,
                omissions=(f"producer_failed:{type(exc).__name__}",),
                revision=_producer_revision(request),
            )
    payload = canonical_bytes(produced.answer).decode("utf-8")
    envelope = EvidenceEnvelope.build(
        producer=f"deterministic_query.{request.kind.value}",
        fact_id=request.action_id,
        target=str(request.arguments.get("symbol") or request.arguments.get("path") or request.kind.value),
        evidence_type=request.kind.value,
        payload=(payload,), provenance=produced.anchors,
        confidence=1.0 if produced.semantics is EvidenceSemantics.EXACT else 0.8,
        tier=INFO, graph_revision=produced.revision, valid_until=produced.revision,
    )
    return artifact_from_envelope(
        request=request, envelope=envelope, producer_version=PRODUCER_VERSION,
        semantics=produced.semantics, direct_answer=produced.answer,
        coverage=produced.coverage, witnesses=produced.witnesses,
        ambiguity=produced.ambiguity, omissions=produced.omissions,
        raw_fallback=produced.raw_fallback,
    )


__all__ = ["DeterministicQueryContext", "PRODUCER_VERSION", "execute_query"]

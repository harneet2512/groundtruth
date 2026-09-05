"""Deterministic shadow localization orchestration.

This module does not render or deliver model-visible text.  Its only output is
the canonical ``LocalizationResult`` decision trace.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import subprocess
import threading
import time
import tracemalloc
import weakref
from collections import OrderedDict, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from groundtruth.pretask.anchors import extract_issue_anchors
from groundtruth.pretask.spec import extract_spec_v2

from .model import (
    BehaviorFacet,
    CandidateAction,
    CandidateDecision,
    CapabilityMatrix,
    CoverageState,
    EvidenceFamily,
    EvidenceUnit,
    LocalizationDelta,
    LocalizationPolicy,
    LocalizationRequest,
    LocalizationResult,
    LocalizationState,
    ReasonCode,
    SourceRegion,
)


_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)+[\w.@+-]+(?:\.[A-Za-z0-9]+)?)(?=$|[\s`'\",:;)])"
)
_TRACEBACK_RE = re.compile(
    r'(?:File\s+["\'](?P<py>[^"\']+)["\'],\s*line\s*(?P<pyline>\d+))'
    r"|(?P<generic>(?:[\w.-]+/)+[\w.-]+):(?P<gline>\d+)"
)
_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+")
_ACTUAL_RE = re.compile(
    r"(?is)\b(?:actual(?:\s+behavior)?|observed|currently)\s*:\s*(.+?)(?=\n\s*\n|\bexpected(?:\s+behavior)?\s*:|$)"
)
_EXPECTED_RE = re.compile(
    r"(?is)\b(?:expected(?:\s+behavior)?|should|must)\s*:\s*(.+?)(?=\n\s*\n|\bactual(?:\s+behavior)?\s*:|$)"
)
_OP_VERBS = (
    "authorize",
    "authenticate",
    "parse",
    "decode",
    "encode",
    "serialize",
    "deserialize",
    "configure",
    "route",
    "dispatch",
    "publish",
    "subscribe",
    "validate",
    "read",
    "write",
    "catch",
    "raise",
    "return",
    "persist",
)
_ACTOR_STOPWORDS = {
    "Actual",
    "Expected",
    "Observed",
    "Current",
    "Currently",
    "When",
    "After",
    "Before",
    "Error",
    "Issue",
    "Bug",
    "None",
    "True",
    "False",
}
_POLICY_TERMS: dict[str, tuple[str, ...]] = {
    "authorization": ("authorize", "authorization", "permission", "policy", "access control"),
    "parsing": ("parse", "parser", "decode", "tokenize", "malformed", "syntax"),
    "serialization": ("serialize", "deserialize", "json", "yaml", "codec", "marshal"),
    "configuration": ("config", "configuration", "setting", "environment variable", "env var"),
    "route_api": ("route", "endpoint", "request", "response", "api", "handler"),
    "event_driven": ("event", "publish", "subscribe", "listener", "callback", "dispatch"),
    "distributed": ("distributed", "replica", "cluster", "consensus", "remote", "network"),
}
_TRUSTED_METHODS = {
    "same_file",
    "import",
    "verified_unique",
    "type_flow",
    "lsp",
    "impl_method",
    "inherited",
    "unique_method",
    "return_type",
    "promote_serde",
    "promote_field_read",
    "promote_write",
    "promote_raises",
    "decorator_route",
}
_SUPPORTED_RELATIONS = {
    "CALLS",
    "IMPORTS",
    "IMPLEMENTS",
    "EXTENDS",
    "HANDLES_ROUTE",
    "API_CALL",
    "DATA_FLOW",
    "PRECEDES",
    "READS",
    "WRITES",
    "RAISES",
    "CO_SERIALIZES",
    "OVERRIDES",
    "CATCHES",
}
_PARSER_ONLY_RELATIONS = {"PUBLISHES", "SUBSCRIBES", "CONFIGURES", "VALIDATES"}
_POLICY_RELATION_PRIORITY: dict[str, tuple[str, ...]] = {
    "parsing": ("DATA_FLOW", "PRECEDES", "RAISES", "CATCHES", "CALLS"),
    "configuration": ("READS", "WRITES", "IMPORTS", "CALLS"),
    "authorization": ("HANDLES_ROUTE", "READS", "WRITES", "CALLS"),
    "serialization": ("CO_SERIALIZES", "DATA_FLOW", "CALLS"),
    "route_api": ("HANDLES_ROUTE", "API_CALL", "CALLS"),
    "event_driven": ("PRECEDES", "CALLS", "WRITES", "READS"),
    "distributed": ("API_CALL", "DATA_FLOW", "PRECEDES", "CALLS"),
}
_SOURCE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".go",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".rs",
    ".cs",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".scala",
}
_CONFIG_NAMES = {
    "pyproject.toml",
    "package.json",
    "pom.xml",
    "build.gradle",
    "go.mod",
    "cargo.toml",
    "settings.json",
    "config.yaml",
    "config.yml",
}


def _norm(path: str) -> str:
    normalized = (path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _table_names(con: sqlite3.Connection) -> set[str]:
    try:
        return {
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
    except sqlite3.Error:
        return set()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return set()
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _open_graph(path: str) -> sqlite3.Connection | None:
    if not path or not Path(path).is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    return " ".join(match.group(1).strip().split()) if match else ""


def _sentence_for(text: str, token: str) -> str:
    for piece in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if token.lower() in piece.lower():
            return " ".join(piece.strip().split())
    return ""


def _contains_term(text: str, term: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
            text,
        )
    )


def _issue_paths(
    request: LocalizationRequest,
    additional_paths: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return local/new-file paths, excluding nonlocal URL-only references."""
    text = request.issue_text or ""
    root = Path(request.repository_root)
    url_spans = tuple((match.start(), match.end()) for match in _URL_RE.finditer(text))
    candidates = {
        _norm(path)
        for path in additional_paths
        if _norm(path)
    }
    candidates.update(
        _norm(match.group(1))
        for match in _PATH_RE.finditer(text)
        if _norm(match.group(1))
    )
    admitted: list[str] = []
    for path in sorted(candidates):
        if (root / path).is_file():
            admitted.append(path)
            continue
        occurrences = [
            (match.start(), match.end())
            for match in re.finditer(re.escape(path), text)
        ]
        url_only = bool(occurrences) and all(
            any(
                url_start <= start and end <= url_end
                for url_start, url_end in url_spans
            )
            for start, end in occurrences
        )
        if not url_only:
            admitted.append(path)
    return tuple(admitted)


def extract_behavior_facets(request: LocalizationRequest) -> BehaviorFacet:
    """Extract issue behavior while keeping anchors and obligations independent."""
    text = request.issue_text or ""
    lower = text.lower()
    try:
        anchors = extract_issue_anchors(text, request.graph_db)
    except Exception:
        anchors = extract_issue_anchors(text, None)
    try:
        spec = extract_spec_v2(text)
    except Exception:
        spec = None

    symbols = tuple(sorted(getattr(anchors, "symbols", set()) or set()))
    actor = next(
        (
            symbol
            for symbol in symbols
            if symbol[:1].isupper() and "." not in symbol
            and symbol not in _ACTOR_STOPWORDS
        ),
        "",
    )

    operation = ""
    for symbol in sorted(
        symbols,
        key=lambda value: (
            value == actor,
            value.rsplit(".", 1)[-1][:1].isupper(),
            len(value),
            value,
        ),
    ):
        tail = symbol.rsplit(".", 1)[-1]
        if symbol != actor and any(verb in tail.lower() for verb in _OP_VERBS):
            operation = tail
            break
    if not operation:
        for verb in _OP_VERBS:
            if re.search(rf"\b{re.escape(verb)}\w*\b", lower):
                match = re.search(rf"\b{re.escape(verb)}\w*\b", lower)
                operation = match.group(0) if match else verb
                break

    observed = _first_match(_ACTUAL_RE, text)
    expected = _first_match(_EXPECTED_RE, text)
    if not observed:
        observed = _sentence_for(text, "currently") or _sentence_for(text, "returns")
    if not expected:
        expected = _sentence_for(text, "must") or _sentence_for(text, "should")

    paths = _issue_paths(
        request,
        getattr(anchors, "paths", set()) or set(),
    )
    boundary = ", ".join(paths)
    explicit_symbol_shape = bool(
        re.search(
            r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
            r"|\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b"
            r"|\b[A-Za-z_][A-Za-z0-9_]*\(\)",
            text,
        )
    )

    policies = tuple(
        name
        for name, terms in _POLICY_TERMS.items()
        if any(_contains_term(lower, term) for term in terms)
    )
    if not policies:
        policies = ("generic",)

    obligation_ids = tuple(
        str(getattr(obligation, "clause_id", "") or f"obligation_{idx:03d}")
        for idx, obligation in enumerate(
            getattr(spec, "obligations", ()) if spec is not None else (), start=1
        )
    )
    mandatory_obligations = tuple(
        obligation
        for obligation in (getattr(spec, "obligations", ()) if spec is not None else ())
        if int(getattr(obligation, "modality_strength", 0) or 0) >= 2
    )

    state_sentence = next(
        (
            _sentence_for(text, term)
            for term in ("state", "cache", "stored", "persist", "field")
            if term in lower
        ),
        "",
    )
    transition = next(
        (
            _sentence_for(text, term)
            for term in ("when", "after", "before", "transition", "becomes")
            if term in lower
        ),
        "",
    )
    invariant = next(
        (
            _sentence_for(text, term)
            for term in ("remain", "never", "always", "unchanged", "invariant")
            if term in lower
        ),
        "",
    )

    required: list[str] = []
    if actor:
        required.append("actor")
    if operation:
        required.append("operation")
    if observed:
        required.append("observed_behavior")
    if expected or mandatory_obligations:
        required.append("expected_behavior")
    if state_sentence:
        required.append("state")
    if transition:
        required.append("transition")
    if invariant:
        required.append("invariant")
    if boundary:
        required.append("architectural_boundary")
    for policy in policies:
        if policy in {"configuration", "serialization", "route_api", "authorization", "parsing"}:
            required.append(policy)
    stripped = text.strip()
    if not stripped:
        issue_mode = "evidence_only" if request.new_evidence else "absent"
    elif request.new_evidence and not (
        operation
        or observed
        or expected
        or boundary
        or explicit_symbol_shape
        or _TRACEBACK_RE.search(text)
    ):
        issue_mode = "evidence_only"
    elif _TRACEBACK_RE.search(text):
        issue_mode = "traceback"
    elif boundary:
        issue_mode = "explicit_path"
    elif explicit_symbol_shape:
        issue_mode = "symbol_anchored"
    elif operation or observed or expected or mandatory_obligations:
        issue_mode = "behavior_described"
    else:
        issue_mode = "sparse"
    if issue_mode == "evidence_only":
        required.extend(
            role
            for unit in request.new_evidence
            for role in unit.roles
        )

    expected_roles = tuple(
        role for role in ("exception", "test_link", "alternate_path") if role not in required
    )
    return BehaviorFacet(
        issue_mode=issue_mode,
        actor=actor,
        operation=operation,
        state=state_sentence,
        transition=transition,
        invariant=invariant,
        observed_behavior=observed,
        expected_behavior=expected,
        architectural_boundary=boundary,
        policies=policies,
        anchor_symbols=symbols,
        obligation_ids=obligation_ids,
        required_roles=tuple(dict.fromkeys(required)),
        expected_roles=expected_roles,
    )


class EcosystemAdapter:
    def __init__(self, name: str, priorities: Sequence[str]) -> None:
        self.name = name
        self.priorities = tuple(priorities)


def _read_small(path: Path, limit: int = 250_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit].lower()
    except OSError:
        return ""


def detect_ecosystem_adapter(repository_root: str | Path) -> EcosystemAdapter:
    root = Path(repository_root)
    pyproject = _read_small(root / "pyproject.toml")
    requirements = _read_small(root / "requirements.txt")
    if any(term in pyproject + requirements for term in ("fastapi", "django")):
        return EcosystemAdapter(
            "python_web", ("HANDLES_ROUTE", "READS", "WRITES", "CO_SERIALIZES", "CALLS")
        )
    pom = _read_small(root / "pom.xml") + _read_small(root / "build.gradle")
    if "spring" in pom:
        return EcosystemAdapter(
            "java_spring", ("HANDLES_ROUTE", "IMPLEMENTS", "CO_SERIALIZES", "CALLS")
        )
    package = _read_small(root / "package.json")
    if "express" in package:
        return EcosystemAdapter(
            "javascript_express", ("HANDLES_ROUTE", "API_CALL", "CALLS", "PRECEDES")
        )
    if "react" in package:
        return EcosystemAdapter(
            "javascript_react", ("DATA_FLOW", "PRECEDES", "READS", "WRITES")
        )
    for candidate in sorted(root.glob("*.csproj")):
        if "microsoft.net.sdk.web" in _read_small(candidate):
            return EcosystemAdapter(
                "dotnet_aspnet", ("HANDLES_ROUTE", "IMPLEMENTS", "CO_SERIALIZES", "CALLS")
            )
    gomod = _read_small(root / "go.mod")
    if any(term in gomod for term in ("gin-gonic", "gorilla/mux", "go-chi", "echo")):
        return EcosystemAdapter(
            "go_router", ("HANDLES_ROUTE", "IMPLEMENTS", "CALLS", "READS")
        )
    return EcosystemAdapter("generic", tuple(sorted(_SUPPORTED_RELATIONS)))


def census_capabilities(request: LocalizationRequest) -> CapabilityMatrix:
    available: dict[str, bool] = {
        "graph_schema": False,
        "typed_edges": False,
        "property_spans": False,
        "node_fts": False,
        "body_fts": False,
        "frozen_semantic": False,
        "lsp": False,
        "runtime_evidence": bool(request.new_evidence)
        or bool(_TRACEBACK_RE.search(request.issue_text or "")),
        "git_history": False,
        "source_spans": False,
        "publishes": False,
        "subscribes": False,
        "configures": False,
        "validates": False,
    }
    unavailable: dict[str, str] = {}
    details: dict[str, Any] = {}
    con = _open_graph(request.graph_db)
    if con is not None:
        try:
            tables = _table_names(con)
            node_cols = _columns(con, "nodes")
            edge_cols = _columns(con, "edges")
            prop_cols = _columns(con, "properties")
            available["graph_schema"] = {"id", "file_path", "name"} <= node_cols
            available["source_spans"] = {"start_line", "end_line"} <= node_cols
            available["typed_edges"] = "edges" in tables and "type" in edge_cols
            available["property_spans"] = "properties" in tables and "line" in prop_cols
            available["node_fts"] = "nodes_fts" in tables
            available["body_fts"] = "symbol_content_fts" in tables
            if available["source_spans"]:
                node_total, node_spanned = con.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN start_line > 0
                                         AND end_line >= start_line
                                    THEN 1 ELSE 0 END)
                    FROM nodes
                    """
                ).fetchone()
                details["source_span_quality"] = {
                    "node_count": int(node_total or 0),
                    "valid_span_count": int(node_spanned or 0),
                    "valid_span_fraction": (
                        float(node_spanned or 0) / float(node_total)
                        if node_total
                        else 0.0
                    ),
                }
            if "properties" in tables and "line" in prop_cols:
                property_total, property_spanned = con.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN line > 0 THEN 1 ELSE 0 END)
                    FROM properties
                    """
                ).fetchone()
                details["property_span_quality"] = {
                    "property_count": int(property_total or 0),
                    "line_span_count": int(property_spanned or 0),
                    "line_span_fraction": (
                        float(property_spanned or 0) / float(property_total)
                        if property_total
                        else 0.0
                    ),
                }
            if available["typed_edges"]:
                rows = con.execute(
                    "SELECT type, COUNT(*) n FROM edges GROUP BY type ORDER BY type"
                ).fetchall()
                details["edge_type_counts"] = {str(r[0]): int(r[1]) for r in rows}
                for relation in _PARSER_ONLY_RELATIONS:
                    available[relation.lower()] = (
                        details["edge_type_counts"].get(relation, 0) > 0
                    )
                details["trusted_edge_types"] = sorted(
                    relation
                    for relation in details["edge_type_counts"]
                    if relation in _SUPPORTED_RELATIONS
                )
                if "resolution_method" in edge_cols:
                    available["lsp"] = (
                        con.execute(
                            "SELECT 1 FROM edges WHERE lower(resolution_method)='lsp' LIMIT 1"
                        ).fetchone()
                        is not None
                    )
            if "language" in node_cols:
                lang_rows = con.execute(
                    "SELECT language, COUNT(*) n FROM nodes GROUP BY language ORDER BY n DESC, language"
                ).fetchall()
                details["languages"] = [str(r[0] or "unknown") for r in lang_rows]
                details["language_counts"] = {str(r[0] or "unknown"): int(r[1]) for r in lang_rows}
            details["tables"] = sorted(tables)
        finally:
            con.close()

    models_root = Path(
        os.getenv(
            "GT_MODELS_ROOT",
            Path(__file__).resolve().parents[4] / "models",
        )
    )
    if models_root.is_dir():
        try:
            available["frozen_semantic"] = any(
                path.suffix.lower() == ".onnx" for path in models_root.rglob("*.onnx")
            )
        except OSError:
            available["frozen_semantic"] = False

    git_dir = Path(request.repository_root) / ".git"
    if git_dir.exists():
        try:
            cp = subprocess.run(
                ["git", "-C", request.repository_root, "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if cp.returncode == 0 and cp.stdout.strip().isdigit():
                depth = int(cp.stdout.strip())
                available["git_history"] = depth > 0
                details["git_history_depth"] = depth
        except (OSError, subprocess.SubprocessError):
            pass

    adapter = detect_ecosystem_adapter(request.repository_root)
    details["ecosystem_adapter"] = adapter.name
    details["ecosystem_relation_priorities"] = list(adapter.priorities)
    explicit_missing = {
        "publishes": "no parser-produced PUBLISHES relation",
        "subscribes": "no parser-produced SUBSCRIBES relation",
        "configures": "no parser-produced CONFIGURES relation",
        "validates": "no parser-produced VALIDATES relation",
    }
    for name, present in available.items():
        if not present:
            unavailable[name] = explicit_missing.get(name, f"{name} capability unavailable")
    return CapabilityMatrix(available=available, unavailable=unavailable, details=details)


_PASSAGE_FIELD_ORDER = (
    "symbol",
    "role",
    "signature",
    "callers",
    "callees",
    "reads",
    "writes",
    "routes",
    "configuration",
    "exceptions",
    "serialization",
    "test_linkage",
)


def build_structured_symbol_passages(
    request: LocalizationRequest,
    *,
    file_paths: Iterable[str] | None = None,
) -> dict[str, str]:
    """Build fixed-order, non-leaking semantic symbol representations.

    Test linkage is represented only as a count; test identifiers and assertions
    never enter the passage.
    """
    con = _open_graph(request.graph_db)
    if con is None:
        return {}
    wanted = {_norm(path) for path in file_paths or ()}
    try:
        tables = _table_names(con)
        if "nodes" not in tables:
            return {}
        rows = con.execute(
            """
            SELECT id,label,name,qualified_name,file_path,signature
            FROM nodes WHERE COALESCE(is_test,0)=0
            ORDER BY file_path,COALESCE(start_line,0),id
            """
        ).fetchall()
        if wanted:
            rows = [row for row in rows if _norm(str(row["file_path"] or "")) in wanted]
        has_edges = "edges" in tables
        has_props = "properties" in tables
        passages: dict[str, str] = {}
        for row in rows:
            node_id = int(row["id"])
            values: dict[str, str] = {
                "symbol": str(row["qualified_name"] or row["name"] or ""),
                "role": str(row["label"] or ""),
                "signature": str(row["signature"] or ""),
                "callers": "",
                "callees": "",
                "reads": "",
                "writes": "",
                "routes": "",
                "configuration": "",
                "exceptions": "",
                "serialization": "",
                "test_linkage": "linked_test_count=0",
            }
            if has_edges:
                edge_rows = con.execute(
                    """
                    SELECT e.type,e.source_id,e.target_id,
                           s.name source_name,t.name target_name,
                           COALESCE(s.is_test,0) source_test,
                           COALESCE(t.is_test,0) target_test
                    FROM edges e
                    JOIN nodes s ON s.id=e.source_id
                    JOIN nodes t ON t.id=e.target_id
                    WHERE e.source_id=? OR e.target_id=?
                    ORDER BY e.type,s.name,t.name,e.id
                    """,
                    (node_id, node_id),
                ).fetchall()
                callers: set[str] = set()
                callees: set[str] = set()
                reads: set[str] = set()
                writes: set[str] = set()
                routes: set[str] = set()
                exceptions: set[str] = set()
                serialization: set[str] = set()
                linked_tests = 0
                for edge in edge_rows:
                    relation = str(edge["type"] or "").upper()
                    if bool(edge["source_test"]) or bool(edge["target_test"]):
                        linked_tests += 1
                        continue
                    if relation == "CALLS":
                        if int(edge["source_id"]) == node_id:
                            callees.add(str(edge["target_name"] or ""))
                        else:
                            callers.add(str(edge["source_name"] or ""))
                    elif relation == "READS":
                        reads.add(str(edge["target_name"] or ""))
                    elif relation == "WRITES":
                        writes.add(str(edge["target_name"] or ""))
                    elif relation in {"HANDLES_ROUTE", "API_CALL"}:
                        routes.add(str(edge["target_name"] or ""))
                    elif relation in {"RAISES"}:
                        exceptions.add(str(edge["target_name"] or ""))
                    elif relation == "CO_SERIALIZES":
                        serialization.add(
                            str(
                                edge["target_name"]
                                if int(edge["source_id"]) == node_id
                                else edge["source_name"]
                                or ""
                            )
                        )
                values.update(
                    callers=", ".join(sorted(filter(None, callers))),
                    callees=", ".join(sorted(filter(None, callees))),
                    reads=", ".join(sorted(filter(None, reads))),
                    writes=", ".join(sorted(filter(None, writes))),
                    routes=", ".join(sorted(filter(None, routes))),
                    exceptions=", ".join(sorted(filter(None, exceptions))),
                    serialization=", ".join(sorted(filter(None, serialization))),
                    test_linkage=f"linked_test_count={linked_tests}",
                )
            if has_props:
                prop_rows = con.execute(
                    """
                    SELECT kind,value FROM properties WHERE node_id=?
                    ORDER BY kind,value
                    """,
                    (node_id,),
                ).fetchall()
                config_values = [
                    str(prop["value"] or "")
                    for prop in prop_rows
                    if str(prop["kind"] or "").lower()
                    in {"config_read", "configuration", "env_read"}
                ]
                if config_values:
                    values["configuration"] = ", ".join(sorted(config_values))
            passage = "\n".join(f"{field}: {values[field]}" for field in _PASSAGE_FIELD_ORDER)
            key = (
                f"{_norm(str(row['file_path'] or ''))}::"
                f"{str(row['qualified_name'] or row['name'] or '')}"
            )
            passages[key] = passage
        return passages
    finally:
        con.close()


def _roles_for(
    facets: BehaviorFacet,
    *,
    symbol: str,
    file_path: str,
    relation: str = "",
    property_kind: str = "",
) -> tuple[str, ...]:
    roles: set[str] = set()
    sym_lower = (symbol or "").lower()
    fp_lower = _norm(file_path).lower()
    if facets.actor and facets.actor.lower() in sym_lower:
        roles.add("actor")
    if facets.operation and facets.operation.lower() in sym_lower:
        roles.update(("operation", "observed_behavior"))
    if facets.architectural_boundary and any(
        path.strip().lower() == fp_lower
        for path in facets.architectural_boundary.split(",")
    ):
        roles.add("architectural_boundary")
    if "configuration" in facets.policies and (
        "config" in fp_lower or property_kind in {"config_read", "configuration", "env_read"}
    ):
        roles.add("configuration")
    if "parsing" in facets.policies and (
        "pars" in sym_lower
        or relation in {"DATA_FLOW", "PRECEDES", "RAISES", "CATCHES"}
    ):
        roles.add("parsing")
    if "serialization" in facets.policies and relation == "CO_SERIALIZES":
        roles.add("serialization")
    if "route_api" in facets.policies and relation in {"HANDLES_ROUTE", "API_CALL"}:
        roles.add("route_api")
    if "authorization" in facets.policies and (
        relation in {"HANDLES_ROUTE", "READS", "WRITES"}
        or any(term in sym_lower for term in ("auth", "policy", "permission", "access"))
    ):
        roles.add("authorization")
    if relation in {"RAISES", "CATCHES"} or "exception" in property_kind:
        roles.update(("exception", "expected_behavior", "transition"))
    if relation in {"DATA_FLOW", "PRECEDES", "READS", "WRITES"}:
        roles.add("transition")
    if property_kind in {
        "boundary_condition",
        "guard",
        "guard_clause",
        "conditional_return",
    }:
        roles.update(("invariant", "expected_behavior"))
    if property_kind in {"data_flow", "call_order", "exception_flow"}:
        roles.add("transition")
    if property_kind in {"return_shape", "exception_type"}:
        roles.add("expected_behavior")
    if property_kind in {"serialization", "serialization_pair"}:
        roles.add("serialization")
    if property_kind in {"field_read", "side_effect", "state_read", "state_write"}:
        roles.add("state")
    return tuple(sorted(roles))


def _traceback_evidence(request: LocalizationRequest, facets: BehaviorFacet) -> list[EvidenceUnit]:
    out: list[EvidenceUnit] = []
    for rank, match in enumerate(_TRACEBACK_RE.finditer(request.issue_text), start=1):
        path = _norm(match.group("py") or match.group("generic") or "")
        line = int(match.group("pyline") or match.group("gline") or 0)
        out.append(
            EvidenceUnit.create(
                file_path=path,
                start_line=line,
                end_line=line,
                family=EvidenceFamily.TRACEBACK,
                confidence=1.0,
                provenance=("issue_traceback",),
                roles=("operation", "observed_behavior"),
                source_tokens=1,
                signal_class="runtime",
                signal_rank=rank,
                fact_span=True,
                explicit_provenance=True,
            )
        )
    return out


def _explicit_path_evidence(
    request: LocalizationRequest, facets: BehaviorFacet
) -> list[EvidenceUnit]:
    paths = set(
        _issue_paths(
            request,
            (
                raw_path.strip()
                for raw_path in facets.architectural_boundary.split(",")
                if raw_path.strip()
            ),
        )
    )
    out: list[EvidenceUnit] = []
    for rank, path in enumerate(sorted(paths), start=1):
        if ".." in Path(path).parts:
            continue
        target = Path(request.repository_root) / path
        suffix = Path(path).suffix.lower()
        config_shape = (
            Path(path).name.lower() in _CONFIG_NAMES
            or suffix
            in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".properties"}
        )
        source_or_config_shape = (
            suffix in _SOURCE_EXTENSIONS
            or config_shape
        )
        # Slash-bearing prose such as "CI/CD" is not a repository path. Keep
        # non-existent paths only when their filename has a source/config shape,
        # which preserves explicit new-file tasks without granting hard
        # provenance to architecture prose.
        if not target.is_file() and not source_or_config_shape:
            continue
        roles = {"architectural_boundary"}
        if config_shape or "config" in path.lower():
            roles.add("configuration")
        out.append(
            EvidenceUnit.create(
                file_path=path,
                start_line=0,
                end_line=0,
                family=EvidenceFamily.EXPLICIT_PATH,
                confidence=1.0,
                provenance=("issue_explicit_path",),
                roles=tuple(roles),
                source_tokens=0,
                signal_class="explicit",
                signal_rank=rank,
                explicit_provenance=True,
            )
        )
    return out


def _candidate_node_rows(
    con: sqlite3.Connection, facets: BehaviorFacet, request: LocalizationRequest
) -> list[sqlite3.Row]:
    terms = {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", request.issue_text)
        if len(token) >= 4
    }
    anchors = {symbol.lower().rsplit(".", 1)[-1] for symbol in facets.anchor_symbols}
    operation = facets.operation.lower()
    rows = con.execute(
        """
        SELECT id, label, name, qualified_name, file_path, start_line, end_line,
               signature, language, parent_id
        FROM nodes
        WHERE COALESCE(is_test, 0)=0
        ORDER BY file_path, COALESCE(start_line, 0), id
        """
    ).fetchall()
    scored: list[tuple[tuple[int, int, int, str, int], sqlite3.Row]] = []
    for row in rows:
        name = str(row["name"] or "")
        qname = str(row["qualified_name"] or "")
        fp = _norm(str(row["file_path"] or ""))
        surface = f"{name} {qname} {fp} {row['signature'] or ''}".lower()
        exact = int(name.lower() in anchors or qname.lower() in {s.lower() for s in facets.anchor_symbols})
        op = int(bool(operation) and operation in surface)
        overlap = sum(1 for term in terms if term in surface)
        if exact or op or overlap or (
            facets.architectural_boundary
            and fp in facets.architectural_boundary.split(", ")
        ):
            scored.append(((-exact, -op, -overlap, fp, int(row["id"])), row))
    scored.sort(key=lambda item: item[0])
    return [row for _, row in scored[: request.policy.max_candidates]]


def _fts_candidate_signals(
    con: sqlite3.Connection,
    request: LocalizationRequest,
) -> dict[int, list[tuple[EvidenceFamily, int, float]]]:
    """Reuse the native lexical retrievers without treating BM25 as a fact.

    Node-name FTS and body-content BM25 are correlated lexical surfaces. They
    therefore retain distinct provenance/families in the decision trace but
    share one ``lexical`` signal class during reciprocal-rank fusion.
    """
    tables = _table_names(con)
    if not {"nodes_fts", "symbol_content_fts"} & tables:
        return {}
    try:
        from groundtruth.pretask import graph_localizer as legacy_localizer

        issue_terms = legacy_localizer._issue_terms(request.issue_text)
        limit = min(100, request.policy.max_candidates)
        ranked: list[
            tuple[EvidenceFamily, list[tuple[int, str, str, float]]]
        ] = []
        if "nodes_fts" in tables:
            ranked.append(
                (
                    EvidenceFamily.NODE_FTS,
                    legacy_localizer._fts5_candidates(
                        con,
                        issue_terms,
                        limit=limit,
                    ),
                )
            )
        if "symbol_content_fts" in tables:
            ranked.append(
                (
                    EvidenceFamily.BODY_BM25,
                    legacy_localizer._content_fts_candidates(
                        con,
                        issue_terms,
                        limit=limit,
                        issue_text=request.issue_text,
                    ),
                )
            )
    except Exception:
        # Retrieval is correct-or-quiet. Capability census records table
        # presence separately from successful query execution.
        return {}

    signals: dict[int, list[tuple[EvidenceFamily, int, float]]] = defaultdict(list)
    for family, rows in ranked:
        for rank, (node_id, _name, _file_path, score) in enumerate(
            rows,
            start=1,
        ):
            signals[int(node_id)].append((family, rank, float(score)))
    return dict(signals)


_SEMANTIC_VECTOR_CACHE_MAX = LocalizationPolicy().max_candidates
_SEMANTIC_RANK_DECIMALS = 5
_SEMANTIC_VECTOR_CACHE: weakref.WeakKeyDictionary[
    Any,
    OrderedDict[str, tuple[float, ...]],
] = weakref.WeakKeyDictionary()
_SEMANTIC_VECTOR_CACHE_LOCK = threading.Lock()


def _encode_structured_semantics(
    embedder: Any,
    issue_text: str,
    passages: dict[str, str],
) -> tuple[tuple[float, ...], dict[str, tuple[float, ...]]]:
    """Encode the issue every time and reuse immutable passage vectors."""
    passage_keys = sorted(passages)
    cached: dict[str, tuple[float, ...]] = {}
    cache: OrderedDict[str, tuple[float, ...]] | None
    try:
        with _SEMANTIC_VECTOR_CACHE_LOCK:
            cache = _SEMANTIC_VECTOR_CACHE.setdefault(
                embedder,
                OrderedDict(),
            )
            for key in passage_keys:
                digest = hashlib.sha256(
                    passages[key].encode("utf-8")
                ).hexdigest()
                vector = cache.get(digest)
                if vector is not None:
                    cache.move_to_end(digest)
                    cached[key] = vector
    except TypeError:
        # A custom embedder may not support weak references or identity
        # hashing. Preserve the uncached encode path for compatibility.
        cache = None

    missing = [key for key in passage_keys if key not in cached]
    encoded = embedder.encode(
        [
            issue_text,
            *(passages[key] for key in missing),
        ]
    )
    query_vector = tuple(float(value) for value in encoded[0])
    for key, vector in zip(missing, encoded[1:]):
        cached[key] = tuple(float(value) for value in vector)

    if cache is not None and missing:
        with _SEMANTIC_VECTOR_CACHE_LOCK:
            for key in missing:
                digest = hashlib.sha256(
                    passages[key].encode("utf-8")
                ).hexdigest()
                cache[digest] = cached[key]
                cache.move_to_end(digest)
            while len(cache) > _SEMANTIC_VECTOR_CACHE_MAX:
                cache.popitem(last=False)
    return query_vector, cached


def _node_evidence(
    con: sqlite3.Connection,
    facets: BehaviorFacet,
    request: LocalizationRequest,
) -> tuple[list[EvidenceUnit], set[int]]:
    surface_rows = _candidate_node_rows(con, facets, request)
    surface_rank = {
        int(row["id"]): rank
        for rank, row in enumerate(surface_rows, start=1)
    }
    fts_signals = _fts_candidate_signals(con, request)
    rows = list(surface_rows)
    remaining = max(0, request.policy.max_candidates - len(rows))
    extra_ids = sorted(
        (node_id for node_id in fts_signals if node_id not in surface_rank),
        key=lambda node_id: (
            min(rank for _family, rank, _score in fts_signals[node_id]),
            node_id,
        ),
    )[:remaining]
    if extra_ids:
        placeholders = ",".join("?" for _ in extra_ids)
        extra_rows = con.execute(
            f"""
            SELECT id, label, name, qualified_name, file_path, start_line,
                   end_line, signature, language, parent_id
            FROM nodes
            WHERE COALESCE(is_test, 0)=0
              AND id IN ({placeholders})
            """,
            tuple(extra_ids),
        ).fetchall()
        by_id = {int(row["id"]): row for row in extra_rows}
        rows.extend(by_id[node_id] for node_id in extra_ids if node_id in by_id)
    passages = (
        {}
        if "structured_semantics" in request.policy.disabled_components
        else build_structured_symbol_passages(
            request,
            file_paths={_norm(str(row["file_path"] or "")) for row in rows},
        )
    )
    semantic_rank: dict[str, tuple[int, float]] = {}
    if passages:
        try:
            from groundtruth.pretask import graph_localizer as legacy_localizer

            embedder = getattr(legacy_localizer, "_EMBEDDER", None)
            if embedder is not None:
                query_vector, passage_vectors = _encode_structured_semantics(
                    embedder,
                    request.issue_text,
                    passages,
                )
                query_norm = math.sqrt(
                    sum(float(value) * float(value) for value in query_vector)
                )
                scored_passages: list[tuple[float, str]] = []
                for key in sorted(passages):
                    vector = passage_vectors[key]
                    passage_norm = math.sqrt(
                        sum(float(value) * float(value) for value in vector)
                    )
                    raw_score = (
                        sum(
                            float(left) * float(right)
                            for left, right in zip(query_vector, vector)
                        )
                        / (query_norm * passage_norm)
                        if query_norm > 0 and passage_norm > 0
                        else 0.0
                    )
                    # Frozen CPU inference can vary in the last few decimal
                    # places across fresh processes. Differences below one
                    # part per million are not meaningful ranking evidence;
                    # quantize them so the stable path/symbol key breaks ties.
                    score = round(raw_score, _SEMANTIC_RANK_DECIMALS)
                    scored_passages.append((score, key))
                scored_passages.sort(key=lambda item: (-item[0], item[1]))
                semantic_rank = {
                    key: (rank, score)
                    for rank, (score, key) in enumerate(
                        scored_passages,
                        start=1,
                    )
                }
        except Exception:
            # The capability census remains honest about model files, while
            # actual encode availability is correct-or-quiet. Legacy semantic
            # discoveries, when supplied, remain a separate input class.
            semantic_rank = {}
    evidence: list[EvidenceUnit] = []
    node_ids: set[int] = set()
    for row in rows:
        node_id = int(row["id"])
        node_ids.add(node_id)
        symbol = str(row["qualified_name"] or row["name"] or "")
        fp = _norm(str(row["file_path"] or ""))
        exact = any(
            str(row["name"] or "").lower() == anchor.lower().rsplit(".", 1)[-1]
            or symbol.lower() == anchor.lower()
            for anchor in facets.anchor_symbols
        )
        base_roles = set(_roles_for(facets, symbol=symbol, file_path=fp))
        if (
            node_id in fts_signals
            and facets.issue_mode == "behavior_described"
        ):
            # A body/name match is candidate evidence that this symbol may
            # implement the described behavior. It may cover issue roles for
            # admission, but its 0.6 confidence remains below certification.
            base_roles.update(
                role
                for role in facets.required_roles
                if role not in {"actor", "architectural_boundary"}
            )
        roles = tuple(sorted(base_roles))
        passage = passages.get(f"{fp}::{symbol}", "")
        metadata = [
            ("node_id", str(node_id)),
            ("language", str(row["language"] or "")),
        ]
        if passage:
            metadata.extend(
                (
                    ("structured_passage_sha256", hashlib.sha256(passage.encode("utf-8")).hexdigest()),
                    ("structured_passage_fields", ",".join(_PASSAGE_FIELD_ORDER)),
                )
            )
        if node_id in surface_rank:
            evidence.append(
                EvidenceUnit.create(
                    file_path=fp,
                    symbol=symbol,
                    start_line=int(row["start_line"] or 0),
                    end_line=int(row["end_line"] or row["start_line"] or 0),
                    family=(
                        EvidenceFamily.IDENTIFIER
                        if exact
                        else EvidenceFamily.LEXICAL
                    ),
                    confidence=1.0 if exact else 0.6,
                    provenance=(
                        "nodes",
                        "exact_identifier" if exact else "structured_lexical",
                    ),
                    roles=roles,
                    source_tokens=0,
                    signal_class="identifier" if exact else "lexical",
                    signal_rank=surface_rank[node_id],
                    metadata=tuple(metadata),
                )
            )
        for family, fts_rank, bm25_score in fts_signals.get(node_id, ()):
            evidence.append(
                EvidenceUnit.create(
                    file_path=fp,
                    symbol=symbol,
                    start_line=int(row["start_line"] or 0),
                    end_line=int(row["end_line"] or row["start_line"] or 0),
                    family=family,
                    confidence=0.6,
                    provenance=(
                        "native_node_fts"
                        if family is EvidenceFamily.NODE_FTS
                        else "native_body_bm25",
                    ),
                    roles=roles,
                    source_tokens=0,
                    signal_class="lexical",
                    signal_rank=fts_rank,
                    metadata=tuple(metadata)
                    + (("bm25_score", f"{bm25_score:.8f}"),),
                )
            )
        semantic = semantic_rank.get(f"{fp}::{symbol}")
        if semantic is not None and semantic[1] > 0.0:
            semantic_position, semantic_score = semantic
            evidence.append(
                EvidenceUnit.create(
                    file_path=fp,
                    symbol=symbol,
                    start_line=int(row["start_line"] or 0),
                    end_line=int(
                        row["end_line"] or row["start_line"] or 0
                    ),
                    family=EvidenceFamily.SEMANTIC,
                    confidence=min(
                        0.89,
                        max(0.5, 0.5 + semantic_score * 0.5),
                    ),
                    provenance=(
                        "frozen_loaded_embedder",
                        "structured_symbol_passage",
                    ),
                    roles=roles,
                    source_tokens=0,
                    signal_class="semantic",
                    signal_rank=semantic_position,
                    metadata=(
                        ("semantic_cosine", f"{semantic_score:.8f}"),
                        (
                            "structured_passage_sha256",
                            hashlib.sha256(passage.encode("utf-8")).hexdigest(),
                        ),
                    ),
                )
            )
    return evidence, node_ids


def _edge_evidence(
    con: sqlite3.Connection,
    facets: BehaviorFacet,
    node_ids: set[int],
    request: LocalizationRequest,
) -> list[EvidenceUnit]:
    if not node_ids or "edges" not in _table_names(con):
        return []
    edge_cols = _columns(con, "edges")
    has_conf = "confidence" in edge_cols
    has_method = "resolution_method" in edge_cols
    has_tier = "trust_tier" in edge_cols
    placeholders = ",".join("?" for _ in node_ids)
    query = f"""
        SELECT e.source_id, e.target_id, e.type,
               {"e.confidence" if has_conf else "0.0"} confidence,
               {"e.resolution_method" if has_method else "''"} resolution_method,
               {"e.trust_tier" if has_tier else "''"} trust_tier,
               s.name source_name, s.qualified_name source_qname,
               s.file_path source_file, s.start_line source_start, s.end_line source_end,
               t.name target_name, t.qualified_name target_qname,
               t.file_path target_file, t.start_line target_start, t.end_line target_end
        FROM edges e
        JOIN nodes s ON s.id=e.source_id
        JOIN nodes t ON t.id=e.target_id
        WHERE e.source_id IN ({placeholders}) OR e.target_id IN ({placeholders})
        ORDER BY e.type, s.file_path, s.start_line, t.file_path, t.start_line
    """
    rows = con.execute(query, tuple(sorted(node_ids)) * 2).fetchall()
    priority: list[str] = []
    if "relation_policy" not in request.policy.disabled_components:
        for issue_policy in facets.policies:
            priority.extend(_POLICY_RELATION_PRIORITY.get(issue_policy, ()))
        priority.extend(
            detect_ecosystem_adapter(request.repository_root).priorities
        )
    priority_map = {relation: idx for idx, relation in enumerate(dict.fromkeys(priority))}
    evidence: list[EvidenceUnit] = []
    for raw_rank, row in enumerate(rows, start=1):
        relation = str(row["type"] or "").upper()
        if relation not in _SUPPORTED_RELATIONS | _PARSER_ONLY_RELATIONS:
            continue
        method = str(row["resolution_method"] or "").lower()
        confidence = float(row["confidence"] or 0.0)
        trusted = method in _TRUSTED_METHODS or confidence >= 0.9
        if relation in _PARSER_ONLY_RELATIONS and not trusted:
            continue
        if str(row["trust_tier"] or "").upper() == "SUPPRESSED":
            continue
        if not trusted and confidence < 0.5:
            continue
        for side in ("source", "target"):
            fp = _norm(str(row[f"{side}_file"] or ""))
            symbol = str(row[f"{side}_qname"] or row[f"{side}_name"] or "")
            roles = _roles_for(facets, symbol=symbol, file_path=fp, relation=relation)
            if not roles:
                continue
            evidence.append(
                EvidenceUnit.create(
                    file_path=fp,
                    symbol=symbol,
                    start_line=int(row[f"{side}_start"] or 0),
                    end_line=int(row[f"{side}_end"] or row[f"{side}_start"] or 0),
                    family=EvidenceFamily.GRAPH,
                    relation=relation,
                    confidence=confidence if confidence > 0 else (1.0 if trusted else 0.5),
                    provenance=(relation, method or "schema_without_method"),
                    roles=roles,
                    source_tokens=0,
                    signal_class="structural",
                    signal_rank=priority_map.get(relation, len(priority_map)) * 1000 + raw_rank,
                    metadata=(
                        ("source_id", str(row["source_id"])),
                        ("target_id", str(row["target_id"])),
                    ),
                )
            )
    return evidence


def _property_evidence(
    con: sqlite3.Connection,
    facets: BehaviorFacet,
    node_ids: set[int],
) -> list[EvidenceUnit]:
    if not node_ids or "properties" not in _table_names(con):
        return []
    cols = _columns(con, "properties")
    if not {"node_id", "kind", "value"} <= cols:
        return []
    placeholders = ",".join("?" for _ in node_ids)
    query = f"""
        SELECT p.node_id, p.kind, p.value,
               {"p.line" if "line" in cols else "0"} line,
               {"p.confidence" if "confidence" in cols else "0.0"} confidence,
               n.name, n.qualified_name, n.file_path, n.start_line, n.end_line
        FROM properties p JOIN nodes n ON n.id=p.node_id
        WHERE p.node_id IN ({placeholders})
        ORDER BY n.file_path, line, p.kind, p.value
    """
    evidence: list[EvidenceUnit] = []
    issue_roles = set(facets.required_roles) | set(facets.expected_roles)
    for rank, row in enumerate(con.execute(query, tuple(sorted(node_ids))).fetchall(), start=1):
        kind = str(row["kind"] or "").lower()
        fp = _norm(str(row["file_path"] or ""))
        symbol = str(row["qualified_name"] or row["name"] or "")
        roles = tuple(
            sorted(
                set(
                    _roles_for(
                        facets,
                        # Property evidence proves the typed fact at this
                        # span, not every behavioral role of its enclosing
                        # symbol. Node evidence carries symbol identity.
                        symbol="",
                        file_path=fp,
                        property_kind=kind,
                    )
                )
                & issue_roles
            )
        )
        if not roles:
            continue
        line = int(row["line"] or 0)
        confidence = float(row["confidence"] or 0.0)
        evidence.append(
            EvidenceUnit.create(
                file_path=fp,
                symbol=symbol,
                start_line=line or int(row["start_line"] or 0),
                end_line=line or int(row["end_line"] or row["start_line"] or 0),
                family=EvidenceFamily.PROPERTY,
                confidence=confidence,
                provenance=("properties", kind, str(row["value"] or "")),
                roles=roles,
                source_tokens=0,
                signal_class="property",
                signal_rank=rank,
                fact_span=bool(line),
                metadata=(("kind", kind), ("node_id", str(row["node_id"]))),
            )
        )
    return evidence


def _history_evidence(
    request: LocalizationRequest,
    current: Sequence[EvidenceUnit],
) -> list[EvidenceUnit]:
    """Bounded repository-history support; never discovers a file by itself."""
    if "history" in request.policy.disabled_components:
        return []
    root = Path(request.repository_root)
    if not (root / ".git").exists():
        return []
    candidates = {_norm(unit.file_path) for unit in current if unit.file_path}
    if not candidates:
        return []
    try:
        cp = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--name-only",
                "--pretty=format:",
                "--max-count=200",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0:
        return []
    counts: dict[str, int] = defaultdict(int)
    for line in cp.stdout.splitlines():
        path = _norm(line.strip())
        if path in candidates:
            counts[path] += 1
    role_by_file: dict[str, set[str]] = defaultdict(set)
    for unit in current:
        role_by_file[_norm(unit.file_path)].update(unit.roles)
    ordered = sorted(counts, key=lambda path: (-counts[path], path))
    return [
        EvidenceUnit.create(
            file_path=path,
            family=EvidenceFamily.HISTORY,
            confidence=0.6,
            provenance=("git_log_name_only", f"touch_count={counts[path]}"),
            roles=tuple(sorted(role_by_file[path])),
            source_tokens=0,
            signal_class="history",
            signal_rank=rank,
        )
        for rank, path in enumerate(ordered, start=1)
    ]


def derive_certified_relationships(request: LocalizationRequest) -> list[EvidenceUnit]:
    """Derive only relationships whose inputs prove an unambiguous fact."""
    con = _open_graph(request.graph_db)
    if con is None:
        return []
    facets = extract_behavior_facets(request)
    out: list[EvidenceUnit] = []

    def issue_related(symbol: str, file_path: str, extra: str = "") -> bool:
        symbol_lower = symbol.lower()
        path = _norm(file_path)
        anchor_tails = {
            anchor.lower().rsplit(".", 1)[-1]
            for anchor in facets.anchor_symbols
        }
        return bool(
            any(tail and tail in symbol_lower for tail in anchor_tails)
            or (facets.operation and facets.operation.lower() in symbol_lower)
            or (
                facets.architectural_boundary
                and path in {
                    item.strip()
                    for item in facets.architectural_boundary.split(",")
                }
            )
            or (extra and extra.lower() in request.issue_text.lower())
        )

    try:
        tables = _table_names(con)
        if not {"nodes", "edges"} <= tables:
            return []
        edge_cols = _columns(con, "edges")
        method_expr = "e.resolution_method" if "resolution_method" in edge_cols else "''"
        conf_expr = "e.confidence" if "confidence" in edge_cols else "0.0"
        class_rows = con.execute(
            f"""
            SELECT e.source_id child_id, e.target_id parent_id, e.type,
                   {method_expr} method, {conf_expr} confidence
            FROM edges e
            JOIN nodes c ON c.id=e.source_id
            JOIN nodes p ON p.id=e.target_id
            WHERE e.type IN ('EXTENDS','IMPLEMENTS')
              AND c.label IN ('Class','Interface','Struct','Trait')
              AND p.label IN ('Class','Interface','Struct','Trait')
            ORDER BY child_id, parent_id, e.type
            """
        ).fetchall()
        for class_row in class_rows:
            method = str(class_row["method"] or "").lower()
            confidence = float(class_row["confidence"] or 0.0)
            if method not in _TRUSTED_METHODS and confidence < 0.9:
                continue
            children = con.execute(
                """
                SELECT id,name,qualified_name,file_path,start_line,end_line
                FROM nodes WHERE parent_id=? AND label IN ('Method','Function')
                ORDER BY name,id
                """,
                (class_row["child_id"],),
            ).fetchall()
            for child in children:
                parents = con.execute(
                    """
                    SELECT id,name,qualified_name,file_path,start_line,end_line
                    FROM nodes
                    WHERE parent_id=? AND name=? AND label IN ('Method','Function')
                    ORDER BY id
                    """,
                    (class_row["parent_id"], child["name"]),
                ).fetchall()
                if len(parents) != 1:
                    continue
                symbol = str(child["qualified_name"] or child["name"] or "")
                if not issue_related(
                    symbol,
                    str(child["file_path"] or ""),
                ):
                    continue
                out.append(
                    EvidenceUnit.create(
                        file_path=str(child["file_path"] or ""),
                        symbol=symbol,
                        start_line=int(child["start_line"] or 0),
                        end_line=int(child["end_line"] or child["start_line"] or 0),
                        family=EvidenceFamily.GRAPH,
                        relation="OVERRIDES",
                        confidence=confidence if confidence > 0 else 1.0,
                        provenance=(
                            str(class_row["type"]),
                            method or "certified_edge",
                            str(parents[0]["qualified_name"] or parents[0]["name"] or ""),
                        ),
                        roles=_roles_for(
                            facets,
                            symbol=symbol,
                            file_path=str(child["file_path"] or ""),
                            relation="OVERRIDES",
                        )
                        + ("alternate_path",),
                        source_tokens=0,
                        signal_class="structural",
                        signal_rank=1,
                    )
                )

        if "properties" in tables:
            pcols = _columns(con, "properties")
            if {"node_id", "kind", "value"} <= pcols:
                handler_rows = con.execute(
                    f"""
                    SELECT p.node_id,p.value,
                           {"p.line" if "line" in pcols else "0"} line,
                           {"p.confidence" if "confidence" in pcols else "0.0"} confidence,
                           n.name,n.qualified_name,n.file_path,n.start_line,n.end_line
                    FROM properties p JOIN nodes n ON n.id=p.node_id
                    WHERE lower(p.kind) IN ('exception_handler','catch','catches')
                    ORDER BY n.file_path,line,p.value
                    """
                ).fetchall()
                for handler in handler_rows:
                    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(handler["value"] or ""))
                    target_name = tokens[-1] if tokens else ""
                    if not target_name:
                        continue
                    targets = con.execute(
                        """
                        SELECT id FROM nodes
                        WHERE name=? AND label IN ('Class','Enum','Interface','Struct')
                        ORDER BY id
                        """,
                        (target_name,),
                    ).fetchall()
                    if len(targets) != 1:
                        continue
                    confidence = float(handler["confidence"] or 0.0)
                    if confidence < 0.9:
                        continue
                    symbol = str(handler["qualified_name"] or handler["name"] or "")
                    if not issue_related(
                        symbol,
                        str(handler["file_path"] or ""),
                        target_name,
                    ):
                        continue
                    out.append(
                        EvidenceUnit.create(
                            file_path=str(handler["file_path"] or ""),
                            symbol=symbol,
                            start_line=int(handler["line"] or handler["start_line"] or 0),
                            end_line=int(handler["line"] or handler["end_line"] or 0),
                            family=EvidenceFamily.PROPERTY,
                            relation="CATCHES",
                            confidence=confidence,
                            provenance=("exception_handler", target_name, "unique_internal_type"),
                            roles=tuple(
                                sorted(
                                    set(
                                        _roles_for(
                                            facets,
                                            symbol=symbol,
                                            file_path=str(handler["file_path"] or ""),
                                            relation="CATCHES",
                                        )
                                    )
                                    | {"expected_behavior", "exception", "transition"}
                                )
                            ),
                            source_tokens=0,
                            signal_class="property",
                            signal_rank=1,
                            fact_span=True,
                        )
                    )
    finally:
        con.close()
    dedup = {unit.evidence_id: unit for unit in out}
    return [dedup[key] for key in sorted(dedup)]


def _legacy_evidence(
    legacy_discoveries: Sequence[Any] | None,
    facets: BehaviorFacet,
    policy: LocalizationPolicy,
) -> list[EvidenceUnit]:
    out: list[EvidenceUnit] = []
    if not legacy_discoveries:
        return out
    for rank, item in enumerate(legacy_discoveries, start=1):
        if isinstance(item, EvidenceUnit):
            out.append(item)
            continue
        if isinstance(item, dict):
            fp = item.get("path") or item.get("file") or item.get("file_path") or ""
            symbol = str(item.get("symbol") or "")
            score = float(item.get("score") or 0.0)
            components = item.get("components") or {}
            confidence = max(0.5, min(1.0, score)) if score > 0 else 0.5
            start = int(item.get("start_line") or 0)
            end = int(item.get("end_line") or start)
            roles = _roles_for(facets, symbol=symbol, file_path=str(fp))
            if not roles and facets.operation:
                roles = ("operation",)
            classes: list[tuple[str, EvidenceFamily, tuple[str, ...]]] = []
            if (
                "structured_semantics" not in policy.disabled_components
                and float(components.get("sem") or 0.0) > 0
            ):
                classes.append(
                    ("semantic", EvidenceFamily.SEMANTIC, ("legacy_v74", "sem"))
                )
            if any(
                float(components.get(key) or 0.0) > 0
                for key in ("reach", "prox", "hub", "anchor")
            ) or str(item.get("entered_via") or "") in {
                "graph_rescue",
                "both",
            }:
                classes.append(
                    (
                        "structural",
                        EvidenceFamily.GRAPH,
                        ("legacy_v74", "graph_components"),
                    )
                )
            if float(components.get("commit") or 0.0) > 0:
                classes.append(
                    ("history", EvidenceFamily.HISTORY, ("legacy_v74", "commit"))
                )
            if any(
                float(components.get(key) or 0.0) > 0
                for key in ("lex", "path")
            ) or not classes:
                classes.append(
                    ("lexical", EvidenceFamily.LEXICAL, ("legacy_v74", "lexical"))
                )
            for signal_class, family, provenance in classes:
                out.append(
                    EvidenceUnit.create(
                        file_path=str(fp),
                        symbol=symbol,
                        start_line=start,
                        end_line=end,
                        family=family,
                        confidence=confidence,
                        provenance=provenance,
                        roles=roles,
                        source_tokens=0,
                        signal_class=signal_class,
                        signal_rank=rank,
                    )
                )
            continue
        else:
            fp = getattr(item, "file_path", getattr(item, "path", ""))
            symbol = ""
            confidence = float(getattr(item, "confidence", 0.5) or 0.5)
            witnesses = list(getattr(item, "witnesses", ()) or ())
            relation = next(
                (
                    str(getattr(witness, "edge_type", "") or "").upper()
                    for witness in witnesses
                    if str(getattr(witness, "edge_type", "") or "").upper()
                    in _SUPPORTED_RELATIONS
                ),
                "",
            )
            family = (
                EvidenceFamily.GRAPH
                if relation
                else EvidenceFamily.LEXICAL
            )
            signal_class = (
                "structural"
                if family is EvidenceFamily.GRAPH
                else "legacy"
            )
            start = end = 0
            provenance = ["legacy_localize"]
            for witness in witnesses:
                provenance.append(
                    ":".join(
                        (
                            str(getattr(witness, "edge_type", "") or ""),
                            str(
                                getattr(
                                    witness,
                                    "resolution_method",
                                    "",
                                )
                                or ""
                            ),
                            f"{float(getattr(witness, 'confidence', 0.0) or 0.0):.8f}",
                            f"hop={int(getattr(witness, 'hop', 0) or 0)}",
                        )
                    )
                )
            metadata = (
                (
                    "rendered_witness",
                    str(item.render_witness())
                    if hasattr(item, "render_witness")
                    else "",
                ),
            )
        roles = _roles_for(facets, symbol=symbol, file_path=str(fp))
        if not roles and facets.operation:
            roles = ("operation",)
        out.append(
            EvidenceUnit.create(
                file_path=str(fp),
                symbol=symbol,
                start_line=start,
                end_line=end,
                family=family,
                relation=relation,
                confidence=confidence,
                provenance=tuple(provenance),
                roles=roles,
                source_tokens=0,
                signal_class=signal_class,
                signal_rank=rank,
                metadata=metadata,
            )
        )
    return out


def discover_candidates(
    request: LocalizationRequest,
    facets: BehaviorFacet,
    *,
    legacy_discoveries: Sequence[Any] | None = None,
) -> list[EvidenceUnit]:
    evidence: list[EvidenceUnit] = []
    evidence.extend(_explicit_path_evidence(request, facets))
    evidence.extend(_traceback_evidence(request, facets))
    evidence.extend(_legacy_evidence(legacy_discoveries, facets, request.policy))
    con = _open_graph(request.graph_db)
    if con is not None:
        try:
            nodes, node_ids = _node_evidence(con, facets, request)
            evidence.extend(nodes)
            evidence.extend(_edge_evidence(con, facets, node_ids, request))
            evidence.extend(_property_evidence(con, facets, node_ids))
        finally:
            con.close()
    if "derived_relationships" not in request.policy.disabled_components:
        evidence.extend(derive_certified_relationships(request))
    evidence.extend(_history_evidence(request, evidence))
    evidence.extend(request.new_evidence)
    dedup: dict[str, EvidenceUnit] = {}
    for unit in evidence:
        previous = dedup.get(unit.evidence_id)
        if previous is None or (
            unit.confidence,
            -unit.signal_rank,
            unit.signal_class,
        ) > (
            previous.confidence,
            -previous.signal_rank,
            previous.signal_class,
        ):
            dedup[unit.evidence_id] = unit
    fused = fuse_by_evidence_class(dedup.values())
    by_region: dict[tuple[str, str, int, int, bool], list[EvidenceUnit]] = (
        defaultdict(list)
    )
    for unit in dedup.values():
        by_region[
            (
                unit.file_path,
                unit.symbol,
                unit.start_line,
                unit.end_line,
                unit.fact_span,
            )
        ].append(unit)

    def region_order(
        item: tuple[tuple[str, str, int, int, bool], list[EvidenceUnit]],
    ) -> tuple[Any, ...]:
        key, support = item
        path, symbol, start, end, fact_span = key
        return (
            0 if any(unit.explicit_provenance for unit in support) else 1,
            -fused.get(path, 0.0),
            0 if fact_span else 1,
            min(unit.signal_rank for unit in support),
            -max(unit.confidence for unit in support),
            path,
            start,
            end,
            symbol,
        )

    ranked_regions = sorted(by_region.items(), key=region_order)

    # A discovered candidate is a file/symbol/source region, not each correlated
    # signal row and not an entire file. Consolidating at this level prevents
    # signal-rich files from consuming the rail without laundering one symbol's
    # behavioral roles onto a different symbol's span.
    consolidated: list[EvidenceUnit] = []
    for region_rank, (region_key, region_support) in enumerate(
        ranked_regions[: request.policy.max_candidates], start=1
    ):
        path = region_key[0]
        support = sorted(
            region_support,
            key=lambda unit: (
                0 if unit.explicit_provenance else 1,
                0 if unit.fact_span else 1,
                -unit.confidence,
                unit.signal_rank,
                unit.start_line,
                unit.evidence_id,
            ),
        )
        best = support[0]
        roles = tuple(sorted({role for unit in support for role in unit.roles}))
        classes = tuple(sorted({unit.signal_class for unit in support}))
        families = tuple(sorted({unit.family.value for unit in support}))
        relations = tuple(sorted({unit.relation for unit in support if unit.relation}))
        metadata = tuple(best.metadata) + (
            ("fused_rrf_score", f"{fused.get(path, 0.0):.12f}"),
            ("supporting_signal_classes", ",".join(classes)),
            ("supporting_families", ",".join(families)),
            ("supporting_relations", ",".join(relations)),
            ("support_count", str(len(support))),
        )
        consolidated.append(
            EvidenceUnit.create(
                file_path=best.file_path,
                symbol=best.symbol,
                start_line=best.start_line,
                end_line=best.end_line,
                family=best.family,
                relation=best.relation,
                confidence=max(unit.confidence for unit in support),
                provenance=tuple(
                    dict.fromkeys(
                        item for unit in support for item in unit.provenance
                    )
                ),
                roles=roles,
                source_tokens=best.source_tokens,
                signal_class="+".join(classes),
                signal_rank=region_rank,
                fact_span=best.fact_span,
                explicit_provenance=any(unit.explicit_provenance for unit in support),
                metadata=metadata,
            )
        )
    return consolidated


def fuse_by_evidence_class(evidence: Iterable[EvidenceUnit], k: int = 60) -> dict[str, float]:
    """RRF once per independent class; correlated signals get one vote per file."""
    best_rank: dict[str, dict[str, int]] = defaultdict(dict)
    explicit: set[str] = set()
    for unit in evidence:
        fp = _norm(unit.file_path)
        signal_classes = {
            signal_class
            for signal_class in unit.signal_class.split("+")
            if signal_class
        } or {"unknown"}
        for signal_class in signal_classes:
            previous = best_rank[fp].get(signal_class)
            if previous is None or unit.signal_rank < previous:
                best_rank[fp][signal_class] = unit.signal_rank
        if unit.explicit_provenance:
            explicit.add(fp)
    fused: dict[str, float] = {}
    for fp, class_ranks in best_rank.items():
        score = sum(1.0 / (k + rank) for rank in class_ranks.values())
        if fp in explicit:
            score += 1.0
        fused[fp] = round(score, 12)
    return fused


def _safe_source_path(root: Path, file_path: str) -> Path | None:
    try:
        root_resolved = root.resolve()
        target = (root_resolved / Path(_norm(file_path))).resolve()
        target.relative_to(root_resolved)
        return target if target.is_file() else None
    except (OSError, ValueError):
        return None


def _bounded_region(
    request: LocalizationRequest, unit: EvidenceUnit
) -> SourceRegion | None:
    root = Path(request.repository_root)
    target = _safe_source_path(root, unit.file_path)
    if target is None:
        if unit.explicit_provenance and unit.family is EvidenceFamily.EXPLICIT_PATH:
            try:
                root_resolved = root.resolve()
                proposed = (root_resolved / Path(_norm(unit.file_path))).resolve()
                proposed.relative_to(root_resolved)
            except (OSError, ValueError):
                return None
            return SourceRegion(
                file_path=_norm(unit.file_path),
                symbol=unit.symbol,
                start_line=0,
                end_line=0,
                roles=unit.roles,
                selection_reason="explicit_new_file_path",
                line_count=0,
                char_count=0,
                source_tokens=0,
                content_sha256=hashlib.sha256(b"").hexdigest(),
                _repository_root=str(root),
                _content="",
            )
        return None
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    if "source_regions" in request.policy.disabled_components:
        try:
            return SourceRegion.from_source(
                root,
                unit.file_path,
                1,
                len(lines),
                unit.symbol,
                unit.roles,
                "source_region_ablation_full_file",
            )
        except OSError:
            return None
    start = unit.start_line
    end = unit.end_line
    reason = "property_span" if unit.fact_span else "symbol_span"
    if start <= 0:
        suffix = target.suffix.lower()
        config_or_data = (
            suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".properties"}
            or target.name.lower() in _CONFIG_NAMES
        )
        issue_terms = tuple(
            dict.fromkeys(
                term.lower()
                for term in re.findall(
                    r"[A-Za-z_][A-Za-z0-9_.-]{2,}",
                    request.issue_text,
                )
                if len(term) >= 4
                and term.lower()
                not in {
                    "accept",
                    "default",
                    "expected",
                    "falling",
                    "issue",
                    "must",
                    "should",
                    "value",
                    "without",
                }
            )
        )
        matched_line = 0
        if config_or_data and issue_terms:
            scored_lines = [
                (
                    sum(
                        2 if "_" in term else 1
                        for term in issue_terms
                        if re.search(
                            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                            line,
                            re.IGNORECASE,
                        )
                    ),
                    line_number,
                )
                for line_number, line in enumerate(lines, start=1)
            ]
            best_score, matched_line = max(
                scored_lines,
                key=lambda item: (item[0], -item[1]),
            )
            if best_score <= 0:
                matched_line = 0
        if matched_line:
            start = max(1, matched_line - 2)
            end = min(len(lines), matched_line + 6)
            reason = "config_matched_line_region"
        elif len(lines) <= 240:
            start, end, reason = 1, len(lines), "small_file_fallback"
        else:
            start, end, reason = 1, min(40, len(lines)), "missing_span_bounded_fallback"
    if end <= 0:
        end = start
    if unit.fact_span:
        start = max(1, start - 2)
        end = min(len(lines), end + 2)
    max_lines = max(1, request.policy.max_region_tokens * 4 // 20)
    if end - start + 1 > max_lines:
        end = start + max_lines - 1
        reason += "_token_bounded"
    try:
        return SourceRegion.from_source(
            root,
            unit.file_path,
            start,
            end,
            unit.symbol,
            unit.roles,
            reason,
        )
    except OSError:
        return None


def merge_regions(
    regions: Iterable[SourceRegion],
    adjacent_lines: int = 2,
    max_source_tokens: int | None = None,
) -> tuple[SourceRegion, ...]:
    ordered = sorted(
        regions,
        key=lambda region: (
            region.file_path,
            region.start_line,
            region.end_line,
            region.symbol,
        ),
    )
    merged: list[SourceRegion] = []
    for region in ordered:
        if (
            merged
            and merged[-1].file_path == region.file_path
            and (
                region.start_line <= merged[-1].end_line
                or (
                    merged[-1].symbol == region.symbol
                    and region.start_line
                    <= merged[-1].end_line + adjacent_lines + 1
                )
            )
        ):
            prior = merged.pop()
            root = region._repository_root or prior._repository_root
            combined_symbol = " | ".join(
                sorted(
                    {
                        symbol
                        for symbol in (prior.symbol, region.symbol)
                        if symbol
                    }
                )
            )
            combined = SourceRegion.from_source(
                root,
                region.file_path,
                min(prior.start_line, region.start_line),
                max(prior.end_line, region.end_line),
                combined_symbol,
                tuple(sorted(set(prior.roles) | set(region.roles))),
                "merged_adjacent_regions",
            )
            if (
                max_source_tokens is not None
                and combined.source_tokens > max_source_tokens
            ):
                merged.extend((prior, region))
            else:
                merged.append(combined)
        else:
            merged.append(region)
    return tuple(merged)


def _is_test_path(path: str) -> bool:
    fp = f"/{_norm(path).lower()}/"
    return any(marker in fp for marker in ("/tests/", "/test/", "/__tests__/")) or bool(
        re.search(r"(?:^|/)(?:test_[^/]+|[^/]+_test)\.[^.]+$", _norm(path).lower())
    )


def _looks_like_pass_through(region: SourceRegion | None) -> bool:
    if region is None or region.line_count > 8:
        return False
    lines = [
        line.strip()
        for line in region._content.splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", "//", "/*", "*", "@"))
    ]
    body = [
        line
        for line in lines
        if not re.match(
            r"^(?:async\s+)?(?:def|function|func|fn|public|private|protected|"
            r"internal|static|class|interface|struct)\b",
            line,
            re.IGNORECASE,
        )
        and line not in {"{", "}", "};"}
    ]
    if not body or len(body) > 3:
        return False
    joined = " ".join(body)
    if re.search(
        r"\b(?:if|for|while|try|catch|except|switch|match|raise|throw|yield)\b",
        joined,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"(?<![=!<>])=(?!=|>)", joined):
        return False
    return bool(
        re.search(r"\breturn\b|=>", joined)
        or (
            len(body) == 1
            and re.search(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(", joined)
        )
    )


def _marginal(
    unit: EvidenceUnit,
    covered: set[str],
    required: set[str],
    expected: set[str],
    role_classes: dict[str, set[str]],
    fused_score: float,
) -> tuple[int, int, int, int, int, int, int]:
    roles = set(unit.roles)
    unit_classes = {
        signal_class
        for signal_class in unit.signal_class.split("+")
        if signal_class
    }
    new_required = roles & required - covered
    certified = len(new_required) if unit.confidence >= 0.9 else 0
    independent = sum(
        1
        for role in new_required
        if len(role_classes.get(role, set()) | unit_classes) >= 2
    )
    new_expected = len(roles & expected - covered)
    independent_confirmation = any(
        role in covered
        and not unit_classes <= role_classes.get(role, set())
        and len(role_classes.get(role, set())) == 1
        for role in roles & required
    )
    new_fact = int(
        independent_confirmation
        or (
            bool(unit.relation or unit.fact_span or unit.explicit_provenance)
            and bool(new_required or (roles & expected - covered))
        )
    )
    duplicate_penalty = 0
    token_utility = -max(0, unit.source_tokens)
    fused_rank = int(round(fused_score * 1_000_000))
    return (
        certified,
        independent,
        new_expected,
        new_fact,
        -duplicate_penalty,
        token_utility,
        fused_rank,
    )


def _decision_for_rejection(
    unit: EvidenceUnit,
    *,
    previous_rejected: set[str],
    policy: LocalizationPolicy,
) -> CandidateDecision | None:
    if unit.evidence_id in previous_rejected:
        return CandidateDecision(
            unit.evidence_id, CandidateAction.REJECT, (ReasonCode.PREVIOUSLY_REJECTED,)
        )
    if not unit.file_path:
        return CandidateDecision(
            unit.evidence_id, CandidateAction.REJECT, (ReasonCode.WRONG_REPOSITORY,)
        )
    if _is_test_path(unit.file_path):
        return CandidateDecision(
            unit.evidence_id, CandidateAction.REJECT, (ReasonCode.TEST_ONLY_SURFACE,)
        )
    parser_typed = (
        unit.relation in _PARSER_ONLY_RELATIONS
        and unit.family is EvidenceFamily.GRAPH
        and unit.confidence >= 0.9
    )
    if unit.relation and unit.relation not in _SUPPORTED_RELATIONS and not parser_typed:
        return CandidateDecision(
            unit.evidence_id, CandidateAction.REJECT, (ReasonCode.UNSUPPORTED_RELATION,)
        )
    if unit.confidence < policy.confidence_floor and not unit.explicit_provenance:
        return CandidateDecision(
            unit.evidence_id, CandidateAction.REJECT, (ReasonCode.BELOW_CONFIDENCE,)
        )
    return None


def _capability_unavailable_roles(
    required: set[str],
    coverable: set[str],
    capabilities: CapabilityMatrix,
) -> set[str]:
    """Separate missing instrumentation from an ordinary retrieval miss."""
    available = capabilities.available
    unavailable: set[str] = set()
    structural = bool(
        available.get("typed_edges")
        or available.get("property_spans")
        or available.get("node_fts")
        or available.get("body_fts")
    )
    for role in required - coverable:
        if role == "architectural_boundary":
            unavailable.add(role)
        elif role in {"state", "transition"} and not (
            available.get("typed_edges") or available.get("property_spans")
        ):
            unavailable.add(role)
        elif role == "invariant" and not available.get("property_spans"):
            unavailable.add(role)
        elif role in {
            "authorization",
            "configuration",
            "parsing",
            "route_api",
            "serialization",
        } and not structural:
            unavailable.add(role)
    return unavailable


def _coverage_admit(
    request: LocalizationRequest,
    facets: BehaviorFacet,
    evidence: Sequence[EvidenceUnit],
    capabilities: CapabilityMatrix,
) -> tuple[
    tuple[CandidateDecision, ...],
    tuple[SourceRegion, ...],
    CoverageState,
    str,
]:
    required = set(facets.required_roles)
    expected = set(facets.expected_roles)
    previous_rejected = set(request.prior_state.rejected if request.prior_state else ())
    fused = (
        {}
        if "class_fusion" in request.policy.disabled_components
        else fuse_by_evidence_class(evidence)
    )

    decisions: dict[str, CandidateDecision] = {}
    candidates: list[EvidenceUnit] = []
    for unit in evidence:
        rejection = _decision_for_rejection(
            unit, previous_rejected=previous_rejected, policy=request.policy
        )
        if rejection is not None:
            decisions[unit.evidence_id] = rejection
        else:
            candidates.append(unit)
    covered: set[str] = set()
    role_classes: dict[str, set[str]] = defaultdict(set)
    admitted_regions: list[SourceRegion] = []
    admitted_ids: set[str] = set()
    used_tokens = 0
    stopping_reason = "no_positive_marginal"
    region_cache = {
        unit.evidence_id: _bounded_region(request, unit) for unit in candidates
    }
    missing_source_ids = {
        unit.evidence_id
        for unit in candidates
        if region_cache.get(unit.evidence_id) is None
    }
    if missing_source_ids:
        for unit in candidates:
            if unit.evidence_id in missing_source_ids:
                decisions[unit.evidence_id] = CandidateDecision(
                    unit.evidence_id,
                    CandidateAction.REJECT,
                    (ReasonCode.WRONG_REPOSITORY,),
                )
        candidates = [
            unit for unit in candidates if unit.evidence_id not in missing_source_ids
        ]
    wrapper_ids = {
        unit.evidence_id
        for unit in candidates
        if _looks_like_pass_through(region_cache.get(unit.evidence_id))
    }
    redundant_wrappers = {
        unit.evidence_id
        for unit in candidates
        if unit.evidence_id in wrapper_ids
        and not unit.explicit_provenance
        and any(
            other.evidence_id != unit.evidence_id
            and other.evidence_id not in wrapper_ids
            and region_cache.get(other.evidence_id) is not None
            and bool(set(other.roles) & set(unit.roles) & (required | expected))
            for other in candidates
        )
    }
    if redundant_wrappers:
        for unit in candidates:
            if unit.evidence_id in redundant_wrappers:
                decisions[unit.evidence_id] = CandidateDecision(
                    unit.evidence_id,
                    CandidateAction.REJECT,
                    (ReasonCode.WRAPPER_OR_PASS_THROUGH,),
                )
        candidates = [
            unit for unit in candidates if unit.evidence_id not in redundant_wrappers
        ]
    coverable = {
        role
        for unit in candidates
        if region_cache.get(unit.evidence_id) is not None
        for role in unit.roles
    }
    unavailable = _capability_unavailable_roles(
        required,
        coverable,
        capabilities,
    )
    target_required = required - unavailable

    if "marginal_coverage" in request.policy.disabled_components:
        fixed = sorted(
            candidates,
            key=lambda unit: (
                0 if unit.explicit_provenance else 1,
                unit.signal_rank,
                -unit.confidence,
                unit.file_path,
                unit.start_line,
                unit.evidence_id,
            ),
        )
        for index, unit in enumerate(fixed):
            region = region_cache.get(unit.evidence_id)
            if index < 8 and region is not None and (
                used_tokens + region.source_tokens <= request.policy.max_source_tokens
            ):
                decisions[unit.evidence_id] = CandidateDecision(
                    unit.evidence_id,
                    CandidateAction.ADMIT,
                    (ReasonCode.NEW_PATH_OR_FACT,),
                    tuple(sorted(set(unit.roles) & (required | expected))),
                )
                admitted_regions.append(region)
                used_tokens += region.source_tokens
            else:
                decisions[unit.evidence_id] = CandidateDecision(
                    unit.evidence_id,
                    CandidateAction.DEFER,
                    (ReasonCode.REDUNDANT,),
                )
        merged = merge_regions(
            admitted_regions,
            request.policy.merge_adjacent_lines,
            request.policy.max_region_tokens,
        )
        final_covered = {role for region in merged for role in region.roles} & required
        coverage = CoverageState(
            required=tuple(sorted(required)),
            covered=tuple(sorted(final_covered)),
            unresolved=tuple(sorted(required - final_covered - unavailable)),
            unavailable=tuple(sorted(unavailable)),
        )
        return (
            tuple(
                decisions[unit.evidence_id]
                for unit in evidence
                if unit.evidence_id in decisions
            ),
            merged,
            coverage,
            "ablation_fixed_top_8",
        )

    while candidates:
        ranked: list[tuple[tuple[int, int, int, int, int, int, int], EvidenceUnit]] = []
        for unit in candidates:
            region = region_cache.get(unit.evidence_id)
            region_tokens = region.source_tokens if region else unit.source_tokens
            scored_unit = replace(unit, source_tokens=region_tokens)
            ranked.append(
                (
                    _marginal(
                        scored_unit,
                        covered,
                        target_required,
                        expected,
                        role_classes,
                        fused.get(unit.file_path, 0.0),
                    ),
                    unit,
                )
            )
        ranked.sort(
            key=lambda pair: (
                tuple(-value for value in pair[0]),
                pair[1].file_path,
                pair[1].start_line,
                pair[1].evidence_id,
            )
        )
        marginal, unit = ranked[0]
        candidates.remove(unit)
        new_roles = (set(unit.roles) & (target_required | expected)) - covered
        positive = any(value > 0 for value in marginal[:4])
        if not positive:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (ReasonCode.REDUNDANT if set(unit.roles) & covered else ReasonCode.NO_ISSUE_CONTRIBUTION,),
                (),
                marginal,
            )
            for remainder in candidates:
                remainder_reason = (
                    ReasonCode.REDUNDANT
                    if set(remainder.roles) & covered
                    else ReasonCode.NO_ISSUE_CONTRIBUTION
                )
                decisions[remainder.evidence_id] = CandidateDecision(
                    remainder.evidence_id,
                    CandidateAction.DEFER,
                    (remainder_reason,),
                )
            candidates.clear()
            stopping_reason = (
                "required_roles_covered"
                if target_required <= covered
                else "no_positive_marginal"
            )
            break
        region = region_cache.get(unit.evidence_id)
        if region is None:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.REJECT,
                (ReasonCode.WRONG_REPOSITORY,),
                (),
                marginal,
            )
            continue
        if region.source_tokens > request.policy.max_region_tokens:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.REJECT,
                (ReasonCode.TOKEN_RAIL,),
                (),
                marginal,
            )
            stopping_reason = "source_token_rail"
            continue
        if used_tokens + region.source_tokens > request.policy.max_source_tokens:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (ReasonCode.TOKEN_RAIL,),
                (),
                marginal,
            )
            stopping_reason = "source_token_rail"
            break
        reason = (
            ReasonCode.NEW_MANDATORY_CERTIFIED
            if marginal[0] > 0
            else ReasonCode.NEW_MANDATORY_INDEPENDENT
            if marginal[1] > 0
            else ReasonCode.NEW_EXPECTED
            if marginal[2] > 0
            else ReasonCode.INDEPENDENT_CONFIRMATION
            if any(
                role in covered
                and not {
                    signal_class
                    for signal_class in unit.signal_class.split("+")
                    if signal_class
                }
                <= role_classes.get(role, set())
                for role in set(unit.roles) & target_required
            )
            else ReasonCode.NEW_PATH_OR_FACT
        )
        decisions[unit.evidence_id] = CandidateDecision(
            unit.evidence_id,
            CandidateAction.ADMIT,
            (reason,),
            tuple(sorted(new_roles)),
            marginal,
        )
        admitted_ids.add(unit.evidence_id)
        admitted_regions.append(region)
        used_tokens += region.source_tokens
        covered.update(new_roles)
        for role in unit.roles:
            role_classes[role].update(
                signal_class
                for signal_class in unit.signal_class.split("+")
                if signal_class
            )

    for unit in candidates:
        if unit.evidence_id not in decisions:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (ReasonCode.REDUNDANT if set(unit.roles) & covered else ReasonCode.NO_ISSUE_CONTRIBUTION,),
            )
    for unit in evidence:
        if unit.evidence_id not in decisions:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (ReasonCode.REDUNDANT,),
            )

    merged = merge_regions(
        admitted_regions,
        request.policy.merge_adjacent_lines,
        request.policy.max_region_tokens,
    )
    final_covered = {role for region in merged for role in region.roles} & required
    unresolved = required - final_covered - unavailable
    if stopping_reason == "required_roles_covered" and unresolved:
        stopping_reason = "no_positive_marginal"
    coverage = CoverageState(
        required=tuple(sorted(required)),
        covered=tuple(sorted(final_covered)),
        unresolved=tuple(sorted(unresolved)),
        unavailable=tuple(sorted(unavailable)),
    )
    ordered_decisions = tuple(
        decisions[unit.evidence_id]
        for unit in evidence
        if unit.evidence_id in decisions
    )
    return ordered_decisions, merged, coverage, stopping_reason


def _make_state(
    decisions: Sequence[CandidateDecision], coverage: CoverageState
) -> LocalizationState:
    return LocalizationState(
        accepted=tuple(sorted(d.evidence_id for d in decisions if d.action is CandidateAction.ADMIT)),
        rejected=tuple(sorted(d.evidence_id for d in decisions if d.action is CandidateAction.REJECT)),
        deferred=tuple(sorted(d.evidence_id for d in decisions if d.action is CandidateAction.DEFER)),
        unresolved_roles=coverage.unresolved,
        decision_reasons=tuple(
            sorted(
                (
                    d.evidence_id,
                    tuple(reason.value for reason in d.reason_codes),
                )
                for d in decisions
            )
        ),
    )


def _make_delta(
    prior: LocalizationState | None,
    current: LocalizationState,
    coverage: CoverageState,
) -> LocalizationDelta | None:
    if prior is None:
        return None
    return LocalizationDelta(
        newly_accepted=tuple(sorted(set(current.accepted) - set(prior.accepted))),
        newly_rejected=tuple(sorted(set(current.rejected) - set(prior.rejected))),
        newly_deferred=tuple(sorted(set(current.deferred) - set(prior.deferred))),
        newly_resolved_roles=tuple(
            sorted(set(prior.unresolved_roles) - set(coverage.unresolved))
        ),
        invalidated_evidence=tuple(
            sorted(
                (set(prior.accepted) | set(prior.deferred))
                - (set(current.accepted) | set(current.deferred))
            )
        ),
    )


def _metrics(
    evidence: Sequence[EvidenceUnit],
    decisions: Sequence[CandidateDecision],
    regions: Sequence[SourceRegion],
    coverage: CoverageState,
    stopping_reason: str,
    *,
    latency_ms: float,
    peak_memory: int,
) -> dict[str, Any]:
    actions = [decision.action for decision in decisions]
    files = {region.file_path for region in regions}
    decision_by_id = {decision.evidence_id: decision for decision in decisions}
    admitted = [
        unit
        for unit in evidence
        if decision_by_id.get(unit.evidence_id)
        and decision_by_id[unit.evidence_id].action is CandidateAction.ADMIT
    ]
    leakage_count = sum(
        1
        for unit in admitted
        if _is_test_path(unit.file_path)
        or any(
            forbidden in value.upper()
            for forbidden in ("FAIL_TO_PASS", "PASS_TO_PASS")
            for value in (
                unit.symbol,
                unit.file_path,
                " ".join(unit.provenance),
            )
        )
    )
    duplicate_signals_removed = sum(
        max(
            0,
            int(dict(unit.metadata).get("support_count", "1") or "1") - 1,
        )
        for unit in evidence
    )
    wrappers_removed = sum(
        1
        for decision in decisions
        if ReasonCode.WRAPPER_OR_PASS_THROUGH in decision.reason_codes
    )
    return {
        "discovered_count": len(evidence),
        "admitted_count": actions.count(CandidateAction.ADMIT),
        "rejected_count": actions.count(CandidateAction.REJECT),
        "deferred_count": actions.count(CandidateAction.DEFER),
        "admitted_files": len(files),
        "admitted_regions": len(regions),
        "admitted_lines": sum(region.line_count for region in regions),
        "admitted_characters": sum(region.char_count for region in regions),
        "admitted_source_tokens": sum(region.source_tokens for region in regions),
        "required_roles": len(coverage.required),
        "covered_roles": len(coverage.covered),
        "unresolved_roles": len(coverage.unresolved),
        "unavailable_roles": len(coverage.unavailable),
        "search_iterations": len(decisions),
        "stopping_reason": stopping_reason,
        "latency_ms": float(latency_ms),
        "peak_memory_bytes": int(peak_memory),
        "duplicate_signals_removed": duplicate_signals_removed,
        "wrapper_regions_removed": wrappers_removed,
        "structured_semantic_encoded_count": sum(
            1
            for unit in evidence
            if unit.family is EvidenceFamily.SEMANTIC
            and "structured_symbol_passage" in unit.provenance
        ),
        "leakage_count": leakage_count,
    }


def localize_vnext(
    request: LocalizationRequest,
    *,
    legacy_discoveries: Sequence[Any] | None = None,
) -> LocalizationResult:
    owns_tracer = not tracemalloc.is_tracing()
    if owns_tracer:
        tracemalloc.start()
    baseline_memory = tracemalloc.get_traced_memory()[0]
    try:
        return _localize_vnext_traced(
            request,
            legacy_discoveries=legacy_discoveries,
            baseline_memory=baseline_memory,
        )
    finally:
        if owns_tracer and tracemalloc.is_tracing():
            tracemalloc.stop()


def _localize_vnext_traced(
    request: LocalizationRequest,
    *,
    legacy_discoveries: Sequence[Any] | None,
    baseline_memory: int,
) -> LocalizationResult:
    started = time.perf_counter()
    facets = extract_behavior_facets(request)
    if "behavioral_facets" in request.policy.disabled_components:
        facets = replace(
            facets,
            state="",
            transition="",
            invariant="",
            observed_behavior="",
            expected_behavior="",
            policies=("generic",),
            required_roles=("operation",) if facets.operation else (),
            expected_roles=(),
        )
    facets_done = time.perf_counter()
    capabilities = census_capabilities(request)
    capabilities_done = time.perf_counter()
    abstain = (
        facets.issue_mode in {"absent", "sparse"}
        and not request.new_evidence
    )
    if abstain:
        evidence = []
        discovery_done = time.perf_counter()
        decisions: tuple[CandidateDecision, ...] = ()
        regions: tuple[SourceRegion, ...] = ()
        if request.prior_state is not None:
            coverage = CoverageState(
                required=request.prior_state.unresolved_roles,
                covered=(),
                unresolved=request.prior_state.unresolved_roles,
                unavailable=(),
            )
        else:
            coverage = CoverageState(
                required=(),
                covered=(),
                unresolved=(),
                unavailable=(),
            )
        stopping_reason = "insufficient_issue_evidence"
        admission_done = time.perf_counter()
    else:
        evidence = discover_candidates(
            request, facets, legacy_discoveries=legacy_discoveries
        )
        discovery_done = time.perf_counter()
        decisions, regions, coverage, stopping_reason = _coverage_admit(
            request, facets, evidence, capabilities
        )
        admission_done = time.perf_counter()
    candidate_rail_hit = len(evidence) >= request.policy.max_candidates
    if candidate_rail_hit:
        stopping_reason = "candidate_rail"
    if abstain and request.prior_state is not None:
        state = request.prior_state
        delta = LocalizationDelta()
    else:
        state = _make_state(decisions, coverage)
        delta = _make_delta(request.prior_state, state, coverage)
    _current, peak = tracemalloc.get_traced_memory()
    peak = max(0, peak - baseline_memory)
    elapsed = (time.perf_counter() - started) * 1000.0
    result = LocalizationResult(
        facets=facets,
        capabilities=capabilities,
        discoveries=tuple(evidence),
        decisions=tuple(decisions),
        admitted_regions=tuple(regions),
        coverage=coverage,
        stopping_reason=stopping_reason,
        state=state,
        delta=delta,
        metrics=_metrics(
            evidence,
            decisions,
            regions,
            coverage,
            stopping_reason,
            latency_ms=elapsed,
            peak_memory=peak,
        ),
    )
    result = replace(
        result,
        metrics={
            **result.metrics,
            "candidate_rail_hit": candidate_rail_hit,
            "stage_latency_ms": {
                "behavioral_facets": (facets_done - started) * 1000.0,
                "capability_census": (capabilities_done - facets_done) * 1000.0,
                "candidate_discovery": (discovery_done - capabilities_done)
                * 1000.0,
                "coverage_admission": (admission_done - discovery_done)
                * 1000.0,
            },
        },
    )
    return result.sealed()


__all__ = [
    "EcosystemAdapter",
    "build_structured_symbol_passages",
    "census_capabilities",
    "derive_certified_relationships",
    "detect_ecosystem_adapter",
    "discover_candidates",
    "extract_behavior_facets",
    "fuse_by_evidence_class",
    "localize_vnext",
    "merge_regions",
]

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
from array import array
from collections import OrderedDict, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, cast

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
    elif (
        operation
        or observed
        or expected
        or state_sentence
        or transition
        or invariant
        or mandatory_obligations
        or policies != ("generic",)
    ):
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
_STRUCTURED_PASSAGE_CACHE_MAX = 2
_STRUCTURED_PASSAGE_CACHE_MAX_BYTES = 32 * 1024 * 1024
_STRUCTURED_PASSAGE_CACHE: OrderedDict[
    tuple[Any, ...],
    tuple[dict[str, str], int],
] = OrderedDict()
_STRUCTURED_PASSAGE_CACHE_LOCK = threading.Lock()


def _graph_file_signature(graph_db: str) -> tuple[tuple[int, int], ...]:
    """Return a cheap invalidation signature for an immutable indexed graph."""
    signatures: list[tuple[int, int]] = []
    for suffix in ("", "-wal"):
        try:
            stat = Path(f"{graph_db}{suffix}").stat()
            signatures.append((int(stat.st_size), int(stat.st_mtime_ns)))
        except OSError:
            signatures.append((-1, -1))
    return tuple(signatures)


def _passage_cache_size(passages: dict[str, str]) -> int:
    """Conservative deterministic storage estimate for bounded LRU admission."""
    return 256 + sum(
        128 + 4 * (len(key) + len(value))
        for key, value in passages.items()
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
    wanted = {_norm(path) for path in file_paths or ()}
    try:
        graph_identity = str(Path(request.graph_db).resolve())
    except OSError:
        graph_identity = str(request.graph_db)
    cache_key = (
        graph_identity,
        request.revision_identity,
        _graph_file_signature(request.graph_db),
        tuple(sorted(wanted)),
    )
    with _STRUCTURED_PASSAGE_CACHE_LOCK:
        cached_entry = _STRUCTURED_PASSAGE_CACHE.get(cache_key)
        if cached_entry is not None:
            _STRUCTURED_PASSAGE_CACHE.move_to_end(cache_key)
            return dict(cached_entry[0])

    con = _open_graph(request.graph_db)
    if con is None:
        return {}
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
        with _STRUCTURED_PASSAGE_CACHE_LOCK:
            cached_passages = dict(passages)
            _STRUCTURED_PASSAGE_CACHE[cache_key] = (
                cached_passages,
                _passage_cache_size(cached_passages),
            )
            _STRUCTURED_PASSAGE_CACHE.move_to_end(cache_key)
            while (
                len(_STRUCTURED_PASSAGE_CACHE)
                > _STRUCTURED_PASSAGE_CACHE_MAX
                or sum(
                    entry[1]
                    for entry in _STRUCTURED_PASSAGE_CACHE.values()
                )
                > _STRUCTURED_PASSAGE_CACHE_MAX_BYTES
            ):
                _STRUCTURED_PASSAGE_CACHE.popitem(last=False)
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
        # A symbol name proves entity/operation identity, not what the code
        # currently does. Observed behavior requires body, property, graph, or
        # runtime evidence.
        roles.add("operation")
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
    # Structure proves structure. A typed edge or property proves what the code
    # DOES at that span; it never proves that this is the behavior the issue is
    # asking about. `expected_behavior` is a claim about the ISSUE, so no purely
    # structural fact may grant it - otherwise one unrelated certified fact
    # closes a mandatory role and every relevant region becomes DEFER redundant.
    if relation in {"RAISES", "CATCHES"} or "exception" in property_kind:
        # Raising/catching IS control flow, so `transition` is structural truth
        # and stays. Only the issue-level claim is withheld.
        roles.update(("exception", "transition"))
    if relation in {"DATA_FLOW", "PRECEDES", "READS", "WRITES"}:
        roles.add("transition")
    if relation in {"READS", "WRITES"}:
        roles.add("state")
    if property_kind in {
        "boundary_condition",
        "guard",
        "guard_clause",
        "conditional_return",
    }:
        roles.add("invariant")
    if property_kind in {"data_flow", "call_order", "exception_flow"}:
        roles.add("transition")
    if property_kind in {"serialization", "serialization_pair"}:
        roles.add("serialization")
    if property_kind in {"field_read", "side_effect", "state_read", "state_write"}:
        roles.add("state")
    return tuple(sorted(roles))


def _resolve_traceback_location(
    request: LocalizationRequest,
    raw_path: str,
    line: int,
) -> tuple[str, str]:
    """Resolve an absolute/frame path only through a unique repository suffix."""
    normalized = _norm(raw_path)
    root = Path(request.repository_root)
    matches: set[str] = set()

    direct = _safe_source_path(root, normalized)
    if direct is not None:
        try:
            matches.add(_norm(str(direct.relative_to(root.resolve()))))
        except (OSError, ValueError):
            pass

    con = _open_graph(request.graph_db)
    graph_files: list[str] = []
    if con is not None:
        try:
            if "nodes" in _table_names(con):
                graph_files = [
                    _norm(str(row[0] or ""))
                    for row in con.execute(
                        "SELECT DISTINCT file_path FROM nodes ORDER BY file_path"
                    )
                    if str(row[0] or "")
                ]
        finally:
            con.close()
    for graph_path in graph_files:
        if (
            normalized == graph_path
            or normalized.endswith("/" + graph_path)
        ) and (root / graph_path).is_file():
            matches.add(graph_path)

    if not matches:
        parts = tuple(part for part in normalized.split("/") if part)
        for index in range(len(parts)):
            suffix = "/".join(parts[index:])
            if (root / suffix).is_file():
                matches.add(suffix)

    if not matches:
        return normalized, ""
    longest = max(len(path.split("/")) for path in matches)
    winners = sorted(
        path for path in matches if len(path.split("/")) == longest
    )
    if len(winners) != 1:
        return normalized, ""
    resolved = winners[0]

    symbol = ""
    con = _open_graph(request.graph_db)
    if con is not None:
        try:
            if "nodes" in _table_names(con):
                row = con.execute(
                    """
                    SELECT name,qualified_name
                    FROM nodes
                    WHERE file_path=?
                      AND COALESCE(is_test,0)=0
                      AND COALESCE(start_line,0) <= ?
                      AND COALESCE(end_line,0) >= ?
                    ORDER BY (end_line-start_line) ASC,
                             start_line DESC,
                             id ASC
                    LIMIT 1
                    """,
                    (resolved, line, line),
                ).fetchone()
                if row is not None:
                    symbol = str(row["qualified_name"] or row["name"] or "")
        finally:
            con.close()
    return resolved, symbol


def _traceback_evidence(request: LocalizationRequest, facets: BehaviorFacet) -> list[EvidenceUnit]:
    out: list[EvidenceUnit] = []
    for rank, match in enumerate(_TRACEBACK_RE.finditer(request.issue_text), start=1):
        raw_path = match.group("py") or match.group("generic") or ""
        line = int(match.group("pyline") or match.group("gline") or 0)
        path, symbol = _resolve_traceback_location(
            request,
            raw_path,
            line,
        )
        out.append(
            EvidenceUnit.create(
                file_path=path,
                symbol=symbol,
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


class _TruncationAwareList(list[Any]):
    semantic_executed: bool = False
    lexical_executed: frozenset[str] | None = None

    """A list that preserves whether an upstream candidate pool was cut."""

    def __init__(self, values: Iterable[Any], *, total_count: int) -> None:
        super().__init__(values)
        self.total_count = max(int(total_count), len(self))
        self.truncated = self.total_count > len(self)


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
    return _TruncationAwareList(
        [row for _, row in scored[: request.policy.max_candidates]],
        total_count=len(scored),
    )


class _FtsSignals(dict[int, list[tuple[EvidenceFamily, int, float]]]):
    """Lexical signals plus an EXECUTION witness for the legs that produced them.

    ``census_capabilities`` reports ``node_fts``/``body_fts`` from table NAMES,
    and this retriever is correct-or-quiet: it swallows every failure and yields
    ``{}``. Without a witness a dark leg is indistinguishable from an ordinary
    retrieval miss - the same failure ``frozen_semantic`` already carries an
    execution witness for.
    """

    def __init__(
        self,
        values: dict[int, list[tuple[EvidenceFamily, int, float]]],
        *,
        executed: frozenset[str],
    ) -> None:
        super().__init__(values)
        self.executed = executed


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
    executed: set[str] = set()
    if not {"nodes_fts", "symbol_content_fts"} & tables:
        return _FtsSignals({}, executed=frozenset())
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
            executed.add("node_fts")
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
            executed.add("body_fts")
    except Exception:
        # Retrieval is correct-or-quiet. Capability census records table
        # presence separately from successful query execution - so the witness
        # goes back EMPTY: nothing reached the pipeline, including a leg that
        # ran before a later one raised.
        return _FtsSignals({}, executed=frozenset())

    signals: dict[int, list[tuple[EvidenceFamily, int, float]]] = defaultdict(list)
    for family, rows in ranked:
        for rank, (node_id, _name, _file_path, score) in enumerate(
            rows,
            start=1,
        ):
            signals[int(node_id)].append((family, rank, float(score)))
    return _FtsSignals(dict(signals), executed=frozenset(executed))


_SEMANTIC_VECTOR_CACHE_MAX = 50_000
_SEMANTIC_VECTOR_CACHE_MAX_BYTES = 96 * 1024 * 1024
_SEMANTIC_ENCODE_CHUNK_SIZE = 64
_SEMANTIC_RANK_DECIMALS = 5
_SEMANTIC_VECTOR_CACHE: weakref.WeakKeyDictionary[
    Any,
    OrderedDict[str, array[float]],
] = weakref.WeakKeyDictionary()
_SEMANTIC_VECTOR_CACHE_BYTES: weakref.WeakKeyDictionary[
    Any,
    int,
] = weakref.WeakKeyDictionary()
_SEMANTIC_VECTOR_CACHE_LOCK = threading.Lock()


def _encode_structured_semantics(
    embedder: Any,
    issue_text: str,
    passages: dict[str, str],
) -> tuple[tuple[float, ...], dict[str, array[float]]]:
    """Encode a singleton issue batch and reuse immutable passage vectors.

    Query inference must never share a batch with a variable number of cache
    misses.  Frozen ONNX kernels can vary slightly with batch shape; at the
    candidate rail that last-bit drift can change which evidence unit is kept.
    """
    passage_keys = sorted(passages)
    passage_chunks = [
        passage_keys[index : index + _SEMANTIC_ENCODE_CHUNK_SIZE]
        for index in range(0, len(passage_keys), _SEMANTIC_ENCODE_CHUNK_SIZE)
    ]
    cache_keys: dict[str, str] = {}
    for chunk in passage_chunks:
        passage_digests = [
            hashlib.sha256(passages[key].encode("utf-8")).hexdigest()
            for key in chunk
        ]
        chunk_digest = hashlib.sha256(
            "\0".join(passage_digests).encode("ascii")
        ).hexdigest()
        cache_keys.update(
            {
                key: f"{chunk_digest}:{digest}"
                for key, digest in zip(chunk, passage_digests)
            }
        )
    cached: dict[str, array[float]] = {}
    cache: OrderedDict[str, array[float]] | None
    try:
        with _SEMANTIC_VECTOR_CACHE_LOCK:
            cache = _SEMANTIC_VECTOR_CACHE.setdefault(
                embedder,
                OrderedDict(),
            )
            _SEMANTIC_VECTOR_CACHE_BYTES.setdefault(embedder, 0)
            for key in passage_keys:
                cache_key = cache_keys[key]
                vector = cache.get(cache_key)
                if vector is not None:
                    cache.move_to_end(cache_key)
                    cached[key] = vector
    except TypeError:
        # A custom embedder may not support weak references or identity
        # hashing. Preserve the uncached encode path for compatibility.
        cache = None

    encode_query = getattr(embedder, "encode_query", None)
    if callable(encode_query):
        query_encoded = cast(
            Callable[[str], Any],
            encode_query,
        )(issue_text)
    else:
        query_encoded = embedder.encode([issue_text])
    query_vector = tuple(float(value) for value in query_encoded[0])
    encoded_keys: list[str] = []
    for chunk in passage_chunks:
        if all(key in cached for key in chunk):
            continue
        passage_texts = [passages[key] for key in chunk]
        encode_passages = getattr(embedder, "encode_passages", None)
        if callable(encode_passages):
            passage_encoded = cast(
                Callable[[Sequence[str]], Any],
                encode_passages,
            )(passage_texts)
        else:
            passage_encoded = embedder.encode(passage_texts)
        for key, vector in zip(chunk, passage_encoded):
            dtype = str(getattr(vector, "dtype", "")).lower()
            cached[key] = array(
                "f" if dtype == "float32" else "d",
                (float(value) for value in vector),
            )
            encoded_keys.append(key)

    if cache is not None and encoded_keys:
        with _SEMANTIC_VECTOR_CACHE_LOCK:
            cache_bytes = int(
                _SEMANTIC_VECTOR_CACHE_BYTES.get(embedder, 0)
            )
            for key in encoded_keys:
                cache_key = cache_keys[key]
                previous = cache.get(cache_key)
                if previous is not None:
                    cache_bytes -= (
                        len(previous) * previous.itemsize
                        + len(cache_key)
                        + 64
                    )
                cache[cache_key] = cached[key]
                cache.move_to_end(cache_key)
                cache_bytes += (
                    len(cached[key]) * cached[key].itemsize
                    + len(cache_key)
                    + 64
                )
            while (
                len(cache) > _SEMANTIC_VECTOR_CACHE_MAX
                or cache_bytes > _SEMANTIC_VECTOR_CACHE_MAX_BYTES
            ):
                removed_key, removed = cache.popitem(last=False)
                cache_bytes -= (
                    len(removed) * removed.itemsize
                    + len(removed_key)
                    + 64
                )
            _SEMANTIC_VECTOR_CACHE_BYTES[embedder] = max(
                0,
                cache_bytes,
            )
    return query_vector, cached


def _node_evidence(
    con: sqlite3.Connection,
    facets: BehaviorFacet,
    request: LocalizationRequest,
) -> tuple[list[EvidenceUnit], set[int]]:
    surface_rows = _candidate_node_rows(con, facets, request)
    # Query-MATCHED nodes the candidate cap discarded before they could even be
    # ranked. This, and the retrieved-but-unslotted FTS nodes below, are the only
    # REAL truncation of a candidate pool in this function.
    surface_dropped = max(
        0,
        int(getattr(surface_rows, "total_count", len(surface_rows)))
        - len(surface_rows),
    )
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
    passages: dict[str, str] = {}
    if "structured_semantics" not in request.policy.disabled_components:
        # Structured semantics is an independent discovery leg. Restricting
        # passages to lexical/FTS seeds makes it only a reranker and guarantees
        # misses on the hardest behavior-described issues.
        passages = build_structured_symbol_passages(request)
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
    if semantic_rank:
        all_rows = con.execute(
            """
            SELECT id, label, name, qualified_name, file_path, start_line,
                   end_line, signature, language, parent_id
            FROM nodes
            WHERE COALESCE(is_test, 0)=0
            ORDER BY file_path, COALESCE(start_line, 0), id
            """
        ).fetchall()
        row_by_passage = {
            (
                f"{_norm(str(row['file_path'] or ''))}::"
                f"{str(row['qualified_name'] or row['name'] or '')}"
            ): row
            for row in all_rows
        }
        # NOT a candidate pool: the frozen embedder scores EVERY symbol in the
        # repository, and `score > 0.0` is not a discriminating filter, so this
        # union was |the whole repository| and `truncated` became a function of
        # repository size rather than of an actual cut - candidate_rail then
        # fired on 49/60 cases. A dense ranker's tail is an ORDER over the
        # corpus, not a pool that got truncated, so `node_pool_total` is left to
        # the query-conditioned legs below.
        existing_ids = {int(row["id"]) for row in rows}
        for key, (_rank, score) in sorted(
            semantic_rank.items(),
            key=lambda item: (item[1][0], item[0]),
        ):
            row = row_by_passage.get(key)
            if row is None or score <= 0.0 or int(row["id"]) in existing_ids:
                continue
            rows.append(row)
            existing_ids.add(int(row["id"]))
            if len(existing_ids) >= request.policy.max_candidates * 2:
                break

        def candidate_rank(row: sqlite3.Row) -> tuple[Any, ...]:
            node_id = int(row["id"])
            fp = _norm(str(row["file_path"] or ""))
            symbol = str(row["qualified_name"] or row["name"] or "")
            class_ranks: dict[str, int] = {}
            if node_id in surface_rank:
                class_ranks["lexical"] = surface_rank[node_id]
            if node_id in fts_signals:
                class_ranks["lexical"] = min(
                    class_ranks.get("lexical", request.policy.max_candidates + 1),
                    min(
                        rank
                        for _family, rank, _score
                        in fts_signals[node_id]
                    ),
                )
            semantic = semantic_rank.get(f"{fp}::{symbol}")
            if semantic is not None:
                class_ranks["semantic"] = semantic[0]
            exact = any(
                str(row["name"] or "").lower()
                == anchor.lower().rsplit(".", 1)[-1]
                or symbol.lower() == anchor.lower()
                for anchor in facets.anchor_symbols
            )
            fused_score = sum(
                1.0 / (60 + rank) for rank in class_ranks.values()
            )
            return (
                0 if exact else 1,
                -len(class_ranks),
                -round(fused_score, 12),
                fp,
                int(row["start_line"] or 0),
                node_id,
            )

        rows = sorted(rows, key=candidate_rank)[
            : request.policy.max_candidates
        ]
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
        # A body/name match is candidate evidence that this symbol may implement
        # the behavior the issue asks about. It may COVER issue roles for
        # admission; its 0.6 confidence keeps it below certification, which is
        # what stops it laundering. This is the ONLY source of `expected_behavior`
        # now that no structural fact grants it, so gating it on
        # issue_mode == "behavior_described" starved the 27/60 symbol_anchored /
        # explicit_path / traceback cases of any way to satisfy a role they
        # require - measured as gold admission 21/52 -> 16/52.
        query_retrieved = node_id in fts_signals
        semantic = semantic_rank.get(f"{fp}::{symbol}")
        if semantic is not None and semantic[1] > 0.0:
            query_retrieved = True
        if query_retrieved:
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
                    certified_roles=(
                        tuple(sorted(set(_roles_for(facets, symbol=symbol, file_path=fp))))
                        if exact
                        else ()
                    ),
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
                    issue_roles=roles,
                    source_tokens=0,
                    signal_class="lexical",
                    signal_rank=fts_rank,
                    metadata=tuple(metadata)
                    + (("bm25_score", f"{bm25_score:.8f}"),),
                )
            )
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
                    issue_roles=roles,
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
    # The candidate pool is what the QUERY-CONDITIONED legs produced: nodes the
    # surface filter matched (including the ones the cap dropped before ranking)
    # and nodes the lexical retrievers returned. Truncation is a node in that
    # pool that did not survive a cap - never the tail of a corpus-wide ranking.
    node_pool_total = (
        len(node_ids | set(surface_rank) | set(fts_signals)) + surface_dropped
    )
    emitted = _TruncationAwareList(
        evidence,
        total_count=(
            len(evidence) + 1
            if node_pool_total > len(node_ids)
            else len(evidence)
        ),
    )
    # An execution witness, not a score: did the semantic leg RUN at all? A
    # capability reported from file presence alone fails open, and a dark leg
    # then reads as an ordinary retrieval miss.
    emitted.semantic_executed = bool(semantic_rank)
    # Same treatment for the two lexical legs: `None` means "no witness was
    # reported", which is never downgraded to unavailable.
    emitted.lexical_executed = getattr(fts_signals, "executed", None)
    return emitted, node_ids


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
        # Mirror what the underlying evidence is ELIGIBLE for. Rebuilding from
        # the descriptive `roles` would re-grant, at whole-file granularity,
        # every role the evidence itself was not eligible to satisfy.
        role_by_file[_norm(unit.file_path)].update(unit.issue_roles)
    ordered = sorted(counts, key=lambda path: (-counts[path], path))
    return [
        EvidenceUnit.create(
            file_path=path,
            family=EvidenceFamily.HISTORY,
            confidence=0.6,
            provenance=("git_log_name_only", f"touch_count={counts[path]}"),
            roles=tuple(sorted(role_by_file[path])),
            issue_roles=tuple(sorted(role_by_file[path])),
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
                    # The exception TYPE, not the binding alias: `except ParseError
                    # as exc` ends in `exc`, which resolves to no type node, so
                    # taking the last token left this leg permanently dark. Resolve
                    # against the graph instead of guessing a position.
                    target_name = ""
                    targets: list[Any] = []
                    for token in tokens:
                        if token.lower() in {"except", "catch", "catches", "as", "rescue", "err", "error"}:
                            continue
                        found = con.execute(
                            """
                            SELECT id FROM nodes
                            WHERE name=? AND label IN ('Class','Enum','Interface','Struct')
                            ORDER BY id
                            """,
                            (token,),
                        ).fetchall()
                        if len(found) == 1:
                            target_name, targets = token, found
                            break
                    if not target_name or len(targets) != 1:
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
                                    | {"exception", "transition"}
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
            ranking_prior_only = bool(item.get("ranking_prior_only"))
            legacy_rank = max(1, int(item.get("legacy_rank") or rank))
            roles = (
                ()
                if ranking_prior_only
                else _roles_for(facets, symbol=symbol, file_path=str(fp))
            )
            if not roles and facets.operation and not ranking_prior_only:
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
                        provenance=(
                            ("legacy_floor", "model_visible_rank")
                            if ranking_prior_only
                            else provenance
                        ),
                        roles=roles,
                        issue_roles=() if ranking_prior_only else roles,
                        certified_roles=(),
                        source_tokens=0,
                        signal_class=signal_class,
                        signal_rank=rank,
                        metadata=(
                            (
                                ("legacy_rank", str(legacy_rank)),
                                ("ranking_prior_only", "1"),
                            )
                            if ranking_prior_only
                            else ()
                        ),
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
                issue_roles=roles,
                certified_roles=(),
                source_tokens=0,
                signal_class=signal_class,
                signal_rank=rank,
                metadata=metadata,
            )
        )
    return out


class _DiscoveredCandidates(list[EvidenceUnit]):
    """Public-list-compatible discovery batch with honest truncation metadata."""

    semantic_executed: bool = False
    lexical_executed: frozenset[str] | None = None


    def __init__(
        self,
        values: Iterable[EvidenceUnit],
        *,
        total_count: int,
    ) -> None:
        super().__init__(values)
        self.total_count = int(total_count)
        self.truncated = self.total_count > len(self)


def discover_candidates(
    request: LocalizationRequest,
    facets: BehaviorFacet,
    *,
    legacy_discoveries: Sequence[Any] | None = None,
) -> list[EvidenceUnit]:
    evidence: list[EvidenceUnit] = []
    discovery_was_truncated = False
    semantic_executed = False
    lexical_executed: frozenset[str] | None = None
    evidence.extend(_explicit_path_evidence(request, facets))
    evidence.extend(_traceback_evidence(request, facets))
    evidence.extend(_legacy_evidence(legacy_discoveries, facets, request.policy))
    con = _open_graph(request.graph_db)
    if con is not None:
        try:
            nodes, node_ids = _node_evidence(con, facets, request)
            semantic_executed = bool(getattr(nodes, "semantic_executed", False))
            lexical_executed = getattr(nodes, "lexical_executed", None)
            discovery_was_truncated = bool(
                getattr(nodes, "truncated", False)
            )
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
        legacy_ranks = [
            int(value)
            for unit in support
            for name, value in unit.metadata
            if name == "legacy_rank" and value.isdigit()
        ]
        legacy_rank = min(legacy_ranks) if legacy_ranks else None
        issue_certified = any(
            set(unit.certified_roles) & set(unit.issue_roles)
            for unit in support
        )
        return (
            0 if any(unit.explicit_provenance for unit in support) else 1,
            0 if legacy_rank is not None else 1,
            legacy_rank if legacy_rank is not None else request.policy.max_candidates + 1,
            0 if issue_certified else 1,
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
    shadow_rank_by_key = {
        item[0]: rank
        for rank, item in enumerate(
            sorted(by_region.items(), key=lambda item: region_order(item)[:1] + region_order(item)[3:]),
            start=1,
        )
    }

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
        issue_roles = tuple(
            sorted({role for unit in support for role in unit.issue_roles})
        )
        certified_roles = tuple(
            sorted(
                {
                    role
                    for unit in support
                    for role in set(unit.certified_roles) & set(unit.issue_roles)
                }
            )
        )
        classes = tuple(sorted({unit.signal_class for unit in support}))
        families = tuple(sorted({unit.family.value for unit in support}))
        relations = tuple(sorted({unit.relation for unit in support if unit.relation}))
        legacy_ranks = [
            int(value)
            for unit in support
            for name, value in unit.metadata
            if name == "legacy_rank" and value.isdigit()
        ]
        # A region carried ONLY by model-visible legacy ranking priors is the
        # recall-first floor, not a vNext discovery. Mark it deterministically
        # (never from whichever support row happened to sort first) so shadow
        # ranking stays attributable to the engine that produced it.
        prior_only = all(
            dict(unit.metadata).get("ranking_prior_only") == "1" for unit in support
        )
        metadata = (
            tuple(
                (name, value)
                for name, value in best.metadata
                if name not in {"legacy_rank", "ranking_prior_only"}
            )
            + (
                ("fused_rrf_score", f"{fused.get(path, 0.0):.12f}"),
                ("supporting_signal_classes", ",".join(classes)),
                ("supporting_families", ",".join(families)),
                ("supporting_relations", ",".join(relations)),
                ("support_count", str(len(support))),
            )
            + ((("legacy_rank", str(min(legacy_ranks))),) if legacy_ranks else ())
            + ((("ranking_prior_only", "1"),) if prior_only else ())
            + (("shadow_rank", str(shadow_rank_by_key[region_key])),)
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
                issue_roles=issue_roles,
                certified_roles=certified_roles,
                source_tokens=best.source_tokens,
                signal_class="+".join(classes),
                signal_rank=region_rank,
                fact_span=best.fact_span,
                explicit_provenance=any(unit.explicit_provenance for unit in support),
                metadata=metadata,
            )
        )
    batch = _DiscoveredCandidates(
        consolidated,
        total_count=max(
            len(ranked_regions),
            (
                request.policy.max_candidates + 1
                if discovery_was_truncated
                else 0
            ),
        ),
    )
    batch.semantic_executed = semantic_executed
    batch.lexical_executed = lexical_executed
    return batch


def fuse_by_evidence_class(evidence: Iterable[EvidenceUnit], k: int = 60) -> dict[str, float]:
    """RRF once per independent class; correlated signals get one vote per file."""
    best_rank: dict[str, dict[str, int]] = defaultdict(dict)
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
    fused: dict[str, float] = {}
    for fp, class_ranks in best_rank.items():
        # RRF measures RETRIEVAL AGREEMENT and nothing else. Hard provenance has
        # its own top tier in `region_order` and its own qualifier in `_marginal`
        # (`new_fact`), so an additive bonus here is double-counting. A flat +1.0
        # was worth 61 class-agreements against an achievable maximum of ~0.079,
        # which was inert while fused_rank was the last key in the marginal and
        # became decisive once relevance began leading admission.
        fused[fp] = round(sum(1.0 / (k + rank) for rank in class_ranks.values()), 12)
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
                roles=unit.issue_roles,
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
                unit.issue_roles,
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
    # Bound on the bytes the RAIL measures, not on a guessed line width. The
    # rail counts `(len(content)+3)//4` tokens on the real span
    # (SourceRegion.from_source), while this used to assume 20 chars/line;
    # measured real source is ~42.4 chars/line (Python) and ~37.8 (Go), so the
    # engine built regions its own rail then refused. `source_tokens <= T` holds
    # exactly when the joined content is at most `4 * T` characters.
    span = lines[start - 1 : end]
    if span:
        max_chars = 4 * max(1, request.policy.max_region_tokens)
        used = 0
        bounded_end = start
        for offset, line in enumerate(span):
            # The first line is always kept: a region is at least one line, and
            # a single line wider than the budget is a rail decision, not a
            # bounding decision.
            addition = len(line) + (1 if offset else 0)
            if offset and used + addition > max_chars:
                break
            used += addition
            bounded_end = start + offset
        if bounded_end < end:
            end = bounded_end
            reason += "_token_bounded"
    try:
        return SourceRegion.from_source(
            root,
            unit.file_path,
            start,
            end,
            unit.symbol,
            unit.issue_roles,
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


def _subsumes(outer: SourceRegion | None, inner: SourceRegion | None) -> bool:
    """True when `outer` demonstrably contains `inner` - evidenced redundancy.

    A shared role LABEL is not evidence that one region makes another redundant;
    after query broadening every retrieved node carries the issue's full required
    set, so a label test is true by construction. Span containment in the same
    file is an actual subsumption witness.
    """
    if outer is None or inner is None:
        return False
    if _norm(outer.file_path) != _norm(inner.file_path):
        return False
    if outer.start_line <= 0 or inner.start_line <= 0:
        return False
    return outer.start_line <= inner.start_line and outer.end_line >= inner.end_line


def _marginal(
    unit: EvidenceUnit,
    covered: set[str],
    required: set[str],
    expected: set[str],
    role_classes: dict[str, set[str]],
    fused_score: float,
) -> tuple[int, ...]:
    roles = set(unit.issue_roles)
    unit_classes = {
        signal_class
        for signal_class in unit.signal_class.split("+")
        if signal_class
    }
    new_required = roles & required - covered
    certified = len(new_required & set(unit.certified_roles))
    # `role_classes[role]` is EMPTY BY CONSTRUCTION for every role in
    # `new_required`: a role enters `covered` and `role_classes` in the SAME
    # admit step, so a role that is still uncovered has never had a class
    # recorded. The old term therefore read only `unit_classes` and multiplied
    # the answer by len(new_required) - re-counting role breadth that
    # `contributes`/`new_expected` already carry, and making
    # NEW_MANDATORY_INDEPENDENT a claim about ROLES that nothing measured.
    # The only corroboration observable at this point is the candidate's OWN
    # independent signal classes, which is a property of the REGION: an
    # indicator, never a per-role count. It is not redundant with `fused_rank`,
    # which is FILE-granular and ties across every region in one file.
    independent = int(bool(new_required) and len(unit_classes) >= 2)
    new_expected = len(roles & expected - covered)
    # A covered role is independently confirmed when this candidate carries a
    # signal class the role does not already have. The old `== 1` guard made the
    # answer depend on how many classes the FIRST admitted region happened to
    # carry - a role first covered by a two-class region could never be
    # confirmed again. Require only that the role HAS recorded classes, so the
    # independence claim rests on something actually observed.
    independent_confirmation = any(
        role in covered
        and bool(role_classes.get(role))
        and not unit_classes <= role_classes[role]
        for role in roles & required
    )
    new_fact = int(
        independent_confirmation
        or (
            bool(unit.relation or unit.fact_span or unit.explicit_provenance)
            and bool(new_required or (roles & expected - covered))
        )
    )
    token_utility = -max(0, unit.source_tokens)
    fused_rank = int(round(fused_score * 1_000_000))
    # Novelty is a hard CONSTRAINT; relevance is the OBJECTIVE (MMR, Carbonell &
    # Goldstein 1998, at the lambda->1 limit so no arbitrary lambda is invented).
    # Ordering coverage bookkeeping ahead of relevance let a region that merely
    # carried a required role LABEL take the slot from the region retrieval ranked
    # first - measured on run 30191986149, where gold sat at rank #1 and was
    # deferred as redundant for a vendored two-line span.
    #
    # COVERING A NEW MANDATORY ROLE IS A CONTRIBUTION. Without that term all four
    # components are structurally 0 for a lexical-only region (certified needs
    # >=0.9 confidence, independent needs >=2 signal classes, new_expected covers
    # only exception/test_link/alternate_path, new_fact needs relation|fact_span|
    # explicit_provenance), so a 0.6-confidence single-class region carrying a
    # MANDATORY role could never be admitted and was labelled
    # `no_issue_conditioned_contribution` - a false statement about the candidate.
    contributes = int(
        len(new_required) > 0
        or certified > 0
        or independent > 0
        or new_expected > 0
        or new_fact > 0
    )
    # `fused_rrf_score` is FILE-granular, so every region in one file ties on it.
    # With nothing below it discriminating, `token_utility` (cost) decided, and
    # the SMALLEST span won - cost is a budget, never a preference. `signal_rank`
    # is the engine's own within-file retrieval order, so it is the correct
    # tiebreak; cost drops to a final tiebreak and the token rails enforce the
    # budget (Khuller, Moss & Naor 1999: budget is a feasibility constraint on a
    # greedy that maximises gain, not part of the gain itself).
    retrieval_rank = -max(1, int(unit.signal_rank))
    return (
        contributes,
        fused_rank,
        retrieval_rank,
        certified,
        independent,
        new_expected,
        new_fact,
        token_utility,
    )


def _decision_for_rejection(
    unit: EvidenceUnit,
    *,
    previous_rejected: set[str],
    previous_rejected_candidates: set[str],
    policy: LocalizationPolicy,
) -> CandidateDecision | None:
    if (
        unit.evidence_id in previous_rejected
        or unit.candidate_key in previous_rejected_candidates
    ):
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
    prior_applies = bool(
        request.prior_state is not None
        and (
            not request.prior_state.revision_identity
            or request.prior_state.revision_identity
            == request.revision_identity
        )
    )
    if prior_applies and request.prior_state is not None:
        required.update(request.prior_state.unresolved_roles)
    expected = set(facets.expected_roles)
    previous_rejected = set(
        request.prior_state.rejected
        if prior_applies and request.prior_state
        else ()
    )
    previous_rejected_candidates = set(
        request.prior_state.rejected_candidates
        if prior_applies and request.prior_state
        else ()
    )
    fused = (
        {}
        if "class_fusion" in request.policy.disabled_components
        else fuse_by_evidence_class(evidence)
    )

    decisions: dict[str, CandidateDecision] = {}
    candidates: list[EvidenceUnit] = []
    for unit in evidence:
        rejection = _decision_for_rejection(
            unit,
            previous_rejected=previous_rejected,
            previous_rejected_candidates=previous_rejected_candidates,
            policy=request.policy,
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
    source_token_rail_hit = False
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
            and _subsumes(region_cache.get(other.evidence_id), region_cache.get(unit.evidence_id))
            for other in candidates
        )
    }
    if redundant_wrappers:
        for unit in candidates:
            if unit.evidence_id in redundant_wrappers:
                decisions[unit.evidence_id] = CandidateDecision(
                    unit.evidence_id,
                    CandidateAction.DEFER,
                    (ReasonCode.WRAPPER_OR_PASS_THROUGH,),
                )
        candidates = [
            unit for unit in candidates if unit.evidence_id not in redundant_wrappers
        ]
    coverable = {
        role
        for unit in candidates
        if region_cache.get(unit.evidence_id) is not None
        for role in unit.issue_roles
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
                    tuple(sorted(set(unit.issue_roles) & (required | expected))),
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

    # --- best-single-element arm (Khuller, Moss & Naor 1999) -----------------
    # Budgeted maximum coverage attains its (1-1/e) guarantee from
    # `max(greedy_solution, best_single_element)`. GT shipped the greedy arm
    # only, so once a lower-ranked candidate covered the required roles the
    # loop stopped at `required_roles_covered` and the TOP-RANKED candidate was
    # deferred REDUNDANT - the engine ranked the right file first and then
    # declined to deliver it. Measured on run 30221830560 (oss-60): the gold
    # file was ranked but never delivered on 26/60 cases, and on 10 of those it
    # was ranked #1 (`ext2_go_gozero_rest_engine`: gold `rest/engine.go` at #1,
    # admitted `rest/server.go` at #3, stopped). Role coverage is not this
    # product's objective; the edit target is.
    #
    # The anchor is chosen from `candidates`, i.e. AFTER the rejection filters,
    # so a wrapper, a duplicate, a previously-rejected or out-of-repo unit can
    # never become one. It is the retrieval ranking's own top element, not a
    # new signal, and it introduces no threshold or tuned constant.
    # ISSUE-CONDITIONED. Anchoring on raw retrieval rank alone admitted a
    # certified-but-unrelated typed fact whenever it happened to out-rank the
    # query-conditioned candidate - delivering a confident wrong file, which is
    # worse than delivering nothing. The anchor must already carry a role THIS
    # issue asked for; the greedy applies the same conditioning through
    # `new_required`/`new_expected`. In the oss-60 drops the gold file passed
    # this test easily: its roles were present, merely already COVERED.
    # FIRST IN DISCOVERY ORDER, not lowest `signal_rank`. `candidates` preserves
    # the incoming `evidence` sequence, and `LocalizationResult.discoveries` IS
    # `tuple(evidence)` - so evidence order is the fused ranking the engine
    # publishes as `ranked_discovery_files`. `signal_rank` is a raw per-leg
    # retrieval rank from BEFORE fusion; anchoring on it picked a different file
    # than the one the engine itself ranked first. Measured on run 30226219339:
    # the anchor hit discovery-#1 on 49/60 cases, and on 6 of the rest gold WAS
    # discovery-#1 and was still dropped - which is why the anchor recovered 4
    # of 10 rank-1 drops instead of all 10.
    anchor = next(
        (
            unit
            for unit in candidates
            if region_cache.get(unit.evidence_id) is not None
            and (
                set(unit.issue_roles) & (target_required | expected)
                or unit.explicit_provenance
            )
        ),
        None,
    )
    if anchor is not None:
        anchor_region = region_cache[anchor.evidence_id]
        fits = (
            anchor_region.source_tokens <= request.policy.max_region_tokens
            and anchor_region.source_tokens <= request.policy.max_source_tokens
        )
        if fits:
            anchor_roles = set(anchor.issue_roles) & (target_required | expected)
            decisions[anchor.evidence_id] = CandidateDecision(
                anchor.evidence_id,
                CandidateAction.ADMIT,
                (ReasonCode.TOP_RANKED_ANCHOR,),
                tuple(sorted(anchor_roles)),
            )
            admitted_regions.append(anchor_region)
            admitted_ids.add(anchor.evidence_id)
            used_tokens += anchor_region.source_tokens
            covered |= anchor_roles
            for role in anchor_roles:
                role_classes[role].add(anchor.signal_class)
            candidates = [
                unit for unit in candidates if unit.evidence_id != anchor.evidence_id
            ]

    while candidates:
        ranked: list[tuple[tuple[int, ...], EvidenceUnit]] = []
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
        new_roles = (set(unit.issue_roles) & (target_required | expected)) - covered
        positive = marginal[0] > 0
        if not positive:
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (
                    ReasonCode.REDUNDANT
                    if set(unit.issue_roles) & covered
                    else ReasonCode.NO_ISSUE_CONTRIBUTION,
                ),
                (),
                marginal,
            )
            for remainder in candidates:
                remainder_reason = (
                    ReasonCode.REDUNDANT
                    if set(remainder.issue_roles) & covered
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
            # DEFER, never REJECT: REJECT writes state.rejected_candidates and
            # poisons every later turn of the session. A per-region token budget
            # is a feasibility constraint on THIS turn's policy, not a permanent
            # property of the candidate - the same conclusion already reached
            # one branch down for `max_source_tokens`.
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (ReasonCode.TOKEN_RAIL,),
                (),
                marginal,
            )
            source_token_rail_hit = True
            continue
        if used_tokens + region.source_tokens > request.policy.max_source_tokens:
            # Skip this element and keep going: a candidate that does not fit the
            # REMAINING budget says nothing about the candidates behind it. Using
            # it to `break` deleted admissible regions that DO fit and stamped
            # them NO_ISSUE_CONTRIBUTION with an all-zero marginal - recording
            # "contributed nothing" for candidates never evaluated.
            decisions[unit.evidence_id] = CandidateDecision(
                unit.evidence_id,
                CandidateAction.DEFER,
                (ReasonCode.TOKEN_RAIL,),
                (),
                marginal,
            )
            source_token_rail_hit = True
            continue
        reason = (
            ReasonCode.NEW_MANDATORY_CERTIFIED
            if marginal[3] > 0
            else ReasonCode.NEW_MANDATORY_INDEPENDENT
            if marginal[4] > 0
            else ReasonCode.NEW_EXPECTED
            if marginal[5] > 0
            else ReasonCode.INDEPENDENT_CONFIRMATION
            if any(
                role in covered
                and not {
                    signal_class
                    for signal_class in unit.signal_class.split("+")
                    if signal_class
                }
                <= role_classes.get(role, set())
                for role in set(unit.issue_roles) & target_required
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
        for role in unit.issue_roles:
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
                (
                    ReasonCode.REDUNDANT
                    if set(unit.issue_roles) & covered
                    else ReasonCode.NO_ISSUE_CONTRIBUTION,
                ),
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
    # A budget overflow SKIPS an element; it never terminates the greedy
    # (Khuller, Moss & Naor 1999). So a rail that fired mid-run does not explain
    # a run that finished: completion is checked FIRST and the rail is reported
    # on its own channel (`metrics["source_token_rail_hit"]`). Only when the
    # coverage goal was not reached can the budget be what ended selection.
    if not unresolved and target_required <= final_covered:
        stopping_reason = "required_roles_covered"
    elif source_token_rail_hit:
        stopping_reason = "source_token_rail"
    elif stopping_reason == "required_roles_covered" and unresolved:
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
    decisions: Sequence[CandidateDecision],
    coverage: CoverageState,
    evidence: Sequence[EvidenceUnit],
    prior: LocalizationState | None,
    revision_identity: str,
) -> LocalizationState:
    evidence_by_id = {unit.evidence_id: unit for unit in evidence}
    accepted = {
        d.evidence_id
        for d in decisions
        if d.action is CandidateAction.ADMIT
    }
    rejected = {
        d.evidence_id
        for d in decisions
        if d.action is CandidateAction.REJECT
    }
    deferred = {
        d.evidence_id
        for d in decisions
        if d.action is CandidateAction.DEFER
    }
    accepted_candidates = {
        evidence_by_id[d.evidence_id].candidate_key
        for d in decisions
        if d.action is CandidateAction.ADMIT
        and d.evidence_id in evidence_by_id
    }
    rejected_candidates = {
        evidence_by_id[d.evidence_id].candidate_key
        for d in decisions
        if d.action is CandidateAction.REJECT
        and d.evidence_id in evidence_by_id
    }
    deferred_candidates = {
        evidence_by_id[d.evidence_id].candidate_key
        for d in decisions
        if d.action is CandidateAction.DEFER
        and d.evidence_id in evidence_by_id
    }
    prior_applies = bool(
        prior is not None
        and (
            not prior.revision_identity
            or prior.revision_identity == revision_identity
        )
    )
    current_evidence_candidates = {
        evidence_id: unit.candidate_key
        for evidence_id, unit in evidence_by_id.items()
    }
    evidence_candidates = dict(current_evidence_candidates)
    if prior_applies and prior is not None:
        prior_evidence_candidates = dict(prior.evidence_candidates)
        current_candidate_keys = set(current_evidence_candidates.values())

        def carry_prior_ids(values: tuple[str, ...]) -> set[str]:
            return {
                evidence_id
                for evidence_id in values
                if prior_evidence_candidates.get(evidence_id)
                not in current_candidate_keys
            }

        accepted.update(carry_prior_ids(prior.accepted))
        rejected.update(carry_prior_ids(prior.rejected))
        deferred.update(carry_prior_ids(prior.deferred))
        accepted_candidates.update(
            set(prior.accepted_candidates) - current_candidate_keys
        )
        rejected_candidates.update(
            set(prior.rejected_candidates) - current_candidate_keys
        )
        deferred_candidates.update(
            set(prior.deferred_candidates) - current_candidate_keys
        )
        evidence_candidates.update(
            {
                evidence_id: candidate_key
                for evidence_id, candidate_key
                in prior_evidence_candidates.items()
                if candidate_key not in current_candidate_keys
            }
        )
    return LocalizationState(
        revision_identity=revision_identity,
        accepted=tuple(sorted(accepted)),
        rejected=tuple(sorted(rejected)),
        deferred=tuple(sorted(deferred)),
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
        accepted_candidates=tuple(sorted(accepted_candidates)),
        rejected_candidates=tuple(sorted(rejected_candidates)),
        deferred_candidates=tuple(sorted(deferred_candidates)),
        evidence_candidates=tuple(sorted(evidence_candidates.items())),
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
            sorted(
                set(prior.unresolved_roles)
                - set(coverage.unresolved)
                - set(coverage.unavailable)
            )
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
        # The per-region budget is reported on its own channel so it stays
        # observable now that it no longer overwrites `stopping_reason`.
        "source_token_rail_hit": any(
            ReasonCode.TOKEN_RAIL in decision.reason_codes
            for decision in decisions
        ),
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
        if capabilities.available.get("frozen_semantic") and not getattr(
            evidence, "semantic_executed", False
        ):
            # Claimed from file presence, never executed. Downgrade it so the
            # roles it would have covered are reported UNAVAILABLE (missing
            # instrumentation) rather than an ordinary retrieval miss.
            capabilities = replace(
                capabilities,
                available={**capabilities.available, "frozen_semantic": False},
                unavailable={
                    **capabilities.unavailable,
                    "frozen_semantic": "declared_but_never_executed",
                },
            )
        # Same treatment for the two LEXICAL legs: the census claims them from
        # table NAMES while `_fts_candidate_signals` is correct-or-quiet, so a
        # leg that could not execute (no FTS5 module, missing retriever) read as
        # an ordinary retrieval miss. `None` means no witness was reported - a
        # test double or a legacy path - and is never downgraded.
        lexical_executed = getattr(evidence, "lexical_executed", None)
        if lexical_executed is not None:
            dark_legs = [
                leg
                for leg in ("node_fts", "body_fts")
                if capabilities.available.get(leg) and leg not in lexical_executed
            ]
            if dark_legs:
                capabilities = replace(
                    capabilities,
                    available={
                        **capabilities.available,
                        **{leg: False for leg in dark_legs},
                    },
                    unavailable={
                        **capabilities.unavailable,
                        **{
                            leg: "declared_but_never_executed"
                            for leg in dark_legs
                        },
                    },
                )
        discovery_done = time.perf_counter()
        decisions, regions, coverage, stopping_reason = _coverage_admit(
            request, facets, evidence, capabilities
        )
        admission_done = time.perf_counter()
    candidate_rail_hit = bool(
        getattr(evidence, "truncated", False)
    )
    # A truncated candidate pool only explains a run that did NOT finish, and
    # only when nothing more specific ended selection. Overwriting the reason
    # unconditionally discarded what actually stopped the greedy - a run that
    # covered every required role reported the same string as one the rail
    # stopped. The rail keeps its own metric field either way.
    if candidate_rail_hit and stopping_reason == "no_positive_marginal":
        stopping_reason = "candidate_rail"
    if abstain and request.prior_state is not None:
        state = request.prior_state
        delta = LocalizationDelta()
    else:
        state = _make_state(
            decisions,
            coverage,
            evidence,
            request.prior_state,
            request.revision_identity,
        )
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

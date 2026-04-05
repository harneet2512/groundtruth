"""
Ego-graph retrieval for GT structural navigation.

Given seed entities (from issue keywords or edited functions), BFS through
graph.db edges and return a structural neighborhood as a readable map.

v1.0.2: inline import resolution during BFS. Name-match edges are verified
on-the-fly by parsing the source file's imports (~1ms per file, cached).
No upfront processing of all edges — only the 20-50 edges BFS traverses.

Three independent hard caps prevent BFS explosion:
  1. Fan-out: max 10 neighbors per node per direction (SQL LIMIT)
  2. Total nodes: BFS stops at 30 visited nodes
  3. Output lines: format_structural_map() emits max 8 lines
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import deque
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from groundtruth.lsp.session import LSPSession

# Hard caps
MAX_FANOUT = 10       # neighbors per node per direction
MAX_NODES = 30        # total BFS visited nodes
MAX_OUTPUT_LINES = 8  # structural map lines

# Deterministically resolved edge methods (binary: verified or not)
VERIFIED_METHODS = ("same_file", "import", "import_lazy", "class_hierarchy", "fqn")
ALL_METHODS = VERIFIED_METHODS + ("name_match",)

# ---------------------------------------------------------------------------
# Inline import resolution (ported from import_resolve.py, regex-based)
# ---------------------------------------------------------------------------

_RE_FROM_IMPORT = re.compile(
    r"^(?:from\s+(\.*)(\w[\w.]*)?)\s+import\s+(.+)$", re.MULTILINE
)
_RE_IMPORT = re.compile(r"^import\s+(.+)$", re.MULTILINE)
_RE_NAME = re.compile(r"(\w+)(?:\s+as\s+(\w+))?")

# Session-level caches (persist across BFS calls within same process)
_import_cache: dict[str, dict[str, tuple[str, int]]] = {}  # file → {local_name → (module, level)}
_source_cache: dict[str, str] = {}  # file → source text


def _read_file(filepath: str) -> str:
    """Read file with caching."""
    if filepath not in _source_cache:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                _source_cache[filepath] = f.read()
        except OSError:
            _source_cache[filepath] = ""
    return _source_cache[filepath]


def _join_multiline_imports(source: str) -> str:
    """Join multi-line parenthesized imports into single lines.

    Converts: from .base import (\\n  foo, bar,\\n) → from .base import foo, bar
    Critical for Django-style imports. ~1ms per file.
    """
    result: list[str] = []
    in_paren = False
    current: list[str] = []

    for line in source.splitlines():
        stripped = line.strip()
        if in_paren:
            if ")" in stripped:
                current.append(stripped.replace(")", "").strip().rstrip(","))
                result.append(" ".join(current))
                current = []
                in_paren = False
            else:
                part = stripped.split("#")[0].strip().rstrip(",").strip()
                if part:
                    current.append(part)
        elif stripped.startswith(("from ", "import ")) and "(" in stripped and ")" not in stripped:
            in_paren = True
            before_paren = stripped.split("(")[0].strip()
            after_paren = stripped.split("(", 1)[1].strip().rstrip(",").strip()
            current = [before_paren]
            if after_paren:
                current.append(after_paren)
        else:
            result.append(line)

    if current:
        result.append(" ".join(current))
    return "\n".join(result)


def parse_imports_for_file(file_path: str, root: str) -> dict[str, tuple[str, int]]:
    """Parse imports from ONE file. Cached per file. ~1ms first call, 0ms after.

    Returns: {local_name → (module_path, level)}
    """
    abs_path = os.path.join(root, file_path) if not os.path.isabs(file_path) else file_path
    if abs_path in _import_cache:
        return _import_cache[abs_path]

    source = _read_file(abs_path)
    if not source:
        _import_cache[abs_path] = {}
        return {}

    source = _join_multiline_imports(source)
    imports: dict[str, tuple[str, int]] = {}

    for m in _RE_FROM_IMPORT.finditer(source):
        dots = m.group(1) or ""
        module = m.group(2) or ""
        names_str = m.group(3).split("#")[0].strip()
        level = len(dots)

        if names_str == "*":
            continue  # Can't resolve star imports inline

        for nm in _RE_NAME.finditer(names_str):
            orig = nm.group(1)
            alias = nm.group(2)
            local = alias if alias else orig
            imports[local] = (module, level)

    for m in _RE_IMPORT.finditer(source):
        names_str = m.group(1).split("#")[0].strip()
        for nm in re.finditer(r"([\w.]+)(?:\s+as\s+(\w+))?", names_str):
            fullpath = nm.group(1)
            alias = nm.group(2)
            local = alias if alias else fullpath.split(".")[-1]
            imports[local] = (fullpath, 0)

    _import_cache[abs_path] = imports
    return imports


def _resolve_module_to_file(module: str, level: int, source_file: str, root: str) -> str | None:
    """Resolve a Python module path to a file path on disk.

    Returns relative file path from root, or None.
    """
    if level > 0:
        # Relative import
        parts = source_file.replace(os.sep, "/").split("/")
        pkg_parts = parts[:-1]  # Remove filename
        up = level - 1
        if up > len(pkg_parts):
            return None
        if up > 0:
            pkg_parts = pkg_parts[:-up]
        if module:
            pkg_parts.extend(module.split("."))
        module_path = "/".join(pkg_parts)
    else:
        if not module:
            return None
        module_path = module.replace(".", "/")

    # Check module.py
    candidate = os.path.join(root, module_path + ".py")
    if os.path.isfile(candidate):
        return module_path + ".py"

    # Check module/__init__.py
    candidate = os.path.join(root, module_path, "__init__.py")
    if os.path.isfile(candidate):
        return module_path + "/__init__.py"

    return None


def resolve_import_to_node(
    callee_name: str, source_file: str, root: str, conn: sqlite3.Connection,
) -> int | None:
    """Check if source_file imports callee_name and resolve to a target node.

    Returns target node_id if resolved, None otherwise. ~1ms.
    """
    imports = parse_imports_for_file(source_file, root)
    if callee_name not in imports:
        return None

    module, level = imports[callee_name]
    # Normalize source_file to be relative
    src_rel = source_file
    for prefix in ("/testbed/", "/home/", "/tmp/", "/app/"):
        if src_rel.startswith(prefix):
            src_rel = src_rel[len(prefix):]
    src_rel = src_rel.lstrip("/")

    target_file = _resolve_module_to_file(module, level, src_rel, root)
    if target_file is None:
        return None

    # Find node in graph.db matching callee_name in target_file
    node = conn.execute(
        "SELECT id FROM nodes WHERE name = ? AND file_path LIKE ? "
        "AND label IN ('Function', 'Method', 'Class') LIMIT 1",
        (callee_name, f"%{target_file}%"),
    ).fetchone()

    if node:
        return node[0]

    # If target is __init__.py, also check re-exports by looking for the name
    # in submodules of the package (one level deep)
    if target_file.endswith("__init__.py"):
        pkg_dir = target_file.rsplit("/", 1)[0] if "/" in target_file else ""
        # Check if __init__.py re-exports this name from a submodule
        init_imports = parse_imports_for_file(target_file, root)
        if callee_name in init_imports:
            sub_module, sub_level = init_imports[callee_name]
            sub_file = _resolve_module_to_file(sub_module, sub_level, target_file, root)
            if sub_file:
                node = conn.execute(
                    "SELECT id FROM nodes WHERE name = ? AND file_path LIKE ? "
                    "AND label IN ('Function', 'Method', 'Class') LIMIT 1",
                    (callee_name, f"%{sub_file}%"),
                ).fetchone()
                if node:
                    return node[0]

    return None


def extract_ego_graph(
    seed_node_ids: list[int],
    conn: sqlite3.Connection,
    max_hops: int = 3,
    root: str | None = None,
    lsp_session: Any | None = None,
    min_confidence: float = 0.0,
    verified_only: bool = False,
) -> list[dict[str, Any]]:
    """
    Extract the structural neighborhood around seed entities.

    BFS from seed nodes through edges table. When root is provided,
    name-match edges are verified inline via import parsing (~1ms/file).

    Args:
        seed_node_ids: Node IDs from graph.db to start BFS from.
        conn: SQLite connection to graph.db.
        max_hops: BFS depth limit.
        root: Repository root path. Enables inline import resolution for
            name-match edges during BFS.
        lsp_session: Optional LSPSession for lazy edge resolution (legacy).
        min_confidence: Minimum edge confidence to traverse (0.0 = all edges).
        verified_only: If True, only traverse deterministically resolved edges
            (same_file, import, class_hierarchy, fqn) plus inline-resolved.

    Returns:
        List of edge dicts with from/to/confidence/method/hops.
        Empty list = GT stays silent.
    """
    if not seed_node_ids:
        return []

    # Detect resolution_method column name
    cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)").fetchall()}
    res_col = "resolution_method" if "resolution_method" in cols else "resolution"

    # When root is available, query ALL edges (including name_match) and
    # resolve inline. When not available, fall back to filter-based approach.
    if root:
        # Fetch all edges, resolve name_match inline
        edge_filter = "1=1"
        filter_params: tuple[Any, ...] = ()
    elif verified_only:
        method_placeholders = ",".join("?" * len(VERIFIED_METHODS))
        edge_filter = f"e.{res_col} IN ({method_placeholders})"
        filter_params = VERIFIED_METHODS
    elif min_confidence > 0.0:
        edge_filter = "e.confidence >= ?"
        filter_params = (min_confidence,)
    else:
        edge_filter = "1=1"
        filter_params = ()

    visited: set[int] = set()
    edges_found: list[dict[str, Any]] = []
    # Batched write cache: (source_id, target_id) → new resolution_method
    write_cache: dict[tuple[int, int], str] = {}

    # BFS frontier: (node_id, hops)
    frontier: deque[tuple[int, int]] = deque()
    for nid in seed_node_ids:
        frontier.append((nid, 0))
        visited.add(nid)

    while frontier:
        if len(visited) >= MAX_NODES:
            break

        # Process all nodes at current depth
        current_depth_nodes: list[tuple[int, int]] = []
        peek_hops = frontier[0][1] if frontier else -1

        while frontier and frontier[0][1] == peek_hops:
            current_depth_nodes.append(frontier.popleft())

        depth_edges: list[dict[str, Any]] = []

        for current_id, hops in current_depth_nodes:
            if hops >= max_hops:
                continue

            current = conn.execute(
                "SELECT name, file_path, start_line FROM nodes WHERE id = ?",
                (current_id,),
            ).fetchone()
            if not current:
                continue
            cur_name, cur_file, cur_line = current

            # Outgoing edges
            outgoing = conn.execute(
                "SELECT e.source_id, e.target_id, t.name, t.file_path, t.start_line, "
                f"e.confidence, e.{res_col}, e.source_line "
                "FROM edges e JOIN nodes t ON e.target_id = t.id "
                f"WHERE e.source_id = ? AND {edge_filter} "
                "ORDER BY e.confidence DESC LIMIT ?",
                (current_id, *filter_params, MAX_FANOUT),
            ).fetchall()

            for src_id, tgt_id, t_name, t_file, t_line, conf, method, src_line in outgoing:
                # Check write cache first
                cached = write_cache.get((src_id, tgt_id))
                if cached:
                    method = cached
                    conf = 1.0

                should_traverse = False

                if method in VERIFIED_METHODS:
                    should_traverse = True
                elif method == "name_match" and root:
                    # Inline resolution: check if source imports callee
                    resolved = resolve_import_to_node(t_name, cur_file or "", root, conn)
                    if resolved is not None:
                        method = "import_lazy"
                        conf = 1.0
                        write_cache[(src_id, tgt_id)] = "import_lazy"
                        should_traverse = True
                    elif not verified_only:
                        # Fallback: traverse name-match if not in verified-only mode
                        should_traverse = True
                elif method == "name_match" and not verified_only:
                    should_traverse = True

                if not should_traverse:
                    continue

                depth_edges.append({
                    "source_id": src_id, "target_id": tgt_id,
                    "source_line": src_line or 0,
                    "from": {"name": cur_name, "file": cur_file, "line": cur_line},
                    "to": {"name": t_name, "file": t_file, "line": t_line},
                    "confidence": conf, "method": method, "hops": hops + 1,
                    "neighbor_id": tgt_id,
                })

            # Incoming edges
            incoming = conn.execute(
                "SELECT e.source_id, e.target_id, s.name, s.file_path, s.start_line, "
                f"e.confidence, e.{res_col}, e.source_line "
                "FROM edges e JOIN nodes s ON e.source_id = s.id "
                f"WHERE e.target_id = ? AND {edge_filter} "
                "ORDER BY e.confidence DESC LIMIT ?",
                (current_id, *filter_params, MAX_FANOUT),
            ).fetchall()

            for src_id, tgt_id, s_name, s_file, s_line, conf, method, edge_src_line in incoming:
                cached = write_cache.get((src_id, tgt_id))
                if cached:
                    method = cached
                    conf = 1.0

                should_traverse = False

                if method in VERIFIED_METHODS:
                    should_traverse = True
                elif method == "name_match" and root:
                    # Inline: check if the caller file imports cur_name
                    resolved = resolve_import_to_node(cur_name, s_file or "", root, conn)
                    if resolved is not None:
                        method = "import_lazy"
                        conf = 1.0
                        write_cache[(src_id, tgt_id)] = "import_lazy"
                        should_traverse = True
                    elif not verified_only:
                        should_traverse = True
                elif method == "name_match" and not verified_only:
                    should_traverse = True

                if not should_traverse:
                    continue

                depth_edges.append({
                    "source_id": src_id, "target_id": tgt_id,
                    "source_line": edge_src_line or 0,
                    "from": {"name": s_name, "file": s_file, "line": s_line},
                    "to": {"name": cur_name, "file": cur_file, "line": cur_line},
                    "confidence": conf, "method": method, "hops": hops + 1,
                    "neighbor_id": src_id,
                })

        # Add edges to results and expand frontier
        for edge_info in depth_edges:
            edges_found.append({
                "from": edge_info["from"],
                "to": edge_info["to"],
                "confidence": edge_info["confidence"],
                "method": edge_info["method"],
                "hops": edge_info["hops"],
            })
            neighbor_id = edge_info["neighbor_id"]
            if neighbor_id not in visited and len(visited) < MAX_NODES:
                visited.add(neighbor_id)
                frontier.append((neighbor_id, edge_info["hops"]))

    # Batch writes — flush all cached resolutions in one transaction
    if write_cache:
        _flush_write_cache(conn, write_cache)

    return edges_found


def _flush_write_cache(
    conn: sqlite3.Connection,
    cache: dict[tuple[int, int], str],
) -> None:
    """Flush accumulated edge resolution updates in a single transaction."""
    try:
        conn.execute("BEGIN")
        for (source_id, target_id), method in cache.items():
            confidence = 1.0 if method == "lsp" else 0.1
            conn.execute(
                "UPDATE edges SET resolution_method = ?, confidence = ? "
                "WHERE source_id = ? AND target_id = ?",
                (method, confidence, source_id, target_id),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


def format_structural_map(ego_edges: list[dict[str, Any]], max_lines: int = MAX_OUTPUT_LINES) -> str | None:
    """Legacy structural map format. Use format_verdict() for v1.0.3+."""
    return format_verdict(ego_edges)


def format_verdict(
    ego_edges: list[dict[str, Any]],
    seed_names: list[str] | None = None,
    max_lines: int = 4,
) -> str | None:
    """Render ego-graph as a verdict — cross-file impact, not a tree dump.

    v1.0.3: TARGET/RISK/RUN lines. Filters to cross-file edges only.
    Same-file edges are noise — the agent already knows in-file calls.

    Returns:
        Formatted verdict string or None if no cross-file edges.
    """
    if not ego_edges:
        return None

    # Filter to CROSS-FILE edges only
    cross_file = [e for e in ego_edges if e["from"]["file"] != e["to"]["file"]]
    if not cross_file:
        return None

    # Count unique caller/callee files
    caller_files: set[str] = set()
    callee_files: set[str] = set()
    for e in cross_file:
        caller_files.add(e["from"]["file"])
        callee_files.add(e["to"]["file"])
    all_files = caller_files | callee_files

    # Find the most-referenced target file (the "center" of the graph)
    file_ref_count: dict[str, int] = {}
    for e in cross_file:
        for f in (e["from"]["file"], e["to"]["file"]):
            file_ref_count[f] = file_ref_count.get(f, 0) + 1

    # Sort edges: prefer import/import_lazy, then by hops
    method_priority = {"import": 0, "import_lazy": 0, "same_file": 1, "name_match": 2}
    sorted_edges = sorted(
        cross_file,
        key=lambda e: (method_priority.get(e.get("method", ""), 3), e["hops"]),
    )

    lines: list[str] = []

    # Line 1: TARGET — prefer seed file if present, else most-referenced
    # Collect files containing seed nodes
    seed_files: set[str] = set()
    if seed_names:
        for e in cross_file:
            for side in ("from", "to"):
                if e[side]["name"] in seed_names:
                    seed_files.add(e[side]["file"])

    if seed_files:
        top_file = max(seed_files, key=lambda f: file_ref_count.get(f, 0))
    else:
        top_file = max(file_ref_count, key=file_ref_count.get)  # type: ignore[arg-type]
    top_count = file_ref_count[top_file]
    seed_label = ""
    if seed_names:
        seed_label = f" \u2014 {seed_names[0]}" if len(seed_names) == 1 else f" \u2014 {', '.join(seed_names[:2])}"
    lines.append(f"TARGET: {top_file}{seed_label} ({top_count} cross-file refs)")

    # Line 2: RISK — highest-impact cross-file edge
    if sorted_edges:
        e = sorted_edges[0]
        method_tag = f" [{e.get('method', '')}]" if e.get("method") in ("import", "import_lazy") else ""
        lines.append(
            f"RISK: {e['from']['file']}:{e['from']['name']}() "
            f"\u2192 {e['to']['file']}:{e['to']['name']}(){method_tag}"
        )

    # Line 3: Additional cross-file dependencies (if any unique files)
    other_files = sorted(all_files - {top_file})[:3]
    if other_files:
        lines.append(f"ALSO: {', '.join(other_files)}")

    return "\n".join(lines[:max_lines]) if lines else None


# Paths that should never be seed sources (vendored, docs, tutorials)
_EXCLUDE_PATH_FRAGMENTS = (
    "/doc/", "/docs/", "/tutorial/", "/tutorials/", "/examples/",
    "/example/", "/vendor/", "/vendored/", "/third_party/", "/extern/",
    "/node_modules/", "/__pycache__/", "/test_", "/tests/test_",
)

_SOURCE_EXCLUDE_SQL = " AND ".join(
    f"file_path NOT LIKE '%{frag}%'" for frag in _EXCLUDE_PATH_FRAGMENTS
)


def find_seeds_by_name(
    names: list[str],
    conn: sqlite3.Connection,
) -> list[int]:
    """Find node IDs matching seed names in graph.db.

    v1.0.3: prioritized matching — file paths first, then class names,
    then functions. Excludes doc/vendor/tutorial paths.
    """
    node_ids: list[int] = []
    seen_ids: set[int] = set()

    def _add(rows: list[tuple[int, ...]]) -> None:
        for row in rows:
            if row[0] not in seen_ids and len(node_ids) < 10:
                seen_ids.add(row[0])
                node_ids.append(row[0])

    for name in names:
        if len(node_ids) >= 10:
            break

        # Priority 1: File path match (highest signal)
        # Identifiers like "django/core/validators.py" or "sklearn/linear_model/ridge.py"
        if "/" in name and ("." in name.split("/")[-1]):
            rows = conn.execute(
                f"SELECT id FROM nodes WHERE file_path LIKE ? "
                f"AND label IN ('Function', 'Method', 'Class') "
                f"AND {_SOURCE_EXCLUDE_SQL} LIMIT 5",
                (f"%{name}%",),
            ).fetchall()
            _add(rows)
            continue

        # Priority 2: Exact class match (CamelCase — URLValidator, ContentType)
        if name[0:1].isupper() and any(c.islower() for c in name):
            rows = conn.execute(
                f"SELECT id FROM nodes WHERE name = ? AND label = 'Class' "
                f"AND {_SOURCE_EXCLUDE_SQL} LIMIT 3",
                (name,),
            ).fetchall()
            if rows:
                _add(rows)
                continue

        # Priority 3: Exact function/method match (excluding non-source paths)
        rows = conn.execute(
            f"SELECT id FROM nodes WHERE name = ? "
            f"AND label IN ('Function', 'Method', 'Class') "
            f"AND {_SOURCE_EXCLUDE_SQL} "
            f"ORDER BY start_line LIMIT 3",
            (name,),
        ).fetchall()
        _add(rows)

    return node_ids


def find_test_for_seeds(
    seed_node_ids: list[int],
    conn: sqlite3.Connection,
) -> str | None:
    """Find test file that imports or tests any seed entity."""
    if not seed_node_ids:
        return None

    placeholders = ",".join("?" * len(seed_node_ids))
    test_row = conn.execute(
        f"SELECT DISTINCT n.file_path FROM nodes n "
        f"JOIN edges e ON e.source_id = n.id "
        f"WHERE e.target_id IN ({placeholders}) "
        f"AND n.is_test = 1 "
        f"LIMIT 1",
        seed_node_ids,
    ).fetchone()

    if not test_row:
        return None

    test_file = test_row[0]
    ext = os.path.splitext(test_file)[1].lower()
    if ext == ".py":
        return f"python -m pytest {test_file} -xvs"
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        return f"npx jest {test_file}"
    elif ext == ".go":
        test_dir = os.path.dirname(test_file) or "."
        return f"go test ./{test_dir}/..."
    elif ext == ".rs":
        return "cargo test"
    elif ext == ".java":
        return "mvn test"
    elif ext == ".rb":
        return f"bundle exec rspec {test_file}"
    return None

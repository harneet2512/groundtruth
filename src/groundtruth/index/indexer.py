"""Orchestrates LSP queries to build the symbol index."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from groundtruth.index.ast_parser import (
    ASTSymbol,
    extract_base_classes,
    extract_class_attributes,
    parse_python_file,
    parse_python_imports,
)
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.lsp.client import LSPClient
from groundtruth.lsp.config import LANGUAGE_IDS, get_language_id, get_server_config
from groundtruth.lsp.manager import LSPManager
from groundtruth.lsp.protocol import DocumentSymbol, Hover, MarkupContent, SymbolKind
from groundtruth.utils.logger import get_logger
from groundtruth.utils.platform import normalize_path, paths_equal, uri_to_path
from groundtruth.utils.result import Err, GroundTruthError, Ok, Result

logger = get_logger(__name__)

# Minimal set — git ls-files handles the rest
IGNORE_DIRS = {".git", "node_modules", "__pycache__"}

# Binary/data/config files that should never be indexed
SKIP_EXTENSIONS = frozenset(
    {
        # Images
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".svg",
        ".webp",
        ".tiff",
        ".tif",
        # Compiled / binary
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".obj",
        ".pyc",
        ".pyo",
        ".class",
        ".wasm",
        # Archives
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        # Data / config
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".csv",
        # Documents
        ".md",
        ".rst",
        ".txt",
        ".pdf",
        ".doc",
        ".docx",
        # Lock files
        ".lock",
        # Fonts
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        # Media
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".flv",
        # Other
        ".map",
        ".min.js",
        ".min.css",
        ".d.ts",
    }
)

_SYMBOL_KIND_MAP: dict[SymbolKind, str] = {
    SymbolKind.FILE: "file",
    SymbolKind.MODULE: "module",
    SymbolKind.NAMESPACE: "namespace",
    SymbolKind.PACKAGE: "package",
    SymbolKind.CLASS: "class",
    SymbolKind.METHOD: "method",
    SymbolKind.PROPERTY: "property",
    SymbolKind.FIELD: "field",
    SymbolKind.CONSTRUCTOR: "constructor",
    SymbolKind.ENUM: "enum",
    SymbolKind.INTERFACE: "interface",
    SymbolKind.FUNCTION: "function",
    SymbolKind.VARIABLE: "variable",
    SymbolKind.CONSTANT: "constant",
    SymbolKind.STRING: "string",
    SymbolKind.NUMBER: "number",
    SymbolKind.BOOLEAN: "boolean",
    SymbolKind.ARRAY: "array",
    SymbolKind.OBJECT: "object",
    SymbolKind.KEY: "key",
    SymbolKind.NULL: "null",
    SymbolKind.ENUM_MEMBER: "enum_member",
    SymbolKind.STRUCT: "struct",
    SymbolKind.EVENT: "event",
    SymbolKind.OPERATOR: "operator",
    SymbolKind.TYPE_PARAMETER: "type_parameter",
}


def symbol_kind_to_str(kind: SymbolKind) -> str:
    """Map LSP SymbolKind to our string kind."""
    return _SYMBOL_KIND_MAP.get(kind, "unknown")


def is_exported(symbol: DocumentSymbol, language: str) -> bool:
    """Heuristic: determine if a symbol is exported/public.

    - Python/Go: public by default (not starting with _)
    - TypeScript/JavaScript: conservative — assume exported if top-level
    - Rust: assume public if top-level
    """
    name = symbol.name
    if language in ("python",):
        return not name.startswith("_")
    if language in ("go",):
        return len(name) > 0 and name[0].isupper()
    # TS/JS/Rust: assume top-level symbols are exported
    return True


def parse_hover_signature(
    hover: Hover,
) -> tuple[str | None, str | None, str | None]:
    """Extract (signature, params_json, return_type) from hover content.

    Returns (signature, params, return_type) — all optional.
    """
    text: str
    if isinstance(hover.contents, MarkupContent):
        text = hover.contents.value
    elif isinstance(hover.contents, str):
        text = hover.contents
    elif isinstance(hover.contents, list):
        text = "\n".join(hover.contents)
    else:
        return (None, None, None)

    if not text.strip():
        return (None, None, None)

    # Extract first code block or use entire text as signature
    lines = text.strip().splitlines()
    signature_lines: list[str] = []
    in_code_block = False
    for line in lines:
        if line.startswith("```"):
            if in_code_block:
                break
            in_code_block = True
            continue
        if in_code_block:
            signature_lines.append(line)

    signature = "\n".join(signature_lines).strip() if signature_lines else lines[0].strip()
    if not signature:
        return (None, None, None)

    # Try to extract return type from signature
    return_type: str | None = None
    if " -> " in signature:
        return_type = signature.split(" -> ")[-1].strip().rstrip(":")
    elif ": " in signature and "(" not in signature.split(":")[-1]:
        return_type = signature.split(":")[-1].strip()

    return (signature, None, return_type)


def _parse_package_json(path: str) -> list[tuple[str, str | None, str, bool]]:
    """Parse package.json → list of (name, version, 'npm', is_dev)."""
    results: list[tuple[str, str | None, str, bool]] = []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.loads(f.read())
        for dep_name, version in data.get("dependencies", {}).items():
            results.append((dep_name, version, "npm", False))
        for dep_name, version in data.get("devDependencies", {}).items():
            results.append((dep_name, version, "npm", True))
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return results


def _parse_requirements_txt(path: str) -> list[tuple[str, str | None, str, bool]]:
    """Parse requirements.txt → list of (name, version, 'pip', False)."""
    results: list[tuple[str, str | None, str, bool]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                # Split on version specifiers
                for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
                    if sep in line:
                        name, version = line.split(sep, 1)
                        results.append((name.strip(), version.strip(), "pip", False))
                        break
                else:
                    results.append((line.strip(), None, "pip", False))
    except OSError:
        pass
    return results


def _parse_go_mod(path: str) -> list[tuple[str, str | None, str, bool]]:
    """Parse go.mod → list of (name, version, 'go', False)."""
    results: list[tuple[str, str | None, str, bool]] = []
    try:
        with open(path, encoding="utf-8") as f:
            in_require = False
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith("require ("):
                    in_require = True
                    continue
                if in_require and line == ")":
                    in_require = False
                    continue
                if in_require and line:
                    parts = line.split()
                    if len(parts) >= 2:
                        results.append((parts[0], parts[1], "go", False))
                elif line.startswith("require "):
                    parts = line.split()
                    if len(parts) >= 3:
                        results.append((parts[1], parts[2], "go", False))
    except OSError:
        pass
    return results


def _parse_cargo_toml(path: str) -> list[tuple[str, str | None, str, bool]]:
    """Parse Cargo.toml for dependencies (basic TOML parsing)."""
    results: list[tuple[str, str | None, str, bool]] = []
    try:
        with open(path, encoding="utf-8") as f:
            section = ""
            for raw_line in f:
                line = raw_line.strip()
                if line.startswith("["):
                    section = line.strip("[]").strip()
                    continue
                if section in ("dependencies", "dev-dependencies") and "=" in line:
                    name, value = line.split("=", 1)
                    name = name.strip()
                    value = value.strip().strip('"')
                    is_dev = section == "dev-dependencies"
                    results.append((name, value if value else None, "cargo", is_dev))
    except OSError:
        pass
    return results


_ManifestParser = Callable[[str], list[tuple[str, str | None, str, bool]]]

MANIFEST_PARSERS: dict[str, _ManifestParser] = {
    "package.json": _parse_package_json,
    "requirements.txt": _parse_requirements_txt,
    "go.mod": _parse_go_mod,
    "Cargo.toml": _parse_cargo_toml,
}


class Indexer:
    """Indexes a project by querying LSP servers and storing results in SQLite."""

    def __init__(
        self,
        store: SymbolStore,
        lsp_manager: LSPManager,
        exclude_dirs: set[str] | None = None,
    ) -> None:
        self._store = store
        self._lsp_manager = lsp_manager
        self._poison_files: set[str] = set()
        self._crash_counts: dict[str, int] = {}
        self._exclude_dirs = exclude_dirs or set()
        # Server availability caching
        self._server_available: dict[str, bool] = {}
        self._warned_extensions: set[str] = set()

    def _can_index(self, ext: str) -> bool:
        """Check if the LSP server for an extension is available (cached)."""
        if ext in self._server_available:
            return self._server_available[ext]

        config_result = get_server_config(ext)
        if isinstance(config_result, Err):
            self._server_available[ext] = False
            return False

        config = config_result.value
        available = shutil.which(config.command[0]) is not None
        self._server_available[ext] = available

        if not available and ext not in self._warned_extensions:
            self._warned_extensions.add(ext)
            logger.info(
                "lsp_server_missing",
                ext=ext,
                command=config.command[0],
                msg=f"LSP server '{config.command[0]}' not found for {ext} files, skipping",
            )

        return available

    def _is_indexable(self, file_path: str, ext: str) -> bool:
        """Check if a file should be indexed."""
        if ext in SKIP_EXTENSIONS:
            return False
        if ext not in LANGUAGE_IDS:
            return False
        if ext == ".py":
            return True  # AST-based, no LSP needed
        return self._can_index(ext)

    def _read_file_safe(self, file_path: str) -> Result[str, GroundTruthError]:
        """Read a file with UTF-8 first, latin-1 fallback."""
        try:
            with open(file_path, encoding="utf-8", newline="") as f:
                return Ok(f.read())
        except UnicodeDecodeError:
            try:
                with open(file_path, encoding="latin-1", newline="") as f:
                    return Ok(f.read())
            except OSError as exc:
                return Err(
                    GroundTruthError(
                        code="file_read_failed",
                        message=f"Failed to read file: {exc}",
                    )
                )
        except PermissionError as exc:
            return Err(
                GroundTruthError(
                    code="file_read_failed",
                    message=f"Permission denied: {exc}",
                )
            )
        except OSError as exc:
            return Err(
                GroundTruthError(
                    code="file_read_failed",
                    message=f"Failed to read file: {exc}",
                )
            )

    def _load_ignore_patterns(self, root: str) -> list[str]:
        """Load ignore patterns from .gitignore and .groundtruthignore."""
        patterns: list[str] = []
        for ignore_file in (".gitignore", ".groundtruthignore"):
            ignore_path = os.path.join(root, ignore_file)
            try:
                with open(ignore_path, encoding="utf-8") as f:
                    for raw_line in f:
                        line = raw_line.strip()
                        if line and not line.startswith("#"):
                            patterns.append(line)
            except OSError:
                pass
        return patterns

    def _matches_ignore(self, rel_path: str, patterns: list[str]) -> bool:
        """Check if a relative path matches any ignore pattern.

        Uses pathspec (gitwildmatch) if available, falls back to fnmatch.
        """
        try:
            import pathspec

            spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
            return spec.match_file(rel_path)
        except ImportError:
            pass

        # Fallback: fnmatch + directory name matching
        # Normalize to forward slashes for consistent matching
        rel_path_normalized = rel_path.replace("\\", "/")
        parts = rel_path_normalized.split("/")
        for pattern in patterns:
            pattern = pattern.rstrip("/")
            # Check against full path
            if fnmatch(rel_path_normalized, pattern):
                return True
            # Check against each directory component and filename
            for part in parts:
                if fnmatch(part, pattern):
                    return True
        return False

    def _discover_files(self, root_path: str, max_file_size: int) -> list[str]:
        """Discover source files, preferring git ls-files."""
        try:
            result = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=root_path,
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                raw = result.stdout.decode("utf-8", errors="replace")
                rel_paths = [p for p in raw.split("\0") if p]
                files = self._filter_discovered(root_path, rel_paths, max_file_size)
                logger.info(
                    "file_discovery",
                    method="git_ls_files",
                    total_from_git=len(rel_paths),
                    indexable=len(files),
                )
                return files
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        logger.info("file_discovery", method="os_walk", msg="git not available, falling back")
        return self._discover_files_walk(root_path, max_file_size)

    def _filter_discovered(
        self, root_path: str, rel_paths: list[str], max_file_size: int
    ) -> list[str]:
        """Filter a list of relative paths to indexable files.

        git ls-files already handles .gitignore, so we only apply .groundtruthignore here.
        """
        gt_patterns = self._load_groundtruthignore(root_path)
        files: list[str] = []

        for rel in rel_paths:
            ext = os.path.splitext(rel)[1]
            if not self._is_indexable(rel, ext):
                continue

            full_path = normalize_path(os.path.join(root_path, rel))

            # Skip symlinks
            if os.path.islink(os.path.join(root_path, rel)):
                continue

            # Skip files matching .groundtruthignore patterns
            if gt_patterns and self._matches_ignore(rel, gt_patterns):
                continue

            # Skip large files
            try:
                file_size = os.path.getsize(os.path.join(root_path, rel))
            except OSError:
                continue
            if file_size > max_file_size:
                logger.info("skip_large_file", file=full_path, size=file_size, limit=max_file_size)
                continue

            files.append(full_path)
        return files

    def _load_groundtruthignore(self, root: str) -> list[str]:
        """Load patterns from .groundtruthignore only."""
        patterns: list[str] = []
        path = os.path.join(root, ".groundtruthignore")
        try:
            with open(path, encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
        except OSError:
            pass
        return patterns

    def _discover_files_walk(self, root_path: str, max_file_size: int) -> list[str]:
        """Fallback file discovery using os.walk."""
        ignore_patterns = self._load_ignore_patterns(root_path)
        all_ignore_dirs = IGNORE_DIRS | self._exclude_dirs
        files: list[str] = []

        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            # Filter ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in all_ignore_dirs]

            for filename in filenames:
                ext = os.path.splitext(filename)[1]
                if not self._is_indexable(filename, ext):
                    continue

                rel_path = os.path.relpath(os.path.join(dirpath, filename), root_path)
                if ignore_patterns and self._matches_ignore(rel_path, ignore_patterns):
                    continue

                full_path = normalize_path(os.path.join(dirpath, filename))

                # Skip symlinks
                if os.path.islink(os.path.join(dirpath, filename)):
                    logger.info("skip_symlink", file=full_path)
                    continue

                # Skip large files
                try:
                    file_size = os.path.getsize(os.path.join(dirpath, filename))
                except OSError:
                    continue
                if file_size > max_file_size:
                    logger.info(
                        "skip_large_file", file=full_path, size=file_size, limit=max_file_size
                    )
                    continue

                files.append(full_path)

        logger.info(
            "file_discovery",
            method="os_walk",
            indexable=len(files),
        )
        return files

    def _index_single_python_file(self, file_path: str) -> Result[int, GroundTruthError]:
        """Index a single Python file via ast. Returns symbol count."""
        now = int(time.time())
        ast_symbols = parse_python_file(file_path)
        if not ast_symbols:
            # Could be empty file or syntax error — still counts as indexed
            self._store.delete_symbols_in_file(file_path)
            try:
                stat = os.stat(file_path)
                self._store.upsert_file_metadata(file_path, stat.st_mtime, stat.st_size, 0, now)
            except OSError:
                pass
            return Ok(0)

        self._store.delete_symbols_in_file(file_path)
        count = self._insert_ast_symbols(ast_symbols, file_path, now)

        # Resolve imports
        ast_imports = parse_python_imports(file_path)
        for imp in ast_imports:
            sym_result = self._store.find_symbol_by_name(imp.name)
            if isinstance(sym_result, Ok) and sym_result.value:
                # Prefer match whose file_path best matches the module path
                best = self._best_import_match(sym_result.value, imp.module)
                self._store.insert_ref(
                    symbol_id=best.id,
                    referenced_in_file=file_path,
                    referenced_at_line=imp.line,
                    reference_type="import",
                )

        try:
            stat = os.stat(file_path)
            self._store.upsert_file_metadata(file_path, stat.st_mtime, stat.st_size, count, now)
        except OSError:
            pass
        return Ok(count)

    def _insert_ast_symbols(self, symbols: list[ASTSymbol], file_path: str, now: int) -> int:
        """Insert ASTSymbol list into store. Returns total count."""
        count = 0
        for sym in symbols:
            self._store.insert_symbol(
                name=sym.name,
                kind=sym.kind,
                language="python",
                file_path=file_path,
                line_number=sym.line,
                end_line=sym.end_line,
                is_exported=sym.is_exported,
                signature=sym.signature,
                params=None,
                return_type=sym.return_type,
                documentation=sym.documentation,
                last_indexed_at=now,
            )
            count += 1
            for child in sym.children:
                self._store.insert_symbol(
                    name=child.name,
                    kind=child.kind,
                    language="python",
                    file_path=file_path,
                    line_number=child.line,
                    end_line=child.end_line,
                    is_exported=child.is_exported,
                    signature=child.signature,
                    params=None,
                    return_type=child.return_type,
                    documentation=child.documentation,
                    last_indexed_at=now,
                )
                count += 1
        return count

    @staticmethod
    def _best_import_match(candidates: list[SymbolRecord], module: str | None) -> SymbolRecord:
        """Pick the symbol record whose file_path best matches the module."""
        if not module or len(candidates) == 1:
            return candidates[0]
        # Convert dotted module to path segments
        module_parts = module.replace(".", "/")
        for c in candidates:
            if module_parts in c.file_path:
                return c
        return candidates[0]

    async def _index_python_files(self, files: list[str], now: int) -> tuple[int, int, int]:
        """Index Python files via ast. Returns (symbols, indexed, failed)."""
        total_symbols = 0
        files_indexed = 0
        files_failed = 0

        for i, fp in enumerate(files):
            try:
                ast_symbols = parse_python_file(fp)
                self._store.delete_symbols_in_file(fp)
                self._store.delete_attributes_for_file(fp)
                count = self._insert_ast_symbols(ast_symbols, fp, now)
                total_symbols += count
                files_indexed += 1

                # Extract and insert class attributes
                self._extract_and_store_attributes(fp)

                # Extract and insert inheritance refs
                self._extract_and_store_inheritance(fp)

                try:
                    stat = os.stat(fp)
                    self._store.upsert_file_metadata(fp, stat.st_mtime, stat.st_size, count, now)
                except OSError:
                    pass
            except Exception as exc:
                files_failed += 1
                logger.warning("ast_index_failed", file=fp, error=str(exc))
            # Yield to event loop periodically so asyncio.wait_for timeout can fire
            if i % 20 == 0:
                await asyncio.sleep(0)

        return total_symbols, files_indexed, files_failed

    def _extract_and_store_attributes(self, file_path: str) -> None:
        """Extract self.* attributes from classes in a Python file and store them."""
        attrs_by_class = extract_class_attributes(file_path)
        for class_name, attrs in attrs_by_class.items():
            class_syms = self._store.find_symbol_by_name(class_name)
            if not isinstance(class_syms, Ok) or not class_syms.value:
                continue
            class_sym = next((s for s in class_syms.value if s.file_path == file_path), None)
            if class_sym is None:
                continue
            for attr in attrs:
                method_ids: list[int] = []
                for method_name in set(attr.setter_methods + attr.reader_methods):
                    m = self._store.find_symbol_by_name(method_name)
                    if isinstance(m, Ok):
                        method_ids.extend(s.id for s in m.value if s.file_path == file_path)
                self._store.insert_attribute(class_sym.id, attr.name, method_ids or None)

    def _extract_and_store_inheritance(self, file_path: str) -> None:
        """Extract inheritance relationships from a Python file and store as refs."""
        bases_by_class = extract_base_classes(file_path)
        for class_name, bases in bases_by_class.items():
            child_syms = self._store.find_symbol_by_name(class_name)
            if not isinstance(child_syms, Ok) or not child_syms.value:
                continue
            child_sym = next((s for s in child_syms.value if s.file_path == file_path), None)
            if child_sym is None:
                continue
            for base_name in bases:
                base_syms = self._store.find_symbol_by_name(base_name)
                if isinstance(base_syms, Ok) and base_syms.value:
                    self._store.insert_ref(
                        symbol_id=base_syms.value[0].id,
                        referenced_in_file=file_path,
                        referenced_at_line=child_sym.line_number,
                        reference_type="inherits",
                    )

    async def _resolve_python_imports(self, files: list[str], root_path: str) -> int:
        """Parse imports from Python files and create refs. Returns ref count."""
        ref_count = 0
        for i, fp in enumerate(files):
            ast_imports = parse_python_imports(fp)
            for imp in ast_imports:
                sym_result = self._store.find_symbol_by_name(imp.name)
                if isinstance(sym_result, Ok) and sym_result.value:
                    best = self._best_import_match(sym_result.value, imp.module)
                    insert_result = self._store.insert_ref(
                        symbol_id=best.id,
                        referenced_in_file=fp,
                        referenced_at_line=imp.line,
                        reference_type="import",
                    )
                    if isinstance(insert_result, Ok):
                        ref_count += 1
            # Yield to event loop periodically so asyncio.wait_for timeout can fire
            if i % 20 == 0:
                await asyncio.sleep(0)
        return ref_count

    async def index_file(self, file_path: str) -> Result[int, GroundTruthError]:
        """Index a single file. Returns the number of symbols indexed."""
        file_path = normalize_path(file_path)

        if file_path in self._poison_files:
            return Err(
                GroundTruthError(
                    code="poison_file",
                    message=f"File previously caused crashes, skipping: {file_path}",
                )
            )

        ext = os.path.splitext(file_path)[1]
        lang_result = get_language_id(ext)
        if isinstance(lang_result, Err):
            return Err(lang_result.error)
        language = lang_result.value

        # AST-based path for Python files
        if ext == ".py":
            return self._index_single_python_file(file_path)

        # Ensure LSP server is running
        server_result = await self._lsp_manager.ensure_server(ext)
        if isinstance(server_result, Err):
            # Try restart once
            restart_result = await self._lsp_manager.restart_server(ext)
            if isinstance(restart_result, Err):
                return Err(restart_result.error)
            server_result = restart_result
        client: LSPClient = server_result.value

        # Read file safely
        read_result = self._read_file_safe(file_path)
        if isinstance(read_result, Err):
            return Err(read_result.error)
        text = read_result.value

        uri = Path(file_path).as_uri()
        now = int(time.time())

        # Open document in LSP
        await client.did_open(uri, language, 1, text)

        # Get symbols
        symbols_result = await client.document_symbol(uri)
        if isinstance(symbols_result, Err):
            await client.did_close(uri)
            # Track crash count for poison file detection
            self._crash_counts[file_path] = self._crash_counts.get(file_path, 0) + 1
            if self._crash_counts[file_path] >= 2:
                self._poison_files.add(file_path)
                logger.warning(
                    "poison_file", file=file_path, msg="File crashed LSP twice, skipping"
                )
            return Err(symbols_result.error)

        # Delete old symbols for this file (re-index)
        self._store.delete_symbols_in_file(file_path)

        # Insert symbols recursively
        count = 0
        for sym in symbols_result.value:
            count += await self._insert_symbol_recursive(sym, client, uri, file_path, language, now)

        # Close document
        await client.did_close(uri)
        return Ok(count)

    async def _insert_symbol_recursive(
        self,
        sym: DocumentSymbol,
        client: LSPClient,
        uri: str,
        file_path: str,
        language: str,
        now: int,
        timeout: float = 30.0,
    ) -> int:
        """Insert a symbol and its children. Returns count of symbols inserted."""
        kind_str = symbol_kind_to_str(sym.kind)
        exported = is_exported(sym, language)

        # Get hover info for signature/docs
        signature: str | None = None
        params_json: str | None = None
        return_type: str | None = None

        if exported:
            hover_result = await client.hover(
                uri,
                sym.selection_range.start.line,
                sym.selection_range.start.character,
                timeout=timeout,
            )
            if isinstance(hover_result, Ok) and hover_result.value is not None:
                signature, params_json, return_type = parse_hover_signature(hover_result.value)

        insert_result = self._store.insert_symbol(
            name=sym.name,
            kind=kind_str,
            language=language,
            file_path=file_path,
            line_number=sym.range.start.line,
            end_line=sym.range.end.line,
            is_exported=exported,
            signature=signature,
            params=params_json,
            return_type=return_type,
            documentation=sym.detail,
            last_indexed_at=now,
        )

        count = 1

        if isinstance(insert_result, Ok) and exported:
            symbol_id = insert_result.value
            # Get references
            refs_result = await client.references(
                uri,
                sym.selection_range.start.line,
                sym.selection_range.start.character,
                timeout=timeout,
            )
            if isinstance(refs_result, Ok):
                for loc in refs_result.value:
                    ref_file = uri_to_path(loc.uri)
                    if paths_equal(ref_file, file_path):
                        continue  # Skip self-references
                    self._store.insert_ref(
                        symbol_id=symbol_id,
                        referenced_in_file=ref_file,
                        referenced_at_line=loc.range.start.line,
                        reference_type="call",
                    )

        # Process children
        if sym.children:
            for child in sym.children:
                count += await self._insert_symbol_recursive(
                    child, client, uri, file_path, language, now, timeout=timeout
                )

        return count

    async def _index_batch(
        self,
        files: list[str],
        client: LSPClient,
        language: str,
    ) -> list[tuple[str, Result[int, GroundTruthError]]]:
        """Index a batch of files using batch didOpen for better LSP cache utilization.

        Phase 1: didOpen all files (notifications — no lock contention)
        Phase 2: drain to let the LSP server process the batch
        Phase 3: documentSymbol + hover/references per file with short timeouts
        Phase 4: didClose all files
        """
        results: list[tuple[str, Result[int, GroundTruthError]]] = []
        opened_uris: list[tuple[str, str]] = []  # (file_path, uri)

        # Phase 1: didOpen all files in batch
        for fp in files:
            if fp in self._poison_files:
                results.append(
                    (
                        fp,
                        Err(
                            GroundTruthError(
                                code="poison_file",
                                message=f"File previously caused crashes, skipping: {fp}",
                            )
                        ),
                    )
                )
                continue

            read_result = self._read_file_safe(fp)
            if isinstance(read_result, Err):
                results.append((fp, Err(read_result.error)))
                continue

            uri = Path(fp).as_uri()
            await client.did_open(uri, language, 1, read_result.value)
            opened_uris.append((fp, uri))

        # Phase 2: drain to let LSP server process the batch
        await client.drain(timeout=3.0)

        # Phase 3: query each file with short timeouts (server should have cached analysis)
        now = int(time.time())
        batch_timeout = 5.0

        for fp, uri in opened_uris:
            symbols_result = await client.document_symbol(uri, timeout=batch_timeout)
            if isinstance(symbols_result, Err):
                self._crash_counts[fp] = self._crash_counts.get(fp, 0) + 1
                if self._crash_counts[fp] >= 2:
                    self._poison_files.add(fp)
                    logger.warning("poison_file", file=fp, msg="File crashed LSP twice, skipping")
                results.append((fp, Err(symbols_result.error)))
                continue

            # Delete old symbols for this file (re-index)
            self._store.delete_symbols_in_file(fp)

            count = 0
            for sym in symbols_result.value:
                count += await self._insert_symbol_recursive(
                    sym, client, uri, fp, language, now, timeout=batch_timeout
                )
            results.append((fp, Ok(count)))

        # Phase 4: didClose all files
        for _fp, uri in opened_uris:
            await client.did_close(uri)

        return results

    async def index_project(
        self,
        root_path: str,
        force: bool = False,
        concurrency: int = 10,
        max_file_size: int = 1_048_576,
    ) -> Result[int, GroundTruthError]:
        """Index all files in a project. Returns total symbols indexed.

        Args:
            root_path: Project root directory.
            force: If True, re-index all files regardless of mtime.
            concurrency: Max concurrent file indexing tasks.
            max_file_size: Skip files larger than this (bytes).
        """
        start_time = time.monotonic()

        # Discover files
        source_files = self._discover_files(root_path, max_file_size)

        # Load existing metadata for incremental indexing
        metadata_result = self._store.get_all_file_metadata()
        existing_meta: dict[str, dict[str, object]] = (
            metadata_result.value if isinstance(metadata_result, Ok) else {}
        )

        # Determine which files need indexing
        files_to_index: list[str] = []
        on_disk: set[str] = set(source_files)
        files_skipped = 0

        for fp in source_files:
            if force:
                files_to_index.append(fp)
                continue
            meta = existing_meta.get(fp)
            if meta is not None:
                try:
                    stat = os.stat(fp)
                    if stat.st_mtime == meta["mtime"] and stat.st_size == meta["size"]:
                        files_skipped += 1
                        continue
                except OSError:
                    pass
            files_to_index.append(fp)

        # Remove metadata + symbols for files no longer on disk
        for old_fp in list(existing_meta.keys()):
            if old_fp not in on_disk:
                self._store.delete_symbols_in_file(old_fp)
                self._store.delete_file_metadata(old_fp)
                logger.info("removed_deleted_file", file=old_fp)

        logger.info(
            "index_start",
            total_files=len(source_files),
            indexable_files=len(files_to_index),
            skipped=files_skipped,
        )

        # Index files in batches grouped by extension (shares LSP client)
        total = 0
        files_indexed = 0
        files_failed = 0
        BATCH_SIZE = 50

        # Group files by extension
        ext_groups: dict[str, list[str]] = {}
        for fp in files_to_index:
            ext = os.path.splitext(fp)[1]
            ext_groups.setdefault(ext, []).append(fp)

        now = int(time.time())

        # AST-based Python indexing (instant, no LSP)
        python_files = ext_groups.pop(".py", [])
        if python_files:
            logger.info("indexing_python_ast", files=len(python_files))
            py_syms, py_ok, py_fail = await self._index_python_files(python_files, now)
            total += py_syms
            files_indexed += py_ok
            files_failed += py_fail
            ref_count = await self._resolve_python_imports(python_files, root_path)
            logger.info("python_imports_resolved", refs=ref_count)

        # Existing LSP batch loop (unchanged)
        batch_num = 0
        for ext, group_files in ext_groups.items():
            server_result = await self._lsp_manager.ensure_server(ext)
            if isinstance(server_result, Err):
                files_failed += len(group_files)
                for fp in group_files:
                    logger.warning("index_file_failed", file=fp, error=server_result.error.message)
                continue
            client = server_result.value
            lang_result = get_language_id(ext)
            if isinstance(lang_result, Err):
                files_failed += len(group_files)
                continue
            lang = lang_result.value

            for i in range(0, len(group_files), BATCH_SIZE):
                batch = group_files[i : i + BATCH_SIZE]
                batch_num += 1
                logger.info(
                    "indexing_batch",
                    batch=batch_num,
                    files=len(batch),
                    ext=ext,
                    progress=f"{files_indexed + files_failed + len(batch)}/{len(files_to_index)}",
                )
                try:
                    batch_results = await self._index_batch(batch, client, lang)
                except Exception as exc:
                    files_failed += len(batch)
                    logger.warning("index_batch_exception", error=str(exc))
                    continue

                for fp, result in batch_results:
                    if isinstance(result, Ok):
                        total += result.value
                        files_indexed += 1
                        try:
                            stat = os.stat(fp)
                            self._store.upsert_file_metadata(
                                fp,
                                stat.st_mtime,
                                stat.st_size,
                                result.value,
                                now,
                            )
                        except OSError:
                            pass
                    else:
                        files_failed += 1
                        logger.warning("index_file_failed", file=fp, error=result.error.message)

        # Parse package manifests
        for manifest_name, parser_fn in MANIFEST_PARSERS.items():
            manifest_path = os.path.join(root_path, manifest_name)
            if os.path.isfile(manifest_path):
                packages = parser_fn(manifest_path)
                for name, version, pm, is_dev in packages:
                    self._store.insert_package(name, version, pm, is_dev)

        elapsed = time.monotonic() - start_time
        logger.info(
            "index_complete",
            files_total=len(source_files),
            files_indexed=files_indexed,
            files_skipped=files_skipped,
            files_failed=files_failed,
            symbols_total=total,
            duration_seconds=round(elapsed, 2),
        )

        return Ok(total)

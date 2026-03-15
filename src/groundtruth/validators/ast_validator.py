"""AST-based import validation — bypasses LSP entirely.

Supports Python (via stdlib ast) and TypeScript/JavaScript (via regex).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, Ok, Result, GroundTruthError

# Common stdlib top-level modules (Python 3.11+). Not exhaustive but covers
# the vast majority of real-world imports.
_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "abc",
        "aifc",
        "argparse",
        "array",
        "ast",
        "asyncio",
        "atexit",
        "base64",
        "binascii",
        "bisect",
        "builtins",
        "calendar",
        "cgi",
        "cgitb",
        "cmd",
        "code",
        "codecs",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "cProfile",
        "csv",
        "ctypes",
        "dataclasses",
        "datetime",
        "dbm",
        "decimal",
        "difflib",
        "dis",
        "distutils",
        "doctest",
        "email",
        "encodings",
        "enum",
        "errno",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "fnmatch",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "graphlib",
        "grp",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "idlelib",
        "imaplib",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "lib2to3",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "modulefinder",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "numbers",
        "operator",
        "optparse",
        "os",
        "ossaudiodev",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "posix",
        "posixpath",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "pyclbr",
        "pydoc",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "resource",
        "rlcompleter",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtpd",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "sqlite3",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "syslog",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "termios",
        "test",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "tomllib",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "turtledemo",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uu",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        # commonly used sub-packages that appear as top-level in "from X import ..."
        "_thread",
        "__future__",
    }
)


@dataclass(frozen=True)
class AstValidationError:
    """A single AST-based validation error."""

    error_type: str  # 'missing_package' | 'invented_symbol' | 'wrong_module_path'
    message: str
    symbol_name: str
    line: int
    module_path: str | None = None


def _top_level_module(module: str) -> str:
    """Extract the top-level module name from a dotted path."""
    return module.split(".")[0]


def _module_to_paths(module: str) -> list[str]:
    """Convert a dotted module path to candidate file paths."""
    base = module.replace(".", "/")
    candidates = [base + ".py", base + "/__init__.py"]
    # Handle Windows-style absolute paths that got converted to dotted modules
    # e.g., "D:.Groundtruth.src.module" → try the base as-is with drive letter fix
    if len(module) >= 2 and module[1] == ":":
        win_base = module[0] + ":" + module[2:].replace(".", "/")
        candidates.append(win_base + ".py")
        candidates.append(win_base + "/__init__.py")
    return candidates


def _normalize_file_path(path: str) -> str:
    """Normalize a file path for comparison."""
    return path.replace("\\", "/").lstrip("./")


def _find_matching_file(candidate: str, normalized_files: dict[str, str]) -> str | None:
    """Find a matching file in the index, trying exact then suffix match.

    Suffix matching only allows a single directory prefix (e.g., ``src/``).
    This prevents ``auth.ts`` from matching ``src/middleware/auth.ts``.
    """
    norm = _normalize_file_path(candidate)
    # Exact match
    if norm in normalized_files:
        return normalized_files[norm]
    # Suffix match: handles src/ or other single-level prefixes in store paths
    suffix = "/" + norm
    candidate_depth = norm.count("/")
    for stored_norm, orig in normalized_files.items():
        if stored_norm == norm:
            return orig
        if stored_norm.endswith(suffix):
            # Only allow one extra prefix segment (e.g., src/)
            stored_depth = stored_norm.count("/")
            if stored_depth <= candidate_depth + 1:
                return orig
    return None


# ---------------------------------------------------------------------------
# TypeScript/JavaScript import regexes
# ---------------------------------------------------------------------------

# import { X, Y } from './path'
_TS_NAMED_IMPORT_RE = re.compile(
    r"""import\s*\{([^}]+)\}\s*from\s*['"]([^'"]+)['"]""",
)
# import X from './path'
_TS_DEFAULT_IMPORT_RE = re.compile(
    r"""import\s+(\w+)\s+from\s*['"]([^'"]+)['"]""",
)
# import * as X from './path'
_TS_NAMESPACE_IMPORT_RE = re.compile(
    r"""import\s*\*\s*as\s+(\w+)\s+from\s*['"]([^'"]+)['"]""",
)
# Bare import: import 'module'
_TS_BARE_IMPORT_RE = re.compile(
    r"""import\s+['"]([^'"]+)['"]""",
)

# Known npm built-in / node packages to skip
_NODE_BUILTINS: frozenset[str] = frozenset({
    "fs", "path", "os", "util", "http", "https", "crypto", "stream",
    "events", "child_process", "url", "querystring", "buffer", "net",
    "tls", "dns", "assert", "zlib", "readline", "cluster", "worker_threads",
    "node", "process", "console", "module", "vm", "v8", "perf_hooks",
    "async_hooks", "timers", "string_decoder",
})


def _ts_module_to_paths(module_path: str) -> list[str]:
    """Convert a TS/JS import path to candidate file paths.

    './auth/jwt' → ['auth/jwt.ts', 'auth/jwt.tsx', 'auth/jwt/index.ts', ...]
    """
    # Strip leading ./ or ../
    clean = re.sub(r"^\.{1,2}/", "", module_path)
    # Remove leading src/ if present (some cases use it, some don't)
    candidates: list[str] = []
    for base in [clean, f"src/{clean}"]:
        for ext in [".ts", ".tsx", ".js", ".jsx"]:
            candidates.append(base + ext)
        candidates.append(f"{base}/index.ts")
        candidates.append(f"{base}/index.js")
    return candidates


def _count_args(args_str: str) -> int:
    """Count comma-separated arguments, handling nested parens/brackets."""
    if not args_str.strip():
        return 0
    depth = 0
    count = 1
    for ch in args_str:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _parse_signature_param_count(signature: str) -> int | None:
    """Extract parameter count from a stored signature string.

    Examples:
        '(user_id: int) -> User' → 1
        '(email: str, password: str) -> AuthResult' → 2
        '(userId: number) => Promise<User>' → 1
    """
    m = re.match(r"\(([^)]*)\)", signature)
    if not m:
        return None
    params = m.group(1).strip()
    if not params:
        return 0
    return _count_args(params)


class AstValidator:
    """Validates imports using ast.parse() (Python) and regex (TS/JS/Go) + the SymbolStore."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def validate(
        self, code: str, file_path: str, language: str
    ) -> Result[list[AstValidationError], GroundTruthError]:
        """Validate imports in the given code."""
        if language == "python":
            return self._validate_python(code, file_path)
        elif language in ("typescript", "javascript"):
            return self._validate_typescript(code, file_path, language)
        elif language == "go":
            return self._validate_go(code, file_path)
        return Ok([])

    # ------------------------------------------------------------------
    # Python validation
    # ------------------------------------------------------------------

    def _validate_python(
        self, code: str, file_path: str
    ) -> Result[list[AstValidationError], GroundTruthError]:
        """Validate Python imports and function calls using AST parsing."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return Ok([])

        all_files_result = self._store.get_all_files()
        if isinstance(all_files_result, Err):
            return Ok([])

        all_files = all_files_result.value
        normalized_files = {_normalize_file_path(f): f for f in all_files}

        errors: list[AstValidationError] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    err = self._check_import(alias.name, node.lineno, normalized_files)
                    if err is not None:
                        errors.append(err)
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                for alias in node.names:
                    err = self._check_from_import(
                        node.module, alias.name, node.lineno, normalized_files
                    )
                    if err is not None:
                        errors.append(err)

        # Signature validation
        sig_errors = self._validate_python_signatures(tree)
        errors.extend(sig_errors)

        return Ok(errors)

    def _validate_python_signatures(self, tree: ast.Module) -> list[AstValidationError]:
        """Walk ast.Call nodes and check argument counts against stored signatures."""
        errors: list[AstValidationError] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Get the function name
            func_name: str | None = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name is None:
                continue

            # Look up in store
            find_result = self._store.find_symbol_by_name(func_name)
            if isinstance(find_result, Err) or not find_result.value:
                continue

            sym = find_result.value[0]
            if not sym.signature:
                continue
            if sym.kind not in ("function", "method"):
                continue

            expected = _parse_signature_param_count(sym.signature)
            if expected is None:
                continue

            # Count actual args (positional + keyword)
            actual = len(node.args) + len(node.keywords)
            if actual != expected:
                errors.append(AstValidationError(
                    error_type="wrong_arg_count",
                    message=(
                        f"'{func_name}' expects {expected} arg(s) but called with {actual}. "
                        f"Signature: {sym.signature}"
                    ),
                    symbol_name=func_name,
                    line=node.lineno,
                ))
        return errors

    def _check_import(
        self,
        module: str,
        line: int,
        normalized_files: dict[str, str],
    ) -> AstValidationError | None:
        """Check `import X` statement."""
        top = _top_level_module(module)

        if top in _STDLIB_MODULES:
            return None

        pkg_result = self._store.get_package(top)
        if isinstance(pkg_result, Ok) and pkg_result.value is not None:
            return None

        for candidate in _module_to_paths(module):
            if _find_matching_file(candidate, normalized_files) is not None:
                return None

        return AstValidationError(
            error_type="missing_package",
            message=f"'{module}' is not installed and not found in the codebase",
            symbol_name=module,
            line=line,
        )

    def _check_from_import(
        self,
        module: str,
        name: str,
        line: int,
        normalized_files: dict[str, str],
    ) -> AstValidationError | None:
        """Check `from X import Y` statement."""
        top = _top_level_module(module)

        if top in _STDLIB_MODULES:
            return None

        matched_path: str | None = None
        for candidate in _module_to_paths(module):
            match = _find_matching_file(candidate, normalized_files)
            if match is not None:
                matched_path = match
                break

        if matched_path is None:
            pkg_result = self._store.get_package(top)
            if isinstance(pkg_result, Ok) and pkg_result.value is not None:
                return None

            return AstValidationError(
                error_type="missing_package",
                message=f"'{module}' is not installed and not found in the codebase",
                symbol_name=name,
                line=line,
                module_path=module,
            )

        symbols_result = self._store.get_symbols_in_file(matched_path)
        if isinstance(symbols_result, Err):
            return None

        symbol_names = {s.name for s in symbols_result.value}
        if name in symbol_names:
            return None

        find_result = self._store.find_symbol_by_name(name)
        if isinstance(find_result, Ok) and find_result.value:
            actual_file = find_result.value[0].file_path
            return AstValidationError(
                error_type="wrong_module_path",
                message=f"'{name}' not found in '{module}' (exists in {actual_file})",
                symbol_name=name,
                line=line,
                module_path=module,
            )

        return AstValidationError(
            error_type="invented_symbol",
            message=f"'{name}' not found in '{module}'",
            symbol_name=name,
            line=line,
            module_path=module,
        )

    # ------------------------------------------------------------------
    # TypeScript / JavaScript validation
    # ------------------------------------------------------------------

    def _validate_typescript(
        self, code: str, file_path: str, language: str
    ) -> Result[list[AstValidationError], GroundTruthError]:
        """Validate TypeScript/JavaScript imports and function calls using regex."""
        all_files_result = self._store.get_all_files()
        if isinstance(all_files_result, Err):
            return Ok([])

        all_files = all_files_result.value
        normalized_files = {_normalize_file_path(f): f for f in all_files}

        errors: list[AstValidationError] = []

        lines = code.split("\n")
        for line_num, line_text in enumerate(lines, start=1):
            stripped = line_text.strip()
            if not stripped.startswith("import"):
                continue

            # Named imports: import { X, Y } from './path'
            m = _TS_NAMED_IMPORT_RE.search(line_text)
            if m:
                names_str = m.group(1)
                module_path = m.group(2)
                names = [n.strip().split(" as ")[0].strip() for n in names_str.split(",")]
                for name in names:
                    if not name:
                        continue
                    err = self._check_ts_import(
                        name, module_path, line_num, normalized_files
                    )
                    if err is not None:
                        errors.append(err)
                continue

            # Default import: import X from './path'
            m = _TS_DEFAULT_IMPORT_RE.search(line_text)
            if m:
                name = m.group(1)
                module_path = m.group(2)
                err = self._check_ts_import(name, module_path, line_num, normalized_files)
                if err is not None:
                    errors.append(err)
                continue

            # Namespace import: import * as X from './path'
            m = _TS_NAMESPACE_IMPORT_RE.search(line_text)
            if m:
                module_path = m.group(2)
                if module_path.startswith("."):
                    found = self._find_ts_module(module_path, normalized_files)
                    if found is None:
                        errors.append(AstValidationError(
                            error_type="missing_package",
                            message=f"Module '{module_path}' not found in the codebase",
                            symbol_name=m.group(1),
                            line=line_num,
                            module_path=module_path,
                        ))
                else:
                    # External package — check if installed
                    pkg_name = module_path.split("/")[0]
                    if pkg_name.startswith("@"):
                        pkg_name = "/".join(module_path.split("/")[:2])
                    if pkg_name not in _NODE_BUILTINS:
                        pkg_result = self._store.get_package(pkg_name)
                        if isinstance(pkg_result, Err) or pkg_result.value is None:
                            errors.append(AstValidationError(
                                error_type="missing_package",
                                message=f"'{pkg_name}' is not installed",
                                symbol_name=m.group(1),
                                line=line_num,
                                module_path=module_path,
                            ))
                continue

            # Bare import: import 'module'
            m = _TS_BARE_IMPORT_RE.search(line_text)
            if m:
                module_path = m.group(1)
                if not module_path.startswith("."):
                    # External package
                    pkg_name = module_path.split("/")[0]
                    if pkg_name.startswith("@"):
                        pkg_name = "/".join(module_path.split("/")[:2])
                    if pkg_name not in _NODE_BUILTINS:
                        pkg_result = self._store.get_package(pkg_name)
                        if isinstance(pkg_result, Err) or pkg_result.value is None:
                            errors.append(AstValidationError(
                                error_type="missing_package",
                                message=(
                                    f"'{pkg_name}' is not installed"
                                ),
                                symbol_name=pkg_name,
                                line=line_num,
                            ))

        # Signature validation for TS/JS
        sig_errors = self._validate_ts_signatures(code, lines)
        errors.extend(sig_errors)

        return Ok(errors)

    def _find_ts_module(
        self, module_path: str, normalized_files: dict[str, str]
    ) -> str | None:
        """Find a TypeScript/JavaScript module in the index."""
        for candidate in _ts_module_to_paths(module_path):
            match = _find_matching_file(candidate, normalized_files)
            if match is not None:
                return match
        return None

    def _check_ts_import(
        self,
        name: str,
        module_path: str,
        line: int,
        normalized_files: dict[str, str],
    ) -> AstValidationError | None:
        """Check a single TypeScript/JavaScript import symbol."""
        # External package (no relative path)
        if not module_path.startswith("."):
            pkg_name = module_path.split("/")[0]
            if pkg_name.startswith("@"):
                pkg_name = "/".join(module_path.split("/")[:2])
            if pkg_name in _NODE_BUILTINS:
                return None
            pkg_result = self._store.get_package(pkg_name)
            if isinstance(pkg_result, Ok) and pkg_result.value is not None:
                return None  # Known package, can't validate symbols
            return AstValidationError(
                error_type="missing_package",
                message=f"'{pkg_name}' is not installed",
                symbol_name=name,
                line=line,
                module_path=module_path,
            )

        # Relative import — find the module file
        matched_path = self._find_ts_module(module_path, normalized_files)
        if matched_path is None:
            # Module doesn't exist — check if symbol exists elsewhere
            find_result = self._store.find_symbol_by_name(name)
            if isinstance(find_result, Ok) and find_result.value:
                actual_file = find_result.value[0].file_path
                return AstValidationError(
                    error_type="wrong_module_path",
                    message=(
                        f"Module '{module_path}' not found. "
                        f"'{name}' exists in {actual_file}"
                    ),
                    symbol_name=name,
                    line=line,
                    module_path=module_path,
                )
            # Relative import to a nonexistent local module with unknown symbol
            return AstValidationError(
                error_type="invented_symbol",
                message=f"'{name}' not found — module '{module_path}' does not exist",
                symbol_name=name,
                line=line,
                module_path=module_path,
            )

        # Module found — check if symbol exists in it
        symbols_result = self._store.get_symbols_in_file(matched_path)
        if isinstance(symbols_result, Err):
            return None

        symbol_names = {s.name for s in symbols_result.value}
        if name in symbol_names:
            return None

        # Symbol not in module — does it exist elsewhere?
        find_result = self._store.find_symbol_by_name(name)
        if isinstance(find_result, Ok) and find_result.value:
            actual_file = find_result.value[0].file_path
            return AstValidationError(
                error_type="wrong_module_path",
                message=f"'{name}' not found in '{module_path}' (exists in {actual_file})",
                symbol_name=name,
                line=line,
                module_path=module_path,
            )

        return AstValidationError(
            error_type="invented_symbol",
            message=f"'{name}' not found in '{module_path}'",
            symbol_name=name,
            line=line,
            module_path=module_path,
        )

    def _validate_ts_signatures(
        self, code: str, lines: list[str]
    ) -> list[AstValidationError]:
        """Check function calls in TS/JS code against stored signatures."""
        errors: list[AstValidationError] = []
        # Pattern: functionName(args) — look for known function names being called
        # We look for word(args) patterns not preceded by 'function', 'class', 'import'
        call_re = re.compile(r"(?<!\w)(\w+)\s*\(([^)]*)\)")

        for line_num, line_text in enumerate(lines, start=1):
            stripped = line_text.strip()
            # Skip import/export/function/class declarations
            if stripped.startswith(("import ", "export ", "function ", "class ", "//")):
                continue

            for m in call_re.finditer(line_text):
                func_name = m.group(1)
                args_str = m.group(2)

                # Skip common JS keywords that look like function calls
                if func_name in ("if", "for", "while", "switch", "catch", "return",
                                 "typeof", "await", "new", "const", "let", "var",
                                 "require", "console", "Promise"):
                    continue

                find_result = self._store.find_symbol_by_name(func_name)
                if isinstance(find_result, Err) or not find_result.value:
                    continue

                sym = find_result.value[0]
                if not sym.signature:
                    continue
                if sym.kind not in ("function", "method"):
                    continue

                expected = _parse_signature_param_count(sym.signature)
                if expected is None:
                    continue

                actual = _count_args(args_str)
                if actual != expected:
                    errors.append(AstValidationError(
                        error_type="wrong_arg_count",
                        message=(
                            f"'{func_name}' expects {expected} arg(s) "
                            f"but called with {actual}. Signature: {sym.signature}"
                        ),
                        symbol_name=func_name,
                        line=line_num,
                    ))
        return errors

    # ------------------------------------------------------------------
    # Go validation
    # ------------------------------------------------------------------

    # Single import: import "myapp/users"
    _GO_SINGLE_IMPORT_RE = re.compile(r'import\s+"([^"]+)"')
    # Block import: import ( ... )
    _GO_BLOCK_IMPORT_RE = re.compile(r'import\s*\((.*?)\)', re.DOTALL)
    # Individual import inside block: "myapp/users" or alias "myapp/users"
    _GO_IMPORT_LINE_RE = re.compile(r'(?:(\w+)\s+)?"([^"]+)"')
    # Qualified call: pkg.FuncName(
    _GO_QUALIFIED_CALL_RE = re.compile(r'(\w+)\.(\w+)\s*\(')
    # Qualified access (no parens): pkg.Symbol — used as value reference
    _GO_QUALIFIED_ACCESS_RE = re.compile(r'(\w+)\.([A-Za-z_]\w*)(?!\s*\()')

    # Go stdlib top-level packages (not exhaustive but covers common ones)
    _GO_STDLIB: frozenset[str] = frozenset({
        "fmt", "os", "io", "net", "http", "log", "math", "sort", "sync",
        "time", "strings", "strconv", "bytes", "errors", "context",
        "crypto", "encoding", "flag", "path", "reflect", "regexp",
        "runtime", "testing", "unicode", "bufio", "archive", "compress",
        "database", "debug", "embed", "go", "hash", "html", "image",
        "index", "mime", "plugin", "text",
    })

    def _parse_go_imports(self, code: str) -> list[tuple[str, str]]:
        """Parse Go imports and return list of (alias, import_path).

        The alias is either an explicit alias or the last segment of the path.
        """
        imports: list[tuple[str, str]] = []

        # Block imports
        for block_m in self._GO_BLOCK_IMPORT_RE.finditer(code):
            block_body = block_m.group(1)
            for line_m in self._GO_IMPORT_LINE_RE.finditer(block_body):
                alias = line_m.group(1)
                import_path = line_m.group(2)
                if alias is None:
                    alias = import_path.rstrip("/").rsplit("/", 1)[-1]
                imports.append((alias, import_path))

        # Single imports (only if not already found in a block)
        block_spans = [(m.start(), m.end()) for m in self._GO_BLOCK_IMPORT_RE.finditer(code)]
        for m in self._GO_SINGLE_IMPORT_RE.finditer(code):
            # Skip if this match is inside a block import
            in_block = any(s <= m.start() < e for s, e in block_spans)
            if in_block:
                continue
            import_path = m.group(1)
            alias = import_path.rstrip("/").rsplit("/", 1)[-1]
            imports.append((alias, import_path))

        return imports

    def _is_go_stdlib(self, import_path: str) -> bool:
        """Check if a Go import path is part of the standard library."""
        top = import_path.split("/")[0]
        return top in self._GO_STDLIB

    def _find_go_package_files(
        self, import_path: str, normalized_files: dict[str, str]
    ) -> list[str]:
        """Find files in the store matching a Go import path.

        Go import "myapp/users" maps to files with paths like users/*.go.
        We use suffix matching on the package segment.
        """
        # The last segment is the package name
        pkg_segment = import_path.rstrip("/").rsplit("/", 1)[-1]
        matching: list[str] = []
        for norm_path, orig_path in normalized_files.items():
            # Match files in a directory named after the package
            # e.g., users/queries.go, users/types.go
            if f"{pkg_segment}/" in norm_path and norm_path.endswith(".go"):
                matching.append(orig_path)
        return matching

    def _validate_go(
        self, code: str, file_path: str
    ) -> Result[list[AstValidationError], GroundTruthError]:
        """Validate Go imports and qualified function calls using regex."""
        all_files_result = self._store.get_all_files()
        if isinstance(all_files_result, Err):
            return Ok([])

        all_files = all_files_result.value
        normalized_files = {_normalize_file_path(f): f for f in all_files}

        errors: list[AstValidationError] = []

        # Parse imports to build alias → import_path mapping
        go_imports = self._parse_go_imports(code)
        alias_to_path: dict[str, str] = {}
        for alias, import_path in go_imports:
            if not self._is_go_stdlib(import_path):
                alias_to_path[alias] = import_path

        # Find qualified references: pkg.FuncName( and pkg.Symbol
        lines = code.split("\n")
        seen_symbols: set[tuple[int, str]] = set()  # (line, symbol) to avoid dupes
        for line_num, line_text in enumerate(lines, start=1):
            stripped = line_text.strip()
            # Skip comments and import lines
            if stripped.startswith("//") or stripped.startswith("import"):
                continue

            # Check both calls (pkg.Func() ) and value references (pkg.Symbol)
            all_matches: list[tuple[str, str]] = []
            for m in self._GO_QUALIFIED_CALL_RE.finditer(line_text):
                all_matches.append((m.group(1), m.group(2)))
            for m in self._GO_QUALIFIED_ACCESS_RE.finditer(line_text):
                all_matches.append((m.group(1), m.group(2)))

            for pkg_alias, symbol_name in all_matches:
                # Only check references using our tracked import aliases
                if pkg_alias not in alias_to_path:
                    continue

                # Deduplicate per line+symbol
                key = (line_num, symbol_name)
                if key in seen_symbols:
                    continue
                seen_symbols.add(key)

                import_path = alias_to_path[pkg_alias]

                # Find files for this package
                pkg_files = self._find_go_package_files(import_path, normalized_files)

                # Check if the symbol exists in any of the package files
                found_in_pkg = False
                for pkg_file in pkg_files:
                    symbols_result = self._store.get_symbols_in_file(pkg_file)
                    if isinstance(symbols_result, Ok):
                        for sym in symbols_result.value:
                            if sym.name == symbol_name:
                                found_in_pkg = True
                                break
                    if found_in_pkg:
                        break

                if found_in_pkg:
                    continue

                # Not found in expected package — check if it exists elsewhere
                find_result = self._store.find_symbol_by_name(symbol_name)
                if isinstance(find_result, Ok) and find_result.value:
                    actual_file = find_result.value[0].file_path
                    errors.append(AstValidationError(
                        error_type="wrong_module_path",
                        message=(
                            f"'{symbol_name}' not found in package '{pkg_alias}' "
                            f"(exists in {actual_file})"
                        ),
                        symbol_name=symbol_name,
                        line=line_num,
                        module_path=import_path,
                    ))
                else:
                    errors.append(AstValidationError(
                        error_type="invented_symbol",
                        message=f"'{symbol_name}' not found in package '{pkg_alias}'",
                        symbol_name=symbol_name,
                        line=line_num,
                        module_path=import_path,
                    ))

        # Signature validation for Go
        sig_errors = self._validate_go_signatures(code, lines, alias_to_path, normalized_files)
        errors.extend(sig_errors)

        return Ok(errors)

    def _validate_go_signatures(
        self,
        code: str,
        lines: list[str],
        alias_to_path: dict[str, str],
        normalized_files: dict[str, str],
    ) -> list[AstValidationError]:
        """Check qualified function calls in Go code against stored signatures."""
        errors: list[AstValidationError] = []
        # Match pkg.Func(args) capturing the args
        call_with_args_re = re.compile(r'(\w+)\.(\w+)\s*\(([^)]*)\)')

        for line_num, line_text in enumerate(lines, start=1):
            stripped = line_text.strip()
            if stripped.startswith("//") or stripped.startswith("import"):
                continue

            for m in call_with_args_re.finditer(line_text):
                pkg_alias = m.group(1)
                func_name = m.group(2)
                args_str = m.group(3)

                if pkg_alias not in alias_to_path:
                    continue

                find_result = self._store.find_symbol_by_name(func_name)
                if isinstance(find_result, Err) or not find_result.value:
                    continue

                sym = find_result.value[0]
                if not sym.signature:
                    continue
                if sym.kind not in ("function", "method"):
                    continue

                expected = _parse_signature_param_count(sym.signature)
                if expected is None:
                    continue

                actual = _count_args(args_str)
                if actual != expected:
                    errors.append(AstValidationError(
                        error_type="wrong_arg_count",
                        message=(
                            f"'{func_name}' expects {expected} arg(s) "
                            f"but called with {actual}. Signature: {sym.signature}"
                        ),
                        symbol_name=func_name,
                        line=line_num,
                    ))
        return errors

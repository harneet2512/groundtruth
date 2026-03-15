"""AST-based Python import validation — bypasses LSP entirely."""

from __future__ import annotations

import ast
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
    """Find a matching file in the index, trying exact then suffix match."""
    norm = _normalize_file_path(candidate)
    # Exact match
    if norm in normalized_files:
        return normalized_files[norm]
    # Suffix match: handles src/ or other prefixes in store paths
    suffix = "/" + norm
    for stored_norm, orig in normalized_files.items():
        if stored_norm == norm or stored_norm.endswith(suffix):
            return orig
    return None


class AstValidator:
    """Validates Python imports using ast.parse() + the SymbolStore."""

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def validate(
        self, code: str, file_path: str, language: str
    ) -> Result[list[AstValidationError], GroundTruthError]:
        """Validate Python imports in the given code using AST parsing."""
        if language != "python":
            return Ok([])

        try:
            tree = ast.parse(code)
        except SyntaxError:
            # If we can't parse, skip AST validation — let LSP handle it
            return Ok([])

        # Pre-fetch index data
        all_files_result = self._store.get_all_files()
        if isinstance(all_files_result, Err):
            return Ok([])  # Can't validate without file list

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
                    continue  # relative import with no module
                for alias in node.names:
                    err = self._check_from_import(
                        node.module, alias.name, node.lineno, normalized_files
                    )
                    if err is not None:
                        errors.append(err)

        return Ok(errors)

    def _check_import(
        self,
        module: str,
        line: int,
        normalized_files: dict[str, str],
    ) -> AstValidationError | None:
        """Check `import X` statement."""
        top = _top_level_module(module)

        # Skip stdlib
        if top in _STDLIB_MODULES:
            return None

        # Check packages table
        pkg_result = self._store.get_package(top)
        if isinstance(pkg_result, Ok) and pkg_result.value is not None:
            return None

        # Check if it maps to a local file
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

        # Skip stdlib
        if top in _STDLIB_MODULES:
            return None

        # Try to find the module as a local file
        matched_path: str | None = None
        for candidate in _module_to_paths(module):
            match = _find_matching_file(candidate, normalized_files)
            if match is not None:
                matched_path = match
                break

        if matched_path is None:
            # Not a local module — check packages
            pkg_result = self._store.get_package(top)
            if isinstance(pkg_result, Ok) and pkg_result.value is not None:
                return None  # Known external package, can't validate symbols

            return AstValidationError(
                error_type="missing_package",
                message=f"'{module}' is not installed and not found in the codebase",
                symbol_name=name,
                line=line,
                module_path=module,
            )

        # Module found — check if symbol Y exists in it
        symbols_result = self._store.get_symbols_in_file(matched_path)
        if isinstance(symbols_result, Err):
            return None  # Can't check, skip

        symbol_names = {s.name for s in symbols_result.value}
        if name in symbol_names:
            return None  # Symbol exists

        # Symbol not in this module — does it exist elsewhere?
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

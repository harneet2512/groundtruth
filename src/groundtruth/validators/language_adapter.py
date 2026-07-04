"""Language adapters for validator — extract language-specific logic.

Each adapter knows how to parse imports and calls for a specific language,
resolve effective arity, and detect dynamic exports.
"""

from __future__ import annotations

import ast
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedImport:
    """A parsed import statement."""

    module: str
    name: str
    line: int
    is_from: bool  # True for 'from M import X', False for 'import M'
    is_relative: bool = False


@dataclass(frozen=True)
class ParsedCall:
    """A parsed function call."""

    function_name: str
    arg_count: int
    line: int
    is_method_call: bool = False
    receiver: str | None = None


class LanguageAdapter(ABC):
    """Abstract base class for language-specific parsing logic."""

    @abstractmethod
    def parse_imports(self, code: str) -> list[ParsedImport]:
        """Parse all import statements from source code."""

    @abstractmethod
    def parse_calls(self, code: str) -> list[ParsedCall]:
        """Parse all function/method calls from source code."""

    @abstractmethod
    def resolve_effective_arity(self, signature: str, is_method: bool) -> tuple[int, int | float]:
        """Compute (min_required, max_allowed) argument count from a signature.

        Returns (min, inf) for variadic functions.
        """

    @abstractmethod
    def get_receiver_params(self) -> set[str]:
        """Return receiver parameter names (e.g., {'self', 'cls'} for Python)."""

    @abstractmethod
    def get_builtins(self) -> frozenset[str]:
        """Return stdlib/builtin module names."""

    @abstractmethod
    def get_reexport_filenames(self) -> list[str]:
        """Return filenames that typically re-export (e.g., ['__init__.py'])."""

    @abstractmethod
    def is_variadic(self, param: str) -> bool:
        """Check if a parameter is variadic (e.g., *args, **kwargs)."""

    @abstractmethod
    def has_dynamic_exports(self, code: str) -> bool:
        """Check if source code has dynamic export patterns."""


# ---------------------------------------------------------------------------
# Python adapter
# ---------------------------------------------------------------------------

_PYTHON_STDLIB_MODULES: frozenset[str] = frozenset(
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
        "_thread",
        "__future__",
    }
)


class PythonAdapter(LanguageAdapter):
    """Full Python adapter using stdlib ast module."""

    def parse_imports(self, code: str) -> list[ParsedImport]:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        imports: list[ParsedImport] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(
                        ParsedImport(
                            module=alias.name,
                            name=alias.name,
                            line=node.lineno,
                            is_from=False,
                            is_relative=False,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                is_relative = (node.level or 0) > 0
                for alias in node.names:
                    imports.append(
                        ParsedImport(
                            module=node.module,
                            name=alias.name,
                            line=node.lineno,
                            is_from=True,
                            is_relative=is_relative,
                        )
                    )
        return imports

    def parse_calls(self, code: str) -> list[ParsedCall]:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        calls: list[ParsedCall] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name: str | None = None
            is_method = False
            receiver: str | None = None

            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
                is_method = True
                if isinstance(node.func.value, ast.Name):
                    receiver = node.func.value.id

            if func_name is not None:
                arg_count = len(node.args) + len(node.keywords)
                calls.append(
                    ParsedCall(
                        function_name=func_name,
                        arg_count=arg_count,
                        line=node.lineno,
                        is_method_call=is_method,
                        receiver=receiver,
                    )
                )
        return calls

    def resolve_effective_arity(self, signature: str, is_method: bool) -> tuple[int, int | float]:
        """Compute effective arity from a Python signature string.

        Subtracts self/cls for methods. Detects *args/**kwargs → (min, inf).
        Counts defaults to compute min_required.
        """
        m = re.match(r"\(([^)]*)\)", signature)
        if not m:
            return (0, math.inf)

        params_str = m.group(1).strip()
        if not params_str:
            return (0, 0)

        params = _split_params(params_str)
        receiver_names = self.get_receiver_params()

        # Filter out receiver params
        if is_method and params and params[0].strip().split(":")[0].strip() in receiver_names:
            params = params[1:]

        if not params:
            return (0, 0)

        has_variadic = False
        min_required = 0
        max_allowed = 0

        for p in params:
            p = p.strip()
            if not p:
                continue
            if self.is_variadic(p):
                has_variadic = True
                continue
            max_allowed += 1
            # Has default? (contains '=')
            if "=" not in p:
                min_required += 1

        if has_variadic:
            return (min_required, math.inf)
        return (min_required, max_allowed)

    def get_receiver_params(self) -> set[str]:
        return {"self", "cls"}

    def get_builtins(self) -> frozenset[str]:
        return _PYTHON_STDLIB_MODULES

    def get_reexport_filenames(self) -> list[str]:
        return ["__init__.py"]

    def is_variadic(self, param: str) -> bool:
        p = param.strip()
        return p.startswith("*") or p.startswith("**")

    def has_dynamic_exports(self, code: str) -> bool:
        """Detect __all__, import *, __getattr__ in source."""
        if "__all__" in code:
            return True
        if re.search(r"from\s+\S+\s+import\s+\*", code):
            return True
        if re.search(r"def\s+__getattr__\s*\(", code):
            return True
        return False


# ---------------------------------------------------------------------------
# Stub adapters for unsupported languages
# ---------------------------------------------------------------------------


class TypeScriptAdapter(LanguageAdapter):
    """TypeScript/JavaScript adapter with regex-based parsing."""

    def parse_imports(self, code: str) -> list[ParsedImport]:
        imports: list[ParsedImport] = []
        for i, line in enumerate(code.splitlines(), 1):
            line = line.strip()
            # import { X, Y } from 'module'
            m = re.match(r"import\s+\{([^}]+)\}\s+from\s+['\"]([^'\"]+)['\"]", line)
            if m:
                names = [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",")]
                for name in names:
                    if name:
                        imports.append(
                            ParsedImport(module=m.group(2), name=name, line=i, is_from=True)
                        )
                continue
            # import X from 'module'
            m = re.match(r"import\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]", line)
            if m:
                imports.append(
                    ParsedImport(module=m.group(2), name=m.group(1), line=i, is_from=True)
                )
                continue
            # import * as X from 'module'
            m = re.match(r"import\s+\*\s+as\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"]", line)
            if m:
                imports.append(
                    ParsedImport(module=m.group(2), name=m.group(1), line=i, is_from=True)
                )
                continue
            # const X = require('module')
            m = re.match(r"(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(['\"]([^'\"]+)['\"]\)", line)
            if m:
                imports.append(
                    ParsedImport(module=m.group(2), name=m.group(1), line=i, is_from=False)
                )
        return imports

    def parse_calls(self, code: str) -> list[ParsedCall]:
        calls: list[ParsedCall] = []
        for i, line in enumerate(code.splitlines(), 1):
            for m in re.finditer(r"(\w+)\s*\(", line):
                name = m.group(1)
                if name in (
                    "if",
                    "for",
                    "while",
                    "switch",
                    "catch",
                    "function",
                    "class",
                    "import",
                    "return",
                ):
                    continue
                calls.append(ParsedCall(function_name=name, arg_count=0, line=i))
        return calls

    def resolve_effective_arity(self, signature: str, is_method: bool) -> tuple[int, int | float]:
        params = _extract_params_from_sig(signature)
        if not params:
            return (0, 0)
        if any("..." in p for p in params):
            return (len([p for p in params if "?" not in p and "..." not in p]), math.inf)
        optional = sum(1 for p in params if "?" in p or "=" in p)
        return (len(params) - optional, len(params))

    def get_receiver_params(self) -> set[str]:
        return {"this"}

    def get_builtins(self) -> frozenset[str]:
        return frozenset(
            {
                "console",
                "process",
                "Buffer",
                "setTimeout",
                "setInterval",
                "clearTimeout",
                "clearInterval",
                "Promise",
                "JSON",
                "Math",
                "Date",
                "RegExp",
                "Error",
                "Array",
                "Object",
                "String",
                "Number",
                "Boolean",
                "Symbol",
                "Map",
                "Set",
                "WeakMap",
                "WeakSet",
                "fs",
                "path",
                "http",
                "https",
                "url",
                "util",
                "os",
                "events",
                "stream",
                "crypto",
                "child_process",
                "assert",
                "querystring",
            }
        )

    def get_reexport_filenames(self) -> list[str]:
        return ["index.ts", "index.js", "index.tsx", "index.jsx"]

    def is_variadic(self, param: str) -> bool:
        return param.strip().startswith("...")

    def has_dynamic_exports(self, code: str) -> bool:
        return "module.exports" in code or "export default" in code


class GoAdapter(LanguageAdapter):
    """Go adapter with regex-based parsing."""

    def parse_imports(self, code: str) -> list[ParsedImport]:
        imports: list[ParsedImport] = []
        # Single import: import "fmt"
        for i, line in enumerate(code.splitlines(), 1):
            m = re.match(r'\s*import\s+"([^"]+)"', line)
            if m:
                mod = m.group(1)
                name = mod.rsplit("/", 1)[-1]
                imports.append(ParsedImport(module=mod, name=name, line=i, is_from=False))
                continue
            # Block import member: "fmt" or alias "fmt"
            m = re.match(r'\s*(?:(\w+)\s+)?"([^"]+)"', line)
            if m:
                mod = m.group(2)
                name = m.group(1) or mod.rsplit("/", 1)[-1]
                imports.append(ParsedImport(module=mod, name=name, line=i, is_from=False))
        return imports

    def parse_calls(self, code: str) -> list[ParsedCall]:
        calls: list[ParsedCall] = []
        for i, line in enumerate(code.splitlines(), 1):
            for m in re.finditer(r"(\w+)\s*\(", line):
                name = m.group(1)
                if name in (
                    "if",
                    "for",
                    "switch",
                    "select",
                    "func",
                    "go",
                    "defer",
                    "return",
                    "range",
                ):
                    continue
                calls.append(ParsedCall(function_name=name, arg_count=0, line=i))
        return calls

    def resolve_effective_arity(self, signature: str, is_method: bool) -> tuple[int, int | float]:
        params = _extract_params_from_sig(signature)
        if not params:
            return (0, 0)
        if any("..." in p for p in params):
            return (len(params) - 1, math.inf)
        return (len(params), len(params))

    def get_receiver_params(self) -> set[str]:
        return set()  # Go receivers are separate from params

    def get_builtins(self) -> frozenset[str]:
        return frozenset(
            {
                "fmt",
                "os",
                "io",
                "strings",
                "strconv",
                "errors",
                "log",
                "math",
                "sort",
                "sync",
                "time",
                "context",
                "net",
                "http",
                "encoding",
                "bytes",
                "bufio",
                "path",
                "regexp",
                "testing",
                "reflect",
                "runtime",
                "flag",
                "crypto",
                "hash",
            }
        )

    def get_reexport_filenames(self) -> list[str]:
        return []  # Go doesn't have re-export files

    def is_variadic(self, param: str) -> bool:
        return param.strip().startswith("...")

    def has_dynamic_exports(self, code: str) -> bool:
        return False  # Go exports are compile-time (uppercase)


class GenericAdapter(LanguageAdapter):
    """Generic adapter for any language — returns permissive defaults.

    Used as fallback for languages without a dedicated adapter.
    Validation still works via cross-index lookup and Levenshtein distance;
    this adapter just can't parse language-specific syntax.
    """

    def parse_imports(self, code: str) -> list[ParsedImport]:
        return []

    def parse_calls(self, code: str) -> list[ParsedCall]:
        return []

    def resolve_effective_arity(self, signature: str, is_method: bool) -> tuple[int, int | float]:
        # Permissive: try to extract params from signature string
        params = _extract_params_from_sig(signature)
        if params:
            return (0, len(params) + 2)  # generous upper bound
        return (0, math.inf)

    def get_receiver_params(self) -> set[str]:
        return {"self", "this", "cls"}  # union of common receivers

    def get_builtins(self) -> frozenset[str]:
        return frozenset()

    def get_reexport_filenames(self) -> list[str]:
        return []

    def is_variadic(self, param: str) -> bool:
        p = param.strip()
        return p.startswith("...") or p.startswith("*") or p.startswith("params ")

    def has_dynamic_exports(self, code: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, type[LanguageAdapter]] = {
    "python": PythonAdapter,
    "typescript": TypeScriptAdapter,
    "javascript": TypeScriptAdapter,
    "go": GoAdapter,
}


def get_adapter(language: str) -> LanguageAdapter:
    """Get the language adapter for a given language.

    Always returns an adapter — GenericAdapter for unknown languages.
    Logs a warning when falling back to GenericAdapter.
    """
    adapter_cls = _ADAPTERS.get(language)
    if adapter_cls is None:
        try:
            import structlog

            structlog.get_logger("validators.adapter").info(
                "generic_adapter_used",
                language=language,
                note="no dedicated adapter — import/call validation skipped",
            )
        except Exception:
            pass
        return GenericAdapter()
    return adapter_cls()


def _extract_params_from_sig(signature: str) -> list[str]:
    """Extract parameter list from a function signature string."""
    if not signature:
        return []
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return []
    params_str = m.group(1).strip()
    if not params_str:
        return []
    return _split_params(params_str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_params(params_str: str) -> list[str]:
    """Split a parameter string by commas, respecting nested brackets."""
    params: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params_str:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            params.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        params.append("".join(current))
    return params

"""Static and dynamic hallucination case generation for verification."""

from __future__ import annotations

from dataclasses import dataclass

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err, Ok


@dataclass(frozen=True)
class HallucinationCase:
    """A single hallucination test case."""

    id: str
    category: str  # "wrong_module_path" | "invented_symbol" | "mangled_name" | ...
    code: str  # Python snippet with the hallucination
    file_path: str  # Context file path (must end .py)
    description: str


def get_static_cases() -> list[HallucinationCase]:
    """Return 3 universal hallucination cases that don't depend on the index."""
    return [
        HallucinationCase(
            id="static-missing-package",
            category="missing_package",
            code="import flask_nonexistent_ext\nflask_nonexistent_ext.init()",
            file_path="src/app.py",
            description="Import of a package that does not exist",
        ),
        HallucinationCase(
            id="static-invented-symbol",
            category="invented_symbol",
            code="from os.path import nonexistent_func_xyz\nnonexistent_func_xyz('/tmp')",
            file_path="src/utils.py",
            description="Import of a function that does not exist in os.path",
        ),
        HallucinationCase(
            id="static-wrong-language-package",
            category="missing_package",
            code="import axios\nresponse = axios.get('https://example.com')",
            file_path="src/client.py",
            description="Import of a Node.js package in Python code",
        ),
    ]


def _mangle_name(name: str) -> str:
    """Swap two adjacent characters in the middle of a name."""
    if len(name) < 3:
        return name + "_x"
    # Pick a swap point in the middle
    mid = len(name) // 2
    chars = list(name)
    chars[mid], chars[mid + 1] = chars[mid + 1], chars[mid]
    result = "".join(chars)
    # Ensure it's actually different
    if result == name:
        chars[mid] = "_"
        result = "".join(chars)
    return result


def generate_dynamic_cases(store: SymbolStore) -> list[HallucinationCase]:
    """Generate up to 3 hallucination cases from the index. Returns [] if index is too small."""
    cases: list[HallucinationCase] = []

    # Get symbol names and files
    names_result = store.get_all_symbol_names()
    files_result = store.get_all_files()

    if isinstance(names_result, Err) or isinstance(files_result, Err):
        return cases

    all_names = names_result.value
    all_files = files_result.value

    if not all_names or not all_files:
        return cases

    # Case 1: Mangled name — pick a symbol with len > 5, swap chars
    long_names = [n for n in all_names if len(n) > 5]
    if long_names:
        original = long_names[0]
        mangled = _mangle_name(original)
        # Find the file for this symbol
        sym_result = store.find_symbol_by_name(original)
        sym_file = "src/module.py"
        if isinstance(sym_result, Ok) and sym_result.value:
            sym_file = sym_result.value[0].file_path
        cases.append(HallucinationCase(
            id="dynamic-mangled-name",
            category="mangled_name",
            code=f"from {_path_to_module(sym_file)} import {mangled}\n{mangled}()",
            file_path=sym_file,
            description=f"Mangled version of '{original}' — Levenshtein should suggest the real name",
        ))

    # Case 2: Wrong path — pick an exported symbol, use a different module path
    exported_result = store.get_hotspots(limit=5)
    if isinstance(exported_result, Ok) and exported_result.value and len(all_files) >= 2:
        sym = exported_result.value[0]
        # Find a different file to import from
        wrong_files = [f for f in all_files if f != sym.file_path]
        if wrong_files:
            wrong_file = wrong_files[0]
            cases.append(HallucinationCase(
                id="dynamic-wrong-path",
                category="wrong_module_path",
                code=f"from {_path_to_module(wrong_file)} import {sym.name}\n{sym.name}()",
                file_path=wrong_file,
                description=f"'{sym.name}' imported from wrong module (exists in {sym.file_path})",
            ))

    # Case 3: Invented export — pick a real file, import a fake function from it
    if all_files:
        target_file = all_files[0]
        fake_name = "totally_fake_function_xyz"
        cases.append(HallucinationCase(
            id="dynamic-invented-export",
            category="invented_symbol",
            code=f"from {_path_to_module(target_file)} import {fake_name}\n{fake_name}()",
            file_path=target_file,
            description=f"Invented function imported from real file '{target_file}'",
        ))

    return cases


def _path_to_module(file_path: str) -> str:
    """Convert a file path like 'src/foo/bar.py' to 'src.foo.bar'.

    Handles absolute paths by stripping drive letters and finding the first
    Python-package-style directory (containing only valid identifier segments).
    """
    path = file_path.replace("\\", "/")
    if path.endswith(".py"):
        path = path[:-3]
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]

    parts = path.split("/")

    # Strip drive letter and absolute prefix to find a valid Python module path.
    # Walk forward until we find a segment that is a valid Python identifier.
    start = 0
    for i, part in enumerate(parts):
        if part.isidentifier():
            start = i
            break

    module_parts = parts[start:]
    return ".".join(module_parts)

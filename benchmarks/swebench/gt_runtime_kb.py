#!/usr/bin/env python3
"""Runtime Introspection KB Builder.

Imports actual Python classes from the repo and uses dir(), inspect.getmembers(),
and inspect.signature() to build a ground-truth knowledge base of class members.

This catches metaclass-injected methods, mixin methods, descriptors, and dynamic
dispatch that AST parsing fundamentally misses.

Output: JSON to stdout with the runtime KB.
Designed to run inside a Docker container where the repo is installed and importable.
Stdlib only. NEVER crashes — prints empty KB on any error.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import sys
import time
from typing import Any


REPO_ROOT = "/testbed"
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".tox", ".eggs",
    "venv", "env", "build", "dist", ".mypy_cache", ".pytest_cache",
    "test", "tests", ".egg-info",
}
MAX_IMPORT_TIME = 20  # seconds total for all imports
MAX_PER_MODULE = 2.0  # seconds per module import attempt


def _find_python_modules(repo_root: str) -> list[tuple[str, str]]:
    """Find all Python modules in the repo.

    Returns list of (module_dotpath, file_path) tuples.
    Skips test directories and non-package directories.
    """
    modules: list[tuple[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Skip excluded directories
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS
            and not d.startswith(".")
            and not d.endswith(".egg-info")
        ]

        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if fn.startswith("test_") or fn == "conftest.py":
                continue

            fpath = os.path.join(dirpath, fn)
            rel = os.path.relpath(fpath, repo_root)

            # Skip test paths
            parts = rel.replace(os.sep, "/").split("/")
            if any(p in ("test", "tests", "testing") for p in parts):
                continue

            # Convert to module path
            mod_path = rel.replace(os.sep, ".").replace("/", ".")
            if mod_path.endswith(".py"):
                mod_path = mod_path[:-3]
            if mod_path.endswith(".__init__"):
                mod_path = mod_path[:-9]

            modules.append((mod_path, fpath))

    return modules


def _extract_class_names_from_file(fpath: str) -> list[str]:
    """Use AST to find class names defined in a file (fast, no import needed)."""
    try:
        with open(fpath, "r", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=fpath)
    except (SyntaxError, OSError, UnicodeDecodeError, ValueError):
        return []

    classes = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
    return classes


def _introspect_class(cls: type) -> dict[str, Any]:
    """Introspect a class using runtime reflection.

    Returns dict with methods, attrs, params, bases.
    """
    info: dict[str, Any] = {
        "methods": set(),
        "attrs": set(),
        "params": {},
        "bases": [],
        "source": "runtime",
    }

    # Get base class names
    for base in cls.__mro__[1:]:
        if base is object:
            continue
        info["bases"].append(base.__name__)

    # Get all members via dir() — this is the ground truth
    all_members = set()
    try:
        all_members = set(dir(cls))
    except Exception:
        pass

    # Classify members using inspect
    try:
        members = inspect.getmembers(cls)
    except Exception:
        # Fallback: just use dir()
        info["methods"] = {m for m in all_members if not m.startswith("__")}
        return info

    for name, value in members:
        if name.startswith("__") and name.endswith("__"):
            # Keep some useful dunders
            if name in ("__init__", "__new__", "__call__"):
                info["methods"].add(name)
            continue

        try:
            if callable(value) or isinstance(value, (classmethod, staticmethod, property)):
                info["methods"].add(name)
                # Try to get signature for param validation
                try:
                    if not isinstance(value, property):
                        sig = inspect.signature(value)
                        params = [
                            p.name for p in sig.parameters.values()
                            if p.name != "self"
                        ]
                        if params:
                            info["params"][name] = params
                except (ValueError, TypeError):
                    pass
            else:
                info["attrs"].add(name)
        except Exception:
            # Some descriptors raise on access
            info["attrs"].add(name)

    # Add anything from dir() that we missed (metaclass-injected members)
    classified = info["methods"] | info["attrs"]
    for member in all_members:
        if member not in classified and not (member.startswith("__") and member.endswith("__")):
            info["methods"].add(member)  # assume callable if we can't tell

    return info


def build_runtime_kb(repo_root: str) -> dict[str, Any]:
    """Build the runtime KB by importing classes and introspecting them.

    Returns a JSON-serializable dict.
    """
    kb: dict[str, Any] = {
        "classes": {},
        "import_failures": [],
        "import_successes": 0,
        "total_classes": 0,
        "total_methods": 0,
        "build_time": 0,
    }

    start = time.time()

    # Ensure repo is on sys.path
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    # Find all modules with classes
    modules = _find_python_modules(repo_root)

    # Pre-scan with AST to find which files have classes (fast)
    modules_with_classes: list[tuple[str, str, list[str]]] = []
    for mod_path, fpath in modules:
        class_names = _extract_class_names_from_file(fpath)
        if class_names:
            modules_with_classes.append((mod_path, fpath, class_names))

    # Import each module and introspect its classes
    for mod_path, fpath, class_names in modules_with_classes:
        if time.time() - start > MAX_IMPORT_TIME:
            kb["import_failures"].append({"module": "TIMEOUT", "error": "MAX_IMPORT_TIME exceeded"})
            break

        try:
            # Set a per-module timeout via alarm (Unix only)
            mod = importlib.import_module(mod_path)
            kb["import_successes"] += 1
        except Exception as e:
            kb["import_failures"].append({
                "module": mod_path,
                "error": type(e).__name__ + ": " + str(e)[:100],
            })
            continue

        for class_name in class_names:
            cls = getattr(mod, class_name, None)
            if cls is None or not isinstance(cls, type):
                continue

            try:
                class_info = _introspect_class(cls)
                # Convert sets to lists for JSON serialization
                serializable = {
                    "methods": sorted(class_info["methods"]),
                    "attrs": sorted(class_info["attrs"]),
                    "params": class_info["params"],
                    "bases": class_info["bases"],
                    "source": "runtime",
                    "file": os.path.relpath(fpath, repo_root),
                }

                # Merge with existing (same class name from different files)
                if class_name in kb["classes"]:
                    existing = kb["classes"][class_name]
                    existing_methods = set(existing["methods"])
                    existing_attrs = set(existing["attrs"])
                    existing_methods.update(class_info["methods"])
                    existing_attrs.update(class_info["attrs"])
                    existing["methods"] = sorted(existing_methods)
                    existing["attrs"] = sorted(existing_attrs)
                    existing["params"].update(class_info["params"])
                else:
                    kb["classes"][class_name] = serializable

                kb["total_classes"] += 1
                kb["total_methods"] += len(class_info["methods"])
            except Exception:
                continue

    kb["build_time"] = round(time.time() - start, 2)
    return kb


def main() -> None:
    """Build and output the runtime KB as JSON."""
    try:
        # Try to configure Django settings if present
        _setup_django()
    except Exception:
        pass

    try:
        kb = build_runtime_kb(REPO_ROOT)
        print(json.dumps(kb, default=str))
    except Exception as e:
        print(json.dumps({
            "classes": {},
            "import_failures": [{"module": "FATAL", "error": str(e)[:200]}],
            "import_successes": 0,
            "total_classes": 0,
            "total_methods": 0,
            "build_time": 0,
        }))


def _setup_django() -> None:
    """Attempt to configure Django settings for import compatibility."""
    # Check if this is a Django project
    manage_py = os.path.join(REPO_ROOT, "manage.py")
    if not os.path.exists(manage_py):
        return

    # Try to find settings module from manage.py
    try:
        with open(manage_py, "r") as f:
            content = f.read()
        import re
        match = re.search(r"DJANGO_SETTINGS_MODULE.*?['\"]([^'\"]+)['\"]", content)
        if match:
            settings_module = match.group(1)
        else:
            # Common Django patterns
            for candidate in ["tests.settings", "test_settings", "settings"]:
                settings_module = candidate
                break
    except Exception:
        settings_module = "settings"

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)
    try:
        import django
        django.setup()
    except Exception:
        # Try alternative settings modules
        for alt in ["tests.settings", "test_settings", "tests.test_settings"]:
            try:
                os.environ["DJANGO_SETTINGS_MODULE"] = alt
                import importlib
                if "django" in sys.modules:
                    # Reset Django's setup state
                    import django
                    django.setup()
                break
            except Exception:
                continue


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # NEVER crash
        print(json.dumps({
            "classes": {},
            "import_failures": [{"module": "CRASH", "error": "fatal"}],
            "import_successes": 0,
            "total_classes": 0,
            "total_methods": 0,
            "build_time": 0,
        }))
        sys.exit(0)

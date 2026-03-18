#!/usr/bin/env python3
"""
GroundTruth MCP — On-Demand Codebase Intelligence (v4)

Usage inside SWE-bench container:
  python3 /tmp/gt_tool.py references UniqueConstraint
  python3 /tmp/gt_tool.py outline django/db/models/constraints.py
  python3 /tmp/gt_tool.py coupled UniqueConstraint
  python3 /tmp/gt_tool.py impact UniqueConstraint

Runs on stdlib ast. No dependencies. Designed for any Python codebase.
Indexes the repo on first call, caches the index for subsequent calls.
"""
import ast
import os
import sys
import json
import glob
import time
import tempfile
from collections import defaultdict

REPO_ROOT = '/testbed'
INDEX_CACHE = os.path.join(tempfile.gettempdir(), 'gt_index.json')
MAX_FILE_SIZE = 500_000
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.tox', '.eggs',
             'venv', 'env', 'build', 'dist', '.mypy_cache', '.pytest_cache'}
MAX_INDEX_TIME = 12  # seconds

# ───────────────────────────────
# INDEXER — runs once, caches
# ───────────────────────────────

def build_index(repo_root):
    """Parse all Python source files into a structured index."""
    start = time.time()
    index = {
        'classes': {},       # class_name -> [{file, line, methods, bases, attrs}]
        'functions': {},     # func_name -> [{file, line, sig}]
        'imports': {},       # file -> [imported_names]
        'references': {},    # symbol_name -> [{file, line, context}]
        'files_parsed': 0,
        'build_time': 0,
    }

    py_files = glob.glob(os.path.join(repo_root, '**', '*.py'), recursive=True)

    for filepath in py_files:
        rel = os.path.relpath(filepath, repo_root)

        # Skip excluded directories
        parts = rel.split(os.sep)
        if any(p in SKIP_DIRS for p in parts):
            continue

        # Skip oversized files
        try:
            if os.path.getsize(filepath) > MAX_FILE_SIZE:
                continue
        except OSError:
            continue

        # Skip test files for CLASS indexing (but still scan for references)
        is_test = _is_test_file(rel)

        try:
            with open(filepath, 'r', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except (SyntaxError, ValueError, RecursionError):
            continue

        index['files_parsed'] += 1

        # Extract imports (all files — needed for references)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    name = alias.name
                    index['imports'].setdefault(rel, []).append(name)
                    # Track as a reference
                    index['references'].setdefault(name, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'import'
                    })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split('.')[-1]
                    index['imports'].setdefault(rel, []).append(name)

        # Extract classes and functions (source files only)
        if not is_test:
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    cls_info = _parse_class(node, rel)
                    if cls_info:
                        index['classes'].setdefault(node.name, []).append(cls_info)

                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    index['functions'].setdefault(node.name, []).append({
                        'file': rel, 'line': node.lineno,
                        'sig': _get_signature(node),
                    })

        # Scan for name references (all files — needed for `references` command)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and len(node.id) > 2:
                # Only track CamelCase names (likely class references)
                if node.id[0].isupper() and not node.id.isupper():
                    index['references'].setdefault(node.id, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'usage'
                    })

        # Time budget
        if time.time() - start > MAX_INDEX_TIME:
            break

    index['build_time'] = round(time.time() - start, 2)

    # Cache
    with open(INDEX_CACHE, 'w') as f:
        json.dump(index, f)

    return index


def load_or_build_index(repo_root):
    """Load cached index or build fresh."""
    if os.path.exists(INDEX_CACHE):
        try:
            with open(INDEX_CACHE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return build_index(repo_root)


def _is_test_file(filepath):
    fp = "/" + filepath.lower().replace("\\", "/")
    dir_patterns = ['/tests/', '/test/', '/__tests__/', '/testing/',
                    '/docs/', '/doc/', '/examples/', '/example/',
                    '/fixtures/', '/migrations/']
    if any(pat in fp for pat in dir_patterns):
        return True
    basename = os.path.basename(fp)
    parent = os.path.basename(os.path.dirname(fp))
    if basename.startswith("test_") or basename.endswith("_test.py"):
        if parent in ('tests', 'test', 'testing', '__tests__', 'unit', 'integration'):
            return True
    return False


def _parse_class(node, filepath):
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute) and isinstance(base.attr, str):
            bases.append(base.attr)

    methods = {}
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            attrs = set()
            calls = []
            for child in ast.walk(item):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == 'self'):
                    attrs.add(child.attr)
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == 'self'):
                    calls.append(child.func.attr)

            methods[item.name] = {
                'line': item.lineno,
                'sig': _get_signature(item),
                'attrs': sorted(attrs),
                'calls': calls,
            }

    if not methods:
        return None

    return {
        'file': filepath,
        'line': node.lineno,
        'bases': bases,
        'methods': methods,
    }


def _get_signature(func_node):
    args = func_node.args
    parts = []
    num_defaults = len(args.defaults)
    num_args = len(args.args)
    for i, arg in enumerate(args.args):
        if arg.arg in ('self', 'cls'):
            continue
        default_idx = i - (num_args - num_defaults)
        if 0 <= default_idx < len(args.defaults):
            d = _default_str(args.defaults[default_idx])
            parts.append(f"{arg.arg}={d}")
        else:
            parts.append(arg.arg)
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")
    for i, arg in enumerate(args.kwonlyargs):
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            d = _default_str(args.kw_defaults[i])
            parts.append(f"{arg.arg}={d}")
        else:
            parts.append(arg.arg)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"({', '.join(parts)})"


def _default_str(node):
    if isinstance(node, ast.Constant):
        r = repr(node.value)
        return r if len(r) < 15 else r[:12] + "..."
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, (ast.List, ast.Tuple)):
        return "[]" if isinstance(node, ast.List) else "()"
    if isinstance(node, ast.Dict):
        return "{}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return f"{node.func.id}()"
    return "..."


# ───────────────────────────────
# COMMANDS
# ───────────────────────────────

def cmd_references(index, symbol):
    """Find all files that reference this symbol."""
    refs = index.get('references', {}).get(symbol, [])
    if not refs:
        print(f"No references found for '{symbol}'")
        return

    # Deduplicate and group by file
    by_file = defaultdict(list)
    seen = set()
    for ref in refs:
        key = (ref['file'], ref['line'])
        if key not in seen:
            seen.add(key)
            by_file[ref['file']].append(ref)

    # Sort: definitions first, then by file path
    print(f"References to '{symbol}' ({len(seen)} locations):\n")
    for filepath in sorted(by_file.keys()):
        file_refs = sorted(by_file[filepath], key=lambda r: r['line'])
        types = set(r['type'] for r in file_refs)
        lines = [str(r['line']) for r in file_refs[:5]]
        type_str = ','.join(sorted(types))
        more = f" +{len(file_refs) - 5} more" if len(file_refs) > 5 else ""
        print(f"  {filepath}:{','.join(lines)}{more} ({type_str})")


def _path_match(query, indexed):
    """Check if query path matches indexed path (cross-platform separator handling)."""
    q = query.replace("\\", "/")
    p = indexed.replace("\\", "/")
    return p == q or q in p


def cmd_outline(index, filepath):
    """Show structured outline of a file."""
    # Find classes in this file
    found = False
    for class_name, locations in index.get('classes', {}).items():
        for loc in locations:
            if _path_match(filepath, loc['file']):
                if not found:
                    print(f"Outline of {loc['file']}:\n")
                    found = True

                bases_str = f" ({', '.join(loc['bases'])})" if loc['bases'] else ""
                print(f"  class {class_name}{bases_str} — line {loc['line']}")
                for mname, minfo in sorted(loc['methods'].items(), key=lambda x: x[1]['line']):
                    print(f"    {mname}{minfo['sig']} — line {minfo['line']}")

    # Find module-level functions
    for func_name, locations in index.get('functions', {}).items():
        for loc in locations:
            if _path_match(filepath, loc['file']):
                if not found:
                    print(f"Outline of {loc['file']}:\n")
                    found = True
                print(f"  def {func_name}{loc['sig']} — line {loc['line']}")

    if not found:
        print(f"No symbols found in '{filepath}'")
        print("Hint: use a partial path (e.g., 'constraints.py' instead of full path)")


def cmd_coupled(index, class_name):
    """Show methods that share self.* state within a class."""
    locations = index.get('classes', {}).get(class_name, [])
    if not locations:
        print(f"Class '{class_name}' not found in source files")
        return

    for loc in locations:
        bases_str = f" (extends {', '.join(loc['bases'])})" if loc['bases'] else ""
        print(f"{class_name}{bases_str} — {loc['file']}:{loc['line']}\n")

        # Build attribute coupling
        attr_to_methods = defaultdict(list)
        for mname, minfo in loc['methods'].items():
            for attr in minfo.get('attrs', []):
                attr_to_methods[attr].append(mname)

        # Only show attributes used in 2+ methods
        coupled = {attr: meths for attr, meths in attr_to_methods.items()
                   if len(meths) >= 2}

        if coupled:
            print("  Shared state (methods coupled through self.* attributes):")
            for attr, meths in sorted(coupled.items(), key=lambda x: -len(x[1])):
                print(f"    self.{attr} → {', '.join(sorted(meths))}")
        else:
            print("  No shared state coupling detected")

        # Show call coupling
        call_pairs = []
        for mname, minfo in loc['methods'].items():
            for call in minfo.get('calls', []):
                if call in loc['methods']:
                    call_pairs.append((mname, call))
        if call_pairs:
            print("\n  Internal calls:")
            for caller, callee in call_pairs:
                print(f"    {caller}() → self.{callee}()")

        print()


def cmd_impact(index, symbol):
    """Composed answer: if you change this symbol, what else needs updating?"""
    print(f"Impact analysis for '{symbol}':\n")

    # 1. Find the class definition
    cls_locations = index.get('classes', {}).get(symbol, [])

    if cls_locations:
        for loc in cls_locations:
            print(f"  Definition: {loc['file']}:{loc['line']}")

            # 2. Coupled methods (from coupling analysis)
            attr_to_methods = defaultdict(list)
            for mname, minfo in loc['methods'].items():
                for attr in minfo.get('attrs', []):
                    attr_to_methods[attr].append(mname)

            coupled = {attr: meths for attr, meths in attr_to_methods.items()
                       if len(meths) >= 2}

            if coupled:
                # Show which methods need coordinated updates
                all_coupled_methods = set()
                for meths in coupled.values():
                    all_coupled_methods.update(meths)
                print(f"\n  Coupled methods (change one → check all):")
                for mname in sorted(all_coupled_methods):
                    minfo = loc['methods'].get(mname, {})
                    shared = [a for a, ms in coupled.items() if mname in ms]
                    shared_str = ', '.join(f'self.{a}' for a in sorted(shared)[:4])
                    if len(shared) > 4:
                        shared_str += f' +{len(shared) - 4}'
                    print(f"    {mname}{minfo.get('sig', '()')}:{minfo.get('line', '?')} — via {shared_str}")

            # 3. Base class interface
            if loc['bases']:
                print(f"\n  Inherits from: {', '.join(loc['bases'])}")
                for base in loc['bases']:
                    base_locs = index.get('classes', {}).get(base, [])
                    if base_locs:
                        base_methods = list(base_locs[0]['methods'].keys())[:8]
                        print(f"    {base} interface: {', '.join(base_methods)}")

    # 4. External references (who uses this symbol)
    refs = index.get('references', {}).get(symbol, [])
    if refs:
        by_file = defaultdict(list)
        for ref in refs:
            by_file[ref['file']].append(ref)

        # Filter to unique files, skip the definition file
        def_files = {loc['file'] for loc in cls_locations} if cls_locations else set()
        external = {f: r for f, r in by_file.items() if f not in def_files}

        if external:
            print(f"\n  Used in {len(external)} other files:")
            for filepath in sorted(external.keys())[:10]:
                file_refs = external[filepath]
                lines = sorted(set(r['line'] for r in file_refs))[:3]
                print(f"    {filepath}:{','.join(str(l) for l in lines)}")
            if len(external) > 10:
                print(f"    ... and {len(external) - 10} more files")

    # 5. If not a class, check as function
    if not cls_locations:
        func_locs = index.get('functions', {}).get(symbol, [])
        if func_locs:
            for loc in func_locs:
                print(f"  Function: {loc['file']}:{loc['line']}")
                print(f"  Signature: {symbol}{loc['sig']}")

        if refs:
            print(f"\n  Referenced in {len(set(r['file'] for r in refs))} files")

    if not cls_locations and not index.get('functions', {}).get(symbol):
        print(f"  '{symbol}' not found as a class or function in source files")
        print(f"  Try: python3 /tmp/gt_tool.py references {symbol}")


def cmd_help():
    print("""GroundTruth Codebase Intelligence

Usage:
  python3 /tmp/gt_tool.py references <symbol>  — Find all files that use this symbol
  python3 /tmp/gt_tool.py outline <file_path>   — Structured outline of a file
  python3 /tmp/gt_tool.py coupled <ClassName>   — Show methods sharing self.* state
  python3 /tmp/gt_tool.py impact <ClassName>    — Full change impact: coupling + references + inheritance

Examples:
  python3 /tmp/gt_tool.py references UniqueConstraint
  python3 /tmp/gt_tool.py outline django/db/models/constraints.py
  python3 /tmp/gt_tool.py coupled UniqueConstraint
  python3 /tmp/gt_tool.py impact UniqueConstraint

The index is built on first call and cached. Subsequent calls are instant.""")


# ───────────────────────────────
# MAIN
# ───────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        cmd_help()
        sys.exit(0)

    command = sys.argv[1].lower()

    # help also triggers index build (pre-warm cache)
    repo = os.environ.get('GT_REPO', REPO_ROOT)

    if command in ('help', '--help', '-h'):
        load_or_build_index(repo)
        cmd_help()
        sys.exit(0)

    index = load_or_build_index(repo)

    if command == 'references' and len(sys.argv) >= 3:
        cmd_references(index, sys.argv[2])
    elif command == 'outline' and len(sys.argv) >= 3:
        cmd_outline(index, sys.argv[2])
    elif command == 'coupled' and len(sys.argv) >= 3:
        cmd_coupled(index, sys.argv[2])
    elif command == 'impact' and len(sys.argv) >= 3:
        cmd_impact(index, sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        cmd_help()
        sys.exit(1)

#!/usr/bin/env python3
"""
GroundTruth MCP — On-Demand Codebase Intelligence (v4.1)

Usage inside SWE-bench container:
  Exploration (use BEFORE reading code):
    python3 /tmp/gt_tool.py references <Symbol>   — Find all usages (supports Class.method)
    python3 /tmp/gt_tool.py outline <file_path>    — Class/method map
    python3 /tmp/gt_tool.py impact <Symbol>        — Full change scope

  Validation (use AFTER editing code):
    python3 /tmp/gt_tool.py diagnose <file_path>   — Syntax errors + undefined names
    python3 /tmp/gt_tool.py check                  — Verify edit completeness

Runs on stdlib ast. No dependencies. Designed for any Python codebase.
Indexes the repo on first call, caches the index for subsequent calls.
"""
import ast
import os
import sys
import json
import glob
import time
import subprocess
import tempfile
from collections import defaultdict

REPO_ROOT = '/testbed'
INDEX_CACHE = os.path.join(tempfile.gettempdir(), 'gt_index.json')
MAX_FILE_SIZE = 500_000
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.tox', '.eggs',
             'venv', 'env', 'build', 'dist', '.mypy_cache', '.pytest_cache'}
MAX_INDEX_TIME = 20  # seconds (12 was too tight for large Django repos)

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

    # Prioritize source files over test files (source defs are more important
    # than test references when the time budget is tight)
    def _sort_key(fp):
        rel = os.path.relpath(fp, repo_root).lower()
        if _is_test_file(rel):
            return (1, rel)
        return (0, rel)
    py_files.sort(key=_sort_key)

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

                        # Index each method for method-level references
                        # Skip dunder methods (noisy, rarely useful for navigation)
                        _SKIP_METHODS = {'__str__', '__repr__', '__hash__', '__eq__',
                                         '__ne__', '__lt__', '__le__', '__gt__', '__ge__',
                                         '__len__', '__bool__', '__contains__',
                                         '__enter__', '__exit__', '__del__'}
                        for method_name, method_info in cls_info['methods'].items():
                            if method_name in _SKIP_METHODS:
                                continue
                            # Bare method name (e.g., "references resolve_redirects")
                            index['references'].setdefault(method_name, []).append({
                                'file': rel, 'line': method_info['line'],
                                'type': 'method_def', 'class': node.name
                            })
                            # Qualified name (e.g., "references Session.resolve_redirects")
                            qualified = f"{node.name}.{method_name}"
                            index['references'].setdefault(qualified, []).append({
                                'file': rel, 'line': method_info['line'],
                                'type': 'method_def', 'class': node.name
                            })

                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    index['functions'].setdefault(node.name, []).append({
                        'file': rel, 'line': node.lineno,
                        'sig': _get_signature(node),
                    })

        # Scan for name, attribute, and call references (all files)
        for node in ast.walk(tree):
            # CamelCase names (likely class references)
            if isinstance(node, ast.Name) and len(node.id) > 2:
                if node.id[0].isupper() and not node.id.isupper():
                    index['references'].setdefault(node.id, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'usage'
                    })
            # Attribute access: obj.method — track for method-level lookups
            elif isinstance(node, ast.Attribute) and isinstance(node.attr, str) and len(node.attr) > 2:
                attr = node.attr
                if not attr.startswith('_'):
                    index['references'].setdefault(attr, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'attr_access'
                    })
                    if isinstance(node.value, ast.Name) and node.value.id[0:1].isupper():
                        qualified = f"{node.value.id}.{attr}"
                        index['references'].setdefault(qualified, []).append({
                            'file': rel, 'line': node.lineno, 'type': 'attr_access'
                        })
            # Direct function calls: func_name(...) — track snake_case function calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and len(node.func.id) > 2:
                    fname = node.func.id
                    if not fname[0].isupper() and not fname.isupper() and '_' in fname:
                        # snake_case function call (not a class constructor, not a constant)
                        index['references'].setdefault(fname, []).append({
                            'file': rel, 'line': node.lineno, 'type': 'call'
                        })

        # Time budget
        if time.time() - start > MAX_INDEX_TIME:
            index['truncated'] = True
            index['total_py_files'] = len(py_files)
            break

    index['build_time'] = round(time.time() - start, 2)
    index['truncated'] = index.get('truncated', False)

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


def _warn_if_truncated(index):
    """Print a warning if the index was truncated due to time budget."""
    if index.get('truncated'):
        total = index.get('total_py_files', '?')
        parsed = index.get('files_parsed', '?')
        print(f"[NOTE: Index covers {parsed}/{total} files (time budget). Use grep for files not found.]\n")


# ───────────────────────────────
# COMMANDS
# ───────────────────────────────

def cmd_references(index, symbol):
    """Find all files that reference this symbol."""
    _warn_if_truncated(index)
    refs = index.get('references', {}).get(symbol, [])

    # Fallback: if "Foo.bar" not found directly, search for method "bar" in class "Foo"
    if not refs and '.' in symbol:
        cls_name, method_name = symbol.rsplit('.', 1)
        # Search bare method name with class filter
        for ref in index.get('references', {}).get(method_name, []):
            if ref.get('class') == cls_name:
                refs.append(ref)
        # Also search for attribute access patterns (obj.method)
        if not refs:
            for ref in index.get('references', {}).get(method_name, []):
                refs.append(ref)

    if not refs:
        # Suggest close matches with file locations
        sym_lower = symbol.lower()
        candidates = []
        for k, v in index.get('references', {}).items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                # Get first file location for context
                first_file = v[0]['file'] if v else '?'
                candidates.append((k, first_file, len(v)))
        # Also check class/function definitions
        for k, v in index.get('classes', {}).items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                first_file = v[0]['file'] if v else '?'
                candidates.append((k, first_file, -1))
        for k, v in index.get('functions', {}).items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                first_file = v[0]['file'] if v else '?'
                candidates.append((k, first_file, -1))

        # Deduplicate by name
        seen_names = set()
        unique_candidates = []
        for name, fpath, count in candidates:
            if name not in seen_names:
                seen_names.add(name)
                unique_candidates.append((name, fpath, count))

        if unique_candidates:
            print(f"'{symbol}' not found. Did you mean:")
            for name, fpath, count in unique_candidates[:5]:
                count_str = f" ({count} refs)" if count > 0 else ""
                print(f"  {name} in {fpath}{count_str}")
        else:
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

    # Compact output: definition files first, then usage files
    def_files = []
    use_files = []
    for filepath in sorted(by_file.keys()):
        file_refs = sorted(by_file[filepath], key=lambda r: r['line'])
        has_def = any(r['type'] in ('method_def', 'import') for r in file_refs)
        lines = ','.join(str(r['line']) for r in file_refs[:5])
        more = f"+{len(file_refs) - 5}" if len(file_refs) > 5 else ""
        entry = f"{filepath}:{lines}{more}"
        if has_def:
            def_files.append(entry)
        else:
            use_files.append(entry)

    print(f"{symbol} ({len(seen)} refs in {len(by_file)} files)")
    if def_files:
        print(f"Defined: {' | '.join(def_files)}")
    if use_files:
        for f in use_files[:15]:
            print(f"  {f}")
        if len(use_files) > 15:
            print(f"  ...+{len(use_files) - 15} more files")


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


def cmd_impact(index, symbol):
    """Compact impact analysis: definition, inheritance, and files that need updating."""
    cls_locations = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])
    refs = index.get('references', {}).get(symbol, [])

    if not cls_locations and not func_locs:
        # Try fuzzy match
        candidates = [k for k in list(index.get('classes', {}).keys()) + list(index.get('functions', {}).keys())
                      if symbol.lower() in k.lower() or k.lower() in symbol.lower()]
        if candidates:
            print(f"'{symbol}' not found. Similar: {', '.join(candidates[:5])}")
        else:
            print(f"'{symbol}' not found. Try: python3 /tmp/gt_tool.py references {symbol}")
        return

    # Definition + methods
    if cls_locations:
        for loc in cls_locations:
            bases_str = f" < {', '.join(loc['bases'])}" if loc['bases'] else ""
            methods = sorted(loc['methods'].items(), key=lambda x: x[1]['line'])
            method_list = ', '.join(f"{m}{info['sig']}" for m, info in methods[:10])
            more = f" +{len(methods) - 10}" if len(methods) > 10 else ""
            print(f"{symbol}{bases_str} @ {loc['file']}:{loc['line']}")
            print(f"Methods: {method_list}{more}")

            # Base class methods (for override awareness)
            for base in loc['bases']:
                base_locs = index.get('classes', {}).get(base, [])
                if base_locs:
                    base_methods = list(base_locs[0]['methods'].keys())[:8]
                    print(f"  {base} methods: {', '.join(base_methods)}")

    if func_locs:
        for loc in func_locs:
            print(f"{symbol}{loc['sig']} @ {loc['file']}:{loc['line']}")

    # External usage (the key actionable info)
    if refs:
        by_file = defaultdict(list)
        for ref in refs:
            by_file[ref['file']].append(ref)
        def_files = {loc['file'] for loc in cls_locations} if cls_locations else set()
        external = {f: r for f, r in by_file.items() if f not in def_files}

        if external:
            print(f"Used in {len(external)} files:")
            for fp in sorted(external.keys())[:10]:
                lines = sorted(set(r['line'] for r in external[fp]))[:3]
                print(f"  {fp}:{','.join(str(l) for l in lines)}")
            if len(external) > 10:
                print(f"  +{len(external) - 10} more")


def cmd_scope(index, symbol):
    """Answer: if I change this symbol, which files need editing?

    Returns a ranked list of files: definition file first, then files
    that import/use/subclass the symbol, sorted by coupling strength.
    Supports Class.method notation.
    """
    _warn_if_truncated(index)

    # Handle Class.method notation: scope the class, but show method context
    method_context = None
    if '.' in symbol:
        cls_name, method_name = symbol.rsplit('.', 1)
        # Check if the class exists
        if cls_name in index.get('classes', {}):
            method_context = method_name
            symbol = cls_name  # Scope the class

    files = {}  # file -> (score, reason, lines)

    # 1. Definition file (highest priority)
    cls_locs = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])

    for loc in cls_locs:
        f = loc['file']
        files[f] = (100, 'defines class', [loc['line']])
    for loc in func_locs:
        f = loc['file']
        files[f] = (100, 'defines function', [loc['line']])

    # 2. Files that reference the symbol
    refs = index.get('references', {}).get(symbol, [])
    for ref in refs:
        f = ref['file']
        if f not in files:
            rtype = ref.get('type', 'usage')
            score = 80 if rtype == 'import' else 60 if rtype == 'attr_access' else 40
            files[f] = (score, rtype, [ref['line']])
        else:
            old_score, old_reason, old_lines = files[f]
            if ref['line'] not in old_lines:
                old_lines.append(ref['line'])
            if old_score < 100:
                files[f] = (min(old_score + 10, 99), old_reason, old_lines)

    # 3. Files that subclass (for classes)
    if cls_locs:
        for other_cls, other_locs in index.get('classes', {}).items():
            for oloc in other_locs:
                if symbol in oloc.get('bases', []):
                    f = oloc['file']
                    if f not in files:
                        files[f] = (90, f'subclass ({other_cls})', [oloc['line']])
                    else:
                        old_score, _, old_lines = files[f]
                        files[f] = (max(old_score, 90), f'subclass ({other_cls})', old_lines)

    if not files:
        candidates = [k for k in list(index.get('classes', {}).keys()) + list(index.get('functions', {}).keys())
                      if symbol.lower() in k.lower()]
        if candidates:
            print(f"'{symbol}' not found. Similar: {', '.join(candidates[:5])}")
        else:
            print(f"'{symbol}' not found in index")
        return

    # Sort by score descending
    ranked = sorted(files.items(), key=lambda x: -x[1][0])

    label = f"{symbol}.{method_context}" if method_context else symbol
    print(f"Files to check when changing '{label}' ({len(ranked)} files):")
    for filepath, (score, reason, lines) in ranked[:20]:
        line_str = ':' + ','.join(str(l) for l in sorted(lines)[:3]) if lines else ''
        print(f"  {filepath}{line_str} ({reason})")
    if len(ranked) > 20:
        print(f"  +{len(ranked) - 20} more files")


def cmd_search(index, pattern):
    """Search for pattern across indexed source files. Faster, smarter grep."""
    results = []
    pattern_lower = pattern.lower()

    # First: search symbol names in the index (instant)
    for name, locs in index.get('classes', {}).items():
        if pattern_lower in name.lower():
            for loc in locs:
                results.append((loc['file'], loc['line'], f"class {name}"))
    for name, locs in index.get('functions', {}).items():
        if pattern_lower in name.lower():
            for loc in locs:
                results.append((loc['file'], loc['line'], f"def {name}{loc['sig']}"))

    # Second: grep source files if index search found < 5 results
    if len(results) < 5:
        try:
            # Use grep for content search (available in all containers)
            cmd = ['grep', '-rn', '--include=*.py', '-l', pattern, REPO_ROOT]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            grep_files = [os.path.relpath(f, REPO_ROOT) for f in proc.stdout.strip().split('\n') if f]
            # Filter out test files and limit
            grep_files = [f for f in grep_files if not _is_test_file(f)][:15]

            if grep_files:
                # Get line numbers for matched files
                for gf in grep_files:
                    if not any(r[0] == gf for r in results):
                        full = os.path.join(REPO_ROOT, gf)
                        try:
                            line_cmd = ['grep', '-n', pattern, full]
                            line_proc = subprocess.run(line_cmd, capture_output=True, text=True, timeout=5)
                            for line in line_proc.stdout.strip().split('\n')[:3]:
                                if ':' in line:
                                    lnum, ctx = line.split(':', 1)
                                    ctx = ctx.strip()[:80]
                                    results.append((gf, int(lnum), ctx))
                        except (subprocess.TimeoutExpired, ValueError):
                            results.append((gf, 0, "(match)"))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not results:
        print(f"No matches for '{pattern}'")
        return

    # Deduplicate by file+line
    seen = set()
    unique = []
    for r in results:
        key = (r[0], r[1])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    print(f"'{pattern}' — {len(unique)} matches:")
    for filepath, line, context in unique[:25]:
        print(f"  {filepath}:{line}  {context}")
    if len(unique) > 25:
        print(f"  +{len(unique) - 25} more")


def cmd_diagnose(index, filepath):
    """Check a file for syntax errors and basic undefined name issues."""
    # Find the file
    full_path = os.path.join(REPO_ROOT, filepath)
    if not os.path.exists(full_path):
        # Try partial path match
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                candidate = os.path.join(root, f)
                rel = os.path.relpath(candidate, REPO_ROOT)
                if _path_match(filepath, rel):
                    full_path = candidate
                    filepath = rel
                    break

    if not os.path.exists(full_path):
        print(f"File not found: {filepath}")
        return

    try:
        with open(full_path, 'r', errors='replace') as f:
            source = f.read()
    except OSError as e:
        print(f"Cannot read {filepath}: {e}")
        return

    print(f"Diagnosing {filepath}:\n")

    # 1. Syntax check
    try:
        tree = ast.parse(source)
        print("  [OK] No syntax errors")
    except SyntaxError as e:
        print(f"  [ERROR] Syntax error at line {e.lineno}: {e.msg}")
        if e.text:
            print(f"    {e.text.rstrip()}")
        return

    # 2. Basic undefined name detection
    # Collect defined names
    defined = set()
    BUILTINS = {
        'True', 'False', 'None', 'print', 'len', 'range', 'str', 'int', 'float',
        'bool', 'list', 'dict', 'set', 'tuple', 'type', 'isinstance', 'issubclass',
        'hasattr', 'getattr', 'setattr', 'delattr', 'super', 'property', 'classmethod',
        'staticmethod', 'object', 'Exception', 'ValueError', 'TypeError', 'KeyError',
        'AttributeError', 'IndexError', 'RuntimeError', 'NotImplementedError',
        'StopIteration', 'OSError', 'IOError', 'ImportError', 'NameError',
        'enumerate', 'zip', 'map', 'filter', 'sorted', 'reversed', 'min', 'max',
        'sum', 'abs', 'any', 'all', 'open', 'repr', 'id', 'hash', 'callable',
        'iter', 'next', 'vars', 'dir', 'globals', 'locals', 'format', 'chr', 'ord',
        'hex', 'oct', 'bin', 'bytes', 'bytearray', 'memoryview', 'frozenset',
        'complex', 'divmod', 'pow', 'round', 'input', 'breakpoint', 'compile',
        'eval', 'exec', 'exit', 'quit', 'copyright', 'credits', 'license',
        'NotImplemented', 'Ellipsis', '__name__', '__file__', '__doc__', '__all__',
        '__import__', '__build_class__', '__spec__', '__loader__', '__package__',
    }
    defined.update(BUILTINS)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    defined.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    defined.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            for arg in node.args.args + node.args.kwonlyargs:
                defined.add(arg.arg)
            if node.args.vararg:
                defined.add(node.args.vararg.arg)
            if node.args.kwarg:
                defined.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.Global):
            defined.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            defined.update(node.names)

    # Collect used names (Load context only, skip self/cls)
    used = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in ('self', 'cls') and node.id not in used:
                used[node.id] = node.lineno

    # Report undefined
    undefined = {name: line for name, line in used.items()
                 if name not in defined and not name.startswith('_')}
    if undefined:
        print(f"\n  Possibly undefined names ({len(undefined)}):")
        for name, line in sorted(undefined.items(), key=lambda x: x[1]):
            print(f"    line {line}: {name}")
    else:
        print("  [OK] No obviously undefined names")

    # 3. Check method override signatures against base class in index
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                base_name = None
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if not base_name:
                    continue
                base_locs = index.get('classes', {}).get(base_name, [])
                if not base_locs:
                    continue
                base_methods = base_locs[0].get('methods', {})
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in base_methods:
                            base_sig = base_methods[item.name].get('sig', '')
                            curr_sig = _get_signature(item)
                            if base_sig and curr_sig != base_sig:
                                print(f"\n  [WARN] {node.name}.{item.name}{curr_sig} overrides {base_name}.{item.name}{base_sig}")


def cmd_check():
    """Check edit completeness against git diff."""
    result = subprocess.run(
        ['git', 'diff', '--name-only'],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    modified_files = [f for f in result.stdout.strip().split('\n')
                      if f.endswith('.py') and f]

    if not modified_files:
        print("No modified Python files found.")
        return

    print(f"Checking {len(modified_files)} modified file(s):\n")

    for filepath in modified_files:
        full_path = os.path.join(REPO_ROOT, filepath)
        if not os.path.exists(full_path):
            continue

        try:
            with open(full_path, 'r', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source)
        except (SyntaxError, OSError) as e:
            print(f"  {filepath}: [ERROR] {e}")
            continue

        issues = []

        # For each class, check if new self.* attrs were added in one method
        # but not initialized in __init__ (common incomplete edit)
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            init_attrs = set()
            method_attrs = {}

            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                attrs = set()
                for child in ast.walk(item):
                    if (isinstance(child, ast.Attribute)
                            and isinstance(child.value, ast.Name)
                            and child.value.id == 'self'):
                        attrs.add(child.attr)
                method_attrs[item.name] = attrs
                if item.name == '__init__':
                    # Attrs that are assigned in __init__
                    for child in ast.walk(item):
                        if (isinstance(child, ast.Attribute)
                                and isinstance(child.value, ast.Name)
                                and child.value.id == 'self'
                                and isinstance(child.ctx, ast.Store)):
                            init_attrs.add(child.attr)

            if not init_attrs:
                continue

            # Check: attrs used in non-init methods but never set in __init__
            for mname, attrs in method_attrs.items():
                if mname == '__init__':
                    continue
                missing = attrs - init_attrs - {'__class__', '__dict__'}
                # Filter to attrs that look like they should be initialized
                for attr in sorted(missing):
                    # Only flag if attr is STORED (written to) in this method
                    for child in ast.walk(node):
                        if (isinstance(child, ast.Attribute)
                                and isinstance(child.value, ast.Name)
                                and child.value.id == 'self'
                                and child.attr == attr
                                and isinstance(child.ctx, ast.Store)):
                            issues.append(
                                f"  {node.name}.{mname}: sets self.{attr} "
                                f"but __init__ doesn't initialize it"
                            )
                            break

        if issues:
            print(f"  {filepath}:")
            for issue in issues[:10]:
                print(f"    {issue}")
        else:
            print(f"  {filepath}: [OK]")


def cmd_help():
    print("""GroundTruth Codebase Intelligence (v5)

  python3 /tmp/gt_tool.py references <Symbol>    — Find all files using this symbol
  python3 /tmp/gt_tool.py impact <Symbol>         — What breaks if you change this?
  python3 /tmp/gt_tool.py search <pattern>        — Smart grep across source files
  python3 /tmp/gt_tool.py outline <file_path>     — Class/method map of a file
  python3 /tmp/gt_tool.py diagnose <file_path>    — Syntax errors + undefined names

Examples:
  python3 /tmp/gt_tool.py references UniqueConstraint
  python3 /tmp/gt_tool.py references Session.resolve_redirects
  python3 /tmp/gt_tool.py impact UniqueConstraint
  python3 /tmp/gt_tool.py search validate_constraints

Index builds on first call, cached for subsequent calls.""")


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
    REPO_ROOT = repo  # noqa: update global for diagnose/check commands

    if command in ('help', '--help', '-h'):
        load_or_build_index(repo)
        cmd_help()
        sys.exit(0)

    index = load_or_build_index(repo)

    if command == 'references' and len(sys.argv) >= 3:
        cmd_references(index, sys.argv[2])
    elif command == 'outline' and len(sys.argv) >= 3:
        cmd_outline(index, sys.argv[2])
    elif command == 'impact' and len(sys.argv) >= 3:
        cmd_impact(index, sys.argv[2])
    elif command == 'search' and len(sys.argv) >= 3:
        cmd_search(index, ' '.join(sys.argv[2:]))
    elif command == 'scope' and len(sys.argv) >= 3:
        cmd_scope(index, sys.argv[2])
    elif command == 'diagnose' and len(sys.argv) >= 3:
        cmd_diagnose(index, sys.argv[2])
    elif command == 'check':
        cmd_check()
    else:
        print(f"Unknown command: {command}")
        cmd_help()
        sys.exit(1)

#!/usr/bin/env python3
"""
GroundTruth v4 — Passive Hook Architecture (OpenHands variant)

Original commands (kept for debugging):
    python3 /tmp/gt_tool.py impact <Symbol>
    python3 /tmp/gt_tool.py references <Symbol>
    python3 /tmp/gt_tool.py check

New hook modes:
    python3 /tmp/gt_tool.py enrich --file=<path>          — Structural coupling notes for file read
    python3 /tmp/gt_tool.py check --quiet --max-items=3   — Filtered obligation check for file edit
    python3 /tmp/gt_tool.py --build-index                 — Pre-build index and exit

Runs on stdlib ast. No dependencies. Indexes on first call, caches.
Designed for SWE-bench containers via OpenHands passive hooks.
"""
import ast
import os
import re
import sys
import json
import glob
import time
import subprocess
import tempfile
from collections import defaultdict

REPO_ROOT = '/testbed'
INDEX_CACHE = os.path.join(tempfile.gettempdir(), 'gt_index.json')
HOOK_LOG = os.path.join(tempfile.gettempdir(), 'gt_hook_log.jsonl')
MAX_FILE_SIZE = 750_000
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.tox', '.eggs',
             'venv', 'env', 'build', 'dist', '.mypy_cache', '.pytest_cache'}
MAX_INDEX_TIME = 30  # seconds


# ═══════════════════════════════════════
# HOOK LOGGING
# ═══════════════════════════════════════

def _log_hook(entry):
    """Append one JSON line to hook log. Never raises."""
    try:
        entry['timestamp'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        with open(HOOK_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


# ═══════════════════════════════════════
# INDEXER
# ═══════════════════════════════════════

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


def _get_written_attrs(method_node):
    """Return set of self.X attrs that are WRITTEN (Store ctx) in a method."""
    written = set()
    for child in ast.walk(method_node):
        if (isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == 'self'
                and isinstance(child.ctx, ast.Store)):
            written.add(child.attr)
    return written


def _classify_method_role(method_name, method_node):
    """Classify a method's role based on AST patterns. Returns a role string."""
    # 1. stores: __init__ or ≥2 self.X = ... assignments
    if method_name == '__init__':
        return 'stores'
    written = _get_written_attrs(method_node)
    if len(written) >= 2:
        return 'stores'

    # 2. serializes: returns dict/tuple/list, or name suggests serialization
    serialize_names = ('deconstruct', 'serialize', 'to_dict', 'as_dict',
                       'to_json', 'as_json', 'to_tuple', 'get_params')
    if any(s in method_name.lower() for s in serialize_names):
        return 'serializes'
    for child in ast.walk(method_node):
        if isinstance(child, ast.Return) and child.value is not None:
            if isinstance(child.value, (ast.Dict, ast.Tuple, ast.List)):
                # Check it references self attrs
                has_self_attr = False
                for sub in ast.walk(child.value):
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == 'self'):
                        has_self_attr = True
                        break
                if has_self_attr:
                    return 'serializes'

    # 3. compares: __eq__/__ne__/__hash__ or comparisons involving self attrs
    if method_name in ('__eq__', '__ne__', '__hash__', '__lt__', '__le__', '__gt__', '__ge__'):
        return 'compares'
    for child in ast.walk(method_node):
        if isinstance(child, ast.Compare):
            for sub in ast.walk(child):
                if (isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == 'self'):
                    return 'compares'

    # 4. validates: raises exceptions after checking self attrs
    validate_names = ('validate', 'check', 'clean', 'verify')
    if any(s in method_name.lower() for s in validate_names):
        return 'validates'
    has_raise = False
    for child in ast.walk(method_node):
        if isinstance(child, ast.Raise):
            has_raise = True
            break
    if has_raise:
        # Check if method also reads self attrs
        for child in ast.walk(method_node):
            if (isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == 'self'
                    and isinstance(child.ctx, ast.Load)):
                return 'validates'

    # 5. reads: default
    return 'reads'


def _get_role_label(role):
    """Human-readable label for output."""
    labels = {
        'stores': 'stores',
        'serializes': 'serializes to kwargs',
        'compares': 'compares',
        'validates': 'checks',
        'reads': 'reads',
    }
    return labels.get(role, role)


def build_index(repo_root):
    start = time.time()
    index = {
        'classes': {},
        'functions': {},
        'imports': {},
        'references': {},
        'files_parsed': 0,
        'build_time': 0,
    }
    py_files = glob.glob(os.path.join(repo_root, '**', '*.py'), recursive=True)

    def _sort_key(fp):
        rel = os.path.relpath(fp, repo_root).lower()
        return (1 if _is_test_file(rel) else 0, rel)
    py_files.sort(key=_sort_key)

    _SKIP_METHODS = {'__str__', '__repr__', '__hash__', '__eq__',
                     '__ne__', '__lt__', '__le__', '__gt__', '__ge__',
                     '__len__', '__bool__', '__contains__',
                     '__enter__', '__exit__', '__del__'}

    for filepath in py_files:
        rel = os.path.relpath(filepath, repo_root)
        parts = rel.split(os.sep)
        if any(p in SKIP_DIRS for p in parts):
            continue
        try:
            if os.path.getsize(filepath) > MAX_FILE_SIZE:
                continue
        except OSError:
            continue
        is_test = _is_test_file(rel)
        try:
            with open(filepath, 'r', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source, filename=filepath)
        except (SyntaxError, ValueError, RecursionError):
            continue

        index['files_parsed'] += 1

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    name = alias.name
                    index['imports'].setdefault(rel, []).append(name)
                    index['references'].setdefault(name, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'import'
                    })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split('.')[-1]
                    index['imports'].setdefault(rel, []).append(name)

        if not is_test:
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    cls_info = _parse_class(node, rel)
                    if cls_info:
                        index['classes'].setdefault(node.name, []).append(cls_info)
                        for method_name, method_info in cls_info['methods'].items():
                            if method_name in _SKIP_METHODS:
                                continue
                            index['references'].setdefault(method_name, []).append({
                                'file': rel, 'line': method_info['line'],
                                'type': 'method_def', 'class': node.name
                            })
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

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and len(node.id) > 2:
                if node.id[0].isupper() and not node.id.isupper():
                    index['references'].setdefault(node.id, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'usage'
                    })
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
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and len(node.func.id) > 2:
                    fname = node.func.id
                    if not fname[0].isupper() and not fname.isupper() and '_' in fname:
                        index['references'].setdefault(fname, []).append({
                            'file': rel, 'line': node.lineno, 'type': 'call'
                        })

        if time.time() - start > MAX_INDEX_TIME:
            index['truncated'] = True
            index['total_py_files'] = len(py_files)
            break

    index['build_time'] = round(time.time() - start, 2)
    index['truncated'] = index.get('truncated', False)
    with open(INDEX_CACHE, 'w') as f:
        json.dump(index, f)
    return index


def load_or_build_index(repo_root):
    if os.path.exists(INDEX_CACHE):
        try:
            with open(INDEX_CACHE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return build_index(repo_root)


def _warn_if_truncated(index):
    if index.get('truncated'):
        total = index.get('total_py_files', '?')
        parsed = index.get('files_parsed', '?')
        print(f"[NOTE: Index covers {parsed}/{total} files (time budget). Use grep for files not found.]\n")


# ═══════════════════════════════════════
# ENDPOINT 1: impact
# ═══════════════════════════════════════

def cmd_impact(index, symbol):
    """Pre-edit: what breaks if I change this symbol?

    Shows: definition, inheritance, methods with shared state,
    and files that import/use the symbol.
    """
    _warn_if_truncated(index)
    cls_locations = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])
    refs = index.get('references', {}).get(symbol, [])

    if not cls_locations and not func_locs:
        candidates = [k for k in list(index.get('classes', {}).keys()) + list(index.get('functions', {}).keys())
                      if symbol.lower() in k.lower() or k.lower() in symbol.lower()]
        if candidates:
            print(f"'{symbol}' not found. Similar: {', '.join(candidates[:5])}")
        else:
            print(f"'{symbol}' not found in index.")
        return

    # Definition + structure
    if cls_locations:
        for loc in cls_locations:
            bases_str = f" < {', '.join(loc['bases'])}" if loc['bases'] else ""
            methods = sorted(loc['methods'].items(), key=lambda x: x[1]['line'])
            method_list = ', '.join(f"{m}{info['sig']}" for m, info in methods[:10])
            more = f" +{len(methods) - 10}" if len(methods) > 10 else ""
            print(f"IMPACT: {symbol}{bases_str} @ {loc['file']}:{loc['line']}")
            print(f"Methods: {method_list}{more}")

            # Shared state obligations
            init_attrs = set()
            for m, info in loc['methods'].items():
                if m == '__init__':
                    init_attrs = set(info.get('attrs', []))
                    break

            if init_attrs:
                obligations = []
                for m, info in loc['methods'].items():
                    if m == '__init__':
                        continue
                    shared = set(info.get('attrs', [])) & init_attrs
                    if len(shared) >= 2:
                        obligations.append(f"  {symbol}.{m}:{info['line']} shares {', '.join(sorted(shared))}")
                if obligations:
                    print(f"OBLIGATIONS (shared state):")
                    for ob in obligations[:8]:
                        print(ob)

            # Base class methods (override awareness)
            for base in loc['bases']:
                base_locs = index.get('classes', {}).get(base, [])
                if base_locs:
                    base_methods = list(base_locs[0]['methods'].keys())[:8]
                    print(f"  {base} methods: {', '.join(base_methods)}")

    if func_locs:
        for loc in func_locs:
            print(f"IMPACT: {symbol}{loc['sig']} @ {loc['file']}:{loc['line']}")

    # External usage — which files must be checked
    if refs:
        by_file = defaultdict(list)
        for ref in refs:
            by_file[ref['file']].append(ref)
        def_files = {loc['file'] for loc in cls_locations} if cls_locations else set()
        external = {f: r for f, r in by_file.items() if f not in def_files}

        if external:
            print(f"CALLERS ({len(external)} files):")
            for fp in sorted(external.keys())[:10]:
                lines = sorted(set(r['line'] for r in external[fp]))[:3]
                print(f"  {fp}:{','.join(str(l) for l in lines)}")
            if len(external) > 10:
                print(f"  +{len(external) - 10} more")
    else:
        print("No external callers found.")


# ═══════════════════════════════════════
# ENDPOINT 2: references
# ═══════════════════════════════════════

def cmd_references(index, symbol):
    """Where is this symbol defined and where is it used?"""
    _warn_if_truncated(index)
    refs = index.get('references', {}).get(symbol, [])

    # Fallback: Class.method notation
    if not refs and '.' in symbol:
        cls_name, method_name = symbol.rsplit('.', 1)
        for ref in index.get('references', {}).get(method_name, []):
            if ref.get('class') == cls_name:
                refs.append(ref)
        if not refs:
            for ref in index.get('references', {}).get(method_name, []):
                refs.append(ref)

    if not refs:
        sym_lower = symbol.lower()
        candidates = []
        for k, v in index.get('references', {}).items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                first_file = v[0]['file'] if v else '?'
                candidates.append((k, first_file, len(v)))
        for k, v in index.get('classes', {}).items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                first_file = v[0]['file'] if v else '?'
                candidates.append((k, first_file, -1))
        for k, v in index.get('functions', {}).items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                first_file = v[0]['file'] if v else '?'
                candidates.append((k, first_file, -1))

        seen_names = set()
        unique = []
        for name, fpath, count in candidates:
            if name not in seen_names:
                seen_names.add(name)
                unique.append((name, fpath, count))

        if unique:
            print(f"'{symbol}' not found. Did you mean:")
            for name, fpath, count in unique[:5]:
                count_str = f" ({count} refs)" if count > 0 else ""
                print(f"  {name} in {fpath}{count_str}")
        else:
            print(f"No references found for '{symbol}'")
        return

    # Deduplicate and group
    by_file = defaultdict(list)
    seen = set()
    for ref in refs:
        key = (ref['file'], ref['line'])
        if key not in seen:
            seen.add(key)
            by_file[ref['file']].append(ref)

    def_files = []
    src_files = []
    test_files = []
    for filepath in sorted(by_file.keys()):
        file_refs = sorted(by_file[filepath], key=lambda r: r['line'])
        has_def = any(r['type'] in ('method_def', 'import') for r in file_refs)
        lines = ','.join(str(r['line']) for r in file_refs[:5])
        more = f"+{len(file_refs) - 5}" if len(file_refs) > 5 else ""
        entry = f"{filepath}:{lines}{more}"

        if has_def:
            def_files.append(entry)
        elif _is_test_file(filepath):
            test_files.append(entry)
        else:
            src_files.append(entry)

    print(f"REFERENCES: {symbol} ({len(seen)} refs in {len(by_file)} files)")
    if def_files:
        print(f"Defined: {' | '.join(def_files)}")
    if src_files:
        print(f"Source ({len(src_files)}):")
        for f in src_files[:10]:
            print(f"  {f}")
        if len(src_files) > 10:
            print(f"  ...+{len(src_files) - 10} more")
    if test_files:
        print(f"Tests ({len(test_files)}):")
        for f in test_files[:5]:
            print(f"  {f}")
        if len(test_files) > 5:
            print(f"  ...+{len(test_files) - 5} more")


# ═══════════════════════════════════════
# ENDPOINT 3: check
# ═══════════════════════════════════════

def _get_sibling_patterns(dir_path, exclude_file):
    patterns = {'exception_types': set()}
    try:
        siblings = [f for f in os.listdir(dir_path)
                     if f.endswith('.py') and os.path.join(dir_path, f) != exclude_file
                     and not f.startswith('test_')]
    except OSError:
        return patterns
    for sib in siblings[:10]:
        sib_path = os.path.join(dir_path, sib)
        try:
            with open(sib_path, 'r', errors='replace') as f:
                sib_source = f.read()
            sib_tree = ast.parse(sib_source)
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(sib_tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                if isinstance(node.exc.func, ast.Name):
                    patterns['exception_types'].add(node.exc.func.id)
    return patterns


def _run_pyright_diagnostics(modified_files):
    full_paths = [os.path.join(REPO_ROOT, f) for f in modified_files if os.path.exists(os.path.join(REPO_ROOT, f))]
    if not full_paths:
        return []
    try:
        result = subprocess.run(
            ["pyright", "--outputjson"] + full_paths,
            capture_output=True, text=True, timeout=30, cwd=REPO_ROOT,
        )
        data = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError,
            json.JSONDecodeError, ValueError):
        return []
    issues = []
    for diag in data.get("generalDiagnostics", []):
        if diag.get("severity", "") != "error":
            continue
        file_path = diag.get("file", "")
        if file_path.startswith(REPO_ROOT):
            file_path = os.path.relpath(file_path, REPO_ROOT)
        line = diag.get("range", {}).get("start", {}).get("line", 0)
        message = diag.get("message", "")
        rule = diag.get("rule", "")
        label = f"[pyright:{rule}] {message}" if rule else f"[pyright] {message}"
        issues.append(("ERROR", file_path, line, label))
    return issues


def cmd_check():
    """Post-edit: is my patch structurally complete?

    Checks:
    1. Shared-state obligations (self.attr coupling between methods)
    2. Self.method() calls to nonexistent methods
    3. Import verification
    4. Override signature consistency
    5. Exception pattern consistency
    6. Pyright diagnostics (optional)
    """
    index = load_or_build_index(REPO_ROOT)
    result = subprocess.run(
        ['git', 'diff', '--name-only'],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    modified_files = [f for f in result.stdout.strip().split('\n')
                      if f.endswith('.py') and f]

    if not modified_files:
        print("No modified Python files found.")
        return

    all_issues = []

    for filepath in modified_files:
        full_path = os.path.join(REPO_ROOT, filepath)
        if not os.path.exists(full_path):
            continue
        try:
            with open(full_path, 'r', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source)
        except SyntaxError as e:
            all_issues.append(("ERROR", filepath, e.lineno or 0, f"Syntax error: {e.msg}"))
            continue
        except OSError:
            continue

        # Check 1: Shared-state obligation sites
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
                    for child in ast.walk(item):
                        if (isinstance(child, ast.Attribute)
                                and isinstance(child.value, ast.Name)
                                and child.value.id == 'self'
                                and isinstance(child.ctx, ast.Store)):
                            init_attrs.add(child.attr)
            if not init_attrs:
                continue
            for mname, attrs in method_attrs.items():
                if mname == '__init__':
                    continue
                missing = attrs - init_attrs - {'__class__', '__dict__'}
                for attr in sorted(missing):
                    for child in ast.walk(node):
                        if (isinstance(child, ast.Attribute)
                                and isinstance(child.value, ast.Name)
                                and child.value.id == 'self'
                                and child.attr == attr
                                and isinstance(child.ctx, ast.Store)):
                            all_issues.append(("INFO", filepath, node.lineno,
                                               f"{node.name}.{mname}: self.{attr} not in __init__ "
                                               "(may be intentional — do not revise unless clearly wrong)"))
                            break

            # Check 2: self.method() calls to nonexistent methods
            cls_info = index.get('classes', {}).get(node.name, [])
            if cls_info:
                known_methods = set(cls_info[0].get('methods', {}).keys())
            else:
                known_methods = set(m for m in method_attrs.keys())
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(item):
                    if (isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and isinstance(child.func.value, ast.Name)
                            and child.func.value.id == 'self'):
                        called = child.func.attr
                        if (called not in known_methods
                                and called not in method_attrs
                                and not called.startswith('_')
                                and len(called) > 2):
                            all_issues.append(("ERROR", filepath, item.lineno,
                                               f"{node.name}.{item.name}: self.{called}() not found"))

        # Check 3: Import verification
        all_known_names = set()
        for cls_name in index.get('classes', {}):
            all_known_names.add(cls_name)
        for func_name in index.get('functions', {}):
            all_known_names.add(func_name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    name = alias.name
                    if name == '*' or len(name) <= 2:
                        continue
                    if name not in all_known_names:
                        if node.level and node.level > 0:
                            all_issues.append(("INFO", filepath, node.lineno,
                                               f"Import '{name}' from {node.module} not in index "
                                               "(may be intentional — do not revise unless clearly wrong)"))

    # Check 4: Override signature + exception pattern consistency
    for filepath in modified_files:
        full_path = os.path.join(REPO_ROOT, filepath)
        if not os.path.exists(full_path):
            continue
        try:
            with open(full_path, 'r', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue

        dir_path = os.path.dirname(full_path)
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
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
                                all_issues.append(("INFO", filepath, item.lineno,
                                                   f"{node.name}.{item.name}{curr_sig} vs base "
                                                   f"{base_name}.{item.name}{base_sig} "
                                                   "(may be intentional — do not revise unless clearly wrong)"))

        sibling_patterns = _get_sibling_patterns(dir_path, full_path)
        if sibling_patterns.get('exception_types'):
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    if isinstance(node.exc.func, ast.Name):
                        exc_name = node.exc.func.id
                        if (exc_name not in sibling_patterns['exception_types']
                                and exc_name.endswith('Error')
                                and len(sibling_patterns['exception_types']) > 0):
                            common = ', '.join(sorted(sibling_patterns['exception_types'])[:3])
                            all_issues.append(("INFO", filepath, node.lineno,
                                               f"Unusual exception {exc_name} — siblings use: {common} "
                                               "(may be intentional — do not revise unless clearly wrong)"))

    # Check 5: Pyright diagnostics
    pyright_issues = _run_pyright_diagnostics(modified_files)
    all_issues.extend(pyright_issues)

    if not all_issues:
        print(f"CLEAN: All {len(modified_files)} file(s) pass checks")
        return

    severity_order = {"ERROR": 0, "INFO": 1}
    all_issues.sort(key=lambda x: (severity_order.get(x[0], 2), x[1], x[2]))
    errors = [i for i in all_issues if i[0] == "ERROR"]
    infos = [i for i in all_issues if i[0] == "INFO"]

    if errors:
        print(f"NEEDS_FIXES: {len(errors)} error(s), {len(infos)} info(s)")
    else:
        print(f"INCOMPLETE: {len(infos)} obligation site(s) to check")

    for severity, fpath, line, msg in all_issues[:5]:
        print(f"[{severity}] {fpath}:{line} — {msg}")
    if len(all_issues) > 5:
        print(f"  ...+{len(all_issues) - 5} more issues")


# ═══════════════════════════════════════
# HOOK MODE 1: enrich (file read)
# ═══════════════════════════════════════

def cmd_enrich(filepath):
    """Structural coupling notes for a file. Output 0-5 lines or nothing."""
    start = time.time()
    log_entry = {'mode': 'enrich', 'file': filepath, 'classes_found': 0,
                 'coupled_classes': 0, 'output_lines': 0}

    load_or_build_index(REPO_ROOT)  # ensure index is cached for other hooks

    full_path = filepath
    if not os.path.isabs(filepath):
        full_path = os.path.join(REPO_ROOT, filepath)
    if not os.path.exists(full_path):
        log_entry['wall_time_ms'] = int((time.time() - start) * 1000)
        _log_hook(log_entry)
        return

    # Skip test files
    rel = os.path.relpath(full_path, REPO_ROOT)
    if _is_test_file(rel):
        log_entry['wall_time_ms'] = int((time.time() - start) * 1000)
        _log_hook(log_entry)
        return

    try:
        with open(full_path, 'r', errors='replace') as f:
            source = f.read()
        tree = ast.parse(source, filename=full_path)
    except (SyntaxError, OSError):
        log_entry['wall_time_ms'] = int((time.time() - start) * 1000)
        _log_hook(log_entry)
        return

    # Find classes with coupled methods
    coupled_output = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        log_entry['classes_found'] += 1

        # Collect method info with AST nodes for role classification
        method_infos = {}
        method_nodes = {}
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            attrs = set()
            for child in ast.walk(item):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == 'self'):
                    attrs.add(child.attr)
            method_infos[item.name] = attrs
            method_nodes[item.name] = item

        if len(method_infos) < 3:
            continue

        # Find attrs shared across ≥3 methods
        attr_counts = defaultdict(int)
        for attrs in method_infos.values():
            for attr in attrs:
                attr_counts[attr] += 1
        shared_attrs = sorted(a for a, c in attr_counts.items() if c >= 3)

        if len(shared_attrs) < 2:
            continue

        # This class qualifies: ≥3 methods sharing ≥2 attrs
        log_entry['coupled_classes'] += 1

        # Classify roles and build output
        method_chain = []
        for mname, mnode in sorted(method_nodes.items(), key=lambda x: x[1].lineno):
            mattrs = method_infos[mname]
            shared_count = len(mattrs & set(shared_attrs))
            if shared_count < 2:
                continue
            role = _classify_method_role(mname, mnode)
            method_chain.append((mname, mnode.lineno, role))

        if len(method_chain) < 2:
            continue

        # Build compact output (max 3 lines per class, max 5 total)
        shared_str = ', '.join(f'self.{a}' for a in shared_attrs[:4])
        if len(shared_attrs) > 4:
            shared_str += f', +{len(shared_attrs) - 4} more'

        lines = []
        lines.append("-- structural coupling --")
        lines.append(
            f"{node.name}: {len(method_chain)} methods share {shared_str}")

        chain_parts = []
        for mname, lineno, role in method_chain[:6]:
            chain_parts.append(f"{mname}:{lineno} ({_get_role_label(role)})")
        lines.append("  " + " -> ".join(chain_parts))

        # Actionable rule
        store_methods = [m for m, _, r in method_chain if r == 'stores']
        serialize_methods = [m for m, _, r in method_chain if r == 'serializes']
        compare_methods = [m for m, _, r in method_chain if r == 'compares']
        validate_methods = [m for m, _, r in method_chain if r == 'validates']

        targets = serialize_methods + compare_methods + validate_methods
        if store_methods and targets:
            rule_targets = ' and '.join(t for t in targets[:3])
            lines.append(
                f"  Rule: changes to {store_methods[0]} params must appear in {rule_targets}")

        # Only add if we have room for at least the header + name line
        if len(coupled_output) + 2 > 5:
            break
        coupled_output.extend(lines)
        if len(coupled_output) >= 5:
            break

    # Output (max 5 lines, or nothing)
    output = coupled_output[:5]
    if output:
        print('\n'.join(output))

    log_entry['output_lines'] = len(output)
    log_entry['wall_time_ms'] = int((time.time() - start) * 1000)
    _log_hook(log_entry)


# ═══════════════════════════════════════
# HOOK MODE 2: check --quiet (file edit)
# ═══════════════════════════════════════

def _parse_diff_hunks():
    """Parse git diff to find changed line ranges per file.

    Returns dict: filepath -> list of (start_line, end_line) tuples for added lines.
    """
    try:
        result = subprocess.run(
            ['git', 'diff', '--unified=0'],
            capture_output=True, text=True, cwd=REPO_ROOT
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    hunks = defaultdict(list)
    current_file = None
    for line in result.stdout.split('\n'):
        if line.startswith('+++ b/'):
            current_file = line[6:]
        elif line.startswith('@@') and current_file:
            # Parse @@ -a,b +c,d @@ format
            match = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if count > 0:
                    hunks[current_file].append((start, start + count - 1))

    return dict(hunks)


def _find_changed_methods(hunks, tree):
    """Map changed line ranges to enclosing methods. Returns list of (class_name, method_name, method_node, class_node)."""
    changed = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            m_start = item.lineno
            m_end = getattr(item, 'end_lineno', m_start + 50)
            for h_start, h_end in hunks:
                if m_start <= h_end and h_start <= m_end:
                    changed.append((node.name, item.name, item, node))
                    break
    return changed


def _compute_obligation_confidence(changed_attrs, target_attrs, target_role,
                                   changed_written, same_file, mutual_calls):
    """Compute confidence score for an obligation finding. Returns float 0-1."""
    shared = changed_attrs & target_attrs
    score = 0.0
    if len(shared) >= 3:
        score += 0.3
    if shared & changed_written:
        score += 0.2
    if same_file:
        score += 0.2
    if mutual_calls:
        score += 0.15
    if target_role in ('validates', 'serializes'):
        score += 0.15
    return score


def cmd_check_quiet(max_items=3):
    """Filtered obligation check for file edits. Output 0-N lines or nothing."""
    start = time.time()
    log_entry = {'mode': 'check_quiet', 'files_changed': [],
                 'raw_findings': 0, 'after_abstention': 0,
                 'suppressed_count': 0, 'suppressed_reasons': [],
                 'output': '', 'obligations_reported': []}

    load_or_build_index(REPO_ROOT)  # ensure index is cached

    # Get modified files
    result = subprocess.run(
        ['git', 'diff', '--name-only'],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    modified_files = [f for f in result.stdout.strip().split('\n')
                      if f.endswith('.py') and f]
    if not modified_files:
        log_entry['wall_time_ms'] = int((time.time() - start) * 1000)
        _log_hook(log_entry)
        return

    log_entry['files_changed'] = modified_files

    # Parse diff hunks for changed line ranges
    diff_hunks = _parse_diff_hunks()

    # Collect obligation findings with strict abstention
    findings = []
    suppressed = []

    for filepath in modified_files:
        full_path = os.path.join(REPO_ROOT, filepath)
        if not os.path.exists(full_path):
            continue

        # Skip test files
        if _is_test_file(filepath):
            continue

        try:
            with open(full_path, 'r', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue

        file_hunks = diff_hunks.get(filepath, [])
        if not file_hunks:
            continue

        # Find which methods were changed
        changed_methods = _find_changed_methods(file_hunks, tree)
        if not changed_methods:
            continue

        # For each class with changed methods, find obligation targets
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # Collect all method attrs and nodes
            method_attrs = {}
            method_nodes = {}
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
                method_nodes[item.name] = item

            # Find changed methods in this class
            cls_changed = [(cn, mn, mnode) for cn, mn, mnode, _cnode in changed_methods
                           if cn == node.name]
            if not cls_changed:
                continue

            for _, changed_name, changed_node in cls_changed:
                changed_a = method_attrs.get(changed_name, set())
                changed_written = _get_written_attrs(changed_node)
                changed_calls = set()
                for child in ast.walk(changed_node):
                    if (isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and isinstance(child.func.value, ast.Name)
                            and child.func.value.id == 'self'):
                        changed_calls.add(child.func.attr)

                # Check each other method as a potential obligation target
                for target_name, target_node in method_nodes.items():
                    if target_name == changed_name:
                        continue

                    target_a = method_attrs.get(target_name, set())
                    shared = changed_a & target_a
                    log_entry['raw_findings'] += 1

                    # Abstention filter (all 7 must pass)
                    reason = None

                    # 1. Must involve shared self.attrs
                    if len(shared) < 1:
                        reason = 'no_shared_attrs'
                    # 2. Target shares ≥2 self.attrs with changed
                    elif len(shared) < 2:
                        reason = 'insufficient_shared_attrs'
                    # 3. Same file (we're iterating within the same file, so always true here)
                    # 4. Shared attrs are WRITTEN in changed method
                    elif not (shared & changed_written):
                        reason = 'attrs_not_written'
                    # 5. Not cosmetic methods
                    elif target_name in ('__str__', '__repr__', '__format__'):
                        reason = 'cosmetic_method'
                    # 6. Not private
                    elif target_name.startswith('_') and target_name != '__init__':
                        reason = 'private_method'
                    else:
                        # Was the target already edited?
                        target_changed = any(
                            mn == target_name
                            for cn, mn, _mn, _cn in changed_methods
                            if cn == node.name)
                        if target_changed:
                            # Already covered — don't report
                            continue

                        # 7. Confidence threshold
                        target_role = _classify_method_role(target_name, target_node)

                        # Check mutual calls
                        target_calls = set()
                        for tc in ast.walk(target_node):
                            if (isinstance(tc, ast.Call)
                                    and isinstance(getattr(tc, 'func', None), ast.Attribute)
                                    and isinstance(getattr(getattr(tc, 'func', None), 'value', None), ast.Name)
                                    and tc.func.value.id == 'self'):
                                target_calls.add(tc.func.attr)
                        mutual = (target_name in changed_calls or
                                  changed_name in target_calls)

                        conf = _compute_obligation_confidence(
                            changed_a, target_a, target_role,
                            changed_written, True, mutual)

                        if conf < 0.8:
                            reason = 'low_confidence'
                        else:
                            # Finding passes all filters
                            shared_str = ','.join(sorted(shared)[:3])
                            findings.append({
                                'class': node.name,
                                'target': target_name,
                                'line': target_node.lineno,
                                'role': target_role,
                                'shared': shared_str,
                                'confidence': round(conf, 2),
                            })

                    if reason:
                        suppressed.append(reason)

    log_entry['suppressed_count'] = len(suppressed)
    log_entry['suppressed_reasons'] = list(set(suppressed))
    log_entry['after_abstention'] = len(findings)

    # Output
    if not findings:
        # Silent — no output
        pass
    else:
        # Sort by confidence descending, take top max_items
        findings.sort(key=lambda f: -f['confidence'])
        top = findings[:max_items]

        parts = []
        for f in top:
            parts.append(f"{f['target']}:{f['line']} ({_get_role_label(f['role'])} self.{f['shared']})")
            log_entry['obligations_reported'].append(f"{f['class']}.{f['target']}")

        output = f"GT: {len(top)} uncovered — " + ", ".join(parts)
        print(output)
        log_entry['output'] = output

    log_entry['wall_time_ms'] = int((time.time() - start) * 1000)
    _log_hook(log_entry)


# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════

if __name__ == '__main__':
    try:
        repo = os.environ.get('GT_REPO', REPO_ROOT)
        REPO_ROOT = repo

        if len(sys.argv) < 2:
            print("""GroundTruth v4 — Passive Hook Architecture

  impact <Symbol>                     — Pre-edit: what breaks if I change this?
  references <Symbol>                 — Where is this defined and used?
  check                               — Post-edit: is my patch structurally complete?
  enrich --file=<path>                — Structural coupling notes (hook mode)
  check --quiet [--max-items=N]       — Filtered obligation check (hook mode)
  --build-index                       — Pre-build index and exit

Examples:
  python3 /tmp/gt_tool.py impact UniqueConstraint
  python3 /tmp/gt_tool.py enrich --file=django/db/models/constraints.py
  python3 /tmp/gt_tool.py check --quiet --max-items=3""")
            sys.exit(0)

        command = sys.argv[1].lower()

        # --build-index: pre-build and exit
        if command == '--build-index':
            start = time.time()
            index = build_index(repo)
            elapsed = round(time.time() - start, 2)
            print(f"INDEX_READY {elapsed}s {index['files_parsed']} files")
            sys.exit(0)

        if command in ('help', '--help', '-h'):
            load_or_build_index(repo)
            print("GroundTruth v4: impact | references | check | enrich | check --quiet | --build-index")
            sys.exit(0)

        if command == 'enrich':
            # Parse --file=<path>
            filepath = None
            for arg in sys.argv[2:]:
                if arg.startswith('--file='):
                    filepath = arg[7:]
                elif not arg.startswith('-'):
                    filepath = arg
            if not filepath:
                print("Usage: enrich --file=<path>")
                sys.exit(1)
            cmd_enrich(filepath)

        elif command == 'impact' and len(sys.argv) >= 3:
            index = load_or_build_index(repo)
            cmd_impact(index, sys.argv[2])

        elif command == 'references' and len(sys.argv) >= 3:
            index = load_or_build_index(repo)
            cmd_references(index, sys.argv[2])

        elif command in ('check', 'groundtruth_check'):
            # Check for --quiet flag
            quiet = '--quiet' in sys.argv
            if quiet:
                max_items = 3
                for arg in sys.argv:
                    if arg.startswith('--max-items='):
                        try:
                            max_items = int(arg.split('=')[1])
                        except (ValueError, IndexError):
                            pass
                cmd_check_quiet(max_items)
            else:
                cmd_check()

        else:
            print(f"Unknown command: {command}")
            print("Usage: impact <sym> | references <sym> | check | enrich --file=<path>")
            sys.exit(1)

    except (MemoryError, RecursionError) as e:
        print(f"GT tool error ({type(e).__name__}). Use grep/find instead.")
        sys.exit(1)
    except Exception as e:
        print(f"GT tool error: {e}. Use grep/find for this query.")
        sys.exit(1)

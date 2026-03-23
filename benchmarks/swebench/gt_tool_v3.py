#!/usr/bin/env python3
"""
GroundTruth v3 — 3-Endpoint Architecture (OpenHands variant)

Three commands only:
    python3 /tmp/gt_tool.py impact <Symbol>      — Pre-edit: what breaks if I change this?
    python3 /tmp/gt_tool.py references <Symbol>   — Where is this defined and used?
    python3 /tmp/gt_tool.py check                 — Post-edit: is my patch complete?

Runs on stdlib ast. No dependencies. Indexes on first call, caches.
Designed for SWE-bench containers via OpenHands.
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
MAX_FILE_SIZE = 750_000
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.tox', '.eggs',
             'venv', 'env', 'build', 'dist', '.mypy_cache', '.pytest_cache'}
MAX_INDEX_TIME = 30  # seconds


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
# MAIN
# ═══════════════════════════════════════

if __name__ == '__main__':
    try:
        if len(sys.argv) < 2:
            print("""GroundTruth v3 — 3-Endpoint Architecture

  impact <Symbol>      — Pre-edit: what breaks if I change this?
  references <Symbol>  — Where is this defined and used?
  check                — Post-edit: is my patch structurally complete?

Examples:
  python3 /tmp/gt_tool.py impact UniqueConstraint
  python3 /tmp/gt_tool.py references Session.resolve_redirects
  python3 /tmp/gt_tool.py check""")
            sys.exit(0)

        command = sys.argv[1].lower()
        repo = os.environ.get('GT_REPO', REPO_ROOT)
        REPO_ROOT = repo

        if command in ('help', '--help', '-h'):
            load_or_build_index(repo)
            print("GroundTruth v3: impact <sym> | references <sym> | check")
            sys.exit(0)

        if command == 'impact' and len(sys.argv) >= 3:
            index = load_or_build_index(repo)
            cmd_impact(index, sys.argv[2])
        elif command == 'references' and len(sys.argv) >= 3:
            index = load_or_build_index(repo)
            cmd_references(index, sys.argv[2])
        elif command in ('check', 'groundtruth_check'):
            cmd_check()
        else:
            print(f"Unknown command: {command}")
            print("Usage: impact <sym> | references <sym> | check")
            sys.exit(1)
    except (MemoryError, RecursionError) as e:
        print(f"GT tool error ({type(e).__name__}). Use grep/find instead.")
        sys.exit(1)
    except Exception as e:
        print(f"GT tool error: {e}. Use grep/find for this query.")
        sys.exit(1)

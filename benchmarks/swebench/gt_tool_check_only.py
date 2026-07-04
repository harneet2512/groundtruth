#!/usr/bin/env python3
"""
GroundTruth — Check-Only Version (leaderboard variant)

Stripped-down gt_tool containing ONLY the groundtruth_check / check command.
No references, impact, scope, obligations, context, related, diagnose, summary, etc.

Usage:
    python3 /tmp/gt_tool.py groundtruth_check   — Completeness check against git diff
    python3 /tmp/gt_tool.py check               — (alias)

Runs on stdlib ast. No dependencies. Indexes on first call, caches.
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
MAX_FILE_SIZE = 750_000  # 750KB — some Django files (models.py) are large
SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.tox', '.eggs',
             'venv', 'env', 'build', 'dist', '.mypy_cache', '.pytest_cache'}
MAX_INDEX_TIME = 30  # seconds


# ───────────────────────────────
# INDEXER HELPERS
# ───────────────────────────────

def _is_test_file(filepath):
    fp = "/" + filepath.lower().replace("\\", "/")
    dir_patterns = ['/tests/', '/test/', '/__tests__/', '/testing/',
                    '/docs/', '/doc/', '/examples/', '/example/',
                    '/fixtures/']
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
    sig = f"({', '.join(parts)})"
    if hasattr(func_node, 'returns') and func_node.returns:
        try:
            ret = ast.unparse(func_node.returns)
            if len(ret) < 40:
                sig += f" -> {ret}"
        except (ValueError, AttributeError):
            pass
    return sig


def _classify_attr_roles(method_node, method_name):
    """For each self.X in a method, classify by AST context (how the attr is used)."""
    roles = {}

    class AttrRoleVisitor(ast.NodeVisitor):
        def __init__(self):
            self.parent_stack = []

        def _push(self, node):
            self.parent_stack.append(node)

        def _pop(self):
            if self.parent_stack:
                self.parent_stack.pop()

        def _parent(self, n=1):
            if len(self.parent_stack) >= n:
                return self.parent_stack[-n]
            return None

        def _is_self_attr(self, node):
            return (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == 'self')

        def _classify(self, node):
            if not self._is_self_attr(node):
                return
            attr = node.attr
            roles.setdefault(attr, set())
            parent = self._parent()

            if isinstance(node.ctx, ast.Store):
                roles[attr].add('stores_in_state')
                return
            if method_name in ('__eq__', '__hash__', '__ne__'):
                roles[attr].add('compares_in_eq')
            elif isinstance(parent, ast.Compare):
                roles[attr].add('compares_in_eq')
            if isinstance(parent, ast.Dict):
                roles[attr].add('serializes_to_kwargs')
            elif isinstance(parent, (ast.Tuple, ast.List)):
                roles[attr].add('serializes_to_kwargs')
            elif isinstance(parent, ast.keyword):
                roles[attr].add('serializes_to_kwargs')
            if isinstance(parent, ast.FormattedValue):
                roles[attr].add('emits_to_output')
            elif isinstance(parent, ast.JoinedStr):
                roles[attr].add('emits_to_output')
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Mod):
                roles[attr].add('emits_to_output')
            if isinstance(parent, ast.Call) and not self._is_self_attr(parent.func if hasattr(parent, 'func') else parent):
                if hasattr(parent, 'func'):
                    func = parent.func
                    if not (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == 'self'):
                        roles[attr].add('passes_to_validator')
            if isinstance(parent, ast.If) or isinstance(parent, ast.While):
                roles[attr].add('reads_in_logic')
            elif isinstance(parent, ast.Assert):
                roles[attr].add('reads_in_logic')
            elif isinstance(parent, ast.BoolOp):
                roles[attr].add('reads_in_logic')

        def generic_visit(self, node):
            self._push(node)
            for child in ast.iter_child_nodes(node):
                self._classify(child)
            super().generic_visit(node)
            self._pop()

    try:
        visitor = AttrRoleVisitor()
        visitor.visit(method_node)
    except (RecursionError, Exception):
        pass

    return {k: sorted(v) for k, v in roles.items() if v}


def _classify_method_conventions(method_node):
    """Per-method convention detection."""
    conventions = []

    body = method_node.body
    start_idx = 0
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        start_idx = 1

    if start_idx < len(body):
        first = body[start_idx]
        if isinstance(first, ast.If):
            for child in ast.walk(first):
                if isinstance(child, ast.Raise):
                    conventions.append('guards_on_state')
                    break

    for node in ast.walk(method_node):
        if isinstance(node, ast.Raise) and node.exc:
            exc = node.exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                conventions.append(f'raises:{exc.func.id}')
            elif isinstance(exc, ast.Name):
                conventions.append(f'raises:{exc.id}')

    return_types = set()
    for node in ast.walk(method_node):
        if isinstance(node, ast.Return) and node.value:
            val = node.value
            if isinstance(val, ast.Dict):
                return_types.add('dict')
            elif isinstance(val, ast.List):
                return_types.add('list')
            elif isinstance(val, ast.Tuple):
                return_types.add('tuple')
            elif isinstance(val, ast.Set):
                return_types.add('set')
            elif isinstance(val, ast.Call):
                if isinstance(val.func, ast.Name):
                    return_types.add(val.func.id)
                elif isinstance(val.func, ast.Attribute):
                    return_types.add(val.func.attr)
    if len(return_types) == 1:
        conventions.append(f'returns:{return_types.pop()}')

    for node in ast.walk(method_node):
        if isinstance(node, ast.Return) and node.value:
            val = node.value
            if (isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Attribute)
                    and val.func.attr == 'copy'):
                conventions.append('clones_before_return')
                break

    if start_idx < len(body):
        first = body[start_idx]
        if isinstance(first, ast.If):
            test = first.test
            if isinstance(test, ast.Compare):
                for comp in test.comparators:
                    if isinstance(comp, ast.Constant) and comp.value is None:
                        conventions.append('normalizes_empty_input')
                        break
            elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                conventions.append('normalizes_empty_input')

    return conventions


def _parse_class(node, filepath):
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute) and isinstance(base.attr, str):
            bases.append(base.attr)

    methods = {}
    class_attrs = {}

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

            decorators = []
            for dec in item.decorator_list:
                if isinstance(dec, ast.Name):
                    decorators.append(dec.id)
                elif isinstance(dec, ast.Attribute):
                    decorators.append(dec.attr)
                elif isinstance(dec, ast.Call):
                    if isinstance(dec.func, ast.Name):
                        decorators.append(dec.func.id)
                    elif isinstance(dec.func, ast.Attribute):
                        decorators.append(dec.func.attr)

            methods[item.name] = {
                'line': item.lineno,
                'sig': _get_signature(item),
                'attrs': sorted(attrs),
                'calls': calls,
                'decorators': decorators,
                'attr_roles': _classify_attr_roles(item, item.name),
                'conventions': _classify_method_conventions(item),
            }
        elif isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    class_attrs[target.id] = {'line': item.lineno}
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            class_attrs[item.target.id] = {'line': item.lineno}
        elif isinstance(item, ast.ClassDef):
            class_attrs[item.name] = {'line': item.lineno, 'type': 'inner_class'}

    for mname, minfo in methods.items():
        if 'property' in minfo.get('decorators', []):
            class_attrs[mname] = {'line': minfo['line'], 'type': 'property'}

    if not methods and not class_attrs:
        return None

    return {
        'file': filepath,
        'line': node.lineno,
        'bases': bases,
        'methods': methods,
        'class_attrs': class_attrs,
    }


# ───────────────────────────────
# INDEXER — runs once, caches
# ───────────────────────────────

def _resolve_class_hierarchy(index):
    """Propagate inherited methods and attrs through the full class hierarchy (MRO)."""
    classes = index.get('classes', {})
    resolved = set()

    def resolve(cls_name, depth=0):
        if cls_name in resolved or depth > 15:
            return
        resolved.add(cls_name)
        locs = classes.get(cls_name, [])
        for loc in locs:
            for base_name in loc.get('bases', []):
                resolve(base_name, depth + 1)
                base_locs = classes.get(base_name, [])
                if base_locs:
                    base_methods = base_locs[0].get('methods', {})
                    base_attrs = set()
                    for bm_info in base_methods.values():
                        base_attrs.update(bm_info.get('attrs', []))
                    for mname, minfo in base_methods.items():
                        if mname not in loc['methods']:
                            loc['methods'][mname] = {
                                **minfo,
                                '_inherited_from': base_name,
                            }
                    for mname in loc['methods']:
                        if mname in base_methods and '_inherited_from' not in loc['methods'][mname]:
                            loc['methods'][mname]['_overrides'] = base_name

    for cls_name in list(classes.keys()):
        resolve(cls_name)


def build_index(repo_root):
    """Parse all Python source files into a structured index."""
    start = time.time()
    index = {
        'classes': {},
        'functions': {},
        'imports': {},
        'import_graph': {},
        'module_all': {},
        'references': {},
        'files_parsed': 0,
        'build_time': 0,
    }

    py_files = glob.glob(os.path.join(repo_root, '**', '*.py'), recursive=True)

    def _sort_key(fp):
        rel = os.path.relpath(fp, repo_root).lower()
        basename = os.path.basename(rel)
        if _is_test_file(rel):
            return (3, rel)
        if basename == '__init__.py':
            return (0, rel)
        if basename in ('models.py', 'views.py', 'forms.py', 'admin.py', 'urls.py',
                        'serializers.py', 'managers.py', 'fields.py', 'utils.py'):
            return (1, rel)
        return (2, rel)
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

        # Extract imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_names = []
                for alias in node.names:
                    name = alias.name
                    imported_names.append(name)
                    index['imports'].setdefault(rel, []).append(name)
                    index['references'].setdefault(name, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'import'
                    })
                index['import_graph'].setdefault(rel, []).append({
                    'from': node.module,
                    'names': imported_names,
                    'line': node.lineno,
                    'level': node.level or 0,
                })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name.split('.')[-1]
                    index['imports'].setdefault(rel, []).append(name)

        # Extract __all__
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == '__all__':
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            all_names = []
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    all_names.append(elt.value)
                            if all_names:
                                index['module_all'][rel] = all_names

        # Extract classes and functions (source files only)
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

        # Scan for name, attribute, and call references (all files)
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
                    if not fname[0].isupper() and not fname.isupper():
                        index['references'].setdefault(fname, []).append({
                            'file': rel, 'line': node.lineno, 'type': 'call'
                        })
                if (isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Call)
                        and isinstance(node.func.value.func, ast.Name)
                        and node.func.value.func.id == 'super'):
                    method = node.func.attr
                    if method and len(method) > 2:
                        index['references'].setdefault(method, []).append({
                            'file': rel, 'line': node.lineno, 'type': 'super_call'
                        })

        # Time budget
        if time.time() - start > MAX_INDEX_TIME:
            index['truncated'] = True
            index['total_py_files'] = len(py_files)
            break

    index['build_time'] = round(time.time() - start, 2)
    index['truncated'] = index.get('truncated', False)

    _resolve_class_hierarchy(index)

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


# ───────────────────────────────
# CHECK COMMAND
# ───────────────────────────────

def _run_pyright_diagnostics(modified_files):
    """Run Pyright on modified files. Returns [(severity, filepath, line, msg)]. Graceful degradation."""
    full_paths = [os.path.join(REPO_ROOT, f) for f in modified_files if os.path.exists(os.path.join(REPO_ROOT, f))]
    if not full_paths:
        return []
    try:
        result = subprocess.run(
            ["pyright", "--outputjson"] + full_paths,
            capture_output=True, text=True, timeout=30,
            cwd=REPO_ROOT,
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


def _get_sibling_patterns(dir_path, exclude_file):
    """Analyze sibling Python files for common patterns."""
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


def cmd_check():
    """Check edit completeness against git diff — validates multiple error classes."""
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

    all_issues = []  # (severity, filepath, line, message)

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
                                               f"{node.name}.{mname}: self.{attr} not in __init__ (may be intentional — do not revise unless clearly wrong)"))
                            break

            # Check 2: self.method() calls to methods not in class or bases
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

        # Check 3: Imports — verify imported names exist in target module
        all_known_names = set()
        for cls_name in index.get('classes', {}):
            all_known_names.add(cls_name)
        for func_name in index.get('functions', {}):
            all_known_names.add(func_name)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module_path = node.module
                for alias in node.names:
                    name = alias.name
                    if name == '*' or len(name) <= 2:
                        continue
                    if name not in all_known_names:
                        if node.level and node.level > 0:
                            all_issues.append(("INFO", filepath, node.lineno,
                                               f"Import '{name}' from {module_path} not in index (may be intentional — do not revise unless clearly wrong)"))

    # Check 4: Contradiction detection — compare modified file patterns against siblings
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

        # 4a: Check method override signatures against base class
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
                                                   f"{node.name}.{item.name}{curr_sig} vs base {base_name}.{item.name}{base_sig} (may be intentional — do not revise unless clearly wrong)"))

        # 4b: Check error handling patterns against siblings
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
                                               f"Unusual exception {exc_name} — siblings use: {common} (may be intentional — do not revise unless clearly wrong)"))

    # Check 5: Pyright diagnostics (optional, graceful degradation)
    pyright_issues = _run_pyright_diagnostics(modified_files)
    all_issues.extend(pyright_issues)

    if not all_issues:
        print(f"All {len(modified_files)} file(s) pass checks")
        return

    # Sort ERROR before INFO, print top 5
    severity_order = {"ERROR": 0, "INFO": 1}
    all_issues.sort(key=lambda x: (severity_order.get(x[0], 2), x[1], x[2]))
    for severity, fpath, line, msg in all_issues[:5]:
        print(f"[{severity}] {fpath}:{line} — {msg}")


# ───────────────────────────────
# MAIN
# ───────────────────────────────

if __name__ == '__main__':
    try:
        if len(sys.argv) < 2:
            print("Usage: python3 gt_tool_check_only.py groundtruth_check")
            print("       python3 gt_tool_check_only.py check")
            sys.exit(0)

        command = sys.argv[1].lower()

        repo = os.environ.get('GT_REPO', REPO_ROOT)
        REPO_ROOT = repo

        if command in ('groundtruth_check', 'check'):
            cmd_check()
        elif command in ('help', '--help', '-h'):
            print("GroundTruth — Check-Only Version")
            print()
            print("Usage:")
            print("  python3 gt_tool_check_only.py groundtruth_check   — Completeness check against git diff")
            print("  python3 gt_tool_check_only.py check               — (alias)")
        else:
            print(f"Unknown command: {command}")
            print("This is the check-only version. Use: groundtruth_check or check")
            sys.exit(1)
    except (MemoryError, RecursionError) as e:
        print(f"GT tool error ({type(e).__name__}). Use grep/find instead.")
        sys.exit(1)
    except Exception as e:
        print(f"GT tool error: {e}")
        print("Fallback: use grep/find for this query.")
        sys.exit(1)

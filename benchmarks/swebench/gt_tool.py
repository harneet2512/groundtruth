#!/usr/bin/env python3
"""
GroundTruth — On-Demand Codebase Intelligence (v8 + Phase 3)

Usage inside SWE-bench container:
  Pattern-Aware (Phase 3):
    python3 /tmp/gt_tool.py groundtruth_references <Symbol>  — Find all usages + definition pointer
    python3 /tmp/gt_tool.py groundtruth_impact <Symbol>      — Obligation sites + conventions + subclass overrides
    python3 /tmp/gt_tool.py groundtruth_check                — Completeness check against git diff

  Exploration:
    python3 /tmp/gt_tool.py summary                — Quick codebase overview
    python3 /tmp/gt_tool.py references <Symbol>    — Find all usages (supports Class.method)
    python3 /tmp/gt_tool.py impact <Symbol>        — What breaks if you change this?
    python3 /tmp/gt_tool.py scope <Symbol>         — Which files need editing?
    python3 /tmp/gt_tool.py obligations <Symbol>   — What MUST change if you modify this?
    python3 /tmp/gt_tool.py context <Symbol>       — Show usage code snippets
    python3 /tmp/gt_tool.py related <file_path>    — Find files via shared symbols

  Validation:
    python3 /tmp/gt_tool.py check                  — Verify completeness + contradictions
    python3 /tmp/gt_tool.py diagnose <file_path>   — Syntax + undefined + overrides

Features: Full MRO inheritance, import graph, __all__ tracking, decorator awareness,
class-level attributes, transitive scope, contradiction detection, pattern-aware obligations.
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
MAX_INDEX_TIME = 30  # seconds (20 was still tight for large Django repos)

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
        'import_graph': {},  # file -> [{from: module, names: [names], line: N}]
        'module_all': {},    # file -> [names in __all__]
        'references': {},    # symbol_name -> [{file, line, context}]
        'files_parsed': 0,
        'build_time': 0,
    }

    py_files = glob.glob(os.path.join(repo_root, '**', '*.py'), recursive=True)

    # Prioritize: __init__.py first (public API), then source files, then tests
    def _sort_key(fp):
        rel = os.path.relpath(fp, repo_root).lower()
        basename = os.path.basename(rel)
        if _is_test_file(rel):
            return (3, rel)
        if basename == '__init__.py':
            return (0, rel)  # __init__.py defines re-exports / public API
        if basename in ('models.py', 'views.py', 'forms.py', 'admin.py', 'urls.py',
                        'serializers.py', 'managers.py', 'fields.py', 'utils.py'):
            return (1, rel)  # Core Django patterns
        return (2, rel)
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
                imported_names = []
                for alias in node.names:
                    name = alias.name
                    imported_names.append(name)
                    index['imports'].setdefault(rel, []).append(name)
                    # Track as a reference
                    index['references'].setdefault(name, []).append({
                        'file': rel, 'line': node.lineno, 'type': 'import'
                    })
                # Build import graph entry with source module
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

        # Extract __all__ if defined (all files)
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
            # Direct function calls: func_name(...) — track all non-builtin calls
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and len(node.func.id) > 2:
                    fname = node.func.id
                    if not fname[0].isupper() and not fname.isupper():
                        # Any lowercase function call (not a class constructor, not ALL_CAPS constant)
                        index['references'].setdefault(fname, []).append({
                            'file': rel, 'line': node.lineno, 'type': 'call'
                        })
                # Track super().method() calls as references to parent methods
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

    # Resolve class hierarchy: propagate base class methods/attrs to subclasses
    _resolve_class_hierarchy(index)

    # Cache
    with open(INDEX_CACHE, 'w') as f:
        json.dump(index, f)

    return index


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
                # Resolve base first (recursive)
                resolve(base_name, depth + 1)
                base_locs = classes.get(base_name, [])
                if base_locs:
                    base_methods = base_locs[0].get('methods', {})
                    base_attrs = set()
                    for bm_info in base_methods.values():
                        base_attrs.update(bm_info.get('attrs', []))
                    # Add inherited methods that aren't overridden
                    for mname, minfo in base_methods.items():
                        if mname not in loc['methods']:
                            loc['methods'][mname] = {
                                **minfo,
                                '_inherited_from': base_name,
                            }
                    # Mark which methods are overrides
                    for mname in loc['methods']:
                        if mname in base_methods and '_inherited_from' not in loc['methods'][mname]:
                            loc['methods'][mname]['_overrides'] = base_name

    for cls_name in list(classes.keys()):
        resolve(cls_name)


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
                    '/fixtures/']
    # NOTE: /migrations/ deliberately NOT excluded — Django migration files
    # contain schema definitions and model references needed by the index
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
    class_attrs = {}  # class-level attributes (field definitions, Meta, etc.)

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

            # Track decorators
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
            # Class-level assignments (e.g., field = CharField(...), Meta, objects)
            for target in item.targets:
                if isinstance(target, ast.Name):
                    class_attrs[target.id] = {'line': item.lineno}
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            # Annotated class-level assignments
            class_attrs[item.target.id] = {'line': item.lineno}
        elif isinstance(item, ast.ClassDef):
            # Inner class (e.g., Meta)
            class_attrs[item.name] = {'line': item.lineno, 'type': 'inner_class'}

    # Also track @property methods as pseudo-attributes (accessed as obj.prop)
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
    # Add return type annotation if present
    if hasattr(func_node, 'returns') and func_node.returns:
        try:
            ret = ast.unparse(func_node.returns)
            if len(ret) < 40:  # Cap for readability
                sig += f" -> {ret}"
        except (ValueError, AttributeError):
            pass
    return sig


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


def _classify_attr_roles(method_node, method_name):
    """For each self.X in a method, classify by AST context (how the attr is used)."""
    roles = {}  # attr_name -> set of roles

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
            grandparent = self._parent(2)

            # stores_in_state: self.X on left side of assignment
            if isinstance(node.ctx, ast.Store):
                roles[attr].add('stores_in_state')
                return

            # compares_in_eq: inside __eq__/__hash__/__ne__ or in ast.Compare
            if method_name in ('__eq__', '__hash__', '__ne__'):
                roles[attr].add('compares_in_eq')
            elif isinstance(parent, ast.Compare):
                roles[attr].add('compares_in_eq')

            # serializes_to_kwargs: value in dict, element in tuple/list, or keyword arg
            if isinstance(parent, ast.Dict):
                roles[attr].add('serializes_to_kwargs')
            elif isinstance(parent, (ast.Tuple, ast.List)):
                roles[attr].add('serializes_to_kwargs')
            elif isinstance(parent, ast.keyword):
                roles[attr].add('serializes_to_kwargs')

            # emits_to_output: in f-string or format call
            if isinstance(parent, ast.FormattedValue):
                roles[attr].add('emits_to_output')
            elif isinstance(parent, ast.JoinedStr):
                roles[attr].add('emits_to_output')
            # % format or .format() call
            if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Mod):
                roles[attr].add('emits_to_output')

            # passes_to_validator: arg in call to non-self function
            if isinstance(parent, ast.Call) and not self._is_self_attr(parent.func if hasattr(parent, 'func') else parent):
                if hasattr(parent, 'func'):
                    func = parent.func
                    if not (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == 'self'):
                        roles[attr].add('passes_to_validator')

            # reads_in_logic: test of if/while/assert, or in BoolOp
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

    # Convert sets to sorted lists for JSON serialization
    return {k: sorted(v) for k, v in roles.items() if v}


def _classify_method_conventions(method_node):
    """Per-method convention detection."""
    conventions = []

    body = method_node.body
    # Skip docstring
    start_idx = 0
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        start_idx = 1

    # guards_on_state: first non-docstring stmt is if ... raise
    if start_idx < len(body):
        first = body[start_idx]
        if isinstance(first, ast.If):
            # Check if any branch has a Raise
            for child in ast.walk(first):
                if isinstance(child, ast.Raise):
                    conventions.append('guards_on_state')
                    break

    # raises:<ExcType>: method raises specific exception
    for node in ast.walk(method_node):
        if isinstance(node, ast.Raise) and node.exc:
            exc = node.exc
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                conventions.append(f'raises:{exc.func.id}')
            elif isinstance(exc, ast.Name):
                conventions.append(f'raises:{exc.id}')

    # returns:<type>: check return values for common container types
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

    # clones_before_return: return value is .copy() call
    for node in ast.walk(method_node):
        if isinstance(node, ast.Return) and node.value:
            val = node.value
            if (isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Attribute)
                    and val.func.attr == 'copy'):
                conventions.append('clones_before_return')
                break

    # normalizes_empty_input: first stmt checks for None/empty
    if start_idx < len(body):
        first = body[start_idx]
        if isinstance(first, ast.If):
            test = first.test
            # Check for `if x is None` or `if not x`
            if isinstance(test, ast.Compare):
                for comp in test.comparators:
                    if isinstance(comp, ast.Constant) and comp.value is None:
                        conventions.append('normalizes_empty_input')
                        break
            elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                conventions.append('normalizes_empty_input')

    return conventions


def _compute_obligation_groups(cls_info):
    """Find methods sharing self.X attrs — these are obligation groups."""
    methods = cls_info.get('methods', {})
    attr_to_methods = defaultdict(set)

    for mname, minfo in methods.items():
        if mname.startswith('_') and mname not in ('__init__', '__eq__', '__hash__', '__ne__', '__repr__', '__str__'):
            continue
        for attr in minfo.get('attrs', []):
            attr_to_methods[attr].add(mname)

    # Filter to attrs used by 2+ methods (shared state)
    shared_attrs = {attr: meths for attr, meths in attr_to_methods.items()
                    if len(meths) >= 2}

    # Group methods by shared attr sets
    groups = []
    for attr, meths in sorted(shared_attrs.items(), key=lambda x: -len(x[1])):
        groups.append({
            'attr': attr,
            'methods': sorted(meths),
            'count': len(meths),
        })

    return groups


def _role_confidence(role):
    """Confidence based on role type: structural roles are HIGH, logic reads are MED."""
    HIGH_ROLES = {'serializes_to_kwargs', 'compares_in_eq', 'stores_in_state', 'emits_to_output'}
    MED_ROLES = {'passes_to_validator', 'reads_in_logic'}
    if role in HIGH_ROLES:
        return 'HIGH'
    if role in MED_ROLES:
        return 'MED'
    return 'LOW'


def _best_role_for_method(minfo, attr):
    """Pick the highest-confidence role for an attr in a method."""
    roles = minfo.get('attr_roles', {}).get(attr, [])
    if not roles:
        return None, 'LOW'
    # Sort by confidence: HIGH first
    best_role = None
    best_conf = 'LOW'
    priority = {'HIGH': 0, 'MED': 1, 'LOW': 2}
    for role in roles:
        conf = _role_confidence(role)
        if priority.get(conf, 3) < priority.get(best_conf, 3):
            best_conf = conf
            best_role = role
    if best_role is None:
        best_role = roles[0]
    return best_role, best_conf


def _detect_class_conventions(cls_info):
    """Class-level patterns from method conventions."""
    methods = cls_info.get('methods', {})
    conventions = []

    # Count public methods
    public_methods = [m for m in methods if not m.startswith('_')]
    if not public_methods:
        return conventions

    # Guard clause: >70% public methods have guards_on_state
    guard_count = sum(1 for m in public_methods
                      if 'guards_on_state' in methods[m].get('conventions', []))
    if len(public_methods) >= 3 and guard_count / len(public_methods) > 0.7:
        conventions.append({
            'pattern': 'guard_clause',
            'confidence': 'HIGH',
            'detail': f'{guard_count}/{len(public_methods)} public methods start with guard clause',
        })

    # Error type: >70% raise sites use same exception
    raise_types = defaultdict(int)
    for mname, minfo in methods.items():
        for conv in minfo.get('conventions', []):
            if conv.startswith('raises:'):
                exc_type = conv.split(':', 1)[1]
                raise_types[exc_type] += 1
    total_raises = sum(raise_types.values())
    if total_raises >= 3:
        top_exc, top_count = max(raise_types.items(), key=lambda x: x[1])
        if top_count / total_raises > 0.7:
            conventions.append({
                'pattern': 'error_type',
                'confidence': 'HIGH' if top_count / total_raises > 0.9 else 'MED',
                'detail': f'{top_count}/{total_raises} raise sites use {top_exc}',
            })

    # Return shape: >70% same-prefix methods return same type
    return_types = defaultdict(int)
    for mname, minfo in methods.items():
        for conv in minfo.get('conventions', []):
            if conv.startswith('returns:'):
                ret_type = conv.split(':', 1)[1]
                return_types[ret_type] += 1
    total_returns = sum(return_types.values())
    if total_returns >= 3:
        top_ret, top_count = max(return_types.items(), key=lambda x: x[1])
        if top_count / total_returns > 0.7:
            conventions.append({
                'pattern': 'return_shape',
                'confidence': 'MED',
                'detail': f'{top_count}/{total_returns} methods return {top_ret}',
            })

    # Empty input normalization: >70% public methods normalize None/empty
    normalize_count = sum(1 for m in public_methods
                          if 'normalizes_empty_input' in methods[m].get('conventions', []))
    if len(public_methods) >= 3 and normalize_count / len(public_methods) > 0.7:
        conventions.append({
            'pattern': 'normalizes_empty_input',
            'confidence': 'MED',
            'detail': f'{normalize_count}/{len(public_methods)} public methods normalize empty input',
        })

    return conventions


def _count_required_args(func_node):
    """Count required (non-default) args, excluding self/cls."""
    args = func_node.args
    num_defaults = len(args.defaults)
    num_args = len(args.args)
    required = 0
    for i, arg in enumerate(args.args):
        if arg.arg in ('self', 'cls'):
            continue
        default_idx = i - (num_args - num_defaults)
        if default_idx < 0:
            required += 1
    return required


def _count_required_args_from_sig(sig_str):
    """Estimate required args from a signature string like '(a, b, c=None)'."""
    if not sig_str or sig_str == '()':
        return 0
    # Strip parens
    inner = sig_str.strip('()')
    if not inner:
        return 0
    parts = [p.strip() for p in inner.split(',')]
    required = 0
    for p in parts:
        if '=' not in p and p not in ('*', '**'):
            required += 1
    return required


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
        # Suggest close matches (max 3, one line each)
        sym_lower = symbol.lower()
        seen_names = set()
        suggestions = []
        for src in (index.get('references', {}), index.get('classes', {}), index.get('functions', {})):
            for k, v in src.items():
                if k in seen_names:
                    continue
                if sym_lower in k.lower() or k.lower() in sym_lower:
                    seen_names.add(k)
                    first_file = (v[0]['file'] if v and isinstance(v[0], dict) and 'file' in v[0] else '?')
                    suggestions.append(f"{k} in {first_file}")
                if len(suggestions) >= 3:
                    break
        if suggestions:
            print(f"'{symbol}' not found. Did you mean: {' | '.join(suggestions)}")
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

    # Build flat list of (sort_key, label, text) tuples
    entries = []
    cls_defs = index.get('classes', {}).get(symbol, [])
    func_defs = index.get('functions', {}).get(symbol, [])

    for filepath in sorted(by_file.keys()):
        file_refs = sorted(by_file[filepath], key=lambda r: r['line'])
        has_def = any(r['type'] in ('method_def', 'import') for r in file_refs)
        is_test = _is_test_file(filepath)
        lines_str = ','.join(str(r['line']) for r in file_refs[:3])
        ref_count = len(file_refs)

        if has_def:
            # Build evidence from class/func signature
            evidence = ""
            if cls_defs:
                for loc in cls_defs:
                    if loc['file'] == filepath:
                        bases = f" < {', '.join(loc['bases'])}" if loc['bases'] else ""
                        evidence = f"class{bases}, {len(loc['methods'])} methods"
                        break
            if not evidence and func_defs:
                for loc in func_defs:
                    if loc['file'] == filepath:
                        evidence = f"def{loc['sig']}"
                        break
            if not evidence:
                evidence = "definition"
            entries.append((0, "[DEF]", f"{filepath}:{lines_str} — {evidence}"))
        elif is_test:
            entries.append((2, "[TEST]", f"{filepath}:{lines_str} — {ref_count} refs"))
        else:
            entries.append((1, "[USE]", f"{filepath}:{lines_str} — {ref_count} refs"))

    # Sort: DEF first, USE by position, TEST last
    entries.sort(key=lambda e: e[0])

    # Print top 5 only
    for _, label, text in entries[:5]:
        print(f"{label} {text}")


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
                cattrs = loc.get('class_attrs', {})
                n_methods = len(loc.get('methods', {}))
                stats = f" [{n_methods} methods"
                if cattrs:
                    stats += f", {len(cattrs)} attrs"
                stats += "]"
                print(f"  class {class_name}{bases_str}{stats} - line {loc['line']}")
                for mname, minfo in sorted(loc['methods'].items(), key=lambda x: x[1]['line']):
                    dec_str = ""
                    if minfo.get('decorators'):
                        dec_str = f" @{','.join(minfo['decorators'])}"
                    inherited_str = ""
                    if minfo.get('_inherited_from'):
                        inherited_str = f" [from {minfo['_inherited_from']}]"
                    print(f"    {mname}{minfo['sig']}{dec_str}{inherited_str} — line {minfo['line']}")

    # Find module-level functions
    for func_name, locations in index.get('functions', {}).items():
        for loc in locations:
            if _path_match(filepath, loc['file']):
                if not found:
                    print(f"Outline of {loc['file']}:\n")
                    found = True
                print(f"  def {func_name}{loc['sig']} — line {loc['line']}")

    # Show __all__ if defined
    for f, all_names in index.get('module_all', {}).items():
        if _path_match(filepath, f):
            if not found:
                print(f"Outline of {f}:\n")
                found = True
            print(f"  __all__ = {all_names}")

    if not found:
        print(f"No symbols found in '{filepath}'")
        print("Hint: use a partial path (e.g., 'constraints.py' instead of full path)")


def cmd_impact(index, symbol):
    """Compact impact analysis: definition, inheritance, and files that need updating."""
    cls_locations = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])
    refs = index.get('references', {}).get(symbol, [])

    if not cls_locations and not func_locs:
        candidates = [k for k in list(index.get('classes', {}).keys()) + list(index.get('functions', {}).keys())
                      if symbol.lower() in k.lower() or k.lower() in symbol.lower()]
        if candidates:
            print(f"'{symbol}' not found. Similar: {', '.join(candidates[:3])}")
        else:
            print(f"'{symbol}' not found.")
        return

    entries = []

    # Line 1: definition
    if cls_locations:
        loc = cls_locations[0]
        bases_str = f"({', '.join(loc['bases'])})" if loc['bases'] else ""
        n_methods = len(loc['methods'])
        cattrs = loc.get('class_attrs', {})
        n_attrs = len(cattrs)
        entries.append(f"[DIRECT] {loc['file']}:{loc['line']} — {symbol}{bases_str}, {n_methods} methods, {n_attrs} attrs")
    if func_locs:
        loc = func_locs[0]
        entries.append(f"[DIRECT] {loc['file']}:{loc['line']} — {symbol}{loc['sig']}")

    # Lines 2-4: top external files
    if refs:
        by_file = defaultdict(list)
        for ref in refs:
            by_file[ref['file']].append(ref)
        def_files = {loc['file'] for loc in cls_locations} if cls_locations else set()
        if func_locs:
            def_files.update(loc['file'] for loc in func_locs)
        external = sorted(
            ((f, r) for f, r in by_file.items() if f not in def_files),
            key=lambda x: -len(x[1])
        )
        for fp, file_refs in external[:3]:
            lines = sorted(set(r['line'] for r in file_refs))[:3]
            entries.append(f"[DIRECT] {fp}:{','.join(str(l) for l in lines)} — {len(file_refs)} refs")

    # Line 5: subclass info
    if cls_locations:
        subclass_count = 0
        for other_cls, other_locs in index.get('classes', {}).items():
            for oloc in other_locs:
                if symbol in oloc.get('bases', []):
                    subclass_count += 1
        if subclass_count > 0:
            entries.append(f"[TRANSITIVE] {subclass_count} subclasses — changes may need override updates")

    for entry in entries[:5]:
        print(entry)


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

    # 4. For classes, also include files that reference Class.method patterns
    if cls_locs:
        for loc in cls_locs:
            for method_name in loc.get('methods', {}):
                qualified = f"{symbol}.{method_name}"
                for ref in index.get('references', {}).get(qualified, []):
                    f = ref['file']
                    if f not in files:
                        files[f] = (50, f'uses .{method_name}', [ref['line']])
                    elif files[f][0] < 100:
                        old_score, old_reason, old_lines = files[f]
                        if ref['line'] not in old_lines:
                            old_lines.append(ref['line'])
                        files[f] = (min(old_score + 5, 99), old_reason, old_lines)

    # 5. Transitive: files that import from files that directly use the symbol
    #    (one level of transitivity — catches cascade dependencies)
    direct_files = set(files.keys())
    import_graph = index.get('import_graph', {})
    for file_path, imports in import_graph.items():
        if file_path in files:
            continue  # Already included
        for imp in imports:
            # Check if any imported name comes from a direct-use file
            from_module = imp.get('from', '')
            for df in direct_files:
                # Match module path to file path (heuristic: module.submod - dir/submod.py)
                df_module = df.replace(os.sep, '.').replace('/', '.').rstrip('.py').replace('.__init__', '')
                if from_module and (from_module.endswith(df_module.split('.')[-1]) or df_module.endswith(from_module.split('.')[-1])):
                    files[file_path] = (30, f'imports from {df}', [imp['line']])
                    break

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

    for filepath, (score, reason, lines) in ranked[:5]:
        line_str = ':' + ','.join(str(l) for l in sorted(lines)[:3]) if lines else ''
        if score >= 90:
            priority = "MUST"
        elif score >= 60:
            priority = "SHOULD"
        else:
            priority = "CHECK"
        print(f"[{priority}] {filepath}{line_str} ({reason})")


def cmd_search(index, pattern):
    """Search for pattern across indexed source files. Faster, smarter grep."""
    results = []
    pattern_lower = pattern.lower()

    # First: search symbol names in the index (instant)
    for name, locs in index.get('classes', {}).items():
        if pattern_lower in name.lower():
            for loc in locs:
                results.append((loc['file'], loc['line'], f"class {name}"))
        # Also search method names within classes
        for loc in locs:
            for mname, minfo in loc.get('methods', {}).items():
                if pattern_lower in mname.lower():
                    results.append((loc['file'], minfo['line'], f"{name}.{mname}{minfo['sig']}"))
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

    for filepath, line, context in unique[:5]:
        print(f"{filepath}:{line} — {context}")


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
                            if not base_sig or curr_sig == base_sig:
                                continue
                            # Count required args (non-default, non-self/cls)
                            curr_required = _count_required_args(item)
                            base_required = _count_required_args_from_sig(base_sig)
                            if base_required is not None and curr_required < base_required:
                                print(f"\n  [ERROR] {node.name}.{item.name}{curr_sig} has fewer required args "
                                      f"than {base_name}.{item.name}{base_sig}")
                            elif base_sig and curr_sig != base_sig:
                                print(f"\n  [WARN] {node.name}.{item.name}{curr_sig} overrides {base_name}.{item.name}{base_sig}")

    # 4. Check for duplicate method definitions within classes
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            method_names = {}  # name -> first line
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name in method_names:
                        print(f"\n  [ERROR] Duplicate method: {node.name}.{item.name} "
                              f"defined at lines {method_names[item.name]} and {item.lineno}")
                    else:
                        method_names[item.name] = item.lineno


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


def _get_sibling_patterns(dir_path, exclude_file):
    """Analyze sibling Python files for common patterns."""
    patterns = {'exception_types': set()}
    try:
        siblings = [f for f in os.listdir(dir_path)
                     if f.endswith('.py') and os.path.join(dir_path, f) != exclude_file
                     and not f.startswith('test_')]
    except OSError:
        return patterns

    for sib in siblings[:10]:  # Cap at 10 siblings
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


def cmd_related(index, filepath):
    """Find files related to this one via shared symbols and imports.

    Starts from a FILE (not a symbol) and finds siblings that share
    imports, classes, or function references.
    """
    _warn_if_truncated(index)

    # Normalize path
    target = None
    for cls_name, locs in index.get('classes', {}).items():
        for loc in locs:
            if _path_match(filepath, loc['file']):
                target = loc['file']
                break
        if target:
            break
    if not target:
        for func_name, locs in index.get('functions', {}).items():
            for loc in locs:
                if _path_match(filepath, loc['file']):
                    target = loc['file']
                    break
            if target:
                break
    if not target:
        # Try import graph
        for f in index.get('import_graph', {}):
            if _path_match(filepath, f):
                target = f
                break
    if not target:
        print(f"'{filepath}' not found in index. Try a partial path.")
        return

    # Collect symbols defined in and imported by target file
    target_symbols = set()
    # Symbols defined in target
    for cls_name, locs in index.get('classes', {}).items():
        for loc in locs:
            if loc['file'] == target:
                target_symbols.add(cls_name)
    for func_name, locs in index.get('functions', {}).items():
        for loc in locs:
            if loc['file'] == target:
                target_symbols.add(func_name)
    # Symbols imported by target
    for name in index.get('imports', {}).get(target, []):
        target_symbols.add(name)

    if not target_symbols:
        print(f"No symbols found in '{target}'")
        return

    # Find files that share symbols with target
    related_files = {}  # file -> (overlap_count, shared_symbols)
    for sym in target_symbols:
        for ref in index.get('references', {}).get(sym, []):
            f = ref['file']
            if f == target:
                continue
            if f not in related_files:
                related_files[f] = (0, set())
            count, shared = related_files[f]
            shared.add(sym)
            related_files[f] = (len(shared), shared)

    if not related_files:
        print(f"No related files found for '{target}'")
        return

    # Sort by overlap count
    ranked = sorted(related_files.items(), key=lambda x: -x[1][0])

    print(f"Files related to '{target}' ({len(ranked)} files, {len(target_symbols)} symbols):")
    for f, (count, shared) in ranked[:15]:
        shared_preview = ', '.join(sorted(shared)[:3])
        more = f"+{len(shared) - 3}" if len(shared) > 3 else ""
        print(f"  {f} ({count} shared: {shared_preview}{more})")
    if len(ranked) > 15:
        print(f"  +{len(ranked) - 15} more")


def cmd_context(index, symbol):
    """Show usage PATTERNS of a symbol — actual code snippets showing how it's called.

    Unlike references (which shows files), context shows the concrete code patterns.
    Limited to 10 snippets for token efficiency.
    """
    _warn_if_truncated(index)
    refs = index.get('references', {}).get(symbol, [])

    # Fallback for Class.method
    if not refs and '.' in symbol:
        cls_name, method_name = symbol.rsplit('.', 1)
        refs = index.get('references', {}).get(method_name, [])

    if not refs:
        print(f"No usage context found for '{symbol}'")
        return

    # Deduplicate and pick diverse files
    seen_files = set()
    selected_refs = []
    for ref in refs:
        if ref['file'] not in seen_files and ref.get('type') in ('call', 'attr_access', 'usage'):
            seen_files.add(ref['file'])
            selected_refs.append(ref)
        if len(selected_refs) >= 10:
            break

    if not selected_refs:
        # Fallback: use any ref type
        seen_files = set()
        for ref in refs:
            if ref['file'] not in seen_files:
                seen_files.add(ref['file'])
                selected_refs.append(ref)
            if len(selected_refs) >= 10:
                break

    print(f"Usage patterns for '{symbol}' ({len(selected_refs)} examples):\n")
    for ref in selected_refs:
        full_path = os.path.join(REPO_ROOT, ref['file'])
        line_num = ref.get('line', 0)
        if not os.path.exists(full_path) or line_num <= 0:
            continue
        try:
            with open(full_path, 'r', errors='replace') as f:
                lines = f.readlines()
            # Show 2 lines before and 1 line after for context
            start = max(0, line_num - 3)
            end = min(len(lines), line_num + 2)
            snippet = ''.join(lines[start:end]).rstrip()
            # Truncate long snippets
            if len(snippet) > 200:
                snippet = snippet[:200] + '...'
            print(f"  {ref['file']}:{line_num}")
            for sl in snippet.split('\n'):
                print(f"    {sl}")
            print()
        except OSError:
            continue


def cmd_obligations(index, symbol):
    """Infer change obligations: if you change this symbol, what SPECIFICALLY must change elsewhere?

    Goes beyond scope (which just lists files) to identify concrete obligations:
    - Callers that pass positional args (must update arg order/count)
    - Subclasses that override this method (must update override signature)
    - Files that import specific names (must update import if renamed)
    """
    _warn_if_truncated(index)

    # Resolve symbol to class or function
    cls_locs = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])

    # Handle Class.method notation
    method_info = None
    parent_class = None
    if '.' in symbol:
        cls_name, method_name = symbol.rsplit('.', 1)
        for loc in index.get('classes', {}).get(cls_name, []):
            if method_name in loc.get('methods', {}):
                method_info = loc['methods'][method_name]
                parent_class = cls_name
                break

    if not cls_locs and not func_locs and not method_info:
        print(f"'{symbol}' not found. Try: references {symbol}")
        return

    obligations = []

    # 1. For methods: find subclass overrides
    if cls_locs:
        for other_cls, other_locs in index.get('classes', {}).items():
            if other_cls == symbol:
                continue
            for oloc in other_locs:
                if symbol in oloc.get('bases', []):
                    # This class inherits from our target
                    overrides = []
                    target_methods = cls_locs[0].get('methods', {})
                    for mname in oloc.get('methods', {}):
                        if mname in target_methods and not mname.startswith('__'):
                            overrides.append(mname)
                    if overrides:
                        obligations.append({
                            'file': oloc['file'],
                            'type': 'override',
                            'detail': f"{other_cls} overrides: {', '.join(overrides)}",
                            'priority': 'HIGH',
                        })
                    else:
                        obligations.append({
                            'file': oloc['file'],
                            'type': 'subclass',
                            'detail': f"{other_cls} inherits from {symbol}",
                            'priority': 'MEDIUM',
                        })

    # 2. For methods: find callers and their call patterns
    search_names = [symbol]
    if parent_class:
        search_names = [symbol, method_info and symbol.split('.')[-1]]
    elif cls_locs:
        # Add method names from the class
        for loc in cls_locs:
            for mname in loc.get('methods', {}):
                search_names.append(f"{symbol}.{mname}")

    seen_files = set()
    for sname in search_names:
        if not sname:
            continue
        for ref in index.get('references', {}).get(sname, []):
            if ref.get('type') in ('call', 'attr_access') and ref['file'] not in seen_files:
                seen_files.add(ref['file'])
                obligations.append({
                    'file': ref['file'],
                    'line': ref['line'],
                    'type': 'caller',
                    'detail': f"Calls {sname} at line {ref['line']}",
                    'priority': 'HIGH' if ref.get('type') == 'call' else 'MEDIUM',
                })

    # 3. Import obligations — files that import this name directly
    for file_path, imports in index.get('import_graph', {}).items():
        for imp in imports:
            if symbol in imp.get('names', []) or (parent_class and parent_class in imp.get('names', [])):
                if file_path not in seen_files:
                    seen_files.add(file_path)
                    target = parent_class or symbol
                    obligations.append({
                        'file': file_path,
                        'line': imp['line'],
                        'type': 'import',
                        'detail': f"Imports {target} from {imp['from']}",
                        'priority': 'LOW',
                    })

    if not obligations:
        print(f"No change obligations found for '{symbol}' (symbol may be internal-only)")
        return

    # Sort: HIGH first, then MEDIUM, then LOW
    priority_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    obligations.sort(key=lambda o: (priority_order.get(o['priority'], 3), o['file']))

    for ob in obligations[:5]:
        line_str = f":{ob['line']}" if 'line' in ob else ""
        print(f"[{ob['priority']}] {ob['file']}{line_str} — {ob['detail']}")


def cmd_summary(index):
    """Quick codebase overview: package structure, key classes, key functions."""
    _warn_if_truncated(index)

    classes = index.get('classes', {})
    functions = index.get('functions', {})
    files_parsed = index.get('files_parsed', 0)
    build_time = index.get('build_time', 0)

    print(f"Codebase: {files_parsed} files indexed in {build_time}s")
    print(f"  {len(classes)} classes, {len(functions)} functions\n")

    # Package structure: group files by top-level directory
    all_files = set()
    for locs in classes.values():
        for loc in locs:
            all_files.add(loc['file'])
    for locs in functions.values():
        for loc in locs:
            all_files.add(loc['file'])

    packages = defaultdict(int)
    for f in sorted(all_files):
        parts = f.replace('\\', '/').split('/')
        pkg = parts[0] if len(parts) > 1 else '(root)'
        packages[pkg] += 1

    if packages:
        print("Packages:")
        for pkg, count in sorted(packages.items(), key=lambda x: -x[1])[:10]:
            print(f"  {pkg}/ ({count} files)")

    # Key classes (most methods = most important)
    if classes:
        top_classes = sorted(
            [(name, locs[0]) for name, locs in classes.items() if locs],
            key=lambda x: len(x[1].get('methods', {})),
            reverse=True,
        )[:10]
        print(f"\nKey classes (by method count):")
        for name, loc in top_classes:
            n_methods = len(loc.get('methods', {}))
            bases_str = f" < {', '.join(loc['bases'])}" if loc.get('bases') else ""
            print(f"  {name}{bases_str} ({n_methods} methods) @ {loc['file']}:{loc['line']}")

    # Most-referenced symbols
    refs = index.get('references', {})
    if refs:
        top_refs = sorted(
            [(name, len(r)) for name, r in refs.items()
             if not name.startswith('_') and '.' not in name and len(name) > 2],
            key=lambda x: -x[1],
        )[:10]
        if top_refs:
            print(f"\nMost-referenced symbols:")
            for name, count in top_refs:
                print(f"  {name} ({count} refs)")


def cmd_diff(index):
    """Show git diff with GT annotations: flag potential issues in changes."""
    result = subprocess.run(
        ['git', 'diff', '--stat'],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    if not result.stdout.strip():
        print("No changes to review.")
        return

    print("Changed files:")
    print(result.stdout)

    # Run check automatically
    print("\nRunning GT validation...\n")
    cmd_check()


def cmd_help():
    print("""GroundTruth Codebase Intelligence (v8 + Phase 3)

  PATTERN-AWARE (Phase 3):
    groundtruth_references <Symbol>  — Find all usages + definition pointer
    groundtruth_impact <Symbol>      — Obligation sites + conventions + subclass overrides
    groundtruth_check                — Completeness check against git diff

  EXPLORE (use BEFORE editing):
    summary                 — Quick codebase overview (packages, key classes)
    references <Symbol>     — Find all usages (supports Class.method)
    impact <Symbol>         — What breaks if you change this?
    scope <Symbol>          — Which files need editing?
    obligations <Symbol>    — What MUST change if you modify this?
    context <Symbol>        — Show actual code snippets of usage
    related <file_path>     — Find files related via shared symbols
    outline <file_path>     — Class/method map of a file
    search <pattern>        — Smart grep across source

  VALIDATE (use AFTER editing):
    check                   — Verify completeness + contradictions
    diff                    — Review changes with GT validation
    diagnose <file_path>    — Syntax errors + undefined names + overrides

  WORKFLOW: groundtruth_references → groundtruth_impact → edit → groundtruth_check → submit

Examples:
  python3 /tmp/gt_tool.py groundtruth_impact UniqueConstraint
  python3 /tmp/gt_tool.py groundtruth_references Session.resolve_redirects
  python3 /tmp/gt_tool.py groundtruth_check
  python3 /tmp/gt_tool.py check

Index builds on first call, cached for subsequent calls.""")


# ───────────────────────────────
# PHASE 3 COMMANDS (pattern-aware)
# ───────────────────────────────

def cmd_groundtruth_impact(index, symbol):
    """Pattern-aware impact + obligations. Merges old impact + obligations."""
    cls_locations = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])

    # Handle Class.method notation
    if not cls_locations and not func_locs and '.' in symbol:
        cls_name = symbol.rsplit('.', 1)[0]
        cls_locations = index.get('classes', {}).get(cls_name, [])

    if not cls_locations and not func_locs:
        candidates = [k for k in list(index.get('classes', {}).keys()) + list(index.get('functions', {}).keys())
                      if symbol.lower() in k.lower() or k.lower() in symbol.lower()]
        if candidates:
            print(f"'{symbol}' not found. Similar: {', '.join(candidates[:3])}")
        else:
            print(f"'{symbol}' not found.")
        return

    # Line 1: definition header
    if cls_locations:
        loc = cls_locations[0]
        bases_str = f"({', '.join(loc['bases'])})" if loc['bases'] else ""
        print(f"{symbol}{bases_str} ({loc['file']}:{loc['line']})")
    elif func_locs:
        loc = func_locs[0]
        print(f"{symbol}{loc['sig']} ({loc['file']}:{loc['line']})")

    # For classes: compute obligation groups
    if cls_locations:
        loc = cls_locations[0]
        groups = _compute_obligation_groups(loc)
        methods = loc.get('methods', {})

        if groups:
            print(f"\nOBLIGATION SITES (share state — edit ALL):")
            # Deduplicate: show each method once with its best role across all shared attrs
            method_best = {}  # mname -> (conf, role_desc, line)
            role_descs = {
                'stores_in_state': 'stores self.{attr} in state',
                'serializes_to_kwargs': 'packs self.{attr} into dict/tuple',
                'compares_in_eq': 'uses self.{attr} in equality check',
                'emits_to_output': 'formats self.{attr} to output',
                'passes_to_validator': 'passes self.{attr} to validator',
                'reads_in_logic': 'reads self.{attr} in control flow',
            }
            priority = {'HIGH': 0, 'MED': 1, 'LOW': 2}
            for group in groups[:8]:
                attr = group['attr']
                for mname in group['methods']:
                    minfo = methods.get(mname, {})
                    mline = minfo.get('line', '?')
                    best_role, best_conf = _best_role_for_method(minfo, attr)
                    if best_role is None:
                        best_role = f'uses self.{attr}'
                        best_conf = 'MED'  # Still an obligation site
                    desc = role_descs.get(best_role, best_role).format(attr=attr)
                    if mname not in method_best or priority.get(best_conf, 2) < priority.get(method_best[mname][0], 2):
                        method_best[mname] = (best_conf, desc, mline)

            # Sort: HIGH first, then MED
            sorted_methods = sorted(method_best.items(),
                                    key=lambda x: (priority.get(x[1][0], 2), x[1][2]))
            shown = 0
            for mname, (conf, desc, mline) in sorted_methods:
                if conf == 'LOW':
                    continue
                print(f"  [{conf}] {mname}:{mline} — {desc}")
                shown += 1
                if shown >= 10:
                    break

        # Class conventions
        class_convs = _detect_class_conventions(loc)
        if class_convs:
            print(f"\nCONVENTIONS:")
            for conv in class_convs[:5]:
                print(f"  [{conv['confidence']}] {conv['detail']}")

        # Subclass overrides
        subclasses = []
        for other_cls, other_locs in index.get('classes', {}).items():
            if other_cls == symbol:
                continue
            for oloc in other_locs:
                if symbol in oloc.get('bases', []):
                    overrides = [m for m in oloc.get('methods', {})
                                 if m in loc.get('methods', {}) and not m.startswith('__')]
                    if overrides:
                        subclasses.append((other_cls, oloc['file'], overrides))

        if subclasses:
            print(f"\nSUBCLASS OVERRIDES:")
            for scls, sfile, overrides in subclasses[:5]:
                print(f"  {scls} ({sfile}) overrides: {', '.join(overrides[:5])}")

    # Dynamic nudge
    has_high = False
    has_med = False
    if cls_locations:
        loc = cls_locations[0]
        groups = _compute_obligation_groups(loc)
        methods = loc.get('methods', {})
        for group in groups:
            attr = group['attr']
            for mname in group['methods']:
                minfo = methods.get(mname, {})
                _, conf = _best_role_for_method(minfo, attr)
                if conf == 'HIGH':
                    has_high = True
                elif conf == 'MED':
                    has_med = True

    if has_high and has_med:
        print(f"\n→ New init params must appear in ALL [HIGH] obligation sites. Consider [MED] sites too.")
    elif has_high:
        print(f"\n→ New init params must appear in ALL [HIGH] obligation sites following their pattern roles.")
    elif has_med:
        print(f"\n→ Consider updating [MED] obligation sites with new parameters.")


def cmd_groundtruth_references(index, symbol):
    """Enhanced references with footer nudge."""
    cmd_references(index, symbol)

    # Add footer with definition location
    cls_locs = index.get('classes', {}).get(symbol, [])
    func_locs = index.get('functions', {}).get(symbol, [])
    if cls_locs:
        loc = cls_locs[0]
        print(f"\n→ Definition is at {loc['file']}:{loc['line']}. Start there.")
    elif func_locs:
        loc = func_locs[0]
        print(f"\n→ Definition is at {loc['file']}:{loc['line']}. Start there.")
    # Handle Class.method
    elif '.' in symbol:
        cls_name, method_name = symbol.rsplit('.', 1)
        for loc in index.get('classes', {}).get(cls_name, []):
            if method_name in loc.get('methods', {}):
                mline = loc['methods'][method_name]['line']
                print(f"\n→ Definition is at {loc['file']}:{mline}. Start there.")
                break


def _parse_diff_hunks(diff_output):
    """Parse git diff --unified=0 output into file:line_set mapping."""
    file_hunks = {}  # file -> set of modified lines
    current_file = None

    for line in diff_output.split('\n'):
        if line.startswith('+++ b/'):
            current_file = line[6:]
        elif line.startswith('@@ ') and current_file:
            # Parse @@ -old,count +new,count @@
            import re as _re
            match = _re.search(r'\+(\d+)(?:,(\d+))?', line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if current_file not in file_hunks:
                    file_hunks[current_file] = set()
                for i in range(start, start + count):
                    file_hunks[current_file].add(i)

    return file_hunks


def _estimate_method_end(cls_info, mname):
    """Estimate method end line: next method start - 1, or start + 100."""
    methods = cls_info.get('methods', {})
    if mname not in methods:
        return 0
    start = methods[mname]['line']
    next_start = None
    for other_name, other_info in methods.items():
        if other_name == mname:
            continue
        other_line = other_info['line']
        if other_line > start:
            if next_start is None or other_line < next_start:
                next_start = other_line
    return (next_start - 1) if next_start else (start + 100)


def cmd_groundtruth_check(index=None):
    """Completeness check with live re-indexing and v2 STRUCTURAL VIOLATION format."""
    # Live re-index: always rebuild from current disk state
    if os.path.exists(INDEX_CACHE):
        os.remove(INDEX_CACHE)
    index = build_index(REPO_ROOT)

    # Get diff with unified=0 for precise hunk mapping
    result = subprocess.run(
        ['git', 'diff', '--unified=0'],
        capture_output=True, text=True, cwd=REPO_ROOT
    )
    diff_output = result.stdout.strip()

    # Track has_patch in phase3 state
    phase3_state = _load_phase3_state()
    phase3_state['has_patch'] = bool(diff_output)
    _save_phase3_state(phase3_state)

    if not diff_output:
        print("No changes detected. Make edits first, then verify.")
        return

    file_hunks = _parse_diff_hunks(result.stdout)
    if not file_hunks:
        print("No structural issues detected in your changes.")
        return

    # Only check .py files
    py_hunks = {f: lines for f, lines in file_hunks.items() if f.endswith('.py')}
    if not py_hunks:
        print("No structural issues detected in your changes.")
        return

    # Check for syntax errors first
    for filepath in py_hunks:
        full_path = os.path.join(REPO_ROOT, filepath)
        if os.path.exists(full_path):
            try:
                with open(full_path, 'r', errors='replace') as f:
                    source = f.read()
                ast.parse(source)
            except SyntaxError as e:
                print(f"STRUCTURAL VIOLATION [1/1]:")
                print(f"  File: {filepath}")
                print(f"  Line: {e.lineno}")
                print(f"  Symbol: (module level)")
                print(f"  Issue: syntax error — {e.msg}")
                print(f"  Required action: fix the syntax error before submitting")
                return

    # Map each hunk to class.method via index
    touched_classes = {}  # cls_name -> set of touched method names
    for filepath, modified_lines in py_hunks.items():
        for cls_name, cls_list in index.get('classes', {}).items():
            for cls_info in cls_list:
                if not _path_match(filepath, cls_info['file']):
                    continue
                for mname, minfo in cls_info.get('methods', {}).items():
                    mstart = minfo['line']
                    mend = _estimate_method_end(cls_info, mname)
                    if any(mstart <= line <= mend for line in modified_lines):
                        touched_classes.setdefault(cls_name, set()).add(mname)

    if not touched_classes:
        # No class methods in diff — no obligation analysis possible
        print("No structural issues detected in your changes.")
        return

    # For each touched class, compute obligation groups and check completeness
    violations = []
    for cls_name, touched_methods in touched_classes.items():
        cls_list = index.get('classes', {}).get(cls_name, [])
        if not cls_list:
            continue
        cls_info = cls_list[0]
        groups = _compute_obligation_groups(cls_info)

        if not groups:
            continue

        # For each obligation group that involves a touched method
        for group in groups:
            attr = group['attr']
            group_methods = group['methods']
            # Is any group method touched?
            if not any(m in touched_methods for m in group_methods):
                continue

            # Find the touched method(s) for context
            touched_in_group = [m for m in group_methods if m in touched_methods]

            # Only report methods that are NOT modified (the actual violations)
            # Skip __init__ — it sets attributes but is not a consumer of shared state.
            # Flagging __init__ as a violation is almost always a false positive.
            for mname in group_methods:
                if mname not in touched_methods and mname != '__init__':
                    minfo = cls_info.get('methods', {}).get(mname, {})
                    mline = minfo.get('line', '?')
                    violations.append({
                        'file': cls_info['file'],
                        'line': mline,
                        'symbol': f"{cls_name}.{mname}",
                        'attr': attr,
                        'touched_peers': touched_in_group,
                        'cls_name': cls_name,
                    })

    if not violations:
        print("No structural issues detected in your changes.")
        return

    # Output in v2 STRUCTURAL VIOLATION format
    total = len(violations)
    for i, v in enumerate(violations, 1):
        peers = ', '.join(f"{v['cls_name']}.{p}" for p in v['touched_peers'])
        print(f"STRUCTURAL VIOLATION [{i}/{total}]:")
        print(f"  File: {v['file']}")
        print(f"  Line: {v['line']}")
        print(f"  Symbol: {v['symbol']}")
        print(f"  Issue: shares self.{v['attr']} with {peers} but was NOT modified")
        print(f"  Required action: review and update {v['attr']} handling in this method")
        if i < total:
            print()


# ───────────────────────────────
# SPIN DETECTION
# ───────────────────────────────

SPIN_STATE_PATH = os.path.join(tempfile.gettempdir(), 'gt_spin_state.json')
CHECK_COUNT_PATH = os.path.join(tempfile.gettempdir(), 'gt_check_count')


def _load_spin_state():
    """Read spin state from disk. Returns default if missing/corrupt."""
    try:
        with open(SPIN_STATE_PATH) as f:
            state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError
            state.setdefault("history", [])
            state.setdefault("redirects", 0)
            state.setdefault("triggers", [])
            return state
    except (OSError, json.JSONDecodeError, ValueError):
        return {"history": [], "redirects": 0, "triggers": []}


def _save_spin_state(state):
    """Write spin state to disk."""
    try:
        with open(SPIN_STATE_PATH, 'w') as f:
            json.dump(state, f)
    except OSError:
        pass


def _check_spin(state, current_cmd, current_symbol):
    """Check for spin patterns. Returns redirect message or None."""
    history = state.get("history", [])
    recent = history[-5:] if len(history) >= 5 else history

    # Trigger 1: Search spam — 3+ search calls in last 5 entries
    search_count = sum(1 for h in recent if h.get("cmd") == "search")
    if search_count >= 3:
        sym = current_symbol or "the key symbol"
        return f"You're exploring without editing. Run: python3 /tmp/gt_tool.py obligations {sym} — then start fixing."

    # Trigger 2: Volume spin — 5+ consecutive GT calls without editing
    if len(history) >= 5:
        sym = current_symbol or "the key symbol"
        return f"You're exploring without editing. Run: python3 /tmp/gt_tool.py obligations {sym} — then start fixing."

    # Trigger 3: Symbol repeat — current symbol matches any in last 5 entries (need 3+ history)
    if current_symbol and len(current_symbol) >= 6 and len(history) >= 3:
        prefix = current_symbol[:6].lower()
        for h in recent:
            prev_sym = h.get("symbol", "")
            if prev_sym and prev_sym[:6].lower() == prefix:
                sym = current_symbol
                return f"You're exploring without editing. Run: python3 /tmp/gt_tool.py obligations {sym} — then start fixing."

    return None


# ───────────────────────────────
# PHASE 3 STATE
# ───────────────────────────────

PHASE3_STATE_PATH = os.path.join(tempfile.gettempdir(), 'gt_phase3_state.json')


def _load_phase3_state():
    """Load Phase 3 dynamic state."""
    try:
        with open(PHASE3_STATE_PATH) as f:
            state = json.load(f)
            if not isinstance(state, dict):
                raise ValueError
            state.setdefault('call_counts', {})
            state.setdefault('symbols_queried', {})
            state.setdefault('has_patch', False)
            return state
    except (OSError, json.JSONDecodeError, ValueError):
        return {'call_counts': {}, 'symbols_queried': {}, 'has_patch': False}


def _save_phase3_state(state):
    """Save Phase 3 dynamic state."""
    try:
        with open(PHASE3_STATE_PATH, 'w') as f:
            json.dump(state, f)
    except OSError:
        pass


def _check_suppression(state, command, symbol):
    """Check if command should be suppressed due to repetition."""
    key = f"{command}:{symbol}" if symbol else command

    counts = state.get('symbols_queried', {})
    count = counts.get(key, 0)

    if command == 'groundtruth_check' and count >= 3:
        return "Already checked 3 times. Submit your patch."
    if command in ('impact', 'groundtruth_impact') and symbol and count >= 3:
        return f"Already queried '{symbol}' twice. Edit now."
    if command in ('references', 'groundtruth_references') and symbol and count >= 3:
        return f"Already queried '{symbol}' twice. Edit now."

    return None


# ───────────────────────────────
# MAIN
# ───────────────────────────────

if __name__ == '__main__':
    try:
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

        # --- Spin detection ---
        spin_state = _load_spin_state()
        symbol_arg = sys.argv[2] if len(sys.argv) >= 3 else ""
        spin_state["history"].append({"cmd": command, "symbol": symbol_arg})

        if command in ("obligations", "check", "references",
                       "groundtruth_impact", "groundtruth_references", "groundtruth_check"):
            spin_state["redirects"] = 0
            spin_state["history"] = []  # Reset history — pipeline is advancing
            _save_spin_state(spin_state)
            # Execute normally — fall through to dispatch
        else:
            redirect = _check_spin(spin_state, command, symbol_arg)
            if redirect and spin_state["redirects"] < 2:
                spin_state["redirects"] += 1
                spin_state["triggers"].append({"cmd": command, "symbol": symbol_arg})
                _save_spin_state(spin_state)
                print(redirect)
                sys.exit(0)
            if redirect is None:
                spin_state["redirects"] = 0
            _save_spin_state(spin_state)

        # --- Phase 3 commands (pattern-aware) ---
        phase3_state = _load_phase3_state()

        # Check suppression BEFORE incrementing count
        suppression = _check_suppression(phase3_state, command, symbol_arg)
        if suppression:
            print(suppression)
            sys.exit(0)

        # Track call for future suppression
        p3_key = f"{command}:{symbol_arg}" if symbol_arg else command
        p3_counts = phase3_state.setdefault('symbols_queried', {})
        p3_counts[p3_key] = p3_counts.get(p3_key, 0) + 1
        _save_phase3_state(phase3_state)

        if command == 'groundtruth_impact' and len(sys.argv) >= 3:
            cmd_groundtruth_impact(index, sys.argv[2])
        elif command == 'groundtruth_references' and len(sys.argv) >= 3:
            cmd_groundtruth_references(index, sys.argv[2])
        elif command == 'groundtruth_check':
            cmd_groundtruth_check()

        # --- Legacy commands ---
        elif command == 'summary':
            cmd_summary(index)
        elif command == 'references' and len(sys.argv) >= 3:
            cmd_references(index, sys.argv[2])
        elif command == 'outline' and len(sys.argv) >= 3:
            cmd_outline(index, sys.argv[2])
        elif command == 'impact' and len(sys.argv) >= 3:
            cmd_impact(index, sys.argv[2])
        elif command == 'search' and len(sys.argv) >= 3:
            cmd_search(index, ' '.join(sys.argv[2:]))
        elif command == 'scope' and len(sys.argv) >= 3:
            cmd_scope(index, sys.argv[2])
        elif command == 'obligations' and len(sys.argv) >= 3:
            cmd_obligations(index, sys.argv[2])
        elif command == 'context' and len(sys.argv) >= 3:
            cmd_context(index, sys.argv[2])
        elif command == 'related' and len(sys.argv) >= 3:
            cmd_related(index, sys.argv[2])
        elif command == 'diagnose' and len(sys.argv) >= 3:
            cmd_diagnose(index, sys.argv[2])
        elif command == 'check':
            check_count = 0
            try:
                with open(CHECK_COUNT_PATH) as f:
                    check_count = int(f.read().strip())
            except (OSError, ValueError):
                pass
            if check_count >= 1:
                print("Already checked. If your patch addresses the findings above, submit it now. Do not keep revising.")
            else:
                try:
                    with open(CHECK_COUNT_PATH, 'w') as f:
                        f.write(str(check_count + 1))
                except OSError:
                    pass
                cmd_check()
        elif command == 'diff':
            cmd_diff(index)
        else:
            print(f"Unknown command: {command}")
            cmd_help()
            sys.exit(1)
    except (MemoryError, RecursionError) as e:
        print(f"GT tool error ({type(e).__name__}). Use grep/find instead.")
        sys.exit(1)
    except Exception as e:
        # Provide actionable fallback
        print(f"GT tool error: {e}")
        print("Fallback: use grep/find for this query.")
        sys.exit(1)

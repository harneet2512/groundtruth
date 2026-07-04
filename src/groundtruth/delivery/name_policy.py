"""Centralized delivery NAME policy — builtin/dunder-shadow + stdlib-shadow.

The name-class half of the delivery fact-filter (the path-class half lives in
``path_policy.py``). A cross-file call edge whose TARGET name is a language
builtin / dunder, or whose call site is a ``<stdlib_module>.<name>(`` attribute
call, is NEVER a DELIVERED fact regardless of the edge's recorded
``resolution_method`` — callers overwhelmingly invoke the language builtin /
stdlib, not a same-named project symbol that happens to shadow it.

This is the SINGLE SOURCE imported by both the Product brief
(``pretask/v1r_brief.py``) and the Integration delivery hook
(``artifact_deepswe/gt_mini_patch.py``) so a policy change reaches BOTH the
proof-time brief and agent-time delivery from one edit (the B1 single-source
invariant). It mirrors ``resolver.go`` ``builtinMethodNames`` /
``strongBuiltinMethodNames`` (the index-time T2 builtin drop, gt_gt §2.3) and
adds the shadowable bare-call builtins the T2 drop cannot see (it fires on
QUALIFIED calls only). Stdlib-only, language-agnostic, no benchmark shape.
"""
from __future__ import annotations

import re

# Mirrors resolver.go builtinMethodNames + strongBuiltinMethodNames (the T2
# builtin drop, gt_gt §2.3) + the shadowable language builtins the T2 drop
# cannot see (it only fires on QUALIFIED calls; a bare ``isinstance(...)``
# resolves verified_unique when one project symbol shadows the name). Static
# set by design (consistent with the resolver's T2 list; no invented
# distribution threshold).
BUILTIN_CALLABLE_NAMES: frozenset[str] = frozenset({
    # resolver.go builtinMethodNames (str/dict/list/set methods)
    "join", "split", "splitlines", "strip", "lstrip", "rstrip", "lower",
    "upper", "title", "startswith", "endswith", "encode", "decode", "format",
    "replace", "find", "rfind",
    "get", "keys", "values", "items", "setdefault", "update", "popitem",
    "append", "extend", "pop", "insert", "remove", "index", "count", "sort",
    "reverse", "add", "discard", "clear", "copy",
    # resolver.go strongBuiltinMethodNames extras
    "rsplit", "zfill", "casefold", "loads", "dumps",
    # shadowable Python builtins (the isinstance/len launder class) + os.path
    "isinstance", "issubclass", "len", "print", "open", "type", "super",
    "getattr", "setattr", "hasattr", "delattr", "repr", "str", "int", "float",
    "bool", "list", "dict", "set", "tuple", "iter", "next", "range", "zip",
    "map", "filter", "sorted", "reversed", "enumerate", "sum", "min", "max",
    "abs", "round", "all", "any", "id", "hash", "vars", "dir", "callable",
    "exists",
    # JS/TS/Go/Rust ultra-common builtin method names
    "push", "shift", "unshift", "slice", "splice", "concat", "indexof",
    "foreach", "tostring", "write", "read", "close", "new", "make", "clone",
    "unwrap", "expect",
})

# Stdlib/builtin module names whose attribute calls (os.walk, json.loads, ...)
# get name-matched to a same-named PROJECT function by the indexer. A project
# file with a function named walk/join/split/load collides with stdlib on EVERY
# repo — this is general, not benchmark-shaped.
STDLIB_MODULES: frozenset[str] = frozenset({
    "os", "sys", "re", "io", "json", "math", "time", "copy", "glob", "uuid",
    "shutil", "random", "typing", "logging", "pathlib", "datetime", "string",
    "decimal", "inspect", "warnings", "argparse", "textwrap", "itertools",
    "functools", "operator", "collections", "subprocess", "contextlib",
})

_STDLIB_SHADOW_RE = re.compile(r"([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)\s*\(")


def is_builtin_shadow_name(name: str) -> bool:
    """True when ``name`` is a builtin/dunder callable name whose call edges
    cannot be trusted as facts regardless of recorded provenance (the bare
    builtin-shadow launder; dunders are invoked via the language protocol, not
    by callers naming the project's definition)."""
    n = (name or "").strip()
    if not n:
        return False
    if n.startswith("__") and n.endswith("__") and len(n) > 4:
        return True
    return n.lower() in BUILTIN_CALLABLE_NAMES


def is_stdlib_shadow(code: str, target_name: str) -> bool:
    """True when ``code`` calls ``<stdlib_module>.<target_name>(`` — a stdlib
    attribute call the indexer name-matched to a project function of the same
    name (the proven ``os.walk`` -> ``account.walk`` false caller). Defends
    against an indexer that records such an edge with a DETERMINISTIC
    ``resolution_method`` (the provenance gate alone would trust it). Repo- and
    language-agnostic."""
    if not code or not target_name:
        return False
    for m in _STDLIB_SHADOW_RE.finditer(code):
        head = m.group(1).split(".")[0]
        if m.group(2) == target_name and head in STDLIB_MODULES:
            return True
    return False

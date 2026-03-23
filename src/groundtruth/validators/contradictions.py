"""Contradiction detector — positive structural evidence only.

ONLY emits findings when BOTH sides of a contradiction are in evidence:
the code under analysis AND the index data that proves it wrong.
When uncertain about any aspect, stays silent.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Err, Ok


@dataclass(frozen=True)
class Contradiction:
    """A contradiction backed by positive structural evidence."""

    kind: str  # override_violation | arity_mismatch | import_path_moved
    file_path: str
    line: int | None
    message: str  # human-readable
    evidence: str  # what positive evidence proves this
    confidence: float  # 0.0-1.0


def _parse_ast(source_code: str) -> ast.Module | None:
    """Parse source code into an AST, returning None on failure."""
    try:
        return ast.parse(source_code)
    except SyntaxError:
        return None


def _count_required_params(signature: str) -> tuple[int, bool] | None:
    """Extract (required_param_count, has_variadic) from a signature string.

    Returns None if the signature cannot be parsed reliably.
    The signature is expected in the form: "(param1, param2, ...)" or
    "(self, param1, param2: type = default, *args, **kwargs)".
    """
    # Strip outer parens and return type
    sig = signature.strip()
    if not sig.startswith("("):
        return None
    paren_depth = 0
    end = -1
    for i, ch in enumerate(sig):
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth == 0:
                end = i
                break
    if end < 0:
        return None

    inner = sig[1:end].strip()
    if not inner:
        return 0, False

    # Split on commas respecting brackets
    params: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in inner:
        if ch in ("(", "[", "{"):
            depth += 1
            current.append(ch)
        elif ch in (")", "]", "}"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            params.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        params.append("".join(current).strip())

    has_variadic = False
    required = 0

    for p in params:
        p = p.strip()
        if not p:
            continue
        # Skip self/cls
        name_part = p.split(":")[0].split("=")[0].strip()
        if name_part in ("self", "cls"):
            continue
        # Positional-only separator
        if p == "/":
            continue
        # Keyword-only separator
        if p == "*":
            continue
        # *args or **kwargs
        if p.startswith("*"):
            has_variadic = True
            continue
        # Has default → not required
        if "=" in p:
            continue
        required += 1

    return required, has_variadic


def _count_call_args(call_node: ast.Call) -> tuple[int, bool]:
    """Count positional args and detect if kwargs are used.

    Returns (positional_arg_count, has_kwargs).
    """
    has_kwargs = len(call_node.keywords) > 0
    # Check for **kwargs unpacking
    for kw in call_node.keywords:
        if kw.arg is None:  # **kwargs
            has_kwargs = True
    # Check for *args unpacking
    has_starargs = False
    for arg in call_node.args:
        if isinstance(arg, ast.Starred):
            has_starargs = True
    return len(call_node.args), has_kwargs or has_starargs


class ContradictionDetector:
    """Detects contradictions backed by positive structural evidence.

    Each check requires BOTH sides of the contradiction to be in evidence.
    When uncertain, stays silent.
    """

    def __init__(self, store: SymbolStore) -> None:
        self._store = store

    def check_file(self, file_path: str, source_code: str) -> list[Contradiction]:
        """Run all contradiction checks on a file."""
        results: list[Contradiction] = []
        results.extend(self.check_override_violation(source_code, file_path))
        results.extend(self.check_arity_mismatch(source_code, file_path))
        results.extend(self.check_import_path_moved(source_code, file_path))
        return results

    def check_override_violation(
        self, source_code: str, file_path: str
    ) -> list[Contradiction]:
        """If subclass override has incompatible signature with base.

        Both the base method signature (from store) and the override signature
        (from AST) must be present. If either is missing or ambiguous, stays silent.
        """
        tree = _parse_ast(source_code)
        if tree is None:
            return []

        contradictions: list[Contradiction] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # Get base class names
            base_names: list[str] = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    base_names.append(base.id)
                elif isinstance(base, ast.Attribute):
                    base_names.append(base.attr)

            if not base_names:
                continue

            # For each method in this class, check against base methods
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue

                method_name = item.name
                # Skip dunder methods — too many special calling conventions
                if method_name.startswith("__") and method_name.endswith("__"):
                    continue

                override_params = self._count_method_params(item)
                if override_params is None:
                    continue
                override_count, override_variadic = override_params

                # If override uses *args/**kwargs, it accepts anything — silent
                if override_variadic:
                    continue

                # Look up base class methods in the store
                for base_name in base_names:
                    base_method = self._find_base_method(base_name, method_name)
                    if base_method is None:
                        continue
                    if base_method.signature is None:
                        continue

                    base_params = _count_required_params(base_method.signature)
                    if base_params is None:
                        continue
                    base_count, base_variadic = base_params

                    # If base uses *args/**kwargs, can't determine expected params — silent
                    if base_variadic:
                        continue

                    # Both sides in evidence: compare
                    if override_count != base_count:
                        contradictions.append(Contradiction(
                            kind="override_violation",
                            file_path=file_path,
                            line=item.lineno,
                            message=(
                                f"'{node.name}.{method_name}' overrides "
                                f"'{base_name}.{method_name}' but has "
                                f"{override_count} required param(s) "
                                f"vs base's {base_count}"
                            ),
                            evidence=(
                                f"Base '{base_name}.{method_name}' signature: "
                                f"{base_method.signature}; "
                                f"override in '{node.name}' has {override_count} "
                                f"required param(s)"
                            ),
                            confidence=0.9,
                        ))

        return contradictions

    def check_arity_mismatch(
        self, source_code: str, file_path: str
    ) -> list[Contradiction]:
        """If call site passes wrong number of args to known function.

        Both the call (from AST) and the function signature (from store)
        must be present. If the function has *args/**kwargs, or the call
        uses *args/**kwargs, or there's ambiguity — stays silent.
        """
        tree = _parse_ast(source_code)
        if tree is None:
            return []

        contradictions: list[Contradiction] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Extract function name
            func_name = self._extract_call_name(node)
            if func_name is None:
                continue

            # Look up function in store — must resolve to exactly one
            find_result = self._store.find_symbol_by_name(func_name)
            if isinstance(find_result, Err) or not find_result.value:
                continue
            if len(find_result.value) > 1:
                # Ambiguous — silent
                continue

            sym = find_result.value[0]
            if sym.signature is None:
                continue
            if sym.kind not in ("function", "method"):
                continue

            sig_params = _count_required_params(sym.signature)
            if sig_params is None:
                continue
            required_count, has_variadic = sig_params

            # If function has *args/**kwargs, can't determine max — silent
            if has_variadic:
                continue

            # Count call args
            positional_count, call_has_dynamic = _count_call_args(node)
            # If call uses *args or **kwargs, can't determine actual count — silent
            if call_has_dynamic:
                continue

            # Both sides in evidence: compare
            if positional_count < required_count:
                contradictions.append(Contradiction(
                    kind="arity_mismatch",
                    file_path=file_path,
                    line=getattr(node, "lineno", None),
                    message=(
                        f"'{func_name}' requires {required_count} arg(s) "
                        f"but called with {positional_count}"
                    ),
                    evidence=(
                        f"Store signature: {sym.signature}; "
                        f"call passes {positional_count} positional arg(s)"
                    ),
                    confidence=0.85,
                ))

        return contradictions

    def check_import_path_moved(
        self, source_code: str, file_path: str
    ) -> list[Contradiction]:
        """If import references old path but symbol moved to new path.

        Both the import statement (from AST) and the correct location (from store)
        must be present. If the symbol is unknown to the store entirely,
        stays silent — it might be an external package.
        """
        tree = _parse_ast(source_code)
        if tree is None:
            return []

        contradictions: list[Contradiction] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module is None:
                continue

            for alias in node.names:
                symbol_name = alias.name
                import_module = node.module

                # Look up symbol in the store
                find_result = self._store.find_symbol_by_name(symbol_name)
                if isinstance(find_result, Err) or not find_result.value:
                    # Symbol not in store — might be external package — silent
                    continue

                # Check if ANY match is at the imported module path
                matches = find_result.value
                module_path_candidates = self._module_to_file_suffixes(import_module)

                found_at_import_path = False
                actual_locations: list[str] = []
                for sym in matches:
                    norm_path = sym.file_path.replace("\\", "/")
                    if any(norm_path.endswith(suffix) for suffix in module_path_candidates):
                        found_at_import_path = True
                        break
                    actual_locations.append(sym.file_path)

                if found_at_import_path:
                    # Import path is correct — no contradiction
                    continue

                if not actual_locations:
                    # No alternative location found — silent
                    continue

                # Positive evidence: symbol exists at a different path
                best_location = actual_locations[0]
                contradictions.append(Contradiction(
                    kind="import_path_moved",
                    file_path=file_path,
                    line=node.lineno,
                    message=(
                        f"'{symbol_name}' imported from '{import_module}' "
                        f"but found at '{best_location}'"
                    ),
                    evidence=(
                        f"Import says '{import_module}'; "
                        f"store has '{symbol_name}' at '{best_location}'"
                    ),
                    confidence=0.8,
                ))

        return contradictions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _count_method_params(
        self, func: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> tuple[int, bool] | None:
        """Count required params of a method (excluding self/cls).

        Returns (required_count, has_variadic) or None if unparseable.
        """
        args = func.args
        has_variadic = args.vararg is not None or args.kwarg is not None

        # All positional args minus self/cls
        all_args = list(args.posonlyargs) + list(args.args)
        # Remove self/cls (first param of methods)
        if all_args and all_args[0].arg in ("self", "cls"):
            all_args = all_args[1:]

        num_defaults = len(args.defaults)
        # Defaults are right-aligned: last N args have defaults
        # But posonlyargs defaults overlap with args.defaults
        total = len(all_args)
        required = total - num_defaults
        # Clamp to 0 in case of weird edge cases
        if required < 0:
            required = 0

        return required, has_variadic

    def _find_base_method(
        self, base_class_name: str, method_name: str
    ) -> SymbolRecord | None:
        """Find a method in a base class via the store."""
        # Find the base class
        find_result = self._store.find_symbol_by_name(base_class_name)
        if isinstance(find_result, Err) or not find_result.value:
            return None

        # Get the class — prefer class kind
        base_class: SymbolRecord | None = None
        for sym in find_result.value:
            if sym.kind in ("class", "Class"):
                base_class = sym
                break
        if base_class is None:
            return None
        if base_class.line_number is None or base_class.end_line is None:
            return None

        # Find methods within the class's line range
        file_syms = self._store.get_symbols_in_file(base_class.file_path)
        if isinstance(file_syms, Err):
            return None

        for sym in file_syms.value:
            if (
                sym.name == method_name
                and sym.kind in ("method", "function")
                and sym.line_number is not None
                and base_class.line_number <= sym.line_number <= base_class.end_line
            ):
                return sym

        return None

    @staticmethod
    def _extract_call_name(node: ast.Call) -> str | None:
        """Extract a simple function name from a Call node.

        Returns None for complex call expressions (e.g., chained calls).
        """
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    @staticmethod
    def _module_to_file_suffixes(module: str) -> list[str]:
        """Convert a dotted module path to candidate file path suffixes."""
        base = module.replace(".", "/")
        return [
            base + ".py",
            base + "/__init__.py",
            "/" + base + ".py",
            "/" + base + "/__init__.py",
        ]

"""Obligation engine — infer what MUST change when a symbol changes.

Obligations: constructor_symmetry, override_contract, caller_contract, shared_state.
Pure deterministic. Reads from SQLite KB.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err, Ok

log = get_logger("validators.obligations")

_STRUCTURAL_METHODS = frozenset({
    "__eq__", "__repr__", "__hash__", "__copy__", "__deepcopy__",
    "deconstruct", "__reduce__", "__getstate__", "__setstate__",
    "__str__", "__lt__", "__le__", "__gt__", "__ge__", "__ne__",
})


@dataclass
class Obligation:
    kind: str           # constructor_symmetry | override_contract | caller_contract | shared_state
    source: str         # symbol that changed
    target: str         # symbol that must also change
    target_file: str
    target_line: int | None
    reason: str         # one-line evidence
    confidence: float   # 0.0-1.0


class ObligationEngine:
    def __init__(self, store: SymbolStore, graph: ImportGraph) -> None:
        self.store = store
        self.graph = graph

    def infer(self, symbol: str, file_context: str | None = None) -> list[Obligation]:
        """Given a symbol, return all change obligations."""
        sym = self._resolve(symbol, file_context)
        if sym is None:
            return []

        obligations: list[Obligation] = []

        if sym.kind in ("class", "Class"):
            obligations.extend(self._constructor_symmetry(sym))
            # Check all methods of this class for override contracts
            children = self._get_class_methods(sym)
            for child in children:
                obligations.extend(self._override_contract(sym, child.name))
            # Shared state: find methods coupled through shared attributes
            obligations.extend(self._shared_state_for_class(sym))
        elif sym.kind in ("method", "function"):
            obligations.extend(self._caller_contract(sym))
            # If it's a method, check override contract and shared state
            enclosing = self._find_enclosing_class(sym)
            if enclosing:
                obligations.extend(self._override_contract(enclosing, sym.name))
                obligations.extend(self._shared_state_for_method(sym, enclosing))
        else:
            obligations.extend(self._caller_contract(sym))

        return self._deduplicate(obligations)

    def infer_from_patch(self, diff_text: str) -> list[Obligation]:
        """Given a diff, extract changed symbols and compute obligations."""
        changed = self._parse_changed_symbols(diff_text)
        all_obligations: list[Obligation] = []

        for sym_name, file_path in changed:
            obligations = self.infer(sym_name, file_context=file_path)
            all_obligations.extend(obligations)

        return self._deduplicate(all_obligations)

    def _resolve(self, symbol: str, file_context: str | None = None) -> SymbolRecord | None:
        """Resolve a symbol name to a SymbolRecord."""
        result = self.store.resolve_symbol(symbol, file_context=file_context)
        if isinstance(result, Ok):
            return result.value
        return None

    def _get_class_methods(self, class_sym: SymbolRecord) -> list[SymbolRecord]:
        """Get methods within a class's line range."""
        if class_sym.line_number is None or class_sym.end_line is None:
            return []
        result = self.store.get_symbols_in_line_range(
            class_sym.file_path, class_sym.line_number, class_sym.end_line
        )
        if isinstance(result, Ok):
            return [s for s in result.value if s.kind in ("method", "function") and s.id != class_sym.id]
        return []

    def _find_enclosing_class(self, method_sym: SymbolRecord) -> SymbolRecord | None:
        """Find the class that contains this method."""
        result = self.store.get_symbols_in_file(method_sym.file_path)
        if isinstance(result, Err):
            return None
        for s in result.value:
            if s.kind in ("class", "Class") and s.line_number is not None and s.end_line is not None:
                if method_sym.line_number is not None:
                    if s.line_number <= method_sym.line_number <= s.end_line:
                        return s
        return None

    def _constructor_symmetry(self, class_sym: SymbolRecord) -> list[Obligation]:
        """If __init__ sets self.X, structural methods referencing other attrs but not self.X are obligated."""
        obligations: list[Obligation] = []

        # Get attributes for this class
        attrs_result = self.store.get_attributes_for_symbol(class_sym.id)
        if isinstance(attrs_result, Err) or not attrs_result.value:
            return obligations

        attrs = attrs_result.value
        methods = self._get_class_methods(class_sym)

        # Find __init__ and which attrs it sets
        init_attrs: set[str] = set()
        for attr in attrs:
            method_ids = attr.get("method_ids") or []
            # Check if any method_id belongs to __init__
            for m in methods:
                if m.name == "__init__" and m.id in method_ids:
                    init_attrs.add(attr["name"])

        if not init_attrs:
            # Fallback: all attrs are considered init attrs
            init_attrs = {a["name"] for a in attrs}

        if not init_attrs:
            return obligations

        # Find structural methods and check completeness
        for m in methods:
            if m.name not in _STRUCTURAL_METHODS:
                continue

            # Which attrs does this structural method reference?
            method_attrs: set[str] = set()
            for attr in attrs:
                method_ids = attr.get("method_ids") or []
                if m.id in method_ids:
                    method_attrs.add(attr["name"])

            # If method references SOME attrs but not ALL init attrs, it's an obligation
            if method_attrs and not init_attrs.issubset(method_attrs):
                missing = init_attrs - method_attrs
                obligations.append(Obligation(
                    kind="constructor_symmetry",
                    source=f"{class_sym.name}.__init__",
                    target=f"{class_sym.name}.{m.name}",
                    target_file=class_sym.file_path,
                    target_line=m.line_number,
                    reason=f"{m.name} references {sorted(method_attrs)} but misses {sorted(missing)}",
                    confidence=0.85,
                ))

        return obligations

    def _override_contract(self, class_sym: SymbolRecord, method_name: str) -> list[Obligation]:
        """If a base method signature changes, subclass overrides are obligated."""
        obligations: list[Obligation] = []

        subclasses = self.store.get_subclasses(class_sym.name)
        if isinstance(subclasses, Err) or not subclasses.value:
            return obligations

        # Find the base method's signature
        base_method: SymbolRecord | None = None
        for m in self._get_class_methods(class_sym):
            if m.name == method_name:
                base_method = m
                break

        if base_method is None:
            return obligations

        for sub_sym in subclasses.value:
            # Check if subclass has a method with the same name
            sub_methods = self._get_class_methods(sub_sym)
            for sub_m in sub_methods:
                if sub_m.name == method_name:
                    obligations.append(Obligation(
                        kind="override_contract",
                        source=f"{class_sym.name}.{method_name}",
                        target=f"{sub_sym.name}.{method_name}",
                        target_file=sub_sym.file_path,
                        target_line=sub_m.line_number,
                        reason=f"overrides {class_sym.name}.{method_name} — signature change propagates",
                        confidence=0.9,
                    ))

        return obligations

    def _caller_contract(self, sym: SymbolRecord) -> list[Obligation]:
        """If a function's params change, all callers are obligated."""
        obligations: list[Obligation] = []

        callers_result = self.graph.find_callers(sym.name)
        if isinstance(callers_result, Err):
            return obligations

        for ref in callers_result.value:
            obligations.append(Obligation(
                kind="caller_contract",
                source=sym.name,
                target=f"call site in {ref.file_path}",
                target_file=ref.file_path,
                target_line=ref.line,
                reason=f"calls {sym.name} — argument changes may be needed",
                confidence=0.7,
            ))

        return obligations

    @staticmethod
    def _deduplicate(obligations: list[Obligation], cap: int = 10) -> list[Obligation]:
        """Deduplicate by (kind, target, target_file), sort by confidence, cap."""
        seen: set[tuple[str, str, str]] = set()
        unique: list[Obligation] = []
        for o in obligations:
            key = (o.kind, o.target, o.target_file)
            if key not in seen:
                seen.add(key)
                unique.append(o)
        unique.sort(key=lambda o: o.confidence, reverse=True)
        return unique[:cap]

    def _shared_state_for_class(self, class_sym: SymbolRecord) -> list[Obligation]:
        """When a class changes, find methods coupled through shared attributes."""
        obligations: list[Obligation] = []
        attrs_result = self.store.get_attributes_for_symbol(class_sym.id)
        if isinstance(attrs_result, Err) or not attrs_result.value:
            return obligations
        # Collect unique attr names
        attr_names = {a["name"] for a in attrs_result.value}
        for attr_name in sorted(attr_names):
            obligations.extend(self._shared_state(class_sym, attr_name))
        return obligations

    def _shared_state_for_method(
        self, method_sym: SymbolRecord, class_sym: SymbolRecord
    ) -> list[Obligation]:
        """When a method changes, find other methods sharing the same attributes."""
        obligations: list[Obligation] = []
        attrs_result = self.store.get_attributes_for_symbol(class_sym.id)
        if isinstance(attrs_result, Err) or not attrs_result.value:
            return obligations
        # Find attrs this method touches
        for attr in attrs_result.value:
            method_ids = attr.get("method_ids") or []
            if method_sym.id in method_ids:
                for o in self._shared_state(class_sym, attr["name"]):
                    # Exclude the method itself from results
                    if o.target != f"{class_sym.name}.{method_sym.name}":
                        obligations.append(o)
        return obligations

    def _shared_state(self, class_sym: SymbolRecord, attr_name: str) -> list[Obligation]:
        """Methods sharing self.attr are coupled."""
        obligations: list[Obligation] = []

        attrs_result = self.store.get_attributes_for_symbol(class_sym.id)
        if isinstance(attrs_result, Err):
            return obligations

        for attr in attrs_result.value:
            if attr["name"] != attr_name:
                continue
            method_ids = attr.get("method_ids") or []
            for mid in method_ids:
                sym_result = self.store.get_symbol_by_id(mid)
                if isinstance(sym_result, Ok) and sym_result.value:
                    m = sym_result.value
                    obligations.append(Obligation(
                        kind="shared_state",
                        source=f"{class_sym.name}.{attr_name}",
                        target=f"{class_sym.name}.{m.name}",
                        target_file=m.file_path,
                        target_line=m.line_number,
                        reason=f"{m.name} reads/writes self.{attr_name} — semantics coupled",
                        confidence=0.6,
                    ))

        return obligations

    def _parse_changed_symbols(self, diff_text: str) -> list[tuple[str, str]]:
        """Parse a unified diff to extract changed symbol names and files.

        Returns list of (symbol_name, file_path) tuples.
        """
        results: list[tuple[str, str]] = []
        current_file: str | None = None

        for line in diff_text.splitlines():
            # Track current file from diff headers
            if line.startswith("+++ b/"):
                current_file = line[6:]
            elif line.startswith("+++"):
                current_file = line[4:].strip()

            # Only look at added/modified lines
            if not line.startswith("+") or line.startswith("+++"):
                continue

            content = line[1:]

            if current_file is None:
                continue

            # Extract function/method definitions
            func_match = re.match(r"\s*(?:async\s+)?def\s+(\w+)", content)
            if func_match:
                results.append((func_match.group(1), current_file))
                continue

            # Extract class definitions
            class_match = re.match(r"\s*class\s+(\w+)", content)
            if class_match:
                results.append((class_match.group(1), current_file))

        return results

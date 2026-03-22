"""FingerprintExtractor — deterministic structural fingerprint (fingerprint_v1).

Packs a ~32-byte fingerprint from:
- symbol_kind hash (2 bytes)
- arity: parameter count capped at 15 (1 byte)
- control_skeleton: hash of control-flow node sequence (8 bytes)
- return_shape: hash of return construct types (4 bytes)
- read_set_hash: hash of self.X attributes read (8 bytes)
- write_set_hash: hash of self.X attributes written (8 bytes)

Total: 31 bytes. Distance = normalized Hamming.
"""

from __future__ import annotations

import ast
import hashlib
import struct

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.repr.registry import register_extractor

# Control flow node types to detect
_CONTROL_FLOW_TYPES_AST = (
    ast.If, ast.For, ast.While, ast.Try, ast.Return, ast.Raise,
)

_CONTROL_FLOW_NAMES = {
    ast.If: "if",
    ast.For: "for",
    ast.While: "while",
    ast.Try: "try",
    ast.Return: "return",
    ast.Raise: "raise",
}

# Return shape categories
_RETURN_SHAPES = {"dict", "list", "name", "none", "call", "tuple"}

FINGERPRINT_SIZE = 31  # 2 + 1 + 8 + 4 + 8 + 8


def _hash_truncate(data: str, nbytes: int) -> bytes:
    """SHA-256 hash of data, truncated to nbytes."""
    return hashlib.sha256(data.encode("utf-8")).digest()[:nbytes]


def _extract_control_skeleton_ast(tree: ast.AST) -> list[str]:
    """Walk AST and extract sequence of control-flow node types."""
    skeleton: list[str] = []
    for node in ast.walk(tree):
        name = _CONTROL_FLOW_NAMES.get(type(node))
        if name is not None:
            skeleton.append(name)
    return skeleton


def _try_parse_tree_sitter_control_flow(body_node: object) -> list[str] | None:
    """Try to extract control flow from a tree-sitter node. Returns None if not tree-sitter."""
    if body_node is None:
        return None
    # Check if it has tree-sitter node interface
    if not (hasattr(body_node, 'walk') and hasattr(body_node, 'type')):
        return None
    ts_control_types = {
        "if_statement": "if",
        "for_statement": "for",
        "while_statement": "while",
        "try_statement": "try",
        "return_statement": "return",
        "raise_statement": "raise",
    }
    skeleton: list[str] = []
    cursor = body_node.walk()
    visited = True
    while visited:
        name = ts_control_types.get(cursor.node.type)
        if name is not None:
            skeleton.append(name)
        if not cursor.goto_first_child():
            if not cursor.goto_next_sibling():
                visited = False
                while cursor.goto_parent():
                    if cursor.goto_next_sibling():
                        visited = True
                        break
    return skeleton


def _extract_return_shapes(tree: ast.AST) -> set[str]:
    """Find what return constructs the function uses."""
    shapes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            val = node.value
            if val is None:
                shapes.add("none")
            elif isinstance(val, ast.Dict):
                shapes.add("dict")
            elif isinstance(val, ast.List):
                shapes.add("list")
            elif isinstance(val, ast.Name):
                shapes.add("name")
            elif isinstance(val, ast.Call):
                shapes.add("call")
            elif isinstance(val, ast.Tuple):
                shapes.add("tuple")
            else:
                shapes.add("name")  # fallback
    return shapes


def _extract_self_attrs(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Extract self.X read set and write set."""
    read_set: set[str] = set()
    write_set: set[str] = set()

    # First pass: find all written self.X (targets of assignments)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                targets = [node.target] if node.target else []
            for t in targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    write_set.add(t.attr)

    # Second pass: find all self.X references that are reads (not targets)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            if node.attr not in write_set or not _is_assignment_target(node, tree):
                read_set.add(node.attr)

    return read_set, write_set


def _is_assignment_target(attr_node: ast.Attribute, tree: ast.AST) -> bool:
    """Check if this specific attribute node is an assignment target."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                targets = [node.target] if node.target else []
            for t in targets:
                if t is attr_node:
                    return True
    return False


class FingerprintExtractor:
    """Extracts a deterministic structural fingerprint from a symbol."""

    @property
    def rep_type(self) -> str:
        return "fingerprint_v1"

    @property
    def rep_version(self) -> str:
        return "1.0"

    @property
    def dimension(self) -> int | None:
        return None

    @property
    def supported_languages(self) -> list[str]:
        return ["python"]

    def extract(self, symbol: ExtractedSymbol) -> bytes:
        """Extract a fingerprint from the symbol."""
        # 1. symbol_kind hash (2 bytes)
        kind_hash = _hash_truncate(symbol.kind, 2)

        # 2. arity (1 byte)
        arity = min(len(symbol.parameters), 15)
        arity_byte = struct.pack("B", arity)

        # 3. control_skeleton hash (8 bytes)
        skeleton = self._get_control_skeleton(symbol)
        skeleton_str = ",".join(skeleton)
        skeleton_hash = _hash_truncate(skeleton_str, 8)

        # 4. return_shape hash (4 bytes)
        return_shapes = self._get_return_shapes(symbol)
        shape_str = ",".join(sorted(return_shapes))
        shape_hash = _hash_truncate(shape_str, 4)

        # 5. read_set_hash (8 bytes)
        read_set, write_set = self._get_self_attrs(symbol)
        read_str = ",".join(sorted(read_set))
        read_hash = _hash_truncate(read_str, 8)

        # 6. write_set_hash (8 bytes)
        write_str = ",".join(sorted(write_set))
        write_hash = _hash_truncate(write_str, 8)

        return kind_hash + arity_byte + skeleton_hash + shape_hash + read_hash + write_hash

    def distance(self, a: bytes, b: bytes) -> float:
        """Hamming distance normalized to 0-1."""
        if len(a) != len(b):
            return 1.0
        if len(a) == 0:
            return 0.0
        differing_bits = 0
        total_bits = len(a) * 8
        for ba, bb in zip(a, b):
            xor = ba ^ bb
            differing_bits += bin(xor).count("1")
        return differing_bits / total_bits

    def invalidation_key(self, file_path: str, content: str) -> str:
        """SHA-256 of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _get_control_skeleton(self, symbol: ExtractedSymbol) -> list[str]:
        """Get control flow skeleton, using tree-sitter if available, else ast."""
        # Try tree-sitter first
        ts_result = _try_parse_tree_sitter_control_flow(symbol.body_node)
        if ts_result is not None:
            return ts_result

        # Fall back to ast
        if not symbol.raw_text.strip():
            return []
        try:
            tree = ast.parse(symbol.raw_text)
            return _extract_control_skeleton_ast(tree)
        except SyntaxError:
            return []

    def _get_return_shapes(self, symbol: ExtractedSymbol) -> set[str]:
        """Get return shapes from the symbol body."""
        if not symbol.raw_text.strip():
            return set()
        try:
            tree = ast.parse(symbol.raw_text)
            return _extract_return_shapes(tree)
        except SyntaxError:
            return set()

    def _get_self_attrs(self, symbol: ExtractedSymbol) -> tuple[set[str], set[str]]:
        """Get self attribute read/write sets."""
        if not symbol.raw_text.strip():
            return set(), set()
        try:
            tree = ast.parse(symbol.raw_text)
            return _extract_self_attrs(tree)
        except SyntaxError:
            return set(), set()


# Auto-register
_instance = FingerprintExtractor()
register_extractor(_instance)

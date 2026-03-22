"""TokenSketchExtractor — MinHash sketch for token-level similarity (tokensketch_v1).

From the symbol body (raw_text):
- Tokenize: extract identifier tokens, function/method calls, string literal keys
- Apply k=64 MinHash functions
- Result: 64 uint32 values = 256 bytes

Distance = 1.0 - (matching_minhashes / k) = estimated Jaccard distance.
"""

from __future__ import annotations

import ast
import hashlib
import struct

from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.repr.registry import register_extractor

K = 64  # number of hash functions
SKETCH_BYTES = K * 4  # 256 bytes
LARGE_PRIME = 2**31 - 1  # Mersenne prime
MAX_HASH = 2**32 - 1


def _normalize_token(token: str) -> str:
    """Normalize a token: lowercase, strip leading underscores."""
    return token.lstrip("_").lower()


def _extract_tokens_ast(raw_text: str) -> set[str]:
    """Extract identifier tokens from Python source using ast."""
    if not raw_text.strip():
        return set()
    try:
        tree = ast.parse(raw_text)
    except SyntaxError:
        return set()

    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            normalized = _normalize_token(node.id)
            if normalized:
                tokens.add(normalized)

        elif isinstance(node, ast.Attribute):
            normalized = _normalize_token(node.attr)
            if normalized:
                tokens.add(normalized)

        elif isinstance(node, ast.Call):
            # Extract called function/method names
            if isinstance(node.func, ast.Name):
                normalized = _normalize_token(node.func.id)
                if normalized:
                    tokens.add(f"call:{normalized}")
            elif isinstance(node.func, ast.Attribute):
                normalized = _normalize_token(node.func.attr)
                if normalized:
                    tokens.add(f"call:{normalized}")

        elif isinstance(node, ast.Constant):
            # Extract string literal keys
            if isinstance(node.value, str) and node.value:
                tokens.add(f"str:{node.value.lower()}")

    return tokens


def _minhash(tokens: set[str], k: int = K) -> list[int]:
    """Compute MinHash signature with k hash functions.

    Uses hash(seed_i + token) % large_prime for seeds i=0..k-1.
    """
    if not tokens:
        return [MAX_HASH] * k

    minhashes = [MAX_HASH] * k
    for i in range(k):
        seed = str(i)
        for token in tokens:
            h = hash(seed + token) % LARGE_PRIME
            if h < minhashes[i]:
                minhashes[i] = h

    # Ensure values fit in uint32
    return [h & 0xFFFFFFFF for h in minhashes]


class TokenSketchExtractor:
    """MinHash sketch extractor for token-level similarity."""

    @property
    def rep_type(self) -> str:
        return "tokensketch_v1"

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
        """Extract MinHash sketch from symbol's raw_text."""
        tokens = _extract_tokens_ast(symbol.raw_text)
        minhashes = _minhash(tokens)
        return struct.pack(f"{K}I", *minhashes)

    def distance(self, a: bytes, b: bytes) -> float:
        """Distance = 1.0 - estimated Jaccard similarity."""
        hashes_a = struct.unpack(f"{K}I", a)
        hashes_b = struct.unpack(f"{K}I", b)
        matching = sum(1 for ha, hb in zip(hashes_a, hashes_b) if ha == hb)
        return 1.0 - (matching / K)

    def invalidation_key(self, file_path: str, content: str) -> str:
        """SHA-256 of content."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Auto-register
_instance = TokenSketchExtractor()
register_extractor(_instance)

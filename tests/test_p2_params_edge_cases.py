"""
P2 Structured Parameter Parsing -- Edge Case Tests

Tests the _parse_signature_params function proposed in IMPLEMENTATION_HANDOFF.md.
Since P2 is not yet implemented, this file IMPLEMENTS the spec locally and then
tests edge cases against it.

Edge cases tested:
1. Function with 0 parameters -> should emit nothing (empty list or None)
2. Function with 15 parameters -> should cap at 10
3. Parameter with very long default value (200 chars) -> should truncate at 40
4. Go-style parameter "x int, y string" -> should use "parameter_declaration" case
"""

import re
import sys

# ── P2 Implementation (from IMPLEMENTATION_HANDOFF.md spec) ─────────────

MAX_PARAMS = 10
MAX_DEFAULT_LEN = 40


def _split_params(params_str: str) -> list[str]:
    """Split parameter string respecting nested brackets/parens."""
    params = []
    depth = 0
    current = ""
    for ch in params_str:
        if ch in ("(", "[", "{", "<"):
            depth += 1
            current += ch
        elif ch in (")", "]", "}", ">"):
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            stripped = current.strip()
            if stripped:
                params.append(stripped)
            current = ""
        else:
            current += ch
    stripped = current.strip()
    if stripped:
        params.append(stripped)
    return params


def _merge_go_params(raw_params: list[str]) -> list[str]:
    """Go-specific: merge comma-separated names that share a type.

    Go syntax: "x, y int" splits into ["x", "y int"]. We need to detect
    that "x" has no type and the NEXT param "y int" has a type, so "x"
    should inherit that type. The canonical Go tree-sitter node is
    "parameter_declaration" which groups them: (parameter_declaration
    name: (identifier) name: (identifier) type: (type_identifier)).

    Since we're parsing from signature strings, we must reassemble:
    ["x", "y int", "z string"] -> ["x int", "y int", "z string"]
    """
    if not raw_params:
        return raw_params

    # First pass: identify which params have types (2+ tokens) vs bare names (1 token)
    merged: list[str] = []
    pending_names: list[str] = []

    for p in raw_params:
        parts = p.strip().split()
        if len(parts) == 1:
            # Bare name with no type -- accumulate until we find a typed param
            pending_names.append(parts[0])
        else:
            # This param has a type. The last token is the type.
            type_str = parts[-1]
            # All preceding tokens are names in this declaration
            current_names = [n.strip().rstrip(",") for n in parts[:-1]]
            # Apply the type to all pending bare names
            for name in pending_names:
                merged.append(f"{name} {type_str}")
            pending_names = []
            # Add current names with type
            for name in current_names:
                merged.append(f"{name} {type_str}")

    # If there are trailing bare names with no type, add them as-is
    for name in pending_names:
        merged.append(name)

    return merged


def _parse_signature_params(
    signature: str, language: str
) -> list[dict] | None:
    """Parse function signature into structured params per the P2 spec."""
    if not signature:
        return None

    # Extract param list between parens
    m = re.search(r"\((.*)\)", signature, re.DOTALL)
    if not m:
        return None

    params_str = m.group(1).strip()
    if not params_str:
        return None  # 0 parameters

    raw_params = _split_params(params_str)

    # Go-specific: merge bare names with their shared type declaration
    if language == "go":
        raw_params = _merge_go_params(raw_params)

    params: list[dict] = []

    for p in raw_params:
        p = p.strip()
        if not p:
            continue

        param: dict = {"name": "", "type": None, "default": None, "required": True}

        # Go-style: "x int" or "x, y int" — name(s) followed by type, no colon
        if language == "go":
            # Go parameter_declaration: "x int", "x, y string", "opts ...Option"
            parts = p.split()
            if len(parts) >= 2:
                # Last token is the type, preceding tokens are names
                type_str = parts[-1]
                names = [n.strip().rstrip(",") for n in parts[:-1]]
                for name in names:
                    if name and name not in ("self", "cls"):
                        entry = {
                            "name": name,
                            "type": type_str,
                            "default": None,
                            "required": True,
                        }
                        params.append(entry)
                continue
            elif len(parts) == 1:
                param["name"] = parts[0]
                params.append(param)
                continue

        # Python/JS/TS/Java/Rust: name: type = default
        if ":" in p:
            name_part, type_part = p.split(":", 1)
            param["name"] = name_part.strip()
            if "=" in type_part:
                type_val, default = type_part.rsplit("=", 1)
                param["type"] = type_val.strip()
                default_val = default.strip()
                if len(default_val) > MAX_DEFAULT_LEN:
                    default_val = default_val[:MAX_DEFAULT_LEN] + "..."
                param["default"] = default_val
                param["required"] = False
            else:
                param["type"] = type_part.strip()
        elif "=" in p:
            name_part, default = p.split("=", 1)
            param["name"] = name_part.strip()
            default_val = default.strip()
            if len(default_val) > MAX_DEFAULT_LEN:
                default_val = default_val[:MAX_DEFAULT_LEN] + "..."
            param["default"] = default_val
            param["required"] = False
        else:
            param["name"] = p.strip()

        if param["name"] and param["name"] not in ("self", "cls"):
            params.append(param)

    # Cap at MAX_PARAMS
    if len(params) > MAX_PARAMS:
        params = params[:MAX_PARAMS]

    return params if params else None


# ── Tests ────────────────────────────────────────────────────────────────

def test_zero_params():
    """Edge case 1: Function with 0 parameters should emit nothing."""
    sig = "def empty()"
    result = _parse_signature_params(sig, "python")
    assert result is None, f"Expected None for 0-param function, got {result}"

    # Also test: only 'self'
    sig2 = "def method(self)"
    result2 = _parse_signature_params(sig2, "python")
    assert result2 is None, f"Expected None when only 'self', got {result2}"

    # No parens at all
    sig3 = "func noparens"
    result3 = _parse_signature_params(sig3, "python")
    assert result3 is None, f"Expected None when no parens, got {result3}"

    print("  PASS: test_zero_params")


def test_fifteen_params_capped_at_ten():
    """Edge case 2: Function with 15 parameters should cap at 10."""
    param_names = [f"p{i}" for i in range(1, 16)]
    params_str = ", ".join(f"{name}: int" for name in param_names)
    sig = f"def big_func({params_str})"
    result = _parse_signature_params(sig, "python")

    assert result is not None, "Expected non-None for 15-param function"
    assert len(result) == MAX_PARAMS, (
        f"Expected {MAX_PARAMS} params after cap, got {len(result)}"
    )
    # Verify first 10 are present, last 5 are dropped
    for i in range(10):
        assert result[i]["name"] == f"p{i+1}", (
            f"Param {i} should be p{i+1}, got {result[i]['name']}"
        )
    print("  PASS: test_fifteen_params_capped_at_ten")


def test_long_default_truncated():
    """Edge case 3: Parameter with very long default value (200 chars) should
    truncate at 40 characters."""
    long_default = "x" * 200
    sig = f'def func(data: str = "{long_default}")'
    result = _parse_signature_params(sig, "python")

    assert result is not None
    assert len(result) == 1
    default_val = result[0]["default"]
    assert default_val is not None, "Expected non-None default"
    # The default includes the quotes from the signature, but the VALUE portion
    # should be truncated to 40 chars + "..."
    assert len(default_val) <= MAX_DEFAULT_LEN + len("..."), (
        f"Default should be <= {MAX_DEFAULT_LEN + 3} chars, got {len(default_val)}: {default_val[:50]}..."
    )
    assert default_val.endswith("..."), (
        f"Truncated default should end with '...', got: {default_val[-10:]}"
    )
    assert result[0]["required"] is False
    print("  PASS: test_long_default_truncated")


def test_go_style_params():
    """Edge case 4: Go-style parameter 'x int, y string' should use
    parameter_declaration parsing case."""
    sig = "func process(x int, y string)"
    result = _parse_signature_params(sig, "go")

    assert result is not None, "Expected non-None for Go params"
    assert len(result) == 2, f"Expected 2 params, got {len(result)}"
    assert result[0]["name"] == "x", f"First param name should be 'x', got '{result[0]['name']}'"
    assert result[0]["type"] == "int", f"First param type should be 'int', got '{result[0]['type']}'"
    assert result[1]["name"] == "y", f"Second param name should be 'y', got '{result[1]['name']}'"
    assert result[1]["type"] == "string", f"Second param type should be 'string', got '{result[1]['type']}'"
    # Go params have no defaults -- all required
    assert result[0]["required"] is True
    assert result[1]["required"] is True
    print("  PASS: test_go_style_params")


def test_go_multi_name_param():
    """Go allows multiple names sharing a type: 'x, y int'."""
    sig = "func swap(x, y int)"
    result = _parse_signature_params(sig, "go")

    assert result is not None
    assert len(result) == 2, f"Expected 2 params from 'x, y int', got {len(result)}"
    assert result[0]["name"] == "x"
    assert result[0]["type"] == "int"
    assert result[1]["name"] == "y"
    assert result[1]["type"] == "int"
    print("  PASS: test_go_multi_name_param")


def main():
    print("=" * 60)
    print("P2 Structured Parameter Parsing -- Edge Case Tests")
    print("=" * 60)

    tests = [
        test_zero_params,
        test_fifteen_params_capped_at_ten,
        test_long_default_truncated,
        test_go_style_params,
        test_go_multi_name_param,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

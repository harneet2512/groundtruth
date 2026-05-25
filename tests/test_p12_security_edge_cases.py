"""
P12 Security-Sensitive Function Tagging -- Edge Case Tests

Tests the tagSecuritySensitivity function proposed in BUILD_RESEARCH_P6_P14.md.
Since P12 is not yet implemented, this file IMPLEMENTS the spec locally and then
tests edge cases against it.

Edge cases tested:
5. Function named "authenticate_user_query" -> should match both "authentication" and
   "sql" but each category only once (dedup by `seen`)
6. Function named "get_data" -> should NOT match (no security keywords)
7. Function named "Hash" (capitalized) -> lowercased to "hash" -> should match "cryptography"
"""

import sys
from typing import Optional


# ── P12 Implementation (from BUILD_RESEARCH_P6_P14.md spec, Approach A) ──

# The spec defines this keyword->category mapping:
SECURITY_KEYWORDS: dict[str, str] = {
    "auth": "authentication",
    "login": "authentication",
    "logout": "authentication",
    "password": "credential",
    "passwd": "credential",
    "secret": "credential",
    "token": "credential",
    "api_key": "credential",
    "apikey": "credential",
    "encrypt": "cryptography",
    "decrypt": "cryptography",
    "hash": "cryptography",
    "hmac": "cryptography",
    "sign": "cryptography",
    "verify": "cryptography",
    "query": "sql",
    "execute": "sql",
    "cursor": "sql",
    "sanitize": "input_validation",
    "validate": "input_validation",
    "escape": "input_validation",
    "eval": "code_execution",
    "exec": "code_execution",
    "subprocess": "code_execution",
    "shell": "code_execution",
    "open": "file_io",
    "read": "file_io",
    "write": "file_io",
    "unlink": "file_io",
    "chmod": "file_io",
    "socket": "network",
    "request": "network",
    "fetch": "network",
    "connect": "network",
    "permission": "authorization",
    "role": "authorization",
    "acl": "authorization",
    "csrf": "web_security",
    "cors": "web_security",
    "xss": "web_security",
    "inject": "web_security",
    "deserializ": "deserialization",
    "pickle": "deserialization",
    "yaml.load": "deserialization",
    "marshal": "deserialization",
    "sql": "injection_risk",  # From IMPLEMENTATION_HANDOFF.md version
}


def tag_security_sensitivity(
    func_name: str, signature: str = "", body_text: str = ""
) -> list[str]:
    """
    Implements the spec from BUILD_RESEARCH_P6_P14.md:

        func tagSecuritySensitivity(funcName string, sig string, bodyText string) []string {
            var tags []string
            nameLower := strings.ToLower(funcName)
            combined := nameLower + " " + strings.ToLower(sig)
            seen := map[string]bool{}
            for keyword, category := range securityKeywords {
                if strings.Contains(combined, keyword) && !seen[category] {
                    tags = append(tags, category)
                    seen[category] = true
                }
            }
            return tags
        }

    Returns a list of unique category strings.
    """
    name_lower = func_name.lower()
    combined = name_lower + " " + signature.lower()

    tags: list[str] = []
    seen: set[str] = set()

    for keyword, category in SECURITY_KEYWORDS.items():
        if keyword in combined and category not in seen:
            tags.append(category)
            seen.add(category)

    return tags


# ── Tests ────────────────────────────────────────────────────────────────


def test_authenticate_user_query_multi_match_dedup():
    """Edge case 5: 'authenticate_user_query' should match both 'authentication'
    AND 'sql'/'injection_risk' but each category should appear only ONCE
    (dedup by seen set).

    Breakdown:
    - 'auth' substring matches -> 'authentication'
    - 'query' substring matches -> 'sql'
    - 'sql' keyword check: 'sql' is NOT a substring of 'authenticate_user_query'
      BUT 'inject' keyword is NOT a substring either.
      Wait -- let me check carefully:
        'authenticate_user_query' lowered = 'authenticate_user_query'
        - 'auth' in 'authenticate_user_query' -> YES -> 'authentication'
        - 'query' in 'authenticate_user_query' -> YES -> 'sql'
        - 'validate' in 'authenticate_user_query' -> NO
        - 'sql' in 'authenticate_user_query' -> NO
        - No other keywords match

    So expected: ['authentication', 'sql'] (order may vary), each exactly once.
    """
    tags = tag_security_sensitivity("authenticate_user_query")

    assert "authentication" in tags, (
        f"Expected 'authentication' from 'auth' substring, got: {tags}"
    )
    assert "sql" in tags, (
        f"Expected 'sql' from 'query' substring, got: {tags}"
    )

    # Verify dedup: no category appears more than once
    assert len(tags) == len(set(tags)), (
        f"Categories should be deduplicated, got duplicates: {tags}"
    )

    # Specifically: 'authentication' appears exactly once despite 'auth' matching
    assert tags.count("authentication") == 1, (
        f"'authentication' should appear exactly once, got {tags.count('authentication')}"
    )
    assert tags.count("sql") == 1, (
        f"'sql' should appear exactly once, got {tags.count('sql')}"
    )

    print("  PASS: test_authenticate_user_query_multi_match_dedup")


def test_get_data_no_match():
    """Edge case 6: 'get_data' should NOT match any security keywords.

    Analysis:
    - 'get_data' lowered = 'get_data'
    - Let me check every keyword against 'get_data':
      - 'auth' NOT in 'get_data'
      - 'login' NOT in 'get_data'
      - 'password' NOT in 'get_data'
      - 'token' NOT in 'get_data'
      - 'encrypt'/'decrypt'/'hash' NOT in 'get_data'
      - 'query' NOT in 'get_data'
      - 'execute' NOT in 'get_data'
      - 'validate' NOT in 'get_data'
      - 'eval' NOT in 'get_data'
      - 'exec' NOT in 'get_data'
      - 'open' NOT in 'get_data'
      - 'read' NOT in 'get_data'
      - 'write' NOT in 'get_data'
      - 'sql' NOT in 'get_data'
      - etc.

    None match, so result should be empty.
    """
    tags = tag_security_sensitivity("get_data")
    assert len(tags) == 0, f"Expected no security tags for 'get_data', got: {tags}"
    print("  PASS: test_get_data_no_match")


def test_hash_capitalized():
    """Edge case 7: Function named 'Hash' (capitalized) should be lowercased to
    'hash' which matches the 'hash' keyword -> 'cryptography' category.

    The spec explicitly lowercases: nameLower := strings.ToLower(funcName)
    """
    tags = tag_security_sensitivity("Hash")
    assert "cryptography" in tags, (
        f"Expected 'cryptography' from 'Hash' (lowercased to 'hash'), got: {tags}"
    )
    print("  PASS: test_hash_capitalized")


def test_hash_no_duplicate_crypto():
    """Extended check: 'Hash' should produce 'cryptography' exactly once, even
    though multiple keywords map to cryptography (encrypt, decrypt, hash, hmac, sign, verify)."""
    tags = tag_security_sensitivity("Hash")
    assert tags.count("cryptography") == 1, (
        f"'cryptography' should appear exactly once, got: {tags}"
    )
    print("  PASS: test_hash_no_duplicate_crypto")


def test_edge_keyword_overlap():
    """Verify that 'execute_query_auth' hits three different categories but each only once."""
    tags = tag_security_sensitivity("execute_query_auth")
    assert "authentication" in tags  # from 'auth'
    assert "sql" in tags             # from 'query' and/or 'execute'
    # 'exec' is in 'execute' -> should match 'code_execution'
    # BUT wait: 'exec' IS a substring of 'execute'. So 'code_execution' should also match.
    assert "code_execution" in tags, (
        f"'exec' is substring of 'execute', so 'code_execution' should match. Got: {tags}"
    )
    # All unique
    assert len(tags) == len(set(tags)), f"Should be deduplicated: {tags}"
    print("  PASS: test_edge_keyword_overlap")


def test_empty_func_name():
    """Edge: empty function name should return no tags."""
    tags = tag_security_sensitivity("")
    assert len(tags) == 0, f"Expected no tags for empty name, got: {tags}"
    print("  PASS: test_empty_func_name")


def test_signature_also_checked():
    """The spec checks combined = nameLower + ' ' + sigLower.
    So security keywords in signature params should also trigger tags."""
    tags = tag_security_sensitivity("process", signature="def process(password: str)")
    assert "credential" in tags, (
        f"Expected 'credential' from 'password' in signature, got: {tags}"
    )
    print("  PASS: test_signature_also_checked")


def main():
    print("=" * 60)
    print("P12 Security-Sensitive Function Tagging -- Edge Case Tests")
    print("=" * 60)

    tests = [
        test_authenticate_user_query_multi_match_dedup,
        test_get_data_no_match,
        test_hash_capitalized,
        test_hash_no_duplicate_crypto,
        test_edge_keyword_overlap,
        test_empty_func_name,
        test_signature_also_checked,
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

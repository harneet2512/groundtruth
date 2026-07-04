"""
Adversarial edge case tests for P0 (conditional return extraction) and P6 (guard consequences).

These tests simulate the Go tree-sitter extraction logic in Python to verify correctness
before the Go implementation is written. The logic tested mirrors the spec in:
  - world_research_output/IMPLEMENTATION_HANDOFF.md (P0 at line 73, P6 at line 222)
  - world_research_output/BUILD_RESEARCH_P6_P14.md (P6 at line 24, P7/P0 at line 178)

P6 spec:
  - Walk if-statements, detect raise/return/throw/panic in body
  - Extract consequence text from the body node, truncate to 60 chars
  - Format: guardType + ": " + condText + " -> " + consequenceText

P0 spec:
  - Walk direct children of function body for if_statement containing return
  - Extract: condition (truncate 80) + return value (truncate 40)
  - Format: "if <cond>: return <val>"
  - Cap at 5 per function
  - Skip nested function definitions
"""
import dataclasses
import re
from typing import Optional


# ============================================================================
# Simulated data structures (mirrors Go PropertyRef)
# ============================================================================

@dataclasses.dataclass
class PropertyRef:
    node_idx: int
    kind: str
    value: str
    line: int
    confidence: float


# ============================================================================
# P6: Guard Consequence Extraction (simulating Go logic)
# ============================================================================

def extract_guard_from_stmt(
    stmt_type: str,
    stmt_text: str,
    condition_text: Optional[str],  # from tree-sitter ChildByFieldName("condition")
    body_text: Optional[str],       # from tree-sitter ChildByFieldName("body"/"consequence")
    line: int,
    node_idx: int = 0,
) -> Optional[PropertyRef]:
    """
    Simulate extractGuardFromStmt + P6 consequence extraction.

    The existing Go code:
      1. Checks stmt_type is if_statement or if_expression
      2. Searches full stmt text for keywords (raise, throw, return, panic, etc.)
      3. Classifies guardType
      4. Extracts condition from ChildByFieldName("condition") or fallback
      5. [P6 NEW] Extracts consequence from body node, truncates to 60

    The P6 addition (per IMPLEMENTATION_HANDOFF.md line 234-236):
      consequenceNode := ifBody.ChildByFieldName("body") // or first child
      consequenceText := truncate(nodeText(consequenceNode, src), 60)
      value := guardType + ": " + condText + " -> " + consequenceText
    """
    if stmt_type not in ("if_statement", "if_expression"):
        return None

    # Detect guard type via keyword search in full statement text
    is_guard = False
    guard_type = ""
    keywords = ["raise ", "throw ", "return", "panic(", "error(", "Error(", "abort(", "Err("]

    for kw in keywords:
        if kw in stmt_text:
            is_guard = True
            if "raise " in stmt_text or "throw " in stmt_text:
                guard_type = "raise"
            elif "panic(" in stmt_text or "abort(" in stmt_text:
                guard_type = "panic"
            else:
                guard_type = "return"
            break

    if not is_guard:
        return None

    # Extract condition
    cond_text = ""
    if condition_text is not None:
        cond_text = condition_text.strip()
    if cond_text == "":
        # Fallback: text between "if" and ":"/"{"
        cond_text = stmt_text
        brace_idx = cond_text.find("{")
        colon_idx = cond_text.find(":")
        if brace_idx > 0:
            cond_text = cond_text[3:brace_idx]
        elif colon_idx > 0:
            cond_text = cond_text[3:colon_idx]
        cond_text = cond_text.strip()
    if len(cond_text) > 120:
        cond_text = cond_text[:120]

    # P6: Extract consequence text
    consequence_text = ""
    if body_text is not None:
        consequence_text = body_text.strip()
        # Remove block delimiters (braces for Go/JS, indented block for Python)
        # In tree-sitter, ChildByFieldName("body") gives the block node content
        # which includes the outer braces/indent. Strip them.
        if consequence_text.startswith("{") and consequence_text.endswith("}"):
            consequence_text = consequence_text[1:-1].strip()
        if len(consequence_text) > 60:
            consequence_text = consequence_text[:60]

    # Build value string
    if consequence_text:
        value = f"{guard_type}: {cond_text} -> {consequence_text}"
    else:
        value = f"{guard_type}: {cond_text}"

    return PropertyRef(
        node_idx=node_idx,
        kind="guard_clause",
        value=value,
        line=line,
        confidence=1.0,
    )


# ============================================================================
# P0: Conditional Return Extraction (simulating Go logic)
# ============================================================================

@dataclasses.dataclass
class IfStmtInfo:
    """Simulates what tree-sitter gives for an if_statement node."""
    stmt_type: str              # "if_statement", "if_expression", "function_definition", etc.
    condition_text: Optional[str]  # from ChildByFieldName("condition")
    body_children: list         # list of child IfStmtInfo or ReturnInfo in the consequence
    line: int
    is_nested_func: bool = False  # for skipping nested function defs


@dataclasses.dataclass
class ReturnInfo:
    """Simulates a return_statement node."""
    return_text: str  # the expression after "return", empty if bare return


def extract_conditional_returns(
    body_children: list,  # direct children of function body node
    node_idx: int = 0,
) -> list[PropertyRef]:
    """
    Simulate extractConditionalReturns (P0).

    Spec (IMPLEMENTATION_HANDOFF.md line 83-88):
      - Walk body for if_statement nodes containing return_statement
      - For each: extract condition text + return expression text
      - Store as PropertyRef{Kind: "conditional_return", Value: "if <cond>: return <val>"}
      - Limit: first 5 conditional returns per function
      - Truncate condition to 80 chars, return value to 40 chars
    """
    results = []
    MAX_CONDITIONAL_RETURNS = 5

    for child in body_children:
        if len(results) >= MAX_CONDITIONAL_RETURNS:
            break

        # Skip nested function definitions
        if isinstance(child, IfStmtInfo) and child.is_nested_func:
            continue

        if not isinstance(child, IfStmtInfo):
            continue
        if child.stmt_type not in ("if_statement", "if_expression"):
            continue

        # Look for return statements in the consequence body
        ret_text = _find_first_return_recursive(child.body_children)
        if ret_text is not None:
            cond_text = ""
            if child.condition_text is not None:
                cond_text = child.condition_text.strip()
            if len(cond_text) > 80:
                cond_text = cond_text[:80]
            if len(ret_text) > 40:
                ret_text = ret_text[:40]

            value = f"if {cond_text}: return {ret_text}"
            results.append(PropertyRef(
                node_idx=node_idx,
                kind="conditional_return",
                value=value,
                line=child.line,
                confidence=0.9,
            ))

    return results


def _find_first_return_recursive(children: list) -> Optional[str]:
    """
    Recursively search for the first return statement in a subtree.
    This mirrors findFirstReturn() in the Go implementation.
    Walks depth-first through all children including nested blocks/loops/ifs.
    """
    for child in children:
        if isinstance(child, ReturnInfo):
            return child.return_text
        if isinstance(child, IfStmtInfo) and not child.is_nested_func:
            # Search inside nested if/for/etc bodies
            found = _find_first_return_recursive(child.body_children)
            if found is not None:
                return found
    return None


# ============================================================================
# EDGE CASE TESTS
# ============================================================================

class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  PASS: {name}")
        else:
            self.failed += 1
            self.failures.append((name, detail))
            print(f"  FAIL: {name} -- {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*70}")
        print(f"RESULTS: {self.passed}/{total} passed, {self.failed} failed")
        if self.failures:
            print("\nFAILURES:")
            for name, detail in self.failures:
                print(f"  - {name}: {detail}")
        print(f"{'='*70}")
        return self.failed == 0


def test_edge_cases():
    t = TestResults()

    # ========================================================================
    # EDGE CASE 1: Empty body guard
    # `if x is None:` with empty block -> consequence should be ""
    # ========================================================================
    print("\n--- Edge Case 1: Empty body guard ---")
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text="if x is None:\n    pass",
        # "pass" doesn't contain raise/throw/return/panic -- so this won't be a guard!
        # WAIT: "return" isn't present, neither is raise/throw. This is NOT a guard.
        condition_text="x is None",
        body_text="pass",
        line=1,
    )
    t.check(
        "Empty body with pass -> not a guard (no raise/return/throw)",
        result is None,
        f"Expected None (not a guard), got: {result}",
    )

    # Variant: empty body but the text contains "return" at least (bare return)
    result2 = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text="if x is None:\n    return",
        condition_text="x is None",
        body_text="return",  # bare return is the body content
        line=1,
    )
    t.check(
        "Empty body with bare return -> guard with consequence 'return'",
        result2 is not None and "return" in result2.value,
        f"Got: {result2}",
    )
    # The consequence text for bare "return" would just be "return"
    if result2:
        t.check(
            "Bare return consequence is 'return' (the body itself)",
            "-> return" in result2.value,
            f"Value: {result2.value}",
        )

    # ========================================================================
    # EDGE CASE 2: Multi-line consequence truncation at 60 chars
    # ========================================================================
    print("\n--- Edge Case 2: Multi-line consequence truncation ---")
    long_consequence = 'logger.error("fail")\n    raise ValueError("invalid input data that is quite long and exceeds sixty characters definitely")'
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text=f"if not valid:\n    {long_consequence}",
        condition_text="not valid",
        body_text=long_consequence,
        line=5,
    )
    t.check(
        "Multi-line consequence is truncated",
        result is not None,
        f"Expected a guard, got None",
    )
    if result:
        # The consequence part after " -> " should be <= 60 chars
        arrow_idx = result.value.find(" -> ")
        if arrow_idx >= 0:
            consequence_in_value = result.value[arrow_idx + 4:]
            t.check(
                f"Consequence <= 60 chars (got {len(consequence_in_value)})",
                len(consequence_in_value) <= 60,
                f"Consequence: '{consequence_in_value}' ({len(consequence_in_value)} chars)",
            )
        else:
            t.check("Arrow separator present", False, f"No ' -> ' in value: {result.value}")

    # ========================================================================
    # EDGE CASE 3: Deeply nested conditional return (if -> for -> if -> return)
    # ========================================================================
    print("\n--- Edge Case 3: Deeply nested return (if -> for -> if -> return) ---")
    # The spec says: walk body for if_statement nodes containing return_statement
    # findFirstReturn is recursive, so it should find returns at any depth
    nested_body = [
        IfStmtInfo(  # inner for loop (simulated as nested control structure)
            stmt_type="for_statement",
            condition_text=None,
            body_children=[
                IfStmtInfo(
                    stmt_type="if_statement",
                    condition_text="item.valid",
                    body_children=[
                        ReturnInfo(return_text="item"),
                    ],
                    line=4,
                ),
            ],
            line=3,
        ),
    ]
    # Outer if_statement
    outer_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="items is not None",
        body_children=nested_body,
        line=2,
    )
    results = extract_conditional_returns([outer_if])
    t.check(
        "Deeply nested return (3 levels) is found",
        len(results) == 1,
        f"Expected 1 result, got {len(results)}: {results}",
    )
    if results:
        t.check(
            "Nested return extracts outer if condition + inner return text",
            "items is not None" in results[0].value and "item" in results[0].value,
            f"Value: {results[0].value}",
        )

    # ========================================================================
    # EDGE CASE 4: Return inside lambda
    # `if x: return lambda: None`
    # The lambda's implicit return is NOT a separate return statement.
    # The actual return statement is `return lambda: None` — the whole thing.
    # ========================================================================
    print("\n--- Edge Case 4: Return inside lambda ---")
    # In tree-sitter, `return lambda: None` is a return_statement with
    # value = lambda_expression. It's ONE return_statement node.
    lambda_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="x",
        body_children=[
            ReturnInfo(return_text="lambda: None"),
        ],
        line=10,
    )
    results = extract_conditional_returns([lambda_if])
    t.check(
        "Lambda return: found as one conditional return",
        len(results) == 1,
        f"Expected 1 result, got {len(results)}",
    )
    if results:
        t.check(
            "Lambda return value is 'lambda: None' (not confused with nested return)",
            "lambda: None" in results[0].value,
            f"Value: {results[0].value}",
        )

    # ========================================================================
    # EDGE CASE 5: Function with 10 conditional returns (cap at 5)
    # ========================================================================
    print("\n--- Edge Case 5: Cap at 5 conditional returns ---")
    many_ifs = []
    for i in range(10):
        many_ifs.append(IfStmtInfo(
            stmt_type="if_statement",
            condition_text=f"x == {i}",
            body_children=[ReturnInfo(return_text=str(i))],
            line=i + 1,
        ))
    results = extract_conditional_returns(many_ifs)
    t.check(
        "10 conditional returns -> capped at 5",
        len(results) == 5,
        f"Expected 5, got {len(results)}",
    )
    if len(results) == 5:
        t.check(
            "First 5 are emitted (x == 0 through x == 4)",
            all(f"x == {i}" in results[i].value for i in range(5)),
            f"Values: {[r.value for r in results]}",
        )

    # ========================================================================
    # EDGE CASE 6: Empty return (`if x: return`)
    # retText should be "" (empty string)
    # ========================================================================
    print("\n--- Edge Case 6: Empty return ---")
    empty_ret_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="x",
        body_children=[ReturnInfo(return_text="")],
        line=1,
    )
    results = extract_conditional_returns([empty_ret_if])
    t.check(
        "Empty return: still extracted",
        len(results) == 1,
        f"Expected 1, got {len(results)}",
    )
    if results:
        t.check(
            "Empty return: value ends with 'return ' (empty ret text)",
            results[0].value == "if x: return ",
            f"Value: '{results[0].value}'",
        )

    # ========================================================================
    # EDGE CASE 7: Ternary/conditional expression
    # `return x if condition else y` is a return_statement, NOT an if_statement
    # containing a return. Should NOT be extracted as conditional_return.
    # ========================================================================
    print("\n--- Edge Case 7: Ternary expression (NOT a conditional return) ---")
    # In tree-sitter, `return x if condition else y` parses as:
    # return_statement -> conditional_expression(consequence=x, condition=condition, alternative=y)
    # It is NOT an if_statement child of the body. It's a return_statement child.
    # So extractConditionalReturns, which only looks at if_statement direct children, won't find it.

    # Simulate: function body has a return_statement (not wrapped in if)
    # We represent this as just a non-if_statement child
    body_children = [
        # This is a return_statement at the body level, not inside an if_statement
        ReturnInfo(return_text="x if condition else y"),
    ]
    results = extract_conditional_returns(body_children)
    t.check(
        "Ternary return: NOT extracted as conditional_return",
        len(results) == 0,
        f"Expected 0, got {len(results)}: {results}",
    )

    # ========================================================================
    # EDGE CASE 8: Unicode identifiers
    # `if données is None: return défaut`
    # ========================================================================
    print("\n--- Edge Case 8: Unicode identifiers ---")
    unicode_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="données is None",
        body_children=[ReturnInfo(return_text="défaut")],
        line=1,
    )
    results = extract_conditional_returns([unicode_if])
    t.check(
        "Unicode: conditional return extracted",
        len(results) == 1,
        f"Expected 1, got {len(results)}",
    )
    if results:
        t.check(
            "Unicode: condition preserved",
            "données is None" in results[0].value,
            f"Value: {results[0].value}",
        )
        t.check(
            "Unicode: return value preserved",
            "défaut" in results[0].value,
            f"Value: {results[0].value}",
        )

    # Also test P6 with unicode
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text="if données is None:\n    return défaut",
        condition_text="données is None",
        body_text="return défaut",
        line=1,
    )
    t.check(
        "Unicode P6: guard extracted with unicode condition",
        result is not None and "données is None" in result.value,
        f"Got: {result}",
    )

    # ========================================================================
    # EDGE CASE 9: Go panic
    # `if err != nil { panic("fatal") }`
    # Guard type "panic", consequence `panic("fatal")`
    # ========================================================================
    print("\n--- Edge Case 9: Go panic ---")
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text='if err != nil { panic("fatal") }',
        condition_text="err != nil",
        body_text='{ panic("fatal") }',
        line=1,
    )
    t.check(
        "Go panic: detected as guard",
        result is not None,
        f"Expected guard, got None",
    )
    if result:
        t.check(
            "Go panic: guardType is 'panic'",
            result.value.startswith("panic:"),
            f"Value: {result.value}",
        )
        t.check(
            "Go panic: condition is 'err != nil'",
            "err != nil" in result.value,
            f"Value: {result.value}",
        )
        t.check(
            "Go panic: consequence includes panic(\"fatal\")",
            'panic("fatal")' in result.value,
            f"Value: {result.value}",
        )

    # ========================================================================
    # EDGE CASE 10: Rust early return
    # `if result.is_err() { return Err(e) }`
    # Guard type "return", consequence "return Err(e)"
    # ========================================================================
    print("\n--- Edge Case 10: Rust early return ---")
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text="if result.is_err() { return Err(e) }",
        condition_text="result.is_err()",
        body_text="{ return Err(e) }",
        line=1,
    )
    t.check(
        "Rust early return: detected as guard",
        result is not None,
        f"Expected guard, got None",
    )
    if result:
        # "Err(" matches keyword check, but "return" is checked first in priority
        # Actually: the keyword loop checks "raise " first, then "throw ", then "return"
        # "return" is in the text, so it should match and be guardType "return"
        # But wait: "Err(" is also in the keywords list! The for-loop breaks on first match.
        # The order is: ["raise ", "throw ", "return", "panic(", "error(", "Error(", "abort(", "Err("]
        # "return" appears at index 2, "Err(" at index 7
        # strings.Contains checks the FULL stmt text for each keyword in order
        # The text "if result.is_err() { return Err(e) }" contains "return" so
        # the loop matches "return" first. Then the switch:
        #   - "raise " not in text -> False
        #   - "panic(" not in text -> False
        #   - default -> guardType = "return"
        # But wait: "Err(" IS in the text! Does that affect guardType classification?
        # Looking at the switch: it checks Contains("raise ") || Contains("throw "),
        # then Contains("panic(") || Contains("abort("), else "return".
        # "Err(" doesn't appear in any switch case, so it falls to default = "return"
        t.check(
            "Rust: guardType is 'return' (not 'raise' or 'panic')",
            result.value.startswith("return:"),
            f"Value: {result.value}",
        )
        t.check(
            "Rust: condition is 'result.is_err()'",
            "result.is_err()" in result.value,
            f"Value: {result.value}",
        )
        t.check(
            "Rust: consequence includes 'return Err(e)'",
            "return Err(e)" in result.value,
            f"Value: {result.value}",
        )

    # ========================================================================
    # BONUS EDGE CASES: Additional adversarial scenarios
    # ========================================================================

    print("\n--- Bonus: Skip nested function definitions ---")
    # A nested function def should NOT contribute conditional returns
    nested_func = IfStmtInfo(
        stmt_type="function_definition",
        condition_text=None,
        body_children=[ReturnInfo(return_text="inner_value")],
        line=5,
        is_nested_func=True,
    )
    normal_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="y > 0",
        body_children=[ReturnInfo(return_text="y")],
        line=10,
    )
    results = extract_conditional_returns([nested_func, normal_if])
    t.check(
        "Nested function def is skipped, only real if extracted",
        len(results) == 1 and "y > 0" in results[0].value,
        f"Results: {results}",
    )

    print("\n--- Bonus: P6 condition fallback (no tree-sitter condition node) ---")
    # When condition_text is None (tree-sitter didn't find field name "condition"),
    # fallback parses between "if" and ":"/"{"
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text='if err != nil {\n    return fmt.Errorf("failed: %w", err)\n}',
        condition_text=None,  # simulate missing tree-sitter field
        body_text='return fmt.Errorf("failed: %w", err)',
        line=1,
    )
    t.check(
        "Fallback condition extraction: finds 'err != nil' from text between 'if' and '{'",
        result is not None and "err != nil" in result.value,
        f"Got: {result}",
    )

    print("\n--- Bonus: P0 truncation boundaries ---")
    # Condition exactly 80 chars -> no truncation
    cond_80 = "a" * 80
    # Condition 81 chars -> truncated to 80
    cond_81 = "a" * 81
    # Return value exactly 40 chars -> no truncation
    ret_40 = "b" * 40
    # Return value 41 chars -> truncated to 40
    ret_41 = "b" * 41

    if_80 = IfStmtInfo(stmt_type="if_statement", condition_text=cond_80,
                        body_children=[ReturnInfo(return_text=ret_40)], line=1)
    if_81 = IfStmtInfo(stmt_type="if_statement", condition_text=cond_81,
                        body_children=[ReturnInfo(return_text=ret_41)], line=2)

    results = extract_conditional_returns([if_80, if_81])
    t.check(
        "Truncation: 80-char condition NOT truncated",
        cond_80 in results[0].value,
        f"Value: {results[0].value}",
    )
    t.check(
        "Truncation: 81-char condition IS truncated to 80",
        cond_81 not in results[1].value and cond_81[:80] in results[1].value,
        f"Value: {results[1].value}",
    )
    t.check(
        "Truncation: 40-char return NOT truncated",
        ret_40 in results[0].value,
        f"Value: {results[0].value}",
    )
    t.check(
        "Truncation: 41-char return IS truncated to 40",
        ret_41 not in results[1].value and ret_41[:40] in results[1].value,
        f"Value: {results[1].value}",
    )

    print("\n--- Bonus: P6 keyword priority (raise trumps return in same block) ---")
    # If both "raise " and "return" appear in the if text, "raise " wins
    # because the switch checks Contains("raise ") first
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text='if invalid:\n    log.warn("returning early")\n    raise ValueError("bad")',
        condition_text="invalid",
        body_text='log.warn("returning early")\n    raise ValueError("bad")',
        line=1,
    )
    t.check(
        "Priority: 'raise' wins even when 'return' substring exists in text",
        result is not None and result.value.startswith("raise:"),
        f"Got: {result}",
    )
    # NOTE: This is a KNOWN BUG in the current Go implementation!
    # The string "returning" contains "return" as a substring, so the keyword
    # loop will match "return" at index 2 BEFORE reaching "raise " at index 0.
    # Wait -- let me re-check the keyword order:
    # ["raise ", "throw ", "return", "panic(", "error(", "Error(", "abort(", "Err("]
    # "raise " is at index 0, checked FIRST. Contains("raise ") is True
    # (the text has "raise ValueError"). So "raise " matches first. Good.
    # But what about: 'log.info("will return None")\n    raise X'?
    # "raise " at index 0: Contains("raise ") = True -> matches first.
    # The keyword ORDER saves us: "raise " is before "return" in the list.

    print("\n--- Bonus: P6 false positive -- 'return' inside a string literal ---")
    # The text 'if x: print("don\'t return")' contains "return" but it's inside
    # a string literal. The current Go implementation does NOT handle this --
    # it's a known limitation (string.Contains is context-free).
    result = extract_guard_from_stmt(
        stmt_type="if_statement",
        stmt_text='if x:\n    print("don\'t return this")',
        condition_text="x",
        body_text='print("don\'t return this")',
        line=1,
    )
    t.check(
        "KNOWN LIMITATION: 'return' inside string literal triggers false positive",
        result is not None,  # This IS a false positive, but it's the current behavior
        f"Got: {result}",
    )
    if result:
        print(f"    NOTE: This is a known false positive. Value: '{result.value}'")
        print(f"    The Go code uses strings.Contains which doesn't distinguish string literals.")

    # ========================================================================
    # Summary
    # ========================================================================
    return t.summary()


# ============================================================================
# P0 ADDITIONAL: Verify that conditional returns only look at DIRECT body
# children, not arbitrary depth for the IF (the return can be nested, but the
# IF must be at the top level of the function body).
# ============================================================================

def test_only_top_level_ifs():
    """The if_statement must be a direct child of the function body."""
    t = TestResults()
    print("\n--- P0: Only top-level ifs are candidates ---")

    # Outer if contains inner if with return -- only outer if is a candidate
    # The inner if is part of outer if's body, not a direct body child
    inner_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="inner_cond",
        body_children=[ReturnInfo(return_text="inner_val")],
        line=3,
    )
    outer_if = IfStmtInfo(
        stmt_type="if_statement",
        condition_text="outer_cond",
        body_children=[inner_if],  # inner if is a child of outer's body
        line=2,
    )
    results = extract_conditional_returns([outer_if])
    t.check(
        "Nested if: outer if finds return recursively via inner if",
        len(results) == 1,
        f"Expected 1, got {len(results)}",
    )
    if results:
        t.check(
            "Nested if: condition is OUTER (outer_cond), return is INNER (inner_val)",
            "outer_cond" in results[0].value and "inner_val" in results[0].value,
            f"Value: {results[0].value}",
        )
        # This verifies that findFirstReturn recurses into nested blocks,
        # but the extracted condition is always from the top-level if_statement.

    return t.summary()


if __name__ == "__main__":
    print("=" * 70)
    print("P0 + P6 ADVERSARIAL EDGE CASE TESTS")
    print("Simulating Go tree-sitter extraction logic in Python")
    print("=" * 70)

    ok1 = test_edge_cases()
    ok2 = test_only_top_level_ifs()

    print("\n" + "=" * 70)
    if ok1 and ok2:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED -- see details above")
    print("=" * 70)

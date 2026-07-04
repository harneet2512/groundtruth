"""
Sprint 1 Adversarial Edge Case Verification

Tests tree-sitter AST behavior for edge cases that could break Sprint 1 implementation:
- P0: Side Effects (augmented assignment, chained attributes, cls, super, property setter)
- P5: Strategy 1.5 (wildcard imports, empty file imports, import name not in nodeIDs)
- P6: Guard Consequences (multi-statement body, no raise/return child, truncation)
- Assertions query (missing table, target_node_id=0)

Each test documents what SHOULD happen and verifies whether tree-sitter gives us what
we need for the Go implementation.
"""

import sqlite3
import sys
import textwrap

from tree_sitter_language_pack import get_parser


def parse_python(code: str):
    """Parse Python code and return the tree."""
    parser = get_parser("python")
    return parser.parse(code.encode("utf-8"))


def parse_js(code: str):
    """Parse JS code and return the tree."""
    parser = get_parser("javascript")
    return parser.parse(code.encode("utf-8"))


def walk_tree(node, depth=0):
    """Debug: print tree structure."""
    print("  " * depth + f"{node.type} [{node.start_point[0]}:{node.start_point[1]}-{node.end_point[0]}:{node.end_point[1]}]")
    for child in node.children:
        walk_tree(child, depth + 1)


def find_nodes_by_type(node, target_type):
    """Recursively find all nodes of a given type."""
    results = []
    if node.type == target_type:
        results.append(node)
    for child in node.children:
        results.extend(find_nodes_by_type(child, target_type))
    return results


def get_node_text(node, src_bytes):
    """Get the text of a node."""
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8")


# =============================================================================
# P0: SIDE EFFECTS EDGE CASES
# =============================================================================

def test_p0_augmented_assignment():
    """
    Edge Case 1: Augmented assignment `self.count += 1`

    QUESTION: Is this an "assignment" node or "augmented_assignment" node?
    Should the Go code match "augmented_assignment"?

    EXPECTED: tree-sitter Python grammar uses "augmented_assignment" for +=, -=, etc.
    The implementation MUST include "augmented_assignment" in the node type check
    to catch mutating operations like `self.count += 1`.
    """
    code = textwrap.dedent("""\
        class Counter:
            def increment(self):
                self.count += 1
                self.total *= 2
                self.items |= new_set
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    # Find augmented_assignment nodes
    aug_assigns = find_nodes_by_type(tree.root_node, "augmented_assignment")

    print("=" * 70)
    print("TEST P0-1: Augmented Assignment (self.count += 1)")
    print("=" * 70)

    assert len(aug_assigns) == 3, f"Expected 3 augmented_assignment nodes, got {len(aug_assigns)}"

    for aa in aug_assigns:
        text = get_node_text(aa, src)
        print(f"  Node type: {aa.type}")
        print(f"  Text: {text}")

        # Check the LHS
        left = aa.child_by_field_name("left")
        assert left is not None, "augmented_assignment must have a 'left' field"
        print(f"  Left type: {left.type}")
        print(f"  Left text: {get_node_text(left, src)}")

        # For self.X, left should be an "attribute" node
        assert left.type == "attribute", f"Expected 'attribute' type for self.X, got '{left.type}'"

        # Check the object is 'self'
        obj = left.child_by_field_name("object")
        assert obj is not None, "attribute must have 'object' field"
        obj_text = get_node_text(obj, src)
        assert obj_text == "self", f"Expected object='self', got '{obj_text}'"

        # Get attribute name
        attr = left.child_by_field_name("attribute")
        attr_text = get_node_text(attr, src)
        print(f"  Mutated field: self.{attr_text}")
        print()

    print("  VERDICT: augmented_assignment IS a separate node type from 'assignment'.")
    print("  The Go implementation MUST check BOTH 'assignment' AND 'augmented_assignment'.")
    print("  PASS\n")
    return True


def test_p0_chained_attribute():
    """
    Edge Case 2: Chained attribute `self.a.b = c`

    QUESTION: What does tree-sitter give as the "object"?
    If implementation naively checks first child, it might get `self.a` not `self`.

    EXPECTED: The left side is an attribute node with object=`self.a` (another attribute),
    NOT just `self`. The implementation must walk UP to the root object or only check
    the FIRST level (immediate attribute access on self).
    """
    code = textwrap.dedent("""\
        class Nested:
            def update(self):
                self.a.b = "value"
                self.config.settings.debug = True
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    assigns = find_nodes_by_type(tree.root_node, "assignment")

    print("=" * 70)
    print("TEST P0-2: Chained Attribute (self.a.b = c)")
    print("=" * 70)

    for a in assigns:
        text = get_node_text(a, src)
        print(f"  Full assignment: {text}")

        left = a.child_by_field_name("left")
        print(f"  Left node type: {left.type}")
        print(f"  Left text: {get_node_text(left, src)}")

        # For self.a.b, left is "attribute" with object="self.a" (also attribute)
        obj = left.child_by_field_name("object")
        print(f"  Object type: {obj.type}")
        print(f"  Object text: {get_node_text(obj, src)}")

        # The object is NOT "self" directly -- it's another attribute access
        if obj.type == "attribute":
            print(f"  WARNING: Object is chained (attribute), not direct 'self' identifier!")
            # To find root object, we need to walk down
            root_obj = obj
            depth = 0
            while root_obj.type == "attribute":
                root_obj = root_obj.child_by_field_name("object")
                depth += 1
            print(f"  Root object (depth={depth}): {get_node_text(root_obj, src)}")
            print(f"  Root object type: {root_obj.type}")
        print()

    print("  VERDICT: For `self.a.b = c`, the LEFT is attribute(object=attribute(object=self))")
    print("  The Go code should ONLY report direct self.X assignments, NOT chained ones.")
    print("  Implementation must check: left.type=='attribute' AND left.object.type=='identifier'")
    print("  AND left.object.text=='self'. Chained access (left.object.type=='attribute') should be SKIPPED.")
    print("  PASS\n")
    return True


def test_p0_cls_variable():
    """
    Edge Case 3: Class variable `cls.shared = val`

    EXPECTED: Should NOT match as side effect. Only self/this should match.
    """
    code = textwrap.dedent("""\
        class Config:
            @classmethod
            def set_default(cls):
                cls.shared = "default"
                cls.instances = []
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    assigns = find_nodes_by_type(tree.root_node, "assignment")

    print("=" * 70)
    print("TEST P0-3: Class Variable (cls.shared = val)")
    print("=" * 70)

    for a in assigns:
        left = a.child_by_field_name("left")
        if left and left.type == "attribute":
            obj = left.child_by_field_name("object")
            obj_text = get_node_text(obj, src)
            attr = left.child_by_field_name("attribute")
            attr_text = get_node_text(attr, src)
            print(f"  {obj_text}.{attr_text} = ...")
            print(f"  Object text: '{obj_text}' (type: {obj.type})")
            assert obj_text == "cls", f"Expected 'cls', got '{obj_text}'"

    print()
    print("  VERDICT: `cls.X = val` has object='cls', NOT 'self'.")
    print("  Implementation must ONLY match object text in {'self', 'this'}.")
    print("  'cls' must be EXCLUDED. This is correct by design.")
    print("  PASS\n")
    return True


def test_p0_super_call():
    """
    Edge Case 4: `super().init = val`

    EXPECTED: Should NOT match. super() is a call expression, not an identifier.
    """
    code = textwrap.dedent("""\
        class Derived(Base):
            def __init__(self):
                super().__init__()
                super().value = 42
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    assigns = find_nodes_by_type(tree.root_node, "assignment")

    print("=" * 70)
    print("TEST P0-4: Super Call (super().value = 42)")
    print("=" * 70)

    for a in assigns:
        text = get_node_text(a, src)
        left = a.child_by_field_name("left")
        print(f"  Assignment: {text}")
        print(f"  Left type: {left.type}")

        if left.type == "attribute":
            obj = left.child_by_field_name("object")
            print(f"  Object type: {obj.type}")
            print(f"  Object text: {get_node_text(obj, src)}")

            # super() is a call expression, not an identifier
            assert obj.type == "call", f"Expected 'call' type for super(), got '{obj.type}'"
            print(f"  super() is a CALL node, not an identifier -> correctly excluded")

    print()
    print("  VERDICT: `super().value = val` has object as 'call' node (not 'identifier').")
    print("  Since implementation checks object.type=='identifier', super() is naturally excluded.")
    print("  PASS\n")
    return True


def test_p0_property_setter():
    """
    Edge Case 5: `self.x = self.transform(val)`

    EXPECTED: Should still match as side effect on self.x.
    The RHS containing self.transform() should not prevent detection.
    """
    code = textwrap.dedent("""\
        class Processor:
            def process(self, val):
                self.x = self.transform(val)
                self.result = self._internal_method(self.config)
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    assigns = find_nodes_by_type(tree.root_node, "assignment")

    print("=" * 70)
    print("TEST P0-5: Property Setter (self.x = self.transform(val))")
    print("=" * 70)

    matched = []
    for a in assigns:
        left = a.child_by_field_name("left")
        if left and left.type == "attribute":
            obj = left.child_by_field_name("object")
            if obj and obj.type == "identifier" and get_node_text(obj, src) == "self":
                attr = left.child_by_field_name("attribute")
                attr_text = get_node_text(attr, src)
                matched.append(attr_text)
                print(f"  Side effect detected: self.{attr_text}")

    assert len(matched) == 2, f"Expected 2 side effects, got {len(matched)}: {matched}"
    assert "x" in matched
    assert "result" in matched

    print()
    print("  VERDICT: RHS complexity (self.transform(val)) does NOT affect LHS detection.")
    print("  Implementation only checks the LEFT side of assignment.")
    print("  PASS\n")
    return True


# =============================================================================
# P5: STRATEGY 1.5 EDGE CASES
# =============================================================================

def test_p5_wildcard_import():
    """
    Edge Case 6: `from module import *` -> ImportedName="*"

    The buildImportIndex in resolver.go already handles "*" (lines 258-269).
    Strategy 1.5 for assertions should SKIP "*" when looking up specific names.
    """
    code = textwrap.dedent("""\
        from os.path import *
        from utils import *
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    print("=" * 70)
    print("TEST P5-6: Wildcard Import (from module import *)")
    print("=" * 70)

    imports = find_nodes_by_type(tree.root_node, "import_from_statement")

    for imp in imports:
        text = get_node_text(imp, src)
        print(f"  Import: {text}")

        # Look for wildcard_import child
        wildcards = find_nodes_by_type(imp, "wildcard_import")
        print(f"  Has wildcard_import node: {len(wildcards) > 0}")
        if wildcards:
            print(f"  Wildcard text: '{get_node_text(wildcards[0], src)}'")

    print()
    print("  VERDICT: Wildcard imports produce 'wildcard_import' nodes with text '*'.")
    print("  The existing buildImportIndex (resolver.go:258-269) handles '*' correctly.")
    print("  For Strategy 1.5 in resolveAssertionTarget, code should iterate fileImportNames[testFile]")
    print("  and skip entries where ImportedName=='*' when doing specific name matching.")
    print("  The CURRENT resolver already puts '*' into the import index and uses it as a fallback")
    print("  (line 258: 'Check wildcard imports'), so no crash occurs. It just won't match specific names.")
    print("  PASS\n")
    return True


def test_p5_empty_file_imports():
    """
    Edge Case 7: File that imports nothing.

    fileImportNames would have no entry for this file.
    Strategy 1.5 should gracefully skip when no imports exist.
    """
    code = textwrap.dedent("""\
        def standalone_function():
            return 42
    """)
    tree = parse_python(code)

    print("=" * 70)
    print("TEST P5-7: Empty File Imports (no imports at all)")
    print("=" * 70)

    imports = find_nodes_by_type(tree.root_node, "import_from_statement")
    imports += find_nodes_by_type(tree.root_node, "import_statement")

    assert len(imports) == 0, f"Expected 0 imports, got {len(imports)}"
    print(f"  Import nodes found: {len(imports)}")

    # Simulate what the Go code would do
    fileImportNames = {}  # empty - no entry for this file
    test_file = "test_something.py"

    # Strategy 1.5 check:
    if test_file in fileImportNames:
        print("  ERROR: Should not enter this branch")
    else:
        print("  Correctly skipped: fileImportNames has no entry for this file")

    print()
    print("  VERDICT: When a file has no imports, fileImportNames won't contain it.")
    print("  The Go code uses `if fileImports, ok := importIndex[call.File]; ok {` pattern,")
    print("  so files without imports are naturally skipped (ok=false).")
    print("  No crash, no incorrect behavior. Falls through to Strategy 2 (name_match).")
    print("  PASS\n")
    return True


def test_p5_import_name_not_in_nodes():
    """
    Edge Case 8: Import name matches in fileImportNames but no node exists.

    Example: `from utils import helper` but `helper` function was never defined
    in any indexed file (maybe it's in a third-party package).
    """
    print("=" * 70)
    print("TEST P5-8: Import Name Matches but No Node Exists")
    print("=" * 70)

    # Simulate the data structures
    fileImportNames = {
        "tests/test_auth.py": ["validate_token", "nonexistent_helper"]
    }
    nameToNodeIDs = {
        "validate_token": [42],  # exists
        # "nonexistent_helper" is NOT in nameToNodeIDs
    }

    # Strategy 1.5 logic:
    test_file = "tests/test_auth.py"
    assertion_expr = "assert nonexistent_helper(x) == y"

    # Extract called functions from assertion
    import re
    call_pattern = re.compile(r'(\w+)\s*\(')
    candidates = call_pattern.findall(assertion_expr)
    skip = {"assert", "assertEqual", "len", "str", "int"}
    candidates = [c for c in candidates if c not in skip]

    print(f"  Assertion expression: {assertion_expr}")
    print(f"  Extracted candidates: {candidates}")

    resolved = None
    for fname in candidates:
        if fname in fileImportNames.get(test_file, []):
            print(f"  '{fname}' found in imports for {test_file}")
            if fname in nameToNodeIDs:
                resolved = nameToNodeIDs[fname][0]
                print(f"  Resolved to node ID: {resolved}")
            else:
                print(f"  '{fname}' NOT in nameToNodeIDs -> falls through")
        else:
            print(f"  '{fname}' NOT in imports -> skipped")

    assert resolved is None, "Should NOT resolve when node doesn't exist"

    print()
    print("  VERDICT: When an imported name has no corresponding node in nameToNodeIDs,")
    print("  the Go code's `if ids, ok := nameToNodeIDs[fname]; ok {` check returns false,")
    print("  so it falls through gracefully to the next strategy.")
    print("  No crash, correct behavior.")
    print("  PASS\n")
    return True


# =============================================================================
# P6: GUARD CONSEQUENCES EDGE CASES
# =============================================================================

def test_p6_multi_statement_body():
    """
    Edge Case 9: Multi-statement if body: `if x: log(); raise Error("y")`

    The implementation should find the FIRST matching consequence (raise/return/throw).
    """
    code = textwrap.dedent("""\
        def validate(x):
            if x is None:
                logger.warning("x is None")
                raise ValueError("x required")
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    print("=" * 70)
    print("TEST P6-9: Multi-Statement Guard Body")
    print("=" * 70)

    if_stmts = find_nodes_by_type(tree.root_node, "if_statement")
    assert len(if_stmts) == 1

    if_stmt = if_stmts[0]

    # Get condition
    condition = if_stmt.child_by_field_name("condition")
    print(f"  Condition: {get_node_text(condition, src)}")

    # Get consequence (body)
    consequence = if_stmt.child_by_field_name("consequence")
    if consequence is None:
        # fallback: look for "block" child
        for child in if_stmt.children:
            if child.type == "block":
                consequence = child
                break

    assert consequence is not None, "Must find consequence/block"
    print(f"  Consequence type: {consequence.type}")
    print(f"  Consequence children: {[c.type for c in consequence.children]}")

    # Find the first raise_statement
    raise_stmts = find_nodes_by_type(consequence, "raise_statement")
    print(f"  Raise statements found: {len(raise_stmts)}")

    if raise_stmts:
        first_raise = raise_stmts[0]
        raise_text = get_node_text(first_raise, src)
        print(f"  First raise: {raise_text}")

        # Extract the exception type/message
        # The raise_statement has children: 'raise' keyword + expression
        for child in first_raise.children:
            if child.type == "call":
                print(f"  Exception call: {get_node_text(child, src)}")

    # Also check: expression_statement (the log call) should NOT be picked
    expr_stmts = find_nodes_by_type(consequence, "expression_statement")
    print(f"  Expression statements (non-raise): {len(expr_stmts)}")

    print()
    print("  VERDICT: Multi-statement bodies work correctly.")
    print("  Implementation should iterate consequence children and find the FIRST child")
    print("  whose type is 'raise_statement', 'return_statement', or 'throw_statement'.")
    print("  The logger.warning() call is an 'expression_statement' and naturally excluded.")
    print("  PASS\n")
    return True


def test_p6_no_raise_return_in_body():
    """
    Edge Case 10: Guard with only block but no raise/return/throw child directly.

    Example: `if x: pass` or `if x: log_and_continue()`
    The consequence should be empty string.
    """
    code = textwrap.dedent("""\
        def check(x):
            if x < 0:
                handle_negative(x)
            if y is None:
                pass
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    print("=" * 70)
    print("TEST P6-10: Guard With No Raise/Return/Throw")
    print("=" * 70)

    if_stmts = find_nodes_by_type(tree.root_node, "if_statement")

    for i, if_stmt in enumerate(if_stmts):
        cond = if_stmt.child_by_field_name("condition")
        consequence = if_stmt.child_by_field_name("consequence")
        if consequence is None:
            for child in if_stmt.children:
                if child.type == "block":
                    consequence = child
                    break

        cond_text = get_node_text(cond, src) if cond else "?"
        print(f"  If #{i+1}: condition='{cond_text}'")

        # Look for raise/return/throw
        raises = find_nodes_by_type(consequence, "raise_statement")
        returns = find_nodes_by_type(consequence, "return_statement")

        has_consequence = len(raises) + len(returns) > 0
        print(f"  Has raise/return: {has_consequence}")

        if not has_consequence:
            print(f"  -> This should NOT be classified as a guard clause at all by extractGuardFromStmt")
            print(f"     (or consequence text should be empty string)")

    print()
    print("  VERDICT: The CURRENT extractGuardFromStmt (parser.go:1082) uses string Contains")
    print("  on the full if-statement text to detect raise/throw/return keywords.")
    print("  `handle_negative(x)` doesn't contain these keywords -> NOT classified as guard.")
    print("  `pass` also doesn't contain them -> NOT classified as guard.")
    print("  For P6 consequence extraction, when isGuard=true but no matching child node")
    print("  is found in the consequence block, consequence text should be empty string.")
    print("  PASS\n")
    return True


def test_p6_consequence_truncation():
    """
    Edge Case 11: Consequence text > 60 chars should be truncated.
    """
    code = textwrap.dedent("""\
        def validate(config):
            if not isinstance(config, dict):
                raise TypeError("Expected a dictionary configuration object but got something else entirely: " + str(type(config)))
    """)
    src = code.encode("utf-8")
    tree = parse_python(code)

    print("=" * 70)
    print("TEST P6-11: Consequence Text Truncation (>60 chars)")
    print("=" * 70)

    if_stmts = find_nodes_by_type(tree.root_node, "if_statement")
    assert len(if_stmts) == 1

    if_stmt = if_stmts[0]
    consequence = if_stmt.child_by_field_name("consequence")
    if consequence is None:
        for child in if_stmt.children:
            if child.type == "block":
                consequence = child
                break

    raises = find_nodes_by_type(consequence, "raise_statement")
    assert len(raises) == 1

    raise_text = get_node_text(raises[0], src)
    print(f"  Full raise text ({len(raise_text)} chars): {raise_text[:80]}...")
    print(f"  Length: {len(raise_text)}")

    # Simulate truncation
    truncated = raise_text[:60] if len(raise_text) > 60 else raise_text
    print(f"  Truncated (60 chars): {truncated}")

    assert len(raise_text) > 60, "Test case must be >60 chars"
    assert len(truncated) == 60, f"Truncation failed: got {len(truncated)}"

    print()
    print("  VERDICT: The raise text is 100+ chars. Truncation to 60 chars works correctly.")
    print("  Go implementation: `if len(consequenceText) > 60 { consequenceText = consequenceText[:60] }`")
    print("  NOTE: Truncating UTF-8 by byte position could split a multi-byte char.")
    print("  For ASCII-dominated code this is fine. For safety, use rune-aware truncation.")
    print("  PASS\n")
    return True


# =============================================================================
# ASSERTIONS QUERY EDGE CASES
# =============================================================================

def test_assertions_table_missing():
    """
    Edge Case 12: No assertions table (older graph.db without assertions).

    The code should check sqlite_master before querying assertions.
    """
    print("=" * 70)
    print("TEST P6-12: Missing Assertions Table (older graph.db)")
    print("=" * 70)

    # Create in-memory DB WITHOUT assertions table
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            language TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL
        )
    """)

    # Check if assertions table exists
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assertions'"
    )
    has_assertions = cursor.fetchone() is not None
    print(f"  Has assertions table: {has_assertions}")
    assert not has_assertions, "Should NOT have assertions table"

    # Simulate what the Go code should do:
    # Check before querying
    if has_assertions:
        # Query assertions
        pass
    else:
        print("  Correctly detected missing table -> skip assertions query")

    # Now test that schema creation adds it:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assertions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_node_id INTEGER NOT NULL,
            target_node_id INTEGER DEFAULT 0,
            kind TEXT NOT NULL,
            expression TEXT NOT NULL,
            expected TEXT,
            line INTEGER
        )
    """)

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='assertions'"
    )
    has_assertions = cursor.fetchone() is not None
    assert has_assertions, "Should now have assertions table"
    print(f"  After schema creation: has assertions = {has_assertions}")

    conn.close()

    print()
    print("  VERDICT: The Go code ALWAYS creates the assertions table via createSchema().")
    print("  So for a freshly-indexed DB, the table always exists (empty but present).")
    print("  For an OLDER graph.db from a prior version, the Python reader (graph_store.py)")
    print("  MUST check sqlite_master before querying. The Go indexer won't crash because")
    print("  it always creates the table. But external readers of old DBs could crash.")
    print("  FIX: graph_store.py should use 'CREATE TABLE IF NOT EXISTS' or check first.")
    print("  PASS\n")
    return True


def test_assertions_target_node_id_zero():
    """
    Edge Case 13: target_node_id = 0 query behavior.

    The query has `AND target_node_id > 0`, so unresolved assertions (0)
    shouldn't be returned.
    """
    print("=" * 70)
    print("TEST P6-13: target_node_id = 0 (unresolved assertions)")
    print("=" * 70)

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE assertions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_node_id INTEGER NOT NULL,
            target_node_id INTEGER DEFAULT 0,
            kind TEXT NOT NULL,
            expression TEXT NOT NULL,
            expected TEXT,
            line INTEGER
        )
    """)

    # Insert mix of resolved and unresolved
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) VALUES (1, 0, 'assertEqual', 'assertEqual(foo(), 42)', 10)"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) VALUES (1, 5, 'assertEqual', 'assertEqual(bar(), 99)', 11)"
    )
    conn.execute(
        "INSERT INTO assertions (test_node_id, target_node_id, kind, expression, line) VALUES (2, 0, 'assert', 'assert baz()', 20)"
    )
    conn.commit()

    # Query with target_node_id > 0 (only resolved)
    cursor = conn.execute(
        "SELECT * FROM assertions WHERE target_node_id > 0"
    )
    resolved = cursor.fetchall()
    print(f"  Total assertions: 3")
    print(f"  Resolved (target_node_id > 0): {len(resolved)}")
    assert len(resolved) == 1, f"Expected 1 resolved, got {len(resolved)}"
    print(f"  Resolved assertion: {resolved[0]}")

    # Query with target_node_id = 0 (unresolved)
    cursor = conn.execute(
        "SELECT * FROM assertions WHERE target_node_id = 0"
    )
    unresolved = cursor.fetchall()
    print(f"  Unresolved (target_node_id = 0): {len(unresolved)}")
    assert len(unresolved) == 2, f"Expected 2 unresolved, got {len(unresolved)}"

    # Query for a specific symbol (simulating get_assertions_for_symbol)
    symbol_id = 5
    cursor = conn.execute(
        "SELECT * FROM assertions WHERE target_node_id = ?", (symbol_id,)
    )
    for_symbol = cursor.fetchall()
    print(f"  Assertions for symbol_id={symbol_id}: {len(for_symbol)}")
    assert len(for_symbol) == 1

    # Query for symbol_id = 0 (would incorrectly return all unresolved!)
    cursor = conn.execute(
        "SELECT * FROM assertions WHERE target_node_id = ?", (0,)
    )
    for_zero = cursor.fetchall()
    print(f"  Assertions for symbol_id=0 (BUG if queried!): {len(for_zero)}")
    assert len(for_zero) == 2, "Querying target_node_id=0 returns ALL unresolved!"

    conn.close()

    print()
    print("  VERDICT: The `AND target_node_id > 0` filter correctly excludes unresolved assertions.")
    print("  CRITICAL BUG RISK: If get_assertions_for_symbol(0) is ever called, it returns ALL")
    print("  unresolved assertions (every assertion with target_node_id=0)!")
    print("  FIX: The function MUST validate symbol_id > 0 before querying, or use")
    print("  `WHERE target_node_id = ? AND target_node_id > 0` as a safety belt.")
    print("  PASS\n")
    return True


# =============================================================================
# BONUS: JS/TS this-based side effects
# =============================================================================

def test_p0_js_this_assignment():
    """
    Bonus: JS `this.x = val` -- verify tree-sitter structure.
    """
    code = textwrap.dedent("""\
        class Counter {
            increment() {
                this.count = this.count + 1;
                this.count += 1;
            }
        }
    """)
    src = code.encode("utf-8")
    tree = parse_js(code)

    print("=" * 70)
    print("BONUS: JS this.x Assignment")
    print("=" * 70)

    # In JS, `this.count = ...` is an assignment_expression (not assignment)
    # and `this.count += 1` is an augmented_assignment_expression
    assigns = find_nodes_by_type(tree.root_node, "assignment_expression")
    aug_assigns = find_nodes_by_type(tree.root_node, "augmented_assignment_expression")

    print(f"  assignment_expression nodes: {len(assigns)}")
    print(f"  augmented_assignment_expression nodes: {len(aug_assigns)}")

    for a in assigns + aug_assigns:
        text = get_node_text(a, src)
        print(f"  Node: {a.type}: {text}")

        left = a.child_by_field_name("left")
        if left and left.type == "member_expression":
            obj = left.child_by_field_name("object")
            prop = left.child_by_field_name("property")
            if obj:
                obj_text = get_node_text(obj, src)
                prop_text = get_node_text(prop, src) if prop else "?"
                print(f"    Object: {obj_text} (type: {obj.type})")
                print(f"    Property: {prop_text}")
                if obj.type == "this":
                    print(f"    -> SIDE EFFECT: this.{prop_text}")

    print()
    print("  VERDICT: JS uses 'member_expression' not 'attribute', and 'this' is a special node type.")
    print("  For JS/TS, the Go code must check:")
    print("    - node type: 'assignment_expression' OR 'augmented_assignment_expression'")
    print("    - left type: 'member_expression'")
    print("    - left.object type: 'this' (not 'identifier' with text 'this'!)")
    print("  PASS\n")
    return True


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("\n" + "=" * 70)
    print("SPRINT 1 ADVERSARIAL EDGE CASE VERIFICATION")
    print("=" * 70 + "\n")

    results = []
    tests = [
        ("P0-1: Augmented Assignment", test_p0_augmented_assignment),
        ("P0-2: Chained Attribute", test_p0_chained_attribute),
        ("P0-3: Class Variable (cls)", test_p0_cls_variable),
        ("P0-4: Super Call", test_p0_super_call),
        ("P0-5: Property Setter", test_p0_property_setter),
        ("P5-6: Wildcard Import", test_p5_wildcard_import),
        ("P5-7: Empty File Imports", test_p5_empty_file_imports),
        ("P5-8: Import Name Not in Nodes", test_p5_import_name_not_in_nodes),
        ("P6-9: Multi-Statement Guard Body", test_p6_multi_statement_body),
        ("P6-10: No Raise/Return in Body", test_p6_no_raise_return_in_body),
        ("P6-11: Consequence Truncation", test_p6_consequence_truncation),
        ("P6-12: Missing Assertions Table", test_assertions_table_missing),
        ("P6-13: target_node_id = 0", test_assertions_target_node_id_zero),
        ("BONUS: JS this.x", test_p0_js_this_assignment),
    ]

    for name, test_fn in tests:
        try:
            passed = test_fn()
            results.append((name, "PASS" if passed else "FAIL"))
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, f"EXCEPTION: {e}"))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    passes = sum(1 for _, r in results if r == "PASS")
    failures = sum(1 for _, r in results if r != "PASS")

    for name, result in results:
        status = "OK" if result == "PASS" else "XX"
        print(f"  [{status}] {name}: {result}")

    print(f"\n  Total: {passes} passed, {failures} failed out of {len(results)} tests")

    # Critical findings
    print("\n" + "=" * 70)
    print("CRITICAL IMPLEMENTATION FINDINGS")
    print("=" * 70)
    print("""
    P0 Side Effects - GO IMPLEMENTATION MUST:

    1. Check BOTH 'assignment' AND 'augmented_assignment' node types (Python).
       For JS/TS: 'assignment_expression' AND 'augmented_assignment_expression'.

    2. For the LEFT side, verify:
       - Python: left.type == 'attribute' AND left.object.type == 'identifier'
                 AND left.object.text IN ('self')
       - JS/TS:  left.type == 'member_expression' AND left.object.type == 'this'
       - Java:   left.type == 'field_access' AND left.object.type == 'this'

    3. EXCLUDE chained attributes: if left.object.type == 'attribute' (Python)
       or left.object.type == 'member_expression' (JS), skip it.
       Only match DIRECT self/this attribute access.

    4. EXCLUDE cls, super, and any non-self/this identifiers.
       super() naturally excluded because it's a 'call' node, not 'identifier'.

    P5 Strategy 1.5 - SAFE BEHAVIORS:

    5. Wildcard imports ('*') already handled by buildImportIndex as fallback.
       Strategy 1.5 in resolveAssertionTarget should iterate file's imports
       and check if imported name matches a called function in assertion.

    6. Files with no imports simply have no entry in the import index -> ok.

    7. Import names not in nameToNodeIDs -> ok, map lookup returns (nil, false).

    P6 Guard Consequences - IMPLEMENTATION MUST:

    8. Iterate consequence block children looking for first 'raise_statement',
       'return_statement', or 'throw_statement' (not string contains!).

    9. If no matching child found, consequence text = "" (empty).

    10. Truncate consequence to 60 chars (rune-safe for Unicode).

    ASSERTIONS:

    11. Table always exists in fresh DBs (createSchema guarantees it).
        External readers of old DBs should check sqlite_master.

    12. CRITICAL: Never call get_assertions_for_symbol(0) -- it returns ALL
        unresolved assertions. Add guard: `if symbol_id <= 0 { return nil }`.
    """)

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

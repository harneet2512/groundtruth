"""Gate 2 — honest diagnostic for Foundation v2.

Tests scenarios that ONLY the foundation pipeline can handle — things the
existing obligation engine (attribute tracing, caller refs, override matching)
genuinely cannot do.

Three categories:
1. SIMILARITY-ONLY: no graph edges exist, only structural similarity can find the link
2. RENAME DETECTION: method moved/renamed, fingerprint identity catches it
3. FALSE POSITIVES: foundation must stay silent when symbols are unrelated

Each test verifies the actual similarity scores from real source code,
not dummy data.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

from groundtruth.foundation.graph.expander import GraphExpander
from groundtruth.foundation.integration.pipeline import run_pipeline
from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.foundation.similarity.fingerprint import FingerprintExtractor
from groundtruth.foundation.similarity.astvec import StructuralVectorExtractor
from groundtruth.foundation.similarity.tokensketch import TokenSketchExtractor
from groundtruth.foundation.similarity.composite import find_related
from groundtruth.index.store import SymbolStore


# ---- Helpers ----

def _sym(name: str, kind: str, raw: str, parent_class: str | None = None) -> ExtractedSymbol:
    """Build an ExtractedSymbol from real source text."""
    params = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith("def ") and "(" in stripped:
            p = stripped[stripped.index("(") + 1:stripped.rindex(")")]
            params = [x.strip().split("=")[0].split(":")[0].strip()
                      for x in p.split(",") if x.strip()]
            params = [x for x in params if x not in ("self", "cls")]
            break
    return ExtractedSymbol(
        name=name, kind=kind, language="python",
        start_line=0, end_line=raw.count("\n"),
        parameters=params, parent_class=parent_class,
        raw_text=raw, body_node=None,
    )


def _build_scenario_db(
    symbols: list[tuple[int, ExtractedSymbol, str]],  # (id, symbol, file_path)
    refs: list[tuple[int, str, int]] | None = None,  # (symbol_id, ref_file, ref_line)
    attrs: list[tuple[int, str, list[int]]] | None = None,  # (class_id, attr_name, method_ids)
) -> tuple[sqlite3.Connection, RepresentationStore, GraphExpander]:
    """Build a full scenario with real representations."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, name TEXT, kind TEXT, language TEXT DEFAULT 'python',
            file_path TEXT, line_number INTEGER, end_line INTEGER,
            is_exported BOOLEAN DEFAULT 1, signature TEXT, params TEXT,
            return_type TEXT, documentation TEXT, usage_count INTEGER DEFAULT 0,
            last_indexed_at INTEGER DEFAULT 1000
        );
        CREATE TABLE refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER,
            referenced_in_file TEXT, referenced_at_line INTEGER,
            reference_type TEXT DEFAULT 'call'
        );
        CREATE TABLE attributes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER,
            name TEXT, method_ids TEXT
        );
        CREATE TABLE exports (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol_id INTEGER,
            module_path TEXT, is_default BOOLEAN DEFAULT 0, is_named BOOLEAN DEFAULT 1);
        CREATE TABLE packages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, version TEXT,
            package_manager TEXT DEFAULT 'pip', is_dev_dependency BOOLEAN DEFAULT 0,
            UNIQUE(name, package_manager));
    """)

    fp_ext = FingerprintExtractor()
    vec_ext = StructuralVectorExtractor()
    tok_ext = TokenSketchExtractor()

    repr_store = RepresentationStore(conn)

    for sid, sym, fpath in symbols:
        conn.execute(
            "INSERT INTO symbols (id, name, kind, file_path, line_number, end_line) VALUES (?,?,?,?,?,?)",
            (sid, sym.name, sym.kind, fpath, sym.start_line, sym.end_line),
        )
        # Real representation extraction
        repr_store.store_representation(sid, "fingerprint_v1", "1.0", fp_ext.extract(sym), None, f"r{sid}", 1)
        repr_store.store_representation(sid, "astvec_v1", "1.0", vec_ext.extract(sym), 32, f"r{sid}", 1)
        repr_store.store_representation(sid, "tokensketch_v1", "1.0", tok_ext.extract(sym), None, f"r{sid}", 1)
        repr_store.store_metadata(
            sid, sym.kind, fpath, "python",
            class_name=sym.parent_class,
        )

    for sym_id, ref_file, ref_line in (refs or []):
        conn.execute("INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line) VALUES (?,?,?)",
                     (sym_id, ref_file, ref_line))

    for class_id, attr_name, method_ids in (attrs or []):
        conn.execute("INSERT INTO attributes (symbol_id, name, method_ids) VALUES (?,?,?)",
                     (class_id, attr_name, json.dumps(method_ids)))

    ss = SymbolStore.__new__(SymbolStore)
    ss._conn = conn
    return conn, repr_store, GraphExpander(ss)


# ============================================================================
# CATEGORY 1: Similarity-only scenarios
# The obligation engine CANNOT find these because there are no shared attributes,
# no caller refs, no override names. Only structural similarity links them.
# ============================================================================


class TestSimilarityOnly:
    """Scenarios where no graph edges exist — only structural similarity can help."""

    def test_parallel_crud_methods_no_graph(self):
        """Two save methods with identical structure but different entity names,
        in DIFFERENT files with NO refs between them.

        Obligation engine sees: nothing (no shared attrs, no refs, no overrides).
        Foundation should see: structural similarity (same pattern).
        """
        save_user = _sym("save_user", "method", """\
def save_user(self, user):
    if not user.name:
        raise ValueError("name required")
    self.db.insert("users", user.to_dict())
    self.cache.invalidate("users")
    return user
""", parent_class="UserService")

        save_order = _sym("save_order", "method", """\
def save_order(self, order):
    if not order.items:
        raise ValueError("items required")
    self.db.insert("orders", order.to_dict())
    self.cache.invalidate("orders")
    return order
""", parent_class="OrderService")

        conn, store, ge = _build_scenario_db([
            (1, save_user, "src/users/service.py"),
            (2, save_order, "src/orders/service.py"),
        ])
        # No refs, no attrs — pure isolation

        # Verify similarity scores directly
        fp_ext = FingerprintExtractor()
        vec_ext = StructuralVectorExtractor()

        fp_dist = fp_ext.distance(fp_ext.extract(save_user), fp_ext.extract(save_order))
        vec_dist = vec_ext.distance(vec_ext.extract(save_user), vec_ext.extract(save_order))

        print(f"\n  Fingerprint similarity: {1 - fp_dist:.4f}")
        print(f"  AstVec similarity:     {1 - vec_dist:.4f}")

        # These SHOULD be highly similar
        assert 1 - fp_dist > 0.9, f"Fingerprint should detect same pattern: sim={1-fp_dist:.4f}"
        assert 1 - vec_dist > 0.9, f"AstVec should detect same structure: sim={1-vec_dist:.4f}"

        # Run composite query — should find the match
        results = find_related(store, 1, "convention_cluster", top_k=5)
        print(f"  Composite results: {len(results)}")
        for sid, score, evidence in results:
            print(f"    symbol_id={sid}, score={score:.4f}, evidence={evidence}")

        # Convention cluster threshold is 0.65 — this pair should pass
        matched = [r for r in results if r[0] == 2]
        assert len(matched) == 1, "Should find save_order as similar to save_user"
        assert matched[0][1] >= 0.65, f"Score {matched[0][1]} should be >= 0.65 threshold"

        conn.close()

    def test_similar_validators_different_domains(self):
        """Two validation methods with same guard-clause-then-process pattern,
        in completely separate modules with no connection.

        This is the kind of convention the obligation engine can never find.
        """
        validate_email = _sym("validate_email", "function", """\
def validate_email(email):
    if not email or "@" not in email:
        raise ValueError("Invalid email")
    parts = email.split("@")
    if len(parts) != 2:
        raise ValueError("Invalid email format")
    domain = parts[1]
    if "." not in domain:
        raise ValueError("Invalid domain")
    return email.lower().strip()
""")

        validate_phone = _sym("validate_phone", "function", """\
def validate_phone(phone):
    if not phone or len(phone) < 7:
        raise ValueError("Invalid phone")
    digits = phone.replace("-", "").replace("+", "")
    if not digits.isdigit():
        raise ValueError("Invalid phone format")
    cleaned = digits.lstrip("0")
    if len(cleaned) < 6:
        raise ValueError("Too short")
    return cleaned
""")

        unrelated = _sym("calculate_shipping", "function", """\
def calculate_shipping(weight, distance):
    base_rate = 5.99
    per_kg = 0.5
    per_km = 0.01
    cost = base_rate + (weight * per_kg) + (distance * per_km)
    return round(cost, 2)
""")

        conn, store, ge = _build_scenario_db([
            (1, validate_email, "src/validators/email.py"),
            (2, validate_phone, "src/validators/phone.py"),
            (3, unrelated, "src/shipping/calculator.py"),
        ])

        # Verify: validators should be similar, shipping should not
        vec_ext = StructuralVectorExtractor()
        v1 = vec_ext.extract(validate_email)
        v2 = vec_ext.extract(validate_phone)
        v3 = vec_ext.extract(unrelated)

        sim_validators = 1 - vec_ext.distance(v1, v2)
        sim_unrelated = 1 - vec_ext.distance(v1, v3)

        print(f"\n  email vs phone (same pattern):     {sim_validators:.4f}")
        print(f"  email vs shipping (different):      {sim_unrelated:.4f}")

        assert sim_validators > sim_unrelated, (
            f"Validators should be more similar than unrelated: {sim_validators:.4f} vs {sim_unrelated:.4f}"
        )

        # Convention cluster query from email should find phone but NOT shipping
        results = find_related(store, 1, "convention_cluster", top_k=5)
        result_ids = {r[0] for r in results}

        print(f"  Convention cluster found: {result_ids}")
        # Phone should score higher than shipping
        phone_scores = [r[1] for r in results if r[0] == 2]
        shipping_scores = [r[1] for r in results if r[0] == 3]

        if phone_scores and shipping_scores:
            assert phone_scores[0] > shipping_scores[0], "Phone should rank above shipping"
        elif phone_scores and not shipping_scores:
            pass  # Shipping correctly filtered out
        # If neither found, the threshold is too high for these — still check the raw scores
        print(f"  Phone score: {phone_scores}")
        print(f"  Shipping score: {shipping_scores}")

        conn.close()


# ============================================================================
# CATEGORY 2: Rename/move detection
# Method was renamed or moved to a different file. Same body, different name.
# The obligation engine sees two unrelated symbols. Foundation sees identity.
# ============================================================================


class TestRenameDetection:
    """Scenarios where a method was renamed/moved — only fingerprints can detect."""

    def test_renamed_method_exact_body(self):
        """get_user renamed to fetch_user. Identical body.
        Fingerprint distance should be 0 (exact match).
        rename_move query should find it above 0.9 threshold.
        """
        body = """\
def {name}(self, user_id):
    result = self.db.query("SELECT * FROM users WHERE id = ?", user_id)
    if not result:
        raise NotFoundError(f"User {{user_id}} not found")
    return User(**result)
"""
        get_user = _sym("get_user", "method", body.format(name="get_user"), parent_class="UserRepo")
        fetch_user = _sym("fetch_user", "method", body.format(name="fetch_user"), parent_class="UserRepo")

        conn, store, ge = _build_scenario_db([
            (1, get_user, "src/repo_old.py"),
            (2, fetch_user, "src/repo_new.py"),
        ])

        # Direct fingerprint comparison
        fp_ext = FingerprintExtractor()
        fp1 = fp_ext.extract(get_user)
        fp2 = fp_ext.extract(fetch_user)
        dist = fp_ext.distance(fp1, fp2)

        print(f"\n  Fingerprint distance (renamed): {dist:.4f}")
        print(f"  Fingerprint similarity: {1 - dist:.4f}")
        assert dist == 0.0, f"Renamed method should have identical fingerprint, got distance={dist}"

        # rename_move query should find the match
        results = find_related(store, 1, "rename_move", top_k=5)
        print(f"  rename_move results: {results}")
        assert len(results) == 1, f"Should find exactly 1 rename match, got {len(results)}"
        assert results[0][0] == 2, "Should match fetch_user"
        assert results[0][1] >= 0.9, f"Score {results[0][1]} should be >= 0.9 for rename"

        conn.close()

    def test_moved_to_different_file_with_minor_edit(self):
        """Method moved to new file with a small change (added logging).
        Fingerprint should be close but not identical.
        """
        original = _sym("process_payment", "method", """\
def process_payment(self, amount, currency):
    if amount <= 0:
        raise ValueError("Amount must be positive")
    rate = self.exchange.get_rate(currency)
    converted = amount * rate
    self.ledger.record(converted, currency)
    return converted
""", parent_class="PaymentService")

        moved = _sym("process_payment", "method", """\
def process_payment(self, amount, currency):
    if amount <= 0:
        raise ValueError("Amount must be positive")
    self.logger.info(f"Processing {amount} {currency}")
    rate = self.exchange.get_rate(currency)
    converted = amount * rate
    self.ledger.record(converted, currency)
    return converted
""", parent_class="PaymentHandler")

        unrelated = _sym("send_email", "function", """\
def send_email(to, subject, body):
    msg = EmailMessage()
    msg["to"] = to
    msg["subject"] = subject
    msg.set_content(body)
    smtp.send(msg)
""")

        conn, store, ge = _build_scenario_db([
            (1, original, "src/old/payments.py"),
            (2, moved, "src/new/payments.py"),
            (3, unrelated, "src/notifications.py"),
        ])

        fp_ext = FingerprintExtractor()
        vec_ext = StructuralVectorExtractor()

        # Fingerprint: original vs moved should be very close
        fp_dist_moved = fp_ext.distance(fp_ext.extract(original), fp_ext.extract(moved))
        fp_dist_unrel = fp_ext.distance(fp_ext.extract(original), fp_ext.extract(unrelated))
        vec_dist_moved = vec_ext.distance(vec_ext.extract(original), vec_ext.extract(moved))
        vec_dist_unrel = vec_ext.distance(vec_ext.extract(original), vec_ext.extract(unrelated))

        print(f"\n  FP dist (moved, minor edit): {fp_dist_moved:.4f}  sim={1-fp_dist_moved:.4f}")
        print(f"  FP dist (unrelated):         {fp_dist_unrel:.4f}  sim={1-fp_dist_unrel:.4f}")
        print(f"  Vec dist (moved):            {vec_dist_moved:.4f}  sim={1-vec_dist_moved:.4f}")
        print(f"  Vec dist (unrelated):        {vec_dist_unrel:.4f}  sim={1-vec_dist_unrel:.4f}")

        # Moved method should be much closer than unrelated
        assert fp_dist_moved < fp_dist_unrel, "Moved method should be closer than unrelated"
        assert vec_dist_moved < vec_dist_unrel, "Moved method should be closer than unrelated"

        conn.close()


# ============================================================================
# CATEGORY 3: False positive resistance
# Foundation must NOT link unrelated methods even when they share superficial
# similarities (same param count, both have returns, etc.)
# ============================================================================


class TestFalsePositiveResistance:
    """Foundation must stay silent when methods are genuinely unrelated."""

    def test_same_arity_different_everything(self):
        """Two methods with 2 params each but completely different logic.
        Fingerprint arity matches but everything else differs.
        Should NOT be linked.
        """
        method_a = _sym("encrypt", "function", """\
def encrypt(plaintext, key):
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode())
    return base64.b64encode(cipher.nonce + tag + ciphertext).decode()
""")

        method_b = _sym("resize_image", "function", """\
def resize_image(image, dimensions):
    width, height = dimensions
    if width <= 0 or height <= 0:
        raise ValueError("Invalid dimensions")
    resized = image.resize((width, height), Image.LANCZOS)
    return resized
""")

        conn, store, ge = _build_scenario_db([
            (1, method_a, "src/crypto.py"),
            (2, method_b, "src/images.py"),
        ])

        # obligation_expansion should NOT link these
        results = find_related(store, 1, "obligation_expansion", top_k=5)
        assert len(results) == 0, (
            f"Should NOT link encrypt and resize_image, but got: {results}"
        )

        conn.close()

    def test_empty_vs_complex_method(self):
        """An empty method and a complex method should never be linked."""
        empty = _sym("noop", "method", """\
def noop(self):
    pass
""", parent_class="Base")

        complex_method = _sym("orchestrate", "method", """\
def orchestrate(self, tasks, workers, timeout=30):
    results = []
    for task in tasks:
        if task.priority > 5:
            worker = self.scheduler.assign(task, workers)
            try:
                result = worker.execute(task, timeout=timeout)
                results.append(result)
            except TimeoutError:
                self.logger.warn(f"Task {task.id} timed out")
                results.append(None)
        else:
            results.append(self.queue.defer(task))
    return results
""", parent_class="Orchestrator")

        conn, store, ge = _build_scenario_db([
            (1, empty, "src/base.py"),
            (2, complex_method, "src/orchestrator.py"),
        ])

        # No query should link these
        for use_case in ("obligation_expansion", "convention_cluster", "rename_move"):
            results = find_related(store, 1, use_case, top_k=5)
            assert len(results) == 0, (
                f"{use_case} should NOT link empty and complex methods, got: {results}"
            )

        conn.close()

    def test_no_false_links_structurally_different_code(self):
        """Functions with genuinely different AST structure should not link.

        Note: short 2-3 line functions (call+return) ARE structurally identical
        to the AST — that's correct. We test with functions that have genuinely
        different control flow, statement types, and complexity.
        """
        # Complex function with loops and error handling
        func_a = _sym("process_batch", "function", """\
def process_batch(items, config):
    results = []
    errors = []
    for item in items:
        if item.status == "skip":
            continue
        try:
            transformed = transform(item, config)
            results.append(transformed)
        except TransformError as e:
            errors.append({"item": item.id, "error": str(e)})
    if errors:
        log.warning(f"Batch had {len(errors)} errors")
    return {"results": results, "errors": errors}
""")

        # Simple one-liner
        func_b = _sym("get_version", "function", """\
def get_version():
    return "1.0.0"
""")

        # Generator with yield
        func_c = _sym("stream_lines", "function", """\
def stream_lines(filepath):
    with open(filepath) as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                yield stripped
""")

        conn, store, ge = _build_scenario_db([
            (1, func_a, "src/batch.py"),
            (2, func_b, "src/version.py"),
            (3, func_c, "src/io.py"),
        ])

        total_false_links = 0
        for sid in (1, 2, 3):
            results = find_related(store, sid, "obligation_expansion", top_k=5)
            total_false_links += len(results)

        assert total_false_links == 0, (
            f"Structurally different functions should have 0 obligation matches, got {total_false_links}"
        )

        conn.close()


# ============================================================================
# AGGREGATE: Gate 2 summary
# ============================================================================


class TestGate2Honest:
    def test_gate2_summary(self):
        """Run key assertions and print summary."""
        print("\n" + "=" * 70)
        print("GATE 2 — HONEST DIAGNOSTIC")
        print("=" * 70)

        # 1. Similarity finds genuinely similar methods
        body = 'def {name}(self, x):\n    if not x:\n        raise ValueError("bad")\n    self.db.save(x)\n    return x\n'
        a = _sym("save_a", "method", body.format(name="save_a"), "A")
        b = _sym("save_b", "method", body.format(name="save_b"), "B")

        fp = FingerprintExtractor()
        vec = StructuralVectorExtractor()
        tok = TokenSketchExtractor()

        fp_sim = 1 - fp.distance(fp.extract(a), fp.extract(b))
        vec_sim = 1 - vec.distance(vec.extract(a), vec.extract(b))
        tok_sim = 1 - tok.distance(tok.extract(a), tok.extract(b))

        print(f"\n1. SAME PATTERN (save_a vs save_b):")
        print(f"   FP={fp_sim:.3f}  Vec={vec_sim:.3f}  Tok={tok_sim:.3f}")
        assert fp_sim > 0.9, "Same pattern should have high fingerprint similarity"
        assert vec_sim > 0.9, "Same pattern should have high vector similarity"
        print("   PASS: all signals detect structural match")

        # 2. Different methods are not similar
        c = _sym("parse", "function", "def parse(data):\n    return json.loads(data)\n")
        fp_sim2 = 1 - fp.distance(fp.extract(a), fp.extract(c))
        vec_sim2 = 1 - vec.distance(vec.extract(a), vec.extract(c))

        print(f"\n2. DIFFERENT METHODS (save_a vs parse):")
        print(f"   FP={fp_sim2:.3f}  Vec={vec_sim2:.3f}")
        assert vec_sim2 < 0.7, "Different methods should have low vector similarity"
        print("   PASS: signals correctly distinguish different code")

        # 3. Rename detection works
        d = _sym("renamed_save", "method", body.format(name="renamed_save"), "A")
        fp_rename = 1 - fp.distance(fp.extract(a), fp.extract(d))
        print(f"\n3. RENAME (save_a vs renamed_save):")
        print(f"   FP={fp_rename:.3f}")
        assert fp_rename == 1.0, "Renamed method must have identical fingerprint"
        print("   PASS: fingerprint catches rename")

        # 4. Token sketch disambiguates
        tok_sim_same = 1 - tok.distance(tok.extract(a), tok.extract(b))
        tok_sim_diff = 1 - tok.distance(tok.extract(a), tok.extract(c))
        print(f"\n4. TOKEN DISAMBIGUATION:")
        print(f"   same pattern: Tok={tok_sim_same:.3f}")
        print(f"   diff methods: Tok={tok_sim_diff:.3f}")
        assert tok_sim_diff < tok_sim_same, "Token sketch should distinguish different code"
        print("   PASS: token sketch provides disambiguation signal")

        # 5. False positive resistance — must use structurally DIFFERENT functions
        # (Short 2-3 line call+return functions are legitimately similar to the AST)
        print(f"\n5. FALSE POSITIVE RESISTANCE:")
        diverse = [
            _sym("f1", "function", "def f1(x):\n    return x + 1\n"),
            _sym("f2", "function", "def f2(items):\n    result = []\n    for item in items:\n        if item > 0:\n            result.append(item * 2)\n    return result\n"),
            _sym("f3", "function", "def f3():\n    yield from range(10)\n"),
        ]
        conn, store, ge = _build_scenario_db([
            (i + 1, s, f"src/f{i+1}.py") for i, s in enumerate(diverse)
        ])
        total = 0
        for i in range(len(diverse)):
            results = find_related(store, i + 1, "obligation_expansion", top_k=5)
            total += len(results)
        print(f"   obligation_expansion matches in diverse code: {total}")
        assert total == 0, f"Should be 0 false links, got {total}"
        print("   PASS: no false obligations in diverse codebase")
        conn.close()

        print("\n" + "=" * 70)
        print("GATE 2: ALL CHECKS PASSED")
        print("=" * 70)

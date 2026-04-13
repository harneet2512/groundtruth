import sqlite3

from groundtruth.procedures.cluster import ProcedureClusterer, TrajectoryRecord
from groundtruth.procedures.retrieve import ProcedureRetriever
from groundtruth.contracts.repo_coupling import build_repo_coupling_contracts
from groundtruth.substrate.types import ContractRecord
from groundtruth.verification.contract_checker import ContractChecker
from groundtruth.verification.models import PatchCandidate


def test_contract_checker_flags_arity_obligation_change():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-1",
        diff=(
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@\n"
            "-def foo(x):\n"
            "+def foo(x, y):\n"
        ),
        changed_files=("mod.py",),
        changed_symbols=("foo",),
    )
    contract = ContractRecord(
        contract_type="obligation",
        scope_kind="function",
        scope_ref="mod.foo",
        predicate="Signature must remain compatible",
        normalized_form="obligation:arity:foo",
        support_sources=("caller.py:10",),
        support_count=3,
        confidence=0.95,
        tier="verified",
    )

    score, violations = checker.check(candidate, [contract])

    assert score == 0.0
    assert violations
    assert violations[0].severity == "hard"


def test_contract_checker_flags_negative_non_none_regression():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-2",
        diff=(
            "--- a/mod.py\n"
            "+++ b/mod.py\n"
            "@@\n"
            "+    return None\n"
        ),
        changed_files=("mod.py",),
        changed_symbols=("foo",),
    )
    contract = ContractRecord(
        contract_type="negative_contract",
        scope_kind="function",
        scope_ref="mod.foo",
        predicate="Must not be None",
        normalized_form="negative:must_not_be_none:mod.foo",
        support_sources=("tests/test_mod.py:12",),
        support_count=1,
        confidence=0.85,
        tier="likely",
    )

    _, violations = checker.check(candidate, [contract])

    assert len(violations) == 1
    assert "return None" in violations[0].explanation


def test_contract_checker_flags_registry_coupling_break():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-3",
        diff=(
            "--- a/src/handlers.py\n"
            "+++ b/src/handlers.py\n"
            "@@\n"
            "-class Handler:\n"
            "+class RenamedHandler:\n"
        ),
        changed_files=("src/handlers.py",),
        changed_symbols=("Handler",),
    )
    contract = ContractRecord(
        contract_type="registry_coupling",
        scope_kind="class",
        scope_ref="pkg.handlers.Handler",
        predicate="Registration must be preserved",
        normalized_form="registry_coupling:preserve:Handler:src/handlers.py:src/__init__.py",
        support_sources=("src/__init__.py:0",),
        support_count=1,
        confidence=0.80,
        tier="likely",
    )

    _, violations = checker.check(candidate, [contract])

    assert len(violations) == 1
    assert "updating src/__init__.py" in violations[0].explanation
    assert violations[0].severity == "soft"


def test_contract_checker_flags_doc_coupling_break():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-4",
        diff=(
            "--- a/src/api.py\n"
            "+++ b/src/api.py\n"
            "@@\n"
            "-def PublicThing(x):\n"
            "+def RenamedThing(x):\n"
        ),
        changed_files=("src/api.py",),
        changed_symbols=("PublicThing",),
    )
    contract = ContractRecord(
        contract_type="doc_coupling",
        scope_kind="file",
        scope_ref="src/api.py",
        predicate="Docs must stay aligned",
        normalized_form="doc_coupling:preserve_file:src/api.py:docs/api.md",
        support_sources=("docs/api.md:0",),
        support_count=1,
        confidence=0.80,
        tier="likely",
    )

    _, violations = checker.check(candidate, [contract])

    assert len(violations) == 1
    assert "docs/api.md" in violations[0].explanation
    assert violations[0].severity == "soft"


def test_build_repo_coupling_contracts_from_docs_and_config(tmp_path):
    root = tmp_path
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("Use `PublicThing` for public access", encoding="utf-8")
    (root / "settings.yaml").write_text("handler: PublicThing", encoding="utf-8")

    class FakeReader:
        def get_file_paths(self):
            return ["src/api.py", "docs/api.md", "settings.yaml"]

        def get_nodes_in_file(self, file_path):
            if file_path == "src/api.py":
                return [{"id": 1, "name": "PublicThing", "label": "Function", "is_exported": True}]
            return []

        def get_callees(self, node_id):
            return []

    contracts = build_repo_coupling_contracts(FakeReader(), str(root), ["src/api.py"])
    types = {c.contract_type for c in contracts}

    assert "doc_coupling" in types
    assert "config_coupling" in types


def test_contract_checker_flags_protocol_invariant_break():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-5",
        diff=(
            "--- a/src/data.py\n"
            "+++ b/src/data.py\n"
            "@@\n"
            "+    return None\n"
        ),
        changed_files=("src/data.py",),
        changed_symbols=("get_data",),
    )
    contract = ContractRecord(
        contract_type="protocol_invariant",
        scope_kind="function",
        scope_ref="pkg.get_data",
        predicate="Return must remain destructurable",
        normalized_form="protocol_invariant:destructurable:pkg.get_data",
        support_sources=("src/a.py:10", "src/b.py:20"),
        support_count=2,
        confidence=0.85,
        tier="verified",
    )

    _, violations = checker.check(candidate, [contract])

    assert len(violations) == 1
    assert "destructurable" in violations[0].explanation
    assert violations[0].severity == "hard"


def test_contract_checker_flags_registry_break_for_go_style_symbol():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-6",
        diff=(
            "--- a/src/handler.go\n"
            "+++ b/src/handler.go\n"
            "@@\n"
            "-type Handler struct {\n"
            "+type RenamedHandler struct {\n"
        ),
        changed_files=("src/handler.go",),
        changed_symbols=("Handler",),
    )
    contract = ContractRecord(
        contract_type="registry_coupling",
        scope_kind="class",
        scope_ref="pkg.Handler",
        predicate="Registration must be preserved",
        normalized_form="registry_coupling:preserve:Handler:src/handler.go:src/routes.go",
        support_sources=("src/routes.go:0",),
        support_count=1,
        confidence=0.80,
        tier="likely",
    )

    _, violations = checker.check(candidate, [contract])

    assert len(violations) == 1
    assert "src/routes.go" in violations[0].explanation
    assert violations[0].severity == "soft"


def test_contract_checker_flags_arity_change_for_go_style_signature():
    checker = ContractChecker()
    candidate = PatchCandidate(
        task_ref="task",
        candidate_id="cand-7",
        diff=(
            "--- a/mod.go\n"
            "+++ b/mod.go\n"
            "@@\n"
            "-func Foo(x int) string {\n"
            "+func Foo(x int, y int) string {\n"
        ),
        changed_files=("mod.go",),
        changed_symbols=("Foo",),
    )
    contract = ContractRecord(
        contract_type="obligation",
        scope_kind="function",
        scope_ref="mod.Foo",
        predicate="Signature must remain compatible",
        normalized_form="obligation:arity:Foo",
        support_sources=("caller.go:10", "caller2.go:20"),
        support_count=2,
        confidence=0.85,
        tier="verified",
    )

    score, violations = checker.check(candidate, [contract])

    assert score == 0.0
    assert len(violations) == 1
    assert violations[0].severity == "hard"


def test_procedure_clusterer_distinguishes_post_validation():
    clusterer = ProcedureClusterer()
    traj = TrajectoryRecord(
        task_ref="t1",
        repo_ref="repo",
        issue_text="ValueError when parsing config",
        files_visited=["src/parser.py", "src/config.py"],
        files_edited=["src/parser.py"],
        tests_run=["tests/test_parser.py"],
        outcome="resolved",
        patch_diff="",
    )

    signature = clusterer._compute_repair_signature(traj)

    assert signature.endswith("|e1t0|post")


def test_procedure_retriever_uses_changed_file_overlap(tmp_path):
    db_path = tmp_path / "procedures.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE repair_procedures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_signature TEXT NOT NULL,
            procedure_name TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            anti_patterns_json TEXT,
            validation_plan_json TEXT,
            confidence REAL NOT NULL,
            source_count INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO repair_procedures
        (issue_signature, procedure_name, steps_json, anti_patterns_json,
         validation_plan_json, confidence, source_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            "value_error:validation",
            "generic",
            '{"inspection_order":["check_source"],"co_edit_sets":[["src/other.py","tests/test_other.py"]]}',
            "[]",
            '["run tests/test_other.py"]',
            0.92,
            5,
        ),
    )
    conn.execute(
        """
        INSERT INTO repair_procedures
        (issue_signature, procedure_name, steps_json, anti_patterns_json,
         validation_plan_json, confidence, source_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            "value_error:validation",
            "matching",
            '{"inspection_order":["check_source"],"co_edit_sets":[["src/parser.py","tests/test_parser.py"]]}',
            "[]",
            '["run tests/test_parser.py"]',
            0.85,
            4,
        ),
    )
    conn.commit()

    retriever = ProcedureRetriever(conn)
    results = retriever.retrieve(
        "ValueError raised while parsing config",
        changed_files=["src/parser.py"],
        max_results=2,
    )

    assert [r.procedure_name for r in results][:1] == ["matching"]

import sqlite3

from groundtruth.procedures.cluster import ProcedureClusterer, TrajectoryRecord
from groundtruth.procedures.retrieve import ProcedureRetriever
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

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import numpy as np

from groundtruth.pretask import v1r_brief as v1r
from groundtruth.pretask import graph_localizer as gl
from groundtruth.pretask import anchor_select
from groundtruth.pretask import v7_4_brief as v74


def _native_brief() -> str:
    return "\n".join((
        "<gt-task-brief>",
        "1. src/alpha.py (alpha)",
        "2. src/beta.py (beta)",
        "",
        v1r._OBLIGATION_NATIVE_HEADER,
        "- [ ] preserve ordering",
        "</gt-task-brief>",
    ))


def test_terminal_mediators_share_exact_final_block_identity(monkeypatch) -> None:
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    monkeypatch.setenv("GT_CONTENT_LEG", "1")
    monkeypatch.setenv("GT_SEM_BODY", "1")

    text = _native_brief()
    candidate_ids = [
        v1r._localization_candidate_id("src/alpha.py"),
        v1r._localization_candidate_id("src/beta.py"),
    ]
    receipts = v1r._brief_block_receipts(
        text, localization_candidate_ids=candidate_ids)
    rows = v1r._terminal_pretask_mediator_participation(
        text,
        receipts,
        content_paths={"src/beta.py"},
        content_decision="APPLIED",
        content_reason="margin_cleared",
        semantic_anchor_paths={"src/alpha.py"},
        semantic_localizer_paths={"src/alpha.py", "src/beta.py"},
    )

    by_feature: dict[str, list[dict]] = {}
    for row in rows:
        by_feature.setdefault(row["control_ref"]["feature_id"], []).append(row)
        receipt = next(
            r for r in receipts if r["candidate_id"] == row["candidate_id"])
        start, end = receipt["char_span"]
        exact = text[start:end]
        assert row["candidate_chars"] == len(exact)
        assert row["candidate_sha256_16"] == hashlib.sha256(
            exact.encode("utf-8", "surrogatepass")
        ).hexdigest()[:16]
        assert receipt["content_hash"].startswith(row["candidate_sha256_16"])

    assert len(by_feature["GT_BRIEF_NATIVE"]) == 1
    assert len(by_feature["GT_SS_ACK_FORM"]) == 1
    assert [r["candidate_id"] for r in by_feature["GT_CONTENT_LEG"]] == [
        v1r._localization_candidate_id("src/beta.py")]
    assert {r["candidate_id"] for r in by_feature["GT_SEM_BODY"]} == set(candidate_ids)
    sem_reasons = {r["candidate_id"]: r["reason"] for r in by_feature["GT_SEM_BODY"]}
    assert sem_reasons[candidate_ids[0]] == "anchor_and_localizer"
    assert sem_reasons[candidate_ids[1]] == "localizer_only"
    assert len({(r["control_ref"]["feature_id"], r["candidate_id"]) for r in rows}) == len(rows)


def test_terminal_mediators_do_not_promote_non_survivors(monkeypatch) -> None:
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_CONTENT_LEG", "1")
    monkeypatch.setenv("GT_SEM_BODY", "1")
    text = "<gt-task-brief>\n1. src/alpha.py (alpha)\n</gt-task-brief>"
    alpha_id = v1r._localization_candidate_id("src/alpha.py")
    receipts = v1r._brief_block_receipts(
        text, localization_candidate_ids=[alpha_id])
    rows = v1r._terminal_pretask_mediator_participation(
        text,
        receipts,
        content_paths={"src/not-rendered.py"},
        content_decision="APPLIED",
        content_reason="vacant_lexical_slot",
        semantic_anchor_paths={"src/not-rendered.py"},
        semantic_localizer_paths=set(),
    )
    assert all(r["candidate_id"] != v1r._localization_candidate_id(
        "src/not-rendered.py") for r in rows)
    content = next(r for r in rows if r["control_ref"]["feature_id"] == "GT_CONTENT_LEG")
    semantic = next(r for r in rows if r["control_ref"]["feature_id"] == "GT_SEM_BODY")
    assert content["decision"] == "NO_EFFECT"
    assert semantic["decision"] == "NO_EFFECT"
    assert content["candidate_chars"] == semantic["candidate_chars"] == 0


def test_receipt_instrumentation_is_render_neutral(monkeypatch) -> None:
    text = _native_brief()
    before = text.encode("utf-8")
    receipts = v1r._brief_block_receipts(
        text,
        localization_candidate_ids=[
            v1r._localization_candidate_id("src/alpha.py"),
            v1r._localization_candidate_id("src/beta.py"),
        ],
    )
    assert text.encode("utf-8") == before
    assert [r["candidate_id"] for r in receipts if r["fact_class"] == "localization"] == [
        v1r._localization_candidate_id("src/alpha.py"),
        v1r._localization_candidate_id("src/beta.py"),
    ]
    monkeypatch.delenv("GT_INSEAM_METRICS", raising=False)
    assert v1r._terminal_pretask_mediator_participation(text, receipts) == []


def test_generate_matched_path_joins_only_rendered_semantic_candidates(
    tmp_path, monkeypatch,
) -> None:
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
             type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
             confidence REAL, metadata TEXT);
           CREATE TABLE properties(node_id INTEGER, kind TEXT, value TEXT);"""
    )
    con.execute(
        "INSERT INTO nodes VALUES(1,'Function','apply_defaults',NULL,'pkg/config.py',"
        "1,3,'apply_defaults(cfg)',NULL,1,0,'python',NULL)"
    )
    con.execute(
        "INSERT INTO properties VALUES(1,'body_terms','mapping defaults preserve ordering')"
    )
    con.commit()
    con.close()
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "config.py").write_text(
        "def apply_defaults(cfg):\n    return cfg\n", encoding="utf-8")

    class _Model:
        dim = 2
        model_name = "test-sem-body"

        def encode(self, texts, **_kwargs):
            return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

    model = _Model()
    monkeypatch.setattr(v74, "_get_model", lambda: model)
    monkeypatch.setattr(gl, "_get_embedder", lambda: model)
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_SEM_BODY", "1")
    monkeypatch.delenv("GT_CONTENT_LEG", raising=False)

    result = v1r.generate_v1r_brief(
        "apply_defaults must preserve mapping ordering", str(root), db)
    receipt_ids = {
        r["candidate_id"] for r in result.block_receipts
        if r["label"].startswith("file-entry")
    }
    proof_ids = {p["candidate_id"] for p in result.localization_proof}
    semantic = [
        r for r in result.control_participation
        if r["control_ref"]["feature_id"] == "GT_SEM_BODY"
        and r["decision"] == "APPLIED"
    ]
    assert semantic
    assert {r["candidate_id"] for r in semantic} <= receipt_ids <= proof_ids
    assert all(r["reason"] == "anchor_and_localizer" for r in semantic)


def test_generate_no_match_path_calls_terminal_builder(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GT_INSEAM_METRICS", "1")
    monkeypatch.setenv("GT_BRIEF_NATIVE", "1")
    monkeypatch.setenv("GT_SS_ACK_FORM", "1")
    monkeypatch.setattr(
        v1r,
        "run_v74",
        lambda *args, **kwargs: SimpleNamespace(
            ranked_full=[], effective_w_sem=0.0, k_sem_top_effective=0,
        ),
    )
    result = v1r.generate_v1r_brief(
        "The parser must preserve ordering on empty input.",
        str(tmp_path),
        str(tmp_path / "missing.db"),
    )
    features = {
        row["control_ref"]["feature_id"] for row in result.control_participation
    }
    assert "GT_BRIEF_NATIVE" in features
    assert "GT_SS_ACK_FORM" in features
    obligations = next(
        r for r in result.block_receipts if r["fact_class"] == "obligations")
    for row in result.control_participation:
        if row["control_ref"]["feature_id"] in {"GT_BRIEF_NATIVE", "GT_SS_ACK_FORM"}:
            assert row["candidate_id"] == obligations["candidate_id"]


def test_content_leg_exposes_only_final_localizer_paths(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "content.db")
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
             type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
             confidence REAL, metadata TEXT);
           CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content);"""
    )
    con.execute(
        "INSERT INTO nodes VALUES(1,'Function','handle_stream',NULL,'net.py',1,3,"
        "'handle_stream(x)',NULL,1,0,'python',NULL)"
    )
    con.execute(
        "INSERT INTO symbol_content_fts(rowid,content) VALUES(1,"
        "'websocket handshake opcode masking payload')"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("GT_CONTENT_LEG", "1")
    monkeypatch.setattr(gl, "_get_embedder", lambda: None)
    result = gl.localize(
        "websocket handshake opcode masking fails", db, top_k=8, repo_root="")
    assert result.content_leg_decision == "APPLIED"
    assert result.content_leg_reason == "vacant_lexical_slot"
    assert result.content_leg_paths == frozenset({"net.py"})
    assert result.content_leg_paths <= {
        gl._normalize(candidate.file_path) for candidate in result.candidates
    }


def test_semantic_body_cache_identity_separates_on_and_off(tmp_path) -> None:
    graph = tmp_path / "graph.db"
    graph.write_bytes(b"graph")
    off = anchor_select._cache_key(
        str(graph), "model", 8, body_on=False)
    on = anchor_select._cache_key(
        str(graph), "model", 8, body_on=True)
    assert off != on

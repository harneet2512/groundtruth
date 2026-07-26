from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path


def _fixture(tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "parser.py").write_text(
        "def parse_value(value):\n"
        "    return decode(value)\n"
        "\n"
        "def decode(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    con.executemany(
        """
        INSERT INTO nodes
        (id,label,name,qualified_name,file_path,start_line,end_line,signature,
         return_type,is_exported,is_test,language,parent_id)
        VALUES (?,?,?,?,?,?,?,?,?,1,0,'python',NULL)
        """,
        [
            (1, "Function", "parse_value", "parse_value", "src/parser.py", 1, 2, "parse_value(value)", ""),
            (2, "Function", "decode", "decode", "src/parser.py", 4, 5, "decode(value)", ""),
        ],
    )
    con.execute(
        """
        INSERT INTO edges
        VALUES (1,1,2,'CALLS',2,'src/parser.py','same_file',1.0,'')
        """
    )
    con.commit()
    con.close()
    return str(repo), str(db)


def _json_dataclass(value, *, drop_elapsed: bool = False) -> str:
    payload = asdict(value)
    if drop_elapsed:
        payload.pop("elapsed_ms", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def test_localize_shadow_on_is_object_identical_and_writes_sidecar(tmp_path, monkeypatch):
    from groundtruth.pretask.graph_localizer import localize

    repo, db = _fixture(tmp_path)
    issue = "parse_value should decode malformed values using decode"
    monkeypatch.delenv("GT_LOC_VNEXT_SHADOW", raising=False)
    legacy = localize(issue, db, repo_root=repo)
    before = _json_dataclass(legacy, drop_elapsed=True)

    sidecars = tmp_path / "sidecars"
    monkeypatch.setenv("GT_LOC_VNEXT_SHADOW", "1")
    monkeypatch.setenv("GT_LOC_VNEXT_SIDECAR_DIR", str(sidecars))
    shadow = localize(issue, db, repo_root=repo)

    assert shadow == legacy
    assert _json_dataclass(shadow) == before
    files = list(sidecars.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "gt.localization.vnext.v1"
    assert payload["source_projection"] == "localize"
    assert payload["legacy"]["output_sha256"]
    assert payload["vnext"]["deterministic_hash"]


def test_shadow_sidecar_failure_is_fail_open_for_legacy_output(tmp_path, monkeypatch):
    from groundtruth.pretask import graph_localizer
    from groundtruth.pretask.localization_vnext import shadow

    repo, db = _fixture(tmp_path)
    issue = "parse_value should call decode"
    monkeypatch.delenv("GT_LOC_VNEXT_SHADOW", raising=False)
    legacy = graph_localizer.localize(issue, db, repo_root=repo)

    monkeypatch.setenv("GT_LOC_VNEXT_SHADOW", "1")
    monkeypatch.setattr(
        shadow,
        "write_shadow_sidecar",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("forced sidecar failure")),
    )
    actual = graph_localizer.localize(issue, db, repo_root=repo)
    assert actual == legacy
    assert _json_dataclass(actual) == _json_dataclass(legacy)


def test_run_v74_shadow_on_preserves_complete_projection(tmp_path, monkeypatch):
    from groundtruth.pretask import v7_4_brief as module

    repo, db = _fixture(tmp_path)
    issue = "parse_value should call decode"
    monkeypatch.setattr(module, "_get_model", lambda: module._ZeroEmbeddingModel())
    monkeypatch.setattr(module, "_SEMANTIC_AVAILABLE", False)
    monkeypatch.delenv("GT_LOC_VNEXT_SHADOW", raising=False)
    legacy = module.run_v74(issue, repo, db)
    before = _json_dataclass(legacy, drop_elapsed=True)

    sidecars = tmp_path / "v74-sidecars"
    monkeypatch.setenv("GT_LOC_VNEXT_SHADOW", "1")
    monkeypatch.setenv("GT_LOC_VNEXT_SIDECAR_DIR", str(sidecars))
    actual = module.run_v74(issue, repo, db)

    assert _json_dataclass(actual, drop_elapsed=True) == before
    assert actual.elapsed_ms >= 0
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in sidecars.glob("*.json")]
    assert len(payloads) == 1
    assert payloads[0]["source_projection"] == "run_v74"


def test_live_brief_text_and_localization_proof_are_byte_identical(tmp_path, monkeypatch):
    from groundtruth.pretask import v7_4_brief as module
    from groundtruth.pretask.v1r_brief import generate_v1r_brief

    repo, db = _fixture(tmp_path)
    issue = "parse_value should call decode"
    monkeypatch.setattr(module, "_get_model", lambda: module._ZeroEmbeddingModel())
    monkeypatch.setattr(module, "_SEMANTIC_AVAILABLE", False)
    monkeypatch.delenv("GT_LOC_VNEXT_SHADOW", raising=False)
    legacy = generate_v1r_brief(issue, repo, db)

    monkeypatch.setenv("GT_LOC_VNEXT_SHADOW", "1")
    monkeypatch.setenv("GT_LOC_VNEXT_SIDECAR_DIR", str(tmp_path / "brief-sidecars"))
    shadow = generate_v1r_brief(issue, repo, db)

    assert shadow.brief_text.encode("utf-8") == legacy.brief_text.encode("utf-8")
    assert shadow.localization_proof == legacy.localization_proof
    assert [entry.path for entry in shadow.files] == [entry.path for entry in legacy.files]

"""Replay-corpus growth generator (Cluster-5 ITEM 5, 2026-07-18).

The trusted corpus tests/fixtures/ss_replay/cases.json is CURATED. This generator PROPOSES
candidate cases from a run's per-delivery ledgers into a SEPARATE, provenance-marked file — never
into cases.json, never enabled by default. RED-first: before ITEM 5 there was no generator, so a
completed run's deliveries could not be turned into reviewable replay-case candidates at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "scripts" / "swebench",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ss_replay_case_generator as gen  # noqa: E402

_LEDGERS = {
    "repo__task-1": [
        # scratch-path delivery -> suppress_provenance candidate
        {"outcome": "delivered", "layer": "l3.contract", "iteration": 19,
         "file_path": "tmp/patch_fix.py", "chars_delivered": 88, "content_sha256_16": "aaaa1111"},
        # empty payload -> empty_payload candidate
        {"outcome": "delivered", "layer": "gateway.trace_frame", "iteration": 7,
         "file_path": "src/app.py", "chars_delivered": 0, "content_sha256_16": "bbbb2222"},
        # a normal, real-source delivery -> NOT a suppress candidate
        {"outcome": "delivered", "layer": "l3b.evidence", "iteration": 9,
         "file_path": "src/app.py", "chars_delivered": 120, "content_sha256_16": "dup55555"},
        # not delivered -> ignored entirely
        {"outcome": "suppressed", "layer": "l3b.evidence", "iteration": 11,
         "file_path": "tmp/scratch.py", "chars_delivered": 50, "content_sha256_16": "cccc3333"},
    ],
    "repo__task-2": [
        # same content_sha256_16 as task-1 m9 -> suppress_semantic_dup candidate (cross-task)
        {"outcome": "delivered", "layer": "l3.contract", "iteration": 49,
         "file_path": "src/other.py", "chars_delivered": 120, "content_sha256_16": "dup55555"},
    ],
}


def _doc():
    return gen.generate_candidates(_LEDGERS, source_run="run-2-test")


def test_candidates_carry_provenance_and_candidate_schema() -> None:
    doc = _doc()
    assert doc["schema"] == gen.CANDIDATES_SCHEMA
    assert doc["schema"] != gen.TRUSTED_SCHEMA  # never confused with the curated corpus
    prov = doc["provenance"]
    assert prov["auto_generated"] is True and prov["curated"] is False
    assert prov["source_run"] == "run-2-test"
    assert prov["generator_version"] == gen.GENERATOR_VERSION


def test_scratch_path_delivery_becomes_suppress_provenance_candidate() -> None:
    doc = _doc()
    sp = doc["suppress_provenance"]
    assert {e["task"] for e in sp} == {"repo__task-1"}  # only the scratch-path row
    entry = sp[0]
    assert entry["delivery"] == "l3.contract m19"
    assert entry["paths"] == ["tmp/patch_fix.py"]
    # A real-source delivery must NOT be proposed for suppression.
    assert all("src/app.py" not in e["paths"] for e in sp)


def test_empty_payload_and_semantic_dup_candidates() -> None:
    doc = _doc()
    ep = doc["empty_payload"]
    assert [e["delivery"] for e in ep] == ["gateway.trace_frame m7"]
    dup = doc["suppress_semantic_dup"]
    assert len(dup) == 1  # dup55555 delivered in both tasks
    assert dup[0]["content_sha256_16"] == "dup55555"
    assert dup[0]["deliveries"] == ["repo__task-1:l3b.evidence m9", "repo__task-2:l3.contract m49"]


def test_generation_is_deterministic() -> None:
    a = json.dumps(_doc(), sort_keys=True)
    b = json.dumps(_doc(), sort_keys=True)
    assert a == b


def test_cli_refuses_to_overwrite_trusted_cases_json(tmp_path: Path) -> None:
    run = tmp_path / "ll-full-x"
    run.mkdir(parents=True)
    (run / "gt_runtime_ledger_x.jsonl").write_text("", encoding="utf-8")
    rc = gen.main([str(tmp_path), "--output", str(tmp_path / "cases.json")])
    assert rc == 3  # MUST refuse to clobber the curated corpus


def test_cli_writes_separate_candidate_file_only(tmp_path: Path) -> None:
    task_dir = tmp_path / "ll-full-repo__task-1"
    task_dir.mkdir(parents=True)
    with open(task_dir / "gt_runtime_ledger_repo__task-1.jsonl", "w", encoding="utf-8") as fh:
        for row in _LEDGERS["repo__task-1"]:
            fh.write(json.dumps(row) + "\n")
    out = tmp_path / "ss_replay" / "cases_candidates_run.json"
    rc = gen.main([str(tmp_path), "--source-run", "run", "--output", str(out)])
    assert rc == 0
    assert out.is_file()  # separate file
    assert not (tmp_path / "cases.json").exists()  # trusted corpus untouched
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == gen.CANDIDATES_SCHEMA
    assert doc["counts"]["suppress_provenance"] == 1

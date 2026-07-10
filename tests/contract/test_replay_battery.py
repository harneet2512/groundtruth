"""E-replay battery — whole-D0-corpus determinism replay (P7-E consolidation).

The Gateway is the ONE model-facing byte producer; RL training on tool-augmented
trajectories requires the appended bytes to be REPRODUCIBLE from the log without the
live graph. This battery replays the WHOLE real D0 corpus
(tests/fixtures/gateway_corpus/*.jsonl) through the CURRENT augment() pipeline and pins
two laws:

  1. FLAG-OFF ZERO-ADDITIONS — GT_GATEWAY unset => augment() adds nothing (byte-
     identical to a GT-off run) over every corpus record.
  2. CROSS-PROCESS BYTE-IDENTITY — the same corpus, seeded graph, and pipeline yield
     byte-identical augment output across two SEPARATE python processes with different
     PYTHONHASHSEED (no set-iteration / time / randomness leaks into a delivered fact).

It reuses the corpus-replay + subprocess PATTERN of tests/runtime/test_gateway.py
(the graph is seeded from the corpus's OWN search symbols so producers actually fire)
but does NOT duplicate that file's golden normalization tables — it runs the corpus
through the pipeline and hashes.

STANDALONE: PYTHONIOENCODING=utf-8 python -m pytest tests/contract/ -q
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
_CORPUS = _REPO_ROOT / "tests" / "fixtures" / "gateway_corpus"

_SCHEMA = (
    "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
    " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
    " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
    " language TEXT, parent_id INTEGER);"
    "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
    " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
    " confidence REAL, metadata TEXT);"
)


def _corpus_records() -> list[dict]:
    """Every real corpus record (the __meta__ header line excluded), file-sorted."""
    recs: list[dict] = []
    for fp in sorted(glob.glob(str(_CORPUS / "*.jsonl"))):
        with open(fp, encoding="utf-8") as fh:
            for ln in fh:
                rec = json.loads(ln)
                if "__meta__" in rec:
                    continue
                recs.append(rec)
    return recs


def _seed_graph(db_path: str, records: list[dict]) -> None:
    """Seed a graph with 2 def files per corpus SEARCH symbol (so the AMBIGUOUS
    def/ref partition + name-fold producers actually fire on real commands). Same
    seeding shape as test_gateway's corpus-validate test — a minimal builder, not a
    copy of its golden tables."""
    from groundtruth.runtime.gateway import search_pattern

    syms: set[str] = set()
    for rec in records:
        s = search_pattern(rec["command"])
        if s:
            syms.add(s)
    con = sqlite3.connect(db_path)
    con.executescript(_SCHEMA)
    nid = 1
    for s in sorted(syms):
        for fdir in ("alpha", "beta"):
            con.execute(
                "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,"
                "is_test,language) VALUES(?,?,?,?,?,?,?,?)",
                (nid, "Function", s, f"{fdir}/{s}_mod.py", nid, nid + 4, 0, "python"))
            nid += 1
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# 1. FLAG-OFF: augment adds nothing over the whole corpus (GT-off byte-identity).
# ---------------------------------------------------------------------------
def test_flag_off_zero_additions_over_corpus(tmp_path, monkeypatch):
    """GT_GATEWAY unset => augment() returns [] for EVERY corpus record (the flag-off
    path is byte-identical to GT-off). Home: gateway._gateway_on / augment."""
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    from groundtruth.runtime.gateway import (
        GatewayState, ToolEvent, augment, classify_command,
    )
    records = _corpus_records()
    db = str(tmp_path / "graph.db")
    _seed_graph(db, records)
    st = GatewayState(graph_db=db, repo_root=str(tmp_path))
    total = 0
    for i, rec in enumerate(records):
        ev = ToolEvent(kind=classify_command(rec["command"]), command=rec["command"],
                       output=rec["output"], action_index=i + 1)
        total += len(augment(ev, st))
    assert total == 0, f"flag-off must add nothing, got {total}"


# ---------------------------------------------------------------------------
# 2. CROSS-PROCESS BYTE-IDENTITY: the corpus replay is deterministic across two
#    processes with different PYTHONHASHSEED.
# ---------------------------------------------------------------------------
_RUNNER = '''\
import glob, json, os, sys
sys.path.insert(0, {src!r})
os.environ["GT_GATEWAY"] = "1"
from groundtruth.runtime.gateway import (
    ToolEvent, GatewayState, augment, classify_command,
)
db = {db!r}
root = {root!r}
corpus = {corpus!r}
records = []
for fp in sorted(glob.glob(os.path.join(corpus, "*.jsonl"))):
    with open(fp, encoding="utf-8") as fh:
        for ln in fh:
            rec = json.loads(ln)
            if "__meta__" in rec:
                continue
            records.append(rec)
st = GatewayState(graph_db=db, repo_root=root)
out = []
for i, rec in enumerate(records):
    ev = ToolEvent(kind=classify_command(rec["command"]), command=rec["command"],
                   output=rec["output"], action_index=i + 1)
    for a in augment(ev, st):
        out.append([a.evidence_type, a.target, list(a.payload), list(a.provenance),
                    a.tier, a.producer, a.dedup_key, a.graph_revision,
                    a.confidence, a.preferred_event, a.blocking_eligibility,
                    a.measured, a.valid_until])
print(json.dumps(out, sort_keys=True))
'''


def _run_replay(runner_path: str, seed: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, runner_path], capture_output=True,
                          text=True, env=env)


def test_whole_corpus_determinism_two_processes(tmp_path):
    """The whole D0 corpus, run through the CURRENT augment() pipeline against a graph
    seeded from its own search symbols, yields BYTE-IDENTICAL delivered facts across
    two processes with PYTHONHASHSEED 0 vs 1 — and the fact set is NON-EMPTY (the run
    actually produced deliveries). Home: gateway.augment (TITO law 10 determinism)."""
    records = _corpus_records()
    db = str(tmp_path / "graph.db")
    _seed_graph(db, records)  # built ONCE — identical binary input for both processes
    runner = tmp_path / "replay_runner.py"
    runner.write_text(_RUNNER.format(src=_SRC, db=db, root=str(tmp_path),
                                     corpus=str(_CORPUS)))
    r0 = _run_replay(str(runner), "0")
    r1 = _run_replay(str(runner), "1")
    assert r0.returncode == 0, r0.stderr
    assert r1.returncode == 0, r1.stderr
    assert r0.stdout == r1.stdout                     # cross-process byte-identity
    facts = json.loads(r0.stdout)
    assert facts, "non-vacuous: the corpus replay produced at least one delivered fact"
    # a stable content hash the acceptance gate can pin if it ever wants a golden.
    digest = hashlib.sha256(r0.stdout.encode("utf-8")).hexdigest()
    assert len(digest) == 64


def test_in_process_replay_is_deterministic(tmp_path, monkeypatch):
    """Fast guard: two in-process replays of the whole corpus over the SAME seeded
    graph serialize identically (a cheap determinism check complementing the
    cross-process one). Home: gateway.augment."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    from groundtruth.runtime.gateway import (
        GatewayState, ToolEvent, augment, classify_command,
    )
    records = _corpus_records()
    db = str(tmp_path / "graph.db")
    _seed_graph(db, records)

    def replay() -> str:
        st = GatewayState(graph_db=db, repo_root=str(tmp_path))
        out = []
        for i, rec in enumerate(records):
            ev = ToolEvent(kind=classify_command(rec["command"]),
                           command=rec["command"], output=rec["output"],
                           action_index=i + 1)
            for a in augment(ev, st):
                out.append([a.evidence_type, a.target, list(a.payload),
                            list(a.provenance), a.tier, a.producer, a.dedup_key])
        return json.dumps(out, sort_keys=True)

    first = replay()
    assert first == replay()
    assert json.loads(first), "non-vacuous replay"

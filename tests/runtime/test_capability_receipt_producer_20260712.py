"""Pin: the capability-receipt PRODUCER emits the real baked-surface facts Fix B consumes.

Fix B (2026-07-12, ``rl_profile._MEMBER_CAPABILITY_RECEIPT``) makes the 4 substrate-property
Profile-2 members fail CLOSED against a ``GT_CAPABILITY_RECEIPT``. Without a producer that emits
that receipt from the REAL substrate, every Profile-2 run correctly aborts those members — so GT
would run WEAKER than baseline (3 real capabilities wrongly disabled). This producer closes that
loop: it reads the shipped base graph + staged binary + baked brief and emits the exact fields the
consumer predicates read. The final test is the end-to-end seam: producer JSON -> _available_from_env
admits exactly the 4 members (guards against field-name drift between producer and consumer).

Each field's authoritative source (verified against runtime code 2026-07-12):
  * symbol_content_fts_rows -> SELECT count(*) FROM symbol_content_fts   (content_fts.go:201 surface)
  * sem_body_rows           -> count of properties.kind IN (body_terms,string_literals)
                               (the EXACT C1 fail-closed gate, graph_localizer.py:2288)
  * gt_index_bin            -> GT_INDEX_BIN path iff it is a real file   (staged reindex binary)
  * brief_minimal           -> $GT_CERT_DIR/brief.txt lacks the heavy tags (gt_agent.py:1013)

Fail-closed by construction: any absent/empty/unreadable source => the field's fail-closed value
(0 / "" / False) => the member aborts. A stale substrate can never self-attest a surface it lacks.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from groundtruth.runtime import capability_receipt as cr
from groundtruth.runtime import rl_profile


# ─────────────────────────── fixtures: a real (tiny) substrate ───────────────────────────

def _make_graph(path: Path, *, content_rows: int, body_rows: int) -> None:
    """A minimal graph.db carrying exactly the two surfaces the receipt reads. Plain tables
    (not FTS5) — the producer's count query is table-kind agnostic, so the fixture never depends
    on the sqlite build having the fts5 extension compiled in."""
    c = sqlite3.connect(str(path))
    try:
        c.execute("CREATE TABLE symbol_content_fts (content TEXT)")
        c.executemany("INSERT INTO symbol_content_fts(content) VALUES (?)",
                      [(f"body vocab {i}",) for i in range(content_rows)])
        c.execute("CREATE TABLE properties (node_id INTEGER, kind TEXT, value TEXT)")
        # body-channel rows the sem-body leg reads, plus decoy kinds that must NOT count.
        rows = [(1, "body_terms", "redis tls handshake") for _ in range(body_rows)]
        rows += [(2, "docstring", "unrelated"), (3, "calls", "noise")]
        c.executemany("INSERT INTO properties(node_id,kind,value) VALUES (?,?,?)", rows)
        c.commit()
    finally:
        c.close()


_MINIMAL_BRIEF = "<gt-obligations>\nstep-0 obligations only\n</gt-obligations>\n"
_FULL_BRIEF = (
    "<gt-localization>\nranked files ...\n</gt-localization>\n"
    "<gt-graph-map>\nheavy map ...\n</gt-graph-map>\n"
)


@pytest.fixture()
def substrate(tmp_path: Path) -> dict:
    gdb = tmp_path / "graph.db"
    _make_graph(gdb, content_rows=5, body_rows=3)
    idx = tmp_path / "gt-index"
    idx.write_bytes(b"\x7fELF fake binary")
    cert_dir = tmp_path / "cert"
    cert_dir.mkdir()
    (cert_dir / "brief.txt").write_text(_MINIMAL_BRIEF, encoding="utf-8")
    return {"graph_db": str(gdb), "index_bin": str(idx), "cert_dir": str(cert_dir)}


# ─────────────────────────────── field-by-field truth ───────────────────────────────

def test_content_and_body_row_counts_are_real(substrate: dict) -> None:
    r = cr.build_receipt(graph_db=substrate["graph_db"])
    assert r["symbol_content_fts_rows"] == 5
    assert r["sem_body_rows"] == 3, "must count ONLY body_terms/string_literals, not docstring/calls"


def test_gt_index_bin_present_iff_file_exists(substrate: dict, tmp_path: Path) -> None:
    assert cr.build_receipt(index_bin=substrate["index_bin"])["gt_index_bin"] == substrate["index_bin"]
    # a path that does not resolve => fail-closed empty string (the reindex binary is absent)
    assert cr.build_receipt(index_bin=str(tmp_path / "nope"))["gt_index_bin"] == ""


def test_brief_minimal_true_only_for_minimal_brief(substrate: dict, tmp_path: Path) -> None:
    assert cr.build_receipt(brief_path=str(Path(substrate["cert_dir"]) / "brief.txt"))["brief_minimal"] is True
    full = tmp_path / "full.txt"
    full.write_text(_FULL_BRIEF, encoding="utf-8")
    assert cr.build_receipt(brief_path=str(full))["brief_minimal"] is False
    # absent brief => fail-closed False (a run with no baked brief cannot claim minimal)
    assert cr.build_receipt(brief_path=str(tmp_path / "absent.txt"))["brief_minimal"] is False


# ─────────────────────────────── fail-closed by construction ───────────────────────────────

def test_absent_graph_fails_all_counts_to_zero() -> None:
    r = cr.build_receipt(graph_db="/no/such/graph.db")
    assert r["symbol_content_fts_rows"] == 0
    assert r["sem_body_rows"] == 0


def test_empty_zero_byte_graph_fails_closed(tmp_path: Path) -> None:
    # the known 0-byte-handoff trap (gt_mini_patch.py:1814) must degrade to 0, never crash.
    z = tmp_path / "empty.db"
    z.write_bytes(b"")
    r = cr.build_receipt(graph_db=str(z))
    assert r["symbol_content_fts_rows"] == 0 and r["sem_body_rows"] == 0


def test_graph_missing_the_surface_tables_fails_closed(tmp_path: Path) -> None:
    g = tmp_path / "old.db"
    c = sqlite3.connect(str(g))
    c.execute("CREATE TABLE nodes (id INTEGER)")  # a pre-B1 graph without the content surfaces
    c.commit()
    c.close()
    r = cr.build_receipt(graph_db=str(g))
    assert r["symbol_content_fts_rows"] == 0 and r["sem_body_rows"] == 0


def test_no_inputs_at_all_is_the_fully_closed_receipt() -> None:
    r = cr.build_receipt(env={})
    assert r == {
        "symbol_content_fts_rows": 0,
        "sem_body_rows": 0,
        "gt_index_bin": "",
        "brief_minimal": False,
    }


# ─────────────────────────────── env-driven resolution ───────────────────────────────

def test_env_resolution_mirrors_runtime_precedence(substrate: dict) -> None:
    # GT_HOST_GRAPH_DB is the base-graph source (the untouched mount) — the receipt proves the
    # SHIPPED surface, so it reads the base, never the L6 work-copy.
    env = {
        "GT_HOST_GRAPH_DB": substrate["graph_db"],
        "GT_INDEX_BIN": substrate["index_bin"],
        "GT_CERT_DIR": substrate["cert_dir"],
    }
    r = cr.build_receipt(env=env)
    assert r["symbol_content_fts_rows"] == 5
    assert r["sem_body_rows"] == 3
    assert r["gt_index_bin"] == substrate["index_bin"]
    assert r["brief_minimal"] is True


def test_gt_graph_db_is_the_fallback_when_host_unset(substrate: dict) -> None:
    r = cr.build_receipt(env={"GT_GRAPH_DB": substrate["graph_db"]})
    assert r["symbol_content_fts_rows"] == 5


# ─────────────────────────────── the CLI + the end-to-end seam ───────────────────────────────

def test_emit_json_cli_roundtrips(substrate: dict) -> None:
    import io

    buf = io.StringIO()
    env = {
        "GT_HOST_GRAPH_DB": substrate["graph_db"],
        "GT_INDEX_BIN": substrate["index_bin"],
        "GT_CERT_DIR": substrate["cert_dir"],
    }
    rc = cr.main(["--emit-json"], env=env, out=buf)
    assert rc == 0
    line = buf.getvalue().strip()
    assert "\n" not in line, "receipt JSON must be a single line (captured into one env var)"
    obj = json.loads(line)
    assert obj["symbol_content_fts_rows"] == 5 and obj["sem_body_rows"] == 3


def test_producer_output_admits_exactly_the_four_members(substrate: dict) -> None:
    # THE seam: the producer's JSON, handed to the consumer as GT_CAPABILITY_RECEIPT, must admit
    # exactly the 4 substrate-property members. Bites on any field-name drift between the two sides.
    env = {
        "GT_HOST_GRAPH_DB": substrate["graph_db"],
        "GT_INDEX_BIN": substrate["index_bin"],
        "GT_CERT_DIR": substrate["cert_dir"],
    }
    receipt_json = json.dumps(cr.build_receipt(env=env))
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": receipt_json})
    for m in ("GT_CONTENT_LEG", "GT_SEM_BODY", "GT_BRIEF_MINIMAL", "GT_L6_FRESH"):
        assert m in avail, f"{m} must be admitted once the producer proves its surface"


def _trial_run_text() -> str:
    import yaml

    wf = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "swebench_live_lite_full.yml"
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict) and "trial" in (step.get("name") or "").lower() \
                    and "capability_receipt" in (step.get("run") or ""):
                return step["run"]
    raise AssertionError("no trial step emits GT_CAPABILITY_RECEIPT via capability_receipt")


def test_workflow_emits_receipt_before_the_profile_preflight() -> None:
    # The wiring seam: the receipt must be EXPORTED (so the rl_profile subprocess inherits it) and
    # emitted BEFORE the --emit-exports preflight — else the manifest never sees it and fails the 4
    # substrate-property members closed on a substrate that actually carries the surface.
    run = _trial_run_text()
    assert "export GT_CAPABILITY_RECEIPT=" in run, "receipt must be exported, not a local var"
    i_receipt = run.index("groundtruth.runtime.capability_receipt --emit-json")
    i_profile = run.index("groundtruth.runtime.rl_profile --emit-exports")
    assert i_receipt < i_profile, "receipt export must PRECEDE the rl_profile preflight"


def test_workflow_receipt_line_has_no_apostrophe() -> None:
    # The trial block is a single-quoted docker `bash -c '...'` — one apostrophe closes it and the
    # agent silently never launches. The receipt fallback must use double-quoted echo "{}", not '{}'.
    run = _trial_run_text()
    for line in run.splitlines():
        if "capability_receipt" in line:
            assert "'" not in line, f"apostrophe in receipt line closes the single-quoted block: {line!r}"


def test_body_less_substrate_drops_only_sem_body(substrate: dict, tmp_path: Path) -> None:
    # A substrate with content-fts but NO body channels: GT_CONTENT_LEG stays, GT_SEM_BODY drops.
    g = tmp_path / "nobody.db"
    _make_graph(g, content_rows=4, body_rows=0)
    env = {
        "GT_HOST_GRAPH_DB": str(g),
        "GT_INDEX_BIN": substrate["index_bin"],
        "GT_CERT_DIR": substrate["cert_dir"],
    }
    receipt_json = json.dumps(cr.build_receipt(env=env))
    avail = rl_profile._available_from_env({"GT_RL_PROFILE": "2", "GT_CAPABILITY_RECEIPT": receipt_json})
    assert "GT_CONTENT_LEG" in avail
    assert "GT_SEM_BODY" not in avail, "no body channels => sem-body must fail closed"

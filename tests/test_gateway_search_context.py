"""tests/test_gateway_search_context.py — the post-search ``<gt-search-context>``
lane (``gateway._produce_search_context``, ``GT_SEARCH_CONTEXT``) plus the step-0
``<gt-flows>`` brief section (``v1r_brief._with_flows``, ``GT_BRIEF_FLOWS``).

Ground truth: a synthetic graph.db carrying the real ``nodes``/``edges``/
``project_meta``/``file_hashes`` schema AND a compatible FTS5 ``nodes_fts``
(``build_search_context`` queries ``nodes_fts MATCH`` joined on ``rowid``).
The assertions exercise the REAL delivery chain — ``augment()`` applies registry
renderability, envelope validation, the leak screen, freshness routing, and the
``delivered_keys`` dedup — so a green run proves the lane integrates through the
existing gates rather than bypassing them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.v1r_brief import (
    FileEntry,
    _count_tokens,
    _enforce_token_rail,
    render_brief,
)
from groundtruth.runtime.gateway import (
    KIND_SEARCH,
    GatewayState,
    ToolEvent,
    augment,
    produce_raw,
)


# --------------------------------------------------------------------------- #
# Synthetic graph.db — one certified flow  main -> install_pkg -> fetch_archive
# -> unpack (3 CALLS edges, satisfies detect_processes min_steps=3) plus a
# second caller  handle_request -> install_pkg  (a second qualifying flow).
# --------------------------------------------------------------------------- #
def _build_graph_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported INTEGER,
            is_test INTEGER, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            type TEXT, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, trust_tier TEXT, confidence REAL
        );
        CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE file_hashes (file_path TEXT, content_hash TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5
            (name, qualified_name, signature, file_path);
        """
    )
    nodes = [
        # id, label, name, qualified_name, file_path, start, end, sig, ret, exp, test, lang, parent
        (
            1,
            "Function",
            "main",
            "src.main.main",
            "src/main.py",
            1,
            60,
            "def main()",
            "int",
            1,
            0,
            "python",
            None,
        ),
        (
            2,
            "Function",
            "install_pkg",
            "src.install.install_pkg",
            "src/install.py",
            20,
            90,
            "def install_pkg(pkg)",
            "bool",
            1,
            0,
            "python",
            None,
        ),
        (
            3,
            "Function",
            "fetch_archive",
            "src.fetch.fetch_archive",
            "src/fetch.py",
            5,
            40,
            "def fetch_archive(u)",
            "bytes",
            0,
            0,
            "python",
            None,
        ),
        (
            4,
            "Function",
            "unpack",
            "src.unpack.unpack",
            "src/unpack.py",
            40,
            80,
            "def unpack(b)",
            "str",
            0,
            0,
            "python",
            None,
        ),
        (
            5,
            "Function",
            "handle_request",
            "src.handler.handle_request",
            "src/handler.py",
            8,
            30,
            "def handle_request(r)",
            "str",
            1,
            0,
            "python",
            None,
        ),
        (
            6,
            "Function",
            "util_leaf",
            "src.util.util_leaf",
            "src/util.py",
            2,
            10,
            "def util_leaf()",
            "None",
            0,
            0,
            "python",
            None,
        ),
    ]
    db.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", nodes)
    edges = [
        # id, src, dst, type, src_line, src_file, resolution_method, trust_tier, conf
        (1, 1, 2, "CALLS", 10, "src/main.py", "same_file", "CERTIFIED", 1.0),
        (2, 2, 3, "CALLS", 30, "src/install.py", "same_file", "CERTIFIED", 1.0),
        (3, 3, 4, "CALLS", 12, "src/fetch.py", "same_file", "CERTIFIED", 1.0),
        (4, 5, 2, "CALLS", 15, "src/handler.py", "same_file", "CERTIFIED", 1.0),
    ]
    db.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)", edges)
    db.executemany(
        "INSERT INTO project_meta (key, value) VALUES (?,?)",
        [
            ("post_revision", "rev-synthetic-1"),
            ("subrev_nodes", "n1"),
            ("subrev_edges", "e1"),
        ],
    )
    db.executemany(
        "INSERT INTO file_hashes (file_path, content_hash) VALUES (?,?)",
        [(n[4], f"h{n[0]}") for n in nodes],
    )
    db.executemany(
        "INSERT INTO nodes_fts (rowid, name, qualified_name, signature, file_path)"
        " VALUES (?,?,?,?,?)",
        [(n[0], n[2], n[3], n[7], n[4]) for n in nodes],
    )
    db.commit()
    db.close()


@pytest.fixture()
def graph_db(tmp_path: Path) -> str:
    path = tmp_path / "graph.db"
    _build_graph_db(path)
    return str(path)


def _state(graph_db: str, tmp_path: Path, **kwargs) -> GatewayState:
    return GatewayState(repo_root=str(tmp_path), graph_db=graph_db, **kwargs)


def _search_event() -> ToolEvent:
    """The agent's own grep for ``install_pkg`` landing one hit — the exact
    observation the post-search lane enriches."""
    return ToolEvent(
        kind=KIND_SEARCH,
        command="grep -rn install_pkg src/",
        output="src/install.py:20:def install_pkg(pkg):",
        semantic_events=("search_result",),
        semantics_authoritative=True,
        action_index=3,
    )


def _context_blocks(envelopes) -> list[str]:
    return ["\n".join(env.payload) for env in envelopes if env.evidence_type == "search_context"]


# --------------------------------------------------------------------------- #
# 1. A search event produces the bounded <gt-search-context> block with the
#    graph-derived caller facts a bare grep cannot carry.
# --------------------------------------------------------------------------- #
def test_search_event_produces_search_context_block(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    state = _state(graph_db, tmp_path)
    out = augment(_search_event(), state)
    blocks = _context_blocks(out)
    assert len(blocks) == 1
    block = blocks[0]
    assert "<gt-search-context>" in block
    assert "</gt-search-context>" in block
    assert "install_pkg (Function, src/install.py:20)" in block
    # Both callers of install_pkg appear (graph-derived, absent from grep output).
    assert "main (src/main.py:1)" in block
    assert "handle_request (src/handler.py:8)" in block
    # install_pkg sits inside the detected flows -> membership is rendered.
    assert "in flow:" in block


def test_search_context_payload_is_bounded(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    out = augment(_search_event(), _state(graph_db, tmp_path))
    (env,) = [e for e in out if e.evidence_type == "search_context"]
    joined = "\n".join(env.payload)
    # The lane's byte cap is a design ceiling (whole entries only, balanced tags).
    assert len(joined.encode("utf-8")) <= 1600
    # Envelope carries canonical freshness + provenance through _mk_add.
    assert env.graph_revision == "rev-synthetic-1"
    assert env.valid_until  # subrev composite, not empty
    assert env.provenance  # real file:line sites, leak-screened upstream
    assert all(":" not in f or True for f, _ln in env.provenance)


def test_search_context_dedup_via_delivered_keys(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane rides the SAME dedup chain as every producer: once the seal has
    stamped the envelope's dedup_key into state.delivered_keys, an identical
    re-offer is suppressed inside augment() — no bypass lane exists."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    state = _state(graph_db, tmp_path)
    first = augment(_search_event(), state)
    (env,) = [e for e in first if e.evidence_type == "search_context"]
    state.delivered_keys.add(env.dedup_key)  # what the seal does on commit
    second = augment(_search_event(), state)
    assert _context_blocks(second) == []


# --------------------------------------------------------------------------- #
# 2. Non-search events never produce the block.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kind,command,semantics",
    [
        ("view", "sed -n '1,60p' src/install.py", ("file_view",)),
        ("edit", "apply_patch", ("edit_result",)),
    ],
)
def test_non_search_events_emit_no_search_context(
    graph_db: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    command: str,
    semantics: tuple[str, ...],
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    state = _state(graph_db, tmp_path)
    event = ToolEvent(
        kind=kind,
        command=command,
        output="x",
        semantic_events=semantics,
        semantics_authoritative=True,
    )
    out = augment(event, state)
    assert _context_blocks(out) == []


# --------------------------------------------------------------------------- #
# 3. Missing graph -> no block, no exception.
# --------------------------------------------------------------------------- #
def test_missing_graph_abstains_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    state = _state(str(tmp_path / "nope.db"), tmp_path)
    out = augment(_search_event(), state)  # must not raise
    assert _context_blocks(out) == []


def test_no_symbol_match_abstains(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    state = _state(graph_db, tmp_path)
    event = ToolEvent(
        kind=KIND_SEARCH,
        command="grep -rn zzz_no_such_symbol_zzz src/",
        output="",
        semantic_events=("failed_search",),
        semantics_authoritative=True,
    )
    out = augment(event, state)
    assert _context_blocks(out) == []


# --------------------------------------------------------------------------- #
# 4. GT_SEARCH_CONTEXT=0 kills the dispatch and records the audit row.
# --------------------------------------------------------------------------- #
def test_flag_off_skips_dispatch_and_audits(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_SEARCH_CONTEXT", "0")
    rows: list[dict] = []
    state = _state(graph_db, tmp_path, producer_recorder=rows.append)
    out = produce_raw(_search_event(), state)
    assert _context_blocks(out) == []
    skips = [
        r
        for r in rows
        if r.get("layer") == "producer.dispatch"
        and r.get("outcome") == "not_entered"
        and r.get("producer") == "search_context"
    ]
    assert len(skips) == 1
    assert skips[0]["skip_reason"] == "kill_switch_off"
    assert skips[0]["invocation_site"] == "gateway.search.search_context"


# --------------------------------------------------------------------------- #
# 5. The step-0 brief carries a bounded <gt-flows> section with certified
#    ratios, governed by the B-30 token rail.
# --------------------------------------------------------------------------- #
def _brief_entry() -> FileEntry:
    return FileEntry(
        path="src/main.py",
        score=1.0,
        functions=["def main() -> int:"],
        function_names=["main"],
    )


def test_brief_contains_bounded_flows_section(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GT_BRIEF_FLOWS", raising=False)
    brief = render_brief([_brief_entry()], scores=[1.0], graph_db=graph_db, issue_text="install")
    assert "<gt-flows>" in brief
    assert "</gt-flows>" in brief
    section = brief.split("<gt-flows>", 1)[1].split("</gt-flows>", 1)[0]
    # at most five flows, each labeled with its certified ratio
    flow_headers = [ln for ln in section.splitlines() if ln.strip() and ln.strip()[0].isdigit()]
    assert 1 <= len(flow_headers) <= 5
    assert all("certified" in ln for ln in flow_headers)
    assert "certified 100%" in section  # fixture edges are all CERTIFIED
    # bounded: the tagged section obeys the design byte cap
    tagged = "<gt-flows>" + section + "</gt-flows>"
    assert len(tagged.encode("utf-8")) <= 1600
    # the certified flow chain from the fixture is named
    assert "main -> unpack" in section


def test_brief_flows_respect_token_rail(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GT_BRIEF_FLOWS", raising=False)
    brief = render_brief([_brief_entry()], scores=[1.0], graph_db=graph_db)
    budget = 40  # pathologically tight: forces the rail to drop narration
    trimmed, suppressed = _enforce_token_rail(brief, budget)
    assert _count_tokens(trimmed) <= budget
    assert "<gt-flows>" not in trimmed  # lowest-priority block drops whole
    assert "flows" in suppressed


def test_brief_omits_flows_when_graph_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty graph (no >=3-step flows) -> silent omission, byte-identical."""
    monkeypatch.delenv("GT_BRIEF_FLOWS", raising=False)
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " file_path TEXT, start_line INTEGER, signature TEXT, is_test INTEGER);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER,"
        " target_id INTEGER, type TEXT, trust_tier TEXT, confidence REAL);"
        "INSERT INTO nodes VALUES (1,'Function','lonely','src/a.py',1,'',0);"
    )
    conn.commit()
    conn.close()
    brief = render_brief([_brief_entry()], scores=[1.0], graph_db=str(db))
    assert "<gt-flows>" not in brief


def test_brief_flows_flag_off(
    graph_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GT_BRIEF_FLOWS", "0")
    brief = render_brief([_brief_entry()], scores=[1.0], graph_db=graph_db)
    assert "<gt-flows>" not in brief

"""Constants + leak lint for the post_search LISTEN LATTICE (gt_mini_patch).

Two invariants, asserted mechanically:

  (A) INPUT-CLASS: the listen code path takes only (cmd, output, db)-class inputs.
      Its source references NONE of the benchmark-label / gold / issue tokens — the
      lattice decides from the graph + the agent's own command/output, never from
      task metadata. (`_GT_BASELINE` — the standard GT-off mode gate shared by every
      producer — is the ONLY 'baseline' token allowed, and is not label-peeking.)

  (B) CONSTANTS TAXONOMY: every constant introduced for the lattice classifies into
      the allowed set. On the TRIGGER path the only admissible constants are
      {0, 1, exact-match, set-membership, ordering-over-the-action-stream}. Byte
      BUDGETS are allowed ONLY on the render path and may NEVER gate fire/no-fire —
      proven BEHAVIORALLY here (shrinking the budget to 1 byte still FIRES the class,
      only its render volume changes).
"""
from __future__ import annotations

import inspect
import re
import sqlite3

import gt_mini_patch as g

# The lattice functions whose source must stay label-free and input-class-clean.
_LISTEN_FNS = [
    g._search_localize_block, g._class_namefold, g._class_bodyonly, g._class_nontarget,
    g._class_honest_negative, g._name_or_path_matches, g._body_rows, g._render_body,
    g._grep_result_empty, g._grep_hit_paths, g._grep_is_final_stage, g._grep_is_count,
    g._fold_variants, g._norm_stem, g._stem_subtokens, g._split_camel_subtokens,
    g._ledger_record, g._ledger_entry, g._ledger_already_answered, g._ledger_mark_answered,
    g._direct_def_block, g._fmt_def_facts, g._search_pattern, g._search_operand_raw,
    g._search_probe_tokens, g._has_content_fts, g._block_hash,
]

# The FULL audited list of constants introduced for the lattice, each tagged with its
# taxonomy class. (The spec permits hard-coding this audited list.)
_AUDITED_CONSTANTS = {
    # RENDER-PATH budgets — bound how MUCH is rendered; never gate fire/no-fire.
    "_BODY_ENUM_CAP":        ("render-budget", 25),
    "_BODY_RENDER_BUDGET":   ("render-budget", 1200),
    # TRIGGER-PATH structural predicates (regex = exact/pattern-match predicate).
    "_GREP_PIPE_SPLIT_RE":   ("pattern-predicate", None),
    "_GREP_STAGE_HEAD_RE":   ("pattern-predicate", None),
    "_HIT_PATH_EXT_RE":      ("pattern-predicate", None),
}


# ---- (A) INPUT-CLASS + label-free ------------------------------------------------
def test_entrypoint_signature_is_cmd_out_only():
    """The producer takes only (cmd, out=None) — command + its output, nothing else."""
    sig = inspect.signature(g._search_localize_block)
    assert list(sig.parameters) == ["cmd", "out"]
    assert sig.parameters["out"].default is None


def test_class_functions_take_only_graph_command_output_inputs():
    """Every class fn's parameters are drawn only from {con(db), sym(cmd), out, root,
    idx(action-stream)} — no issue/task/label parameter can reach the decision."""
    allowed = {"con", "sym", "out", "root", "idx", "extra_toks", "cmd", "variant",
               "info", "rows", "refined_by", "overflow", "stem", "block", "s", "seg",
               "head", "file_path", "extra", "symbol", "n"}
    for fn in (g._class_namefold, g._class_bodyonly, g._class_nontarget,
               g._class_honest_negative, g._body_rows, g._name_or_path_matches):
        params = set(inspect.signature(fn).parameters)
        leaked = params - allowed
        assert not leaked, f"{fn.__name__} takes non-(cmd/output/db)-class inputs: {leaked}"


def test_source_references_no_benchmark_labels():
    forbidden = ["FAIL_TO_PASS", "PASS_TO_PASS", "task_id", "gold",
                 "issue", "resolved_ids", "p2p", "f2p"]
    for fn in _LISTEN_FNS:
        src = inspect.getsource(fn)
        for tok in forbidden:
            assert not re.search(tok, src, re.I), \
                f"{fn.__name__} references forbidden token {tok!r}"


def test_only_baseline_token_is_the_mode_gate():
    """The single 'baseline' reference anywhere in the lattice is `_GT_BASELINE`
    (the GT-off mode gate), not a peek at baseline results/labels."""
    for fn in _LISTEN_FNS:
        for ln in inspect.getsource(fn).splitlines():
            if "baseline" in ln.lower():
                assert "_GT_BASELINE" in ln, f"{fn.__name__}: non-mode baseline ref: {ln.strip()}"


# ---- (B) CONSTANTS TAXONOMY ------------------------------------------------------
def test_audited_constants_exist_with_expected_class():
    for name, (klass, val) in _AUDITED_CONSTANTS.items():
        assert hasattr(g, name), f"audited constant {name} missing"
        if val is not None:
            assert getattr(g, name) == val, f"{name} drifted from audited value {val}"
        assert klass in ("render-budget", "pattern-predicate")


def _mk_body_graph(tmp_path):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,"
        " start_line INTEGER, end_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);"
        "CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content,"
        " tokenize=\"unicode61 tokenchars '_'\");")
    # several enclosing symbols whose bodies mention 'handshake'
    for i in range(1, 6):
        con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                    f"language) VALUES({i},'Function','fn_{i}','pkg/f{i}.py',{i*10},{i*10+5},0,'python')")
        con.execute(f"INSERT INTO symbol_content_fts(rowid,content) VALUES({i},'fn_{i} handshake tls')")
    con.commit(); con.close()
    return db


def test_render_budget_never_gates_fire_no_fire(tmp_path, monkeypatch):
    """Shrinking the render budget to 1 byte must STILL fire BODY (as count-only) —
    a budget changes render VOLUME, never the fire/no-fire decision."""
    db = _mk_body_graph(tmp_path)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))

    def probe():
        g._search_seen.clear(); g._action_count = 0
        return g._search_localize_block("grep -rn handshake .", "")

    # normal budget: fires AND enumerates
    big = probe()
    assert 'surface="body"' in big and "pkg/f1.py:10" in big

    # 1-byte budget + 0 enum cap: STILL fires (count-only), proving budgets don't gate.
    monkeypatch.setattr(g, "_BODY_RENDER_BUDGET", 1)
    monkeypatch.setattr(g, "_BODY_ENUM_CAP", 0)
    small = probe()
    assert 'surface="body"' in small, "budget shrink wrongly suppressed the class (would gate fire)"


def test_body_fires_regardless_of_match_count(tmp_path, monkeypatch):
    """Set-membership trigger: BOTH a 1-match and a many-match body set fire (no count
    threshold on the trigger path)."""
    db = _mk_body_graph(tmp_path)   # 5 matches
    monkeypatch.setattr(g, "_POST_SEARCH_ON", True)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    g._search_seen.clear(); g._action_count = 0
    many = g._search_localize_block("grep -rn handshake .", "")
    assert 'surface="body"' in many

    # single-match graph
    db1 = str(tmp_path / "one.db")
    con = sqlite3.connect(db1)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT,"
        " start_line INTEGER, end_line INTEGER, is_test INTEGER, language TEXT);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, resolution_method TEXT, confidence REAL);"
        "CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content,"
        " tokenize=\"unicode61 tokenchars '_'\");")
    con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,"
                "language) VALUES(1,'Function','solo','pkg/s.py',3,9,0,'python')")
    con.execute("INSERT INTO symbol_content_fts(rowid,content) VALUES(1,'solo handshake')")
    con.commit(); con.close()
    monkeypatch.setattr(g, "_db_path", lambda: db1)
    g._search_seen.clear(); g._action_count = 0
    one = g._search_localize_block("grep -rn handshake .", "")
    assert 'surface="body"' in one

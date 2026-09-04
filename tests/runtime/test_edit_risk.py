"""TTD for structural_edit_risk (the blast-radius verification-timing signal).

Asserts the load-bearing properties: a high-fan-in edited symbol is risky; an
isolated leaf is quiet; absence is quiet; a name_match (guessed) caller is NEVER
counted as blast radius; risk is RELATIVE to the repo's own fan-in distribution
(dynamic); an old graph without a confidence column degrades, not crashes.

Pure sqlite. No AI, no task symbols, no per-case tuning.
"""

from __future__ import annotations

import sqlite3

from groundtruth.runtime.edit_risk import structural_edit_risk, reset_reference_cache


def _build(
    tmp_path,
    spec,
    *,
    with_confidence=True,
    method="import",
    conf=1.0,
    name="graph.db",
    etype="CALLS",
):
    """spec = list of (target_name, n_distinct_callers). Builds nodes + ``etype`` edges
    where each named target receives ``n_distinct_callers`` distinct source nodes."""
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT)"
    )
    if with_confidence:
        conn.execute(
            "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
            "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
        )
    else:
        conn.execute(
            "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
            "target_id INTEGER, type TEXT, resolution_method TEXT)"
        )
    nid = 0

    def add(name_):
        nonlocal nid
        nid += 1
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path) VALUES (?,?,?,?)",
            (nid, "Function", name_, "a.py"),
        )
        return nid

    for tname, ncallers in spec:
        tid = add(tname)
        for _ in range(ncallers):
            cid = add(f"caller_{nid + 1}")
            if with_confidence:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, type, resolution_method, "
                    "confidence) VALUES (?,?,?,?,?)",
                    (cid, tid, etype, method, conf),
                )
            else:
                conn.execute(
                    "INSERT INTO edges (source_id, target_id, type, resolution_method) "
                    "VALUES (?,?,?,?)",
                    (cid, tid, etype, method),
                )
    conn.commit()
    conn.close()
    reset_reference_cache()
    return db


def test_high_fanin_edited_symbol_is_risky(tmp_path):
    db = _build(tmp_path, [("hub", 10)] + [(f"t{i}", 1) for i in range(10)])
    r = structural_edit_risk(db, {"hub"})
    assert not r.is_quiet()
    assert r.score > 0.5, r
    top = r.top_reason()
    assert top is not None and top.name == "hub" and top.dependents == 10


def test_isolated_leaf_is_quiet(tmp_path):
    # 'leaf' has zero incoming CALLS (it only calls 'x').
    db = str(tmp_path / "g.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, resolution_method TEXT, confidence REAL)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path) VALUES (?,?,?,?)",
        [(1, "Function", "leaf", "a.py"), (2, "Function", "x", "a.py")],
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,2,'CALLS','import',1.0)"
    )
    conn.commit()
    conn.close()
    reset_reference_cache()
    r = structural_edit_risk(db, {"leaf"})
    assert r.is_quiet() and r.score == 0.0 and r.reasons == ()


def test_absence_is_quiet(tmp_path):
    assert structural_edit_risk(str(tmp_path / "nope.db"), {"x"}).is_quiet()
    assert structural_edit_risk(None, {"x"}).is_quiet()
    assert structural_edit_risk("", {"x"}).is_quiet()
    db = _build(tmp_path, [("hub", 5)])
    assert structural_edit_risk(db, set()).is_quiet()  # no symbols


def test_namematch_callers_are_not_blast_radius(tmp_path):
    # 'hub' has 6 callers but ALL are name_match guesses (conf 0.2 < floor) -> 0 verified.
    db = _build(tmp_path, [("hub", 6)], method="name_match", conf=0.2)
    assert structural_edit_risk(db, {"hub"}).is_quiet()


def test_risk_is_relative_to_repo_distribution(tmp_path):
    # Same symbol (5 callers) is riskier in a low-fan-in repo than a high-fan-in one.
    low = _build(tmp_path, [("T", 5)] + [(f"t{i}", 1) for i in range(8)], name="low.db")
    risk_low = structural_edit_risk(low, {"T"}).score
    high = _build(tmp_path, [("T", 5)] + [(f"t{i}", 12) for i in range(8)], name="high.db")
    risk_high = structural_edit_risk(high, {"T"}).score
    assert risk_low > risk_high, (risk_low, risk_high)


def test_old_schema_without_confidence_degrades(tmp_path):
    db = _build(tmp_path, [("hub", 6)] + [(f"t{i}", 1) for i in range(6)], with_confidence=False)
    r = structural_edit_risk(db, {"hub"})
    assert not r.is_quiet()
    assert r.top_reason().dependents == 6


def test_top_reason_is_the_riskiest_edited_symbol(tmp_path):
    db = _build(tmp_path, [("big", 12), ("small", 2)] + [(f"t{i}", 1) for i in range(6)])
    r = structural_edit_risk(db, {"big", "small"})
    assert r.top_reason().name == "big"
    names = [sr.name for sr in r.reasons]
    assert names.index("big") < names.index("small")


def test_reads_and_writes_count_as_dependents(tmp_path):
    # Depth trickle-down: a field WRITTEN by many places has blast radius too. With
    # CALLS-only this scored 0; READS/WRITES/DATA_FLOW must now count as dependents.
    db = _build(tmp_path, [("field", 8)] + [(f"t{i}", 1) for i in range(8)], etype="WRITES")
    r = structural_edit_risk(db, {"field"})
    assert not r.is_quiet()
    top = r.top_reason()
    assert top is not None and top.name == "field" and top.dependents == 8


def test_namematch_above_floor_still_excluded(tmp_path):
    # A 2-candidate name_match has conf 0.6 (>= the 0.5 floor) but is STILL a guess,
    # never blast radius — must be excluded by resolution_method, not just confidence.
    db = _build(tmp_path, [("hub", 6)], method="name_match", conf=0.6)
    assert structural_edit_risk(db, {"hub"}).is_quiet()


def test_implmethod_is_not_blast_radius(tmp_path):
    # impl_method resolves obj.method() by GLOBAL name-uniqueness with NO receiver-type
    # check (curation_map:98-110) — a cross-receiver/external collision (Array.push ->
    # List.push), NEVER blast radius. conf 0.6 >= floor AND not name_match, so the old
    # NOT-LIKE-name_match blacklist LEAKED it; the canonical fact-set excludes it.
    db = _build(tmp_path, [("hub", 8)], method="impl_method", conf=0.6)
    assert structural_edit_risk(db, {"hub"}).is_quiet()


def test_uniquemethod_is_not_blast_radius(tmp_path):
    # unique_method is the same receiver-unproven rung (curation_map excludes both) —
    # never blast radius.
    db = _build(tmp_path, [("hub", 8)], method="unique_method", conf=0.6)
    assert structural_edit_risk(db, {"hub"}).is_quiet()


def test_promoted_depth_still_counts(tmp_path):
    # the promote pass mints the dependency-DEPTH edges (READS/WRITES/DATA_FLOW) with
    # 'promote_%' provenance — VERIFIED (non-inventing, both endpoints real), so they
    # MUST still count after the fact-set tightening (regression guard: don't nuke the
    # deep-graph blast radius the whitelist alone would drop).
    db = _build(tmp_path, [("field", 8)], method="promote_field_read", conf=0.6, etype="READS")
    assert not structural_edit_risk(db, {"field"}).is_quiet()


# --------------------------------------------------------------------------- #
# R3 — line-range scoping: a node counts only when its DEFINITION line is inside
# an edited hunk. A hub DEFINED high in an edited file but only CALLED in the
# agent's actual hunk (def-line outside the hunk) must NOT win on degree.
# (Live failures: fastapi/push/run named though the agent edited another symbol.)
# --------------------------------------------------------------------------- #
def _build_with_lines(tmp_path, spec, *, name="g.db"):
    """spec = list of (target_name, def_start_line, n_distinct_callers). Each target is
    a node at ``def_start_line`` in a.py receiving ``n_distinct_callers`` verified CALLS."""
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "file_path TEXT, start_line INTEGER)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, "
        "target_id INTEGER, type TEXT, resolution_method TEXT, confidence REAL)"
    )
    nid = 0

    def add(name_, sl):
        nonlocal nid
        nid += 1
        conn.execute(
            "INSERT INTO nodes (id, label, name, file_path, start_line) VALUES (?,?,?,?,?)",
            (nid, "Function", name_, "a.py", sl),
        )
        return nid

    for tname, sl, ncallers in spec:
        tid = add(tname, sl)
        for _ in range(ncallers):
            cid = add(f"caller_{nid + 1}", 1000 + nid)
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, resolution_method, "
                "confidence) VALUES (?,?,'CALLS','import',1.0)",
                (cid, tid),
            )
    conn.commit()
    conn.close()
    reset_reference_cache()
    return db


def test_hub_referenced_not_defined_in_edited_range_is_quiet(tmp_path):
    # 'hub' is DEFINED at line 5 of a.py (high fan-in). The agent edited ONLY lines
    # 40-60 of a.py — where it merely CALLS hub. Def-line 5 is outside 40-60, so hub
    # is a reference, not the agent's change -> it must NOT be named (R3 fix).
    db = _build_with_lines(tmp_path, [("hub", 5, 10)] + [(f"t{i}", 100 + i, 1) for i in range(10)])
    r = structural_edit_risk(db, {"hub"}, edited_files={"a.py"}, edited_ranges={"a.py": [(40, 60)]})
    assert r.is_quiet(), r  # hub's definition is outside the edited hunk


def test_symbol_defined_in_edited_range_still_fires(tmp_path):
    # Control: 'victim' is DEFINED at line 50 (inside the edited 40-60 hunk) with high
    # fan-in. The agent edited its DEFINITION -> the range filter must NOT over-suppress.
    db = _build_with_lines(
        tmp_path, [("victim", 50, 10)] + [(f"t{i}", 100 + i, 1) for i in range(10)]
    )
    r = structural_edit_risk(
        db, {"victim"}, edited_files={"a.py"}, edited_ranges={"a.py": [(40, 60)]}
    )
    assert not r.is_quiet(), r
    top = r.top_reason()
    assert top is not None and top.name == "victim" and top.dependents == 10


def test_line_ranges_separate_the_hub_from_the_real_edit(tmp_path):
    # Both in scope by file AND by name; only line-range disambiguates. hub@5 (ref'd,
    # 10 deps) vs construct@50 (defined in the hunk, 2 deps). The R3 fix must name the
    # symbol the agent DEFINED (construct), never the higher-degree REFERENCED hub.
    db = _build_with_lines(
        tmp_path, [("hub", 5, 10), ("construct", 50, 2)] + [(f"t{i}", 100 + i, 1) for i in range(8)]
    )
    r = structural_edit_risk(
        db, {"hub", "construct"}, edited_files={"a.py"}, edited_ranges={"a.py": [(40, 60)]}
    )
    names = [sr.name for sr in r.reasons]
    assert "hub" not in names, names  # referenced, not defined in the hunk
    assert "construct" in names, names  # the agent's actual edit


def test_no_ranges_is_unchanged_back_compat(tmp_path):
    # Without edited_ranges the behavior is the legacy file-scope: hub (defined in the
    # edited file) still fires. Proves the new param is opt-in, not a behavior change.
    db = _build_with_lines(tmp_path, [("hub", 5, 10)] + [(f"t{i}", 100 + i, 1) for i in range(10)])
    r = structural_edit_risk(db, {"hub"}, edited_files={"a.py"})
    assert not r.is_quiet(), r
    assert r.top_reason().name == "hub"

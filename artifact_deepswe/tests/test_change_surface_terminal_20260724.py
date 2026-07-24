"""AUDIT 2026-07-24 — change_surface TERMINAL STATE.

newfile_precedent / GT_CHANGE_SURFACE emitted ZERO ledger rows across all 4 tasks of run
30121930273 — not even an eligibility row — so "engine abstained", "found only leaky targets",
"engine faulted" and "never consulted" were indistinguishable. Same silent-zero class the
edit.syntax denominator fixed. Zero model bytes; the dominance VERDICT must be unchanged.
"""
from __future__ import annotations
import gt_mini_patch as g


def _arm(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "graph.db"))
    monkeypatch.setattr(g, "_issue_text", lambda: "add a new exporter module")
    monkeypatch.setattr(g, "_cs_dominance_memo", None, raising=False)
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    monkeypatch.setenv("GT_CS_TELEMETRY", "1")
    monkeypatch.setattr(g, "_runtime_ledger_record",
                        lambda **kw: rows.append((kw.get("kind"), kw.get("reason"))))


def test_engine_consult_always_records_an_opportunity(tmp_path, monkeypatch):
    """A consult must ALWAYS leave a classified row — never a silent zero."""
    rows = []
    _arm(monkeypatch, tmp_path, rows)
    g._change_surface_dominates()
    cs = [r for k, r in rows if k == "change_surface"]
    assert cs, f"REGRESSION: change_surface consult left NO ledger row; got {rows}"
    assert str(cs[0]).startswith("cs_opportunity:"), f"unclassified: {cs[0]}"


def test_engine_fault_is_visible_not_silent(tmp_path, monkeypatch):
    """A faulting engine must be reported, not silently degrade to 'no opportunity'."""
    rows = []
    _arm(monkeypatch, tmp_path, rows)
    import groundtruth.pretask.change_surface as cs_mod

    def _boom(*a, **k):
        raise RuntimeError("engine down")
    monkeypatch.setattr(cs_mod, "detect_change_surface", _boom)
    assert g._change_surface_dominates() is False, "a fault must keep the honest negative"
    cs = [r for k, r in rows if k == "change_surface"]
    assert any("engine_fault" in str(r) for r in cs), f"fault not surfaced: {cs}"


def test_flag_off_stays_byte_identical(tmp_path, monkeypatch):
    """GT_CHANGE_SURFACE=0 must short-circuit BEFORE any consult or telemetry."""
    rows = []
    _arm(monkeypatch, tmp_path, rows)
    monkeypatch.setenv("GT_CHANGE_SURFACE", "0")
    assert g._change_surface_dominates() is False
    assert not [r for k, r in rows if k == "change_surface"], "flag-off must not record a consult"


def test_telemetry_flag_off_is_byte_identical(tmp_path, monkeypatch):
    """GT_CS_TELEMETRY=0 (default) must emit NO row — an unconditional extra row landed in the
    durable ledger delta ss_gate compares and made the gate flake RED 1-in-5."""
    rows = []
    _arm(monkeypatch, tmp_path, rows)
    monkeypatch.setenv("GT_CS_TELEMETRY", "0")
    g._change_surface_dominates()
    assert not [r for k, r in rows if k == "change_surface"], \
        "telemetry must be strictly opt-in — the proof gate must never see an extra row"


def test_the_repeat_gate_itself_is_countable(tmp_path, monkeypatch):
    """THE bottleneck: change_surface is reachable ONLY after the agent fails the SAME search
    stem TWICE (`_class_honest_negative`'s prior_zero gate). A first zero-result probe must now
    leave a countable near-miss row — otherwise 'never consulted' stays indistinguishable from
    'consulted and abstained', and the b22f655ba telemetry (placed BEHIND this gate) is dead."""
    import sqlite3
    rows = []
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_CS_TELEMETRY", "1")
    monkeypatch.setattr(g, "_runtime_ledger_record",
                        lambda **kw: rows.append((kw.get("kind"), kw.get("reason"))))
    db = str(tmp_path / "g.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, is_test INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, resolution_method TEXT, confidence REAL);")
    con.commit()
    # a stem probed ONCE with a zero result -> the gate stops here today
    monkeypatch.setattr(g, "_search_seen",
                        {g._norm_stem("missing_sym"): {"probe_indices": [], "outcomes": []}},
                        raising=False)
    g._class_honest_negative(con, "missing_sym", 5, str(tmp_path))
    con.close()
    cs = [r for k, r in rows if k == "change_surface"]
    assert any("cs_gate:first_zero_probe_no_repeat" in str(r) for r in cs), \
        f"the repeat-gate near-miss is still invisible: {rows}"

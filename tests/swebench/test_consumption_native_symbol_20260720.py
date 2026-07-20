"""D-Y (run6: referenced=0 for EVERY producer despite delivered>0): the
consumption-ledger receipt ladder was DARK on the tagless native path. _block_entities
extracted symbols only from [CALLERS]/[WITNESS]/def/class markers — all absent in
the native delivery forms (bare `path:line:sym` and the D-S `path:line: note: sym …`
compiler-note) — so `pats` carried files only and the referenced-rung detector could
never match a symbol mention in the agent's later prose.

Fix: extract the trailing symbol identifier from the native row forms too.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "scripts" / "swebench" / "consumption_ledger.py"


def _load():
    spec = importlib.util.spec_from_file_location("consumption_ledger_dy", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def test_bare_native_row_symbol_extracted():
    m = _load()
    files, symbols = m._block_entities("src/api/routes.py:88:get_session")
    assert "get_session" in symbols, "bare native row symbol must be extracted"
    assert "src/api/routes.py" in files


def test_note_form_symbol_extracted():
    m = _load()
    files, symbols = m._block_entities(
        "src/api/routes.py:88: note: get_session - verify your change is consistent here")
    assert "get_session" in symbols, "compiler-note (D-S) row symbol must be extracted"
    # the note prose must NOT become spurious symbols
    assert "verify" not in symbols and "consistent" not in symbols


def test_tagged_markers_still_extracted():
    m = _load()
    _, symbols = m._block_entities("[CALLERS] process_config\nsrc/x.py:5:process_config")
    assert "process_config" in symbols


def test_referenced_rung_fires_on_native_symbol_reference():
    # end-to-end intent: with the symbol now in pats, an agent naming the symbol in
    # LATER prose promotes the receipt to >=2 (referenced). Exercise the predicate path.
    m = _load()
    _, symbols = m._block_entities("src/api/routes.py:88:get_session")
    pats = m._entity_patterns(set(), symbols)          # symbol-only pats
    assert m._named_in("I will update get_session to keep the signature", pats) is True
    assert m._named_in("unrelated prose about something else", pats) is False

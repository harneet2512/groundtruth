"""Fixture-driven microbench for the obligation engine.

Loads unified diffs + mock graph definitions from tests/fixtures/obligation_diffs/,
runs infer_from_patch(), and compares actual obligations against expected.json.

Parsing limitations surfaced by these fixtures:
- _parse_changed_symbols only detects `def`/`async def`/`class` lines
- Body-only changes (no new def/class line) produce zero changed symbols
- Variable assignments, type aliases, decorators-only are invisible
- Non-Python keywords (function, func, fn, interface, struct) are not matched
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from groundtruth.index.graph import ImportGraph, Reference
from groundtruth.index.store import SymbolRecord, SymbolStore
from groundtruth.utils.result import Ok
from groundtruth.validators.obligations import ObligationEngine

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "obligation_diffs"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NEXT_ID = 9000  # avoid collisions with test_obligations.py if run together


def _sym_from_dict(d: dict) -> SymbolRecord:
    return SymbolRecord(
        id=d["id"],
        name=d["name"],
        kind=d["kind"],
        language="python",
        file_path=d["file_path"],
        line_number=d.get("line"),
        end_line=d.get("end_line"),
        is_exported=True,
        signature=d.get("signature"),
        params=None,
        return_type=None,
        documentation=None,
        usage_count=0,
        last_indexed_at=0,
    )


def _build_engine(setup: dict) -> ObligationEngine:
    """Build an ObligationEngine from a declarative setup.json dict."""
    symbols = [_sym_from_dict(s) for s in setup["symbols"]]
    sym_index = {i: sym for i, sym in enumerate(symbols)}

    store = MagicMock(spec=SymbolStore)
    graph = MagicMock(spec=ImportGraph)

    # resolve_symbol: maps name -> symbol via resolve_map
    resolve_map: dict[str, int] = setup.get("resolve_map", {})

    def mock_resolve(name: str, file_context: str | None = None) -> Ok:
        idx = resolve_map.get(name)
        if idx is not None and idx < len(symbols):
            return Ok(symbols[idx])
        return Ok(None)

    store.resolve_symbol.side_effect = mock_resolve

    # symbols_in_range: indices into symbols list
    range_syms = [symbols[i] for i in setup.get("symbols_in_range", [])]
    store.get_symbols_in_line_range.return_value = Ok(range_syms)

    # symbols_in_file
    file_syms = [symbols[i] for i in setup.get("symbols_in_file", [])]
    store.get_symbols_in_file.return_value = Ok(file_syms)

    # attributes
    store.get_attributes_for_symbol.return_value = Ok(setup.get("attributes", []))

    # subclasses
    sub_indices = setup.get("subclasses", [])
    sub_syms = [symbols[i] for i in sub_indices]
    store.get_subclasses.return_value = Ok(sub_syms)

    # callers
    callers_raw = setup.get("callers", [])
    caller_refs = [Reference(file_path=c["file_path"], line=c.get("line"), context=c.get("context", "")) for c in callers_raw]
    graph.find_callers.return_value = Ok(caller_refs)

    # symbol_by_id
    by_id_map: dict[int, SymbolRecord] = {}
    for k, v in setup.get("symbol_by_id", {}).items():
        sym_id = int(k)
        if isinstance(v, int) and v < len(symbols):
            by_id_map[sym_id] = symbols[v]
    store.get_symbol_by_id.side_effect = lambda mid: Ok(by_id_map.get(mid))

    engine = ObligationEngine(store, graph)

    # Override _get_class_methods for subclass scenarios
    subclass_methods_raw = setup.get("subclass_methods", {})
    if subclass_methods_raw:
        original_get = engine._get_class_methods

        def patched_get(cls: SymbolRecord) -> list[SymbolRecord]:
            key = str(cls.id)
            if key in subclass_methods_raw:
                return [symbols[i] for i in subclass_methods_raw[key]]
            return original_get(cls)

        engine._get_class_methods = patched_get  # type: ignore[assignment]

    return engine


def _match_obligation(actual: dict, expected: dict) -> bool:
    """Check if an actual obligation matches an expected spec."""
    for key, val in expected.items():
        if key == "confidence_gte":
            if actual.get("confidence", 0) < val:
                return False
        elif actual.get(key) != val:
            return False
    return True


# ---------------------------------------------------------------------------
# Discover fixture scenarios
# ---------------------------------------------------------------------------

def _discover_scenarios() -> list[tuple[str, Path]]:
    """Find all scenario dirs containing expected.json."""
    scenarios = []
    if not FIXTURES_DIR.exists():
        return scenarios
    for d in sorted(FIXTURES_DIR.iterdir()):
        if d.is_dir() and (d / "expected.json").exists():
            scenarios.append((d.name, d))
    return scenarios


SCENARIOS = _discover_scenarios()


@pytest.mark.parametrize("name,scenario_dir", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_obligation_fixture(name: str, scenario_dir: Path) -> None:
    """Run a single obligation fixture scenario."""
    # Load files
    diff_files = list(scenario_dir.glob("*.patch")) + list(scenario_dir.glob("*.diff"))
    assert diff_files, f"No .patch or .diff file in {scenario_dir}"

    diff_text = "\n".join(f.read_text(encoding="utf-8") for f in sorted(diff_files))
    setup = json.loads((scenario_dir / "setup.json").read_text(encoding="utf-8"))
    expected = json.loads((scenario_dir / "expected.json").read_text(encoding="utf-8"))

    # Build engine and run
    engine = _build_engine(setup)
    actual_obligations = engine.infer_from_patch(diff_text)

    # Convert to dicts for comparison
    actual_dicts = [
        {
            "kind": o.kind,
            "source": o.source,
            "target": o.target,
            "target_file": o.target_file,
            "confidence": o.confidence,
        }
        for o in actual_obligations
    ]

    if not expected:
        # False-positive case: should produce no obligations
        assert actual_dicts == [], (
            f"Expected no obligations for {name}, got {len(actual_dicts)}: {actual_dicts}"
        )
        return

    # Each expected entry must match at least one actual obligation
    unmatched = []
    for exp in expected:
        found = any(_match_obligation(act, exp) for act in actual_dicts)
        if not found:
            unmatched.append(exp)

    assert not unmatched, (
        f"Unmatched expected obligations in {name}:\n"
        f"  Expected (unmatched): {json.dumps(unmatched, indent=2)}\n"
        f"  Actual: {json.dumps(actual_dicts, indent=2)}"
    )

    # No spurious obligation kinds beyond what's expected
    expected_kinds = {e["kind"] for e in expected}
    actual_kinds = {a["kind"] for a in actual_dicts}
    unexpected = actual_kinds - expected_kinds
    assert not unexpected, (
        f"Unexpected obligation kinds in {name}: {unexpected}\n"
        f"  Actual: {json.dumps(actual_dicts, indent=2)}"
    )

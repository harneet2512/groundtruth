"""G4 pin: the canonical LSP cert must be the DECLARED task language's cert, not
the graph-DOMINANT language's. On a python task whose graph is vendored-JS
dominant, copying langs[0] (js) makes the canonical cert a no-op that masks the
real python LSP outcome.

Loaded by path — scripts/ is not a package.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "scripts" / "swebench" / "gt_run_proof.py"
_spec = importlib.util.spec_from_file_location("gt_run_proof", _MOD)
assert _spec and _spec.loader
grp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = grp
_spec.loader.exec_module(grp)


def _touch(p: Path) -> None:
    p.write_text("{}", encoding="utf-8")


def test_g4_declared_language_cert_wins_over_dominant(tmp_path) -> None:
    _touch(tmp_path / "lsp_certificate_python.json")
    _touch(tmp_path / "lsp_certificate_javascript.json")
    src, lang = grp._canonical_cert_source(str(tmp_path), "python", "javascript")
    assert lang == "python"
    assert src.endswith("lsp_certificate_python.json")


def test_g4_falls_back_to_dominant_when_declared_cert_absent(tmp_path) -> None:
    # only the dominant (js) cert exists; declared python produced none
    _touch(tmp_path / "lsp_certificate_javascript.json")
    src, lang = grp._canonical_cert_source(str(tmp_path), "python", "javascript")
    assert lang == "javascript"
    assert src.endswith("lsp_certificate_javascript.json")


def test_g4_no_declared_language_uses_dominant(tmp_path) -> None:
    _touch(tmp_path / "lsp_certificate_go.json")
    src, lang = grp._canonical_cert_source(str(tmp_path), "", "go")
    assert lang == "go"
    assert src.endswith("lsp_certificate_go.json")


def test_g4_lsp_stamped_edge_count_reads_only_lsp_edges(tmp_path) -> None:
    import sqlite3
    db = str(tmp_path / "g.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, resolution_method TEXT)")
    con.executemany(
        "INSERT INTO edges (resolution_method) VALUES (?)",
        [("lsp",), ("lsp_verified",), ("import",), ("same_file",), ("name_match",)],
    )
    con.commit()
    con.close()
    assert grp._count_lsp_stamped_edges(db) == 2

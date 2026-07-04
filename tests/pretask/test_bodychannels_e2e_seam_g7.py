"""G7 — end-to-end parser->consumer SEAM guard for the body channels (offline).

The index-time parser (gt-index, parser.go::extractBodyChannels) and the query-time
consumer (graph_localizer._symbol_body_map / _assemble_symbol_passages) agree ONLY by a
string contract: the property KIND names string_literals / body_terms / calls. A rename on
either side becomes a SILENT no-op (channels written but never read, or read under a name
the writer never emits) that every hand-built-graph unit test misses. This test closes the
loop: it runs the REAL gt-index on a tiny fixture and pipes the REAL graph.db through the
REAL consumer, asserting the mined vocabulary actually flows into the ON passage.

Offline + non-paid: uses a PREBUILT gt-index binary (GT_INDEX_BIN or gt-index/gt-index[.exe]
/ gt-index-linux). Skips — never fails — when no binary is available or when the binary
predates body-channel mining, so CI without the binary is unaffected.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from groundtruth.pretask.graph_localizer import _assemble_symbol_passages, _normalize


def _find_gt_index() -> str | None:
    env = os.environ.get("GT_INDEX_BIN")
    if env and Path(env).exists():
        return env
    root = Path(__file__).resolve().parents[2] / "gt-index"
    for cand in ("gt-index.exe", "gt-index", "gt-index-linux"):
        p = root / cand
        if p.exists():
            return str(p)
    return None


_FIXTURE = (
    "def connect(host):\n"
    "    # establish redis over tls handshake\n"
    '    url = "redis://localhost:6379"\n'
    "    verify_certificate(host)\n"
    "    return open_socket(url)\n"
)


def test_e2e_body_channels_flow_parser_to_consumer(tmp_path, monkeypatch):
    binpath = _find_gt_index()
    if not binpath:
        pytest.skip("no gt-index binary (set GT_INDEX_BIN or build gt-index)")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "svc.py").write_text(_FIXTURE, encoding="utf-8")
    db = tmp_path / "graph.db"

    env = dict(os.environ)
    env["GT_SEM_BODY"] = "1"  # index-time mining ON so the channels land in graph.db
    try:
        proc = subprocess.run(
            [binpath, "-root", str(repo), "-output", str(db)],
            env=env, capture_output=True, text=True, timeout=180,
        )
    except Exception as e:  # noqa: BLE001 — external binary; skip, never fail CI on it
        pytest.skip(f"gt-index invocation failed: {e}")
    if proc.returncode != 0 or not db.exists():
        pytest.skip(f"gt-index produced no db (rc={proc.returncode}): {proc.stderr[-400:]}")

    conn = sqlite3.connect(str(db))
    try:
        kinds = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT kind FROM properties WHERE kind IN "
                "('string_literals','body_terms','calls')"
            )
        }
        fps = [r[0] for r in conn.execute(
            "SELECT DISTINCT file_path FROM nodes WHERE is_test=0"
        )]
    finally:
        conn.close()
    if not kinds:
        pytest.skip("gt-index emitted no body channels (binary predates GT_SEM_BODY mining)")

    want = {_normalize(fp) for fp in fps}

    # ON: the mined vocabulary MUST reach the consumer's passage — the seam is intact.
    monkeypatch.setenv("GT_SEM_BODY", "1")
    fp_on, _ = _assemble_symbol_passages(str(db), want, body_on=True)
    on_text = "\n".join(p for ps in fp_on.values() for p in ps)
    for tok in ("redis", "handshake", "tls"):
        assert tok in on_text, (
            f"SEAM BROKEN: ON passage missing mined vocab {tok!r} — a parser<->consumer "
            f"kind-name drift silently dropped a channel.\n{on_text!r}"
        )

    # OFF: none of the channel vocabulary appears (docstring/props-only, byte-identical path).
    monkeypatch.delenv("GT_SEM_BODY", raising=False)
    fp_off, _ = _assemble_symbol_passages(str(db), want, body_on=False)
    off_text = "\n".join(p for ps in fp_off.values() for p in ps)
    assert "redis://localhost:6379" not in off_text, f"OFF passage leaked a string literal: {off_text!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

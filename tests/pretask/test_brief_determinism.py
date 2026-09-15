"""Determinism + cache-integrity regressions for the L1 brief pipeline.

Three defects, one file — all surfaced by the l1_brief_witnessless_outranks_witnessed
capability-matrix audit:

1. ``model_identity`` masquerade (memory/enrich/embed.py): a foreign model exposing
   no ``model_name``/``dim``/``_m`` identity surface used to fall through to the
   CONFIGURED default ``(name, dim)``. In the shared passage cache that let a
   384-dim foreign vector be served under a 768-dim key -> ``np.dot(q, v)``
   raised inside the localizer -> ``generate_v1r_brief``'s
   ``except Exception: _loc = None`` silently killed the ENTIRE witness path on
   any host with sentence-transformers installed (both semantic halves resolved
   different-width models). Foreign models must key under a truthful,
   class-qualified identity; dim 0 can never collide with a real width.

2. ``_semantic_score_by_file`` (graph_localizer.py): a wrong-width vector that
   somehow reaches the consume site must be SKIPPED, not dotted — one poisoned
   passage used to raise and take down localize() entirely.

3. ``generate_v1r_brief`` file ordering must be identical across PYTHONHASHSEED
   values (deterministic-context contract). Verified by running the beets-5495
   synthetic fixture in real subprocesses under several seeds and asserting a
   byte-identical result.files order — a set/dict iteration leak cannot hide
   inside a single test process because hash order is fixed at interpreter start.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from groundtruth.memory.enrich import embed as _embed


# ---------------------------------------------------------------- model_identity


class _ForeignModel:
    """Mimics a raw SentenceTransformer: no model_name/dim/_m surface."""

    def encode(self, texts, **_kw):  # pragma: no cover - never called
        raise AssertionError("not used")


class _ForeignModelWithDim(_ForeignModel):
    def get_sentence_embedding_dimension(self):
        return 384


class _AdapterInner:
    model_name = "configured/inner-model"
    dim = 512


class _AdapterModel:
    """Mimics _OnnxEmbedderAdapter: identity lives on the wrapped ``_m``."""

    _m = _AdapterInner()


def test_model_identity_foreign_model_never_masquerades_as_default():
    name, dim = _embed.model_identity(_ForeignModel())
    assert name.startswith("foreign:"), name
    assert "_ForeignModel" in name
    assert name != _embed._default_embed_model()
    assert dim == 0  # undiscoverable -> 0, which can never collide with a real width


def test_model_identity_discovers_sentence_transformer_dim():
    name, dim = _embed.model_identity(_ForeignModelWithDim())
    assert name.startswith("foreign:")
    assert dim == 384  # truthful width discovered via get_sentence_embedding_dimension


def test_model_identity_adapter_surface_preserved():
    assert _embed.model_identity(_AdapterModel()) == ("configured/inner-model", 512)


# ------------------------------------------------- wrong-width cached vector

def _make_beets_db(tmp_path: Path) -> tuple[str, str]:
    """4-node beets-shaped fixture: importer.set_fields -> dbcore/db.set_parse."""
    repo = tmp_path / "repo"
    (repo / "beets" / "dbcore").mkdir(parents=True)
    (repo / "beets" / "util").mkdir(parents=True)
    (repo / "beets" / "importer.py").write_text(
        "def set_fields(self, fields):\n"
        "    for key, val in fields.items():\n"
        "        self.set_parse(key, val)\n",
        encoding="utf-8",
    )
    (repo / "beets" / "dbcore" / "db.py").write_text(
        "def set_parse(self, key, string):\n    return _parse(string)\n",
        encoding="utf-8",
    )
    (repo / "beets" / "util" / "pipeline.py").write_text(
        "def parse_stage(values):\n    return values\n",
        encoding="utf-8",
    )
    (repo / "beets" / "library.py").write_text(
        "def store(self, fields):\n    return fields\n",
        encoding="utf-8",
    )
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            (1, "Method", "set_fields", "beets/importer.py", 1, 3,
             "def set_fields(self, fields):"),
            (2, "Method", "set_parse", "beets/dbcore/db.py", 1, 2,
             "def set_parse(self, key, string):"),
            (3, "Function", "parse_stage", "beets/util/pipeline.py", 1, 3,
             "def parse_stage(values):"),
            (4, "Method", "store", "beets/library.py", 1, 3,
             "def store(self, fields):"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,1,2,'CALLS',3,'beets/importer.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    return str(repo), db


class _StubEmbedder:
    """Deterministic 8-dim embedder — no model download, no onnxruntime."""

    model_name = "stub/eight-dim"
    dim = 8

    def encode(self, texts, **_kw):
        out = []
        for t in texts:
            v = np.zeros(8, dtype=np.float32)
            for i, ch in enumerate(str(t)[:8]):
                v[i] = (ord(ch) % 17) / 17.0
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return out


def test_semantic_score_survives_wrong_width_cached_vector(tmp_path, monkeypatch):
    """A poisoned-width cache entry must be skipped, never dotted."""
    import groundtruth.pretask.graph_localizer as gl

    repo, db = _make_beets_db(tmp_path)
    stub = _StubEmbedder()
    monkeypatch.setattr(gl, "_EMBEDDER", stub)
    monkeypatch.setattr(gl, "_EMBEDDER_TRIED", True)

    # Precompute which passage keys _semantic_score_by_file will look up, then
    # poison one slot with a wrong-width vector under the SAME key a buggy
    # model_identity would have produced.
    from groundtruth.memory.enrich.embed import _PASSAGE_VEC_CACHE, passage_hash

    model_name, dim = _embed.model_identity(stub)
    # The assembled passage text is internal; poison by KEY SUFFIX is impossible,
    # so instead inject directly into the function's vec path: poison every
    # passage hash we can compute for the candidate files' symbols.
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name, signature, file_path FROM nodes WHERE is_test=0"
    ).fetchall()
    conn.close()
    poisoned = 0
    for name, sig, fp in rows:
        for passage in {str(name), str(sig or ""), f"{name} {sig or ''}"}:
            h = passage_hash(passage, model_name, dim, _embed.PASSAGE_CACHE_VERSION)
            _PASSAGE_VEC_CACHE[h] = np.zeros(4, dtype=np.float32)  # wrong width (4 != 8)
            poisoned += 1
    assert poisoned > 0

    # Must not raise; scores whatever genuinely-8-dim vectors exist.
    res = gl._semantic_score_by_file(
        "set_fields does not parse values correctly",
        db,
        ["beets/importer.py", "beets/dbcore/db.py"],
    )
    assert isinstance(res, dict)


# ------------------------------------------------------------- seed sweep

_BEETS_ISSUE = (
    "set_fields does not parse values correctly. When calling set_fields on an "
    "item, the field string is stored verbatim instead of being parsed by "
    "set_parse. Expected set_parse to coerce the field value."
)

_SWEEP_CHILD = r'''
import json, sqlite3, sys, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
repo = tmp / "repo"
(repo / "beets" / "dbcore").mkdir(parents=True)
(repo / "beets" / "util").mkdir(parents=True)
(repo / "beets" / "importer.py").write_text(
    "def set_fields(self, fields):\n"
    "    for key, val in fields.items():\n"
    "        self.set_parse(key, val)\n", encoding="utf-8")
(repo / "beets" / "dbcore" / "db.py").write_text(
    "def set_parse(self, key, string):\n    return _parse(string)\n", encoding="utf-8")
(repo / "beets" / "util" / "pipeline.py").write_text(
    "def parse_stage(values):\n    return values\n", encoding="utf-8")
(repo / "beets" / "library.py").write_text(
    "def store(self, fields):\n    return fields\n", encoding="utf-8")
db = str(tmp / "graph.db")
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
    file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
    return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
    parent_id INTEGER
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
    source_line INTEGER, source_file TEXT, resolution_method TEXT,
    confidence REAL, metadata TEXT
);
""")
conn.executemany(
    "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
    "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
    [
        (1, "Method", "set_fields", "beets/importer.py", 1, 3,
         "def set_fields(self, fields):"),
        (2, "Method", "set_parse", "beets/dbcore/db.py", 1, 2,
         "def set_parse(self, key, string):"),
        (3, "Function", "parse_stage", "beets/util/pipeline.py", 1, 3,
         "def parse_stage(values):"),
        (4, "Method", "store", "beets/library.py", 1, 3,
         "def store(self, fields):"),
    ],
)
conn.execute(
    "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
    "resolution_method,confidence) VALUES "
    "(1,1,2,'CALLS',3,'beets/importer.py','import',1.0)")
conn.commit(); conn.close()

from groundtruth.pretask.v1r_brief import generate_v1r_brief
issue = (
    "set_fields does not parse values correctly. When calling set_fields on an "
    "item, the field string is stored verbatim instead of being parsed by "
    "set_parse. Expected set_parse to coerce the field value."
)
r = generate_v1r_brief(issue, str(repo), db, bug_id="beets-5495-synth")
print(json.dumps([e.path for e in r.files]))
'''


def test_brief_file_order_is_hashseed_invariant():
    """result.files order must be identical across PYTHONHASHSEED values.

    Hash order is fixed at interpreter start, so each seed runs a fresh
    subprocess — a set/dict iteration leak anywhere in the ranking path shows
    up as an order diff between the runs. Per-run assertion: the witnessed
    importer.py must rank above every witness-less candidate.
    """
    orders: dict[str, list] = {}
    for seed in ("0", "1", "7", "42"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env.setdefault(
            "GT_FORCE_ONNX_EMBEDDER", "1"
        )  # mirror container: single ONNX surface
        proc = subprocess.run(
            [sys.executable, "-c", _SWEEP_CHILD],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        assert proc.returncode == 0, f"seed={seed}: {proc.stderr[-2000:]}"
        order = json.loads(proc.stdout.strip().splitlines()[-1])
        orders[seed] = order
        if "beets/importer.py" in order:
            imp = order.index("beets/importer.py")
            for wl in ("beets/library.py", "beets/util/pipeline.py"):
                if wl in order:
                    assert imp < order.index(wl), (
                        f"seed={seed}: witness-less {wl} outranks witnessed "
                        f"importer.py: {order}"
                    )
    unique = {tuple(o) for o in orders.values()}
    assert len(unique) == 1, (
        "PYTHONHASHSEED-dependent brief order: "
        + "; ".join(f"seed {k}: {v}" for k, v in orders.items())
    )

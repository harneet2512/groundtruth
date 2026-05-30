"""TTD red-before-green for Task #48 (P1 LEVER, EDIT-TARGET CONTRACTS) and
Task #45 (P0 HARM, confident-wrong "Highest-confidence candidate" suppression).

Both fixtures are built from the REAL beets trajectory failure mode, not from
reading the implementation:

  importer.py::set_fields calls db.py:722 ``set_parse(self, key, string: str)``.
  A second identical ``set_fields`` exists in SingletonImportTask (same file).
  beetsplug/zero.py also defines an unrelated ``set_fields(self, item, tags)``
  (homonym). The deciding fact the agent greps db.py for is the callee signature
  ``set_parse(self, key, string: str)``.

TASK #48 DEFECT (red): the brief never delivers the contract of the methods the
edit-target CALLS. ``contract_line`` hardcodes ``include_callees=False`` and
``_fmt_one`` suppressed callee signatures (``if ev.signature and not
ev.is_callee``). So ``set_parse(self, key, string: str)`` is never sent.
ORACLE: the rendered brief string must CONTAIN ``set_parse(self, key, string:
str)``. Before the fix it does NOT (red); after, it does (green).

TASK #45 DEFECT (red): the brief emits ``Highest-confidence candidate ...
{top.path}`` gated only on a [VERIFIED] tier + score gap, but on a name_match-
only ranking it confidently names a WRONG file. ORACLE: when the top file has
ONLY name_match backing, the ``Highest-confidence candidate`` line must be
ABSENT. With genuine verified backing it may appear (negative control).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# graph.db fixture matching the beets reference case (TTD: NOT derived from the
# implementation — it encodes the observed importer.py/db.py/zero.py topology).
# ---------------------------------------------------------------------------
_SET_PARSE_SIG = "def set_parse(self, key, string: str):"


def _make_beets_db(tmp: str, *, callee_resolution: str, callee_conf: float) -> str:
    """Build a graph.db where importer.py::set_fields --CALLS--> db.py::set_parse.

    ``callee_resolution`` / ``callee_conf`` drive the edge provenance so the test
    can exercise both the verified path (import / 1.0) and a name_match guess.
    Also seeds the SingletonImportTask::set_fields twin (same file) and the
    unrelated zero.py::set_fields homonym, so a correct fix must still pick the
    verified db.py callee and not be confused by the homonyms.
    """
    importer = "beets/importer.py"
    db_py = "beets/dbcore/db.py"
    zero = "beetsplug/zero.py"
    db = os.path.join(tmp, "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    # node 1: importer.py::set_fields (the edit-target the agent is editing)
    conn.execute(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,is_test,language) VALUES "
        "(1,'Method','set_fields','beets.importer.ImportTask.set_fields',?,"
        "200,215,'def set_fields(self, **kwargs):',0,'python')",
        (importer,),
    )
    # node 2: db.py::set_parse (the CALLED method — its sig is the deciding fact)
    conn.execute(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,is_test,language) VALUES "
        "(2,'Method','set_parse','beets.dbcore.db.Model.set_parse',?,"
        "722,730,?,0,'python')",
        (db_py, _SET_PARSE_SIG),
    )
    # node 3: SingletonImportTask::set_fields — identical twin, SAME file.
    conn.execute(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,is_test,language) VALUES "
        "(3,'Method','set_fields','beets.importer.SingletonImportTask.set_fields',?,"
        "400,412,'def set_fields(self, **kwargs):',0,'python')",
        (importer,),
    )
    # node 4: zero.py::set_fields — unrelated HOMONYM, DIFFERENT file/signature.
    conn.execute(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,is_test,language) VALUES "
        "(4,'Method','set_fields','beetsplug.zero.ZeroPlugin.set_fields',?,"
        "55,70,'def set_fields(self, item, tags):',0,'python')",
        (zero,),
    )
    # edge: set_fields --CALLS--> set_parse (provenance under test)
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (1,2,'CALLS',722,?,?,?)",
        (importer, callee_resolution, callee_conf),
    )
    conn.commit()
    conn.close()
    return db


# ===========================================================================
# TASK #48 — EDIT-TARGET CONTRACTS: the callee signature must reach the brief.
# ===========================================================================
def test_callee_contracts_verified_edge_exposes_signature():
    """build_contract(include_callees=True) + _fmt_one must render the verified
    callee signature ``set_parse(self, key, string: str)`` (red before fix:
    _fmt_one dropped callee signatures via `not ev.is_callee`)."""
    from groundtruth.pretask.contract_map import build_contract, render_contract

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_beets_db(tmp, callee_resolution="import", callee_conf=1.0)
        items = build_contract(db, [("beets/importer.py", "set_fields")], include_callees=True)
        rendered = render_contract(items)
        assert "set_parse(self, key, string: str)" in rendered, (
            f"verified callee signature was suppressed:\n{rendered}"
        )


def test_edit_target_callee_contracts_struct():
    """The dedicated public builder returns the verified callee with its sig +
    location (db.py:722), and NEVER the same-file twin or zero.py homonym as a
    callee (they are not call targets of set_fields)."""
    from groundtruth.pretask.contract_map import edit_target_callee_contracts

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_beets_db(tmp, callee_resolution="import", callee_conf=1.0)
        callees = edit_target_callee_contracts(db, "beets/importer.py", ["set_fields"])
        assert callees, "no callee contracts returned for a verified callee edge"
        cc = callees[0]
        assert cc.callee == "set_parse"
        assert cc.file == "beets/dbcore/db.py"
        assert cc.line == 722
        assert "set_parse" in cc.signature
        # No homonym / twin leaked in as a callee.
        assert all(c.file == "beets/dbcore/db.py" for c in callees)


def test_edit_target_callee_contracts_name_match_suppressed():
    """Correct-or-quiet: a name_match call target is NEVER claimed as a callee
    contract (it would launder a guess as a fact)."""
    from groundtruth.pretask.contract_map import edit_target_callee_contracts

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_beets_db(tmp, callee_resolution="name_match", callee_conf=0.9)
        callees = edit_target_callee_contracts(db, "beets/importer.py", ["set_fields"])
        assert callees == [], (
            f"name_match callee was laundered as a contract fact: {callees}"
        )


def test_brief_renders_edit_target_contracts_block():
    """END-TO-END (the Task #48 oracle): the rendered brief string must CONTAIN
    the deciding callee signature ``set_parse(self, key, string: str)``.

    Red before fix: contract_line hardcodes include_callees=False and the brief
    has no EDIT-TARGET CONTRACTS block, so the signature is absent. Green after:
    the block renders it for the top-ranked file."""
    from groundtruth.pretask.v1r_brief import FileEntry, render_brief

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_beets_db(tmp, callee_resolution="import", callee_conf=1.0)
        top = FileEntry(
            path="beets/importer.py",
            score=0.9,
            functions=["def set_fields(self, **kwargs):"],
            function_names=["set_fields"],
        )
        runner_up = FileEntry(path="beets/library.py", score=0.4)
        brief = render_brief([top, runner_up], scores=[0.9, 0.4], graph_db=db)
        assert "EDIT-TARGET CONTRACTS" in brief, (
            f"EDIT-TARGET CONTRACTS block missing:\n{brief}"
        )
        assert "set_parse(self, key, string: str)" in brief, (
            f"deciding callee signature not delivered to agent:\n{brief}"
        )


def test_brief_omits_edit_target_contracts_when_no_verified_callee():
    """Correct-or-quiet negative control: when the only callee edge is name_match,
    the EDIT-TARGET CONTRACTS block is OMITTED entirely (no header, no guess)."""
    from groundtruth.pretask.v1r_brief import FileEntry, render_brief

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_beets_db(tmp, callee_resolution="name_match", callee_conf=0.9)
        top = FileEntry(
            path="beets/importer.py",
            score=0.9,
            functions=["def set_fields(self, **kwargs):"],
            function_names=["set_fields"],
        )
        runner_up = FileEntry(path="beets/library.py", score=0.4)
        brief = render_brief([top, runner_up], scores=[0.9, 0.4], graph_db=db)
        assert "EDIT-TARGET CONTRACTS" not in brief, (
            f"unverified callee leaked an EDIT-TARGET CONTRACTS block:\n{brief}"
        )


# ===========================================================================
# TASK #45 — suppress confident-wrong "Highest-confidence candidate" line.
# ===========================================================================
def _make_namematch_only_db(tmp: str) -> str:
    """A graph where the TOP file (pipeline.py, the beets ev1 mislocalization)
    has ONLY name_match edges — no deterministic backing. Naming it as the
    'highest-confidence candidate' would be confidently wrong."""
    pipeline = "beets/util/pipeline.py"
    db = os.path.join(tmp, "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "signature,is_test,language) VALUES "
        "(1,'Function','run',?,10,30,'def run(self):',0,'python')",
        (pipeline,),
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "signature,is_test,language) VALUES "
        "(2,'Function','stage',?,40,60,'def stage(self):',0,'python')",
        (pipeline,),
    )
    # ONLY name_match edges back this file -> no verified provenance.
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,2,'CALLS','name_match',0.6)"
    )
    conn.commit()
    conn.close()
    return db, pipeline


def _make_verified_db(tmp: str):
    """A graph where the top file has a genuine import-verified edge."""
    mod = "pkg/api.py"
    db = os.path.join(tmp, "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "signature,is_test,language) VALUES "
        "(1,'Function','handler',?,10,30,'def handler(req):',0,'python')",
        (mod,),
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "signature,is_test,language) VALUES "
        "(2,'Function','validate',?,40,60,'def validate(req):',0,'python')",
        (mod,),
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,2,'CALLS','import',1.0)"
    )
    conn.commit()
    conn.close()
    return db, mod


def _verified_entry(path: str):
    """A FileEntry that earns a [VERIFIED] tier (contract with func-name spans),
    so only the name_match-backing gate (Task #45) decides the candidate line."""
    from groundtruth.pretask.v1r_brief import FileEntry

    return FileEntry(
        path=path,
        score=0.95,
        functions=["def run(self):"],
        function_names=["run"],
        # _entry_confidence_tier returns [VERIFIED] when contract has "() in ".
        contract="run() in " + os.path.basename(path) + ":12 `self.run()`",
        test_mappings=["tests/test_x.py::test_run"],
    )


def test_highest_confidence_line_suppressed_on_namematch_only_top():
    """RED-before-fix oracle: with a clear score gap and a [VERIFIED] tier but a
    top file backed ONLY by name_match, the 'Highest-confidence candidate' line
    must be ABSENT (it confidently named the wrong file before the fix)."""
    from groundtruth.pretask.v1r_brief import render_brief

    with tempfile.TemporaryDirectory() as tmp:
        db, pipeline = _make_namematch_only_db(tmp)
        top = _verified_entry(pipeline)
        from groundtruth.pretask.v1r_brief import FileEntry

        runner_up = FileEntry(path="beets/library.py", score=0.4)
        brief = render_brief([top, runner_up], scores=[0.95, 0.4], graph_db=db)
        assert "Highest-confidence candidate" not in brief, (
            f"named a name_match-only top file as highest-confidence:\n{brief}"
        )


def test_highest_confidence_line_present_on_verified_top():
    """Negative control: a genuinely verified top file with a clear gap MAY be
    named (no over-suppression)."""
    from groundtruth.pretask.v1r_brief import render_brief, FileEntry

    with tempfile.TemporaryDirectory() as tmp:
        db, mod = _make_verified_db(tmp)
        top = FileEntry(
            path=mod,
            score=0.95,
            functions=["def handler(req):"],
            function_names=["handler"],
            contract="handler() in " + os.path.basename(mod) + ":12 `handler(r)`",
            test_mappings=["tests/test_api.py::test_handler"],
        )
        runner_up = FileEntry(path="pkg/other.py", score=0.4)
        brief = render_brief([top, runner_up], scores=[0.95, 0.4], graph_db=db)
        assert "Highest-confidence candidate" in brief, (
            f"verified top file was over-suppressed:\n{brief}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

"""Static patch-delta engine (W-C) — Stage-1 determinism + leak-law + abstentions.

Proves the STATIC fallback for the ~98% of edits with no executed covering RED:
(a) arity-mismatch advisories on FACT-tier callers, (b) companion registration +
co-change advisories. Every fixture asserts the EXACT conservative behaviour on a
controlled input (real sqlite graph + real repo tree), never a mock, and pins the
leak law (is_test / name_match callers NEVER surfaced) and the abstentions
(defaulted param, **kwargs, multiline, decorator) that keep the engine
correct-or-quiet.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from groundtruth.runtime.patch_delta import (
    CochangePartner,
    CompanionSurface,
    PatchDeltaResult,
    SignatureMismatch,
    analyze_patch_delta,
)

_NODES_SCHEMA = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
    "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, "
    "signature TEXT, return_type TEXT, is_exported INT, is_test INT, "
    "language TEXT, parent_id INT)"
)
_EDGES_SCHEMA = (
    "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
    "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, "
    "confidence REAL, metadata TEXT)"
)


def _make_graph(path, nodes, edges) -> None:
    """nodes: (id, name, file_path, is_test); edges: (src, tgt, type, method, conf, src_file, src_line)."""
    con = sqlite3.connect(str(path))
    con.execute(_NODES_SCHEMA)
    con.execute(_EDGES_SCHEMA)
    for nid, name, fpath, is_test in nodes:
        con.execute(
            "INSERT INTO nodes (id, label, name, file_path, is_test, language) VALUES (?,?,?,?,?,?)",
            (nid, "Function", name, fpath, is_test, "python"),
        )
    for src, tgt, etype, method, conf, sfile, sline in edges:
        con.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method, "
            "confidence, source_file, source_line) VALUES (?,?,?,?,?,?,?)",
            (src, tgt, etype, method, conf, sfile, sline),
        )
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch):
    """The engine is flag-gated; every behavioural test runs with it ON."""
    monkeypatch.setenv("GT_PATCH_DELTA", "1")


# ---------------------------------------------------------------------------
# (a) SIGNATURE-COMPAT
# ---------------------------------------------------------------------------
def _write_caller(repo: Path, rel: str, body: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_added_required_param_fires_mismatch_with_right_line(tmp_path):
    """(1) A function gains a required positional param + a FACT-edge caller with
    the OLD arity -> a mismatch advisory carrying the exact call-site line."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert len(res.signature_mismatches) == 1, res
    m = res.signature_mismatches[0]
    assert isinstance(m, SignatureMismatch)
    assert (m.symbol, m.caller, m.caller_file, m.caller_line) == (
        "get_user", "use", "src/caller.py", 2)
    assert m.positional_args == 1
    assert (m.old_min_params, m.old_max_params) == (1, 1)
    assert (m.new_min_params, m.new_max_params) == (2, 2)
    assert m.call_site_text == "return get_user(5)"
    assert m.tier == "WARNING"


def test_removed_param_fires_too_many_args(tmp_path):
    """A dropped positional param leaves an over-supplied caller incompatible."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return f(1, 2)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "f", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.95, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def f(a, b):\n    return a\n", "def f(a):\n    return a\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert len(res.signature_mismatches) == 1, res
    assert res.signature_mismatches[0].positional_args == 2


def test_dotted_method_call_fires_with_self_bound(tmp_path):
    """A method gains a required param; a dotted caller (self implicit) mismatches."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use(obj):\n    return obj.m(1)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "m", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.8, "src/caller.py", 2)],
    )
    edited = {"src/api.py": (
        "class C:\n    def m(self, a):\n        return a\n",
        "class C:\n    def m(self, a, b):\n        return a\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert len(res.signature_mismatches) == 1, res
    m = res.signature_mismatches[0]
    # self dropped at the dotted call site: old accepts exactly 1, new needs 2.
    assert (m.old_min_params, m.old_max_params) == (1, 1)
    assert (m.new_min_params, m.new_max_params) == (2, 2)
    assert m.positional_args == 1


def test_defaulted_new_param_abstains(tmp_path):
    """(2) The added param has a DEFAULT that covers the delta -> ABSTAIN."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name=\"x\"):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_kwargs_call_abstains(tmp_path):
    """(3) The caller unpacks **kwargs at the call site -> ABSTAIN (never a claim)."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use(opts):\n    return get_user(**opts)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_multiline_call_abstains(tmp_path):
    """A call not closed on the source_line line -> ABSTAIN (cannot count safely)."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(\n        5,\n    )\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_decorator_abstains(tmp_path):
    """A decorator may rewrite the call convention -> ABSTAIN even on an arity change."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "@deco\ndef get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_is_test_caller_never_surfaced(tmp_path):
    """(5) A test caller must NEVER surface (leak law)."""
    repo = tmp_path
    _write_caller(repo, "tests/test_caller.py", "def test_use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "test_use", "tests/test_caller.py", 1)],
        edges=[(2, 1, "CALLS", "import", 0.9, "tests/test_caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_name_match_caller_never_surfaced(tmp_path):
    """(6) A name_match (guess) caller is not a FACT -> NEVER surfaced."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "name_match", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_low_confidence_fact_method_never_surfaced(tmp_path):
    """A deterministic method BELOW the 0.7 fact floor is not a fact -> excluded."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.5, "src/caller.py", 2)],  # FACT method, sub-floor conf
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_call_site_still_valid_no_false_positive(tmp_path):
    """The caller already passes the new arity (e.g. same-file caller updated) ->
    no mismatch (the valid-after guard prevents a false claim)."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5, 'bob')\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_new_file_has_no_signature_advisory(tmp_path):
    """A brand-new file (before=None) has no prior callers to break -> (a) silent."""
    repo = tmp_path
    db = repo / "graph.db"
    _make_graph(db, nodes=[(1, "get_user", "src/api.py", 0)], edges=[])
    edited = {"src/new.py": (None, "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


# ---------------------------------------------------------------------------
# (b)(1) COMPANION REGISTRATION SURFACES
# ---------------------------------------------------------------------------
def test_registration_surface_advisory_fires(tmp_path):
    """(4) A registry file referencing aws+gcp (>=2 parallel siblings) but NOT the
    newly-added `azure`, untouched by the diff -> a WARNING companion advisory
    carrying the sibling referencing lines."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "registry.py").write_text(
        'PROVIDERS = {\n    "aws": aws,\n    "gcp": gcp,\n}\n', encoding="utf-8")
    before = "def aws():\n    pass\n\n\ndef gcp():\n    pass\n"
    after = before + "\n\ndef azure():\n    pass\n"
    edited = {"src/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert len(res.companion_surfaces) == 1, res
    c = res.companion_surfaces[0]
    assert isinstance(c, CompanionSurface)
    assert c.symbol == "azure"
    assert c.edited_file == "src/providers.py"
    assert c.file == "src/registry.py"
    assert c.siblings == ("aws", "gcp")
    lines = {ln for ln, _ in c.referencing_lines}
    assert lines == {2, 3}  # aws on line 2, gcp on line 3
    assert c.tier == "WARNING"


def test_registration_surface_only_one_sibling_abstains(tmp_path):
    """<2 parallel siblings referenced is an incidental match, not a registry -> quiet."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "registry.py").write_text('X = {"aws": aws}\n', encoding="utf-8")
    before = "def aws():\n    pass\n\n\ndef gcp():\n    pass\n"
    after = before + "\n\ndef azure():\n    pass\n"
    edited = {"src/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert res.companion_surfaces == []


def test_registration_surface_already_registered_abstains(tmp_path):
    """If the surface already references the added symbol, nothing was forgotten."""
    repo = tmp_path
    (repo / "src").mkdir()
    (repo / "src" / "registry.py").write_text(
        'PROVIDERS = {\n    "aws": aws,\n    "gcp": gcp,\n    "azure": azure,\n}\n',
        encoding="utf-8")
    before = "def aws():\n    pass\n\n\ndef gcp():\n    pass\n"
    after = before + "\n\ndef azure():\n    pass\n"
    edited = {"src/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert res.companion_surfaces == []


def test_registration_surface_test_file_never_scanned(tmp_path):
    """A test file that references the family must NEVER be a companion (leak law)."""
    repo = tmp_path
    (repo / "tests").mkdir()
    (repo / "tests" / "test_providers.py").write_text(
        'def test_all():\n    assert aws and gcp\n', encoding="utf-8")
    before = "def aws():\n    pass\n\n\ndef gcp():\n    pass\n"
    after = before + "\n\ndef azure():\n    pass\n"
    edited = {"src/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert res.companion_surfaces == []


# ---------------------------------------------------------------------------
# (b)(2) CO-CHANGE PARTNERS (reuse of pretask.cochange, INFO tier)
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_cochange_partner_is_info(tmp_path):
    """A file that historically co-changes with an edited file (>=2 commits), not in
    the diff, surfaces as an INFO co-change partner."""
    repo = tmp_path
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    prim = repo / "core.py"
    partner = repo / "helper.py"
    for i in range(3):
        prim.write_text(f"# core rev {i}\n", encoding="utf-8")
        partner.write_text(f"# helper rev {i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"rev {i}")
    edited = {"core.py": ("# core rev 2\n", "# core rev 2 edited\n")}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    partners = [p for p in res.cochange_partners if p.file == "helper.py"]
    assert partners, res.cochange_partners
    p = partners[0]
    assert isinstance(p, CochangePartner)
    assert p.tier == "INFO"
    assert p.count >= 2
    assert "core.py" in p.primaries


# ---------------------------------------------------------------------------
# F2 — SQL LIKE metacharacters / suffix collisions (bounce repro FP-1 / FP-4)
# ---------------------------------------------------------------------------
def test_underscore_in_edited_path_never_matches_sibling(tmp_path):
    """Editing pkg/a_b.py must NOT attribute a caller of the UNCHANGED pkg/axb.py
    (`_` is a LIKE any-char wildcard). Bounce repro FP-1."""
    repo = tmp_path
    _write_caller(repo, "pkg/caller2.py", "def use2():\n    return f(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "f", "pkg/a_b.py", 0), (2, "f", "pkg/axb.py", 0),
               (3, "use2", "pkg/caller2.py", 0)],
        # the caller calls axb.py's f — NOT the edited file's f
        edges=[(3, 2, "CALLS", "import", 0.9, "pkg/caller2.py", 2)],
    )
    edited = {"pkg/a_b.py": ("def f(x):\n    return x\n", "def f(x, y):\n    return x\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_parallel_package_suffix_never_matches(tmp_path):
    """Editing pkg/api.py must NOT attribute a caller of other/pkg/api.py (leading-%
    suffix collision). Bounce repro FP-4."""
    repo = tmp_path
    _write_caller(repo, "sub/caller3.py", "def use3():\n    return g(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "g", "pkg/api.py", 0), (2, "g", "other/pkg/api.py", 0),
               (3, "use3", "sub/caller3.py", 0)],
        edges=[(3, 2, "CALLS", "import", 0.9, "sub/caller3.py", 2)],
    )
    edited = {"pkg/api.py": ("def g(x):\n    return x\n", "def g(x, y):\n    return x\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_root_level_edit_never_matches_prefixed_sibling(tmp_path):
    """Editing root-level api.py must NOT attribute a caller of legacy_api.py
    (a bare suffix match ignores the name boundary)."""
    repo = tmp_path
    _write_caller(repo, "caller4.py", "def use4():\n    return g(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "g", "api.py", 0), (2, "g", "legacy_api.py", 0),
               (3, "use4", "caller4.py", 0)],
        edges=[(3, 2, "CALLS", "import", 0.9, "caller4.py", 2)],
    )
    edited = {"api.py": ("def g(x):\n    return x\n", "def g(x, y):\n    return x\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_dot_slash_stored_path_still_attributed(tmp_path):
    """POSITIVE control for F2: a graph that stored the target as ./src/api.py must
    still attribute the caller when src/api.py is edited (normalization, not a
    suffix guess)."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "./src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert len(res.signature_mismatches) == 1, res


# ---------------------------------------------------------------------------
# F3 — method-ness from AST context, never the first-param NAME (bounce FP-2)
# ---------------------------------------------------------------------------
def test_class_method_nonself_first_param_abstains(tmp_path):
    """class C: def m(a,b,c=0) -> m(a,b,x,c=0); caller obj.m(1,2) is VALID after
    (obj binds a). A first-param-NAME heuristic falsely fires -> must ABSTAIN."""
    repo = tmp_path
    _write_caller(repo, "pkg/caller.py", "def use(obj):\n    return obj.m(1, 2)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "m", "pkg/mod.py", 0), (2, "use", "pkg/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "pkg/caller.py", 2)],
    )
    edited = {"pkg/mod.py": (
        "class C:\n    def m(a, b, c=0):\n        return b\n",
        "class C:\n    def m(a, b, x, c=0):\n        return b\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


def test_module_level_function_called_dotted_abstains(tmp_path):
    """A dotted call to a MODULE-LEVEL function: binding cannot be proven (module
    attribute vs rebound method) -> ABSTAIN on dotted-call arity."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use(api):\n    return api.get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


# ---------------------------------------------------------------------------
# F5 — nested same-quote f-string (py3.12) on the call line -> abstain
# ---------------------------------------------------------------------------
def test_nested_same_quote_fstring_call_abstains(tmp_path):
    """f"{d["k"]}" (3.12 same-quote nesting) mis-toggles any quote scanner -> the
    line must ABSTAIN, never produce a parsed-arity claim."""
    repo = tmp_path
    _write_caller(repo, "src/caller.py",
                  'def use(d):\n    return get_user(f"{d["k"]}")\n')
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.signature_mismatches == []


# ---------------------------------------------------------------------------
# F4 — deterministic ordering of mismatch rows
# ---------------------------------------------------------------------------
def test_mismatch_rows_deterministically_ordered(tmp_path):
    """Two mismatching callers at different confidence -> rows ordered by
    confidence DESC deterministically."""
    repo = tmp_path
    _write_caller(repo, "src/c1.py", "def u1():\n    return get_user(5)\n")
    _write_caller(repo, "src/c2.py", "def u2():\n    return get_user(6)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "u1", "src/c1.py", 0),
               (3, "u2", "src/c2.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.8, "src/c1.py", 2),
               (3, 1, "CALLS", "import", 0.95, "src/c2.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert [m.caller for m in res.signature_mismatches] == ["u2", "u1"]
    assert [m.confidence for m in res.signature_mismatches] == [0.95, 0.8]


# ---------------------------------------------------------------------------
# F1 — companion surface must be REGISTRATION-shaped, never consumer/comment
# ---------------------------------------------------------------------------
def test_consumer_file_is_not_a_companion(tmp_path):
    """A plain CONSUMER (`return alpha() + beta()`) is not a registry -> quiet.
    Bounce repro FP-3a."""
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "consumer.py").write_text(
        "def run_it():\n    return alpha() + beta()  # legit consumer, no registry\n",
        encoding="utf-8")
    before = "def alpha():\n    pass\n\n\ndef beta():\n    pass\n\n\ndef gamma():\n    pass\n"
    after = before + "\n\ndef delta():\n    pass\n"
    edited = {"pkg/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert res.companion_surfaces == []


def test_docstring_and_comment_mentions_never_count(tmp_path):
    """References that exist ONLY in a docstring + a comment are not registrations
    -> quiet. Bounce repro FP-3b."""
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "notes.py").write_text(
        '"""Design notes: alpha and beta subsystems interact here."""\n'
        "# TODO revisit gamma later\n"
        "def unrelated():\n    return 42\n",
        encoding="utf-8")
    before = "def alpha():\n    pass\n\n\ndef beta():\n    pass\n\n\ndef gamma():\n    pass\n"
    after = before + "\n\ndef delta():\n    pass\n"
    edited = {"pkg/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert res.companion_surfaces == []


def test_all_export_list_is_a_registration_surface(tmp_path):
    """POSITIVE control: an __all__ list naming >=2 siblings but not the added
    symbol still fires (string entries of __all__ are a registration shape)."""
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "exports.py").write_text(
        '__all__ = ["alpha", "beta"]\n', encoding="utf-8")
    before = "def alpha():\n    pass\n\n\ndef beta():\n    pass\n\n\ndef gamma():\n    pass\n"
    after = before + "\n\ndef delta():\n    pass\n"
    edited = {"pkg/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert len(res.companion_surfaces) == 1, res
    c = res.companion_surfaces[0]
    assert c.symbol == "delta" and c.file == "pkg/exports.py"
    assert set(c.siblings) == {"alpha", "beta"}


def test_init_reexport_is_a_registration_surface(tmp_path):
    """POSITIVE control: an __init__.py re-exporting >=2 siblings but not the added
    symbol fires (package-surface import IS a registration shape there)."""
    repo = tmp_path
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text(
        "from .providers import alpha, beta\n", encoding="utf-8")
    before = "def alpha():\n    pass\n\n\ndef beta():\n    pass\n\n\ndef gamma():\n    pass\n"
    after = before + "\n\ndef delta():\n    pass\n"
    edited = {"pkg/providers.py": (before, after)}
    res = analyze_patch_delta(edited, str(repo), str(repo / "nope.db"))
    assert len(res.companion_surfaces) == 1, res
    c = res.companion_surfaces[0]
    assert c.symbol == "delta" and c.file == "pkg/__init__.py"
    assert set(c.siblings) == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# Flag gate + empties
# ---------------------------------------------------------------------------
def test_flag_off_returns_empty(tmp_path, monkeypatch):
    """Unset/0 flag -> an empty result immediately, no work done."""
    monkeypatch.setenv("GT_PATCH_DELTA", "0")
    repo = tmp_path
    _write_caller(repo, "src/caller.py", "def use():\n    return get_user(5)\n")
    db = repo / "graph.db"
    _make_graph(
        db,
        nodes=[(1, "get_user", "src/api.py", 0), (2, "use", "src/caller.py", 0)],
        edges=[(2, 1, "CALLS", "import", 0.9, "src/caller.py", 2)],
    )
    edited = {"src/api.py": ("def get_user(uid):\n    return uid\n",
                             "def get_user(uid, name):\n    return uid\n")}
    res = analyze_patch_delta(edited, str(repo), str(db))
    assert res.is_empty
    assert res == PatchDeltaResult()


def test_no_input_abstains(tmp_path):
    res = analyze_patch_delta({}, str(tmp_path), "")
    assert res.is_empty and res.abstained

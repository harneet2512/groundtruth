"""Pins the structural (non-git) co-change v2 signal: name-twin method PAIRS found
WITHIN issue-relevant files — keyed on the file, not a guessed edit_target (v1's bug).
Exercises the cfn-3764 loss shape (values()/value() in one class) + the guards from the
v1 post-mortem (dunders excluded, no edit_target needed, big-file over-fire capped)."""
import sqlite3

from groundtruth.pretask.structural_cochange import twin_pairs_in_files, _stem_twin


def _graph() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, is_test INTEGER DEFAULT 0, parent_id INTEGER)"
    )
    # cfn-3764 shape: class _ForEachCollection with twin methods value() and values(),
    # plus dunders + a generic method that must NOT pair.
    c.execute("INSERT INTO nodes VALUES (1,'Class','_ForEachCollection','','cfn/_language_extensions.py',0,NULL)")
    c.execute("INSERT INTO nodes VALUES (2,'Method','values','','cfn/_language_extensions.py',0,1)")
    c.execute("INSERT INTO nodes VALUES (3,'Method','value','','cfn/_language_extensions.py',0,1)")
    c.execute("INSERT INTO nodes VALUES (4,'Method','__init__','','cfn/_language_extensions.py',0,1)")
    c.execute("INSERT INTO nodes VALUES (5,'Method','create','','cfn/_language_extensions.py',0,1)")
    c.execute("INSERT INTO nodes VALUES (6,'Method','test_values','','cfn/_language_extensions.py',1,1)")
    c.commit()
    return c


def test_twin_pair_found_from_file_only():
    """value/values must surface as a twin pair given only the FILE (no edit_target).
    This is the v1 targeting bug fixed: v1 keyed on 'transform' and missed it."""
    hits = twin_pairs_in_files(_graph(), ["cfn/_language_extensions.py"])
    pairs = {frozenset((h["a"], h["b"])) for h in hits}
    assert frozenset(("value", "values")) in pairs, hits
    assert hits[0]["same_class"] is True


def test_dunders_and_generics_excluded():
    hits = twin_pairs_in_files(_graph(), ["cfn/_language_extensions.py"])
    flat = {h["a"] for h in hits} | {h["b"] for h in hits}
    assert "__init__" not in flat
    assert "test_values" not in flat   # is_test excluded


def test_anchor_member_ranked_first():
    """A pair with an issue-term member outranks one without."""
    c = _graph()
    # second class with an unrelated twin pair (load/loads), no anchor match
    c.execute("INSERT INTO nodes VALUES (10,'Class','Other','','cfn/_language_extensions.py',0,NULL)")
    c.execute("INSERT INTO nodes VALUES (11,'Method','load','','cfn/_language_extensions.py',0,10)")
    c.execute("INSERT INTO nodes VALUES (12,'Method','loads','','cfn/_language_extensions.py',0,10)")
    c.commit()
    hits = twin_pairs_in_files(c, ["cfn/_language_extensions.py"], anchor_terms={"values"})
    assert frozenset((hits[0]["a"], hits[0]["b"])) == frozenset(("value", "values")), hits


def test_cap_bounds_overfire():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
              "qualified_name TEXT, file_path TEXT, is_test INTEGER DEFAULT 0, parent_id INTEGER)")
    # 10 twin pairs in one file -> must cap at limit
    for i in range(10):
        c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", (100+i*2, 'Method', f'field{i}', '', 'big.py', 0, 1))
        c.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)", (101+i*2, 'Method', f'field{i}s', '', 'big.py', 0, 1))
    c.commit()
    hits = twin_pairs_in_files(c, ["big.py"], limit=4)
    assert len(hits) == 4


def test_quiet_when_no_twin():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
              "qualified_name TEXT, file_path TEXT, is_test INTEGER DEFAULT 0, parent_id INTEGER)")
    c.execute("INSERT INTO nodes VALUES (1,'Function','alpha','','solo.py',0,NULL)")
    c.execute("INSERT INTO nodes VALUES (2,'Function','beta','','solo.py',0,NULL)")
    c.commit()
    assert twin_pairs_in_files(c, ["solo.py"]) == []


def test_stem_twin_logic():
    assert _stem_twin("value", "values")        # singular/plural, any length
    assert _stem_twin("load", "load_many")      # 4+ char prefix family
    assert not _stem_twin("get", "get_all")     # 3-char stem too common -> excluded
    assert not _stem_twin("__init__", "__iter__")  # dunders excluded
    assert not _stem_twin("main", "mains")      # generic stoplist
    assert not _stem_twin("value", "value")     # identical is not a twin
    assert not _stem_twin("foo", "bar")

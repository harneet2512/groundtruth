"""Pins the structural (non-git) co-change signal — the completeness lever recovered
from graph facts, exercising the exact two loss shapes from the Live-Lite audit:
cfn-3764 (values()/value() same-class twin) and aiogram (get_value import-mirror)."""
import sqlite3

from groundtruth.pretask.structural_cochange import structural_cochange, _stem_twin


def _graph() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, is_test INTEGER DEFAULT 0, parent_id INTEGER)"
    )
    c.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_file TEXT)"
    )
    # cfn-3764 shape: class _ForEachCollection with sibling methods value() and values()
    c.execute("INSERT INTO nodes VALUES (1,'Class','_ForEachCollection','','cfn/_language_extensions.py',0,NULL)")
    c.execute("INSERT INTO nodes VALUES (2,'Method','values','','cfn/_language_extensions.py',0,1)")
    c.execute("INSERT INTO nodes VALUES (3,'Method','value','','cfn/_language_extensions.py',0,1)")
    c.execute("INSERT INTO nodes VALUES (4,'Method','_unrelated','','cfn/_language_extensions.py',0,1)")
    # a test method in the same class must be excluded
    c.execute("INSERT INTO nodes VALUES (5,'Method','test_values','','cfn/_language_extensions.py',1,1)")
    # aiogram shape: FSMContext.get_value (context.py) mirrored by SceneWizard.get_value (scene.py),
    # scene.py imports context.py
    c.execute("INSERT INTO nodes VALUES (10,'Class','FSMContext','','aiogram/fsm/context.py',0,NULL)")
    c.execute("INSERT INTO nodes VALUES (11,'Method','get_value','','aiogram/fsm/context.py',0,10)")
    c.execute("INSERT INTO nodes VALUES (12,'Class','SceneWizard','','aiogram/fsm/scene.py',0,NULL)")
    c.execute("INSERT INTO nodes VALUES (13,'Method','get_value','','aiogram/fsm/scene.py',0,12)")
    c.execute("INSERT INTO edges VALUES (1,13,11,'IMPORTS','aiogram/fsm/scene.py')")
    c.commit()
    return c


def test_same_class_name_twin_found():
    """values() must surface its sibling value() as a name_twin (the cfn-3764 loss)."""
    hits = structural_cochange(_graph(), "values", "cfn/_language_extensions.py")
    names = [(h["name"], h["reason"]) for h in hits]
    assert ("value", "name_twin") in names, names


def test_import_mirror_found():
    """get_value in context.py must surface the SceneWizard mirror in scene.py (aiogram loss)."""
    hits = structural_cochange(_graph(), "get_value", "aiogram/fsm/context.py")
    files = [h["file"] for h in hits if h["reason"] == "import_mirror"]
    assert "aiogram/fsm/scene.py" in files, hits


def test_excludes_self_and_tests():
    hits = structural_cochange(_graph(), "values", "cfn/_language_extensions.py")
    assert all(not (h["name"] == "values") for h in hits)
    assert all(h["name"] != "test_values" for h in hits)  # is_test excluded


def test_quiet_when_no_sibling():
    """A standalone function with no class/twin/mirror returns [] (correct-or-quiet)."""
    c = _graph()
    c.execute("INSERT INTO nodes VALUES (20,'Function','lonely','','solo.py',0,NULL)")
    c.commit()
    assert structural_cochange(c, "lonely", "solo.py") == []


def test_stem_twin_logic():
    assert _stem_twin("value", "values")        # singular/plural, any length
    assert _stem_twin("load", "load_many")      # 4+ char prefix family
    # deliberately NOT twins: 3-char stems (get/set/add) are too common -> would spam
    # every get() method; correct-or-quiet floors the prefix-family branch at 4 chars.
    assert not _stem_twin("get", "get_all")
    assert not _stem_twin("value", "value")     # identical is not a twin
    assert not _stem_twin("foo", "bar")

"""Stage-1 GENERAL correctness for contract DRIFT — edit-shape matrix, real binary, multi-language.

NOT benchmark-tied: synthetic fixtures, no gold files / no outcomes / no task IDs. Asserts drift is
CORRECT-OR-QUIET as a general property — it fires iff the behavioral contract MATERIALLY changed,
stays silent on a no-op, never leaks a test reference — across edit shapes (no-op, return-shape,
add-raise, add-guard, drop-raise, rename) and across languages (Python + Go). Gated on
GT_INDEX_BINARY (the real indexer populates the properties table drift reads).
"""
import os
import re
import subprocess

import pytest

from groundtruth.pretask.contract_map import build_drift, snapshot_contract

BIN = os.environ.get("GT_INDEX_BINARY")
pytestmark = pytest.mark.skipif(
    not BIN or not os.path.exists(BIN), reason="GT_INDEX_BINARY (real indexer) not set"
)

PY_BASE = '''def process(x):
    if x is None:
        raise ValueError("x required")
    data = compute(x)
    return list(data)


def compute(x):
    return [x]


def caller():
    return process(5)
'''

# (name, edit, expect_fires, expect_substr)
PY_SHAPES = [
    ("noop", PY_BASE.replace("data = compute(x)", "data = compute(x)  # noop"), False, None),
    ("return_shape", PY_BASE.replace("return list(data)", "return None"), True, "return shape"),
    ("add_new_raise",
     PY_BASE.replace("data = compute(x)", "if x < 0:\n        raise TypeError('neg')\n    data = compute(x)"),
     True, "new raise"),
    # add a guard whose exception is ALREADY raised -> NOT drift (single-guard-capture FP guard)
    ("add_guard_existing_exc",
     PY_BASE.replace("data = compute(x)", "if x == 0:\n        raise ValueError('zero')\n    data = compute(x)"),
     False, None),
    ("drop_raise",
     PY_BASE.replace('    if x is None:\n        raise ValueError("x required")\n', ""),
     True, "dropped raise"),
    ("rename", PY_BASE.replace("def process(x):", "def process_v2(x):"), True, "removed or renamed"),
]

GO_BASE = (
    "package mod\n\n"
    "func Process(x []int) []int {\n"
    "\tif x == nil {\n\t\tpanic(\"x required\")\n\t}\n"
    "\treturn Compute(x)\n}\n\n"
    "func Compute(x []int) []int {\n\treturn x\n}\n\n"
    "func Caller() []int {\n\treturn Process([]int{5})\n}\n"
)


def _drift_after(tmp_path, rel, base, edit, func):
    root, db, fp = str(tmp_path), str(tmp_path / "g.db"), tmp_path / rel
    fp.write_text(base, encoding="utf-8")
    subprocess.run([BIN, "-root", root, "-output", db], capture_output=True, text=True)
    pre = snapshot_contract(db, rel, [func])
    fp.write_text(edit, encoding="utf-8")
    subprocess.run([BIN, "-root", root, "-file", rel, "-output", db], capture_output=True, text=True)
    return build_drift(db, rel, [func], pre_snapshot=pre)


@pytest.mark.parametrize("name,edit,fires,substr", PY_SHAPES, ids=[s[0] for s in PY_SHAPES])
def test_python_edit_shapes(tmp_path, name, edit, fires, substr):
    d = _drift_after(tmp_path, "mod.py", PY_BASE, edit, "process")
    assert bool(d.strip()) == fires, f"{name}: correct-or-quiet violated; drift={d!r}"
    if substr:
        assert substr in d, f"{name}: expected '{substr}' in {d!r}"
    assert not re.search(r"test_[A-Za-z]|assert |/tests/", d or ""), f"{name}: test leakage in drift"


def test_go_noop_is_quiet(tmp_path):
    d = _drift_after(tmp_path, "mod.go", GO_BASE, GO_BASE.replace("return Compute(x)", "return Compute(x) // noop"), "Process")
    assert not d.strip(), f"go no-op must be quiet; drift={d!r}"


def test_go_return_shape_change_fires(tmp_path):
    d = _drift_after(tmp_path, "mod.go", GO_BASE, GO_BASE.replace("return Compute(x)", "return nil"), "Process")
    assert d.strip(), "go return-shape change must fire drift"
    assert not re.search(r"_test|/test", d or ""), "test leakage in go drift"

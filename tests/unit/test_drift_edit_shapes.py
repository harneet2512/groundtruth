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


# --- LANGUAGE-AGNOSTIC coverage: (rel, func, base, old->new material change) ---
# Drift reads the indexer's per-language `properties` table. These assert the SIGNAL coverage
# (fires on a real change) on the languages where the extractor populates contract fields, AND
# correct-or-quiet (no-op silent) everywhere. Rust is a KNOWN coverage gap, asserted explicitly
# below so a future indexer fix flips it.
_TS = ('export function process(x: number[]): number[] {\n  if (x == null) {\n    throw new Error("x required");\n  }\n  return compute(x);\n}\nfunction compute(x: number[]): number[] { return x; }\n', "return compute(x);", "return null;")
_JS = ('function process(x) {\n  if (x == null) {\n    throw new Error("x required");\n  }\n  return compute(x);\n}\nfunction compute(x) { return [x]; }\n', "return compute(x);", "return null;")
_JAVA = ('public class Mod {\n  public int[] process(int[] x) {\n    if (x == null) {\n      throw new IllegalArgumentException("x required");\n    }\n    return compute(x);\n  }\n  public int[] compute(int[] x) { return x; }\n}\n', "return compute(x);", "return null;")

@pytest.mark.parametrize("rel,base,old,new", [
    ("mod.ts", _TS[0], _TS[1], _TS[2]),
    ("mod.js", _JS[0], _JS[1], _JS[2]),
    ("Mod.java", _JAVA[0], _JAVA[1], _JAVA[2]),
], ids=["ts", "js", "java"])
def test_language_agnostic_fires_and_quiet(tmp_path, rel, base, old, new):
    noop = _drift_after(tmp_path, rel, base, base.replace(old, old + " // noop"), "process")
    assert not noop.strip(), f"{rel}: no-op must be quiet; drift={noop!r}"
    chg = _drift_after(tmp_path, rel, base, base.replace(old, new), "process")
    assert chg.strip(), f"{rel}: material change must fire drift"
    assert not re.search(r"test_[A-Za-z]|/tests/", chg or ""), f"{rel}: test leakage"


_RUST = 'pub fn process(x: Option<Vec<i32>>) -> Vec<i32> {\n  if x.is_none() {\n    panic!("x required");\n  }\n  compute(x.unwrap())\n}\nfn compute(x: Vec<i32>) -> Vec<i32> { x }\n'

def test_rust_drift_fires(tmp_path):
    # Rust gap CLOSED (parser.go: implicit-return tail expression + panic! macro guard +
    # expression_statement unwrap). Rust now extracts return_shape AND guards, so drift fires on a
    # return-value change and a guard drop, stays quiet on a no-op, with zero leakage. Capability-
    # gated: a gt-index built BEFORE this fix won't extract Rust contracts -> skip with a clear note.
    base = _RUST
    root, db = str(tmp_path), str(tmp_path / "g.db")
    (tmp_path / "mod.rs").write_text(base, encoding="utf-8")
    subprocess.run([BIN, "-root", root, "-output", db], capture_output=True, text=True)
    if not (snapshot_contract(db, "mod.rs", ["process"]).get("process") or {}).get("return_shape"):
        pytest.skip("gt-index predates the Rust contract-extraction fix — rebuild gt-index from source")
    noop = _drift_after(tmp_path, "mod.rs", base, base.replace("compute(x.unwrap())", "compute(x.unwrap()) // noop"), "process")
    assert not noop.strip(), f"rust no-op must be quiet; drift={noop!r}"
    ret = _drift_after(tmp_path, "mod.rs", base, base.replace("compute(x.unwrap())", "Vec::new()"), "process")
    assert ret.strip(), "rust return-value change must fire drift"
    grd = _drift_after(tmp_path, "mod.rs", base, base.replace('  if x.is_none() {\n    panic!("x required");\n  }\n', ""), "process")
    assert grd.strip(), "rust guard-drop must fire drift"
    assert not re.search(r"test_[A-Za-z]|/tests/", (ret + grd) or ""), "test leakage in rust drift"

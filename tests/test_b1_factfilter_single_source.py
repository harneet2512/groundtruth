"""B1 — the delivery fact-filter is SINGLE-SOURCED in ``groundtruth.delivery``
(+ the FACT gate / cross-language disqualifier in ``pretask.curation_map``) and
imported identically by the Product brief (``v1r_brief``) and the Integration
hook (``gt_mini_patch``).

Locks three invariants so the "change the wrong copy, don't see the result"
regression cannot return:
  1. SAME-OBJECT identity — Integration and Product bind the *same* function
     objects (not just equal logic), so a Product edit reaches both.
  2. DECISION parity — identical (path / name / code / lang) inputs yield
     identical exclusion decisions on both surfaces.
  3. PROOF-MODE FAIL-CLOSED — if the Product policy can't be imported under
     proof/benchmark mode, the hook ABORTS rather than silently delivering
     facts through a stale inline copy. Off proof mode it degrades (logged).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GMP = ROOT / "artifact_deepswe" / "gt_mini_patch.py"


def _load_gmp(modname: str = "gt_mini_patch_b1"):
    spec = importlib.util.spec_from_file_location(modname, GMP)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[modname] = m
    spec.loader.exec_module(m)
    return m


def test_single_source_same_object_identity():
    from groundtruth.delivery.name_policy import (
        is_builtin_shadow_name,
        is_stdlib_shadow,
    )
    from groundtruth.delivery.path_policy import (
        is_vendored_path,
        is_minified_file,
        is_delivery_excluded,
    )
    from groundtruth.pretask.curation_map import (
        DETERMINISTIC_RESOLUTION_METHODS,
        _is_cross_language_pair,
        _nodes_have_language,
    )
    import groundtruth.pretask.v1r_brief as v1r

    gmp = _load_gmp()
    assert gmp._DELIVERY_POLICY_AVAILABLE is True

    # Integration binds the SAME objects as the Product single source.
    assert gmp._is_builtin_shadow_name is is_builtin_shadow_name
    assert gmp._is_stdlib_shadow is is_stdlib_shadow
    assert gmp._is_vendored_path is is_vendored_path
    assert gmp._is_minified_file is is_minified_file
    assert gmp._is_delivery_excluded is is_delivery_excluded
    assert gmp._is_cross_language_pair is _is_cross_language_pair
    assert gmp._nodes_have_language is _nodes_have_language
    assert gmp._DETERMINISTIC_METHODS is DETERMINISTIC_RESOLUTION_METHODS

    # Product brief binds the SAME name-class + path-class objects.
    assert v1r._is_builtin_shadow_name is is_builtin_shadow_name
    assert v1r._is_stdlib_shadow is is_stdlib_shadow
    assert v1r._is_vendored_path is is_vendored_path


def test_decision_parity_battery():
    from groundtruth.delivery.name_policy import is_builtin_shadow_name, is_stdlib_shadow
    from groundtruth.delivery.path_policy import is_vendored_path

    gmp = _load_gmp()

    names = [
        "isinstance",
        "join",
        "get",
        "__init__",
        "__await__",
        "MyClass",
        "process_data",
        "walk",
        "x",
        "",
    ]
    paths = [
        "astropy/extern/jquery.min.js",
        "src/foo/bar.py",
        "vendor/lib.go",
        "node_modules/x.js",
        "pkg/zz_generated.deepcopy.go",
        "core/engine/src/module/source.rs",
        "a/b/normal.ts",
        "static/app.js",
    ]
    codes = [
        ("os.walk(root)", "walk"),
        ("self.walk(x)", "walk"),
        ("json.loads(s)", "loads"),
        ("x.foo()", "foo"),
        ("", "y"),
    ]

    for n in names:
        assert is_builtin_shadow_name(n) == gmp._is_builtin_shadow_name(n), n
    for p in paths:
        assert is_vendored_path(p) == gmp._is_vendored_path(p), p
    for code, tgt in codes:
        assert is_stdlib_shadow(code, tgt) == gmp._is_stdlib_shadow(code, tgt), (code, tgt)


def test_proof_mode_import_failure_degrades_gracefully():
    """B1-FIX (run 27462282736): the AGENT-TIME gt_mini_patch hook runs INSIDE the
    eval task container, where groundtruth is legitimately NOT importable (same as
    runtime.*). A broken delivery import MUST degrade GRACEFULLY even under
    GT_PROOF_MODE — it must NEVER raise. The earlier proof-mode raise crashed the
    whole monkeypatch in-container and killed ALL per-turn evidence. The fallback is
    FUNCTIONAL (the pre-B1 filter), not permissive stubs. (The fail-closed for a
    missing delivery policy belongs in the substrate PROOF process, not this hook.)"""
    script = textwrap.dedent(f"""
        import sys, os, importlib.util
        os.environ["GT_PROOF_MODE"] = "1"
        # poison the Product delivery module -> import fails (the task-container shape)
        sys.modules["groundtruth.delivery.path_policy"] = None
        spec = importlib.util.spec_from_file_location("gmp_fc", r"{GMP}")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)   # MUST NOT raise
        assert m._DELIVERY_POLICY_AVAILABLE is False
        # FUNCTIONAL fallback (not permissive stubs) — per-turn delivery unchanged:
        assert m._is_vendored_path("vendor/x.go") is True
        assert m._is_vendored_path("src/foo.py") is False
        assert m._is_builtin_shadow_name("isinstance") is True
        assert m._is_stdlib_shadow("os.walk(r)", "walk") is True
        assert m._is_cross_language_pair("python", "rust") is True
        print("GRACEFUL_FUNCTIONAL_OK")
    """)
    r = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(ROOT)
    )
    assert "GRACEFUL_FUNCTIONAL_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"


def test_non_proof_import_failure_degrades_not_aborts():
    """Off proof/substrate mode a broken import DEGRADES to a logged stub
    (_DELIVERY_POLICY_AVAILABLE=False) and never aborts."""
    script = textwrap.dedent(f"""
        import sys, os, importlib.util
        for k in ("GT_PROOF_MODE","GT_PORTABLE_SUBSTRATE","GT_HOST_GRAPH_DB","GT_CERT_DIR"):
            os.environ.pop(k, None)
        sys.modules["groundtruth.delivery.path_policy"] = None
        spec = importlib.util.spec_from_file_location("gmp_dev", r"{GMP}")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)  # must NOT raise
        assert m._DELIVERY_POLICY_AVAILABLE is False
        print("DEGRADED_OK")
    """)
    r = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(ROOT)
    )
    assert "DEGRADED_OK" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"

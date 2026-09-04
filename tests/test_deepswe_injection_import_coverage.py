"""F2+F5 (2026-06-13) — in-container injection import-coverage guard.

ROOT 2 of the 0/8 delivery-dark diagnosis: ``gt_mini_patch.py`` imports the REAL
Product fact-filter policy at module scope::

    from groundtruth.delivery.path_policy  import ...
    from groundtruth.delivery.name_policy  import ...
    from groundtruth.pretask.curation_map  import ...

but the adapter (``gt_agent.py``) historically injected ONLY ``groundtruth.runtime.*``
into the task container. So those three imports FAILED in-container, printed
``[GT_META] delivery_policy_import_fallback=true``, and ran a DIVERGENT inline copy
whose ``_VENDOR_DIR_MARKERS_FB`` omitted ``/static/`` + ``/assets/`` that the REAL
``path_policy._VENDOR_DIR_MARKERS`` carries — Product != agent-time.

The fix SHIPS THE PACKAGES (one source of truth) and adds a fail-closed coverage
guard so a future module-scope ``from groundtruth.* import`` can't silently
re-introduce the drift. These tests pin that guard, behavior-first:

  1. Every module-scope ``from groundtruth.* import`` in gt_mini_patch.py is in the
     injection allow-list (``_INJECTED_GT_MODULES``) — the guard does not raise.
  2. The specific delivery/pretask modules that caused the drift ARE shipped.
  3. The generated install steps actually create the ``delivery``/``pretask``
     package dirs + ``__init__.py`` and emit + decode each module file.
  4. The guard is REAL: injecting a synthetic uncovered import makes it RAISE
     (negative control — proves the assertion isn't vacuous).
  5. The shipped Product policy carries ``/static/`` + ``/assets/`` (the exact
     divergence the inline fallback had) — so importing it in-container restores
     the correct exclusion.

Deterministic: no Go toolchain, no network, no Docker, no task IDs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip('pier', reason='pier is not installed')

_ROOT = Path(__file__).resolve().parents[1]
_AGENT_PATH = _ROOT / "artifact_deepswe" / "gt_agent.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"

_load_count = 0


def _load(path: Path, name_prefix: str):
    global _load_count
    _load_count += 1
    name = f"{name_prefix}_{_load_count}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent_mod():
    return _load(_AGENT_PATH, "gt_agent_injcov")


# ---------------------------------------------------------------------------
# 1. The coverage guard passes on the REAL gt_mini_patch.py (no uncovered import)
# ---------------------------------------------------------------------------
def test_real_patch_imports_are_all_covered(agent_mod):
    """Every module-scope `from groundtruth.* import` in gt_mini_patch.py is in the
    injection allow-list. If this fails, the in-container import would fall back to
    the divergent inline copy (Product != agent-time)."""
    required = agent_mod.gt_mini_patch_required_modules()
    # Sanity: the parse actually found the imports we know are there (not a silent 0).
    assert "groundtruth.delivery.path_policy" in required
    assert "groundtruth.delivery.name_policy" in required
    assert "groundtruth.pretask.curation_map" in required

    uncovered = required - set(agent_mod._INJECTED_GT_MODULES)
    assert uncovered == set(), (
        "gt_mini_patch.py imports groundtruth modules NOT shipped by gt_agent.py "
        f"injection: {sorted(uncovered)} — these will hit the inline fallback "
        "in-container (the /static//assets/ drift). Add them to "
        "_PRODUCT_PACKAGE_MODULES."
    )
    # And the explicit assertion API does not raise.
    agent_mod._assert_gt_mini_patch_imports_covered()


# ---------------------------------------------------------------------------
# 2. The exact modules that caused the 0/8 drift are in the shipped allow-list
# ---------------------------------------------------------------------------
def test_delivery_and_pretask_modules_are_shipped(agent_mod):
    shipped = set(agent_mod._INJECTED_GT_MODULES)
    for dotted in (
        "groundtruth.delivery.path_policy",
        "groundtruth.delivery.name_policy",
        "groundtruth.pretask.curation_map",
    ):
        assert dotted in shipped, f"{dotted} must be injected, was not in {shipped}"
    # The module CONTENT was actually loaded from source (not None / skipped).
    assert agent_mod._PRODUCT_PACKAGE_FILES["delivery"]["path_policy.py"]
    assert agent_mod._PRODUCT_PACKAGE_FILES["delivery"]["name_policy.py"]
    assert agent_mod._PRODUCT_PACKAGE_FILES["pretask"]["curation_map.py"]
    # Back-compat alias still resolves to the runtime view.
    assert "context_policy.py" in agent_mod._PRODUCT_RUNTIME_FILES


# ---------------------------------------------------------------------------
# 3. The generated install steps create the package dirs + emit/decode the files
# ---------------------------------------------------------------------------
def test_inject_steps_create_packages_and_decode_modules(agent_mod):
    steps = agent_mod._inject_steps()
    runs = "\n".join(s.run for s in steps)

    # Package dirs + __init__.py created for delivery and pretask (like runtime).
    assert "mkdir -p /opt/gt/groundtruth/delivery" in runs
    assert "touch /opt/gt/groundtruth/delivery/__init__.py" in runs
    assert "mkdir -p /opt/gt/groundtruth/pretask" in runs
    assert "touch /opt/gt/groundtruth/pretask/__init__.py" in runs
    # runtime still shipped (no regression).
    assert "mkdir -p /opt/gt/groundtruth/runtime" in runs
    assert "touch /opt/gt/groundtruth/runtime/__init__.py" in runs

    # Each module is base64-decoded to its package path.
    for tail in (
        "> /opt/gt/groundtruth/delivery/path_policy.py",
        "> /opt/gt/groundtruth/delivery/name_policy.py",
        "> /opt/gt/groundtruth/pretask/curation_map.py",
        "> /opt/gt/groundtruth/runtime/context_policy.py",
    ):
        assert tail in runs, f"missing decode step ending in {tail!r}"


# ---------------------------------------------------------------------------
# 4. Negative control: an uncovered import makes the guard RAISE (not vacuous)
# ---------------------------------------------------------------------------
def test_guard_raises_on_uncovered_import(agent_mod):
    fake_src = (
        "from __future__ import annotations\n"
        "from groundtruth.analysis.not_shipped import frobnicate\n"
        "from groundtruth.delivery.path_policy import is_vendored_path\n"
    )
    required = agent_mod.gt_mini_patch_required_modules(fake_src)
    assert "groundtruth.analysis.not_shipped" in required
    with pytest.raises(RuntimeError, match="NOT covered by the injection allow-list"):
        agent_mod._assert_gt_mini_patch_imports_covered(fake_src)


def test_guard_ignores_bare_import_groundtruth(agent_mod):
    """`import groundtruth` (no submodule) is not a from-import the allow-list must
    cover; the regex only flags `from groundtruth.<pkg> import`."""
    src = "import groundtruth\nimport os\nfrom groundtruth.runtime.ledger import Ledger\n"
    required = agent_mod.gt_mini_patch_required_modules(src)
    assert required == {"groundtruth.runtime.ledger"}


# ---------------------------------------------------------------------------
# 5. The shipped Product policy carries the markers the inline fallback dropped
# ---------------------------------------------------------------------------
def test_shipped_path_policy_carries_static_and_assets(agent_mod):
    """The exact divergence: the inline fallback `_VENDOR_DIR_MARKERS_FB` omitted
    `/static/` + `/assets/`; the REAL path_policy carries them. Shipping the real
    module is what restores agent-time == Product."""
    src = agent_mod._PRODUCT_PACKAGE_FILES["delivery"]["path_policy.py"]
    assert src is not None
    assert '"/static/"' in src
    assert '"/assets/"' in src

    # Load the shipped source in isolation and assert it actually excludes those
    # paths (behavior, not just substring presence).
    ns: dict = {}
    exec(compile(src, "path_policy_shipped", "exec"), ns)  # noqa: S102 -- Product source
    is_vendored_path = ns["is_vendored_path"]
    assert is_vendored_path("web/static/app.js") is True
    assert is_vendored_path("frontend/assets/logo.png") is True
    assert is_vendored_path("src/core/handler.py") is False

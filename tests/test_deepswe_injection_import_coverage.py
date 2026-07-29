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

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

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
    # W2 nested adapters package (runtime/adapters): dir + __init__.py created.
    assert "mkdir -p /opt/gt/groundtruth/runtime/adapters" in runs
    assert "touch /opt/gt/groundtruth/runtime/adapters/__init__.py" in runs

    # Each module is base64-decoded to its package path.
    for tail in (
        "> /opt/gt/groundtruth/delivery/path_policy.py",
        "> /opt/gt/groundtruth/delivery/name_policy.py",
        "> /opt/gt/groundtruth/pretask/curation_map.py",
        "> /opt/gt/groundtruth/runtime/context_policy.py",
        # W2: the nested adapters module decodes to its package path.
        "> /opt/gt/groundtruth/runtime/adapters/miniswe.py",
    ):
        assert tail in runs, f"missing decode step ending in {tail!r}"

    # W2 FLATTEN: the nested subdir's temp b64 name is slash-free (a `/` in the
    # temp name would target a nonexistent dir under /opt/gt).
    assert "runtime_adapters__miniswe.b64" in runs
    assert "/opt/gt/runtime/adapters__" not in runs


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


def test_guard_resolves_package_form_import_to_exact_submodule(agent_mod):
    fake_src = "from groundtruth.runtime import definitely_not_shipped\n"
    required = agent_mod.gt_mini_patch_required_modules(fake_src)
    assert required == {"groundtruth.runtime.definitely_not_shipped"}
    with pytest.raises(RuntimeError, match="definitely_not_shipped"):
        agent_mod._assert_gt_mini_patch_imports_covered(fake_src)


def test_package_form_rl_profile_import_is_really_shipped(agent_mod):
    required = agent_mod.gt_mini_patch_required_modules()
    assert "groundtruth.runtime.rl_profile" in required
    assert "groundtruth.runtime.rl_profile" in agent_mod._INJECTED_GT_MODULES


def test_guard_ignores_bare_import_groundtruth(agent_mod):
    """`import groundtruth` (no submodule) is not a from-import the allow-list must
    cover; the AST guard only evaluates `from groundtruth.<pkg> import`."""
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


# ---------------------------------------------------------------------------
# 6. Import-closure: the shipped set must be self-contained. This is the real
#    defect class the naive coverage test (#1) misses — it only checks
#    gt_mini_patch.py's own imports. A shipped module can itself import another
#    groundtruth.* module at MODULE SCOPE (e.g. covering_runner ->
#    groundtruth.runtime.test_runner -> groundtruth.runtime.repo_adapters). If
#    that transitive dep is NOT shipped, `import groundtruth.runtime.covering_runner`
#    raises ModuleNotFoundError in-container -> the B1 seam's try/except swallows it
#    -> B1 goes dark even though the naive test is green.
#
#    Only MODULE-SCOPE imports (top-level ast nodes) are import-time and can crash
#    container startup. Function-scope `from groundtruth... import` (e.g. `telemetry`
#    behind `if log_dir is not None`) is lazy and does NOT crash the import, so it is
#    correctly excluded here (and deliberately not shipped). Relative imports
#    (`from . import context_policy`, `from .patterns import X`) are resolved to their
#    absolute dotted name so a future relative hole is also caught.
# ---------------------------------------------------------------------------
def _module_scope_gt_deps(dotted: str, src: str) -> set[str]:
    """The set of ``groundtruth.*`` modules imported at MODULE SCOPE by the source
    of ``dotted`` (its fully-qualified name, used to resolve relative imports).
    Excludes function/class-nested (lazy) imports by walking only ``tree.body``."""
    package = dotted.rsplit(".", 1)[0]  # e.g. groundtruth.runtime
    pkg_parts = package.split(".")
    deps: set[str] = set()
    for node in ast.parse(src, filename=dotted).body:  # top-level only
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "groundtruth":
                    deps.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.split(".")[0] == "groundtruth":
                    deps.add(node.module)
            else:
                # relative: anchor = package with (level-1) trailing parts removed
                anchor = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)])
                if not anchor:
                    continue
                if node.module:  # from .sub import name -> anchor.sub is the module
                    deps.add(f"{anchor}.{node.module}")
                else:            # from . import mod1, mod2 -> anchor.mod1, anchor.mod2
                    for alias in node.names:
                        deps.add(f"{anchor}.{alias.name}")
    return deps


def test_shipped_module_set_is_import_closed(agent_mod):
    """Every MODULE-SCOPE groundtruth.* import inside a shipped module is itself
    shipped. Closes the transitive-dep hole (covering_runner needs test_runner;
    test_runner + patch_auditor need repo_adapters)."""
    shipped = set(agent_mod._INJECTED_GT_MODULES)
    files = agent_mod._PRODUCT_PACKAGE_FILES
    holes: dict[str, list[str]] = {}
    for subdir, name_to_src in files.items():
        for name, src in name_to_src.items():
            if src is None:  # absent source is skipped at injection time too
                continue
            # P4 (bounce 2026-07-10): a NESTED subdir ("runtime/adapters") must
            # build a DOTTED module name — `/` would corrupt both the dep-anchor
            # (relative-import resolution) and the shipped-set membership check.
            # Uses the adapter's OWN identity function so the test can never disagree
            # with the allow-list it is checking against — in particular the
            # package-ROOT key ("" -> groundtruth.confidence, not groundtruth..confidence).
            dotted = agent_mod._dotted_module(subdir, name)
            uncovered = _module_scope_gt_deps(dotted, src) - shipped
            if uncovered:
                holes[dotted] = sorted(uncovered)
    assert holes == {}, (
        "shipped modules import groundtruth.* modules at MODULE SCOPE that are NOT "
        f"shipped (import-closure hole -> in-container ModuleNotFoundError): {holes}. "
        "Add each to _PRODUCT_PACKAGE_MODULES in gt_agent.py."
    )
    # W2: the nested adapters module is in the shipped set under its DOTTED name,
    # and its module-scope deps (gateway, evidence_envelope) are shipped too.
    assert "groundtruth.runtime.adapters.miniswe" in shipped
    for dep in ("groundtruth.runtime.gateway",
                "groundtruth.runtime.evidence_envelope",
                "groundtruth.runtime.episode_state"):
        assert dep in shipped, f"{dep} must ship for the W2 gateway seam"


def test_b1_engines_and_transitive_deps_are_shipped(agent_mod):
    """The four B1 verification-execution engines gt_mini_patch.py imports AND their
    module-scope transitive deps are all in the shipped allow-list (regression pin
    for the 0/8 B1-dark defect)."""
    shipped = set(agent_mod._INJECTED_GT_MODULES)
    for dotted in (
        "groundtruth.runtime.covering_runner",
        "groundtruth.runtime.native_render",
        "groundtruth.runtime.submit_gate",
        "groundtruth.runtime.patch_auditor",
        "groundtruth.runtime.test_runner",   # transitive: covering_runner, test_runner
        "groundtruth.runtime.repo_adapters", # transitive: patch_auditor, test_runner
    ):
        assert dotted in shipped, f"{dotted} must be shipped for B1 to load in-container"


def test_lane_attestation_factory_and_schema_are_shipped(agent_mod):
    """Covering staging must not import-fail/quiet inside the live container."""
    shipped = set(agent_mod._INJECTED_GT_MODULES)
    assert "groundtruth.runtime.lane_attestation" in shipped
    assert "groundtruth.runtime.producer_attestation" in shipped

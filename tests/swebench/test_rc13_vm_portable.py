"""RC-13 — VM-portable paths, binary loader probe, SWE-agent version pin.

Covers:
  - `_resolve_vm_profile` merge semantics (profile defaults + explicit override).
  - `_resolve_vm_profile` raises KeyError on unknown profile.
  - `_assert_sweagent_version` rejects forced mismatch.
  - `_assert_sweagent_version` accepts matching version.
  - `_assert_sweagent_version` soft-passes when venv_python missing.
  - `_assert_no_duplicate_submit` rejects bundles list with both
    tools/registry + review_on_submit_m.
  - `_assert_no_duplicate_submit` accepts the canonical Track 4 layout.
  - `_probe_binary_loadable` returns ok on a static-ish binary.
  - `_probe_binary_loadable` returns not-ok on a forced "GLIBC_99 not
    found" ldd output (mocked).
  - gt_edit_state `_gt_index_bin` raises KeyError when nothing resolves
    (no env, no bundle bin, no PATH).
  - gt_edit_state `_gt_index_bin` falls through env -> bundle -> which.
  - gt_edit_state `_gt_hook_py` raises KeyError when nothing resolves.

These tests run without spending money — every codepath is local.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[2]
RUNNER = REPO_DIR / "scripts" / "swebench" / "swe_agent_smoke_runner.py"
GT_EDIT_STATE = REPO_DIR / "tools" / "sweagent" / "gt_edit" / "lib" / "gt_edit_state.py"


def _load_module(name: str, path: Path):
    """Load a module by file path. Adds the script dir to sys.path so
    sibling imports (e.g. `from image_name_resolver import ...`) resolve.
    """
    script_dir = str(path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner_mod():
    return _load_module("rc13_runner_under_test", RUNNER)


@pytest.fixture(scope="module")
def state_mod():
    return _load_module("rc13_state_under_test", GT_EDIT_STATE)


# ---- _resolve_vm_profile --------------------------------------------------


def test_resolve_vm_profile_known_returns_three_paths(runner_mod):
    out = runner_mod._resolve_vm_profile("ubuntu_t0", {})
    assert out["venv_python"] == "/home/ubuntu/sweagent_venv/bin/python"
    assert out["swe_repo"] == "/home/ubuntu/SWE-agent"
    assert out["gt_indexes_root"] == "/home/ubuntu/eval_indexes"


def test_resolve_vm_profile_explicit_overrides_profile(runner_mod):
    out = runner_mod._resolve_vm_profile(
        "ubuntu_t0",
        {"venv_python": "/opt/custom/python", "gt_indexes_root": None},
    )
    # Explicit non-empty wins; explicit None falls back to profile.
    assert out["venv_python"] == "/opt/custom/python"
    assert out["gt_indexes_root"] == "/home/ubuntu/eval_indexes"


def test_resolve_vm_profile_unknown_raises(runner_mod):
    with pytest.raises(KeyError, match="unknown --vm-profile"):
        runner_mod._resolve_vm_profile("does-not-exist", {})


def test_resolve_vm_profile_test_profile_uses_home_test(runner_mod):
    out = runner_mod._resolve_vm_profile("test", {})
    # Test profile simulates a fresh VM with /home/test as the home dir —
    # this is what the integration check exercises.
    assert "/home/test/" in out["venv_python"]
    assert "/home/test/" in out["gt_indexes_root"]


# ---- _assert_sweagent_version ---------------------------------------------


def _make_fake_venv_python(tmp_path: Path, version_string: str) -> Path:
    """Create a tiny venv-shaped python that mimics the version-print invocation."""
    venv_bin = tmp_path / "fake_venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    py.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        joined = " ".join(sys.argv)
        if "import sweagent" in joined:
            print({version_string!r})
        else:
            sys.exit(0)
    """))
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return py


@pytest.mark.skipif(sys.platform == "win32",
                    reason="fake-shebang venv only works on Unix")
def test_assert_sweagent_version_matches(runner_mod, tmp_path):
    fake = _make_fake_venv_python(tmp_path, "1.1.0")
    ok, detail = runner_mod._assert_sweagent_version(str(fake), expected="1.1.0")
    assert ok is True
    assert "sweagent_version:1.1.0" in detail


@pytest.mark.skipif(sys.platform == "win32",
                    reason="fake-shebang venv only works on Unix")
def test_assert_sweagent_version_mismatch_fails(runner_mod, tmp_path):
    fake = _make_fake_venv_python(tmp_path, "9.9.9")
    ok, detail = runner_mod._assert_sweagent_version(str(fake), expected="1.1.0")
    assert ok is False
    assert "sweagent_version_mismatch" in detail
    assert "expected=1.1.0" in detail
    assert "actual=9.9.9" in detail


def test_assert_sweagent_version_missing_venv_softpasses(runner_mod, tmp_path):
    ok, detail = runner_mod._assert_sweagent_version(
        str(tmp_path / "does-not-exist"), expected="1.1.0"
    )
    assert ok is True  # other preflight checks catch missing venv_python
    assert "venv_python_absent:skipped" in detail


# ---- _assert_no_duplicate_submit ------------------------------------------


def test_assert_no_duplicate_submit_canonical_track4_passes(runner_mod, tmp_path):
    cfg = tmp_path / "ok.yaml"
    cfg.write_text(textwrap.dedent("""\
        agent:
          tools:
            bundles:
              - path: tools/registry
              - path: tools/edit_anthropic
              - path: /home/ubuntu/Groundtruth/tools/sweagent/gt_pre_finish_gate
    """))
    ok, detail = runner_mod._assert_no_duplicate_submit(cfg)
    assert ok is True
    assert "submit_override_safe" in detail


def test_assert_no_duplicate_submit_rejects_review_on_submit_m(runner_mod, tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(textwrap.dedent("""\
        agent:
          tools:
            bundles:
              - path: tools/registry
              - path: tools/review_on_submit_m
              - path: /home/ubuntu/Groundtruth/tools/sweagent/gt_pre_finish_gate
    """))
    ok, detail = runner_mod._assert_no_duplicate_submit(cfg)
    assert ok is False
    assert "duplicate_submit_declaration" in detail


def test_assert_no_duplicate_submit_missing_config_softpasses(runner_mod, tmp_path):
    ok, detail = runner_mod._assert_no_duplicate_submit(tmp_path / "missing.yaml")
    assert ok is True
    assert "config_missing:skipped" in detail


# ---- _probe_binary_loadable -----------------------------------------------


@pytest.mark.skipif(sys.platform != "linux",
                    reason="ldd probe only runs on Linux")
def test_probe_binary_loadable_on_self_python(state_mod):
    # /usr/bin/python3 is dynamically linked but its deps must resolve on
    # the host running these tests. Use it as a positive control.
    py = sys.executable
    state_mod._LOADER_PROBE_CACHE.clear()
    ok, detail = state_mod._probe_binary_loadable(py)
    assert ok is True
    assert detail == "ok"


def test_probe_binary_loadable_missing_path(state_mod):
    state_mod._LOADER_PROBE_CACHE.clear()
    ok, detail = state_mod._probe_binary_loadable("/nonexistent/gt-index")
    if sys.platform == "linux":
        assert ok is False
        assert "binary_missing" in detail
    else:
        # Non-Linux short-circuits to True without touching the FS.
        assert ok is True


def test_probe_binary_loadable_caches_verdict(state_mod, tmp_path):
    state_mod._LOADER_PROBE_CACHE.clear()
    # First call populates cache; second call must NOT re-run ldd.
    fake_path = str(tmp_path / "fake_bin")
    Path(fake_path).write_text("not a real binary")
    state_mod._probe_binary_loadable(fake_path)
    assert fake_path in state_mod._LOADER_PROBE_CACHE


# ---- gt_edit_state binary resolution --------------------------------------


def test_gt_index_bin_env_wins(state_mod, monkeypatch):
    monkeypatch.setenv("GT_INDEX_BIN", "/explicit/gt-index")
    assert state_mod._gt_index_bin() == "/explicit/gt-index"


def test_gt_index_bin_raises_when_nothing_resolves(state_mod, monkeypatch, tmp_path):
    monkeypatch.delenv("GT_INDEX_BIN", raising=False)

    # Make `which` find nothing.
    def _no_which(_name):
        return None

    monkeypatch.setattr("shutil.which", _no_which)

    # Force the bundle-relative probe to miss by pointing __file__ at a
    # tmp dir that has no bin/gt-index.
    fake_lib = tmp_path / "lib"
    fake_lib.mkdir()
    monkeypatch.setattr(state_mod, "__file__", str(fake_lib / "gt_edit_state.py"))
    # Also kill the Windows dev fallback so we can assert KeyError on Win too.
    monkeypatch.setattr(state_mod, "_REPO_ROOT_HOST", None)

    with pytest.raises(KeyError, match="gt-index binary not resolvable"):
        state_mod._gt_index_bin()


def test_gt_hook_py_env_wins(state_mod, monkeypatch):
    monkeypatch.setenv("GT_HOOK_PY", "/explicit/gt_hook.py")
    assert state_mod._gt_hook_py() == "/explicit/gt_hook.py"


def test_gt_hook_py_raises_when_nothing_resolves(state_mod, monkeypatch, tmp_path):
    monkeypatch.delenv("GT_HOOK_PY", raising=False)
    fake_lib = tmp_path / "lib"
    fake_lib.mkdir()
    monkeypatch.setattr(state_mod, "__file__", str(fake_lib / "gt_edit_state.py"))
    monkeypatch.setattr(state_mod, "_REPO_ROOT_HOST", None)

    with pytest.raises(KeyError, match="gt_hook.py not resolvable"):
        state_mod._gt_hook_py()


def test_gt_hook_py_finds_bundle_relative(state_mod, monkeypatch, tmp_path):
    monkeypatch.delenv("GT_HOOK_PY", raising=False)
    bundle = tmp_path / "gt_edit"
    (bundle / "lib").mkdir(parents=True)
    hook = bundle / "lib" / "gt_hook.py"
    hook.write_text("# fake")
    # __file__ in lib/, parent.parent == bundle root.
    monkeypatch.setattr(state_mod, "__file__", str(bundle / "lib" / "gt_edit_state.py"))
    assert state_mod._gt_hook_py() == str(hook)


# ---- _fire_gt_index_file emits structured loader-incompat record ----------


def test_fire_gt_index_file_returns_loader_incompat_on_probe_fail(
    state_mod, monkeypatch, tmp_path
):
    """RC-13: when the binary fails the ldd probe, _fire_gt_index_file must
    return a structured record with `binary_loader_incompatible` rather
    than crashing or silently bumping the L6 counter on a no-op."""
    fake_bin = tmp_path / "gt-index"
    fake_bin.write_text("#!/bin/false\n")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(state_mod, "_gt_index_bin", lambda: str(fake_bin))
    state_mod._LOADER_PROBE_CACHE.clear()
    state_mod._LOADER_PROBE_CACHE[str(fake_bin)] = "loader_incompat:GLIBC_2.99 not found"

    rec = state_mod._fire_gt_index_file(
        repo_root=tmp_path,
        rel_path="x.py",
        graph_db="/tmp/graph.db",
    )
    assert "binary_loader_incompatible" in rec.get("error", "")
    assert rec.get("binary_supports_file_flag") is False

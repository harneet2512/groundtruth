"""_TEST_RUNNER_RE wrapper-prefix gaps — red->green tests (G6 Finding 2 + LIPI).

Two bounded wrapper-recognition gaps in artifact_deepswe/gt_mini_patch.py:

  GAP 1 (G6 Finding 2, python): `python manage.py test myapp` — Django's
     prefixed form — was NOT recognized. The regex caught bare
     `manage.py test` and `python -m pytest`, but `python ` was not a wrapper
     prefix, so the whole governor chain (no_test_evidence /
     failure_persisted) was blind to the single most common Django test
     invocation. Fix: the wrapper group accepts `python[\\d.]*\\s+` ONLY when
     the next token is a .py script (lookahead) — the runner-shape
     alternatives (`manage.py test`, `runtests.py`) still decide, so an
     arbitrary `python script.py` stays OUT of the governor (a scratch
     script's silence/failure must never count as test evidence).

  GAP 2 (LIPI minor, kill-after form): `timeout -k 5 60 cargo test`
     interleaves a flag-with-value and the duration; the old wrapper
     (`timeout\\s+(?:-\\S+\\s+)*\\d+\\S*\\s+` — flags THEN one duration) could
     not consume `-k 5 60`. Fix: the timeout wrapper accepts any run of
     flag/duration tokens (`(?:-\\S+\\s+|\\d+\\S*\\s+)+`).

All deterministic: pure regex + module-level governor state, no network,
no task IDs, no gold labels.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"

_load_count = 0


def _load(path: Path, name_prefix: str):
    """Fresh module instance per call (module-level state isolated per test)."""
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
def patch_mod(monkeypatch):
    for k in list(os.environ):
        if k.startswith("GT_"):
            monkeypatch.delenv(k, raising=False)
    return _load(_PATCH_PATH, "gt_mini_patch_runnerwrap")


# Blind output: a SIGKILLed run — no pass marker, no fail marker, no env
# failure, no compile error (the boa "Killed" shape, Django-flavored).
_BLIND = "Creating test database for alias 'default'...\nKilled\n"


# ===========================================================================
# GAP 1 — python-wrapped .py test runners ARE recognized
# ===========================================================================
def test_python_manage_test_is_recognized(patch_mod):
    assert patch_mod._TEST_RUNNER_RE.search("python manage.py test myapp") is not None, (
        "Django's prefixed form `python manage.py test` must be a recognized test-runner invocation"
    )


def test_python3_runtests_is_recognized(patch_mod):
    assert patch_mod._TEST_RUNNER_RE.search("python3 runtests.py") is not None
    assert patch_mod._TEST_RUNNER_RE.search("python3.11 ./manage.py test app.tests") is not None


def test_wrapped_python_manage_test_is_recognized(patch_mod):
    """Wrappers compose: `timeout N python manage.py test`."""
    assert patch_mod._TEST_RUNNER_RE.search("timeout 120 python manage.py test myapp") is not None


def test_plain_python_script_is_NOT_recognized(patch_mod):
    """An arbitrary `python script.py` (even with a trailing `test` arg) is
    NOT a test-runner shape — a scratch script must stay out of the governor."""
    assert patch_mod._TEST_RUNNER_RE.search("python script.py") is None
    assert patch_mod._TEST_RUNNER_RE.search("python script.py test") is None
    assert patch_mod._TEST_RUNNER_RE.search("python repro.py --verbose") is None


def test_python_manage_blind_runs_fire_governor(patch_mod):
    """Red->green behavior: 2 blind `python manage.py test` runs + 1 source
    edit -> no_test_evidence fires on the 2nd (was silent pre-fix)."""
    patch_mod._source_edit_count = 1
    out1 = patch_mod._l5_no_test_evidence_nudge("python manage.py test myapp", _BLIND)
    assert out1 == ""
    out2 = patch_mod._l5_no_test_evidence_nudge("python manage.py test myapp", _BLIND)
    assert 'reason="no_test_evidence"' in out2, (
        "python-wrapped manage.py test blind x2 must fire no_test_evidence"
    )


def test_plain_python_script_blind_runs_never_fire_governor(patch_mod):
    patch_mod._source_edit_count = 1
    for _ in range(4):
        out = patch_mod._l5_no_test_evidence_nudge("python script.py test", _BLIND)
    assert out == "", "a non-runner python script must never enter the governor"


# ===========================================================================
# GAP 2 — timeout kill-after form
# ===========================================================================
def test_timeout_kill_after_cargo_test_is_recognized(patch_mod):
    assert patch_mod._TEST_RUNNER_RE.search("timeout -k 5 60 cargo test") is not None, (
        "the kill-after form `timeout -k 5 60 cargo test` must not escape the wrapper"
    )
    assert patch_mod._TEST_RUNNER_RE.search("timeout --kill-after=5s 1m go test ./...") is not None


def test_existing_wrapper_forms_still_recognized(patch_mod):
    """No regression on the previously-working wrapper shapes."""
    r = patch_mod._TEST_RUNNER_RE
    assert r.search("timeout 60 cargo test") is not None
    assert r.search("time pytest tests/") is not None
    assert r.search("env RUST_BACKTRACE=1 cargo test") is not None
    assert r.search("python -m pytest tests/") is not None
    assert r.search("manage.py test myapp") is not None
    assert r.search("npm test") is not None
    # and plain non-runner commands stay out
    assert r.search("timeout 60 python build.py") is None
    assert r.search("cargo build") is None

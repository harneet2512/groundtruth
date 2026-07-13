"""SS-4 (2026-07-13) — HONEST js/ts LSP secondary-leg classification (flag
``GT_SS_ELIGIBILITY``).

REPRODUCED from the recorded run 29236533134 proof logs:
  * python-babel__babel-1179 (a Python repo with 1 stray JS file): the js leg's
    ``typescript-language-server`` fails ``initialize`` with -32603 "Could not find a
    valid TypeScript installation. Please ensure that the 'typescript' dependency is
    installed in the workspace or that a valid `tsserver.path` is specified." → the leg
    resolved 0 edges and the CANONICAL cert correctly fell back to python (LSP_ACTIVE_VALID).
  * jupyterlab__jupyter-ai-1294 (a real TS repo): the "No Project" errors are on
    ``workspace/symbol`` only; the leg STILL reached project_ready=True and resolved 5
    edges (LSP_ACTIVE_VALID) — NOT a failure, just log noise. Left alone (honest already).

FIX (safe, never over-skips): classify the ALREADY-OBSERVED no-workspace-typescript
``initialize`` failure as an explicit install/substrate gap so the cert says WHY
(``install_missing_reason``), rather than a generic warm-probe failure. This does NOT
pre-skip a leg a bundled TypeScript could serve — it only relabels a leg that already
failed to initialize. DEFAULT-OFF byte-identical (flag off -> today's generic
``initialize: <msg>`` detail).
"""

from __future__ import annotations

import importlib

import pytest

resolve = importlib.import_module("groundtruth.resolve")


_BABEL_INIT_ERR = (
    "Request initialize failed with message: Could not find a valid TypeScript "
    'installation. Please ensure that the "typescript" dependency is installed in the '
    "workspace or that a valid `tsserver.path` is specified. Exiting."
)
_NO_PROJECT_ERR = (
    "<syntax> TypeScript Server Error (4.9.5)\nNo Project.\nError: No Project."
)
_GENERIC_INIT_ERR = "gopls: no package metadata for file:///x.go"


# ── the pure classifier bites the exact babel signature, and ONLY it ────────────
def test_classifier_matches_no_ts_install():
    # MUTATION: narrow/break the regex -> this RED-flips.
    assert resolve._is_no_typescript_install_error(_BABEL_INIT_ERR) is True


def test_classifier_ignores_no_project_error():
    # "No Project" is a workspace/symbol error on a WORKING leg (jupyter-ai), NOT an
    # install gap — must NOT be classified as a missing-TS skip.
    assert resolve._is_no_typescript_install_error(_NO_PROJECT_ERR) is False


def test_classifier_ignores_generic_init_error():
    # MUTATION: match-all (e.g. `return True`) -> this RED-flips.
    assert resolve._is_no_typescript_install_error(_GENERIC_INIT_ERR) is False
    assert resolve._is_no_typescript_install_error("") is False


# ── the stats-enrichment helper is honest ON and byte-identical OFF ─────────────
def test_enrich_records_honest_reason_when_flag_on(monkeypatch):
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    stats = {"failure_detail": f"initialize: {_BABEL_INIT_ERR}", "install_missing_reason": ""}
    resolve._classify_ts_install_failure(stats, _BABEL_INIT_ERR, "javascript")
    assert stats["install_missing_reason"]                      # a specific WHY is recorded
    assert "typescript" in stats["install_missing_reason"].lower()
    assert stats["failure_detail"].startswith("skip:")          # honest, not generic


def test_enrich_byte_identical_when_flag_off(monkeypatch):
    # MUTATION: drop the flag gate -> this RED-flips (reason set with flag off).
    monkeypatch.delenv("GT_SS_ELIGIBILITY", raising=False)
    orig = f"initialize: {_BABEL_INIT_ERR}"
    stats = {"failure_detail": orig, "install_missing_reason": ""}
    resolve._classify_ts_install_failure(stats, _BABEL_INIT_ERR, "javascript")
    assert stats["failure_detail"] == orig                      # unchanged
    assert stats["install_missing_reason"] == ""                # unchanged


def test_enrich_noop_on_non_ts_error_even_when_flag_on(monkeypatch):
    monkeypatch.setenv("GT_SS_ELIGIBILITY", "1")
    orig = f"initialize: {_GENERIC_INIT_ERR}"
    stats = {"failure_detail": orig, "install_missing_reason": ""}
    resolve._classify_ts_install_failure(stats, _GENERIC_INIT_ERR, "go")
    assert stats["failure_detail"] == orig                      # a real gopls failure is untouched
    assert stats["install_missing_reason"] == ""

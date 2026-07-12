r"""ITEM 1 (2026-07-12) — CompletionCertificate MODEL-FACING delivery at D7 (the submit turn).

Before this wave the full CompletionCertificate was TELEMETRY-ONLY (``_gt_completion_cert_record``,
0 model-visible bytes) even though D7 (pre-submit) is the dominant-failure decision point where the
ONE proven-consumed pattern (``no_test_evidence``: SHORT · ACTIVE · at the decision point) lives.

``GT_CERT_DELIVERY`` (default OFF, byte-identical) turns a NOT-CLEAN certificate into a native
PRE-COMMIT HOOK failure block (``native_render.render_completion_cert_native`` — one
``<hook>....Failed`` line per failing head, world-fact not instruction, ZERO ``<gt-*>``), delivered
INTEGRATED into the submit gate's EXISTING refusal appender (never a second dose).

PINNED HERE:
  1. RED-FIRST — a failing-cert submit turn under the flag surfaces the native block in the
     observation. Pre-wire (blank renderer mutation) it reverts to the single-line refusal.
  2. CLEAN cert -> ZERO bytes (correct-or-quiet; no praise line); the cert never turns allow->block.
  3. BYTE-IDENTICAL OFF — flag unset -> the observation is EXACTLY ``render_submit_rejection`` (today).
  4. LEAK-0 — the covering head reports its VERDICT, never a test name; a test identity riding the
     hygiene world-fact detail is scrubbed. Neuter the scrub -> the leak pin reddens.

Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime.native_render import (  # noqa: E402
    contains_gt_tag,
    contains_test_identity,
    render_completion_cert_native,
    render_submit_rejection,
)


class Submitted(Exception):
    """Name-matches mini-swe-agent's completion signal (``_is_submitted_exc`` keys on the name)."""


@pytest.fixture
def submit_env(monkeypatch, tmp_path):
    """Drive ``_gt_gate_submit_exception`` deterministically: verify-execute on, not baseline,
    a stub repo root, no executor, and an isolated dedup ledger / bounce count."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_gt_submit_covering", lambda root: (None, []))
    monkeypatch.setattr(g, "_build_env_executor", lambda: None)
    monkeypatch.setattr(g, "_gt_submit_bounce_count", 0, raising=False)
    g._oracle_delivered_hashes.clear()
    yield
    g._oracle_delivered_hashes.clear()


def _block_hygiene(root):
    return {"blocking": True, "reason": "hygiene_test", "detail": "vendored file changed"}


def _clean_hygiene(root):
    return None


def _fail_covering(names, files, *, root=None):
    def _cov(_r):
        return ({"verdict": "fail", "failing_test_names": names,
                 "stdout_tail": "E   assert produced != required"}, list(files))
    return _cov


# =========================================================================== #
# renderer-level pins (the native pre-commit grammar + leak firewall)
# =========================================================================== #
def test_renderer_covering_head_reports_verdict_never_names():
    out = render_completion_cert_native(["selected_test_status"])
    assert out.startswith("pre-commit hook failed:")
    assert out.endswith("commit aborted (exit 1)")
    assert "run covering tests" in out and "Failed" in out
    # the covering head reports VERDICT only — no test name / nodeid ever.
    assert contains_test_identity(out) is False and not contains_gt_tag(out)


def test_renderer_clean_cert_delivers_nothing():
    # no failing head + no hygiene block -> "" (correct-or-quiet; no praise line).
    assert render_completion_cert_native([]) == ""
    assert render_completion_cert_native(None) == ""
    # an unknown/ADVISORY field name is not a hook label -> still "".
    assert render_completion_cert_native(["obligation_coverage"]) == ""


def test_renderer_hygiene_world_fact_detail_is_scrubbed():
    # a test identity riding the world-fact detail is scrubbed to <test> (leak=0).
    out = render_completion_cert_native(
        [], hygiene_blocked=True, hygiene_detail="broke tests/test_secret.py")
    assert "diff hygiene" in out and "Failed" in out
    assert contains_test_identity(out) is False and "test_secret" not in out
    assert "<test>" in out


# =========================================================================== #
# seam integration — a HYGIENE block surfaces the cert block under the flag
# =========================================================================== #
def test_hygiene_block_delivers_native_cert_block(submit_env, monkeypatch):
    monkeypatch.setattr(g, "_gt_submit_hygiene", _block_hygiene)
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    g._gt_submit_bounce_count = 0
    out = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert out is not None and out.get("returncode") == 1, "hygiene head must block"
    obs = out["output"]
    # the D7 native pre-commit block — the per-head signature the single-line refusal lacks.
    assert "pre-commit hook failed:" in obs
    assert "diff hygiene" in obs and "Failed" in obs      # RED pre-wire (single-line refusal)
    assert "vendored file changed" in obs                 # the world-fact reason
    assert not contains_gt_tag(obs) and contains_test_identity(obs) is False


def test_covering_block_delivers_native_cert_block_leak0(submit_env, monkeypatch):
    monkeypatch.setattr(g, "_gt_submit_hygiene", _clean_hygiene)
    monkeypatch.setattr(g, "_gt_submit_covering",
                        _fail_covering(["tests/test_x.py::test_a"], ["tests/test_x.py"]))
    # attribution proven -> the covering fail reaches the head (else it is dropped, FP=0).
    monkeypatch.setattr("groundtruth.runtime.covering_runner.is_red_attributable",
                        lambda *a, **k: True)
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("mod.py")
    g._gt_submit_bounce_count = 0
    out = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert out is not None and out.get("returncode") == 1
    obs = out["output"]
    assert "run covering tests" in obs and "Failed" in obs
    # LEAK-0: the failing test name (in head_record for host use) is NEVER model-facing.
    assert contains_test_identity(obs) is False
    assert "test_x" not in obs and "::" not in obs


# =========================================================================== #
# CLEAN cert -> zero bytes (allow is never turned into a block)
# =========================================================================== #
def test_clean_cert_delivers_nothing(submit_env, monkeypatch):
    monkeypatch.setattr(g, "_gt_submit_hygiene", _clean_hygiene)  # clean head -> ALLOW
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    out = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert out is None, "a clean cert on an allow must deliver NOTHING (correct-or-quiet)"


# =========================================================================== #
# byte-identical OFF — the observation is EXACTLY the single-line refusal
# =========================================================================== #
def test_cert_delivery_off_is_byte_identical(submit_env, monkeypatch):
    monkeypatch.setattr(g, "_gt_submit_hygiene", _block_hygiene)
    monkeypatch.delenv("GT_CERT_DELIVERY", raising=False)
    monkeypatch.delenv("GT_COMPLETION_CERT", raising=False)
    g._gt_submit_bounce_count = 0
    out = g._gt_gate_submit_exception(object(), "submit", Submitted())
    expected = render_submit_rejection("hygiene", "vendored file changed")
    assert out is not None and out["output"] == expected, "flag-off must be byte-identical"
    assert "diff hygiene" not in out["output"]            # the cert block is NOT rendered off-path


# =========================================================================== #
# MUTATIONS — each applied, observed to bite, restored.
# =========================================================================== #
def test_mutation_blank_renderer_reverts_to_single_line(submit_env, monkeypatch):
    """MUTATION — the renderer returns "" (blank): the seam falls back to the single-line
    refusal, so the per-head ``diff hygiene`` signature vanishes (reddens the delivery pin)."""
    monkeypatch.setattr(g, "_gt_submit_hygiene", _block_hygiene)
    monkeypatch.setattr(
        "groundtruth.runtime.native_render.render_completion_cert_native",
        lambda *a, **k: "")
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    g._gt_submit_bounce_count = 0
    out = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert out is not None and "diff hygiene" not in out["output"]  # reverted to render_submit_rejection
    assert out["output"] == render_submit_rejection("hygiene", "vendored file changed")


def test_mutation_neuter_scrub_reddens_leak_pin(submit_env, monkeypatch):
    """MUTATION — neuter ``_final_scrub`` (identity passthrough): a test path riding the hygiene
    world-fact detail leaks onto the model-facing observation (reddens LEAK-0)."""
    monkeypatch.setattr(
        g, "_gt_submit_hygiene",
        lambda root: {"blocking": True, "reason": "h", "detail": "broke tests/test_secret.py"})
    monkeypatch.setattr(
        "groundtruth.runtime.native_render._final_scrub",
        lambda line, tf=None: line)  # the neutered scrub
    monkeypatch.setenv("GT_CERT_DELIVERY", "1")
    g._gt_submit_bounce_count = 0
    out = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert out is not None
    # with the scrub neutered the identity survives -> the leak pin would RED here.
    assert contains_test_identity(out["output"]) is True
    assert "test_secret" in out["output"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

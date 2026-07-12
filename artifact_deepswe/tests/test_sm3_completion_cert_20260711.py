"""SM-3 (2026-07-11) — CompletionCertificate activation at the submit gate.

The DARK half of ``submit_gate`` (``build_certificate`` / ``safe_build_certificate``)
is wired into ``_gt_gate_submit_exception`` behind ``GT_COMPLETION_CERT`` (default OFF).

CONTRACT PINNED HERE (behaviour, not telemetry):
  1. DECISION == HEAD. The certificate ``decision`` is a PURE function of the frozen
     Gate-Kernel head (submit_gate single-decision-path law), so the seam's BLOCK SET is
     UNCHANGED whether the flag is on or off — a head BLOCK stays a block, a head ALLOW
     stays an allow. UNKNOWN never blocks; obligations are ADVISORY (never a block input).
  2. FLAG-GATED. Off -> the cert is NEVER built and NEVER recorded (byte-identical path).
     On  -> the cert is built + the host-only record fires exactly once per gate.
  3. FAIL-OPEN + LEAK-0. ``_gt_completion_cert_record`` never raises and surfaces only
     SDLC field NAMES + counts (never a test identity, never a raw runner detail).

These bite the mutations: flip the decision mapping (M1) -> the block/allow assertions
fail; build the cert regardless of the flag / never build it (M2) -> the flag-gating
assertions fail.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402


class Submitted(Exception):
    """Name-matches mini-swe-agent's completion signal (``_is_submitted_exc`` keys on the
    class NAME being exactly ``Submitted``)."""


@pytest.fixture
def submit_env(monkeypatch, tmp_path):
    """Drive ``_gt_gate_submit_exception`` deterministically: verify-execute on, not
    baseline, a stub repo root, an empty covering axis, and a spy over the host record."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_gt_submit_covering", lambda root: (None, []))
    monkeypatch.setattr(g, "_build_env_executor", lambda: None)
    monkeypatch.setattr(g, "_gt_submit_bounce_count", 0, raising=False)
    # isolate the dedup ledger so a repeated identical refusal is not suppressed.
    g._oracle_delivered_hashes.clear()
    calls: list = []
    monkeypatch.setattr(g, "_gt_completion_cert_record", lambda c: calls.append(c))
    return calls


def _block_hygiene(root):
    return {"blocking": True, "reason": "hygiene_test", "detail": "vendored file changed"}


def _clean_hygiene(root):
    return None


# --------------------------------------------------------------------------- #
# 1. HEAD BLOCK: same decision on/off; cert built+recorded only when flag ON.
# --------------------------------------------------------------------------- #
def test_block_decision_unchanged_and_cert_only_when_flag_on(submit_env, monkeypatch):
    monkeypatch.setattr(g, "_gt_submit_hygiene", _block_hygiene)
    calls = submit_env

    # flag OFF -> block, cert NEVER built (byte-identical path).
    monkeypatch.delenv("GT_COMPLETION_CERT", raising=False)
    g._oracle_delivered_hashes.clear()
    g._gt_submit_bounce_count = 0
    off = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert off is not None and off.get("returncode") == 1, "head hygiene block must block"
    assert calls == [], "flag OFF must NOT build/record the certificate"

    # flag ON -> SAME block decision, cert built + recorded exactly once, decision head-derived.
    monkeypatch.setenv("GT_COMPLETION_CERT", "1")
    g._oracle_delivered_hashes.clear()
    g._gt_submit_bounce_count = 0
    on = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert on is not None and on.get("returncode") == 1, "cert must not change the block"
    assert len(calls) == 1, "flag ON must build+record the certificate exactly once"
    assert calls[0].decision == "bounce_once", "a head BLOCK -> cert decision bounce_once"


# --------------------------------------------------------------------------- #
# 2. HEAD ALLOW: same decision on/off; cert decision mirrors the allow.
# --------------------------------------------------------------------------- #
def test_allow_decision_unchanged_and_cert_decision_allow(submit_env, monkeypatch):
    monkeypatch.setattr(g, "_gt_submit_hygiene", _clean_hygiene)
    calls = submit_env

    monkeypatch.delenv("GT_COMPLETION_CERT", raising=False)
    off = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert off is None, "a clean head must ALLOW (return None)"
    assert calls == []

    monkeypatch.setenv("GT_COMPLETION_CERT", "1")
    on = g._gt_gate_submit_exception(object(), "submit", Submitted())
    assert on is None, "cert must not turn a clean allow into a block (UNKNOWN never blocks)"
    assert len(calls) == 1 and calls[0].decision == "allow"


# --------------------------------------------------------------------------- #
# 3. The host record is fail-open + leak-safe.
# --------------------------------------------------------------------------- #
def test_completion_cert_record_never_raises_and_no_leak(capsys):
    from groundtruth.runtime.submit_gate import safe_build_certificate, GateVerdict

    cert = safe_build_certificate(
        head=GateVerdict(allow=False, reason="hygiene", detail="d"),
        covering={"verdict": "fail", "failing_test_names": ["tests/test_secret.py::test_x"]},
        hygiene={"blocking": True})
    g._gt_completion_cert_record(cert)
    err = capsys.readouterr().err
    assert "[GT_META] completion_cert" in err
    assert "decision=" in err
    # LEAK-0: the model never sees this line, but even so it must carry NO test identity.
    assert "test_secret" not in err and "::" not in err

    # A malformed cert (missing attrs / raising) must be swallowed, never propagated.
    class _Bad:
        @property
        def decision(self):
            raise RuntimeError("boom")
    g._gt_completion_cert_record(_Bad())  # must not raise

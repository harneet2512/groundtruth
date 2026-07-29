r"""RED (2026-07-28) — EVERY seal site must stamp an ObservationBinding.

LIVE SYMPTOM REPRODUCED (run 30390877219, every task):
``D:\gt_runs\30390877219\artifacts\ll-full-aiogram__aiogram-1594\
gt_receipts_aiogram__aiogram-1594.jsonl`` — every line carries
``"observation_binding": null``.  ``receipt_sidecar._parse_record`` then raises
``ValueError("receipt_observation_binding_missing")`` for every line, so
``gt_feature_metrics._receipt_corroboration`` reports ``integrity_ok: false`` and
``envelope_rows_seen: 0``.  The whole receipt ladder — GT's ONLY durable consumption
evidence — is unusable.

ROOT CAUSE (code-read, working tree 2026-07-28):
``ObservationBinding`` reaches a seal through the ContextVar
``gt_mini_patch._delivery_observation_context`` (declared ``:58``, read by
``_current_observation_binding`` ``:63``).  It is ``.set()`` ONLY inside the
formatter-level batch-commit path (``:17604`` ``_commit_batch_arbitration``,
``:17772`` precommitted submit dose, ``:17798`` phase-1 candidate preparation,
``:21988`` the canonical-runtime attachment).  THREE seal sites read it OUTSIDE any
batch and therefore get ``None``:

  * ``gt_mini_patch.py:14577``  inside ``_seal_lane_delivery``       (def ``:14524``)
  * ``gt_mini_patch.py:15482``  pooled gateway ``_commit_gateway``   (def ``:15464``)
  * ``gt_mini_patch.py:15854``  standalone gateway ``_commit_gateway``(def ``:15833``)

``adapters/miniswe.seal_delivery`` (``:1287``, binding kwarg ``:1298``, stamp ``:1346``)
resolves ``observation_binding or _current_observation_binding()`` and stamps whatever it
gets onto the sealed copy — ``None`` in, ``null`` out.

WHAT THIS FILE PINS (drives the REAL legacy seam via ``ss_gate.RealSeamDriver``, $0,
offline, hermetic — no container, no network, no benchmark repo):

  1. ``load_receipt_sidecar(...).integrity_ok is True`` over the receipts the real seam
     writes, and the ``envelope_rows_seen`` numerator (the exact ``envelope_owned``
     predicate ``gt_feature_metrics.py:710-717`` applies to the runtime-ledger rows) is
     ``> 0``.  Today: False / 0 — the live symptom, for free.
  2. EVERY persisted receipt carries ``envelope.observation_binding`` as a dict whose
     ``candidate_id`` equals ``observation_candidate_id(envelope["dedup_key"])``.
  3. COVERAGE: at least one receipt with ``kind == "lane"`` AND at least one with
     ``kind == "gateway"`` — a fix that lands on only one seal path FAILS this test.
  4. NEGATIVE / fail-closed: with ``_current_observation_binding`` forced to ``None``
     (and no batch context), the sidecar must STILL reject every receipt with exactly
     ``receipt_observation_binding_missing`` and yield ZERO trusted records.  That is
     ``receipt_sidecar.py:396`` and it must stay fail-closed after the fix.

Correction to the original brief, from reading + running the code:
``renderer_id`` is NEVER ``"gateway"``.  ``_persist_receipt`` (``:14167``) writes the
SEAL SITE as ``kind`` (``"lane"``/``"gateway"``); the envelope's ``renderer_id`` is
``"lane"`` for the lane seal and ``"native"``/``"tagged"`` for the gateway seals.  The
coverage assertion therefore joins on ``kind``, with ``renderer_id`` asserted as a
secondary witness so a fix cannot satisfy coverage by relabelling.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "src"), str(_REPO / "scripts" / "swebench"),
           str(_REPO / "artifact_deepswe"), str(_REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import receipt_sidecar as rs  # noqa: E402
import ss_gate as sg  # noqa: E402
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    observation_binding_from_dict,
    observation_candidate_id,
    validate_observation_binding,
)

# The receipt-file basename `_receipts_sidecar_path()` derives from `GT_C_OUT`.
_RECEIPTS_BASENAME = "gt_receipts.jsonl"


def _mixed_seal_events() -> list:
    """One event stream that provokes BOTH seal families in a single seam run.

    * ``grep run`` over the fixture's deliberately-ambiguous ``run`` symbol ->
      the gateway ``def_ref_partition`` delivery (``kind="gateway"``).
    * ``str_replace`` on ``pkg/mod_b.py`` -> the ``l3b.evidence`` caller block
      (``kind="lane"``, via ``_seal_lane_delivery``).
    * ``str_replace`` on ``pkg/util.py`` -> the ``l3.contract`` caller witness
      (``kind="lane"``), riding the graph edge the S2 fixture declares.

    This is exactly the ss_gate fixture vocabulary (``_grep``/``_write`` over
    ``tests/fixtures/ss_gate/``); no new fixture is introduced.
    """
    return [
        sg._grep("run", sg._run_hits()),
        sg._write(sg._MOD_B, "return 'b'", "return 'B'"),
        sg._write(sg._UTIL, "return x + 1", "return x + 2"),
    ]


def _drive_real_seam(out_dir: Path, ss_env: dict | None = None):
    """Run the REAL legacy seam once and return ``(SeamResult, raw_receipt_lines)``.

    ``GT_C_OUT`` is passed through the driver's per-arm env overlay (applied AFTER the
    hermetic ``GT_*`` strip), which is what makes ``_receipts_sidecar_path()`` resolve to
    a directory that OUTLIVES the driver's own temp dir.  ``GT_LANE_ENVELOPE`` and
    ``GT_GATEWAY`` are already pinned "1" by the driver's Profile-2 core; they are
    re-asserted here so the posture is readable at the call site.
    """
    env = {
        "GT_C_OUT": str(out_dir),
        "GT_LANE_ENVELOPE": "1",
        "GT_GATEWAY": "1",
    }
    env.update(ss_env or {})
    driver = sg.RealSeamDriver()
    result = driver.run(_mixed_seal_events(), env)
    path = out_dir / _RECEIPTS_BASENAME
    lines = (
        [json.loads(line) for line in
         path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if path.is_file() else []
    )
    return result, lines, path


@pytest.fixture(scope="module")
def sealed_run(tmp_path_factory):
    """POOLED arm (``GT_GLOBAL_ARBITER=1``, the driver's Profile-2 default).

    Verified by stack capture on the working tree: this arm reaches
    ``_seal_lane_delivery`` ``:14577`` (via ``_global_pool_flush`` -> ``_commit_lane``
    -> ``_lane_a_deliver``) and the POOLED ``_commit_gateway`` seal ``:15482`` (via
    ``_global_pool_flush``).  It does NOT reach the standalone ``:15854`` seal — see
    :func:`unarbitrated_run`.
    """
    out_dir = tmp_path_factory.mktemp("gt_c_out_pooled")
    result, lines, path = _drive_real_seam(out_dir)
    # Harness liveness: if the seam delivered nothing at all, every assertion below
    # would pass/fail vacuously.  Fail LOUD on a dead channel instead.
    assert lines, (
        "no receipts were written at all — the ss_gate fixture channel is DEAD "
        "(this is a harness fault, not the binding defect under test)"
    )
    return result, lines, path


@pytest.fixture(scope="module")
def unarbitrated_run(tmp_path_factory):
    """STANDALONE arm (``GT_GLOBAL_ARBITER=0``) — the THIRD seal site.

    Verified by stack capture: with the pool off, the gateway commits through
    ``_gt_gateway_deliver`` -> ``_deliver_one`` -> the NESTED ``_commit_gateway``
    (def ``:15833``, seal ``:15854``) — a genuinely DIFFERENT code path from the pooled
    ``_commit_gateway`` (def ``:15464``, seal ``:15482``), not the same function.  Both
    are near-duplicates that each read ``_current_observation_binding()`` independently,
    so a fix applied to only one of them leaves the other unbound.
    """
    out_dir = tmp_path_factory.mktemp("gt_c_out_standalone")
    result, lines, path = _drive_real_seam(out_dir, {"GT_GLOBAL_ARBITER": "0"})
    assert lines, "the unarbitrated arm produced no receipts — dead fixture channel"
    return result, lines, path


def _envelope_rows_seen(rows: list[dict]) -> int:
    """The EXACT ``envelope_owned`` numerator of ``gt_feature_metrics.py:710-717``.

    Recomputed here rather than imported so this test states the contract it pins and
    does not silently track a future narrowing of that private predicate.
    """
    seen = 0
    for row in rows:
        if row.get("layer") in rs._RECEIPT_EXEMPT_LAYERS:
            continue
        try:
            binding = observation_binding_from_dict(row.get("observation_binding"))
        except (TypeError, ValueError):
            binding = None
        candidate_id = row.get("candidate_id")
        seal = row.get("content_sha256_16")
        if (binding is not None
                and not validate_observation_binding(binding)
                and isinstance(candidate_id, str) and candidate_id
                and isinstance(seal, str)):
            seen += 1
    return seen


def test_sidecar_integrity_ok_and_envelope_rows_seen(sealed_run):
    """(1) The live kill: integrity_ok False / envelope_rows_seen 0."""
    result, _lines, path = sealed_run
    rows = result.ledger
    expected = rs.sealed_receipt_expected(rows)
    sidecar = rs.load_receipt_sidecar(path, sealed_delivery_expected=expected)

    codes = sorted({f.code for f in sidecar.failures})
    assert sidecar.integrity_ok is True, (
        f"receipt sidecar is UNTRUSTED: {len(sidecar.failures)} integrity failures "
        f"{codes} over {len(_lines)} persisted receipts"
    )
    seen = _envelope_rows_seen(rows)
    assert seen > 0, (
        "envelope_rows_seen == 0: no delivered runtime-ledger row carries a valid "
        "observation_binding, so the receipt ladder can never be joined "
        f"(delivered rows: {len([r for r in rows if r.get('outcome') == 'delivered'])})"
    )
    # A delivered+bound+sealed row is precisely what makes a sidecar MANDATORY. Once
    # (1) holds, this must flip too — otherwise the ladder is "green" only because
    # nothing was ever expected of it.
    assert expected is True, (
        "sealed_receipt_expected() is False — no delivered row is bound+sealed, so a "
        "missing/empty sidecar would be silently forgiven"
    )


def test_every_receipt_carries_a_binding_matching_its_dedup_key(sealed_run):
    """(2) Binding present as a dict AND joined to the sealed content identity."""
    _result, lines, _path = sealed_run
    unbound = [
        (i, rec.get("kind"), rec["envelope"].get("producer"))
        for i, rec in enumerate(lines, start=1)
        if not isinstance(rec.get("envelope", {}).get("observation_binding"), dict)
    ]
    assert not unbound, (
        f"{len(unbound)}/{len(lines)} receipts carry a NULL observation_binding: "
        f"{unbound}"
    )
    for i, rec in enumerate(lines, start=1):
        env = rec["envelope"]
        binding = env["observation_binding"]
        rebuilt = observation_binding_from_dict(binding)
        assert not validate_observation_binding(rebuilt), (
            f"receipt line {i}: binding fails its own validator: "
            f"{validate_observation_binding(rebuilt)}"
        )
        # SS-RCPT tolerance (receipt_sidecar.py:376-382): a post-pool byte mutation may
        # legally shift the FINAL dedup_key away from the PRODUCED candidate identity,
        # but ONLY when the record proves it with produced_dedup_key.
        produced = rec.get("produced_dedup_key")
        expected_candidate = observation_candidate_id(env["dedup_key"])
        if expected_candidate != binding["candidate_id"]:
            assert isinstance(produced, str) and produced, (
                f"receipt line {i} ({rec.get('kind')}/{env.get('producer')}): "
                "candidate identity diverges from dedup_key with NO mutation proof"
            )
            assert observation_candidate_id(produced) == binding["candidate_id"], (
                f"receipt line {i}: produced_dedup_key does not canonicalize to the "
                "binding candidate_id"
            )


def test_both_lane_and_gateway_seal_sites_are_covered(sealed_run):
    """(3) Coverage — a fix that lands on ONE seal path must fail here.

    Joins on the receipt ``kind`` (the seal SITE, written by ``_persist_receipt``), not
    on ``renderer_id``: the gateway seals stamp ``renderer_id`` ``native``/``tagged``,
    never ``"gateway"``.
    """
    _result, lines, _path = sealed_run
    bound_kinds = {
        rec.get("kind") for rec in lines
        if isinstance(rec.get("envelope", {}).get("observation_binding"), dict)
    }
    kinds_seen = sorted({rec.get("kind") for rec in lines})
    assert "lane" in bound_kinds, (
        f"no BOUND receipt from the _seal_lane_delivery site (kinds persisted: "
        f"{kinds_seen}) — gt_mini_patch.py:14577 still seals with a null binding"
    )
    assert "gateway" in bound_kinds, (
        f"no BOUND receipt from a gateway _commit_gateway site (kinds persisted: "
        f"{kinds_seen}) — gt_mini_patch.py:15482 / :15854 still seal with a null binding"
    )
    # Secondary witness: coverage cannot be faked by relabelling `kind`.
    renderers = {
        rec.get("kind"): rec["envelope"].get("renderer_id")
        for rec in lines
        if isinstance(rec.get("envelope", {}).get("observation_binding"), dict)
    }
    assert renderers.get("lane") == "lane"
    assert renderers.get("gateway") in {"native", "tagged"}


def test_standalone_gateway_seal_site_is_covered(unarbitrated_run):
    """(3b) The THIRD seal site — standalone ``_commit_gateway`` (``:15854``).

    A fix that lands only on the POOLED gateway seal (``:15482``) passes
    :func:`test_both_lane_and_gateway_seal_sites_are_covered` and FAILS here.
    """
    _result, lines, _path = unarbitrated_run
    bound_kinds = {
        rec.get("kind") for rec in lines
        if isinstance(rec.get("envelope", {}).get("observation_binding"), dict)
    }
    assert "gateway" in bound_kinds, (
        "the STANDALONE gateway seal (gt_mini_patch.py:15854, def :15833) still seals "
        f"with a null binding; kinds persisted: {sorted({r.get('kind') for r in lines})}"
    )
    assert "lane" in bound_kinds, (
        "the lane seal (gt_mini_patch.py:14577) is unbound on the unarbitrated path"
    )


def test_missing_binding_stays_fail_closed(tmp_path, monkeypatch):
    """(4) receipt_sidecar.py:396 must STAY fail-closed after the fix.

    EVERY identity source is forced empty, so the seal is genuinely unbound and no fallback
    can mask it.  There are three, and all three must be neutralised or this test locks
    nothing:
      1. ``_delivery_observation_context`` / ``_current_observation_binding`` -- the ContextVar;
      2. ``_batch_context`` -- the formatter-level batch identity;
      3. ``_legacy_observation_context`` -- the single-action identity ``_ensure_observation_
         binding`` derives on the legacy path (added 2026-07-28, and the ONLY one that is
         populated on mini-swe, so omitting it would have made this test vacuous the moment
         the fix landed).
    A receipt sealed in that state must be REJECTED by name.

    Honest scope: ``_persist_receipt`` (``:14167``) is correct-or-quiet and DOES still
    append the line — persistence never gates on the binding.  Fail-closure lives in the
    READER, and that is what is locked here: zero trusted records, and the failure named
    exactly ``receipt_observation_binding_missing``.
    """
    import gt_mini_patch as g

    monkeypatch.setattr(g, "_current_observation_binding", lambda: None)
    monkeypatch.setattr(
        g, "_current_observation_binding_dict", lambda: None, raising=False)
    g._delivery_observation_context.set(None)
    g._batch_context.set(None)
    # Source 3: neuter the legacy single-action identity at its producer, so the seam cannot
    # re-populate it on the next observation boundary mid-run.
    monkeypatch.setattr(g, "_begin_legacy_observation", lambda _action: None)
    g._legacy_observation_context.set(None)

    out_dir = tmp_path / "unbound_out"
    out_dir.mkdir()
    result, lines, path = _drive_real_seam(out_dir)
    assert lines, "the unbound arm produced no receipts at all — nothing to fail-close on"

    sidecar = rs.load_receipt_sidecar(
        path, sealed_delivery_expected=rs.sealed_receipt_expected(result.ledger))
    assert sidecar.integrity_ok is False
    assert sidecar.records == (), (
        "a receipt with a NULL observation_binding was accepted as a trusted record"
    )
    codes = {f.code for f in sidecar.failures}
    assert codes == {"receipt_observation_binding_missing"}, (
        f"expected the sidecar to fail-close by name; got {sorted(codes)}"
    )
    # And the row-level reader must raise, not merely be filtered out upstream.
    with pytest.raises(ValueError, match="receipt_observation_binding_missing"):
        rs._parse_record(lines[0], 1)


def test_environment_is_hermetic():
    """No network, no container, no benchmark artifact is consulted by this file."""
    assert os.environ.get("GT_HOST_GRAPH_DB") in (None, ""), (
        "an ambient GT_HOST_GRAPH_DB would steer the fixture graph channel"
    )
    assert (_REPO / "tests" / "fixtures" / "ss_gate" / "repo").is_dir()
    assert (_REPO / "tests" / "fixtures" / "ss_gate" / "graph_spec.json").is_file()

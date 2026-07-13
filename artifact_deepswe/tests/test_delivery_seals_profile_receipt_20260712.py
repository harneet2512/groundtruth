r"""W2 wave (2026-07-12): DELIVERY SEALS + PROFILE RECEIPT — host-side observability only.

Two features, both PURE telemetry (the model-facing observation append is byte-identical
before/after — see the BYTE-IDENTITY pins below):

1. DELIVERY SEALS. Every runtime-ledger row that records a model-visible delivery
   (outcome "delivered" with chars_delivered>0) also carries ``content_sha256_16`` =
   sha256(delivered-block-bytes).hexdigest()[:16] — the hash of the block appended to the
   observation (NOT the whole observation), so a post-run §4 audit can verify what the model
   actually saw. Rows without content are unchanged (no seal key at all). The writer gained an
   optional ``content=None`` kwarg (``_runtime_ledger_record``); the three byte-delivery sites
   thread the block through (Lane-A ``text`` / gateway ``_shipped`` / gate-winner ``win_text``).

2. PROFILE RECEIPT. At ``_install()`` time (once per process) a ``gt_profile_receipt.json`` is
   written next to the runtime ledger recording GT_RL_PROFILE + the ON GT_ flags + patched
   classes + pid. Rationale: on run 29217805592 the Profile-2 activation existed ONLY as a stderr
   log line, so the §4 auditor mis-graded the run as profile-dark (no artifact recorded it).

RED-first + biting mutations: see the module-tail comment for the two mutation sentinels.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.join(r"D:\Groundtruth", "artifact_deepswe"))
sys.path.insert(0, os.path.join(r"D:\Groundtruth", "src"))

import gt_mini_patch as g  # noqa: E402


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _read_ledger_rows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# 1. DELIVERY SEALS — writer level
# ---------------------------------------------------------------------------
def test_seal_pin_writer_hashes_the_exact_content(tmp_path, monkeypatch):
    """content='X'*10 -> the durable row carries content_sha256_16 == sha256(b'X'*10)[:16]
    and seal_scope=='block'. This is the load-bearing seal contract."""
    led = tmp_path / "led.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
    payload = "X" * 10
    g._runtime_ledger_record(
        kind="t.seal", outcome="delivered", chars=len(payload), content=payload)
    rows = _read_ledger_rows(str(led))
    assert rows, "writer produced no durable row"
    row = rows[-1]
    assert row["content_sha256_16"] == _sha16(payload) == hashlib.sha256(b"X" * 10).hexdigest()[:16]
    assert row["seal_scope"] == "block"
    # the seal is over the BLOCK, never the whole observation: a longer 'whole obs' hashes differently
    assert row["content_sha256_16"] != _sha16("PRE-OBSERVATION\n" + payload)


def test_no_content_row_unchanged(tmp_path, monkeypatch):
    """A row written WITHOUT content keeps the legacy shape EXACTLY — no seal key, no null.
    Content-absent rows must be indistinguishable from pre-feature rows."""
    led = tmp_path / "led.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
    g._runtime_ledger_record(
        kind="t.nocontent", outcome="suppressed_budget", reason="lane_budget", chars=0)
    row = _read_ledger_rows(str(led))[-1]
    assert "content_sha256_16" not in row, "content-absent row must NOT gain a seal key"
    assert "seal_scope" not in row, "content-absent row must NOT gain a seal_scope key"
    # exact legacy key set (the durable-writer adds timestamp_ms):
    assert set(row) == {
        "layer", "event_type", "file_path", "outcome", "reason",
        "chars_delivered", "iteration", "timestamp_ms",
    }


# ---------------------------------------------------------------------------
# 1b. DELIVERY SEALS — call-site wiring (mutation-(a) sentinel)
# ---------------------------------------------------------------------------
def test_gate_winner_site_seals_the_block_not_the_whole_observation(monkeypatch):
    """The gate-winner delivery threads the STEER BLOCK (win_text) as content — NOT the whole
    post-append observation. Base observation is non-empty so block != whole-obs; a mutation that
    passes out['output'] instead of win_text is caught here (the captured content would differ)."""
    calls: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: calls.append(kw))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_last_gate_winner_kind", "l5.no_test")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    monkeypatch.setattr(g, "_action_count", 3, raising=False)
    monkeypatch.setenv("GT_LANE_ENVELOPE", "0")  # _seal_lane_delivery is a no-op (default off)

    base = "COMMAND OUTPUT: 42 tests passed, all green"
    out = {"output": base}
    win_text = "GT: no covering test ran for this edit"
    g._deliver_gate_winner(
        out, "pytest -q", win_text,
        kkind="", kf="", krel="src/x.py", event=None, steer_base=base)

    delivered = [c for c in calls if str(c.get("outcome")).endswith("delivered")
                 or getattr(c.get("outcome"), "value", None) == "delivered"]
    assert delivered, "gate-winner did not record a DELIVERED row"
    d = delivered[-1]
    assert "content" in d, "delivery site must thread the block as content="
    assert d["content"] == win_text, (
        "site must seal the BLOCK (win_text), not the whole observation "
        f"({out['output']!r})")
    # the block is strictly SHORTER than the whole observation here — proves it's block-scoped
    assert d["content"] != out["output"]
    # and the model-facing append itself is byte-correct (base preserved as a pure prefix)
    assert out["output"].startswith(base) and win_text in out["output"]


# ---------------------------------------------------------------------------
# 2. PROFILE RECEIPT
# ---------------------------------------------------------------------------
def test_profile_receipt_written_with_members_filtering(tmp_path, monkeypatch):
    """_write_profile_receipt drops gt_profile_receipt.json next to the ledger; members_on
    contains a truthy GT_ flag and EXCLUDES a GT_ flag set to '0' (mutation-(b) sentinel)."""
    led = tmp_path / "sub" / "led.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
    monkeypatch.setenv("GT_RL_PROFILE", "2")
    monkeypatch.setenv("GT_W2_SEAL_ON", "1")
    monkeypatch.setenv("GT_W2_SEAL_OFF", "0")
    monkeypatch.setattr(g, "_PATCHED_CLASSES",
                        ["minisweagent.environments.local.LocalEnvironment"])

    g._write_profile_receipt()

    receipt_path = tmp_path / "sub" / "gt_profile_receipt.json"
    assert receipt_path.is_file(), "receipt file was not written next to the ledger"
    r = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert r["schema"] == "gt.profile_receipt.v1"
    assert r["gt_rl_profile"] == "2"
    assert "GT_W2_SEAL_ON" in r["members_on"], "truthy GT_ flag missing from members_on"
    assert "GT_W2_SEAL_OFF" not in r["members_on"], "GT_ flag set to '0' must be filtered out"
    assert r["patched_classes"] == ["minisweagent.environments.local.LocalEnvironment"]
    assert r["pid"] == os.getpid()
    assert isinstance(r["timestamp_ms"], int)
    assert r["members_on"] == sorted(r["members_on"]), "members_on must be sorted"


def test_profile_receipt_members_filter_normalizes_case_and_whitespace(tmp_path, monkeypatch):
    """LIPI fix (2026-07-12): the members_on filter must match rl_profile._is_on semantics —
    strip + lowercase BEFORE comparing to the OFF set (rl_profile.py:281-282,
    `value.strip().lower() not in _OFF_VALUES`). "OFF", "False", and "0 " (trailing space) are
    OFF to the runtime; a case/whitespace-sensitive filter would report them as ON — the receipt
    over-claiming activation, an honesty defect in the exact artifact built to prevent audit
    mis-grading."""
    led = tmp_path / "led.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
    monkeypatch.setenv("GT_W2_CASE_OFF", "OFF")     # uppercase OFF -> off to the runtime
    monkeypatch.setenv("GT_W2_PAD_OFF", "0 ")       # trailing space -> off to the runtime
    monkeypatch.setenv("GT_W2_STILL_ON", "1")       # control: a truthy flag stays ON
    monkeypatch.setattr(g, "_PATCHED_CLASSES", [])

    g._write_profile_receipt()

    r = json.loads((tmp_path / "gt_profile_receipt.json").read_text(encoding="utf-8"))
    assert "GT_W2_CASE_OFF" not in r["members_on"], \
        'env "OFF" is off to rl_profile._is_on and must be excluded (case-normalize)'
    assert "GT_W2_PAD_OFF" not in r["members_on"], \
        'env "0 " is off to rl_profile._is_on and must be excluded (strip whitespace)'
    assert "GT_W2_STILL_ON" in r["members_on"], "control truthy flag must remain ON"


def test_profile_receipt_write_is_best_effort_never_raises(monkeypatch):
    """A bad ledger dir (write fault) must be swallowed — the seam never crashes on telemetry."""
    # point the ledger at a path whose parent cannot be created (a file used as a dir component)
    monkeypatch.setenv("GT_RUNTIME_LEDGER", "\x00illegal\x00/led.jsonl")
    # must not raise
    g._write_profile_receipt()


# ---------------------------------------------------------------------------
# 3. BYTE-IDENTITY pins — the HARD INVARIANT (model-facing append unchanged)
# ---------------------------------------------------------------------------
# Behavioral pin (PRIMARY, robust): the append composers are pure functions of (base, block)
# and this change touches NONE of them, so their exact output is frozen here.
def test_join_lane_output_byte_identity():
    # boundary INSERTED when neither side has a newline (block ships as '\n'+block):
    assert g._join_lane_output("base output", "STEER block") == "base output\nSTEER block"
    # no boundary when the block already opens with '\n' (tagged Lane-A):
    assert g._join_lane_output("base output", "\n<gt-nudge>x</gt-nudge>") == "base output\n<gt-nudge>x</gt-nudge>"
    # no boundary when the base already ends with '\n':
    assert g._join_lane_output("base output\n", "STEER block") == "base output\nSTEER block"
    # empty base -> verbatim block (no leading boundary):
    assert g._join_lane_output("", "STEER block") == "STEER block"


def test_gt_gateway_append_is_pure_suffix():
    out = {"output": "PREFIX"}
    g._gt_gateway_append(out, "\nDELTA")
    assert out["output"] == "PREFIX\nDELTA"


def test_gt_deliver_append_byte_identity():
    out = {"output": "PREFIX"}
    ok = g._gt_deliver_append(out, "\nSTEER", join=True)
    assert ok and out["output"] == "PREFIX\nSTEER"
    out2 = {"output": "PREFIX"}
    ok2 = g._gt_deliver_append(out2, "RAW", join=False)
    assert ok2 and out2["output"] == "PREFIXRAW"


# Structural tripwire (SECONDARY): freeze the source of the three append composers. My diff
# touches none of them, so these hashes must hold. UPDATE-NOTE: if you INTENTIONALLY change a
# composer, first re-verify byte-identity end-to-end, then update the frozen hash below.
_FROZEN_COMPOSER_SRC_SHA16 = {
    "_join_lane_output": "fd62754bb81a0900",
    "_gt_gateway_append": "67b04d630a04517b",
    "_gt_deliver_append": "d0ed3a19a2582ac4",
}


def test_append_composers_source_unchanged():
    for fn, want in _FROZEN_COMPOSER_SRC_SHA16.items():
        src = inspect.getsource(getattr(g, fn))
        got = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
        assert got == want, (
            f"{fn} source changed (got {got}, frozen {want}). The delivery-seal / profile-receipt "
            f"work must NOT touch the model-facing append composers — re-verify byte-identity.")


# ---------------------------------------------------------------------------
# MUTATION SENTINELS (run manually, confirm bite, restore):
#   (a) seal the WHOLE observation instead of the block:
#       at the gate-winner site (gt_mini_patch _deliver_gate_winner) change
#       `content=win_text` -> `content=out.get("output")`
#       => test_gate_winner_site_seals_the_block_not_the_whole_observation FAILS.
#   (b) drop members_on filtering:
#       in _write_profile_receipt change the comprehension guard to just
#       `if k.startswith("GT_")` (drop the `and v not in (...)` value filter)
#       => test_profile_receipt_written_with_members_filtering FAILS (GT_W2_SEAL_OFF appears).
# ---------------------------------------------------------------------------

"""Part 3b — the caller_contract co-fact bucketer credit (2026-07-20).

`classify_ledger` buckets runtime-ledger rows by fact class. A pre-edit
`post_search.localize` row typed `def_partition` that carries a `co_fact` sidecar ALSO
credits caller_contract delivered on the SAME physical row — so caller_contract's
`delivered_byte_proven` gate can reflect the pre-edit delivery. It mints NO physical_id
(dose is graded on the shared physical_id), so a co-fact can never add a physical dose.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench"),
           str(_ROOT / "scripts" / "metrics"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.swebench.gt_feature_metrics import classify_ledger  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage, lineage_ledger_extra)


def _co():
    return lineage_ledger_extra(build_lineage(
        runtime_producer_id="contract_map",
        evidence_type="caller_contract_search",
        actual_event="search_result"))


_BLOCK = "src/app/config.py:12:load_config\ncallers: src/app/api.py:40 (get_settings)"


def _cofact_row():
    # a realistic pre-edit def_partition delivery: its own typed lineage flattened to the
    # top level (as _lane_delivery_extra writes it) so _typed_fact_class classifies it, PLUS
    # the co_fact sidecar the seam stamps when the block carries a callers line.
    row = dict(lineage_ledger_extra(build_lineage(
        runtime_producer_id="post_search",
        evidence_type="def_partition",
        actual_event="search_result")))
    row.update({
        "layer": "post_search.localize",
        "event_type": "search_result",
        "file_path": "src/app/config.py",
        "outcome": "delivered",
        "reason": "delivery",
        "chars_delivered": len(_BLOCK),
        "content_sha256_16": hashlib.sha256(_BLOCK.encode()).hexdigest()[:16],
        "co_fact": _co(),
    })
    return row


def test_cofact_row_credits_both_def_partition_and_caller_contract():
    per = classify_ledger([_cofact_row()])
    assert per["def_partition"]["delivered"] == 1, "host def_partition still credited"
    assert per["caller_contract"]["delivered"] == 1, "co-fact credits caller_contract"
    assert "search_result" in per["caller_contract"]["delivered_boundaries"]
    assert "src/app/config.py" in per["caller_contract"]["delivered_files"]


def test_no_cofact_no_caller_contract_credit():
    row = _cofact_row()
    del row["co_fact"]
    per = classify_ledger([row])
    assert per["def_partition"]["delivered"] == 1
    assert "caller_contract" not in per, "no co_fact -> no caller_contract bucket"


def test_cofact_credit_ignores_unauthorized_sidecar():
    # a sidecar that does not self-declare the registered producer match is REJECTED
    row = _cofact_row()
    row["co_fact"] = dict(row["co_fact"])
    row["co_fact"]["producer_registration_match"] = False
    per = classify_ledger([row])
    assert "caller_contract" not in per

"""The control-arm drift report must count the SCORED quantity, not the hash."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from legacy_control_drift import compare, load_run  # noqa: E402


def _sealed(tmp_path: Path, run: str, cases: dict[str, tuple[list[str], str]]) -> Path:
    root = tmp_path / run
    for index, (case_id, (order, projection)) in enumerate(cases.items()):
        shard = root / f"loc-vnext-python-{index}" / "sealed"
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{case_id}.json").write_text(
            json.dumps(
                {
                    "case": {"id": case_id},
                    "legacy": {
                        "candidate_order": order,
                        "projection_sha256": projection,
                    },
                }
            ),
            encoding="utf-8",
        )
    return root


def test_scored_drift_counts_candidate_order_not_the_projection_hash(tmp_path):
    """A projection hash covers unscored internals; quoting it overstates drift.

    Measured on the real runs: candidate_order agreed on 75-97% of shared cases
    while the projection hash agreed on 25-81%. Reporting the hash alone turned
    a 3-25% noise floor into a claim that two thirds of the control was
    irreproducible, which is not what any comparison actually scores.
    """
    left = _sealed(
        tmp_path,
        "left",
        {"a": (["src/x.py"], "hash-1"), "b": (["src/y.py"], "hash-2")},
    )
    right = _sealed(
        tmp_path,
        "right",
        # same scored order on BOTH; the hash moves on one of them
        {"a": (["src/x.py"], "hash-1"), "b": (["src/y.py"], "hash-DIFFERENT")},
    )

    result = compare(load_run(left), load_run(right))

    assert result["shared"] == 2
    assert result["candidate_order_identical"] == 2
    assert result["projection_identical"] == 1
    assert result["scored_drift_rate"] == 0.0, (
        "an unscored hash difference was reported as scored drift"
    )


def test_a_changed_candidate_order_is_reported_as_drift(tmp_path):
    left = _sealed(tmp_path, "left", {"a": (["src/x.py"], "h")})
    right = _sealed(tmp_path, "right", {"a": (["src/x.py", "src/z.py"], "h")})

    result = compare(load_run(left), load_run(right))

    assert result["scored_drift_rate"] == 1.0
    assert result["drifted"][0]["case_id"] == "a"


def test_no_shared_cases_is_reported_not_divided_by_zero(tmp_path):
    left = _sealed(tmp_path, "left", {"a": (["src/x.py"], "h")})
    right = _sealed(tmp_path, "right", {"b": (["src/y.py"], "h")})

    assert compare(load_run(left), load_run(right)) == {"shared": 0}

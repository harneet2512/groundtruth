from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from groundtruth.runtime.fact_registry import all_fact_classes
from groundtruth.runtime.rl_profile import PROFILE_MEMBERS


ROOT = Path(__file__).resolve().parents[1]


def _mandatory_perf_names() -> list[str]:
    """PERF is sections 1-9; correctness/comparative are outcome views."""
    text = (ROOT / "MANDATORY_METRICS.md").read_text(encoding="utf-8")
    names: list[str] = []
    for number in range(1, 10):
        section = re.search(
            rf"^## {number}\. .*?$(.*?)(?=^## |\Z)", text, re.M | re.S
        )
        assert section is not None
        names.extend(
            match.group(1)
            for match in re.finditer(
                r"^\| ([A-Za-z0-9_]+) \|", section.group(1), re.M
            )
            if match.group(1) != "metric"
        )
    return names


def test_scoreboard_inventory_is_exact_executable_128_universe() -> None:
    scoreboard = json.loads(
        (ROOT / ".claude/reports/SS_SCOREBOARD.json").read_text(encoding="utf-8")
    )
    inventory = scoreboard["feature_inventory"]

    assert {family: len(inventory[family]) for family in ("ACQ", "CAP", "FACT", "PERF")} == {
        "ACQ": 12,
        "CAP": 47,
        "FACT": 11,
        "PERF": 58,
    }
    assert inventory["count"] == 128
    assert set(inventory["CAP"]) == set(PROFILE_MEMBERS["2"])
    assert inventory["FACT"] == list(all_fact_classes())
    assert inventory["PERF"] == _mandatory_perf_names()

    contract_rows = scoreboard["contract_bearing"]
    assert Counter(row["family"] for row in contract_rows) == {
        "ACQ": 12,
        "CAP": 47,
        "FACT": 11,
        "PERF": 58,
    }
    assert Counter((row["family"], row["feature"]) for row in contract_rows) == Counter(
        (family, feature)
        for family in ("ACQ", "CAP", "FACT", "PERF")
        for feature in inventory[family]
    )


def test_no_inventory_row_can_start_ss_live() -> None:
    scoreboard = json.loads(
        (ROOT / ".claude/reports/SS_SCOREBOARD.json").read_text(encoding="utf-8")
    )

    assert scoreboard["status_now"]["ss_live"] == 0
    assert "all seven gates" in scoreboard["status_now"]["definition"]
    assert "live L witness" in scoreboard["feature_inventory"]["proof_rule"]

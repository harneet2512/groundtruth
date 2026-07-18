from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import gt_dispatch_feature_preflight as preflight  # noqa: E402


def test_static_manifest_derives_exact_inventory_and_accepts_all_authoritative_byte_owners() -> None:
    manifest = preflight.build_static_dispatch_manifest()
    assert manifest["schema"] == "gt.static_dispatch_feature_manifest.v1"
    assert manifest["family_counts"] == {"ACQ": 12, "CAP": 48, "FACT": 11, "PERF": 58}
    assert len(manifest["features"]) == 129
    assert manifest["dynamic_opportunity_proven"] is False
    assert manifest["ss_live_proven"] is False
    result = preflight.validate_static_dispatch_manifest(manifest)
    assert result["valid"] is True
    assert result["blocked_features"] == []
    # P4 (B-TERM 2026-07-16): coherence reclassified byte_owner → mediator. The static manifest
    # now derives its CAP row from the control decision contract, not the byte-owner mechanism:
    # its producer_authority is the executable decision site, and it rides the control terminal.
    coherence = manifest["features"]["GT_SS_COHERENCE_V2"]
    assert coherence["producer_authority"] == ["mini_seam.coherence.recovery_pivot"]
    assert coherence["evidence_relationship"] == (
        "control_participation->typed_FACT_candidate->sealed_delivery_observation"
    )
    assert coherence["collector_authority"] == (
        "scripts.swebench.gt_feature_metrics.control_participation"
    )
    for row in manifest["features"].values():
        assert "producer_authority" in row
        assert row["collector_authority"]
        assert row["evidence_relationship"]
        assert row["terminal_artifact"]


@pytest.mark.parametrize(
    "field", ["producer_authority", "collector_authority", "evidence_relationship", "terminal_artifact"],
)
def test_static_preflight_fails_closed_when_any_row_lacks_required_declaration(field: str) -> None:
    manifest = preflight.build_static_dispatch_manifest()
    broken = copy.deepcopy(manifest)
    broken["features"]["GT_GATEWAY"][field] = ""
    result = preflight.validate_static_dispatch_manifest(broken)
    assert result["valid"] is False
    assert f"GT_GATEWAY:authority_drift:{field}" in result["errors"]


def test_static_preflight_rejects_nonempty_self_attested_authority() -> None:
    manifest = preflight.build_static_dispatch_manifest()
    broken = copy.deepcopy(manifest)
    broken["features"]["GT_GATEWAY"]["producer_authority"] = "invented.but.nonempty"
    result = preflight.validate_static_dispatch_manifest(broken)
    assert "GT_GATEWAY:authority_drift:producer_authority" in result["errors"]


def test_static_preflight_rejects_null_acq_producer_authority() -> None:
    manifest = preflight.build_static_dispatch_manifest()
    broken = copy.deepcopy(manifest)
    broken["features"]["graph_validity"]["producer_authority"] = None
    result = preflight.validate_static_dispatch_manifest(broken)
    assert "graph_validity:authority_drift:producer_authority" in result["errors"]
    assert "graph_validity:missing_producer_authority" in result["errors"]


def test_acq_component_authorities_name_the_actual_construction_chain() -> None:
    manifest = preflight.build_static_dispatch_manifest()
    features = manifest["features"]

    assert features["graph_validity"]["producer_authority"] == [
        "groundtruth.pretask.graph_localizer.localize",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ]
    assert features["lexical_FTS5"]["producer_authority"] == [
        "groundtruth.pretask.hybrid.lexical_file_search",
        "groundtruth.pretask.v7_4_brief._score_variant_C",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ]
    assert features["semantic_embedder"]["producer_authority"] == [
        "groundtruth.pretask.anchor_select.select_anchors",
        "groundtruth.pretask.v7_4_brief._score_variant_C",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ]
    assert features["body_retrieval"]["producer_authority"] == [
        "groundtruth.pretask.graph_localizer._content_fts_candidates",
        "groundtruth.pretask.graph_localizer.localize",
        "groundtruth.pretask.anchor_select.select_anchors",
        "groundtruth.pretask.v7_4_brief.run_v74",
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
    ]


def test_body_authority_covers_both_terminal_source_families() -> None:
    authorities = preflight.build_static_dispatch_manifest()["features"][
        "body_retrieval"
    ]["producer_authority"]

    assert {
        "groundtruth.pretask.graph_localizer._content_fts_candidates",
        "groundtruth.pretask.graph_localizer.localize",
    } <= set(authorities)
    assert {
        "groundtruth.pretask.anchor_select.select_anchors",
        "groundtruth.pretask.v7_4_brief.run_v74",
    } <= set(authorities)
    assert authorities[-1] == "groundtruth.pretask.v1r_brief.generate_v1r_brief"


def test_compound_acq_authority_requires_every_callable(monkeypatch) -> None:
    original = preflight.ACQ_PRODUCER_AUTHORITIES["lexical_FTS5"]
    monkeypatch.setitem(
        preflight.ACQ_PRODUCER_AUTHORITIES,
        "lexical_FTS5",
        (*original, "groundtruth.pretask.v7_4_brief.not_a_real_callable"),
    )

    manifest = preflight.build_static_dispatch_manifest()
    row = manifest["features"]["lexical_FTS5"]
    assert row["blocked_by"] == ["missing_source_declared_acq_producer_authority"]


def test_checkout_source_accepts_only_direct_exact_callable_declarations() -> None:
    assert preflight._source_declares_callable(
        "groundtruth.pretask.hybrid", "lexical_file_search",
    ) is True
    assert preflight._source_declares_callable(
        "groundtruth.pretask.v7_4_brief", "lexical_file_search",
    ) is False
    assert preflight._source_declares_callable(
        "groundtruth.pretask.hybrid", "not_a_real_callable",
    ) is False
    assert preflight._checkout_module_source(
        "groundtruth.pretask.Hybrid",
    ) is None


def test_source_authority_never_falls_through_to_an_installed_copy(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(preflight, "_SOURCE_ROOTS", (tmp_path,))
    preflight._checkout_module_source.cache_clear()
    preflight._source_declares_callable.cache_clear()
    try:
        assert preflight._source_declares_callable(
            "groundtruth.pretask.hybrid", "lexical_file_search",
        ) is False
    finally:
        preflight._checkout_module_source.cache_clear()
        preflight._source_declares_callable.cache_clear()


def test_static_manifest_validates_after_canonical_json_round_trip() -> None:
    manifest = preflight.build_static_dispatch_manifest()
    decoded = json.loads(json.dumps(manifest, sort_keys=True))

    result = preflight.validate_static_dispatch_manifest(decoded)

    assert result["valid"] is True
    assert result["blocked_features"] == []


def test_cli_exception_writes_fail_closed_diagnostic_artifact(
    tmp_path, monkeypatch,
) -> None:
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(
        preflight, "build_static_dispatch_manifest",
        lambda: (_ for _ in ()).throw(ValueError("authority exploded")),
    )
    monkeypatch.setattr(
        sys, "argv", ["gt_dispatch_feature_preflight.py", "--output", str(output)],
    )

    assert preflight.main() == 1
    payload = json.loads(output.read_text("utf-8"))
    assert payload["manifest"] is None
    assert payload["preflight"]["valid"] is False
    assert payload["preflight"]["dispatch_authorized"] is False
    assert payload["preflight"]["error"] == "ValueError: authority exploded"


def test_static_preflight_never_accepts_inventory_drift() -> None:
    manifest = preflight.build_static_dispatch_manifest()
    broken = copy.deepcopy(manifest)
    broken["features"].pop("gold_rank")
    with pytest.raises(ValueError, match="exact 129"):
        preflight.validate_static_dispatch_manifest(broken)


def test_cli_is_valid_under_exact_workflow_pythonpath(tmp_path: Path) -> None:
    output = tmp_path / "static_feature_manifest.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((
        str(ROOT / "src"), str(ROOT / "scripts" / "swebench"),
    ))
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "swebench" / "gt_dispatch_feature_preflight.py"),
            "--output", str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert payload["manifest"]["feature_count"] == 129
    assert payload["preflight"]["valid"] is True
    assert payload["preflight"]["blocked_features"] == []


def test_cli_static_authority_does_not_depend_on_host_site_packages(
    tmp_path: Path,
) -> None:
    """The prepare host is not the substrate that executes ACQ producers."""
    output = tmp_path / "static_feature_manifest_no_site.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((
        str(ROOT / "src"), str(ROOT / "scripts" / "swebench"),
    ))

    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "scripts" / "swebench" / "gt_dispatch_feature_preflight.py"),
            "--output", str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert payload["preflight"]["valid"] is True
    assert payload["preflight"]["blocked_features"] == []

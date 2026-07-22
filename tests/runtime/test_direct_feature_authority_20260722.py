"""Fine evidence authority and Profile-2 obligation ownership regressions."""

from __future__ import annotations

import pytest

from groundtruth.runtime import fact_registry as fr


def test_fine_authority_matches_live_native_delivery_forms():
    """Aliases must describe their own bytes/channel, not their coarse class metadata."""
    expected = {
        "brief_localization": ("brief", "ranked-list"),
        "localization": ("post_search", "ranked-list"),
        "obligation_unexercised": ("post_test", "plan"),
        "trace_frame": ("reactive", "trace-native"),
        "signature_mismatch": ("post_edit", "compiler-native"),
        "companion_surface": ("post_edit", "linter-native"),
        "caller_break": ("post_edit", "contract"),
        "caller_contract_search": ("post_search", "grep-native"),
        "covering_red": ("post_edit", "test-native"),
        "covering_verdict": ("post_test", "test-native"),
        "new_file_destination": ("post_search", "change-native"),
        "missing_role:registration": ("post_search", "change-native"),
    }
    for evidence_type, (surface, renderer) in expected.items():
        authority = fr.authority_for(evidence_type)
        assert authority is not None
        assert authority.surface == surface
        assert authority.native_renderer == renderer
        assert fr.required_surface(evidence_type) == surface
        assert fr.required_renderer(evidence_type) == renderer


def test_fine_authority_uses_exact_producer_allowlist_and_rejects_unknowns():
    authority = fr.authority_for("localization")
    assert authority is not None
    assert authority.producers == ("ranked_localization", "v1r_brief")
    assert fr.producer_matches("localization", "ranked_localization")
    assert not fr.producer_matches("localization", "trace")
    assert fr.authority_for("not_a_fact") is None
    assert fr.required_surface("not_a_fact") is None


def test_profile2_retires_untyped_legacy_obligation_resurface(monkeypatch):
    """Missing V2 artifact must be quiet under Profile-2, not fall through to v1 bytes."""
    from artifact_deepswe import gt_mini_patch as seam

    monkeypatch.setenv("GT_RL_PROFILE", "2")
    monkeypatch.setattr(seam, "_GT_BASELINE", False)
    monkeypatch.setattr(seam, "_oblig_resurface_fired", False)
    monkeypatch.setattr(seam, "_load_gt_oracle", lambda: None)
    monkeypatch.setattr(seam, "_prose_leaks_test_identity", lambda _text: False)
    monkeypatch.setenv("GT_ISSUE_FILE", __file__)
    assert seam._obligation_resurface_candidate() is None
    # Do not manufacture lineage for the retired generic lane.
    assert seam._lane_registered_lineage("obligation.resurface", "edit_result") is None


def test_explicit_legacy_profile_preserves_resurface_bytes(monkeypatch, tmp_path):
    """The retirement is Profile-2-only; explicit legacy/control remains byte-compatible."""
    from artifact_deepswe import gt_mini_patch as seam

    issue = tmp_path / "issue.txt"
    issue.write_text("Implement the requested behavior across every supported input shape.")
    monkeypatch.setenv("GT_RL_PROFILE", "0")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_ISSUE_FILE", str(issue))
    monkeypatch.setattr(seam, "_GT_BASELINE", False)
    monkeypatch.setattr(seam, "_oblig_resurface_fired", False)
    monkeypatch.setattr(seam, "_load_gt_oracle", lambda: None)
    monkeypatch.setattr(seam, "_prose_leaks_test_identity", lambda _text: False)
    candidate = seam._obligation_resurface_candidate()
    assert candidate is not None
    assert "Implement the requested behavior" in candidate[1]
    monkeypatch.setenv("GT_RL_PROFILE", "1")
    assert seam._obligation_resurface_candidate() == candidate


def _set_profile2_artifact_state(seam, monkeypatch, tmp_path, state: str) -> None:
    monkeypatch.setenv("GT_RL_PROFILE", "2")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    if state == "corrupt":
        (tmp_path / "gt_obligations_v2.json").write_text(
            "{not-valid-json", encoding="utf-8")
    seam._obligations_v2_cache = None


@pytest.mark.parametrize("artifact_state", ["missing", "corrupt"])
def test_profile2_missing_or_corrupt_v2_is_quiet_on_resurface_route(
        monkeypatch, tmp_path, artifact_state):
    """Profile ownership cannot be inferred from artifact parse success."""
    from artifact_deepswe import gt_mini_patch as seam

    _set_profile2_artifact_state(seam, monkeypatch, tmp_path, artifact_state)
    monkeypatch.setattr(seam, "_GT_BASELINE", False)
    monkeypatch.setattr(seam, "_oblig_resurface_fired", False)
    monkeypatch.setattr(
        seam, "_load_gt_oracle",
        lambda: (_ for _ in ()).throw(AssertionError("legacy resurface touched")))

    assert seam._obligation_resurface_candidate() is None


@pytest.mark.parametrize("artifact_state", ["missing", "corrupt"])
def test_profile2_missing_or_corrupt_v2_is_quiet_on_review_route(
        monkeypatch, tmp_path, artifact_state):
    """The review dispatcher must not relabel artifact failure as legacy mode."""
    from artifact_deepswe import gt_mini_patch as seam

    _set_profile2_artifact_state(seam, monkeypatch, tmp_path, artifact_state)
    monkeypatch.setattr(
        seam, "_load_gt_oracle",
        lambda: (_ for _ in ()).throw(AssertionError("legacy review touched")))
    monkeypatch.setattr(
        seam, "_obligation_nudge_block",
        lambda: (_ for _ in ()).throw(AssertionError("legacy bytes produced")))

    assert seam._review_obligation_candidate() == (
        "obligation.unexercised", None, True)

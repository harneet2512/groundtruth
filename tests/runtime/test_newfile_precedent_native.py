from __future__ import annotations

from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime.adapters import miniswe as ad
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope, HYPOTHESIS
from groundtruth.runtime.feature_lineage import build_lineage
from groundtruth.runtime.native_render import contains_gt_tag, contains_test_identity


def _env(evidence_type: str, target: str, *, entity: str = "azure") -> EvidenceEnvelope:
    lineage = build_lineage(
        runtime_producer_id="change_surface",
        evidence_type=evidence_type,
        actual_event="failed_search",
        cap_feature_ids=("GT_CHANGE_SURFACE",),
    )
    assert lineage is not None
    return EvidenceEnvelope.build(
        producer="change_surface",
        fact_id=entity,
        target=target,
        evidence_type=evidence_type,
        payload=("generic English must not reach the native observation",),
        confidence=0.45,
        tier=HYPOTHESIS,
        lineage=lineage,
    )


def test_destination_renders_as_short_active_patch_header() -> None:
    env = _env("new_file_destination", "src/providers/azure.py")

    rendered = ad.render_envelope(env, native=True)

    assert rendered == "*** Add File: src/providers/azure.py\n"
    assert "generic English" not in rendered
    assert not contains_gt_tag(rendered)
    assert not contains_test_identity(rendered)


def test_missing_registration_renders_as_active_native_note() -> None:
    env = _env("missing_role:registration", "src/providers/registry.py")

    rendered = ad.render_envelope(env, native=True)

    assert rendered == "src/providers/registry.py: note: add 'azure' registration here\n"
    assert env.lineage is not None
    assert env.lineage.fact_class == "newfile_precedent"
    assert env.lineage.required_event == "failed_search"
    assert {(ref.category, ref.feature_id, ref.role) for ref in env.lineage.features} == {
        ("CAP", "GT_CHANGE_SURFACE", "byte_owner"),
        ("FACT", "newfile_precedent", "fact"),
    }


def test_missing_implementation_names_existing_path_as_precedent_not_destination() -> None:
    env = _env("missing_role:implementation", "src/providers/aws.py")

    assert ad.render_envelope(env, native=True) == (
        "src/providers/aws.py: note: add 'azure' implementation beside this precedent\n"
    )
    config = _env("missing_role:config_schema", "config/providers/aws.yaml")
    assert ad.render_envelope(config, native=True) == (
        "config/providers/aws.yaml: note: add 'azure' config/schema beside this precedent\n"
    )


def test_newfile_native_abstention_is_quiet_not_generic_prose() -> None:
    cases = (
        _env("new_file_destination", "tests/providers/test_azure.py"),
        _env("missing_role:test_shape", "tests/providers/test_aws.py"),
        _env("missing_role:unknown", "src/providers/registry.py"),
        _env("new_file_destination", "", entity="azure"),
        _env("new_file_destination", "src/providers/azure.py", entity="two words"),
        _env("new_file_destination", "../outside.py"),
        _env("new_file_destination", "C:/outside.py"),
    )

    for env in cases:
        assert ad.render_envelope(env, native=True) == ""


def test_declared_renderer_and_one_dose_contract_remain_bound() -> None:
    destination = _env("new_file_destination", "src/providers/azure.py")
    registration = _env("missing_role:registration", "src/providers/registry.py")

    assert fr.required_renderer(destination.evidence_type) == "change-native"
    assert fr.required_renderer(registration.evidence_type) == "change-native"
    winner = ad.arbitrate([destination, registration])
    assert winner is registration
    assert ad.render_envelope(winner, native=True).count("\n") == 1


def test_tagged_default_path_is_byte_unchanged() -> None:
    env = _env("new_file_destination", "src/providers/azure.py")
    assert ad.render_envelope(env, native=False) == (
        '<gt-fact kind="new_file_destination">\n'
        "generic English must not reach the native observation\n"
        "</gt-fact>\n"
    )

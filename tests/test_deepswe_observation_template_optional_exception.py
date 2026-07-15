"""Live observation rendering must accept ordinary outputs without exception metadata."""

from pathlib import Path

from jinja2 import Environment, StrictUndefined
import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _ROOT / "artifact_deepswe" / "gt_integration"
_CONFIG_NAMES = (
    "deepswe_gt_official.yaml",
    "deepswe_gt_pier.yaml",
    "deepswe_gt_pier_baseline.yaml",
    "deepswe_gt_pier_gemini.yaml",
    "deepswe_gt_pier_minimax.yaml",
    "deepswe_official_probe.yaml",
)


def _render(config_name: str, output: dict[str, object]) -> str:
    config = yaml.safe_load(
        (_CONFIG_DIR / config_name).read_text(encoding="utf-8")
    )
    template = Environment(undefined=StrictUndefined).from_string(
        config["model"]["observation_template"]
    )
    return template.render(output=output)


@pytest.mark.parametrize("config_name", _CONFIG_NAMES)
def test_observation_without_exception_info_renders_normally(config_name: str) -> None:
    rendered = _render(config_name, {"returncode": 0, "output": "finished"})

    assert "<returncode>0</returncode>" in rendered
    assert "finished" in rendered
    assert "<exception>" not in rendered


@pytest.mark.parametrize("config_name", _CONFIG_NAMES)
def test_present_exception_info_is_preserved(config_name: str) -> None:
    rendered = _render(
        config_name,
        {"returncode": 1, "output": "failed", "exception_info": "timed out"}
    )

    assert "<exception>timed out</exception>" in rendered


@pytest.mark.parametrize("config_name", _CONFIG_NAMES)
def test_empty_exception_info_does_not_emit_an_exception_block(config_name: str) -> None:
    rendered = _render(
        config_name, {"returncode": 0, "output": "ok", "exception_info": ""}
    )

    assert "<exception>" not in rendered

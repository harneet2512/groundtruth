"""Live observation rendering must accept ordinary outputs without exception metadata."""

from pathlib import Path

from jinja2 import Environment, StrictUndefined
import yaml


_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _ROOT / "artifact_deepswe" / "gt_integration" / "deepswe_gt_pier.yaml"


def _render(output: dict[str, object]) -> str:
    config = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    template = Environment(undefined=StrictUndefined).from_string(
        config["model"]["observation_template"]
    )
    return template.render(output=output)


def test_observation_without_exception_info_renders_normally() -> None:
    rendered = _render({"returncode": 0, "output": "finished"})

    assert "<returncode>0</returncode>" in rendered
    assert "finished" in rendered
    assert "<exception>" not in rendered


def test_present_exception_info_is_preserved() -> None:
    rendered = _render(
        {"returncode": 1, "output": "failed", "exception_info": "timed out"}
    )

    assert "<exception>timed out</exception>" in rendered


def test_empty_exception_info_does_not_emit_an_exception_block() -> None:
    rendered = _render({"returncode": 0, "output": "ok", "exception_info": ""})

    assert "<exception>" not in rendered

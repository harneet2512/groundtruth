"""GitHub Actions DISPATCHABILITY invariants — catch "the workflow cannot be run at all" locally.

WHY (2026-07-24): commit c6cc8b6b7 added a 26th ``workflow_dispatch`` input. GitHub caps inputs at
25 and enforces it ONLY at dispatch time (HTTP 422 "you may only define up to 25 `inputs`"), so:
  * the YAML still parses perfectly,
  * every test/lint/gate still passes,
  * the workflow is nevertheless UNDISPATCHABLE — every run request fails.
It went unnoticed until the next real dispatch. A pre-dispatch audit that checks code correctness
but never asks "can this workflow actually be launched?" cannot catch this class. These tests do.

They are pure-static (parse the YAML), need no network/credentials, and run in the normal suite.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

# GitHub Actions hard limits (docs: workflow_dispatch inputs).
_MAX_DISPATCH_INPUTS = 25

_REPO = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOWS = sorted((_REPO / ".github" / "workflows").glob("*.yml"))


def _dispatch_inputs(doc: object) -> "dict | None":
    """Return the workflow_dispatch inputs mapping, or None when the workflow has no manual trigger.

    NOTE the YAML ``on:`` key parses as the BOOLEAN True (YAML 1.1 truthy), which is why a naive
    ``doc["on"]`` lookup silently finds nothing and a guard like this reports a false PASS.
    """
    if not isinstance(doc, dict):
        return None
    triggers = doc.get(True, doc.get("on"))
    if not isinstance(triggers, dict):
        return None
    wd = triggers.get("workflow_dispatch")
    if not isinstance(wd, dict):
        return None
    ins = wd.get("inputs")
    return ins if isinstance(ins, dict) else None


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda p: p.name)
def test_workflow_dispatch_input_count_within_github_cap(path: pathlib.Path) -> None:
    """A workflow with >25 dispatch inputs is UNDISPATCHABLE (HTTP 422) even though its YAML is valid."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    ins = _dispatch_inputs(doc)
    if ins is None:
        pytest.skip(f"{path.name}: no workflow_dispatch inputs")
    assert len(ins) <= _MAX_DISPATCH_INPUTS, (
        f"{path.name} defines {len(ins)} workflow_dispatch inputs; GitHub allows at most "
        f"{_MAX_DISPATCH_INPUTS}. Every dispatch of this workflow will fail HTTP 422 "
        f"('you may only define up to 25 `inputs`'). Fold related toggles onto ONE existing input "
        f"(as the 2026-07-24 audit broadenings were folded onto `new_delivery_levers`) instead of "
        f"adding another. Inputs: {sorted(ins)}"
    )


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda p: p.name)
def test_workflow_yaml_parses(path: pathlib.Path) -> None:
    """A workflow whose YAML does not parse is dead on arrival (e.g. an unindented heredoc inside
    a `run: |` block scalar — the exact break introduced and caught locally on 2026-07-24)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path.name}: workflow did not parse to a mapping"
    assert doc.get("jobs"), f"{path.name}: no jobs defined"


def test_the_guard_itself_detects_an_over_cap_workflow(tmp_path: pathlib.Path) -> None:
    """MUTATION check: prove the guard BITES (a guard that cannot fail is not a guard).

    Without this, `_dispatch_inputs` returning None on every file would make the cap test a
    vacuous skip-everything PASS — which is precisely how the original 26-input bug survived.
    """
    over = {True: {"workflow_dispatch": {"inputs": {f"i{n}": {} for n in range(26)}}}, "jobs": {"j": {}}}
    ins = _dispatch_inputs(over)
    assert ins is not None and len(ins) == 26
    assert len(ins) > _MAX_DISPATCH_INPUTS, "guard would not bite on a 26-input workflow"
    # and the real workflow that carries the audit levers must be found (not silently skipped)
    real = _REPO / ".github" / "workflows" / "deepswe_full.yml"
    if real.exists():
        found = _dispatch_inputs(yaml.safe_load(real.read_text(encoding="utf-8")))
        assert found is not None, "deepswe_full.yml inputs not discovered — guard would be vacuous"

"""Pins the PROACTIVE top-N front-load in ``v1r_brief.render_brief``.

The rich per-file block is rendered only for non-``[INFO]`` candidates (usually
#1); lower-ranked candidates are ``[INFO]``-dropped (anti-flood). When
``GT_PROACTIVE_TOPN`` > 1, the brief front-loads a COMPACT contract/callers block
for the next-ranked ``[INFO]`` candidates so the agent has their cross-file facts
without opening each file.

Non-negotiable invariant pinned here: default (unset / ``=1``) is BYTE-IDENTICAL
to the pre-change brief, so the A/B OFF arm is exactly current behavior. The tier
classifier is monkeypatched so the test isolates THIS change from the localizer.
"""

from __future__ import annotations

import pytest

from groundtruth.pretask import v1r_brief as V

FE = V.FileEntry


def _mk(path: str, score: float, cp: str, callers: str, fn: str) -> "V.FileEntry":
    return FE(
        path=path,
        score=score,
        functions=[f"{fn}()"],
        function_names=[fn],
        contract=callers,
        contract_props=cp,
    )


@pytest.fixture()
def files() -> list:
    return [
        _mk("pkg/main.py", 9.0, "raises ValueError", "run() in cli.py:10", "main"),
        _mk("pkg/loader.py", 5.0, "returns Optional[T]", "load() in svc.py:20", "load"),
        _mk("pkg/cache.py", 4.0, "guards: if not key", "get() in svc.py:30", "get"),
        _mk("pkg/util.py", 3.0, "returns int", "calc() in m.py:40", "calc"),
        # [INFO] candidate with NO contract facts -> correct-or-quiet, never rendered
        FE(path="pkg/empty.py", score=2.0, functions=["noop()"], function_names=["noop"]),
    ]


@pytest.fixture(autouse=True)
def _force_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1 (main.py) is [VERIFIED]; everything else is [INFO].
    monkeypatch.setattr(
        V,
        "_entry_confidence_tier",
        lambda f, issue="": "[VERIFIED]" if f.path.endswith("main.py") else "[INFO]",
    )


def _render(files: list, topn, monkeypatch: pytest.MonkeyPatch) -> str:
    if topn is None:
        monkeypatch.delenv("GT_PROACTIVE_TOPN", raising=False)
    else:
        monkeypatch.setenv("GT_PROACTIVE_TOPN", str(topn))
    return V.render_brief(files, scores=[f.score for f in files], issue_text="please fix the loader")


def test_default_is_byte_identical_and_drops_info(files, monkeypatch):
    off = _render(files, None, monkeypatch)
    one = _render(files, 1, monkeypatch)
    assert off == one, "default must equal explicit =1 (byte-identical baked default)"
    assert "Other candidates" not in off
    # [INFO] candidates stay out of the body when the lever is off
    for p in ("pkg/loader.py", "pkg/cache.py", "pkg/util.py"):
        assert p not in off
    assert "pkg/main.py" in off  # [VERIFIED] #1 always present


def test_topn_4_frontloads_info_facts(files, monkeypatch):
    out = _render(files, 4, monkeypatch)
    assert "Other candidates" in out
    # compact contract + callers for the 3 [INFO] files
    assert "pkg/loader.py" in out and "returns Optional[T]" in out and "load() in svc.py:20" in out
    assert "pkg/cache.py" in out and "guards: if not key" in out
    assert "pkg/util.py" in out and "returns int" in out
    # correct-or-quiet: the fact-less [INFO] file is never rendered
    assert "pkg/empty.py" not in out


def test_topn_bound_is_honored(files, monkeypatch):
    # =2 -> need = 2 - 1(rendered non-INFO) = 1 -> ONLY the top [INFO] file
    out = _render(files, 2, monkeypatch)
    assert "pkg/loader.py" in out
    assert "pkg/cache.py" not in out
    assert "pkg/util.py" not in out


def test_no_test_name_leakage(files, monkeypatch):
    # The front-loaded facts are contract/callers only — never test identifiers.
    out = _render(files, 4, monkeypatch)
    lowered = out.lower()
    assert "test_" not in lowered
    assert "fail_to_pass" not in lowered
    assert "assert" not in lowered

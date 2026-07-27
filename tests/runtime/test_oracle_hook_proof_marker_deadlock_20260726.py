"""The ORACLE HOOK vs the PROOF MARKER — a self-inflicted import deadlock.

WHAT WENT WRONG (run 30229092179: 81 delivered payloads, ZERO canonical rows).

`gt_agent.py` appends a fail-closed assertion to the BOTTOM of mini-swe-agent's installed
`minisweagent/agents/default.py`::

    if GT_PROOF_MODE == '1' and GT_BASELINE != '1' and not exists(GT_PROOF_MARKER):
        raise RuntimeError('GT_PROOF_MARKER_ABSENT: ...')

Its purpose is sound: if gt_mini_patch's `.pth` load raised and site.py swallowed it, the
agent must CRASH rather than run a GT-off trajectory that gets graded GT-on.

The oracle hook then added, INSIDE `_install()`, a loop that does
`importlib.import_module("minisweagent.agents.default")` in order to wrap `run`. But
`_write_proof_marker()` was the LAST statement of the module, AFTER `_install()`. So at the
moment the oracle loop imports that module the marker does not exist yet, the appended
assertion fires, and the loop's own `except Exception: continue` -- written for "agent class
not in this install" -- swallows it. Python then evicts the half-initialised module from
`sys.modules`, so the harness's later real import SUCCEEDS and returns a class that was
never patched.

Net effect: `_PATCHED_AGENT_CLASSES == []`, the attachment stays None, `_augment_output`
takes its legacy branch every turn, GT delivers bytes with NO timing authority behind them,
and every delivery metric still reads green. Exactly the failure mode the oracle-hook tests
were written to catch -- and they missed it, because they asserted on a class that is
importable in the DEV tree (where nothing appends an assertion) instead of reproducing the
container's booby-trapped module.

THE FIX, and why it is not a weakening of the guard. `_write_proof_marker` already defines
its own precondition in its docstring: attest ATTACHMENT, i.e. write nothing unless
`_PATCHED_CLASSES` is non-empty. That predicate is fully determined the instant the env-class
loop finishes. Everything after it -- the oracle loop, discovery, the receipt -- is
self-guarded and cannot raise or change `_PATCHED_CLASSES`. So writing the marker directly
after the env loop attests exactly the same fact at exactly the same strength, while letting
the oracle loop import a module that checks for it. The module-end call stays (idempotent) so
the sentinel's "reached only if the module fully loaded" property is preserved for any future
code that lands in between.

DO NOT "fix" a failure here by deleting the assertion from `gt_agent.py`, by widening the
marker's write condition, or by making the oracle loop import lazily -- the first two remove
a real fail-closed guard and the third just moves the detonation to the first agent step.
"""

from __future__ import annotations

import inspect
import os
import sys
import textwrap
from pathlib import Path

import pytest

from artifact_deepswe import gt_mini_patch as seam


BOOBY_TRAPPED_MODULE = textwrap.dedent(
    '''
    """Stands in for the container's minisweagent/agents/default.py.

    Byte-for-byte the same GUARD SHAPE gt_agent.py appends: a module-level raise
    conditioned on the proof marker's existence.
    """
    import os as _gto

    if _gto.environ.get("GT_PROOF_MODE") == "1" and _gto.environ.get("GT_BASELINE") != "1":
        _gtm = _gto.environ.get("GT_PROOF_MARKER") or "/tmp/gt_proof_active"
        if not _gto.path.exists(_gtm):
            raise RuntimeError("GT_PROOF_MARKER_ABSENT: " + _gtm)


    class DefaultAgent:
        def __init__(self, model=None, env=None):
            self.model = model

        def run(self, task="", **kw):
            return {"exit_status": "Submitted", "task": task}
    '''
)


@pytest.fixture()
def booby_trapped_agent(tmp_path, monkeypatch):
    """Install a fake agent module that detonates exactly like the container's does."""
    pkg = tmp_path / "gt_fake_agents"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "trapped.py").write_text(BOOBY_TRAPPED_MODULE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    marker = tmp_path / "gt_proof_active"
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.delenv("GT_BASELINE", raising=False)
    monkeypatch.setenv("GT_PROOF_MARKER", str(marker))
    monkeypatch.setattr(seam, "_PROOF_MARKER_PATH", str(marker), raising=False)
    monkeypatch.setattr(seam, "_GT_BASELINE", False, raising=False)

    # The marker must be ABSENT going in -- that is the whole point.
    if marker.exists():
        marker.unlink()

    # Isolate _install: no env patching (the real env classes are already patched in this
    # interpreter and re-running the loop is noise), and no capability discovery.
    monkeypatch.setattr(seam, "_ENV_CLASSES", (), raising=False)
    monkeypatch.setattr(seam, "_AGENT_CLASSES", (("gt_fake_agents.trapped", "DefaultAgent"),))
    monkeypatch.setattr(seam, "_discover_agent_classes", lambda: [])
    # _write_proof_marker refuses to attest a process with no env hook attached; give it the
    # one real precondition it checks so the test exercises the ORDERING, not that predicate.
    monkeypatch.setattr(seam, "_PATCHED_CLASSES", ["fake.Env"], raising=False)
    monkeypatch.setattr(seam, "_PATCHED_AGENT_CLASSES", [], raising=False)
    monkeypatch.setattr(seam, "_write_profile_receipt", lambda *a, **k: None)

    sys.modules.pop("gt_fake_agents.trapped", None)
    sys.modules.pop("gt_fake_agents", None)
    yield marker
    sys.modules.pop("gt_fake_agents.trapped", None)
    sys.modules.pop("gt_fake_agents", None)


def test_the_trap_is_real_before_we_test_the_fix(booby_trapped_agent):
    """POSITIVE CONTROL. If this module imports cleanly with no marker, the fixture is not
    reproducing the container and every other assertion in this file is vacuous."""
    import importlib

    with pytest.raises(RuntimeError, match="GT_PROOF_MARKER_ABSENT"):
        importlib.import_module("gt_fake_agents.trapped")
    assert "gt_fake_agents.trapped" not in sys.modules, (
        "a raising module must be evicted from sys.modules -- that eviction is why the "
        "harness's later import silently succeeds UNPATCHED"
    )


def test_oracle_hook_attaches_even_though_the_agent_module_checks_the_marker(
    booby_trapped_agent,
):
    """THE REGRESSION TEST. `_install()` must leave the agent class patched.

    Before the fix this failed with `_PATCHED_AGENT_CLASSES == []`: the import raised
    GT_PROOF_MARKER_ABSENT and `except Exception: continue` ate it.
    """
    import importlib

    seam._install()

    assert seam._PATCHED_AGENT_CLASSES == ["gt_fake_agents.trapped.DefaultAgent"], (
        "the oracle hook did not attach -- GT will deliver bytes with no timing authority "
        f"and every metric will still look green (got {seam._PATCHED_AGENT_CLASSES!r})"
    )
    mod = importlib.import_module("gt_fake_agents.trapped")
    assert getattr(mod.DefaultAgent, "_gt_oracle_patched", False)
    assert mod.DefaultAgent.run.__qualname__.startswith("_wrap_agent_run")


def test_install_writes_the_marker_before_it_imports_any_agent_module(booby_trapped_agent):
    """Ordering stated as a fact about `_install`, not inferred from the outcome above.

    The marker must exist by the time the agent loop runs; a passing outcome test could in
    principle be satisfied by catching-and-retrying, which would be the wrong fix.
    """
    assert not booby_trapped_agent.exists()
    seam._install()
    assert booby_trapped_agent.exists(), (
        "_install() finished without writing the proof marker -- the agent-module import "
        "inside it can only have detonated"
    )


def test_marker_still_attests_attachment_and_is_not_written_for_a_dark_process(
    booby_trapped_agent, monkeypatch
):
    """ANTI-WEAKENING. Moving the write earlier must not turn it into an unconditional one.

    `_write_proof_marker`'s contract is 'attest ATTACHMENT': with zero env classes patched
    GT is effectively OFF in this process and the marker must stay absent so every reader
    fails closed. If a future edit moves the call somewhere that skips this predicate, a
    GT-off trajectory becomes gradable as GT-on -- the exact thing the sentinel prevents.
    """
    monkeypatch.setattr(seam, "_PATCHED_CLASSES", [], raising=False)
    seam._install()
    assert not booby_trapped_agent.exists(), (
        "marker written for a process with NO env hook attached -- the fail-closed sentinel "
        "has been reduced to a no-op"
    )


def test_module_end_sentinel_survives_so_late_failures_still_fail_closed():
    """The write must be ADDED before the agent loop, not MOVED there.

    The module-end call is what makes the sentinel mean 'the module fully loaded'. Deleting
    it in favour of the earlier one would let any future top-level code that raises after
    `_install()` still leave a marker behind.
    """
    src = Path(seam.__file__).read_text(encoding="utf-8")
    tail = src[src.rindex("_install()") :]
    assert "_write_proof_marker()" in tail, (
        "the module-end proof sentinel is gone; a raise after _install() would now be "
        "attested as a healthy GT-on process"
    )
    install_src = inspect.getsource(seam._install)
    assert "_write_proof_marker(" in install_src, (
        "the in-install marker write is gone -- the oracle loop's agent import will "
        "detonate the GT_PROOF_MARKER_ABSENT assertion again"
    )
    assert install_src.index("_write_proof_marker(") < install_src.index("_agent_targets"), (
        "the marker is written AFTER the oracle loop; the import inside that loop is "
        "exactly what fails"
    )


def test_env_loop_completes_before_the_marker_is_written():
    """Ordering the OTHER way: the marker attests `_PATCHED_CLASSES`, so it must be written
    only once the env loop has finished populating it. Writing it earlier would attest a
    set that is still being built."""
    install_src = inspect.getsource(seam._install)
    assert install_src.index("_ENV_CLASSES") < install_src.index("_write_proof_marker("), (
        "the marker is written before the env loop has populated _PATCHED_CLASSES"
    )


def test_no_test_pollution_of_the_real_marker():
    """Guards the fixture itself: these tests must never touch the production marker path."""
    assert os.environ.get("GT_PROOF_MARKER") in (None, "") or not os.environ.get(
        "GT_PROOF_MARKER", ""
    ).endswith("gt_proof_active_REAL")

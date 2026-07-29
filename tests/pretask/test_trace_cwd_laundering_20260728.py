"""A relative frame must not become "in repo" because of where the process runs.

MEASURED, not hypothesised.  ``_is_in_repo`` (``src/groundtruth/pretask/traces.py:124``)
resolves the candidate with ``os.path.realpath(path)`` at :151 and then tests
containment under ``realpath(repo_root)`` at :158.  For an ABSOLUTE path that is
correct and handles symlinks, which is why it is there.  For a RELATIVE path
``realpath`` resolves against the **current working directory** -- so when the CWD
happens to be the repo root, EVERY relative frame satisfies the containment test
and short-circuits before reaching the existence check at :170-171.

That existence check is not incidental.  The comment at :161-169 records why it
exists: run6 audit, finding D-2.  A third-party dependency frame stripped of its
install prefix (``omegaconf/base.py`` while fixing hydra) is relative and
bad-marker-free, and was wrongly delivered as the "deepest in-repo frame" with 0
rows in graph.db.  ``parse_stack_traces`` MANUFACTURES exactly this shape: it
strips ``site-packages/`` / ``dist-packages/`` at :212-216 specifically so frames
arrive relative.  The discriminator is stated in that comment -- EXISTENCE under
repo_root -- and the CWD short-circuit is what stops it running.

The CWD is the repo root in production.  Agents run in ``/testbed``, which is the
checkout.  So the guard is bypassed precisely in the environment it was written
for, and the same input yields opposite verdicts depending on where the process
was launched -- which also makes every offline test of this function unfaithful
to the live path unless it controls the CWD.

THE FIX.  Apply the realpath containment test only to ABSOLUTE paths.  A relative
path carries no host location of its own, so the only honest question to ask about
it is whether it names a real file under the root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from groundtruth.pretask.traces import _is_in_repo, parse_stack_traces  # noqa: E402


@pytest.fixture()
def testbed(tmp_path):
    """A hydra checkout: ``hydra/`` is repo source, ``omegaconf/`` is NOT vendored."""
    repo = tmp_path / "hydra"
    (repo / "hydra").mkdir(parents=True)
    (repo / "hydra" / "core.py").write_text("x", encoding="utf-8")
    return repo


@pytest.fixture()
def in_repo_cwd(testbed, monkeypatch):
    """Run from inside the checkout -- what the container actually does."""
    monkeypatch.chdir(testbed)
    return testbed


def test_a_third_party_frame_is_rejected_from_a_neutral_cwd(testbed, monkeypatch, tmp_path):
    """CONTROL. The guard works when the CWD is unrelated to the repo."""
    monkeypatch.chdir(tmp_path)
    assert _is_in_repo("omegaconf/base.py", str(testbed)) is False


def test_a_third_party_frame_is_still_rejected_from_INSIDE_the_repo(in_repo_cwd):
    """THE BUG. Identical input, and the only thing that changed is the CWD.

    ``omegaconf/base.py`` does not exist under a hydra checkout. It must be
    dropped whether the process runs from ``/testbed`` or from anywhere else.
    """
    assert not (in_repo_cwd / "omegaconf" / "base.py").exists(), "fixture precondition"
    assert _is_in_repo("omegaconf/base.py", str(in_repo_cwd)) is False, (
        "a non-existent third-party frame was laundered into the repo by the CWD"
    )


def test_real_repo_source_is_still_accepted_from_both_cwds(testbed, monkeypatch, tmp_path):
    """NEAR-NEGATIVE. The fix must not close the case the fallback exists to serve.

    ``loguru/_datetime.py`` under a loguru testbed IS repo source arriving via an
    installed-package traceback -- the flip lever described at traces.py:199-207.
    """
    for cwd in (tmp_path, testbed):
        monkeypatch.chdir(cwd)
        assert _is_in_repo("hydra/core.py", str(testbed)) is True, (
            f"real repo source was dropped when running from {cwd}"
        )


def test_absolute_paths_keep_their_symlink_aware_containment(testbed, monkeypatch, tmp_path):
    """NEAR-NEGATIVE. The realpath test is correct for absolute paths; keep it."""
    monkeypatch.chdir(tmp_path)
    inside = str(testbed / "hydra" / "core.py")
    outside = str(tmp_path / "elsewhere" / "mod.py")
    assert _is_in_repo(inside, str(testbed)) is True
    assert _is_in_repo(outside, str(testbed)) is False


def test_the_end_to_end_shape_parse_stack_traces_manufactures(in_repo_cwd):
    """INTEGRATION. ``parse_stack_traces`` strips ``site-packages/`` itself, so it
    is the producer of the relative third-party frame this bug mishandles."""
    issue = (
        "Traceback (most recent call last):\\n"
        '  File "/usr/lib/python3/site-packages/omegaconf/base.py", line 12, in get\\n'
        "    raise KeyError\\n"
        '  File "/usr/lib/python3/site-packages/hydra/core.py", line 30, in run\\n'
        "    return self._go()\\n"
    )
    files = [fr.file for fr in parse_stack_traces(issue, str(in_repo_cwd))]
    assert "omegaconf/base.py" not in files, (
        "a third-party dep frame was delivered as an in-repo frame"
    )
    assert "hydra/core.py" in files, "the real repo frame must survive"

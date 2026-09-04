"""Tripwire — the pier submit template must capture NEWLY-CREATED source files.

Blast site: artifact_deepswe/gt_integration/deepswe_gt_pier.yaml, the Submission block.
Activation: a future edit reverts the patch-capture command to a bare `git diff -- <files>`,
which silently OMITS untracked new files. That dropped the new files of every additive-feature
fix (a fix that CREATES source files) -> incomplete patch -> unresolved even when the agent
solved it (the superjson-error-stack-serialization ledger). Fixed in 225edaae with `git add -N`
(intent-to-add), proven RED->GREEN->apply on a scratch repo.

This is a staleness guard, not the proof (the proof is the live run). It fails closed if the
mechanism regresses.
"""

from pathlib import Path

_PIER_CFG = (
    Path(__file__).resolve().parents[1]
    / "artifact_deepswe"
    / "gt_integration"
    / "deepswe_gt_pier.yaml"
)


def test_pier_submit_captures_newly_created_files():
    text = _PIER_CFG.read_text(encoding="utf-8")
    # Assert the COMMAND form (`git add -N -- ... && git diff`), not the bare string —
    # the explanatory prose also says "git add -N", so a regression that reverts only the
    # command would falsely pass a `"git add -N" in text` check (caught by mutation test).
    assert "git add -N -- " in text and "git add -N -- <file1> <file2> && git diff" in text, (
        "pier submit template lost the `git add -N -- <files> && git diff` capture command "
        "-> additive-feature fixes will drop their newly-created files from the submission "
        "patch (the superjson additive-flip blocker; see 225edaae). A bare `git diff -- "
        "<files>` does not capture untracked new files."
    )


def test_pier_submit_wording_includes_created_files():
    text = _PIER_CFG.read_text(encoding="utf-8")
    # The wording must not say "only ... modified" without acknowledging created files,
    # or the agent is steered to omit new source files even with `git add -N` available.
    assert "created" in text.lower(), (
        "pier submit wording no longer mentions CREATED files — additive fixes need the "
        "agent to list its new source files (see 225edaae)."
    )

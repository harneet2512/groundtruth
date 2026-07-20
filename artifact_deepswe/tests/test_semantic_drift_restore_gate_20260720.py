"""D-Q (run6 cfn-lint m125): semantic_drift attributed a git-stash revert to
"your edit". When the agent runs `git stash` to probe a baseline, under V2 the
restore is routed to post_edit (the mtime-diff is non-empty and `or _v2_write_truth`
lets it through), and _semantic_drift_candidate then subtracts the reverted file's
guard set from the still-edited _sem_cache -> a false "your edit removed a guard".

Fix: an insight nudge routed from a recognized VCS RESTORE (_edit_from_restore)
is suppressed at the append, while the candidate is still CALLED so _sem_cache
stays in lock-step. Keys on restore-origin only: a genuine authored guard
deletion (a direct post_edit) still fires.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (_REPO / "artifact_deepswe", _REPO / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gt_mini_patch as g  # noqa: E402


_DRIFT_NUDGE = '\n<gt-nudge reason="semantic_drift">\nGT: your edit removed a guard\n</gt-nudge>'


def _setup(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def run():\n    if x is None:\n        return None\n")
    for k in [e for e in __import__("os").environ if e.startswith("GT_")]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    monkeypatch.setattr(g, "_root", lambda: str(root))
    # force the drift candidate to ALWAYS fire so the gate is what decides delivery
    monkeypatch.setattr(g, "_semantic_drift_candidate", lambda rel: (9.9, _DRIFT_NUDGE))
    # a git-stash reverts a file -> the mtime-diff routes it to post_edit under V2
    monkeypatch.setattr(g, "_subprocess_write_targets",
                        lambda root_, force_paths=None: ["pkg/mod.py"])
    g._reset_oracle_state()
    fired: list[str] = []
    monkeypatch.setattr(g, "_record_hook_fire", lambda kind: fired.append(kind))
    monkeypatch.setattr(g, "_record_hook_suppress", lambda kind, reason="": fired.append(kind))
    return fired


def _drift_seen(out, fired):
    return ("semantic_drift" in (out.get("output") or "")) or ("semantic_drift" in fired)


def test_git_stash_restore_does_not_emit_semantic_drift(monkeypatch, tmp_path):
    fired = _setup(monkeypatch, tmp_path)
    out = {"output": "", "returncode": 0}
    g._augment_output({"command": "git stash"}, out)
    assert not _drift_seen(out, fired), (
        "a git-stash revert must NOT be attributed to 'your edit' as semantic_drift")


def test_direct_authored_edit_still_emits_semantic_drift(monkeypatch, tmp_path):
    # CONTROL: a direct str_replace edit (not a restore) that removes the same guard
    # while nothing masks it MUST still fire — the gate keys on restore-origin only.
    fired = _setup(monkeypatch, tmp_path)
    out = {"output": "", "returncode": 0}
    g._augment_output({"command": "str_replace pkg/mod.py old new"}, out)
    assert _drift_seen(out, fired), (
        "a genuine authored guard deletion must still emit semantic_drift "
        "(the gate must not over-suppress ordinary post_edits)")

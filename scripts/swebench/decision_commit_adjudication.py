#!/usr/bin/env python3
"""E5 — the correctness + durability adjudicator for decision commits.

CLAUDE.md §5 names two publication endpoints that nothing currently computes:
``steps to durable correct decision commit`` and ``durable-correct-state risk
difference``.  Both need one primitive that does not exist anywhere in the tree:
a judgement of whether a DECISION COMMIT was CORRECT and whether it was DURABLE.

What this module is
-------------------
A PURE function over one trajectory.  No I/O beyond an optional path argument,
no network, no environment reads, no artifact writes.

Definitions (each traced to the code that already owns the notion):

* **decision commit** — an assistant step that MUTATES the repository, i.e. the
  ``is_edit`` event of ``gt_performance_metrics._parse_timeline``.  This is the
  same boundary ``chronological_adjudication.Chronology.decision_commit_index``
  names, and the same one ``fair_probe_result._treatment_acted`` closes the
  precommit-use window against (``d < a <= c``,
  ``scripts/swebench/fair_probe_result.py:258``).
* **correct** — the commit's target file is in the dataset gold set
  (``gold_source="dataset_gold"``, produced by
  ``gt_performance_metrics.gold_files_from_dataset``).  Membership uses the same
  suffix-tolerant ``_path_match`` every other gold-gated section uses, so this
  adjudicator can never disagree with ``_compute_localization`` about what
  "gold" means.
* **durable** — the commit is never undone afterwards.  Two undo signals, both
  strictly AFTER the commit's step: an explicit revert command
  (``gt_performance_metrics._REVERT_PATTERNS``: ``git checkout --`` /
  ``git restore`` / ``git revert``, plus the ``undo_edit``/``undo`` tool verbs
  from ``_NON_EDIT_VERBS_SET``), and — only when the caller supplies it —
  absence from the final submission patch, which is exactly the survival test
  ``_compute_edit_quality.first_edit_correctness`` already applies
  (``gt_performance_metrics.py:1553``).

  KNOWN BOUND (stated, not hidden): revert recognition is deliberately pinned to
  ``_REVERT_PATTERNS``, which requires ``git checkout --`` adjacently and so does
  NOT match ``git checkout HEAD -- <path>``.  Broadening it here would make this
  module disagree with ``_parse_timeline``'s ``is_revert`` and with
  ``edit_revert_rate``.  Broaden the shared pattern, not this reader.

FAIL-CLOSED (the rule this module exists to honour)
---------------------------------------------------
No gold  ->  ``correct=None`` on EVERY record, ``n_correct=None``,
``steps_to_first_durable_correct_commit=None`` with
``reason="no_gold_available"``.  Absence of gold is UNMEASURED, NEVER
incorrectness.  The same discipline applies to ``final_patch_files``: passing
``None`` means "no patch truth available", which never demotes durability; only
an explicitly supplied patch file list can.

REUSE, NOT REIMPLEMENTATION
---------------------------
The edit detector is IMPORTED, never mirrored: ``_parse_timeline`` walks the
mini-swe-agent ``messages`` list and does all edit/view/revert classification.
If that parser changes, this adjudicator changes with it — the two can never
disagree about what an edit is.  The ONE thing gt_performance_metrics does not
provide is the *target* of a revert (it only asks ``is_revert`` yes/no), so
``_revert_targets`` below is new code, written against the same
``_REVERT_PATTERNS`` vocabulary.

OFFLINE-ONLY: gold is a metric-side artifact and must never reach the model.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

try:  # standalone (scripts/swebench on sys.path)
    from gt_performance_metrics import (  # type: ignore[import-not-found]
        _REVERT_PATTERNS,
        _decode_tool_args,
        _norm_path,
        _parse_timeline,
        _path_match,
    )
except ImportError:  # packaged import path
    from scripts.swebench.gt_performance_metrics import (
        _REVERT_PATTERNS,
        _decode_tool_args,
        _norm_path,
        _parse_timeline,
        _path_match,
    )

SCHEMA = "gt.decision_commit_adjudication.v1"

# Reasons a durable-correct commit could not be named.  Named constants because a
# reader must never have to guess what an absent number meant.
REASON_NO_GOLD = "no_gold_available"
REASON_NO_COMMITS = "no_decision_commits"
REASON_NO_DURABLE_CORRECT = "no_durable_correct_commit"

# Durability bases, most-specific first.
BASIS_NO_REVERT = "no_revert_observed"
BASIS_REVERT_COMMAND = "revert_command"
BASIS_ABSENT_FROM_PATCH = "absent_from_final_patch"

GOLD_SOURCE_NONE = "none"
GOLD_SOURCE_DATASET = "dataset_gold"

# Undo verbs the structured tool surface uses.  gt_performance_metrics classifies
# these as NON-edits (``_NON_EDIT_VERBS_SET``) but never records them as reverts;
# for durability they are exactly a revert of the named path.
_UNDO_VERBS = {"undo_edit", "undo"}

# A bare file token: no whitespace, redirects, quotes, backslash.  Same token class
# gt_performance_metrics._extract_edited_file uses for shell operands.
_FILE_TOKEN = r"[^\s|;&><\"'\\]+"

# ``git checkout [<flags>] [<tree-ish>] -- <paths>`` and ``git restore [<flags>] <paths>``.
_CHECKOUT_DDASH_RE = re.compile(r"git\s+checkout\b[^\n;|&]*?--\s+(?P<paths>[^\n;|&]+)", re.I)
_RESTORE_RE = re.compile(r"git\s+restore\b(?P<rest>[^\n;|&]*)", re.I)
# Whole-tree reverts: no path operand, or an operand that names the tree itself.
_WHOLE_TREE_OPERANDS = {".", "./", "*", "-a", "--all", "--staged", "--worktree", "--source"}


def _iter_command_texts(event: dict) -> tuple[str, str]:
    """Return (decoded bash command, full raw command text) for one assistant event.

    Mirrors the two surfaces every gt_performance_metrics detector reads: the
    decoded structured argument (clean, unescaped) first, then the raw
    ``tool_calls`` JSON + prose blob.
    """
    tc_json = str(event.get("tc_json") or "")
    args = _decode_tool_args(tc_json)
    bash_cmd = str(args.get("command") or "") if args else ""
    return bash_cmd, str(event.get("cmd") or "")


def _bare_paths(operand_text: str) -> list[str]:
    """Split a shell operand blob into concrete file paths.

    Returns ``[]`` when the operand names the whole tree (``.``) or nothing —
    the caller reads that as an unbounded revert.
    """
    out: list[str] = []
    for token in re.findall(_FILE_TOKEN, operand_text or ""):
        if token in _WHOLE_TREE_OPERANDS:
            return []
        if token.startswith("-"):
            continue  # a flag (or the bare ``--`` separator), not a path
        out.append(_norm_path(token))
    return out


def _revert_targets(event: dict) -> list[str] | None:
    """Paths this event reverts.

    Returns ``None`` when the event reverts the WHOLE tree (``git checkout .``,
    ``git restore .``, ``git revert <sha>``) — an unbounded undo that covers every
    prior commit.  Returns ``[]`` when the event is not a revert at all.

    NOT available upstream: ``gt_performance_metrics`` only answers ``is_revert``
    (boolean).  Written against the same ``_REVERT_PATTERNS`` vocabulary so the
    two agree on WHICH commands are reverts; this adds only the target.
    """
    tc_json = str(event.get("tc_json") or "")
    args = _decode_tool_args(tc_json)
    if args:
        verb = str(args.get("command") or "").lower()
        if verb in _UNDO_VERBS:
            path = str(args.get("path") or args.get("file_path") or "")
            return [_norm_path(path)] if path else None

    bash_cmd, full_cmd = _iter_command_texts(event)
    for text in (bash_cmd, full_cmd):
        if not text or not _REVERT_PATTERNS.search(text):
            continue
        # ``git revert <sha>`` names a commit, not a path -> whole tree.
        if re.search(r"git\s+revert\b", text, re.I):
            return None
        match = _CHECKOUT_DDASH_RE.search(text)
        if match is None:
            match = _RESTORE_RE.search(text)
            if match is not None:
                # ``git restore -- path`` and ``git restore path`` are both legal;
                # _bare_paths drops every ``-``-prefixed token, so the bare ``--``
                # separator and any flags fall out on their own.
                paths = _bare_paths(match.group("rest"))
                return paths if paths else None
            continue
        paths = _bare_paths(match.group("paths"))
        return paths if paths else None
    return []


def _load_messages(trajectory: Any) -> list[dict]:
    """Accept a messages list, a trajectory dict, or a path to trajectory JSON.

    Fail-closed: an unreadable / unrecognised input yields ``[]`` (zero decision
    commits, a named reason), never an exception and never a fabricated timeline.
    """
    if isinstance(trajectory, list):
        return [m for m in trajectory if isinstance(m, dict)]
    if isinstance(trajectory, dict):
        messages = trajectory.get("messages") or []
        return [m for m in messages if isinstance(m, dict)]
    if isinstance(trajectory, (str, os.PathLike)):
        try:
            with open(trajectory, encoding="utf-8", errors="replace") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            return []
        return _load_messages(loaded)
    return []


def adjudicate_decision_commits(
    trajectory: Any,
    gold_files: list[str] | None = None,
    *,
    final_patch_files: list[str] | None = None,
    gold_source: str | None = None,
) -> dict[str, Any]:
    """Adjudicate every decision commit in one trajectory for correctness + durability.

    Args:
        trajectory: a mini-swe-agent trajectory dict (``{"messages": [...]}``),
            a bare messages list, or a path to ``mini-swe-agent.trajectory.json``.
        gold_files: the dataset gold SOURCE files (see
            ``gt_performance_metrics.gold_files_from_dataset``).  Empty/None means
            NO GOLD: correctness is UNMEASURED, never False.
        final_patch_files: files present in the final submission patch.  ``None``
            means no patch truth is available and durability is decided on revert
            commands alone.  A supplied list additionally demotes any commit whose
            target did not survive into the patch.
        gold_source: provenance label for the gold set.  Defaults to
            ``"dataset_gold"`` when gold is present, ``"none"`` when it is not.

    Returns:
        ``{"schema", "decisions": [...], "summary": {...}}``.

        Each decision record:
        ``{step, target_file, correct, durable, reverted_at, durability_basis}``
        where ``correct`` is ``None`` under no-gold (UNMEASURED) and
        ``reverted_at`` is the step of the first covering undo, else ``None``.

        Summary:
        ``{steps_to_first_durable_correct_commit, reason, n_commits, n_correct,
        n_durable, gold_source}``.  ``n_correct`` is ``None`` under no-gold.
    """
    messages = _load_messages(trajectory)
    timeline = _parse_timeline(messages)

    gold_set = [_norm_path(g) for g in (gold_files or []) if g]
    has_gold = bool(gold_set)
    effective_gold_source = gold_source or (
        GOLD_SOURCE_DATASET if has_gold else GOLD_SOURCE_NONE
    )
    patch_set = (
        [_norm_path(p) for p in final_patch_files if p]
        if final_patch_files is not None
        else None
    )

    # Pass 1: the decision commits, in trajectory order.
    commits: list[dict[str, Any]] = []
    for event in timeline:
        if event.get("role") != "assistant" or not event.get("is_edit"):
            continue
        target = _norm_path(str(event.get("edited_file") or ""))
        if not target:
            # An edit whose target could not be attributed is NOT a decision
            # commit we can adjudicate.  Dropping it is fail-closed: counting it
            # would inflate n_commits with an unadjudicable row.
            continue
        commits.append({"step": int(event.get("step") or 0), "target_file": target})

    # Pass 2: the undo events, in trajectory order.
    undos: list[tuple[int, list[str] | None]] = []
    for event in timeline:
        if event.get("role") != "assistant":
            continue
        targets = _revert_targets(event)
        if targets == []:
            continue
        undos.append((int(event.get("step") or 0), targets))

    decisions: list[dict[str, Any]] = []
    for commit in commits:
        step = commit["step"]
        target = commit["target_file"]

        correct: bool | None
        correct = None if not has_gold else any(_path_match(target, g) for g in gold_set)

        reverted_at: int | None = None
        for undo_step, undo_targets in undos:
            if undo_step <= step:
                continue  # an undo BEFORE the commit cannot undo it
            if undo_targets is None or any(_path_match(target, t) for t in undo_targets):
                reverted_at = undo_step
                break

        if reverted_at is not None:
            durable = False
            basis = BASIS_REVERT_COMMAND
        elif patch_set is not None and not any(_path_match(target, p) for p in patch_set):
            durable = False
            basis = BASIS_ABSENT_FROM_PATCH
        else:
            durable = True
            basis = BASIS_NO_REVERT

        decisions.append({
            "step": step,
            "target_file": target,
            "correct": correct,
            "durable": durable,
            "reverted_at": reverted_at,
            "durability_basis": basis,
        })

    # DETERMINISM: the walk is already ordered, but sorting on the record's own
    # keys makes the output independent of message ordering accidents and of any
    # future set-iteration in the parser.
    decisions.sort(key=lambda d: (d["step"], d["target_file"]))

    first_durable_correct: int | None = None
    if has_gold:
        for record in decisions:
            if record["correct"] is True and record["durable"] is True:
                first_durable_correct = record["step"]
                break

    if not has_gold:
        reason: str | None = REASON_NO_GOLD
    elif not decisions:
        reason = REASON_NO_COMMITS
    elif first_durable_correct is None:
        reason = REASON_NO_DURABLE_CORRECT
    else:
        reason = None

    summary = {
        "steps_to_first_durable_correct_commit": first_durable_correct,
        "reason": reason,
        "n_commits": len(decisions),
        "n_correct": (
            sum(1 for d in decisions if d["correct"] is True) if has_gold else None
        ),
        "n_durable": sum(1 for d in decisions if d["durable"] is True),
        "gold_source": effective_gold_source,
    }

    return {"schema": SCHEMA, "decisions": decisions, "summary": summary}


__all__ = [
    "BASIS_ABSENT_FROM_PATCH",
    "BASIS_NO_REVERT",
    "BASIS_REVERT_COMMAND",
    "GOLD_SOURCE_DATASET",
    "GOLD_SOURCE_NONE",
    "REASON_NO_COMMITS",
    "REASON_NO_DURABLE_CORRECT",
    "REASON_NO_GOLD",
    "SCHEMA",
    "adjudicate_decision_commits",
]

"""WS-5 (2026-07-23) — pre-submit reproduction-test SYNTHESIS (Agentless/Otter).

GT can SELECT + RUN an existing covering test (``_covering_tests_for_symbols``) but cannot
MANUFACTURE a covering RED when the runtime test does not exist — the common issue-driven /
new-feature case. The frontier's whole pre-submit catch (Agentless 2024 · Otter ICLR'25) is
built on SYNTHESIS: draft a self-checking reproduction from the issue, diversify, keep only
candidates that FAIL on the pre-edit tree for the issue's reason, require the RED to execute the
edited lines (change-coverage, SWT-Bench), and refuse a submit ONLY on a confirmed change-
covering RED that survives the edit. This module is the synthesis leg feeding ``covering_red`` ->
``GT_SS_SUBMIT_RED`` -> ``submit_refusal``. HYPOTHESIS-tier by nature (a predicted RED, never
[VERIFIED]).

**HARD BENCHMAXX BOUNDARY (this module's first and load-bearing primitive).** Synthesis inputs are
an ALLOW-LIST: issue text, obligations, the repo tree, prior execution results, and the agent's own
diff — NOTHING else. Reading a gold patch, FAIL_TO_PASS / PASS_TO_PASS labels, a hidden test, or a
task / instance id is a HARD ABORT (mirrors ``gt_feature_inventory`` abort-on-drift): a synthesis
that peeks at the oracle is not reproduction, it is a benchmark leak, and it must fail LOUDLY rather
than silently degrade. Off-benchmark quality is evaluated on TDD-Bench-Verified, never SWE-bench gold.

Flag: ``GT_REPRO_SYNTH`` (default off => :func:`synthesize_repro` abstains => byte-identical, and
nothing in the seam calls it yet). Pure / deterministic / LLM-free at this layer (the draft step,
when built, is the ONLY model-touching leg and lands behind its own sub-flag).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# A fenced code block in an issue — the reproduction the reporter usually provides. Language tag
# optional; python/py/pycon/text or none. Deterministic extraction only (regex, never an LLM).
_FENCE_RE = re.compile(
    r"```[ \t]*(?:python|py|pycon|text)?[ \t]*\r?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_REPRO_MARKER = "GT_REPRO_PASS"  # Agentless stdout-marker oracle (printed iff no bug)
_MAX_REPRO_CANDIDATES = 5  # Otter-style small ensemble (K=5), never Agentless's 40

__all__ = [
    "ForbiddenSynthesisInput",
    "ReproCandidate",
    "ReproSynthResult",
    "ReproVerdict",
    "assert_inputs_allowed",
    "run_submit_check",
    "synthesize_repro",
    "verify_candidate",
]

# The legitimate synthesis input allow-list (documented contract; enforced negatively via the
# forbidden-substring denylist below): issue_text, obligations, repo_root, exec_results, agent_diff.
# Substrings that mark an ORACLE input. Matched case-insensitively against every input key so a
# renamed alias (``fail_to_pass``, ``FAIL_TO_PASS``, ``gold_patch``, ``hidden_tests``, ``task_id``)
# cannot slip past. This is deliberately broad: a false abort is a loud, fixable bug; a false allow
# is a silent benchmark leak.
_FORBIDDEN_INPUT_SUBSTRINGS: tuple[str, ...] = (
    "fail_to_pass",
    "pass_to_pass",
    "f2p",
    "p2p",
    "gold",
    "oracle",
    "solution_patch",
    "reference_patch",
    "hidden_test",
    "hidden",
    "expected_patch",
    "task_id",
    "instance_id",
    "task-id",
    "instance-id",
)


class ForbiddenSynthesisInput(RuntimeError):
    """A synthesis input carried oracle signal (gold / FAIL_TO_PASS / hidden test / task id).

    Raised — never swallowed — so a benchmaxx leak fails LOUDLY at the boundary."""


@dataclass
class ReproCandidate:
    """One synthesized reproduction candidate. HYPOTHESIS-tier."""

    source: str  # the self-checking repro program text
    oracle_marker: str = ""  # stdout marker the pass/fail verdict keys on (Agentless)
    pre_edit_red: bool | None = None  # confirmed FAIL on the pre-edit tree for the issue's reason
    change_covering: bool | None = None  # RED executes the agent's edited lines (SWT-Bench)


@dataclass
class ReproSynthResult:
    """Synthesis output. ``abstained`` + ``reason`` explain a quiet (correct-or-quiet)."""

    abstained: bool = True
    reason: str = "flag_disabled"
    candidates: list[ReproCandidate] = field(default_factory=list)


def _synth_enabled() -> bool:
    return os.environ.get("GT_REPRO_SYNTH", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def assert_inputs_allowed(inputs: "dict[str, object]") -> None:
    """Hard-abort if any input key carries oracle signal (the benchmaxx boundary).

    Raises :class:`ForbiddenSynthesisInput` on the FIRST forbidden key (deterministic: keys are
    checked in sorted order). A key outside the allow-list is tolerated ONLY when it carries no
    forbidden substring (callers may pass incidental context), but an allow-list-external key that
    matches a forbidden substring aborts. Empty / None inputs are allowed (they abstain downstream)."""
    if not inputs:
        return
    for key in sorted(inputs):
        low = str(key).lower()
        for bad in _FORBIDDEN_INPUT_SUBSTRINGS:
            if bad in low:
                raise ForbiddenSynthesisInput(
                    f"synthesis input {key!r} carries oracle signal ({bad!r}); "
                    f"repro synthesis must read issue/obligations/repo/exec/diff ONLY"
                )


@dataclass
class ReproVerdict:
    """Deterministic verdict over a GIVEN candidate repro (LLM-free — GT's actual job)."""

    pre_edit_red: bool | None = None  # candidate FAILS on the pre-edit tree (a real repro)
    change_covering: bool | None = None  # the RED executes the agent's edited lines (SWT-Bench)
    post_edit_green: bool | None = None  # candidate PASSES after the edit (fix confirmed)
    refuse_submit: bool = False  # RED->still-RED after edit = the ONLY refusal trigger
    reason: str = "not_implemented"


def _run_is_green(stdout: object, marker: str) -> bool:
    """A candidate run is GREEN iff its self-check PASS ``marker`` appears in stdout (Agentless
    stdout-marker oracle). No marker / absent marker => RED. Deterministic; no test-identity read."""
    return bool(marker) and marker in str(stdout or "")


def _lines_overlap(a: object, b: object) -> bool:
    """True iff two ``{rel_path: iterable[int]}`` maps share any (path, line). Change-coverage
    (SWT-Bench): the RED must execute at least one line the agent's edit touched."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    for rel, lines in a.items():
        other = b.get(rel)
        if other and set(lines) & set(other):
            return True
    return False


def verify_candidate(
    candidate: ReproCandidate,
    inputs: "dict[str, object]",
) -> ReproVerdict:
    """Deterministically verify a GIVEN candidate repro — the LLM-free core of the pre-submit
    catch. GT does NOT draft the test body (semantic drafting requires an LLM, which GT's core
    forbids; the draft is the agent's or a structured obligation's). GT computes the verdict from
    execution RESULTS the seam already captured (the seam runs the candidate the same way
    ``gt_mini_patch._executed_covering_emission`` runs the repo's own tests):
      1. RED-confirm — the candidate FAILS on the PRE-edit tree (no PASS marker),
      2. change-coverage — the RED executes >=1 line the agent's edit touched (SWT-Bench),
      3. post-edit verdict — RED->GREEN = fix confirmed; a CONFIRMED change-covering RED that
         SURVIVES the edit (still RED post-edit) is the ONLY ``refuse_submit`` trigger.
    A candidate that is not a real repro (green pre-edit) or not change-covering => GT ABSTAINS
    (never refuses on an unattributable test — correct-or-quiet). Behind GT_REPRO_SYNTH,
    byte-identical off. ``inputs`` keys read: ``pre_edit_stdout``, ``post_edit_stdout``,
    ``executed_lines`` ({rel:[int]}), ``edited_lines`` ({rel:[int]}) — all allow-list sources."""
    if not _synth_enabled():
        return ReproVerdict(reason="flag_disabled")
    assert_inputs_allowed(inputs)

    marker = candidate.oracle_marker
    if not marker:
        return ReproVerdict(reason="no_oracle_marker")  # cannot verify without a self-check
    if "pre_edit_stdout" not in inputs:
        return ReproVerdict(reason="no_pre_edit_run")  # seam has not run it yet

    pre_red = not _run_is_green(inputs.get("pre_edit_stdout"), marker)
    covers = _lines_overlap(inputs.get("executed_lines"), inputs.get("edited_lines"))

    if not pre_red:
        # Green on the pre-edit tree => not a repro of THIS bug. Abstain (never a fabricated RED).
        return ReproVerdict(pre_edit_red=False, change_covering=covers, reason="not_a_repro")
    if not covers:
        # Real RED but it does not exercise the agent's edit => unattributable. Abstain.
        return ReproVerdict(pre_edit_red=True, change_covering=False, reason="not_change_covering")
    if "post_edit_stdout" not in inputs:
        # Confirmed change-covering RED; post-edit run pending -> report without a verdict yet.
        return ReproVerdict(pre_edit_red=True, change_covering=True, reason="awaiting_post_edit")

    post_green = _run_is_green(inputs.get("post_edit_stdout"), marker)
    return ReproVerdict(
        pre_edit_red=True,
        change_covering=True,
        post_edit_green=post_green,
        refuse_submit=not post_green,  # RED -> still-RED after the edit = the ONLY refusal
        reason="fix_confirmed" if post_green else "red_survived_edit",
    )


def _extract_repro_snippets(issue_text: str) -> "list[str]":
    """Deterministically pull candidate reproduction snippets from an issue's fenced code blocks
    (the repro the reporter usually pastes). LLM-FREE (regex only). Keeps blocks that look like
    executable exercise code — contain a call ``(`` or an ``assert`` / ``raise`` / ``import`` — so
    a pure traceback paste or prose block is skipped. De-duplicated, deterministic order, capped."""
    if not issue_text:
        return []
    out: "list[str]" = []
    seen: "set[str]" = set()
    for m in _FENCE_RE.finditer(issue_text):
        body = (m.group(1) or "").strip("\n")
        if not body.strip():
            continue
        low = body.lower()
        if low.lstrip().startswith("traceback"):
            continue  # a traceback paste (contains "(" but is not executable repro) -> skip
        if not ("(" in body or "assert" in low or "raise " in low or "import " in low):
            continue  # not executable exercise code (prose / output) -> skip
        key = body.strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(body)
        if len(out) >= _MAX_REPRO_CANDIDATES:
            break
    return out


def _wrap_with_oracle(snippet: str) -> ReproCandidate:
    """Wrap a raw repro snippet with the Agentless stdout-marker oracle: the snippet runs, and the
    PASS marker is printed ONLY if it completes without raising. So on the PRE-edit tree (bug
    present) the snippet's assert/exception fires -> no marker -> RED; POST-edit (fixed) it
    completes -> marker -> GREEN. Deterministic template; no bug-specific logic is synthesized —
    the reporter's own snippet is the oracle."""
    source = (
        "# GT-synthesized reproduction harness (deterministic; issue snippet + stdout-marker oracle)\n"
        + snippet.rstrip("\n")
        + "\nprint("
        + repr(_REPRO_MARKER)
        + ")\n"
    )
    return ReproCandidate(source=source, oracle_marker=_REPRO_MARKER)


def run_submit_check(
    issue_text: str,
    repo_root: str,
    edited_lines: "dict[str, object]",
    run_candidate,
) -> "ReproVerdict | None":
    """WS-5 seam ORCHESTRATION (LLM-free, testable): synthesize repro candidates from the issue,
    RUN each pre- and post-edit via ``run_candidate``, verify, and return the FIRST verdict whose
    ``refuse_submit`` is True (a change-covering RED that SURVIVES the edit) — else None (no
    refusal). This is the brain the seam calls; the seam supplies the in-container ``run_candidate``
    and hooks the returned verdict into ``submit_refusal`` / ``GT_SS_SUBMIT_RED``.

    ``run_candidate(candidate, pre_edit: bool) -> (stdout: str, executed_lines: dict[str, list[int]])``.
    Behind GT_REPRO_SYNTH via :func:`synthesize_repro` (abstains => returns None => byte-identical).
    Correct-or-quiet: an execution fault on a candidate skips it (never a refusal on a broken run)."""
    res = synthesize_repro({"issue_text": issue_text, "repo_root": repo_root})
    if res.abstained:
        return None
    for cand in res.candidates:
        try:
            pre_out, _pre_lines = run_candidate(cand, True)
            post_out, post_lines = run_candidate(cand, False)
        except Exception:  # noqa: BLE001 — an execution fault must never refuse a submit
            continue
        verdict = verify_candidate(
            cand,
            {
                "issue_text": issue_text,
                "repo_root": repo_root,
                "pre_edit_stdout": pre_out,
                "post_edit_stdout": post_out,
                "executed_lines": post_lines,
                "edited_lines": edited_lines,
            },
        )
        if verdict.refuse_submit:
            return verdict
    return None


def synthesize_repro(inputs: "dict[str, object]") -> ReproSynthResult:
    """Draft reproduction candidates DETERMINISTICALLY from the issue (option C, LLM-FREE): extract
    the reporter's fenced repro snippet(s) and wrap each with the stdout-marker oracle. GT does NOT
    invent bug-specific logic (that needs an LLM); when the issue carries no executable snippet it
    ABSTAINS (correct-or-quiet, ~common). The seam then RUNS each candidate and calls
    :func:`verify_candidate` (RED-confirm -> change-coverage -> post-edit verdict).

    Flag-off => abstain (byte-identical). The benchmaxx boundary is enforced first: ``inputs`` may
    carry issue_text / obligations / repo_root / exec_results / agent_diff ONLY (a gold / F2P /
    hidden-test / task-id key HARD-ABORTS via :func:`assert_inputs_allowed`)."""
    if not _synth_enabled():
        return ReproSynthResult(abstained=True, reason="flag_disabled")
    assert_inputs_allowed(inputs)
    snippets = _extract_repro_snippets(str(inputs.get("issue_text") or ""))
    if not snippets:
        return ReproSynthResult(abstained=True, reason="no_repro_snippet")
    candidates = [_wrap_with_oracle(s) for s in snippets]
    return ReproSynthResult(abstained=False, reason="drafted", candidates=candidates)

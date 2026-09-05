"""Layer 5 — Post-Edit Validator (FINAL_ARCH_V2 §3 Layer 5).

Fires ONLY on actionable contradictions after an edit:

- Signature change + ≥1 caller passes args that no longer match
- Return-type change + ≥1 caller assigns the result and reads a removed attribute
- Co-change rule violation: edited A but not B where A+B always move together

Never emits ``[GT_OK]``. Never narrates success. Never duplicates Layer 3
edit-context. Silence = no problems detected.

For this pass the validator is intentionally narrow: it only implements two
deterministic checks driven by ``graph.db`` (signature change → broken callers;
co-change miss). Diff-based return-type checking is stubbed; the framework is
in place but the heuristic is conservative and returns no warning until the
upstream diff analyzer lands.
"""

from __future__ import annotations

import enum
import os
import re
import sqlite3
from dataclasses import dataclass

from groundtruth.providers import (
    caller_code_provider,
    co_change_provider,
    contract_provider,
)
from groundtruth.state.agent_state import AgentState, canonical_repo_path


class WarningKind(str, enum.Enum):
    SIGNATURE_BROKEN_CALLER = "signature_broken_caller"
    RETURN_TYPE_CHANGED = "return_type_changed"
    CO_CHANGE_MISSED = "co_change_missed"


@dataclass(frozen=True)
class PostEditWarning:
    kind: WarningKind
    function: str
    file: str
    detail: str  # short, actionable; e.g. "auth.py:42 passes 3 args, new signature takes 2"


def _extract_old_call_arity(code: str, function_name: str) -> int | None:
    """Approximate arity of how ``function_name`` is *called* in this snippet.

    The provider returns caller code snippets; we look at the first call site
    inside the snippet that mentions the function and count comma-separated
    arguments. Conservative — only used when the legacy snippet contains the
    call literally.
    """
    if not code or not function_name:
        return None
    # Find the first ``function_name(`` and count args until matching close.
    idx = code.find(function_name + "(")
    if idx < 0:
        return None
    open_idx = idx + len(function_name)
    depth = 0
    args = 0
    has_content = False  # True iff any non-whitespace appeared inside the parens
    for ch in code[open_idx:]:
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                if has_content:
                    args += 1
                break
            continue
        if depth == 1 and ch == ",":
            args += 1
        elif depth == 1 and not ch.isspace():
            has_content = True
    return args


def _signature_param_count(signature: str) -> int | None:
    """Best-effort parameter count from a signature like ``def f(a, b, c=1)``.

    Returns ``None`` if the signature can't be parsed.
    """
    if not signature:
        return None
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner:
        return 0
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    # Drop ``self``/``cls`` so method signatures compare cleanly with call arity.
    filtered = [p for p in parts if p.split(":")[0].split("=")[0].strip() not in ("self", "cls")]
    return len(filtered)


def check_signature_break(
    db_path: str,
    repo_root: str,
    edited_file: str,
    function_names: list[str],
    state: AgentState,
) -> list[PostEditWarning]:
    """Detect callers whose call arity disagrees with the edited signature.

    Note: without a diff this is an approximation — we use the CURRENT signature
    (which is what the agent just edited) and compare against the call sites in
    callers' source. A call with a different arity than the live signature is a
    contradiction the agent should resolve.
    """
    warnings: list[PostEditWarning] = []
    canon = canonical_repo_path(edited_file, repo_root)
    if not canon:
        return warnings
    seen_files = sorted(state.visited_files_set())
    for fn in function_names[:3]:
        contract = contract_provider(db_path, canon, fn)
        if contract is None or not contract.signature:
            continue
        sig_arity = _signature_param_count(contract.signature)
        if sig_arity is None:
            continue
        callers = caller_code_provider(
            db_path, canon, fn, repo_root, seen_files=seen_files, limit=5,
        )
        for c in callers:
            call_arity = _extract_old_call_arity(c.code, fn)
            if call_arity is None:
                continue
            if call_arity != sig_arity:
                warnings.append(
                    PostEditWarning(
                        kind=WarningKind.SIGNATURE_BROKEN_CALLER,
                        function=fn,
                        file=edited_file,
                        detail=(
                            f"{c.file}:{c.line} passes {call_arity} arg(s), "
                            f"new signature takes {sig_arity}"
                        ),
                    )
                )
    return warnings


def check_co_change_miss(
    repo_root: str,
    edited_file: str,
    state: AgentState,
    *,
    min_cooccurrence: int = 3,
) -> list[PostEditWarning]:
    """Warn if files that *always* co-change with this one have been ignored.

    Stricter than the Layer-3 co-change hint: only fires when (a) git history
    shows the partner file in >= ``min_cooccurrence`` past commits with this
    file, and (b) the agent has edited at least one file but not the partner.
    """
    if not state.edited_files:
        return []
    edited_canon = canonical_repo_path(edited_file, repo_root)
    if not edited_canon:
        return []
    edited_set_canon = {canonical_repo_path(f, repo_root) for f in state.edited_files}
    co = co_change_provider(
        repo_root, edited_canon, edited_files=sorted(edited_set_canon), min_cooccurrence=min_cooccurrence,
    )
    warnings: list[PostEditWarning] = []
    for partner in co:
        partner_canon = canonical_repo_path(partner.file_path, repo_root)
        if partner_canon in edited_set_canon:
            continue
        warnings.append(
            PostEditWarning(
                kind=WarningKind.CO_CHANGE_MISSED,
                function="",
                file=edited_file,
                detail=(
                    f"history shows {partner.file_path} co-changes with "
                    f"{edited_canon} {partner.cooccurrence_count}x; not yet edited"
                ),
            )
        )
    return warnings


def check_post_edit(
    state: AgentState,
    db_path: str,
    repo_root: str,
    edited_file: str,
    function_names: list[str],
    edit_diff: str | None = None,  # noqa: ARG001 — reserved for future diff-based checks
) -> list[PostEditWarning]:
    """Run every Layer 5 check and return actionable warnings (may be empty).

    The empty list is a valid result and the *expected* result on most edits.
    Layer 5 never emits ``[GT_OK]``.
    """
    if not os.path.exists(db_path):
        return []
    warnings: list[PostEditWarning] = []
    warnings.extend(check_signature_break(db_path, repo_root, edited_file, function_names, state))
    warnings.extend(check_co_change_miss(repo_root, edited_file, state))
    return warnings


# Convenience wrapper for the validators package (mirrors orchestrator style).


def post_edit_validator(
    state: AgentState,
    db_path: str,
    repo_root: str,
    edited_file: str,
    function_names: list[str],
) -> list[PostEditWarning]:
    """Function-form alias kept in the public API."""
    return check_post_edit(state, db_path, repo_root, edited_file, function_names)


# Be hygienic in unit tests by exposing the internal arity helpers.
__all__ = [
    "PostEditWarning",
    "WarningKind",
    "check_co_change_miss",
    "check_post_edit",
    "check_signature_break",
    "post_edit_validator",
]

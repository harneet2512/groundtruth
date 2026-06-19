"""L3 engagement measurement — language-agnostic.

Reports whether evidence injected at edit-completion time is reflected in
the agent's *next* actions (window of N steps). Pure measurement, no gating.

Token model:
  TOKEN_RE  matches identifier-like tokens of length >= 3 (case-folded)
  SPLIT_RE  splits qualified names on .:/\\ before tokenizing
  STOPWORDS strips ~50 cross-language keywords + generic noise

Edit-completion rule (per design spec):
  - step.action.startswith("str_replace_editor")
  - the action verb (token 2 of the action string) is in {create, str_replace, insert}
  - len(step.state.gt_evidence) > 100

The spec wording "any of {create, str_replace, insert} in action" is honored
via the action-verb token because `str_replace` is a substring of
`str_replace_editor` itself; a literal substring check would mark every
view action as an edit.

Match metrics over the next-N-step window:
  match_rate      = |evidence_tokens & window_tokens| / max(1, |evidence_tokens|)
  substring_match = count of verbatim identifiers from evidence (3+ chars,
                    not stopword) appearing in the joined window text

Reporting-only: no thresholds, no exit codes. Population p10 floor must be
calibrated from n>=10 trajectories before any gate is locked.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
SPLIT_RE = re.compile(r"[.:/\\]")

STOPWORDS = {
    "def", "class", "func", "function", "fn", "let", "var", "const", "pub", "static",
    "public", "private", "protected", "final", "abstract", "for", "while", "loop",
    "each", "map", "filter", "range", "yield", "await", "async", "try", "catch",
    "except", "finally", "throw", "throws", "raise", "return", "break", "continue",
    "pass", "else", "elif", "then", "case", "switch", "default", "new", "delete",
    "null", "none", "nil", "true", "false", "undefined", "self", "this", "that",
    "args", "kwargs", "arg", "argv", "the", "and", "not", "with", "from", "import",
    "use", "using", "require", "module", "package", "file", "line", "code", "path",
    "name", "value", "type", "size", "len", "length", "int", "str", "bool", "dict",
    "list", "set", "any", "obj", "ref", "ptr", "more", "here", "have", "note",
    "todo", "fix", "bug", "issue",
}

EDIT_VERBS = {"create", "str_replace", "insert"}

GT_FAMILY_TAGS = {
    "IMPORT", "CALLER", "SIBLING", "TEST", "IMPACT", "TYPE", "PRECEDENT",
    "CALLER-BLIND-EDIT", "HALLUCINATED-IMPORT", "PATTERN-DIVERGENCE",
    "UNVERIFIED-EDIT", "BLAST-RADIUS", "CONTRACT-BREAK", "STYLE-DIVERGENCE",
    "TARGET", "CALLS", "CALLED BY", "SIMILAR",
}
_FAMILY_LINE_RE = re.compile(
    r"^\[(" + "|".join(re.escape(t) for t in GT_FAMILY_TAGS) + r")\]"
)
_STRUCT_TAG_RE = re.compile(r"^(TARGET|CALLS|CALLED BY|SIMILAR)\b")


def tokenize(text: str) -> set[str]:
    """Lowercased identifier tokens of length >= 3, minus stopwords."""
    if not text:
        return set()
    expanded = SPLIT_RE.sub(" ", text)
    out: set[str] = set()
    for m in TOKEN_RE.finditer(expanded):
        tok = m.group(0).lower()
        if len(tok) < 3:
            continue
        if tok in STOPWORDS:
            continue
        out.add(tok)
    return out


def tokenize_ordered(text: str) -> list[str]:
    """Same filter as tokenize() but preserves duplicates and order."""
    if not text:
        return []
    expanded = SPLIT_RE.sub(" ", text)
    return [
        m.group(0).lower()
        for m in TOKEN_RE.finditer(expanded)
        if len(m.group(0)) >= 3 and m.group(0).lower() not in STOPWORDS
    ]


def count_family_tags(evidence: str) -> int:
    if not evidence:
        return 0
    n = 0
    for line in evidence.splitlines():
        line = line.strip()
        if not line:
            continue
        if _FAMILY_LINE_RE.match(line) or _STRUCT_TAG_RE.match(line):
            n += 1
    return n


def _action_verb(action: str) -> str:
    parts = (action or "").split(None, 2)
    return parts[1] if len(parts) >= 2 else ""


def _window_text(traj: list[dict[str, Any]], idx: int, window: int) -> str:
    parts: list[str] = []
    for j in range(idx + 1, min(idx + 1 + window, len(traj))):
        s = traj[j]
        for k in ("thought", "action", "observation", "response"):
            v = s.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
    return "\n".join(parts)


def measure_l3_engagement(
    traj_path: Path,
    evidence_dir: Path | None = None,
    *,
    window: int = 3,
) -> dict[str, Any]:
    data = json.loads(Path(traj_path).read_text(encoding="utf-8", errors="replace"))
    traj = data.get("trajectory") or data.get("history") or (data if isinstance(data, list) else [])

    per_edit: list[dict[str, Any]] = []
    match_rates: list[float] = []
    substring_counts: list[int] = []
    family_counts: list[int] = []
    edits_with_evidence = 0

    edit_count = 0
    for i, step in enumerate(traj):
        action = step.get("action") or ""
        if not action.startswith("str_replace_editor"):
            continue
        verb = _action_verb(action)
        if verb not in EDIT_VERBS:
            continue
        edit_count += 1

        state = step.get("state") or {}
        evidence = state.get("gt_evidence") if isinstance(state, dict) else ""
        evidence = evidence or ""
        if len(evidence) <= 100:
            per_edit.append({
                "step": i,
                "action_verb": verb,
                "evidence_present": False,
                "match_rate": 0.0,
                "substring_match": 0,
                "family_tags": 0,
            })
            continue

        edits_with_evidence += 1
        ev_tokens = tokenize(evidence)
        ev_ordered = [t for t in tokenize_ordered(evidence) if len(t) >= 3 and t not in STOPWORDS]
        win_text = _window_text(traj, i, window)
        win_tokens = tokenize(win_text)
        win_lower = win_text.lower()

        overlap = ev_tokens & win_tokens
        match_rate = len(overlap) / max(1, len(ev_tokens))

        verbatim_count = 0
        seen: set[str] = set()
        for t in ev_ordered:
            if t in seen:
                continue
            seen.add(t)
            if t in win_lower:
                verbatim_count += 1

        family_tags = count_family_tags(evidence)

        match_rates.append(match_rate)
        substring_counts.append(verbatim_count)
        family_counts.append(family_tags)

        per_edit.append({
            "step": i,
            "action_verb": verb,
            "evidence_present": True,
            "evidence_len": len(evidence),
            "evidence_token_count": len(ev_tokens),
            "window_token_count": len(win_tokens),
            "overlap_token_count": len(overlap),
            "match_rate": round(match_rate, 4),
            "substring_match": verbatim_count,
            "family_tags": family_tags,
        })

    def _avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    engaged = sum(1 for c in substring_counts if c >= 1)
    return {
        "traj_path": str(traj_path),
        "window": window,
        "edit_count": edit_count,
        "edits_with_evidence": edits_with_evidence,
        "avg_match_rate": _avg(match_rates),
        "avg_substring_match": _avg([float(x) for x in substring_counts]),
        "engagement_rate": round(engaged / edits_with_evidence, 4) if edits_with_evidence else 0.0,
        "family_tag_richness_avg": _avg([float(x) for x in family_counts]),
        "per_edit": per_edit,
    }


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--traj", required=True, type=Path, help="path to .traj JSON")
    ap.add_argument("--evidence-dir", type=Path, default=None,
                    help="optional gt_evidence/ dir (currently unused — evidence read from step.state)")
    ap.add_argument("--window", type=int, default=3, help="next-step aggregation window (default 3)")
    args = ap.parse_args()
    out = measure_l3_engagement(args.traj, args.evidence_dir, window=args.window)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

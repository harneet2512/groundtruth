"""Product-owned payload budget and dedupe helpers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


FACT_TAG_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)\]")
IMPERATIVE_PREFIXES = (
    "Changing",
    "Must",
    "Check",
    "Run",
    "You edited",
    "Inspect",
    "GT:",
    "Before",
)


@dataclass
class BudgetResult:
    text: str
    meta: dict
    pending_lines: list[str] = field(default_factory=list)


@dataclass
class ContextBudgeter:
    delivered_facts: set[str] = field(default_factory=set)
    delivered_fact_ids: set[str] = field(default_factory=set)

    def stable_fact_id(self, line: str) -> str:
        return stable_fact_id(line)

    def trim(self, payload: str, max_tokens: int = 500) -> BudgetResult:
        if not payload:
            return BudgetResult(
                "",
                {
                    "max_tokens_est": max_tokens,
                    "char_cap": max_tokens * 4,
                    "chars_used": 0,
                    "lines_kept": 0,
                    "lines_total": 0,
                    "dedupe_ids": len(self.delivered_fact_ids),
                },
                [],
            )
        lines = payload.splitlines()
        fresh = [
            ln
            for ln in lines
            if ln.strip()
            and ln.strip() not in self.delivered_facts
            and self.stable_fact_id(ln) not in self.delivered_fact_ids
        ]
        imperative = [
            ln for ln in fresh if any(ln.strip().startswith(w) for w in IMPERATIVE_PREFIXES)
        ]
        facts = [ln for ln in fresh if ln not in imperative and ("[" in ln or "->" in ln)]
        other = [ln for ln in fresh if ln not in imperative and ln not in facts]
        result: list[str] = []
        chars = 0
        limit = max_tokens * 4
        for line in imperative + facts + other:
            if chars + len(line) > limit:
                break
            result.append(line)
            chars += len(line) + 1
        return BudgetResult(
            "\n".join(result),
            {
                "max_tokens_est": max_tokens,
                "char_cap": limit,
                "chars_used": chars,
                "lines_kept": len(result),
                "lines_total": len(lines),
                "dedupe_ids": len(self.delivered_fact_ids),
            },
            list(result),
        )

    def commit_delivered(self, lines: list[str]) -> None:
        """Call ONLY after the gate confirms this candidate won."""
        for line in lines:
            stripped = line.strip()
            if stripped:
                self.delivered_facts.add(stripped)
                fid = self.stable_fact_id(line)
                if fid:
                    self.delivered_fact_ids.add(fid)

    def reset(self) -> None:
        """Clear all state — call between retry attempts."""
        self.delivered_facts.clear()
        self.delivered_fact_ids.clear()


def stable_fact_id(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    m = FACT_TAG_RE.search(stripped)
    if m:
        tag = m.group(1)
        rest = stripped[m.end() :].strip()
        sym = re.split(r"\s|->|,|\(", rest, maxsplit=1)[0].strip()
        if sym:
            return f"{tag}:{sym.lower()}"
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]

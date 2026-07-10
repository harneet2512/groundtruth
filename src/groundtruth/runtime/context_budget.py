"""Product-owned payload budget and dedupe helpers."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


FACT_TAG_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)\]")
IMPERATIVE_PREFIXES = (
    "Changing", "Must", "Check", "Run", "You edited", "Inspect",
    "GT:", "Before",
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
            return BudgetResult("", {
                "max_tokens_est": max_tokens,
                "char_cap": max_tokens * 4,
                "chars_used": 0,
                "lines_kept": 0,
                "lines_total": 0,
                "dedupe_ids": len(self.delivered_fact_ids),
            }, [])
        lines = payload.splitlines()
        fresh = [
            ln for ln in lines
            if ln.strip()
            and ln.strip() not in self.delivered_facts
            and self.stable_fact_id(ln) not in self.delivered_fact_ids
        ]
        # C-1 (2026-07-10): SELECT under budget by priority (imperative > facts >
        # other — keep the actionable line when clipping), but EMIT the survivors in
        # the PRODUCER's original order. Previously the output was reordered into the
        # priority buckets, so the delivered block's line order diverged from what the
        # producer rendered (a FORM bug — scrambled narrative even when nothing was
        # clipped). Two phases: rank for selection, then restore producer order.
        def _prio(ln: str) -> int:
            s = ln.strip()
            if any(s.startswith(w) for w in IMPERATIVE_PREFIXES):
                return 0
            if "[" in ln or "->" in ln:
                return 1
            return 2
        limit = max_tokens * 4
        chars = 0
        chosen: set[int] = set()
        # selection order = (priority, original index); prefix-preserving break (C-2).
        for idx, line in sorted(enumerate(fresh), key=lambda t: (_prio(t[1]), t[0])):
            if chars + len(line) > limit:
                break
            chosen.add(idx)
            chars += len(line) + 1
        result: list[str] = [fresh[i] for i in range(len(fresh)) if i in chosen]
        return BudgetResult("\n".join(result), {
            "max_tokens_est": max_tokens,
            "char_cap": limit,
            "chars_used": chars,
            "lines_kept": len(result),
            "lines_total": len(lines),
            "dedupe_ids": len(self.delivered_fact_ids),
        }, list(result))

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
        rest = stripped[m.end():].strip()
        sym = re.split(r"\s|->|,|\(", rest, maxsplit=1)[0].strip()
        if sym:
            # C-3 (2026-07-10): TAG:first-symbol alone is too coarse — two DISTINCT
            # facts sharing tag+symbol ("[WITNESS] get_user calls -> A" vs "[WITNESS]
            # get_user called by -> B") collided on `witness:get_user`, so the fresh-
            # filter permanently dropped the second as already-delivered. Fold a
            # whitespace-normalized hash of the FULL remainder into the id: distinct
            # facts get distinct ids (no false suppression), while an identical
            # re-render (same tag/sym/rest modulo whitespace) still collides (true dedup).
            rest_norm = re.sub(r"\s+", " ", rest).lower()
            digest = hashlib.sha256(rest_norm.encode("utf-8")).hexdigest()[:8]
            return f"{tag}:{sym.lower()}:{digest}"
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]

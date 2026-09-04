"""Module — Issue-as-SPEC: deterministic requirement decomposition.

Oracle Stage 1 (`ORACLE_ARCHITECTURE_PLAN.md` §5.1).  Parses an issue/instruction
body into an ``obligations[]`` array — shallow, rule-based, verbatim-preserving.
NO LLM, NO paraphrase (the killed "semantic judge" stays dead).  Pure regex over
issue-text STRUCTURE (modal/requirement markers, behavior verbs, parenthesized
API-shape qualifiers, quoted literals, code spans, expected-vs-actual blocks,
numbered repro steps) — never specific keywords/repos/task IDs (LEG 1).

This is a SEPARATE consumer of the issue text from ``anchors.py`` with OPPOSITE
filtering: ``anchors.py`` deliberately DROPS language keywords like ``async`` /
``await`` (``_LANG_KEYWORD_TOKENS``, anchors.py:256-267) because they are noise
for *localization*; but ``async`` is precisely the *requirement* the spec
extractor must KEEP (the aiomonitor killer was the single verbatim word "async").
Localization wants symbols-that-are-nodes; the spec wants obligations-the-fix-
must-satisfy.  Different jobs, different filters — never merge them.

The extractor is deterministic and language-agnostic: it keys on requirement
GRAMMAR (modals, quoted spans, fenced code, numbered steps), which is invariant
across English issue bodies for any repo/language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ----------------------------------------------------------------- regex set
_IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]+)*)\b")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```[\w-]*\n(.*?)```", re.S)
# Double-quoted or single-quoted short literals the reporter calls out verbatim.
_QUOTED_RE = re.compile(r"[\"']([^\"'\n]{1,80})[\"']")

# Requirement modals / obligation markers (surface forms, case-insensitive).
# These are English requirement grammar, not domain keywords.
_MODAL_RE = re.compile(
    r"\b(must|must not|should|should not|shall|shall not|needs? to|is required|"
    r"are required|required to|expected to|has to|have to|ought to|always|never|"
    r"cannot|can't|won't|will not)\b",
    re.I,
)
# Behavior verbs (3rd-person / imperative) that describe expected behavior.
_BEHAVIOR_VERB_RE = re.compile(
    r"\b(returns?|raises?|throws?|accepts?|emits?|yields?|produces?|outputs?|"
    r"rejects?|resolves?|expects?|ignores?|preserves?|requires?|supports?|"
    r"handles?|defaults? to|fails? with|errors? out)\b",
    re.I,
)
# API-shape qualifiers in a parenthesized fragment attached to a symbol:
#   capture_snapshot (async, optional name, returns ID)
_API_QUALIFIER_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()\n]{0,120})\)")
# Qualifier keywords WORTH keeping (the requirement words anchors.py drops):
_QUALIFIER_KEYWORDS = (
    "async",
    "await",
    "sync",
    "synchronous",
    "asynchronous",
    "optional",
    "required",
    "nullable",
    "immutable",
    "mutable",
    "readonly",
    "read-only",
    "deprecated",
    "lazy",
    "eager",
    "abstract",
    "static",
    "const",
    "final",
    "thread-safe",
    "idempotent",
    "recursive",
    "ordered",
    "sorted",
    "unique",
    "returns",
    "raises",
    "throws",
    "default",
    "defaults",
)
# Expected-vs-actual / repro markers.
_EXPECTED_RE = re.compile(
    r"^\s*(?:expected(?:\s+behaviou?r)?|actual(?:\s+behaviou?r)?|"
    r"steps to reproduce|to reproduce|reproduction|repro)\b",
    re.I | re.M,
)
# Numbered repro steps: "1. do X", "2) do Y".
_NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$", re.M)
# Sentence splitter — coarse, deterministic (period/newline boundaries).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

# impl <Trait> for <Type> surface form (rust/trait-style — kept verbatim).
_IMPL_FOR_RE = re.compile(r"\bimpl\s+([A-Za-z_][\w:]*)\s+for\s+([A-Za-z_][\w:]*)")


@dataclass(frozen=True)
class Obligation:
    """One requirement mined from the issue text.

    verbatim_text  — the enclosing sentence / fragment, EXACTLY as written (the
                     un-misdirectable ``issue_verbatim`` provenance).
    kind           — behavior | signature | error | repro.
    symbols        — identifier surface forms named in/near the obligation.
    keywords       — requirement qualifiers (async, optional, returns, ...) —
                     the words anchors.py drops but the spec must keep.
    checkable_forms— deterministic surface forms a drift check can test WITHOUT
                     being wrong about the surface (§5.2 SAFE set): e.g.
                     {"async"} or {"impl Trait for Type"}.  Empty = not checkable.
    """

    verbatim_text: str
    kind: str
    symbols: frozenset[str] = frozenset()
    keywords: frozenset[str] = frozenset()
    checkable_forms: frozenset[str] = frozenset()


@dataclass
class IssueSpec:
    """The decomposed issue: an ordered list of obligations + their union views."""

    obligations: list[Obligation] = field(default_factory=list)
    # union convenience views (telemetry / oracle relevance keys)
    all_symbols: set[str] = field(default_factory=set)
    all_keywords: set[str] = field(default_factory=set)

    def to_serializable(self) -> list[dict]:
        """JSON-ready (for the gt_issue_anchors.json extension)."""
        return [
            {
                "verbatim_text": o.verbatim_text,
                "kind": o.kind,
                "symbols": sorted(o.symbols),
                "keywords": sorted(o.keywords),
                "checkable_forms": sorted(o.checkable_forms),
            }
            for o in self.obligations
        ]


def _idents(text: str) -> set[str]:
    out: set[str] = set()
    for m in _IDENT_RE.finditer(text):
        tok = m.group(1)
        out.add(tok)
        if "." in tok:
            for part in tok.split("."):
                if part and (len(part) >= 3 or part.startswith("_")):
                    out.add(part)
    return out


def _code_symbols(text: str) -> set[str]:
    out: set[str] = set()
    for m in _BACKTICK_RE.finditer(text):
        out |= _idents(m.group(1))
    for m in _FENCE_RE.finditer(text):
        out |= _idents(m.group(1))
    return out


def _qualifier_keywords(fragment: str) -> set[str]:
    low = fragment.lower()
    return {kw for kw in _QUALIFIER_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", low)}


def _checkable(keywords: set[str], verbatim: str) -> set[str]:
    """The SAFE drift-checkable subset (§5.2): only surface forms a deterministic
    check cannot be WRONG about.  Today: the async/await shape and an explicit
    ``impl Trait for Type`` mention.  Everything else stays unchecked (silent)."""
    forms: set[str] = set()
    if "async" in keywords or re.search(r"\basync\b", verbatim, re.I):
        forms.add("async")
    if "await" in keywords or re.search(r"\bawait\b", verbatim, re.I):
        forms.add("await")
    mm = _IMPL_FOR_RE.search(verbatim)
    if mm:
        forms.add(f"impl {mm.group(1)} for {mm.group(2)}")
    return forms


def _classify_kind(sentence: str) -> str | None:
    """The obligation kind, or None if the sentence carries no obligation."""
    has_modal = bool(_MODAL_RE.search(sentence))
    has_behavior = bool(_BEHAVIOR_VERB_RE.search(sentence))
    low = sentence.lower()
    if re.search(r"\b(raise|raises|throw|throws|error|errors?|exception|fails?)\b", low):
        if has_modal or has_behavior:
            return "error"
    # a parenthesized API qualifier with a kept keyword -> signature obligation
    for mm in _API_QUALIFIER_RE.finditer(sentence):
        if _qualifier_keywords(mm.group(2)):
            return "signature"
    if has_modal or has_behavior:
        return "behavior"
    return None


def extract_spec(issue_text: str, max_obligations: int = 40) -> IssueSpec:
    """Decompose an issue body into obligations.  Deterministic; pure regex.

    Rules (all surface-form, §5.1):
      - REQUIREMENT sentences: contain a modal/requirement marker OR a behavior
        verb with an object -> kind behavior/error.
      - API-SHAPE qualifiers: parenthesized spec fragment attached to a named
        symbol with a kept qualifier keyword -> kind signature.
      - NUMBERED repro steps + expected/actual blocks -> kind repro, kept
        verbatim with their enclosing line.
    """
    spec = IssueSpec()
    if not issue_text:
        return spec
    code_syms = _code_symbols(issue_text)
    seen_verbatim: set[str] = set()

    def _add(verbatim: str, kind: str) -> None:
        v = verbatim.strip()
        if not v or v in seen_verbatim:
            return
        seen_verbatim.add(v)
        syms = (_idents(v) & code_syms) or _idents(v)
        # keep only plausible symbol-shaped tokens (CamelCase, snake, dotted, or
        # explicitly code-marked) — drop bare English words unless code-marked.
        # A capitalized SINGLE-HUMP word (Optional, Trace) counts ONLY when it
        # occurs mid-fragment: a fragment-INITIAL capitalized word is English
        # sentence case (The/When/Run/...), and admitting it polluted the
        # obligation symbol sets -> _oracle_focus() relevance gate (LIPI
        # 2026-06-10).  CamelCase-internal (KeyError) is symbol-shaped anywhere.
        lead = re.sub(r"^[^A-Za-z_]+", "", v)
        kept_syms: set[str] = set()
        for s in syms:
            if s in code_syms or "_" in s or "." in s:
                kept_syms.add(s)
            elif any(c.isupper() for c in s[1:]):
                kept_syms.add(s)  # internal capital — CamelCase / acronym
            elif s[:1].isupper() and any(
                m.start() > 0 for m in re.finditer(rf"\b{re.escape(s)}\b", lead)
            ):
                kept_syms.add(s)  # capitalized, seen mid-fragment
        kws = _qualifier_keywords(v)
        # also harvest modal/behavior markers as keywords for relevance.
        for mm in _MODAL_RE.finditer(v):
            kws.add(mm.group(1).lower())
        spec.obligations.append(
            Obligation(
                verbatim_text=v,
                kind=kind,
                symbols=frozenset(kept_syms),
                keywords=frozenset(kws),
                checkable_forms=frozenset(_checkable(kws, v)),
            )
        )
        spec.all_symbols |= kept_syms
        spec.all_keywords |= kws

    # 1. numbered repro steps (verbatim line)
    for m in _NUMBERED_STEP_RE.finditer(issue_text):
        _add(m.group(0), "repro")

    # 2. expected/actual + repro header lines (and the line after)
    for m in _EXPECTED_RE.finditer(issue_text):
        line_start = issue_text.rfind("\n", 0, m.start()) + 1
        line_end = issue_text.find("\n", m.end())
        if line_end == -1:
            line_end = len(issue_text)
        _add(issue_text[line_start:line_end], "repro")

    # 3. requirement / behavior / signature sentences
    for sent in _SENT_SPLIT_RE.split(issue_text):
        sent = sent.strip()
        if not sent or len(sent) < 8:
            continue
        kind = _classify_kind(sent)
        if kind:
            _add(sent, kind)
        if len(spec.obligations) >= max_obligations:
            break

    return spec


__all__ = ["Obligation", "IssueSpec", "extract_spec"]

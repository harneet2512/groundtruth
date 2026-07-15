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
_IDENT_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]+)*)\b"
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```[\w-]*\n(.*?)```", re.S)
# Double-quoted or single-quoted short literals the reporter calls out verbatim.
_QUOTED_RE = re.compile(r"[\"']([^\"'\n]{1,80})[\"']")

# Requirement modals / obligation markers (surface forms, case-insensitive).
# These are English requirement grammar, not domain keywords.
_MODAL_RE = re.compile(
    r"\b(must|must not|should|should not|shall|shall not|needs? to(?=\s+(?:be\b|[A-Za-z_]))|is required|"
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
_API_QUALIFIER_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^()\n]{0,120})\)"
)
# Qualifier keywords WORTH keeping (the requirement words anchors.py drops):
_QUALIFIER_KEYWORDS = (
    "async", "await", "sync", "synchronous", "asynchronous", "optional",
    "required", "nullable", "immutable", "mutable", "readonly", "read-only",
    "deprecated", "lazy", "eager", "abstract", "static", "const", "final",
    "thread-safe", "idempotent", "recursive", "ordered", "sorted", "unique",
    "returns", "raises", "throws", "default", "defaults",
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
    # ── v2 fields (GT_OBLIGATIONS_V2) — all defaulted so every v1 constructor and
    # consumer keeps working byte-identically. See GT_OBLIGATIONS_V2 plan §1.
    clause_id: str = ""            # sha256(normalized verbatim + part_index)[:8]
    modality: str = ""             # mandatory | expected | declarative | descriptive
    modality_strength: int = 0     # 3 / 2 / 1 / 0 (RFC-2119 tiers; ordering + T3)
    subject_symbols: frozenset[str] = frozenset()  # high-specificity exercise keys
    parent_id: str = ""            # clause_id of enclosing compound (provenance)
    part_index: int = 0            # position within the parent compound
    region: str = "normative"      # normative | process | evidence (v2 only)


@dataclass
class IssueSpec:
    """The decomposed issue: an ordered list of obligations + their union views."""
    obligations: list[Obligation] = field(default_factory=list)
    # union convenience views (telemetry / oracle relevance keys)
    all_symbols: set[str] = field(default_factory=set)
    all_keywords: set[str] = field(default_factory=set)

    def to_serializable(self, version: int = 1) -> list[dict]:
        """JSON-ready (for the gt_issue_anchors.json extension).

        ``version=1`` emits EXACTLY the five v1 keys — byte-identical to the
        pre-v2 payload (flag-off golden invariant). ``version=2`` adds the
        GT_OBLIGATIONS_V2 keys; the writer stamps a top-level
        ``obligations_version`` alongside (artifact-wins split-brain rule).
        """
        rows = []
        for o in self.obligations:
            row = {
                "verbatim_text": o.verbatim_text,
                "kind": o.kind,
                "symbols": sorted(o.symbols),
                "keywords": sorted(o.keywords),
                "checkable_forms": sorted(o.checkable_forms),
            }
            if version >= 2:
                row.update(
                    clause_id=o.clause_id,
                    modality=o.modality,
                    modality_strength=o.modality_strength,
                    subject_symbols=sorted(o.subject_symbols),
                    parent_id=o.parent_id,
                    part_index=o.part_index,
                    region=o.region,
                )
            rows.append(row)
        return rows


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
                m.start() > 0
                for m in re.finditer(rf"\b{re.escape(s)}\b", lead)
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


# ═══════════════════════════ GT_OBLIGATIONS_V2 ══════════════════════════════
# v2 extractor — same doctrine as v1 (deterministic, verbatim-preserving, keyed
# on requirement GRAMMAR never domain keywords), higher recall via three new
# grammar classes (EARS conditional-imperatives, clause-initial imperative spec
# verbs, compat markers), atomic clause splitting (arrow mappings, semicolon
# alternative flows — EARS/Gherkin one-checkable-behavior-per-clause), and
# RFC-2119 modality tiers. v1 (`extract_spec`) is untouched: flag-off callers
# are byte-identical by construction.

import hashlib as _hashlib

_V2_BULLET_RE = re.compile(r"^\s*[-*+•]\s+(.+)$")
_V2_BRACKET_TITLE_RE = re.compile(r"^\s*\[[^\]\n]{1,30}\]\s+(.+)$")
_V2_LABEL_LINE_RE = re.compile(r"^([A-Z][A-Za-z /`_-]{1,40}):\s*$")
_V2_LABEL_REST_RE = re.compile(r"^([A-Za-z][^:\n]{0,60}):\s+(.+)$")
_V2_ATX_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")
_V2_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*\n]{1,80})\*\*:?\s*$")
_V2_COMMENT_HEADING_RE = re.compile(
    r"^\s*([A-Za-z][^:\n]{0,60}):\s*<!--.*-->\s*$"
)
_V2_DESCRIPTIVE_SECTION_RE = re.compile(
    r"^(?:describe\s+the\s+bug|bug\s+description|actual(?:\s+behaviou?r)?|"
    r"current(?:\s+behaviou?r)?|summary|"
    r"what\s+do\s+you\s+see\s+instead|additional\s+(?:context|information))$",
    re.I,
)
_V2_PROCESS_SECTION_RE = re.compile(
    r"^(?:(?:what\s+)?steps?\s+(?:can\s+)?reproduce(?:\s+the\s+bug)?|"
    r"to\s+reproduce|reproduction|repro|returns?|output|traceback|logs?|"
    r"environment|versions?|tests?|testing|alternatives?|motivation|checklist|"
    r"i\s+have|what\s+version.*|what\s+runtime.*|"
    r"the\s+author\s+should\s+do\s+the\s+followings?,?\s+if\s+applicable)$",
    re.I,
)
_V2_EXPECTED_SECTION_RE = re.compile(
    r"^(?:(?:what\s+is\s+the\s+)?expected(?:\s+behaviou?r)?|"
    r"desired(?:\s+behaviou?r)?|proposal|proposed(?:\s+behaviou?r)?|"
    r"requirements?|acceptance\s+criteria|solution|fix|"
    r"ideas?\s+of\s+implementation|what\s+changed)$",
    re.I,
)
# Mapping arrows: '->' / '→' ONLY (never '=>', which is lambda syntax and lives
# inside code spans — treating it as a mapping would shred `(items) => result`).
_V2_ARROW_SPLIT_RE = re.compile(r"\s*(?:->|→)\s*")
_V2_COND_RE = re.compile(
    r"^(?:if|when|while|unless|once|after|before|whenever)\b", re.I
)
_V2_IMPERATIVE_RE = re.compile(
    r"^(?:(?:correctly|properly|safely|consistently|gracefully|fully)\s+)?"
    r"(?:add|implement|make|support|provide|introduce|expose|allow|ensure|"
    r"return|raise|skip|run|call|use|keep|preserve|clear|reject|emit|stop|"
    r"retry|default|rename|remove|drop|split|convert|record|create|apply|"
    r"handle|fix|update|replace|refactor)\b",
    re.I,
)
_V2_SIGNATURE_RE = re.compile(
    r"\bsignatures?\b|\bcurried\b|\boverloads?\b|\breturn(?:s|ing)?\s+type\b",
    re.I,
)
_V2_COMPAT_RE = re.compile(
    r"remains?\s+(?:unchanged|readable|valid|intact)|"
    r"still\s+(?:runs?|works?|applies)|continues?\s+to\b|"
    r"(?:not|never)\s+break\b|backward[- ]compat",
    re.I,
)
# v1 behavior verbs + (s|ed|ing) inflections — recall, not new vocabulary.
_V2_BEHAVIOR_RE = re.compile(
    r"\b(?:return|raise|throw|accept|emit|yield|produce|output|reject|resolve|"
    r"expect|ignore|preserve|require|support|handle)(?:s|ed|ing)?\b"
    r"|\bdefaults?\s+to\b|\bfails?\s+with\b|\berrors?\s+out\b",
    re.I,
)
_V2_ERROR_TOKEN_RE = re.compile(
    r"\b[A-Z]\w*(?:Error|Exception|Warning)\b|\braises?\b|\bsignals\.\w+"
)
_V2_MANDATORY_RE = re.compile(
    r"\b(?:must|shall|never|always|cannot|can't|will not|won't)\b", re.I
)
_V2_EXPECTED_MODAL_RE = re.compile(
    r"\b(?:should|expected to|needs? to(?=\s+(?:be\b|[A-Za-z_]))|has to|have to|required)\b", re.I
)
_V2_SUGGESTION_RE = re.compile(
    r"\b(?:i\s+(?:think|suggest|propose|would\s+like|prefer)|"
    r"would\s+be\s+(?:helpful|useful|better|preferable|desirable|great))\b",
    re.I,
)
_V2_REQUEST_MODAL_RE = re.compile(
    r"\b(?:must|shall|should|expected to|needs? to(?=\s+(?:be\b|[A-Za-z_]))|has to|have to|required)\b",
    re.I,
)
_V2_REQUEST_QUESTION_RE = re.compile(
    r"^(?:is\s+it\s+possible\s+to|would\s+it\s+be\s+possible\s+to|"
    r"could\s+(?:you|we)\s+|can\s+(?:you|we)\s+)(.+?)\?\s*$",
    re.I,
)
_V2_OBSERVED_STATE_RE = re.compile(
    r"\b(?:currently|right\s+now|at\s+present|"
    r"i(?:'m|\s+am)\s+(?:getting|seeing|experiencing)|"
    r"still\s+(?:requires?|fails?|throws?|errors?)|"
    r"does(?:n't|\s+not)\s+(?:fit|work|support)|"
    r"not\s+(?:working|supported))\b",
    re.I,
)
_V2_CALL_SHAPE_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def _v2_clause_id(verbatim: str, part_index: int) -> str:
    norm = " ".join(verbatim.split()).lower()
    return _hashlib.sha256(f"{norm}|{part_index}".encode()).hexdigest()[:8]


def _v2_span_ranges(s: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _BACKTICK_RE.finditer(s)]


def _v2_outside_spans(s: str, pos: int, spans: list[tuple[int, int]]) -> bool:
    return not any(a <= pos < b for a, b in spans)


def _v2_split_semicolons(s: str) -> list[str]:
    """Split on ';' outside code spans when both sides are substantive
    (>=12 chars) or the tail opens an alternative flow (otherwise/then/else —
    EARS alternative-flow markers split unconditionally)."""
    spans = _v2_span_ranges(s)
    pieces: list[str] = []
    start = 0
    for i, ch in enumerate(s):
        if ch != ";" or not _v2_outside_spans(s, i, spans):
            continue
        left, right = s[start:i].strip(), s[i + 1:].strip()
        alt = re.match(r"^(?:otherwise|then|else)\b", right, re.I)
        if alt or (len(left) >= 12 and len(right) >= 12):
            if left:
                pieces.append(left)
            start = i + 1
    tail = s[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces or ([s.strip()] if s.strip() else [])


def _v2_is_arrow_mapping(piece: str) -> tuple[str, str] | None:
    """``X -> Y`` (arrow outside code spans, both sides short) -> (lhs, rhs)."""
    spans = _v2_span_ranges(piece)
    m = re.search(r"->|→", piece)
    while m and not _v2_outside_spans(piece, m.start(), spans):
        m = re.search(r"->|→", piece[m.end():])
    if not m:
        return None
    parts = _V2_ARROW_SPLIT_RE.split(piece, maxsplit=1)
    if len(parts) != 2:
        return None
    lhs, rhs = parts[0].strip(), parts[1].strip()
    if not lhs or not rhs or len(lhs) > 60 or len(rhs) > 60:
        return None
    return lhs, rhs


def _v2_subject_symbols(verbatim: str) -> set[str]:
    """High-specificity exercise keys: backticked identifiers, call-shape
    names, dotted names (+their parts). Same symbol-shape discipline as v1
    (_add's kept_syms): code-marked / snake / dotted / internal-capital only."""
    out: set[str] = set()
    ticked: set[str] = set()
    for m in _BACKTICK_RE.finditer(verbatim):
        frag = m.group(1)
        ticked |= _idents(frag)
        for cm in _V2_CALL_SHAPE_RE.finditer(frag):
            out.add(cm.group(1))
    out |= ticked
    for m in re.finditer(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w+)+)\b", verbatim):
        out.add(m.group(1))
        out |= {p for p in m.group(1).split(".") if len(p) >= 3}
    kept: set[str] = set()
    for s in out:
        if s in ticked or "_" in s or "." in s or any(c.isupper() for c in s[1:]):
            kept.add(s)
    # error tokens are always subject-eligible (ValueError, signals.TestError)
    for m in re.finditer(r"\b[A-Z]\w*(?:Error|Exception|Warning)\b", verbatim):
        kept.add(m.group(0))
    return kept


def _v2_classify(decision_text: str) -> tuple[str, str, int] | None:
    """(kind, modality, strength) — grammar classes in fixed precedence."""
    t = decision_text.strip()
    if not t:
        return None
    low = t.lower()
    mandatory = bool(_V2_MANDATORY_RE.search(t))
    expected = bool(_V2_EXPECTED_MODAL_RE.search(t) or _V2_SUGGESTION_RE.search(t))
    behavior = bool(_V2_BEHAVIOR_RE.search(t))
    imperative = bool(_V2_IMPERATIVE_RE.match(t))
    conditional = bool(
        _V2_COND_RE.match(t)
        and "," in t
        and (
            mandatory
            or expected
            or _V2_IMPERATIVE_RE.match(t.split(",", 1)[1].strip() or "")
            or behavior
        )
    )
    qualifies = mandatory or expected or behavior or imperative or conditional

    def _strength() -> tuple[str, int]:
        if mandatory or conditional:
            return "mandatory", 3
        if expected or imperative:
            return "expected", 2
        return "declarative", 1

    # precedence: error > signature > compat > behavior
    if qualifies and re.search(
        r"\b(raise|raises|throw|throws|error|errors?|exception|fails?)\b", low
    ):
        mod, s = _strength()
        return "error", mod, s
    if qualifies and _V2_SIGNATURE_RE.search(t):
        mod, s = _strength()
        return "signature", mod, max(s, 2)
    for mm in _API_QUALIFIER_RE.finditer(t):
        if _qualifier_keywords(mm.group(2)):
            mod, s = _strength() if qualifies else ("expected", 2)
            return "signature", mod, max(s, 2)
    if _V2_COMPAT_RE.search(t):
        mod, s = _strength() if qualifies else ("expected", 2)
        return "compat", mod, max(s, 2)
    if qualifies:
        mod, s = _strength()
        return "behavior", mod, s
    return None


def _v2_heading_text(line: str) -> str | None:
    """Return a normalized structural heading, never its following prose.

    GitHub issue templates commonly use ATX headings, bold-only headings, and
    label-only lines.  Treating those labels as prose created obligations such
    as ``Returns:`` and joined ``Describe the bug`` to the first observation.
    """
    stripped = line.strip()
    match = _V2_ATX_HEADING_RE.match(stripped)
    if match:
        return match.group(1).strip().strip("*_").rstrip(":?!").strip()
    match = _V2_BOLD_HEADING_RE.match(stripped)
    if match:
        return match.group(1).strip().rstrip(":?!").strip()
    match = _V2_COMMENT_HEADING_RE.match(stripped)
    if match:
        return match.group(1).strip().rstrip(":?!").strip()
    match = _V2_LABEL_LINE_RE.match(stripped)
    if match:
        return match.group(1).strip().strip("`*_").rstrip(":?!").strip()
    return None


def _v2_section_role(heading: str) -> str:
    normalized = " ".join(heading.split()).strip()
    if _V2_DESCRIPTIVE_SECTION_RE.fullmatch(normalized):
        return "descriptive"
    if _V2_PROCESS_SECTION_RE.fullmatch(normalized):
        return "process"
    if _V2_EXPECTED_SECTION_RE.fullmatch(normalized):
        return "expected"
    return "neutral"


@dataclass(frozen=True)
class SpecRegion:
    """One heading-delimited issue region classified from document structure.

    The classification describes the author's document region, not whether an
    individual sentence happens to contain a modal verb.  This keeps repro
    instructions and observed failures out of the normative denominator.
    """

    kind: str
    heading: str
    text: str


def classify_spec_v2_regions(issue_text: str) -> tuple[SpecRegion, ...]:
    """Classify headed v2 regions as normative, process, or evidence.

    Unknown headings fail closed to evidence.  Unheaded prose remains the
    extractor's title/neutral grammar path and is not fabricated into a
    structural region.
    """
    regions: list[SpecRegion] = []
    heading = ""
    kind = "evidence"
    body: list[str] = []
    in_fence = False

    def flush() -> None:
        if heading:
            regions.append(SpecRegion(kind, heading, "\n".join(body).strip()))
        body.clear()

    for raw in (issue_text or "").splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            if heading:
                body.append(raw)
            continue
        candidate = None if in_fence else _v2_heading_text(raw)
        if candidate is not None:
            flush()
            heading = candidate
            role = _v2_section_role(candidate)
            kind = {
                "expected": "normative",
                "process": "process",
                "descriptive": "evidence",
            }.get(role, "evidence")
            continue
        if heading:
            body.append(raw)
    flush()
    return tuple(regions)


def extract_spec_v2(issue_text: str, max_obligations: int = 64) -> IssueSpec:
    """GT_OBLIGATIONS_V2 extractor: Stage A (blocks) / B (atomic) / C (classify).

    Deterministic, format-agnostic, verbatim-preserving. Produces Obligations
    carrying the v2 fields (clause_id, modality, subject_symbols, provenance).
    """
    spec = IssueSpec()
    if not issue_text:
        return spec
    seen: set[str] = set()

    def _emit(verbatim: str, kind: str, modality: str, strength: int,
              parent_id: str, part_index: int,
              context_symbols: set[str] | None = None) -> None:
        v = verbatim.strip().rstrip(".")
        if not v or len(v) < 8:
            return
        key = " ".join(v.split()).lower()
        if key in seen or len(spec.obligations) >= max_obligations:
            return
        seen.add(key)
        subj = _v2_subject_symbols(v) | (context_symbols or set())
        broad = _idents(v)
        kws = _qualifier_keywords(v)
        for mm in _MODAL_RE.finditer(v):
            kws.add(mm.group(1).lower())
        spec.obligations.append(Obligation(
            verbatim_text=v,
            kind=kind,
            symbols=frozenset(broad),
            keywords=frozenset(kws),
            checkable_forms=frozenset(_checkable(kws, v)),
            clause_id=_v2_clause_id(v, part_index),
            modality=modality,
            modality_strength=strength,
            subject_symbols=frozenset(subj),
            parent_id=parent_id,
            part_index=part_index,
            region="normative",
        ))
        spec.all_symbols |= set(subj) | broad
        spec.all_keywords |= kws

    def _process_candidate(text: str, bullet_label_syms: set[str],
                           bullet_fallback: tuple[str, str, int] | None,
                           section_role: str = "neutral") -> None:
        parent = _v2_clause_id(text, 0)
        for idx, piece in enumerate(_v2_split_semicolons(text)):
            mapping = _v2_is_arrow_mapping(piece)
            if mapping:
                _emit(
                    piece,
                    "error" if _V2_ERROR_TOKEN_RE.search(mapping[1]) else "behavior",
                    "mandatory", 3, parent, idx, bullet_label_syms,
                )
                continue
            decision = piece
            lm = _V2_LABEL_REST_RE.match(piece)
            # label-strip GUARD: an imperative/conditional head is the clause
            # itself, never a label ("use hyper directly: works, but…" — the
            # gate caught the stripper eating the imperative head).
            if (lm and len(lm.group(1).split()) <= 6
                    and not _V2_IMPERATIVE_RE.match(lm.group(1))
                    and not _V2_COND_RE.match(lm.group(1))):
                decision = lm.group(2)
            decision = re.sub(r"^(?:otherwise|then|else)[\s,]+", "", decision, flags=re.I)
            request_question = _V2_REQUEST_QUESTION_RE.match(decision)
            if request_question:
                decision = request_question.group(1).strip()
            elif decision.rstrip().endswith("?"):
                continue
            cls = _v2_classify(decision)
            if cls is None and bullet_fallback is not None:
                cls = bullet_fallback
            if cls is None and section_role == "expected" and len(decision) >= 8:
                cls = ("behavior", "expected", 2)
            if cls is None:
                continue
            kind, modality, strength = cls
            if request_question and strength < 2:
                modality, strength = "expected", 2
            if section_role == "process":
                continue
            if _V2_OBSERVED_STATE_RE.search(decision) and not (
                _V2_REQUEST_MODAL_RE.search(decision)
                or _V2_SUGGESTION_RE.search(decision)
                or _V2_IMPERATIVE_RE.match(decision)
            ):
                continue
            # Current-state/reproduction prose is evidence about the defect,
            # not a completion requirement.  Retain only explicit requirement
            # grammar there; an imperative repro step ("Run ...") is not one.
            if section_role == "descriptive" and not (
                _V2_REQUEST_MODAL_RE.search(decision)
                or _V2_SUGGESTION_RE.search(decision)
            ):
                continue
            # The first unheaded paragraph is normally a GitHub title/current-
            # state synopsis.  Declarative behavior there is not an obligation,
            # while an instruction-style title ("Add ...") remains expected.
            if section_role == "title" and strength <= 1:
                continue
            if section_role == "expected" and strength < 2:
                modality, strength = "expected", 2
            _emit(piece, kind, modality, strength, parent, idx, bullet_label_syms)

    # ── Stage A: block segmentation ──────────────────────────────────────────
    in_fence = False
    label_syms: set[str] = set()
    prose: list[str] = []
    section_role = "title"
    first_block = True
    parity_candidates: list[tuple[str, str]] = []

    def _flush_prose() -> None:
        if not prose:
            return
        block = " ".join(prose)
        prose.clear()
        role = section_role
        for sent in _SENT_SPLIT_RE.split(block):
            sent = sent.strip()
            if sent and len(sent) >= 8:
                parity_candidates.append((sent, role))
                _process_candidate(sent, set(), None, role)

    for raw in issue_text.split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_fence = not in_fence
            _flush_prose()
            continue
        if in_fence:
            continue
        if not line.strip():
            _flush_prose()
            label_syms = set()
            if first_block:
                first_block = False
                if section_role == "title":
                    section_role = "neutral"
            continue
        heading = _v2_heading_text(line)
        if heading is not None:
            _flush_prose()
            first_block = False
            role = _v2_section_role(heading)
            if role != "neutral":
                section_role = role
                label_syms = set()
            else:
                section_role = "neutral"
                label_syms = _idents(heading)
            continue
        title_match = _V2_BRACKET_TITLE_RE.match(line)
        if title_match:
            _flush_prose()
            first_block = False
            content = title_match.group(1).strip()
            if _v2_classify(content) is not None:
                parity_candidates.append((content, "expected"))
                _process_candidate(content, set(), None, "expected")
            section_role = "neutral"
            label_syms = set()
            continue
        bm = _V2_BULLET_RE.match(line)
        if bm:
            _flush_prose()
            content = bm.group(1).strip()
            # bullet-level fallback: if the bullet AS A WHOLE — or ANY of its
            # semicolon sub-pieces — classifies, non-classifying sub-pieces
            # inherit that class (sub-requirements of one requirement line;
            # covers "Label (qualifier): piece; imperative piece; …" bullets).
            whole = content
            wl = _V2_LABEL_REST_RE.match(content)
            if wl and len(wl.group(1).split()) <= 6:
                whole = wl.group(2)
            fallback = _v2_classify(whole)
            if fallback is None:
                for _p in _v2_split_semicolons(whole):
                    _p = re.sub(r"^(?:otherwise|then|else)[\s,]+", "", _p, flags=re.I)
                    _pl = _V2_LABEL_REST_RE.match(_p)
                    if _pl and len(_pl.group(1).split()) <= 6:
                        _p = _pl.group(2)
                    fallback = _v2_classify(_p)
                    if fallback is not None:
                        break
            parity_candidates.append((content, section_role))
            _process_candidate(content, set(label_syms), fallback, section_role)
            continue
        nm = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if nm:
            _flush_prose()
            content = nm.group(1).strip()
            cls = _v2_classify(content)
            if cls:
                parity_candidates.append((content, section_role))
                _process_candidate(content, set(label_syms), None, section_role)
            else:
                # Numbered items in a requirement section can be useful
                # checklist rows; numbered repro steps are observations only.
                if section_role not in {"descriptive", "process"}:
                    _emit(line.strip(), "repro", "descriptive", 0,
                          _v2_clause_id(line.strip(), 0), 0)
            continue
        prose.append(line.strip())
    _flush_prose()

    # ── section-preserving v1 parity sweep: retain v1's useful classifier
    # recall only inside the same structural role assigned above.  Raw parity
    # was unsound: it resurrected fenced/process/current-state false positives
    # that Stage A had deliberately rejected (for example ``Returns:``).
    covered = [" ".join(o.verbatim_text.split()).lower() for o in spec.obligations]
    for sent, role in parity_candidates:
        sent = sent.strip()
        if not sent or len(sent) < 8:
            continue
        kind = _classify_kind(sent)
        if not kind:
            continue
        if role == "process":
            continue
        decision = sent
        lm = _V2_LABEL_REST_RE.match(sent)
        if lm and len(lm.group(1).split()) <= 6:
            decision = lm.group(2)
        if decision.rstrip().endswith("?"):
            continue
        if _V2_OBSERVED_STATE_RE.search(decision) and not (
            _V2_REQUEST_MODAL_RE.search(decision)
            or _V2_SUGGESTION_RE.search(decision)
            or _V2_IMPERATIVE_RE.match(decision)
        ):
            continue
        if role == "descriptive" and not (
            _V2_REQUEST_MODAL_RE.search(decision)
            or _V2_SUGGESTION_RE.search(decision)
        ):
            continue
        if role == "title" and not (
            _V2_MANDATORY_RE.search(decision)
            or _V2_EXPECTED_MODAL_RE.search(decision)
            or _V2_SUGGESTION_RE.search(decision)
            or _V2_IMPERATIVE_RE.match(decision)
        ):
            continue
        key = " ".join(sent.rstrip(".").split()).lower()
        if any((key in c) or (c in key) for c in covered):
            continue
        modality, strength = ("expected", 2) if role == "expected" else ("declarative", 1)
        if _V2_MANDATORY_RE.search(decision):
            modality, strength = "mandatory", 3
        elif _V2_EXPECTED_MODAL_RE.search(decision) or _V2_SUGGESTION_RE.search(decision):
            modality, strength = "expected", 2
        _emit(sent, kind, modality, strength, _v2_clause_id(sent, 0), 0)
        covered.append(key)

    return spec


__all__ = [
    "Obligation",
    "IssueSpec",
    "SpecRegion",
    "classify_spec_v2_regions",
    "extract_spec",
    "extract_spec_v2",
]

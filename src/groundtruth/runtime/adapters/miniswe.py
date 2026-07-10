"""mini-swe-agent seam adapter for the GT Gateway (W2 — the one-call wiring).

The mini seam (``artifact_deepswe/gt_mini_patch.py``) completes one tool
observation with deterministic FACTS by making ONE ``gateway.augment()`` call and
appending the result BYTE-PRESERVING into the SAME observation the model already
reads. This module owns every decision-shaped step of that pipeline so it is
unit-testable in isolation, WITHOUT importing gt_mini_patch (importing it runs
runtime monkeypatches):

    normalize_event  raw (cmd, output, returncode, index) -> gateway.ToolEvent
    augment          (gateway.augment — the ONE call, done by the caller)
    arbitrate        many envelopes -> at most ONE (the per-turn dose, ABI §2)
    render_envelope  one envelope -> model-facing bytes (tagged default / native)
    fits_budget      TITO law 8 — refuse a partial/over-budget delta whole
    seal_delivery    stamp TITO metadata + extend the per-episode hash-chain
    update_receipts  TITO law 7 — promote receipt_state (delivered->referenced->acted)

PURE · DETERMINISTIC · LLM-FREE · stdlib + gateway/evidence_envelope only. No time,
no randomness, no I/O. The observation splice (mutating the mini's ``out`` dict) and
the leak-guard on the rendered bytes stay on the SEAM side — this module never
touches a harness object and never renders a ``<gt-*>`` tag it cannot leak-check.

TITO CONTRACT (evidence_envelope §"TITO"): every delivery is byte/prefix-preserving.
:func:`seal_delivery` records ``rendered_bytes_hash`` = sha256 of the bytes ACTUALLY
appended and threads ``parent_observation_hash`` through
``evidence_envelope.chain_hash`` over the episode's delivery sequence, so a replayed
trajectory reproduces the exact appended bytes (law 5/6). The chain is PER-EPISODE:
a fresh attempt starts at genesis and re-sees facts (ABI dedup keys are
episode-invariant by design; the chain head is not).
"""
from __future__ import annotations

import dataclasses
import hashlib
import re

from groundtruth.runtime.evidence_envelope import (
    RECEIPT_ACTED,
    RECEIPT_CAUSAL,
    RECEIPT_DELIVERED,
    RECEIPT_NONE,
    RECEIPT_REFERENCED,
    RECEIPT_RESOLVED_STATE,
    EvidenceEnvelope,
    chain_hash,
)
from groundtruth.runtime.evidence_envelope import (
    HYPOTHESIS,
    INFO,
    VERIFIED,
    WARNING,
)
from groundtruth.runtime.gateway import ToolEvent, classify_command

__all__ = [
    "normalize_event",
    "arbitrate",
    "render_envelope",
    "fits_budget",
    "seal_delivery",
    "update_receipts",
    "DEF_FACTS_KINDS",
    "DEFAULT_MAX_DELTA_CHARS",
]

# The def-facts kinds ROUTED to the mini's own ``_fmt_def_facts`` /
# ``_fmt_def_facts_native`` via the injected ``def_facts_renderer`` (never
# re-implemented here). P5 honesty note (bounce 2026-07-10): the reuse HOLDS for
# ``def_ref_partition`` (fact_id = the resolved symbol -> the renderer re-resolves
# it). For ``name_fold`` the fact_id is the ORIGINAL unresolved spelling, so the
# injected renderer abstains ('' -> generic fallback — correct-or-quiet, the fold
# note survives in the payload). For ``wrong_surface`` the renderer emits the plain
# def-facts block (the def sites), dropping the wrong-surface note — acceptable:
# the def site IS the actionable fact. Absent a renderer (host/unit tests without
# the mini loaded), all three render generic.
DEF_FACTS_KINDS = frozenset({"def_ref_partition", "name_fold", "wrong_surface"})

# TITO law 8 default budget: a delta longer than this is DROPPED WHOLE (never a
# partial/fabricated splice). The seam may pass a tighter remaining budget.
DEFAULT_MAX_DELTA_CHARS = 4000


# --------------------------------------------------------------------------- #
# 1. normalize — raw seam values -> the gateway's ONE interception unit
# --------------------------------------------------------------------------- #
def normalize_event(
    command: str,
    output: str,
    returncode: "int | None",
    action_index: int,
    *,
    cwd: str = "",
    changed_files: "tuple[str, ...]" = (),
    edit_before_after: "dict | None" = None,
    covering: "object | None" = None,
) -> ToolEvent:
    """Build the gateway :class:`ToolEvent` from the mini's raw per-turn values.

    The kind is classified by ``gateway.classify_command`` (the segment-head rule
    proven byte-equivalent to the mini's own parser), so the seam and a direct
    ``augment`` call agree on the event shape."""
    return ToolEvent(
        kind=classify_command(command or ""),
        command=command or "",
        output=output or "",
        exit_status=(None if returncode is None else int(returncode)),
        cwd=cwd or "",
        changed_files=tuple(changed_files or ()),
        action_index=int(action_index),
        edit_before_after=edit_before_after,
        covering=covering,
    )


# --------------------------------------------------------------------------- #
# 2. arbitrate — the per-turn dose (<= 1 steer), deterministic
# --------------------------------------------------------------------------- #
# Severity of a produced FACT class (ABI §2 ordering, projected onto the
# envelope's evidence_type). Higher wins the single dose. An unknown kind gets a
# low floor so a new producer never silently out-ranks a verified world-fact.
_EVIDENCE_TYPE_RANK: dict[str, int] = {
    "covering_verdict": 60,       # executed world-fact (the repo's own RED)
    "signature_mismatch": 50,     # edit-bound contract break
    "wrong_surface": 45,          # every hit is a test/vendored copy
    "trace_frame": 40,            # deepest in-repo stack frame
    "def_ref_partition": 35,      # def/ref partition on an ambiguous/flood grep
    "name_fold": 34,              # indexed under a fold variant
    "companion_surface": 30,      # registration/companion gap
    "missing_role": 22,           # change_surface missing role (hypothesis)
    "body_concept": 20,           # vocabulary-gap function bodies
    "new_file_destination": 15,   # feature-add destination (hypothesis)
    "cochange_partner": 12,       # co-change partner
}
_TIER_RANK: dict[str, int] = {VERIFIED: 3, WARNING: 2, INFO: 1, HYPOTHESIS: 0}


def _priority(env: EvidenceEnvelope) -> tuple:
    """A TOTAL deterministic order over envelopes (higher = deliver first).

    Ranked by evidence-type severity, then tier, then confidence, then the stable
    ``dedup_key`` so two facts with identical severity resolve deterministically
    (no set-iteration / no time)."""
    et = env.evidence_type or ""
    base = _EVIDENCE_TYPE_RANK.get(et)
    if base is None:  # family prefix (e.g. "missing_role:registry")
        base = _EVIDENCE_TYPE_RANK.get(et.split(":", 1)[0], 10)
    return (base, _TIER_RANK.get(env.tier, 1), float(env.confidence), env.dedup_key)


def arbitrate(envelopes: "list[EvidenceEnvelope]") -> "EvidenceEnvelope | None":
    """The per-turn dose: at most ONE envelope (ABI §2 — <=1 steer/turn). Returns
    the highest-priority envelope, or ``None`` when the list is empty. augment()
    already dedup-suppressed repeats via the per-episode chain, so this only picks
    the winner among the survivors."""
    if not envelopes:
        return None
    return max(envelopes, key=_priority)


# --------------------------------------------------------------------------- #
# 3. render — one envelope -> model-facing bytes
# --------------------------------------------------------------------------- #
def render_envelope(
    env: EvidenceEnvelope,
    *,
    native: bool,
    def_facts_renderer: "object | None" = None,
) -> str:
    """Render ONE envelope to the model-facing string that will be appended.

    Def-facts kinds ROUTE to the mini's own formatter (``def_facts_renderer`` — the
    injected ``_fmt_def_facts``, which itself dispatches to ``_fmt_def_facts_native``
    under ``GT_POST_SEARCH_NATIVE``), so the gateway's post_search bytes equal the
    pre-wired mini's — never a re-implementation. The byte-equal reuse is PROVEN for
    ``def_ref_partition``; ``name_fold`` falls back generic (the renderer abstains on
    the unresolved spelling — see ``DEF_FACTS_KINDS``). Other kinds render generically:
    tag-free under ``native`` (world-fact voice), a single ``<gt-fact>`` wrapper by
    default. The leak-guard on the result lives on the seam (it owns the
    native_render predicates); this function only assembles deterministic bytes."""
    if env.evidence_type in DEF_FACTS_KINDS and def_facts_renderer is not None:
        try:
            rendered = def_facts_renderer(env)
        except Exception:  # noqa: BLE001 — a renderer fault falls back to generic
            rendered = ""
        if rendered:
            return rendered
    return _render_generic(env, native=native)


def _render_generic(env: EvidenceEnvelope, *, native: bool) -> str:
    body = [ln for ln in env.payload if ln]
    prov = [f"{f}:{ln}" for f, ln in env.provenance if f]
    # P3 (bounce 2026-07-10): BOTH branches dedup provenance rows already embedded
    # in a payload line — the tagged form no longer repeats `a/x.py:7` when the
    # body says "deepest in-repo frame: a/x.py:7 in f" (parity with native).
    extra = [p for p in prov if not any(p in b for b in body)]
    if native:
        # world-fact voice: tag-free. Payload lines first (already human-readable),
        # then any provenance row not already embedded in a payload line.
        lines = body + extra
        return ("\n".join(lines) + "\n") if lines else ""
    if not body and not extra:
        return ""
    head = f'<gt-fact kind="{env.evidence_type}">'
    lines = [head, *body, *extra, "</gt-fact>"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 4. TITO law 8 — never append a partial/over-budget delta
# --------------------------------------------------------------------------- #
def fits_budget(delta: str, *, max_delta_chars: int = DEFAULT_MAX_DELTA_CHARS) -> bool:
    """True iff ``delta`` is non-empty and fits the delivery budget WHOLE. Law 8: a
    delta that would be truncated at the observation boundary is dropped ENTIRELY —
    a fabricated/partial splice is never appended. The seam passes the remaining
    observation budget so a near-full observation refuses the delta rather than
    clipping it."""
    return bool(delta) and len(delta) <= int(max_delta_chars)


# --------------------------------------------------------------------------- #
# 5. seal — stamp TITO metadata + extend the per-episode hash-chain
# --------------------------------------------------------------------------- #
def seal_delivery(
    env: EvidenceEnvelope,
    *,
    episode_id: str,
    event_id: str,
    parent_hash: str,
    rendered_bytes: bytes,
    renderer_id: str,
    dedup_chain: "set[str] | None" = None,
) -> "tuple[EvidenceEnvelope, str]":
    """Stamp a DELIVERED copy of ``env`` and return ``(sealed_copy, new_chain_head)``.

    ``rendered_bytes`` are the bytes ACTUALLY appended into the observation:
    ``rendered_bytes_hash`` = their sha256 (law 6, the replay check) and the chain
    advances via ``chain_hash(parent_hash, rendered_bytes)`` (law 5). The copy is a
    ``dataclasses.replace`` — fact identity (producer/type/target/fact_id/payload/
    provenance and thus ``dedup_key``) is UNTOUCHED, so the sealed copy still passes
    ``evidence_envelope.validate``. ``receipt_state`` starts at ``delivered`` (law 7,
    level 1) with the delivery-evidence hash validate requires.

    THE DELIVERY COMMIT (F1, bounce 2026-07-10): when ``dedup_chain`` (the episode's
    ``delivered_dedup`` set) is passed, the fact's ``dedup_key`` is stamped HERE — at
    the seal, the seam-visible commit — never at production. ``gateway.augment`` only
    READS the chain, so a produced-but-dropped fact (arbitration loss, leak-guard
    drop, law-8 budget drop) is deferred and re-offered, never destroyed."""
    rbh = hashlib.sha256(rendered_bytes).hexdigest()
    new_head = chain_hash(parent_hash, rendered_bytes)
    if dedup_chain is not None:
        dedup_chain.add(env.dedup_key)
    sealed = dataclasses.replace(
        env,
        episode_id=str(episode_id or ""),
        event_id=str(event_id or ""),
        parent_observation_hash=parent_hash or "",
        rendered_bytes_hash=rbh,
        renderer_id=str(renderer_id or ""),
        receipt_state=RECEIPT_DELIVERED,
        delivery_reason="gateway_augment",
        suppression_reason="",
    )
    return sealed, new_head


# --------------------------------------------------------------------------- #
# 6. receipts — TITO law 7 reward-attribution ladder (levels 1-3 live)
# --------------------------------------------------------------------------- #
_RECEIPT_ORDER: dict[str, int] = {
    RECEIPT_NONE: 0,
    RECEIPT_DELIVERED: 1,
    RECEIPT_REFERENCED: 2,
    RECEIPT_ACTED: 3,
    RECEIPT_RESOLVED_STATE: 4,
    RECEIPT_CAUSAL: 5,
}


def _delivered_entities(env: EvidenceEnvelope) -> "list[tuple[str, str]]":
    """The typed entities a later message/action can REFERENCE or ACT on:
    ``("path", ...)`` for the target and each provenance file (full repo-relative
    path — a bare basename is NEVER derived from a pathed value, F2), and
    ``("symbol", fact_id)`` for the producer-natural symbol (a path-shaped fact_id
    is typed path). Deterministic, de-duplicated, non-empty values only."""
    ents: list[tuple[str, str]] = []

    def add(kind: str, v: str) -> None:
        v = (v or "").strip()
        if v and (kind, v) not in ents:
            ents.append((kind, v))

    add("path", env.target)
    fid = (env.fact_id or "").strip()
    add("path" if "/" in fid.replace("\\", "/") else "symbol", fid)
    for f, _ln in env.provenance:
        add("path", f)
    return ents


# Path-shaped tokens in free text/commands: slash-joined runs (an optional
# leading '/' is CAPTURED — R2: absolute vs relative prefix must be
# distinguishable), or a bare dotted filename (util.py). Used to compare
# DELIVERED path entities against the turn's text on token boundaries, never
# raw substring (F2).
_TEXT_PATH_TOKEN_RE = re.compile(r"/?[\w.+\-]+(?:/[\w.+\-]+)+|[\w+\-]+\.[A-Za-z0-9_]+")


def _path_entity_matches(entity: str, text: str) -> bool:
    """True iff the DELIVERED path ``entity`` matches a path token of ``text``:
    exact relpath; a suffix form ending in ``/<entity>`` ONLY for an ABSOLUTE
    token (R2, micro-bounce 2026-07-10: a container/absolute prefix like
    ``/testbed/`` legitimately re-roots the SAME repo-relative file, but a
    sibling-RELATIVE prefix like ``backup/`` names a DIFFERENT file — a clone —
    and must not match); or — ONLY when the delivered entity is basename-only
    (carries no directory to contradict) — basename equality. A pathed entity
    never matches a same-basename file in a DIFFERENT directory (F2)."""
    ent = (entity or "").replace("\\", "/").strip().lstrip("./")
    if not ent:
        return False
    basename_only = "/" not in ent
    for tok in _TEXT_PATH_TOKEN_RE.findall((text or "").replace("\\", "/")):
        absolute = tok.startswith("/")
        tok = tok.lstrip("./")
        if tok == ent or (absolute and tok.endswith("/" + ent)):
            return True
        if basename_only and tok.rsplit("/", 1)[-1] == ent:
            return True
    return False


def _symbol_entity_matches(entity: str, text: str) -> bool:
    """True iff ``entity`` appears in ``text`` as a WHOLE identifier token —
    word-boundary on identifier chars, so ``get`` never matches inside ``target``
    and ``run`` never matches inside ``running`` (F2). The boundary class is
    ``\\w`` — UNICODE-aware (R1, micro-bounce 2026-07-10): the ASCII class let
    ``café`` match inside ``écafé`` (é was not "identifier" to the lookaround),
    inflating receipts on unicode identifiers; ``\\w`` is the strict superset."""
    ent = (entity or "").strip()
    if len(ent) < 2:
        return False  # degenerate 1-char "symbols" are noise, never a receipt
    pat = re.compile(r"(?<!\w)" + re.escape(ent) + r"(?!\w)")
    return bool(pat.search(text or ""))


def _entity_matches(kind: str, entity: str, text: str) -> bool:
    if kind == "path":
        return _path_entity_matches(entity, text)
    return _symbol_entity_matches(entity, text)


def update_receipts(
    deliveries: "list[EvidenceEnvelope]",
    *,
    observed_text: str = "",
    next_action_cmd: str = "",
) -> "list[EvidenceEnvelope]":
    """Promote each recorded delivery's ``receipt_state`` from THIS turn's evidence
    (law 7, levels 1-3):

      * level 2 REFERENCED — a later message NAMES a delivered entity (``observed_text``).
      * level 3 ACTED — the next tool action TARGETS a delivered entity (``next_action_cmd``).

    ACTED dominates REFERENCED. Monotone (never downgrades). Cheap and deterministic
    — no graph, no time — but NEVER raw substring (F2, bounce 2026-07-10): symbols
    match on identifier word boundaries; paths match whole path tokens (full relpath
    or a ``/``-prefixed form; basename equality only for a basename-only entity), so
    ``get``⊂``target``, ``run``⊂``running``, and a same-basename file in a different
    directory can no longer inflate a receipt. Returns a NEW list of copies
    (``dataclasses.replace``); the input is not mutated."""
    otext = observed_text or ""
    cmd = next_action_cmd or ""
    out: list[EvidenceEnvelope] = []
    for env in deliveries:
        cur = _RECEIPT_ORDER.get(env.receipt_state, 0)
        ents = _delivered_entities(env)
        acted = any(_entity_matches(k, e, cmd) for k, e in ents)
        referenced = any(_entity_matches(k, e, otext) for k, e in ents)
        new_state = env.receipt_state
        if acted and cur < _RECEIPT_ORDER[RECEIPT_ACTED]:
            new_state = RECEIPT_ACTED
        elif referenced and cur < _RECEIPT_ORDER[RECEIPT_REFERENCED]:
            new_state = RECEIPT_REFERENCED
        if new_state != env.receipt_state:
            env = dataclasses.replace(env, receipt_state=new_state)
        out.append(env)
    return out

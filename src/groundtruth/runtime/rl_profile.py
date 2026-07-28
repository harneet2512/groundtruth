"""Official RL profile resolver — B-16 / B-17.

Problem (verifyandobserve.md rows B-16/B-17, §4 "Official RL profile"):
the whole RL-adherence stack defaults OFF in the official ``deepswe_full.yml``
harness, and six of its member flags have NO workflow-dispatch input, so they
expand ``unset -> "0"`` and the submit gate + native forms are unreachable.

Fix: ONE versioned toggle ``GT_RL_PROFILE``. When set (e.g. ``=1``) it activates
the coherent member set; an EXPLICITLY-set member flag overrides the profile so
per-flag control still works; if the profile is requested but a member capability
is unavailable the harness ABORTS before model spend (fail-closed); and when the
profile is UNSET the transform is a strict no-op (byte-identical to today).

This module is PURE + deterministic (no I/O, no LLM). The two library verbs are
unit-testable without running the workflow:

  * ``resolve_profile(env) -> dict[str, str]`` — the member flag map to export.
  * ``preflight(env, available) -> list[str]`` — members to abort on (empty = OK).

A thin ``main()`` CLI (``python -m groundtruth.runtime.rl_profile --emit-exports``)
is what ``artifact_deepswe/gt_integration/gt_ae_block.sh`` sources: it prints
``export GT_X=...`` lines on success and exits non-zero (printing
``GT_RL_PREFLIGHT_ABORT: ...``) when the requested profile is partially
unavailable — the shell aborts the run before the paid ``pier run``.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional, Sequence

# ── The versioned profile registry ───────────────────────────────────────────
# ``GT_RL_PROFILE=<version>`` -> the coherent member set that version activates.
# Source of truth: `.claude/reports/verifyandobserve.md` §4. Adding a versioned
# profile here is the ONLY way to extend the toggle; unknown versions fail closed.
_PROFILE_1_MEMBERS: frozenset[str] = frozenset(
    {
        "GT_GATEWAY",
        "GT_GATEWAY_NATIVE",
        "GT_STEER_NATIVE",
        "GT_LANE_ENVELOPE",
        "GT_EDIT_CHECK",
        "GT_VERIFY_EXECUTE",
        "GT_D7_RELATEDNESS",
        "GT_OBLIGATION_FRESHNESS",
        # 2026-07-10 seam flags — the mode-conditioned/bilateral contract (B-19/B-20)
        # and the edit before/after bridges (B-3). Forwarded in gt_ae_block.sh; part
        # of the coherent RL-adherence stack the profile activates.
        "GT_CONTRACT_MODE",
        "GT_CONTRACT_BILATERAL",
        "GT_GATEWAY_EDIT_BRIDGES",
        "GT_POST_SEARCH_NATIVE",
        # Task #63 (2026-07-12) — the SCOPE-surface FORM arm (<gt-scope> tag -> native
        # compiler `note:` constraint via render_scope_constraint_native). SAME FORM-arm
        # family as GT_POST_SEARCH_NATIVE / GT_GATEWAY_NATIVE / GT_STEER_NATIVE; read
        # in-container by gt_mini_patch._consensus_scope_block and forwarded in
        # gt_ae_block.sh. Default-OFF byte-identical. No _MEMBER_CAPABILITY_MODULE entry
        # (import-isolated native render, unmapped-by-convention like GT_POST_SEARCH_NATIVE
        # — never a FALSE abort).
        "GT_SCOPE_NATIVE",
        # ITEM 1-6 (2026-07-13) — the RL-native FORM sweep over the remaining tagged model-facing
        # surfaces. SAME FORM-arm family as GT_POST_SEARCH_NATIVE / GT_SCOPE_NATIVE / GT_STEER_NATIVE
        # (a per-surface tag->native FORM flip): <gt-contract> -> native compiler diagnostics + rg
        # caller rows (GT_CONTRACT_NATIVE); <gt-evidence> -> bare path:line:code rows
        # (GT_EVIDENCE_NATIVE); <gt-nudge>x7 -> body-only imperative (GT_NUDGE_NATIVE); the brief
        # obligations block -> a plain `- [ ]` checklist (GT_BRIEF_NATIVE, read at brief-gen). Plus
        # GT_INSEAM_METRICS — host-side in-seam instrumentation (per-fact-class eligible counter +
        # tier/conf authority stamps; NEVER model-visible, zero observation bytes). All default-OFF
        # byte-identical in-code; read in gt_mini_patch / v1r_brief. No _MEMBER_CAPABILITY_MODULE
        # entry (import-isolated native render / seam code property, unmapped-by-convention like
        # GT_SCOPE_NATIVE — never a FALSE abort). Forwarded in gt_ae_block.sh; pinned in
        # tests/runtime/test_rl_profile.py::MEMBERS + the AE-forward completeness pin.
        "GT_CONTRACT_NATIVE",
        "GT_EVIDENCE_NATIVE",
        "GT_NUDGE_NATIVE",
        "GT_BRIEF_NATIVE",
        "GT_INSEAM_METRICS",
    }
)

# Super-Mode (Profile-2) members beyond Profile-1. SM-0 owns GT_REGISTRY_ENFORCE; SM-3
# (2026-07-11) activates four BUILT-but-DARK engines; SM-4 re-lights GT_EDIT_OVERLAY now that
# it carries model-facing value (the episode-overlay stale-drop). Each flag is a real wired
# call site in gt_mini_patch (submit-gate certificate / per-turn hypothesis classification /
# progressive verification ladder / transactional edit overlay + episode-overlay freshness).
# Each flag's capability is proven at preflight (typed manifest, 2026-07-12): a runtime-module member
# fails CLOSED via find_spec; a SUBSTRATE-PROPERTY member (GT_CONTENT_LEG/GT_SEM_BODY/GT_L6_FRESH)
# fails CLOSED against a required RECEIPT (_MEMBER_CAPABILITY_RECEIPT below) so a stale
# substrate that ships the code but lacks the baked SURFACE ABORTS before spend — NOT self-attested.
# GT_BRIEF_MINIMAL is DELIBERATELY NOT receipt-gated (2026-07-12): the reduced brief is DORMANT until an
# SM-8 rebake bakes it (read at brief-generation time), and setting the flag on a non-minimal brief is
# BYTE-IDENTICAL (the generator ignores it) — it has no runtime surface to corrupt, unlike GT_SEM_BODY
# (which mis-reads absent body rows). Gating it on brief_minimal==true made a dormant member abort the
# WHOLE profile on every pre-rebake run (proven: micro-verify 29214296174). It is assumed-available by
# convention (same as GT_LOC_RESLOT/GT_CERT_DELIVERY) — correct-or-quiet, never a FALSE abort.
# The 3 mini-seam-spread flags (GT_D7_RELATEDNESS/GT_CONTRACT_MODE/GT_CONTRACT_BILATERAL) have no single
# importable module and no baked surface; they are synced-seam code (present iff the seam synced,
# byte-identical-off) and are available-by-convention (documented, never a substrate-absence risk).
_SUPER_MODE_MEMBERS: frozenset[str] = frozenset(
    {
        "GT_REGISTRY_ENFORCE",    # SM-0 — executable-registry enforcement in the Gateway
        "GT_COMPLETION_CERT",     # SM-3 — CompletionCertificate at the submit gate
        "GT_HYPOTHESIS",          # SM-3 — HypothesisLedger classification -> recovery selection
        "GT_VERIFICATION_PLAN",   # SM-3 — progressive verification ladder at the verify boundary
        # SM-4 (2026-07-11): re-added. GT_EDIT_OVERLAY now has MODEL-FACING value — the episode
        # overlay makes SM-0's freshness enforcement REAL: after the agent structurally edits a
        # file, a Gateway fact read from the (frozen, ro-mounted) base graph.db about that file
        # is DROPPED as stale, not shipped. (SM-3 LIPI F2 pulled it while it was host-only; SM-4
        # gives it the stale-drop, so it re-joins the coherent Super-Mode set.)
        "GT_EDIT_OVERLAY",        # SM-4 — episode-overlay freshness (stale base-read drop)
        # SM-5 (2026-07-11): the ONE global ranked competition over all delivery planes
        # (global_arbiter). Its pool is only COMPLETE when every factual producer can fire on
        # the mini run, so the two Gateway producers gated by their own flags join Profile-2:
        #   GT_CHANGE_SURFACE — the W-A change-surface producer (new-file destination /
        #     missing-role integration point); ranks in the ``localization`` class.
        #   GT_PATCH_DELTA    — the W-C patch-delta producer. Its DISTINCT axis vs SM-2b
        #     caller_contract is ``companion_surface`` (registration/companion gap, ``causal_chain``);
        #     its ``signature_mismatch`` caller axis ranks AT caller_contract (never above), and
        #     the gateway's own <=1 arbiter collapses it before the global pool, so it never
        #     double-delivers a caller break.
        "GT_GLOBAL_ARBITER",      # SM-5 — the single global dose over all planes
        "GT_CHANGE_SURFACE",      # SM-5 — W-A change-surface Gateway producer (pool completeness)
        "GT_PATCH_DELTA",         # SM-5 — W-C patch-delta Gateway producer (companion/signature)
        # SM-6 (2026-07-11): the step-0 baked-brief reduction. Retires the heavy
        # <gt-graph-map>/<gt-localization>/contract-narration surfaces from the step-0
        # brief, keeping obligations + minimal orientation (localization rides the reactive
        # def_partition channel, already an enabled Profile-2 fact class). It is READ at
        # BRIEF-GENERATION time (v1r_brief.generate_v1r_brief), so it takes effect only when
        # the substrate is REBAKED with the profile active (SM-8) — dormant until then.
        # NO _MEMBER_CAPABILITY_MODULE entry: the capability is a code property of the baked
        # generator (not a separately-importable runtime module), and the pretask brief
        # pipeline is too heavy to find_spec at resolver time. An unmapped member is assumed
        # available (correct-or-quiet: never a FALSE abort — flag-off is byte-identical anyway).
        "GT_BRIEF_MINIMAL",       # SM-6 — reduced step-0 brief (obligations + minimal orientation)
        # SM-9c (2026-07-11): the CROSS-SESSION learned-delivery policy. When active, the
        # Gateway consults this repo's durable causal-consumption ledger (xsession_memory,
        # loaded once at task-start) to suppress a fact-class this repo never consumed and
        # rank-up one it historically acted on. Backed by a concrete module
        # (_MEMBER_CAPABILITY_MODULE below) so the Profile-2 preflight fails CLOSED on a
        # stale substrate lacking the engine. Default-OFF byte-identical until the profile
        # fan-out (or an explicit env value) sets it to "1".
        "GT_XSESSION_MEMORY",     # SM-9c — cross-session learned delivery policy
        # SM-9c rank-up (2026-07-12): the WINNER-PROMOTION half of the cross-session policy.
        # When active, a historically-CONSUMED policy class earns a ladder-derived arbitration
        # boost (gateway._apply_xsession_rankup stamps it onto the envelope's native_args
        # side-car; the adapter's _priority adds it) so the consumed class can WIN the <=1 dose,
        # not merely sort ahead. Backed by the SAME xsession_memory engine (ladder_boost) as
        # GT_XSESSION_MEMORY, so the Profile-2 preflight fails CLOSED on a stale substrate
        # lacking it. Default-OFF byte-identical until the profile fan-out (or an explicit env
        # value) sets it to "1".
        "GT_XSESSION_RANKUP",     # SM-9c — cross-session rank-up -> winner promotion
        # SM-10 (2026-07-12): the two BODY-content retrieval legs — the ONLY body-semantic
        # surfaces and the stratum-B (behavior-described) lever. Both are default-OFF direct
        # os.environ reads in graph_localizer.localize (read at CALL time), so Profile-2 fans
        # them to "1" and an unset profile is byte-identical (both flags stay OFF). No
        # _MEMBER_CAPABILITY_MODULE entry: the real capability is a SUBSTRATE property — the
        # baked graph.db's symbol_content_fts table (GT_CONTENT_LEG) / per-symbol body
        # embeddings (GT_SEM_BODY) — not an importable module, so (per the documented
        # convention) an unmapped member is treated as available: never a FALSE abort, and
        # both legs are correct-or-quiet at runtime when the substrate lacks the surface.
        "GT_CONTENT_LEG",         # SM-10 — per-symbol body-BM25 (symbol_content_fts), grep-dead lane
        "GT_SEM_BODY",            # SM-10 — semantic leg embeds per-symbol BODY passages
        # T0->T2 re-slot (2026-07-12): the reactive ranked-localization producer at the
        # D2/post-search boundary (gateway._produce_ranked_localization) — re-homes GT's
        # ranked localization answer off the T0 baked <gt-localization> tag onto the agent's
        # own grep channel. Default-OFF direct os.environ read in gateway.augment (read at
        # dispatch time), so Profile-2 fans it to "1" and an unset profile is byte-identical
        # (no localization envelope is ever produced). No _MEMBER_CAPABILITY_MODULE entry: the
        # capability is a code property of gateway.augment (like GT_BRIEF_MINIMAL/GT_SEM_BODY),
        # not a separately-importable module -> assumed available (never a FALSE abort; flag-off
        # is byte-identical anyway).
        "GT_LOC_RESLOT",          # re-slot — reactive ranked-localization at post-search (D2)
        # POST_SEARCH master (2026-07-18, Cluster-5 ITEM 0): the MASTER enable for the whole
        # seam post_search def-partition delivery lattice (gt_mini_patch.py:816 `_POST_SEARCH_ON`
        # gates _search_localize_decision at :4588 — flag OFF -> the lattice abstains -> Lane A
        # drops every post_search fact). Its FORM arm GT_POST_SEARCH_NATIVE was already a Profile-1
        # member (renders the SAME def-facts in the native voice), but WITHOUT this master the
        # producer never fires, so the form arm rendered nothing and gateway.localization stayed
        # dead behind it. Adding the master lets the producer COMPETE (arbiter suppression still
        # applies — this claims no delivery). Default-OFF direct os.environ read (byte-identical
        # when GT_RL_PROFILE is unset). NO _MEMBER_CAPABILITY_MODULE entry: the capability is a
        # seam code property (_search_localize_decision + _direct_def_block over graph.db), not a
        # single importable module -> unmapped-by-convention like GT_LOC_RESLOT's sibling seam
        # flags (never a FALSE abort; correct-or-quiet at runtime when no db/symbol).
        "GT_POST_SEARCH",         # ITEM 0 — post_search lattice MASTER enable (form arm's master)
        # SM-3 D7 delivery (2026-07-12): the CompletionCertificate's NOT-CLEAN heads rendered
        # MODEL-FACING at the submit turn — a native pre-commit-hook failure block (one line per
        # failing head) at the dominant-failure decision point (D7). Companion to
        # GT_COMPLETION_CERT (which only BUILDS + host-records the cert); this flag turns the
        # cert into bytes the model sees, INTEGRATED into the submit gate's existing refusal
        # appender (never a second dose). Default-OFF direct os.environ read in
        # gt_mini_patch._gt_gate_submit_exception (read at the submit chokepoint), so Profile-2
        # fans it to "1" and an unset profile is byte-identical (the block path keeps rendering
        # the single-line render_submit_rejection). NO _MEMBER_CAPABILITY_MODULE entry: the
        # capability is a code property of the seam + the native_render.render_completion_cert_native
        # renderer (both already backing GT_COMPLETION_CERT / GT_GATEWAY_NATIVE), not a separately
        # importable module -> assumed available (never a FALSE abort; flag-off is byte-identical).
        "GT_CERT_DELIVERY",       # SM-3 — model-facing CompletionCertificate delivery at D7
        # #48 L6-FRESH (2026-07-12): the per-turn in-container reindex engine. After the agent
        # structurally edits a file, the L6 reindex refreshes a WRITABLE work-copy of the base
        # graph.db so the agent's OWN added structure stays visible through the solve. The #48
        # coder staged the runnable gt-index binary into the /opt/gt mount but was forbidden from
        # rl_profile, leaving it a NON-MEMBER while a runtime consumer already reads it in-container
        # (gt_mini_patch._db_path, `if os.environ.get("GT_L6_FRESH") == "1":`) — so under an active
        # Profile-2 the reindex stayed DARK on any caller that fans out via the profile. NO
        # _MEMBER_CAPABILITY_MODULE entry: the capability is a SUBSTRATE property (the staged
        # gt-index binary in /opt/gt), not an importable module — unmapped-by-convention (assumed
        # available: never a FALSE abort, and the reindex is correct-or-quiet at runtime when the
        # binary is absent — same convention as GT_BRIEF_MINIMAL/GT_CONTENT_LEG/GT_SEM_BODY/
        # GT_LOC_RESLOT/GT_CERT_DELIVERY). Default-OFF direct os.environ read; Profile-2 fans it
        # to "1" and an unset profile is byte-identical (the flag stays OFF -> no work-copy).
        "GT_L6_FRESH",            # #48 — per-turn in-container L6 reindex (edit-fresh work-copy)
        # SS-1..SS-N (2026-07-13) — the SUPER-SEAM adherence sweep from the 29-task causal audit
        # of run 29236533134. Each is a DEFAULT-OFF, byte-identical-when-unset seam/arbiter code
        # property (NOT a new tag / pull-tool). NO _MEMBER_CAPABILITY_MODULE entry — these are
        # seam-spread / arbiter-engine code properties with no single baked SURFACE to find_spec,
        # so they are available-BY-CONVENTION exactly like GT_D7_RELATEDNESS / GT_SCOPE_NATIVE /
        # GT_LOC_RESLOT (never a FALSE abort; flag-off is byte-identical anyway). Forwarded in
        # gt_ae_block.sh (pier drops host env) and pinned in test_ae_forward_profile2_completeness.
        # ONE evidence line each (audit finding it addresses):
        "GT_SS_NOVELTY",          # SS — novelty gate: a fact re-offered unchanged is not re-delivered
        "GT_SS_DEDUP2",           # SS — 2nd-order dedup: cross-plane identity beyond the unified key
        "GT_SS_COHERENCE_V2",     # SS — coherence/companion re-detection (the l3b-dominated pool composition)
        "GT_SS_RECOVERY_V2",      # SS — recovery selection v2 (the one proven-consumed short/active form)
        "GT_SS_PROVENANCE",       # SS — provenance stamp on every model-visible delivery (seal completeness)
        "GT_SS_LATE_DROP",        # SS — late-fact drop timing (defer-refire vs destroy on a passed boundary)
        "GT_SS_ACK_METRICS",      # SS — ack-ladder telemetry (fact_registry.ack_expected; host-side only)
        # SS-5 (2026-07-13): the acknowledgment-FORM arm. Rewrites the m1 preamble (gt_agent.py) off
        # the tool-framing + retired-<gt-*>-tag text that TRAINED the discount policy (296-delivery
        # audit: ~3% ack, half of them GT-tool dismissals) onto native environment-diagnostic
        # guidance, AND applies the requirement-extractor discipline to the GT_BRIEF_NATIVE step-0
        # obligations checklist (imperative items only; repro/pleas dropped). Default-OFF byte-
        # identical seam/brief code property (NOT a new tag/pull-tool). NO _MEMBER_CAPABILITY_MODULE
        # entry — baked-text (preamble) + brief-gen code property with no single find_spec-able
        # surface, unmapped-BY-CONVENTION exactly like the other SS members (never a FALSE abort;
        # flag-off is byte-identical). Forwarded in gt_ae_block.sh; pinned in the AE-forward
        # completeness pin + tests/pretask/test_ss5_*.
        "GT_SS_ACK_FORM",         # SS-5 — acknowledgment FORM: non-tool-framing preamble + imperative checklist
        # GT_SS_ARBITER_V2: the arbiter defer-and-refire + repair_support relaxation + empty-payload
        # guard (global_arbiter.arbitrate / gateway.augment). Fixes: scope_map blanket-retire (29/29),
        # consensus.scope repair_support_late (18x suppressed — the ONLY proven-consumed class), and
        # the hydra-3005 chars=0 empty-payload delivered row. Backed by global_arbiter, but left
        # UNMAPPED-by-convention with its SS siblings (per task directive; flag-off is byte-identical).
        "GT_SS_ARBITER_V2",       # SS — arbiter v2: defer-refire + repair_support relax + empty-payload guard
        # SS-2 (2026-07-13, run 29236533134 causal audit) — the EXECUTED-truth pair. Both are
        # DEFAULT-OFF byte-identical-when-unset seam/covering-runner code properties (NOT a new
        # tag). GT_SS_EXEC_TRUTH: covering SELECTION drops a phantom graph test node absent from
        # the working tree, so the "a graph-linked covering test covers them" advisory/obligation
        # claim is made ONLY when the runner could actually run it (kills the fonttools-3682/
        # cfn-lint-3749 false assurance; one eligibility for advisory + executed). GT_SS_SUBMIT_RED:
        # the submit boundary consumes the agent's OWN unresolved observed test RED (the conan-17092
        # "observed a fail, rationalized it away" class) — ONE native pre-commit refusal, single
        # dose. UNMAPPED-by-convention with the SS siblings (seam/covering-runner-spread code, no
        # single baked SURFACE to find_spec) -> never a FALSE abort; forwarded in gt_ae_block.sh;
        # pinned in test_ae_forward_profile2_completeness + the SS-2 member pin.
        "GT_SS_EXEC_TRUTH",       # SS-2 — covering selection = runner-eligible (no phantom claim)
        "GT_SS_SUBMIT_RED",       # SS-2 — submit gate consumes the agent's own unresolved observed RED
                                  #   NOTE: reached only via the GT_VERIFY_EXECUTE=1 submit branch
                                  #   (gt_mini_patch._gt_gate_submit_exception) — enabling this flag
                                  #   alone (profile off) is a silent no-op; Profile-2 co-activates both.
        # SS-4 (2026-07-13, run 29236533134 causal audit) — the starved-producer ELIGIBILITY
        # widening. MEASURED: 16/29 tasks reached the seam with the arm-4 harness prefix
        # `cd $(cat /tmp/gt_root.txt) && <cmd>` (a command-substitution cd the W13 bare-token
        # cd-strip does NOT recognise), so the post_search isolation gate rejected ~all real
        # searches and post_search/def_partition/loc_reslot/content_leg/sem_body were structurally
        # mute on those tasks. When on, gt_mini_patch._strip_leading_cd_prefix ALSO strips a pure
        # `cd $(...)`/`cd "$(...)"`/cd `...`/`cd $VAR` command-substitution cd (a no-op for the
        # grep's OWN zero/hit signal, exactly like the bare-token cd) — a STRUCTURAL recognition,
        # NOT a confidence drop. Measured widening over the 29 recorded trajectories: +27 bare-symbol
        # def_partition triggers + 35 broad loc_reslot triggers, 0 divergence on any non-widened
        # command. DEFAULT-OFF byte-identical (flag off -> the W13 bare-token strip only). UNMAPPED-
        # by-convention with the SS siblings (seam code property, no single baked SURFACE to
        # find_spec) -> never a FALSE abort; forwarded in gt_ae_block.sh; pinned in
        # test_ae_forward_profile2_completeness + the SS-4 member pin.
        "GT_SS_ELIGIBILITY",      # SS-4 — cd-$() prefix widening (post_search/loc_reslot eligibility)
        # SS-8 (2026-07-13): the SHADOW-HOLDOUT causal instrument (gt-math E10). Per eligible
        # advisory delivery a deterministic seed decides DELIVER vs HOLDOUT, so ONE benchmark run
        # yields a delivered-vs-withheld contrast per fact class. UNLIKE the other SS members this
        # one IS backed by a single importable module (groundtruth.runtime.shadow_holdout) -> it is
        # MAPPED in _MEMBER_CAPABILITY_MODULE below (fail-closed on a stale substrate lacking the
        # engine, like GT_VERIFY_EXECUTE -> covering_runner). Default-OFF byte-identical; the
        # separate rate knob GT_SS_SHADOW_RATE defaults "0" (never withhold even when the flag is
        # on), so an active Profile-2 activates the instrument INERT — no bytes are ever withheld
        # until an E10 eval explicitly sets the rate. Read in-container by gt_mini_patch's two
        # delivery chokepoints; forwarded in gt_ae_block.sh (with the rate knob); pinned in
        # test_ss8_* + the AE-forward completeness pin.
        "GT_SS_SHADOW",           # SS-8 — shadow-holdout causal instrument (E10)
    }
)

PROFILE_MEMBERS: dict[str, frozenset[str]] = {
    "1": _PROFILE_1_MEMBERS,
    # Profile 2 (SM "Super Mode") = Profile-1's coherent set PLUS the Super-Mode members.
    # When active, the fact-registry is ENFORCED in the Gateway (SM-0) and the four SM-3
    # engines light up (all still default-OFF byte-identical until their flag is set to "1"
    # by the profile fan-out or an explicit env value).
    "2": _PROFILE_1_MEMBERS | _SUPER_MODE_MEMBERS,
}

# THE-17 wave activation (2026-07-17, user-ratified "activate and go"), CORRECTED after the
# 29592317152 preflight refusal (CAP expected 47 rows, got 52): these five are BEHAVIOR
# FLAGS (fix switches at seam/runtime decision sites), NOT inventory features — Profile-2
# MEMBERSHIP doubles as the exact-129 CAP inventory (gt_feature_inventory reads
# PROFILE_MEMBERS["2"]; CAP == 48 after ITEM 0 added GT_POST_SEARCH), so they must never join
# it. They are fanned to "1" by resolve_profile_defaults alongside the members (same setdefault
# kill-switch semantics at the seam), keeping the CAP inventory at its member count. All default-OFF byte-identical when no
# profile is active. GT_LOC_VENDOR_DEMOTE deliberately excluded (semantic-on confirm owed).
PROFILE_BEHAVIOR_FLAGS: dict[str, frozenset[str]] = {
    "2": frozenset(
        {
            "GT_CS_EDIT_TRIGGER",
            "GT_SS_EDIT_PREVENTIVE",  # W2b/P10 — edit-boundary premature-reactive deferral
            "GT_INFRA_NOISE_GUARD",   # W4-1 — infra/teardown-noise exclusion
            "GT_HYP_CONTRA_GUARD",    # W4-2 — no wrong-target steer vs corroborated targets
            "GT_RECOVERY_ESCALATE",   # W4-3 — repeat-N recovery escalates, never weaker
            "GT_OBLIG_STEER_GUARD",   # W4-4 — obligation narrowing cannot exclude top-ranked
            # C9/C10 behavior only; this must never become a CAP inventory row.
            "GT_ROLE_DRIVEN_COALITION",
        }
    ),
}

# ── Form-arm ↔ master consistency (ITEM 0b, 2026-07-18) ───────────────────────
# A ``*_NATIVE`` member is the FORM arm of a delivery surface: it re-renders an existing fact
# in the native (tag-free) voice. SOME surfaces ALSO have a MASTER enable flag that gates whether
# the underlying producer runs at all — e.g. GT_GATEWAY for GT_GATEWAY_NATIVE, and GT_POST_SEARCH
# for GT_POST_SEARCH_NATIVE. When such a master exists it MUST be a profile member too, or the
# form arm renders nothing: the EXACT defect ITEM 0 fixed (GT_POST_SEARCH_NATIVE was an active
# Profile-2 member while its GT_POST_SEARCH master was absent, so the whole post_search lattice
# was dead behind it).
#
# The OTHER ``*_NATIVE`` arms are PURE form flips with NO separate master gate — their fact is
# always produced (by the gateway / consensus / brief-gen) and the arm only changes rendering.
# Those are genuinely master-less and are listed here WITH A REASON so the consistency check does
# not raise a false gap on them. Deriving the split from a same-prefix master would be ambiguous
# (GT_CONTRACT_MODE exists but is not GT_CONTRACT's producer gate), so the allowlist is explicit.
_MASTERLESS_FORM_ARMS: dict[str, str] = {
    "GT_STEER_NATIVE": "steer/oracle route has no separate GT_STEER producer gate — always produced when the gateway is on",
    "GT_SCOPE_NATIVE": "pure render of the consensus scope block; no GT_SCOPE producer-enable flag",
    "GT_CONTRACT_NATIVE": "render form of caller_contract (gated by GT_GATEWAY, not a GT_CONTRACT master)",
    "GT_EVIDENCE_NATIVE": "evidence rg-row render form of caller_contract; no GT_EVIDENCE producer gate",
    "GT_NUDGE_NATIVE": "nudge body-frame drop of the recovery/steer fact; no GT_NUDGE producer gate",
    "GT_BRIEF_NATIVE": "brief obligations-checklist render at brief-gen; no GT_BRIEF producer gate",
}


def form_arm_master_gaps(token: str = "2") -> dict[str, str]:
    """Every ``*_NATIVE`` FORM-arm member of profile ``token`` whose MASTER enable flag (the
    name minus the ``_NATIVE`` suffix) is NEITHER also a member NOR a documented master-less
    form arm (``_MASTERLESS_FORM_ARMS``). A non-empty result is a form-arm/master inconsistency —
    the ITEM-0 defect class: the profile ships a native render for a producer it never enables.

    PURE + deterministic. Returns ``{form_arm: derived_master_name}`` in sorted order.
    """
    members = PROFILE_MEMBERS.get(token, frozenset())
    gaps: dict[str, str] = {}
    for name in sorted(members):
        if not name.endswith("_NATIVE") or name in _MASTERLESS_FORM_ARMS:
            continue
        master = name[: -len("_NATIVE")]
        if master not in members:
            gaps[name] = master
    return gaps


# Values (case-insensitive, stripped) that count as "OFF" for a member.
_OFF_VALUES = {"", "0", "false", "no", "off"}

# ── W8 PRODUCTION-DEFAULT INVERSION (2026-07-13) ──────────────────────────────
# The seam now resolves its OWN profile IN-PROCESS at install time (gt_mini_patch
# _resolve_production_profile) instead of depending on the workflow's external
# GT_RL_PROFILE=2 fan-out. When GT_RL_PROFILE is UNSET the seam resolves THIS default
# (Profile-2, "Super Mode") — so a launcher that forgets the fan-out no longer silently
# degrades GT to the legacy stack. An EXPLICIT "0"/"off"/"none" is the legacy/control
# posture (resolve nothing, byte-identical to the pre-W8 unset path). ``resolve_profile``
# (the workflow verb) is UNCHANGED — the fan-out keeps working, merely redundant.
_PRODUCTION_DEFAULT_PROFILE = "2"

# Explicit tokens (case/space-insensitive) that mean "resolve NOTHING — legacy/control".
# Deliberately a SUBSET of _OFF_VALUES: an unset/empty GT_RL_PROFILE resolves the
# production default, but a user who TYPED "0"/"off"/"none" chose the legacy posture.
_EXPLICIT_LEGACY_TOKENS = {"0", "off", "none"}


# ── The per-profile MANIFEST (SM-0) ──────────────────────────────────────────
# A profile's members say WHICH flags flip on; the manifest declares WHAT the running
# substrate must satisfy for the profile to be honest — the commit it was baked/checked-out
# at, the modules that must be synced, the fact classes it enables + each class's native
# renderer (cross-checked against ``fact_registry`` at preflight), the live call site, the
# receipt predicate, and the surfaces that must be ABSENT (explicitly retired). Preflight
# turns this into a fail-closed abort. All PURE data — no I/O, no liveness probe (that is the
# SM-7 fixture gate's job, not preflight's).
@dataclass(frozen=True)
class ProfileManifest:
    version: str
    required_baked_commit: str = ""          # "" => not pinned yet (skip the check)
    required_live_commit: str = ""           # "" => not pinned yet (skip the check)
    substrate_features: tuple[str, ...] = ()
    synced_modules: tuple[str, ...] = ()
    event_bridges: tuple[str, ...] = ()
    enabled_fact_classes: tuple[str, ...] = ()
    renderer_per_class: Mapping[str, str] = field(default_factory=dict)  # class -> renderer
    expected_live_call_site: str = ""
    receipt_predicate: str = ""
    retired_surfaces: frozenset[str] = frozenset()  # flags that MUST be OFF/absent


# The Super-Mode (Profile 2) manifest. ``renderer_per_class`` is declared explicitly and
# CROSS-CHECKED against ``fact_registry.required_renderer`` at preflight (a drift aborts). The
# commit pins are "" (not pinned yet) and are SKIPPED until a wave sets them. ``retired_surfaces``
# is empty for SM-0 (Super Mode retires no flag yet); the forbidden-present mechanism is live so
# a later wave only needs to list a concrete retired flag.
PROFILE_MANIFESTS: dict[str, ProfileManifest] = {
    "2": ProfileManifest(
        version="2",
        required_baked_commit="",
        required_live_commit="",
        substrate_features=(
            "fact_registry_enforce",     # the executable-registry gate in gateway.augment
            "route_registry_timing",     # route_delivery derives want from the registry
            "freshness_single_source",   # freshness surfaces sourced from fact_registry
            "episode_overlay_freshness", # SM-4 — base-read facts stale against the CURRENT edit
        ),
        synced_modules=(
            "groundtruth.runtime.fact_registry",
            "groundtruth.runtime.gateway",
            "groundtruth.runtime.edit_overlay",  # SM-4 — the episode-overlay freshness engine
        ),
        event_bridges=("brief", "post_search", "post_view", "post_edit", "post_test", "submit", "steer"),
        enabled_fact_classes=(
            "caller_contract", "cochange_prior", "covering_red", "def_partition",
            "localization", "newfile_precedent", "obligations", "recovery",
            "signature_delta", "submit_refusal", "syntax_result",
        ),
        renderer_per_class={
            "obligations": "plan",
            "localization": "ranked-list",
            "def_partition": "grep-native",
            "caller_contract": "contract",
            "syntax_result": "compiler-native",
            "signature_delta": "diff-native",
            "covering_red": "test-native",
            "submit_refusal": "test-native",
            "cochange_prior": "list",
            "newfile_precedent": "list",
            "recovery": "imperative",
        },
        expected_live_call_site="groundtruth.runtime.gateway.augment (post_search/post_edit/post_test seam)",
        receipt_predicate="registry_boundary_and_renderer_respected",
        retired_surfaces=frozenset(),
    ),
}


def _profile_token(env: Mapping[str, str]) -> str:
    """The requested profile version, stripped. Empty string when unset/OFF."""
    return (env.get("GT_RL_PROFILE") or "").strip()


def _profile_is_off(token: str) -> bool:
    return token == "" or token == "0"


def _is_on(value: str) -> bool:
    return value.strip().lower() not in _OFF_VALUES


def profile_members(token: str) -> frozenset[str]:
    """Canonical members for a profile version (empty for unknown/off)."""
    return PROFILE_MEMBERS.get(token, frozenset())


def resolve_profile(env: Mapping[str, str]) -> dict[str, str]:
    """Fan ``GT_RL_PROFILE`` out to its member flag map.

    Rules:
      * profile UNSET / "" / "0" / unknown version  -> ``{}`` (no member touched).
      * else each member -> ``"1"`` UNLESS the env carries an explicit, non-empty
        value for it, in which case that value wins (per-flag override, incl "0").
        An EMPTY string is treated as unset (the workflow's ``|| ''`` default),
        NOT as a chosen "0", so it does not suppress the profile.

    Deterministic: the returned dict is insertion-ordered by sorted member name.
    """
    token = _profile_token(env)
    if _profile_is_off(token):
        return {}
    members = PROFILE_MEMBERS.get(token)
    if members is None:
        return {}
    out: dict[str, str] = {}
    for member in sorted(members):
        explicit = env.get(member)
        if explicit is not None and explicit.strip() != "":
            out[member] = explicit  # explicit override wins (including "0")
        else:
            out[member] = "1"
    return out


def resolve_default_token(env: Mapping[str, str]) -> Optional[str]:
    """The profile token the W8 in-process seam inversion resolves (production default).

    UNLIKE ``resolve_profile`` (whose UNSET case is a no-op), the inversion treats an
    UNSET/empty ``GT_RL_PROFILE`` as the PRODUCTION DEFAULT and an EXPLICIT off-token as
    the legacy/control posture:

      * ``GT_RL_PROFILE`` unset / "" / whitespace-only  -> ``_PRODUCTION_DEFAULT_PROFILE``.
      * explicit "0"/"off"/"none" (case/space-insensitive) -> ``None`` (resolve NOTHING;
        byte-identical to the pre-W8 unset posture — the control/legacy arm).
      * any other value -> that token verbatim (an unknown version resolves no members,
        see ``resolve_profile_defaults`` — fail-safe, never a crash).

    PURE: reads only ``env``; no I/O.
    """
    raw = env.get("GT_RL_PROFILE")
    if raw is None or raw.strip() == "":
        return _PRODUCTION_DEFAULT_PROFILE
    token = raw.strip()
    if token.lower() in _EXPLICIT_LEGACY_TOKENS:
        return None
    return token


def resolve_profile_defaults(env: Mapping[str, str]) -> dict[str, str]:
    """The member-default map the W8 in-process seam inversion applies via ``setdefault``.

    Returns ``{member: "1"}`` for EVERY member of the resolved profile (deterministic,
    insertion-ordered by sorted member name). The seam applies each with
    ``os.environ.setdefault(member, "1")``, so an explicit env value for a member (incl
    "0") is a KILL-SWITCH that survives — that per-member override lives at the seam's
    ``setdefault`` call site, NOT here. This helper therefore returns the FULL member set
    (kill-switched members included) so the seam's ``setdefault`` remains the single
    load-bearing kill-switch (a direct-assignment regression is caught by the seam's
    kill-switch test, not masked by this helper pre-filtering).

    Resolution honours ``resolve_default_token``:
      * profile-level explicit-off / unknown version -> ``{}`` (nothing to set).
      * else -> the resolved profile's ``PROFILE_MEMBERS``, each defaulted to "1".

    PURE: reads only ``env``; no I/O.
    """
    token = resolve_default_token(env)
    if token is None:
        return {}
    members = PROFILE_MEMBERS.get(token)
    if not members:
        return {}
    # Behavior flags ride the same fan-out (and the seam's setdefault kill-switch) but are
    # NOT members: they never enter the exact-128 inventory or membership counts.
    flags = PROFILE_BEHAVIOR_FLAGS.get(token, frozenset())
    return {name: "1" for name in sorted(members | flags)}


def _registry_renderer(fact_class: str) -> Optional[str]:
    """The DECLARED native renderer for ``fact_class`` from ``fact_registry`` (lazy import so
    a partial substrate that lacks the module ABORTS via a ``None`` here rather than crashing
    module import). ``None`` = the class has no resolvable renderer (or fact_registry absent)."""
    try:
        from groundtruth.runtime import fact_registry as _fr

        return _fr.required_renderer(fact_class)
    except Exception:  # noqa: BLE001 — a missing/broken fact_registry => renderer unresolved
        return None


def _manifest_preflight(env: Mapping[str, str], token: str) -> list[str]:
    """The profile-MANIFEST fail-closed checks (SM-0), appended to the member-capability
    preflight. Returns abort sentinels (empty = clear). PURE + deterministic.

    (a) COMMIT PINS — a non-empty ``required_baked_commit`` / ``required_live_commit`` must
        equal the provided ``GT_BAKED_COMMIT`` / ``GT_LIVE_COMMIT`` (skipped when the manifest
        field is "" — not yet pinned).
    (b) RENDERER PRESENCE — every enabled class's declared renderer must resolve in
        ``fact_registry`` AND match the manifest's declared renderer (a drift aborts).
    (c) FORBIDDEN-RETIRED-SURFACE — a listed retired surface flag that is ON is an ABORT
        (the mirror of the required-present member check: a retired surface must be ABSENT).
    """
    manifest = PROFILE_MANIFESTS.get(token)
    if manifest is None:
        return []
    out: list[str] = []
    # (a) commit pins
    if manifest.required_baked_commit:
        got = (env.get("GT_BAKED_COMMIT") or "").strip()
        if got != manifest.required_baked_commit:
            out.append(f"GT_BAKED_COMMIT!={manifest.required_baked_commit}")
    if manifest.required_live_commit:
        got = (env.get("GT_LIVE_COMMIT") or "").strip()
        if got != manifest.required_live_commit:
            out.append(f"GT_LIVE_COMMIT!={manifest.required_live_commit}")
    # (b) per-class renderer presence + agreement with the registry
    for cls in sorted(manifest.renderer_per_class):
        declared = manifest.renderer_per_class[cls]
        actual = _registry_renderer(cls)
        if not actual or actual != declared:
            out.append(f"renderer:{cls}")
    # (c) forbidden retired surface ON
    for surf in sorted(manifest.retired_surfaces):
        if _is_on(env.get(surf, "")):
            out.append(f"retired:{surf}")
    return out


def preflight(env: Mapping[str, str], available: Iterable[str]) -> list[str]:
    """Members that block the run: requested-ON but capability absent, PLUS the profile-
    manifest aborts (commit pins / renderer presence / forbidden-retired-surface, SM-0).

    Returns the member-missing list (sorted) FOLLOWED BY the manifest aborts — a non-empty
    result must ABORT the harness before model spend. Empty list = clear to run. A member
    turned OFF by an explicit override does NOT require its capability. Profile OFF returns
    ``[]``; an unknown version fails closed with an ``GT_RL_PROFILE=<v>`` sentinel.
    """
    # SM-7 GATE FIX (2026-07-13, orchestrator): route the effective token through the W8
    # production default. Pre-fix, an UNSET GT_RL_PROFILE returned [] (treated as OFF) while
    # the seam's in-process inversion resolves the SAME unset env to Profile-2 — so the
    # fail-closed capability preflight was BYPASSED on the exact path W8 made the production
    # default (a stale substrate on a default launcher would not abort before spend).
    # resolve_default_token: unset/"" -> "2"; explicit 0/off/none -> None (true control arm).
    default_token = resolve_default_token(env)
    if default_token is None:
        return []  # explicit legacy/control posture — genuinely OFF
    token = _profile_token(env) or default_token
    if _profile_is_off(token):
        return []
    if token not in PROFILE_MEMBERS:
        return [f"GT_RL_PROFILE={token}"]  # unknown version -> fail closed
    available_set = set(available)
    # Resolve members from the EFFECTIVE token (the W8 default included): resolve_profile
    # reads the raw env token, which is empty on the unset production path — synthesize the
    # effective token so the member-capability loop is never vacuous where the seam inverts.
    _env_eff = dict(env)
    # ``setdefault`` is insufficient here: workflow expansion commonly leaves
    # ``GT_RL_PROFILE`` present as an empty string.  That value is semantically
    # UNSET, but it would make resolve_profile() return no members and bypass
    # the production Profile-2 capability check.  Bind the already-resolved
    # effective token explicitly for both absent and empty inputs.
    _env_eff["GT_RL_PROFILE"] = token
    resolved = resolve_profile(_env_eff)
    requested_on = {m for m, v in resolved.items() if _is_on(v)}
    missing = sorted(requested_on - available_set)
    return missing + _manifest_preflight(_env_eff, token)


# Brief-F6: each RL-profile member's capability is BACKED by a concrete module in the
# ``groundtruth`` package (the feature's implementation). A member whose backing module
# is ABSENT from the running interpreter's import path (a stale/partial substrate) is
# genuinely unavailable → the preflight must abort before model spend. Members without a
# single clean backing module (GT_D7_RELATEDNESS + the contract flags are spread across
# the in-container mini-seam) are omitted here and treated as available — correct-or-
# quiet: NEVER a FALSE abort on an unmapped member. Only LIGHT ``groundtruth.runtime.*``
# modules are mapped so the probe imports nothing heavy at resolver time.
_MEMBER_CAPABILITY_MODULE: dict[str, str] = {
    "GT_GATEWAY": "groundtruth.runtime.gateway",
    "GT_GATEWAY_EDIT_BRIDGES": "groundtruth.runtime.gateway",
    "GT_GATEWAY_NATIVE": "groundtruth.runtime.native_render",
    "GT_STEER_NATIVE": "groundtruth.runtime.native_render",
    "GT_LANE_ENVELOPE": "groundtruth.runtime.evidence_envelope",
    "GT_EDIT_CHECK": "groundtruth.runtime.edit_check",
    "GT_VERIFY_EXECUTE": "groundtruth.runtime.covering_runner",
    "GT_OBLIGATION_FRESHNESS": "groundtruth.runtime.obligations",
    # SM-0: the executable-registry enforcement is backed by fact_registry (pure/light).
    "GT_REGISTRY_ENFORCE": "groundtruth.runtime.fact_registry",
    # SM-3/SM-4: each PROFILE-ACTIVATED engine's backing module. A stale substrate missing any
    # of these aborts the Profile-2 preflight before model spend (fail-closed capability probe).
    "GT_COMPLETION_CERT": "groundtruth.runtime.submit_gate",
    "GT_HYPOTHESIS": "groundtruth.runtime.hypothesis_ledger",
    "GT_VERIFICATION_PLAN": "groundtruth.runtime.verification_plan",
    # SM-4: GT_EDIT_OVERLAY re-added — the episode-overlay freshness engine is backed by
    # edit_overlay (build_episode_overlay_entry / compose_episode_revision / mark_stale_envelopes).
    "GT_EDIT_OVERLAY": "groundtruth.runtime.edit_overlay",
    # SM-5: the global-arbiter engine + the two Gateway producers its pool needs. Each maps to a
    # concrete backing module so the Profile-2 preflight fails CLOSED on a stale substrate that
    # lacks the engine (both pretask + runtime packages are staged into /opt/gt in-container).
    "GT_GLOBAL_ARBITER": "groundtruth.runtime.global_arbiter",
    "GT_CHANGE_SURFACE": "groundtruth.pretask.change_surface",
    "GT_PATCH_DELTA": "groundtruth.runtime.patch_delta",
    # SM-9c: the cross-session learned-policy engine (pure/light; stdlib + fact_registry +
    # evidence_envelope). A stale substrate lacking it aborts the Profile-2 preflight.
    "GT_XSESSION_MEMORY": "groundtruth.runtime.xsession_memory",
    # SM-9c rank-up: the winner-promotion boost (ladder_boost) lives in the SAME engine, so it
    # maps to the same backing module — fail-closed on a substrate lacking it.
    "GT_XSESSION_RANKUP": "groundtruth.runtime.xsession_memory",
    # 2026-07-12 typed-manifest: the cleanly-importable native-FORM members (renderer/gateway code
    # properties). Mapped so preflight fails CLOSED on a substrate whose synced seam lacks the renderer
    # module — no longer self-attested. (GT_D7_RELATEDNESS/GT_CONTRACT_MODE/GT_CONTRACT_BILATERAL stay
    # unmapped BY CONVENTION: mini-seam-spread code, no single module + no baked surface.)
    "GT_POST_SEARCH_NATIVE": "groundtruth.runtime.native_render",
    "GT_SCOPE_NATIVE": "groundtruth.runtime.native_render",
    "GT_CERT_DELIVERY": "groundtruth.runtime.native_render",
    "GT_LOC_RESLOT": "groundtruth.runtime.gateway",
    # SS-8: the shadow-holdout instrument IS a single importable engine (pure/stdlib-only), so
    # unlike the seam-property SS flags it maps to its backing module -> the Profile-2 preflight
    # fails CLOSED on a stale substrate whose synced source lacks shadow_holdout.
    "GT_SS_SHADOW": "groundtruth.runtime.shadow_holdout",
}


# 2026-07-12 typed capability manifest (readiness spec §2) — SUBSTRATE-PROPERTY members whose capability
# is a BAKED SURFACE, NOT importable code: an indexed table / per-symbol body embeddings / a minimized
# baked brief / a staged binary. find_spec CANNOT prove these (the code can ship while the surface is
# absent), so each maps to a RECEIPT PREDICATE over a capability-receipt bundle (env GT_CAPABILITY_RECEIPT,
# inline JSON the run's preflight produces from the REAL substrate). A requested member whose receipt is
# absent/false is UNAVAILABLE => fail-closed abort. This is the anti-self-attestation core: no substrate
# property is ever assumed available.
_MEMBER_CAPABILITY_RECEIPT: dict[str, Callable[[Mapping[str, object]], bool]] = {
    "GT_CONTENT_LEG": lambda r: _as_int(r.get("symbol_content_fts_rows")) > 0,
    "GT_SEM_BODY": lambda r: _as_int(r.get("sem_body_rows")) > 0,
    # NB: GT_BRIEF_MINIMAL is INTENTIONALLY absent — it is dormant until an SM-8 rebake and
    # byte-identical when set on a non-minimal brief, so it has no surface to fail closed against.
    # Gating it aborted the whole Profile-2 on every pre-rebake run (micro-verify 29214296174).
    # It is assumed-available by convention (see the header note). Do NOT re-add it here.
    "GT_L6_FRESH": lambda r: bool(str(r.get("gt_index_bin") or "").strip()),
}


def _as_int(v: object) -> int:
    """Best-effort int for a receipt count; non-numeric/absent => 0 (=> the member fails closed)."""
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _capability_receipt(env: Mapping[str, str]) -> Mapping[str, object]:
    """The capability-receipt bundle from ``GT_CAPABILITY_RECEIPT`` (inline JSON). Absent/empty/malformed
    => ``{}`` (=> every receipt-gated member fails closed). Pure w.r.t. env; no file I/O."""
    raw = (env.get("GT_CAPABILITY_RECEIPT") or "").strip()
    if not raw:
        return {}
    try:
        import json

        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 — a malformed receipt fails closed, never crashes preflight
        return {}


def _module_findable(module: str) -> bool:
    """True iff ``module`` is importable-by-path WITHOUT executing it — a side-effect-
    free capability probe (``importlib.util.find_spec``). A definitive 'not found'
    (``find_spec`` → None) means the implementation is ABSENT; any probe-machinery error
    is treated as findable (conservative: the probe itself never triggers a FALSE abort).
    """
    import importlib.util as _ilu

    try:
        return _ilu.find_spec(module) is not None
    except Exception:
        return True


def _available_from_env(env: Mapping[str, str]) -> set[str]:
    """The capability-availability set for preflight — a REAL signal, not self-attestation.

    Precedence:
      1. ``GT_RL_PROFILE_AVAILABLE`` (comma-separated) — an explicit operator/CI stamp
         wins outright (e.g. narrowed from a baked-substrate manifest).
      2. else PROBE (typed manifest, 2026-07-12):
         * a RECEIPT-gated member (``_MEMBER_CAPABILITY_RECEIPT`` — a baked substrate surface) is
           available IFF its receipt predicate holds over ``GT_CAPABILITY_RECEIPT``; absent => fail closed.
         * a MODULE-backed member (``_MEMBER_CAPABILITY_MODULE``) is available iff its module is
           importable-by-path in THIS interpreter.
         * a member with neither mapping (the 3 mini-seam-spread flags) is available by convention —
           synced-seam code, present iff the seam synced, never a substrate-absence risk.
         Brief-F6: the pre-2026-07-12 default returned ALL 11 unmapped members unconditionally (the
         fail-closed abort was structurally dead for them); now only 3 documented seam flags self-attest.

    Pure w.r.t. ``env``; the probe is side-effect-free (never executes member code).
    """
    raw = env.get("GT_RL_PROFILE_AVAILABLE")
    if raw is not None and raw.strip() != "":
        return {x.strip() for x in raw.split(",") if x.strip()}
    receipt = _capability_receipt(env)
    out: set[str] = set()
    for member in profile_members(_profile_token(env)):
        predicate = _MEMBER_CAPABILITY_RECEIPT.get(member)
        if predicate is not None:
            # SUBSTRATE-PROPERTY member: available IFF its baked-surface receipt proves it (fail-closed).
            if predicate(receipt):
                out.add(member)
            continue
        module = _MEMBER_CAPABILITY_MODULE.get(member)
        if module is None or _module_findable(module):
            out.add(member)
    return out


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    out=None,
    err=None,
) -> int:
    """CLI entry point sourced by ``gt_ae_block.sh``.

    ``--emit-exports``: print ``export GT_X=<shell-quoted-value>`` for each
    resolved member on success (rc 0). On a preflight abort, print
    ``GT_RL_PREFLIGHT_ABORT: ...`` to stderr, emit NO exports, and return 3.
    Profile OFF => rc 0, no output (byte-identical no-op).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    emit = "--emit-exports" in argv

    missing = preflight(env, _available_from_env(env))
    if missing:
        err.write(
            "GT_RL_PREFLIGHT_ABORT: profile=%s unavailable=%s\n"
            % (_profile_token(env) or "<unset>", ",".join(missing))
        )
        return 3

    if emit:
        for key, value in resolve_profile(env).items():
            out.write("export %s=%s\n" % (key, shlex.quote(value)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

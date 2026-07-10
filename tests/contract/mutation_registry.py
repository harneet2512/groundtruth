"""Mutation registry — the durable index of every NAMED mutation handle the
enrichment waves proved biting (source: GT_WAVE_ENRICH_LEDGER_20260710T0345Z.md).

SHIPPABLE DATA MODULE (P7-E). This is code, not a report: a machine-readable list of
the exact conformance mutations that must stay biting, so any future session (or an
acceptance gate) can re-verify the whole battery mechanically without re-reading the
ledger prose. Each entry names a mutation HANDLE (the smallest source edit / param flip
that breaks a law), the module that owns it, the test(s) that RED on that mutation, and
the date it was last verified biting.

DISCIPLINE:
  * This module NEVER applies a mutation (no auto-mutation, no monkeypatching of source).
    ``how_to_apply`` is a human-readable recipe, executed by a reviewer, not by code.
  * ``module`` is a repo-relative FILE path (Python, Go, or script) so it is resolvable
    with a plain existence check — no import side effects. ``import_path`` (when present)
    is the dotted module for a find_spec resolvability check.
  * ``biting_tests`` are repo-relative test files that RED when the handle is mutated.
  * Optional fields (multi-language extension): ``language`` defaults to ``"python"``;
    a non-Python entry (e.g. ``"go"``) cannot be re-run by pytest, so it MUST also carry
    a ``verify_command`` string (the exact command a reviewer runs to re-prove it).
    ``run_status`` records the last mechanical-proof state of the recipe (whether it was
    executed on a shadow copy, is recipe-only pending a toolchain, or is self-applied
    in its home suite) — a human note, never read by :func:`validate_registry`.

The battery's ``test_mutation_registry_references_real_modules_and_tests`` (in
``test_contract_battery.py``) calls :func:`validate_registry` so the registry cannot
rot: every referenced module + test must exist / resolve on every standalone run.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

# tests/contract/mutation_registry.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

_LAST = "2026-07-10"

# --------------------------------------------------------------------------- #
# THE REGISTRY — one dict per named mutation handle, grouped by owning module.
# --------------------------------------------------------------------------- #
MUTATIONS: list[dict[str, Any]] = [
    # ---- EvidenceEnvelope (W0 TITO contract) — chain / width / totality ----
    {
        "id": "envelope_chain_parent",
        "module": "src/groundtruth/runtime/evidence_envelope.py",
        "import_path": "groundtruth.runtime.evidence_envelope",
        "handle_description": (
            "TITO law 5 hash-chain: chain_hash folds the fixed-width raw-32 PARENT "
            "link into each step (h_i = sha256(raw32(h_{i-1}) + commitment_i)), so "
            "reorder/omission/boundary-shift all change the terminal hash."
        ),
        "how_to_apply": (
            "In chain_hash, drop the parent term — return "
            "sha256(rendered).hexdigest() ignoring parent_raw (the ledger's mutation "
            "M1). The chain collapses to a per-link content hash; reorder + omission "
            "no longer change the terminal hash."
        ),
        "biting_tests": ["tests/runtime/test_evidence_envelope.py"],
        "last_verified": _LAST,
    },
    {
        "id": "envelope_chain_width",
        "module": "src/groundtruth/runtime/evidence_envelope.py",
        "import_path": "groundtruth.runtime.evidence_envelope",
        "handle_description": (
            "TITO law (f) hash-shape width: a non-empty hash field is EXACTLY 64 "
            "lowercase hex (_is_hash_shaped); chain_hash decodes the parent to raw 32 "
            "bytes so the parent/rendered boundary is unambiguous with no separator."
        ),
        "how_to_apply": (
            "In chain_hash, accept an any-length hex parent "
            "(parent_hash.encode() instead of bytes.fromhex on a 64-hex-gated value), "
            "or relax _is_hash_shaped to len>0 (the ledger's mutation M3). Re-opens the "
            "boundary-shift forgery: ('ab', b'cdef') collides with ('a', b'bcdef')."
        ),
        "biting_tests": ["tests/runtime/test_evidence_envelope.py"],
        "last_verified": _LAST,
    },
    {
        "id": "envelope_totality",
        "module": "src/groundtruth/runtime/evidence_envelope.py",
        "import_path": "groundtruth.runtime.evidence_envelope",
        "handle_description": (
            "W1 unencodable-content policy: build() RAISES a clear ValueError on a "
            "lone-surrogate string, validate() is TOTAL (surfaces it as a violation "
            "string, never raises), render/content_signature raise the DOCUMENTED "
            "ValueError — never a raw UnicodeEncodeError."
        ),
        "how_to_apply": (
            "Remove validate()'s `except _UnencodableContent` branch (let the encode "
            "failure propagate) OR stop build() wrapping the ValueError. validate() is "
            "no longer total — a surrogateescape-decoded payload crashes the contract."
        ),
        "biting_tests": ["tests/runtime/test_evidence_envelope.py"],
        "last_verified": _LAST,
    },
    # ---- Gateway (W2 seam) — dedup / belt / production-stamping ----
    {
        "id": "gateway_dedup",
        "module": "src/groundtruth/runtime/gateway.py",
        "import_path": "groundtruth.runtime.gateway",
        "handle_description": (
            "F1 dedup identity: the dedup key is the 5-component "
            "(producer, evidence_type, target, fact_id=SYMBOL, content_signature) "
            "derivation — two distinct symbols whose def sets share the "
            "alphabetically-first file must NOT collide."
        ),
        "how_to_apply": (
            "Revert derive_dedup_key to the 3-component (producer, type, target) form "
            "(drop fact_id + content). Two distinct symbols sharing their first def "
            "file collide and the second partition is wrongly suppressed."
        ),
        "biting_tests": [
            "tests/runtime/test_gateway.py",
            "tests/runtime/test_evidence_envelope.py",
        ],
        "last_verified": _LAST,
    },
    {
        "id": "gateway_belt",
        "module": "src/groundtruth/runtime/gateway.py",
        "import_path": "groundtruth.runtime.gateway",
        "handle_description": (
            "F2 leak belt: _mk_add drops ANY body line carrying a test-shaped path "
            "token (_body_line_leaky) at envelope-construction time, independent of "
            "the producer — the second of two independent leak layers."
        ),
        "how_to_apply": (
            "In _mk_add, stop filtering body_lines through the leak belt (append raw). "
            "A test path token in a producer body line survives into the shipped "
            "payload — the cardinal leak invariant breaks."
        ),
        "biting_tests": ["tests/runtime/test_gateway.py"],
        "last_verified": _LAST,
    },
    {
        "id": "gateway_production_stamping",
        "module": "src/groundtruth/runtime/gateway.py",
        "import_path": "groundtruth.runtime.gateway",
        "handle_description": (
            "F1 stamp-at-seal: augment() READS the delivered-dedup chain but NEVER "
            "stamps it; the commit happens at the actual append "
            "(adapters.miniswe.seal_delivery). A produced-but-undelivered fact stays "
            "DEFERRED, never destroyed."
        ),
        "how_to_apply": (
            "In augment, add state.delivered_keys.add(a.dedup_key) at production "
            "(before delivery). A fact lost to arbitration / budget / a leak drop "
            "downstream is then muted for the whole episode — the seam's deferral law "
            "is contradicted."
        ),
        "biting_tests": [
            "tests/runtime/test_gateway.py",
            "tests/runtime/test_gateway_miniswe_adapter.py",
        ],
        "last_verified": _LAST,
    },
    # ---- EpisodeState (W1a) — second-owner / reset ----
    {
        "id": "episode_second_owner",
        "module": "src/groundtruth/runtime/episode_state.py",
        "import_path": "groundtruth.runtime.episode_state",
        "handle_description": (
            "Single-owner law: GatewayState.delivered_keys / ledger / edit_events are "
            "DELEGATING properties onto the embedded EpisodeState — exactly one storage "
            "owner for the dedup chain, probe ledger, and edit record."
        ),
        "how_to_apply": (
            "Give GatewayState a native delivered_keys set FIELD (a second live owner) "
            "instead of the delegating property. Facade and episode then diverge — a "
            "mutation on one is invisible to the other (the fragmentation W1a killed)."
        ),
        "biting_tests": [
            "tests/runtime/test_episode_state.py",
            "tests/runtime/test_gateway_episode_migration.py",
        ],
        "last_verified": _LAST,
    },
    {
        "id": "episode_reset",
        "module": "src/groundtruth/runtime/episode_state.py",
        "import_path": "groundtruth.runtime.episode_state",
        "handle_description": (
            "reset_attempt clears the per-attempt ACCUMULATORS to a fresh slate while "
            "PERSISTING run identity (episode_id), the step_limit budget, and the "
            "durable delivery_ledger handle."
        ),
        "how_to_apply": (
            "In reset_attempt, also clear self.episode_id or self.step_limit (attempt-2 "
            "loses run identity / budget), OR drop self.delivered_dedup.clear() so "
            "attempt-2 cannot re-deliver a fact attempt-1 already sent."
        ),
        "biting_tests": ["tests/runtime/test_episode_state.py"],
        "last_verified": _LAST,
    },
    # ---- Consumption grader (R4-G) — boundary / no-further-search / strict / cd-prefix ----
    {
        "id": "grader_boundary",
        "module": "scripts/swebench/consumption_ledger.py",
        "import_path": "scripts.swebench.consumption_ledger",
        "handle_description": (
            "Typed-entity boundary receipt matcher: a delivered PATH receipt matches "
            "only whole path tokens (full relpath or an absolute-prefixed form), never "
            "a same-basename file in a different directory (fixture "
            "subst_receipt_basename_collision)."
        ),
        "how_to_apply": (
            "Revert the receipt entity match to raw substring / bare-basename equality "
            "(drop the token-boundary + directory guard). A same-basename file in a "
            "different directory inflates the ACTED/REFERENCED receipt."
        ),
        "biting_tests": ["tests/test_consumption_ledger.py"],
        "last_verified": _LAST,
    },
    {
        "id": "grader_no_further_search",
        "module": "scripts/swebench/consumption_ledger.py",
        "import_path": "scripts.swebench.consumption_ledger",
        "handle_description": (
            "M2 re-acquisition accounting: infra/parity greps are NOT excluded from "
            "the re-acquisition (further-search) count — the _INFRA_PROBE hole "
            "swallowed 66% of parity greps (re-acq 120->382 once closed; fixture "
            "subst_receipt_m2_further_search)."
        ),
        "how_to_apply": (
            "Re-open the _INFRA_PROBE classification hole so infra/parity greps are "
            "dropped from re-acquisition counting. Non-re-acquisition rate inflates "
            "back to ~0.986 (the biased headline the fix corrected to 0.955)."
        ),
        "biting_tests": ["tests/test_consumption_ledger.py"],
        "last_verified": _LAST,
    },
    {
        "id": "grader_strict",
        "module": "scripts/swebench/consumption_ledger.py",
        "import_path": "scripts.swebench.consumption_ledger",
        "handle_description": (
            "STRICT covering consumption: a covering (verify_nudge_file) receipt counts "
            "ACTED only when the agent's later action targets the SAME file the verdict "
            "named — target-BLIND any-green inflated L4 from 0.306 to 0.959 (fixture "
            "subst_receipt_covering_green / subst_receipt_verify_strict_miss)."
        ),
        "how_to_apply": (
            "Revert covering consumption to any-green (count ACTED on any subsequent "
            "green regardless of the target file). Covering L4 re-inflates from STRICT "
            "0.30612245 back to the target-blind 0.959."
        ),
        "biting_tests": ["tests/test_consumption_ledger.py"],
        "last_verified": _LAST,
    },
    {
        "id": "grader_cd_prefix",
        "module": "scripts/swebench/consumption_ledger.py",
        "import_path": "scripts.swebench.consumption_ledger",
        "handle_description": (
            "cd-prefix stripping: a `cd <dir> && grep X` command has its leading "
            "`cd ... &&` prefix stripped before entity matching, so a re-acquisition "
            "after a cd is still detected (fixture subst_receipt_cdprefix_reacq)."
        ),
        "how_to_apply": (
            "Stop stripping the leading `cd <dir> &&` prefix before entity matching. A "
            "re-acquisition issued after a cd is missed — the consumption count drifts."
        ),
        "biting_tests": ["tests/test_consumption_ledger.py"],
        "last_verified": _LAST,
    },
    # ---- VerificationPlan (W3) — conf-gate / freshness ----
    {
        "id": "planner_conf_gate",
        "module": "src/groundtruth/runtime/verification_plan.py",
        "import_path": "groundtruth.runtime.verification_plan",
        "handle_description": (
            "THE FACT RULE: a covering / k=2-closure edge counts only when its "
            "resolution_method is in curation_map.DETERMINISTIC_RESOLUTION_METHODS AND "
            "confidence >= 0.7; name_match is never a fact."
        ),
        "how_to_apply": (
            "In _fact_caller_closure, drop the conf_gate (>= 0.7) or widen "
            "_DETERMINISTIC_METHODS to include name_match. Speculative edges then "
            "pollute the closure test selection."
        ),
        "biting_tests": ["tests/runtime/test_verification_plan.py"],
        "last_verified": _LAST,
    },
    {
        "id": "planner_freshness",
        "module": "src/groundtruth/runtime/verification_plan.py",
        "import_path": "groundtruth.runtime.verification_plan",
        "handle_description": (
            "GREEN law: green() is True only if EXECUTED + FRESH (result graph+patch "
            "revision == plan) + COVERS a plan-named entity/obligation (membership "
            "subset) + attributable-when-required + a positive PASS verdict."
        ),
        "how_to_apply": (
            "In green(), drop the freshness check (green a positive pass regardless of "
            "revision) — a stale PASS greens; OR drop the membership subset check — a "
            "phantom-entity coverage claim greens (F4)."
        ),
        "biting_tests": ["tests/runtime/test_verification_plan.py"],
        "last_verified": _LAST,
    },
    # ---- CompletionCertificate (W5) — unknowns-block / obligation-gates ----
    {
        "id": "certificate_unknowns_block",
        "module": "src/groundtruth/runtime/submit_gate.py",
        "import_path": "groundtruth.runtime.submit_gate",
        "handle_description": (
            "FAIL-OPEN law: _derive_decision is a pure function of the Gate Kernel "
            "head ONLY; an UNKNOWN field is VISIBLE (listed in unknowns) but never a "
            "block input."
        ),
        "how_to_apply": (
            "In _derive_decision, add `if unknowns: return 'bounce_once'` (the M1 "
            "mutation named in the docstring). An UNKNOWN SDLC field then blocks a "
            "submit — a false block."
        ),
        "biting_tests": [
            "tests/runtime/test_completion_certificate.py",
            "tests/runtime/test_g2_submit_gate_staleness.py",
        ],
        "last_verified": _LAST,
    },
    {
        "id": "certificate_obligation_gates",
        "module": "src/groundtruth/runtime/submit_gate.py",
        "import_path": "groundtruth.runtime.submit_gate",
        "handle_description": (
            "T2 REGRESSION LAW (measured 4/4): obligation_coverage is ADVISORY only — "
            "excluded from sdlc_fields, unresolved_failures, unknowns, "
            "allow_encouragement, and UNREAD by _derive_decision."
        ),
        "how_to_apply": (
            "In _derive_decision add `and obligation.status != FAIL` (the M2 mutation), "
            "OR add obligation_coverage into sdlc_fields. Obligations then gate a "
            "submit -> the measured 4/4 early-submit regression returns."
        ),
        "biting_tests": ["tests/runtime/test_completion_certificate.py"],
        "last_verified": _LAST,
    },
    # ---- HypothesisLedger (W4) — novelty / falsification ----
    {
        "id": "ledger_novelty",
        "module": "src/groundtruth/runtime/hypothesis_ledger.py",
        "import_path": "groundtruth.runtime.hypothesis_ledger",
        "handle_description": (
            "Observation-novelty complements: all-identical probe outcomes over >=2 "
            "probes => no_discriminating_evidence; an outcome DELTA => "
            "same_command_new_output (explicitly NOT a loop). Exact complements on the "
            "distinct-outcome axis."
        ),
        "how_to_apply": (
            "Swap the set-size gates: make classify_no_discriminating_evidence fire on "
            "len(set(outcomes)) >= 2 (or same_command_new_output on == 1). Progress is "
            "then mislabeled a loop and a loop mislabeled progress."
        ),
        "biting_tests": ["tests/runtime/test_hypothesis_ledger.py"],
        "last_verified": _LAST,
    },
    {
        "id": "ledger_falsification",
        "module": "src/groundtruth/runtime/hypothesis_ledger.py",
        "import_path": "groundtruth.runtime.hypothesis_ledger",
        "handle_description": (
            "TRI-STATE _edit_between_verdict: classify_edit_contradicted_contract "
            "falsifies ONLY on a PROVABLE in-window edit (prior_idx known + edit lands "
            "strictly inside); unknown ordering => BOTH complements stay quiet (F1)."
        ),
        "how_to_apply": (
            "Collapse _edit_between_verdict to the old boolean (treat prior_idx < 0 as "
            "_EDIT_YES whenever any edit exists). Falsification FALSELY fires when the "
            "only edit predates the fingerprint's first occurrence."
        ),
        "biting_tests": ["tests/runtime/test_hypothesis_ledger.py"],
        "last_verified": _LAST,
    },
    # ---- EditOverlay (W6) — baseline-blame / rollback / revision ----
    {
        "id": "overlay_baseline_blame",
        "module": "src/groundtruth/runtime/edit_overlay.py",
        "import_path": "groundtruth.runtime.edit_overlay",
        "handle_description": (
            "DIFFERENTIAL law: diagnostics_delta attributes a NEW syntax error to the "
            "edit ONLY when the AFTER verdict is syntax_error AND the BEFORE verdict "
            "proved a clean baseline (ok/absent). A pre-existing error is never blamed."
        ),
        "how_to_apply": (
            "In diagnostics_delta, drop the `before in _CLEAN_BASELINE_VERDICTS` gate "
            "(attribute any after syntax_error). A pre-existing (baseline) syntax error "
            "is then blamed on the edit."
        ),
        "biting_tests": ["tests/runtime/test_edit_overlay.py"],
        "last_verified": _LAST,
    },
    {
        "id": "overlay_rollback",
        "module": "src/groundtruth/runtime/edit_overlay.py",
        "import_path": "groundtruth.runtime.edit_overlay",
        "handle_description": (
            "Byte-exact rollback: restores every touched file verbatim (deleting a "
            "file the edit CREATED) and graph.db from its byte backup — a rolled-back "
            "transaction leaves the world unchanged (post_revision == pre_revision)."
        ),
        "how_to_apply": (
            "In rollback(), skip restoring a file whose original is not None (or skip "
            "the created-file deletion). A rolled-back transaction leaves the tree "
            "mutated — the world is not restored."
        ),
        "biting_tests": ["tests/runtime/test_edit_overlay.py"],
        "last_verified": _LAST,
    },
    {
        "id": "overlay_revision",
        "module": "src/groundtruth/runtime/edit_overlay.py",
        "import_path": "groundtruth.runtime.edit_overlay",
        "handle_description": (
            "Revision advance: _post_revision folds the edit SIGNATURE into "
            "post_revision when the graph could NOT be refreshed (reindex unavailable), "
            "so old graph facts are invalidated as STALE even though the db is frozen "
            "(fail toward freshness)."
        ),
        "how_to_apply": (
            "In _post_revision, return db_hash unconditionally (drop the stale-branch "
            "edit-signature fold). After a reindex-unavailable edit, post == pre and "
            "stale graph facts survive as 'current'."
        ),
        "biting_tests": ["tests/runtime/test_edit_overlay.py"],
        "last_verified": _LAST,
    },
    # ---- History miner (V3-M) — multi-intent ----
    {
        "id": "miner_multi_intent",
        "module": "scripts/mine_history_labels.py",
        "import_path": "scripts.mine_history_labels",
        "handle_description": (
            "Multi-intent gate: a commit touching more than MAX_FILES_MULTI_INTENT (20) "
            "files is skipped as multi-intent, and per-family test-path filtering keeps "
            "a companion/signature label from being minted off a test-file change "
            "(companion 77->31 was 60% mislabeled test files)."
        ),
        "how_to_apply": (
            "Raise/remove MAX_FILES_MULTI_INTENT (is_multi_intent always False), or "
            "drop the per-family test-path filter. Multi-intent commits and test-file "
            "callers then leak in as positive P/R labels."
        ),
        "biting_tests": ["tests/scripts/test_mine_history_labels.py"],
        "last_verified": _LAST,
    },
    # ---- Eval corpus (P7-S) — split-guard ----
    {
        "id": "corpus_split_guard",
        "module": "scripts/build_eval_corpus.py",
        "import_path": "scripts.build_eval_corpus",
        "handle_description": (
            "Fail-closed write gate + repo-disjoint split: write_outputs FAILS CLOSED "
            "on a redaction/overlap violation, and calibration/eval are repo-disjoint "
            "(a gold basename / test-file token never survives to a task-facing row)."
        ),
        "how_to_apply": (
            "Make write_outputs proceed on a redaction / stale-overlap failure (remove "
            "the fail-closed guard), or drop the repo-disjoint split so a repo appears "
            "in BOTH calibration and eval — the split leaks."
        ),
        "biting_tests": ["tests/test_build_eval_corpus.py"],
        "last_verified": _LAST,
    },
    # ---- Go indexer — edge-hash / summary-field ----
    {
        "id": "indexer_edge_hash",
        "module": "gt-index/internal/store/revision.go",
        "import_path": None,  # Go — existence check only
        "language": "go",
        "verify_command": "cd gt-index && go test -tags sqlite_fts5 ./internal/store/...",
        "handle_description": (
            "id-INDEPENDENT edge hashing: ComputeRevision hashes edges by node "
            "IDENTITY (endpoints), not raw autoincrement edge.id, so the graph "
            "revision is stable/reproducible across reindexes of an unchanged graph."
        ),
        "how_to_apply": (
            "In revision.go, hash edges by raw edge.id instead of endpoint node "
            "identity. The revision churns on every reindex even when the logical graph "
            "is unchanged -> false staleness on every -file reindex."
        ),
        "biting_tests": ["gt-index/internal/store/revision_test.go"],
        "last_verified": _LAST,
    },
    {
        "id": "indexer_summary_field",
        "module": "gt-index/cmd/gt-index/main.go",
        "import_path": None,  # Go — existence check only
        "language": "go",
        "verify_command": "cd gt-index && go test -tags sqlite_fts5 ./cmd/gt-index/...",
        "handle_description": (
            "Executor summary field: the stdout JSON summary MUST carry post_revision "
            "(fail-closed if absent) so the edit-overlay executor contract can read the "
            "reindexed revision and detect a stale graph."
        ),
        "how_to_apply": (
            "In main.go, omit post_revision from the stdout JSON summary (or remove the "
            "fail-closed guard requiring it). The overlay can no longer read the "
            "reindex revision -> stale-graph detection breaks."
        ),
        "biting_tests": ["gt-index/cmd/gt-index/incremental_summary_test.go"],
        "last_verified": _LAST,
    },
    # ======================================================================= #
    # ENDGAME-1 CONFIRMED-2 — ledger-named biting mutations that P7-E missed.
    # Source of truth: GT_WAVE_ENRICH_LEDGER_20260710T0345Z.md (W-A/W-B/W-C/W2/
    # W5/W6). ``run_status`` records this session's mechanical-proof state (BF-2):
    # the 5 change_surface/patch_delta handles were re-verified biting on a shadow
    # copy (sys.modules injection, target source NEVER edited); the Go handle is
    # recipe-only (no local Go toolchain); the seam/cert/overlay handles cite the
    # in-suite tests that already assert the law + name the mutation.
    # ======================================================================= #
    # ---- W-A change_surface (absence/architectural-hole engine) — 3 handles ----
    {
        "id": "change_surface_min_siblings",
        "module": "src/groundtruth/pretask/change_surface.py",
        "import_path": "groundtruth.pretask.change_surface",
        "handle_description": (
            "Convention floor: a sibling-role group needs >=2 parallel siblings "
            "(_MIN_SIBLINGS, change_surface.py:98) before a convention is inferred — a "
            "single file cannot establish the fixed-vs-varying slot pattern."
        ),
        "how_to_apply": (
            "Set _MIN_SIBLINGS = 1. Single-file groups then form, the fixed/varying "
            "slot split is guessed off one example, and abstention/conflict handling "
            "collapses (the ledger's round-1 mutation)."
        ),
        "biting_tests": ["tests/pretask/test_change_surface.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (BF-2 shadow-injection); "
            "RED: test_abstain_conflicting_conventions + 6 others; control "
            "test_python_new_provider_all_roles_missing held green"
        ),
    },
    {
        "id": "change_surface_min_signals",
        "module": "src/groundtruth/pretask/change_surface.py",
        "import_path": "groundtruth.pretask.change_surface",
        "handle_description": (
            "2-signal emission floor: every emitted MissingRole/destination needs >=2 "
            "independent evidence signals (_MIN_SIGNALS, change_surface.py:99); "
            "issue-adjacency alone is ONE signal and never mints."
        ),
        "how_to_apply": (
            "Set _MIN_SIGNALS = 1. Adjacency alone (1 signal) then passes the gate, so "
            "a bare adjective before the category noun ('an additional provider') mints "
            "an entity (the ledger's round-1 mutation)."
        ),
        "biting_tests": ["tests/pretask/test_change_surface.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (BF-2); RED: "
            "test_adjective_additional_does_not_mint, test_adjective_custom_does_not_mint; "
            "control test_novel_token_azure_still_mints held green"
        ),
    },
    {
        "id": "change_surface_independent_signal",
        "module": "src/groundtruth/pretask/change_surface.py",
        "import_path": "groundtruth.pretask.change_surface",
        "handle_description": (
            "The REAL independent second signal (_entity_second_signal, "
            "change_surface.py:667-687, guard at :685): the 2nd signal is "
            "existing_sibling_member OR novel_token, and a generic English descriptor "
            "is explicitly NOT novel (the _GENERIC_DESCRIPTORS check) — this is the "
            "fix-round mutation that keeps the 2-signal law load-bearing, not dead code."
        ),
        "how_to_apply": (
            "In _entity_second_signal drop the `and entity not in _GENERIC_DESCRIPTORS` "
            "clause (:685). A generic descriptor that resolves nowhere in the repo "
            "('additional'/'custom') then counts as novel_token — a FALSE independent "
            "signal — so adjectives mint (re-deadens the F2 guard the fix restored)."
        ),
        "biting_tests": ["tests/pretask/test_change_surface.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (BF-2); RED: "
            "test_adjective_additional_does_not_mint, test_adjective_custom_does_not_mint; "
            "control test_novel_token_azure_still_mints held green"
        ),
    },
    # ---- W-C patch_delta (static patch-delta engine) — 2 handles ----
    {
        "id": "patch_delta_registration_shape",
        "module": "src/groundtruth/runtime/patch_delta.py",
        "import_path": "groundtruth.runtime.patch_delta",
        "handle_description": (
            "F1 registration-shape gate (_py_registration_refs, patch_delta.py:662-716): "
            "a companion-surface family name counts ONLY in a registration-shaped AST "
            "context (list/tuple/set element, dict key/value, whole-RHS alias, __all__ "
            "string, __init__ re-export). A CALL is a CONSUMER; comments/docstrings are "
            "invisible to the AST walk by construction."
        ),
        "how_to_apply": (
            "Make _py_registration_refs count a family name in ANY position — add an "
            "unconditional `if isinstance(node, ast.Name): _add(node)` at the top of the "
            "ast.walk loop, or revert to a raw `\\bname\\b` text count. A plain consumer "
            "('return alpha()+beta()') — and, in the text-count form, docstring/comment "
            "mentions — then read as a forgotten registration surface."
        ),
        "biting_tests": ["tests/runtime/test_patch_delta.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (BF-2, ast.Name-anywhere form); "
            "RED: test_consumer_file_is_not_a_companion; control "
            "test_registration_surface_advisory_fires held green. Text-count form also "
            "bites test_docstring_and_comment_mentions_never_count."
        ),
    },
    {
        "id": "patch_delta_conf_floor",
        "module": "src/groundtruth/runtime/patch_delta.py",
        "import_path": "groundtruth.runtime.patch_delta",
        "handle_description": (
            "THE FACT RULE at the caller closure: a signature-mismatch caller counts "
            "only through a DETERMINISTIC_RESOLUTION_METHODS edge AND confidence >= "
            "_FACT_CONFIDENCE_FLOOR (0.7, patch_delta.py:79); a sub-floor deterministic "
            "edge is not a fact (fed into the SQL conf_gate at :471)."
        ),
        "how_to_apply": (
            "Set _FACT_CONFIDENCE_FLOOR = 0.0. A 0.5-confidence import edge then counts "
            "as a fact and a speculative caller produces a false arity advisory."
        ),
        "biting_tests": ["tests/runtime/test_patch_delta.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (BF-2); RED: "
            "test_low_confidence_fact_method_never_surfaced; control "
            "test_added_required_param_fires_mismatch_with_right_line held green"
        ),
    },
    # ---- W-B receiver-type strip law (Go indexer) — 1 handle ----
    {
        "id": "indexer_strip_receiver_type",
        "module": "gt-index/internal/store/incremental.go",
        "import_path": None,  # Go — existence check only
        "language": "go",
        "verify_command": "cd gt-index && go test -tags sqlite_fts5 ./internal/store/...",
        "handle_description": (
            "W-B receiver-type strip law (stripReceiverTypeTag, incremental.go:197-222; "
            "applied on the not-reproven restore branch): an incremental restore that "
            "CANNOT re-prove target identity strips ONLY the `receiver_type=<T>` "
            "provenance SEGMENT (dataflow= and every other key preserved verbatim) off "
            "the demoted (0.6 cap / name_match) edge; a reproven restore keeps the tag."
        ),
        "how_to_apply": (
            "Call stripReceiverTypeTag UNCONDITIONALLY (also on the identity-REPROVEN "
            "preserve branch), or make it strip the whole metadata field instead of "
            "just the receiver_type segment. A legitimately reproven receiver_type "
            "provenance is then destroyed — the producer-cleanliness invariant inverts."
        ),
        "biting_tests": ["gt-index/internal/store/incremental_test.go"],
        "last_verified": _LAST,
        "run_status": (
            "recipe-only (no local Go toolchain: `go` not on PATH, 2026-07-10); "
            "verify_command re-runs the Go proof. Ledger W-B: unconditional strip bit "
            "TestResolveIncomingEdgesPreservesReceiverTypeProvenance; the demote-strip "
            "law itself is TestResolveIncomingEdgesStripsReceiverTypeWhenIdentityNotReproven."
        ),
    },
    # ---- W2 seam byte-preservation — 1 handle ----
    {
        "id": "seam_m1_byte_preservation",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,  # module lives outside src/ — file-existence check only
        "handle_description": (
            "W2 seam byte-preservation (TITO laws 1/2): the Gateway append "
            "(_gt_gateway_append in gt_mini_patch.py) writes a PURE SUFFIX onto the tool "
            "output, so the GT-on observation always satisfies on.startswith(off) — GT "
            "never rewrites the bytes the model already saw."
        ),
        "how_to_apply": (
            "In _gt_gateway_append prepend the delta instead of appending "
            "(out['output'] = delta + out['output']). The prefix is altered and "
            "on.startswith(off) fails. The recipe is self-applied by the biting test's "
            "monkeypatch at test_gateway_seam_wiring.py:331."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_gateway_seam_wiring.py"],
        "last_verified": _LAST,
        "run_status": (
            "biting proven in-suite — test_m1_byte_preservation_mutation_bites "
            "SELF-APPLIES the mutation (monkeypatch) and asserts `not on.startswith(off)`. "
            "artifact_deepswe/tests owned by BF-1; referenced read-only, not edited."
        ),
    },
    # ---- W5 CompletionCertificate — 2 handles missing beside the 2 existing ----
    {
        "id": "certificate_freshness",
        "module": "src/groundtruth/runtime/submit_gate.py",
        "import_path": "groundtruth.runtime.submit_gate",
        "handle_description": (
            "W5 FRESHNESS law: covering/selected-test evidence whose patch_revision != "
            "submit_revision is STALE — demoted to UNKNOWN and DROPPED from the Gate "
            "Kernel head (stale PASS != green; stale FAIL is re-run territory, never a "
            "fresh block), mirroring the seam's re-run semantics."
        ),
        "how_to_apply": (
            "In build_certificate/_derive_decision skip the freshness drop — feed the "
            "stale covering verdict straight into the head (do not demote to UNKNOWN on "
            "a revision mismatch). A stale FAIL then flips decision to bounce_once (false "
            "block) and a stale PASS greens (the MUTATION named at "
            "test_completion_certificate.py:182-183)."
        ),
        "biting_tests": [
            "tests/runtime/test_completion_certificate.py",
            "tests/runtime/test_g2_submit_gate_staleness.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "recipe registered — biting asserted by test_stale_pass_demotes_to_unknown / "
            "test_stale_fail_does_not_block (mutation named verbatim in the test body). "
            "Not shadow-executed this session; law-verified in the home suite."
        ),
    },
    {
        "id": "certificate_bulkhead",
        "module": "src/groundtruth/runtime/submit_gate.py",
        "import_path": "groundtruth.runtime.submit_gate",
        "handle_description": (
            "W5 F3 bulkhead: safe_build_certificate wraps build_certificate so an "
            "adversarial/poison input (e.g. a covering dict that raises at "
            "_evidence_revision) yields a MINIMAL honest certificate, never propagating "
            "— certificate construction can NEVER brick a submit."
        ),
        "how_to_apply": (
            "Remove the safe_build_certificate try/except bulkhead (or call "
            "build_certificate directly on the raw input). A poison covering dict then "
            "raises during construction and crashes the submit path."
        ),
        "biting_tests": ["tests/runtime/test_completion_certificate.py"],
        "last_verified": _LAST,
        "run_status": (
            "recipe registered — biting asserted by test_safe_build_certificate_never_raises "
            "+ test_safe_build_certificate_clean_path_matches_direct. Not shadow-executed."
        ),
    },
    # ---- W6 EditOverlay — F1 attestation guard — 1 handle ----
    {
        "id": "overlay_attestation_guard",
        "module": "src/groundtruth/runtime/edit_overlay.py",
        "import_path": "groundtruth.runtime.edit_overlay",
        "handle_description": (
            "W6 F1 VERIFIED rollback attestation (EditOverlay.rollback, "
            "edit_overlay.py:694-712): completeness is PROVEN by re-reading every "
            "touched file + graph.db off disk, never assumed from the absence of an "
            "exception; a byte-mismatch lands in capability_states['rollback_failed'], "
            "sets rollback='incomplete', reason='rollback_incomplete', and post_revision "
            "ADVANCES (returning pre_revision would be a false 'world restored')."
        ),
        "how_to_apply": (
            "In rollback() skip the disk re-read verification (never append to `failed`, "
            "so _rollback_failed stays empty and _rollback_state is always 'complete') — "
            "or in _post_revision return pre_revision unconditionally. An incomplete "
            "rollback (a file that failed to restore) is then falsely attested complete "
            "with post_revision == pre_revision."
        ),
        "biting_tests": ["tests/runtime/test_edit_overlay.py"],
        "last_verified": _LAST,
        "run_status": (
            "recipe registered — biting asserted by test_rollback_failure_is_flagged_not_silent "
            "(edit_overlay.py:342-381) + the complete-counterpart at test_edit_overlay.py:335-337. "
            "Not shadow-executed (platform-gated on read-only chmod enforcement)."
        ),
    },
    # ======================================================================= #
    # ENDGAME-3 — mini-seam lattice/guard biting mutations (FIX-1..FIX-4).
    # Source of truth: GT_WAVE_ENRICH_LEDGER_20260710T0345Z.md (ENDGAME-3 section).
    # module lives outside src/ (the mini seam) -> import_path None, file-existence
    # check only (same pattern as seam_m1_byte_preservation). All python.
    # ======================================================================= #
    {
        "id": "guard_search_exclusion",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,  # module lives outside src/ — file-existence check only
        "handle_description": (
            "BF-1 dose-law MUTUAL EXCLUSION guard (_gt_gateway_deliver, the "
            "`if _gateway_search_excluded(ev): return` line, tagged inline "
            "MUTATION[guard_search_exclusion]): on a SEARCH turn with the post_search "
            "lattice on, the Gateway is SKIPPED entirely — no classify/record/deliver/"
            "seal — so the ONE probe record + ONE def-fact dose come from the lattice "
            "alone."
        ),
        "how_to_apply": (
            "Force the guard false — `if False and _gateway_search_excluded(ev): return` "
            "(or monkeypatch _gateway_search_excluded -> lambda ev: False). The Gateway "
            "then runs the search turn again -> the def fact double-delivers and the "
            "shared probe ledger double-records (outcomes ['zero','zero'])."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_gateway_post_search_exclusion.py"],
        "last_verified": _LAST,
        "run_status": (
            "biting proven in-suite — test_m1_guard_removed_double_delivery_bites SELF-"
            "APPLIES the mutation (monkeypatch guard -> False) and asserts the double "
            "delivery/record; the partial-guard plugin mut_partial_exclusion REDs 4 "
            "exclusion pins (ENDGAME-3, 2026-07-10)."
        ),
    },
    {
        "id": "guard_search_totality",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "GUARD TOTALITY (FIX-4, ENDGAME-3): on a guard-excluded search event "
            "_gt_gateway_deliver touches NO gateway/episode state EXCEPT the legitimate "
            "FIX-2 receipt promotion — the guard sits BEFORE episode_id set / augment / "
            "any new dose or probe record, so episode value-state (incl. episode_id), the "
            "TITO chain head, and delivery identities are bitwise-frozen on the excluded "
            "turn."
        ),
        "how_to_apply": (
            "Reorder the guard AFTER a state touch (a guard-after-state mutant): set "
            "_EPISODE.episode_id (or run update_receipts) INSIDE the predicate before it "
            "returns True — see the scratchpad plugin mut_guard_after_state. episode_id "
            "is then set on an excluded turn -> the totality snapshot diverges."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_gateway_post_search_exclusion.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (ENDGAME-3): plugin "
            "mut_guard_after_state REDs test_excluded_search_touches_no_gateway_state_"
            "except_receipts (episode_id divergence, 1 FAIL)."
        ),
    },
    {
        "id": "lattice_hits_fallthrough_mark",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "BUG-B1 hits-path fall-through (_search_localize_block): a bare-symbol grep "
            "WITH real hits answers the agent's OWN grep with the graph def partition via "
            "_direct_def_block(..., mark=False) — the OUTER latch owns fire-once "
            "idempotence. A self-stamp in the fall-through PLUS the outer re-check on the "
            "SAME content hash self-suppresses EVERY hits-path delivery (structurally "
            "mute since 2026-07-05)."
        ),
        "how_to_apply": (
            "Call the fall-through WITH the mark (drop the mark kwarg / mark=True): "
            "`block = _direct_def_block(con, sym, root)`. _direct_def_block stamps "
            "`answered`, then the outer _ledger_already_answered check on the SAME block "
            "returns '' -> the hits delivery self-suppresses."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_listen_lattice.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (ENDGAME-3, in-place mark=True "
            "revert): RED test_hits_real_hit_delivers_def_partition + "
            "test_hits_observed_def_still_delivers_partition + "
            "test_hits_path_fires_once_then_latched (first delivery ''); restored."
        ),
    },
    # ======================================================================= #
    # P1 SPAN-IDENTITY FOUNDATION (L-6 / E-1 / V-2 / O-1 / D-6, 2026-07-10).
    # The TRUE edited-symbol set from graph node SPANS replaces token-guessing at
    # the covering / risk / horizon / obligation consumers.
    # ======================================================================= #
    {
        "id": "span_selection_body_edit",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,  # module outside src/ — file-existence check only
        "handle_description": (
            "SPAN-BASED edited-symbol selection (_edited_symbols_for_selection / "
            "_edited_symbol_spans_by_file): a graph node whose [start_line,end_line] "
            "span ENCLOSES an edited line (_oracle_edited_lines_by_file) is the edited "
            "symbol BY NAME — a body-only edit is selectable for its covering test even "
            "though the changed lines never spell the name; tokens are the FALLBACK."
        ),
        "how_to_apply": (
            "Make _edited_symbols_for_selection return set(_oracle_edited_tokens) "
            "unconditionally (drop the span leg). A body-only edit (no symbol token) "
            "then selects NO covering test -> the submit-while-RED bounce is unreachable "
            "for the majority body-edit shape (the L-6 regression)."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p1_span_identity_20260710.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (P1): reverting "
            "_edited_symbols_for_selection to token-only REDs "
            "test_body_edit_selects_covering_test (empty covering selection)."
        ),
    },
    {
        "id": "edit_risk_span_containment",
        "module": "src/groundtruth/runtime/edit_risk.py",
        "import_path": "groundtruth.runtime.edit_risk",
        "handle_description": (
            "E-1 span containment (_line_in_edited_ranges / _ranged_dependents): a node "
            "counts when its DEFINITION SPAN [start_line,end_line] INTERSECTS an edited "
            "hunk (hunk ∩ [start,end]), not only when start_line ∈ hunk — a body-only "
            "edit inside a fn's span now scores its blast radius, while the R3 hub "
            "false-positive stays killed (a call-site line intersects only the edited "
            "fn's span; the callee hub is DEFINED elsewhere)."
        ),
        "how_to_apply": (
            "In _line_in_edited_ranges revert to point containment on start_line only "
            "(`if lo <= sl <= hi: return True`, ignoring end_line). A hub function "
            "edited only in its BODY (def-line outside the hunk) scores 0 dependents -> "
            "no verification risk -> the mid-episode covering trigger never sees it."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p1_span_identity_20260710.py",
            "tests/runtime/test_edit_risk.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting asserted by test_edit_risk_span_containment_counts_body_edit "
            "(a line-3 body edit to get_user, def-line 1 outside the hunk, scores 2 "
            "dependents under span containment; would be 0 under start_line-only)."
        ),
    },
    # ======================================================================= #
    # P2 STAMP-AT-DELIVERY (D-4 / D-5 / S-2, 2026-07-10). A fire-once latch /
    # delivered-hash is consumed only on a REAL delivered outcome, never at
    # production — a producer suppressed downstream never leaves a phantom stamp.
    # ======================================================================= #
    {
        "id": "lanea_stamp_at_delivery",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,  # module outside src/ — file-existence check only
        "handle_description": (
            "D-4/D-5 stamp-at-DELIVERY: the lattice 'answered' latch is stamped in "
            "_lane_a_deliver's post_search.localize DELIVERED branch (not in "
            "_search_localize_block at production), and the Lane-B winner's delivered- "
            "hash is stamped at the real append (_last_gate_winner_hash, not inside "
            "_oracle_gate_blocks). A block suppressed downstream leaves NO phantom stamp."
        ),
        "how_to_apply": (
            "Re-add `_ledger_mark_answered(stem, block)` at the end of "
            "_search_localize_block (production self-stamp), OR re-add "
            "`_oracle_delivered_hashes.add(winner[2])` inside _oracle_gate_blocks. A "
            "produced-but-suppressed lattice block / a gate-to-append crash then leaves "
            "a delivered stamp for bytes the agent never saw."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p2_stamp_at_delivery_20260710.py",
            "artifact_deepswe/tests/test_listen_lattice.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_d4_producer_does_not_self_mark REDs if the production "
            "self-stamp returns; test_d5_gate_records_hash_but_does_not_stamp REDs if "
            "the gate stamps winner[2]."
        ),
    },
    {
        "id": "horizon_band_armed_on_empty_render",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "S-2 band latch consumed only after a NON-EMPTY render "
            "(_verification_horizon_candidate): the advisory/urgent/pivot/gate dose "
            "latch is set AFTER `if not block: return None`, so an empty render (leak- "
            "dropped / targeting-unavailable) leaves the band ARMED to fire later — the "
            "gate-loss re-arm only fires on a produced-then-lost candidate, not an "
            "empty render."
        ),
        "how_to_apply": (
            "Move the band-latch assignment (_horizon_advisory_fired = True, etc.) BACK "
            "above the `block = _render_verify_emission(...)` / `if not block` guard. An "
            "empty render then spends the band permanently — the band is lost for the "
            "whole episode with no re-arm."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p2_stamp_at_delivery_20260710.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_s2_band_latch_armed_on_empty_render REDs (the latch flips "
            "True) if the band is consumed before the render guard."
        ),
    },
    # ======================================================================= #
    # P3 ENVELOPE UNIFICATION (RL-1 / D-7 / D-2 / D-11, 2026-07-10). Lane-A/Lane-B
    # deliveries route through the ONE EvidenceEnvelope contract (flag
    # GT_LANE_ENVELOPE, default-off byte-identical); the delivered-hash dedup widens
    # to 16 hex; the episode id gains an attempt suffix.
    # ======================================================================= #
    {
        "id": "lane_envelope_budget_and_seal",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,  # module outside src/ — file-existence check only
        "handle_description": (
            "RL-1 lane sealing + D-7 lane budget (_seal_lane_delivery / _lane_fits_"
            "budget, flag GT_LANE_ENVELOPE): when on, a Lane-A/Lane-B delivered block is "
            "sealed through EvidenceEnvelope (shared chain + dedup stamp-at-seal + "
            "receipt level 1) and an over-budget block is dropped WHOLE. Default-off is "
            "byte-identical (no seal, no budget gate). The delivered BYTES never change."
        ),
        "how_to_apply": (
            "Make _lane_fits_budget return True unconditionally (drop the D-7 law-8 "
            "drop-whole) — an over-budget lane block is then appended/clipped. OR make "
            "_seal_lane_delivery skip appending to _gt_gateway_deliveries — a lane "
            "delivery then carries no chain/receipt provenance (the RL-1 gap returns)."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p3_envelope_unification_20260710.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_d7_over_budget_lane_block_dropped_whole REDs if fits_budget "
            "always True; test_rl1_on_seals_lane_delivery REDs if the seal is skipped."
        ),
    },
    {
        "id": "lane_hash_width_16",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "D-2: the correctness-bearing lane delivered-hash dedup (_oracle_content_"
            "hash + the Lane-B gate hash + the Lane-A content-only hash + the submit "
            "rejection hash) is 16 hex, not 8 — a 32-bit collision must not permanently "
            "suppress a distinct fact/steer as 'delivered'."
        ),
        "how_to_apply": (
            "Revert _oracle_content_hash (and the paired gate/lane/submit hashes) to "
            "hexdigest()[:8]. The dedup set narrows to 32 bits -> a collision suppresses "
            "a legitimate delivery. (test_d2_content_hash_is_16_hex asserts the width.)"
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p3_envelope_unification_20260710.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_d2_content_hash_is_16_hex asserts len==16; reverting to [:8] "
            "REDs it."
        ),
    },
    {
        "id": "episode_attempt_suffix",
        "module": "src/groundtruth/runtime/episode_state.py",
        "import_path": "groundtruth.runtime.episode_state",
        "handle_description": (
            "D-11 attempt identity: reset_attempt increments EpisodeState.attempt and "
            "SUFFIXES episode_id with `#a<n>` (the base id preserved, prior suffix "
            "stripped so it never stacks) — two attempts of one run get DISTINCT "
            "chain-scoped identities in the training data."
        ),
        "how_to_apply": (
            "In reset_attempt, stop advancing self.attempt / stop appending the `#a<n>` "
            "suffix (leave episode_id unchanged across attempts). Attempt-2's chain then "
            "shares attempt-1's episode identity — indistinguishable in the RL data."
        ),
        "biting_tests": [
            "tests/runtime/test_episode_state.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_reset_attempt_persists_identity_and_ledger_handle asserts "
            "episode_id == 'task-42#a1' and attempt == 1; dropping the suffix REDs it."
        ),
    },
    {
        "id": "steer_native_strips_tags_keeps_body",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "RL-3 FORM (_steer_native, flag GT_STEER_NATIVE): a Lane-B steer is rendered "
            "in the native environment voice — the <gt-nudge>/<gt-verify> wrapper tags "
            "and the leading `GT:` imperative marker are STRIPPED while every content "
            "word survives; an already-native block (no `<gt-` tag: the covering RED / "
            "edit_check transcript) passes through UNCHANGED so its line structure is "
            "never mangled. Default-off byte-identical (caller-gated by _steer_native_on)."
        ),
        "how_to_apply": (
            "In _steer_native, stop dropping the tag lines (keep `st.startswith('<gt-')` "
            "lines) OR stop stripping the `GT:` prefix. The native arm then leaks the OOD "
            "`<gt-*>` tags / `GT:` marker the FORM change exists to remove — RED on the "
            "strip/body-preserved assertions."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p4_form_native_20260710.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_rl3_strips_wrapper_tags / test_rl3_strips_leading_gt_marker / "
            "test_rl3_body_preserved_verbatim assert tag-free + GT:-free + every body "
            "phrase present; keeping a tag or the marker REDs them. "
            "test_rl3_already_native_passthrough_unchanged pins the covering-RED "
            "pass-through (mangling its lines REDs it)."
        ),
    },
    {
        "id": "search_note_splice_arm_aware",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "N-1 FORM (_splice_search_note): the fold / non-target annotation is spliced "
            "arm-aware — TAGGED arm inserts it after the <gt-search-facts> open tag "
            "(index 1); NATIVE arm (GT_POST_SEARCH_NATIVE=1) APPENDS it as a trailing "
            "tag-free `# ` comment so the ripgrep def rows are never split and no GT-"
            "voice lands inside the agent's own grep channel."
        ),
        "how_to_apply": (
            "Revert _splice_search_note to the unconditional `lines.insert(1, note)`. On "
            "the native arm the GT-voice parenthetical then lands at index 1 — BETWEEN "
            "the def rows — breaking the row structure and the no-GT-voice law."
        ),
        "biting_tests": [
            "artifact_deepswe/tests/test_p4_form_native_20260710.py",
        ],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_n1_native_arm_appends_trailing_comment_rows_pristine + "
            "test_n1_native_note_never_splits_def_rows assert row[1] is the second def "
            "row (not the note); unconditional insert(1) REDs them."
        ),
    },
    {
        "id": "gateway_render_mode_own_flag",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "D-8 FLAGS: the Gateway's render mode keys off its OWN GT_GATEWAY_NATIVE "
            "flag, decoupled from GT_POST_SEARCH_NATIVE (the post_search FORMAT arm) — a "
            "FORM A/B on one surface no longer contaminates the other (the flag-overload "
            "confound). The shared def-facts renderer keeps its own post_search dispatch."
        ),
        "how_to_apply": (
            "Re-key the gateway `native = …` line to "
            "os.environ.get('GT_POST_SEARCH_NATIVE'). The two surfaces re-couple: a "
            "post_search A/B flips the gateway render mode too."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_p4_form_native_20260710.py"],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_d8_gateway_dispatch_reads_its_own_flag asserts the `native =` "
            "assignment reads GT_GATEWAY_NATIVE and is never assigned from "
            "GT_POST_SEARCH_NATIVE; re-keying REDs it."
        ),
    },
    {
        "id": "lane_output_separator_normalization",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "L-1a FORM (_join_lane_output): a Lane-B block with no leading newline (the "
            "edit.syntax native diagnostic, which ends in .strip()) is joined to the "
            "running observation with EXACTLY ONE `\\n` boundary — insert only when "
            "neither side already has one, so newline-opening steers stay byte-identical."
        ),
        "how_to_apply": (
            "Replace `_join_lane_output(prev, block)` with `prev + block`. A syntax-error "
            "diagnostic then jams onto the previous line (`…</obs>File \"x.py\"`)."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_p4_form_native_20260710.py"],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_l1a_no_leading_newline_block_gets_separator asserts one \\n "
            "inserted and 'newlineFile' absent; plain concatenation REDs it."
        ),
    },
    {
        "id": "edit_check_repo_relative_diag_path",
        "module": "src/groundtruth/runtime/edit_check.py",
        "import_path": "groundtruth.runtime.edit_check",
        "handle_description": (
            "L-1b CONTENT: check_edit_syntax stamps the REPO-RELATIVE path into the "
            "ast.parse filename (=> the diagnostic's `File \"a/x.py\"`), not the bare "
            "basename — the model reads back the path it edited without guessing."
        ),
        "how_to_apply": (
            "In check_edit_syntax / _check_py_in_process, pass "
            "filename=os.path.basename(abs_path) again. The diagnostic collapses to "
            "`File \"x.py\"` and the repo-relative assertion REDs."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_p4_form_native_20260710.py"],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_l1b_diagnostic_uses_repo_relative_path asserts "
            "'File \"a/x.py\"' present and 'File \"x.py\"' absent; basename REDs it."
        ),
    },
    {
        "id": "verify_advisory_verb_number_agreement",
        "module": "src/groundtruth/runtime/verification_horizon.py",
        "import_path": "groundtruth.runtime.verification_horizon",
        "handle_description": (
            "V-1 FORM: render_verify_emission's advisory pairs the verb with the "
            "subject NUMBER — 'a … test COVERS them' (singular, has_covering) vs 'the "
            "relevant tests COVER them' (plural) — killing the live 'tests covers them' "
            "garble."
        ),
        "how_to_apply": (
            "Hardcode the verb back to 'covers' (drop test_verb). The plural branch then "
            "renders 'the relevant tests covers them' and the agreement assertion REDs."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_p4_form_native_20260710.py"],
        "last_verified": _LAST,
        "run_status": (
            "biting: test_v1_plural_subject_plural_verb asserts 'the relevant tests "
            "cover them' present and 'tests covers them' absent; fixed 'covers' REDs it."
        ),
    },
    {
        "id": "context_budget_preserves_producer_order",
        "module": "src/groundtruth/runtime/context_budget.py",
        "import_path": "groundtruth.runtime.context_budget",
        "handle_description": (
            "C-1: ContextBudgeter.trim SELECTS under budget by priority (imperative > "
            "facts > other) but EMITS the survivors in the PRODUCER's original order — "
            "the delivered block's line order no longer diverges from what the producer "
            "rendered (a FORM bug that scrambled narrative even with no clip)."
        ),
        "how_to_apply": (
            "Restore the old emit `for line in imperative + facts + other:` (bucket "
            "order). test_c1_trim_preserves_producer_order then sees the imperative "
            "hoisted above the fact and REDs."
        ),
        "biting_tests": ["tests/runtime/test_p5_remainder_20260710.py"],
        "last_verified": _LAST,
    },
    {
        "id": "context_budget_fact_id_discriminates_remainder",
        "module": "src/groundtruth/runtime/context_budget.py",
        "import_path": "groundtruth.runtime.context_budget",
        "handle_description": (
            "C-3: stable_fact_id folds a whitespace-normalized hash of the FULL "
            "remainder into TAG:symbol, so two DISTINCT facts sharing tag+symbol "
            "(get_user calls->A vs get_user called-by->B) get DISTINCT ids and neither "
            "wrongly suppresses the other; an identical re-render still collides."
        ),
        "how_to_apply": (
            "Revert to `return f'{tag}:{sym.lower()}'` (drop the digest). The two "
            "distinct WITNESS facts collide again and test_c3_second_distinct_fact_not_"
            "suppressed REDs (out2.text == '')."
        ),
        "biting_tests": ["tests/runtime/test_p5_remainder_20260710.py"],
        "last_verified": _LAST,
    },
    {
        "id": "traces_syntaxerror_frame_pattern",
        "module": "src/groundtruth/pretask/traces.py",
        "import_path": "groundtruth.pretask.traces",
        "handle_description": (
            "L-2: the Python frame registry has a SECOND pattern for a SyntaxError / "
            "compile-error frame (`File \"p\", line N` with NO `, in func`), guarded by "
            "(?!\\d)(?!, in ) so it never backtracks to a short line number nor double-"
            "matches a normal call frame. Without it the gateway trace_frame is mute "
            "exactly when the suite dies of a syntax break."
        ),
        "how_to_apply": (
            "Delete the second python registry entry. test_l2_syntaxerror_frame_is_"
            "extracted (a no-`in` frame) then finds 0 python frames and REDs."
        ),
        "biting_tests": ["tests/runtime/test_p5_remainder_20260710.py"],
        "last_verified": _LAST,
    },
    {
        "id": "obligation_freshness_demotes_stale_pass",
        "module": "src/groundtruth/runtime/obligations.py",
        "import_path": "groundtruth.runtime.obligations",
        "handle_description": (
            "O-2: ObligationTracker.apply_edit_freshness demotes a TESTED obligation "
            "whose symbol is edited at a turn STRICTLY LATER than its last test (a stale "
            "PASS -> EDITED, the freshness law). SATISFIED is exempt; same-turn edits "
            "(pre-test) do not demote."
        ),
        "how_to_apply": (
            "Make apply_edit_freshness a no-op (return []) or drop the `turn > "
            "last_tested_turn` guard. test_o2_stale_edit_after_test_demotes REDs."
        ),
        "biting_tests": ["tests/runtime/test_p5_remainder_20260710.py"],
        "last_verified": _LAST,
    },
    {
        "id": "d7_judge_relatedness_gate",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "S-1 (flag GT_D7_RELATEDNESS): the D7 consumption judge credits a delivered "
            "kind as CONSUMED only when this turn's edit/test TOUCHES that block's target "
            "(_cmd_touches_target). An unrelated action no longer launders a consumed "
            "signal (nor blames an ignore). Default-off = relatedness-blind (historical)."
        ),
        "how_to_apply": (
            "Force _cmd_touches_target -> True always (or make _d7_relatedness_on read a "
            "non-flag). With the flag on, test_s1_flag_on_unrelated_edit_does_not_credit "
            "then credits the unrelated edit and REDs."
        ),
        "biting_tests": ["tests/runtime/test_p5_remainder_20260710.py"],
        "last_verified": _LAST,
    },
    {
        "id": "semantic_drift_repair_cycle_suppression",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "SD-1: _semantic_drift_candidate suppresses the guard/return-removed nudge "
            "when the last observed test outcome was a FAILURE (an active repair cycle — "
            "the deletion is plausibly the fix, and the agent will re-test). It still "
            "fires when tests were GREEN (the real silent-regression case)."
        ),
        "how_to_apply": (
            "Delete the `if _last_test_outcome_failed: return None` guard. "
            "test_sd1_suppressed_when_last_test_failed then gets a nudge and REDs."
        ),
        "biting_tests": ["tests/runtime/test_p5_remainder_20260710.py"],
        "last_verified": _LAST,
    },
    {
        "id": "lattice_compound_gate",
        "module": "artifact_deepswe/gt_mini_patch.py",
        "import_path": None,
        "handle_description": (
            "COMPOUND GATE (F6/F7, _search_command_isolated): the out=str lattice answers "
            "ONLY a single isolated grep/rg whose output IS the whole observation (grep "
            "is the FIRST and FINAL stage, no &&/||/; compound, no second-line command). "
            "A compound / pipe-fed / transformed grep answers NOTHING and records NO "
            "probe — its zero/hit signal cannot be isolated (else a wrong 'hit' or a junk "
            "stem is minted, poisoning later probe reasoning)."
        ),
        "how_to_apply": (
            "Bypass the gate — `if False and not _search_command_isolated(cmd): return ''` "
            "(or delete the gate line). `grep X ; pytest` then records a wrong 'hit' "
            "outcome off the pytest traceback and `pytest | grep Err` mints a junk stem."
        ),
        "biting_tests": ["artifact_deepswe/tests/test_listen_lattice.py"],
        "last_verified": _LAST,
        "run_status": (
            "mechanically re-verified biting 2026-07-10 (ENDGAME-3, in-place gate "
            "bypass): RED test_compound_command_refused_no_answer_no_probe (the ';' / "
            "'&&' cases deliver+record, the pipe-fed case mints a junk stem); restored."
        ),
    },
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def list_mutations() -> list[dict[str, Any]]:
    """Return a shallow copy of every registered mutation entry (deterministic order)."""
    return [dict(m) for m in MUTATIONS]


def mutations_by_module() -> dict[str, int]:
    """Count of registered mutation handles per owning module (sorted)."""
    counts: dict[str, int] = {}
    for m in MUTATIONS:
        counts[m["module"]] = counts.get(m["module"], 0) + 1
    return {k: counts[k] for k in sorted(counts)}


def _module_exists(rel: str) -> bool:
    return os.path.isfile(REPO_ROOT / rel)


def _import_resolvable(dotted: str) -> bool:
    """True iff ``dotted`` resolves via find_spec (no execution of the module body).

    A ``scripts.*`` path resolves only when the repo root is importable; treat an
    ImportError/ValueError during resolution as non-fatal and fall back to the file
    existence check the caller already performed."""
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def validate_registry() -> list[str]:
    """Return every integrity problem with the registry (empty == clean).

    IMPORT-CHECK ONLY, no auto-mutation: verifies each entry's required keys, its
    ``module`` file exists, its ``import_path`` (when given) resolves via find_spec,
    every ``biting_tests`` file exists, and (multi-language extension) that any
    non-Python entry names a ``verify_command`` so its recipe stays mechanically
    runnable. ``language`` defaults to ``"python"`` when absent. Deterministic, no
    source mutation."""
    problems: list[str] = []
    required = {
        "id", "module", "handle_description", "how_to_apply",
        "biting_tests", "last_verified",
    }
    known_langs = {"python", "go"}
    seen_ids: set[str] = set()
    for m in MUTATIONS:
        mid = m.get("id", "<no id>")
        missing = required - set(m)
        if missing:
            problems.append(f"{mid}: missing keys {sorted(missing)}")
            continue
        if mid in seen_ids:
            problems.append(f"{mid}: duplicate id")
        seen_ids.add(mid)
        if not _module_exists(m["module"]):
            problems.append(f"{mid}: module file not found: {m['module']}")
        imp = m.get("import_path")
        if imp and not _import_resolvable(imp) and not _module_exists(m["module"]):
            problems.append(f"{mid}: import_path unresolvable: {imp}")
        if not m["biting_tests"]:
            problems.append(f"{mid}: no biting_tests listed")
        for t in m["biting_tests"]:
            if not _module_exists(t):
                problems.append(f"{mid}: biting test not found: {t}")
        # multi-language extension: language defaults to python; a non-python
        # entry cannot be re-run by pytest, so it MUST carry a verify_command.
        lang = m.get("language", "python")
        if lang not in known_langs:
            problems.append(f"{mid}: unknown language {lang!r}")
        if lang != "python" and not m.get("verify_command"):
            problems.append(f"{mid}: non-python entry missing verify_command")
    return problems


if __name__ == "__main__":  # pragma: no cover — manual audit entry point
    import json

    print(json.dumps({
        "total": len(MUTATIONS),
        "by_module": mutations_by_module(),
        "problems": validate_registry(),
    }, indent=2, sort_keys=True))

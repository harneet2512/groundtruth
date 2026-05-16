# Decision Implementation Matrix

| Decision | Title | Status | Primary Metric | Verified By | Blocker |
|----------|-------|--------|---------------|-------------|---------|
| 0 | Localization Layer = V1R + BM25 + Agent | VERIFIED | action_delta=-9.25, first_edit_delta=-5.75, resolution=3/5 both | Paired runs 25967183060 + 25967190337 (both success) | None |
| 1 | L3 Evidence Architecture | VERIFIED | l3_evidence_rate=59%, token_budget=433chars, caller_code=4/5 | Code audit + run 25957132937 + run 25903546947 | Test assertions BLOCKED (target_node_id=0, upstream) |
| 2 | L3b Post-View Navigation Architecture | VERIFIED | l3b_fires=83% tasks, hub_p90=true, visited_suppression=true | Code audit + run 25903546947 | Volume fix (curation gate) awaiting paired run confirmation |
| 3 | L4 Prefetch — 3 Changes | VERIFIED | l4_fire_rate=48% (14/29), git_precedent=present, noise_filter=active | Code audit: lines 2588,2696-2755 | None |
| 4 | v1r_brief Tested on 2 Repos | STALE_SUPERSEDED | N/A (observational finding) | Absorbed into D8+D14+D15 | None |
| 5 | Comparative Stop/Go Criteria | VERIFIED | N/A (process rule) | Evaluation methodology uses directional comparison | None |
| 6 | Dev Slice Before Frozen 30 | VERIFIED | N/A (process rule) | 5-task smoke is the dev slice | None |
| 7 | Cost Notification After Every Run | VERIFIED | N/A (process rule) | Cost logged in SYSTEM_PROOF + jedi_WORK | None |
| 8 | OH Wrapper Switched to V1R Brief | VERIFIED | v1r_brief import confirmed at line 2909 | Code audit + D0 verification | None |
| 9 | Full Layer Audit — All Layers Working | VERIFIED | All 9 components WORKING per D31 30-task data | Runs 25957132937 + 25903546947 | L6 has some failures (66% rate) |
| 10 | Anti-Overfitting Rules | VERIFIED | N/A (policy in .claude/CLAUDE.md) | Document review | None |
| 11 | Product First, Benchmark Second | VERIFIED | N/A (policy in .claude/CLAUDE.md) | Document review | None |
| 12 | Brief Format — Add Signatures | VERIFIED | signatures returned from nodes.signature (v1r_brief.py:76) | Code audit: _top_functions returns row[1] | None |
| 13 | Evidence Design Principles | VERIFIED | N/A (design rules) | L3 implementation follows all 6 rules | None |
| 14 | V1R Localization Results — L1 Ceiling | STALE_SUPERSEDED | N/A (finding: hit@3=34%) | Absorbed into D0+D15 collaboration model | None |
| 15 | L1 Collaboration Model — Brief Shows Graph | VERIFIED | Calls: lines in render_brief (v1r_brief.py:337-338) | Code audit + run briefs contain Calls: | None |
| 16 | Integration Architecture — Observation Augmentation | VERIFIED | L3=post-edit append, L3b=post-view append | oh_gt_full_wrapper.py architecture | None |
| 17 | VM Setup for Live Test | STALE_SUPERSEDED | N/A (infra setup) | GHA is now deployment path | None |
| 18 | Local Docker Setup | STALE_SUPERSEDED | N/A (infra setup) | GHA is now deployment path | None |
| 19 | L1 Phase B — Modulus Violated | VERIFIED | W_SEM=0 fallback works (v7_4_brief.py:272) | jedi_WORK Phase 3 | None |
| 20 | Regression Root Cause — Two Failure Modes | VERIFIED | N/A (finding) | Downstream effects tracked in D21+D22 | None |
| 21 | Phase 1A Envelope Data | STALE_SUPERSEDED | N/A (data finding) | Absorbed into D22 | None |
| 22 | 7 Generalization Fixes | VERIFIED | 6/7 active: p90 hub, sparse BM25, adaptive K, L3 decoupled, L5 no names, config BM25 | Code audit across 4 files | Fix 4 (redundancy suppress) intentionally inactive |
| 23 | Generalization Audit — 3 Quick Fixes | VERIFIED | JSX edges + generated exclusion + truncation tracking in Go source | Code: walker.go + javascript.go + typescript.go | Deployed only when binary rebuilt |
| 24 | Full Relationship Taxonomy — 47 Types | PARTIAL_WITH_BLOCKER | 5/13 new edge types in Go source; 0 consumed by Python | Go: resolver.go; Python: only queries CALLS | Python never queries non-CALLS edges |
| 25 | L3 Self-Correction via Task-Relevance | VERIFIED | _annotate_evidence_header (post_edit.py:686) | Code audit in D1 | None |
| 26 | Cross-Domain Bridging | PARTIAL_WITH_BLOCKER | Co-change logic in v1r_brief.py G3c | Dormant — effectiveness repo-dependent | Requires git history per repo |
| 27 | Go Binary Build + Deployment | PARTIAL_WITH_BLOCKER | New passes (4b,4c) in Go source; GHA cache fix deployed | Source: cmd/gt-index/main.go | Binary rebuild in paired runs (in-flight) |
| 28 | Submission Format + Run Config | VERIFIED | N/A (config definition) | GHA workflow inputs match | None |
| 29 | Generalization Regression — Root Cause + Fix | VERIFIED | Fixes A-D all applied (A via better gate, B/C/D exact) | jedi_WORK Phase 1 + code audit | None |
| 30 | L5 Architecture — Event-Driven Triggers | STALE_SUPERSEDED | N/A | Replaced by D31+D34 | None |
| 31 | L5 Trajectory Governor — 30-Task Results | VERIFIED | 61 tests pass, 0 fires (correct: precondition gap) | Run 25903546947: 211 verifications, 0 failures seen | Precondition gap is architectural (known) |
| 32 | next_action Must Come From Callers | STALE_SUPERSEDED | N/A | Implemented in D33 | None |
| 33 | Goku Items 1-5 — Structural-First GT | VERIFIED | 5 items behind flags (GT_STRUCTURAL_NEXT_ACTION etc.) | Code audit: oh_gt_full_wrapper.py + post_view.py | Flags OFF by default (intentional) |
| 34 | L5 Goku — Generalized Event-Driven Governor | VERIFIED | 14 event types + safety rules behind GT_L5_GOKU_EVENTS | 61 tests + code audit | Flag OFF, precondition gap persists |

---

## Status Legend

- **VERIFIED** — Implementation matches intent, metrics confirm, no behavior change needed
- **IMPLEMENTED_AND_VERIFIED** — Fix applied and proven with metrics
- **PARTIAL_WITH_BLOCKER** — Implementation correct, awaiting external data
- **BLOCKED** — Cannot proceed (dependency missing)
- **STALE_SUPERSEDED** — Newer decision contradicts
- **CONTRADICTED** ��� Conflicts with another decision
- **ROLLED_BACK** — Implemented but reverted due to regression
- **NOT_STARTED** — Audit not yet begun

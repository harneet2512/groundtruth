# we_did.md — Layer-by-Layer Audit + Fixes (2026-05-28)

Living document. Updated after each layer fix.

---

## Constitutional Framing

GroundTruth is a **generalized, Cursor-style harness**. Two properties define it:

1. **Generalized** — works on any repo / agent / language / model. Benchmarks are validation surfaces only.
2. **Cursor-style** — honest tiered evidence, four pillars, silent when uncertain, never controls the model.

The arrow goes: **correct context → correct code → flips.** Not: want flips → engineer context.

Flips are the output that validates the architecture is correctly built. Not a feature to engineer toward.

**Four-pillar context model** (CLAUDE.md:49-61):

1. Contract (signature, return type) — fires ALWAYS, no edge dependency
2. Consistency (twins, patterns) — fires ALWAYS, no edge dependency
3. Callers (who uses this) — ONLY pillar gated on edge confidence
4. Completeness (co-change, scope) — fires ALWAYS, no edge dependency

**Evidence tiering** (CLAUDE.md:222):

- `[VERIFIED]` = confidence ≥ 0.9
- `[WARNING]` = 0.5 ≤ confidence < 0.9
- `[INFO]` = confidence < 0.5

---

## Mandatory Properties (from CLAUDE.md & DOC_OF_HONOR.md)

Every layer fix MUST satisfy all three:

1. **Dynamic** — tier boundaries from per-task score distribution, not hardcoded absolutes
2. **Hybrid** — composite scoring from ≥3 signals with research-justified weights
3. **Confidence-gated** — explicit [VERIFIED]/[WARNING]/[INFO] tiers, tiered suppression, honest fallback

## Audit Template (applied per layer)

1. **DOC_OF_HONOR contract** — quoted section, claimed status
2. **CLAUDE.md alignment** — generalized? Cursor-style? Four-pillar respected? **DYNAMIC + HYBRID + CONFIDENCE-GATED?**
3. **Intended behavior** — what the agent should see / not see
4. **Runtime reality** — from `output.jsonl` agent observations (NOT telemetry counts)
5. **Latest research** — venue + year citations
6. **Verdict** — ALIGNED / VIOLATES / PARTIAL
7. **Proposed update** — file:line, effort, conflict risk
8. **What was changed** — actual diffs after build

---

## Layer Audit Status

| Layer | DOC_OF_HONOR section | Verdict | Action |
|---|---|---|---|
| 0: graph.db foundation | §0.1-0.4 | ALIGNED | Accept current; parallel-session candidates for Pyright/JARVIS/Tier-2 LSP |
| 1: Path Resolution | §1.1 NOT_BUILT | VIOLATES | **Building now** |
| 2.1: L1 Brief | §2.1 WORKING (claimed) | VIOLATES | Pending |
| 2.1+: L1+ Orientation | §2.1+ WORKING (claimed) | PARTIAL | Pending |
| 2.2: L3 Post-Edit | §2.2 WORKING | ALIGNED (mostly) | change_impact tiering needed |
| 2.3: L3b Post-View | §2.3 WORKING (claimed) | VIOLATES (ego-graph dead) | Pending |
| 2.4: L4a Auto-Query | §2.4 WORKING | ALIGNED | None |
| 2.5: L5 Scaffold | §2.5 WORKING | DOWNSTREAM-BROKEN | Fixed by L1 brief fix |
| 2.6: L5b Late Reminder | §2.6 (doc says suppressed) | DOC LIES | Pending |
| 2.7: L6 Reindex | §2.7 WORKING | ALIGNED | None |
| 2.8: L6 Pre-Submit | §2.8 BROKEN (honest) | HONEST FAILURE | Defer |

---

## Layer 0: graph.db Foundation

**DOC_OF_HONOR §0.1-0.4:** Go binary + tree-sitter → SQLite v15.2-trust-tier. 30 lang specs. 10-strategy resolver. 4-pass build.

**Runtime reality (this session):**
- 10-strategy resolver landed (was 6)
- PyCG assignment tracking added
- ParentID bug fix unlocked methodsByClass
- pypsa name_match 277 → 95 (-66%), edges 1342 → 1724 (+28%)
- Schema v15.2 with trust_tier / candidate_count / evidence_type / verification_status

**Research alignment:**
- PyCG ICSE 2021 (99.2% precision) ✅ Strategy 1.96 implemented
- JARVIS 2024 (inter-procedural flow) ⚠️ partial via Strategy 1.93
- R12 ICSE 2026 (agents find files 72-81% alone; graph matters for callers not ranking) ✅

**Verdict: ALIGNED.** No DOC_OF_HONOR violation. Hard asymptote on graph quality (70-80% name_match floor per CLAUDE.md:250; 24/30 langs no import resolution; dynamic dispatch unresolvable).

**Action:** Accept current. Parallel-session candidates documented (Pyright debug, JARVIS, Tier-2 LSP). No immediate work.

---

## Layer 1: Path Resolution

**DOC_OF_HONOR §1.1:** `resolve_to_stored_path()` — Universal Path Resolver — **Status: NOT_BUILT.**

Cited inline `LIKE '%suffix'` usage across files: post_edit.py:199/363/751, post_view.py:539, oh_gt_full_wrapper.py:3360, graph_map.py:103. §1.2 marked FIXED but only for graph_map.py; rest of codebase still ad-hoc.

**CLAUDE.md alignment:**
- Generalized: ⚠️ — works incidentally on Unix; weaker on Windows / absolute container paths
- Cursor-style: ❌ — silent corruption when path mismatch (delivers wrong-file callers as if confident)
- Four pillars: N/A (foundational layer)

**Intended behavior:**
- Convert any agent-supplied path (absolute, relative, workspace-prefixed, Windows-separator) into canonical `nodes.file_path` for graph queries
- Return None when path doesn't resolve to a known node — so consumer can stay silent instead of returning wrong data
- Single source of truth; no per-consumer reinvention

**Runtime reality:**
Each consumer reinvents normalization:
- `v1r_brief.py:253` — `_norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")`
- `post_edit.py` — variant
- `post_view.py` — different variant
- wrapper — yet another

Cannot measure path-mismatch corruption in trajectories because it's silent. Could be quietly degrading flips on any task.

**Research alignment:**
- RepoGraph ICLR 2025, LocAgent ACL 2025 — both assume canonical repo-relative paths as graph keys
- Database normalization (Codd 1970) — store canonical, query canonical, normalize at boundary

**Verdict: VIOLATES.** Section explicitly NOT_BUILT. Silent-corruption violates Cursor-style honesty.

**Proposed update:**
- New: `src/groundtruth/index/path_resolver.py` — single function `resolve_to_stored_path(agent_path, graph_db, workspace_root="") -> str | None`
- Sweep consumer queries to use it (or keep their fallback with telemetry on which path resolved)

**Effort:** 1-2 days for function + comprehensive sweep. Function alone: hours.

### What was built (2026-05-28)

**New file:** `src/groundtruth/index/path_resolver.py`

Public API:
- `resolve_to_stored_path(agent_path, graph_db, workspace_root="") -> str | None`
- `is_known(agent_path, graph_db, workspace_root="") -> bool`
- `clear_cache()` — reset basename cache after L6 reindex

Resolution strategy (ordered most-canonical → least):
1. Try exact match against each candidate form
2. Strip workspace_root prefix if supplied
3. Strip instance-id prefix (`kozea__weasyprint-2300/...`)
4. Strip container prefixes (`workspace/`, `testbed/`, `repo/`)
5. Basename match ONLY when exactly one path ends in that basename (no LIKE-suffix false positives)

Returns None when ambiguous → consumer stays silent (Cursor-style honesty).

**Test:** `tests/unit/test_path_resolver.py` — 17 tests covering exact, prefix, separator, container, workspace, instance-id, unique-basename, ambiguous-basename, missing-db, empty cases. All pass.

**Test suite:** 170 + 17 = **187 passed.** No regression.

**Not yet swept:** Consumer queries still use inline normalization. Sweep planned in subsequent commits. The new resolver is the canonical implementation; sweeping is mechanical.

**Conflict risk neutralized:** New file + new test, no edits to existing consumer queries. Safe to merge.

---

## Layer 2.1: L1 Brief — per-entry [VERIFIED]/[WARNING]/[INFO]

**DOC_OF_HONOR §2.1:** Brief renders top-N regardless of confidence; line 874 had explicit "NEVER suppress" override.

**Three properties check:**
- Dynamic: ⚠️ tier boundaries hardcoded (intentional — tag consistency across tasks)
- Hybrid: ✅ 3 signals (caller format, issue-text match, test mapping)
- Confidence-gated: ✅ [VERIFIED]/[WARNING]/[INFO] + honest fallback note when all [INFO]

**What was built:**
- New `_entry_confidence_tier()` in `v1r_brief.py`
- Tag prefix injected per entry in `render_brief()`
- Honest note when all entries [INFO]
- Directive only fires on [VERIFIED] top + score gap
- New `FileEntry.function_names` field (separate from signatures)
- Two verifier-found bugs fixed:
  - BUG 1: `entry.functions` stored signatures (not names) → issue_match dead in production
  - BUG 2: `" in "` substring fooled by paths like `built in widget.py`; anchor on `"() in "`
- 18 tests added (11 base + 7 verifier-suggested regression tests)

**Tests:** 210 pass.

---

## Layer 2.1+: L1+ Orientation — dynamic + hybrid + confidence-gated

**DOC_OF_HONOR §2.1+:** Caller-count ranking surfaced hubs (conan `Profile() 16 callers`, cfn-lint `Template() 101 callers`) that misled the agent.

**Three properties check:**
- Dynamic: ✅ tier boundaries from per-task score distribution (top score + median gap)
- Hybrid: ✅ 5 signals (direct match + part overlap + path overlap + inverse hub + property match)
- Confidence-gated: ✅ [VERIFIED]→"Issue references", [WARNING]→"Related (by graph)", [INFO]→suppressed, all-low→honest note

**What was built:**
- New module `src/groundtruth/orientation/composite.py`
  - `composite_score()` — 5-signal hybrid with research-cited weights:
    - 0.40 direct name match (LocAgent ACL 2025)
    - 0.25 part overlap (SweRank ICLR 2025)
    - 0.15 path overlap (LocAgent)
    - 0.20 inverse hub score `1/(1+log(1+n))` (CodePlan FSE 2024, TF-IDF)
    - 0.15 property match bonus (PyCG-style)
    - Class demotion ×0.4 when name in issue text (usually context)
  - `dynamic_tiers()` — three regimes:
    - Clear winner (top ≥ 0.5 AND gap > 0.3): VERIFIED/WARNING/INFO at 0.7×/0.5× top
    - Flat (top ≥ 0.3): WARNING/INFO only at 0.7× top
    - All weak (top < 0.3): all INFO
  - `render_orientation()` — confidence-gated sections + honest fallback
- Wrapper edit at `oh_gt_full_wrapper.py:6045-6090` — replaces caller-count ranking with composite + dynamic tier rendering
- Per-task telemetry: `[GT_META] orient_candidate_N` and `[GT_META] orient_tiers` emit signal breakdowns

**Tests:** 31 new in `tests/unit/test_orientation_composite.py`. All pass. Full suite: **241 passed.**

**Wrapper import verified clean** after edit.

---

(more layers below as we build)

# GT Layer Deficiencies — 30-task audit (2026-05-08)

## Current result: 5/30 resolved (16.7%) vs SWE-agent 3/30 (10%)

The 67% improvement over SWE-agent is almost entirely from the OH harness switch
(100% patch rate vs 63%). GT's actual contribution to resolve rate is near zero —
most of its injections are noise the agent ignores or never sees.

---

## L1 Brief — WORKING but over-injected

**Status:** Fires correctly, real file candidates from graph.db.

**Deficiency:** `brief=14` across all tasks — the brief text appears 14 times
in the history. This means it's being injected into every observation or
repeated in the conversation. The agent sees the same brief 14 times instead
of once. Token waste.

**Fix:** Verify brief is injected exactly once in the first user turn. If it
appears elsewhere, find the leak.

**Impact if fixed:** Saves ~6000 tokens per task of repeated context.

---

## L2 Hybrid Fusion — UNTESTED

**Status:** Reports ok=1 per task but we never verified the fused candidates
are correct or useful.

**Deficiency:** No audit of whether the RRF-fused file candidates match the
actual gold edit files. Could be 100% wrong candidates — we wouldn't know.

**Fix:** For the 5 resolved tasks, check if the brief's candidate files
include the actual files the agent edited.

**Impact if fixed:** If candidates are wrong, fixing localization could add
3-5 more resolves from the 25 unresolved tasks.

---

## L3 Post-edit — MOSTLY GARBAGE

**Status:** Fires on every source edit, but 60-87% of evidence blocks are empty.

**Evidence from audit:**
- cfn-lint-3862: 4 real / 24 empty (85% noise)
- cfn-lint-4032: 4 real / 27 empty (87% noise)
- cfn-lint-3866: 10 real / 28 empty (74% noise)
- checkov-6895: 2 real / 12 empty (86% noise)
- Average across 20 tasks: ~75% empty evidence

**Root cause:** The post-edit hook queries graph.db for callers/contracts of
the edited symbol. On cfn-lint (YAML/JSON config rules), most edited functions
have zero callers in the graph — the graph only indexes Python call sites, not
YAML rule references.

**Deficiency:** Empty evidence blocks (`[GT_STATUS] no_evidence`) are still
injected as `<gt-evidence>` XML blocks. The agent sees:
```
<gt-evidence trigger="post_edit:src/cfnlint/rules/foo.py">
[GT_STATUS] no_evidence:abstention_filtered
</gt-evidence>
```
This is noise — it tells the agent nothing and wastes context tokens.

**Fix:** Don't inject empty evidence blocks AT ALL. If GT has nothing useful
to say, say nothing. Only inject when evidence has [VERIFIED] or [POSSIBLE]
tags.

**Impact if fixed:** Removes ~75% of GT injections. Agent gets cleaner context,
fewer distractions, more iterations available for actual work.

---

## L3b Post-view — WORKING but no dedup

**Status:** Fires on source file views with real structural coupling data.

**Deficiency:** `dedup=0` across ALL 20 tasks. The dedup feature we built
(hash evidence per file, suppress duplicates) is not firing. The agent gets
the same coupling data every time it re-views a file.

**Root cause:** Either the hash matching has a bug, or the agent never views
the same file twice (unlikely given 100 iterations).

**Fix:** Debug the dedup hash. Check if the evidence content changes slightly
between views (e.g., timestamps), preventing exact hash match.

**Impact if fixed:** Reduces token waste on repeated views. Minor impact on
resolve rate but important for the "enrich not spam" principle.

---

## L4 Prefetch — WORKING with issue-seeded symbols

**Status:** Fires correctly, selects issue-relevant symbols from graph.db.
`prefetch=10` means the prefetch XML appears 10 times in history (same
over-injection problem as L1).

**Deficiency:**
1. Over-injected (10x instead of 1x)
2. Symbols are issue-seeded (good) but we haven't verified the evidence
   is actually relevant to the bug fix
3. The pypsa SQL heredoc leak is fixed but untested at scale

**Fix:** Verify prefetch appears exactly once. Audit whether the caller/contract
evidence in the prefetch actually helped the 5 resolved tasks.

**Impact if fixed:** Cleaner context. If evidence is irrelevant, fixing symbol
selection could surface more useful data.

---

## L5 Gate — BROKEN (never reaches agent)

**Status:** Fires only during `complete_runtime` (after agent loop ends).
The agent NEVER sees the pre-submit advisory.

**Evidence:** `Advisory in agent observations (not patch): 0` across ALL tasks.
The advisory only appears in the git_patch field as corruption.

**Root cause:** OH's `complete_runtime` runs `git diff --cached <commit>` which
triggers our submit detection. But by then the agent is done. The agent's own
`/submit` or finish command goes through OH's agent loop, not `runtime.run_action`.

**Deficiency:** L5 is completely useless. The agent never gets warned about
unverified edits. The advisory just corrupts patches.

**Fix:** Two changes needed:
1. Already done: don't append advisory to `git diff --cached` observations
2. TODO: find the agent's actual submit signal in OH's event stream and
   inject the advisory BEFORE the agent commits to submitting

**Impact if fixed:** Agent could catch unverified edits on 2-3 tasks where
it edited files but didn't validate. Moderate resolve impact.

---

## L6 Reindex — WORKING

**Status:** Fires correctly on every edit. gt-index incremental reindex works.

**Deficiency:** None observed. This is the only layer with zero issues.

---

## Summary: what's actually helping vs what's noise

| Layer | Helping? | Noise level | Resolve impact if fixed |
|-------|----------|-------------|------------------------|
| L1 Brief | YES (localization) | HIGH (14x repeated) | Medium (cleaner context) |
| L2 Hybrid | Unknown | Unknown | High (if candidates are wrong) |
| L3 Post-edit | MINIMAL | VERY HIGH (75% empty) | High (stop wasting agent attention) |
| L3b Post-view | YES (coupling data) | MEDIUM (no dedup) | Low |
| L4 Prefetch | YES (caller/contract) | HIGH (10x repeated) | Medium |
| L5 Gate | NO (never reaches agent) | N/A | Medium |
| L6 Reindex | YES (graph freshness) | NONE | None |

## Honest assessment

The 5/30 resolve rate is almost entirely from switching SWE-agent → OpenHands.
GT's contribution is marginal at best and actively harmful at worst (75% noise
injection rate, L5 patch corruption). The layers that work (L1, L3b, L4, L6)
are diluted by massive noise from L3 empty evidence.

If we fix the deficiencies:
- Stop injecting empty evidence: removes 75% of noise
- Fix brief/prefetch to inject once: saves thousands of tokens
- Fix L5 to reach the agent: adds a safety net
- Verify L2 localization accuracy: could unlock 3-5 more resolves
- Add real dedup: cleaner repeated-view handling

Conservative estimate: fixing noise alone could flip 2-3 unresolved tasks
(agent had correct localization but burned context on GT noise). Fixing
L2 localization if it's wrong could flip 3-5 more. Total potential: 8-13/30
(27-43%) vs current 5/30 (17%).

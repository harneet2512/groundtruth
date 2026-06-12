# GT Bugfree Status & Build Plan

Date: 2026-06-11  
Branch: `gt-trial`  
HEAD: `834029f8` (closeout) + `7a283c60`..`5e713278` (this session's LSP/oracle fixes)

---

## PART 0: THE CORE BUILD — Predictive Action-Aware Context Delivery

**This is what GT is building toward. Everything else serves this.**

GT has:
- The **full code map** (graph.db — every function, every call edge, every file relationship)
- **Real-time visibility** into every agent action (files opened, edits made, tests run, commands executed — all flow through `_augment_output`)
- The **oracle metrics** that describe the agent's state RIGHT NOW (action count, edited files set, tested tokens, nonedit streak, phase)

GT's job: **When the agent performs an action, deliver the context THAT action will need — so the agent never has to go find it itself.**

```
Agent opens file X
  → GT already knows: who calls X, what X calls, what contract X must satisfy,
    what sibling patterns X must match, which obligations touch X
  → GT delivers EXACTLY that context, AT that moment, in the smallest useful form
  → Agent writes correct code without searching for callers/contracts/patterns

Agent edits function Y
  → GT already knows: 5 callers depend on Y's return type, 2 siblings have the same pattern
  → GT delivers: "Y must return Optional[User] (5 callers depend on it). Match sibling Z's pattern."
  → Agent doesn't break callers, matches patterns, gets the fix right

Agent runs tests
  → GT already knows: which obligations are still unmet, which edited symbols lack coverage
  → GT delivers: "You edited A and B but only tested A. Obligation 3 ('handle invalid n') is unaddressed."
  → Agent tests B before submitting
```

**The oracle metrics are the decision signal:**
- `_oracle_edited_rels` = what the agent has touched → determines which contracts/callers are relevant
- `_oracle_tested_tokens` = what's been verified → determines which obligations are still at risk  
- `_action_count / _GT_STEP_LIMIT` = budget position → determines urgency (advisory vs urgent vs gate)
- `_oracle_nonedit_streak` = is the agent searching or editing → determines speak vs silent
- `len(_oracle_focus_cache)` = issue-relevant files identified → determines scope of delivery

**The state-aware dedup we just built (`5e713278`) is the first piece of this:** the oracle hash includes `len(edited_rels):len(tested_tokens):action_count//30` — so the same obligation delivered after 3 new edits is a NEW delivery because the agent is in a different state now.

**What makes this NOT benchmaxxing:**
- It uses the GENERAL code map (any repo, any language)
- It observes GENERAL agent behavior (actions, not task-specific patterns)
- It delivers GENERAL code facts (callers, contracts, patterns — not gold files)
- It works for ANY agent on ANY MCP client
- It's deterministic, LLM-free, $0

**What's built vs what's needed:**

| Layer | Built | Needed |
|---|---|---|
| Code map | graph.db + LSP + FTS5 | ✓ Complete |
| Action observation | `_augment_output` sees every action | ✓ Complete |
| Oracle metrics | edit/test/action/streak tracking | ✓ Complete |
| State-aware dedup | Behavioral hash in gate | ✓ Complete (this session) |
| Phase detection | Partial (streak-based) | Needs: explicit ORIENT/SEARCH/VIEW/EDIT/VERIFY/SUBMIT |
| Predictive selection | Not built | Needs: "agent opened X → deliver X's callers/contracts" |
| Action-to-context mapping | Not built | Needs: templates that translate graph facts → imperative action |
| Obligation lifecycle | Partial (per-turn recomputation) | Needs: persistent tracker with status transitions |
| Budget-aware urgency | Partial (>80% clear) | Needs: full escalation (dormant→advisory→urgent→gate) |
| Pre-submit enforcement | Not built (attempted, failed) | Needs: severity boost at >90% budget |

The 10 architectural pieces below are the decomposition of this core concept into buildable checkpoints.

---

## PART 1: CLOSURE STATUS — What the handoff doc asked vs what's done

### Bugs B1-B11

| Bug | Description | CP | Status | Evidence |
|---|---|---|---|---|
| B1 | Deep metrics false zero for GT injection | 001 | **CLOSED** | `060eccc9`, test `test_gt_deep_metrics_trajectory_fallback.py` |
| B2 | Embedder cert vs metrics contradiction | 003 | **CLOSED** | `e013c7be`, cert-first deep metrics source |
| B3 | Exact test leak in DeepSWE runtime | 002 | **CLOSED** | `956c32e1`, sanitized verify/nudge surfaces, test coverage |
| B4 | LSP warm over-credit (Go/Rust 0 conversions) | 005 | **CLOSED** | `7a554ec8` (product readiness gate) + `92db200c` (WARN soft-pass) + `7a283c60` (dep mount). Three layers: gate truth, no-abort, env fix |
| B5 | Graph cert vs outcome truth contradiction | 006 | **PARTIAL** | `9a7ce8b4` task_truth.json reconciler built. Open: cross-run validation on held-out tasks |
| B6 | No consumption/agreement metric | 007 | **CLOSED** | `6c0b1af6` gt_consumption_ledger + used/enforced columns |
| B7 | Low-value/static surface leakage | 009 | **CLOSED** | `f5dee492` centralized path_policy filter |
| B8 | Patch capture/hygiene | 010 | **CLOSED** | `eb5cfe5f` patch hygiene classification |
| B9 | Outcome schema confusion | 006 | **PARTIAL** | same reconciler as B5 |
| B10 | Infra/capture classification | 008 | **CLOSED** | `eae6667a` infra subtypes (ENOSPC, zero-byte, missing) |
| B11 | Runtime evidence delivery | 004 | **CLOSED** | `d7da2bc2` oracle event-bound waiver, 23/23 test_verified_adapter |

### This Session's Additional Fixes (not in CP chain)

| Commit | What | Status |
|---|---|---|
| `9bd3bf82` | Fail-closed gates (LSP verdict, obligation render, confidence floor, source path) | Done, LIPI'd |
| `430f6b8f` | Self-verify env classifier (full _ENV_FAIL_RE port) | Done, verified 4/4 patterns |
| `197301a5` | Dockerfile CDN curl (HF Hub 429 fix) | Done, builds succeed |
| `5688bdc3` | LIPI fixes (obligation dose, C1 MAD parity, LSP aggregation) | Done, Fable LIPI found obligation fix inert |
| `92db200c` | LSP WARN soft-pass (zero-conversion not hard abort) | Done, Go/Rust tasks pass proof step |
| `5e713278` | State-aware oracle dedup + warm-but-unattempted soft pass | Done, LIPI'd, 270/270 tests pass |
| `7a283c60` | Mount task image dep stores (Go module cache + Rust cargo) | Done, untested on live run |

---

## PART 2: THE 10 MISSING ARCHITECTURAL PIECES — What needs to be built

These are from the handoff doc's "Architecture GT Is Lacking" section. Scored by current state.

### PIECE 1: Trajectory-State Controller
**Status: NOT BUILT**

**What it is:** A controller that observes the agent's trajectory phase and decides what GT should do NOW.

**What exists today:**
- `gt_oracle_sense.py` computes `loop_ratio`, `new_state_rate`, `edit_churn`, phase flags
- `_oracle_gate_blocks` in `gt_mini_patch.py` selects ONE candidate per turn
- The oracle has `_oracle_nonedit_streak`, `_oracle_edited_rels`, `_oracle_tested_tokens`

**What's missing:**
- No unified phase detector that says "ORIENT / SEARCH / EDIT / VERIFY / SUBMIT"
- No policy that maps phase → allowed GT interventions
- Candidates are produced by all producers simultaneously; the gate picks by severity, not by phase relevance
- No "should GT speak or stay silent this turn?" decision separate from "which candidate wins?"

**What to build:**
```
_detect_phase(trajectory_state) → Phase enum {ORIENT, SEARCH, VIEW, EDIT, VERIFY, SUBMIT}

_phase_policy(phase) → {
  allowed_kinds: set[str],    # which producer kinds can fire
  max_tokens: int,            # context budget for this phase
  urgency_floor: float,       # minimum severity to speak at all
  silence_preference: float,  # 0=always speak, 1=prefer silence
}
```

**Where:** `artifact_deepswe/gt_mini_patch.py` — wraps `_oracle_gate_blocks`, pre-filters candidates by phase policy before the severity/confidence ranking.

**Research:** Wink (coding-agent misbehavior recovery, 2024); SWE-agent ACI (agent-computer interfaces); position/context bias (Liu et al. TACL 2024 — context at decision point, not beginning).

---

### PIECE 2: Context Selection Policy
**Status: PARTIALLY BUILT (fragmented)**

**What it is:** Per-phase rules for WHAT context to surface.

**What exists today:**
- Brief at turn 1 (ORIENT)
- `post_view` witnesses on file read (VIEW)
- `post_edit` contracts on file write (EDIT)
- Obligation nudge at review-transition (VERIFY)
- `_final_obligation_block` was dead code (removed); no SUBMIT gate

**What's missing:**
- VIEW delivers ALL witnesses regardless of the viewed file's relevance to the issue
- EDIT delivers contracts for ALL symbols in the file, not just the edited ones
- No SUBMIT gate (the one we tried was architecturally unreachable — §15.2 F3)
- ORIENT brief is one-shot; no "you've been searching 30 turns, here's a redirect"

**What to build:**
```
Per phase, a selector that narrows the candidate set:
  ORIENT: top 3 files + first useful command (not full graph dump)
  VIEW:   only callers/contracts for symbols THE AGENT WILL LIKELY EDIT (issue-anchored)
  EDIT:   only contracts for the EDITED symbol + its direct callees
  VERIFY: obligation checklist (no test names, no commands — category-level)
  SUBMIT: unresolved obligations only (the "you haven't tested X" gate)
```

**Where:** Each selector is a filter applied inside the respective producer (`_evidence()`, `_obligation_nudge_block()`, brief generator). Not a new module — narrowing the existing producers.

---

### PIECE 3: Consumption Feedback Loop
**Status: CLOSED (CP007)**

`gt_consumption_ledger` records delivery→next-action pairs. Deep metrics has `used`/`enforced` columns.

**Remaining gap:** The ledger records but doesn't yet DRIVE suppression. A future iteration should: if block X was delivered 2 turns ago and the agent ignored it, don't re-deliver the same block (waste). If block Y was consumed (agent acted on it), boost that producer's priority.

---

### PIECE 4: First-Class Obligation Model
**Status: NOT BUILT (partial primitives exist)**

**What exists today:**
- `spec.py` extracts obligations from issue text (imperatives, code-fences, named symbols)
- `gt_oracle.py` has `load_obligations()`, `obligation_statuses()`, `order_unmet()`
- Status is computed from token overlap (`_oracle_edited_tokens ∩ obligation.symbols`)

**What's missing:**
- No lifecycle: `unedited → edited → tested → satisfied → contradicted`
- "edited" = token overlap, not semantic implementation check
- "tested" = test output tokens overlap, not "test covers this obligation"
- No contradiction detection (agent wrote code that VIOLATES the obligation)
- The obligation object doesn't persist across turns — recomputed fresh each time

**What to build:**
```python
@dataclass
class Obligation:
    id: int
    verbatim: str           # issue text clause
    symbols: set[str]       # extracted symbols
    status: Literal["unedited", "edited", "tested", "satisfied", "contradicted"]
    evidence: list[str]     # what proves the status
    last_updated_turn: int  # when status last changed

class ObligationTracker:
    def update(self, edited_files: set, test_output: str, turn: int) -> list[StatusChange]:
        """Called every turn. Returns status transitions for logging."""
    
    def unmet_at_submit(self) -> list[Obligation]:
        """The pre-submit gate query."""
```

**Where:** `artifact_deepswe/gt_oracle.py` (already has primitives) → promote to a persistent per-run tracker, not a per-turn recomputation.

---

### PIECE 5: Pre-Submit Gate
**Status: NOT BUILT (attempted and failed)**

**What exists:** `_final_obligation_block` was dead code (submit action never reaches `_augment_output` per §15.2 F3). Removed in `5688bdc3`.

**Why it failed:** In pier/mini-swe-agent, the submit command (`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat /tmp/patch.txt`) is the LAST action. Its output is captured but GT's `_augment_output` hook fires on the observation — by then the agent cannot act on injected context.

**What to build:** The gate must fire BEFORE submit, not after. Two options:

1. **Budget-triggered final check (feasible now):** At >90% budget, if obligations are unmet, inject a final "you have unverified requirements — test before submitting" message regardless of other candidate competition. The state-aware dedup already enables this (different hash at high budget).

2. **Patch-intercept gate (requires pier change):** Intercept the `git diff > /tmp/patch.txt` step in `gt_agent.py`'s self-verify loop. Before allowing submit, check obligations against the patch content. This is the `_run_with_test_retry` path — already has the retry loop.

**Recommended:** Option 1 is a configuration of the existing obligation producer (severity boost at >90% budget so it always wins the gate). No new code path. Option 2 is the long-term correct answer but requires pier adapter changes.

**Where:** `artifact_deepswe/gt_mini_patch.py` (option 1) or `artifact_deepswe/gt_agent.py` (option 2).

---

### PIECE 6: Verifier-Fail Retry Plumbing
**Status: NOT BUILT**

**What exists:**
- `GT_SELF_VERIFY_ATTEMPTS=2` runs the agent's OWN test command post-exit
- If test fails, agent gets another attempt
- `_ENV_UNVERIFIABLE_RE` classifies env failures as non-retryable

**What's missing:**
- Self-verify runs the agent's test command, NOT the official verifier
- When the official verifier fails (reward=0), there's no classify→map→repair→retry loop
- The gap between "agent's tests pass" and "official verifier passes" is the adaptix shape: 15/16 tests pass agent-side, but 1 hidden test fails on the official verifier
- No way to inject "the verifier found X wrong" feedback

**What to build:**
```
Official verifier fail (reward=0)
  → classify failure type (test output from verifier/test-stdout.txt):
      - assertion_error (logic bug in patch)
      - regression (broke existing tests)
      - missing_feature (new tests fail — incomplete implementation)
      - env_failure (Docker/infra, not retryable)
  → map failure to obligation/edited symbol
  → inject repair context:
      "Your patch passes 15/16 tests but fails on: [category-level description].
       The failure is in [file:function area]. Check [obligation X]."
  → bounded retry (1 additional attempt, capped)
```

**Where:** `artifact_deepswe/gt_agent.py` — the `_run_with_test_retry` loop already exists. Extend it to also parse the official verifier output (if available) and inject targeted feedback. Requires pier to expose verifier results before final submission.

**Blocker:** Pier's current architecture runs the verifier AFTER the trial is complete. The agent cannot act on verifier results. This requires a pier-level change (verifier-in-loop) or a pre-verifier approximation (run the hidden test.sh inside the agent container if accessible — but it's NOT accessible by design).

**Practical workaround:** Use the agent's own test results more aggressively. If the agent's test output shows failures it didn't address, retry before submitting. This is what self-verify already does but with better failure classification.

---

### PIECE 7: Trust-Gated Context Surfaces
**Status: MOSTLY CLOSED**

| Trust issue | Status |
|---|---|
| Vendor/static file in brief | **CLOSED** — CP009 path_policy |
| Exact test names in verify | **CLOSED** — CP002 sanitized |
| Graph cert vs gate contradiction | **CLOSED** — CP006 task_truth reconciler |
| Embedder cert vs metrics | **CLOSED** — CP003 cert-first source |
| LSP warm ≠ useful resolution | **CLOSED** — CP005 product readiness split |

**Remaining:** The trust policy is applied per-surface but not centralized. `path_policy.py` (CP009) is the start. A future unification would make every surface call the same trust function.

---

### PIECE 8: Context Budgeting
**Status: NOT BUILT**

**What exists:**
- Oracle gate limits to ≤1 emission per turn
- `max_listed` on obligation render (now None — shows all)
- Observation template truncates output >10KB with head/tail

**What's missing:**
- No token budget per GT injection (some GT blocks are 2KB, some are 200B — no control)
- No "prefer next-action over background explanation" prioritization
- No dedup of repeated facts across turns (same caller info delivered 5 times)
- Low-confidence facts still render (C1 confidence floor is relative MAD, not absolute)

**What to build:**
```python
class ContextBudget:
    max_tokens_per_injection: int = 500  # target, not hard limit
    
    def trim(self, block: str) -> str:
        """Trim to budget: keep imperative lines, drop explanatory lines."""
    
    def dedupe_cross_turn(self, block: str, history: list[str]) -> str:
        """Remove facts already delivered in previous turns."""
    
    def rank_by_actionability(self, lines: list[str]) -> list[str]:
        """Sort: imperative > fact > explanation. Truncate from bottom."""
```

**Where:** Applied inside each producer before returning the payload to `_oracle_gate_blocks`.

---

### PIECE 9: Graph-To-Action Translation
**Status: NOT BUILT**

**What exists:** GT renders facts like:
```
[CALLERS] X.process() calls Y.validate() at line 42
[CALLEE] Y.validate(input: str) -> bool
```

**What's needed:** GT should translate to action:
```
Because X.process() calls Y.validate(), changing validate()'s return type
will break process(). Check X.process() handles the new return before submitting.
```

**What to build:** A thin translation layer between the evidence renderer and the final output that:
1. Identifies the agent's likely next action (from trajectory state)
2. Frames the evidence as a risk/instruction relative to that action
3. Keeps it to 1-2 sentences (context budgeting)

**Where:** Post-processing step in `_evidence()` (post_view) and contract renderer. NOT an LLM call — template-based translation from graph relationship type → imperative sentence.

**Templates:**
```python
TEMPLATES = {
    "caller_risk": "Changing {callee} risks breaking {caller} ({file}:{line}). Inspect before editing.",
    "contract_break": "{symbol} must return {type} — {N} callers depend on it.",
    "sibling_pattern": "Sibling {sibling} follows pattern {pattern}. Match it.",
    "untested_edit": "You edited {symbol} but no test covers it. Run {test_hint}.",
}
```

---

### PIECE 10: Flip/Trajectory Scorecard
**Status: PARTIALLY BUILT (CP007 consumption columns)**

**What exists:**
- CP007 consumption ledger (delivered/used/enforced)
- Deep metrics (action count, first edit, cost, tokens)
- Oracle telemetry (per-turn events)

**What's missing:**
- `steps_saved_to_gold` — not computed (requires gold file knowledge, only post-hoc)
- `wasted_file_views_reduced` — not computed
- `first_correct_edit_earlier` — not computed
- `obligation_coverage_at_submit` — obligation tracker not persistent (Piece 4 dependency)
- `gt_caused_flip` — requires paired comparison methodology (Stage 2)

**What to build:** Most of these are POST-HOC analysis metrics, not runtime components:
```python
def compute_trajectory_scorecard(gt_on_trajectory, baseline_trajectory, gold_files):
    return {
        "steps_saved_to_gold": baseline_first_gold_edit - gt_first_gold_edit,
        "wasted_views_reduced": baseline_non_gold_views - gt_non_gold_views,
        "first_correct_edit_earlier": bool(gt_first_gold_edit < baseline_first_gold_edit),
        "obligation_coverage_at_submit": obligations_met / obligations_total,
        "gt_consumed": consumption_ledger.used_count / consumption_ledger.delivered_count,
        "gt_changed_action": consumption_ledger.enforced_count,
        "gt_caused_flip": gt_resolved and not baseline_resolved,
    }
```

**Where:** `scripts/metrics/compute_paired_metrics.py` — post-run analysis, not runtime.

**Blocker:** Requires paired runs (GT-on vs baseline on same tasks) + gold file knowledge (from the benchmark, not GT). This is the Stage 2 paired experiment, gated on Stage 1 being clean.

---

## PART 2B: FABLE LIPI OF CP011-015 — 10 CONFIRMED DEFECTS

CP011-015 was shipped as commit `df4c37c5` (997 lines, 17 files, single Cursor commit). Fable LIPI found 4 FAIL, 3 PARTIAL, 1 PASS. After verifying against the current refactored code (which moved phase policy into `src/groundtruth/runtime/context_policy.py`), these defects are **CONFIRMED still present**:

### D1. Budget dedup marks facts "delivered" at PRODUCTION time, not after gate decision
**File:** `src/groundtruth/runtime/context_budget.py:64`
**Bug:** `self.delivered_facts.add(line.strip())` runs inside `trim()` which is called by `_budget_trim()` at `gt_mini_patch.py:1474` inside `_evidence()` — BEFORE `_oracle_gate_blocks` decides if this candidate wins. Gate losses permanently destroy evidence the agent never saw.
**Fix:** Remove the `.add()` from `trim()`. Instead, return the trimmed lines in `BudgetResult`. The caller (`_augment_output`) commits to `delivered_facts` only after `_win` confirms delivery. Add a `commit_delivered(lines)` method to `ContextBudgeter`.

### D2. No oracle state reset between retry attempts
**File:** `artifact_deepswe/gt_mini_patch.py` (global state)
**Bug:** `_action_count`, `_oracle_edited_rels`, `_oracle_tested_tokens`, `_DELIVERED_FACTS`, `_oblig_status_emitted`, `_oracle_delivered_hashes`, `_obligation_tracker`, `_ledger_*` all persist across `super().run()` retry attempts in the same process. Attempt 2 starts with budget>0.90, permanently SUBMIT phase.
**Fix:** Add `_reset_oracle_state()` function that clears all globals. Call from `gt_agent.py`'s retry loop before each `super().run()`.

### D3. Pre-submit severity boost loses to horizon gate
**File:** `gt_mini_patch.py:2406-2407`
**Bug:** `sev = float(_SEV_GATE)` = flat 6.0. Horizon's `composite_severity(_SEV_GATE, budget_b, unmet_ratio)` = 7.8-8.9. Obligation never wins.
**Fix:** Use `composite_severity(_SEV_GATE, budget_b_sev, unmet_ratio)` for the boost too. Or: when budget>0.90, drop the `nonedit_streak>=3` requirement so obligation fires on ANY turn with unmet obligations.

### D4. ObligationTracker adds zero new signal over stateless computation
**File:** `gt_oracle.py:682-710`
**Bug:** `update()` calls stateless `obligation_statuses()` with cumulative token sets — the same computation that already happens without the tracker. "satisfied" status is never assigned (dead code). Token overlap uses the whole command string, not edit content. 3-char symbol `map` matches everything.
**Fix:** (a) Monotonic ratchet: status only moves forward (unedited→edited→tested). (b) Scope tokens to written content only (heredoc body, not command flags). (c) Min symbol length 4. (d) Consume transitions (emit on status change).

### D5. Action templates semantically inverted
**File:** `gt_mini_patch.py:2505-2531` (approximate, after refactor)
**Bug:** `[WITNESS] name_mapping called by -> provider.py:241` renders as "Inspect name_mapping at provider.py:241" — inverted (caller is at provider.py, not name_mapping). `caller_risk` and `contract_must` templates are defined but never used. `[CALLERS]` line is replaced with a generic imperative pointing at nothing.
**Fix:** (a) Use `caller_risk` template for caller direction — keep the code snippet. (b) Add `calls`-direction branch. (c) Append imperatives after originals, never replace.

### D6. Retry classifier never fires — env regex eats agent-caused breakage
**File:** `gt_agent.py:871-890`
**Bug:** `_ENV_UNVERIFIABLE_RE` matches `ImportError`, `Cannot find module`, `fatal error:`, `undefined reference` — all printed by agent-broken patches. 5/9 tasks classified "unverifiable" instead of getting retry feedback. `env_failure` classifier (gt_agent.py:926-928) is unreachable dead code (all its patterns already matched by `_ENV_UNVERIFIABLE_RE`).
**Fix:** The env classifier needs a pre-edit baseline: if the verifier failed the same way BEFORE the agent edited, it's env. If it newly fails, it's the patch. Short-term: narrow `_ENV_UNVERIFIABLE_RE` to exclude patterns an agent patch can cause.

### D7. Consumption ledger judges from trigger, not response
**File:** `gt_mini_patch.py:2669-2700` (approximate)
**Bug:** `_ledger_cmd_acted()` scores the command whose output GT appended to — the agent's action BEFORE seeing GT's delivery. Consumption must be judged from the NEXT action. Also permanently mutes `spec.obligation` after 2 "ignored" fires.
**Fix:** Defer judgment one turn. Stash `(kind, relevance_keys)` on delivery, score the NEXT command against those keys.

### D8. Phase detection thresholds are invented constants
**File:** `gt_mini_patch.py:2451`
**Bug:** ORIENT=5 actions, SUBMIT=0.90 budget — not derived from frozen trajectory data. ORIENT is over before GT's first delivery on all 4 audited trajectories.
**Status:** Cosmetic — ORIENT phase is harmless (passes raw evidence). Fix by deriving from corpus.

### D9. `_detect_phase()` SEARCH state missing from context_policy.py
**File:** `src/groundtruth/runtime/context_policy.py`
**Bug:** Phase enum has ORIENT/VIEW/EDIT/VERIFY/SUBMIT but no SEARCH. `_detect_phase()` in gt_mini_patch.py returns Phase.SEARCH (from the old enum) which may not match the policy module's Phase enum. Needs reconciliation.
**Status:** Check if the import aliases correctly.

### D10. Scorecard `gt_caused` is grep-shaped heuristic labeled as causal
**File:** `compute_paired_metrics.py:1588-1590`
**Bug:** `gt_caused` = flip ∧ consumption>0 ∧ coverage≥0.5. The project's own audit law (gt_trial §4) forbids treating grep-derived causation as truth.
**Fix:** Rename to `gt_caused_heuristic`. Emit per-flip evidence rows for §4 audit.

---

## PART 3: PRIORITY ORDER FOR THE BUILD

Based on the context gap audit (trajectory evidence of what's actually missing):

| Priority | Piece | Why | Effort |
|---|---|---|---|
| **P0** | 5. Pre-Submit Gate (option 1) | The agent passes 15/16, 20/24, 43/47 tests — close misses everywhere. A "you haven't tested requirement X" nudge at >90% budget would catch these. | Small (severity boost config) |
| **P1** | 4. First-Class Obligation Model | Obligation tracking is the #1 lever per the gap audit. The agent edits but doesn't verify specific requirements. | Medium (promote existing primitives to persistent tracker) |
| **P2** | 1. Trajectory-State Controller | Katex got first witness at turn 87 (too late). Phase detection → "redirect to correct files" earlier. | Medium (phase enum + policy filter) |
| **P3** | 9. Graph-To-Action Translation | "X calls Y" → "changing Y risks breaking X" is a template, not an LLM. Directly improves consumption. | Small (templates) |
| **P4** | 8. Context Budgeting | GT blocks are variable-length, some noisy. Trim + dedupe improves signal density. | Small (trim function per producer) |
| **P5** | 2. Context Selection Policy | Narrow VIEW/EDIT to issue-relevant symbols only. | Medium (filter per producer) |
| **P6** | 6. Verifier-Fail Retry | Requires pier change or workaround. High impact but blocked. | Large (pier integration) |
| **P7** | 10. Scorecard | Post-hoc analysis, not runtime. Requires paired runs. | Medium (analysis script) |

---

## PART 4: IMMEDIATE NEXT ACTIONS

1. **Verify CP004-010 compatibility with this session's LSP/oracle fixes** — the commit chains diverged. Run `python -m pytest tests/ -q --tb=short` on the merged HEAD to confirm no conflicts.

2. **P0 Pre-Submit Gate (option 1):** In `_obligation_nudge_block()`, at `budget_b > 0.90`, set severity to `_SEV_GATE` (6, maximum) so the obligation always wins the gate in the final 10% of budget. No new code path — just a severity boost.

3. **Rebuild substrate + 10-task run** with the dep mount fix (`7a283c60`) to get Go/Rust LSP conversions.

4. **P1 Obligation Tracker:** Promote `gt_oracle.load_obligations()` + `obligation_statuses()` from per-turn recomputation to a persistent `ObligationTracker` class that updates status each turn and detects transitions.

---

## PART 5: VERIFICATION COMMANDS

```powershell
# All CP004-010 tests
python -m pytest tests/test_verified_adapter.py tests/fail_closed/test_lsp_liveness.py tests/test_task_truth.py tests/test_consumption_ledger.py tests/test_path_policy.py tests/test_patch_hygiene.py -q

# This session's tests
python -m pytest tests/fail_closed/ tests/test_delivery_stage2_obligation_status.py tests/unit/test_signal_thresholds.py tests/unit/test_runtime_repo_adapters.py -q

# Full suite
python -m pytest tests/ -x -q --tb=short
```

---

## PART 6: KEY FILES FOR THE BUILD

| Component | File | What to change |
|---|---|---|
| Phase detector | `artifact_deepswe/gt_mini_patch.py` | Add `_detect_phase()` before `_oracle_gate_blocks` |
| Phase policy | `artifact_deepswe/gt_mini_patch.py` | Filter candidates by `_phase_policy(phase).allowed_kinds` |
| Obligation tracker | `artifact_deepswe/gt_oracle.py` | Promote to persistent `ObligationTracker` class |
| Pre-submit severity | `artifact_deepswe/gt_mini_patch.py:~2380` | `if budget_b > 0.90: sev = _SEV_GATE` |
| Action templates | `artifact_deepswe/gt_mini_patch.py` | Post-process evidence with `TEMPLATES[relationship_type]` |
| Context budget | `artifact_deepswe/gt_mini_patch.py` | `_trim_to_budget(payload, max_tokens=500)` per producer |
| Scorecard | `scripts/metrics/compute_paired_metrics.py` | Add trajectory comparison functions |
| Retry classifier | `artifact_deepswe/gt_agent.py` | Parse test output, classify failure, inject feedback |

---

## PART 7: WHAT SUCCESS LOOKS LIKE

**Stage 1 (deterministic correctness):**
- All 10 tasks pass the proof step (DONE)
- GT delivers correct context on all 5 languages (DONE for Py/TS/JS; Go/Rust pending dep mount verification)
- Obligation tracker correctly classifies obligation status (NOT DONE)
- Pre-submit gate fires when requirements are unmet (NOT DONE)

**Stage 2 (flips):**
- Paired GT-on vs baseline run on 30+ tasks
- At least 1 flip where GT caused the correct fix (trajectory proves causation)
- No regressions (GT-on never resolves fewer than baseline)
- Wilcoxon p<0.05 on per-task metric deltas

**The gap:** The agent writes mostly-correct code (15/16, 43/47 tests pass). The remaining failures are specific requirement misses that a proper obligation tracker + pre-submit gate would catch. That's the path to flips.

---

## PART 8: FINAL DEFECT STATUS AND SESSION CLOSEOUT (2026-06-12)

### D1-D10 Defect Status

| ID | Defect | Status | Commit | Notes |
|---|---|---|---|---|
| D1 | Budget dedup marks "delivered" at production, not after gate | **CLOSED** | `77dc857c` | `.add()` moved to post-gate `commit_delivered()` in `_augment_output` |
| D2 | No oracle state reset between retry attempts | **CLOSED** | `77dc857c` | `_reset_oracle_state()` clears all globals; called per retry in `gt_agent.py` |
| D3 | Pre-submit boost (6.0) loses to horizon (7.8+) | **CLOSED** | `77dc857c` | Uses `composite_severity` for obligation boost; always wins at >90% budget |
| D4 | ObligationTracker = zero signal over stateless | **CLOSED** | `8d505603` | Monotonic ratchet (status only moves forward), min symbol len 4, scoped tokens |
| D5 | Action templates semantically inverted | **CLOSED** | `8d505603` + D5-fix | Producer now emits `w["target"]` (callee) for caller-direction witness lines; template correctly identifies the at-risk symbol |
| D6 | Retry classifier unreachable — env regex eats patch failures | **CLOSED** | `e7fd256e` | `_ENV_UNVERIFIABLE_RE` split into env-only patterns; agent-caused failures (`ImportError`, `Cannot find module`, etc.) now get retry feedback |
| D7 | Ledger judges trigger, not response; mutes obligation | **CLOSED** | `8d505603` + `7da50622` | Deferred one-turn judgment; decay (not permanent mute); threshold raised to 3; `2>&1` false positive fixed |
| D8 | Phase thresholds invented | **OPEN** | — | Cosmetic; ORIENT phase harmless; derive from frozen corpus when prioritized |
| D9 | SEARCH phase missing from policy module | **OPEN** | — | Cosmetic; enum reconciliation deferred; no runtime impact (SEARCH falls through to VIEW) |
| D10 | gt_caused labeled causal, is grep heuristic | **OPEN** | — | Cosmetic; rename to `gt_caused_heuristic` when scorecard is next touched |

**Summary:** 7/10 defects CLOSED (D1-D7). 3/10 OPEN (D8-D10) — all cosmetic, no runtime harm.

### Commits This Session

| SHA | Message | What |
|---|---|---|
| `77dc857c` | fix(lipi-d1d2d3): budget dedup post-gate, oracle reset, pre-submit boost | D1: budget commit scoped to post-gate. D2: `_reset_oracle_state()` for retry. D3: composite severity for obligation boost |
| `8d505603` | fix(lipi-d4d5d6d7): tracker ratchet, templates fixed, ledger deferred | D4: monotonic obligation ratchet + min symbol len 4. D5: caller_risk template wired, callee branch added, append-not-replace. D7: one-turn deferred judgment + decay |
| `7da50622` | fix(lipi-d1d2d7-followup): budget commit scoped to winner, dead reset removed, decay fixed | D1 follow-up: commit only winning candidate's lines. D2: removed dead `_oracle_obligation_fired` reset. D7: decay halves count on re-delivery |
| `e7fd256e` | fix(d6): split env regex — agent-caused failures now get retry feedback | D6: `_ENV_UNVERIFIABLE_RE` narrowed to env-only patterns; agent-caused `ImportError`/`Cannot find module`/`fatal error:`/`undefined reference` no longer swallowed |
| *(D5 fix)* | fix(d5): caller-direction witness emits callee name, not caller name | D5 completion: producer uses `w["target"]` for caller direction so `[WITNESS] X called by -> Y` correctly has the callee as X |

### Push Status

Local `gt-trial` is ahead of `hbali` remote. Push is blocked by authentication — `harneet2512` is denied on `hbali-stack`. Resolution: either `gh auth login` as `hbali-stack` or add `harneet2512` as a collaborator on the `hbali-stack` remote.

### Benchmark Readiness Checklist

- [ ] Push to hbali-stack (auth fix needed)
- [ ] Substrate rebuild from new HEAD (includes all D1-D7 fixes)
- [ ] 10-task validation run
- [ ] gt_trial section 4 audit on trajectories
- [ ] If clean: full 113-task benchmark

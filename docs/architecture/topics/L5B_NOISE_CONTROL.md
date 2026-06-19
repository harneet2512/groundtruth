# Topic Dossier: L5b Ignored Witness Noise Control

**Source:** DOC_OF_HONOR §2.6 (L5b Late Reminder)
**Risk level:** HIGH — 9x noise on weasyprint + cfn-lint, agent ignored 100%

---

## 1. DOC_OF_HONOR Intent

- Fire when agent ignores GT-suggested next_action for 3 consecutive actions
- Gate: goku_active (default "1" = suppressed, "0" = active)
- Safety: L5bSafetyChecker.validate() blocks if unsafe
- Status: WORKING (code fires but suppressed by default)

## 2. Current Branch Implementation

- `oh_gt_full_wrapper.py:1763-1878`
- pending_next_actions registered by L3b (every post-view), L3 (every post-edit), goku
- No cap on total L5b firings per task
- No relevance gate (any graph neighbor qualifies)
- No dedup (same file can be suggested multiple times)
- GHA canary: GT_L5_GOKU_EVENTS=0 → L5b active

## 3. jedi__branch

Same code, same canary config. Same bugs.

## 4. Runtime Trajectory Reality

- weasyprint: 9x L5b (table.py, float.py ×3, column.py, test_block.py) — all irrelevant
- cfn-lint: 9x L5b (test_formatters.py, test_ref.py, _filter.py ×3, NumberRange.py, etc.) — 8/9 irrelevant
- pypsa: 4x L5b (test_statistics.py) — relevant but agent already lost
- Agent ignored 100% of L5b across all tasks
- 0% follow rate = pure context waste

## 5. Research

- ProAIDE IUI 2026: 62% dismissal rate for unsolicited suggestions
- Du et al. EMNLP 2025: context length hurts even with perfect retrieval
- Cursor behavior: show inline hints, don't push follow-up reminders

## 6. Gap Analysis

DOC says "fires when goku is OFF." GHA has goku OFF. Result: fires on every unfollowed
next_action with no cap, no relevance filter, no dedup. The safety checker doesn't gate
on relevance or frequency — only on content safety.

## 7. Invariants

- L5B-INV-1: Max 2 L5b firings per task
- L5B-INV-2: L5b only suggests files that are in brief_candidates
- L5B-INV-3: Same file never suggested twice by L5b

## 8. Minimal Repair

Cap L5b at 2 firings per task. Only suggest files in brief_candidates. Dedup by file.

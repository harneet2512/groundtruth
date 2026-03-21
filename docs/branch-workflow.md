# Branch Workflow

How GroundTruth branches are planned, executed, and closed.

## Templates

| Situation | Template | Output |
|-----------|----------|--------|
| Starting any branch | `docs/templates/BRANCH_PLAN.template.md` | `BRANCH_PLAN.md` at repo root |
| Closing a branch (merge/continue/kill) | `docs/templates/BRANCH_REVIEW.template.md` | `docs/reviews/REVIEW-[branch].md` |

Or use `/branch-plan [capability]` to generate a plan from the template automatically.

## Classifying Work

| Type | Branch? | Plan? | Kill condition? |
|------|---------|-------|-----------------|
| **Research** | Feature branch required | Full plan with hypothesis | Required |
| **Productization** | Feature branch | Plan without hypothesis | Optional ("ship incrementally") |
| **Bugfix** | Feature branch if large | Plan only if multi-file | Not needed |
| **Cleanup / Docs** | Optional | No | No |

Rule of thumb: if you're not sure whether it's research or productization, it's research.

## Choosing the Cheapest Benchmark

Use `/eval-ladder` or apply directly:

| Change type | Start at |
|-------------|----------|
| Refactor, no logic change | Gate 0 only |
| New obligation rule or validator | Gate 0 + Gate 1 (fixture) |
| Changed briefing or context injection | Gate 0 + Gate 1, then Gate 2 if signal |
| Major architecture change | Gate 0 through Gate 3 |
| Merge candidate to main | Full ladder through Gate 4 |

**Never jump to Gate 4 without passing earlier gates.**

## Gate Progression

1. Run Gate 0 (unit tests) after every change.
2. When Gate 0 passes, run Gate 1 (fixtures/microbench).
3. If Gate 1 shows promise, run Gate 2 (10-task diagnostic).
4. If Gate 2 shows clear signal, consider Gate 3 (50-task).
5. Only run Gate 4 (300-task + Docker) for merge candidates.

Update the Status section in `BRANCH_PLAN.md` at each gate.

## Branch Lifecycle

```
1. Create branch from main
2. /branch-plan [capability]     → generates BRANCH_PLAN.md
3. Do the work                   → update PROGRESS.md at milestones
4. Progress through gates        → update Status in BRANCH_PLAN.md
5. /phase-review                 → get ship/defer/research/reject verdict
6. Fill in BRANCH_REVIEW         → evidence-based close-out
7. Merge, continue, or kill      → based on evidence, not hope
```

## Why This Exists

GroundTruth optimizes for proof over narrative. Every branch must:

- Start with a falsifiable claim (hypothesis or expected outcome)
- Define the cheapest valid benchmark first
- Define a merge threshold before writing code
- Track precision and false positives for any validation work
- Use 300-task runs only for promotion candidates, never for exploration

The cost of a disciplined branch plan is 10 minutes. The cost of an undisciplined branch is days of wasted eval time and ambiguous results.

## Skill Reference

| Skill | When to use |
|-------|-------------|
| `/branch-plan` | Create or update BRANCH_PLAN.md |
| `/eval-ladder` | Choose cheapest valid eval gate |
| `/phase-review` | Get ship/defer/research/reject verdict before merge |

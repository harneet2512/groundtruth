# Contract-DELTA — LOCKED acceptance criteria (pre-registered; do NOT move)

**Locked 2026-06-05, BEFORE any live verification run.** These criteria are fixed. I will report
results against THESE exact gates and will not redefine probe, gate, or success after seeing data.
Origin: on 2026-06-05 I called a *false-positive* drift "works" because it was delivered. Never again
— this spec makes "works" falsifiable in advance.

---

## 1. Probe selection (objective criterion — fixed)

A probe is FAIR only if ALL hold:
1. **Baseline-FAILURE** task — id is in the 213 non-resolved (NOT in `resolved_ids` of
   `full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`). (A flip is only possible on baseline=NO.)
2. **Explicit-contract gold** — the GOLD patch adds/removes/changes a `return`, a `raise`, or a
   guard/early-return on a function that has **≥1 verified caller** in graph.db. (Checked by reading the
   gold diff — this selects WHERE to measure, not what GT delivers; it is experiment design, not benchmaxxing.
   The gold is NEVER given to GT or the agent.)
3. **Not pre-localized to triviality** — the issue text does not already hand the exact gold file+function+line.

**Explicitly rejected probes (bad, on record):** `beetbox__beets-5495` (issue pre-localizes the gold;
fix changes no contract) and `delgan__loguru-1297` (fix is an implicit stdlib `OverflowError` clamp —
below structural detection). Re-using these to claim success is goalpost-moving and forbidden.

Pre-registered pick procedure: inspect gold diffs of baseline-failure candidates; take the first 2 that
meet criterion (1)+(2)+(3). Record the chosen ids in the run report.

## 2. The three gates — each PASS/FAIL, pre-committed

Verified from the agent's `output.jsonl` (AGENT-OBSERVATION rule), never telemetry/event counts.

**Gate 1 — DELIVERED.** A `<gt-evidence>` block containing `[CONTRACT-DELTA]` appears in ≥1 agent
observation after an edit.
  - PASS = present. FAIL = absent. (An EMPTY delta is NOT "delivered" — correct-or-quiet silence is a
    separate, valid outcome but does not pass Gate 1.)

**Gate 2 — CORRECT (the one that failed before).** For every `[CONTRACT-DELTA]` block emitted:
  - every function named in it was **actually edited** by the agent in that step's `git diff`, AND
  - the described change (return shape / raise / guard) **matches** the diff.
  - PASS = **zero** functions flagged that are not in the diff (0 false positives) AND ≥1 described
    change matches the diff. **FAIL = any false positive** (a flagged function not in the agent's diff).

**Gate 3 — CONSUMED.** After a `[CONTRACT-DELTA]` delivery, the agent **references the named
symbol/change OR revises its edit** in a later action.
  - PASS = ≥1 observable reaction in the trajectory. FAIL = zero reaction across all deltas.
  - A `utilization_score` is NOT evidence of consumption — only a trajectory-observed reaction counts.

## 3. Two SEPARATE verdicts (I commit to reporting BOTH)

- **"WORKS"** = Gate 1 PASS **and** Gate 2 PASS **and** Gate 3 PASS, on a fair probe.
  (Delivered, correct, used. NOT "delivered" alone.)
- **"PRODUCES VALUE (flip)"** = the task RESOLVES GT-on where baseline=NO **and** the trajectory shows the
  delta was consumed en route to the correct fix (right trajectory), paired vs the frozen baseline.

These are independent. **"Works" ≠ "produces flips."** A correct, consumed delta with 0 flips is reported
as *"works; 0 flip value on this probe"* — never as success-toward-flips.

## 4. Kill conditions (pre-committed)

- Gate 2 FAILS on a fair probe (any false positive) → lever is **BROKEN**. Fix or kill. Do not report "works".
- Gates 1+2 PASS but Gate 3 FAILS across **N ≥ 3** fair probes → lever is **INERT** (agent ignores it).
  Do not claim flip value; report inert.
- Consumed but **0 flips across the fair-probe set (≥3 tasks)** → report *"correct + consumed, no flip
  value"*. Do not iterate the probe set to fish for a flip.

## 5. Anti-goalpost rules (explicit)

- Will NOT redefine "fair probe" after seeing results.
- Will NOT count an empty (correctly-quiet) delta as a delivery pass.
- Will NOT cite `utilization_score` as consumption.
- Will NOT call DELIVERED alone "works."
- Will report the chosen probe ids, the raw `[CONTRACT-DELTA]` text, the agent's `git diff`, and the
  per-gate PASS/FAIL in the run report, so the verdict is auditable against this locked spec.

---
*Locked. Any change to this file after the first verification run must be dated and justified as a
correction, not a redefinition of success.*

# Research: Structural Twin Detection for Edit Consistency

## The Principle

When an agent edits code, it treats its change as an INSERTION (additive). But real code has STRUCTURAL TWINS — code in the same scope that handles parallel cases. Modifying one twin without considering the other causes inconsistent behavior.

GT's role: after edit, detect structural twins within the same function/file and surface them. This forces the agent to reason about INTERACTION, not just addition.

## Research Evidence

| Source | Venue/Year | Finding | Number |
|---|---|---|---|
| SWE-Bench+ | arXiv 2024 | LLM agents produce incomplete patches (main fix present, related change missing) | 5.56-9.82% of patches |
| PatchDiff | arXiv 2025 | Plausible patches with behavioral divergence from gold | 29.6% divergent, of which 46.8% are "similar but incomplete" |
| Multi-hunk ASE 2025 | ASE 2025 | Repair drops to near-zero when changes are structurally dispersed (Fragment class) | 0% success on most dispersed |
| LASE | ICSE 2013 | Edit propagation tool finds missed sites | 99% precision, 89% recall, found 9 developer-confirmed missed locations |
| Getafix (Meta) | OOPSLA 2019 | Pattern-based fix propagation to structural twins | 12-91% exact match depending on bug category |
| Mondal et al. | JSS 2019 | Bugs from fixing one clone but not its twin | 18-33% of clone-associated bugs |
| Krinke | WCRE 2007 | Inconsistent changes to code clone groups | ~50% of all clone changes are inconsistent |
| Maple + Gemini | arXiv 2025 | Enhanced localization of structural targets improves repair | +30% accuracy |
| HKI | arXiv 2025 | Structurally similar code as context improves fix quality | +17-23% fix rate |

## Mechanism Design

### What "Structural Twin" Means

Two lines are structural twins if they share the same PATTERN with different VALUES:
```python
if "FORCE_COLOR" in os.environ: return True   # twin A
if "NO_COLOR" in os.environ: return False      # twin B
```
Pattern: `if STRING in os.environ: return BOOL`

### Detection Algorithm (no LLM, no tests)

1. After edit, read the edited function body
2. For each line, create a pattern template:
   - Replace string literals with `STRING`
   - Replace numeric literals with `NUM`
   - Replace identifiers after `=` with `VAL`
   - Keep structure keywords (if, for, return, raise)
3. Group lines by template
4. If any group has 2+ members: these are structural twins
5. Show: "Lines N,M share pattern `template` — verify consistent handling"

### Why This Produces Flips

- Agent adds FORCE_COLOR check (correct for the issue)
- GT detects: line 10 and line 14 share pattern `if STRING in os.environ: return BOOL`
- GT shows: "Structural twin at L14: `if 'NO_COLOR' in os.environ: return False`"
- Agent thinks: "these should be handled consistently — if FORCE_COLOR checks value, NO_COLOR should too"
- Agent updates BOTH → gold patch behavior → resolves

Without GT: agent only thinks about its ADDITION, doesn't reconsider the EXISTING twin.

### Properties

- **Not test-dependent:** derived from code structure alone
- **Not caller-dependent:** about intra-function patterns, not inter-function relationships
- **Not spec-dependent:** doesn't require external documentation
- **Language-agnostic:** pattern templates work for any language with if/return/assignment
- **Scale-agnostic:** function-local analysis, O(lines_in_function)
- **Repo-agnostic:** no repository-specific tuning

### Expected Impact

Based on research:
- 5-30% of agent patches are incomplete (structural twin missed)
- Showing the twin prevents the miss in ~89% of cases (LASE precision)
- Net: 5-30% × 89% = 4-27% of currently-failing tasks become flippable
- Conservative estimate on 300 tasks: 0.04 × 210 fails = 8 flips → +2.7pp
- Optimistic: 0.15 × 210 = 31 flips → +10pp

### Integration Point

L3 post-edit hook (fires after every source edit). Current L3 shows callers. Add structural twin detection as a SECOND signal:

```
CALLERS: auth.py:42 `if user is None: raise AuthError`
TWIN: L14 `if "NO_COLOR" in os.environ: return False` (same pattern as your edit)
```

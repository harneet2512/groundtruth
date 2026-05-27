# L1 Edit-Target Gen6 Audit

GHA Run: 26542650701. All values from output.jsonl trajectory reading.

| Task | Selected target | Callers | File agent edited | Target matched? | Notes |
|------|----------------|---------|-------------------|-----------------|-------|
| weasyprint-2300 | flex_layout() in flex.py | 4 | block.py | NO | Target was flex.py but agent edited block.py. However task RESOLVED — agent navigated correctly despite wrong target. |
| flexget-4306 | Session() in requests.py | 246 | qbittorrent.py | NO | Target was requests.py but agent edited qbittorrent.py. Task did NOT resolve. Session() is a generic high-caller hub. |
| pypsa-1172 | Network() in networks.py | 97 | (no edits) | N/A | Agent never edited. Network() is a generic hub class, not the fix target. |
| cfn-lint-3875 | Properties() in Properties.py | 1 | (no edits) | N/A | Agent never edited. Properties is a rule class. |
| sh-744 | bake() in sh.py | 23 | sh.py | PARTIAL | Agent edited sh.py (correct file) but the actual function edited was __await__, not bake(). bake() was a reasonable orientation. |
| arviz-2413 | plot_hdi() in hdiplot.py | ? | hdiplot.py | YES | Agent edited the exact file and function. Task did NOT resolve (test failures). |

## Analysis

**Correct:** 1/6 (arviz — exact match)
**Partial:** 1/6 (sh — right file, wrong function)
**Wrong:** 2/6 (weasyprint, flexget — wrong file entirely)
**N/A:** 2/6 (pypsa, cfn-lint — no edits made)

**Pattern:** High-caller-count generic classes (Session 246cal, Network 97cal) dominate selection. The score-all fix (commit a100fd21) prevents first-match-wins but caller count still drives the ranking when no issue keyword matches a function name exactly.

## Verdict

Edit target quality is CONDITIONAL:
- Works when the root-cause function is in a structurally prominent file (arviz)
- Partially works when the right file is prominent but wrong function selected (sh)
- Fails when root cause is in a non-prominent file (weasyprint, flexget)

**No code change in this commit.** The edit target is a best-effort orientation signal. Tasks resolve despite wrong targets (weasyprint resolved, sh resolved). The agent's own exploration compensates. Fixing this properly requires semantic localization (LLM-based), which violates GT's "$0 AI" constraint.

**Status:** CONDITIONAL — documented, not patched.

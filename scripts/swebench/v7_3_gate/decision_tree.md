# v7.3 verification gate -- decision tree

Source: `D:\Groundtruth\localization_docs\v7\HANDOFF_GT_TO_FULL_POTENTIAL.md` section 4.
Consumed by: human reading `~/runs/v7_3_gate_<ts>_VERDICT.md`.

---

## Top-level branches

| OUTCOME (from VERDICT.md `<!-- gate_outcome: ... -->` marker) | Action |
|---|---|
| `BOTH_PASS_NO_REGRESSION` | v7.3 lands as default. Trigger Phase 0 (kernel API spec). |
| `ARM_FAIL` | At least one arm hit a hard-zero or mechanism gate FAIL. Move that run dir to `~/runs/fast_diag/<run_id>/` per CLAUDE.md. Do NOT compare arms. Diagnose root cause first. Do not launch follow-up repeats while a prior run is FAIL. |
| `BOTH_PASS_V73_REGRESSION` | Both arms PASS verify_report, but paired Wilcoxon shows v7.3 < v7.2 with p<0.05. Drop to the regression sub-table below. |
| `BOTH_PASS_AMBIGUOUS` | Both arms PASS verify_report, paired Wilcoxon p in (0.05, 0.10) or sign mismatch. Re-smoke at n=15 before deciding. |
| `BOTH_PASS_NO_PAIRED_TEST` | All paired deltas are zero (no signed-rank possible). No regression observed; treat as `BOTH_PASS_NO_REGRESSION` only after confirming brief_chars and sanitizer counts also unchanged. |
| `INCONCLUSIVE` | verify_report verdicts could not be parsed. Re-run `bash verify_and_report.sh` and inspect `verify_results.md` Part 3 by hand. |

---

## Regression sub-table (only when `BOTH_PASS_V73_REGRESSION`)

Per handoff section 4 sub-table. Look at the VERDICT.md "Brief size + sanitizer counts" and "Confidence distribution" sections to choose the branch:

| Diagnosis | Signal in VERDICT.md | Action |
|---|---|---|
| Cosmetic only | brief_chars distribution shifted, but sanitizer_affected_lines_total ~ 0, confidence buckets unchanged | Patch `_sanitize_brief_line` to skip pattern-list lines, re-smoke. |
| Confidence regress | confidence bucket `0_5_to_0_6` is materially populated for v7.3 but framing flipped suggestive on tasks where v7.2 was directive | Tune `HIGH_CONFIDENCE_MIN`: try 0.5 (more directive) or 0.7 (more suggestive). Re-smoke. |
| Sanitizer regress | sanitizer_affected_lines_total >> v7.2 AND v7.3 lost CONTRACT info on regressing tasks (inspect briefs by hand) | Add fallback: if stripping leaves <8 chars of content, keep original line. Re-smoke. |
| Both regress | sanitizer + confidence both implicated | Revert to v7.0 render, ship sanitizer-only fix as v7.0.1. Confidence gating goes back to drawing board. |

---

## How to read the recommended action

After verify_and_report.sh writes `~/runs/v7_3_gate_<ts>_VERDICT.md`, run this one-liner to extract the outcome marker and print the matching action verbatim from this file:

```bash
VERDICT=$(ls -t ~/runs/v7_3_gate_*_VERDICT.md 2>/dev/null | head -1)
if [ -z "$VERDICT" ]; then
  echo "FAIL: no VERDICT.md found in ~/runs/"
  exit 1
fi
OUTCOME=$(grep -oE 'gate_outcome: [A-Z_]+' "$VERDICT" | head -1 | awk '{print $2}')
echo "VERDICT file: $VERDICT"
echo "OUTCOME:      $OUTCOME"
echo ""
echo "Recommended action (from decision_tree.md):"
echo ""
case "$OUTCOME" in
  BOTH_PASS_NO_REGRESSION)
    echo "  v7.3 lands as default. Trigger Phase 0 (kernel API spec)." ;;
  ARM_FAIL)
    echo "  At least one arm hit a hard-zero or mechanism gate FAIL."
    echo "  Move that run dir to ~/runs/fast_diag/<run_id>/ per CLAUDE.md."
    echo "  Do NOT compare arms. Diagnose root cause first."
    echo "  Do not launch follow-up repeats while a prior run is FAIL." ;;
  BOTH_PASS_V73_REGRESSION)
    echo "  Both arms PASS verify_report, but paired Wilcoxon shows v7.3 < v7.2"
    echo "  with p<0.05. Read the regression sub-table in decision_tree.md and"
    echo "  match the diagnosis to brief_chars / sanitizer / confidence signals." ;;
  BOTH_PASS_AMBIGUOUS)
    echo "  Both arms PASS verify_report, paired Wilcoxon p in (0.05, 0.10) or"
    echo "  sign mismatch. Re-smoke at n=15 before deciding." ;;
  BOTH_PASS_NO_PAIRED_TEST)
    echo "  All paired deltas are zero (no signed-rank possible). No regression"
    echo "  observed; treat as no-regression only after confirming brief_chars"
    echo "  and sanitizer counts also unchanged." ;;
  INCONCLUSIVE|"")
    echo "  verify_report verdicts could not be parsed. Re-run verify_and_report.sh"
    echo "  and inspect verify_results.md Part 3 by hand." ;;
  *)
    echo "  Unrecognized OUTCOME=$OUTCOME. Read VERDICT.md by hand." ;;
esac
echo ""
echo "User decides + executes. No automation of the action."
```

The user invokes this; nothing is auto-applied.

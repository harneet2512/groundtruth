# L5 Noise Gen6 Audit

| Task | L5 count | Variants | Duplicates within 5 entries | Assessment |
|------|----------|----------|----------------------------|------------|
| weasyprint-2300 | 5 | 1 NoSourceEdits + 4 IgnoredWitness | 0 | acceptable |
| flexget-4306 | 1 | 1 IgnoredWitness | 0 | minimal |
| pypsa-1172 | 4 | 1 NoSourceEdits + 3 IgnoredWitness | 0 | acceptable |
| cfn-lint-3875 | 14 | 1 NoSourceEdits + 13 IgnoredWitness | 0 within 5, but 2 same-target repeats | borderline noisy |
| sh-744 | 1 | 1 NoSourceEdits | 0 | minimal |
| arviz-2413 | 0 | — | 0 | silent |

## cfn-lint detail (14 messages, heaviest)

All 13 IgnoredWitness messages suggest different files — the agent is browsing many files and ignoring structural suggestions for each. This is not spam (each is unique) but it's heavy. The agent may be overwhelmed by 13 reminders.

Duplicates: e123 and e147 both target `NumberRange.py` (24 entries apart). Not within 5 entries.

## Verdict

L5 is NOT critically noisy on gen6. No same-file duplicates within 5 entries. cfn-lint is borderline at 14 messages but each targets a different file. No code change needed in this commit.

If future runs show same-file duplicates within 5 entries, add rate-limit. For now: document and move on.

**Status:** CLEAN — no patch needed.

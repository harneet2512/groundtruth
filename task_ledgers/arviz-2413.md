# Ledger -- arviz-devs__arviz-2413  (run 27107841613, branch gt-trial, 2026-06-07)

Outcome: **no_patch (agent job=failure)** . resolved=0 . baseline_pass=no . flip=no

## UNAUDITABLE -- no agent-observation trajectory
- agent job: no_patch (agent job=failure)
- artifacts uploaded: eval_result.json only
- **output.jsonl: ABSENT** -> per gt_trial.md section 4, the per-component audit (GT SENT / AGENT DID) CANNOT be performed: there is no agent-observation record to READ.
- reason: agent produced no patch; no output.jsonl uploaded

| component | verdict |
|---|---|
| PREREQS . L1 . L3b . consensus . L3/GT_VERIFY . L4 . L5 . L5b . L6 | **UNAUDITABLE -- no output.jsonl** |

**Cross-component line:** leakage=unverifiable . delivered=unverifiable . consumed=unverifiable.
**FINDING:** no auditable trajectory -- part of the artifact gap (5/10 tasks this run left no readable output.jsonl: 3 no_patch eval-only + 2 cancelled). Fix: per-job timeout that uploads the partial trajectory on cancel/failure so every task is auditable.

---

# §4 DEEP AUDIT — arviz-devs__arviz-2413 (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: **UNRESOLVED** (submitted, completed, non-empty patch) · baseline_pass=NO · flip=NO · regression=NO (flip-candidate)
Gold files: `arviz/plots/hdiplot.py` + `CHANGELOG.md`. FAIL_TO_PASS: `test_plot_hdi_string_error`.
Source: output.jsonl history (93 events) read chronologically. actions=40, edits=1.

## (a) PREREQS / substrate (gt_gates_deep, 8-dp, telemetry → reached agent only as resolved-edge lines in brief)
| dim | REAL value (verbatim from gate-deep JSON) | GREEN? | how it reached the agent |
|---|---|---|---|
| P1 resolution | det_pct=85.87834964 · name_match=332 · calls_edges=2351 · typing: type_flow=7/impl_method=28/inherited=0 | YES | as `Witness: … [CALLS]` / `Calls:` lines in brief + `Called by:` in post-view |
| P2 graph.db | calls_edges=2351 · breakdown verified_unique=1084/same_file=534/lsp=362/name_match=332/impl_method=28/type_flow=7/import=4 | YES | `<gt-graph-map>` (histogram, WRONG node) |
| P3 embedder | class=EmbeddingModel · cos_related=0.86053280 · cos_unrelated=0.76078654 · is_zero=false · effective_w_sem=0.15 · sem_max=0.86277800 | YES | ranking only (not text) |

## (b) per-component tables

### L1 brief (event id=1, user message, prepended)
| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| id=1 | `<gt-localization confidence="medium"> Candidate edit targets: 1. arviz/stats/density_utils.py … 2. arviz/plots/kdeplot.py … 3. arviz/stats/stats.py … 4. arviz/plots/bpvplot.py … 5. arviz/plots/loopitplot.py … 6. arviz/plots/posteriorplot.py …` | id=5 agent plan: "Read hdiplot.py" — formed from the ISSUE (which links hdiplot.py), NOT from this list | **DELIVERED=YES · CORRECT=NO** (gold `arviz/plots/hdiplot.py` is NOT in the ranked candidate list — all 6 are wrong files) · **CONSUMED=NO** (agent ignored the list) |
| id=1 | `<gt-task-brief> 1. arviz/stats/density_utils.py (def histogram(… ) …) Witness: _bw_isj called by histogram [CALLS] …` | agent never reads density_utils.py | DELIVERED=YES · CORRECT=NO (wrong primary) · CONSUMED=NO |
| id=1 | `<gt-graph-map> arviz/stats/density_utils.py :: histogram …` | — | DELIVERED=YES · CORRECT=NO · CONSUMED=NO |
| id=1 | `<gt-orientation> Issue references: plot_hdi() in hdiplot.py … plot_hdi() in hdiplot.py (6 callers)` | id=5/id=7 agent reads hdiplot.py | DELIVERED=YES · CORRECT=YES (names gold) · CONSUMED=partial — but this block merely **echoes the issue text** (issue links hdiplot.py); it is the agent self-localizing, not GT's localizer ranking |

**L1 verdict:** D=YES, C=**NO** (ranked localizer mislocalized; gold absent from candidates; only the issue-echo "orientation" names gold). leakage=0.

### L3b post-view contract (observation on `read hdiplot.py`)
| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| id=8 | `[GT] hdiplot: [CONTRACT] def plot_hdi( … [RAISES] WHEN isinstance(x[0], np.datetime64): raise TypeError("Cannot deal with x as type datetime. Recommend setting smooth=False.")` + `<gt-scope files="5"> 1. plots/hdiplot.py — in scope (you are viewing this) …` | id=51 agent edits hdiplot.py: adds `if isinstance(x[0], str): raise TypeError("Cannot deal with x as type string (categorical). Recommend setting smooth=False.")` **INSIDE the `if smooth:` block, next to the datetime check** | **DELIVERED=YES · CORRECT=YES** (real contract on the gold file) · **CONSUMED=YES — but HARMFULLY**: the agent copied GT's surfaced `[RAISES] … datetime: raise TypeError` pattern, producing the WRONG exception type/location |

**L3b verdict:** D=YES, C=YES (content true), CONSUMED=YES — but the consumed pattern (TypeError inside `if smooth:`) is exactly what made the fix wrong. leakage=0.

### L3 / GT_VERIFY (appended to run obs id=56)
| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| id=56 | `[GT_VERIFY] You edited 1 file(s). … confirm your change preserves the behavioral contract: hdiplot.py: exception_type = ValueError / hdiplot.py: exception_type = TypeError / …` | id=57-76 agent runs pytest -k test_plot_hdi (9 pre-existing PASS — the new grader test is NOT in the repo at HEAD) | DELIVERED=YES · CORRECT=YES (lists real existing exception types) · CONSUMED=YES (ran tests) — but the surfaced `exception_type = TypeError` REINFORCED the wrong choice; never hinted NotImplementedError |

### consensus / L4 / L5 / L5b / L6
| component | row |
|---|---|
| consensus | DELIVERED=NO — no `<gt-scope … primary target …>` consensus pick fired; brief was MEDIUM-tier mislocalized |
| L4/L5/L5b/L6 | DELIVERED=NO — no distinct agent-visible bytes beyond L1+L3b+GT_VERIFY above |

## (c) verdicts
- L1: delivered; **CORRECT=NO** (mislocalized; gold not ranked). L3b: delivered+correct+consumed (but consumption was harm-shaped). GT_VERIFY: delivered+correct+consumed.
- **Cross-component:** test-name/FAIL_TO_PASS leakage=**0** · consumed-count=2 (L3b contract, GT_VERIFY) · fair-probe: the issue text directly links `hdiplot.py`, so localization is NOT a fair GT probe here (agent self-localized).

## right_trajectory = **FALSE**
The L1 ranked localizer pointed at 6 WRONG files; the agent reached the gold `hdiplot.py` by itself from the issue's own link. GT's post-view contract was correct content but its `[RAISES] … TypeError` example nudged the agent to raise `TypeError` inside `if smooth:`, while the gold raises `NotImplementedError` **unconditionally** with an exact message (`"The arviz.plot_hdi() function does not support categorical data. Consider using arviz.plot_forest()."`). Test `test_plot_hdi_string_error` calls `plot_hdi(x=str, y, hdi_data=…)` and asserts `pytest.raises(NotImplementedError, match=…)` → triple mismatch (type, message, location). **Failure locus: post-localization implementation miss (wrong exception type + message + placement).** GT did not deliver the correct trajectory: localizer wrong, and the one correct delivery (contract) steered toward the wrong exception family.

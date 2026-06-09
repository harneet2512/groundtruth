# Ledger — beancount__beancount-931  (run 27214152241, branch gt-trial, 2026-06-09)

> §4 DEEP AUDIT, independent chronological read of THIS run's `output.jsonl`
> (`task-beancount__beancount-931/results/.../deepseek-v4-flash_maxiter_100/output.jsonl`, 97 history events).
> Read turn-by-turn with the Read tool (never grep); quotes verbatim. Lead with the trajectory,
> not pass/fail.

Outcome: resolved=**yes** (`task-beancount__beancount-931/eval_result.json` `"resolved_ids":["beancount__beancount-931"]`) · baseline_pass=**yes** (in frozen `full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json` resolved_ids) · flip=**NO** (resolved AND baseline already passed). GOLD = `beancount/plugins/leafonly.py::validate_leaf_only` (gold: skip accounts whose postings are all `(data.Open, data.Balance)`). FAIL_TO_PASS = `beancount/plugins/leafonly_test.py::TestLeafOnly::test_leaf_only3`.

## (a) PREREQS / substrate  (`gt-gate-deep-beancount__beancount-931/gt_gates_deep_*.json`, 8-dp)
| dim | REAL value | GREEN? | how it reached the agent |
|---|---|---|---|
| P1 resolution | calls_edges=2858 · deterministic=2562 · det_pct=**89.64310707** · name_match=296 · tiers type_flow=0 / impl_method=116 / inherited=120 | YES | telemetry-only; reaches agent only as brief edge lines |
| P2 graph.db | breakdown: lsp=983, same_file=793, import=411, name_match=296, verified_unique=139, inherited=120, impl_method=116 | YES | mediated via brief edge lines |
| P3 embedder | class=EmbeddingModel · is_zero=**false** · cos_related=**0.86053280** · cos_unrelated=**0.76078654** · effective_w_sem=0.15 · sem_max=0.84306800 · sem_median=0.80053800 | YES | affects candidate ordering only |

Foundational gates ALL GREEN (`verdict.all_on=true`) → run NOT void on substrate. (Substrate correct != GT caused it.)

## (b) per-component tables — `turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C`

### L1 brief (prepended block, EVENT 1)
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="medium"> Candidate edit targets: 1. beancount/ops/summarize.py - open, close, clear ... 2. beancount/core/data.py ... 3. beancount/scripts/example.py ... 4. beancount/plugins/nounused.py ... 5. beancount/plugins/leafonly.py - validate_leaf_only 6. beancount/core/realization.py ...`; `<gt-task-brief>` headlines `summarize.py` (`get_open_entries`/`conversions`/`transfer_balances`) + `EDIT-TARGET CONTRACTS (summarize.py)`; `<gt-graph-map> beancount/ops/summarize.py :: get_open_entries`; `<gt-orientation> check() in balance.py / balance() in grammar.py / validate_unused_accounts() in nounused.py`; `[GT KEY CONTRACTS] Preserve: conditional_return: if conversion_cost_balance.is_empty(): return entries` | EVENT 4: plan 6 items. EVENT 6: `read /workspace/...` (repo dir). EVENT 8: `read beancount/plugins/` (the agent goes STRAIGHT to the plugins dir — because the ISSUE text says "when leafonly plugin is enabled"). EVENT 10: `read beancount/plugins/leafonly.py` = the GOLD file, as its FIRST file read. | **D=YES / C=NO (misranked + off-target detail) / C=NO** |

Verdict L1: DELIVERED. CORRECT=NO — the gold file `leafonly.py` is buried as **candidate #5 of 6**, while the ENTIRE detailed brief (task-brief, graph-map, orientation, key-contract) points at the WRONG files (`summarize.py` primarily, plus `data.py`/`example.py`/`nounused.py`/`balance.py`/`grammar.py`). CONSUMED=NO — the agent ignored the brief's #1/summarize.py emphasis and opened the gold file directly from the issue's own word "leafonly plugin". Leak=0.

### L3b post-view (`[GT] leafonly:` / `[GT] data:` / `<gt-context>`, EVENTS 11, 19, 31)
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 11 (after read leafonly.py — gold) | `[GT] leafonly:` + `[CONTRACT] def validate_leaf_only(entries, unused_options_map):` + `flows: entries -> realization.realize(...) | getters.get_account_open_close(entries) | entries, errors` + `<gt-scope files="1"> leafonly.py is the file you're viewing; GT could not expand scope from the graph - confirm the edit target with grep.` | EVENT 12: reads `leafonly_test.py` (its own choice). EVENT 14 `think`: already states the full fix ("skip `balance` directives ... when checking whether non-leaf accounts have postings"). | D=YES / C=partial (right file/fn, but no `Balance`/`TxnPosting` filtering insight — the crux) / C=NO |
| EVENT 19 (after read data.py) | `[GT] data:` + `[CONTRACT] def sanity_check_types( / def source(self) / def message(self)` + `Called by: ... grammar.py:191 ... summarize.py:446 ...` | reads `class Balance` types; greps `TxnPosting` (EVENT 26). | D=YES / C=NO (off-target contracts/callers) / C=NO |

Verdict L3b: DELIVERED. On the gold file (EVENT 11) it surfaced the right function signature but NOT the directive-type distinction (`Balance`/`Open`/`TxnPosting`) that IS the fix; the agent derived that itself. `<gt-scope>` explicitly abstained. Not consumed. Leak=0.

### consensus `<gt-scope>` (EVENT 11)
| turn | GT SENT | AGENT DID | D/C/C |
|---|---|---|---|
| EVENT 11 | `<gt-scope files="1"> ... GT could not expand scope from the graph - confirm the edit target with grep.` | already on the gold file by its own choice; proceeds with its own greps. | D=YES / C=honest-abstain / C=NO |

Verdict consensus: correct-or-quiet abstain; no scope expansion; not causal. Leak=0.

### L3 / GT_VERIFY + governor L5 nudge (EVENTS 47, 57, 79)
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 47 | `[GT L5: No Source Edits] Iteration: 25/100 You have run 25 actions with 0 source file edits.` | was installing env (`pip install -e .`) so its own tests would run; edits at EVENT 73 per its own plan. | D=YES / C=n/a / C=NO |
| EVENT 57 | `[GT] realization.py was confirmed earlier. Key evidence: [CONTRACT] def iterate_with_balance(txn_postings): Scope: leafonly.py` | continues its own `TxnPosting`-import reasoning (EVENT 64). | D=YES / C=restates known file / C=NO |
| EVENT 79 (after fix) | `[GT_VERIFY] You edited 1 file(s). ... preserve the behavioral contract: leafonly.py: return_shape = value|entries, errors` | runs leafonly_test + loader_test + full plugin suite (all pass). | D=YES / C=partial (true return shape) / C=NO |

Verdict L3/GT_VERIFY: delivered; not consumed; restated a return-shape the agent's edit already preserves.

### L4 / L5b / L6
DELIVERED=NO — no agent-visible bytes attributable to these in `output.jsonl`.

## THE EDIT (EVENT 72-73, self-driven)
GT SENT: nothing new at the edit. AGENT DID (EVENT 14 `think`, verbatim, BEFORE any deep GT contract): "The fix should be: in the `validate_leaf_only` function in `leafonly.py`, skip `balance` directives (and possibly other non-posting directives) when checking whether non-leaf accounts have postings." Refined at EVENT 56 to "only count `TxnPosting` instances". EVENT 73 edit: adds `from beancount.core.data import TxnPosting` and `if (len(real_account) > 0 and any(isinstance(item, TxnPosting) for item in real_account.txn_postings)):`. Different mechanism than gold (gold allow-lists `(data.Open, data.Balance)`) but functionally equivalent — both stop balance directives on non-leaf accounts triggering the error; resolves `test_leaf_only3`. CONSUMED of GT = NO.

## Cross-component line
leakage=**0** (GT surfaced NO test name / FAIL_TO_PASS; the grader test `test_leaf_only3` was NOT yet in the file — repo had only `test_leaf_only1/2` — and GT never named it) · consumed=**0** (no GT payload changed an agent decision; brief mis-emphasized `summarize.py`; agent self-localized to gold from the issue word "leafonly plugin") · fair-probe=**BAD PROBE / PRE-LOCALIZED** (issue title "Allow 'balance' check directives against non-leaf accounts" + body "when leafonly plugin is enabled" names the exact plugin/file) · GT reaction telemetry (cross-ref) agrees: `IGNORED: 3, NOT_MEASURABLE: 1` — GT itself scored all its events as ignored.

## VERDICT: gt_caused = **FALSE**
Agent self-localized to `leafonly.py::validate_leaf_only` directly from the issue text ("leafonly plugin"), reading it as its FIRST file. By EVENT 14 it had stated the correct fix from the issue + leafonly.py + leafonly_test.py alone. GT's brief actively MIS-EMPHASIZED the wrong file (`summarize.py` as #1; gold buried at #5/6); the agent ignored that emphasis. GT post-view echoes confirmed the file the agent already had, never the directive-type insight that was the crux. ZERO GT payload changed an agent decision; GT's own telemetry says IGNORED. Resolved != GT win. baseline_pass=yes, flip=no. (Net: a near-miss for HARM — had the agent trusted GT's #1 ranking it would have wandered into summarize.py; it was saved by the issue text, not GT.)

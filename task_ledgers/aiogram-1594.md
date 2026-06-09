# Ledger -- aiogram__aiogram-1594  (run 27107841613, branch gt-trial, 2026-06-07)

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

# §4 DEEP AUDIT — aiogram__aiogram-1594 (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: **UNRESOLVED** (submitted, completed, non-empty patch) · baseline_pass=NO · flip=NO · regression=NO (flip-candidate)
Gold files (MULTI-FILE feature): `aiogram/fsm/context.py` (FSMContext.get_value) + `aiogram/fsm/scene.py` (SceneWizard.get_value) + `aiogram/fsm/storage/base.py` (BaseStorage.get_value) + `aiogram/fsm/storage/memory.py` (MemoryStorage.get_value) + `CHANGES/1431.feature.rst`.
FAIL_TO_PASS: `test_context.py::test_address_mapping`, `test_scene.py::test_scene_wizard_get_value_with_default`, `test_storages.py::test_set_data[memory_storage]`.
Source: output.jsonl history (66 events) read chronologically. actions=29, edits=2 (one is final no-op verify).

## (a) PREREQS / substrate
| dim | REAL value (gate-deep, 8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| P1 resolution | det_pct=76.79764769 · name_match=868 · calls_edges=3741 · typing: type_flow=80/impl_method=360/inherited=16 | YES | `Witness … [CALLS]`, `Called by:` lines |
| P2 graph.db | calls_edges=3741 · verified_unique=966/name_match=868/import=638/same_file=407/lsp=398/impl_method=360/type_flow=80 | YES | `<gt-graph-map>`, `<gt-scope>` |
| P3 embedder | class=EmbeddingModel · cos_related=0.86053280 · cos_unrelated=0.76078654 · is_zero=false · effective_w_sem=0.15 · sem_max=0.82035600 · pred_2_coverage=false (only 1 sem signal) | YES (pass) | ranking only |

## (b) per-component tables

### L1 brief (event id=1)
| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| id=1 | `<gt-localization confidence="medium"> Candidate edit targets: 1. aiogram/fsm/scene.py — HistoryManager,… 2. aiogram/fsm/context.py — FSMContext, __init__, set_state … 3. aiogram/client/bot.py … 4. aiogram/utils/i18n/middleware.py … 5. aiogram/dispatcher/dispatcher.py … 6. aiogram/utils/keyboard.py …` | id=9 agent reads context.py; id=25 reads scene.py | **DELIVERED=YES · CORRECT=partial** — gold `scene.py` (rank 1, wrong symbol HistoryManager) and gold `context.py` (rank 2, RIGHT class FSMContext) BOTH surfaced; storage gold files not in list · **CONSUMED=YES** (agent read context.py + scene.py) |
| id=1 | `<gt-task-brief> … Likely multi-file scope: quiz_scene.py, scene.py … Scope chain (graph-connected, check ALL): scene.py → context.py` | id=27 agent THINK: "The issue is straightforward - add a get_value method to FSMContext class" | DELIVERED=YES · CORRECT=YES (multi-file scope is the gold reality) · **CONSUMED=NO — agent explicitly collapsed scope to FSMContext-only** |

### L3b post-view contract
| turn | GT SENT (verbatim) | AGENT DID | D/C/C |
|---|---|---|---|
| id=10 | `[GT] context: [CONTRACT] async def set_state… get_data() in context.py:20 … <gt-scope files="6"> 1. fsm/context.py — in scope … 3. fsm/scene.py — verified by language server 4. storage/base.py — verified by language server 5. i18n/middleware.py — verified by language server …` | id=31 agent edits ONLY context.py: `async def get_value(self, key, default=None): data = await self.get_data(); return data.get(key, default)` | **DELIVERED=YES · CORRECT=YES** (gt-scope named 3 of 4 gold files: context.py, scene.py, storage/base.py) · **CONSUMED=NO** — agent read scene.py (id=25/26 `[GT] scene: … also in scope`) and storage/base.py (id=15/16 `[GT] base: … also in scope`) but added get_value to NEITHER |
| id=16 | `[GT] base: [CONTRACT] async def set_state… get_data() in base.py:138 … [GT] base.py: also in scope.` | no edit to base.py | DELIVERED=YES · CORRECT=YES · CONSUMED=NO |
| id=26 | `[GT] scene: [CONTRACT] … [GT] scene.py: also in scope.` | no edit to scene.py | DELIVERED=YES · CORRECT=YES · CONSUMED=NO |

### consensus / L3-GT_VERIFY / L4 / L5 / L5b / L6
| component | row |
|---|---|
| consensus | DELIVERED=NO — no single-primary consensus block; brief stayed MEDIUM "GT has not confirmed a single primary target" |
| GT_VERIFY | DELIVERED=YES (id=55 `gt_validate` ran but path=`unknown` → no-op; later `git diff` shows context.py-only) · CONSUMED=NO actionable signal (gt_validate CALLER-BLIND warning informational) |
| L4/L5/L5b/L6 | DELIVERED=NO — no distinct agent-visible bytes |

## (c) verdicts
- L1: delivered + correct(scope flag) — **CONSUMED=NO** on the multi-file signal. L3b: delivered+correct on 3 gold files — **CONSUMED=NO** (agent edited none of scene/base/memory).
- **Cross-component:** test-name/FAIL_TO_PASS leakage=**0** · consumed-count=1 (read the named files, but acted on only context.py) · fair-probe: issue names `FSMContext`/`get_value` so context.py is partly self-localizable; the MULTI-FILE scope (the actual difficulty) was a GT-only signal the agent ignored.

## right_trajectory = **FALSE**
This is the cleanest GT-delivered, agent-ignored case. GT correctly surfaced the multi-file nature: `<gt-scope files="6">` named context.py + scene.py + storage/base.py (3 of 4 gold files), and the brief said "Likely multi-file scope" + "Scope chain (check ALL): scene.py → context.py." The agent READ scene.py and storage/base.py (with GT contracts attached) but at id=27 decided "the issue is straightforward — add get_value to FSMContext" and edited ONLY `aiogram/fsm/context.py` (3-line method `return data.get(key, default)`). Final `git diff` (id=58) = context.py only. The 3 FAIL_TO_PASS tests require `SceneWizard.get_value` (scene.py), storage `get_value` (base.py/memory.py) — all skipped. **Failure locus: post-localization implementation miss — incomplete multi-file scope, despite GT explicitly delivering the correct broader scope.** GT delivered correct context; the agent did not consume the scope signal. Not a GT-correctness failure of delivery, but GT did not CONVERT the agent (no consensus/forcing of multi-file completion).

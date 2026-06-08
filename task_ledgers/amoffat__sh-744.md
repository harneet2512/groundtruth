# Ledger — amoffat__sh-744  (run 27107841613, branch gt-trial, 2026-06-07)

Outcome: resolved=**yes** (`eval_result.json` `"resolved_ids":["amoffat__sh-744"]`, `resolved_instances:1`) · baseline_pass=**yes** (`amoffat__sh-744` ∈ `full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json` `resolved_ids`, 87/300) · flip=**no** (resolved AND baseline already passed). GOLD file = **`sh.py`** (gold patch edits `RunningCommand.__await__`, lines 889-895: `if self.call_args["return_cmd"]: return self  else: return str(self)`).

> Fair-probe note: this is a **baseline-pass** task — the baseline agent resolves it without GT (~88% self-localization). GT's job here is Stage-1 (deliver the RIGHT context, correct-or-quiet, no leak), NOT a flip. The audit below grades each component on whether it sent correct context the agent could consume, and whether it leaked.

---

## PREREQS (substrate — 8-dp verbatim from `gd-amoffat/gt_gates_deep_amoffat__sh-744.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1 resolution/jarvis** | det_pct=`89.16256158`; calls_edges=`406.0`; deterministic=`362.0`; name_match=`44.0`; typing_fired=`true`; typing_tier_counts=`{type_flow:0, impl_method:21, inherited:0, ev:assignment_tracked:0}`; pred_A_det_floor=`true`, pred_B_nondominance=`true`, pred_C_typing=`true` | **GREEN** (`pass:true`; name_match 44 ≪ deterministic 362) | telemetry-only — NO substrate number appears in any agent observation. Its sole agent-visible footprint is the brief's resolved-edge line, turn 1: `__await__ -> calls wait( self: Self@RunningCommand, timeout: Any \| None = None )  [sh.py:736]` and `2. tests/sh_test.py ... resolved call: -> Command() in sh.py:96`. |
| **P2 graph.db / resolution_method_breakdown** | `{same_file:285, name_match:44, lsp:34, impl_method:21, import:15, verified_unique:7}` (Σ=406) | **GREEN** (deterministic methods dominate; name_match 10.84%) | telemetry-only — reaches the agent only as the brief's `<gt-graph-map>` (`sh.py :: bake  calls: _extract_call_args (sh.py), compile_args (sh.py)  called by: bash, git, resolve_command, ssh, sudo`) + the EDIT-TARGET CONTRACTS edges. |
| **P3 embedder** | class=`EmbeddingModel`; is_zero=`false`; cos_related=`0.86053280`; cos_unrelated=`0.76078654`; effective_w_sem=`0.15000000`; semantic_signal_count=`1`; sem_max=`0.83620200`; sem_median=`0.41810100`; sem_mad=`0.41810100`; sep gap=`0.41810100` ≥ `1.0*MAD`; pred_1/2/3=`true` | **GREEN** (`pass:true`; cos_related>cos_unrelated, not zero) | telemetry-only — no cosine/weight number reaches the agent; it only re-orders the brief's candidate list (`<gt-localization>` rank #1 = `sh.py`). |

3-GATE VERDICT (from `l1_debug.txt`): `resolution/jarvis=ON  lsp_enrichment=ON  embedder=ON` → `verdict.all_on:true`.

**Prereqs verdict:** all three substrate gates **GREEN / all_on=true**, so the downstream component audit is meaningful (substrate is genuinely consumed by the pipeline that builds the brief). Substrate numbers themselves are **DELIVERED=NO** to the agent (telemetry-only, confirmed by full chronological scan — zero `[GT_META]`/`GT_GATE_METRICS`/`det_pct` lines in any turn); they reach the agent **only** mediated through the brief's resolved-edge and graph-map lines. **Leak: 0** in every substrate-surfaced line.

---

## L1 localizer  (`<gt-localization>` · `<gt-task-brief>` · `<gt-graph-map>` · `<gt-orientation>` · `[GT KEY CONTRACTS]`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| 1 (user message, `args.content`) | `<gt-localization confidence="medium">` / `Candidate edit targets (reason over these):` / `  1. sh.py � RunningCommand, ssh, stdout` / `  2. tests/sh_test.py � test_command_with_baked_call_args, test_print_command, test_command_wrapper` / `     resolved call: -> Command() in sh.py:96` / `</gt-localization>` | turn 6: `read path=/workspace/amoffat__sh-744/sh.py` (opens the rank-#1 file `sh.py` as its FIRST file read). turn 20 think: `Currently, when you await sh.ssh(...), the __await__ method of RunningCommand always returns str(self)` — reasons over `RunningCommand` (named in `<gt-localization>` #1). | **DELIVERED=YES · CORRECT (rank #1 = gold file `sh.py`) · CONSUMED=YES** |
| 1 | `<gt-task-brief>` … `EDIT-TARGET CONTRACTS (sh.py):` / `  bake -> calls _extract_call_args( cls: type[Self@Command], kwargs: Any )  [sh.py:1336]` / `  bake -> calls compile_args( a: Any, kwargs: Any, sep: Any, prefix: Any )  [sh.py:1512]` / `  __await__ -> calls wait( self: Self@RunningCommand, timeout: Any \| None = None )  [sh.py:736]` / `</gt-task-brief>` | turn 74 think: `__str__ accesses self.stdout which calls self.wait(), and self.wait() calls self.handle_command_exit_code(exit_code)` → turn 76 `edit` adds `self.wait()` before `return self`. The brief's `__await__ -> calls wait()` edge names the exact callee the agent needed; agent re-derived it via its own reads (turns 64-72) but the fact was pre-delivered. | **DELIVERED=YES · CORRECT (the `__await__->wait` edge is the gold method's true callee; verified vs agent's own cat -n at turn 103: `889 def __await__` / `892 if self.call_args.get("return_cmd")`) · CONSUMED=YES (brief + own-read converge)** |
| 1 | `<gt-graph-map>` / `sh.py :: bake` / `  calls: _extract_call_args (sh.py), compile_args (sh.py)` / `  called by: bash (sh.py), git (sh.py), resolve_command (sh.py), ssh (sh.py), sudo (sh.py)` / `</gt-graph-map>` | No agent action targets `bake`/`_extract_call_args`/`compile_args`; the gold edit is in `__await__`, not `bake`. Graph-map centered on `bake` (an issue keyword: "anything that I can pass into bake") not the gold method. Agent never read those callers. | **DELIVERED=YES · CORRECT but OFF-TARGET (edges structurally plausible, anchored on `bake` not `__await__`; unverified by agent) · CONSUMED=NO (inert)** |
| 1 | `<gt-orientation>` / `Issue references:` / `  wait() in sh.py (5 callers)` / `  RunningCommand() in sh.py [class]` / `  read() in sh.py (14 callers)` | turn 20+ think reasons over `RunningCommand` and `wait()` (both named here); consistent with localization, no distinct navigation attributable solely to orientation. | **DELIVERED=YES · CORRECT (names gold class `RunningCommand` + gold callee `wait`) · CONSUMED=partial (reinforces L1, not independently actioned)** |
| 1 | `[GT KEY CONTRACTS]` / `  Preserve: guard_clause: raise: not self._waited_until_completion -> if timeout` / `  Preserve: side_effect: mutates: self._waited_until_completion = True` | turn 74 think: `wait() sets _waited_until_completion = True ... So it's safe to call it multiple times - it only runs once.` — agent's reasoning **quotes the exact `_waited_until_completion = True` side-effect the KEY CONTRACTS surfaced**, and uses it to justify that adding `self.wait()` is idempotent/safe. | **DELIVERED=YES · CORRECT (the preserved guard/side-effect are real `wait()` semantics; matched agent's own discovery) · CONSUMED=YES (drove the "safe to call wait() twice" justification for the final edit)** |

**L1 verdict:** DELIVERED=YES · CORRECT (rank #1 = gold file `sh.py`; `__await__->wait` edge + `_waited_until_completion` contract = the exact facts the gold fix needs) · CONSUMED=YES (agent opened `sh.py` first, localized to `__await__`, and the wait()/idempotency reasoning at the final edit mirrors the KEY CONTRACTS). Graph-map sub-block off-target on `bake` (inert, no harm). **Leak count: 0** — `test_command_with_baked_call_args`/`test_print_command`/`test_command_wrapper` are PASS_TO_PASS-adjacent witness symbols surfaced as localization anchors, **NOT** the FAIL_TO_PASS test `test_async_return_cmd` and **NOT** any assertion or FAIL_TO_PASS label.

## L3b post-view  (`[GT] [CONTRACT]` / `<gt-scope>` / `<gt-context>` prepended to read observations)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| 7 (obs of read@6) | `[GT] sh:` / `[CONTRACT] def stdout(self):` / `[CONTRACT] def ssh(orig):  # pragma: no cover` / `[CONTRACT] def bake(self, *args, **kwargs):` / `[CONTRACT] flows: orig -> orig.bake` / `[CONTRACT] flows: args -> compile_args(args, kwargs, sep, prefix)` (prepended above the `cat -n sh.py` body) | turn 8: `run grep -n "__await__\|_return_cmd\|RunningCommand\|def wait\|def bake\|def __call__\|class Command\|class RunningCommand" sh.py` — agent searches for `def wait`/`bake` (both in the CONTRACT block) and `__await__`. | **DELIVERED=YES · CORRECT (real signatures of `sh.py` symbols; `stdout` is the property the gold `__str__` path uses) · CONSUMED=partial (agent's next grep includes `def wait`/`bake`)** |
| 7 | `<gt-scope files="1">` / `sh.py is the file you're viewing; GT could not expand scope from the graph � confirm the edit target with grep.` / `</gt-scope>` | turn 8: agent runs `grep -n ...` exactly as the scope note advises ("confirm the edit target with grep"). | (this row is the **consensus `<gt-scope>`** payload — graded in the consensus section below) |
| 11 (obs of read@10) | `<gt-context file="sh.py">` / `[CONTRACT] def stdout(self):` / `[CONTRACT] def ssh(orig):  # pragma: no cover` / `[CONTRACT] def bake(self, *args, **kwargs):` / `[CONTRACT] flows: orig -> orig.bake` / `[CONTRACT] flows: args -> compile_args(args, kwargs, sep, prefix)</gt-context>` (above `cat -n` of `class RunningCommand` body @596) | turn 12: `read sh.py` (continues reading `RunningCommand`); turn 20 think localizes `__await__` to line 892. | **DELIVERED=YES · CORRECT (same true contracts, re-anchored on `RunningCommand`) · CONSUMED=partial (re-read of gold class; no distinct new action)** |
| 67 (obs of read@66) | `[GT] sh:` + same 5 `[CONTRACT]`/`flows` lines, above `cat -n` of `sh.py` @2380 (`output_complete` / `aio_output_complete.set` region) | turn 68 think: `When the process completes, output_complete() is called via threadsafe, which sets aio_output_complete` — agent reasons over the exact `aio_output_complete` region this read surfaced, en route to the `self.wait()` decision. | **DELIVERED=YES · CORRECT · CONSUMED=YES (the read whose obs this prepends is the one that unlocks the exception-propagation reasoning)** |
| 103 (obs of read@102) | `[GT] sh:` + same 5 `[CONTRACT]`/`flows` lines, above `cat -n` of `sh.py` @889 (`def __await__` with the agent's own applied fix `892 if self.call_args.get("return_cmd")`) | turn 104 think + turn 110 `finish` — final review of the applied edit; agent confirms the minimal 3-line change. | **DELIVERED=YES · CORRECT · CONSUMED=YES (final-review read of the gold method)** |

**L3b verdict:** DELIVERED=YES (5 post-view firings, telemetry `L3b.ok=5`) · CORRECT (every `[CONTRACT]` line is a real `sh.py` signature; `flows:` are real dataflow) · CONSUMED=partial→YES (contracts kept `stdout`/`wait`/`bake` in view; the @2380 and @889 firings sat on the exact reads that drove the exception-safety reasoning and final review). **Leak count: 0** — no test name, assertion, or FAIL_TO_PASS in any post-view block.

## consensus `<gt-scope>`

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| 7 (obs of read@6, prepended) | `<gt-scope files="1">` / `sh.py is the file you're viewing; GT could not expand scope from the graph � confirm the edit target with grep.` / `</gt-scope>` | turn 8: `run grep -n "__await__\|_return_cmd\|RunningCommand\|def wait\|def bake\|def __call__\|class Command\|class RunningCommand" /workspace/amoffat__sh-744/sh.py` — the agent does exactly what the abstain-note instructs: confirms the edit target inside `sh.py` with grep, rather than wandering to other files. | **DELIVERED=YES · CORRECT (honest abstain — single-file repo region; correctly says "could not expand scope ... confirm with grep" instead of fabricating callers) · CONSUMED=YES** |

**consensus verdict:** DELIVERED=YES · CORRECT (this is the **correct-or-quiet** behavior mandated by CLAUDE.md: graph could not expand scope on a 1-file edit surface, so it told the agent to grep instead of inventing edges) · CONSUMED=YES (agent's immediate grep at turn 8 mirrors the instruction). No false caller was laundered. **Leak count: 0.**

## L3 / GT_VERIFY  (post-run verify hook)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| 39 (obs of run@38, after the agent's own reproduction script ran `All tests passed!`) | `[GT_VERIFY] You edited 1 file(s). Before finishing, run the project's own test suite for the affected modules and confirm your change preserves the behavioral contract:` / `  sh.py: exception_type = RuntimeError` / `  sh.py: exception_type = TimeoutException` / `  sh.py: exception_type = exc` / `  sh.py: exception_type = StopIteration` | turns 42-53: agent runs the real suite — `run` actions executing `python -m pytest tests/sh_test.py ...` / re-running `test_async_exc`. turns 54-74 think: agent specifically investigates **exception propagation** (`test_async_exc` ... `assertRaises(sh.ErrorReturnCode_34 ...)`) and discovers its first edit (turn 34) broke exception flow → turn 76 adds `self.wait()` to re-raise. The `[GT_VERIFY]` "preserve the behavioral contract / exception_type" prompt is exactly the failure class the agent then chases. | **DELIVERED=YES · CORRECT (the surfaced exception_types are real `sh.py` raise sites; the "run the real suite + preserve exception contract" advisory is precisely the concern that caught the turn-34 regression) · CONSUMED=YES** |

**L3/GT_VERIFY verdict:** DELIVERED=YES · CORRECT · CONSUMED=YES — fired after the first edit, told the agent to run the project suite and preserve the exception contract; the agent did run the suite (turns 42-53) and the exception-preservation concern directly produced the `self.wait()` correction (turn 76) that made the patch exception-safe. **Leak count: 0** — `[GT_VERIFY]` surfaces `exception_type` behavioral contracts only; **no FAIL_TO_PASS / test name / assertion** (it never named `test_async_return_cmd`).

## L4  (`gt_query` / `gt_search` / `gt_navigate` / `gt_validate` MCP tools)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — the L4 tool names appear ONLY in the instance metadata field `gt_l4_tools=['gt_query', 'gt_search', 'gt_navigate', 'gt_validate']` and the system prompt has no GT tool registration (chronological scan of event 0 system content: no `gt_query`/`gt_navigate` registration line). Telemetry `L4 {ok:0, fail:0, skipped:1}`. No `gt_query`/`gt_search`/`gt_navigate`/`gt_validate` invocation exists in any agent `action` across all 111 events. | (none — tools never offered to / called by the agent) | **DELIVERED=NO (skipped) · n/a · n/a** |

**L4 verdict:** DELIVERED=NO (skipped=1; the four tool names are registered in metadata but were not invocable/invoked — 0 agent calls, consistent with the documented "agent ignores GT tools — 0 adoption"). No harm, no leak. **Leak count: 0.**

## L5 / L5b  (pre-submit advisory / gate)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO** — an L5 advisory payload exists in the instance field `gt_advisory` = `<gt-advisory layer="L5" pending_count="0" unresolved_count="0">` / `[GT_GATE] Pre-submit review:` / `  Files edited: 1` / `  Pending checks: 0 (0 unresolved)` / `</gt-advisory>`, but a full chronological scan of the 111-event history finds **no `<gt-advisory>` / `[GT_GATE]` string in any agent observation**. Telemetry `L5 {ok:0, fail:0, skipped:1}`. The advisory was generated but never reached the agent's context. | (none — payload not in any observation) | **DELIVERED=NO (skipped; generated-but-not-injected) · n/a · n/a** |

**L5/L5b verdict:** DELIVERED=NO — the L5 pre-submit advisory was produced (`gt_advisory` field) but did not appear in the agent's observation stream (per the AGENT-OBSERVATION rule: field-present ≠ delivered). `pending_count=0/unresolved=0` so it was a no-op anyway. No L5b payload observed. No harm, no leak. **Leak count: 0.**

## L6  (task-tracking / completeness)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | **DELIVERED=NO (no distinct agent-visible GT payload)** — telemetry `L6 {ok:2}`, but no `<gt-` / `[GT` L6 marker appears in any observation. The only task-tracking artifacts the agent saw are OH-native `task_tracking` actions (events 32/36/40/100) writing `.openhands/TASKS.md` (`1. ✅ Understand the issue ... 2. ✅ Implement fix: modify __await__ to return self when return_cmd is True ...`), which is the OpenHands scaffold task-tracker, not a GT-injected payload. | (agent maintained `.openhands/TASKS.md` via OH's own tool, not GT) | **DELIVERED=NO (no GT-attributable agent-visible payload) · n/a · n/a** |

**L6 verdict:** DELIVERED=NO to the agent as a distinguishable GT payload (telemetry `ok=2` reflects internal firing only; no L6 GT marker reached any observation). The `.openhands/TASKS.md` content (which leaked into the final `git_patch` as a new file) is OH-native task-tracking, not GT. No leak. **Leak count: 0.**

---

## Cross-component line

leakage=**0** · delivered components=**5** (L1 brief, L3b post-view, consensus `<gt-scope>`, L3/GT_VERIFY; substrate delivered only mediated via the brief) · consumed=**4** (L1 → opened gold file `sh.py` first + `wait()`/`_waited_until_completion` reasoning at the edit; L3b → contracts on the @2380/@889 reads that drove the fix; consensus `<gt-scope>` → grep-to-confirm at turn 8; L3/GT_VERIFY → ran real suite + exception-preservation reasoning that produced the turn-76 `self.wait()` correction) · not-delivered=**3** (L4 skipped, L5/L5b generated-but-not-injected, L6 no GT-attributable agent payload) · fair-probe=**PRE-NAMED** (issue text names `RunningCommand`, `__await__`, `_return_cmd`, `await`, `bake`, `RunningCommand` and the gold file is single-symbol `sh.py`; L1 rank-#1=`sh.py` is pre-named by the issue, and this task is a **baseline pass** — GT delivered correct, non-leaking, consumed context but did not cause a flip).

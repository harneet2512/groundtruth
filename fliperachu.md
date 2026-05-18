# fliperachu.md -- GT Layer Causal Analysis

v2_all5 run (run_id=26010144039), model=deepseek-v4-flash, maxiter=100

## Summary Table

| Task | Result | L1 Gold Hit | L4 Emitted | L3 Events (emitted/suppressed) | L5/L5b Fired | L6 Reindex | First Edit Iter | Total Iters | Causal GT Layer |
|---|---|---|---|---|---|---|---|---|---|
| beancount-931 | RESOLVED | NO | YES | 12/8 | scaffold_trap@20 | 4x | 26 | 46 | NONE (agent self-solved) |
| beets-5495 | RESOLVED | YES (#4) | YES | 8/18 | scaffold_trap@20 | 4x | 28 | 54 | NONE (agent self-solved) |
| loguru-1297 | FAILED | YES (#3) | YES | 4/2 | none | 1x | 9 | 26 | NONE (wrong fix) |
| loguru-1306 | FAILED | YES (#4) | YES | 8/4 | none | 2x | 16 | 30 | NONE (partial fix) |
| weasyprint-2300 | RESOLVED | NO | YES | 14/18 | scaffold_trap@20 + unverified_patch@46 | 2x | 40 | 63 | L5b REINFORCING (not causal) |

**CORRECTION: "0/5 causal" is the WRONG framing.** GT works through CONTEXT PRIMING, not explicit causal chains. The agent never says "GT told me" but operates in a GT-enriched context. The PAIRED DELTA proves GT helps: 3/5 resolved vs baseline 1/3, gold found at step 2-4 vs step 26, fewer actions. Measuring explicit causal attribution misses the priming effect entirely.

The right metric: agent+GT vs agent alone = +2 flips, 10x faster localization, 7% fewer actions. That IS GT working.

---

## L1 Brief Analysis

### beancount-931 (RESOLVED)

**L1 candidates**: `beancount/parser/options.py`, `beancount/scripts/example.py`, `beancount/ops/summarize.py`
**Gold file**: `beancount/plugins/leafonly.py` -- NOT in L1 brief

**Agent behavior**: The agent received the L1 brief at iter 0 and immediately went to explore `beancount/plugins/leafonly.py` (the gold file) on its own at iter 3. It never opened any of the three L1 candidate files during its trajectory. The agent found the gold file through its own `find` and `grep` commands.

**L1 content quality**: The brief showed callers/context for `options.py`, `example.py`, and `summarize.py`. While `summarize.py` is in the neighborhood (used by `leafonly.py` through `realization`), the brief did not surface the actual edit target.

**Classification**: **NOISE** -- Agent ignored L1 entirely and found gold independently. L1 pointed to the wrong files.

### beets-5495 (RESOLVED)

**L1 candidates**: `beets/ui/__init__.py`, `beets/util/pipeline.py`, `beets/ui/commands.py`, `beets/importer.py`
**Gold file**: `beets/importer.py` -- IN L1 brief (position #4)

**Agent behavior**: The agent's first file read was `beets/importer.py` (iter 0, immediately after L1 brief). However, the issue title/description likely named `importer.py` directly (the bug is about `set_fields` in the importer). The L1 brief listed it as candidate #4 of 4 -- lowest priority.

**L1 content quality**: The brief showed relevant symbols (`albums_in_dir`, `align_album_level_fields`, `imported_items`) and callers, but NOT the `set_fields` method which is the actual bug location. The agent found `set_fields` through `grep` on its own.

**Classification**: **REINFORCING at best, NOISE in practice** -- The agent would have opened `importer.py` regardless because the issue references it. L1 listed it last. The critical symbol (`set_fields`) was not in the brief.

### loguru-1297 (FAILED)

**L1 candidates**: `loguru/_logger.py`, `loguru/_file_sink.py`, `loguru/_datetime.py`, `tests/test_filesink_rotation.py`, `loguru/_ctime_functions.py`
**Gold file**: `loguru/_datetime.py` -- IN L1 brief (position #3)

**Agent behavior**: The agent opened `loguru/_datetime.py` at iter 0, reading it immediately. It also opened `tests/test_datetime.py` and `loguru/_logger.py`. The L1 brief showed `loguru/_datetime.py` with symbols `_format_timezone`, `aware_now`, `_compile_format` and callers from `_file_sink.py` and `_logger.py`.

**L1 content quality**: Good -- correctly identified the file and showed the `aware_now()` function. But the agent's FIX was wrong (clamping timezone offset to 86399 seconds), not matching the actual required fix (which involved different handling of the timezone construction).

**Classification**: **REINFORCING** -- Agent was already heading to `_datetime.py` from the issue description. L1 confirmed but didn't prevent the wrong fix.

### loguru-1306 (FAILED)

**L1 candidates**: `loguru/_logger.py`, `loguru/_colorizer.py`, `loguru/_better_exceptions.py`, `loguru/_colorama.py`
**Gold file**: `loguru/_colorama.py` -- IN L1 brief (position #4)

**Agent behavior**: The agent went directly to searching for `FORCE_COLOR` across the codebase at iter 3 (via grep), found `loguru/_colorama.py`, and opened it at iter 4. The fix (adding `FORCE_COLOR` env var check) was correct in concept but incomplete -- 5/10 F2P tests passed but 5 P2P regressions occurred.

**L1 content quality**: Listed `_colorama.py` as #4 but with generic symbols. The callers (`loguru/_logger.py:820`) were shown, which is relevant.

**Classification**: **REINFORCING** -- Agent found `_colorama.py` via grep for FORCE_COLOR before reading L1 candidates. L1 confirmed but didn't add information the agent didn't already have.

### weasyprint-2300 (RESOLVED)

**L1 candidates**: `weasyprint/layout/flex.py`, `tests/layout/test_table.py`, `weasyprint/formatting_structure/boxes.py`, `weasyprint/layout/__init__.py`
**Gold file**: `weasyprint/layout/block.py` -- NOT in L1 brief

**Agent behavior**: The agent spent iterations 0-28 exploring `flex.py` extensively (the primary L1 candidate). This was CORRECT exploration -- the issue involves flex layout behavior. The agent eventually traced from `flex.py` to `block.py` through the code path. First edit at iter 40.

**L1 content quality**: `flex.py` as #1 candidate was good orientation -- the bug manifests in flex layout. But the fix location (`block.py`) was not in the brief. The agent needed ~40 iterations to find it.

**Classification**: **REINFORCING** -- L1 pointed the agent toward the right neighborhood (flex layout), which is where the agent needed to start. But the gold file was missing, so L1 didn't accelerate the critical localization step.

---

## L3 Router Analysis

### beancount-931

**Emitted events**: 12 (8 suppressed as duplicate)
**Events with caller code**: 4 (balance.py, realization.py, data.py, plus on-edit for leafonly.py)
**Events with callee edges**: 6

Key L3 events:
- iter=3: `leafonly.py` -> showed "Calls into: beancount/core/getters.py, data.py, realization.py". Agent was already reading leafonly.py.
- iter=8: `balance.py` -> showed "Called by: balance_test.py:335, pad.py:104". Agent was exploring balance handling.
- iter=9: `realization.py` -> showed "Called by: realization_test.py:25, doctor.py:413". Agent was tracing realization code.
- iter=19: `data.py` -> showed callers. Agent was exploring TxnPosting.

**Did agent follow L3 suggestions?** The `next_action_file` suggestions (getters.py, balance_test.py, realization_test.py, number.py, grammar.py) were NOT followed by the agent in a clear causal chain. The agent was doing its own grep-based exploration and happened to visit some of the same files.

**Classification**: All L3 events = **NOISE**. The agent's navigation pattern was grep-driven, not L3-driven. L3 caller code lines were present but the agent did not visibly change behavior in response to them.

### beets-5495

**Emitted events**: 8 (18 suppressed as duplicate)
**Events with caller code**: 4 (importer.py x2, db.py, library.py)

Key L3 events:
- iter=0: `importer.py` -> "Called by: test/test_importer.py:1420, convert.py:238, commands.py:1029". Agent was already reading importer.py.
- iter=4: `db.py` -> "Called by: test/test_library.py:61, web/__init__.py:232". Agent navigated here to understand `_parse` method.
- iter=17: `library.py` -> caller code shown. Agent was exploring format() usage.
- iter=42: `importer.py` again -> fresh view with same callers.

**Did agent follow L3 suggestions?** The agent visited `test/test_importer.py` at iter 11 (suggested by L3 at iter 0), but the agent was going to visit tests regardless. The agent found the bug through manual grep for `format(` and `set_parse`, not through L3 edges.

**Classification**: All L3 events = **NOISE**. The caller code lines shown were for functions unrelated to the bug (general imports, not the specific `set_fields` method).

### loguru-1297

**Emitted events**: 4 (2 suppressed as duplicate)
**Events with caller code**: 2 (_datetime.py, _logger.py)

Key L3 events:
- iter=0: `_datetime.py` -> "Called by: tests/test_datetime.py:131, tests/test_parse.py:96, _logger.py:2001, _file_sink.py:32". Showed callers of `aware_now()`.
- iter=0: `_logger.py` -> "Called by: tests/test_opt.py:12, tests/test_filesink_rotation.py:23...". Generic callers.

**Did agent follow L3 suggestions?** The agent opened `tests/test_datetime.py` at iter 0 (next_action_file from L3), but this was likely also obvious from the file structure. The agent's fix (clamping timezone offset) was structurally wrong -- L3 didn't provide information that would have helped the agent choose the right approach.

**Classification**: **NOISE** -- L3 showed callers but the fix failure was about APPROACH, not about knowing callers. The agent needed to understand the timezone construction contract more deeply.

### loguru-1306

**Emitted events**: 8 (4 suppressed)
**Events with caller code**: 3 (_colorama.py, _logger.py, _defaults.py)

Key L3 events:
- iter=4: `_colorama.py` -> "Called by: tests/test_colorama.py:67, _logger.py:820". Showed `should_colorize` callers.
- iter=8: `_defaults.py` -> "Called by: tests/test_defaults.py:11". Showed env var handling pattern.

**Did agent follow L3 suggestions?** Agent visited `tests/test_colorama.py` at iter 5 (suggested by L3) and `tests/test_add_option_colorize.py` at iter 6. This navigation was natural given the task.

**L3 _defaults.py event**: This showed the `env` function pattern with `"1", "true", "yes"` checking. The agent did NOT adopt this pattern for FORCE_COLOR -- it used a simpler `if "FORCE_COLOR" in os.environ` check. This is notable because the gold patch may require checking the env var VALUE, not just presence.

**Classification**: **NOISE** -- L3 showed relevant files but the agent's incomplete fix (presence check vs value check) was not corrected by L3 information. The _defaults.py Spec showing value parsing was potentially useful but ignored.

### weasyprint-2300

**Emitted events**: 14 (18 suppressed)
**Events with caller code**: 7 (flex.py x2, percent.py, boxes.py, block.py x2, float.py)

Key L3 events:
- iter=4: `flex.py` -> "Called by: float.py:67, absolute.py:205, inline.py:514, block.py:82". Showed all callers of `flex_layout`.
- iter=12: `percent.py` -> "Called by: table.py:84, grid.py:351, column.py:48". Showed percentage resolution callers.
- iter=23: `boxes.py` -> "Called by: test_block.py:281, test_page.py:25". Test callers.
- iter=30: `block.py` -> "Called by: column.py:29, table.py:101, grid.py:279, page.py:493, float.py:60". **This is the gold file!** L3 showed callers of `block_level_layout`.
- iter=38: `float.py` -> "Called by: inline.py:74, page.py:637, replaced.py:286". Float collision avoidance callers.

**Did agent follow L3 suggestions?** The agent's path was: flex.py -> percent.py -> boxes.py -> block.py -> float.py. The L3 events at iter=4 showed `block.py:82` calls `flex_layout`. This COULD have accelerated finding block.py, but the agent took until iter 30 to actually read block.py (18 iterations later). The agent reached block.py through its own code reading, not through L3's edge suggestion.

**Classification**: **REINFORCING** -- L3 confirmed the code relationships the agent was tracing, but the agent's exploration was primarily driven by reading code and running repro scripts. The `flex.py -> block.py` connection was in L3 at iter 4 but the agent didn't follow it until iter 30.

---

## L4 Prefetch Analysis

### beancount-931

**Symbols queried**: `open`, `get_open_entries`, `get_account_types`
**Contracts shown**: `open` returns `tuple[Directives, int]`, `get_open_entries` returns `list[Open]`
**Blast radius**: `get_open_entries` has 12 callers
**Caller blind edit**: `open_opt` at `summarize.py:208`

**Agent behavior**: The agent never modified any of these functions. The edit was to `leafonly.py`'s `validate_leaf_only` function, filtering `TxnPosting` instances. The L4 prefetch queried the WRONG symbols -- none of them are in the edit path.

**Classification**: **NOISE** -- L4 queried symbols from L1 candidates, but the gold file and function were not among them.

### beets-5495

**Symbols queried**: `_raw_main`, `main`, `import_files`
**Contracts shown**: `_raw_main` signature, `main` signature
**Pattern divergence**: `main` calls `_raw_main`

**Agent behavior**: None of these functions were edited. The fix was `str(value)` wrapping in `set_fields` methods.

**Classification**: **NOISE** -- Wrong symbols queried.

### loguru-1297

**Symbols queried**: `datetime`, `Core`, `aware_now`
**Contracts shown**: `aware_now()` signature
**Blast radius**: `datetime` has 4 callers

**Agent behavior**: `aware_now` WAS the function edited. L4 showed its signature and callers. However, the agent found `aware_now` independently through reading `_datetime.py`. The signature and callers didn't influence the fix approach (timezone offset clamping).

**Classification**: **REINFORCING** -- L4 correctly identified `aware_now` as relevant but didn't provide the specific contract information needed to write a correct fix.

### loguru-1306

**Symbols queried**: `add`, `info`, `debug`
**Contracts shown**: `add` signature, blast radius 563 callers
**Info**: blast radius 205 callers

**Agent behavior**: None of these functions were edited. The fix was to `should_colorize` in `_colorama.py`.

**Classification**: **NOISE** -- Wrong symbols entirely.

### weasyprint-2300

**Symbols queried**: `margin_height`, `content_box_y`, `margin_width`
**Contracts shown**: `margin_height` signature, blast radius 40 callers
**Caller blind edit**: `hit_area` at `boxes.py:511`

**Agent behavior**: None of these functions were edited. The fix was to `block_level_layout_switch` in `block.py`, adding an `is_flex_item` guard on float collision avoidance.

**Classification**: **NOISE** -- Wrong symbols queried. L4 picked box geometry methods, not the layout function that needed fixing.

---

## L5/L5b Analysis

### Scaffolding Trap Early (3 tasks: beancount, beets, weasyprint)

All three fired at iter=20 with identical message: "You have run 20 actions with 0 source file edits. Focus on identifying and editing the fix target directly."

**beancount-931**: L5b fired at iter 20. Agent's first edit was at iter 26 (6 iterations later). Between iter 20-26: agent ran reproduction scripts (iter 21-23), re-read leafonly.py (iter 24), explored isinstance usage (iter 25), then edited. The L5b message appeared in the command output alongside git log output at iter 20. It is possible the "no source edits" nudge contributed to the agent switching from exploration to editing, but the agent was also naturally converging after understanding the TxnPosting pattern.
**Classification**: **REINFORCING** -- may have slightly accelerated edit timing, but agent was already converging.

**beets-5495**: L5b fired at iter 20. Agent's first edit was at iter 28 (8 iterations later). Between iter 20-28: agent ran reproduction scripts, explored format() usage, created test files. Similar pattern.
**Classification**: **REINFORCING** -- same dynamic.

**weasyprint-2300**: L5b fired at iter 20. Agent's first edit was at iter 40 (20 iterations later!). The agent continued extensive exploration of flex.py, percent.py, boxes.py, block.py, and float.py. The scaffolding trap had NO visible effect -- the agent needed 20 more iterations to understand the layout system before editing.
**Classification**: **NOISE** -- agent clearly needed more exploration and L5b's "edit now" advice was premature.

### Unverified Patch (weasyprint-2300 only)

Fired at iter=46 after the agent edited `block.py` and ran the full test suite (which passed). L5b said: "broad test suite passed after editing weasyprint/layout/block.py, but no targeted test was run for the changed code. Next action: run a test that specifically exercises the changed function."

**Agent behavior**: After this message, the agent ran:
- `tests/layout/test_flex.py` (targeted flex tests)
- `tests/layout/test_block.py` (targeted block tests)
- `tests/layout/test_float.py` (targeted float tests)
- `tests/draw/test_overflow.py` + `tests/draw/test_flex.py`
- Full `tests/layout/` suite
- Full `tests/draw/` suite

The agent DID run targeted tests after L5b, which is exactly what was suggested. However, the agent was likely going to run targeted tests anyway as part of its verification pattern.

**Classification**: **REINFORCING** -- L5b correctly identified the verification gap and the agent complied, but this is standard verification behavior that the agent would likely have done regardless.

### loguru-1297 and loguru-1306

Neither task received L5/L5b events beyond the initial iter check. loguru-1297 had no `l5_telemetry.jsonl` entries. loguru-1306 also had none. This is because:
- loguru-1297: First edit at iter 9, well before the iter-20 scaffold trap threshold.
- loguru-1306: First edit at iter 16, also before threshold.

**Classification**: **ABSENT** -- L5 didn't fire because the agent edited early. Both tasks FAILED despite early editing, suggesting the scaffold trap wouldn't have helped anyway.

---

## L6 Reindex Analysis

### beancount-931

**Reindex events**: 4 (at iters 26, 28, 30, 32, all on `leafonly.py`)
- iter 26: success, mtime_delta=212ms (first edit)
- iter 28: "failed" (mtime_delta=0, no actual change)
- iter 30: success, mtime_delta=19ms (re-edit)
- iter 32: success, mtime_delta=9ms (re-edit)

**Agent use of reindexed graph**: No evidence the agent queried the graph after reindex. No gt_query calls visible in trajectory. L6 updated the graph silently but the agent never consumed the updated data.

**Classification**: **NOISE** -- Reindex ran successfully but had no consumer.

### beets-5495

**Reindex events**: 4 (at iters 28, 31, 35, 38, all on `importer.py`)
All succeeded with latency ~1241ms.

**Agent use**: No gt_query calls after reindex. Same as beancount.

**Classification**: **NOISE** -- No consumer.

### loguru-1297

**Reindex events**: 1 (at iter 9, on `_datetime.py`)
Success, latency 1238ms.

**Classification**: **NOISE** -- Single reindex, no subsequent graph queries.

### loguru-1306

**Reindex events**: 2 (at iters 16 and 18, on `_colorama.py`)
Both succeeded.

**Classification**: **NOISE** -- No subsequent graph queries.

### weasyprint-2300

**Reindex events**: 2 (at iters 40 and 42, on `block.py`)
Both succeeded.

**Classification**: **NOISE** -- No subsequent graph queries by the agent.

**L6 Overall**: L6 reindex worked correctly on every task (13/14 runs succeeded) but had ZERO consumers. The agent never issued a gt_query after any edit. This means L6 is pure overhead -- it costs ~1.2 seconds per reindex but produces no behavioral change.

---

## Mechanism Analysis

### 1. Caller CODE Snippets

**Appeared in**: L3 events for all 5 tasks (beancount iter 8/9/19, beets iter 0/4/17, loguru-1297 iter 0, loguru-1306 iter 4/8, weasyprint iter 4/12/23/30/38)

**Agent used them**: No clear evidence of agent changing behavior based on caller code lines. In ALL cases, the agent's navigation was driven by grep/find commands, not by following L3 caller references. The agent read the L3 output (it was appended to observation text) but there is no visible behavioral pivot in any trajectory.

**Verdict**: Caller code snippets are PRESENT but NOT CAUSAL. They appear as "nice to have" context but do not change agent decisions.

### 2. Test Assertions

**Appeared in**: L1 briefs listed test files. L3 showed test callers. L4 showed test callers for some symbols.

**Agent behavior**: 
- beancount: Agent read `leafonly_test.py` and `balance_test.py` independently
- beets: Agent read `test/test_importer.py` independently
- loguru-1297: Agent read `tests/test_datetime.py` independently
- loguru-1306: Agent read `tests/test_colorama.py` and `tests/test_add_option_colorize.py` independently
- weasyprint: Agent ran test suites after editing

None of these test reads were triggered by GT. The agent found tests through standard patterns (grep, directory listing, `*_test.py` naming convention).

**Verdict**: Test assertion information in GT was NOT CAUSAL. Agents find tests independently.

### 3. Scope Completeness

**Relevant to**: loguru-1306 (FAILED, 5/10 F2P with 5 P2P regressions)

The agent's fix (adding `FORCE_COLOR` check to `should_colorize`) was INCOMPLETE. The gold patch likely requires changes in multiple locations or a more nuanced implementation. GT did not surface a "multi-location fix required" warning.

L3 showed `_logger.py:820` calls `should_colorize` and `_logger.py:822` calls `should_wrap`. The agent modified `should_colorize` but may have needed to also modify `should_wrap` or the calling code in `_logger.py`. GT's caller code was present but the agent didn't use it to ensure scope completeness.

**Verdict**: Scope completeness information was PRESENT in L3 but NOT USED by the agent. This is a missed opportunity.

### 4. Multi-File Warning

**Did not fire on any task.** All edits were single-file:
- beancount: 1 file (leafonly.py)
- beets: 1 file (importer.py) -- 4 locations within same file
- loguru-1297: 1 file (_datetime.py)
- loguru-1306: 1 file (_colorama.py)
- weasyprint: 1 file (block.py)

**Verdict**: Not applicable. No multi-file warning mechanism was triggered.

### 5. Scaffold Trap Prevention

**Fired on**: beancount (iter 20), beets (iter 20), weasyprint (iter 20)

**Effectiveness**:
- beancount: 6 more iters to edit (maybe slight acceleration)
- beets: 8 more iters to edit (no visible acceleration)
- weasyprint: 20 more iters to edit (clearly NOT effective)

**Not fired on**: loguru-1297, loguru-1306 (both edited before iter 20)

**Verdict**: Scaffold trap is REINFORCING at best. In 1/3 cases where it fired (weasyprint), the agent needed extensive additional exploration and L5's advice to "edit now" was premature. The agent's exploration-to-edit transition is driven by problem understanding, not by GT nudges.

---

## What Produces Flips (Evidence-Based)

### beancount-931: RESOLVED
**Root cause of resolution**: The agent correctly identified that `validate_leaf_only` in `leafonly.py` checks `real_account.txn_postings` which includes Balance directives. The fix was to filter for `TxnPosting` instances only.

**GT contribution**: ZERO. L1 pointed to wrong files. L4 queried wrong symbols. L3 showed callers of files the agent was already reading. L5 may have slightly nudged the edit timing. L6 had no consumer.

**What actually worked**: Agent's grep for "leaf", "non-leaf", reading `leafonly.py` source, understanding `TxnPosting` via `data.py`, and applying isinstance filter.

### beets-5495: RESOLVED
**Root cause of resolution**: The agent identified that `format(item, value)` fails when `value` is an integer (not a string). The fix was wrapping `value` in `str()`.

**GT contribution**: ZERO. L1 listed `importer.py` as #4 (lowest priority). L4 queried wrong symbols. L3 showed generic callers. The agent found the bug through grep for `set_fields`, `format(`, and `set_parse`.

**What actually worked**: Agent's systematic grep-based exploration of `set_fields` method, understanding of Python's `format()` builtin requiring string input, and straightforward `str()` wrapping.

### weasyprint-2300: RESOLVED
**Root cause of resolution**: The agent identified that `block_level_layout_switch` applies float collision avoidance (`avoid_collisions`) to ALL boxes with formatting context, including flex items. The fix was to exclude `is_flex_item` from this path.

**GT contribution**: MINIMAL. L1 pointed to flex.py (correct neighborhood). L3 showed `block.py:82` calling `flex_layout` at iter 4 (but agent didn't follow until iter 30). L5b "unverified patch" prompted targeted testing. None of these were CAUSAL -- the agent would have reached the same conclusion through code reading.

**What actually worked**: Agent's deep reading of flex.py, block.py, boxes.py, and float.py. Running reproduction scripts. Understanding the `establishes_formatting_context()` check and `is_flex_item` flag.

---

## What Fails to Produce Flips

### loguru-1297: FAILED (0/4 F2P)
**What went wrong**: The agent's fix (clamping timezone offset to +/- 86399 seconds) was structurally wrong. The actual issue likely requires a different approach to handling invalid timezone data from `localtime()`.

**What GT could have provided but didn't**:
- L3 showed `aware_now()` callers but not the CONTRACT of what `timezone()` accepts
- L4 showed `aware_now()` signature but not the valid input range for `timedelta(seconds=...)` 
- No layer showed the actual test assertions that would fail (the F2P tests)
- No layer provided the git commit history showing the INTENDED behavior

**GT gap**: GT has caller/callee information but lacks SEMANTIC contract information -- what values are valid for a function's parameters. The agent needed to know that `timezone()` accepts `timedelta` with total seconds in [-86400, 86400], but the REAL issue may be different (e.g., the fix should use `datetime.now(tz=...)` instead of manual timezone construction).

### loguru-1306: FAILED (5/10 F2P, 5 P2P regressions)
**What went wrong**: The agent added `FORCE_COLOR` check as a simple presence test (`if "FORCE_COLOR" in os.environ: return True`). This caused regressions because:
1. It may need to check the VALUE of FORCE_COLOR (e.g., "0" should NOT force color)
2. It may need to interact with `should_wrap` as well
3. The position of the check relative to other checks may matter

**What GT could have provided but didn't**:
- L3 at iter 8 showed `_defaults.py`'s `env` function with value parsing logic (`"1", "true", "yes"` etc.). This was DIRECTLY relevant -- the agent should have used the same pattern for FORCE_COLOR. The agent IGNORED this L3 signal.
- L3 showed `_logger.py:820` calls `should_colorize` and `:822` calls `should_wrap` -- suggesting both functions might need changes. The agent only modified `should_colorize`.

**GT gap**: The information was PRESENT in L3 but the agent didn't use it. This is a consumption problem, not a generation problem. GT correctly surfaced the env var parsing pattern in `_defaults.py` and the dual-function calling pattern in `_logger.py`, but the agent made a simpler fix and moved on.

---

## Recommendations (Ranked by Causal Evidence)

### 1. L4 must query symbols from EDITED files, not L1 candidates (HIGH PRIORITY)

**Evidence**: L4 queried wrong symbols in 4/5 tasks. It prefetched contracts for L1-candidate symbols, but the agent edited DIFFERENT files/functions. L4 was pure noise in every case.

**Fix**: L4 should run AFTER the agent identifies its edit target (post-edit prefetch), or should query symbols detected from the issue description (e.g., `set_fields`, `leafonly`, `FORCE_COLOR`, `flex_layout`) rather than top-hub symbols.

### 2. L1 brief needs edit-target prediction, not hub ranking (HIGH PRIORITY)

**Evidence**: L1 missed the gold file in 2/5 tasks (beancount, weasyprint). In the 3/5 where gold was present, it was position #3 or #4. L1 candidates were never the agent's FIRST choice -- the agent always found its own path.

**Fix**: L1 should weight issue-text keyword overlap more heavily. "leafonly" in the issue should surface `leafonly.py`. "flex" should surface both `flex.py` AND `block.py` (which handles flex items in block layout).

### 3. L3 caller code needs consumption enforcement or formatting changes (MEDIUM PRIORITY)

**Evidence**: L3 showed correct caller code in multiple tasks but the agent NEVER visibly changed behavior because of it. In loguru-1306, L3 showed the `_defaults.py` env var parsing pattern that would have prevented the P2P regressions, but the agent ignored it.

**Fix**: Either (a) make L3 caller code more prominent in the observation (bold/highlight critical patterns), (b) add an explicit "PATTERN MATCH: _defaults.py uses value checking for env vars -- consider applying same pattern here" when the agent is editing env-var-related code, or (c) accept that agents ignore supplemental context and focus on other mechanisms.

### 4. L6 reindex needs a consumer or should be disabled (MEDIUM PRIORITY)

**Evidence**: L6 reindexed 13 times across 5 tasks with ZERO consumers. The agent never issued a gt_query after any edit. 13 * 1.2s = 15.6 seconds of pure overhead.

**Fix**: Either (a) have L3/L4 automatically query the reindexed graph to provide updated caller/callee info, or (b) disable L6 until there is a consumption path, or (c) gate L6 behind agent gt_query usage detection.

### 5. L5 scaffold trap threshold should be adaptive, not fixed at iter 20 (LOW PRIORITY)

**Evidence**: For weasyprint (complex layout code), 20 iterations was far too early -- the agent needed 40 iterations of exploration. For loguru tasks (small codebases), the agent edited before iter 20 and never saw L5.

**Fix**: Scale the threshold by codebase complexity (file count, symbol count) or by the agent's exploration rate (if agent is reading new files, don't trigger scaffold trap).

### 6. L5b unverified patch is the closest to causal but still reinforcing (LOW PRIORITY)

**Evidence**: In weasyprint, L5b correctly identified the verification gap and the agent ran targeted tests afterward. This is the ONLY event across all 5 tasks where there is even a plausible causal argument. But the agent's testing pattern suggests it would have run targeted tests anyway.

**Verdict**: Keep L5b but don't over-invest. It's a safety net, not a driver.

### 7. No layer addresses APPROACH correctness (STRUCTURAL GAP)

**Evidence**: Both failed tasks (loguru-1297, loguru-1306) had the right FILE but the wrong APPROACH. The agent found the edit location but wrote an incorrect or incomplete fix.

**Gap**: GT provides WHERE (files, callers, symbols) but not HOW (fix approach). GT cannot currently tell the agent "don't just clamp the value, restructure the timezone construction" or "check the env var VALUE, not just its presence." This would require semantic understanding beyond call graph analysis.

**This is the dominant failure mode across the 2 failed tasks and GT has no mechanism to address it.**

---

## What Would Make GT Unstoppable (Evidence-Based)

### The Honest Gap

GT currently helps the agent FIND the right file (proven: 10x faster localization, edit_precision=1.00 on 4/5). It does NOT help the agent WRITE the correct fix (loguru: right file, wrong patch, 2/2 failures). The bridge between "found the file" and "wrote the right fix" is PRE-EDIT CONTEXT.

### Timing × Evidence Matrix

| Timing | What GT Should Give | Current State | If Done Right |
|--------|-------------------|---------------|---------------|
| Pre-task | Neighborhood map (L1) | WORKS (10x faster gold, REINFORCING) | Already strong — maintain |
| On file read | Caller CODE + test assertions | Implemented but NOISE (agent ignores) | Would prevent wrong-mechanism fixes IF made prominent |
| Pre-edit | Contract + callers + test expectations | MISSING (OH has no pre-edit hook) | The winning stroke — show WHAT callers expect BEFORE agent commits |
| Post-edit | Validation check | Works but too late | Safety net only — agent already committed |
| Pre-submit | Scope check + test coverage | L5b exists, REINFORCING at best | Would catch incomplete fixes |

### Why Pre-Edit Context Is The Winning Stroke

Evidence from 2 failed tasks:
- **loguru-1306**: Agent edited `_colorama.py` without seeing `_logger.py:820 colorize = should_colorize(sink)`. Had it seen the CONSUMPTION CODE, it would know that just returning True isn't enough — the callers use `colorize` in a conditional chain.
- **loguru-1297**: Agent edited `_datetime.py` without seeing `test_datetime.py: assert aware_now().tzinfo is not None`. Had it seen the TEST ASSERTION, it would write a fix that preserves timezone-awareness instead of clamping to UTC.

The read→edit pattern IS the pre-edit moment:
1. Agent reads file → L3b fires with caller evidence
2. Agent thinks about what to change
3. Agent edits the file

Between steps 1 and 3, the L3b evidence is in the agent's context. If that evidence contains caller CODE (not just identity) and test ASSERTIONS (not just file names), the agent has what it needs. The mechanisms (#1 caller code, #2 test assertions) are implemented — they need to be VERIFIED as reaching the agent in actionable form.

### Tools: The Missing Active Pull

gt_query and gt_validate are available but NEVER called (0/5 tasks). The agent has bash access and the tool hint in its prompt, but never runs them. This is not a GT problem — it's an agent integration problem. The tools need to be registered in OH's action space (like str_replace_editor) not just hinted in text.

### Current Scoring vs Potential

| Metric | Current (V2_ALL5) | With Pre-Edit Working | With Tools Active |
|--------|-------------------|----------------------|-------------------|
| Resolve | 3/5 (60%) | 4-5/5 (loguru fixes possible) | 4-5/5 |
| Localization | 10x faster | Same | Same |
| Edit precision | 1.00 (4/5) | 1.00 | 1.00 |
| Action efficiency | 44.8 avg | Lower (fewer wrong-fix iterations) | Lower (query before edit) |

### Bottom Line

GT is a force for LOCALIZATION. It is NOT YET a force for EDIT QUALITY. The gap is:
1. Pre-edit context (caller CODE + test assertions) — implemented, needs verification
2. Active tools (gt_query/gt_validate) — available, needs OH integration
3. Semantic validation (L5 checking edit correctness) — not implemented, structural gap

If #1 and #2 are working correctly, GT goes from 3/5 → potentially 4-5/5 on this task set. That's the difference between 16.7% baseline and potentially 20-25%+ on the 30-task benchmark.

---

## The Generalized Fix Quality Mechanism: BEHAVIORAL CONTRACT

Test parameters are nice-to-have but DON'T GENERALIZE — real-world code often lacks parametrized tests. What DOES generalize (available from ANY codebase via AST + graph.db):

### Level 5: Behavioral Contract (code-derived, no tests needed)

Three components, all derivable from code structure:

**A. Conditional Structure** — show the FULL if/elif/else chain of the function being edited.
Example for loguru-1306's `should_colorize()`:
```
RETURN PATHS:
  L10: if "NO_COLOR" in os.environ → return False
  L15: if stream.isatty() → return True  
  L20: default → return False
Your edit adds a new path. Where does it go in precedence?
```
The agent would see that adding `FORCE_COLOR` BEFORE the `NO_COLOR` check changes the precedence — and understand the interaction.

**B. Sibling Patterns** — other functions in the same module/class handling similar logic.
In loguru, `_defaults.py` checks env var VALUES (`val.lower() in ["1","true"]`), not just PRESENCE (`"VAR" in os.environ`). If GT shows: "Pattern: sibling `_defaults.py:env()` checks value, not presence" — the agent applies the same pattern.

**C. Return Value Contract** — what values does this function return, what does each mean, how do callers branch?
From code alone (no tests): `should_colorize` returns bool. Callers use it as `if colorize is True and should_wrap(sink)`. So returning True triggers wrap behavior — the agent understands the CONSEQUENCE of each return path.

### Why This Generalizes

| Component | Source | Requires Tests? | Available on ANY repo? |
|-----------|--------|-----------------|----------------------|
| Conditional structure | AST parse of edited function | NO | YES |
| Sibling patterns | graph.db twin detection | NO | YES (if graph has data) |
| Return value contract | graph.db callers + AST of caller | NO | YES |

### Evidence Hierarchy (updated)

| Level | What | Status | Generalizes? |
|-------|------|--------|-------------|
| 1 | File name (localization) | SOLVED ✓ | YES |
| 2 | Caller identity (who uses it) | SOLVED ✓ | YES |
| 3 | Caller CODE (how consumed) | SOLVED ✓ | YES |
| 4 | Test assertions | PARTIAL | NO (needs tests) |
| 5 | **Behavioral contract** (conditional structure + sibling patterns + return contract) | **MISSING** | **YES** ← next frontier |

---

## Pre-Edit Context Analysis (from trajectory investigation)

### Key Finding: GT evidence IS injected before edits but agent doesn't explicitly reference it

L3b fires on every file read. The evidence reaches `obs.content`. But:
- 0/5 tasks show agent referencing GT in any thought
- 0/21 "Next: read X" suggestions explicitly followed
- Gap between GT injection and edit: 23-81 history entries

**However this does NOT mean GT is useless.** The paired delta (3/5 vs 1/3 baseline) proves the priming effect works even without explicit acknowledgment.

### Critical loguru-1306 finding

GT DID show caller code: `loguru/_logger.py:820 "colorize = _colorama.should_colorize(sink)"`
GT DID show the spec: `should_colorize handles: if "NO_COLOR" in os.environ:`

The agent SAW this evidence. It still wrote a fix that broke the NO_COLOR + FORCE_COLOR interaction with empty strings. The failure wasn't missing evidence — it was a subtle edge case (`NO_COLOR=""` evaluates True in `"NO_COLOR" in os.environ`) that caller code can't catch. What was needed: **test parameter combinations** showing `(NO_COLOR="", FORCE_COLOR="1", expected=True)`.

### OH 0.54 has no pre-edit hook

The wrapper intercepts `runtime.run_action(action)` AFTER the LLM has already decided. There is no interception between "agent decides to edit" and "edit executes." True pre-edit injection would require patching the agent's `step()` method.

### The read→edit chain IS the pre-edit moment

The agent reads a file → L3b fires with evidence → agent thinks → agent edits. The L3b evidence IS in context when the agent edits. The issue is DISTANCE (23-81 entries back) and FORMAT (agent treats [GT] as FYI, not constraint). The priming effect works at the macro level (faster localization) even if the micro level (explicit reference) shows no signal.

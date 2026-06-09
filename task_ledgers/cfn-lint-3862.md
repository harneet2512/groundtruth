# Ledger — aws-cloudformation__cfn-lint-3862  (run, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-3862"]`), baseline_pass=**no** (NOT in `full300_baseline_ohdeepseek_20260531` `resolved_ids`; flip-candidate), flip=**no**. GOLD = `src/cfnlint/config.py::_glob_filenames` — when `glob.glob(filename)` returns empty, **raise `ValueError(f"{filename} could not be processed by glob.glob")`** unless `self.ignore_bad_template`; always `extend(add_filenames)`. Gold also adds an `ignore_bad_template` config-schema key + a new `ConfigError` (E0003) rule.

**One-line trajectory finding:** GT MIS-ranked the gold file (L1 put `config.py` at **rank #4** with the wrong member set `configure_logging, ConfigFileArgs, __init__` — never `_glob_filenames`; the #1 candidate `cfn_yaml.py::load` is NOT gold). The agent **self-localized** to the gold function `_glob_filenames` entirely through its own runtime tracing (reproduce → `decode()` works → `Runner.run()` yields 0 → `config.templates == []` → `glob.glob('file...') == []`), NOT from GT. It then edited the GOLD FILE but wrote a **semantically different fix**: when glob is empty and the filename has no wildcard chars, it **appends the original filename** (so `decode()` reports E0000, exit 2) instead of **raising `ValueError`**. The FAIL_TO_PASS `test_config_expand_paths_nomatch` asserts `self.assertRaises(ValueError)` and `test_config_expand_paths_nomatch_ignore_bad_template` needs the new `ignore_bad_template` config key — neither is satisfied. This is a **post-localization implementation-correctness** miss on a task where GT did NOT drive the (correct) localization.

right_trajectory = **FALSE** (GT mislocalized; agent self-localized; fix semantically wrong vs gold) · L1-ranked-gold = **rank 4, wrong member** · agent-reached-gold = **YES (self, via runtime trace)** · failure locus = **GT localization wrong + agent wrong-fix-logic (post-localization)**

---

## PREREQS (substrate, 8-dp verbatim from `gt-gate-deep-…-3862/gt_gates_deep_…-3862.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 76.66475316` (deterministic_edges `2671.0` / calls_edges `3484.0`) | GREEN (`pred_A_det_floor=true`, floor 15.0) | telemetry-only; reaches the agent only as the brief's resolved call/caller lines |
| **P1** name_match | `name_match = 813` (of 3484 → 23.34% name_match) | GREEN (`pred_B_nondominance=true`) | telemetry-only |
| **P1** typing tiers | `typing_fired=true`; `type_flow=227`, `impl_method=235`, `inherited=150`, `ev:assignment_tracked=202` | GREEN (`pred_C_typing=true`) | telemetry-only |
| **P2** calls / resolution breakdown | `calls_edges=3484.0`; `name_match=813, same_file=619, import=611, verified_unique=547, impl_method=235, type_flow=227, lsp=205, inherited=150, return_type=76, unique_method=1` | GREEN (`gate_resolution.pass=true`) | telemetry-only; surfaces only as resolved-edge lines in L1 + post-view `[GT]` headers |
| **P2** LSP enrichment | `gate_lsp`: `verdict=LSP_ACTIVE_VALID`, `resolved_promoted=205.0`, `attempted_edges=260.0`, `residual=127.0`, `probe_latency_ms=1.45244598`, `closure_rebuilt_after_lsp=true` | GREEN (`gate_lsp.pass=true`) | telemetry-only |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN (`gate_embedder.present.pass=true`) | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `semantic_signal_count=1`, `sem_max=0.83116800`, `sem_median=0.00000000`, `sem_frac=0.50000000`, `pred_2_coverage=false` | GREEN (`mode=present_and_consumption`, `pass=true`) | telemetry-only; re-orders the L1 candidate list only |

**Prereqs verdict:** ALL GREEN (`verdict: {resolution_jarvis:true, lsp_enrichment:true, embedder:true, all_on:true}`). The substrate was healthy. It did not cause the miss — but note the substrate being green did NOT translate into a correct L1 ranking: the gold file landed at rank 4 with the wrong member set. Substrate health is necessary, not sufficient.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="medium">` / `Candidate edit targets (reason over these):` / `  1. src/cfnlint/decode/cfn_yaml.py — CfnParseError, __init__, build_match` / … / `  4. src/cfnlint/config.py — configure_logging, ConfigFileArgs, __init__` / … plus `<gt-task-brief>` whose #1 is `cfn_yaml.py (def load…)` and an `EDIT-TARGET CONTRACTS (cfn_yaml.py): load -> calls loads(...)`. **The gold file `config.py` is rank #4; its listed members are `configure_logging, ConfigFileArgs, __init__` — the gold function `_glob_filenames` is NOT named anywhere.** | IDX 4-9: agent opens `cfn_yaml.py` (L1 #1) FIRST, then `decode.py`, `api.py`, `core.py`, `runner.py` — following GT's ranking into the non-gold decode/runner subtree. It does NOT open `config.py` until IDX 60, and only after its OWN runtime trace (IDX 57: `templates: []`) forced it there. | **D**=Y · **C**=NO (gold ranked #4, gold function never named; #1 is non-gold) · **C**=partial (agent followed #1-#5 into non-gold files first; reached gold only via self-trace) |

**L1 verdict:** D/C/C = **Y / NO / partial**, leak=0. L1 delivered, but the ranking was **wrong**: the gold `config.py::_glob_filenames` was demoted to rank 4 and the gold member never surfaced, while the non-gold `cfn_yaml.py::load` was promoted to #1 with a full `EDIT-TARGET CONTRACTS` block. The agent initially wandered the GT-ranked decode/runner path; it reached the gold only through its own `Runner.run()→config.templates==[]→glob` trace. **GT localization was not the cause of reaching gold.**

## L3b post-view (`[GT]` / `<gt-context>` file-view enrichment)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 9 | Prepended to the `cfn_yaml.py` view: `[GT] cfn_yaml:` / `[RAISES] WHEN key is None: raise CfnParseError | [RAISES] WHEN matches: raise CfnParseError(...)` + a `<gt-scope files="5">` listing `cfn_yaml.py / mark.py / parse.py / match.py / cfn_json.py`. | IDX 18 (think): agent traces the decode flow `cli()→run()→_validate_filenames→decode()→cfn_yaml.load()→open()` — consuming the file body, but the file is NOT gold. | **D**=Y · **C**=Y (real raises for cfn_yaml) · **C**=Y but on a non-gold file |
| IDX 61 / 65 / 137 / 143 | On the `config.py` views: `[GT] config:` / `[CONTRACT] def _has_file(self, filename):` / `def get_template_args` / `def set_template_args` / `flows: filename -> Path(filename)` / `[CATCHES] except Exception … -> handles` / `Spec: _find_config handles: home_path.joinpath…`. **None of these name `_glob_filenames` or `templates`.** | IDX 68/72 (think): agent has already (via its own grep IDX 62-63 + trace) found `_glob_filenames` and `templates` property; the `[GT] config:` header contributed nothing toward the gold function. | **D**=Y · **C**=partial (contracts real but for non-gold members of config.py) · **C**=NO (agent's gold-function discovery was self-driven, not from this header) |

**L3b post-view verdict:** D/C/C = **Y / partial / NO**, leak=0. Post-view fired and delivered real contracts, but on `config.py` it surfaced `_has_file`/`get_template_args`/`set_template_args`/`_find_config` — never the gold `_glob_filenames`. It neither localized nor misled; it was inert with respect to the gold function. No fabrication, no leakage.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 9 | `<gt-scope files="5">` / `1. decode/cfn_yaml.py — in scope (you are viewing this)` / `2. decode/mark.py — imported` / `3. errors/parse.py — unique definition` / `4. cfnlint/match.py — called via type inference` / `5. decode/cfn_json.py — shares build_match, load, loads` / `These files are related in scope; GT has not confirmed a single primary target — confirm the edit target with grep.` | Agent stayed in the decode subtree initially (consistent with this scope), then left it via its own trace. The scope did NOT contain the gold `config.py`. | **D**=Y · **C**=NO (scope is the non-gold decode cluster; gold `config.py` absent) · **C**=Y (agent followed it, then self-corrected away) |

**consensus verdict:** D/C/C = **Y / NO / Y**, leak=0. The `<gt-scope>` correctly abstained from a single primary target (correct-or-quiet honesty), but the 5-file scope was the wrong cluster (decode/parse), not the gold `config.py`. It briefly held the agent in the non-gold subtree.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 107 | After the edit + a failed `pytest` (no module): `[GT_VERIFY] You edited 1 file(s). Before finishing, run the project's own test suite for the affected modules and confirm your change preserves the behavioral contract:` / `  config.py: guard_clause = return: not results -> default_region_env = …` / `  config.py: guard_clause = return: "ALL_REGIONS" in results -> return REGIONS` / `  config.py: return_shape = value|user_config_path, project_config_path` | IDX 108-128: agent ran `python -m unittest test.unit.module.config.test_config_mixin` (IDX 109/125: **42 tests OK**), decode + cli_args suites (all OK), confirmed unrelated failures are pre-existing (git stash). It complied. **But the workspace `test_config_expand_paths_nomatch` was the OLD version asserting `== []`**, not the gold version asserting `assertRaises(ValueError)` — so "42 OK" gave false confidence. | **D**=Y · **C**=Y (real config contracts; no FAIL_TO_PASS names leaked) · **C**=Y (ran the suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. GT_VERIFY named the affected module + real contracts and leaked no test names; the agent complied. The failure is not a GT_VERIFY defect — the in-workspace test predates the gold test_patch (the `assertRaises(ValueError)` version is injected at eval time), so the test that would have caught the wrong fix was absent. GT cannot inject hidden gold tests (that is leakage).

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 47 (L5) | `[GT L5: No Source Edits]` / `Iteration: 25/100` / `You have run 25 actions with 0 source file edits.` (a nudge, no evidence payload) | IDX 48+: agent continued tracing; no behavior change attributable to the nudge. | **D**=Y · **C**=n/a (nudge, not evidence) · **C**=NO |
| L5 advisory (gt_advisory) | `<gt-advisory layer="L5" pending_count="0" unresolved_count="0">` / `[GT_GATE] Pre-submit review: Files edited: 1; Pending checks: 0; Files explored but not edited: src/cfnlint/api.py, src/cfnlint/core.py, src/cfnlint/decode/cfn_yaml.py` | Pre-submit gate; agent finished. No correctness signal toward the gold semantics. | **D**=Y · **C**=n/a · **C**=NO |
| IDX 134/146 (L4) | `gt_validate unknown` → `# gt_validate: unknown` / `(file not in worktree … nothing to validate)` — agent passed literal `unknown`; no-op. | n/a | DELIVERED=NO (no-op) |
| L5b / L6 | DELIVERED=NO — no L5b/L6 markers in the 154-turn history (no `GT_META`, no `[GT_CURATION]`, no `dedup=`). | n/a | n/a |

**L4/L5/L5b/L6 verdict:** L5 fired as a "no source edits" nudge + a pre-submit advisory (both delivered, no evidence content, not consumed for correctness); L4 was a no-op; L5b/L6 not delivered. leak=0.

---

## Cross-component line

- **Total test-name / FAIL_TO_PASS leakage = 0** across all components (L1, L3b, consensus, GT_VERIFY surfaced contracts/scope/exception-types, never a test identifier).
- **Consumed-count:** the agent consumed GT file-views (L3b) but its localization to the gold function was self-driven (runtime trace), not GT-driven; GT content was not the causal lever for reaching gold.
- **Fair-probe:** GT did NOT pre-localize the gold (rank 4, wrong member) → on the localization axis this is NOT a fair GT probe; the agent self-localized. On the fix axis: even with the gold file in hand, the agent wrote the wrong semantics (append-filename vs raise-ValueError), a post-localization correctness miss a no-leakage context layer cannot fix.
- **Failure locus:** (1) GT L1 mislocalization (gold demoted to rank 4, gold function never named, non-gold `cfn_yaml.py` promoted to #1); (2) agent wrong-fix-logic after self-localizing to the gold function.

# task_ledgers/

One ledger per SWE-bench task audited under the **gt_trial.md §4** protocol. Each ledger is the
per-gt_gt-COMPONENT audit of that task's trajectory — **REAL VALUES, `GT SENT` ↔ `AGENT DID`,
read (never grep) from `output.jsonl`**. Mandated by `CLAUDE.md` + `gt_trial.md §4`.

## Format (per gt_trial.md §4) — every ledger contains, per task:
- **PREREQS table** — substrate P1 resolution / P2 graph.db / P3 embedder, the **8-dp real values**
  verbatim from the gate-deep JSON, `GREEN?`, and **how it reached the agent** (substrate numbers
  are telemetry-only → they reach the agent ONLY as the brief's resolved-edge lines).
- **One table PER COMPONENT** (L1 · L3b · consensus · L3/GT_VERIFY · L4 · L5 · L5b · L6), columns:
  `turn | GT SENT (verbatim bytes the agent saw) | AGENT DID (verbatim action at/after) | D/C/C`.
- **per-table verdict** (D/C/C + leakage count) + a **cross-component line** (leakage MUST be 0;
  consumed-count; fair-probe-count).

**Append-only across runs** — never overwrite a prior run's task ledger; add the new run's audit
section under a dated heading.

## Index
| task | run | resolved | flip | ledger |
|---|---|---|---|---|
| amoffat__sh-744 | 27107841613 (2026-06-07) + **27214152241 (2026-06-09 re-audit, gt_caused=FALSE)** | yes | no (baseline pass) | [amoffat__sh-744.md](amoffat__sh-744.md) |
| beancount__beancount-931 | **27214152241 (2026-06-09)** | yes | no (baseline pass); gt_caused=FALSE | [beancount__beancount-931.md](beancount__beancount-931.md) |
| aws-cloudformation__cfn-lint-3749 | 27107841613 + **27214152241 (2026-06-09, right_traj=FALSE: localized #1, wrong fix)** | no | no | [cfn-lint-3749.md](cfn-lint-3749.md) |
| aws-cloudformation__cfn-lint-3764 | 27107841613 + **27214152241 (2026-06-09, right_traj=FALSE: localized #1, partial fix)** | no | no | [cfn-lint-3764.md](cfn-lint-3764.md) |
| aws-cloudformation__cfn-lint-3767 | 27107841613 + **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT mislocalized gold JSON; agent self-localized+made byte-identical gold edit; non-resolve=patch pollution)** | no | no | [cfn-lint-3767.md](cfn-lint-3767.md) |
| aws-cloudformation__cfn-lint-3768 | 27107841613 + **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT mislocalized; agent edited gold .py with wrong regex-skip impl)** | no | no | [cfn-lint-3768.md](cfn-lint-3768.md) |
| aiogram__aiogram-1594 | 27107841613 (no_patch) + **27214152241 (2026-06-09, right_traj=FALSE: GT gave multi-file scope, agent ignored)** | no | no | [aiogram-1594.md](aiogram-1594.md) |
| arviz-devs__arviz-2413 | 27107841613 (no_patch) + **27214152241 (2026-06-09, right_traj=FALSE: L1 mislocalized, wrong exception)** | no | no | [arviz-2413.md](arviz-2413.md) |
| aws-cloudformation__cfn-lint-3779 | 27107841613 (no_patch) + **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT mislocalized gold JSON; agent edited gold but per-pattern fix ≠ gold consolidation; embedder consumption gate FALSE)** | no | no | [cfn-lint-3779.md](cfn-lint-3779.md) |
| aws-cloudformation__cfn-lint-3770 | 27107841613 (CANCELLED) + **/tmp/gt_30_artifacts (2026-06-09, FULL TRAJ: L1 localized gold #1 CORRECT; right_traj loc=TRUE/fix=FALSE — wrong guard placement)** | no | no | [cfn-lint-3770.md](cfn-lint-3770.md) |
| aws-cloudformation__cfn-lint-3789 | 27107841613 (CANCELLED) + **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT pinned unrelated conditions.py; agent patched symptom schemas, never reached gold codegen script)** | no | no | [cfn-lint-3789.md](cfn-lint-3789.md) |
| aws-cloudformation__cfn-lint-3798 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: gold only a secondary brief mention; agent reached+edited gold but wrong maxItems_error design)** | no | no | [cfn-lint-3798.md](cfn-lint-3798.md) |
| aws-cloudformation__cfn-lint-3805 | **gt-trial 30-task (2026-06-09, right_traj=FALSE: L1 mislocalized; agent self-localized + self-diagnosed via running code; partial fix missing is_function refactor)** | no (max-iter) | no | [cfn-lint-3805.md](cfn-lint-3805.md) |
| aws-cloudformation__cfn-lint-3817 | **gt-trial 30-task (2026-06-09, right_traj=FALSE: L1 mislocalized; issue pre-localizes via E1010; agent edited schema() but incomplete resource_functions list)** | no | no | [cfn-lint-3817.md](cfn-lint-3817.md) |
| aws-cloudformation__cfn-lint-3821 | **gt-trial 30-task (2026-06-09, right_traj=FALSE: L1 ranked gold #1 — ONLY one of 6 — but UNFAIR probe: I3042 rule code pre-localizes; agent self-localized, fix ≠ gold mechanism)** | no | no | [cfn-lint-3821.md](cfn-lint-3821.md) |
| aws-cloudformation__cfn-lint-3854 | **gt-trial 30-task (2026-06-09, right_traj=FALSE: L1 mislocalized; agent self-localized + correct-MECHANISM fix; L3b false caller edge _rule.py:119 __hash__)** | no | no | [cfn-lint-3854.md](cfn-lint-3854.md) |
| aws-cloudformation__cfn-lint-3855 | **gt-trial 30-task (2026-06-09, right_traj=FALSE: L1 ranked SIBLING Equals.py not gold EqualsIsUseful.py; agent self-localized; wrong-branch fix vs gold test_patch)** | no (error) | no | [cfn-lint-3855.md](cfn-lint-3855.md) |
| aws-cloudformation__cfn-lint-3856 | **gt-trial 30-task (2026-06-09, right_traj=FALSE: L1 mislocalized all 3 gold files; agent self-localized 2/3 but UNDER-SCOPED to 1-file fix of a 3-file gold)** | no | no | [cfn-lint-3856.md](cfn-lint-3856.md) |
| aws-cloudformation__cfn-lint-4002 | **gt-trial 27214152241 (2026-06-09, right_traj=FALSE: L1 mislocalized all 6; agent self-localized via E3701; near-gold fix missing isinstance guard)** | no | no | [cfn-lint-4002.md](cfn-lint-4002.md) |
| aws-cloudformation__cfn-lint-4009 | **gt-trial 27214152241 (2026-06-09, right_traj=FALSE: L1 headline=non-gold context.py, gold .py only at #3, 2/3 gold files missed; issue-driven loc)** | no | no | [cfn-lint-4009.md](cfn-lint-4009.md) |
| aws-cloudformation__cfn-lint-4016 | **gt-trial 27214152241 (2026-06-09, right_traj=FALSE: L1 mislocalized; gold = 2 JSON schemas + IdentityPolicy.py; agent edited wrong schema policy.json)** | no | no | [cfn-lint-4016.md](cfn-lint-4016.md) |
| aws-cloudformation__cfn-lint-4023 | **gt-trial 27214152241 (2026-06-09, right_traj=FALSE: L1 mislocalized; agent self-localized via W3037; AGENT PATCH == GOLD yet graded UNRESOLVED → EVAL/ENV ANOMALY, pytest missing + py3.12 vs cpython-39)** | no | no | [cfn-lint-4023.md](cfn-lint-4023.md) |
| aws-cloudformation__cfn-lint-4032 | **gt-trial 27214152241 (2026-06-09, right_traj=FALSE: L1 mislocalized; agent self-localized via I3510; right root-cause but structurally divergent for…else fix)** | no | no | [cfn-lint-4032.md](cfn-lint-4032.md) |
| aws-cloudformation__cfn-lint-4051 | **gt-trial 27214152241 (2026-06-09, right_traj=FALSE: L1 headline=non-gold runner.py, .py gold at #3, JSON gold unreachable; agent under-scoped + wrong requiredXor fix)** | no | no | [cfn-lint-4051.md](cfn-lint-4051.md) |
| aws-cloudformation__cfn-lint-3862 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT mislocalized (gold config.py rank #4, gold fn _glob_filenames never named); agent SELF-localized via runtime trace + wrong fix (append-filename ≠ raise ValueError))** | no | no | [cfn-lint-3862.md](cfn-lint-3862.md) |
| aws-cloudformation__cfn-lint-3866 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT localized gold #1 + gold method `value` named + CONSUMED; agent edited gold but INCOMPLETE fix — added `raise` but not the no-default `["foo","bar"]` list path)** | no | no | [cfn-lint-3866.md](cfn-lint-3866.md) |
| aws-cloudformation__cfn-lint-3875 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT localized gold #2 + correct contracts CONSUMED; agent LOOPED on schema-manager probing, hit maxiter, EMPTY source patch — never edited)** | no | no | [cfn-lint-3875.md](cfn-lint-3875.md) |
| aws-cloudformation__cfn-lint-3890 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT mislocalized (gold SnapStartSupported.py absent from top-6); agent SELF-localized via issue URL + INCOMPLETE fix — omitted `dotnet` runtimes)** | no | no | [cfn-lint-3890.md](cfn-lint-3890.md) |
| aws-cloudformation__cfn-lint-3947 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE: GT mislocalized (gold=schema DATA + StringLength.py, neither ranked); agent SELF-localized via grep "6144" + WRONG-gate-field fix (`"object" in type` ≠ `format=="json"`), never touched schema files)** | no | no | [cfn-lint-3947.md](cfn-lint-3947.md) |
| aws-cloudformation__cfn-lint-3982 | **/tmp/gt_30_artifacts (2026-06-09, right_traj=FALSE on outcome: GT localized gold AREA (IAM rule files #1/#3) + post-view routed agent to gold schema dir CONSUMED; agent edited the 2 gold schema files w/ correct `uniqueKeys:[Sid]` but +extra policy.json + placement/rule-path gap)** | no | no | [cfn-lint-3982.md](cfn-lint-3982.md) |

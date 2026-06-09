# Ledger — aws-cloudformation__cfn-lint-4023  (run 27214152241, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` → `resolved_instances: 0`, `unresolved_ids: ["aws-cloudformation__cfn-lint-4023"]`, NOT in `error_ids` → a real grade), baseline_pass=**no** (flip-CANDIDATE), flip=**no**. GOLD = `src/cfnlint/rules/resources/iam/Permissions.py` (W3037; gold adds `if not validator.is_type(action, "string"): continue` at the top of the `for action in actions` loop). Issue = W3037 false positive on `Fn::If` in `Action`.

**One-line trajectory finding (lead with this):** **right_trajectory = FALSE for GT, and a RUN-LEVEL EVAL ANOMALY.** GT MISLOCALIZED (L1 LOW ranked `PrimaryIdentifiers.py`, `PermissionSourceAccount.py`, `helpers.py`, `CacheClusterFailover.py`, `RelationshipConditions.py`, `Condition.py` — **none is `Permissions.py`**; the closest, `PermissionSourceAccount.py`, is a different file). The agent self-localized via `grep -r "W3037"` (the code is in the issue) at EVENT 8, landed on the gold file in ONE action, and wrote a patch **BYTE-IDENTICAL to the gold source patch**. Yet the task graded UNRESOLVED. The agent's correct fix not resolving — combined with `pytest` being absent in the agent env and the repo's `cpython-39` cache vs the agent's `python3.12.13` — points to a **run-level grading/environment reliability problem** affecting the cfn-lint repo, NOT a GT or agent-logic failure on this task.

---

## PREREQS (substrate, 8-dp verbatim from gate-deep + certs)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 76.39344262` (deterministic_edges `2796.0` / calls_edges `3660.0`) | GREEN | resolved-edge lines in brief only |
| **P1** name_match | `name_match_edges = 864` (23.6%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=249`, `impl_method=269`, `inherited=142`, `return_type=81`, `typing_fired=true` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls_edges=3660`; `name_match=864, import=661, same_file=627, verified_unique=584, impl_method=269, type_flow=249, lsp=182, inherited=142, return_type=81, unique_method=1` | GREEN | call/caller lines only |
| **P2** LSP | `server_launched=true`, `attempted=235`, `verified=106`, `corrected=76`, `deleted=0`, `failed=17`, `residual=176`, verdict `LSP_ACTIVE_VALID` | GREEN | 182 lsp edges fold in |
| **P3** embedder present | `class=EmbeddingModel`, dim=384, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000`, `rendered_semantic_nonzero=80/91`, `upstream_semantic_nonzero=344`, mode `present_and_consumption` | GREEN | re-orders L1 list only |

**Prereqs verdict:** gate-deep `all_on:true` — ALL GREEN. **Provenance caveat:** `graph_certificate` verdict `GRAPH_FAIL_MISSING_HANDOFF`. Substrate healthy.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 1 | `<gt-localization confidence="low">` / `Region: src/cfnlint/ — candidate edit targets (reason over these, confirm with grep):` / `  1. …resources/PrimaryIdentifiers.py` / `  2. …resources/lmbd/PermissionSourceAccount.py` / `  3. src/cfnlint/helpers.py` / `  4. …elasticache/CacheClusterFailover.py` / `  5. …functions/RelationshipConditions.py` / `  6. …resources/Condition.py` (gold `iam/Permissions.py` ABSENT) | EVENT 8 `grep -r "W3037" .../src/ --include="*.py" -l` → EVENT 9 obs `.../iam/Permissions.py` → EVENT 10 opens GOLD. The agent never opened any of the 6 GT candidates. | **D**=Y · **C**=N (0/6 gold; LOW conf, honest) · **C**=N (agent self-localized via W3037) |

**L1 verdict:** D/C/C = **Y/N/N**, leak=0. MISLOCALIZED; honest LOW + "confirm with grep". Inert.

## L3b post-view (`[GT]` enrichment)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| EVENT 11 | Prepended to the gold-file view: `[GT] Permissions:` / `[CONTRACT] def __init__(self):` / `[CONTRACT] def validate( -> ValidationResult` / `[CONTRACT] flows: validator -> validator.context` | EVENT 12 `[thought] Now I can see the issue. In the validate method… each action is expected to be a string. But when Fn::If is used, the action would be a dict…` — reasoning from the file body it just received. | **D**=Y · **C**=Y (real `validate`/`__init__` contracts, no leak) · **C**=N (reasoned from body; header inert, post-self-localization) |
| EVENT 15 | On `CfnLintKeyword.py`: `[GT] CfnLintKeyword:` / `Called by: …SnapStartSupported.py:27 …, …RuleScheduleExpression.py:25 …, …ecs/ServiceFargate.py:31 super().__init__ [service], …` | EVENT 16-17: agent greps `isinstance` patterns in iam dir; caller list not cited. | **D**=Y · **C**=Y (real subclass callers) · **C**=N |
| EVENT 19 | On `StatementResources.py` (sibling the agent consulted for the `is_type` pattern): `[GT] StatementResources:` / `[CONTRACT] def __init__(self, full_arn: str):` … | EVENT 17 obs had already shown `StatementResources.py:107 if not isinstance(resource, str)` — the agent copies THAT pattern (`is_type(action,"string"): continue`) from the sibling's body, not the GT header. | **D**=Y · **C**=Y · **C**=N (pattern came from the grep result body, not the GT contract) |

**L3b verdict:** D/C/C = **Y/Y/N**, leak=0. Correct, non-fabricated headers; all post-self-localization and inert. The agent's gold-matching fix idea came from a sibling file's source body (`StatementResources.py:107`), surfaced by the agent's own grep, not from GT.

## consensus (`<gt-scope>`)
`<gt-scope>` count = 2 firings (telemetry shows L5b active); no agent-visible scope block redirected the agent. The agent was already on the gold file. No misdirection, no consumption.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| post-edit | `gt_validate` advisory `CALLER-BLIND-EDIT  symbol=validate  callers=5  (no test file edited alongside this change)` (informational) | agent acknowledged, declined to edit tests (user said tests are handled). | **D**=Y · **C**=Y (5 callers real; **no FAIL_TO_PASS / test name leaked**) · **C**=Y (reasoned about it) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Correct, informational, no leakage.

## L4 / L5 / L5b / L6
Active per `gt_run_summary` (L1 util=0.75 "by_design", L3_router_v2 util=0.5 "structured_gt_side_but_no_agent_reaction" — telemetry self-admits no agent reaction). No observable governor effect.

---

## Cross-component line
- **Test-name / FAIL_TO_PASS leakage = 0** (chronological read of 87 events; `test_iam_permissions.py::test_permissions[A function…]` never surfaced by GT).
- **Consumed-count = 0** of GT drove the fix. The agent's gold-identical patch came from grepping `W3037` + copying the `is_type` guard from `StatementResources.py`'s source body.
- **Fair-probe = NO (BAD PROBE).** Issue prints `W3037`; `grep W3037` → gold in one action.

## Failure locus (why it did not resolve)
**RUN-LEVEL EVAL / ENVIRONMENT ANOMALY — NOT agent-logic, NOT GT.** The agent's source patch is **byte-identical to the gold source patch** (`if not validator.is_type(action, "string"): continue`), and the FAIL_TO_PASS case (`("A function", {"Ref": "MyParameter"}, 0)` — expects 0 errors for a non-string action) is exactly what that line satisfies. Yet the harness graded unresolved with `error_instances: 0`. Evidence of a degraded eval/agent environment:
- EVENT ~55: agent ran `pytest` → **`No module named pytest`** despite the prompt promising "all dependencies already installed"; agent had to `pip install pytest`.
- The freshly-installed pytest ran on **Python 3.12.13** (`/openhands/poetry/openhands-ai-...-py3.12`), while the repo's `__pycache__` is **cpython-39** → the agent tested in a different interpreter than the repo's prepared env (and likely than the grading container).
- This pattern is run-wide: 0/28 cfn-lint tasks resolved (2/30 overall, both non-cfn-lint), several with gold-equivalent agent patches.
The agent's only non-substantive pollution is a new `.openhands/TASKS.md` (harmless additive file). The grade contradicting a byte-identical-to-gold patch is the dominant anomaly for this task and must be classified as an eval/plumbing reliability issue before any "agent failed" or "GT failed" conclusion (CLAUDE.md TTD: do not classify a FAIL as model behavior until the metric/eval contract is proven correct).

**right_trajectory (GT) = FALSE** (GT mislocalized, not consumed; agent self-solved correctly but the grade is anomalous). gt_caused = FALSE. flip = no. **The agent wrote the gold fix; the eval did not credit it — investigate the grading environment.**

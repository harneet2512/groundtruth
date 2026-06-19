# Deep SWE PRODUCER Smoke — Go (anko)

Controlled producer-level check (Axis-1 logic). NO Docker, NO LLM, NO autonomous run.

## Task tuple

| field | value |
|---|---|
| language | go |
| instance_id | anko-default-function-arguments |
| repo_url | https://github.com/mattn/anko |
| commit_hash | 9d2d84bb1564e9513287998c56ccf16c01c19008 |
| issue_title | Add default arguments to Anko function parameters |

## 1. Clone / build stats

| stat | value |
|---|---|
| clone_ok | true |
| checkout HEAD | `9d2d84bb1564e9513287998c56ccf16c01c19008` (matches target) |
| corpus size | 4.0 MB |
| index_ok | true |
| files | 94 |
| nodes | 400 |
| edges | 651 |
| imports | 312 |
| properties | 2426 |
| assertions | 336 |
| build time | 3253 ms |
| workers | 12 |
| indexer | `gt-index-t1t2.exe` |

Edge breakdown (from indexer stdout): edges_import=76, edges_same_file=111, edges_name_match=123.

## 2. Schema

| key | value |
|---|---|
| schema_version | `v15.1-trust-tier` |
| indexer_version | `v16-multilang` |
| tables | assertions, cochanges, edges, file_hashes, nodes, project_meta, properties, sqlite_sequence |
| edge cols | id, source_id, target_id, type, source_line, source_file, resolution_method, confidence, metadata, trust_tier, candidate_count, evidence_type, verification_status |

## 3. Provenance distribution

resolution_method (651 edges):

| method | count |
|---|---|
| verified_unique | 290 |
| name_match | 123 |
| same_file | 111 |
| import | 76 |
| inheritance | 51 |

name_match % = **18.9%** (123/651).

trust_tier:

| tier | count |
|---|---|
| CERTIFIED | 477 |
| CANDIDATE | 97 |
| (empty) | 51 |
| SPECULATIVE | 26 |

Note: trust_tier counts sum to 651 only if the 51 empty-tier rows are the 51 inheritance edges (they are). CERTIFIED (477) ≈ verified_unique(290)+same_file(111)+import(76).

## 4. Producer output

Representative source file: **`vm/vmExprFunction.go`** (non-test, 6 funcs, 28 edges; directly relevant — function-call expression evaluation, where default arguments would be implemented).

### L1 brief (`generate_v1r_brief`) — excerpt

```
<gt-task-brief>
1. vm/vmExprFunction.go (func checkIfRunVMFunction(rt reflect.Type) bool {, func processCallReturnValues(...), func (runInfo *runInfoStruct) anonCallExpr() {)
   Contract: preserve return: rt.NumIn() < 1 || rt.NumOut() != 2 || ... ; return: rt.NumIn() > 1 | returns value|false
   Callers: convertVMFunctionToType() in vm/vmConvertToX.go:137 `if !checkIfRunVMFunction(rv.Type()) {` | convertVMFunctionToType() in vm/vmConvertToX.go:162 `rv, err := processCallReturnValues(rvs, true, false)`
   Context: funcExpr, callExpr, makeCallArgs | Last: b791e68 Fix empty position of lets-statement
   Calls: vm/vm.go, packages/sort.go, vm/vmExpr.go
2. core/core.go (func Import(e *env.Env) *env.Env {)
   ...
EDIT-TARGET CONTRACTS (vmExprFunction.go):
  processCallReturnValues -> calls func (e *Env)  [env/env.go:97]
  processCallReturnValues -> calls func reflectValueSlicetoInterfaceSlice(...)  [vm/vmConvertToX.go:11]
  anonCallExpr -> calls func (x *PosImpl)  [ast/pos.go:26]
  ...
Related files to inspect: vmConvertToX.go, vmExpr.go
</gt-task-brief>
<gt-graph-map>
vm/vmExprFunction.go :: checkIfRunVMFunction
  called by: callExpr (vm/vmExprFunction.go), con...
```

- HAS_BRIEF_TAG: true, HAS_GRAPHMAP: true.
- The two real callers surfaced (`convertVMFunctionToType` @ vmConvertToX.go:137/162) are CORRECT anko calls.

### L3b (`graph_navigation` on `vm/vmExprFunction.go`) — excerpt

```
[CONTRACT] func (runInfo *runInfoStruct) funcExpr() {
[CONTRACT] func (runInfo *runInfoStruct) anonCallExpr() {
[CONTRACT] func (runInfo *runInfoStruct) callExpr() {
Called by: vm/vmExpr.go:726 `runInfo.funcExpr()`, vm/vmConvertToX.go:137 `if !checkIfRunVMFunction(rv.Type())`
```

- L3b_contract: true, L3b_leak: false.

## 5. Hygiene verdict

| check | result |
|---|---|
| L1_HYGIENE | **CLEAN** — no `[GT_META]`/`[GT_STATUS]`/`__GT_STRUCTURED__`/`[VERIFIED]`/`[WARNING]`/`[INFO]`/`v22` in brief_text |
| L3b_leak | **false** |
| graphmap_present | true |
| L3b_contract | true |

Note: `[GT_CONFIG]` / `[GT_META] contract_pillar:` lines print to STDOUT from the harness but are NOT in `brief_text` (confirmed by membership test on `bt`). They are diagnostic stderr-class output, not agent-delivered content.

## 6. PROVENANCE BUG (Axis-1, Go) — CONFIRMED

**Go package-qualified / receiver-method calls into stdlib (esp. `reflect.Value`) are name-matched to project methods and CERTIFIED as DETERMINISTIC callers.**

At least **29 CERTIFIED edges** are demonstrably FALSE (receiver type does not match the certified target's receiver). The graph asserts the highest-trust tier on calls that go to stdlib, not to anko.

Confirmed false-edge classes:

1. **`reflect.Value.String()` / `reflect.Type.String()` / `reflect.Kind().String()`** → certified as a call to `env/env.go:97 func (e *Env) String()`.
   - e.g. `vmToX.go:20 return v.String()` where `v reflect.Value` (sig at vmToX.go:12: `func toString(v reflect.Value) string`). Receiver is `reflect.Value`, target is `*env.Env`. FALSE.
   - Also e47 (`reflect.TypeOf(v).String()`), e82, e99, e409, e427, e458, e462, e470, e477, e500, e541, e548, e551, e554, e556, e558.
2. **`reflect.Value.Set()`** (`ptrV.Elem().Set(v)`, `rv.Set(value)`, `value.Index(i).Set(v)`) → certified as `env/envValues.go:50 func (e *Env) Set(...)`. e378, e408, e412, e429, e482, e501.
3. **`reflect.Value.Len()`** (`rhsV.Len()`, `rv.Len()`, `item.Len()`) → certified as `packages/sort.go:17 func (s SortFuncsStruct) Len()`. e372, e410, e418, e437, e474, e503, e534, e550.
4. **`bufio.Scanner.Scan()`** (`anko.go:106 if !scanner.Scan()`) → certified as `parser/lexer.go:Scan`. e11. FALSE.
5. **`flag.Parse()`** (`anko.go:47`) → certified `verified_unique` as `parser/lexer.go:Parse`. e5. FALSE (direct stdlib package call).
6. **`syscall/js.Value.Set()/.String()`** in WASM glue (`misc/wasm/anko.go:23/28/33`, `result.Set(...)`, `.String()`) → certified as `env` methods. e207, e208, e210, e211, e213, e214, e222, e223. FALSE.

Root cause: Go's name-match resolver does not use receiver-type information, so any `x.String()` / `x.Set()` / `x.Len()` / `pkg.Parse()` where a project method shares the bare name is resolved to the project method and (because there's a single project-side candidate) promoted to `verified_unique` / CERTIFIED. The "verified" label is unjustified — the resolver verified name uniqueness within the project, not that the call actually targets a project symbol rather than stdlib/3rd-party. This is the documented Go failure mode (package-qualified / homonym collision).

**Impact on agent delivery (mitigated, not eliminated):** For the chosen edit-target file, the brief's `Calls:`/`Callers:` lines and L3b `Called by:` lines surfaced only the REAL callers (vmConvertToX.go, vmExpr.go) — the spurious `env.String`/`sort.Len` targets did not render into the agent-facing `Calls:` summary. So this run's L1/L3b output for `vm/vmExprFunction.go` is not visibly poisoned. But the graph itself carries the false CERTIFIED edges, and any producer path that enumerates outgoing CERTIFIED edges (e.g. impact/blast-radius, EDIT-TARGET CONTRACTS expansion) can surface them. The `processCallReturnValues -> calls func (e *Env) [env/env.go:97]` line in the EDIT-TARGET CONTRACTS block is itself one of these false edges (the `.String()` at line 456 is `reflect`/`fmt`, not `env.Env.String`).

## Verdict

- clone/index/schema: PASS
- L1/L3b hygiene + delivery for REL: PASS (clean, contract present, no leak)
- Axis-1 provenance: **FAIL** — ≥29 CERTIFIED false callers from stdlib/reflect homonym name-match promoted to verified_unique. Receiver-type-blind resolution mislabels stdlib method calls as deterministic project calls.

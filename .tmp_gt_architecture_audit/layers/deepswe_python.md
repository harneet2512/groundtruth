# Deep SWE PRODUCER Smoke — Python (mashumaro)

Controlled producer-level check (Axis-1 logic correctness). NO Docker, NO LLM, NO agent.

## Task tuple
- language: python
- instance_id: `mashumaro-flattened-dataclass-fields`
- repo_url: https://github.com/Fatal1ty/mashumaro
- commit_hash: `de139fd51c4d347666d109a8aea9d25451d908f6`
- issue_title: "Add flattened dataclass fields to Mashumaro field options"

## 1. Clone / checkout — OK
- Clone succeeded into `.tmp_gt_architecture_audit/fixtures/deepswe/python`.
- `git checkout de139fd…` → `HEAD is now at de139fd Bump version`
- `git rev-parse HEAD` = `de139fd51c4d347666d109a8aea9d25451d908f6` (matches)

## 2. Build graph.db — OK
Binary: `gt-index/gt-index-t1t2.exe`. BuildTime 2449 ms, 12 workers.
```
{"files":148,"nodes":1765,"edges":2915,"imports":1625,"properties":9289,
 "assertions":1800,"edges_import":734,"edges_same_file":578,"edges_name_match":371,
 "time_ms":2449,"workers":12}
```

## 3. Inspection
- **schema_version:** `v15.1-trust-tier` (indexer_version `v16-multilang`)
- **tables:** assertions, cochanges, edges, file_hashes, nodes, project_meta, properties, sqlite_sequence
- **nodes:** 1765   **edges:** 2915
- **edge resolution_method distribution:**

  | method | count |
  |---|---|
  | verified_unique | 899 |
  | import | 734 |
  | same_file | 578 |
  | name_match | 371 |
  | inheritance | 333 |

- **name_match %:** 371 / 2915 = **12.7 %**
- **trust_tier distribution:** CERTIFIED 2211, (empty, = inheritance) 333, CANDIDATE 255, SPECULATIVE 116

(Note: project_meta records edge_count=2582 while the live `edges` table holds 2915; the meta count was written before inheritance edges (333) were added — 2582 + 333 = 2915. Cosmetic meta-vs-table drift, not a producer-logic defect.)

- **Representative source file:** `mashumaro/core/meta/helpers.py` (55 functions; strong incoming edges — `type_name` has 53 incoming, `get_args` 49; outgoing present). Paths stored repo-relative.

## 4. GT producers (black-box invocation)
`v1r_brief.generate_v1r_brief` + `post_view.graph_navigation`, `GT_REPO_ROOT=DST`.

### L1 brief excerpt
```
<gt-task-brief>
1. mashumaro/jsonschema/schema.py (def get_schema(, def derive(self, **changes: Any) -> "Instance":, def get_self_config(self) -> Type[BaseConfig]:)
   Witness: fields called by _default [CALLS]
   Contract: returns Iterable[tuple[str, Type, bool, Any]]
   Spec: handles: f'Type {type_name(instance.type)} of field "{in... | f"in {type_name(instance.owner_class)} isn't su...
   Callers: build_json_schema() in mashumaro/jsonschema/builder.py:51 `schema = get_schema(instance, context, with_dialect_uri=with_dialect_uri)`
   Context: metadata, alias, owner_class, update_type, get_overridden_serialization_method | Last: 282531a Add support for recursive TypeAliasType types in JSON Schema
   Calls: mashumaro/core/meta/code/builder.py, mashumaro/core/meta/helpers.py, mashumaro/core/meta/types/common.py
   Tests: tests/test_jsonschema/test_json_schema_common.py, ...
EDIT-TARGET CONTRACTS (schema.py):
  fields -> calls def _default(  [mashumaro/jsonschema/schema.py:303]
  ...
</gt-task-brief>
<gt-graph-map>
mashumaro/jsonschema/schema.py :: fields
  calls: _default (...), get_self_config (...), get (...)
```
- **L1 hygiene:** CLEAN (no `[GT_META]`, `[GT_STATUS]`, `__GT_STRUCTURED__`, `[VERIFIED]/[WARNING]/[INFO]`, `v22` markers in brief body)
- `<gt-task-brief>` present: True   `<gt-graph-map>` present: True
- One `[GT_CONFIG] L1_SCOPE=low …` line goes to **stderr** (diagnostic), not into brief_text — OK.
- Minor: one non-ASCII glyph (`�`) rendered in the brief separator on a Windows console; cosmetic encoding artifact, not a content defect.

### L3b excerpt (`graph_navigation('mashumaro/core/meta/helpers.py')`)
```
Called by: mashumaro/core/meta/types/pack.py:100 `value_type: Union[type, Any] = get_function_return_annotation`,
           tests/test_meta.py:87 `assert is_init_var(InitVar[int])` [test],
           mashumaro/core/meta/types/unpack.py:272 `for literal_value in get_literal_values(spec.type):`,
           mashumaro/jsonschema/schema.py:163 `if is_annotated(self.type):` [model],
           mashumaro/exceptions.py:14 `return type_name(self.field_type, short=True)`
```
- **L3b contract present:** False (no `[CONTRACT]` line emitted for this file — caller-list mode)
- **L3b leak:** False (no `[GT_META]` / `__GT_STRUCTURED__`)

## 5. PROVENANCE BUG — FOUND (stdlib/builtin name-shadow, analogous to Python `os.walk`)

**Bug:** Builtin `list.append` call sites are resolved as `verified_unique` / `CERTIFIED`
(confidence 1.0) edges into the single project method `CodeLines.append`.

- Target node id=278: `append` — **the only** project definition named `append`,
  a `Method` of class `CodeLines` at `mashumaro/core/meta/code/lines.py:13`
  (`def append(self, line: str) -> None: self._lines.append(...)`).
- Because it is the sole same-named definition, the resolver promotes every unresolved
  `…append(` reference to **verified_unique** ("1 candidate ⇒ certain") and stamps it
  **CERTIFIED**, without checking the receiver is a `CodeLines` instance.

**Blast radius:** 40 edges target `CodeLines.append`:
- 38 × `verified_unique` / CERTIFIED (cross-file)
- 2 × `same_file` / CERTIFIED (id=336 `extend`→append, id=337 `indent`→append — these two are LEGIT; they call `self.append` inside `lines.py`)

**Exact false edges (call site verified in source = builtin `list.append`, NOT `CodeLines.append`):**

| edge id | method/tier | source func | source line | actual receiver |
|---|---|---|---|---|
| 386 | verified_unique/CERTIFIED | `collect_type_params` | `mashumaro/core/meta/helpers.py:482` → `type_params.append(type_arg)` | `type_params` is a local `list` |
| 393 | verified_unique/CERTIFIED | `_flatten_type_args` | `mashumaro/core/meta/helpers.py:525` → `result.append(type_arg)` | `result` is a local `list` |
| 867 | verified_unique/CERTIFIED | `on_dataclass` | `mashumaro/jsonschema/schema.py:400` → `required.append(f_name)` | `required` is a local `list` |
| 8 | verified_unique/CERTIFIED | `create_spec` | `benchmark/create_chart_specs.py:22` → `values.append({...})` | `values` is a plain `list` |

Source proof (helpers.py:482):
```python
type_params.append(type_arg)          # builtin list.append — NOT CodeLines.append
```
Source proof (schema.py:400):
```python
required.append(f_name)               # builtin list.append — NOT CodeLines.append
```
Of 158 `*.append(` attribute call sites in `mashumaro/`, only the ~26 `self.append(` sites
inside `lines.py` (and genuine `CodeLines`-typed receivers) are real `CodeLines.append`
calls. The 38 cross-file `verified_unique` edges are predominantly **false callers** laundered
as CERTIFIED. This is the same class of defect as the Python `os.walk` stdlib-shadow: a
builtin/stdlib member name collides with a uniquely-named project symbol, and single-candidate
uniqueness is mistaken for type-verified resolution.

**Why it matters for the product:** these false CERTIFIED edges inflate `CodeLines.append`'s
caller/impact counts and could surface a builtin call as a "verified" project caller in L3/L1
evidence — exactly the "confident on a wrong signal" failure the trust-tier system is meant to
prevent. `verified_unique` should require an attribute-receiver / type check (or at least demote
method-name single-candidates that are also builtin container methods) before being stamped CERTIFIED.

## Hygiene verdict
- L1: CLEAN. L3b: no leak, no contract line (caller-list mode).
- Producers ran deterministically, no Docker/LLM. Brief + graph-map tags present.
- One provenance bug found (builtin `append` → `CodeLines.append` false-CERTIFIED, 38 cross-file edges).

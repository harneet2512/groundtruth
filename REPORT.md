# final_hardening item 11 — symbol taxonomy and edge kinds

Closes delta rows 8 and 9 of `instinct_work/03-gt-gnx-pipeline.md`.

- **Producer worktree:** `D:/gt-fh-item11-taxonomy`, branch `final_hardening/item11-taxonomy`
- **Base:** `43514ced1` — 10 code commits plus this report, not pushed
- **Harness worktree** `D:/gt-fh-item11-engine`: **untouched.** No consumer edit was
  trivial enough to make safely under test; the allow-lists to extend are listed in
  §6 with file and line so the orchestrator can do it deliberately. §6 was
  rewritten after review — the first version surveyed the wrong checkout and
  wrongly reported that `gt_engine/retrieval.py` and `SYMBOL_LABELS` do not
  exist. They do, and `SYMBOL_LABELS` is the most consequential gate in the
  list.

Everything numeric below was measured on this machine on 2026-09-03/04. Nothing
is estimated. Where a thing was not measured it says so.

---

## 1. Before — what GT emits today

Read-only from `D:/tmp/claude/D--gt-harness/.../scratchpad/ark-new.db`
(arktype at its task base `04355e8b`, 458 files). The `edges` table's kind column
is named `type`, not `kind`; `properties.kind` is a separate table, given too.

```sql
SELECT label, COUNT(*) FROM nodes GROUP BY 1;
SELECT type,  COUNT(*) FROM edges GROUP BY 1;
SELECT kind,  COUNT(*) FROM properties GROUP BY 1;
```

| node label | count | | edge type | count |
|---|---:|---|---|---:|
| CompletenessFact | 113,695 | | HAS_COMPLETENESS_FACT | 113,695 |
| Callsite | 16,431 | | HAS_CALLSITE | 16,431 |
| UnresolvedFact | 15,089 | | HAS_UNRESOLVED_FACT | 15,089 |
| DerivationFact | 10,820 | | CANDIDATE | 10,820 |
| **Function** | **2,747** | | CANDIDATE_TARGET | 10,820 |
| **Class** | **350** | | HAS_DERIVATION_FACT | 10,820 |
| **Method** | **314** | | CALLS | 5,933 |
| **File** | **100** | | IMPORTS | 2,445 |
| QueryPolicyVersion | 2 | | SELECTED_TARGET | 1,342 |
| | | | CONTAINS | 314 |
| | | | READS | 210 |
| | | | DATA_FLOW | 116 |
| | | | RE_EXPORTS | 87 |
| | | | WRITES | 60 |
| | | | COMPOSES | 48 |
| | | | PRECEDES | 15 |
| | | | EXTENDS | 10 |
| | | | CO_SERIALIZES | 8 |
| | | | RAISES | 1 |

Four of the nine labels are code symbols; the other five are evidence rows. Of
159,548 nodes, 3,511 (2.20%) are code symbols, and **every one of them is one of
four labels**. A Rust `struct`, `enum`, `trait` and `impl` are all `Class`. A
TypeScript `interface` is `Class`. That is the row 8 gap: not that GT lacks
tables, but that it cannot *say what a symbol is*.

`properties.kind` before: caller_usage 1,637 · fingerprint 1,407 · param 1,285 ·
data_flow 1,236 · return_shape 1,158 · field_read 565 · visibility 403 ·
boundary_condition 323 · guard_clause 317 · call_order 279 · class_field 255 ·
side_effect 151 · conditional_return 80 · exception_type 39 · docstring 35 ·
exception_handler 29 · exception_flow 16 · serialization_pair 16 · config_read 2.

**This before-table reproduces exactly.** Rebuilding the graph from source at the
base commit `43514ced1` gave byte-identical counts for every label, every edge
type and every property kind. The comparison below is therefore against a
regenerated baseline, not a copied one.

---

## 2. After — the same rebuild, with item 11

`gt-index -root repos/arktype-04355e8b -output ark-after.db`, binary built at
HEAD with the RC-17 identity stamps.

### 2.1 Pre-existing kinds: zero drift

**Every pre-existing node label, edge type and property kind has the identical
count before and after.** Not "within noise" — identical, all 9 labels, all 19
edge types, all 19 property kinds. Total nodes 159,548 → 159,548. The resolver's
own line is unchanged too: `Resolved 5933/16431 ... [name_match:2312]
[import:756] [same_file:625]` before and after.

### 2.2 What is new

| new edge type | rows | mechanism | CANDIDATE | SPECULATIVE | CERTIFIED |
|---|---:|---|---:|---:|---:|
| RETURNS_TYPE | 107 | `syntactic_return_annotation` | 55 | 52 | **0** |
| OVERRIDES | 9 | `syntactic_override_marker` | 5 | 4 | **0** |
| DECLARED_IMPLEMENTS | 2 | `syntactic_implements_clause` | 2 | 0 | **0** |
| DECORATES | 0 | — | — | — | — |
| PARAM_TYPE | 0 | — | — | — | — |

Edges total 188,264 → 188,382 (+118, exactly the three new kinds).
DECORATES and PARAM_TYPE are zero on arktype because it records no
`class_decorator` property and no `param` row whose annotated type names a
declared symbol; both kinds are unit-tested in `internal/taxonomy`.

| new property kind | rows |
|---|---:|
| `symbol_kind` | 3,511 |
| `override_marker` | 9 |
| `implements_type` | 3 |

**3,511 = every code symbol, exactly once.** Measured directly: 0 symbol nodes
without a `symbol_kind` row, 0 nodes with more than one.

### 2.3 The taxonomy on arktype

Four labels resolve into eight kinds. The cross-tab is the point of the item, so
here it is in full rather than as a summary:

| label | kind | count |
|---|---|---:|
| Class | interface | 277 |
| Class | class | 73 |
| Function | test | 1,654 |
| Function | function | 934 |
| Function | **method** | **127** |
| Function | accessor | 25 |
| Function | constructor | 7 |
| Method | method | 188 |
| Method | accessor | 67 |
| Method | function | 30 |
| Method | constructor | 29 |
| File | file | 100 |

Read the bold row. 127 nodes labelled `Function` are `method_definition` nodes:
the walk did not parent them, because in arktype they sit inside class
expressions and object literals rather than a `class_declaration`, so the label
says Function and the grammar says method. The taxonomy is strictly more precise
than the label it leaves untouched. The reverse also shows: 30 nodes labelled
`Method` are `function_declaration` or `arrow_function` nodes that happen to be
nested under a class node.

The rest is the row 8 capability in one table. The 350 nodes labelled `Class`
are 73 classes and 277 interfaces — a distinction the graph could not previously
express in TypeScript at all. The 2,747 labelled `Function` are 934 functions,
25 accessors, 7 constructors, 127 methods, and **1,654 synthesized test cases**:
60% of the label is test scaffolding that nothing in the graph previously
distinguished from product code.

### 2.4 Verification of the no-promotion rule, against the stored rows

```sql
SELECT COUNT(*) FROM edges
 WHERE type IN ('DECLARED_IMPLEMENTS','OVERRIDES','DECORATES','RETURNS_TYPE','PARAM_TYPE')
   AND (trust_tier NOT IN ('CANDIDATE','SPECULATIVE')
        OR confidence >= 1.0
        OR verification_status <> 'unverified'
        OR evidence_type <> 'syntax');
-- 0
```

The tier is a function of the candidate set and nothing else: exactly one
declaration in the repository carries the name → CANDIDATE at confidence 0.9;
several → SPECULATIVE at 1/N, **all retained**, with `candidate_count` holding
the true size even when the retained list hits the cap of 8, and
`abstention_reason=candidate_set_truncated` in the metadata when it does. That
is delta row 9's point: gnx's one-row-one-target schema forces a pick at write
time; here the disagreement stays in the graph.

---

## 3. Language × kind matrix

Generated, not typed:

```bash
(cd gt-index && go run -tags sqlite_fts5 ./internal/specs/cmd/taxonomy-probe)
(cd gt-index && go run -tags sqlite_fts5 ./internal/specs/cmd/taxonomy-probe -reasons)
```

| language | function | method | constructor | accessor | class | interface | struct | enum | enum_member | trait | impl | type_alias | protocol | record | union | namespace | module | macro | constant | annotation | decorator | field | file | table | message | service | rule | element | section | resource | test |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bash | 1 | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| c | 1 | . | . | . | . | . | 1 | D | D | . | . | D | . | . | D | . | . | . | . | . | . | P | 1 | . | . | . | . | . | . | . | . |
| cpp | 1 | . | . | . | 1 | . | 1 | D | D | . | . | D | . | . | D | D | . | . | . | . | . | P | 1 | . | . | . | . | . | . | . | . |
| csharp | . | 1 | 1 | . | 1 | 1 | 1 | D | D | . | . | . | . | D | . | D | . | . | . | . | P | P | 1 | . | . | . | . | . | . | . | . |
| css | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | 1 | . | . | . | . |
| cue | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| elixir | 1 | . | . | . | . | . | 1 | . | . | . | 1 | . | 1 | . | . | . | 1 | 2 | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| elm | 1 | . | . | . | . | . | . | . | D | . | . | 1 | . | . | 1 | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| go | 1 | 1 | . | . | . | 1 | 1 | . | . | . | . | 2 | . | . | . | . | . | . | . | . | . | P | 1 | . | . | . | . | . | . | . | . |
| groovy | . | 1 | D | . | 1 | D | . | D | D | . | . | . | . | D | . | . | . | . | . | D | P | P | 1 | . | . | . | . | . | . | . | . |
| hcl | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | 1 | . |
| html | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | 1 | . | . | . |
| java | . | 1 | 1 | . | 1 | 1 | . | 1 | D | . | . | . | . | D | . | . | . | . | . | D | P | P | 1 | . | . | . | . | . | . | . | . |
| javascript | 3 | 1 | 1 | 2 | 2 | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | P | P | 1 | . | . | . | . | . | . | . | 1 |
| kotlin | 1 | . | . | . | 2 | 1 | . | 1 | . | . | . | D | . | 1 | . | . | . | . | . | . | P | P | 1 | . | . | . | . | . | . | . | . |
| lua | 2 | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| markdown | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | 1 | . | . |
| ocaml | 2 | . | . | . | . | . | . | . | . | . | . | 1 | . | 1 | 1 | . | 1 | . | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| php | 1 | 1 | 1 | . | 1 | 1 | . | D | D | D | . | . | . | . | . | D | . | . | . | . | P | P | 1 | . | . | . | . | . | . | . | . |
| protobuf | . | 1 | . | . | . | . | . | 1 | D | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | 1 | 1 | . | . | . | . | . |
| python | 1 | . | 1 | . | 1 | . | . | . | . | . | . | D | . | . | . | . | . | . | . | . | P | P | 1 | . | . | . | . | . | . | . | . |
| ruby | . | 2 | 1 | . | 1 | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | P | 1 | . | . | . | . | . | . | . | . |
| rust | 2 | . | . | . | . | . | 1 | 1 | . | 1 | 1 | D | . | . | D | . | D | D | D | . | . | P | 1 | . | . | . | . | . | . | . | . |
| scala | 1 | . | . | . | 2 | . | . | D | . | 1 | . | D | . | 1 | . | . | . | . | 1 | . | . | P | 1 | . | . | . | . | . | . | . | . |
| sql | 1 | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | 1 | . | . | . | . | . | . | . |
| svelte | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | . | . |
| swift | 1 | . | D | . | 1 | . | 1 | 1 | D | . | 1 | D | 1 | . | . | . | . | D | . | . | . | P | 1 | . | . | . | . | . | . | . | . |
| toml | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | 1 | . |
| typescript | 3 | 1 | 1 | 2 | 2 | 1 | . | D | D | . | . | D | . | . | . | D | . | . | . | . | P | P | 1 | . | . | . | . | . | . | . | 1 |
| yaml | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | . | 1 | . | . | . | . | . | . | 1 | . |

30 languages, 31 kinds. N = annotated on a node GT already emits, via N node types (or synthesized by the parser); D = declared: the grammar has it and the mapping names the node type, but GT emits no node because parser.EmitTaxonomyDeclarations is off; P = already a typed property row; . = absent, with a recorded reason (-reasons).

**30 languages, 31 kinds, 930 cells, and not one of them is blank by omission.**
`TestTaxonomyCoversEveryKind` fails the build if any language leaves any kind
unaccounted, so a `.` is a written claim about the grammar. `-reasons` prints
all 768 of them; a sample:

```
== go ==
  constructor  Go has no constructor declaration; NewT is a naming convention, not syntax
  enum         Go has no enum; the iota idiom is a const_declaration indistinguishable from any other
  class        Go has no class; a named type is mapped to struct, interface or type_alias
== python ==
  enum         an Enum is a class_definition inheriting enum.Enum; the declaration node is identical
               to any other class
  protocol     typing.Protocol is a base-class name, not a declaration form
  method       a Python method is a function_definition inside a class body; GT already records the
               difference with the Method label and a parent
== rust ==
  interface    Rust spells the interface construct trait
  class        Rust has no class; the declaration forms are struct, enum, union and trait
```

Restraint is the interesting part of the matrix. IMPLEMENTS is claimed for
**five** grammars only - typescript, java, groovy, php, rust - because those are
the five whose syntax SEPARATES interface conformance from superclass extension
(`implements_clause`, `super_interfaces`, `class_interface_clause`, and Rust's
`impl Trait for Type` where the trait is a named field). C#'s `base_list`,
Kotlin's `delegation_specifier`, Scala's extends-with, Swift's
`inheritance_specifier` and Python's base-class list carry both in one clause
with identical syntax, so those five declare IMPLEMENTS unavailable rather than
guess from a name. `TestImplementsIsOnlyClaimedWhereTheGrammarSeparatesIt` makes
adding a sixth a deliberate act.

---

## 4. Four pre-existing spec defects this work MEASURED

`TestTaxonomyNodeTypesExistInTheGrammar` checks every node type named in a
mapping against the compiled grammar's own tree-sitter symbol table. Running the
same check against GT's existing `internal/specs` entries found four names no
grammar can produce, so they never match and the language silently indexes
nothing:

| language | spec looks for | grammar actually has | effect |
|---|---|---|---|
| lua | `function_declaration`, `function_definition_statement` | `function_statement`, `function` | **no Lua symbol is ever emitted** |
| sql | `create_table_statement`, `create_function_statement` | `create_table`, `create_function` | **no SQL symbol is ever emitted** |
| svelte | `function_declaration` | grammar has none; the script body is `raw_text` | **no Svelte symbol is ever emitted** |
| swift | `struct_declaration` | grammar has none - struct, enum, actor and extension are all `class_declaration` | every Swift struct and enum looked like a class |

These are **not regressions from this item** and are deliberately **not fixed**
here: correcting lua, sql or svelte would create symbols where there were none
and move label totals, which this item may not do. They are mapped truthfully,
and `TestLanguagesWithNoReachableDeclarationsStayEmpty` pins the lua and sql
cases so a later correction is a visible test change rather than a silent shift
in graph size. The swift case IS fixed within the rules, because it needs no new
node: keyword refinement on `class_declaration` separates struct/enum/extension
from class without touching the label.

---

## 5. What is NOT delivered, and the measurement that stopped it

The item has two halves. The kind annotation shipped. **Emitting new nodes for
declarations GT has no entry for is implemented, tested, and gated OFF**
(`parser.EmitTaxonomyDeclarations = false`).

The first arktype rebuild had it on. It added 1,565 real symbols - TypeScript
`Enum` 2, `EnumMember` 4, `TypeAlias` 1,446, `Namespace` 113 - and moved eleven
pre-existing totals. The CALLS edges did not merely grow, they **moved**:

| CALLS target label | before | with declarations on |
|---|---:|---:|
| Method | 3,188 | 2,266 |
| Class | 328 | 17 |
| Function | 2,417 | 2,380 |
| Namespace | 0 | 1,053 |
| TypeAlias | 0 | 460 |

1,513 calls that had resolved to a callable now resolved to a namespace or a
type alias. Neither is callable. The summary line read "+243 calls resolved" and
"import 756 -> 1,980, name_match 2,312 -> 1,422", which looks like a resolution
improvement and is the opposite of one: 922 method targets and 311 class targets
were lost to names that cannot be called. TypeScript declaration merging is the
mechanism - a `namespace X` shadows the `class X` it merges with - and the
resolver rung reached through the import index does not filter by label. Ten
further totals moved with it, including `File` 100 -> 66, because a file that now
yields a `TypeAlias` node no longer qualifies for the synthetic file anchor.

Fixing this means isolating taxonomy declarations from the pre-existing name
resolution, which is work inside `internal/resolver` - a protected path and a
different item. Consequences, stated plainly:

- The plan's row 8 criterion **"code symbols as a share of nodes rises materially
  above the 2.2% baseline" is NOT met.** Measured: 3,511/159,548 = **2.20%
  before and after**, unchanged. With the gate on it was 5,076/161,998 = 3.13%,
  at the cost above. This criterion is deferred, not claimed.
- Struct, Enum, Trait, Impl, TypeAlias and Namespace **are** emitted per language
  as `symbol_kind` wherever the parser already emits the node (Rust
  struct/enum/trait/impl, Go struct/interface/alias, Swift struct/enum/extension,
  Scala trait, OCaml module, protobuf message/service/enum, Elm union). Where GT
  emits no node at all they show as `D` in the matrix.
- The gate is a `var`, not a `const`, so the emitter stays under test:
  `TestGatedDeclarationEmitterStillWorks` turns it on and asserts the TypeScript
  and Rust declarations appear; `TestDeclarationEmissionIsOffByDefault` states
  the shipped default in one place.

### 5.2 DECLARED_IMPLEMENTS, and why it is not called IMPLEMENTS

`internal/resolver/relationships.go` **already writes an IMPLEMENTS edge** for
JS/TS, Java/Kotlin and Rust, derived by a line-based regex over source text and
written at confidence 1.0 (arktype happens to have 0 such rows; other
repositories do not). Reusing the kind would have moved a pre-existing total and
merged two mechanisms of very different strength under one name. Row 9 says the
opposite - "two analyses that disagree stay representable" - so the tree-sitter
reading is published beside the regex one as its own kind. The collision guard in
`edges_test.go` is driven by a measured enumeration of every upper-case literal
in `internal/resolver`, `internal/store`, `internal/closure`, `internal/process`,
`internal/community`, `internal/cochange` and `cmd/gt-index`.

---

## 6. Consumer allow-lists the orchestrator must extend

The harness worktree `D:/gt-fh-item11-engine` (base `7b8d8183`) was **not**
edited. This section was **corrected after review**: the first version surveyed
`D:/gt-harness`, which is a different checkout on `linear/har-81-diagnostics`
and does NOT contain item 4's engine half. Everything below was re-read in
`D:/gt-fh-item11-engine` itself. The two trees genuinely differ -- files cited
in the first version (`relational_context.py`, `thin_compiler.py`,
`decision_sufficiency.py`, `scripts/swebench/oh_gt_full_wrapper.py`) do not
exist in the engine worktree at all, and the two that do exist there
(`graph_context.py`, `hybrid_retrieval.py`) carry different content. The earlier
claim that "there is no `gt_engine/retrieval.py` and no `SYMBOL_LABELS`" was
false and is withdrawn.

### 6.1 `gt_engine/retrieval.py` — `SYMBOL_LABELS`

```python
# gt_engine/retrieval.py:106
SYMBOL_LABELS: tuple[str, ...] = ("Class", "File", "Function", "Method")
```

It is the **default `labels=` argument of all three rankers** —
`lexical_rank` (line 464), `dense_rank` (line 733), `hybrid_rank` (line 928) —
and it reaches SQL as `AND n.label IN (...)` at line 498 (the `nodes_fts` BM25
join) and line 693 (the dense pool).

**Consequence:** a symbol carrying a new label is **unreachable by retrieval**.
Not down-ranked — absent. It cannot be returned lexically, it is never admitted
to the dense pool, and therefore it cannot appear in the fused hybrid ranking
either. Extending this tuple is the single change that makes new labels
deliverable. It is the most important entry in this section and its omission
from the first version made §6 incomplete exactly where a reader would rely on
it.

This gate binds only once `parser.EmitTaxonomyDeclarations` is on (§5); with the
gate off, every symbol still carries one of the four labels, so retrieval is
unaffected by this item as shipped.

### 6.2 `gt_engine/contract.py` — `CODE_SYMBOL_LABELS`

```python
# gt_engine/contract.py:85
CODE_SYMBOL_LABELS: tuple[str, ...] = ("Function", "Class", "Method", "File")
```

Expanded into `_LABEL_PLACEHOLDERS` (line 139) and applied as `label IN (...)`
in four statements — `_SELECT_SYMBOLS_WITH_MINTED` (151),
`_SELECT_SYMBOLS_NODES_ONLY` (158), `_COUNT_SYMBOLS_BY_LABEL` (168) and
`_COUNT_FACTS` (176) — and passed at call sites 437, 460, 496 and 500.

**Consequence:** a new label never enters the engine's symbol set at all, and is
excluded from the symbol and fact counts `coverage()` reports. Note the second
half specifically: `_COUNT_SYMBOLS_BY_LABEL` and `_COUNT_FACTS` are what produce
the code-symbols-versus-fact-rows figure that row 8's 2.2% baseline is measured
against, so this tuple and that acceptance criterion are the same tuple. Its own
comment already says why `File` is in it — "excluding it would quietly flatter
the denominator that `coverage()` reports" — and the same argument applies to
every label added later.

### 6.3 Not a gate, but a behaviour change worth knowing

`property_rank` (`retrieval.py:542`) takes `kinds: Sequence[str] | None = None`
and applies `AND p.kind IN (...)` only when it is passed (line 601), so the
3,511 new `symbol_kind` rows need **no** allow-list change to flow through.
They do change what that ranker sees: matching is substring `LIKE` over
`properties.value` (line 596) and scoring is distinct-query-term coverage, so a
query containing the word *function*, *test*, *class*, *interface*, *method*,
*accessor*, *constructor* or *file* now matches every symbol of that kind. On
arktype that is 1,654 symbols for "test" alone. Whether that is signal or noise
depends on the query; it is stated here because it is a real change to a live
ranker that no allow-list would have flagged.

`gt_engine/graph_context.py` in this tree references `label` nowhere at all, so
no label gate applies. Its edge gate is
`e.confidence>=0.7 AND COALESCE(n.is_test,0)=0` (line 474), which admits
CANDIDATE taxonomy edges (confidence 0.9) and excludes SPECULATIVE ones
(<= 0.5) — sensible as-is, and worth knowing rather than discovering. Nothing in
this tree allow-lists edge `type`; the only literal edge-type predicate is
`gt_engine/runtime_observation.py:492`, `WHERE e.type='CALLS'`, which is a
CALLS-specific query rather than a vocabulary to extend.

### 6.4 One that must NOT be extended

`gt_engine/persistent_execution_state.py` `_CERTIFIED_RELATIONS` already lists
`implements` and `overrides`, and it gates what may be presented as CERTIFIED.
**No taxonomy edge is ever certified**, so adding `declared_implements` there
would grant exactly the promotion this item forbids. `_certified_relation()`
already refuses anything outside the set, so leaving it alone is the correct
default, not an oversight.

## 7. Commits

| SHA | subject |
|---|---|
| `93b4e0348` | feat: a closed symbol-kind vocabulary across all 30 grammars |
| `bac5d8e52` | feat: emit symbol kinds and new declarations from the parser |
| `f6dd178cc` | feat: derive taxonomy edge kinds with a tier the syntax earns |
| `ae2845587` | fix: publish the tree-sitter reading as DECLARED_IMPLEMENTS |
| `3cb3aec40` | **test(red)**: the taxonomy edge kinds never reach the graph |
| `6b171d5f5` | fix: wire the taxonomy edges into publication |
| `a5ec39dd2` | feat: a probe that prints the language x kind matrix |
| `5df753e5d` | fix: gate declaration nodes off, measured, not out of caution |
| `df4d1b68a` | feat: give the synthesized test-case node its own kind |
| `2c013e240` | chore: restore four files this stream only reformatted |

`cmd/gt-index/main.go` is the only protected path touched: an 18-line hook beside
the CONTAINS block. It is covered by the fixture-first pair `3cb3aec40` ->
`6b171d5f5`, receipt format `gt.fixture-red.v1`, `base_sha=ae2845587`, exit code
1 recorded, 0 after the fix. The fixture re-executes:

```bash
.githooks/tests/item11_taxonomy_edges_red.sh    # exit 1 at ae2845587, exit 0 at HEAD
```

`internal/parser/parser.go` takes four hooks totalling 37 lines; all logic lives
in new files. `internal/store/sqlite.go` was **not** touched - the new edge kinds
need no schema change, because `edges` already carries type, resolution_method,
trust_tier, candidate_count, evidence_type, verification_status and metadata.

Coordination: `D:/gt-fh-budget` was read with `git -C ... diff` only; no file in
it was edited, and this stream touches none of `AnalyzeVTA`,
`persistOneVTAFlowFactTx` or `AttachResolutionGraphTx`.

---

## 8. Tests

23 new tests, all green.

`internal/specs/taxonomy_test.go`:

- `TestEveryLanguageHasATaxonomy` - a grammar with no mapping is a grammar nobody
  looked at
- `TestTaxonomyCoversEveryKind` - every kind, every language, in exactly one of
  present / already-a-property-row / absent-with-a-reason
- `TestTaxonomyKindsAreInTheVocabulary` - the vocabulary is closed
- `TestTaxonomyNodeTypesExistInTheGrammar` - every mapped node type checked
  against the compiled grammar's tree-sitter symbol table. This is the test that
  stops the taxonomy inventing evidence, and it found the four defects in section 4
- `TestNewLabelsNeverCollideWithASymbolLabelTheParserAlreadyAssigns`
- `TestTaxonomyEdgeKindsCarryAMechanismAndNeverCertify`
- `TestImplementsIsOnlyClaimedWhereTheGrammarSeparatesIt`
- `TestContainsWordMatchesOnlyWholeWords` - def must not match inside defmodule,
  get must not match inside getUser

`internal/parser/taxonomy_lang_test.go` - per-language fixtures asserting the
**exact** kind set (whole-set comparison, so an over-claiming mapping fails
rather than passing on a subset) for TypeScript, Go, Rust, Java, Python, C++, C#,
PHP, Swift and Elixir; plus `TestEveryEmittedSymbolCarriesExactlyOneKind`,
`TestTaxonomyNeverRelabelsAnExistingNode`,
`TestLanguagesWithNoReachableDeclarationsStayEmpty`,
`TestGatedDeclarationEmitterStillWorks`, `TestDeclarationEmissionIsOffByDefault`.

`internal/taxonomy/edges_test.go` - `TestNoTaxonomyEdgeIsEverCertified`,
`TestUniqueNameIsCandidateAmbiguousIsSpeculative`,
`TestOverSizedCandidateSetKeepsTheTrueCount`,
`TestAMechanismTheKindDoesNotGrantIsDropped`, `TestUnresolvableNamesAbstain`,
`TestOverridesNeverPointsAtItself`, `TestDecoratesPointsFromTheDecorator`,
`TestNoTaxonomyEdgeReusesAPreExistingKind`, plus the reference-name readers.

`cmd/gt-index/taxonomy_edges_test.go` - end-to-end over the compiled binary:
`TestTaxonomyEdgesReachTheGraph` (the rows are in the database, and every stored
row carries a granted mechanism and an unpromoted tier) and
`TestTaxonomyDoesNotMoveAPreExistingEdgeTotal` (the control: CONTAINS=2,
EXTENDS=1, Class=2, Method=2, Function=0, unchanged by the wiring).

The end-to-end test earned its cost immediately: it caught that
`typeReferenceName` refused ": Circle" and "-> Circle" - the forms tree-sitter
actually returns for a return type - so RETURNS_TYPE silently produced nothing
while every unit test, which passed bare type names, was green.

### Verify

```bash
cd gt-index && go build -tags sqlite_fts5 ./... && go test -tags sqlite_fts5 ./...
```

Exact result at `2c013e240`, exit 0:

```
ok    cmd/gt-index         71.226s     ok    internal/process     (cached)
ok    internal/closure     (cached)    ok    internal/resolver     2.698s
ok    internal/cochange    15.611s     ok    internal/specs        1.472s
ok    internal/community   19.806s     ok    internal/store       (cached)
ok    internal/parser       2.493s     ok    internal/taxonomy     1.560s
ok    internal/walker       1.487s
?     internal/cochange/cmd/cochange-probe        [no test files]
?     internal/community/cmd/community-probe      [no test files]
?     internal/specs/cmd/taxonomy-probe           [no test files]
?     internal/types                              [no test files]
```

Baseline at `43514ced1`, before any change: the same packages, all `ok`, no
failures. No pre-existing test was modified.

Graph rebuild:

```bash
scratchpad/item11/build.sh D:/gt-fh-item11-taxonomy scratchpad/item11/gt-index-after.exe
scratchpad/item11/gt-index-after.exe -root repos/arktype-04355e8b -output ark-after.db
```

`build.sh` mirrors `scripts/swebench/build_gt_index_linux.sh`'s
`-ldflags "-X main.commitSHA=... -X main.sourceFingerprint=..."`. Without those
stamps publication aborts with "attach graph with incomplete producer identity"
and no graph is written - worth knowing before reproducing this.

---

## 9. Gaps

1. **Declaration nodes are gated off** (section 5). The blocker is resolver
   isolation, not the emitter. Row 8's "code symbols as a share of nodes rises
   materially above 2.2%" is therefore **not met**: measured 2.20% before and
   after.
2. **INSTANTIATES is in the vocabulary and is not derived.** The parser's
   `AssignmentRef` carries an enclosing scope NAME, not a node index, so binding
   one to a symbol would be a name guess rather than a syntactic reading.
3. **DECORATES and PARAM_TYPE are 0 on arktype.** Both are unit-tested but
   unexercised by this fixture; a Python or Java repository would exercise them.
   Unmeasured.
4. **class_decorator is emitted for class nodes only.** Python function
   decorators and Rust attribute macros produce no property row, so DECORATES
   cannot see them. Recorded in the Rust and Swift mappings.
5. **Only one repository was measured** - arktype, TypeScript. The Rust, Java,
   C++, C#, PHP, Swift, Python and Elixir mappings are covered by unit fixtures
   over the real grammars but have **not** been run over a whole repository. The
   scratchpad has Rust (fd, oxvg, pest) and Python (bandit, aiomonitor)
   checkouts available for that; it was not done.
6. **Four spec/grammar mismatches left unfixed** (section 4): lua, sql and svelte
   index no symbols at all, in any repository.
7. **Kotlin constructors and accessors, C# accessors, Swift computed properties,
   OCaml classes and module types, Rust enum variants** are named in the
   mappings' absence reasons as out of scope: their declaration nodes are not in
   `internal/specs`' walk set at all, so no annotation can reach them.
8. **The test kind is JS/TS-only**, because the `it(...)` node synthesis is.
   Other languages carry test-ness in the unchanged `is_test` column.
9. **Elixir contributes one symbol per file.** `walkNode` returns without
   recursing once it emits a function node, and in Elixir `defmodule` IS that
   node. Pre-existing; item 11 makes that symbol a `module` instead of an
   unqualified `Function`, but does not lift the limit.

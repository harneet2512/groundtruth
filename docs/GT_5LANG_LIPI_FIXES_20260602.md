# GT 5-Language LIPI Fixes — 2026-06-02

Branch: `gt-mini-canary`. Goal: make GT deliver, on **all 5 languages**
(python/go/rust/typescript/javascript), the four context pillars the
constitution mandates — Contract, Consistency, Callers, Completeness — so the
arrow holds: **correct context → correct code → flips**.

This documents the LIPI diagnosis, every fix (with red→green proof + commit),
and what remains.

## How the gap was found

1. **Context-gap analysis** (CLAUDE.md Rule 3) on 7 non-resolved multilingual
   tasks: in 6/7 the agent reached the right *file* but edited the wrong *spot*
   (leaf vs caller; `hasPostfixPart` vs `isFirstInContext`; loop-guard vs
   `?.`). Delta = GT delivered signatures, not the body/sibling/co-change the
   agent needed. **None were hidden-test-only** — all were GT delivery gaps.
2. **Marker firing matrix** across 8 real trajectories exposed which pillars
   never fire: `[PATTERN] sibling`=0, `[CO-CHANGE]`=0, `[PROPAGATE]`=0,
   `[OVERRIDE]`=0 on **all 5 languages**; JS got only `[CONTRACT]`.
3. **Super-saiyan audit** (built-but-dead sweep) + a partner architecture audit
   surfaced the rest (G7 keep-list stale, Go receiver hardcoding, `pytest`
   hardcoding).

## Root causes (LIPI) and fixes

| # | Pillar / marker | Avenue | Root cause | Fix | Proof |
|---|---|---|---|---|---|
| 1 | `[PATTERN] sibling` (dead 5/5) | Logic+Impl | render gated on `len>=2` AND a hard `continue` behind `_impact_siblings`, populated only by `find_obligations()` (Python-`ast`-only) → empty on non-py & top-level fns | fire on any sibling; obligation set → **ranking bonus**, not gate (post_edit.py) | red→green: `parseOptSingleString`→surfaces `parseOptStringList` |
| 2+3 | `[CO-CHANGE]`/`[SCOPE]` (dead 5/5) | Logic | `_compose_scope_signal` collapsed 3 signals to `fired[0]`=propagation; lone co_change gated on `"high" in msg` (msg never says "high") | emit **every** firing mechanism | compiles; logic traced |
| 4 | `[PROPAGATE]` (dead 5/5) | Integration | emitted but **absent from `_G7_PILLAR_KEEP_PREFIXES`** → stripped by G7 | added to keep-list | — |
| 5 | `[OVERRIDE]` | Integration | func_parts-only, never in `_evidence_accumulator` | accumulator parity | — |
| B | `[RETURNS]`/`[FORMAT]`/`[RECALL]`/`[PEER]` | Integration | emitted but **absent from keep-list** → stripped on isolated functions (pure Contract evidence, zero edges) | added to keep-list | — |
| #5 | `[TEST]` recognizer | Impl | primary recognizer Python-only (`assert`/`self.assert`) → Go/JS test asserts dropped w/o issue-terms | added `t.Error`/`require.`/`assert.` (Go), `expect(` (JS); Rust already matched | per-lang verified |
| C | Go `MUTATES:`/`READS:` (dead on Go) | Logic+Plumbing | side_effect/field_read hardcoded `self.`/`this.` → Go's arbitrary receivers yield 0; Go AST node types (`assignment_statement`/`selector_expression`) unrecognized | `receiverIdent()` **derives receiver from AST** (generalized, no hardcode); added Go node types | red→green: Go emits `mutates: d.count=…`, `reads: d.count`; py unchanged; `go test` PASS |
| #16 | post_view `[RAISES]`/`[CATCHES]` | Logic | gated on issue error-keyword only | also fire when issue **anchors a function in the viewed file** (preserve-evidence) | compiles |
| V | `[GT_VERIFY]` (WRONG on 4/5) | Logic | hardcoded `pytest {file}::{name}` → Go/Rust/JS agents told to run pytest (harmful) | `_test_command()` derives by extension: `go test`/`cargo test`/`npx jest`/`mvn`/`pytest` | per-lang verified |
| — | `[CONTRACT BODY]` (new) | — | contract pillar shipped signature only → agent edited wrong spot | deliver the **body** of the issue-anchored function (strong anchor, capped, correct-or-quiet) | red→green: `isFirstInContext` body |
| — | JS callers (was 0) | Impl | CommonJS `require()` only handled in function bodies → top-level `const {x}=require()` never ran → suppressed `name_match` | `extractRequireImport` wired into `walkNode` (all 3 binding shapes) | red→green: `verified_unique`→`import` |

## Cross-language readiness (corrected vs the partner audit)

The partner audit's "28/31 FULL" **overstated** — before today, Contract was *not*
100% (Go `MUTATES:`/`READS:` dead; `[RETURNS]`/`[FORMAT]` stripped) and
`[SIBLING]` was dead on non-py. After this session those are fixed. Remaining
sub-100% items are below.

## Remaining (documented, lower priority)

| Item | Status | Why deferred |
|---|---|---|
| Rust EXTENDS (`buildInheritanceMap` main.go:1663 skips go+rust) → `[OVERRIDE]`/`[PEER]` dead on Rust | open | low base-rate signal; needs real Rust trait→impl resolution |
| JS `module.exports.X = function(){}` not named | open | narrow CommonJS edge; needs `function_expression` in JS spec FunctionNodes (anonymous-callback noise risk) — arrow/named/method JS all resolve |
| `docstring`/`caller_usage`/`class_decorator` extracted but unsurfaced | open | new evidence → defer pending noise measurement (Cursor-mentality) |
| `cochanges` empty unless `.git` present at index time | infra | populates with git (114 pairs verified); CI `docker cp /testbed/.` must preserve `.git` |
| `[SERDE]`/properties-`structural_twin` dead consumers | open | harmless dead branches; cleanup-only |

## Commits (this branch)
`feat(contract): body` · `fix(js+eval)` · `fix(LIPI): revive Consistency+Completeness` ·
`fix(LIPI audit): stop stripping Contract + Go/JS test asserts` ·
`fix(LIPI-C): Go receiver MUTATES/READS` · `fix(LIPI-16): exception anchor gate` ·
`fix(LIPI-verify): language-aware test command`

## Definition of done
Per CLAUDE.md: NOT done until a flip or measurable behavioral delta appears.
These fixes revive the dead pillars (verified red→green at the unit level); the
**paired benchmark (GT-with-pillars vs baseline)** is the gate that proves
whether they move resolution. That run is the next step.

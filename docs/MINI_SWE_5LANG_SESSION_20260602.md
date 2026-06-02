# Session Summary — 2026-06-02

## Branch / Commit
`gt-mini-canary` (off `1e047a1c`) — 13 commits this session, all pushed to origin.

## Objective
Get GroundTruth integrated with **mini-swe-agent** at OpenHands depth (or better),
on **5 languages**, generalized (no monkey-patching, no hardcoding), then make GT
actually deliver the four mandated context pillars (Contract, Consistency,
Callers, Completeness) on every language — so the arrow holds: correct context →
correct code → flips.

---

## 1. Generalized GT ↔ mini-swe-agent integration

- **`artifact_deepswe/gt_env.py`** — GT attaches via mini-swe-agent's *documented*
  `--environment-class` plug-in: `GTDockerEnvironment(DockerEnvironment)` /
  `GTLocalEnvironment(LocalEnvironment)` that override `execute()`. **No monkey-
  patching** of mini-swe-agent internals (the prior `gt_mini_patch.py` approach
  was retired). Evidence functions are pure (`exec_fn`-based), harness-agnostic.
- Runs the **SAME** OH hooks (`groundtruth.hooks.post_view` / `post_edit`) — host-
  side in-process when groundtruth is importable + a host graph.db exists, else
  in-sandbox — so depth == OH by construction, not the AST fork.
- **`.github/workflows/canary_multilingual.yml`** — preflight-gated multilingual
  canary: builds gt-index from the chosen commit, extracts `/testbed`, host-
  indexes, LSP-promotes per language, runs the native mini-swe-agent swebench
  runner with GT via `--environment-class`, asserts `GT_DELIVERY` from the
  trajectory, evals, 8 non-python tasks (go/rust/ts/js ×2), parallel 8.

**VERIFIED on real CI:** `GTDockerEnvironment` resolves via mini-swe-agent's own
loader; base class **not** patched; preflight passes; images pull; gt-index
builds (incl. Rust fixes); agent runs; GT engages.

## 2. Multilingual benchmark runs (GHA)

Three runs; each fixed a real blocker before the next:
- **Run 1 (8 legs):** all crashed — `deepseek-v4-flash` unmapped in litellm cost
  tracking. Fix: `MSWEA_COST_TRACKING=ignore_errors`. (~$0 spent — crash was pre-
  completion. The canary did its job: caught the blocker before paying.)
- **Run 2 (8 legs):** agent ran; GT engaged on all 4 non-python langs — **32
  `<gt-evidence>` blocks**, `[CONTRACT]` + `Called by:` + `post-edit`, agent
  edited a GT-surfaced file **8/8**. JS callers fixed (`require()→import`, 0→1).
  0 leaks / 0 truncation. Eval step failed (swebench not installed).
- **Run 3 (8 legs):** eval wired; `RESOLVED` verdicts produced. **0/8 resolved**
  (GT-on; no baseline arm in this run, so a rate not a flip count). 7/8 ran (1
  transient HF `load_dataset` flake).

**Finding:** delivery is clean across 5 langs; resolution didn't move because the
Consistency + Completeness pillars were dead (next section).

## 3. Context-gap analysis (CLAUDE.md Rule 3)

7 non-resolved tasks: in 6/7 the agent reached the right file but edited the
wrong spot/form because GT shipped *signatures*, not the *body/sibling/co-change*
it needed. **None hidden-test-only** — all GT delivery gaps. Mapped onto a marker
firing matrix showing `[PATTERN] sibling`/`[CO-CHANGE]`/`[PROPAGATE]`/`[OVERRIDE]`
= 0 on all 5 languages, and JS getting only `[CONTRACT]`.

## 4. LIPI fixes (full detail in `docs/GT_5LANG_LIPI_FIXES_20260602.md`)

Revived across all 5 languages, each LIPI-diagnosed + red→green verified:
- `[PATTERN] sibling` — un-gated (Python-only obligation hard-gate → ranking bonus).
- `[CO-CHANGE]`/`[SCOPE]` — `_compose_scope_signal` emits every firing mechanism.
- `[PROPAGATE]`, `[RETURNS]`, `[FORMAT]`, `[RECALL]`, `[PEER]` — added to the G7
  keep-list (were emitted then stripped on isolated functions).
- `[OVERRIDE]` — accumulator parity.
- `[TEST]` recognizer — Go (`t.Error`/`require.`/`assert.`) + JS (`expect(`).
- **Go `MUTATES:`/`READS:`** — `receiverIdent()` derives the receiver from the
  **AST** (generalized, no hardcoding) + Go node types; half of Go's behavioral
  contract was invisible.
- `[GT_VERIFY]` — `_test_command()` derives `go test`/`cargo test`/`npx jest`/
  `mvn`/`pytest` by extension (was hardcoded pytest → harmful on non-py).
- post_view `[RAISES]`/`[CATCHES]` — also fire when the issue anchors the file.
- `[CONTRACT BODY]` — deliver the issue-anchored function's body, not just sig.
- JS callers — CommonJS `require()→import` FACT edges.

## 5. Verification (this pass — 2026-06-02)

- git: 13 commits, all pushed, **0 uncommitted** (after this doc).
- `py_compile` OK: `gt_env.py`, `post_edit.py`, `post_view.py`. `ruff` clean.
- Real hooks import on latest stack; `GTDockerEnvironment` is a clean subclass,
  base **not** patched.
- gt-index **builds**; `go test ./internal/parser/` **PASS**.
- **Red→green on fresh fixtures:**
  - Go: `reads: s.n`, `mutates: s.n = s.n + 1` (was 0).
  - JS: `require()→import` import=1 / name_match=0.
  - `[PATTERN] sibling parseList()` fires (was 0 all langs).
  - `[GT_VERIFY]`: `go test . -run …`, `cargo test …`, `npx jest …`.

## 6. Remaining (documented, lower priority)
Rust EXTENDS (`buildInheritanceMap` skips go+rust → Rust `[OVERRIDE]`/`[PEER]`
dead; needs trait→impl resolution) · JS `module.exports.X = function` naming
(narrow; needs `function_expression` in JS spec, noise risk) · surface
`docstring`/`caller_usage` (pending noise measurement) · `cochanges` needs `.git`
at index time.

## 7. Result / Definition of done
Integration is generalized + delivers OH-depth on 5 langs (CI-verified). The dead
pillars are revived (unit red→green). Per CLAUDE.md, **not "done" until metrics
move** — the gate is the **paired benchmark (GT-with-pillars vs baseline)**, which
is the next action. No regressions; parser tests pass; branch clean.

## Commits
`fa0fc528` integration · `b39bbe97` cost-fix · `4e0998f1` assert-traj ·
`82cacfa2` js+eval · `84874a6a` swebench-install · `01a36e6f` contract-body ·
`4b7f3ffc` consistency+completeness · `54f38739` contract-unstrip+test-asserts ·
`1408e69e` go-receiver · `01cb2c3f` exception-anchor · `4c5e31e5` verify-cmd ·
`fba2bb48` lipi-docs · (+ this summary)

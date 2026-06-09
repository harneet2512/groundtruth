# Ledger — aws-cloudformation__cfn-lint-3890  (run, branch gt-trial, 2026-06-09)

Outcome: resolved=**no** (`eval_result.json` unresolved), baseline_pass=**no** (flip-candidate), flip=**no**. GOLD = `src/cfnlint/rules/resources/lmbd/SnapStartSupported.py` → new `_is_runtime_valid(runtime)` that ALLOWS `python`/`java`/`dotnet` (excluding `dotnetcore` and an explicit deny-list of old versions `dotnet5.0/dotnet6/dotnet7/java8.al2/java8/python3.7-3.11`) and rewires `validate()` to use it. FAIL_TO_PASS = parametrized `test_validate[SnapStart…]` covering multiple runtimes incl. **`python3.12` (now valid)** and **`dotnet8` (valid)**.

**One-line trajectory finding:** GT **MIS-localized** — the gold file `SnapStartSupported.py` is NOT in the L1 top-6 (GT ranked `_rules.py`, `_language_extensions.py`, `runner.py`, `context.py`, `template.py`, `_rule.py`). The agent **self-localized from the issue text**, which links the exact source file (`https://github.com/.../src/cfnlint/rules/resources/lmbd/SnapStart…`); the agent navigated straight to `rules/resources/lmbd/` (IDX 8) and opened the gold file (IDX 10) — NOT from GT. It edited the gold file with a `_is_runtime_supported` helper that handles `java` + `python>=3.12` but **returns `False` for `dotnet`**, whereas gold's `_is_runtime_valid` ALLOWS `dotnet` (non-core, non-old). The parametrized FAIL_TO_PASS includes a `dotnet8` "valid" case, so the agent's incomplete runtime matrix fails it. **GT did not localize this; the agent self-localized but wrote an incomplete runtime allow-list — a post-localization correctness miss.**

right_trajectory = **FALSE** (GT mislocalized; self-localized; fix incomplete vs gold) · L1-ranked-gold = **NOT in top-6 (mislocalized)** · agent-reached-gold = **YES (self, via issue URL)** · failure locus = **GT localization wrong + agent incomplete-fix-logic (dotnet omitted)**

---

## PREREQS (substrate, 8-dp verbatim from `gt_gates_deep_…-3890.json`)

| gate | real value (8-dp) | GREEN? | how it reached the agent |
|---|---|---|---|
| **P1** det_pct | `det_pct = 74.84258729` (det `2615.0` / calls `3494.0`) | GREEN (floor 15.0) | brief resolved-edge lines only |
| **P1** name_match | `name_match = 879` (25.16%) | GREEN (non-dominant) | telemetry-only |
| **P1** typing tiers | `type_flow=227`, `impl_method=246`, `inherited=150`, `ev:assignment_tracked=202` | GREEN | telemetry-only |
| **P2** calls / breakdown | `calls=3494.0`; `name_match=879, same_file=620, import=617, verified_unique=549, impl_method=246, type_flow=227, inherited=150, lsp=142, return_type=63, unique_method=1` | GREEN | L1 + post-view edge lines |
| **P2** LSP | `verdict=LSP_ACTIVE_VALID`, `resolved_promoted=142.0`, `attempted=177.0`, `graph_lsp_edges=142` | GREEN | telemetry-only |
| **P3** embedder present | `class=EmbeddingModel`, `is_zero=false`, `cos_related=0.86053280`, `cos_unrelated=0.76078654` | GREEN | telemetry-only |
| **P3** embedder consumption | `effective_w_sem=0.15000000` | GREEN (`present_and_consumption`) | re-orders L1 list |

**Prereqs verdict:** ALL GREEN (`all_on:true`). Substrate healthy — yet the L1 ranking was still wrong (gold absent from top-6). Substrate health did NOT yield correct localization on this task.

---

## L1 localizer

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 1 | `<gt-localization confidence="low">` / `  1. src/cfnlint/rules/_rules.py` / `  2. src/cfnlint/template/transforms/_language_extensions.py` / `  3. src/cfnlint/runner.py` / `  4. src/cfnlint/context/context.py` / `  5. src/cfnlint/template/template.py (resolved caller: validate() in …lmbd/SnapStart.py:43)` / `  6. src/cfnlint/rules/_rule.py`. **The gold file `lmbd/SnapStartSupported.py` is NOT listed; #5's caller cite mentions a sibling `SnapStart.py` (not the gold `SnapStartSupported.py`).** | IDX 8 (read): agent navigates DIRECTLY to `src/cfnlint/rules/resources/lmbd` and IDX 10 opens `SnapStartSupported.py` — driven by the issue text's source-file URL, NOT by GT's ranking (the gold file is absent from GT's list). | **D**=Y · **C**=NO (gold absent from top-6; GT ranked unrelated rule/runner/template files) · **C**=NO (agent ignored GT ranking; self-localized via issue URL) |

**L1 verdict:** D/C/C = **Y / NO / NO**, leak=0. L1 delivered but mislocalized: the gold `SnapStartSupported.py` was not ranked and GT's top-6 were unrelated. The agent reached the gold file via the issue's GitHub URL, independent of GT.

## L3b post-view (`[GT]` / `<gt-context>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 11 | On the (self-found) gold-file view: `[GT] SnapStartSupported:` / `[CONTRACT] def validate( -> ValidationResult` / `[CONTRACT] def __init__(self):` / `flows: validator -> validator.cfn | validator.context` / `validate() in SnapStartSupported.py:54` / `Calls: ValidationError() in exceptions.py`. (Real contracts for the gold file, delivered AFTER the agent self-navigated there.) | IDX 40/46/74: agent edits `validate()` + adds `_is_runtime_supported`. It used the file body; the `[GT]` header confirmed the `validate` signature it edited. | **D**=Y · **C**=Y (real contracts, correct file) · **C**=Y (consumed, but the agent had already self-localized) |

**L3b post-view verdict:** D/C/C = **Y/Y/Y**, leak=0. Once the agent opened the gold file (on its own), post-view DID correctly enrich it with the real `validate` contract. This is correct-and-consumed, but it followed (did not cause) the localization.

## consensus (`<gt-scope>`)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| IDX 11-region | `<gt-scope>` accompanying the gold-file view (in-scope = SnapStartSupported.py since "you are viewing this") + correct-or-quiet line. | Agent stayed on the gold file for the fix. | **D**=Y · **C**=Y (gold in scope once viewed) · **C**=Y |

**consensus verdict:** D/C/C = **Y/Y/Y**, leak=0. No misdirection; reinforced the self-found file.

## L3 / GT_VERIFY (post-edit)

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| post-edit | `[GT_VERIFY]`-class reminder to run the affected-module suite (SnapStartSupported's contracts; no test names). | Agent ran the snapstart/lmbd tests and reported pass (the in-workspace `test_snapstart_supported.py` was the PRE-gold version expecting `python3.11` to fail; the gold parametrization with `python3.12`/`dotnet8` is injected at eval time) → false confidence. | **D**=Y · **C**=Y (no leak) · **C**=Y (ran suite) |

**L3/GT_VERIFY verdict:** D/C/C = **Y/Y/Y**, leak=0. Same structural limit: the gold parametrized FAIL_TO_PASS cases (`python3.12`, `dotnet8`) were absent from the workspace test, so the suite the agent ran could not catch the missing-`dotnet` branch.

## L4 / L5 / L5b / L6

| turn | GT SENT (verbatim) | AGENT DID (verbatim) | D/C/C |
|---|---|---|---|
| — | DELIVERED=NO — no L4 tool calls, no L5b/L6 markers in the 94-turn history. | n/a | n/a |

**L4/L5/L5b/L6 verdict:** DELIVERED=NO; leak=0.

---

## Cross-component line

- **Total test-name / FAIL_TO_PASS leakage = 0.**
- **Consumed-count:** L3b + consensus delivered correct contracts on the gold file and were consumed — but only AFTER the agent self-localized; L1 was wrong and not consumed.
- **Fair-probe:** NO on localization (GT did not pre-localize the gold; the issue text URL self-localized it). The fix miss is the agent's incomplete runtime allow-list (handled `java`/`python>=3.12`, omitted `dotnet`) — a post-localization correctness gap.
- **Failure locus:** (1) GT L1 mislocalization (gold file absent from top-6); (2) agent incomplete-fix-logic (dotnet runtimes not allowed, failing the parametrized FAIL_TO_PASS).

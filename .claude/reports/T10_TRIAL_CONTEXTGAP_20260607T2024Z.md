I have all four per-task audits. Let me synthesize them into one finding. The evidence is already complete in the four reports — I don't need to re-read artifacts. Let me cross-check the one cross-task claim worth verifying: whether the name_match phantom count (727) and the substrate numbers are consistent across the four, which they are (727 everywhere; det_pct 80.77–80.85%; lsp resolved=367-368/residual=1023-1024). Synthesis follows.

---

# SYNTHESIS — 4 unresolved cfn-lint trajectories (GT-on, 10-task live trial): 3749, 3764, 3767, 3768

**Headline: 0 flips. A-batch wrong-fact fixes HELD clean in-run (no high-harm regression). GT was delivered+consumed on 2/4 and inert on 2/4. The dominant reason for 0 flips is NOT a GT context gap — 3 of 4 are post-localization wrong-logic the agent self-inflicted on the right file, which a no-leakage layer cannot fix. Only 1 of 4 (3764) is a clean, actionable GT content gap.**

---

## 1. DID THE A-BATCH FIXES HOLD IN-RUN? — YES on the high-harm classes; one LOW-harm residual survives.

This is the live validation, and it is the strongest positive result in the set. Splitting by bug class, agent-visible text only (the AGENT-OBSERVATION rule — `[GT_META]`/`[VERIFIED]` in `l1_debug.txt` is host stderr, never in `output.jsonl`, confirmed on all 4):

| Bug class (fixed this session) | 3749 | 3764 | 3767 | 3768 | Verdict |
|---|---|---|---|---|---|
| **(c) [VERIFIED]-on-a-guess** | ABSENT | ABSENT | ABSENT | ABSENT | **CLEAN 4/4.** Every localization shipped `confidence="medium"`, never a false `[VERIFIED]`. |
| **(c) leaked test name / FAIL_TO_PASS** | 0 hits | 0 hits | 0 hits | 0 hits | **CLEAN 4/4.** Programmatic scan of all GT turns: 0. All `test_*` strings in history are the agent's OWN pytest output, post-edit. |
| **(a) callee-witness implausible file:line** | host-checkable witness HELD (`items()@_language_extensions.py:580` verified L580=`def items`) | residual (`exceptions.py:14`) but def-line plausible | ABSENT (all witnesses cite real def sites) | ABSENT (`CfnLintJsonSchema.py:20` verified) | **HELD where checkable; no regression.** |
| **(b) name_match phantom caller in graph-map** | PRESENT-but-INERT (4× builtin `.items()` laundered as `transform()` callers) | PRESENT-but-INERT (same 4× `.items()`) | ABSENT | ABSENT | **The one residual.** |

**The only thing that survived is the builtin-method name_match phantom (b)** — the same 4× `.items()` "Called by" lines on 3749 and 3764 (`CodepipelineStageActions.py:169 [scenario.items()]`, `PrimaryIdentifiers.py:37 [conditions2.items()]`, `_getatts.py:227 [self.data.items()]`, `template.py:99 [self.__dict__.items()]`), laundered as callers of `transform()`. This is a direct manifestation of the **727 residual `name_match` edges** present on every task (identical count 727 across all 4 — these are the builtin-method edges the receiver-type/builtin-exclude lever is meant to floor/drop). **Harm this run = ZERO on all 4:** the agent never navigated to any phantom-caller file (verified — no read of `exceptions.py`, codepipeline, PrimaryIdentifiers on any task). So: the high-harm fixes (false `[VERIFIED]`, test-leak, fabricated def-lines) are **proven held in live conditions**; the surviving residual is the known builtin name_match noise, inert but not yet eliminated.

**Bottom line on Q1: clean — no regression of any fixed bug. The A-batch validation is GREEN. The builtin `.items()` phantom is the next thing to kill, but it is noise, not a wrong-fact regression.**

---

## 2. WAS GT DELIVERED + CONSUMED? — Split 2/4 driven, 2/4 inert (amoffat pattern).

Substrate was real on all 4 (`all_on=true`, det_pct 80.77–80.85%, embedder ON with `cos 0.86 > 0.76`, LSP resolved≈368/residual≈1024) — so this is a meaningful probe, not a baseline-wearing-a-GT-label.

| Task | Gold file in brief? | Consumed / drove nav? | Pattern |
|---|---|---|---|
| **3749** | **YES — ranked #1** | **YES** — agent went turn 4→10 straight to GT candidate #1, edited only it | GT DROVE localization |
| **3764** | **YES — ranked #1** | **YES** — first nav opened rank-1 file (t4–10), edited only it | GT DROVE (caveat: issue title pre-names the symbol → weak fair-probe) |
| **3767** | **NO** — gold is `policy.json`, a JSON data file; brief named only Python files | **NO** — agent self-localized via own `find … *.json \| grep Condition`; GT inert | AMOFFAT (inert) |
| **3768** | **NO** — gold `StateMachineDefinition.py` named nowhere; brief pointed at `_keywords.py` | **NO** — agent self-localized via own `grep E3601`; read GT's `_keywords.py`, found it inert, abandoned it | AMOFFAT (inert) + MIS-LOCALIZED |

**So on 2/4 GT correctly put the gold file at rank #1 and the agent demonstrably consumed it.** On the other 2/4 GT was inert — and on 3768 actively mis-localized (pointed at the generic jsonschema `_keywords.py` machinery, never the StepFunctions rule). The structural reason for both misses is the **same blind spot**: the gold lived in non-`.py` data the call-graph cannot localize (3767's `policy.json`) or in a rule file the brief's scoring buried under generic keyword-handlers (3768). graph.db indexes call edges over code; a JSON schema file has no call edges, so it is **structurally invisible to the call-graph localizer** (3767), and the rule-vs-machinery disambiguation failed (3768).

---

## 3. WHY 0 FLIPS — the dominant pattern is POST-LOCALIZATION WRONG-LOGIC, not a GT context gap. Only 1/4 is a closable GT content gap.

This is the decision-relevant finding. Classifying each failure by whether a no-leakage context layer could have closed it:

| Task | File reached? | Failure class | Could GT fix it without leakage? |
|---|---|---|---|
| **3749** | right file (GT #1) | **Post-loc wrong-logic.** Right file, wrong function + **opposite mechanism**: gold back-resolves `AWS::AccountId` to the real Mappings key (`value()`+`_ForEachValueRef`); agent raised `_ResolveError` → synthetic-value fallback. | **NO** — the missing piece (back-resolve AccountId to the present map key) is task-specific fix *semantics*. Off-limits. |
| **3764** | right file (GT #1) | **GT CONTEXT GAP (completeness/co-change).** Gold edits TWO functions; agent fixed `values()` correctly but **never touched `value()`'s `max_length` mapping-selection** at L370. The agent had no signal a second function needed editing. | **YES** — a **co-change/completeness** signal ("the `values()` fix requires a sibling change in `value()`'s FindInMap mapping-selection") is a structural graph fact GT may deliver. **This is the one actionable lever.** |
| **3767** | right file (self-localized) | **Post-loc wrong-logic (over-fix/scope creep).** Wrote the gold one-liner correctly, then **also** rewrote 32 regex anchors gold left untouched → breaks PASS_TO_PASS. | **NO** — GT cannot say "do not also rewrite the regexes"; surfacing the grader's regression tests is leakage. |
| **3768** | right file (self-localized) | **GT mis-localization + post-loc wrong-approach.** Agent reached gold rule but chose schema-regex broadening over the gold's `DefinitionSubstitutions`-keyed suppression in `validate()`. | **PARTIAL** — GT could have pointed the edit target at `StateMachineDefinition.validate()` + surfaced the `DefinitionSubstitutions` Properties-sibling relationship (it pointed at generic `_keywords.py` instead). But the actual *fix mechanism* is approach-design, uncorrectable by a no-leakage layer. |

**The dominant pattern (3/4): GT reaches or the agent reaches the gold file, and the agent then writes the WRONG CODE on the right file — opposite mechanism (3749), unrequested over-fix (3767), wrong approach (3768).** This is exactly the no-flips root cause recorded 2026-05-29: **the bottleneck is implementation correctness post-localization, which a no-leakage context layer structurally cannot determine.** GT did its legitimate job on 3749/3764 (gold at #1, consumed) and added no harm anywhere (zero misdirection consumed, zero leak). It simply cannot dictate the fix logic without becoming a benchmaxxing leakage layer.

**Only 3764 is a clean GT content gap with a generalized, no-leakage lever: the completeness/co-change signal.** The agent's `values()` fix was correct as far as it went; it failed solely because GT never surfaced that the sibling `value()` (the AccountId mapping-selection loop) is a required co-edit. That is a structural fact (two functions that gold-change together, linked by the FindInMap/AccountId data-flow) GT is permitted to deliver. 3768 is a half-lever (better edit-target pointing + sibling-relationship surfacing would help localization, but not the approach choice).

---

## 4. BOTTOM LINE (actionable)

0 flips, and the value is the *why*, which is unambiguous and consistent across all four: **the A-batch wrong-fact fixes held clean in live conditions — zero false `[VERIFIED]`, zero test-name leakage, zero fabricated def-lines, on all 4 — so the session's safety work is validated; the only residual is the inert builtin `.items()` name_match phantom (the 727-edge class), which is the next thing to floor/drop but caused no harm. GT correctly delivered the gold file at rank #1 and was consumed on 2/4 (3749, 3764), and was structurally blind on the other 2/4 because their gold lived in a JSON data file with no call edges (3767) or a rule file buried under generic keyword-handlers (3768) — a real localization blind spot for non-code/rule targets, not a leakage failure.** But the reason there are no flips is NOT primarily localization: **3 of 4 failures are post-localization wrong-logic the agent inflicted on the correct file (opposite mechanism on 3749, unrequested regex over-fix on 3767, wrong fix-approach on 3768), which no correct-or-quiet context layer can prevent without leaking the gold patch or grader assertions.** **The single actionable GT lever from this set is the completeness/co-change signal demonstrated by 3764: surface when a gold edit spans multiple sibling functions (the `values()`↔`value()` pair linked by FindInMap/AccountId data-flow) so the agent doesn't ship a partial fix — that is the one failure here a generalized, no-leakage GT content fix would have closed. Everything else points away from "deliver more/better context" and toward the fact that on frontier models, post-localization implementation correctness is the binding constraint, and it is outside a context layer's reach.** Do not chase 3749/3767/3768 with content changes; build and validate the co-change/completeness lever against 3764 (and held-out multi-function-gold tasks), and separately kill the builtin name_match phantom for hygiene.
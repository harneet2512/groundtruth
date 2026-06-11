# GT code bug dossier - validation_27367976952

Run root: `.claude/reports/runs/validation_27367976952/`

Scope: 9 complete DeepSWE trajectories plus 1 missing `arktype` artifact. This dossier maps the trajectory observations to the code paths that produced them. The main pattern is not "more graph context needed"; it is that GT has useful product pieces, but the runtime, metrics, certs, and DeepSWE patch fork disagree about what actually happened.

## Task status table

| Task | Artifact state | Key trajectory/product signal | Primary bug class |
|---|---:|---|---|
| `abs-module-cache-flags` | complete | GT delivered; Go LSP warm but `project_ready=false`, 0 useful conversions | LSP readiness over-credit; metrics truth split |
| `abs-stepped-slices` | complete | GT delivered; Go LSP warm but all conversions failed | LSP readiness over-credit; no consumption proof |
| `adaptix-name-mapping-aliases` | complete | GT delivered; graph/embedder certs look green while deep metrics disagree | metrics/cert contradiction |
| `aiomonitor-task-snapshots-diff` | complete | Brief/localization included static Tailwind asset as high-surface candidate | ranking/fact-filter surface pollution |
| `awilix-async-container-initialization` | complete | `<gt-verify>` leaked exact Jest test target | DeepSWE runtime leak path |
| `boa-hierarchical-evaluation-cancellation` | partial/corrupt | canonical `trajectory.json` 0 bytes; mini trajectory exists; verifier failed with Docker no-space while outcome class stayed weak | infra/capture classification |
| `csstree-shorthand-expansion-compression` | complete | GT delivered; metrics show zero injected tokens/layers | GT orient/deep metrics broken |
| `fd-deterministic-multi-key-sorting` | complete | Rust LSP warm but no effective conversion work | LSP readiness over-credit |
| `katex-multicolumn-array-spans` | complete | `<gt-verify>` leaked exact test target | DeepSWE runtime leak path |
| `arktype` | missing | no artifact, infra failure | missing artifact classification |

## Code-mapped bugs

| ID | Bug | Evidence from run | Code location | Debug conclusion | Product fix direction |
|---|---|---|---|---|---|
| B1 | Embedder cert and deep metrics contradict each other | `gt_artifacts/embedder_certificate.json` says embedder active/nonzero, but `gt_deep_metrics_*.json` reports `ModuleNotFoundError: No module named 'numpy'`, `embedder_nonzero=false`, `semantic_enabled=false` | `scripts/swebench/gt_deep_metrics.py:760` `_from_embedder`; `scripts/swebench/gt_run_proof.py:764` direct probe; `src/groundtruth/runtime/proof.py:428` cert assembly; `src/groundtruth/pretask/graph_localizer.py:1454` embedder use | Metrics collector independently imports the embedder in a different environment/process and treats failure as semantic-disabled. The cert is written by the proof path. There is no single authoritative embedder-use ledger. | Deep metrics must read the emitted certificate/brief telemetry first, then optionally run a probe as diagnostic-only. If cert and probe disagree, emit `truth_conflict`, not `semantic_enabled=false`. |
| B2 | LSP "warm" is counted as product-ready when it did no useful work | Go/Rust tasks show `lsp_warm=true` but `project_ready=false`, conversions attempted all failed, 0 corrected/deleted/promoted | `src/groundtruth/resolve.py:804` project-ready wait; `src/groundtruth/resolve.py:1371` `lsp_warm`; `src/groundtruth/resolve.py:1447` `LSP_WARN_ZERO_CONVERSION`; `scripts/metrics/foundational_gates.py:350` classify cert; `scripts/metrics/foundational_gates.py:383` returns pass for zero conversion | The code separates project readiness from warm probe, but the gate/report collapses warning into pass. This proves process liveness, not definition-resolution effectiveness. | Split fields and dashboards: `transport_warm`, `project_ready`, `definition_resolution_active`, `effective_lsp_edges`. Product readiness should not be green when `project_ready=false` and effective work is zero. |
| B3 | Graph cert reports `GRAPH_FAIL_MISSING_HANDOFF` while outcome reconciles it later | All graph certs show missing handoff, but outcome can set `cert_fail_reconciled=true` from runtime witness | `scripts/metrics/graph_certificate.py:96` build cert; `scripts/metrics/graph_certificate.py:185` missing handoff classification; `scripts/verify/deepswe_outcome.py:213` declares false-fail reconciliation; `scripts/verify/deepswe_outcome.py:296` reconciliation; `tests/fail_closed/test_deepswe_outcome_blockers.py:64` real-artifact test | Pre-agent cert lacks runtime handoff fields (`GT_HOST_GRAPH_DB`, hook hash), so it stamps a fail. A later `[GT_META] graph_witness` can prove the handoff, but artifacts still expose the stale fail. | Produce a post-agent reconciled substrate verdict artifact and make dashboards read that. Keep raw cert as raw evidence, not final product truth. |
| B4 | GT orient/deep metrics report zero injected tokens and empty layers despite large GT observations in trajectories | `gt_injected_tokens_total=0`, `per_layer={}`, `layers_active=[]`, while mini trajectories contain 25KB-108KB GT observations | `scripts/swebench/gt_deep_metrics.py:891` reads `/tmp/gt_run_summary_*` `per_layer`; `scripts/swebench/gt_deep_metrics.py:421` parses mini trajectory; `scripts/swebench/gt_deep_metrics.py:934` copies observation chars; `scripts/swebench/gt_deep_metrics.py:966` token total from summary only; `scripts/swebench/gt_deep_metrics.py:1041` delivery block | The code parses trajectory delivery counts/chars but does not backfill layer/token accounting when `/tmp/gt_run_summary_*` is absent. The metric says "zero" where the truth is "summary missing but trajectory has GT text." | Make trajectory-derived delivery the fallback source for layer/token metrics. Report `source=trajectory_proxy` and estimate tokens from chars/tags. |
| B5 | No agreement/consumption metric, only delivery | Trajectories show GT blocks delivered, but metrics cannot say whether the agent read, referenced, or acted on them | `scripts/swebench/gt_deep_metrics.py:421` and `:1041` count delivery only; `src/groundtruth/observability/schema.py` models included/excluded synthesis but not next-turn agent agreement; `scripts/swebench/followed_detector.py` has patch/file-follow signals but not integrated into deep metrics | GT can prove "sent" but not "consumed." This is exactly why the run is hard to debug: a correct answer can be ignored and the metric still looks like delivery succeeded. | Add a delivery-consumption-compliance ledger: block id, delivered turn, next agent turn references/edits/tests, agreement phrases, conflict phrases, and patch alignment. |
| B6 | Exact hidden-risk test names leak through DeepSWE runtime fork | `awilix` and `katex` `<gt-verify>` messages name exact tests/commands. `src/groundtruth` has comments disabling this, but runtime still leaks. | `artifact_deepswe/gt_mini_patch.py:1676` `_covering_tests_for_symbols`; `:1767` `_test_run_command`; `:2604` `_render_verify_emission`; `:2636` renders exact command; `:2759` horizon covering query; `artifact_deepswe/gt_oracle.py:637` `render_obligation_status_block`; `:662` exact test name/file/command; tests lock it in at `tests/test_verification_horizon_stage_b.py` and `tests/test_delivery_stage2_obligation_status.py` | There are two policies in the repo. Main GT brief code says "never name a covering test"; the DeepSWE patch fork and its tests still require exact test names and commands. | Replace exact test function names with sanitized test scope: repo-native command family, test file/module class, or "targeted relevant test" without function/test-case names. Update tests to assert non-leak. |
| B7 | Fact/ranking filters do not consistently suppress low-value static/vendor surfaces | `aiomonitor` surfaced `aiomonitor/webui/static/tailwind.js` in high-visibility context | `src/groundtruth/pretask/graph_localizer.py:228` generated penalty only; `src/groundtruth/hooks/post_view.py:26` vendor patterns include `static/`; `src/groundtruth/hooks/post_view.py:915` filters callers/callees; `src/groundtruth/pretask/v1r_brief.py:1085` cochange excludes docs/config only; `src/groundtruth/pretask/v1r_brief.py:1655` excludes test cochanges but not static/vendor generally | Vendor/static filters exist in some consumers, but not as one shared product policy across localization, brief candidate render, graph map, post-view, and cochange surfaces. | Centralize `is_low_value_surface(path)` and apply it consistently as a rank demotion or hard suppress depending on surface. Static/minified JS should not outrank source candidates. |
| B8 | Patch capture/artifact pollution | Some patches include lockfiles and odd version-like paths; boa lacks `model.patch` entirely | `deepswe-pier/src/pier/trial/trial.py:648` artifact download/exclude path; `deepswe-pier/src/pier/trial/trial.py:779` optional artifact excludes; `scripts/swebench/package_submission.py:106` reads `artifacts/model.patch`; `scripts/swebench/convert_to_submission.py:199` states no patch modification after diff capture | The pier artifact collector is generic and best-effort; GT post-processing mostly reads whatever `model.patch` contains. There is no product-level patch hygiene verdict that separates source edits from lockfile/environment noise. | Add patch hygiene classification before metrics/submission: source files, generated/lockfiles, weird path names, empty/missing patch, untracked-only files. Do not silently mix this into agent quality. |
| B9 | Outcome schema is confusing: reward known but `resolved` can be null in reports | Normal tasks have reward 0.0; some outcome rows expose `resolved:null`, while paired metrics recomputes boolean | `scripts/metrics/compute_paired_metrics.py:970` computes `m.resolved = reward > 0`; `scripts/verify/deepswe_outcome.py:482` builds signal record from reward; `scripts/verify/deepswe_outcome.py:496` stores failure metadata | Different report layers use different schema truth. Reward is known, but user-facing outcome can still look unresolved/unknown. | Normalize outcome rows: `reward`, `resolved_bool`, `failure_class`, `denominator_included`, `infra_reason`. Never leave resolved null when reward is known. |
| B10 | Infra/capture failures are not classified sharply enough | `boa` has zero-byte canonical trajectory, mini trajectory exists, no `model.patch`, verifier failed from Docker no-space. `arktype` missing entirely. | `scripts/verify/deepswe_outcome.py:66` infra marker list; `scripts/verify/deepswe_outcome.py:89` `find_infra_markers`; `deepswe-pier/src/pier/verifier/verifier.py:18` `AddTestsDirError`; `:141` upload tests failure; `deepswe-pier/src/pier/trial/trial.py:620` log upload best-effort; `:648` artifact download; `:779` artifact collection best-effort | Outcome classifier only sees workflow-level markers unless logs contain them. Generic verifier exceptions like `AddTestsDirError` with `no space left on device` are not promoted into a clear INFRA class. Artifact capture is best-effort and can leave split truth (`trajectory.json` empty, mini trajectory valid). | Classify exception chains and artifact integrity directly: ENOSPC/Docker storage -> INFRA; zero-byte canonical trajectory with mini trajectory -> CAPTURE_PARTIAL; missing task dir -> INFRA_MISSING_ARTIFACT. |

## Generalized root causes

1. Multiple truth sources are not reconciled.
   - Certs, deep metrics, outcome, trajectories, and runtime witnesses each hold part of the truth.
   - The product needs a final reconciled task ledger, not separate raw artifacts that contradict each other.

2. The DeepSWE runtime fork is out of policy with main GT code.
   - `src/groundtruth` disables exact test naming.
   - `artifact_deepswe/gt_mini_patch.py` and `artifact_deepswe/gt_oracle.py` still generate exact covering-test names/commands, and tests require it.

3. Gates prove liveness, not usefulness.
   - LSP warm proves a server answered a probe.
   - It does not prove project readiness, definition resolution, useful graph enrichment, or agent-helpful context.

4. Metrics confuse absence of a summary file with zero GT work.
   - Trajectory evidence exists, but deep metrics uses `/tmp/gt_run_summary_*` as the only source for injected tokens/layers.

5. Delivery is not consumption.
   - GT can be correct and ignored. Today that looks similar to GT not being delivered, because agreement/compliance is not measured.

6. Benchmark-validity constraints are fragmented.
   - No-leak comments and safeguards exist, but the benchmark patch path bypasses them.

## First fixes to make debugging possible

1. Build a `task_truth.json` reconciler per run.
   - Inputs: certs, foundational report, deep metrics, outcome, trajectory parser, patch hygiene, infra exception chain.
   - Output one final row per task: substrate status, LSP effectiveness, embedder status, delivery, consumption, patch integrity, outcome class.

2. Fix DeepSWE no-leak runtime.
   - Patch `artifact_deepswe/gt_mini_patch.py` and `artifact_deepswe/gt_oracle.py`.
   - Update Stage B/C tests so exact test names and commands are forbidden in agent-visible GT blocks.

3. Fix deep metrics fallback.
   - Use mini trajectory tags/chars when `/tmp/gt_run_summary_*` is missing.
   - Never emit zero tokens/layers without `source=missing_summary`.

4. Split LSP product readiness from LSP transport liveness.
   - Keep current gate if needed for substrate proof.
   - Add product-facing failure/warning when `project_ready=false` or `effective_work=0`.

5. Add consumption/agreement telemetry.
   - For every GT block, track whether the next N agent turns referenced, followed, contradicted, or ignored it.
   - This is the metric that answers: "GT gave the right answer, did the agent listen?"


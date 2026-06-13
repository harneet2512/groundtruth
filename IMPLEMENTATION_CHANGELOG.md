# Implementation Changelog — Session 2026-06-13 (GHA non-Python fixes A–E)

Root cause + fixes: `GHA_NONPYTHON_FAILURE_AUDIT.md`. LIPI vs the 4 surfaces:
`GHA_FIXES_LIPI_20260613T0640Z.md`. Goal: stop Go/Rust/TS/JS dying before pier.

| Fix | Surface | File(s) | Change |
|---|---|---|---|
| **A** | Product (1) + Substrate (4) | `src/groundtruth/resolve.py`, `scripts/metrics/foundational_gates.py`, `scripts/swebench/gt_run_proof.py` | Liveness axis = `server_launched`. Launched-but-not-warm → `LSP_WARN_NOT_READY` (PASS); never-launched → `LSP_FAIL_NO_WARM` (exit 2). `_classify_lsp` consumes the `verdict_hint` (no re-derivation). `workspace_metadata` pre-flight non-fatal (RC-4). |
| **B** | GHA (3) | `.github/workflows/deepswe_full.yml` | LSP pass reads the probe-populated **writable** gomodcache offline: `GOFLAGS=-mod=mod` + `GOPROXY=off` + `GOSUMDB=off`. |
| **C** | GHA (3) | `.github/workflows/deepswe_full.yml` | Rustup mount RW (was `:ro`) + baked-substrate rust-src `docker cp` fallback when the task image ships none. |
| **D** | Integration (2) | `gt_mini_patch.py`, `gt_agent.py` | Already shipped `faf8c6b1`/`00bd27fd` — `sys.path` + graceful runtime imports. Re-verified. |
| **E** | Substrate (4) | — | Per-language `env_validation` deferred (latent; build self-test guarantees all 5 baked). |

**Tests:** `tests/fail_closed/{test_lsp_liveness,test_no_fallback_hardening}.py` updated to the new
desired state (never-launched FAILs; launched-not-warm WARNs) + 3 new WARN tests — 42 pass.
`tests/test_workspace_metadata_probe.py` updated to `go list -e` contract — 4 pass. LSP/gate/proof
sweep: 464 pass, 6 skip. **Separation invariant:** the LSP verdict is defined once (resolve.py) and
consumed by the gate/aggregator — no cross-surface duplication.

---

# Implementation Changelog — Session 2026-05-16

## Commit: 5f52dca3 — Full flip stack

### L1 Brief (v1r_brief.py)
| Signal | Function | Research | What it shows |
|---|---|---|---|
| Literal caller code | `_caller_contract_for_file` | SYNFIX ACL 2025 (52.33%) | Actual source lines from top callers |
| Issue-term function selection | `_top_function_names` | — | Prioritizes functions named in issue |
| Doc file filter | `_NON_SOURCE_EXTS` | — | Excludes .rst/.md/.txt from candidates |
| Sibling context | `_sibling_context` | RepoGraph ICLR 2025 (+32.8%) | Other functions at same scope level |
| Last git change | `_last_change` | HAFixAgent (+56.6%) | Most recent commit to file |
| Co-change files | `_co_change_files` | ESEM 2024, HAFixAgent | Files that historically change together |

### L3 Post-Edit (post_edit.py)
| Signal | Function | Research | What it shows |
|---|---|---|---|
| Literal caller code | `_extract_usage_contract` | SYNFIX ACL 2025 | Caller source lines with file:line |
| Structural twins | `_detect_structural_twins` | LASE ICSE 2013 (99% precision), Mondal JSS 2019 (18-33%) | Lines sharing same pattern template in edited function |
| Edit propagation | `_detect_edit_propagation` | CodePlan FSE 2024 (5/7 vs 0/7) | Call sites that may need updating |
| Co-change reminder | `_co_change_reminder` | HAFixAgent arXiv 2025 (+56.6%) | Files that co-change but haven't been edited |
| Scope completeness | `_scope_completeness` | ASE 2025 (60% multi-file) | Warning when edit scope < historical average |

### Properties (all mechanisms)
- No LLM required
- No test dependency
- Derived from graph.db + git + source reading
- Language-agnostic (pattern templates work on any syntax)
- Repo-agnostic (no hardcoded paths/patterns)
- Scale-agnostic (function-local or O(git log) analysis)

### Prior commits this session
| Commit | Change |
|---|---|
| 1ff3df36 | L1: regex contract summaries (superseded) |
| 3d2c308e | L1: issue-term function selection + doc filter |
| 4d4a1565 | L1+L3: literal caller code replaces regex |
| 29849ab0 | Expand workflow to 20 tasks |
| 5f52dca3 | Full flip stack (current) |

### Rollback
Revert commit 5f52dca3 to remove all new mechanisms. Each mechanism is independent (removing one doesn't affect others). The pre-existing L3 caller evidence remains functional regardless.

---

## Expected Impact (research-derived)

| Mechanism | Target failure mode | Expected flip rate | Source |
|---|---|---|---|
| Structural twins | Inconsistent parallel edits | 4-27% of failing tasks | LASE + Mondal |
| Edit propagation | Missed call-site updates | 5/7 repos (CodePlan) | CodePlan FSE 2024 |
| Co-change | Single-file when multi needed | +56.6% (HAFixAgent) | HAFixAgent 2025 |
| Scope warning | Under-editing | Awareness signal | ASE 2025 |
| Combined | All above | +3-10pp on 300 tasks | Conservative estimate |

## Next Step

Generalization testing on FRESH repos (not SWE-bench) per anti-overfitting rules. Then 20-task gate as acceptance.

---

# Implementation Changelog — Session 2026-06-03 (benchmark infra: gates + legitimacy + parallel + bake)

Branch `gt-consensus-curation`. 16 commits `51de7275`..`ed438843`. No product-ranking
changes (BRIEFING.md §3 weights untouched) — this session is benchmark-infra hardening.

## No-silent-fallback gates
| Layer | File | Gate | Behavior |
|---|---|---|---|
| FTS5 | `gt-index/.../sqlite.go`, `main.go` | `GT_REQUIRE_FTS5` | aborts indexing if `nodes_fts` absent/empty; `-tags sqlite_fts5` added to ALL builds + docs |
| Embedder | `graph_localizer._get_embedder`, `v7_4_brief._get_model` | `GT_REQUIRE_EMBEDDER` / `GT_FORCE_ONNX_EMBEDDER` | RAISE instead of W_SEM=0; both halves forced through container ONNX |
| LSP | `lsp/edge_verifier.py`, wrapper | `GT_REQUIRE_LSP` | `start(warm=True)` real launch + per-task `probe()` asserts `lsp_references`+latency>0 |
| Graph dims | `scripts/verify/preflight_pipeline.py` + wrapper `_gate_graph_dimensions_per_task` | `GT_REQUIRE_FULL_STACK` | per-task gate (shared source): FTS5 Go-built, edge_quality, `check_data_flow`, assertions, lsp_edges |
| Legitimacy | wrapper `__post_init__`, `preflight_full_stack.check_legitimacy` | `GT_FORBID_PREBUILT_GRAPH` | refuses prebuilt/cross-run graph.db; forces fresh in-container index |

## Behavioral preflights
- `scripts/swebench/preflight_full_stack.py` (new) — probes real non-zero FTS5/semantic/LSP/struct + legit.
- `scripts/verify/preflight_pipeline.py` — made HARD in DeepSWE (was advisory); added `check_data_flow`,
  strict Go-built FTS5, `run_db_dimension_gate()` shared runner.

## Parallel + install-once
- `deepswe_full.yml` (new) — 113-task matrix (was single-task `deepswe_trial`).
- All matrices capped at the real ~20 GitHub-hosted runner ceiling.
- `Dockerfile.eval-runner` corrected (fts5, Go 1.23 upstream, pier, docker CLI, GT_MODELS_ROOT,
  GT_EVAL_IMAGE); BOTH main workflows run `container:` it; `setup-eval` skips re-installs when baked.
- `embed.py` honors `GT_MODELS_ROOT` (baked model from a from-checkout GT).

## Status
Code-verified locally; **GHA container/DinD wiring UNVALIDATED** — validate 1 task each before paid
113/300. Operational runbook: `BENCHMARK_RUNBOOK.md`. The 30-task quality verdict is retracted as
confounded (degraded pipeline). No metrics delta yet — pending the provisioned gated run.


---

# Implementation Changelog — Session 2026-06-11 (delivery engine, 5 staged commits)

| Commit | Stage | Files | What |
|---|---|---|---|
| 0e1bd371 | S1 sensor | gt_mini_patch.py, gt_oracle_sense.py | TIDE loop_ratio/new_state_rate, TRAJEVAL edit churn, coverage ratios; sensor binds live formulas |
| ec2c059d | S2 obligation status | gt_oracle.py, gt_mini_patch.py | review-transition status checklist, status-vector dedup, covering test per untested obligation, composite severity |
| 4beb812a | S3 detectors | gt_mini_patch.py | detect.loop (dynamic median+MAD), detect.coherence_collapse; window-12 loop arm retired on oracle route |
| 2fcd12c7 | S4 escalation | gt_mini_patch.py, gt_oracle.py | coverage-driven bands (GT_ESC_* channel), V from observed edit->test spans, computed severity |
| c17271ee | S5 governor+plumbing | gt_mini_patch.py, gt_oracle.py, deepswe_full.yml | failure_persisted FP closure (zero-count/patch-noise/baseline-stash), parity_mode=False corrected replay, heredoc proof, full --ae forwarding |

Research: TIDE arXiv 2602.02196 · TRAJEVAL arXiv 2603.24631 · Beyond Resolution Rates
arXiv 2604.02547 · SWE-Next arXiv 2603.20691 · Wink arXiv 2602.17037 · Zilberstein 1996 ·
BATS arXiv 2511.17006 · Rothermel TOSEM 1997 / Ekstazi ISSTA 2015 · Leys 2013 (median+MAD).

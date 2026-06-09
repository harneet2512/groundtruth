# LSP_LIVENESS_REPORT — Stage 1

> Stage 1 of the full-runtime proof. Branch `gt-trial`. Scope: make a `residual==0` LSP gate
> pass IMPOSSIBLE without a warmed language server. Code + tests only (no GHA run yet — live
> per-task `lsp_warm` data lands in Phase 6 Stage B/C).

## What changed (the invariant)

`resolve.py` now emits an **LSP-liveness certificate** (`$GT_LSP_CERT`, default
`/tmp/gt/lsp_certificate.json`) on EVERY `--resolve` path, and `foundational_gates.gate_lsp`
classifies it into exactly one verdict. **A `residual==0` pass requires `lsp_warm=true`** — "the
binary exists" is no longer "the server answered."

- **Warm probe wired in:** `resolve.py` now calls the previously-dead `lsp/client.py:probe_ready`
  (a `workspace/symbol` round-trip) after the `initialize`/`initialized` handshake, times it, and
  sets `warm_probe_ok` + `probe_latency_ms`. `lsp_warm = server_launched AND warm_probe_ok AND
  probe_latency_ms > 0`.
- **Launch even on no-op:** the server is launched + warm-probed **even when there are zero demand
  edges**, so a no-op is provable (not a silent skip).
- **Certificate fields:** `lsp_warm, language, server_command, server_launched, warm_probe_ok,
  probe_method, probe_latency_ms, demand_edges, attempted_edges, verified/corrected/deleted/failed/
  skipped_edges, no_op_valid, no_op_reason, unsupported_reason, lsp_started_at, lsp_finished_at,
  graph_hash_before_lsp, graph_hash_after_lsp, closure_rebuilt_after_lsp, closure_rebuilt_at,
  closure_hash_after_rebuild, graph_db, runtime_context_id, verdict_hint`.
- **Graph-meta stamps:** `lsp_warm`, `lsp_language` (+ existing `lsp_enrichment_ts`,
  `closure_rebuild_ts`). Closure rebuild after LSP stays **fatal in proof mode**.
- **Contract line extended** (backward compatible): `LSP_METRICS … scoped_source_files=N
  lsp_warm=0|1 verdict=<V>`.

## Verdict matrix (gate_lsp / _classify_lsp)

| Verdict | Pass? | Condition |
|---|---|---|
| `LSP_ACTIVE_VALID` | ✅ | warm server + demand>0 + attempted>0 + closure-after-lsp |
| `LSP_NO_OP_VALID_WITH_WARM_SERVER` | ✅ | warm server + residual==0/no demand + `no_op_valid` |
| `LSP_UNSUPPORTED_EXPLICIT` | ✅ (labeled) | no server for the language (honest, never a fake success) |
| `LSP_FAIL_NO_WARM` | ❌ | server not launched, or probe never returned (latency 0), or residual==0 without a warm no-op |
| `LSP_FAIL_STALE_CLOSURE` | ❌ | closure not rebuilt after LSP, or `closure_rebuilt_at < lsp_finished_at` |
| `LSP_FAIL_NOT_RUN_BEFORE_SCORING` | ❌ | `lsp_finished_at` missing, or demand>0 with 0 attempts |
| `LSP_FAIL_MISSING_CERTIFICATE` | ❌ | no certificate + no `lsp_warm=1` proof on the contract line |

## Tests (red→green) — `tests/fail_closed/test_lsp_liveness.py`

15 tests, all passing, covering the full required matrix:
`residual==0 + no warm ⇒ NO_WARM` · `residual==0 + warm + reason ⇒ NO_OP_VALID` · `demand>0 + 0
attempts ⇒ NOT_RUN` · `closure ts < lsp ts ⇒ STALE_CLOSURE` · `closure not rebuilt ⇒ STALE_CLOSURE`
· `command exists but probe not run ⇒ NO_WARM` · `non-python + python-only LSP ⇒
UNSUPPORTED_EXPLICIT` · `scoring before lsp finished ⇒ NOT_RUN` · `missing cert ⇒
MISSING_CERTIFICATE` · `active valid ⇒ ACTIVE_VALID` · gate_lsp cert-arg + file-load + line-fallback
(no-warm line / legacy line cannot vacuously pass).

## Known pre-existing failure (NOT introduced by Stage 1, NOT fixed here)

`tests/fail_closed/test_fail_closed_gates.py::test_lsp_installed_zero_enrichment_fails_or_reason`
(variant B) asserts a substring in `preflight_pipeline.check_lsp_edges`'s message
(`"no edge corrections needed"`). The actual message already reads "warm-probe gate (GT_REQUIRE_LSP)
is the LSP-ran proof" — i.e. `preflight_pipeline.py` (a file Stage 1 does **not** touch) drifted
toward the warm-probe concept in a prior commit while the test's expected string was not updated.
`ok_b is True` still holds; only the substring assertion fails. **This is out of Stage 1's surface
(LSP cert + gate + tests); it belongs to the `preflight_pipeline` surface and should be reconciled
separately** (it is aligned with Stage 1's direction).

## Live data status

`lsp_warm`/probe-latency/per-task verdicts on real repos are produced by the official workflow and
will be collected in Phase 6 **Stage B (1-task)** and **Stage C (held-out-10)**. This stage proves
the gate logic locally; it does not yet assert a live warm probe.

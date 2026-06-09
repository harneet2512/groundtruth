# EMBEDDER_USAGE_REPORT — Stage 3

> Stage 3 of the full-runtime proof. Branch `gt-trial`. Scope: prove the embedder is **consumed
> by every semantic path that claims semantic evidence**, not merely available. Code + tests only
> (live per-task identities/counts land in Phase 6 Stage B/C). No workflow / image-cache /
> LSP-gates-container / graph-handoff / ranking-weight / task-specific changes.

## What changed (the invariant)

The embedder must be **loaded from the required runtime/model root** and **actually used** by
`run_v74`, `localize`, and (where it consumes semantic evidence) v1r/render. Stage 3 wires the
identity proof, emits an embedder certificate, and classifies it.

- **Identity proof wired** (was defined-but-never-called): `proof.assert_same_embedder_identity`
  is now called from `run_v74` (`v7_4_brief.py`, after `_get_model`) and `localize`
  (`graph_localizer.py`, after `_get_embedder`). The first caller stamps the identity
  (`models_root | class | dim | force_onnx`) into graph-meta; any later caller with a different
  identity **raises in proof mode** → catches model-root divergence across semantic paths.
- **Certificate** (`proof.build_embedder_certificate` + `write_embedder_certificate`, output
  `$GT_EMBEDDER_CERT` / `/tmp/gt/embedder_certificate.json`): `GT_FORCE_ONNX_EMBEDDER`,
  `GT_REQUIRE_EMBEDDER`, `GT_MODELS_ROOT`, `model_path`, `model_sha` (sidecar/env, never hashed
  per task), `embedder_class`, `embedder_dim`, `runtime_context_id`, `run_v74`/`localize`/`v1r`
  identities, `semantic_candidate_count`, `rendered_candidate_count`,
  `rendered_semantic_nonzero_count`, `upstream_semantic_nonzero_count`, `effective_w_sem`,
  `all_zero_semantic_reason`, `model_download_attempted`.
- **run_v74 emits** the cert after the consumption check, computing `rendered_semantic_nonzero`
  (over the delivered components) and `upstream_semantic_nonzero` (over the component source
  `sem_all`/`sem_scores`) — so an upstream signal dropped before render is detectable.
- **Wrapped, proof-mode-only:** all additions are no-ops outside proof mode and never alter
  ranking, the brief, or measurement (BRIEFING.md invariants respected — no weight changes,
  semantic ON in both `run_v74` and `localize` via the same ONNX identity).

## Hard-gate verdicts (`embedder_certificate.classify_embedder`)

| Verdict | Pass? | Condition |
|---|---|---|
| `EMBEDDER_USAGE_VALID` | ✅ | real embedder, single model root, semantic consumed |
| `EMBEDDER_USAGE_VALID_NOOP` | ✅ | no candidates to embed |
| `EMBEDDER_FAIL_NO_CERT` | ❌ | certificate absent |
| `EMBEDDER_FAIL_ZERO_MODEL` | ❌ | `_ZeroEmbeddingModel` used |
| `EMBEDDER_FAIL_LOAD_ERROR` | ❌ | embedder failed to load |
| `EMBEDDER_FAIL_ST_UNDER_FORCED_ONNX` | ❌ | sentence-transformers while `GT_FORCE_ONNX_EMBEDDER=1` |
| `EMBEDDER_FAIL_MODEL_ROOT_DIVERGENCE` | ❌ | run_v74 / localize / v1r model roots differ |
| `EMBEDDER_FAIL_MODEL_DOWNLOAD` | ❌ | model download attempted during the task |
| `EMBEDDER_FAIL_DROPPED_SEMANTIC` | ❌ | upstream semantic nonzero but rendered all-zero (join/drop) |
| `EMBEDDER_USAGE_FAIL` | ❌ | non-empty candidates render all-zero under proof + `GT_REQUIRE_EMBEDDER` |

`all_zero_semantic_reason` is **strict — no broad escape hatch**: in proof mode with a required
embedder and non-empty candidates, all-zero is `EMBEDDER_USAGE_FAIL` (or `_DROPPED_SEMANTIC`).
Acceptable no-ops are structural only: zero candidates, or outside proof/`require_embedder`
(correct-or-quiet). The `localize` path additionally **raises** in proof on a swallowed encode
error / DB read error / no-docs / all-zero ranks (`graph_localizer.py:1382-1432`).

## Tests — `tests/fail_closed/test_embedder_usage.py` (15, all pass)

Classify matrix (valid · zero-model · load-error · ST-under-forced-ONNX · model-root divergence ·
model-download · all-zero-nonempty-fail · dropped-semantic-fail · no-candidates-noop · no-cert ·
all-zero-outside-proof-valid · all-zero-without-require-valid) · `assert_same_embedder_identity`
match-then-mismatch (raises in proof) · build/write/load roundtrip · **real `localize` encode
exception → raises in proof**. Full fail-closed now 109/109.

## Live data status

Per-task embedder identities (run_v74 vs localize), model SHA, and the semantic counts are produced
by the official workflow in Phase 6 **Stage B/C**, where the cross-path identity must agree and
non-empty candidates must render nonzero semantics. This stage proves the cert + classify + identity
logic locally and wires the asserts/emit; container-side cert collection is wired in **Stage 4**.

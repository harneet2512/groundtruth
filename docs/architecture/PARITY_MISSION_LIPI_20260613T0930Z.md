# LIPI — architecture-parity mission code (commits `549f586c`..`a0e2ab3d`)

4-avenue review (**L**ogic · **I**mplementation · **I**ntegration · **P**lumbing) of every production
line changed this mission, verified against the live code (file:line) and the 399 green tests. Range
`15cbe1ea..a0e2ab3d`; 10 production files, +509 / −314.

---

## Commit `549f586c` — B1 single-source the delivery fact-filter

### `src/groundtruth/delivery/name_policy.py` (NEW, +92) — builtin/dunder + stdlib-shadow
- **Logic** ✅ `is_builtin_shadow_name`: dunder check `n.startswith("__") and n.endswith("__") and len(n)>4`
  (excludes `__` and `____`; `__init__` len 8 → True). `is_stdlib_shadow`: regex
  `([A-Za-z_][\w.]*)\.([A-Za-z_]\w*)\s*\(`, `head=group(1).split(".")[0] in STDLIB_MODULES` — same
  algorithm as the removed inline copies; sets are the exact frozensets moved verbatim.
- **Implementation** ✅ `(name or "").strip()` guards None/empty → False (no crash). regex precompiled
  module-level. No swallowed exceptions; pure functions.
- **Integration** ✅ proven by `test_b1::test_single_source_same_object_identity`:
  `gmp._is_builtin_shadow_name IS name_policy.is_builtin_shadow_name` and v1r binds the same object —
  not equal values, the SAME object. A Product edit reaches both consumers.
- **Plumbing** ✅ stdlib-only (`import re`); importable in-container from `/opt/gt/src` (same tree as
  `groundtruth.runtime.*` which FIX-D already imports).

### `src/groundtruth/delivery/__init__.py` (+40) — re-export surface
- **L/I** ✅ re-exports `is_vendored_path/is_generated/is_delivery_excluded/is_minified_file` (path) +
  name-class. **Integration** ✅ does NOT re-home the FACT gate / cross-language (kept in
  `curation_map`) — avoids creating a SECOND source. **Plumbing** ✅ `__all__` explicit.

### `src/groundtruth/pretask/v1r_brief.py` (−99 net) — inline → import
- **Logic** ✅ behavior-preserving: removed inline `_STDLIB_MODULES/_is_stdlib_shadow/
  _BUILTIN_CALLABLE_NAMES/_is_builtin_shadow_name`, aliased imports keep every call site
  (`_is_stdlib_shadow`, `_is_builtin_shadow_name`, `_is_vendored_path`) unchanged.
- **Implementation** ✅ dropped the now-unused `_STDLIB_MODULES`/`_BUILTIN_CALLABLE_NAMES` set aliases
  (Pyright "not accessed" confirmed gone). Mid-file imports are the file's existing pattern.
- **Integration** ✅ v1r already imported path-class + cross-lang + FACT gate from curation_map; this
  only adds the name-class import. **Plumbing** ✅ `test_v1r_brief`/`test_brief_fact_correctness`/
  `test_v1r_crosslang_disqualifier` green (73 in the earlier sweep).
- **NON-HARM (BRIEFING.md invariant)** ✅ no ranking/semantic/weight code touched — pure filter-source
  swap; identical decisions (battery test).

### `artifact_deepswe/gt_mini_patch.py` (−328 inline) — import block + removals
- **Logic** ✅ the proof-mode FAIL-CLOSED is correct: `except Exception` → `if
  _gt_proof_or_substrate_mode(): raise RuntimeError(GT_DELIVERY_POLICY_IMPORT_FAILED)`; else logged
  degraded stubs. `_gt_proof_or_substrate_mode` mirrors `_substrate_active` + `GT_PROOF_MODE`.
- **Implementation** ✅ removed ALL inline fact-filter (path markers, `_is_vendored_path`,
  `_is_minified_file`, `_is_delivery_excluded`, builtin set + `_is_builtin_shadow_name`,
  `_STDLIB_MODULES`+`_STDLIB_SHADOW_RE`+`_is_stdlib_shadow`, `_DETERMINISTIC_METHODS`, the cross-lang
  block). **KEPT** the edit-detection helpers — verified `grep -c "def _has_source_ext|
  _is_repo_source_path|_norm_fp" = 3`. No dangling reference (the `_STDLIB_SHADOW_RE`/marker
  undefined-var errors were chased to zero before commit).
- **Integration** ✅ imports the SAME objects (identity test); `_is_repo_source_path` still calls the
  now-imported `_is_vendored_path` (works). The degraded stubs are unreachable when groundtruth is
  importable (always, in CI / in-container).
- **Plumbing** ✅ `test_b1::test_proof_mode_import_failure_fails_closed` (subprocess, poisoned import,
  GT_PROOF_MODE=1) → raises; `test_non_proof_..._degrades` → no raise, `_DELIVERY_POLICY_AVAILABLE=False`.

---

## Commit `de90b78f` — C/D1 early substrate-handoff guard (`gt_agent.py`, +43)
- **Logic** ✅ `_assert_substrate_handoff`: `if not _proof_mode(): return` (no-op off proof);
  `if not _substrate_active(): abort PROOF_WITHOUT_SUBSTRATE_HANDOFF`; `missing=[GT_HOST_GRAPH_DB,
  GT_CERT_DIR not set] → abort PROOF_HANDOFF_ENV_MISSING`. Correct truth table (5 tests).
- **Implementation** ✅ `_adapter_fail` ALWAYS raises (`gt_agent.py:115-116` — verified), so each branch
  hard-stops. Guard gates on `_proof_mode()` first, so it never fires off-proof.
- **Integration** ✅ called in `run()` AFTER the `_GT_BASELINE` early-return (baseline arm unaffected)
  and BEFORE `_emit_gt_meta_witness()` — front-loads the env contract; does NOT replace the downstream
  witness graph-hash / `_substrate_brief` existence checks (additive).
- **Plumbing** ✅ reads the handoff env the workflow sets (`deepswe_full.yml:1112-1117`). 5 tests:
  proof-no-handoff aborts, proof+substrate-but-missing-env aborts, full-handoff passes,
  host-graph-only-needs-cert-dir, non-proof no-op.

---

## Commit `98ec1bba` — F quarantine v2_ranker + CI guard
- **Logic** ✅ `dead_path_registry` is passive data; adding `groundtruth.pretask.v2_ranker` (replacement
  `run_v74`) extends the quarantine. **Implementation** ✅ proven v2_ranker's ONLY live-src importer is
  the (also-dead) v22_brief (`test_v2_ranker_is_dead_by_association_only`).
- **Integration** ✅ test extended to the DeepSWE entrypoints (gt_agent, gt_mini_patch) + OH wrappers;
  positive `generate_v1r_brief` assertion; snapshot non-masquerade (`find_spec` resolves to live src,
  not `pregen_gt`/`gen_lab`). **Plumbing** ✅ path comparison made OS-agnostic (`.as_posix()`). 29 green.
- **Safety** ✅ nothing deleted; snapshots left on disk (non-importable, proven).

---

## Commit `4325b787` — B/A1 single brief per proof

### `src/groundtruth/runtime/brief_cache.py` (NEW, +118)
- **Logic** ✅ `get_or_generate`: `load_cached_brief` first (reuse, `generated=False`); else generate +
  `persist_brief` (`generated=True`). `brief_sha256` = sha256 of the **stripped** text (the exact bytes
  written + consumed) — so gate sha == delivered sha by construction.
- **Implementation** ✅ FAIL-SAFE: `load_cached_brief` swallows OSError/ValueError → None → regenerate;
  `persist_brief` swallows OSError/TypeError → returns the dict regardless. `_extract_metrics` coerces
  non-JSON `sem_components` to list/None (no serialization crash). Generator exceptions PROPAGATE (the
  proof must fail-closed on an unproducible brief) — not swallowed.
- **Integration** ✅ `test_a1` proves: gate-then-emit → 1 generation, equal text/sha; no-cache →
  regenerate; the metric fields match `_load_brief_metrics`' contract.
- **Plumbing** ✅ cache path = `os.path.join(out_dir, "brief_result.json")` — one file, both processes.

### `scripts/swebench/gt_run_proof.py` emit_brief + gate_env
- **Logic** ✅ emit_brief uses `get_or_generate(out_dir,...)` → reads the gate-persisted brief; writes
  brief.txt from it; return detail carries `sha256` + `reused_gate_brief`.
- **Implementation** ✅ empty-brief still fail-closed (`if not bt: return False ... EMPTY`) —
  `test_a1::test_emit_brief_empty_fails_closed`. Exception path unchanged (return False, no swallow).
- **Integration** ✅ **ORDER VERIFIED**: gates subprocess `:1077` + `tracker.complete("gates") :1124`
  run BEFORE `emit_brief :1134` — so the gate cache EXISTS when emit reads. `gate_env` adds
  `GT_BRIEF_CACHE_DIR=a.out` (`:1027`), the SAME dir emit_brief writes brief.txt to.
- **Plumbing** ✅ both sides agree on `a.out`; the cache file is `a.out/brief_result.json`.

### `scripts/metrics/foundational_gates.py` `_persist_brief_for_emit` (+20)
- **Logic** ✅ persists ONLY in path-2 success (`if ex is not None: _persist_brief_for_emit(r)`), where
  `r` carries `brief_text` + the metric attrs.
- **Implementation** ✅ best-effort: `if not cache_dir: return`; `try/except Exception: pass` — a persist
  failure NEVER fails the gate (emit then regenerates — fail-safe).
- **Integration** ✅ **CHAIN VERIFIED**: `gate_embedder_consumption :546 → _load_brief_metrics :586 →
  _persist_brief_for_emit`. **Plumbing** ✅ reads `GT_BRIEF_CACHE_DIR` (set by gt_run_proof gate_env).

---

## Commit `a0e2ab3d` — D substrate commit-parity

### `docker/Dockerfile.gt-substrate` (+10)
- **Logic/Implementation** ✅ ARGs don't cross stages → re-declared `ARG GT_COMMIT_SHA=dev` in the
  FINAL stage + `ENV GT_SUBSTRATE_BUILD_COMMIT=${GT_COMMIT_SHA}`. The build workflow passes
  `--build-arg GT_COMMIT_SHA=github.sha` (Dockerfile comment `:38`). **Plumbing** ✅ the ENV is in the
  image → present in every container automatically. *(Build itself is validated on the next substrate
  rebuild — no local Docker.)*

### `scripts/swebench/gt_run_proof.py` parity helpers + gate + manifest
- **Logic** ✅ `commit_parity_status`: `unknown` if either commit missing or build in `{dev,unknown}`;
  else `match`/`mismatch`. `assert_commit_parity`: gate off (`GT_REQUIRE_COMMIT_PARITY!=1`) →
  record-only `(True,...)`; gate on + mismatch → `(False, GT_COMMIT_PARITY_MISMATCH)`. Verified by 7
  tests + the inline truth-table run.
- **Implementation** ✅ default record-only is the SAFE choice (a pinned substrate may lag during
  iteration without breaking); the manifest ALWAYS carries `commit_parity` so drift is never silent.
- **Integration** ✅ gate wired in `env_validation` AFTER the container-boundary assert, BEFORE
  `tracker.complete("env_validation")` → fails as an env_validation stage failure. Manifest adds
  `substrate_build_commit` + `commit_parity` (brief_sha256 already present `:286`).
- **Plumbing** ✅ `GT_GIT_COMMIT` passed by the workflow (`deepswe_full.yml:980`);
  `GT_SUBSTRATE_BUILD_COMMIT` baked. `test_d::test_manifest_carries_provenance` asserts both + the
  mismatch status.

---

## Cross-cutting LIPI verdicts
- **No silent failure introduced.** Every new `except` either fails-closed (proof mode) or is a
  documented best-effort fallback that degrades to PRIOR behavior (never worse). The proof's
  fail-closed contracts (empty brief, missing handoff, hash mismatch) are all preserved.
- **No second implementation created.** B1 + A1 REMOVE duplication; D adds a baked-commit (one source);
  F quarantines. The only new modules (`name_policy`, `brief_cache`) are single sources imported, not
  forked.
- **No benchmark-shape / Python-only / gold leakage** in any change (language-agnostic check `05`).
- **Reversible.** Each commit is atomic; `git revert <sha>` undoes one priority cleanly.
- **Pending (not a code gap):** the A1 cross-process flow + D baked-commit are unit-tested + fail-safe
  but await an IN-CONTAINER substrate proof (no local Docker/CGO) — the FINAL audit's one HIGH item.

**LIPI verdict: all 5 commits CLEAN across L/I/I/P.** 399 tests green; `compileall` clean.

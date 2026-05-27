# TODO: DeepSWE Benchmark Integration

## Status: 2026-05-27

Branch: `artifact_deepswe` (merged with `jedi__branch`)

---

## DONE

### Resolver Overhaul (proven locally on 9 repos)

| Fix | LOC | Impact (local proof) |
|---|---:|---|
| P0: Relative path resolution + extension probing (TS/JS/Rust) | 60 | TS 44→2992 import edges |
| P1: Go method receiver → struct parent_id | 25 | Go 0%→51% parent coverage |
| P2: CommonJS require() extraction (JS) | 40 | JS import extraction enabled |
| P3: Class-method dispatch via imported class | 150 | conf=0.95 for class.method calls |
| P4: Go package name parsing | 45 | Go module path resolution |
| P5: Rust impl_item name from `type` field | 5 | Rust parent linkage fixed |
| P6: Rust mod declarations as imports | 15 | Rust module tree following |
| P9: Rust function_signature_item | 1 | Trait method signatures captured |
| **T1: verified_unique** (globally unique names) | **20** | **All languages: +28-85pp deterministic** |
| T2: type_flow dispatch | 52 | Qualified receiver resolution |
| Go RegisterGoModulePaths dots filter fix | 30 | Go 0→780 import edges |
| Rust workspace path stripping | 20 | Rust import resolution |
| Go module suffix stripping in resolveModulePath | 25 | Go pkg.Func() resolution |

**Local proof (9 repos, gt-index-t1t2.exe built and tested on this machine):**

| Repo | Lang | Before | After | |
|---|---|---:|---:|---|
| ts-pattern | TS | 9.0% | **93.6%** | +85pp |
| etree | Go | 31.0% | **89.1%** | +58pp |
| dateutil | Python | 51.5% | **80.1%** | +29pp |
| task | Go | 34.0% | **78.5%** | +44pp |
| sqlite-utils | Python | 25.9% | **68.3%** | +42pp |
| arktype | TS | 16.6% | **62.1%** | +46pp |
| expr | Go | 34.1% | **59.8%** | +26pp |
| kombu | Python | 38.9% | **56.0%** | +17pp |
| kysely | TS | 10.0% | **53.7%** | +44pp |

### Pier Integration (GTMiniSweAgent)

- [x] `gt_agent.py` — Pier-native subclass, no monkey-patching
- [x] Brief generation on host from pre-built graph.db (all 5 languages)
- [x] graph.db upload to container via `environment.upload_file()`
- [x] gt_hook.py injection via `install_spec()` (base64 chunked)
- [x] Admissibility gate (BUDGET, NOT_TEST, CONFIDENCE, CONCISE, NO_SPAM, HAS_VALUE)
- [x] Seed extraction from instruction text (language-agnostic)
- [x] Post-run GT hook usage extraction
- [x] All SQL uses `resolution_method IN ('same_file', 'import')` — no arbitrary thresholds
- [x] Repo root auto-detection in container

### Infrastructure

- [x] GHA preindex workflow (113 tasks, 95/98 repos indexed on first run)
- [x] GHA trial workflow (single task via Pier)
- [x] Docker Hub secrets configured (DOCKERHUB_USERNAME, DOCKERHUB_TOKEN)
- [x] DEEPSEEK_API_KEY in GHA secrets
- [x] repo_manifest.json (113 tasks, 92 repos, 5 languages)
- [x] Go 1.22 + GCC 13 installed locally for building gt-index

### Documentation

- [x] READINESS_AUDIT.md
- [x] INTEGRATION_MAP.md (GT+OH pattern)
- [x] MINI_SWE_AGENT_MAP.md (Pier architecture)
- [x] DRY_RUN.md (12/12 pipeline checks passed)

---

## TODO: Rebuild GHA indexes with T1/T2 binary

**Priority: HIGH — blocks accurate readiness numbers**

The 95 GHA-indexed repos use the OLD resolver (no T1 verified_unique). The GHA build succeeded for 1 repo (arktype: 62.1% confirmed). Need full 113-repo reindex.

**Fix:** The master branch already has the full gt-index source with T1/T2. Trigger:
```bash
gh workflow run deepswe_preindex.yml -f language=all -f max_tasks=0 -f run_smoke=false
```

**Estimated result (from local proof):** All languages jump from 13-36% to 54-94% deterministic.

---

## TODO: Fix remaining doc-of-honor failures

### Failure 1: Co-change = 0 for ALL languages
**Status:** NOT BUILT
**Cause:** `git log` co-change mining in main.go runs but produces 0 pairs on all repos. The `cochange_pairs` table exists but the mining query returns empty.
**Fix:** Debug `Pass 5c` in main.go — check if git is available in the container, if the log format is correct, if the threshold is too high. ~50 LOC.
**Impact:** Enables "files that historically change together" — the completeness signal from the doc.

### Failure 2: Qualified names = 0-9% populated
**Status:** NOT BUILT
**Cause:** `qualified_name` column is populated inconsistently. Python gets ~6%, Go ~4%, TS ~9%, JS/Rust 0%.
**Fix:** In parser.go, construct `qualified_name` as `parent_class.function_name` for methods and `package.function_name` for top-level functions. ~30 LOC.
**Impact:** Enables full boundary detection — "this is Class.method, not just method."

### Failure 3: TypeScript signatures = 54% (vs 99% for Python/Go/Rust)
**Status:** NOT BUILT
**Cause:** Arrow functions (`const handler = (req) => {...}`) don't have extractable signatures in tree-sitter AST. Only `function_declaration` and `method_definition` have signature fields.
**Fix:** In parser.go, for arrow functions assigned to variables, reconstruct the signature from the parameter list. ~30 LOC.
**Impact:** Agent sees the contract for 46% more TS functions.

### Failure 4: Go parent linkage = 2% on GHA (51% local)
**Status:** CODE DONE, needs GHA rebuild
**Cause:** P1 receiver→struct fix is on `artifact_deepswe` but GHA built from old master.
**Fix:** Reindex with T1/T2 binary (same as TODO #1).

### Failure 5: JavaScript return types = 2%
**Status:** WON'T FIX
**Cause:** JavaScript doesn't have type annotations. JSDoc could be parsed but that's a separate effort.
**Impact:** Low — JS is only 5 tasks (4.4% of DeepSWE).

---

## TODO: Run single trial

**Priority: HIGH — proves GT helps on an actual DeepSWE task**

Task: `ts-pattern-match-each` (TypeScript, 93.6% deterministic, rich graph)
Model: DeepSeek V4 Flash (~$0.05)
Command:
```bash
pier run -p deep-swe/tasks/ts-pattern-match-each \
    --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent \
    --model deepseek/deepseek-v4-flash \
    --ak graph_db_dir=indexes \
    --env docker -y
```

**Blocked by:** GHA trial workflow needs the pre-built graph.db for ts-pattern (available as artifact from run 26491271051).

---

## TODO: Full benchmark run

**Priority: MEDIUM — after single trial proves it works**

1. Reindex all 113 tasks with T1/T2 binary (~25 min GHA)
2. Download all graph.db artifacts
3. Run baseline: `pier run -p deep-swe/tasks -a mini-swe-agent --model deepseek/deepseek-v4-flash -n 4`
4. Run GT: `pier run -p deep-swe/tasks --agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent --model deepseek/deepseek-v4-flash --ak graph_db_dir=indexes -n 4`
5. Compare resolution rates

---

## Architecture (for reference)

```
deep-swe/tasks/<task-id>/
├── task.toml           ← Docker image, language, commit
├── instruction.md      ← What the agent sees
├── tests/test.sh       ← Grading (applied at eval time)
└── solution/           ← Reference (held out)

pier run → DockerEnvironment → MiniSweAgent.install_spec() → mini-swe-agent CLI
                                       ↑
                              GTMiniSweAgent extends:
                              1. install_spec() adds gt_hook.py + repo root detection
                              2. run() generates brief on HOST, uploads graph.db, augments instruction

gt-index (Go binary) → graph.db → v1r/v22 brief pipeline → <gt-task-brief>
                                → gt_hook.py queries  → understand/verify commands
```

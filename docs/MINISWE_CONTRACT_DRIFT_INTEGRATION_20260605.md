# GroundTruth contract-DRIFT — mini-swe-agent integration (2026-06-05)

This document describes exactly **what** was integrated into the mini-swe-agent path and
**how deep** it goes, so the mini-swe-agent / pier side can understand the contract, the
prerequisites, and how to enable + validate it. Hand this to whoever owns the mini-swe
harness.

---

## 0. TL;DR

GroundTruth (GT) adds a **contract-drift advisory** to the agent loop. After the agent edits
a source file, GT re-reads the code graph and **diffs the edited symbol's behavioral contract
before vs after the edit** — return shape, raised exceptions, dropped guards/preconditions —
and tells the agent which of those changes are **breaking changes callers depend on**, e.g.:

```
<gt-drift>
Your edit changed the behavioral contract below. Confirm each change is intended - callers depend on the prior contract:
lib.py :: get_user  (1 verified caller depend on this)
  return shape: collection|[uid] -> none
  dropped raise: KeyError
  dropped guard: raise: not uid -> raise KeyError("missing uid")
</gt-drift>
```

For mini-swe-agent the transport is **PULL**: the agent runs a `gt drift <file>` command
itself (mini-swe-agent is a bash agent; this matches its paradigm and needs no fork of the
mini-swe-agent package). GT **never runs tests, never runs the patch, never modifies the
patch** — it reads the code graph only and prints advice.

The integration is **gated OFF by default** (`GT_DRIFT_ENABLED`); with the flag unset the
mini-swe-agent path is byte-for-byte unchanged.

---

## 1. Why drift (one paragraph of context)

GT already delivers contract *context* (callers, signatures, raises) and that did not move
results — re-delivering context is a proven null. The new signal is the **diff**: "your patch
*changed* the contract the rest of the code relies on." This is the moment the agent
self-certifies a broken patch. Drift is derived purely from the call graph + parsed behavioral
properties, so it is **provably non-leaking**: it never reads, names, maps, or runs any test
(on SWE-bench the grading oracle is drawn from repo tests, so any test contact risks leaking
the oracle — GT structurally avoids it).

---

## 2. Architecture: one payload, two transports

```
                  contract_map.build_drift()          <- the engine (pure, graph.db in -> text out)
                  contract_map.snapshot_contract()        diffs return_shape/return_type/raises/guards
                            ^                               by (file, name); caller exposure from
                            |                               VERIFIED edges only; correct-or-quiet
                  hooks/drift_hook.drift_advisory()    <- transport helper (frozen-original baseline)
                            ^
              +-------------+-------------------+
              |                                 |
   OpenHands (PUSH)                    mini-swe-agent (PULL)
   post_edit.py emits the             agent runs `gt drift <file>`
   <gt-drift> block as an             -> hooks/drift_cli.py
   observation after each edit        -> same drift_advisory(), same payload
```

The **same** `<gt-drift>` payload reaches the agent in both scaffolds; only the delivery
differs (GT pushes an observation in OpenHands; the agent pulls a command in mini-swe).

### The baseline (important)
The "before" side of the diff is a **frozen copy of the session-start graph.db**, taken
*before the agent edits anything*, stored at `<graph.db>.orig`. This is the contract callers
were written against. The "after" side is the working graph.db, which GT reindexes for the
single edited file (`gt-index -file <rel>`). Symbols are matched by `(file_path, name)` — NOT
by node id — because an incremental reindex is delete+insert and ids change.

---

## 3. What was wired into mini-swe-agent (the deep part)

File: `deepswe-pier/src/pier/agents/installed/mini_swe_agent.py`

### 3.1 Module constant — the agent instruction
`_GT_DRIFT_INSTRUCTION` (appended to the task): tells the agent that a `gt` command exists,
to run `gt drift <path>` after editing a source file, and to run it on every changed file
before finishing and confirm each reported change is intentional. It explicitly states GT does
not run tests and does not modify the patch.

### 3.2 `MiniSweAgent._setup_gt_drift(instruction, environment, env) -> str`
Runs once, in the environment, **before** the mini-swe-agent subprocess starts. It:
1. resolves the repo root: `git rev-parse --show-toplevel` (falls back to `pwd`);
2. builds the graph: `$GT_INDEX_BINARY -root="$ROOT" -output=/tmp/gt_graph.db`;
3. **freezes the baseline**: `cp -f /tmp/gt_graph.db /tmp/gt_graph.db.orig`;
4. installs a `gt` command at `$HOME/.local/bin/gt` (a tiny `sh` wrapper that forwards
   `gt drift <files...>` to `python3 -m groundtruth.hooks.drift_cli --root <root> --db
   /tmp/gt_graph.db --file ...`);
5. returns the task instruction with `_GT_DRIFT_INSTRUCTION` appended.

It **never raises** — a standup failure leaves the agent unaffected and drift simply silent.

### 3.3 The gated call inside `run()`
Immediately after the env dict is built and before the mini-swe-agent subprocess command is
assembled:
```python
if self._get_env("GT_DRIFT_ENABLED"):
    augmented_instruction = await self._setup_gt_drift(augmented_instruction, environment, env)
    escaped_instruction = shlex.quote(augmented_instruction)
```
With `GT_DRIFT_ENABLED` unset, none of the above runs — the path is unchanged.

### 3.4 The `gt` command contract
- `gt drift` — drift for all git-modified tracked files (`git diff --name-only HEAD`).
- `gt drift path/to/file.py [more files...]` — drift for the named files.
Output is the `<gt-drift>` block on stdout, or nothing (correct-or-quiet). Always exits 0.

---

## 4. The agent loop, end to end (what mini-swe experiences)

1. **Standup (once):** GT indexes the repo, freezes `/tmp/gt_graph.db.orig`, installs `gt`.
2. **Task:** the agent receives its normal task **plus** the drift instruction.
3. **Edit:** the agent edits `lib.py` as usual (GT does nothing here).
4. **Pull:** the agent runs `gt drift lib.py`. GT reindexes only `lib.py` into the working
   graph, diffs vs the frozen original, prints the `<gt-drift>` block (or nothing).
5. **React:** the agent sees the breaking-change facts and either fixes the patch or confirms
   the change is intended.
6. **Pre-submit:** before finishing, the agent runs `gt drift` (no args) over all changed
   files for a final confirmation.

GT is **agent-assisting, not agent-controlling**: it cannot block submit; it only informs.

---

## 5. What pier / mini-swe-agent MUST provide (prerequisites)

For the gated path to work in the environment/container:

| Requirement | Why | How it's consumed |
|---|---|---|
| `groundtruth` package importable | `gt drift` runs `python3 -m groundtruth.hooks.drift_cli` | `PYTHONPATH` includes the package (the DeepSWE path already ships parts of it) |
| `gt-index` binary present | builds the graph + per-file reindex | `GT_INDEX_BINARY=<path>` (preferred) or `gt-index` on `PATH` |
| `git` + the repo checked out | repo-root resolution + modified-file detection | `git rev-parse` / `git diff` in the env |
| `python3` >= 3.10 | runs the CLI | in the env |
| `GT_DRIFT_ENABLED=1` | arms the integration | set in the agent env |

Notes:
- `GT_INDEX_BINARY` was added to GT's binary resolver precisely because the container binary
  is not on `PATH`. Set it to the uploaded binary path.
- The frozen baseline path is `<db>.orig` by convention; override with `GT_ORIGINAL_DB` if needed.
- No GT state persists across tasks; everything lives under `/tmp` per task.

---

## 6. Invariants / contract (non-negotiable)

1. **Zero test contact.** GT never reads, maps, names, surfaces, or runs tests. `is_test` is
   used only to *exclude* test-sourced callers; the `assertions` table is never read; test/
   fixture files are skipped entirely. This is the legitimacy core (no oracle leakage).
2. **No execution.** GT issues no `pytest` / `go test` / `cargo` / `python -c` / RUN commands.
   It reads `graph.db` only.
3. **Advisory only.** GT prints facts; the agent decides and runs whatever it wants. GT never
   modifies the patch and cannot block submission.
4. **Deterministic, LLM-free, $0.** Pure SQL over the graph; no model calls in GT's path.
5. **Language-agnostic.** Drift reads the language-tagged `properties` table populated by the
   tree-sitter indexer (30+ languages), so it is not Python-specific. (This deliberately uses
   the graph-backed path, NOT the older ast-only `gt_hook.py`, which degraded on go/rust/ts/js.)
6. **Correct-or-quiet.** Emits only material, well-formed drift; stays silent (empty output)
   when nothing material changed or no baseline exists. Caller counts use VERIFIED edges only
   (a guessed `name_match` caller never inflates the consequence).

---

## 7. Enable + validate in the codespace

```bash
# 1. Ensure prerequisites in the env:
export GT_INDEX_BINARY=/path/to/gt-index          # uploaded binary
export PYTHONPATH=/path/to/groundtruth/src:$PYTHONPATH
export GT_DRIFT_ENABLED=1

# 2. Smoke the CLI directly (no agent), inside a checked-out repo:
gt-index -root "$PWD" -output /tmp/gt_graph.db
cp -f /tmp/gt_graph.db /tmp/gt_graph.db.orig
# ...edit a function so it returns differently / drops a raise...
gt-index -file path/to/edited.py -output /tmp/gt_graph.db
python3 -m groundtruth.hooks.drift_cli --root "$PWD" --db /tmp/gt_graph.db --file path/to/edited.py
# expect a <gt-drift> block; an unchanged file prints nothing

# 3. Run mini-swe with the flag set; confirm the agent invokes `gt drift` and sees the block.
```

Measurement (per scaffold, paired vs the frozen GT-off baseline): (1) did the agent **use** the
drift in its trajectory, (2) did **flips** move. Kill the lever if it's ignored or flat.

---

## 8. Failure modes (all safe)

- **No baseline** (`<db>.orig` missing) → `drift_advisory` returns "" (quiet). Standup is what
  creates the baseline; if standup didn't run, drift is simply silent.
- **Standup failure** (no binary, index error) → `_setup_gt_drift` catches and returns the
  un-augmented instruction; the agent runs normally without drift.
- **Binary not found by the CLI** → `run_incremental_index` logs to stderr and returns False;
  drift then diffs whatever the working graph currently holds (no crash).
- **Test/non-source file edited** → skipped (zero test contact, no noise).

---

## 9. File manifest

| File | Role |
|---|---|
| `src/groundtruth/pretask/contract_map.py` | engine: `snapshot_contract`, `build_drift`, `render_drift` |
| `src/groundtruth/pretask/curation_map.py` | shared graph queries (`build_function_map`, `_DETERMINISTIC_METHODS`) reused by drift |
| `src/groundtruth/_binary.py` | `run_incremental_index` (`gt-index -file`) + `GT_INDEX_BINARY` override |
| `src/groundtruth/hooks/drift_hook.py` | transport helper: `freeze_original`, `drift_advisory`, frozen-original baseline |
| `src/groundtruth/hooks/drift_cli.py` | the shared `gt drift` CLI both scaffolds call |
| `src/groundtruth/hooks/post_edit.py` | OpenHands push: emits the `<gt-drift>` block after edits |
| `scripts/swebench/oh_gt_full_wrapper.py` | OpenHands: freezes `graph.db.orig` in-container at session start |
| `deepswe-pier/src/pier/agents/installed/mini_swe_agent.py` | mini-swe pull: `_setup_gt_drift`, `_GT_DRIFT_INSTRUCTION`, gated call |

Tests: `tests/unit/test_contract_drift.py`, `tests/unit/test_drift_hook.py`.
Real-binary proofs: `.tmp_drift_realbinary_proof_20260605.py`, `.tmp_drift_transport_proof_20260605.py`.

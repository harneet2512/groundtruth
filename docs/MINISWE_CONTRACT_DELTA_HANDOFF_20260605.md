# Mini-SWE-Agent — contract-DELTA integration handoff (depth)

Hand this to whoever continues the **mini-swe-agent** integration. It mirrors what was done for the
**OpenHands** path, explains the architecture both share, the ways the two scaffolds DIFFER, the exact
shared engine to reuse, the mistakes already made (so you don't repeat them), and the LOCKED verification
that prevents moving goalposts. Read all of it before wiring anything.

---

## 0. The mistake we already made (read first)

We first built change-detection as a **standalone parallel layer** (`drift_hook` / `drift_cli` /
`<gt-drift>`), using a **graph reindex diff**: freeze a baseline graph, `gt-index -file` reindex after the
edit, diff graph properties. It went live on OpenHands and **delivered a FALSE POSITIVE** — it flagged 4
functions the agent never edited (verified from `output.jsonl`, not telemetry). Root cause: the per-edit
`gt-index -file` reindex re-parses the *whole file*; a **full-build baseline vs incremental-reindex**
mismatch manufactured phantom diffs on unedited functions.

**Two lessons, both binding for mini-swe:**
1. **Do NOT build a parallel layer with its own reindex.** Reuse the shared engine (§2). The diff must use
   **same-path before/after indexing** (index old and current the *same* single-file way) so an unedited
   function is byte-identical and never diffs.
2. **"Delivered" ≠ "correct" ≠ "works."** Verify CORRECT (delta matches the agent's `git diff`, zero false
   positives) and CONSUMED (agent reacts) from `output.jsonl` — never from telemetry/`utilization_score`.

---

## 1. What the capability is

After the agent edits a source file, surface **what its edit CHANGED in the function's behavioral
contract** — return shape, raised exceptions, dropped guard/boundary, side-effects — and **who depends on
it** (verified callers, structural twin not updated). It is the "you broke an interface other code relies
on" signal. Zero test contact, no execution, deterministic, LLM-free, language-agnostic.

Honest ceiling: it detects **structural** contract changes (return/raise/guard/twin). It is **blind to
implicit-semantic** changes (e.g. an internal clamp that removes a stdlib `OverflowError`). Don't claim it
catches those.

---

## 2. The shared engine — REUSE THIS, do not reinvent

`src/groundtruth/hooks/contract_delta.py :: compute_delta(graph_db, file_rel, *, repo_root, diff_text,
current_content=None) -> list[str]`

It is scaffold-neutral and already built + tested. It:
1. recovers the **pre-edit** content from `git show HEAD:<file>` (fallback: reconstruct from the unified diff);
2. reads **current** content from disk;
3. indexes BOTH via the **same single-file full-build path** into scratch dbs (`_index_one`);
4. diffs the **full property depth** (`_DELTA_KINDS`: return_shape, exception_type, guard_clause,
   boundary_condition, conditional_return, exception_handler, side_effect, resource_pattern, field_read,
   call_order) — only genuinely-changed functions surface (same-path ⇒ no phantom drift, no scoping needed);
5. attaches the consequence from the **current** `graph_db`: verified caller count (categorical filter,
   no `name_match` laundering) + `structural_twin`/serde "twin not updated";
6. returns `[CONTRACT-DELTA] <func> …` lines, or `[]` (correct-or-quiet). Never raises.

**Both scaffolds call this same function.** Only the transport differs.

Requirements it needs in the environment: `groundtruth` importable (PYTHONPATH); `gt-index` available
(`GT_INDEX_BINARY` env or on PATH); `git` + the repo checked out; python3.10+.

---

## 3. OpenHands integration (PUSH) — what was done, for reference

OH is in-container with a post-tool hook. The delta lives **inside L3 Post-Edit**
(`post_edit.generate_improved_evidence`), which already owns contract-on-edit:
- It already has the pre-edit file (`old_content_text` via `_git_show_head_file`/diff), an evidence budget,
  a categorical edge filter, a G7 isolation gate, and dedup.
- We call `compute_delta(db_path, file_path, repo_root=repo_root, diff_text=diff_text)` **once per file** and
  `output_parts.insert(0, …)` so the delta **leads** the evidence block (primacy, Lost in the Middle 2024).
- `[CONTRACT-DELTA]` was added to `_G7_PILLAR_KEEP_PREFIXES` (Contract-pillar marker — survives isolation).
- The old `_drift_adv`/`drift_advisory` prefix was **removed** (it was the false-positive source).
The agent SEES it as part of the `<gt-evidence trigger="post_edit:…">` observation pushed after its edit.

---

## 4. Mini-SWE-Agent integration (PULL) — what is DIFFERENT and how to wire it

**The core difference:** mini-swe-agent is an **external subprocess** (`pier` spawns `mini-swe-agent
--task=…`); there is **no in-process post-tool hook** to push an observation, and pier only reads the
trajectory **after** it exits. So OH's push (inject an observation) does NOT exist here. mini-swe-agent's
paradigm is **bash commands** → the delta is delivered by **PULL**: the agent runs a `gt` command itself.
This is the existing `gt_hook.py` agent-invoked model, and it matches the user's rule "GT tells the agent,
the agent runs it."

| | OpenHands (push) | mini-swe-agent (pull) |
|---|---|---|
| Trigger | in-container post-edit hook | agent runs `gt drift <file>` |
| Delivery | injected `<gt-evidence>` observation | command stdout the agent reads |
| Home | `post_edit.generate_improved_evidence` (L3) | a `gt` CLI over `compute_delta` |
| Graph | wrapper builds graph.db in-container | standup builds graph.db; `GT_GRAPH_DB`/`GT_INDEX_BINARY` |
| "before" state | `old_content_text` from git HEAD | `git show HEAD:<file>` inside `compute_delta` |

**Wiring steps (mini-swe branch):**
1. **Standup (once, before the subprocess):** in `deepswe-pier/.../mini_swe_agent.py` (gated by
   `GT_DRIFT_ENABLED`), index the repo to `/tmp/gt_graph.db`, export `GT_INDEX_BINARY` and `GT_GRAPH_DB`,
   install a `gt` wrapper at `$HOME/.local/bin/gt`, and append the task instruction. (The scaffolding for
   this already exists in `_setup_gt_drift` + `_GT_DRIFT_INSTRUCTION` — **but repoint it to the DELTA**.)
2. **The `gt drift <file>` command** must call the SHARED engine:
   `compute_delta(GT_GRAPH_DB, <file>, repo_root=<repo>, diff_text=<git diff -U0 HEAD -- file>)` and print
   the `[CONTRACT-DELTA]` lines. **Do NOT** reuse the old `drift_cli`→`drift_advisory` path (that is the
   retired reindex-diff). Replace `drift_cli`'s internals with `compute_delta`, or write a thin new entry.
3. **Instruction:** tell the agent to run `gt drift <path>` after editing and before finishing. It reads the
   code graph only — no tests, no patch edits.
4. **Same payload** as OH (both call `compute_delta`) — only the transport (command stdout vs pushed obs).

**Do NOT** stand up a separate baseline freeze / reindex for the delta — `compute_delta` gets "before" from
`git HEAD` and indexes both sides itself. The old `graph.db.orig` freeze is obsolete; do not port it.

---

## 5. The architecture both scaffolds sit on (where it comes from)

Topological (DOC_OF_HONOR):
- **Layer 0 (gt-index → graph.db)** is the source of ALL signal: 23 property kinds, edges+trust, closure
  (transitive reach depth 1-3), assertions, structural/serde twins, cochanges. Nothing downstream computes
  contract facts — they all READ graph.db.
- **Layer 2 (passive delivery)** consumes graph.db into agent observations: L1 brief, **L3 post-edit
  (contract-on-edit home)**, L3b post-view, L4a auto-query, L5 governor, L6 reindex/pre-submit, grep intercept.
- The motivation (Layer 6 research) is the 30-category failure taxonomy core finding: *"local correctness
  without global awareness — agents write locally correct code that breaks callers, contracts, cross-file
  invariants."* The delta is the signal for exactly that.

mini-swe and OH are both **Layer-2 consumers** of the same Layer-0 depth; they differ only in transport.

---

## 6. The DEPTH (don't build shallow)

A real graph holds (example real index: 22,202 properties over ~2,000 functions): **caller_usage** (how
each caller consumes it — `destructure_tuple`, `boolean_check`, …), return_shape, fingerprint,
conditional_return, boundary_condition, field_read, guard_clause, exception_handler, side_effect,
resource_pattern, call_order, plus edges+trust, **closure** (blast radius), **structural_twin/serde**.
Diff the depth, attach the consequence — "return shape tuple→dict; 6 callers `destructure_tuple` it; twin
`set_X` not updated" — not a bare "return changed."

---

## 7. Verification — LOCKED, do not move goalposts

`docs/CONTRACT_DELTA_ACCEPTANCE_LOCKED_20260605.md` is pre-registered and committed. Use it verbatim:
- **Fair probe:** baseline-FAILURE task + gold changes an explicit return/raise/guard on a called function +
  not pre-localized. (`beets-5495`, `loguru-1297` are rejected on record.)
- **Three gates from `output.jsonl`:** DELIVERED (`[CONTRACT-DELTA]` present) + CORRECT (**zero** functions
  flagged that aren't in the agent's `git diff`) + CONSUMED (agent reacts; `utilization_score` does NOT count).
- **Two separate verdicts:** "works" (gates 1+2+3) vs "produces flips" (resolves baseline=NO, right
  trajectory). "Works" ≠ "flips."
- **Kill:** any false positive → BROKEN; consumed-but-0-flips → report no flip value, don't fish.

Apply the SAME gates to the mini-swe pull path (the agent's `gt drift` output is the delivered text; check it
against the agent's diff).

---

## 8. File manifest + status (2026-06-05)

| File | Role | Status |
|---|---|---|
| `src/groundtruth/hooks/contract_delta.py` | shared engine `compute_delta` | BUILT, 3 real-binary tests pass |
| `src/groundtruth/hooks/post_edit.py` | OH push: L3 `[CONTRACT-DELTA]` + G7 keep | WIRED, 28 regression tests pass |
| `src/groundtruth/pretask/curation_map.py` | shared queries (`build_function_map`, categorical filter) | reused |
| `deepswe-pier/.../mini_swe_agent.py` | mini-swe pull standup + instruction | EXISTS but points at retired drift — **REPOINT to compute_delta** |
| `src/groundtruth/hooks/drift_hook.py` / `drift_cli.py` | retired reindex-diff | DELETE after repointing mini-swe |
| `docs/CONTRACT_DELTA_L3_DESIGN_20260605.md` | design + research + ceiling | done |
| `docs/CONTRACT_DELTA_ACCEPTANCE_LOCKED_20260605.md` | locked acceptance gates | done |

**Mini-swe TODO:** repoint `gt drift` → `compute_delta`; delete `drift_hook`/`drift_cli` + the obsolete
`graph.db.orig` freeze; live-verify the pull path against the locked gates (it's gated OFF by
`GT_DRIFT_ENABLED` until then).

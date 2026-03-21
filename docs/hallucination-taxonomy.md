# Hallucination Taxonomy

Classification of failure modes that GroundTruth detects and mitigates in AI coding agents.

---

## Two Failure Modes

AI coding agents produce two fundamentally different kinds of hallucination. They require different detection mechanisms, different mitigations, and different metrics.

### Code Hallucination

**Definition:** The agent writes code referencing symbols, methods, imports, or signatures that do not exist or are structurally incorrect relative to the actual codebase.

**Root cause:** Wrong belief about what the codebase contains. The agent's training data or context window does not accurately reflect the current state of the repository.

**Detection:** Structural comparison between generated code and the symbol index. Deterministic in most cases.

**Mitigation:** Trust substrate (runtime introspection), contradiction engine (positive-evidence checks), arity validation, freshness tracking.

### Workflow Hallucination

**Definition:** The agent takes an action that is redundant, counterproductive, or inappropriate for its current task phase. The code it would write may be correct, but it never gets there because it wastes its step budget.

**Root cause:** Wrong belief about what action to take next. The agent lacks a model of its own progress and cannot distinguish productive exploration from spinning.

**Detection:** Session state tracking -- counting tool calls by category, detecting loops, classifying task phase.

**Mitigation:** Communication state machine (phase framing), abstention policy (suppressing low-value findings), edit-site resolution (directing edits to canonical locations).

---

## Code Hallucination Subtypes

| Subtype | Example | GT Subsystem | Module Path |
|---------|---------|-------------|-------------|
| Wrong import path | `from auth import hash` when the symbol lives in `utils.crypto` | Contradiction engine | `src/groundtruth/validators/contradictions.py` |
| Invented symbol | `user.get_profile()` when the class has no such method | Trust substrate (runtime introspection) | `src/groundtruth/core/trust.py` |
| Wrong signature (arity mismatch) | `create_user(name)` when the function requires `(name, email)` | Arity mismatch detection | `src/groundtruth/validators/contradictions.py` (`_count_required_params`) |
| Stale reference | Symbol was moved or renamed in a recent commit; agent uses the old location | Freshness tracking | `src/groundtruth/index/freshness.py` |
| Wrong module (re-export confusion) | Editing a re-export barrel file instead of the canonical definition | Edit-site resolver | `src/groundtruth/analysis/edit_site.py` |
| Override violation | Subclass method signature diverges from parent | Obligation engine (override_contract) | `src/groundtruth/validators/obligations.py` |

### Detection Hierarchy

For each code hallucination, detection follows a cascade:

1. **Deterministic structural check** -- symbol exists in index? Signature matches? Import path resolves? Cost: zero (SQLite lookup). Handles ~85% of cases.
2. **Levenshtein close-match** -- if the symbol is not found, is there a symbol within edit distance 3? Cost: zero. Module: `src/groundtruth/utils/levenshtein.py`.
3. **Cross-index search** -- right symbol name, wrong file path? Cost: zero (SQLite query).
4. **Runtime introspection** (Python only) -- import the module and check `dir()` for dynamically injected members. Cost: near-zero. Module: `src/groundtruth/core/trust.py`.
5. **AI semantic resolver** -- fires only when all deterministic methods fail (~15% of cases). Module: `src/groundtruth/ai/semantic_resolver.py`.

---

## Workflow Hallucination Subtypes

| Subtype | Example | GT Subsystem | Module Path |
|---------|---------|-------------|-------------|
| Search spinning | 5+ consecutive search/reference/trace calls without editing | Communication state machine | `src/groundtruth/core/communication.py` (`LoopState.SEARCH_SPINNING`) |
| Check looping | Running validate/check-diff repeatedly, revising code after INFO-level findings | Communication framing | `src/groundtruth/core/communication.py` (`LoopState.CHECK_LOOPING`) |
| Over-revision | Changing correct code based on a low-confidence or soft-info suggestion | Abstention policy | `src/groundtruth/policy/abstention.py` (`EmissionLevel.EMIT_SOFT_INFO` vs `EMIT_HARD_BLOCKER`) |
| Wrong edit site | Editing a re-export, __init__.py barrel, or test file instead of the canonical definition | Edit-site resolver | `src/groundtruth/analysis/edit_site.py` |
| Budget waste on duplicative tools | Using `outline` when `cat` already provides the same information | Tool surface design | Addressed by removing/gating redundant tools (see v4.2 eval findings in PROGRESS.md) |

### Detection Mechanism

The communication state machine (`src/groundtruth/core/communication.py`) tracks:

- **Task phase:** `EXPLORING` -> `EDITING` -> `PATCH_EXISTS` -> `TESTED` -> `SUBMITTING`
- **Loop state:** `NORMAL`, `SEARCH_SPINNING` (consecutive search tools exceed threshold), `CHECK_LOOPING` (consecutive check tools exceed threshold)
- **Tool categories:** search tools (`search`, `references`, `find_relevant`, `trace`, `impact`), check tools (`check-diff`, `check_patch`, `validate`), test tools (`test`, `run_tests`)

When a loop is detected, the state machine provides contextual framing text to redirect the agent. This is deterministic -- no LLM calls.

### Abstention as Workflow Protection

The abstention policy (`src/groundtruth/policy/abstention.py`) prevents GT itself from causing workflow hallucinations:

- **EMIT_NOTHING:** Evidence is insufficient. Saying nothing avoids triggering over-revision.
- **EMIT_SOFT_INFO:** Evidence exists but is not conclusive. Framed as informational, not actionable.
- **EMIT_HARD_BLOCKER:** Strong positive evidence of a structural error. Framed as a required fix.

Thresholds: minimum 2 pieces of evidence (`MIN_EVIDENCE_COUNT`), minimum 5 known symbols in a module before trusting the index (`MIN_COVERAGE_THRESHOLD`).

---

## Metric Definitions

### Code Hallucination Rate

**Formula:** (code submissions with provable structural errors) / (total code submissions)

**What counts as provable:** The error must be backed by positive structural evidence from the index. A missing symbol in the index does not count (the index may be incomplete). A symbol confirmed to exist at a different path, with a different signature, or not at all via runtime introspection -- that counts.

**Source:** `src/groundtruth/validators/contradictions.py` (contradiction findings with `confidence >= 0.8`).

### Workflow Hallucination Rate

**Formula:** (tool calls classified as redundant or counterproductive) / (total tool calls in session)

**What counts as redundant:** Consecutive search calls beyond the spinning threshold. Check calls after an INFO-only finding. Validate calls on unchanged code.

**Source:** `src/groundtruth/core/communication.py` (loop state transitions).

### Surfacing Rate

**Formula:** (GT findings that appear in the agent's observable context) / (total GT findings emitted)

**Purpose:** Measures whether the agent even sees GT's output. A finding that is emitted but never read has zero value.

**Source:** `src/groundtruth/grounding/events.py` -- the `GroundingEvent` log tracks `INTERVENTION` (GT emitted a finding), `CONSUMPTION` (the finding appeared in agent context), and `OUTCOME` (what happened next).

### Compliance Rate

**Formula:** (GT findings that the agent followed) / (GT findings that the agent read)

**Purpose:** Measures whether the agent acts on GT's advice. Distinct from surfacing rate -- an agent can read a finding and ignore it.

**Source:** `src/groundtruth/analysis/grounding_gap.py` -- compares briefing symbols against subsequent validation output. The `compliance_rate` field in `briefing_logs` captures this per-interaction.

### Observed from SWE-bench v4.2 (300-task run)

From PROGRESS.md:
- 59% of tasks (177/300) used GT tools voluntarily
- Tasks using GT tools resolved at 41.8% vs 25.2% for non-GT tasks
- Heavy GT usage correlated with budget waste on lost tasks (e.g., 30 calls on django-12125)
- `check` command at 100% adoption in v4.1 consumed turns without proportionate gains (workflow hallucination via check looping)

---

## Evaluation Template

For each task in a diagnostic run, classify:

```
Task ID: ___
Resolution: resolved / not_resolved

1. Did GT surface any findings?
   [ ] Yes → list event IDs from grounding/events.py log
   [ ] No → skip to 5

2. Classification of each finding:
   [ ] Code hallucination (subtype: ___)
   [ ] Workflow hallucination (subtype: ___)
   [ ] Obligation (kind: ___)

3. Did the agent read the finding?
   [ ] Yes (CONSUMPTION event logged)
   [ ] No (INTERVENTION only, no CONSUMPTION)

4. Did the agent follow the finding?
   [ ] Followed → code changed accordingly
   [ ] Ignored → no change or contradictory change
   [ ] Partially followed → used some suggestions

5. Outcome attribution:
   [ ] GT finding directly contributed to resolution
   [ ] GT finding was irrelevant to resolution
   [ ] GT finding caused budget waste (workflow hallucination by GT)
   [ ] No GT involvement
```

The template produces four counts per run:
- **True positive interventions:** GT surfaced a finding, agent followed it, task resolved
- **Ignored interventions:** GT surfaced a finding, agent did not follow it
- **Counterproductive interventions:** GT surfaced a finding that caused budget waste or over-revision
- **Missed opportunities:** GT did not surface a finding that would have helped

---

## Engineering Program Alignment

Mapping tasks from `docs/TASK_REGISTRY.md` and the v0.8 branch plan to hallucination types.

### Code Hallucination Tasks

| Task | Hallucination Subtype Addressed |
|------|---------------------------------|
| Obligation engine (4 kinds) -- DONE | Override violation, constructor symmetry (structural correctness) |
| Wire `_shared_state` into `ObligationEngine.infer()` (Task #1) | Stale reference, shared-state coupling errors |
| Contradiction engine (`validators/contradictions.py`) | Wrong import path, arity mismatch, override violation |
| Trust substrate (`core/trust.py`) | Invented symbol (runtime introspection catches dynamic members) |
| Freshness tracking (`index/freshness.py`) | Stale reference (detects when index entries are outdated) |
| Edit-site resolver (`analysis/edit_site.py`) | Wrong module / re-export confusion |
| Enhanced index summary (Task #11) | Wrong import path (coupling clusters surface related modules) |

### Workflow Hallucination Tasks

| Task | Hallucination Subtype Addressed |
|------|---------------------------------|
| Communication state machine (`core/communication.py`) | Search spinning, check looping |
| Abstention policy (`policy/abstention.py`) | Over-revision (suppresses low-evidence findings) |
| `check-diff` CLI enhanced (Task #12) | Check looping (deterministic-only checks avoid triggering revision cycles) |
| Remove `anthropic` from core deps (Task #5) | Reduces tool surface complexity |
| Split `tools.py` into per-tool modules (Task #7) | Reduces cognitive load on tool dispatch |
| CityView watcher opt-in (Task #6) | Budget waste (off-by-default prevents unnecessary tool invocations) |

### Cross-Cutting

| Task | Both Types |
|------|-----------|
| Grounding events (`grounding/events.py`) | Measures surfacing and compliance rates for both failure modes |
| Grounding gap analysis (`analysis/grounding_gap.py`) | Quantifies how often correct context is ignored (compliance rate) |
| Judgment interface (`core/judgment.py`) | Normalizes obligation output for parity between product and eval harness |
| MCP tool exposure (Task #15) -- DONE | Surfaces obligation findings to agents (code hallucination prevention via tool responses) |

---

## Key Design Principles

1. **Deterministic before probabilistic.** Every detection cascade starts with structural evidence (SQLite lookups, AST checks, runtime introspection). AI resolvers fire last and only when deterministic methods are exhausted.

2. **Silence over noise.** A false positive from GT is itself a workflow hallucination trigger. The abstention policy enforces minimum evidence thresholds before emitting any finding.

3. **Separate detection from framing.** The contradiction engine detects code errors. The communication state machine decides how to present them. A hard structural error gets blocker framing. An ambiguous finding gets soft-info framing or is suppressed entirely.

4. **Measure the full lifecycle.** Emitting a finding is not enough. The grounding event log tracks: was it surfaced? Was it consumed? Was it followed? Was the outcome correct? Without this chain, we cannot distinguish "GT helped" from "GT was ignored."

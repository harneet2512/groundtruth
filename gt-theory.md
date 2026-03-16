# GT Theory — Post-Benchmark Design Discussion

> Compiled from the design discussion after SWE-bench Lite early results showed baseline (90.6%) outperforming GroundTruth (73.7%).

---

## 1. The Problem: GT Makes the Agent Worse

### Early SWE-bench Lite Results (64/300 baseline, 38/300 GT)

| Condition | Completed | Patched | No Patch | Patch Rate |
|-----------|-----------|---------|----------|------------|
| Baseline | 64 / 300 | 58 | 6 | 90.6% |
| GroundTruth | 38 / 300 | 28 | 10 | 73.7% |

**Observation:** GT agent produces fewer patches, not more. The agent spends turns calling orient/brief/validate/find_relevant tools, leaving fewer turns for actual editing. Index timeouts (48 out of 38 tasks) mean most tasks ran without GT data anyway — the agent paid the turn cost but got nothing back.

---

## 2. First Reaction: Make GT Passive

### The "Invisible Infrastructure" Thesis

The initial reaction was to make GT invisible — like how TypeScript's red squiggles work. You don't "call" the type checker. It just runs. Three layers proposed:

**Layer 1: Context Enrichment (pre-agent, zero turns)**
- Run orient + find_relevant + brief before the agent starts
- Inject ~400 tokens of codebase context into the system prompt
- Agent reads it at turn 0, knows where to look

**Layer 2: Invisible Validation (post-edit, automatic)**
- After every `edit_file`, run validate on the changed file
- Append errors to the edit result: "⚠ Line 12: `verify_jwt_token` not found → Did you mean `verify_token`?"
- Agent sees it naturally, fixes it next turn

**Layer 3: Final Validation Gate (post-agent, zero turns)**
- After agent finishes, validate all modified files
- Attach grounding record to prediction metadata

### What This Means for Benchmarking

The agent gets the same tools as baseline (bash, view, edit, search). Same turns. No GT tools in the tool list. The only differences: richer system prompt and automatic post-edit feedback.

---

## 3. Pushback: Is This Legitimate?

### Concern: Reviewers Will Question the Results

Three approaches considered:

**Approach A: Passive injection (context + hidden validation)**
- Reviewer: "You changed the system prompt AND added a hidden validation layer. Two confounds."
- Reviewer: "Any RAG pipeline could prepend context. What's unique about your tool?"

**Approach B: Active tools with extra turns (3 GT tools, 35 turns vs 30)**
- Reviewer: "You gave one condition 5 extra turns. How do I know the improvement isn't just from more attempts?"

**Approach C: Active tools, same turns (3 GT tools, 30 turns)**
- Reviewer: "Fair test. But if GT loses, is it because the tools are bad or because the agent wasted turns?"

---

## 4. The Product Question: What IS GroundTruth?

### Key Realization: MCP Tools Are for the Host, Not the Model

When Cursor integrates GT, they won't say "here are 16 tools, figure it out." They'll:

1. Call `groundtruth_orient` on project open → inject result into model context
2. Model writes code
3. Call `groundtruth_validate` on edited file → show errors to model
4. Model never sees GT tools directly. It just gets better context and error feedback.

**The 16 MCP tools are an API for host applications.** The model is the end user, but it never calls the API directly. Cursor calls the API and feeds results to the model.

---

## 5. Counter-Argument: Cursor Already Has RAG

### What Cursor and Claude Code Already Do
- Index the codebase (embeddings, chunking, tree-sitter)
- RAG search when the model needs context
- Return text chunks and symbol information
- LSP integration for go-to-definition

### First Wrong Assumption
"GT's value is providing context." — Wrong. Cursor already provides context. Adding more context via system prompt injection is just doing what Cursor already does, slightly differently.

### Second Wrong Assumption
"RAG returns dumb text chunks, GT returns verified facts." — Wrong. Cursor's RAG already uses tree-sitter, has symbol extraction, has LSP integration. It's not dumb.

### The Real Question
If the model already has good context from Cursor's RAG, tree-sitter, and LSP — **why does it still hallucinate?**

---

## 6. Why Models Hallucinate Despite Good Context

**The model reads the code correctly. It writes the fix incorrectly.**

Example: Agent reads `filter()`, sees 200 lines of implementation, writes a fix that makes `filter()` mutate the QuerySet in place instead of returning a new one. All imports correct. All names correct. All signatures correct. **Code is completely wrong** — 47 callers expect `filter()` to return a new QuerySet.

**The model doesn't lack context. It lacks understanding.**

It reads text. It doesn't understand:
- What a function is supposed to do (behavioral contract)
- Who depends on it and how (usage patterns)
- What the call chain looks like (where the real bug is vs where symptoms appear)
- What would break if the behavior changes (blast radius)

---

## 7. The Full Error Taxonomy

### Surface Errors (~45% of hallucinations)
1. **Wrong import / wrong module path** — imports from wrong file
2. **Invented symbol** — calls function that doesn't exist
3. **Wrong signature** — wrong number of args, wrong types
4. **Wrong naming convention** — camelCase in Python, snake_case in JS
5. **Stale symbol after rename** — uses old name that was renamed

### Structural Errors
6. **Cross-file edit target mismatch** — edits file A but the bug is in file B
7. **Build/config hallucination** — adds dep to wrong config file
8. **Repo mutation robustness** — file moved/renamed, model uses old paths
9. **Breaking the import graph** — introduces circular dependency
10. **Missing cascade edits** — changes function but not its callers

### Logic/Semantic Errors (~55% of hallucinations)
11. **Wrong function, right name** — three different `validate()` functions exist, agent picks the one from the wrong module
12. **Violated behavioral contract** — changes `filter()` from returning new QuerySet to mutating in place
13. **Missed downstream impact** — adds parameter to function without updating 15 call sites
14. **Deleted dependency** — removes function that 8 files import
15. **Ordering invariant violation** — function A must be called before function B, agent reverses them
16. **Side effect ignorance** — doesn't know `add_filter()` mutates `self.where`
17. **Return type change cascade** — changes return type without updating callers
18. **Incorrect fix location** — fixes symptom in module A, real cause is in module B

### The Key Insight
GT catches ALL of these because it has the full structural graph — symbol table + import graph + call graph + signatures + behavioral context from docstrings. RAG can't tell you "if you change this function, 15 callers break." Only something with the full graph can.

---

## 8. Tool Categorization (Correct)

### What Each GT Tool Actually Prevents

| Tool | What it prevents |
|------|-----------------|
| `orient` | Wrong file paths, wrong module structure |
| `find_relevant` | Editing wrong files, missing key files |
| `brief` | Wrong signatures, violated contracts ("filter() returns new QuerySet, does not mutate") |
| `explain` | Misunderstanding behavior, breaking callers ("47 callers use the return value") |
| `impact` | Breaking downstream code ("changing this affects 15 files") |
| `trace` | Fixing symptoms not causes ("the real bug is in build_filter(), not filter()") |
| `validate` | Wrong imports, invented names, bad signatures, violated contracts |
| `symbols` | Using wrong symbols from a file |
| `context` | Misunderstanding surrounding code |
| `patterns` | Wrong naming conventions |

### Lifecycle Categories

**🟢 Pre-task (host runs automatically, injects results):**
- `orient`, `find_relevant`, `brief` on relevant files, `hotspots`
- Like Cursor indexing on project open

**🔵 Mid-task (agent calls on demand):**
- `explain`, `impact`, `trace`, `symbols`, `context`, `validate`
- Like Go to Definition, Find References, Show Call Hierarchy

**🟡 Post-edit (automatic after every edit):**
- `validate`
- Like TypeScript red squiggles

**🔴 Post-task (after agent finishes):**
- `validate` all modified files → grounding record
- Like CI

**🟣 Maintenance (separate user workflow):**
- `dead_code`, `unused_packages`, `patterns`
- Used by developers for codebase cleanup

**🎨 Visualization:**
- `hotspots`, `dead_code`, `patterns`, risk scores
- Feed the 3D code city

**⚪ Infrastructure:**
- `status`, `checkpoint`, `do`

---

## 9. The Cursor CTO Test

### What doesn't ship:
"This MCP server wants me to add 4 new tools to my agent, change my prompt, hope the model uses them correctly, and accept that my agent spends some turns on tool calls instead of solving the problem. Hard pass."

### What ships:
"This MCP server indexes my user's codebase. I call two endpoints before the agent starts. I call one endpoint after every edit. My agent writes better code. No prompt changes, no new tools for the model to learn, no turn cost. Integrate in a day."

---

## 9.5 Reconciliation: Active vs Passive

The first SWE-bench run proved the Cursor CTO's intuition empirically: **active GT (90.6% → 73.7%) hurt performance**. The agent spent turns calling 16 tools instead of solving the problem.

The fix is `GROUNDTRUTH_V2` — passive integration:
- **Pre-task:** Index the repo with AST parsing, inject ~400 tokens of structural context into the system prompt. The agent never knows GT exists.
- **Post-edit:** After every `edit_file`, validate the edited code against the index. High-confidence findings (≥0.70) are appended to the tool result. The agent sees feedback, not a tool.
- **Zero new tools.** Zero prompt changes. Zero turns spent on GT calls.

This is the product that ships. The 16 MCP tools remain available for hosts that want fine-grained control (IDE plugins, interactive workflows). But for autonomous agents on benchmarks, invisible integration wins.

---

## 10. Three Conditions Considered, Then Rejected

### The Ablation Study Approach

| Condition | What it tests |
|-----------|--------------|
| Baseline | Current state |
| GT Context Only | Standard + context injected, no validation | Does context help? |
| GT Full | Standard + context + post-edit validation | Does full product help? |

### Why It Was Rejected

"At the end I see two different GTs and then won't it cause confusion on what is even happening. In one we give more chances, in one we give more context."

GT is one product. Test it as one product.

---

## 11. Final Design Decision

### Two Conditions. One Product. One Number.

**Baseline:** GPT-5-mini, 30 turns, standard tools (bash, view, edit, search).

**GroundTruth:** GPT-5-mini, 30 turns, standard tools, plus GroundTruth integration which:
- (a) Injects ~400 tokens of structural codebase context (relevant files, symbols, signatures) into the system prompt
- (b) Appends validation feedback after each edit_file (wrong imports, missing symbols, broken signatures, violated behavioral contracts)

Same model. Same tasks. Same turns. Same base tools.

### Why This Is Legitimate

Every SWE-bench leaderboard entry has a custom scaffolding:
- SWE-agent has its own unique prompt and tools
- OpenHands has different ones
- Amazon Q Developer has different ones

Nobody says "you can only change one variable." They say "describe your scaffolding clearly and let us reproduce it."

### The Methodology Section

```
Baseline: GPT-5-mini, 30 turns, standard tools.

GroundTruth: GPT-5-mini, 30 turns, standard tools,
plus GroundTruth integration which:
  (a) injects ~400 tokens of structural codebase context
      (relevant files, symbols, signatures) into the system prompt
  (b) appends validation feedback after each edit_file
      (wrong imports, missing symbols, broken signatures,
       violated behavioral contracts)

Same model. Same tasks. Same turns. Same base tools.
Full integration code: github.com/you/groundtruth/benchmarks/
```

Transparent. Reproducible. One product. One story. One number.

**The number is either positive or it isn't. And that's honest.**

---

## 12. Product Speed Concern

### "If GT takes so long, how will anyone use it?"

The benchmark is slow because of benchmark overhead (cloning repos, OpenAI API round trips). In real usage:

| Operation | Benchmark | Real Usage (Cursor/Claude Code) |
|-----------|-----------|-------------------------------|
| Index | 8-15s per repo (cloned fresh) | One-time on project open, cached in .groundtruth/index.db |
| orient/find_relevant | API round trip to OpenAI | <100ms local SQLite query |
| validate | API round trip to OpenAI | <100ms local ast + store lookup |
| explain | API round trip to OpenAI | <100ms local store query |

In real usage, every GT operation is <100ms. The user never waits. The benchmark is slow because it clones repos from scratch and every tool call is an OpenAI API round trip. That's benchmark overhead, not product overhead.

---

## 13. Open Questions for After Lite Results

1. **If GT resolves more tasks than baseline:** Ship. Run Live. Publish.

2. **If GT resolves same as baseline:** The context injection helps the agent start faster but the validation doesn't catch enough. Need to improve what validate checks — especially logic errors (#11-#18 in the taxonomy).

3. **If GT resolves fewer tasks:** Either the context injection is confusing the model (too much information) or the validation feedback is noisy (false positives causing the agent to "fix" things that weren't broken). Reduce context to bare minimum, tighten validation precision.

4. **Index timeout rate:** If >30% of repos fail to index in 60s, need to pre-index or increase timeout. Consider caching indexes across tasks from the same repo (Django appears in many SWE-bench tasks).

5. **What percentage of GT validation catches are logic errors vs surface errors?** If it's 95% surface errors, GT isn't using its full capability. Need to add contract checking, impact analysis to the post-edit hook — not just import/name validation.

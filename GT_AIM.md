# GT AIM — GroundTruth Product Thesis & Direction

> This document captures the core product thesis, validated learnings, and strategic direction.
> Every session should read this before making decisions.

---

## What GT Is

An MCP server that gives AI coding agents **deterministic codebase intelligence** — for any language, any IDE, zero AI cost. The graph.db is infrastructure. The product is **decision quality + speed** for coding agents.

## Core Thesis

**Agents make errors because they have incomplete codebase understanding. GT provides complete structural knowledge from the call graph, eliminating errors that come from not knowing what exists in the codebase.**

These errors don't go away with smarter models because:
1. Context windows are always finite — can't hold a 10M line codebase
2. Codebases change every commit — training data is frozen, the graph is live
3. Search is probabilistic — grep matches text, the graph captures structural relationships
4. Agents hallucinate over long sessions — GT's graph doesn't degrade at turn 50

## Positioning (Validated by Opus vs Codex 4-Round Debate, 2026-04-04)

**GT is a decision intelligence layer for coding agents, not a retrieval layer.**
- WarpGrep/SWE-grep/Cursor retrieve candidates. GT returns **governed answers with calibrated confidence and deterministic abstention.**
- Compete on **decision quality and structural certainty**, not search speed.
- Frame: "eliminates a major class of context errors" — not "solves all errors."
- GT's output is deterministic. The agent consuming it may still hallucinate — GT reduces, doesn't eliminate, agent errors.
- **Moat: decision intelligence with uncertainty-aware gating. Not tool access.** Everyone will have MCP tools. GT owns governed structural verdicts.

### Differentiation from Frontier Labs
| Tool | What it does | GT's difference |
|------|-------------|-----------------|
| Cursor indexing | AST + embeddings, semantic search | Locked to Cursor. GT is universal MCP. |
| SWE-grep (Cognition) | RL-trained parallel search | Locked to Devin. Search-focused. |
| WarpGrep (Morph) | MCP search subagent, +2.1pp | Search-focused. No structural verdicts. |
| CLAUDE.md / AGENTS.md | Static context files | Human-written, no confidence, no graph queries. |
| **GT** | **Deterministic structural intelligence** | **Open, universal, governed answers, tiered confidence** |

## What GT Solves (Major Class of Context Errors)

GT eliminates errors caused by incomplete codebase understanding:
- Breaking a caller the agent didn't know existed
- Missing a dependency it didn't grep for
- Using a function signature that changed 2 commits ago
- Editing the wrong copy of a duplicated function name
- Not knowing a "simple" change cascades to 47 files
- Wrong imports on unfamiliar repos
- Wrong naming conventions
- Missing test files the agent didn't find

**GT does NOT solve only import errors or file localization.** If GT only solves a narrow error class, one model update kills us. GT solves the entire category of structural misunderstanding errors.

## Key Principle: Quality of Determinism

GT never hallucinates. It's a deterministic tool. But determinism has quality levels:

**GT must always tell the truth about WHAT IT KNOWS and HOW SURE IT IS.**

### Tiered Confidence System

GT is useful at EVERY confidence level, not just when certain:

| Confidence | What GT knows | What GT tells agent |
|-----------|---------------|-------------------|
| **High** (1 candidate dominates) | "Definitely this" | `TARGET: auth.py:47` — agent goes straight there |
| **Medium** (2-3 candidates) | "One of these" | `LIKELY: auth.py, session.py, middleware.py` — agent checks 3 not 500 |
| **Low** (5-10 candidates) | "Probably this area" | `SCOPE: 7 files in auth/ package` — narrows search |
| **Zero** | Nothing useful | Stay silent — don't waste context |

This applies to ALL domains: files, functions, callers, imports, tests, return types.

Even "I'm not sure but it's one of these 3" is better than the agent grepping the entire repo. The 3 candidates are a **deterministic fact** from the graph — not a guess.

### Self-Calibrating Confidence (Z-Score) — Learned from BM25 Blunder

**The BM25 static threshold blunder (v21-final):** We set a static confidence threshold of 0.65 based on a gap-based formula. It silenced 100% of briefings across 173 tasks. The formula didn't account for the fact that BM25 scores mean completely different things across repos — a score of 5.0 in a 50-file repo is a strong signal, in a 5000-file Django repo it's noise. Static thresholds on BM25 are fundamentally broken.

**The fix: Z-score (dynamic per repo).** Instead of "is this score above 0.65?", ask "is this score a statistical outlier in THIS repo's distribution?"
- Compute mean and std of ALL file scores in the repo
- Z-score = (top_score - mean) / std
- z > 2.0 → top file is genuinely unusual → HIGH confidence
- z = 1.0-2.0 → some signal → MEDIUM confidence
- z < 1.0 → noise → LOW confidence (still useful: give scope)
- No positive scores → SILENT

This self-calibrates: same threshold works for 50-file repos and 5000-file repos. No magic numbers that break across repo sizes.

**RULE: Never use static thresholds on scores that vary by repo. Always use distribution-relative measures.**

## Delivery: Conditional Push + Pull

### Hook-Based Push (Zero Turn Cost)
- Evidence injected into command output agent already reads
- Fires ONLY when GT has decision-critical signal
- Agent can't miss it — guaranteed delivery
- Risk: context window tax if too noisy

### MCP Tools (Pull)
- 16 tools available for agent to query when it wants
- Agent controls when to call — no context waste
- Risk: agents rarely call tools they don't know they need

### The Hybrid
- **Push** when GT is confident and signal is decision-critical
- **Pull** available always for deeper investigation
- Push must be extremely selective: 1-4 lines, rare, high signal

## What Research Says

### Supports GT's Thesis
- **Codified Context (Feb 2026)**: Pre-synthesized project knowledge → zero persistence bugs across 74 sessions, 29% runtime reduction. Works on REAL projects writing NEW code.
- **Cursor's indexing**: AST-based codebase indexing is core architecture. Agents need cross-file understanding.
- **WarpGrep (+2.1pp)**: MCP server improving agent performance. Proves the MCP delivery model works.
- **Guardrails compound**: 95% single-step accuracy → 60% over 10 steps. Preventing ONE wrong turn saves 3-5 recovery turns.
- **SWE-agent linter gate (+10.7pp)**: Deterministic "NO" signals beat information dumps.
- **Cognition**: Agents spend 60% of time on search/context retrieval.

### Challenges GT's Approach
- **ContextBench**: Block-level retrieval F1 stays below 0.45 regardless of sophistication.
- **AGENTS.md regression (-0.5 to -2pp)**: Pre-computed context dumps can HURT on SWE-bench. BUT this is for bug-fixing, not new code.
- **Static analysis + tests = near zero marginal**: On SWE-bench. Does NOT apply to untested code.
- **RepoGraph: +2-3pp**: Dependency graphs help but modestly on benchmarks.

### Key Distinction
SWE-bench measures bug-fixing in well-tested, memorized repos. GT's value shows on:
- **Writing new code** (no tests to verify)
- **Unfamiliar repos** (model can't guess from training data)
- **Multi-file changes** (cross-file deps matter)
- **Long sessions** (agent context degrades, graph doesn't)

## BM25 Blunder — Why Static Thresholds Are Banned

The static confidence threshold (0.65) silenced 100% of briefings on the Live Lite run. GT became invisible. The root cause: BM25 file scores are NOT comparable across repos.

A score of 4.2 in a 200-file repo means something completely different than 4.2 in a 50,000-file repo. A static threshold of 0.65 on the confidence formula works for some repos and silences everything on others.

**Rule: GT MUST NEVER use static thresholds for confidence gating.**

Research backing: Dynamic thresholding (MSCAD, 2026) outperforms fixed thresholds by F1 +0.06 on average. The improvement is largest when score distributions are similar — which is exactly GT's problem (many files score similarly, only the relevant one stands out).

**Solution: Z-score per repo.** Z-score measures how many standard deviations the top file is from the mean OF THAT REPO'S file scores. A z-score of 2.5 means "this file is clearly different from the pack" regardless of whether the raw score is 4.2 or 42.0. Self-calibrating. No tuning needed per repo.

Z-score tiering (standard statistical thresholds):
- z >= 2.5 = HIGH (statistical outlier) — emit TARGET with evidence
- z >= 1.5 = MEDIUM (unusual) — emit LIKELY FILES (top 2-3)
- z > 0 = LOW (some signal) — emit SCOPE (top 5 candidates)
- z <= 0 = SILENT — no signal, agent explores freely

## Incremental Re-Indexing — Eliminate Staleness

During a benchmark task, the agent makes 5-15 edits. After the first edit, graph.db is stale. GT provides evidence about the OLD code while the agent works on NEW code.

**Solution: Diff-aware validation.** Don't rebuild graph.db. Parse ONLY the edited file with tree-sitter (~5ms), compare new signatures against old signatures in graph.db, and report what CHANGED. This is what every IDE (Neovim, Zed, Helix) does — tree-sitter incremental parsing on every keystroke. GT does it once per edit, on one file.

**Differentiator vs Cursor:** Cursor's embeddings index takes minutes to update. GT's tree-sitter validation takes milliseconds. On a 10-edit task, GT's structural knowledge stays current. Cursor's drifts.

**Implementation (v21-definitive):** Two-layer approach:
- Layer 1 (all 31 languages): Compare graph.db stored signature strings (from Go indexer's `extractSignature()`) against the new file on disk
- Layer 2 (Python): Use `ast.parse()` for richer additive-vs-breaking signature change detection

Verdicts: BREAKING (signature changed, callers affected), DELETED (function removed, callers will break), SAFE (additive change, callers OK), NEW (function added).

## Graph Freshness: Eliminate Staleness, Don't Warn About It

**Stale graph data is harmful.** A confident wrong answer from a stale graph is worse than no answer. Codex's "plausible authority" risk — developers/agents over-trusting tooling that looks definitive but has gaps.

**Current state:** `check_staleness()` compares graph.db mtime vs source file mtime. Warns if stale. File hashes infrastructure exists in Go indexer (`file_hashes` table).

**Target state: The graph should NEVER be stale.**
- Incremental re-index on file change: hash changed files, re-parse ONLY those, re-resolve affected edges
- Time: <1 second for single file change (vs 15-30s full rebuild)
- Trigger options: file watcher, git hook, agent edit hook
- After agent edits a file, GT re-indexes THAT file before answering questions about it

**This is a differentiator:** Cursor's embedding index takes minutes to update. GT's tree-sitter index can update per-file in milliseconds. The `file_hashes` table + `InsertFileHash()`/`GetFileHash()` already exist in `gt-index/internal/store/sqlite.go:195`. Just needs incremental logic in the Go indexer.

**RULE: Rather than warning about staleness, eliminate it. Never serve stale data.**

## What NOT to Do

1. **Don't be a TDD tool** — test-driven feedback can be a feature but not the whole product
2. **Don't compete with grep** — agents already have grep. GT provides structural relationships grep can't.
3. **Don't dump context** — passive information dumps regress performance (AGENTS.md finding)
4. **Don't use static thresholds** — confidence must self-calibrate per repo
5. **Don't measure only on SWE-bench** — it's the least favorable benchmark for GT. Real-world coding tasks are the target.
6. **Don't solve narrow error classes** — one model update obsoletes narrow solutions. Solve the whole category.
7. **Don't add AI layers** — deterministic, zero-cost queries are the moat. AI is optional.

## v1.0.1: LSP Precision + Ego-Graph Navigation

### LSP Edge Resolution (Layer 2)
After tree-sitter indexing, GT sends unresolved call sites to language-specific LSP servers
via `textDocument/definition`. This upgrades name-match edges (60-76% of all edges, mostly wrong)
to LSP-verified edges (confidence 1.0).

**Edge lifecycle after LSP:**
- same_file: 15-18% (unchanged, confidence 1.0)
- import: 9-22% (unchanged, confidence 1.0)
- lsp: 40-55% (NEW — verified by type checker, confidence 1.0)
- name_match: 5-10% (REDUCED — only for languages without LSP server, confidence 0.2)

**Schema addition:** `call_sites` table stores exact (file, line, column) of unresolved calls.
Go indexer captures column from `tree-sitter node.StartPoint().Column`.

**Graceful degradation:** If no LSP server is installed for a language, name-match edges remain
at their original confidence. GT never fails because of a missing server.

### Ego-Graph Retrieval (Layer 3)
Replaces BM25+z-score+graph-boost briefing pipeline with one mechanism: BFS from seed entities
through verified edges, returning a structural neighborhood as a readable map.

**Three hard caps prevent BFS explosion:**
1. Fan-out: max 10 neighbors per node per direction
2. Total nodes: BFS stops at 30 visited
3. Output lines: max 8 lines in structural map

**Adaptive edge filtering:** Per-language check — if LSP edges exist for that language,
traverse only verified edges. If no LSP, traverse all edges (degraded but functional).

**Research backing:** RepoGraph (ICLR 2025) +2.3pp with ego-graphs on SWE-bench Lite.
GT targets better via LSP-verified edges.

## Benchmark Strategy

- SWE-bench Verified: prove zero dampening + small positive delta
- SWE-bench Live Lite: prove value on decontaminated repos where model can't cheat
- Real-world coding tasks: the actual target — new features, refactors, multi-file changes
- Measure: wrong turns prevented, turns saved, not just task completion

## Build Priority (From Opus vs Codex Debate, 4 rounds)

### Phase 1: Decision-Resolving Answers (IMMEDIATE)

**Build a "Safe Change Advisor" — the first 3 agent decisions GT resolves:**

1. **"Where should I edit?"** → Tiered confidence file/function targeting
2. **"What can this break?"** → Impact analysis with risk verdict (SAFE/RISKY/DANGEROUS)
3. **"Is this safe to remove/change?"** → Caller contract verification

**Output contract for every answer:**
```
{
  "decision": "RISKY",
  "risk": "47 callers across 8 packages assume dict return type",
  "confidence": 0.92,
  "evidence": "2 import-verified, 1 name-match at 0.6",
  "next_action": "inspect auth.py:89 — highest-impact caller",
  "missing_evidence": "3 callers via name-match, may be false positives"
}
```

Not data dumps. **Verdicts with evidence chains.**

### Phase 2: Confidence Gate That Actually Works
- Z-score per repo (dynamic, self-calibrating)
- Tiered: HIGH → TARGET, MEDIUM → LIKELY options, LOW → SCOPE, ZERO → silent
- ALL tiers are useful — even "one of these 5" narrows from 500

### Phase 3: Governed Provenance
- Every claim includes: resolution method, edge confidence, graph freshness
- "3 callers (2 import-verified, 1 name-match 0.6)" — agent knows exactly how much to trust
- Abstain/escalate: "uncertain — inspect X" as an explicit recommendation, not silence

### Research Backing for This Priority
- **ReAct (2022)**: Tools help most when they improve **action selection**, not just retrieval
- **Uncertainty-aware planning (2024)**: Asking for missing info improves task success
- **Answer calibration (2023)**: Post-hoc verification improves multi-step reliability
- **Codified Context (2026)**: Pre-computed intelligence improves new code writing
- **Guardrails compound**: 95% single-step → 60% over 10 steps. Each prevented wrong turn saves 3-5 recovery turns.

### What GT Answers vs Does NOT Answer
| GT Answers (structural facts) | GT Does NOT Answer |
|-------------------------------|-------------------|
| What calls this function | Runtime behavior |
| What imports are needed | Business logic correctness |
| What files are affected by this change | Performance characteristics |
| What patterns exist in this codebase | Security vulnerabilities |
| What tests cover this code | Developer intent |
| How confident is this information | What the code SHOULD do |

**GT answers WHAT IS. Not WHAT SHOULD BE.**

## Current State (2026-04-05)

### v22: Graph-Boosted Localization (implemented)

**Root cause found:** GT had a call graph but used BM25 for localization. v21-definitive regression showed 4/6 passing tasks had WRONG localization — agents solved by ignoring GT. Only 1/10 tasks got edit hook evidence.

**Fix:** Six changes, all using existing graph.db:
1. **Call-graph file boosting** — propagate BM25 scores through graph edges (LocAgent technique). Files 1-3 hops from BM25 hits get boosted. Fixes astropy-13398 (gold reachable via edges) and django-11477 (resolvers.py 2 hops from base.py).
2. **File skeleton instead of function pinning** — show all functions + line numbers + caller counts. Agent picks function. Agentless proved function pinning is ~51% accurate; file-level is 78%. GT provides the map, agent navigates.
3. **Edit hook on every structural change** — removed 2nd-edit gating. sympy-15976's `_print_MatrixSymbol` deletion was missed because agent made only 1 edit. Now fires on EVERY edit.
4. **ALSO file filtering** — suppress ALSO files with zero graph edges to target. Fixes sympy-15976 false positives (dimacs.py, generate_tests.py had zero connection to mathml.py).
5. **No passive PRECEDENT** — carried forward from v21.
6. **All v21-def features** — z-score tiering, compound terms, verdicts, provenance, test commands.

**Research backing:**
- LocAgent (ACL 2025): Graph-based code localization → 92.7% file accuracy by traversing contain/import/invoke/inherit edges
- Agentless (2024): Hierarchical localization — file 78%, function 51%. Function pinning by a tool is unreliable.
- v21-definitive regression data: 2/4 failures had gold files reachable via 1-3 hop graph traversal

**Key principle:** GT's graph IS the unique asset. Every feature must USE the graph, not bypass it with text search.

### v21-definitive (2026-04-04-05)
- Tiered Z-score, compound BM25, impact verdicts, diff-aware validation, language-agnostic test commands
- 10-task regression: 6/10 resolved (baseline 7/10, -1)
- The -1 (sympy-15976) was agent behavior variance, not GT misdirection
- Edit hook validated: caught BREAKING signature changes on matplotlib-24149
- Zero dampening, zero empty patches, briefings on 10/10 tasks

### Proven Results

| Benchmark | Model | Baseline | GT | Delta | Date |
|-----------|-------|----------|-----|-------|------|
| Verified 500 | Gemini 2.5 Flash | 275 | 289 | +14 (+2.8pp) | 2026-04-03 |
| Verified 10 (regression) | GPT-5 Mini | 7 | 6 (v21-def) | -1 | 2026-04-05 |

### Next
- v22 10-task regression (graph boost + skeleton + edit hook)
- Live Lite decontaminated benchmark
- SWE-bench Verified 500

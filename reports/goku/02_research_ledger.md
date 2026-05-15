# 02 Research Ledger -- Verified Citations for GroundTruth

> Generated 2026-05-15. Every URL was fetched and content verified before inclusion.
> Sources marked UNVERIFIED have URLs that could not be confirmed.

---

## Section A: Research-Backed Evidence (peer-reviewed or arXiv)

### A1. RepoGraph -- "Enhancing AI Software Engineering with Repository-level Code Graph"

- **URL:** https://arxiv.org/abs/2410.14684
- **Year:** 2024 (arXiv Oct 2024, revised Mar 2025)
- **Venue:** ICLR 2025 (confirmed via arXiv comments field on v2)
- **Type:** paper
- **Why it applies to GT:** Directly validates using repository-level call graphs as a plug-in module for LLM-based SE agents -- the same architecture GT ships.
- **Confidence:** HIGH
- **Key finding:** RepoGraph constructs k-hop ego-graphs around task-relevant symbols and injects them into LLM context, achieving state-of-the-art on SWE-bench when plugged into existing agent frameworks. The primary navigation signal is callers/callees, not file-level similarity. "Substantially boosts the performance of all systems" as a plug-in.
- **GT implication:** graph.db already has the call edges RepoGraph builds from scratch per-task. GT should expose k-hop ego-subgraphs as the primary briefing format, not flat file lists.
- **Layers affected:** L1 (brief generation), L3 (post-edit context)

---

### A2. CodexGraph -- "Bridging Large Language Models and Code Repositories via Code Graph Databases"

- **URL:** https://arxiv.org/abs/2408.03910
- **Year:** 2024 (arXiv Aug 2024)
- **Venue:** NAACL 2025 (confirmed via Semantic Scholar venue field: "North American Chapter of the Association for Computational Linguistics")
- **Type:** paper
- **Why it applies to GT:** Proposes a richer relationship taxonomy (MODULE/CLASS/FUNCTION nodes + CONTAINS/INHERITS/USES edges) and shows LLM agents can construct and execute graph queries for precise code navigation.
- **Confidence:** HIGH
- **Key finding:** CodexGraph enables agents to write and execute graph database queries rather than relying on similarity-based retrieval. The structured query approach achieves precise, code-structure-aware context retrieval. The graph schema goes beyond call edges to include containment, inheritance, and usage relationships.
- **GT implication:** graph.db currently tracks CALLS and IMPORTS edges only. Adding CONTAINS (parent-child), INHERITS, and USES (decorator/config consumer) edges would enable the query-routing patterns CodexGraph validates.
- **Layers affected:** graph.db schema, gt-index edge extraction, MCP tool query capabilities

---

### A3. SWE-Pruner -- "Self-Adaptive Context Pruning for Coding Agents"

- **URL:** https://arxiv.org/abs/2601.16746
- **Year:** 2026 (arXiv Jan 2026)
- **Venue:** arXiv preprint (no confirmed venue)
- **Type:** paper
- **Why it applies to GT:** Directly measures the cost of over-stuffing agent context with code and proves less-but-relevant beats more-but-noisy.
- **Confidence:** HIGH
- **Key finding:** A lightweight neural model dynamically selects relevant lines given an explicit goal, achieving 23-54% token reduction on SWE-bench Verified while maintaining or improving task success (64% pass vs 62% vanilla). Task-aware pruning outperforms fixed compression. The agents formulate explicit goals to guide what content to retain.
- **GT implication:** GT's L3b graph dumps that inject full symbol context hurt agents. Briefings must be selectively rendered against the task goal. The knapsack token budget in gt_intel.py is the right instinct but needs goal-conditioned line selection, not just family-priority ordering.
- **Layers affected:** L1 (brief token budget), L3/L3b (post-edit context injection), evidence engine knapsack

---

### A4. SAGE -- "Self-Abstraction from Grounded Experience for Plan-Guided Policy Refinement"

- **URL:** https://arxiv.org/abs/2511.05931
- **Year:** 2025 (arXiv Nov 2025)
- **Venue:** arXiv preprint (no confirmed venue; Salesforce Research)
- **Type:** paper
- **Why it applies to GT:** Validates the explore-then-abstract-then-re-execute loop for coding agents, achieving 73-74% Pass@1 on SWE-bench Verified.
- **Confidence:** MEDIUM
- **Key finding:** SAGE lets agents learn from their own initial rollouts by extracting concise plan abstractions -- distilling key steps, dependencies, and constraints -- then using those abstractions to guide a refined second execution. Substantial improvements across multiple LLM backbones. The abstraction is structural (steps + dependencies), not a natural-language summary.
- **GT implication:** GT's post-edit hook could provide structural self-verification data (callers affected, type contracts violated) that feeds an abstraction step, rather than waiting passively for test results. The graph enables the "dependencies and constraints" part of the abstraction.
- **Layers affected:** L3 (post-edit feedback structure), L5b (observation-boundary intervention)

---

### A5. Agentless -- "Demystifying LLM-based Software Engineering Agents"

- **URL:** https://arxiv.org/abs/2407.01489
- **Year:** 2024 (arXiv Jul 2024, revised Oct 2024)
- **Venue:** arXiv preprint (UNVERIFIED for ICLR 2025 -- arXiv comments field does not confirm venue acceptance as of last check)
- **Type:** paper
- **Why it applies to GT:** Proves a fixed localize-repair-validate pipeline without autonomous agent decisions outperforms many agent-based systems, validating GT's deterministic approach.
- **Confidence:** HIGH
- **Key finding:** A three-phase pipeline (localization, repair, patch validation via syntax + regression checks without test execution) achieved 32% on SWE-bench Lite with minimal compute cost and full interpretability. No autonomous tool use or complex agent loops required. The localization phase is the dominant contributor to success.
- **GT implication:** GT's pre-task briefing IS the localization phase. The no-test validation hierarchy (syntax check, import check, caller contract check) is proven viable by Agentless. GT should expose a validate-without-tests tool path.
- **Layers affected:** L1 (pre-task localization), validation pipeline (groundtruth_validate)

---

### A6. SWE-agent -- "Agent-Computer Interfaces Enable Automated Software Engineering"

- **URL:** https://arxiv.org/abs/2405.15793
- **Year:** 2024 (arXiv May 2024, revised Nov 2024)
- **Venue:** UNVERIFIED -- arXiv comments and project website do not confirm NeurIPS 2024; widely cited as NeurIPS 2024 in secondary sources but primary confirmation not obtained
- **Type:** paper
- **Why it applies to GT:** Demonstrates that interface design (what the agent sees and can do) matters more than model capability for SE tasks.
- **Confidence:** HIGH
- **Key finding:** Custom agent-computer interfaces that provide concise, well-structured feedback to the LLM significantly improve autonomous software engineering. "LM agents represent a new category of end users" who benefit from purpose-built interfaces. Achieved 12.5% on SWE-bench and 87.7% on HumanEvalFix. The guardrails and feedback format are the key variables, not the model.
- **GT implication:** GT's observation-boundary intervention (L5b) aligns with the ACI pattern: structured, concise feedback at the interface boundary. The MCP tool responses are GT's ACI surface -- they must be terse and actionable, not verbose dumps.
- **Layers affected:** L5b (observation-boundary), MCP tool response format, all tool handlers

---

### A7. SWE-Search -- "Enhancing Software Agents with Monte Carlo Tree Search and Iterative Refinement"

- **URL:** https://arxiv.org/abs/2410.20285
- **Year:** 2024 (arXiv Oct 2024, revised Apr 2025)
- **Venue:** UNVERIFIED -- arXiv comments do not confirm ICLR 2025 acceptance as of last check
- **Type:** paper
- **Why it applies to GT:** Shows structural state evaluation (not just test pass/fail) improves agent exploration, which is what GT's graph-based feedback provides.
- **Confidence:** MEDIUM
- **Key finding:** Integrating MCTS with a self-improvement mechanism achieves 23% relative improvement on SWE-bench. The hybrid value function balances exploration and exploitation by evaluating structural properties of candidate solutions, not just terminal test results. Additional inference-time compute improves performance without larger models or new training.
- **GT implication:** GT's graph data (caller count, blast radius, type contracts) could serve as the structural state evaluation signal in an MCTS-style agent loop. This is a future integration point, not a current layer.
- **Layers affected:** Future: structural state scoring for agent search, groundtruth_impact as value-function input

---

### A8. RANGER -- "Repository-Level Agent for Graph-Enhanced Retrieval"

- **URL:** https://arxiv.org/abs/2509.25257
- **Year:** 2025 (arXiv Sep 2025)
- **Venue:** arXiv preprint (no confirmed venue)
- **Type:** paper
- **Why it applies to GT:** Validates dual-stage retrieval (structured graph queries for entity lookups, MCTS for NL queries) on repo-level code, directly applicable to GT's tool routing.
- **Confidence:** MEDIUM
- **Key finding:** RANGER constructs a comprehensive knowledge graph of entire repositories and uses a dual-stage pipeline: fast Cypher lookups for code-entity queries and MCTS-guided graph exploration for natural language queries. Different query types demonstrably need different retrieval strategies. Superior performance across code search, QA, and code completion benchmarks.
- **GT implication:** GT's groundtruth_do (auto-router) should route entity queries (symbol name, file path) to direct SQL lookups and NL queries (task descriptions, issue text) to graph-walk strategies. Currently all queries go through the same path.
- **Layers affected:** groundtruth_do routing, groundtruth_find_relevant strategy selection

---

### A9. FeedbackEval -- "A Benchmark for Evaluating Large Language Models in Feedback-Driven Code Repair Tasks"

- **URL:** https://arxiv.org/abs/2504.06939
- **Year:** 2025 (arXiv Apr 2025, revised Feb 2026)
- **Venue:** arXiv preprint (no confirmed venue)
- **Type:** paper
- **Why it applies to GT:** Directly benchmarks how feedback format affects LLM code repair success, validating that GT's structured evidence format matters.
- **Confidence:** HIGH
- **Key finding:** Mixed structured feedback yields the highest repair success (63.6%). Structured reasoning approaches and semantic context significantly enhance repair performance. Diminishing returns observed after multiple feedback iterations. Prompt structure (not just content) is a key variable in code repair effectiveness.
- **GT implication:** L3/L5b must deliver structured evidence (tagged families with confidence tiers), not prose dumps. The VERIFIED/WARNING/INFO tier system in gt_intel.py is the right format. Diminishing returns after N iterations means GT should rate-limit hooks after the first few substantive responses.
- **Layers affected:** L3 (post-edit hook format), L5b (observation format), evidence engine output structure

---

## Section B: Product/Engineering-Practice Evidence (not peer-reviewed)

### B1. Blast Radius -- Cross-Service Impact Analysis

- **URL:** https://blast-radius.dev/
- **Year:** 2026
- **Type:** product doc
- **Why it applies to GT:** Implements the same caller-callee blast radius concept as GT's CALLERS evidence family and groundtruth_impact tool, applied to cross-service PR analysis.
- **Confidence:** MEDIUM
- **Key finding:** Blast Radius detects API and schema modifications in pull requests and surfaces potentially affected services and repositories as PR comments. Addresses the reality that "small changes can cause delayed breakage" across interconnected services. Note: the product is no longer active -- the creator concluded the problem exists but was not a team priority during validation.
- **GT implication:** GT's groundtruth_impact tool solves the same problem within a single repo. The PR-comment delivery pattern (inject impact summary at review time) is a proven UX for blast radius data. The product's discontinuation suggests the cross-repo version of this is hard to sell, but intra-repo impact analysis (GT's scope) has clearer demand.
- **Layers affected:** groundtruth_impact, L3 (IMPACT evidence family)

---

### B2. Harness Engineering -- "Harness Engineering for AI Coding Agents: Constraints That Ship Reliable Code"

- **URL:** https://www.augmentcode.com/guides/harness-engineering-ai-coding-agents
- **Year:** Apr 2026
- **Author:** Molisha Shah (Augment Code)
- **Type:** engineering post
- **Why it applies to GT:** Frames the "Agent = Model + Harness" concept (attributed to a LangChain post) and provides production evidence that harness improvements drive agent reliability at scale.
- **Confidence:** HIGH
- **Key finding:** Harness engineering is "the discipline of designing environments, constraints, and feedback loops that make AI coding agents reliable at scale." Cited metrics: AI-generated code introduced 10,000+ new security findings per month by June 2025 (Apiiro); Spotify's Honk merged 1,500+ AI-generated PRs since mid-2024; top agents achieve 65-76.8% on SWE-bench Verified; 30% of developers report little to no trust in AI-generated code (DORA).
- **GT implication:** GT IS the harness. The structural constraints (L5/L5b) and feedback loops (L3 post-edit) are exactly what this post argues for. The model stays fixed; GT improves the harness.
- **Layers affected:** All layers (GT is the harness itself)

**Note on attribution:** The user's request cited "Hashimoto Harness Engineering" and referenced LangChain's 52.8% to 66.5% improvement. UNVERIFIED -- do not rely on this. The original LangChain blog post (blog.langchain.dev/agent-eq-model-plus-harness/) returns HTTP 404. The Augment article references the LangChain formulation but the specific 52.8% to 66.5% numbers could not be independently verified from any accessible source.

---

### B3. Martin Fowler Memo on Harness Engineering -- "Harness Engineering - first thoughts"

- **URL:** https://martinfowler.com/articles/exploring-gen-ai/harness-engineering-memo.html
- **Year:** Feb 2026
- **Author:** Birgitta Bockeler (Thoughtworks, published on martinfowler.com)
- **Type:** blog / engineering memo
- **Why it applies to GT:** Discusses the three components of an agent harness (context engineering, architectural constraints, garbage collection) and explicitly flags a verification gap.
- **Confidence:** HIGH
- **Key finding:** Harnesses for AI agents have three components: context engineering (dynamic observability data), architectural constraints (deterministic linters + structural tests), and periodic garbage collection (documentation consistency, constraint violation cleanup). The author explicitly identifies a limitation: "All of the described measures focus on increasing long-term internal quality and maintainability. What I am missing in the write-up is verification of functionality and behaviour." Building effective harnesses requires substantial investment (OpenAI team spent 5 months). Note: this post does NOT discuss verification hooks after every change -- that absence is the author's stated concern.
- **GT implication:** GT fills exactly the verification gap Bockeler identifies. GT's post-edit hook IS the "verification of functionality and behaviour" that the harness literature is missing. This positions GT as complementary to linter/structural-test harnesses.
- **Layers affected:** L3 (post-edit verification), L5b (observation-boundary verification), positioning against existing harness tooling

---

### B4. Agent Observability for AI Coding -- "Agent Observability for AI Coding: How to Trace What Your Agents Actually Did"

- **URL:** https://www.augmentcode.com/guides/agent-observability-for-ai-coding
- **Year:** Apr 2026
- **Author:** Ani Galstian (Augment Code)
- **Type:** product doc / engineering post
- **Why it applies to GT:** Defines the four pillars of agent observability (traces, evaluations, cost tracking, attribution) and the "200 OK but shipped a bug" problem.
- **Confidence:** MEDIUM
- **Key finding:** Traditional monitoring fails for AI agents because "an agent can return 200 OK while generating code that compiles, passes partial tests, and ships a security bug." Four pillars: traces/spans (execution lifecycle), evaluations (output quality beyond compilation), cost/token tracking, and attribution (which agent/model/tool-call caused what). Recommends tail-based sampling, loop detection alerts, and PII redaction at the span processor level.
- **GT implication:** GT's telemetry (arm summary, verify_report, per-task event chain) already implements the evaluation and attribution pillars. The "200 OK but buggy" problem is exactly what GT's structural validation catches -- passing tests does not mean correct behavior. GT should expose its verification signals as observability spans, not just log entries.
- **Layers affected:** Stats/telemetry layer, verify_report.py, arm summary events

---

### B5. Anthropic -- "Demystifying evals for AI agents"

- **URL:** https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- **Year:** Jan 2026
- **Authors:** Mikaela Grace, Jeremy Hadfield, Rodrigo Olivares, Jiri De Jonghe (Anthropic)
- **Type:** engineering post
- **Why it applies to GT:** Defines eval best practices for multi-turn, tool-using agents -- directly applicable to measuring whether GT layers change agent behavior.
- **Confidence:** HIGH
- **Key finding:** "The capabilities that make agents useful also make them harder to evaluate." Key recommendations: start with 20-50 tasks from real user failures; combine code-based, model-based, and human graders; grade outcomes not paths; include positive AND negative cases; run in isolated environments; use pass@k (success rate) and pass^k (consistency) metrics. Avoid overly rigid step-checking. Monitor transcripts regularly to confirm graders measure what matters.
- **GT implication:** GT eval must prove behavioral change (agent edits different files, follows different paths), not just "layer fired." The pass^k consistency metric applies to GT: does the brief reliably change the agent's first file action, not just sometimes? The "grade outcomes not paths" principle means GT should measure resolved% and gold-file-hit%, not hook-call counts.
- **Layers affected:** Eval framework, verify_report.py gates, behavioral metrics definition

---

## Verification Summary

| Source | URL Verified | Content Matches Claim | Venue Confirmed |
|--------|-------------|----------------------|-----------------|
| A1 RepoGraph | YES | YES | ICLR 2025 -- YES (arXiv comments) |
| A2 CodexGraph | YES | YES | NAACL 2025 -- YES (Semantic Scholar) |
| A3 SWE-Pruner | YES | YES | arXiv preprint (no venue claimed) |
| A4 SAGE | YES | YES | arXiv preprint (no venue claimed) |
| A5 Agentless | YES | YES | ICLR 2025 -- UNVERIFIED (arXiv does not confirm) |
| A6 SWE-agent | YES | YES | NeurIPS 2024 -- UNVERIFIED (arXiv + website do not confirm) |
| A7 SWE-Search | YES | YES | ICLR 2025 -- UNVERIFIED (arXiv does not confirm) |
| A8 RANGER | YES | YES | arXiv preprint (no venue claimed) |
| A9 FeedbackEval | YES | YES | arXiv preprint (no venue claimed) |
| B1 Blast Radius | YES | YES (product discontinued) | N/A |
| B2 Augment Harness | YES | YES | N/A |
| B2 note: LangChain original | 404 NOT FOUND | UNVERIFIED | N/A |
| B3 Fowler Memo | YES | PARTIAL (does NOT discuss verification hooks; flags their absence) | N/A |
| B4 Augment Observability | YES | YES | N/A |
| B5 Anthropic Evals | YES | YES | N/A |

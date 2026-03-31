# GT Product Assessment — What We Know, What's Missing, What to Build

## What 11 Versions Taught Us

### The Data (hard facts from eval)

- **v10 hooked on Lite**: 1 task flip (astropy-6938: baseline FAIL → GT PASS). Architecture proven.
- **v11 Go indexer on Pro**: Indexes 1385 files in 8.3s. Pipeline works end-to-end. Evidence reaches agent on 1/5 tasks.
- **50-task baseline analysis**: 80% of failures are right-file-wrong-fix. Agent localizes well. Fix quality is the problem.
- **What would flip tasks**: Test assertions (#1, ~8 tasks), sibling patterns (#2, ~5), caller types (#3, ~3), git precedent (#4, ~4).

### What We Built vs What the Industry Does

| What GT has | What industry leaders do | Gap |
|---|---|---|
| Go indexer (tree-sitter, 6 languages, 8s) | Kythe/Glean/SCIP: compiler-grade, CI-time, incremental | GT's indexer is fast but resolution is name-only (37%). Import-aware FQN resolution would reach 60-80%. |
| Python intelligence layer (gt_intel.py) | Sourcegraph Cody: pointwise ranking with threshold | GT has the same architecture but scoring needs calibration from real usage data. |
| Post-edit hook (DockerEnvironment.execute patch) | Meta Infer: diff-time delivery, 70%+ fix rate | Same principle — deliver at the moment of relevance. GT does this. |
| Ranked evidence (5 families, scored 0-3) | Greptile: impact-ranked findings, 82% catch rate | GT's ranking is untested at scale. Need action rate metrics. |
| Multi-language (Python, Go, JS, TS, Rust, Java) | codebase-memory-mcp: 66 languages via tree-sitter | GT covers the important ones. Adding more is just spec files. |

### What's Actually Working

1. **Go indexer** — 8s for 1385 files, multi-language, SQLite output. Production-quality speed.
2. **Hook delivery** — Generalized via `git diff --name-only`. Language-agnostic. Works on any edit method.
3. **Evidence format** — Labeled sections (CALLERS, TESTS, SIBLING, IMPACT) with real code. Agent can parse it.
4. **One verified flip** — astropy-6938 proved the mechanism works.

### What's NOT Working

1. **Evidence reach**: Only 1/5 Pro tasks got evidence. The intelligence layer (gt_intel.py) finds zero qualified candidates for most files — the graph has nodes and edges but the scoring/querying doesn't surface them.
2. **Evidence quality**: When evidence does reach the agent, it's unclear if the agent uses it to change behavior. No action rate metric.
3. **End-to-end eval**: We've never run a complete matched eval with the Go indexer pipeline on enough tasks to measure statistical significance.

## What the Product Needs (Industry Best Practices)

### 1. The Output Must Match the Research "Top 10 Facts"

Our research sprint identified the 10 cross-file facts that change agent decisions. GT's current output doesn't prioritize them correctly:

| Research priority | GT has it? | Quality? |
|---|---|---|
| 1. Correct import paths | No — not in evidence output | Should be #1 signal |
| 2. Function signatures with types | Partial — from tree-sitter | Needs return type + param types |
| 3. Error/exception hierarchy | No | Could extract from AST |
| 4. Existing patterns in target file | Yes — sibling norms | Works but rarely fires |
| 5. Constraints/DO NOTs | No | Hardest to extract deterministically |
| 6. Test file locations + assertions | Yes — TEST family | Works when tests reference the function |
| 7. Related callers + usage patterns | Yes — CALLER family | Works but scoring too strict |
| 8. Type definitions | Partial | From tree-sitter |
| 9. Package availability | No | Could check package manifests |
| 10. Convention signals | Partial — norms | Need more dimensions |

**The #1 gap: import paths.** The research says the #1 hallucination type is wrong imports. GT can deterministically provide correct import paths from the graph. We're not doing this.

### 2. Delivery Must Be Validated With Action Rate

Meta's Infer research: "action rate" (did the developer ACT on the finding?) is the only metric that matters. We need to measure: after GT output appears, did the agent's next command change compared to what it would have done without GT?

This requires:
- Running matched baseline + GT on the same tasks
- Comparing the agent's action AFTER GT fires (GT condition) vs the action at the same turn (baseline condition)
- Counting how often the actions differ

### 3. The Intelligence Layer Needs Calibration

The scoring thresholds (≥1, ≥2, etc.) were set by intuition, not data. We need:
- Run gt_intel.py on 100+ files from known repos
- For each file: how many candidates? What scores? What would the output look like at threshold ≥1, ≥2, ≥3?
- Calibrate threshold to produce output for 60-80% of files (not 20%)

### 4. The Go Indexer Needs Import Resolution

37% call resolution via name-matching means 63% of calls are unresolved. For Python specifically:
- `from foo.bar import baz` → resolve to `foo/bar.py::baz`
- `import os.path` → mark as stdlib (skip)
- `from . import sibling` → resolve relative imports

This would push Python resolution from 37% to ~70%, dramatically improving evidence quality.

## What to Do Next (Ordered by Impact)

### Immediate (today): Run the eval we have

1. Run baseline on 60 Pro tasks (6 workers, ~30 min)
2. Run GT v11 hooked on same 60 tasks (~40 min)
3. Eval both with SWE-bench Pro harness
4. Measure: resolution delta, hook firing rate, evidence delivery rate, action rate

This gives us the CURRENT state of GT v11 on Pro. Even if evidence only reaches 20% of tasks, we'll know the baseline and can improve.

### Short-term (this week): Fix the two biggest gaps

1. **Add import path extraction to gt_intel.py output** — When querying for a function, also show its correct import path. This is the #1 hallucination prevention signal.
2. **Lower and calibrate the evidence threshold** — Run gt_intel.py on 50 files across 5 repos, measure candidate counts, calibrate threshold so 60-80% of files produce evidence.

### Medium-term (next 2 weeks): Product-grade intelligence

1. **Python import resolver in Go indexer** — Resolve `from X import Y` to actual file paths. Push resolution from 37% to ~70%.
2. **Package manifest parsing** — Read requirements.txt/package.json/go.mod to know what's installed.
3. **Exception hierarchy extraction** — Track which functions raise what exceptions.
4. **MCP server wrapping gt_intel.py** — Expose as MCP tools (groundtruth_analyze, groundtruth_validate) for Claude Code/Cursor.

### Long-term (next month): Scale and distribute

1. **Incremental reindexing** — Content hash per file, only reindex changed files.
2. **CI integration** — Run gt-index as a CI step, store graph.db as artifact.
3. **More languages** — Add spec files for Ruby, PHP, C/C++, C#, Kotlin, Swift.
4. **Open source launch** — pip install, one-line MCP config, HN/Reddit launch.

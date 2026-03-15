# GroundTruth — Research Findings

> **Status:** Smoke test data only. These results use existing hallucination cases as smoke test data. Real findings require real-world evaluation (SWE-bench, real agent sessions).

## Methodology

### Data
- **100 existing hallucination cases** across 6 categories (wrong-module-path, symbol-not-found, missing-package, wrong-arg-count, invented-symbol, wrong-language-convention)
- **3 languages:** TypeScript, Python, Go
- **Fixture stores** populated with known symbol/ref/package definitions

### Configurations
1. **Baseline:** Validation only (orchestrator.validate). No briefing.
2. **Standard:** FTS5-based briefing (no API key = deterministic fallback) + validation.
3. **Adaptive:** Risk-aware enhanced briefing + validation.

### Metrics
- **Detection rate:** Fraction of cases where at least one error was found.
- **Fix rate:** Fraction of cases where the suggested fix contained the correct symbol/import.
- **Compliance proxy:** Fraction of correct info (symbol + import) covered by briefing. This is a proxy — real compliance requires measuring agent output after briefing.
- **Risk correlation:** Pearson correlation between each risk factor and detection success.

### Limitations
- Fixture data — not real codebases
- Deterministic briefings only (no LLM calls) — compliance measures FTS5 recall
- Compliance is a proxy for what the agent would do with the briefing
- No temporal feedback loop (adaptive briefing has no past failure history to learn from in fixture stores)

---

## RQ1: Grounding Gap — How Well Do Briefings Cover Correct Context?

| Config | Tasks | Symbol Coverage | Import Coverage | Mean Compliance |
|--------|-------|-----------------|-----------------|-----------------|
| Standard | _TBD_ | _TBD_ | _TBD_ | _TBD_ |
| Adaptive | _TBD_ | _TBD_ | _TBD_ | _TBD_ |

### By Category

| Category | Standard Coverage | Adaptive Coverage | Delta |
|----------|-------------------|-------------------|-------|
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |

### By Language

| Language | Standard Coverage | Adaptive Coverage | Delta |
|----------|-------------------|-------------------|-------|
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |

---

## RQ2: Risk Correlation — Do Risk Factors Predict Detection Failures?

| Risk Factor | Correlation | Low Risk Detection | High Risk Detection |
|-------------|-------------|-------------------|---------------------|
| naming_ambiguity | _TBD_ | _TBD_ | _TBD_ |
| import_depth | _TBD_ | _TBD_ | _TBD_ |
| convention_variance | _TBD_ | _TBD_ | _TBD_ |
| overloaded_paths | _TBD_ | _TBD_ | _TBD_ |
| parameter_complexity | _TBD_ | _TBD_ | _TBD_ |
| isolation_score | _TBD_ | _TBD_ | _TBD_ |

---

## RQ3: Adaptive Effectiveness — Does Risk-Aware Briefing Improve Outcomes?

| Metric | Standard | Adaptive | Delta |
|--------|----------|----------|-------|
| Detection Rate | _TBD_ | _TBD_ | _TBD_ |
| Fix Rate | _TBD_ | _TBD_ | _TBD_ |
| Symbol Coverage | _TBD_ | _TBD_ | _TBD_ |
| Compliance | _TBD_ | _TBD_ | _TBD_ |

### By Risk Level

| Risk Level | Cases | Detection Delta | Compliance Delta |
|------------|-------|-----------------|------------------|
| Low (<0.3) | _TBD_ | _TBD_ | _TBD_ |
| Medium (0.3-0.6) | _TBD_ | _TBD_ | _TBD_ |
| High (>0.6) | _TBD_ | _TBD_ | _TBD_ |

---

## Conclusion

_To be completed after real-world evaluation._

> These results use existing hallucination cases as smoke test data. Real findings require real-world evaluation.

package main

import (
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
)

// The budget knobs follow the existing environment-gate convention
// (GT_REQUIRE_PARSE_RATE, GT_REQUIRE_FTS5, GT_HIERARCHY_CLOSED). An explicit 0
// disables a budget, which is the only setting that reproduces the historical
// unbounded counts byte for byte; an unparsable or negative setting is a
// configuration error, never a silent fallback to the default.
func TestIndexBudgetsResolveFromEnvironment(t *testing.T) {
	empty := func(string) (string, bool) { return "", false }
	defaults, err := resolveIndexBudgets(empty)
	if err != nil {
		t.Fatalf("unset environment is not a configuration error: %v", err)
	}
	if defaults.VTAIterations <= 0 || defaults.FlowFacts <= 0 {
		t.Fatalf("unset budgets must take a positive built-in default, got %+v", defaults)
	}
	if defaults.VTAIterations != defaultVTAIterationBudget || defaults.FlowFacts != defaultFlowFactBudget {
		t.Fatalf("unset budgets %+v do not equal the declared defaults %d/%d", defaults, defaultVTAIterationBudget, defaultFlowFactBudget)
	}

	fixed := func(values map[string]string) func(string) (string, bool) {
		return func(key string) (string, bool) {
			value, ok := values[key]
			return value, ok
		}
	}
	disabled, err := resolveIndexBudgets(fixed(map[string]string{
		vtaIterationBudgetEnv: "0", flowFactBudgetEnv: "0",
	}))
	if err != nil {
		t.Fatalf("explicit 0 is not a configuration error: %v", err)
	}
	if disabled.VTAIterations != 0 || disabled.FlowFacts != 0 {
		t.Fatalf("explicit 0 must disable the budget, got %+v", disabled)
	}

	set, err := resolveIndexBudgets(fixed(map[string]string{
		vtaIterationBudgetEnv: "12", flowFactBudgetEnv: "34",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if set.VTAIterations != 12 || set.FlowFacts != 34 {
		t.Fatalf("explicit budgets were not honoured: %+v", set)
	}

	for name, value := range map[string]string{
		vtaIterationBudgetEnv: "not-a-number",
		flowFactBudgetEnv:     "-1",
	} {
		if _, err := resolveIndexBudgets(fixed(map[string]string{name: value})); err == nil {
			t.Fatalf("%s=%q was accepted as a budget", name, value)
		} else if !strings.Contains(err.Error(), name) {
			t.Fatalf("%s=%q was rejected without naming the variable: %v", name, value, err)
		}
	}
}

// Exceeding a budget must produce evidence, not silence. The callsite keeps
// every candidate it derived, loses selection authority, and publishes the
// typed reason that the VTA analyser already defines -- no new vocabulary.
func TestBudgetedCallsiteAbstainsWithTypedReasonAndKeepsCandidates(t *testing.T) {
	state, reason, applied := budgetedCallsiteDispatch(3)
	if !applied {
		t.Fatal("a callsite with candidates did not record its budget abstention")
	}
	if state != string(resolver.DispatchCandidateOnly) {
		t.Fatalf("budgeted dispatch state=%q, want candidate_only", state)
	}
	if reason != string(resolver.VTAAbstentionReasonBudgetExhausted) {
		t.Fatalf("budgeted abstention reason=%q, want the existing %q vocabulary entry",
			reason, resolver.VTAAbstentionReasonBudgetExhausted)
	}
	if _, _, applied := budgetedCallsiteDispatch(0); applied {
		t.Fatal("a callsite with no candidates cannot publish as candidate_only")
	}
}

// A disabled budget must be exactly today's behaviour: no count is ever over
// it. Above zero the bound is inclusive, so a budget of N publishes N facts.
func TestFlowFactBudgetIsDisabledAtZeroAndInclusiveAboveIt(t *testing.T) {
	for _, count := range []int{0, 1, 1 << 20} {
		if flowFactBudgetExceeded(0, count) {
			t.Fatalf("a disabled budget rejected a callsite with %d flow facts", count)
		}
	}
	if flowFactBudgetExceeded(4, 4) {
		t.Fatal("a budget of 4 rejected exactly 4 flow facts")
	}
	if !flowFactBudgetExceeded(4, 5) {
		t.Fatal("a budget of 4 accepted 5 flow facts")
	}
}

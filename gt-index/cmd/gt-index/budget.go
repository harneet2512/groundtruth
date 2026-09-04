package main

import (
	"fmt"
	"io"
	"sort"
	"strconv"

	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
)

// Budget knobs arrive through the environment, following the fail-closed gates
// this binary already carries (GT_REQUIRE_PARSE_RATE, GT_REQUIRE_FTS5,
// GT_HIERARCHY_CLOSED), rather than adding a tenth CLI flag.
const (
	vtaIterationBudgetEnv = "GT_VTA_ITERATION_BUDGET"
	flowFactBudgetEnv     = "GT_FLOW_FACT_BUDGET"
	coverageSetBudgetEnv  = "GT_COVERAGE_SET_BUDGET"
)

// These are execution RAILS, not rescues, and not analysis policy. Publication
// terminates on every repository measured, boa included; the rails exist so a
// pathological fan-out cannot run away, and they are inert on repositories that
// do not have one. Every figure below comes from a run that completed. Where a
// run did not complete, the entry says so rather than guessing.
//
// defaultVTAIterationBudget bounds complete worklist rounds in
// resolver.AnalyzeVTAWithBudget, which AnalyzeVTA was calling with 0
// (unbounded). Measured fixed points: arktype (458 files) 1 round, boa
// (883 files, 68,227 callsites) 5 rounds. 64 is an order of magnitude above
// both, so neither reaches it.
//
// defaultFlowFactBudget bounds the VTA flow facts one callsite may publish,
// summed over its candidates -- the per candidate x per flow source ID x per
// flow edge ID fan-out in persistOneVTAFlowFactTx. 512 is the value two
// completed boa publications ran under (1,198 s and 2,162 s), so it is a
// default a finished run has actually exercised.
//
// What it costs, measured, at 512:
//   - arktype: 0 flow facts, so identical to the unbudgeted producer on all
//     42 profile buckets -- 159,548 nodes, 188,264 edges.
//   - boa: the full 68,227-callsite distribution is total=53, p50=0, p99=0,
//     max=8. Nothing is near the rail; publication is unchanged.
//   - abs (67 files): one callsite fans out to 800. That one callsite is
//     bounded -- it publishes candidate_only with abstention_reason
//     budget_exhausted, its 800 proof facts are withheld, and the node count
//     goes 40,221 -> 39,421 with the edge count untouched. This is the rail
//     working, not a regression, and it is the only repository measured where
//     the default is not inert.
//
// The rail does not and cannot make boa faster: boa's largest fan-out is 8, so
// no setting above 8 changes it, and a completed run at GT_FLOW_FACT_BUDGET=64
// timed out under a 1,500 s cap exactly as the unbudgeted producer did -- the
// cap, not the producer, was the limit. boa's real cost is wall clock and write
// amplification; see REPORT.md.
//
// defaultCoverageSetBudget is 0 -- DISABLED -- on purpose. The rail exists and
// works, but no completed run shows a positive value earning its cost, and a
// positive value changes published evidence. On a 250-file boa sample it
// withheld 1,254,915 stable IDs across 15,885 callsites and cut the
// HAS_CALLSITE coverage payload from 105.9 MB to 21.3 MB with identical node
// and edge counts -- while the attach phase took 2m15s without it and 2m20s
// with it. It bought bytes, not time, at that scale. Turn it on with
// GT_COVERAGE_SET_BUDGET only against a measurement that shows it helping.
const (
	defaultVTAIterationBudget = 64
	defaultFlowFactBudget     = 512
	defaultCoverageSetBudget  = 0
)

// indexBudgets is the effective execution rail for one index build. Zero means
// the corresponding budget is disabled, which is the only setting that
// reproduces the historical unbounded counts byte for byte.
type indexBudgets struct {
	VTAIterations int
	FlowFacts     int
	CoverageSets  int
}

// resolveIndexBudgets reads both knobs from a lookup (os.LookupEnv in
// production). A malformed or negative setting is a configuration error the
// caller must fail closed on: silently falling back to a default would publish
// a graph under a budget the operator did not ask for and cannot see.
func resolveIndexBudgets(lookup func(string) (string, bool)) (indexBudgets, error) {
	iterations, err := budgetFromEnv(lookup, vtaIterationBudgetEnv, defaultVTAIterationBudget)
	if err != nil {
		return indexBudgets{}, err
	}
	facts, err := budgetFromEnv(lookup, flowFactBudgetEnv, defaultFlowFactBudget)
	if err != nil {
		return indexBudgets{}, err
	}
	sets, err := budgetFromEnv(lookup, coverageSetBudgetEnv, defaultCoverageSetBudget)
	if err != nil {
		return indexBudgets{}, err
	}
	return indexBudgets{VTAIterations: iterations, FlowFacts: facts, CoverageSets: sets}, nil
}

func budgetFromEnv(lookup func(string) (string, bool), name string, fallback int) (int, error) {
	raw, ok := lookup(name)
	if !ok || raw == "" {
		return fallback, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("%s=%q is not an integer budget: %w", name, raw, err)
	}
	if value < 0 {
		return 0, fmt.Errorf("%s=%d is negative; use 0 to disable the budget", name, value)
	}
	return value, nil
}

// flowFactBudgetExceeded reports whether one callsite's VTA flow-fact fan-out
// is over budget. The bound is inclusive: a budget of N publishes N facts.
func flowFactBudgetExceeded(budget, factCount int) bool {
	return budget > 0 && factCount > budget
}

// budgetedCallsiteDispatch is what a callsite publishes when a budget stopped
// its analysis. Candidates are conserved and selection authority is dropped --
// a bounded analysis has not proven a unique target -- and the reason is the
// one resolver.VTAAbstentionReasonBudgetExhausted already defines, so no new
// vocabulary enters the graph.
//
// A callsite with no candidates cannot claim candidate_only, so it keeps the
// dispatch state its own resolution produced. Reporting a budget on a callsite
// that published no candidate authority would say the budget cost evidence it
// never had.
func budgetedCallsiteDispatch(candidateCount int) (dispatchState, abstentionReason string, applied bool) {
	if candidateCount < 1 {
		return "", "", false
	}
	return string(resolver.DispatchCandidateOnly), string(resolver.VTAAbstentionReasonBudgetExhausted), true
}

// reportPublicationFanout states, before the publication transaction opens,
// what that transaction is about to write per callsite. A budget can only be
// set from a measured distribution, and a run that never terminates publishes
// no graph to measure afterwards -- so the measurement has to happen here.
func reportPublicationFanout(w io.Writer, vta []resolver.VTAResult, hierarchy []resolver.CHAThenRTAResult, flowFactBudget int) {
	costs := make([]int, 0, len(vta))
	total, over := 0, 0
	for _, result := range vta {
		cost := 0
		for _, proof := range result.FlowProofs {
			cost += len(proof.SourceStableIDs) + len(proof.EdgeStableIDs)
		}
		costs = append(costs, cost)
		total += cost
		if flowFactBudgetExceeded(flowFactBudget, cost) {
			over++
		}
	}
	sort.Ints(costs)
	at := func(fraction float64) int {
		if len(costs) == 0 {
			return 0
		}
		index := int(float64(len(costs)-1) * fraction)
		return costs[index]
	}
	maxReachable, maxRoots, maxCHA, maxRTA := 0, 0, 0, 0
	for _, result := range hierarchy {
		maxReachable = max(maxReachable, len(result.ReachableNodeIDs))
		maxRoots = max(maxRoots, len(result.RootNodeIDs))
		maxCHA = max(maxCHA, len(result.CHACandidateNodeIDs))
		maxRTA = max(maxRTA, len(result.RTACandidateNodeIDs))
	}
	fmt.Fprintf(w, "  Publication fan-out over %d callsites: flow facts total=%d p50=%d p99=%d max=%d over-budget=%d; "+
		"per-callsite RTA sets reachable<=%d roots<=%d; candidates cha<=%d rta<=%d\n",
		len(costs), total, at(0.50), at(0.99), at(1.0), over, maxReachable, maxRoots, maxCHA, maxRTA)
}

// coverageSetBudgetExceeded bounds the repository-scoped stable-ID sets that
// pass coverage materialises once per callsite. reachableInstantiationFixedPoint
// computes the RTA reachable set and the root set once for the whole repository
// and cha_rta.go:101-102 then assigns the same two slices to every
// interface/virtual callsite result, so their cost is (set size x callsite
// count) and it is paid twice -- in the HAS_CALLSITE pass_coverage JSON and in
// the rta CompletenessFact's reachable_stable_ids/root_stable_ids columns.
//
// Measured: arktype carries 0 IDs per callsite, so any positive budget is a
// no-op there. boa carries up to 146 reachable + 71 roots across 68,227
// callsites -- roughly 14.7 KB per callsite, about 2 GB written twice inside
// one atomic transaction.
func coverageSetBudgetExceeded(budget, setSize int) bool {
	return budget > 0 && setSize > budget
}

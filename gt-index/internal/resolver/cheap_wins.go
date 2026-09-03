package resolver

import "github.com/harneet2512/groundtruth/gt-index/internal/parser"

// CheapWinsReport counts what the two projection passes did over one resolve.
// It exists so a build can state the effect on candidate counts instead of
// leaving it to be inferred from a graph diff.
type CheapWinsReport struct {
	CallsitesConsidered int
	CallsitesNarrowed   int
	CandidatesExcluded  int
	CallsitesReordered  int
	NarrowingReasons    map[string]int
	MROReasons          map[string]int
}

// LastCheapWins is the report from the most recent ResolveWithProvenance. The
// resolver already keeps its indexes as package state (inheritanceMap,
// paramTypeIndex); this follows that convention so the indexer can print what
// narrowing did without threading a return value through every caller.
var LastCheapWins = newCheapWinsReport()

func newCheapWinsReport() CheapWinsReport {
	return CheapWinsReport{
		NarrowingReasons: make(map[string]int),
		MROReasons:       make(map[string]int),
	}
}

// applyCheapResolutionWins narrows each resolved callsite's candidate set by
// argument arity and orders inherited-mechanism sets by the class
// linearisation. Both run before the legacy endpoint dedupe, so the collapsed
// CALLS view and the graph-native candidate evidence never disagree about which
// candidates survived.
//
// Neither pass adds a candidate, selects a target, or raises a trust tier. A
// set that was ambiguous before narrowing stays candidate-only after it: that
// is enforced by ArityNarrowedFrom, which sourceSupportedResolution reads.
func applyCheapResolutionWins(calls []parser.CallRef, perCallsite []ResolvedCall, traces [][]ResolutionPassExecution, meta map[int64]NodeMeta) CheapWinsReport {
	report := newCheapWinsReport()
	for i := range perCallsite {
		rc := &perCallsite[i]
		ordinal := rc.CallsiteOrdinal
		if ordinal < 0 || ordinal >= len(calls) {
			continue
		}
		report.CallsitesConsidered++
		language := LanguageForFile(rc.SourceFile)
		applyNarrowing(rc, calls[ordinal], language, meta, traces, &report)
		applyMROOrder(rc, language, meta, traces, &report)
	}
	return report
}

func applyNarrowing(rc *ResolvedCall, call parser.CallRef, language string, meta map[int64]NodeMeta, traces [][]ResolutionPassExecution, report *CheapWinsReport) {
	before := len(rc.CandidateNodeIDs)
	// The resolver's own target is evidence narrowing cannot see. Passing it as
	// the protected identity makes narrowing abstain rather than contradict it.
	selected := rc.TargetNodeID
	narrowing := NarrowByArity(language, call.ArgumentArity, call.ArgumentSpread, rc.CandidateNodeIDs, &selected, meta)
	if narrowing.Reason == NarrowingBelowTwoCandidates {
		return
	}
	report.NarrowingReasons[narrowing.Reason]++
	recordPassExecution(traces, rc.CallsiteOrdinal, ResolutionPassExecution{
		PassKind: OverloadNarrowingPass, Status: narrowing.Status, Reason: narrowing.Reason,
	})
	if narrowing.Status != NarrowingStatusNarrowed {
		return
	}
	rc.CandidateNodeIDs = narrowing.Kept
	rc.CandidateCount = len(narrowing.Kept)
	rc.ArityNarrowedFrom = before
	report.CallsitesNarrowed++
	report.CandidatesExcluded += len(narrowing.Excluded)
}

func applyMROOrder(rc *ResolvedCall, language string, meta map[int64]NodeMeta, traces [][]ResolutionPassExecution, report *CheapWinsReport) {
	ordering := OrderCandidatesByMRO(language, rc.Method, rc.CandidateNodeIDs, inheritanceMap, meta)
	if ordering.Reason == MROReasonNotInheritedMechanism || ordering.Reason == MROReasonBelowTwoCandidates {
		return
	}
	report.MROReasons[ordering.Reason]++
	recordPassExecution(traces, rc.CallsiteOrdinal, ResolutionPassExecution{
		PassKind: MROPass, Status: ordering.Status, Reason: mroPassReason(ordering),
	})
	if ordering.Status != MROStatusOrdered {
		return
	}
	rc.CandidateNodeIDs = ordering.Order
	report.CallsitesReordered++
}

// mroPassReason keeps the linearisation kind on the published fact, so a reader
// can tell a C3 order from a single-chain one without re-deriving it.
func mroPassReason(ordering MROOrdering) string {
	return ordering.Kind + ":" + ordering.Reason
}

func recordPassExecution(traces [][]ResolutionPassExecution, ordinal int, entry ResolutionPassExecution) {
	if ordinal < 0 || ordinal >= len(traces) {
		return
	}
	traces[ordinal] = append(traces[ordinal], entry)
}

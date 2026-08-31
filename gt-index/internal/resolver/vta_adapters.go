package resolver

import (
	"sort"
	"strings"
)

// VTAAdapterKind identifies a dynamic-language or generated-code boundary
// that cannot be treated as an ordinary source-level VTA assignment.
type VTAAdapterKind string

const (
	VTAAdapterReflection          VTAAdapterKind = "reflection"
	VTAAdapterDependencyInjection VTAAdapterKind = "di_binding"
	VTAAdapterHigherOrder         VTAAdapterKind = "higher_order_flow"
	VTAAdapterFFI                 VTAAdapterKind = "ffi"
	VTAAdapterGeneratedCode       VTAAdapterKind = "generated_mapping"
)

// VTAAdapterObservation is the producer-owned, candidate-bound output of a
// language/framework adapter. Adapters may provide viable candidates and
// evidence, but they never select a target: selection requires the normal
// independent resolution policy.
type VTAAdapterObservation struct {
	Kind             VTAAdapterKind
	CallsiteID       string
	Complete         bool
	CandidateNodeIDs []int64
	EvidenceIDs      []string
}

// VTAAdapterAbstentionReason is a closed vocabulary for adapter boundaries.
// Keeping these reasons typed prevents unsupported dynamic behavior from
// being silently represented as an empty or verified candidate set.
type VTAAdapterAbstentionReason string

const (
	VTAAdapterAbstentionInvalidKind       VTAAdapterAbstentionReason = "invalid_adapter_kind"
	VTAAdapterAbstentionMissingCallsite   VTAAdapterAbstentionReason = "missing_callsite_identity"
	VTAAdapterAbstentionIncomplete        VTAAdapterAbstentionReason = "incomplete_adapter_evidence"
	VTAAdapterAbstentionMissingCandidates VTAAdapterAbstentionReason = "missing_candidate_evidence"
	VTAAdapterAbstentionMissingEvidence   VTAAdapterAbstentionReason = "missing_adapter_evidence"
	VTAAdapterAbstentionInvalidCandidate  VTAAdapterAbstentionReason = "invalid_candidate_identity"
	VTAAdapterAbstentionInvalidEvidence   VTAAdapterAbstentionReason = "invalid_evidence_identity"
)

// VTAAdapterResult is the normalized result of one adapter observation.
// Status is either adapted or abstained. SelectedTargetNodeID is intentionally
// always nil; adapters contribute evidence, not authority.
type VTAAdapterResult struct {
	Kind                 VTAAdapterKind
	CallsiteID           string
	Status               string
	CandidateNodeIDs     []int64
	EvidenceIDs          []string
	SelectedTargetNodeID *int64
	AbstentionReason     VTAAdapterAbstentionReason
}

var supportedVTAAdapterKinds = map[VTAAdapterKind]struct{}{
	VTAAdapterReflection:          {},
	VTAAdapterDependencyInjection: {},
	VTAAdapterHigherOrder:         {},
	VTAAdapterFFI:                 {},
	VTAAdapterGeneratedCode:       {},
}

// AnalyzeVTAAdapters normalizes independently produced dynamic-boundary
// observations in input order. It is deliberately conservative: malformed,
// incomplete, or unsupported observations become typed abstentions and never
// leak partial candidate/evidence arrays as a successful result.
func AnalyzeVTAAdapters(observations []VTAAdapterObservation) []VTAAdapterResult {
	results := make([]VTAAdapterResult, len(observations))
	for i, observation := range observations {
		result := VTAAdapterResult{Kind: observation.Kind, CallsiteID: strings.TrimSpace(observation.CallsiteID), Status: "abstained"}
		if _, ok := supportedVTAAdapterKinds[observation.Kind]; !ok {
			result.AbstentionReason = VTAAdapterAbstentionInvalidKind
			results[i] = result
			continue
		}
		if result.CallsiteID == "" {
			result.AbstentionReason = VTAAdapterAbstentionMissingCallsite
			results[i] = result
			continue
		}
		if !observation.Complete {
			result.AbstentionReason = VTAAdapterAbstentionIncomplete
			results[i] = result
			continue
		}
		candidates, invalidCandidate := canonicalAdapterCandidates(observation.CandidateNodeIDs)
		if invalidCandidate {
			result.AbstentionReason = VTAAdapterAbstentionInvalidCandidate
			results[i] = result
			continue
		}
		if len(candidates) == 0 {
			result.AbstentionReason = VTAAdapterAbstentionMissingCandidates
			results[i] = result
			continue
		}
		evidence, invalidEvidence := canonicalAdapterEvidence(observation.EvidenceIDs)
		if invalidEvidence {
			result.AbstentionReason = VTAAdapterAbstentionInvalidEvidence
			results[i] = result
			continue
		}
		if len(evidence) == 0 {
			result.AbstentionReason = VTAAdapterAbstentionMissingEvidence
			results[i] = result
			continue
		}
		result.Status = "adapted"
		result.CandidateNodeIDs = candidates
		result.EvidenceIDs = evidence
		results[i] = result
	}
	return results
}

func canonicalAdapterCandidates(values []int64) ([]int64, bool) {
	seen := make(map[int64]struct{}, len(values))
	result := make([]int64, 0, len(values))
	for _, value := range values {
		if value <= 0 {
			return nil, true
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	sort.Slice(result, func(i, j int) bool { return result[i] < result[j] })
	return result, false
}

func canonicalAdapterEvidence(values []string) ([]string, bool) {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			return nil, true
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	sort.Strings(result)
	return result, false
}

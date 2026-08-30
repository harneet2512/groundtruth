package resolver

import (
	"sort"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// CHAThenRTAResult is the score-free evidence produced for one virtual or
// interface callsite. CHA enumerates the conservative method set first; RTA
// narrows that set only with allocation facts already present in the source.
// An open hierarchy never becomes authoritative merely because RTA found an
// allocation, so the completeness and abstention fields are part of the
// result rather than implicit caller state.
type CHAThenRTAResult struct {
	CallsiteOrdinal      int
	CHACandidateNodeIDs  []int64
	RTACandidateNodeIDs  []int64
	RTACompleteness      string
	RTAAllocationTypeIDs []int64
	RTAAbstentionReason  string
	CHACompleteness      string
}

// AnalyzeCHAThenRTA performs a deterministic, producer-owned CHA→RTA pass.
// The implements map is child type ID → implemented base/interface IDs. When
// a language has no explicit relationship facts, structural method-set
// inference supplies the same conservative candidate boundary for interfaces.
func AnalyzeCHAThenRTA(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef, hierarchyClosed bool) []CHAThenRTAResult {
	if implements == nil {
		implements = make(map[int64][]int64)
	}
	methodsByName := make(map[string][]int64)
	typeIDsByName := make(map[string][]int64)
	interfaceIDs := make(map[int64]struct{})
	concreteIDs := make(map[int64]struct{})
	methodNamesByType := make(map[int64]map[string]struct{})
	for id, node := range meta {
		if isHierarchyType(node.Label) {
			typeIDsByName[normalizedTypeName(node.Name)] = append(typeIDsByName[normalizedTypeName(node.Name)], id)
			if node.Label == "Interface" {
				interfaceIDs[id] = struct{}{}
			} else {
				concreteIDs[id] = struct{}{}
			}
		}
		if isHierarchyMethod(node.Label) && node.ParentID != 0 {
			methodsByName[node.Name] = append(methodsByName[node.Name], id)
			if methodNamesByType[node.ParentID] == nil {
				methodNamesByType[node.ParentID] = make(map[string]struct{})
			}
			methodNamesByType[node.ParentID][node.Name] = struct{}{}
		}
	}
	for name := range methodsByName {
		sort.Slice(methodsByName[name], func(i, j int) bool { return methodsByName[name][i] < methodsByName[name][j] })
	}
	for name := range typeIDsByName {
		sort.Slice(typeIDsByName[name], func(i, j int) bool { return typeIDsByName[name][i] < typeIDsByName[name][j] })
	}

	// Infer Go-style structural implementation facts when the parser did not
	// emit explicit implements/extends relationships.
	for concreteID := range concreteIDs {
		if _, exists := implements[concreteID]; exists {
			continue
		}
		for interfaceID := range interfaceIDs {
			want := methodNamesByType[interfaceID]
			if len(want) == 0 || hasAllMethods(methodNamesByType[concreteID], want) {
				implements[concreteID] = append(implements[concreteID], interfaceID)
			}
		}
		sort.Slice(implements[concreteID], func(i, j int) bool { return implements[concreteID][i] < implements[concreteID][j] })
	}

	results := make([]CHAThenRTAResult, 0, len(calls))
	for ordinal, call := range calls {
		result := CHAThenRTAResult{CallsiteOrdinal: ordinal}
		if call.DispatchForm != "interface" && call.DispatchForm != "virtual" {
			results = append(results, result)
			continue
		}

		candidateTypes := make(map[int64]struct{})
		for concreteID := range concreteIDs {
			if len(implements[concreteID]) == 0 {
				continue
			}
			candidateTypes[concreteID] = struct{}{}
		}
		for _, methodID := range methodsByName[call.CalleeName] {
			method := meta[methodID]
			if _, ok := candidateTypes[method.ParentID]; ok {
				result.CHACandidateNodeIDs = append(result.CHACandidateNodeIDs, methodID)
			}
		}
		// A virtual call without explicit interface implementors still gets a
		// conservative method-name boundary rather than silently disappearing.
		if len(result.CHACandidateNodeIDs) == 0 && call.DispatchForm == "virtual" {
			for _, methodID := range methodsByName[call.CalleeName] {
				if _, ok := concreteIDs[meta[methodID].ParentID]; ok {
					result.CHACandidateNodeIDs = append(result.CHACandidateNodeIDs, methodID)
				}
			}
		}
		sort.Slice(result.CHACandidateNodeIDs, func(i, j int) bool { return result.CHACandidateNodeIDs[i] < result.CHACandidateNodeIDs[j] })
		if hierarchyClosed {
			result.CHACompleteness = "closed"
		} else {
			result.CHACompleteness = "partial"
		}

		allocatedTypes := allocationTypesForCall(call, assignments, typeIDsByName, candidateTypes)
		result.RTAAllocationTypeIDs = allocatedTypes
		allocatedSet := make(map[int64]struct{}, len(allocatedTypes))
		for _, typeID := range allocatedTypes {
			allocatedSet[typeID] = struct{}{}
		}
		for _, methodID := range result.CHACandidateNodeIDs {
			if _, ok := allocatedSet[meta[methodID].ParentID]; ok {
				result.RTACandidateNodeIDs = append(result.RTACandidateNodeIDs, methodID)
			}
		}
		if hierarchyClosed {
			result.RTACompleteness = "closed"
		} else {
			result.RTACompleteness = "partial"
			result.RTAAbstentionReason = "hierarchy_open"
		}
		results = append(results, result)
	}
	return results
}

func isHierarchyType(label string) bool {
	return label == "Class" || label == "Struct" || label == "Type" || label == "Interface" || label == "Trait"
}

func isHierarchyMethod(label string) bool { return label == "Method" || label == "Function" }

func normalizedTypeName(name string) string {
	name = strings.TrimSpace(name)
	name = strings.TrimPrefix(name, "*")
	if dot := strings.LastIndex(name, "."); dot >= 0 {
		name = name[dot+1:]
	}
	return strings.TrimSpace(name)
}

func hasAllMethods(have map[string]struct{}, want map[string]struct{}) bool {
	for name := range want {
		if _, ok := have[name]; !ok {
			return false
		}
	}
	return true
}

func allocationTypesForCall(call parser.CallRef, assignments []parser.AssignmentRef, typeIDsByName map[string][]int64, candidateTypes map[int64]struct{}) []int64 {
	qualifier := call.CalleeQualified
	if dot := strings.LastIndex(qualifier, "."); dot >= 0 {
		qualifier = qualifier[:dot]
	} else {
		qualifier = ""
	}
	if qualifier == "" {
		return nil
	}
	seen := make(map[int64]struct{})
	for _, assignment := range assignments {
		if assignment.VarName != qualifier || assignment.File != call.File || assignment.Line > call.Line {
			continue
		}
		for _, typeID := range typeIDsByName[normalizedTypeName(assignment.TypeName)] {
			if _, candidate := candidateTypes[typeID]; candidate {
				seen[typeID] = struct{}{}
			}
		}
		for _, typeID := range typeIDsByName[normalizedTypeName(assignment.TypeQualified)] {
			if _, candidate := candidateTypes[typeID]; candidate {
				seen[typeID] = struct{}{}
			}
		}
	}
	result := make([]int64, 0, len(seen))
	for typeID := range seen {
		result = append(result, typeID)
	}
	sort.Slice(result, func(i, j int) bool { return result[i] < result[j] })
	return result
}

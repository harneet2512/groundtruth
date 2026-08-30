package resolver

import (
	"sort"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

// VTAResult is conservative variable-type evidence for one virtual or
// interface callsite. VTA retains viable target identities but never selects
// one on flow evidence alone.
type VTAResult struct {
	CallsiteOrdinal      int
	CandidateNodeIDs     []int64
	FlowTypeNodeIDs      []int64
	SelectedTargetNodeID *int64
	Completeness         string
	AbstentionReason     string
}

// AnalyzeVTA propagates source-visible variable, return, and object-field
// types into candidate identities. Local variables are confined to the call's
// source file and preceding assignments; self/this fields are object-scoped
// so a write in one method can inform a read in another. Candidate evidence is
// deliberately nullable-selection: VTA never turns alternatives into a
// verified single target.
func AnalyzeVTA(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef) []VTAResult {
	typeIDsByName := make(map[string][]int64)
	concreteIDs := make(map[int64]struct{})
	interfaceIDs := make(map[int64]struct{})
	methodsByName := make(map[string][]int64)
	for id, node := range meta {
		if isHierarchyType(node.Label) {
			name := normalizedTypeName(node.Name)
			typeIDsByName[name] = append(typeIDsByName[name], id)
			if node.Label == "Interface" {
				interfaceIDs[id] = struct{}{}
			} else {
				concreteIDs[id] = struct{}{}
			}
		}
		if isHierarchyMethod(node.Label) && node.ParentID != 0 {
			methodsByName[node.Name] = append(methodsByName[node.Name], id)
		}
	}
	for name := range typeIDsByName {
		sort.Slice(typeIDsByName[name], func(i, j int) bool { return typeIDsByName[name][i] < typeIDsByName[name][j] })
	}
	for name := range methodsByName {
		sort.Slice(methodsByName[name], func(i, j int) bool { return methodsByName[name][i] < methodsByName[name][j] })
	}

	results := make([]VTAResult, len(calls))
	for ordinal, call := range calls {
		results[ordinal] = VTAResult{CallsiteOrdinal: ordinal, Completeness: "not_run"}
		if call.DispatchForm != "interface" && call.DispatchForm != "virtual" {
			continue
		}
		qualifier := callQualifier(call.CalleeQualified)
		if qualifier == "" {
			continue
		}
		fieldScoped := strings.HasPrefix(qualifier, "self.") || strings.HasPrefix(qualifier, "this.")
		typeIDs := make(map[int64]struct{})
		for _, assignment := range assignments {
			if assignment.VarName != qualifier || assignment.Line > call.Line {
				continue
			}
			if !fieldScoped && assignment.File != call.File {
				continue
			}
			for _, typeName := range assignmentTypeNames(assignment, meta) {
				for _, typeID := range typeIDsByName[normalizedTypeName(typeName)] {
					if _, isInterface := interfaceIDs[typeID]; isInterface {
						for concreteID := range concreteIDs {
							if implementsBase(implements, concreteID, typeID) {
								typeIDs[concreteID] = struct{}{}
							}
						}
						continue
					}
					typeIDs[typeID] = struct{}{}
				}
			}
		}
		if len(typeIDs) == 0 {
			continue
		}
		results[ordinal].Completeness = "candidate_only"
		results[ordinal].FlowTypeNodeIDs = sortedIDs(typeIDs)
		for _, methodID := range methodsByName[call.CalleeName] {
			if _, ok := typeIDs[meta[methodID].ParentID]; ok {
				results[ordinal].CandidateNodeIDs = append(results[ordinal].CandidateNodeIDs, methodID)
			}
		}
		if len(results[ordinal].CandidateNodeIDs) > 1 {
			results[ordinal].AbstentionReason = "ambiguous_viable_set"
		}
	}
	return results
}

func callQualifier(qualified string) string {
	dot := strings.LastIndex(qualified, ".")
	if dot < 0 {
		return ""
	}
	return strings.TrimSpace(qualified[:dot])
}

func assignmentTypeNames(assignment parser.AssignmentRef, meta map[int64]NodeMeta) []string {
	if !assignment.ViaReturn {
		return []string{assignment.TypeName, assignment.TypeQualified}
	}
	names := make([]string, 0, 2)
	for _, node := range meta {
		if (node.Label == "Function" || node.Label == "Method") &&
			normalizedTypeName(node.Name) == normalizedTypeName(assignment.TypeName) &&
			node.ReturnType != "" {
			names = append(names, node.ReturnType)
		}
	}
	return names
}

func implementsBase(implements map[int64][]int64, concreteID, baseID int64) bool {
	seen := make(map[int64]struct{})
	var visit func(int64) bool
	visit = func(current int64) bool {
		if current == baseID {
			return true
		}
		if _, ok := seen[current]; ok {
			return false
		}
		seen[current] = struct{}{}
		for _, parent := range implements[current] {
			if visit(parent) {
				return true
			}
		}
		return false
	}
	return visit(concreteID)
}

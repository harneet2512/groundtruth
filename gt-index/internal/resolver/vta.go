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

type vtaValueKey struct {
	File   string
	Scope  string
	Object string
	Name   string
	Line   int
}

// AnalyzeVTA builds a finite, flow-insensitive value-constraint graph and
// reaches a monotone fixed point over it. Assignments seed value facts;
// argument edges copy facts into callee parameters. Return annotations are
// resolved transitively, so a factory returning another factory is still
// useful without pretending that a unique target was verified.
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

	returnTypes := make(map[string][]string)
	for _, node := range meta {
		if (node.Label == "Function" || node.Label == "Method") && node.ReturnType != "" {
			returnTypes[normalizedTypeName(node.Name)] = append(returnTypes[normalizedTypeName(node.Name)], normalizedTypeName(node.ReturnType))
		}
	}
	var resolveReturn func(string, map[string]struct{}) []string
	resolveReturn = func(name string, seen map[string]struct{}) []string {
		name = normalizedTypeName(name)
		if name == "" {
			return nil
		}
		if _, ok := seen[name]; ok {
			return nil
		}
		seen[name] = struct{}{}
		out := make([]string, 0)
		for _, returned := range returnTypes[name] {
			if len(typeIDsByName[returned]) > 0 {
				out = append(out, returned)
			} else {
				out = append(out, resolveReturn(returned, seen)...)
			}
		}
		return uniqueStrings(out)
	}

	valueTypes := make(map[vtaValueKey]map[string]struct{})
	addValue := func(key vtaValueKey, names []string) bool {
		if key.Name == "" || len(names) == 0 {
			return false
		}
		set := valueTypes[key]
		if set == nil {
			set = make(map[string]struct{})
			valueTypes[key] = set
		}
		changed := false
		for _, name := range names {
			name = normalizedTypeName(name)
			if name == "" {
				continue
			}
			if _, exists := set[name]; !exists {
				set[name] = struct{}{}
				changed = true
			}
		}
		return changed
	}
	assignmentNames := func(assignment parser.AssignmentRef) []string {
		if !assignment.ViaReturn {
			return []string{assignment.TypeName, assignment.TypeQualified}
		}
		return resolveReturn(assignment.TypeName, make(map[string]struct{}))
	}
	for _, assignment := range assignments {
		addValue(vtaValueKey{File: assignment.File, Scope: assignment.Scope, Object: assignment.ObjectScope, Name: assignment.VarName, Line: assignment.Line}, assignmentNames(assignment))
	}

	callValue := func(call parser.CallRef, name string) map[string]struct{} {
		object := callerObjectScope(call.CallerScope)
		field := strings.HasPrefix(name, "self.") || strings.HasPrefix(name, "this.")
		key := vtaValueKey{File: call.File, Scope: call.CallerScope, Object: object, Name: name}
		if !field {
			key.Object = ""
		}
		fallback := make(map[string]struct{})
		for assignment, set := range valueTypes {
			if assignment.Name != key.Name || assignment.Line > call.Line {
				continue
			}
			if !field && (assignment.File != key.File || (call.CallerScope != "" && assignment.Scope != key.Scope)) {
				continue
			}
			if field {
				if key.Object != "" && assignment.Object != "" {
					if assignment.Object != key.Object {
						continue
					}
				} else if assignment.File != key.File {
					continue
				}
			}
			for typ := range set {
				fallback[typ] = struct{}{}
			}
		}
		return fallback
	}
	parameterMatches := func(assignment parser.AssignmentRef, call parser.CallRef, index int) bool {
		if !assignment.IsParameter || assignment.ParameterIndex != index {
			return false
		}
		callee := normalizedTypeName(call.CalleeName)
		scope := normalizedTypeName(assignment.Scope)
		if call.CalleeScope != "" {
			return scope == normalizedTypeName(call.CalleeScope)
		}
		return scope == callee || strings.HasSuffix(scope, "."+callee)
	}
	// Monotone worklist: every iteration only adds a value fact, so finite
	// type/name facts guarantee termination even for cyclic calls.
	for changed := true; changed; {
		changed = false
		for _, call := range calls {
			for index, argument := range call.ArgumentNames {
				for _, parameter := range assignments {
					if !parameterMatches(parameter, call, index) {
						continue
					}
					for typ := range callValue(call, argument) {
						if addValue(vtaValueKey{File: parameter.File, Scope: parameter.Scope, Object: parameter.ObjectScope, Name: parameter.VarName, Line: parameter.Line}, []string{typ}) {
							changed = true
						}
					}
				}
			}
		}
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
		flowNames := callValue(call, qualifier)
		if len(flowNames) == 0 {
			continue
		}
		flowTypeIDs := make(map[int64]struct{})
		for name := range flowNames {
			for _, typeID := range typeIDsByName[normalizedTypeName(name)] {
				if _, isInterface := interfaceIDs[typeID]; isInterface {
					for concreteID := range concreteIDs {
						if implementsBase(implements, concreteID, typeID) {
							flowTypeIDs[concreteID] = struct{}{}
						}
					}
				} else {
					flowTypeIDs[typeID] = struct{}{}
				}
			}
		}
		results[ordinal].Completeness = "closed"
		if call.ParserIncomplete {
			results[ordinal].Completeness = "partial"
		}
		results[ordinal].FlowTypeNodeIDs = sortedIDs(flowTypeIDs)
		for _, methodID := range methodsByName[call.CalleeName] {
			if _, ok := flowTypeIDs[meta[methodID].ParentID]; ok {
				results[ordinal].CandidateNodeIDs = append(results[ordinal].CandidateNodeIDs, methodID)
			}
		}
		if len(results[ordinal].CandidateNodeIDs) > 1 {
			results[ordinal].AbstentionReason = "ambiguous_viable_set"
		} else if len(results[ordinal].CandidateNodeIDs) == 0 {
			results[ordinal].AbstentionReason = "flow_type_without_viable_target"
		}
	}
	return results
}

func callerObjectScope(scope string) string {
	if dot := strings.LastIndex(scope, "."); dot > 0 {
		return scope[:dot]
	}
	return ""
}

func uniqueStrings(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = normalizedTypeName(value)
		if value == "" {
			continue
		}
		if _, ok := seen[value]; !ok {
			seen[value] = struct{}{}
			out = append(out, value)
		}
	}
	sort.Strings(out)
	return out
}

func callQualifier(qualified string) string {
	dot := strings.LastIndex(qualified, ".")
	if dot < 0 {
		return ""
	}
	return strings.TrimSpace(qualified[:dot])
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

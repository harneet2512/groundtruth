package resolver

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
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
	FlowSourceStableIDs  []string
	FlowEdgeStableIDs    []string
	SelectedTargetNodeID *int64
	Completeness         string
	AbstentionReason     string
	// FlowProofs preserves the evidence path for each retained candidate.  A
	// candidate's proof is never inferred from the aggregate callsite set.
	FlowProofs []VTAFlowProof
}

// VTAFlowProof is candidate-keyed provenance for one VTA target.
type VTAFlowProof struct {
	CandidateNodeID int64
	SourceStableIDs []string
	EdgeStableIDs   []string
}

type vtaValueEvidence struct {
	Types   map[string]struct{}
	Sources map[string]struct{}
	Edges   map[string]struct{}
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

	valueTypes := make(map[vtaValueKey]*vtaValueEvidence)
	addValue := func(key vtaValueKey, names, sources, edges []string) bool {
		if key.Name == "" || len(names) == 0 {
			return false
		}
		evidence := valueTypes[key]
		if evidence == nil {
			evidence = &vtaValueEvidence{Types: make(map[string]struct{}), Sources: make(map[string]struct{}), Edges: make(map[string]struct{})}
			valueTypes[key] = evidence
		}
		changed := false
		for _, name := range names {
			name = normalizedTypeName(name)
			if name == "" {
				continue
			}
			if _, exists := evidence.Types[name]; !exists {
				evidence.Types[name] = struct{}{}
				changed = true
			}
		}
		for _, source := range sources {
			if source != "" {
				if _, exists := evidence.Sources[source]; !exists {
					evidence.Sources[source] = struct{}{}
					changed = true
				}
			}
		}
		for _, edge := range edges {
			if edge != "" {
				if _, exists := evidence.Edges[edge]; !exists {
					evidence.Edges[edge] = struct{}{}
					changed = true
				}
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
		addValue(vtaValueKey{File: assignment.File, Scope: assignment.Scope, Object: assignment.ObjectScope, Name: assignment.VarName, Line: assignment.Line}, assignmentNames(assignment), []string{vtaAssignmentStableID(assignment)}, nil)
	}

	callValue := func(call parser.CallRef, name string) *vtaValueEvidence {
		object := callerObjectScope(call.CallerScope)
		field := strings.HasPrefix(name, "self.") || strings.HasPrefix(name, "this.")
		key := vtaValueKey{File: call.File, Scope: call.CallerScope, Object: object, Name: name}
		if !field {
			key.Object = ""
		}
		fallback := &vtaValueEvidence{Types: make(map[string]struct{}), Sources: make(map[string]struct{}), Edges: make(map[string]struct{})}
		for assignment, evidence := range valueTypes {
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
			for typ := range evidence.Types {
				fallback.Types[typ] = struct{}{}
			}
			for source := range evidence.Sources {
				fallback.Sources[source] = struct{}{}
			}
			for edge := range evidence.Edges {
				fallback.Edges[edge] = struct{}{}
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
					evidence := callValue(call, argument)
					edges := sortedStringSet(evidence.Edges)
					edges = append(edges, vtaCallEdgeStableID(call, argument, index, parameter))
					for typ := range evidence.Types {
						if addValue(vtaValueKey{File: parameter.File, Scope: parameter.Scope, Object: parameter.ObjectScope, Name: parameter.VarName, Line: parameter.Line}, []string{typ}, sortedStringSet(evidence.Sources), edges) {
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
		flowEvidence := callValue(call, qualifier)
		if len(flowEvidence.Types) == 0 {
			continue
		}
		results[ordinal].FlowSourceStableIDs = sortedStringSet(flowEvidence.Sources)
		results[ordinal].FlowEdgeStableIDs = sortedStringSet(flowEvidence.Edges)
		flowTypeIDs := make(map[int64]struct{})
		for name := range flowEvidence.Types {
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
		results[ordinal].Completeness = "partial"
		if call.FlowAnalysisComplete && !call.ParserIncomplete {
			results[ordinal].Completeness = "closed"
		}
		if call.ParserIncomplete {
			results[ordinal].Completeness = "partial"
		}
		results[ordinal].FlowTypeNodeIDs = sortedIDs(flowTypeIDs)
		for _, methodID := range methodsByName[call.CalleeName] {
			if _, ok := flowTypeIDs[meta[methodID].ParentID]; ok {
				results[ordinal].CandidateNodeIDs = append(results[ordinal].CandidateNodeIDs, methodID)
			}
		}
		for _, methodID := range results[ordinal].CandidateNodeIDs {
			parent := meta[methodID].ParentID
			proof := VTAFlowProof{CandidateNodeID: methodID}
			for assignment, evidence := range valueTypes {
				if assignment.Name != qualifier || assignment.File != call.File || assignment.Scope != call.CallerScope || assignment.Line > call.Line {
					continue
				}
				supported := false
				for evidenceType := range evidence.Types {
					for _, typeID := range typeIDsByName[normalizedTypeName(evidenceType)] {
						_, isInterface := interfaceIDs[typeID]
						if typeID == parent || (isInterface && implementsBase(implements, parent, typeID)) {
							supported = true
							break
						}
					}
					if supported {
						break
					}
				}
				if !supported {
					continue
				}
				proof.SourceStableIDs = append(proof.SourceStableIDs, sortedStringSet(evidence.Sources)...)
				proof.EdgeStableIDs = append(proof.EdgeStableIDs, sortedStringSet(evidence.Edges)...)
			}
			proof.SourceStableIDs = dedupeSorted(proof.SourceStableIDs)
			proof.EdgeStableIDs = dedupeSorted(proof.EdgeStableIDs)
			results[ordinal].FlowProofs = append(results[ordinal].FlowProofs, proof)
		}
		if len(results[ordinal].CandidateNodeIDs) > 1 {
			results[ordinal].AbstentionReason = "ambiguous_viable_set"
		} else if len(results[ordinal].CandidateNodeIDs) == 0 {
			results[ordinal].AbstentionReason = "flow_type_without_viable_target"
		}
	}
	return results
}

func vtaAssignmentStableID(assignment parser.AssignmentRef) string {
	return vtaStableFactID("source", assignment.File, assignment.Scope, assignment.ObjectScope, assignment.VarName, assignment.TypeName, assignment.TypeQualified, strconv.Itoa(assignment.Line), strconv.FormatBool(assignment.ViaReturn))
}

func vtaCallEdgeStableID(call parser.CallRef, argument string, index int, parameter parser.AssignmentRef) string {
	return vtaStableFactID("edge", call.File, call.CallerScope, call.CalleeScope, call.CalleeName, argument, strconv.Itoa(index), parameter.File, parameter.Scope, parameter.VarName, strconv.Itoa(parameter.Line))
}

func vtaStableFactID(kind string, fields ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(append([]string{kind}, fields...), "\x00")))
	return "vta_" + kind + "_" + hex.EncodeToString(sum[:])
}

func sortedStringSet(values map[string]struct{}) []string {
	result := make([]string, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Strings(result)
	return result
}

func dedupeSorted(values []string) []string {
	sort.Strings(values)
	if len(values) < 2 {
		return values
	}
	out := values[:1]
	for _, value := range values[1:] {
		if value != out[len(out)-1] {
			out = append(out, value)
		}
	}
	return out
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

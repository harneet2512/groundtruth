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
	// Iterations and IterationBudget make the fixed-point boundary explicit.
	// A positive budget that is exhausted is an incomplete analysis, never a
	// successful closed result.
	Iterations      int
	IterationBudget int
	BudgetExhausted bool
	// FlowProofs preserves the evidence path for each retained candidate.  A
	// candidate's proof is never inferred from the aggregate callsite set.
	FlowProofs []VTAFlowProof
}

// VTAAbstentionReason is the closed vocabulary for an analysis that cannot
// claim a complete result. Keep budget exhaustion distinct from parser or
// target ambiguity so consumers can report the actual boundary.
type VTAAbstentionReason string

const (
	VTAAbstentionReasonBudgetExhausted VTAAbstentionReason = "budget_exhausted"
)

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
	File      string
	Scope     string
	Object    string
	Name      string
	Line      int
	ViaReturn bool
	TypeName  string
}

// AnalyzeVTA builds a finite, flow-insensitive value-constraint graph and
// reaches a monotone fixed point over it. Assignments seed value facts;
// argument edges copy facts into callee parameters. Return annotations are
// resolved transitively, so a factory returning another factory is still
// useful without pretending that a unique target was verified.
// AnalyzeVTA uses the unbounded deterministic fixed point retained by the
// historical API. Production callers that need an execution rail should use
// AnalyzeVTAWithBudget and pass an explicit positive round limit.
func AnalyzeVTA(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef) []VTAResult {
	return AnalyzeVTAWithBudget(calls, meta, implements, assignments, 0)
}

// AnalyzeVTAWithBudget runs the same monotone on-the-fly propagation as
// AnalyzeVTA, but bounds complete worklist rounds. A zero budget means no
// bound. If the limit is reached while new facts remain, every affected
// virtual/interface result is marked partial with the typed
// budget_exhausted reason. Candidate identities remain conservative evidence;
// callers must not treat them as a closed analysis.
func AnalyzeVTAWithBudget(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef, maxIterations int) []VTAResult {
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
	// candidateEvidence is deliberately keyed by call ordinal and method ID. It
	// records the interprocedural edges that carried a value into a particular
	// candidate, rather than laundering one callsite-wide edge set into every
	// target's proof.
	candidateEvidence := make(map[int]map[int64]*vtaValueEvidence)
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
		addValue(vtaValueKey{File: assignment.File, Scope: assignment.Scope, Object: assignment.ObjectScope, Name: assignment.VarName, Line: assignment.Line, ViaReturn: assignment.ViaReturn, TypeName: assignment.TypeName}, assignmentNames(assignment), []string{vtaAssignmentStableID(assignment)}, nil)
	}

	callValue := func(call parser.CallRef, name string) *vtaValueEvidence {
		object := callerObjectScope(call.CallerScope)
		field := strings.HasPrefix(name, "self.") || strings.HasPrefix(name, "this.")
		key := vtaValueKey{File: call.File, Scope: call.CallerScope, Object: object, Name: name}
		if !field {
			key.Object = ""
		}
		fallback := &vtaValueEvidence{Types: make(map[string]struct{}), Sources: make(map[string]struct{}), Edges: make(map[string]struct{})}
		type fieldHit struct {
			object   string
			evidence *vtaValueEvidence
		}
		var fieldHits []fieldHit
		for assignment, evidence := range valueTypes {
			if assignment.Name != key.Name {
				continue
			}
			if !field {
				if assignment.Line > call.Line || assignment.File != key.File || (call.CallerScope != "" && assignment.Scope != key.Scope) {
					continue
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
				continue
			}
			if assignment.File == key.File && assignment.Line > call.Line {
				continue
			}
			fieldHits = append(fieldHits, fieldHit{object: assignment.Object, evidence: evidence})
		}
		if field {
			scoped := make([]*vtaValueEvidence, 0, len(fieldHits))
			if key.Object != "" {
				for _, hit := range fieldHits {
					if hit.object == key.Object {
						scoped = append(scoped, hit.evidence)
					}
				}
			}
			if len(scoped) == 0 {
				for _, hit := range fieldHits {
					scoped = append(scoped, hit.evidence)
				}
			}
			for _, evidence := range scoped {
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
	methodScope := func(methodID int64) string {
		method := meta[methodID]
		if method.ParentID != 0 && meta[method.ParentID].Name != "" {
			return meta[method.ParentID].Name + "." + method.Name
		}
		return method.Name
	}
	methodReceiver := func(methodID int64) string {
		if name := strings.TrimSpace(meta[methodID].ReceiverName); name != "" {
			return name
		}
		// Python/JS methods expose the receiver through self/this in the parsed
		// selector even though the generic NodeMeta has no named receiver field.
		return "self"
	}
	mergeEvidence := func(dst *vtaValueEvidence, src *vtaValueEvidence, edges ...string) {
		if dst == nil {
			return
		}
		if src != nil {
			for typ := range src.Types {
				dst.Types[typ] = struct{}{}
			}
			for source := range src.Sources {
				dst.Sources[source] = struct{}{}
			}
			for edge := range src.Edges {
				dst.Edges[edge] = struct{}{}
			}
		}
		for _, edge := range edges {
			if edge != "" {
				dst.Edges[edge] = struct{}{}
			}
		}
	}
	addCandidateEvidence := func(ordinal int, methodID int64, source *vtaValueEvidence, edges ...string) {
		byTarget := candidateEvidence[ordinal]
		if byTarget == nil {
			byTarget = make(map[int64]*vtaValueEvidence)
			candidateEvidence[ordinal] = byTarget
		}
		dst := byTarget[methodID]
		if dst == nil {
			dst = &vtaValueEvidence{Types: make(map[string]struct{}), Sources: make(map[string]struct{}), Edges: make(map[string]struct{})}
			byTarget[methodID] = dst
		}
		mergeEvidence(dst, source, edges...)
	}
	flowCandidates := func(call parser.CallRef, evidence *vtaValueEvidence) []int64 {
		if evidence == nil || len(evidence.Types) == 0 {
			return nil
		}
		flowTypeIDs := make(map[int64]struct{})
		for name := range evidence.Types {
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
		out := make([]int64, 0)
		for _, methodID := range methodsByName[call.CalleeName] {
			if _, ok := flowTypeIDs[meta[methodID].ParentID]; ok {
				out = append(out, methodID)
			}
		}
		return out
	}
	// Monotone worklist: every iteration only adds a value fact, so finite
	// type/name facts guarantee termination even for cyclic calls. In addition
	// to ordinary argument/formal flow, each viable target receives a distinct
	// receiver-to-this edge and target-keyed argument-to-formal edges.
	iterations := 0
	budgetExhausted := false
	for changed := true; changed; {
		if maxIterations > 0 && iterations >= maxIterations {
			budgetExhausted = true
			break
		}
		iterations++
		changed = false
		for ordinal, call := range calls {
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
			if (call.DispatchForm == "interface" || call.DispatchForm == "virtual") && callQualifier(call.CalleeQualified) != "" {
				receiver := callQualifier(call.CalleeQualified)
				receiverEvidence := callValue(call, receiver)
				for _, methodID := range flowCandidates(call, receiverEvidence) {
					method := meta[methodID]
					thisName := methodReceiver(methodID)
					object := ""
					if method.ParentID != 0 {
						object = meta[method.ParentID].Name
					}
					thisKey := vtaValueKey{File: method.File, Scope: methodScope(methodID), Object: object, Name: thisName, Line: method.StartLine}
					if addValue(thisKey, sortedStringSet(receiverEvidence.Types), sortedStringSet(receiverEvidence.Sources), append(sortedStringSet(receiverEvidence.Edges), vtaReceiverToThisStableID(call, receiver, methodID, thisName))) {
						changed = true
					}
					// The source set is filtered again at result construction by the
					// candidate's parent type. Keep only this target's edge here;
					// copying receiverEvidence would reintroduce a callsite-wide source
					// union into every candidate proof.
					addCandidateEvidence(ordinal, methodID, nil, vtaReceiverToThisStableID(call, receiver, methodID, thisName))
					for index, argument := range call.ArgumentNames {
						argumentEvidence := callValue(call, argument)
						if len(argumentEvidence.Types) == 0 {
							continue
						}
						var formal *parser.AssignmentRef
						for i := range assignments {
							candidate := &assignments[i]
							if candidate.IsParameter && candidate.ParameterIndex == index && strings.TrimSpace(candidate.Scope) == strings.TrimSpace(methodScope(methodID)) {
								formal = candidate
								break
							}
						}
						edge := vtaArgumentToFormalStableID(call, argument, index, methodID, formal)
						if formal != nil && addValue(vtaValueKey{File: formal.File, Scope: formal.Scope, Object: formal.ObjectScope, Name: formal.VarName, Line: formal.Line}, sortedStringSet(argumentEvidence.Types), sortedStringSet(argumentEvidence.Sources), append(sortedStringSet(argumentEvidence.Edges), edge)) {
							changed = true
						}
						addCandidateEvidence(ordinal, methodID, nil, edge)
					}
				}
			}
		}
	}

	results := make([]VTAResult, len(calls))
	for ordinal, call := range calls {
		results[ordinal] = VTAResult{CallsiteOrdinal: ordinal, Completeness: "not_run", Iterations: iterations, IterationBudget: maxIterations, BudgetExhausted: budgetExhausted}
		if call.DispatchForm != "interface" && call.DispatchForm != "virtual" {
			continue
		}
		qualifier := callQualifier(call.CalleeQualified)
		if qualifier == "" {
			continue
		}
		flowEvidence := callValue(call, qualifier)
		if len(flowEvidence.Types) == 0 {
			if budgetExhausted {
				results[ordinal].Completeness = "partial"
				results[ordinal].AbstentionReason = string(VTAAbstentionReasonBudgetExhausted)
			}
			continue
		}
		results[ordinal].FlowSourceStableIDs = sortedStringSet(flowEvidence.Sources)
		results[ordinal].FlowEdgeStableIDs = sortedStringSet(flowEvidence.Edges)
		flowTypeIDs := make(map[int64]struct{})
		for _, methodID := range flowCandidates(call, flowEvidence) {
			flowTypeIDs[meta[methodID].ParentID] = struct{}{}
		}
		results[ordinal].Completeness = "partial"
		if call.FlowAnalysisComplete && !call.ParserIncomplete {
			results[ordinal].Completeness = "closed"
		}
		if call.ParserIncomplete {
			results[ordinal].Completeness = "partial"
		}
		results[ordinal].FlowTypeNodeIDs = sortedIDs(flowTypeIDs)
		results[ordinal].CandidateNodeIDs = flowCandidates(call, flowEvidence)
		for _, methodID := range results[ordinal].CandidateNodeIDs {
			parent := meta[methodID].ParentID
			proof := VTAFlowProof{CandidateNodeID: methodID}
			for assignment, evidence := range valueTypes {
				if assignment.Name != qualifier {
					continue
				}
				field := strings.HasPrefix(qualifier, "self.") || strings.HasPrefix(qualifier, "this.")
				if !field && (assignment.File != call.File || assignment.Scope != call.CallerScope || assignment.Line > call.Line) {
					continue
				}
				if field && assignment.File == call.File && assignment.Line > call.Line {
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
				if assignment.ViaReturn {
					// Keep the factory's return transfer in the candidate proof.  The
					// assignment source establishes the returned value, while this edge
					// establishes that the value reached the caller's result/receiver.
					// Without this candidate-bound edge, return-type inference would
					// look indistinguishable from a direct constructor assignment.
					proof.EdgeStableIDs = append(proof.EdgeStableIDs,
						vtaReturnToResultStableID(parser.AssignmentRef{File: assignment.File, Scope: assignment.Scope, VarName: assignment.Name, TypeName: assignment.TypeName, Line: assignment.Line, ViaReturn: true}, methodID))
				}
			}
			if byTarget := candidateEvidence[ordinal]; byTarget != nil {
				if targetEvidence := byTarget[methodID]; targetEvidence != nil {
					proof.SourceStableIDs = append(proof.SourceStableIDs, sortedStringSet(targetEvidence.Sources)...)
					proof.EdgeStableIDs = append(proof.EdgeStableIDs, sortedStringSet(targetEvidence.Edges)...)
				}
			}
			proof.EdgeStableIDs = append(proof.EdgeStableIDs, vtaReceiverToThisStableID(call, qualifier, methodID, methodReceiver(methodID)))
			if call.FlowAnalysisComplete && !call.ParserIncomplete {
				// The explicit closed boundary is the producer's witness that the
				// monotone value/constraint worklist reached its fixed point for the
				// candidate body.  Bind it to the call and target so it cannot be
				// reused for another candidate or callsite.
				proof.EdgeStableIDs = append(proof.EdgeStableIDs,
					vtaBodyConvergenceStableID(call, methodID))
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
		if budgetExhausted {
			results[ordinal].Completeness = "partial"
			results[ordinal].AbstentionReason = string(VTAAbstentionReasonBudgetExhausted)
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

// vtaReceiverToThisStableID identifies the candidate-specific receiver edge.
// The method ID is part of the identity: two viable implementations at one
// callsite must never share a receiver proof merely because their type names
// are equal or their callsite evidence is the same.
func vtaReceiverToThisStableID(call parser.CallRef, receiver string, methodID int64, receiverName string) string {
	return vtaStableFactID("edge", "receiver_to_this", call.File, call.CallerScope, strconv.Itoa(call.Line), receiver, strconv.FormatInt(methodID, 10), receiverName)
}

// vtaReturnToResultStableID identifies the candidate-specific transfer from a
// factory's return value to the caller's result variable.  The candidate ID is
// part of the identity so two viable targets cannot share a return proof.
func vtaReturnToResultStableID(assignment parser.AssignmentRef, methodID int64) string {
	return vtaStableFactID("edge", "return_to_result", assignment.File, assignment.Scope,
		assignment.VarName, assignment.TypeName, strconv.Itoa(assignment.Line), strconv.FormatInt(methodID, 10))
}

// vtaBodyConvergenceStableID identifies the closed fixed-point boundary for a
// particular callsite/target pair.  It is emitted only when CallRef carries the
// explicit complete-analysis proof and parser completeness is intact.
func vtaBodyConvergenceStableID(call parser.CallRef, methodID int64) string {
	return vtaStableFactID("edge", "body_convergence", call.File, call.CallerScope,
		strconv.Itoa(call.Line), call.CalleeName, strconv.FormatInt(methodID, 10))
}

// vtaArgumentToFormalStableID identifies a candidate-specific argument edge.
// A missing parsed formal is represented by empty fields; the edge remains
// deterministic while the caller can keep the candidate conservative until a
// later parser-completeness unit supplies the formal binding.
func vtaArgumentToFormalStableID(call parser.CallRef, argument string, index int, methodID int64, formal *parser.AssignmentRef) string {
	fields := []string{"argument_to_formal", call.File, call.CallerScope, strconv.Itoa(call.Line), argument, strconv.Itoa(index), strconv.FormatInt(methodID, 10)}
	if formal != nil {
		fields = append(fields, formal.File, formal.Scope, formal.VarName, strconv.Itoa(formal.Line))
	}
	return vtaStableFactID("edge", fields...)
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

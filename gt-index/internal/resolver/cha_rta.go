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
	ReceiverScoped       bool
	CHACandidateNodeIDs  []int64
	RTACandidateNodeIDs  []int64
	RTACompleteness      string
	RTAAllocationTypeIDs []int64
	ReachableNodeIDs     []int64
	RTAAbstentionReason  string
	CHACompleteness      string
}

// AnalyzeCHAThenRTA performs a deterministic, producer-owned CHA→RTA pass.
// The implements map is child type ID → implemented base/interface IDs. When
// a language has no explicit relationship facts, structural method-set
// inference supplies the same conservative candidate boundary for interfaces.
func AnalyzeCHAThenRTA(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef, hierarchyClosed bool) []CHAThenRTAResult {
	return analyzeCHAThenRTA(calls, meta, implements, assignments, nil, hierarchyClosed)
}

// AnalyzeCHAThenRTAWithReceiverTypes is the production entry point. The
// receiverTypes map is keyed by callsite ordinal and comes from the existing
// resolver's declared receiver evidence; the assignment fallback keeps the
// small public fixture API useful for direct producer tests.
func AnalyzeCHAThenRTAWithReceiverTypes(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef, receiverTypes map[int]string, hierarchyClosed bool) []CHAThenRTAResult {
	return analyzeCHAThenRTA(calls, meta, implements, assignments, receiverTypes, hierarchyClosed)
}

// AnalyzeCHAThenRTAWithReachability is the production entry point. It runs
// the hierarchy pass, then computes the monotone reachable/instantiated fixed
// point used by RTA. callerNodeIDs and candidateNodeIDs are parallel to calls;
// the latter are the already-resolved direct-call identities from the normal
// resolver path. A virtual call expands only through its current RTA targets,
// so an allocation discovered in a newly reachable body can cause a second
// iteration without importing allocations from unreachable code.
func AnalyzeCHAThenRTAWithReachability(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef, receiverTypes map[int]string, callerNodeIDs []int64, candidateNodeIDs [][]int64, hierarchyClosed bool) []CHAThenRTAResult {
	results := analyzeCHAThenRTA(calls, meta, implements, assignments, receiverTypes, hierarchyClosed)
	// The ordinary analyzer is intentionally useful as a small fixture API and
	// therefore reports lexical allocation evidence immediately. Production RTA
	// must not use that evidence until the owning body is proven reachable.
	for i := range results {
		if i >= len(calls) || (calls[i].DispatchForm != "interface" && calls[i].DispatchForm != "virtual") {
			continue
		}
		results[i].RTACandidateNodeIDs = nil
		results[i].RTAAllocationTypeIDs = nil
	}
	reachable, instantiated := reachableInstantiationFixedPoint(calls, meta, assignments, results, callerNodeIDs, candidateNodeIDs)
	for i := range results {
		if i >= len(calls) {
			break
		}
		if calls[i].DispatchForm != "interface" && calls[i].DispatchForm != "virtual" {
			continue
		}
		results[i].ReachableNodeIDs = sortedIDs(reachable)
		results[i].RTAAllocationTypeIDs = filterInstantiatedCandidateTypes(results[i].CHACandidateNodeIDs, meta, instantiated)
		results[i].RTACandidateNodeIDs = make([]int64, 0, len(results[i].CHACandidateNodeIDs))
		for _, methodID := range results[i].CHACandidateNodeIDs {
			if _, ok := instantiated[meta[methodID].ParentID]; ok {
				results[i].RTACandidateNodeIDs = append(results[i].RTACandidateNodeIDs, methodID)
			}
		}
	}
	return results
}

func analyzeCHAThenRTA(calls []parser.CallRef, meta map[int64]NodeMeta, implements map[int64][]int64, assignments []parser.AssignmentRef, receiverTypes map[int]string, hierarchyClosed bool) []CHAThenRTAResult {
	if implements == nil {
		implements = make(map[int64][]int64)
	}
	methodsByName := make(map[string][]int64)
	typeIDsByName := make(map[string][]int64)
	interfaceIDs := make(map[int64]struct{})
	concreteIDs := make(map[int64]struct{})
	methodNamesByType := make(map[int64]map[string]struct{})
	methodShapesByType := make(map[int64]map[string]struct{})
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
			if shape := methodShape(node.Name, node.Signature); shape != "" {
				if methodShapesByType[node.ParentID] == nil {
					methodShapesByType[node.ParentID] = make(map[string]struct{})
				}
				methodShapesByType[node.ParentID][shape] = struct{}{}
			}
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
			if len(want) == 0 || (hasAllMethods(methodNamesByType[concreteID], want) && hasAllMethodShapes(methodShapesByType[concreteID], methodShapesByType[interfaceID])) {
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

		receiverType := receiverTypeForCall(call, ordinal, receiverTypes, assignments, typeIDsByName, meta)
		candidateTypes := concreteTypesForCall(receiverType, typeIDsByName, concreteIDs, interfaceIDs, implements)
		result.ReceiverScoped = receiverType != "" && len(typeIDsByName[receiverType]) > 0
		if len(candidateTypes) == 0 && receiverType == "" {
			// An untyped virtual call has no receiver boundary to apply. Retain
			// the conservative set of explicitly related concrete types rather
			// than inventing a receiver type from a same-named method.
			for concreteID := range concreteIDs {
				if len(implements[concreteID]) > 0 {
					candidateTypes[concreteID] = struct{}{}
				}
			}
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

func hasAllMethodShapes(have map[string]struct{}, want map[string]struct{}) bool {
	if len(want) == 0 {
		return true
	}
	if len(have) == 0 {
		return false
	}
	for shape := range want {
		if _, ok := have[shape]; !ok {
			return false
		}
	}
	return true
}

func methodShape(name, signature string) string {
	if strings.TrimSpace(signature) == "" {
		return ""
	}
	start := strings.Index(signature, name)
	if start < 0 {
		return ""
	}
	shape := strings.TrimSpace(signature[start:])
	if !strings.Contains(shape, "(") || !strings.Contains(shape, ")") {
		return ""
	}
	return strings.Join(strings.Fields(shape), " ")
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
		if assignment.VarName != qualifier || assignment.Line > call.Line {
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

func receiverTypeForCall(call parser.CallRef, ordinal int, receiverTypes map[int]string, assignments []parser.AssignmentRef, typeIDsByName map[string][]int64, meta map[int64]NodeMeta) string {
	if receiverTypes != nil && strings.TrimSpace(receiverTypes[ordinal]) != "" {
		return normalizedTypeName(receiverTypes[ordinal])
	}
	qualifier := callReceiverQualifier(call)
	if qualifier == "" {
		return ""
	}
	bestLine := -1
	bestType := ""
	for _, assignment := range assignments {
		if assignment.VarName != qualifier || assignment.File != call.File || assignment.Line > call.Line || assignment.ViaReturn {
			continue
		}
		for _, typeName := range []string{assignment.TypeName, assignment.TypeQualified} {
			for _, typeID := range typeIDsByName[normalizedTypeName(typeName)] {
				if meta[typeID].Label == "Interface" && assignment.Line >= bestLine {
					bestLine = assignment.Line
					bestType = normalizedTypeName(typeName)
				}
			}
		}
	}
	return bestType
}

func concreteTypesForCall(receiverType string, typeIDsByName map[string][]int64, concreteIDs, interfaceIDs map[int64]struct{}, implements map[int64][]int64) map[int64]struct{} {
	result := make(map[int64]struct{})
	if receiverType == "" {
		return result
	}
	for _, receiverID := range typeIDsByName[receiverType] {
		if _, ok := interfaceIDs[receiverID]; !ok {
			if _, ok := concreteIDs[receiverID]; ok {
				result[receiverID] = struct{}{}
			}
			continue
		}
		for concreteID := range concreteIDs {
			if implementsTransitively(concreteID, receiverID, implements) {
				result[concreteID] = struct{}{}
			}
		}
	}
	return result
}

func implementsTransitively(childID, targetID int64, implements map[int64][]int64) bool {
	seen := make(map[int64]struct{})
	var visit func(int64) bool
	visit = func(current int64) bool {
		if current == targetID {
			return true
		}
		if _, ok := seen[current]; ok {
			return false
		}
		seen[current] = struct{}{}
		for _, parentID := range implements[current] {
			if visit(parentID) {
				return true
			}
		}
		return false
	}
	return visit(childID)
}

func callReceiverQualifier(call parser.CallRef) string {
	qualified := call.CalleeQualified
	if dot := strings.LastIndex(qualified, "."); dot >= 0 {
		return qualified[:dot]
	}
	return ""
}

func reachableInstantiationFixedPoint(calls []parser.CallRef, meta map[int64]NodeMeta, assignments []parser.AssignmentRef, results []CHAThenRTAResult, callerNodeIDs []int64, candidateNodeIDs [][]int64) (map[int64]struct{}, map[int64]struct{}) {
	callsByCaller := make(map[int64][]int)
	for ordinal, callerID := range callerNodeIDs {
		if callerID != 0 && ordinal < len(calls) {
			callsByCaller[callerID] = append(callsByCaller[callerID], ordinal)
		}
	}

	knownTargets := make(map[int64]struct{})
	for _, targets := range candidateNodeIDs {
		for _, targetID := range targets {
			knownTargets[targetID] = struct{}{}
		}
	}
	for _, result := range results {
		for _, targetID := range result.CHACandidateNodeIDs {
			knownTargets[targetID] = struct{}{}
		}
	}
	reachable := make(map[int64]struct{})
	for _, callerID := range callerNodeIDs {
		if callerID == 0 {
			continue
		}
		if _, isTarget := knownTargets[callerID]; !isTarget {
			reachable[callerID] = struct{}{}
		}
	}
	// A closed cycle has no graph-theoretic root. Retaining the caller set is
	// the conservative fallback; it cannot introduce an allocation by itself.
	if len(reachable) == 0 {
		for _, callerID := range callerNodeIDs {
			if callerID != 0 {
				reachable[callerID] = struct{}{}
			}
		}
	}
	instantiated := make(map[int64]struct{})
	for {
		changed := false
		for callerID := range reachable {
			for _, ordinal := range callsByCaller[callerID] {
				call := calls[ordinal]
				var targets []int64
				if call.DispatchForm == "interface" || call.DispatchForm == "virtual" {
					targets = results[ordinal].RTACandidateNodeIDs
				} else if ordinal < len(candidateNodeIDs) && len(candidateNodeIDs[ordinal]) > 0 {
					targets = candidateNodeIDs[ordinal]
				} else {
					// Direct-call reachability is driven by the already-resolved
					// candidate identities. A global name fallback would make an
					// unrelated same-named function reachable.
					targets = nil
				}
				for _, targetID := range targets {
					if _, ok := meta[targetID]; !ok {
						continue
					}
					if _, ok := reachable[targetID]; !ok {
						reachable[targetID] = struct{}{}
						changed = true
					}
				}
			}
		}
		for nodeID := range reachable {
			node, ok := meta[nodeID]
			if !ok {
				continue
			}
			for _, assignment := range assignments {
				if assignment.ViaReturn || assignment.Scope != node.Name || !sameSourceFile(assignment.File, node.File) {
					continue
				}
				for typeID, typeNode := range meta {
					if !isConcreteHierarchyType(typeNode.Label) || (normalizedTypeName(typeNode.Name) != normalizedTypeName(assignment.TypeName) && normalizedTypeName(typeNode.Name) != normalizedTypeName(assignment.TypeQualified)) {
						continue
					}
					if _, ok := instantiated[typeID]; !ok {
						instantiated[typeID] = struct{}{}
						changed = true
					}
				}
			}
		}
		for ordinal := range calls {
			if calls[ordinal].DispatchForm != "interface" && calls[ordinal].DispatchForm != "virtual" {
				continue
			}
			for _, methodID := range results[ordinal].CHACandidateNodeIDs {
				if _, ok := instantiated[meta[methodID].ParentID]; ok {
					if _, reachableAlready := reachable[methodID]; !reachableAlready {
						reachable[methodID] = struct{}{}
						changed = true
					}
				}
			}
		}
		if !changed {
			break
		}
		for ordinal := range results {
			if calls[ordinal].DispatchForm != "interface" && calls[ordinal].DispatchForm != "virtual" {
				continue
			}
			results[ordinal].RTACandidateNodeIDs = filterReachableMethods(results[ordinal].CHACandidateNodeIDs, meta, instantiated)
		}
	}
	return reachable, instantiated
}

func filterReachableMethods(methodIDs []int64, meta map[int64]NodeMeta, instantiated map[int64]struct{}) []int64 {
	result := make([]int64, 0, len(methodIDs))
	for _, methodID := range methodIDs {
		method, exists := meta[methodID]
		if exists {
			if _, ok := instantiated[method.ParentID]; ok {
				result = append(result, methodID)
			}
		}
	}
	return result
}

func filterInstantiatedCandidateTypes(methodIDs []int64, meta map[int64]NodeMeta, instantiated map[int64]struct{}) []int64 {
	seen := make(map[int64]struct{})
	for _, methodID := range methodIDs {
		method, exists := meta[methodID]
		if !exists {
			continue
		}
		parentID := method.ParentID
		if _, ok := instantiated[parentID]; ok {
			seen[parentID] = struct{}{}
		}
	}
	return sortedIDs(seen)
}

func sortedIDs(ids map[int64]struct{}) []int64 {
	result := make([]int64, 0, len(ids))
	for id := range ids {
		result = append(result, id)
	}
	sort.Slice(result, func(i, j int) bool { return result[i] < result[j] })
	return result
}

func sameSourceFile(left, right string) bool {
	left = strings.ReplaceAll(left, "\\", "/")
	right = strings.ReplaceAll(right, "\\", "/")
	return left == right || strings.HasSuffix(left, "/"+right) || strings.HasSuffix(right, "/"+left)
}

func isConcreteHierarchyType(label string) bool {
	return label == "Class" || label == "Struct" || label == "Type" || label == "Trait"
}

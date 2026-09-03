package resolver

// MROPass is the pass name under which method-resolution-order linearisation
// publishes its outcome in a callsite's pass coverage. Like narrowing it can
// never win a callsite: it orders a candidate set, it does not derive one.
const MROPass = "mro"

// Linearisation kinds. The kind names the algorithm that produced the order, so
// a consumer never has to guess which semantics an order carries.
const (
	MROKindC3          = "c3"
	MROKindDepthFirst  = "depth_first"
	MROKindSingleChain = "single_chain"
	MROKindNone        = "none"
)

// Linearisation and ordering status vocabulary. Every result is named; an
// inconsistent hierarchy is published as inconsistent and ordered by nothing.
const (
	MROStatusLinearized     = "linearized"
	MROStatusNoBases        = "no_base_classes"
	MROStatusInconsistent   = "inconsistent_hierarchy"
	MROStatusCycle          = "hierarchy_cycle"
	MROStatusNoLanguageMRO  = "language_has_no_linearisation"
	MROStatusClassUnknown   = "class_not_in_hierarchy"
	MROStatusNotApplicable  = "not_applicable"
	MROStatusOrdered        = "ordered"
	MROStatusAlreadyOrdered = "order_unchanged"
)

// Reasons an ordering attempt produced nothing.
const (
	MROReasonNotInheritedMechanism = "mechanism_is_not_inherited"
	MROReasonBelowTwoCandidates    = "fewer_than_two_candidates"
	MROReasonNoDefiningClass       = "no_candidate_declares_a_class"
	MROReasonNoCommonLinearisation = "no_candidate_class_linearises_the_others"
)

// mroDepthCap bounds every hierarchy walk. A malformed or generated hierarchy
// must cost a bounded amount of work and say it stopped, never loop.
const mroDepthCap = 64

// multipleInheritanceLanguages linearise a diamond. Python's rule is C3.
// C++ has multiple inheritance but no linearisation in the language, so its
// order is depth-first over the declared base list and is labelled as such.
var multipleInheritanceLanguages = map[string]string{
	"python": MROKindC3,
	"cpp":    MROKindDepthFirst,
}

// singleInheritanceLanguages have at most one base class per type, so their
// resolution order is the chain itself. Ruby and Scala mix in modules and
// traits that this producer does not extract as bases; their chain is the
// class chain only, which is why they are labelled single_chain rather than c3.
var singleInheritanceLanguages = map[string]struct{}{
	"c": {}, "csharp": {}, "go": {}, "java": {}, "javascript": {}, "kotlin": {},
	"php": {}, "ruby": {}, "rust": {}, "scala": {}, "swift": {}, "typescript": {},
	"elixir": {}, "groovy": {}, "lua": {},
}

// MROResult is the named outcome of linearising one class. An empty Order is
// always accompanied by a Status that says why.
type MROResult struct {
	Order  []int64
	Kind   string
	Status string
}

// MROKindForLanguage names the linearisation a grammar supports. A grammar with
// no inheritance at all returns MROKindNone, which is a statement, not silence.
func MROKindForLanguage(language string) string {
	if kind, ok := multipleInheritanceLanguages[language]; ok {
		return kind
	}
	if _, ok := singleInheritanceLanguages[language]; ok {
		return MROKindSingleChain
	}
	return MROKindNone
}

// LinearizeMRO orders a class and its transitive bases by the language's own
// resolution rule. bases maps a class to its declared base classes in
// declaration order. The result never promotes anything: it is an ordering of
// identities the hierarchy extraction already produced.
func LinearizeMRO(language string, class int64, bases map[int64][]int64) MROResult {
	kind := MROKindForLanguage(language)
	if kind == MROKindNone {
		return MROResult{Kind: kind, Status: MROStatusNoLanguageMRO}
	}
	if _, known := bases[class]; !known {
		return MROResult{Kind: kind, Status: MROStatusClassUnknown}
	}
	if len(bases[class]) == 0 {
		return MROResult{Order: []int64{class}, Kind: kind, Status: MROStatusNoBases}
	}
	switch kind {
	case MROKindC3:
		order, status := linearizeC3(class, bases, make(map[int64]bool), 0)
		return MROResult{Order: order, Kind: kind, Status: status}
	case MROKindDepthFirst:
		order, status := linearizeDepthFirst(class, bases)
		return MROResult{Order: order, Kind: kind, Status: status}
	default:
		order, status := linearizeSingleChain(class, bases)
		return MROResult{Order: order, Kind: kind, Status: status}
	}
}

// linearizeC3 is the C3 linearisation: L[C] = C + merge(L[B1]..L[Bn], [B1..Bn]).
// A hierarchy whose merge has no valid head is inconsistent, and Python itself
// refuses to build such a class. This reports that rather than inventing an
// order for it.
func linearizeC3(class int64, bases map[int64][]int64, visiting map[int64]bool, depth int) ([]int64, string) {
	if depth > mroDepthCap {
		return nil, MROStatusCycle
	}
	if visiting[class] {
		return nil, MROStatusCycle
	}
	visiting[class] = true
	defer delete(visiting, class)

	parents := bases[class]
	if len(parents) == 0 {
		return []int64{class}, MROStatusLinearized
	}
	sequences := make([][]int64, 0, len(parents)+1)
	for _, parent := range parents {
		parentOrder, status := linearizeC3(parent, bases, visiting, depth+1)
		if status != MROStatusLinearized && status != MROStatusNoBases {
			return nil, status
		}
		sequences = append(sequences, parentOrder)
	}
	sequences = append(sequences, append([]int64(nil), parents...))
	merged, ok := c3Merge(sequences)
	if !ok {
		return nil, MROStatusInconsistent
	}
	return append([]int64{class}, merged...), MROStatusLinearized
}

// c3Merge takes the head of the first sequence whose head appears in no other
// sequence's tail, removes it everywhere, and repeats. No such head means the
// hierarchy cannot be linearised.
func c3Merge(sequences [][]int64) ([]int64, bool) {
	var merged []int64
	for step := 0; step <= mroDepthCap*len(sequences); step++ {
		sequences = dropEmptySequences(sequences)
		if len(sequences) == 0 {
			return merged, true
		}
		head, ok := nextC3Head(sequences)
		if !ok {
			return nil, false
		}
		merged = append(merged, head)
		sequences = removeFromHeads(sequences, head)
	}
	return nil, false
}

func nextC3Head(sequences [][]int64) (int64, bool) {
	for _, sequence := range sequences {
		head := sequence[0]
		if !appearsInAnyTail(head, sequences) {
			return head, true
		}
	}
	return 0, false
}

func appearsInAnyTail(head int64, sequences [][]int64) bool {
	for _, sequence := range sequences {
		for _, id := range sequence[1:] {
			if id == head {
				return true
			}
		}
	}
	return false
}

func removeFromHeads(sequences [][]int64, head int64) [][]int64 {
	out := make([][]int64, 0, len(sequences))
	for _, sequence := range sequences {
		if sequence[0] == head {
			sequence = sequence[1:]
		}
		out = append(out, sequence)
	}
	return out
}

func dropEmptySequences(sequences [][]int64) [][]int64 {
	out := make([][]int64, 0, len(sequences))
	for _, sequence := range sequences {
		if len(sequence) > 0 {
			out = append(out, sequence)
		}
	}
	return out
}

// linearizeDepthFirst is C++'s order: the declared base list walked left to
// right, depth first, keeping the first occurrence of a repeated base.
func linearizeDepthFirst(class int64, bases map[int64][]int64) ([]int64, string) {
	var order []int64
	seen := make(map[int64]bool)
	var walk func(int64, int) string
	walk = func(current int64, depth int) string {
		if depth > mroDepthCap {
			return MROStatusCycle
		}
		if seen[current] {
			return MROStatusLinearized
		}
		seen[current] = true
		order = append(order, current)
		for _, parent := range bases[current] {
			if status := walk(parent, depth+1); status != MROStatusLinearized {
				return status
			}
		}
		return MROStatusLinearized
	}
	if status := walk(class, 0); status != MROStatusLinearized {
		return nil, status
	}
	return order, MROStatusLinearized
}

// linearizeSingleChain walks the single base chain. A class that declares more
// than one base in a single-inheritance grammar is a contradiction between the
// grammar and the extraction, and is reported as inconsistent rather than
// silently resolved by taking the first base.
func linearizeSingleChain(class int64, bases map[int64][]int64) ([]int64, string) {
	order := []int64{class}
	seen := map[int64]bool{class: true}
	current := class
	for depth := 0; depth <= mroDepthCap; depth++ {
		parents := bases[current]
		switch {
		case len(parents) == 0:
			return order, MROStatusLinearized
		case len(parents) > 1:
			return nil, MROStatusInconsistent
		}
		current = parents[0]
		if seen[current] {
			return nil, MROStatusCycle
		}
		seen[current] = true
		order = append(order, current)
	}
	return nil, MROStatusCycle
}

// MROOrdering is the named outcome of ordering one callsite's candidates.
type MROOrdering struct {
	Status  string
	Reason  string
	Kind    string
	Order   []int64
	Ordered int
}

// OrderCandidatesByMRO orders an inherited-mechanism candidate set by the
// linearisation of the most derived class among the candidates' defining
// classes. It changes order only. No candidate is added, removed, selected, or
// re-tiered, so an ambiguous callsite stays exactly as ambiguous as it was.
func OrderCandidatesByMRO(language, mechanism string, candidates []int64, bases map[int64][]int64, meta map[int64]NodeMeta) MROOrdering {
	ordering := MROOrdering{
		Status: MROStatusNotApplicable,
		Kind:   MROKindForLanguage(language),
		Order:  candidates,
	}
	switch {
	case mechanism != "inherited":
		ordering.Reason = MROReasonNotInheritedMechanism
		return ordering
	case len(candidates) < 2:
		ordering.Reason = MROReasonBelowTwoCandidates
		return ordering
	case ordering.Kind == MROKindNone:
		ordering.Reason = MROStatusNoLanguageMRO
		return ordering
	}
	classes := definingClasses(candidates, meta)
	if len(classes) == 0 {
		ordering.Reason = MROReasonNoDefiningClass
		return ordering
	}
	linear, ok := mostDerivedLinearisation(language, classes, bases)
	if !ok {
		ordering.Reason = MROReasonNoCommonLinearisation
		return ordering
	}
	ordered, ranked := orderByLinearisation(candidates, classes, linear)
	ordering.Order, ordering.Ordered = ordered, ranked
	ordering.Status = MROStatusOrdered
	ordering.Reason = MROStatusLinearized
	if sameOrder(candidates, ordered) {
		ordering.Status = MROStatusAlreadyOrdered
	}
	return ordering
}

// definingClasses maps each candidate to the class that declares it. A
// candidate with no parent class is not in the map and keeps its incoming
// position.
func definingClasses(candidates []int64, meta map[int64]NodeMeta) map[int64]int64 {
	classes := make(map[int64]int64, len(candidates))
	for _, id := range candidates {
		if m, ok := meta[id]; ok && m.ParentID != 0 {
			classes[id] = m.ParentID
		}
	}
	return classes
}

// mostDerivedLinearisation picks the candidate class whose own linearisation
// covers the most of the other candidate classes. That class is the one the
// dispatch actually starts from; ties break on the lowest class ID so the order
// does not depend on map iteration.
func mostDerivedLinearisation(language string, classes map[int64]int64, bases map[int64][]int64) ([]int64, bool) {
	var best []int64
	bestCovered, bestClass := -1, int64(0)
	for _, class := range sortedDistinct(classes) {
		result := LinearizeMRO(language, class, bases)
		if result.Status != MROStatusLinearized && result.Status != MROStatusNoBases {
			continue
		}
		covered := countCovered(result.Order, classes)
		if covered > bestCovered || (covered == bestCovered && class < bestClass) {
			best, bestCovered, bestClass = result.Order, covered, class
		}
	}
	return best, bestCovered > 0
}

func countCovered(order []int64, classes map[int64]int64) int {
	position := make(map[int64]bool, len(order))
	for _, id := range order {
		position[id] = true
	}
	covered := 0
	for _, class := range sortedDistinct(classes) {
		if position[class] {
			covered++
		}
	}
	return covered
}

// sortedDistinct returns the distinct defining classes in ascending ID order so
// every traversal over the map is deterministic.
func sortedDistinct(classes map[int64]int64) []int64 {
	seen := make(map[int64]bool, len(classes))
	var out []int64
	for _, class := range classes {
		if !seen[class] {
			seen[class] = true
			out = append(out, class)
		}
	}
	insertionSortIDs(out)
	return out
}

func insertionSortIDs(ids []int64) {
	for i := 1; i < len(ids); i++ {
		for j := i; j > 0 && ids[j] < ids[j-1]; j-- {
			ids[j], ids[j-1] = ids[j-1], ids[j]
		}
	}
}

// orderByLinearisation is a stable sort of candidates by the position of their
// defining class in the linearisation. Candidates whose class is absent from it
// keep their relative order and follow the ranked ones.
func orderByLinearisation(candidates []int64, classes map[int64]int64, linear []int64) ([]int64, int) {
	position := make(map[int64]int, len(linear))
	for index, class := range linear {
		position[class] = index
	}
	ranked := make([]int64, 0, len(candidates))
	unranked := make([]int64, 0, len(candidates))
	rankOf := make(map[int64]int, len(candidates))
	for _, id := range candidates {
		class, hasClass := classes[id]
		index, inMRO := position[class]
		if hasClass && inMRO {
			rankOf[id] = index
			ranked = append(ranked, id)
			continue
		}
		unranked = append(unranked, id)
	}
	for i := 1; i < len(ranked); i++ {
		for j := i; j > 0 && rankOf[ranked[j]] < rankOf[ranked[j-1]]; j-- {
			ranked[j], ranked[j-1] = ranked[j-1], ranked[j]
		}
	}
	return append(ranked, unranked...), len(ranked)
}

func sameOrder(a, b []int64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

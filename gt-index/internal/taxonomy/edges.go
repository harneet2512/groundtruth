// Package taxonomy derives the additive edge kinds of final_hardening item 11
// (delta row 9) from facts the parser already recorded syntactically.
//
// The discipline is the point. gnx's schema is one row, one target, so a
// resolver that sees three plausible targets must pick one at write time and
// the disagreement is lost. Here a name that matches several declarations
// produces several edges, each marked SPECULATIVE and each carrying the true
// size of the candidate set, so the over-approximation stays visible.
//
// And a syntactic mechanism proves only that the SOURCE TEXT names something.
// It never proves which declaration the name binds to — that is resolution,
// and this package does not do resolution. So the best tier reachable here is
// CANDIDATE (exactly one declaration in the repository carries the name), the
// fallback is SPECULATIVE, and CERTIFIED is not in the vocabulary at all.
package taxonomy

import (
	"encoding/json"
	"sort"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// typeLabels are the labels a type reference may bind to. A return type or an
// implemented interface names a TYPE, so a Function with the same name is not
// a candidate for it.
var typeLabels = map[string]bool{
	"Class": true, "Interface": true, "Struct": true, "Enum": true,
	"TypeAlias": true, "Trait": true, "Record": true, "Union": true,
	"Annotation": true, "Protocol": true,
}

// callableLabels are the labels a decorator may bind to.
var callableLabels = map[string]bool{
	"Function": true, "Method": true, "Class": true,
}

// methodLabels are the labels an override may bind to.
var methodLabels = map[string]bool{"Method": true}

// index maps a symbol name to the database ids of declarations with that name,
// restricted to a set of labels.
type index map[string][]int64

func buildIndex(nodes []*store.Node, ids []int64, labels map[string]bool) index {
	out := index{}
	for i, n := range nodes {
		if n == nil || i >= len(ids) || ids[i] <= 0 {
			continue
		}
		if !labels[n.Label] || n.Name == "" {
			continue
		}
		out[n.Name] = append(out[n.Name], ids[i])
	}
	for name := range out {
		sort.Slice(out[name], func(a, b int) bool { return out[name][a] < out[name][b] })
	}
	return out
}

// edgeKey deduplicates: one taxonomy edge per (source, target, kind).
type edgeKey struct {
	source, target int64
	kind           string
}

// builder accumulates rows and enforces the tier rule in one place, so no call
// site can invent a tier.
type builder struct {
	rows []*store.Edge
	seen map[edgeKey]bool
}

func newBuilder() *builder {
	return &builder{seen: map[edgeKey]bool{}}
}

// emit writes one edge per retained candidate. The tier is a function of the
// candidate-set size and nothing else:
//
//	exactly one declaration carries the name -> CANDIDATE
//	several do                               -> SPECULATIVE, all retained
//
// CandidateCount is always the TRUE size of the set, even when the retained
// list is capped, so a reader can tell a two-way ambiguity from a forty-way one.
func (b *builder) emit(kind, mechanism string, sourceID int64, targets []int64, file string, line int) {
	if sourceID <= 0 || len(targets) == 0 {
		return
	}
	total := len(targets)
	retained := targets
	truncated := false
	if total > specs.MaxTaxonomyCandidates {
		retained = targets[:specs.MaxTaxonomyCandidates]
		truncated = true
	}
	tier := specs.TierCandidate
	confidence := 0.9
	if total > 1 {
		tier = specs.TierSpeculative
		confidence = 1.0 / float64(total)
	}
	meta := map[string]any{
		"mechanism":       mechanism,
		"candidate_total": total,
		"retained":        len(retained),
	}
	if truncated {
		meta["abstention_reason"] = "candidate_set_truncated"
	}
	encoded, err := json.Marshal(meta)
	if err != nil {
		// A metadata blob that cannot be encoded must not silently become an
		// edge with no provenance.
		return
	}
	for _, target := range retained {
		if target <= 0 || target == sourceID {
			continue
		}
		key := edgeKey{source: sourceID, target: target, kind: kind}
		if b.seen[key] {
			continue
		}
		b.seen[key] = true
		b.rows = append(b.rows, &store.Edge{
			SourceID:           sourceID,
			TargetID:           target,
			Type:               kind,
			SourceFile:         file,
			SourceLine:         line,
			ResolutionMethod:   mechanism,
			Confidence:         confidence,
			Metadata:           string(encoded),
			TrustTier:          tier,
			CandidateCount:     total,
			EvidenceType:       "syntax",
			VerificationStatus: "unverified",
		})
	}
}

// DeriveEdges returns the taxonomy edges for one index run.
//
// nodes and ids are parallel: ids[i] is the database id assigned to nodes[i],
// or 0 when the node was not inserted. props carry NodeIdx values that index
// into the same slice.
//
// The function is pure: it reads nothing, writes nothing, and returns rows the
// caller inserts. That is what keeps the hook in main.go to a single call.
func DeriveEdges(nodes []*store.Node, ids []int64, props []parser.PropertyRef) []*store.Edge {
	if len(nodes) == 0 || len(ids) == 0 {
		return nil
	}
	types := buildIndex(nodes, ids, typeLabels)
	callables := buildIndex(nodes, ids, callableLabels)
	methods := buildIndex(nodes, ids, methodLabels)
	b := newBuilder()

	idOf := func(nodeIdx int) int64 {
		if nodeIdx < 0 || nodeIdx >= len(ids) || nodeIdx >= len(nodes) {
			return 0
		}
		return ids[nodeIdx]
	}
	nodeAt := func(nodeIdx int) *store.Node {
		if nodeIdx < 0 || nodeIdx >= len(nodes) {
			return nil
		}
		return nodes[nodeIdx]
	}

	for _, p := range props {
		node := nodeAt(p.NodeIdx)
		if node == nil {
			continue
		}
		id := idOf(p.NodeIdx)
		if id <= 0 {
			continue
		}
		switch p.Kind {
		case parser.PropImplementsType:
			name, mechanism := splitFactValue(p.Value)
			if name == "" || !isTaxonomyMechanism(specs.EdgeImplements, mechanism) {
				continue
			}
			b.emit(specs.EdgeImplements, mechanism, id, types[name], node.FilePath, p.Line)

		case parser.PropOverrideMarker:
			// The marker states that this declaration overrides a supertype
			// member. Which one is a resolution question; every method with the
			// same name is a candidate, and the set size says how uncertain
			// that is.
			b.emit(specs.EdgeOverrides, specs.MechOverrideMarker, id,
				withoutSelf(methods[node.Name], id), node.FilePath, p.Line)

		case propClassDecorator:
			// `@dataclass` / `@pytest.fixture(scope="module")` -> `dataclass`.
			name := decoratorName(p.Value)
			if name == "" {
				continue
			}
			// Direction: the decorator decorates the declaration, so the
			// decorator is the source.
			for _, decorator := range callables[name] {
				b.emit(specs.EdgeDecorates, specs.MechDecoratorApplied, decorator,
					[]int64{id}, node.FilePath, p.Line)
			}

		case propParam:
			name := paramTypeName(p.Value)
			if name == "" {
				continue
			}
			b.emit(specs.EdgeParamType, specs.MechParamAnnotation, id,
				types[name], node.FilePath, p.Line)
		}
	}

	// RETURNS_TYPE reads the declared return type the parser already stored on
	// the node, so it needs no new property row.
	for i, n := range nodes {
		if n == nil || n.ReturnType == "" {
			continue
		}
		id := idOf(i)
		if id <= 0 {
			continue
		}
		name := typeReferenceName(n.ReturnType)
		if name == "" {
			continue
		}
		b.emit(specs.EdgeReturnsType, specs.MechReturnAnnotation, id,
			types[name], n.FilePath, n.StartLine)
	}
	return b.rows
}

// Property row kinds this package reads that the parser already wrote before
// item 11. Named here so the dependency is explicit.
const (
	propClassDecorator = "class_decorator"
	propParam          = "param"
)

// splitFactValue splits a `name|mechanism` fact value.
func splitFactValue(value string) (name, mechanism string) {
	i := strings.LastIndexByte(value, '|')
	if i <= 0 || i == len(value)-1 {
		return "", ""
	}
	return value[:i], value[i+1:]
}

// isTaxonomyMechanism refuses a mechanism the vocabulary does not grant to the
// edge kind. A row with an unexpected mechanism is dropped rather than written
// with a tier it did not earn.
func isTaxonomyMechanism(kind, mechanism string) bool {
	for _, m := range specs.TaxonomyEdgeMechanisms[kind] {
		if m == mechanism {
			return true
		}
	}
	return false
}

// withoutSelf removes the source's own id from a candidate list, so a
// declaration never overrides itself.
func withoutSelf(candidates []int64, self int64) []int64 {
	out := make([]int64, 0, len(candidates))
	for _, c := range candidates {
		if c != self {
			out = append(out, c)
		}
	}
	return out
}

// decoratorName reduces a recorded decorator to the name it applies:
// `@dataclass` -> `dataclass`, `@pytest.fixture(scope="x")` -> `fixture`.
func decoratorName(value string) string {
	name := strings.TrimSpace(value)
	name = strings.TrimPrefix(name, "@")
	if i := strings.IndexAny(name, "(\n\r "); i >= 0 {
		name = name[:i]
	}
	return typeReferenceName(name)
}

// paramTypeName reads the declared type out of a `param` row. The parser
// writes `name:Type [required]`, `name:Type opt=default` or `name [required]`;
// only the annotated forms name a type.
func paramTypeName(value string) string {
	colon := strings.IndexByte(value, ':')
	if colon < 0 {
		return ""
	}
	rest := value[colon+1:]
	if i := strings.IndexAny(rest, " \t"); i >= 0 {
		rest = rest[:i]
	}
	return typeReferenceName(rest)
}

// typeReferenceName reduces a type reference to the declared name a symbol
// node would carry, and refuses anything that is not identifier-shaped rather
// than guessing. `Promise<Circle>` reduces to `Promise`: the outer constructor
// is the type actually named, and the argument is a separate reference this
// pass does not claim.
func typeReferenceName(raw string) string {
	name := strings.TrimSpace(raw)
	name = strings.TrimLeft(name, "*&")
	if i := strings.IndexAny(name, "<([{|&,"); i > 0 {
		name = name[:i]
	}
	for _, sep := range []string{"::", ".", "\\"} {
		if i := strings.LastIndex(name, sep); i >= 0 {
			name = name[i+len(sep):]
		}
	}
	name = strings.TrimSpace(name)
	if name == "" {
		return ""
	}
	for i := 0; i < len(name); i++ {
		c := name[i]
		if !(c == '_' || c == '$' || (c >= 'a' && c <= 'z') ||
			(c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) {
			return ""
		}
	}
	return name
}

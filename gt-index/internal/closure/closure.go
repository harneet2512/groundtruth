// Package closure computes a transitive-reachability sidecar over the graph's
// VERIFIED call edges and persists it to the `closure` table.
//
// C7 (RUNTIME_FUCKUPS.md RF-4). The closure lets impact/trace answer
// "what does X transitively reach (callees) / what transitively reaches X
// (callers)" with a single indexed SELECT instead of a live BFS at query
// time — and, critically, it answers it over edges the indexer is confident
// about, not over name_match guesses.
//
// RF-4 MANDATE: the closure is built over VERIFIED edges ONLY. An edge is
// included iff
//
//	resolution_method ∈ deterministic set (same_file, import, verified_unique,
//	                    type_flow, import_type, inherited, unique_method,
//	                    return_type, lsp_verified, lsp)
//	  AND confidence >= MinEdgeConfidence (0.7)
//
// (#B7) The previous OR-rule (conf >= 0.5 OR deterministic method) admitted
// 0.6 GUESSES — 2-candidate name_match, ambiguous same_file/import variants,
// receiver-unproven impl_method — into a sidecar documented as "over VERIFIED
// CALLS" (gt_gt §2.1). If we let name_match (or any sub-verified guess) in, a
// single bad 1-hop edge propagates transitively — a bad 1-hop becomes a bad
// 2-hop and 3-hop reach, amplifying graph noise instead of delivering
// trustworthy deep reach. name_match is categorically excluded regardless of
// confidence (name_match is never a deterministic fact); the 0.7 floor also
// keeps out the 0.6 ambiguous variants of the deterministic methods.
//
// Properties:
//   - In-memory BFS over an adjacency list built from the filtered edges.
//   - Depth-bounded (MaxDepth, default 3) so the closure stays compact and
//     never blows up combinatorially on dense hubs.
//   - Confidence-gated at EVERY hop: a closure row's min_confidence is the
//     minimum edge confidence along the discovered path (weakest link).
//   - Cycle-safe: a per-source visited set bounds each BFS to one visit per
//     reachable node.
//   - Deduped: the first (shortest) path to a target wins; we keep the best
//     (highest) min_confidence seen at that shortest depth.
//
// Zero new go.mod dependencies — stdlib + the existing store package.
package closure

import (
	"fmt"
	"sort"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// MaxDepth bounds transitive reach. RF-4: depth-bounded (<=3). Three hops is
// enough to capture indirect call relationships (caller → helper → target)
// that impact/trace care about, without the combinatorial blowup a deeper
// closure would cause on hub functions.
const MaxDepth = 3

// MinEdgeConfidence is the VERIFIED-edge floor (RF-4, #B7: raised 0.5 → 0.7).
// 0.5 admitted the 0.6 ambiguity tier (2-candidate name_match, ambiguous
// same_file/import picks, 1-class impl_method/unique_method) — guesses, not
// verified facts. 0.7 sits above every receiver-unproven score the resolver
// emits (max 0.6 — impl_method AND unique_method are both capped there) and
// below the weakest receiver-PROVEN type-derived score (return_type 0.85).
const MinEdgeConfidence = 0.7

// verifiedMethods is the set of resolution methods produced by the receiver-
// PROVEN deterministic resolver strategies (same_file/import/type-derived rungs)
// and the offline LSP promotion pass (C6: lsp / lsp_verified). It must agree
// edge-for-edge with the consumer fact set (curation_map.DETERMINISTIC_RESOLUTION_METHODS):
// reach may only walk what the brief renders as a fact, so a name-uniqueness
// guess can never enter the transitive closure.
//
// impl_method AND unique_method stay OUT — they are the SAME receiver-unproven
// class (global method-name uniqueness, ZERO receiver-type check; rung 1.98's
// own comment: "1.98 does not prove the receiver"). Previously unique_method was
// admitted here at 0.85, classified as "type-derived" — but it is name-derived,
// not type-derived, so it laundered cross-class collisions into reach while the
// fact set correctly excluded it (the reach-vs-fact asymmetry). The resolver now
// caps both at 0.6 (CANDIDATE), and both are excluded here. name_match is never
// a fact at any confidence.
var verifiedMethods = map[string]bool{
	"same_file":       true,
	"import":          true,
	"verified_unique": true,
	"type_flow":       true,
	"import_type":     true,
	"inherited":       true,
	"return_type":     true,
	"lsp_verified":    true,
	"lsp":             true,
}

// isVerifiedEdge implements the RF-4 admission rule. #B7: BOTH conditions are
// now required (deterministic method AND confidence >= floor) — the previous
// OR-rule let any conf>=0.5 edge in regardless of method, so 0.6 name_match
// guesses propagated transitively through a "verified-only" closure. The AND
// keeps ambiguity-demoted variants of deterministic methods (0.6 ambiguous
// import/same_file picks) out as well.
func isVerifiedEdge(e *store.Edge) bool {
	return verifiedMethods[e.ResolutionMethod] && e.Confidence >= MinEdgeConfidence
}

// reachKey is a (source, target) pair used to dedup discovered closure rows.
type reachKey struct {
	source int64
	target int64
}

// ComputeTransitiveClosure reads the DB's VERIFIED edges, computes the
// depth-bounded transitive closure via per-source BFS, persists the result to
// the `closure` table, and returns the number of closure rows written.
//
// edgeType selects which edge relation to close over ("CALLS" for the call
// graph). minConf is the per-repo confidence floor for the *initial* edge
// read; the RF-4 admission rule (deterministic method AND conf >= the
// MinEdgeConfidence floor) is still applied on top of it, so passing
// MinEdgeConfidence here is the conservative default.
func ComputeTransitiveClosure(db *store.DB, edgeType string, maxDepth int, minConf float64) (int, error) {
	if maxDepth <= 0 {
		maxDepth = MaxDepth
	}

	edges, err := db.GetAllEdges(minConf)
	if err != nil {
		return 0, fmt.Errorf("read edges for closure: %w", err)
	}

	// Build a forward adjacency list over VERIFIED edges only. We keep the
	// highest-confidence edge between any (source -> target) pair so a single
	// strong edge is not masked by a weaker parallel one.
	type adjEdge struct {
		target int64
		conf   float64
	}
	adj := make(map[int64][]adjEdge)
	// PASS 1: for every (source -> target) pair, record the MAX confidence across all
	// parallel edges. (Previously the max was tracked here but the adjacency entry's
	// conf was frozen at the FIRST-SCANNED edge's confidence — so a later, stronger
	// parallel edge updated bestEdgeConf but the BFS, which reads adj[].conf, still
	// propagated the weaker first-seen value, contradicting the "keep the highest-
	// confidence edge" invariant above. The adjacency conf must come FROM bestEdgeConf.)
	bestEdgeConf := make(map[reachKey]float64)
	for i := range edges {
		e := edges[i]
		if e.Type != edgeType {
			continue
		}
		if !isVerifiedEdge(e) {
			continue // RF-4: never propagate name_match false positives
		}
		if e.SourceID == 0 || e.TargetID == 0 || e.SourceID == e.TargetID {
			continue // skip unresolved endpoints and self-loops
		}
		k := reachKey{e.SourceID, e.TargetID}
		if prev, ok := bestEdgeConf[k]; !ok || e.Confidence > prev {
			bestEdgeConf[k] = e.Confidence
		}
	}
	// PASS 2: build the adjacency list ONCE per unique pair, reading the confidence
	// from bestEdgeConf so each edge carries the STRONGEST parallel confidence. Iterate
	// edges in their original (id-ordered) order and dedup pairs for deterministic
	// adjacency order.
	seenPair := make(map[reachKey]bool)
	for i := range edges {
		e := edges[i]
		if e.Type != edgeType || !isVerifiedEdge(e) {
			continue
		}
		if e.SourceID == 0 || e.TargetID == 0 || e.SourceID == e.TargetID {
			continue
		}
		k := reachKey{e.SourceID, e.TargetID}
		if seenPair[k] {
			continue
		}
		seenPair[k] = true
		adj[e.SourceID] = append(adj[e.SourceID], adjEdge{target: e.TargetID, conf: bestEdgeConf[k]})
	}

	// Per-source BFS. For each source node, walk the adjacency list up to
	// maxDepth hops. Track, for each reached target, the shortest depth and the
	// best (max) min-confidence-along-path at that shortest depth.
	type seen struct {
		depth   int
		minConf float64
	}
	rows := make([]*store.Closure, 0)
	seenRow := make(map[reachKey]int) // reachKey -> index into rows

	// bfsItem is a frontier entry: the node, the depth at which we reached it,
	// and the weakest edge confidence along the path that got us there.
	type bfsItem struct {
		node    int64
		depth   int
		pathMin float64
	}

	// Determinism: a Go map has randomized range order, so `for src := range adj`
	// would append closure rows (and thus assign closure-table AUTOINCREMENT ids) in
	// a different order on every build → a non-byte-deterministic graph.db. Each
	// source's BFS is fully independent (best/seenRow are per-source or (src,target)-
	// keyed), so the source order only affects row *insertion order* — sorting the
	// source ids makes that order, and the persisted table, byte-deterministic. The
	// inner neighbor iteration (adj[cur.node]) is already deterministic: PASS 2 builds
	// each adjacency slice in id-order.
	srcs := make([]int64, 0, len(adj))
	for src := range adj {
		srcs = append(srcs, src)
	}
	sort.Slice(srcs, func(i, j int) bool { return srcs[i] < srcs[j] })

	for _, src := range srcs {
		// best[node] = the best (depth, minConf) we have committed for this src.
		best := make(map[int64]seen)
		queue := []bfsItem{{node: src, depth: 0, pathMin: 1.0}}
		for len(queue) > 0 {
			cur := queue[0]
			queue = queue[1:]

			if cur.depth >= maxDepth {
				continue
			}
			for _, nbr := range adj[cur.node] {
				if nbr.target == src {
					continue // closure never includes the source reaching itself
				}
				hopMin := cur.pathMin
				if nbr.conf < hopMin {
					hopMin = nbr.conf
				}
				nextDepth := cur.depth + 1

				prev, ok := best[nbr.target]
				if ok {
					// Already reached. Only continue if this path is strictly
					// shorter, or same depth with higher confidence. This keeps
					// the BFS cycle-safe (a node is re-expanded at most when we
					// find a genuinely better path to it).
					if nextDepth > prev.depth {
						continue
					}
					if nextDepth == prev.depth && hopMin <= prev.minConf {
						continue
					}
				}
				best[nbr.target] = seen{depth: nextDepth, minConf: hopMin}

				k := reachKey{src, nbr.target}
				if idx, exists := seenRow[k]; exists {
					// Update the existing closure row if we found a better path.
					r := rows[idx]
					if nextDepth < r.Depth || (nextDepth == r.Depth && hopMin > r.MinConfidence) {
						r.Depth = nextDepth
						r.MinConfidence = hopMin
					}
				} else {
					seenRow[k] = len(rows)
					rows = append(rows, &store.Closure{
						SourceID:      src,
						TargetID:      nbr.target,
						Depth:         nextDepth,
						MinConfidence: hopMin,
					})
				}

				// Re-expand from this node at the new (better) depth.
				queue = append(queue, bfsItem{node: nbr.target, depth: nextDepth, pathMin: hopMin})
			}
		}
	}

	if err := db.BatchInsertClosure(rows); err != nil {
		return 0, fmt.Errorf("persist closure: %w", err)
	}
	return len(rows), nil
}

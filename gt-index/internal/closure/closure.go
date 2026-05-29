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
//	confidence >= MinEdgeConfidence (0.5)
//	  OR resolution_method ∈ {same_file, import, verified_unique,
//	                          type_flow, import_type, lsp_verified, lsp}
//
// If we let name_match (conf 0.2–0.4) edges in, a single bad 1-hop edge
// would propagate transitively — a bad 1-hop becomes a bad 2-hop and 3-hop
// reach, amplifying graph noise instead of delivering trustworthy deep reach.
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

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// MaxDepth bounds transitive reach. RF-4: depth-bounded (<=3). Three hops is
// enough to capture indirect call relationships (caller → helper → target)
// that impact/trace care about, without the combinatorial blowup a deeper
// closure would cause on hub functions.
const MaxDepth = 3

// MinEdgeConfidence is the VERIFIED-edge floor (RF-4). Matches the Python
// reader gate (graph.py / gt_intel MIN_CONFIDENCE = 0.5). Edges below this
// confidence are admitted to the closure only when their resolution_method
// is one of the deterministic methods below.
const MinEdgeConfidence = 0.5

// verifiedMethods is the set of resolution methods that are trustworthy
// regardless of the numeric confidence score. These are produced by the
// deterministic resolver strategies (same_file/import/...) and the offline
// LSP promotion pass (C6: lsp / lsp_verified). resolver.computeConfidence
// already assigns these >= 0.9, so the method check is belt-and-suspenders —
// it future-proofs the filter if a method's score is ever lowered.
var verifiedMethods = map[string]bool{
	"same_file":       true,
	"import":          true,
	"verified_unique": true,
	"type_flow":       true,
	"import_type":     true,
	"lsp_verified":    true,
	"lsp":             true,
}

// isVerifiedEdge implements the RF-4 admission rule: confidence >= floor OR a
// deterministic/LSP resolution method.
func isVerifiedEdge(e *store.Edge) bool {
	if e.Confidence >= MinEdgeConfidence {
		return true
	}
	return verifiedMethods[e.ResolutionMethod]
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
// read; the RF-4 verified-method override is still applied on top of it, so
// passing MinEdgeConfidence here is the conservative default.
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
		if prev, ok := bestEdgeConf[k]; ok {
			if e.Confidence > prev {
				bestEdgeConf[k] = e.Confidence
			}
			continue // adjacency already has this pair
		}
		bestEdgeConf[k] = e.Confidence
		adj[e.SourceID] = append(adj[e.SourceID], adjEdge{target: e.TargetID, conf: e.Confidence})
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

	for src := range adj {
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

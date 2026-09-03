package community

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
)

// TierCertified is the only trust tier admitted into the structural half of
// the clustered graph. The other three tiers the schema defines -- CANDIDATE,
// SPECULATIVE and STRUCTURAL -- are excluded, and their exclusion is the point:
// a community built over a resolver's guesses would silently inherit every
// resolution error the resolver made, and would then be read as if it were
// evidence.
const TierCertified = "CERTIFIED"

// EdgeTypeCalls is the edge relation clustered over.
const EdgeTypeCalls = "CALLS"

// Queryer is the read-only database surface this package needs. Both *sql.DB
// and *sql.Tx satisfy it. The interface is deliberately this narrow: it is the
// mechanical guarantee behind the "membership never changes an edge" invariant
// -- there is no Exec here, so no code path in this package can write to
// `edges` even by mistake.
type Queryer interface {
	QueryContext(ctx context.Context, query string, args ...interface{}) (*sql.Rows, error)
}

// selectCertifiedCallEdgesSQL reads the file endpoints of every CALLS edge,
// tagged with its tier, ordered by edge id so the read order -- and therefore
// every downstream tie-break -- is stable across runs.
//
// The tier is SELECTed rather than filtered in the WHERE clause for one
// reason: Result.ExcludedCallRows must be able to state how many rows the
// tier rule rejected. An exclusion nobody can size is indistinguishable from
// an exclusion that never fired. The rejection itself still happens here, in
// one place, before any weight is accumulated.
//
// Schema relied on (internal/store/sqlite.go, read-only):
//
//	edges(id, source_id, target_id, type, trust_tier)
//	nodes(id, file_path)
const selectCertifiedCallEdgesSQL = `
	SELECT e.id, e.trust_tier, sn.file_path, tn.file_path
	FROM edges e
	JOIN nodes sn ON sn.id = e.source_id
	JOIN nodes tn ON tn.id = e.target_id
	WHERE e.type = ?
	ORDER BY e.id`

// callEdge is one CERTIFIED CALLS edge projected to the file level. FileA is
// always lexicographically less than FileB, matching cochange.Pair's spelling
// so the two populations key identically.
type callEdge struct {
	ID    int64
	FileA string
	FileB string
}

// edgeStats sizes what the tier rule admitted and refused.
type edgeStats struct {
	// certified counts CERTIFIED rows that crossed a file boundary and
	// therefore contributed weight.
	certified int
	// excluded counts CALLS rows refused for their tier.
	excluded int
	// sameFile counts CERTIFIED rows whose endpoints share a file. They are
	// dropped: a file cannot be clustered away from itself, so an intra-file
	// call is a self-loop that changes no partition while inflating every
	// internal-weight figure that reads it.
	sameFile int
}

// loadCertifiedCallEdges reads the certified call graph, projected to files.
func loadCertifiedCallEdges(ctx context.Context, q Queryer) ([]callEdge, edgeStats, error) {
	var stats edgeStats
	if q == nil {
		return nil, stats, fmt.Errorf("community: nil queryer")
	}
	rows, err := q.QueryContext(ctx, selectCertifiedCallEdgesSQL, EdgeTypeCalls)
	if err != nil {
		return nil, stats, fmt.Errorf("community: read %s edges: %w", EdgeTypeCalls, err)
	}
	defer rows.Close()

	var out []callEdge
	for rows.Next() {
		var (
			id       int64
			tier     sql.NullString
			srcFile  sql.NullString
			destFile sql.NullString
		)
		if err := rows.Scan(&id, &tier, &srcFile, &destFile); err != nil {
			return nil, stats, fmt.Errorf("community: scan edge row: %w", err)
		}
		if tier.String != TierCertified {
			stats.excluded++
			continue
		}
		a, b := srcFile.String, destFile.String
		if a == "" || b == "" {
			// An endpoint with no file cannot be placed in a file community.
			// It is not an excluded tier, so it is not counted as one.
			continue
		}
		if a == b {
			stats.sameFile++
			continue
		}
		if a > b {
			a, b = b, a
		}
		stats.certified++
		out = append(out, callEdge{ID: id, FileA: a, FileB: b})
	}
	if err := rows.Err(); err != nil {
		return nil, stats, fmt.Errorf("community: iterate edge rows: %w", err)
	}
	return out, stats, nil
}

// arc is one weighted adjacency entry. Adjacency lists are kept sorted by to,
// which is what makes the local-moving sweep order reproducible.
type arc struct {
	to int
	w  float64
}

// wgraph is an undirected weighted graph with node sizes, the shape both the
// original file graph and every aggregated graph take. size is the number of
// original files a node stands for (1 before aggregation); selfLoop is the
// internal weight already collapsed into a node by aggregation.
type wgraph struct {
	n        int
	size     []float64
	selfLoop []float64
	adj      [][]arc
}

// pairKey is an ordered node-index pair, the single spelling of an undirected
// edge.
type pairKey struct {
	a int
	b int
}

// fileGraph is the clustered graph plus everything needed to explain it: the
// file behind each node, and the evidence behind each edge.
type fileGraph struct {
	wgraph
	// files[i] is node i's path. Sorted, so node indices are a deterministic
	// function of the path set alone -- not of database row order, not of map
	// iteration order.
	files []string
	index map[string]int
	// callEvidence[p] are the edges.id values behind pair p, ascending.
	callEvidence map[pairKey][]int64
	// cochangePairs is a set, since co-change support has no row id to cite.
	cochangeEvidence map[pairKey]bool
	// weight[p] is the total weight on pair p, retained for the structural
	// score so it is computed from the same numbers the clustering saw.
	weight map[pairKey]float64

	callPairs     int
	cochangePairs int
	edgeCount     int
}

// buildFileGraph assembles the trust-weighted multigraph:
//
//	w(a,b) = WCall * |CERTIFIED CALLS edges between a and b|
//	       + WCochange * cochange support(a, b)
//
// Two populations, two weights, one graph. Both are additive, so a pair
// supported by both signals outranks a pair supported by either -- which is
// the whole reason for combining them, since structural and evolutionary
// coupling are known not to be congruent.
//
// Nodes are the union of the files named by either population. A file with no
// edge at all never enters the graph, so it is never reported as a
// single-file community.
func buildFileGraph(calls []callEdge, pairs []cochange.Pair, opts Options) *fileGraph {
	g := &fileGraph{
		index:            make(map[string]int),
		callEvidence:     make(map[pairKey][]int64),
		cochangeEvidence: make(map[pairKey]bool),
		weight:           make(map[pairKey]float64),
	}

	// Pass 1: collect the file set, independently of any map ordering, then
	// sort it. Node indices come from the sorted paths.
	present := make(map[string]bool)
	for _, e := range calls {
		present[e.FileA] = true
		present[e.FileB] = true
	}
	for _, p := range pairs {
		if p.Support <= 0 {
			continue
		}
		present[p.FileA] = true
		present[p.FileB] = true
	}
	g.files = make([]string, 0, len(present))
	for f := range present {
		g.files = append(g.files, f)
	}
	sort.Strings(g.files)
	for i, f := range g.files {
		g.index[f] = i
	}

	// Pass 2: accumulate weight and evidence.
	callWeight := make(map[pairKey]float64)
	for _, e := range calls {
		k := g.key(e.FileA, e.FileB)
		callWeight[k] += opts.WCall
		g.callEvidence[k] = append(g.callEvidence[k], e.ID)
	}
	cochangeWeight := make(map[pairKey]float64)
	for _, p := range pairs {
		if p.Support <= 0 {
			continue
		}
		k := g.key(p.FileA, p.FileB)
		cochangeWeight[k] += opts.WCochange * float64(p.Support)
		g.cochangeEvidence[k] = true
	}
	g.callPairs = len(callWeight)
	g.cochangePairs = len(cochangeWeight)
	for k, w := range callWeight {
		g.weight[k] += w
	}
	for k, w := range cochangeWeight {
		g.weight[k] += w
	}
	g.edgeCount = len(g.weight)

	// Pass 3: adjacency. Built from the sorted pair list, not from map
	// iteration, so every list is in ascending neighbour order.
	g.n = len(g.files)
	g.size = make([]float64, g.n)
	g.selfLoop = make([]float64, g.n)
	g.adj = make([][]arc, g.n)
	for i := range g.size {
		g.size[i] = 1
	}
	keys := make([]pairKey, 0, len(g.weight))
	for k := range g.weight {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].a != keys[j].a {
			return keys[i].a < keys[j].a
		}
		return keys[i].b < keys[j].b
	})
	for _, k := range keys {
		w := g.weight[k]
		g.adj[k.a] = append(g.adj[k.a], arc{to: k.b, w: w})
		g.adj[k.b] = append(g.adj[k.b], arc{to: k.a, w: w})
	}
	for i := range g.adj {
		sort.Slice(g.adj[i], func(x, y int) bool { return g.adj[i][x].to < g.adj[i][y].to })
	}
	for k := range g.callEvidence {
		sort.Slice(g.callEvidence[k], func(i, j int) bool { return g.callEvidence[k][i] < g.callEvidence[k][j] })
	}
	return g
}

// key returns the canonical node-index pair for two file paths.
func (g *fileGraph) key(a, b string) pairKey {
	i, j := g.index[a], g.index[b]
	if i > j {
		i, j = j, i
	}
	return pairKey{a: i, b: j}
}

// materialize turns a node->community assignment into published Communities.
// It returns the surviving communities and the number dropped by MinMembers or
// MaxCommunities.
//
// A community over the MaxCommunities cap is DROPPED, never folded into a
// neighbour to make the cap fit: merging to satisfy a display budget would
// produce a community the objective never found and whose cohesion would then
// be measured against a group nothing predicted.
func (g *fileGraph) materialize(partition []int, opts Options) ([]Community, int) {
	groups := make(map[int][]int)
	for node, comm := range partition {
		groups[comm] = append(groups[comm], node)
	}

	ids := make([]int, 0, len(groups))
	for id := range groups {
		ids = append(ids, id)
	}
	sort.Ints(ids)

	dropped := 0
	built := make([]Community, 0, len(ids))
	for _, id := range ids {
		nodes := groups[id]
		sort.Ints(nodes)
		if len(nodes) < opts.MinMembers {
			dropped++
			continue
		}
		built = append(built, g.buildCommunity(nodes, partition, opts))
	}

	sortCommunities(built)
	if len(built) > opts.MaxCommunities {
		dropped += len(built) - opts.MaxCommunities
		built = built[:opts.MaxCommunities]
	}
	return built, dropped
}

// buildCommunity assembles one Community from its member node indices.
func (g *fileGraph) buildCommunity(nodes []int, partition []int, opts Options) Community {
	member := make(map[int]bool, len(nodes))
	for _, n := range nodes {
		member[n] = true
	}

	members := make([]string, 0, len(nodes))
	for _, n := range nodes {
		members = append(members, g.files[n])
	}
	sort.Strings(members)

	var internal, external float64
	internalPairs := make([]pairKey, 0)
	for _, n := range nodes {
		for _, a := range g.adj[n] {
			if member[a.to] {
				if n < a.to {
					internal += a.w
					internalPairs = append(internalPairs, pairKey{a: n, b: a.to})
				}
				continue
			}
			external += a.w
		}
	}
	sort.Slice(internalPairs, func(i, j int) bool {
		if internalPairs[i].a != internalPairs[j].a {
			return internalPairs[i].a < internalPairs[j].a
		}
		return internalPairs[i].b < internalPairs[j].b
	})

	evidence := make([]string, 0, len(internalPairs))
	truncated := false
	for _, p := range internalPairs {
		for _, eid := range g.callEvidence[p] {
			if len(evidence) >= opts.MaxEvidenceEdges {
				truncated = true
				break
			}
			evidence = append(evidence, strconv.FormatInt(eid, 10))
		}
		if truncated {
			break
		}
		if g.cochangeEvidence[p] {
			if len(evidence) >= opts.MaxEvidenceEdges {
				truncated = true
				break
			}
			evidence = append(evidence, "cochange:"+g.files[p.a]+"|"+g.files[p.b])
		}
	}

	label := heuristicLabel(members)
	c := Community{
		ID:                 communityID(members),
		Label:              label,
		HeuristicLabel:     label,
		Keywords:           keywords(members),
		EnrichedBy:         EnrichedByHeuristic,
		Cohesion:           nan(),
		CohesionReason:     CohesionNoHoldoutPairs,
		StructuralCohesion: ratio(internal, internal+external),
		Members:            members,
		EvidenceEdgeIDs:    evidence,
		EvidenceTruncated:  truncated,
		InternalWeight:     internal,
		ExternalWeight:     external,
	}
	return c
}

// communityID content-addresses a community by its members. Two runs that find
// the same group of files give it the same id, on any machine, without a
// counter that would renumber every community when one file moves.
func communityID(members []string) string {
	sum := sha256.Sum256([]byte(strings.Join(members, "\n")))
	return "community:" + hex.EncodeToString(sum[:])[:16]
}

// ratio guards the division so an empty community scores 0 rather than NaN --
// a NaN here would be indistinguishable from the deliberate NaN that marks an
// unmeasurable held-out cohesion, which is the one signal that must stay
// unambiguous.
func ratio(num, den float64) float64 {
	if den <= 0 {
		return 0
	}
	return num / den
}

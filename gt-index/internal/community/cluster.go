package community

import "sort"

// This file implements the clustering. Read this comment before reading the
// code; it states exactly what was built and what was not.
//
// # THE OBJECTIVE: CPM, NOT MODULARITY
//
// The quality function maximised is the Constant Potts Model:
//
//	H = SUM over communities c of [ e_c - gamma * n_c*(n_c-1)/2 ]
//
// where e_c is the total internal edge weight of c and n_c is c's total node
// size (the number of original files it stands for, which is 1 per node before
// aggregation). Modularity is deliberately NOT used: its null model makes the
// effective resolution depend on the total edge weight of the graph, which is
// the resolution limit -- past a certain graph size, a genuinely separate
// small module cannot be expressed as its own community at any parameter
// setting, because merging it with a neighbour always scores higher. CPM has
// no null model. gamma is a flat density threshold: a community is worth
// forming exactly when its internal weight exceeds gamma per possible internal
// pair, whatever the rest of the graph looks like. That makes the parameter a
// stated choice rather than an emergent property of graph size, which is the
// only way a resolution SWEEP is meaningful -- and the sweep is what
// TestResolutionSensitivity exercises.
//
// # THE ALGORITHM: DETERMINISTIC LEIDEN-STYLE, STATED PRECISELY
//
// What is built is the Leiden three-phase loop -- local moving, REFINEMENT,
// aggregation over the refined partition -- with every random choice replaced
// by a deterministic one:
//
//   - localMove sweeps nodes in ascending index order (indices come from the
//     sorted file paths, so they do not depend on database row order or on Go
//     map iteration), and picks the strictly-best move, breaking ties towards
//     staying put and then towards the smallest community id.
//   - refine restarts every coarse community as singletons and merges a node
//     into a sub-community only when the CPM gain is strictly positive, which
//     requires an actual edge into that sub-community. Only singletons move,
//     so every sub-community is grown one adjacent node at a time and is
//     therefore CONNECTED by induction.
//   - aggregate collapses the REFINED partition (not the coarse one -- that is
//     the distinction between Leiden and Louvain) and carries the coarse
//     partition over as the next level's starting assignment.
//
// What is NOT built: the randomised, Boltzmann-weighted merge selection of the
// published Leiden refinement. Leiden samples a merge target with probability
// proportional to exp(gain/theta) to escape local optima; this takes the
// argmax instead. That trades a little partition quality on adversarial graphs
// for the property this item actually requires -- run it twice, get the same
// answer, byte for byte -- and TestDeterminismAcrossRuns holds it to that.
// Calling it "Leiden" without this paragraph would be a claim the code does
// not support, which is why Algorithm is stored on every row as
// "cpm_leiden_deterministic_v1" rather than "leiden".
//
// # THE CONNECTIVITY GUARANTEE
//
// Refinement makes sub-communities connected, but the coarse partition carried
// across aggregation levels is not itself refined, so the final assignment is
// checked and, if necessary, repaired: splitConnected splits every community
// into its connected components in the ORIGINAL file graph.
//
// That repair is free, not a compromise. Splitting a disconnected community
// into components leaves e_c unchanged -- by definition there is no weight
// between the components -- and reduces the penalty term by
// gamma*n_1*n_2 > 0. CPM quality therefore STRICTLY INCREASES on every split.
// Enforcing connectivity cannot make the objective worse, so the guarantee
// costs nothing and TestCommunitiesAreConnected can assert it unconditionally.

// epsilon is the strict-improvement floor for a move. Float comparison at
// exact equality would let two equal-scoring moves oscillate forever; equality
// must resolve to "stay", not "swap".
const epsilon = 1e-12

// maxSweeps bounds one local-moving phase. The phase normally exits when a
// full sweep produces no move; this is the backstop.
const maxSweeps = 100

// cluster partitions g and returns, for each original node, its community id.
// Ids are renumbered by smallest member index, so they are a deterministic
// function of the partition rather than of the order it was discovered in.
func cluster(g *fileGraph, opts Options) []int {
	gamma := opts.Resolution

	cur := g.wgraph
	// nodeOf maps an ORIGINAL node to its node in the current aggregated
	// graph. It is how the final assignment is pulled back down to files.
	nodeOf := make([]int, g.n)
	for i := range nodeOf {
		nodeOf[i] = i
	}
	part := identity(cur.n)

	for it := 0; it < opts.MaxIterations; it++ {
		part = localMove(cur, part, gamma)
		refined := refine(cur, part, gamma)

		agg, mapping := aggregate(cur, refined)
		if agg.n >= cur.n {
			// The refinement merged nothing, so aggregating would rebuild the
			// same graph and the loop would spin. Stop at this level.
			break
		}

		// Carry the COARSE partition up as the next level's starting point.
		// Every current node that maps to the same aggregate node belongs to
		// the same refined sub-community, and sub-communities never straddle
		// coarse communities, so this assignment is well defined.
		next := make([]int, agg.n)
		for v := 0; v < cur.n; v++ {
			next[mapping[v]] = part[v]
		}
		for i := range nodeOf {
			nodeOf[i] = mapping[nodeOf[i]]
		}
		cur = agg
		part = next
	}
	// One final sweep at the top level: the carried partition may still hold a
	// profitable move that the previous level could not express.
	part = localMove(cur, part, gamma)

	out := make([]int, g.n)
	for i := range out {
		out[i] = part[nodeOf[i]]
	}
	return splitConnected(g, out)
}

// identity assigns every node its own community.
func identity(n int) []int {
	p := make([]int, n)
	for i := range p {
		p[i] = i
	}
	return p
}

// communitySizes totals node size per community.
func communitySizes(g wgraph, part []int) map[int]float64 {
	sizes := make(map[int]float64, len(part))
	for v, c := range part {
		sizes[c] += g.size[v]
	}
	return sizes
}

// localMove runs CPM local moving to convergence and returns the improved
// partition. It does not mutate part.
func localMove(g wgraph, part []int, gamma float64) []int {
	cur := make([]int, len(part))
	copy(cur, part)
	sizes := communitySizes(g, cur)

	next := 0
	for _, c := range cur {
		if c >= next {
			next = c + 1
		}
	}

	for sweep := 0; sweep < maxSweeps; sweep++ {
		moved := false
		for v := 0; v < g.n; v++ {
			from := cur[v]
			// Weight from v into each neighbouring community.
			into := make(map[int]float64, len(g.adj[v]))
			for _, a := range g.adj[v] {
				into[cur[a.to]] += a.w
			}

			// Value of v's current placement: the weight it contributes to
			// its community minus the CPM penalty for occupying it.
			stayValue := into[from] - gamma*g.size[v]*(sizes[from]-g.size[v])

			// Candidate communities, in ascending id order so the scan is
			// reproducible. Moving to a brand new (empty) community always
			// scores exactly 0 and is always available; it is what lets a node
			// leave a group that no longer earns it.
			cands := make([]int, 0, len(into))
			for c := range into {
				if c != from {
					cands = append(cands, c)
				}
			}
			sort.Ints(cands)

			bestComm := from
			bestValue := stayValue
			if 0 > bestValue+epsilon {
				bestComm = -1 // isolate
				bestValue = 0
			}
			for _, c := range cands {
				value := into[c] - gamma*g.size[v]*sizes[c]
				if value > bestValue+epsilon {
					bestComm = c
					bestValue = value
				}
			}
			if bestComm == from {
				continue
			}
			if bestComm == -1 {
				bestComm = next
				next++
			}
			sizes[from] -= g.size[v]
			if sizes[from] <= 0 {
				delete(sizes, from)
			}
			sizes[bestComm] += g.size[v]
			cur[v] = bestComm
			moved = true
		}
		if !moved {
			break
		}
	}
	return cur
}

// refine splits every coarse community into connected sub-communities.
//
// Each node starts alone. A node is merged into a sub-community only if it is
// still alone AND the CPM gain of joining is strictly positive, which requires
// non-zero weight between them -- so a sub-community only ever grows by
// absorbing a node adjacent to it, and is connected by construction.
func refine(g wgraph, coarse []int, gamma float64) []int {
	sub := identity(g.n)
	sizes := make(map[int]float64, g.n)
	for v := 0; v < g.n; v++ {
		sizes[v] = g.size[v]
	}
	// singleton[c] tracks whether sub-community c is still a lone node, which
	// is the condition for it being allowed to move.
	singleton := make([]bool, g.n)
	for i := range singleton {
		singleton[i] = true
	}

	for v := 0; v < g.n; v++ {
		if !singleton[sub[v]] {
			continue
		}
		into := make(map[int]float64, len(g.adj[v]))
		for _, a := range g.adj[v] {
			if coarse[a.to] != coarse[v] {
				continue // refinement never crosses a coarse community
			}
			into[sub[a.to]] += a.w
		}
		cands := make([]int, 0, len(into))
		for c := range into {
			if c != sub[v] {
				cands = append(cands, c)
			}
		}
		sort.Ints(cands)

		bestComm := sub[v]
		bestGain := 0.0
		for _, c := range cands {
			gain := into[c] - gamma*g.size[v]*sizes[c]
			if gain > bestGain+epsilon {
				bestComm = c
				bestGain = gain
			}
		}
		if bestComm == sub[v] {
			continue
		}
		from := sub[v]
		sizes[from] -= g.size[v]
		if sizes[from] <= 0 {
			delete(sizes, from)
		}
		sizes[bestComm] += g.size[v]
		sub[v] = bestComm
		singleton[bestComm] = false
		singleton[from] = false
	}
	return sub
}

// aggregate collapses part into a graph whose nodes are its communities. It
// returns the aggregated graph and, for each node of g, the aggregate node it
// landed in.
func aggregate(g wgraph, part []int) (wgraph, []int) {
	// Renumber communities by their smallest member index so aggregate node
	// ids are a function of the partition, not of discovery order.
	first := make(map[int]int)
	for v := 0; v < g.n; v++ {
		if _, ok := first[part[v]]; !ok {
			first[part[v]] = v
		}
	}
	order := make([]int, 0, len(first))
	for c := range first {
		order = append(order, c)
	}
	sort.Slice(order, func(i, j int) bool { return first[order[i]] < first[order[j]] })
	id := make(map[int]int, len(order))
	for i, c := range order {
		id[c] = i
	}

	mapping := make([]int, g.n)
	for v := 0; v < g.n; v++ {
		mapping[v] = id[part[v]]
	}

	out := wgraph{
		n:        len(order),
		size:     make([]float64, len(order)),
		selfLoop: make([]float64, len(order)),
		adj:      make([][]arc, len(order)),
	}
	for v := 0; v < g.n; v++ {
		out.size[mapping[v]] += g.size[v]
		out.selfLoop[mapping[v]] += g.selfLoop[v]
	}
	between := make(map[pairKey]float64)
	for v := 0; v < g.n; v++ {
		for _, a := range g.adj[v] {
			if a.to <= v {
				continue // count each undirected edge once
			}
			cv, cw := mapping[v], mapping[a.to]
			if cv == cw {
				out.selfLoop[cv] += a.w
				continue
			}
			if cv > cw {
				cv, cw = cw, cv
			}
			between[pairKey{a: cv, b: cw}] += a.w
		}
	}
	keys := make([]pairKey, 0, len(between))
	for k := range between {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].a != keys[j].a {
			return keys[i].a < keys[j].a
		}
		return keys[i].b < keys[j].b
	})
	for _, k := range keys {
		w := between[k]
		out.adj[k.a] = append(out.adj[k.a], arc{to: k.b, w: w})
		out.adj[k.b] = append(out.adj[k.b], arc{to: k.a, w: w})
	}
	for i := range out.adj {
		sort.Slice(out.adj[i], func(x, y int) bool { return out.adj[i][x].to < out.adj[i][y].to })
	}
	return out, mapping
}

// splitConnected splits every community into its connected components in the
// original graph and renumbers the result by smallest member index.
//
// See the file header: under CPM this is never a downgrade. A disconnected
// community has zero weight between its parts, so splitting keeps e_c and
// removes gamma*n_1*n_2 of penalty -- quality strictly increases.
func splitConnected(g *fileGraph, part []int) []int {
	members := make(map[int][]int)
	for v, c := range part {
		members[c] = append(members[c], v)
	}
	comms := make([]int, 0, len(members))
	for c := range members {
		comms = append(comms, c)
	}
	sort.Ints(comms)

	out := make([]int, len(part))
	for i := range out {
		out[i] = -1
	}
	nextID := 0
	for _, c := range comms {
		nodes := members[c]
		sort.Ints(nodes)
		inComm := make(map[int]bool, len(nodes))
		for _, v := range nodes {
			inComm[v] = true
		}
		for _, seed := range nodes {
			if out[seed] != -1 {
				continue
			}
			// Breadth-first over the induced subgraph. Ascending seed and
			// ascending adjacency order make the component ids deterministic.
			id := nextID
			nextID++
			out[seed] = id
			queue := []int{seed}
			for len(queue) > 0 {
				v := queue[0]
				queue = queue[1:]
				for _, a := range g.adj[v] {
					if !inComm[a.to] || out[a.to] != -1 {
						continue
					}
					out[a.to] = id
					queue = append(queue, a.to)
				}
			}
		}
	}
	return out
}

// quality returns the CPM objective value of part on g. It is used by the
// tests to assert that the connectivity repair never lowers the objective,
// and by the probe to report what a resolution setting actually bought.
func quality(g wgraph, part []int, gamma float64) float64 {
	internal := make(map[int]float64)
	sizes := make(map[int]float64)
	for v := 0; v < g.n; v++ {
		sizes[part[v]] += g.size[v]
		internal[part[v]] += g.selfLoop[v]
		for _, a := range g.adj[v] {
			if a.to > v && part[a.to] == part[v] {
				internal[part[v]] += a.w
			}
		}
	}
	total := 0.0
	for c, e := range internal {
		n := sizes[c]
		total += e - gamma*n*(n-1)/2
	}
	for c, n := range sizes {
		if _, ok := internal[c]; !ok {
			total -= gamma * n * (n - 1) / 2
		}
	}
	return total
}

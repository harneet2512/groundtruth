package community

import (
	"context"
	"fmt"
	"sort"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
)

// synth builds a fileGraph directly from named certified call pairs and
// co-change pairs, so the clustering can be tested without a database or a git
// repository standing in the way of the assertion.
//
// calls is a list of {fileA, fileB, count} triples; cochanges is a list of
// {fileA, fileB, support}.
func synth(calls [][3]interface{}, cochanges [][3]interface{}, opts Options) *fileGraph {
	var edges []callEdge
	var id int64
	for _, c := range calls {
		a, b, n := c[0].(string), c[1].(string), c[2].(int)
		if a > b {
			a, b = b, a
		}
		for i := 0; i < n; i++ {
			id++
			edges = append(edges, callEdge{ID: id, FileA: a, FileB: b})
		}
	}
	var pairs []cochange.Pair
	for _, c := range cochanges {
		a, b, n := c[0].(string), c[1].(string), c[2].(int)
		if a > b {
			a, b = b, a
		}
		pairs = append(pairs, cochange.Pair{FileA: a, FileB: b, Support: n})
	}
	return buildFileGraph(edges, pairs, opts.Normalize())
}

// partitionSets renders a partition as sorted, comparable member lists.
func partitionSets(g *fileGraph, part []int) []string {
	groups := map[int][]string{}
	for node, comm := range part {
		groups[comm] = append(groups[comm], g.files[node])
	}
	out := make([]string, 0, len(groups))
	for _, files := range groups {
		sort.Strings(files)
		out = append(out, fmt.Sprint(files))
	}
	sort.Strings(out)
	return out
}

// isConnected reports whether the nodes of one community induce a connected
// subgraph of g. This is the property Louvain does not guarantee and the
// refinement phase plus splitConnected exist to provide.
func isConnected(g *fileGraph, nodes []int) bool {
	if len(nodes) <= 1 {
		return true
	}
	in := make(map[int]bool, len(nodes))
	for _, n := range nodes {
		in[n] = true
	}
	seen := map[int]bool{nodes[0]: true}
	queue := []int{nodes[0]}
	for len(queue) > 0 {
		v := queue[0]
		queue = queue[1:]
		for _, a := range g.adj[v] {
			if in[a.to] && !seen[a.to] {
				seen[a.to] = true
				queue = append(queue, a.to)
			}
		}
	}
	return len(seen) == len(nodes)
}

// hierarchicalGraph builds four tight 4-file groups, pairwise joined by weak
// links, so that the partition genuinely depends on the resolution: at a low
// gamma the weak links are worth keeping and the groups merge into two
// super-groups; at a high gamma they are not and the four groups stand alone.
//
// This is the structure a modularity objective handles badly -- exactly the
// resolution limit -- and it is why CPM's explicit gamma is the point.
func hierarchicalGraph(opts Options) *fileGraph {
	var calls [][3]interface{}
	groups := [][]string{
		{"g0/a.go", "g0/b.go", "g0/c.go", "g0/d.go"},
		{"g1/a.go", "g1/b.go", "g1/c.go", "g1/d.go"},
		{"g2/a.go", "g2/b.go", "g2/c.go", "g2/d.go"},
		{"g3/a.go", "g3/b.go", "g3/c.go", "g3/d.go"},
	}
	for _, g := range groups {
		for i := 0; i < len(g); i++ {
			for j := i + 1; j < len(g); j++ {
				calls = append(calls, [3]interface{}{g[i], g[j], 10})
			}
		}
	}
	// Weak inter-group links: g0<->g1 and g2<->g3.
	calls = append(calls, [3]interface{}{"g0/a.go", "g1/a.go", 2})
	calls = append(calls, [3]interface{}{"g0/b.go", "g1/b.go", 2})
	calls = append(calls, [3]interface{}{"g2/a.go", "g3/a.go", 2})
	calls = append(calls, [3]interface{}{"g2/b.go", "g3/b.go", 2})
	return synth(calls, nil, opts)
}

// TestResolutionSensitivity is the property that distinguishes CPM from
// modularity: gamma is a real knob, and raising it must not lower the number
// of communities found.
func TestResolutionSensitivity(t *testing.T) {
	gammas := []float64{0.05, 0.5, 2.0, 8.0}
	counts := make([]int, len(gammas))
	for i, gamma := range gammas {
		opts := Options{Resolution: gamma}.Normalize()
		g := hierarchicalGraph(opts)
		part := cluster(g, opts)
		sets := partitionSets(g, part)
		counts[i] = len(sets)
		t.Logf("gamma=%-5g communities=%d %v", gamma, len(sets), sets)
	}
	for i := 1; i < len(counts); i++ {
		if counts[i] < counts[i-1] {
			t.Errorf("gamma %g gave %d communities but gamma %g gave %d; raising the resolution must not merge communities",
				gammas[i], counts[i], gammas[i-1], counts[i-1])
		}
	}
	if counts[0] >= counts[len(counts)-1] {
		t.Errorf("resolution had no effect: %v across %v -- gamma is not a knob", counts, gammas)
	}
	// The structure is designed so that a low gamma sees two super-groups and
	// a high gamma sees the four real groups. Anything less is a resolution
	// limit by another name.
	if counts[0] > 2 {
		t.Errorf("gamma=%g found %d communities; the weak links should have merged the pairs", gammas[0], counts[0])
	}
	if counts[2] < 4 {
		t.Errorf("gamma=%g found only %d communities; the four tight groups should be separable", gammas[2], counts[2])
	}
}

// TestCommunitiesAreInternallyConnected asserts the guarantee Louvain does not
// give, over a graph deliberately built to make disconnection plausible: many
// star-shaped groups sharing a single hub file.
func TestCommunitiesAreInternallyConnected(t *testing.T) {
	var calls [][3]interface{}
	for i := 0; i < 12; i++ {
		hub := fmt.Sprintf("pkg%d/hub.go", i)
		for j := 0; j < 5; j++ {
			calls = append(calls, [3]interface{}{hub, fmt.Sprintf("pkg%d/leaf%d.go", i, j), 4})
		}
		// Every hub also touches one shared utility, which is the classic way
		// a naive local-moving pass ends up with a community whose members
		// only reach each other through a node in a different community.
		calls = append(calls, [3]interface{}{hub, "shared/util.go", 3})
	}
	for _, gamma := range []float64{0.01, 0.1, 0.5, 1, 3, 10} {
		opts := Options{Resolution: gamma}.Normalize()
		g := synth(calls, nil, opts)
		part := cluster(g, opts)
		groups := map[int][]int{}
		for node, comm := range part {
			groups[comm] = append(groups[comm], node)
		}
		for comm, nodes := range groups {
			if !isConnected(g, nodes) {
				names := make([]string, 0, len(nodes))
				for _, n := range nodes {
					names = append(names, g.files[n])
				}
				sort.Strings(names)
				t.Errorf("gamma=%g: community %d is internally DISCONNECTED: %v", gamma, comm, names)
			}
		}
	}
}

// TestConnectivityRepairNeverLowersQuality is the argument in the file header,
// held to the code: splitting a disconnected community into its components
// cannot reduce CPM quality, so the guarantee is free.
func TestConnectivityRepairNeverLowersQuality(t *testing.T) {
	opts := Options{Resolution: 0.3}.Normalize()
	g := synth([][3]interface{}{
		{"a/1.go", "a/2.go", 5},
		{"b/1.go", "b/2.go", 5},
	}, nil, opts)
	// Force a deliberately disconnected community: everything in one group,
	// even though the two pairs share no edge.
	forced := make([]int, g.n)
	before := quality(g.wgraph, forced, opts.Resolution)
	after := quality(g.wgraph, splitConnected(g, forced), opts.Resolution)
	if !(after > before) {
		t.Errorf("splitting a disconnected community changed quality %v -> %v; it must strictly increase", before, after)
	}
}

// TestDeterminismAcrossRuns: same input, same answer, every time. This is what
// the deterministic argmax refinement buys, and what the published Leiden's
// randomised merge selection would cost.
func TestDeterminismAcrossRuns(t *testing.T) {
	opts := Options{Resolution: 0.4}.Normalize()
	first := ""
	for run := 0; run < 5; run++ {
		g := hierarchicalGraph(opts)
		got := fmt.Sprint(partitionSets(g, cluster(g, opts)))
		if run == 0 {
			first = got
			continue
		}
		if got != first {
			t.Fatalf("run %d differed:\n first: %s\n  this: %s", run, first, got)
		}
	}
}

// TestDeterminismEndToEnd repeats the check through Build, including the
// database read and the ID derivation, since a nondeterministic map iteration
// anywhere in the pipeline would show up here and not in the pure test above.
func TestDeterminismEndToEnd(t *testing.T) {
	db := graphDB(t)
	twoClusterGraph(t, db, "CANDIDATE", 4)
	dir := holdoutRepo(t)

	var first string
	for run := 0; run < 3; run++ {
		res, err := Build(context.Background(), db, dir, Options{HoldoutCommits: 2})
		if err != nil {
			t.Fatalf("run %d: %v", run, err)
		}
		got := ""
		for _, c := range res.Communities {
			got += fmt.Sprintf("%s|%s|%v|%v|%v|%v\n",
				c.ID, c.HeuristicLabel, c.Members, c.Keywords, c.EvidenceEdgeIDs, c.Cohesion)
		}
		if run == 0 {
			first = got
			continue
		}
		if got != first {
			t.Fatalf("run %d differed:\n first:\n%s\n  this:\n%s", run, first, got)
		}
	}
}

// TestCochangeAloneCanFormACommunity: the co-change half must be able to carry
// a community on its own, since that is the signal a snapshot-shaped store
// cannot hold and the whole reason for the second weight.
func TestCochangeAloneCanFormACommunity(t *testing.T) {
	opts := Options{}.Normalize()
	g := synth(nil, [][3]interface{}{
		{"docs/x.md", "docs/y.md", 9},
	}, opts)
	part := cluster(g, opts)
	got := partitionSets(g, part)
	if len(got) != 1 {
		t.Fatalf("co-change-only graph gave %v, want a single community", got)
	}
	if g.cochangePairs != 1 || g.callPairs != 0 {
		t.Errorf("graph composition = %d call pairs / %d cochange pairs, want 0 / 1", g.callPairs, g.cochangePairs)
	}
}

// TestWeightsAreIndependentlyTunable: WCall and WCochange must be able to
// override each other, or "trust-weighted" is decoration.
func TestWeightsAreIndependentlyTunable(t *testing.T) {
	calls := [][3]interface{}{
		{"a.go", "b.go", 4},
		{"c.go", "d.go", 4},
	}
	cochanges := [][3]interface{}{
		{"b.go", "c.go", 4},
	}
	// Co-change discounted to nothing: the call structure decides, so the
	// bridge b-c cannot hold a-b-c-d together.
	quiet := Options{WCochange: 0.001, Resolution: 1.0}.Normalize()
	gq := synth(calls, cochanges, quiet)
	if got := partitionSets(gq, cluster(gq, quiet)); len(got) != 2 {
		t.Errorf("with co-change discounted, got %v, want two communities", got)
	}
	// Co-change amplified: the historical bridge is now the strongest evidence
	// in the graph and is allowed to pull the two halves together.
	loud := Options{WCochange: 50, Resolution: 1.0}.Normalize()
	gl := synth(calls, cochanges, loud)
	if got := partitionSets(gl, cluster(gl, loud)); len(got) != 1 {
		t.Errorf("with co-change amplified, got %v, want one community", got)
	}
}

// TestMinMembersDropsRatherThanMerges: a group below the floor is dropped and
// counted, never folded into a neighbour to make the output look tidier.
func TestMinMembersDropsRatherThanMerges(t *testing.T) {
	db := graphDB(t)
	a1 := nodeIn(t, db, "src/a1.go", "a1")
	a2 := nodeIn(t, db, "src/a2.go", "a2")
	a3 := nodeIn(t, db, "src/a3.go", "a3")
	b1 := nodeIn(t, db, "lib/b1.go", "b1")
	b2 := nodeIn(t, db, "lib/b2.go", "b2")
	for i := 0; i < 6; i++ {
		edgeBetween(t, db, a1, a2, TierCertified)
		edgeBetween(t, db, a1, a3, TierCertified)
		edgeBetween(t, db, a2, a3, TierCertified)
	}
	for i := 0; i < 6; i++ {
		edgeBetween(t, db, b1, b2, TierCertified)
	}

	res, err := Build(context.Background(), db, nonRepo(t), Options{MinMembers: 3})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if len(res.Communities) != 1 {
		t.Fatalf("got %d communities, want 1:\n%s", len(res.Communities), dump(res))
	}
	if len(res.Communities[0].Members) != 3 {
		t.Errorf("survivor = %v, want the three-file community", res.Communities[0].Members)
	}
	if res.CommunitiesDropped != 1 {
		t.Errorf("dropped = %d, want 1 -- the two-file group must be counted, not silently gone", res.CommunitiesDropped)
	}
}

// TestMaxCommunitiesCapsAndCounts: the display cap drops, and says how many.
func TestMaxCommunitiesCapsAndCounts(t *testing.T) {
	var calls [][3]interface{}
	for i := 0; i < 6; i++ {
		calls = append(calls, [3]interface{}{
			fmt.Sprintf("p%d/a.go", i), fmt.Sprintf("p%d/b.go", i), 8,
		})
	}
	opts := Options{MaxCommunities: 2, Resolution: 1.0}.Normalize()
	g := synth(calls, nil, opts)
	built, dropped := g.materialize(cluster(g, opts), opts)
	if len(built) != 2 {
		t.Errorf("got %d communities, want the cap of 2", len(built))
	}
	if dropped != 4 {
		t.Errorf("dropped = %d, want 4", dropped)
	}
}

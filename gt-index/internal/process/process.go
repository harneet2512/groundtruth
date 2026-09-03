// Package process derives test-witnessed interprocedural slices ("processes")
// from a published GroundTruth graph.
//
// final_hardening item 8 (delta row 7). A "process" is an interprocedural
// slice: a path from an entry point to a terminal. The reference
// implementation guesses both endpoints heuristically. GT does not have to
// guess: it already stores `assertions`, which link a test symbol to the target
// symbol that test exercises. So a GT process is
//
//	a CERTIFIED call path from an assertion's target (the entry point)
//	to a terminal, together with the assertion that witnesses it.
//
// That makes a flow executable-verified rather than inferred. The invariant is
// absolute and is enforced twice — here, by never constructing a Process
// without a witness, and in the schema, by `processes.witness_assertion_id
// INTEGER NOT NULL`. A path with no witnessing assertion is NEVER emitted;
// that is the entire point of the item.
//
// # Why CERTIFIED CALLS edges and not the closure sidecar
//
// The plan text suggests walking the `closure` sidecar. The real schema
// (internal/store/sqlite.go) makes that impossible:
//
//	CREATE TABLE IF NOT EXISTS closure (
//	        source_id INTEGER,
//	        target_id INTEGER,
//	        depth INTEGER,
//	        min_confidence REAL,
//	        PRIMARY KEY(source_id, target_id, depth)
//	);
//
// A closure row records *that* source reaches target in `depth` hops. It does
// not record *how* — there is no intermediate-node column and no path column.
// A process is by definition an ordered path, so the closure cannot produce
// one. We therefore walk `edges` of type CALLS directly, and reproduce the
// closure's discipline (bounded depth, confidence floor, cycle safety) on top
// of a strictly stricter admission rule: trust_tier = 'CERTIFIED'. The closure
// admits an edge on resolution_method in a deterministic set AND confidence >=
// 0.7; we additionally require the stored tier to be CERTIFIED, so a process
// can never be built out of anything the store itself did not certify.
//
// # Bounds
//
// Every Options field is bounded by construction and 0 means "use the default"
// — never "unbounded". An interprocedural slice over a call graph is a path
// enumeration problem, which is exponential in depth on hub functions; an
// unbounded option here is not a configuration choice, it is a way to hang the
// indexer on a dense graph. See Options for the per-field rationale.
package process

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

const (
	// DefaultMaxDepth bounds how many CERTIFIED call hops a process may span.
	// 3 mirrors closure.MaxDepth deliberately: the closure sidecar is the only
	// other transitive-reach artefact GT publishes, and two reach artefacts
	// that disagree about depth would be a reader trap. It is also the depth
	// at which path enumeration stays cheap enough to run inside publication.
	DefaultMaxDepth = 3

	// DefaultMinConfidence is a secondary floor applied on top of the CERTIFIED
	// tier gate, mirroring closure.MinEdgeConfidence. The tier is the primary
	// admission rule; this floor exists so that a graph whose tier stamping is
	// looser than today's (tierFor: conf >= 0.9 -> CERTIFIED) still cannot walk
	// a sub-verified edge into a published process.
	DefaultMinConfidence = 0.7

	// DefaultMaxProcesses caps emitted processes. Path enumeration to sinks is
	// worst-case exponential in MaxDepth; without a cap a single hub target
	// could emit an unbounded result set into an atomic publication
	// transaction. When the cap binds, Result.Truncated records that the result
	// is truncated — the derivation abstains loudly rather than silently.
	DefaultMaxProcesses = 10000

	// MinProcessDepth is the shortest path that counts as an interprocedural
	// slice. A depth-0 "path" is a single symbol, which is not a flow; emitting
	// one would inflate the count without describing any interprocedural
	// behaviour.
	MinProcessDepth = 1

	// TrustFloorCertified is the only trust floor a derived process can carry
	// today, because the walk admits CERTIFIED edges only. It is stored per
	// process rather than assumed, so that if the admission rule is ever
	// relaxed a reader can tell which processes predate the change.
	TrustFloorCertified = "CERTIFIED"

	// processIDDomain namespaces the process content hash so a process id can
	// never collide with another GT stable id computed over similar inputs.
	processIDDomain = "gt.process.v1"
)

// Process is one test-witnessed interprocedural slice.
//
// Every field is derived; nothing here is written by hand or by a model.
type Process struct {
	// ID is a deterministic content hash over the witness and the ordered
	// path, so the same graph yields the same ids across runs and machines.
	ID string

	// EntryStableID is the stable id of the entry point: the symbol the
	// witnessing assertion targets. Equal to Path[0].
	EntryStableID string

	// TerminalStableID is the stable id of the terminal: a symbol with no
	// outgoing CERTIFIED CALLS edge. Equal to Path[len(Path)-1].
	TerminalStableID string

	// Path is the ordered sequence of stable ids from entry to terminal,
	// inclusive of both. len(Path) == Depth+1.
	Path []string

	// WitnessAssertionID is `assertions.id` of the assertion that exercises
	// this flow. Never zero: a Process without a witness is never constructed.
	WitnessAssertionID int64

	// TestStableID is the stable id of the test symbol that owns the witness.
	TestStableID string

	// Kind is the witnessing assertion's `kind` column (e.g. "throws",
	// "equals") — it records how the flow is witnessed, not what it does.
	Kind string

	// Depth is the number of CERTIFIED call hops from entry to terminal.
	Depth int

	// TrustFloor is the weakest trust tier on the path. Always
	// TrustFloorCertified under the current admission rule.
	TrustFloor string
}

// Options bounds the derivation. A zero value is valid and means "all
// defaults"; no field may be set to a value that removes its bound.
type Options struct {
	// MaxDepth is the maximum number of CERTIFIED call hops. <= 0 means
	// DefaultMaxDepth. There is no "unlimited" setting.
	MaxDepth int

	// MinConfidence is the per-edge confidence floor applied on top of the
	// CERTIFIED tier gate. <= 0 means DefaultMinConfidence.
	MinConfidence float64

	// MaxProcesses caps the emitted slice. <= 0 means DefaultMaxProcesses.
	MaxProcesses int
}

func (o Options) maxDepth() int {
	if o.MaxDepth <= 0 {
		return DefaultMaxDepth
	}
	return o.MaxDepth
}

func (o Options) minConfidence() float64 {
	if o.MinConfidence <= 0 {
		return DefaultMinConfidence
	}
	return o.MinConfidence
}

func (o Options) maxProcesses() int {
	if o.MaxProcesses <= 0 {
		return DefaultMaxProcesses
	}
	return o.MaxProcesses
}

// Result is what one derivation run produced, plus enough accounting to tell
// "we found nothing" apart from "we did not look".
type Result struct {
	// Processes are ordered deterministically: by TestStableID, then
	// EntryStableID, then TerminalStableID, then the path, then witness id.
	Processes []Process

	// AssertionsScanned is the total number of `assertions` rows read,
	// including those with no linked target. It is the coverage denominator
	// the plan asks to be reported honestly.
	AssertionsScanned int

	// AssertionsWithTarget is how many of those rows carry a resolved target
	// symbol (target_node_id != 0).
	AssertionsWithTarget int

	// TargetsWithoutCertifiedPath is the number of distinct assertion targets
	// from which no CERTIFIED path to a terminal exists within MaxDepth. It is
	// the honest reason most assertions witness nothing.
	TargetsWithoutCertifiedPath int

	// Truncated reports that MaxProcesses bound the result.
	Truncated bool

	// Reason is set whenever Processes is empty, naming which stage came up
	// empty. It is never set when Processes is non-empty.
	Reason string
}

// Reason codes for an empty result. These are stable strings; callers may
// switch on them.
const (
	ReasonNoAssertions     = "no_assertions_in_graph"
	ReasonNoLinkedTargets  = "assertions_present_but_none_link_a_target"
	ReasonNoCertifiedEdges = "no_certified_calls_edges_in_graph"
	ReasonNoCertifiedPath  = "no_certified_path_from_any_witnessed_target"
	ReasonNoStableIDs      = "witnessed_paths_have_no_resolvable_stable_id"
)

// assertionRow is one `assertions` row that carries a resolved target.
type assertionRow struct {
	id         int64
	testNodeID int64
	targetID   int64
	kind       string
}

// Derive reads the graph and returns every test-witnessed process it can
// prove. It never writes. A read-only connection is sufficient.
func Derive(ctx context.Context, db *sql.DB, opts Options) (Result, error) {
	var res Result

	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM assertions`).Scan(&res.AssertionsScanned); err != nil {
		return res, fmt.Errorf("count assertions: %w", err)
	}
	if res.AssertionsScanned == 0 {
		res.Reason = ReasonNoAssertions
		return res, nil
	}

	assertions, err := readAssertions(ctx, db)
	if err != nil {
		return res, err
	}
	res.AssertionsWithTarget = len(assertions)
	if len(assertions) == 0 {
		res.Reason = ReasonNoLinkedTargets
		return res, nil
	}

	adj, err := readCertifiedCallGraph(ctx, db, opts.minConfidence())
	if err != nil {
		return res, err
	}
	if len(adj) == 0 {
		res.Reason = ReasonNoCertifiedEdges
		return res, nil
	}

	stableIDs, err := readStableIDs(ctx, db)
	if err != nil {
		return res, err
	}

	maxDepth := opts.maxDepth()
	maxProcesses := opts.maxProcesses()

	// A target's walk depends only on the graph, not on which assertion
	// witnesses it, so walk each distinct target once and reuse the paths for
	// every assertion that names it.
	pathsByTarget := make(map[int64][][]int64)
	unresolvedTargets := make(map[int64]bool)
	for _, a := range assertions {
		if _, done := pathsByTarget[a.targetID]; done {
			continue
		}
		paths := walkToTerminals(a.targetID, adj, maxDepth)
		pathsByTarget[a.targetID] = paths
		if len(paths) == 0 {
			unresolvedTargets[a.targetID] = true
		}
	}
	res.TargetsWithoutCertifiedPath = len(unresolvedTargets)

	missingStableID := false
	out := make([]Process, 0)
	for _, a := range assertions {
		testSID, ok := stableIDs[a.testNodeID]
		if !ok {
			// The witness itself is unnameable, so a reader could not trace
			// the process back to the test that proves it. Drop it rather
			// than publish a flow whose witness cannot be looked up.
			if len(pathsByTarget[a.targetID]) > 0 {
				missingStableID = true
			}
			continue
		}
		for _, path := range pathsByTarget[a.targetID] {
			sids, ok := stableIDPath(path, stableIDs)
			if !ok {
				// A path whose members cannot all be named by a stable id is
				// not addressable, so it is not published. It is dropped,
				// never published with a placeholder identifier.
				missingStableID = true
				continue
			}
			out = append(out, Process{
				ID:                 processID(a.id, sids),
				EntryStableID:      sids[0],
				TerminalStableID:   sids[len(sids)-1],
				Path:               sids,
				WitnessAssertionID: a.id,
				TestStableID:       testSID,
				Kind:               a.kind,
				Depth:              len(sids) - 1,
				TrustFloor:         TrustFloorCertified,
			})
		}
	}

	sortProcesses(out)
	if len(out) > maxProcesses {
		out = out[:maxProcesses]
		res.Truncated = true
	}
	res.Processes = out

	if len(out) == 0 {
		if missingStableID {
			res.Reason = ReasonNoStableIDs
		} else {
			res.Reason = ReasonNoCertifiedPath
		}
	}
	return res, nil
}

// readAssertions returns the assertions that link a test to a target symbol,
// ordered by id so the derivation is reproducible.
//
// `assertions.target_node_id` defaults to 0 rather than NULL in the store
// schema, so 0 — not NULL alone — is the "unlinked" sentinel.
func readAssertions(ctx context.Context, db *sql.DB) ([]assertionRow, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT id, test_node_id, target_node_id, COALESCE(kind, '')
		FROM assertions
		WHERE target_node_id IS NOT NULL AND target_node_id <> 0
		  AND test_node_id IS NOT NULL AND test_node_id <> 0
		ORDER BY id`)
	if err != nil {
		return nil, fmt.Errorf("read assertions: %w", err)
	}
	defer rows.Close()

	var out []assertionRow
	for rows.Next() {
		var a assertionRow
		if err := rows.Scan(&a.id, &a.testNodeID, &a.targetID, &a.kind); err != nil {
			return nil, fmt.Errorf("scan assertion: %w", err)
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// readCertifiedCallGraph builds the forward adjacency list the walk runs over.
//
// Admission rule: type = 'CALLS' AND trust_tier = 'CERTIFIED' AND confidence >=
// minConf. Self-loops and unresolved endpoints are dropped and parallel edges
// are deduped, so the adjacency is a simple digraph. Rows are read in
// (source, target) order and appended in that order, which is what makes the
// walk deterministic.
func readCertifiedCallGraph(ctx context.Context, db *sql.DB, minConf float64) (map[int64][]int64, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT DISTINCT source_id, target_id
		FROM edges
		WHERE type = 'CALLS'
		  AND trust_tier = 'CERTIFIED'
		  AND COALESCE(confidence, 0) >= ?
		  AND source_id IS NOT NULL AND target_id IS NOT NULL
		  AND source_id <> 0 AND target_id <> 0
		  AND source_id <> target_id
		ORDER BY source_id, target_id`, minConf)
	if err != nil {
		return nil, fmt.Errorf("read certified CALLS edges: %w", err)
	}
	defer rows.Close()

	adj := make(map[int64][]int64)
	for rows.Next() {
		var src, tgt int64
		if err := rows.Scan(&src, &tgt); err != nil {
			return nil, fmt.Errorf("scan certified edge: %w", err)
		}
		adj[src] = append(adj[src], tgt)
	}
	return adj, rows.Err()
}

// readStableIDs maps node id -> stable id for every code symbol that can
// appear on a path or own an assertion.
//
// Two sources, in priority order:
//
//  1. `nodes.stable_id`, when the producer stamped one.
//  2. `resolution_symbols.stable_id`, joined on native_id = nodes.id -- the key the
//     resolution candidates, the dedupe pass, contract.py and retrieval.py all use.
//
// The fallback is not cosmetic. On a real arktype graph every Function, Method
// and Class node has stable_id NULL while resolution_symbols carries a stable
// id keyed by native_id = nodes.id (3,509 of 3,511 symbols join on arktype) — so
// without the join the derivation would name nothing and emit nothing.
func readStableIDs(ctx context.Context, db *sql.DB) (map[int64]string, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT n.id, COALESCE(NULLIF(n.stable_id, ''), rs.stable_id, '')
		FROM nodes n
		LEFT JOIN resolution_symbols rs
		       ON CAST(rs.native_id AS INTEGER) = n.id
		WHERE n.label IN ('Function', 'Method', 'Class')`)
	if err != nil {
		return nil, fmt.Errorf("read stable ids: %w", err)
	}
	defer rows.Close()

	ids := make(map[int64]string)
	for rows.Next() {
		var id int64
		var sid string
		if err := rows.Scan(&id, &sid); err != nil {
			return nil, fmt.Errorf("scan stable id: %w", err)
		}
		if sid == "" {
			continue
		}
		ids[id] = sid
	}
	return ids, rows.Err()
}

// walkToTerminals enumerates one shortest path from entry to each terminal
// reachable within maxDepth hops.
//
// A terminal is a node with no outgoing CERTIFIED CALLS edge — a genuine sink,
// not a node that merely happened to sit at the depth limit. Truncating at the
// bound and calling the truncation point a terminal would manufacture
// terminals out of a tuning parameter, which is exactly the heuristic guessing
// this item exists to replace.
//
// The walk is a breadth-first sweep with a single visited set, so each node is
// expanded at most once (cycle-safe) and the first path found to any terminal
// is a shortest one. Frontier order follows the adjacency order, which is
// (source, target) sorted, so ties break identically on every run.
func walkToTerminals(entry int64, adj map[int64][]int64, maxDepth int) [][]int64 {
	if len(adj[entry]) == 0 {
		return nil // the entry is itself a sink: no interprocedural slice
	}

	type frontierItem struct {
		node int64
		path []int64
	}

	visited := map[int64]bool{entry: true}
	frontier := []frontierItem{{node: entry, path: []int64{entry}}}
	var terminals [][]int64

	for depth := 0; depth < maxDepth && len(frontier) > 0; depth++ {
		var next []frontierItem
		for _, cur := range frontier {
			for _, nbr := range adj[cur.node] {
				if visited[nbr] {
					continue
				}
				visited[nbr] = true

				path := make([]int64, len(cur.path), len(cur.path)+1)
				copy(path, cur.path)
				path = append(path, nbr)

				if len(adj[nbr]) == 0 {
					terminals = append(terminals, path)
					continue // a sink has nothing to expand
				}
				next = append(next, frontierItem{node: nbr, path: path})
			}
		}
		frontier = next
	}

	// Every path produced above already has depth >= 1; re-assert the floor so
	// the invariant survives a future change to the loop.
	kept := terminals[:0]
	for _, p := range terminals {
		if len(p)-1 >= MinProcessDepth {
			kept = append(kept, p)
		}
	}
	return kept
}

// stableIDPath translates a node-id path into stable ids, reporting false if
// any member is unnameable.
func stableIDPath(path []int64, stableIDs map[int64]string) ([]string, bool) {
	out := make([]string, len(path))
	for i, id := range path {
		sid, ok := stableIDs[id]
		if !ok {
			return nil, false
		}
		out[i] = sid
	}
	return out, true
}

// processID is a deterministic content hash over the witness and the ordered
// path. Two runs over the same graph produce the same id; two different flows
// witnessed by the same assertion produce different ids.
func processID(witnessID int64, path []string) string {
	h := sha256.New()
	h.Write([]byte(processIDDomain))
	h.Write([]byte{0})
	h.Write([]byte(strconv.FormatInt(witnessID, 10)))
	for _, sid := range path {
		h.Write([]byte{0})
		h.Write([]byte(sid))
	}
	return hex.EncodeToString(h.Sum(nil))
}

// sortProcesses imposes the published order: test, then entry, then terminal,
// then the full path, then the witness id. Every component is a total order on
// its own key space, so the composite is total and the output is byte-stable.
func sortProcesses(ps []Process) {
	sort.SliceStable(ps, func(i, j int) bool {
		a, b := ps[i], ps[j]
		if a.TestStableID != b.TestStableID {
			return a.TestStableID < b.TestStableID
		}
		if a.EntryStableID != b.EntryStableID {
			return a.EntryStableID < b.EntryStableID
		}
		if a.TerminalStableID != b.TerminalStableID {
			return a.TerminalStableID < b.TerminalStableID
		}
		if pa, pb := strings.Join(a.Path, ">"), strings.Join(b.Path, ">"); pa != pb {
			return pa < pb
		}
		return a.WitnessAssertionID < b.WitnessAssertionID
	})
}

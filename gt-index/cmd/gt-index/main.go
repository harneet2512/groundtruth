// gt-index: Multi-language code graph indexer using tree-sitter.
//
// Builds a SQLite graph database from source code. Supports 30 languages
// via tree-sitter grammars with import-based edge resolution.
//
// v15: Performance — parallel parsing, batch SQLite inserts, edge confidence.
//
// Usage:
//
//	gt-index -root=/path/to/repo -output=/tmp/gt_graph.db
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"sync"
	"time"


	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
	// Note: specs is imported above (named); its init() functions register all language specs.
)

// RC-17 (F-003): build-stamp variables. Populated at link time via
//   go build -ldflags='-X main.commitSHA=... -X main.buildTimeUTC=... -X main.goToolchain=...'
// Defaults of "unknown" let `go run` and bare `go build` still produce a
// usable binary for development (the smoke-runner preflight refuses
// "unknown" so paid runs cannot ship with an unstamped binary).
//
// TODO(RC-17-build): rebuild on Linux host with the build script — this
// Windows worktree cannot regenerate bin/gt-index-linux.
var (
	commitSHA    = "unknown"
	buildTimeUTC = "unknown"
	goToolchain  = "unknown"
)

// fileParseResult holds the output of parsing a single file.
type fileParseResult struct {
	fileIdx int
	result  *parser.ParseResult
	err     error
}

func main() {
	root := flag.String("root", ".", "Project root directory")
	output := flag.String("output", "graph.db", "Output SQLite database path")
	maxFiles := flag.Int("max-files", 10000, "Maximum files to index")
	workers := flag.Int("workers", 0, "Parallel parse workers (0 = NumCPU)")
	file := flag.String("file", "", "Incremental mode: re-index only this single file (relative to -root) into an existing -output graph.db")
	flag.Parse()

	if *workers <= 0 {
		*workers = runtime.NumCPU()
	}

	// Incremental single-file mode: file-keyed delete-and-replace against an
	// existing graph.db. Does not rebuild from scratch; expects -output to exist.
	if *file != "" {
		if err := runIncremental(*root, *file, *output); err != nil {
			log.Fatalf("incremental: %v", err)
		}
		return
	}

	start := time.Now()

	// Remove old DB if it exists
	os.Remove(*output)

	// Open database
	db, err := store.Open(*output)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer db.Close()

	// ── Pass 1: STRUCTURE — discover files ──────────────────────────────
	fmt.Fprintf(os.Stderr, "Pass 1: discovering files in %s...\n", *root)
	files, err := walker.Walk(*root, *maxFiles)
	if err != nil {
		log.Fatalf("walk: %v", err)
	}
	fmt.Fprintf(os.Stderr, "  Found %d source files\n", len(files))

	langCount := make(map[string]int)
	for _, f := range files {
		langCount[f.Language]++
	}
	for lang, count := range langCount {
		fmt.Fprintf(os.Stderr, "  %s: %d files\n", lang, count)
	}

	// Collect file paths and languages for BuildFileMap
	filePaths := make([]string, len(files))
	fileLangs := make([]string, len(files))
	for i, sf := range files {
		filePaths[i] = sf.Path
		fileLangs[i] = sf.Language
	}

	// ── Pass 2: DEFINITIONS + IMPORTS — parallel parse, batch insert ────
	parseStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 2: parsing %d files (%d workers)...\n", len(files), *workers)

	// Parse files in parallel
	results := make([]*parser.ParseResult, len(files))
	resultCh := make(chan fileParseResult, len(files))

	var wg sync.WaitGroup
	fileCh := make(chan int, len(files))

	// Start workers
	for w := 0; w < *workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for idx := range fileCh {
				sf := files[idx]
				isTest := walker.IsTestFile(sf.Path)
				result, err := parser.ParseFile(sf, isTest)
				resultCh <- fileParseResult{fileIdx: idx, result: result, err: err}
			}
		}()
	}

	// Feed files to workers
	for i := range files {
		fileCh <- i
	}
	close(fileCh)

	// Wait for all workers to finish
	go func() {
		wg.Wait()
		close(resultCh)
	}()

	// Collect results
	for pr := range resultCh {
		if pr.err == nil && pr.result != nil {
			results[pr.fileIdx] = pr.result
		}
	}

	parseElapsed := time.Since(parseStart)
	fmt.Fprintf(os.Stderr, "  Parsed in %s\n", parseElapsed.Round(time.Millisecond))

	// Collect all nodes for batch insert
	var allNodePtrs []*store.Node
	var allCalls []parser.CallRef
	var allImports []parser.ImportRef
	var allProps []parser.PropertyRef
	var allAssertions []parser.AssertionRef
	callerNodeIndexMap := make(map[int]int) // call index → global node index

	globalNodeIdx := 0
	for _, result := range results {
		if result == nil {
			continue
		}
		fileNodeStartIdx := globalNodeIdx
		for i := range result.Nodes {
			node := &result.Nodes[i]
			// Fix M16: ParentID is file-local (1-based index within this file's nodes).
			// Convert to global index so BatchInsertNodes can map to DB IDs.
			if node.ParentID > 0 {
				// ParentID was set as (file-local-idx + 1), convert to global
				node.ParentID = int64(fileNodeStartIdx) + node.ParentID
			}
			allNodePtrs = append(allNodePtrs, node)
			globalNodeIdx++
		}
		for _, call := range result.Calls {
			globalCallerIdx := fileNodeStartIdx + call.CallerNodeIdx
			allCalls = append(allCalls, call)
			callerNodeIndexMap[len(allCalls)-1] = globalCallerIdx
		}
		for _, prop := range result.Properties {
			p := prop
			p.NodeIdx = fileNodeStartIdx + prop.NodeIdx
			allProps = append(allProps, p)
		}
		for _, a := range result.Assertions {
			a2 := a
			a2.TestNodeIdx = fileNodeStartIdx + a.TestNodeIdx
			allAssertions = append(allAssertions, a2)
		}
		allImports = append(allImports, result.Imports...)
	}

	// Before batch insert: convert ParentID from global slice index to 0
	// (we'll fix it up after we have DB IDs)
	parentFixups := make(map[int]int64) // node slice index → parent global index
	for i, n := range allNodePtrs {
		if n.ParentID > 0 {
			parentFixups[i] = n.ParentID
			n.ParentID = 0 // insert with 0, fix up after
		}
	}

	// Batch insert all nodes in one transaction
	insertStart := time.Now()
	nodeDBIDs, err := db.BatchInsertNodes(allNodePtrs)
	if err != nil {
		log.Fatalf("batch insert nodes: %v", err)
	}

	// Fix up parent IDs: map global index → DB ID
	for nodeIdx, parentGlobalIdx := range parentFixups {
		pidx := int(parentGlobalIdx) - 1 // convert 1-based to 0-based
		if pidx >= 0 && pidx < len(nodeDBIDs) {
			parentDBID := nodeDBIDs[pidx]
			if parentDBID > 0 {
				db.UpdateParentID(nodeDBIDs[nodeIdx], parentDBID)
			}
		}
	}

	insertElapsed := time.Since(insertStart)
	fmt.Fprintf(os.Stderr, "  Inserted %d nodes in %s\n", len(nodeDBIDs), insertElapsed.Round(time.Millisecond))

	fmt.Fprintf(os.Stderr, "  Extracted %d definitions, %d imports\n", len(allNodePtrs), len(allImports))

	// ── Pass 3: CALLS — resolve references ──────────────────────────────
	resolveStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 3: resolving %d call references...\n", len(allCalls))

	// Build indexes from collected nodes (not from DB queries)
	allNodes := make([]store.Node, len(allNodePtrs))
	for i, np := range allNodePtrs {
		allNodes[i] = *np
	}
	nameIndex, fileIndex := resolver.BuildNameIndex(db, allNodes, nodeDBIDs)
	fileMap := resolver.BuildFileMap(filePaths, fileLangs)

	// Build caller ID list
	callerDBIDs := make([]int64, len(allCalls))
	for i := range allCalls {
		if globalIdx, ok := callerNodeIndexMap[i]; ok && globalIdx < len(nodeDBIDs) {
			callerDBIDs[i] = nodeDBIDs[globalIdx]
		}
	}

	resolved := resolver.Resolve(allCalls, nameIndex, fileIndex, callerDBIDs, allImports, fileMap)

	resolveElapsed := time.Since(resolveStart)

	// Count by resolution method
	methodCounts := make(map[string]int)
	for _, rc := range resolved {
		methodCounts[rc.Method]++
	}
	fmt.Fprintf(os.Stderr, "  Resolved %d/%d calls in %s", len(resolved), len(allCalls), resolveElapsed.Round(time.Millisecond))
	for method, count := range methodCounts {
		fmt.Fprintf(os.Stderr, " [%s:%d]", method, count)
	}
	fmt.Fprintln(os.Stderr)

	// Batch insert all edges in one transaction
	edgeStart := time.Now()
	edgePtrs := make([]*store.Edge, len(resolved))
	for i, rc := range resolved {
		edgePtrs[i] = &store.Edge{
			SourceID:         rc.SourceNodeID,
			TargetID:         rc.TargetNodeID,
			Type:             "CALLS",
			SourceLine:       rc.SourceLine,
			SourceFile:       rc.SourceFile,
			ResolutionMethod: rc.Method,
			Confidence:       rc.Confidence,
		}
	}
	if err := db.BatchInsertEdges(edgePtrs); err != nil {
		log.Fatalf("batch insert edges: %v", err)
	}
	edgeElapsed := time.Since(edgeStart)
	fmt.Fprintf(os.Stderr, "  Inserted %d edges in %s\n", len(edgePtrs), edgeElapsed.Round(time.Millisecond))

	// ── Pass 4: PROPERTIES + ASSERTIONS ─────────────────────────────────
	propStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 4: inserting %d properties, %d assertions...\n", len(allProps), len(allAssertions))

	// Convert PropertyRefs to store.Property (map node index → DB ID)
	propPtrs := make([]*store.Property, 0, len(allProps))
	for _, p := range allProps {
		if p.NodeIdx >= 0 && p.NodeIdx < len(nodeDBIDs) {
			propPtrs = append(propPtrs, &store.Property{
				NodeID:     nodeDBIDs[p.NodeIdx],
				Kind:       p.Kind,
				Value:      p.Value,
				Line:       p.Line,
				Confidence: p.Confidence,
			})
		}
	}
	if err := db.BatchInsertProperties(propPtrs); err != nil {
		log.Printf("WARNING: batch insert properties: %v", err)
	}

	// Convert AssertionRefs to store.Assertion (map node index → DB ID)
	assertPtrs := make([]*store.Assertion, 0, len(allAssertions))
	for _, a := range allAssertions {
		if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(nodeDBIDs) {
			assertPtrs = append(assertPtrs, &store.Assertion{
				TestNodeID: nodeDBIDs[a.TestNodeIdx],
				Kind:       a.Kind,
				Expression: a.Expression,
				Expected:   a.Expected,
				Line:       a.Line,
			})
		}
	}
	if err := db.BatchInsertAssertions(assertPtrs); err != nil {
		log.Printf("WARNING: batch insert assertions: %v", err)
	}

	propElapsed := time.Since(propStart)
	fmt.Fprintf(os.Stderr, "  Inserted %d properties, %d assertions in %s\n",
		len(propPtrs), len(assertPtrs), propElapsed.Round(time.Millisecond))

	// ── Pass 5: EXTRAS — store metadata ─────────────────────────────────
	fmt.Fprintf(os.Stderr, "Pass 5: storing metadata...\n")
	elapsed := time.Since(start)
	db.SetMeta("root", *root)
	// RC-17 (F-004): build_time_ms removed from project_meta — it's wall-
	// clock dependent and breaks byte-equality across two builds of the
	// same commit. Diagnostic value only; emitted to stderr below instead.
	db.SetMeta("file_count", fmt.Sprintf("%d", len(files)))
	db.SetMeta("node_count", fmt.Sprintf("%d", len(allNodePtrs)))
	db.SetMeta("edge_count", fmt.Sprintf("%d", len(resolved)))
	db.SetMeta("import_count", fmt.Sprintf("%d", len(allImports)))
	db.SetMeta("property_count", fmt.Sprintf("%d", len(propPtrs)))
	db.SetMeta("assertion_count", fmt.Sprintf("%d", len(assertPtrs)))
	db.SetMeta("indexer_version", "v16-multilang")
	// RC-17 (F-003): forensics-grade provenance. commitSHA / buildTimeUTC
	// / goToolchain are injected by the build script via -ldflags. With
	// "unknown" defaults, callers can still distinguish a stamped binary
	// from a bare `go build`.
	db.SetMeta("git_commit", commitSHA)
	db.SetMeta("build_time_utc", buildTimeUTC)
	db.SetMeta("go_toolchain", goToolchain)
	db.SetMeta("workers", fmt.Sprintf("%d", *workers))

	// RC-04: per-repo MIN_CONFIDENCE — write the median (P50) of resolved edge
	// confidences so downstream readers can stop hardcoding 0.7. Writing to
	// project_meta (existing table, no schema change). Readers fall back to
	// 0.5 (brief-layer parity) when this key is missing.
	db.SetMeta("min_confidence", fmt.Sprintf("%.4f", computeMedianConfidence(resolved)))

	// Summary
	fmt.Fprintf(os.Stderr, "\nDone in %s\n", elapsed.Round(time.Millisecond))
	fmt.Fprintf(os.Stderr, "  Files:      %d\n", len(files))
	fmt.Fprintf(os.Stderr, "  Nodes:      %d\n", db.NodeCount())
	fmt.Fprintf(os.Stderr, "  Edges:      %d\n", db.EdgeCount())
	fmt.Fprintf(os.Stderr, "  Imports:    %d\n", len(allImports))
	fmt.Fprintf(os.Stderr, "  Properties: %d\n", db.PropertyCount())
	fmt.Fprintf(os.Stderr, "  Assertions: %d\n", db.AssertionCount())
	fmt.Fprintf(os.Stderr, "  Workers:    %d\n", *workers)
	// RC-17 (F-004): build_time_ms is diagnostic-only now (stderr, not DB).
	fmt.Fprintf(os.Stderr, "  BuildTime:  %d ms (diagnostic; not in project_meta)\n",
		elapsed.Milliseconds())
	// RC-17 (F-003): surface the build stamps so artifact-side logs
	// preserve them even when project_meta is not inspected.
	fmt.Fprintf(os.Stderr, "  Commit:     %s\n", commitSHA)
	fmt.Fprintf(os.Stderr, "  BuiltAt:    %s\n", buildTimeUTC)
	fmt.Fprintf(os.Stderr, "  Toolchain:  %s\n", goToolchain)
	fmt.Fprintf(os.Stderr, "  Output:     %s\n", *output)

	// Print JSON summary to stdout
	importResolved := methodCounts["import"]
	sameFileResolved := methodCounts["same_file"]
	nameMatchResolved := methodCounts["name_match"]
	fmt.Printf(`{"files":%d,"nodes":%d,"edges":%d,"imports":%d,"properties":%d,"assertions":%d,"edges_import":%d,"edges_same_file":%d,"edges_name_match":%d,"time_ms":%d,"workers":%d}`,
		len(files), db.NodeCount(), db.EdgeCount(), len(allImports),
		db.PropertyCount(), db.AssertionCount(),
		importResolved, sameFileResolved, nameMatchResolved,
		elapsed.Milliseconds(), *workers)
	fmt.Println()
}

// runIncremental performs a file-keyed delete-and-replace reindex of a
// single file inside an existing graph.db. Steps follow the Track B0 spec:
//
//  1. Open existing -output db (error if missing).
//  2. SHA-256 of <root>/<relpath>.
//  3. Hash matches stored file_hashes row → exit no-op (short-circuit).
//  4. BEGIN TRANSACTION.
//  5. DELETE edges WHERE source_file=? OR target_id IN (this file's nodes).
//  6. DELETE nodes WHERE file_path=?.
//  7. Re-parse the single file via parser.ParseFile.
//  8. Re-insert nodes; re-resolve calls against the rest of the DB; insert edges.
//  9. INSERT OR REPLACE INTO file_hashes.
//  10. COMMIT.
//  11. Print one JSON line to stdout.
func runIncremental(root, relpath, dbPath string) error {
	startWall := time.Now()

	// Step 1 — db must already exist.
	if _, err := os.Stat(dbPath); err != nil {
		return fmt.Errorf("graph.db not found at %s (incremental mode requires an existing db): %w", dbPath, err)
	}
	db, err := store.Open(dbPath)
	if err != nil {
		return fmt.Errorf("open db: %w", err)
	}
	defer db.Close()

	// Resolve language spec from extension. If unsupported, surface an error
	// rather than silently no-op (caller intent was clearly to reindex this file).
	ext := filepath.Ext(relpath)
	spec := specs.ForExtension(ext)
	if spec == nil {
		return fmt.Errorf("no language spec registered for extension %q (file=%s)", ext, relpath)
	}

	absPath := filepath.Join(root, relpath)
	relSlash := filepath.ToSlash(relpath)

	// Step 2 — sha256 of file contents.
	contents, err := os.ReadFile(absPath)
	if err != nil {
		return fmt.Errorf("read file %s: %w", absPath, err)
	}
	sum := sha256.Sum256(contents)
	newHash := hex.EncodeToString(sum[:])

	// Step 3 — short-circuit if hash matches stored value.
	storedHash := db.GetFileHash(relSlash)
	if storedHash == newHash {
		dur := time.Since(startWall)
		fmt.Printf(
			`{"file":%q,"nodes_replaced":0,"edges_replaced":0,"incoming_restored":0,"incoming_unresolved":0,"duration_ms":%d,"short_circuited":true}`+"\n",
			relSlash, dur.Milliseconds(),
		)
		return nil
	}

	// Step 7 (early) — re-parse the single file BEFORE opening the write tx,
	// so any parser failure aborts cleanly without touching the DB.
	sf := walker.SourceFile{
		Path:     filepath.ToSlash(relpath),
		AbsPath:  absPath,
		Language: spec.Name,
		Spec:     spec,
	}
	isTest := walker.IsTestFile(relSlash)
	pr, err := parser.ParseFile(sf, isTest)
	if err != nil {
		return fmt.Errorf("parse %s: %w", relSlash, err)
	}
	if pr == nil {
		pr = &parser.ParseResult{}
	}

	// Pre-fetch resolver inputs from the existing DB BEFORE the delete (so the
	// just-deleted file's old nodes don't pollute the resolver's name/file
	// indexes used for the new edges; ResolveOnly removes the file's old IDs).
	// We could fetch after the delete-and-insert too — both are correct — but
	// querying outside the tx avoids mixing read-on-tx semantics across drivers.
	allNodes, allIDs, err := db.GetAllNodes()
	if err != nil {
		return fmt.Errorf("read all nodes: %w", err)
	}
	allFiles, allLangs, err := db.GetDistinctFilesAndLanguages()
	if err != nil {
		return fmt.Errorf("read distinct files: %w", err)
	}

	// Step 4 — BEGIN TRANSACTION wrapping steps 5–9.
	tx, err := db.BeginTx()
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	committed := false
	defer func() {
		if !committed {
			tx.Rollback()
		}
	}()

	// Step 4.5 — snapshot incoming cross-file edges BEFORE delete. These get
	// stripped by the upcoming target_id-based DELETE; without this snapshot
	// they'd be lost permanently because re-parsing this file does NOT
	// re-emit the calls that originate in other files. Self-edges (within
	// this same file) are excluded from the snapshot — they'll be re-emitted
	// naturally when the parser re-runs over this file's body.
	incomingSnap, err := store.SnapshotIncomingEdgesTx(tx, relSlash, 0)
	if err != nil {
		return err
	}

	// Steps 5+6 — delete edges (both directions), then nodes, for this file.
	edgesDeleted, nodesDeleted, err := store.DeleteFileEdgesAndNodesTx(tx, relSlash)
	if err != nil {
		return err
	}
	_ = nodesDeleted // captured for diagnostics; not surfaced beyond this scope

	// Step 8 — insert this file's new nodes, then resolve+insert its outgoing edges.
	newNodePtrs := make([]*store.Node, len(pr.Nodes))
	parentLocal := make([]int64, len(pr.Nodes))
	for i := range pr.Nodes {
		n := &pr.Nodes[i]
		parentLocal[i] = n.ParentID
		n.ParentID = 0
		newNodePtrs[i] = n
	}
	newDBIDs, err := store.BatchInsertNodesTx(tx, newNodePtrs)
	if err != nil {
		return fmt.Errorf("insert new nodes: %w", err)
	}
	for i, plocal := range parentLocal {
		if plocal > 0 {
			pidx := int(plocal) - 1
			if pidx >= 0 && pidx < len(newDBIDs) && newDBIDs[pidx] > 0 {
				if err := store.UpdateParentIDTx(tx, newDBIDs[i], newDBIDs[pidx]); err != nil {
					return fmt.Errorf("fixup parent_id: %w", err)
				}
			}
		}
	}

	// Re-resolve outgoing calls. The pre-fetched allNodes/allIDs include the
	// just-deleted file's old IDs; filter them out so calls don't resolve to
	// stale-and-deleted DB rows.
	filteredNodes := make([]store.Node, 0, len(allNodes))
	filteredIDs := make([]int64, 0, len(allIDs))
	for i, n := range allNodes {
		if n.FilePath == relSlash {
			continue
		}
		filteredNodes = append(filteredNodes, n)
		filteredIDs = append(filteredIDs, allIDs[i])
	}
	// Append the freshly-inserted nodes for same-file resolution.
	for i, n := range pr.Nodes {
		if newDBIDs[i] == 0 {
			continue
		}
		nn := n
		nn.ID = newDBIDs[i]
		filteredNodes = append(filteredNodes, nn)
		filteredIDs = append(filteredIDs, newDBIDs[i])
	}
	nameIndex, fileIndex := resolver.BuildNameIndex(db, filteredNodes, filteredIDs)
	fileMap := resolver.BuildFileMap(allFiles, allLangs)

	callerDBIDs := make([]int64, len(pr.Calls))
	for i, call := range pr.Calls {
		if call.CallerNodeIdx >= 0 && call.CallerNodeIdx < len(newDBIDs) {
			callerDBIDs[i] = newDBIDs[call.CallerNodeIdx]
		}
	}

	resolved := resolver.Resolve(pr.Calls, nameIndex, fileIndex, callerDBIDs, pr.Imports, fileMap)
	edgePtrs := make([]*store.Edge, len(resolved))
	for i, rc := range resolved {
		edgePtrs[i] = &store.Edge{
			SourceID:         rc.SourceNodeID,
			TargetID:         rc.TargetNodeID,
			Type:             "CALLS",
			SourceLine:       rc.SourceLine,
			SourceFile:       rc.SourceFile,
			ResolutionMethod: rc.Method,
			Confidence:       rc.Confidence,
		}
	}
	if err := store.BatchInsertEdgesTx(tx, edgePtrs); err != nil {
		return fmt.Errorf("insert new edges: %w", err)
	}

	// Properties + assertions for the reparsed file.
	propPtrs := make([]*store.Property, 0, len(pr.Properties))
	for _, p := range pr.Properties {
		if p.NodeIdx >= 0 && p.NodeIdx < len(newDBIDs) {
			propPtrs = append(propPtrs, &store.Property{
				NodeID:     newDBIDs[p.NodeIdx],
				Kind:       p.Kind,
				Value:      p.Value,
				Line:       p.Line,
				Confidence: p.Confidence,
			})
		}
	}
	if err := store.BatchInsertPropertiesTx(tx, propPtrs); err != nil {
		return fmt.Errorf("insert properties: %w", err)
	}
	assertPtrs := make([]*store.Assertion, 0, len(pr.Assertions))
	for _, a := range pr.Assertions {
		if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(newDBIDs) {
			assertPtrs = append(assertPtrs, &store.Assertion{
				TestNodeID: newDBIDs[a.TestNodeIdx],
				Kind:       a.Kind,
				Expression: a.Expression,
				Expected:   a.Expected,
				Line:       a.Line,
			})
		}
	}
	if err := store.BatchInsertAssertionsTx(tx, assertPtrs); err != nil {
		return fmt.Errorf("insert assertions: %w", err)
	}

	// Step 8.5 — re-resolve the incoming-edge snapshot against the freshly
	// inserted nodes. Edges whose target name no longer exists in this file
	// (rename/removal) are dropped silently and counted in `incomingUnres`.
	incomingRest, incomingUnres, err := store.ResolveIncomingEdgesTx(tx, incomingSnap, relSlash)
	if err != nil {
		return fmt.Errorf("re-resolve incoming edges: %w", err)
	}

	// Step 9 — record new content hash inside the same tx.
	if err := store.InsertFileHashTx(tx, relSlash, newHash, spec.Name); err != nil {
		return fmt.Errorf("update file_hashes: %w", err)
	}

	// Step 10 — COMMIT.
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit: %w", err)
	}
	committed = true
	// RC-04: fold WAL frames into the main DB file immediately so concurrent
	// readers (gt_query/gt_search/gt_navigate/gt_validate) never see a partial
	// WAL after a SIGKILL between commits. The per-file incremental path is
	// the only writer that overlaps with reader processes in practice.
	db.CheckpointWAL()

	// Step 11 — JSON line on stdout. nodes_replaced = inserted count;
	// edges_replaced = max(deleted, inserted) edges so callers see the size of
	// the change, not just the new ones.
	replacedEdges := int64(len(edgePtrs))
	if edgesDeleted > replacedEdges {
		replacedEdges = edgesDeleted
	}
	dur := time.Since(startWall)
	fmt.Printf(
		`{"file":%q,"nodes_replaced":%d,"edges_replaced":%d,"incoming_restored":%d,"incoming_unresolved":%d,"duration_ms":%d,"short_circuited":false}`+"\n",
		relSlash, len(newDBIDs), replacedEdges, incomingRest, incomingUnres, dur.Milliseconds(),
	)
	return nil
}

// computeMedianConfidence returns the P50 of confidences across all resolved
// edges. RC-04: this becomes the per-repo MIN_CONFIDENCE floor surfaced to
// readers via project_meta.min_confidence. Falls back to 0.5 (parity with
// gt_intel.MIN_CONFIDENCE in the brief layer) on empty input so the floor
// never collapses to 0 on tiny / failed indexes.
func computeMedianConfidence(rcs []resolver.ResolvedCall) float64 {
	if len(rcs) == 0 {
		return 0.5
	}
	xs := make([]float64, 0, len(rcs))
	for _, r := range rcs {
		xs = append(xs, r.Confidence)
	}
	sort.Float64s(xs)
	mid := len(xs) / 2
	if len(xs)%2 == 1 {
		return xs[mid]
	}
	return (xs[mid-1] + xs[mid]) / 2
}

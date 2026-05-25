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
	"regexp"
	"runtime"
	"sort"
	"strings"
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

// FINAL_ARCH_V2 schema contract.
// Bump when edges/nodes columns change; Python readers gate on >= this.
const schemaVersion = "v15.1-trust-tier"

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

	// Register Go module-prefixed paths for import resolution
	if goModPath := resolver.FindGoModulePath(*root); goModPath != "" {
		resolver.RegisterGoModulePaths(fileMap, goModPath)
		fmt.Fprintf(os.Stderr, "  Go module: %s\n", goModPath)
	}

	// Register TypeScript tsconfig.json path aliases
	if tsCfg := resolver.ParseTSConfig(*root); tsCfg != nil {
		resolver.RegisterTSConfigPaths(fileMap, tsCfg)
		fmt.Fprintf(os.Stderr, "  TS config: baseUrl=%s, %d path aliases\n", tsCfg.BaseURL, len(tsCfg.Paths))
	}

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
			SourceID:           rc.SourceNodeID,
			TargetID:           rc.TargetNodeID,
			Type:               "CALLS",
			SourceLine:         rc.SourceLine,
			SourceFile:         rc.SourceFile,
			ResolutionMethod:   rc.Method,
			Confidence:         rc.Confidence,
			TrustTier:          rc.TrustTier,
			CandidateCount:     rc.CandidateCount,
			EvidenceType:       rc.EvidenceType,
			VerificationStatus: "unverified",
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

	// Convert AssertionRefs to store.Assertion with target resolution
	assertPtrs := make([]*store.Assertion, 0, len(allAssertions))

	// Build name→nodeDBID lookup for assertion target resolution
	nameToNodeIDs := make(map[string][]int64)
	for i, n := range allNodePtrs {
		if i < len(nodeDBIDs) && n.Label != "Class" && n.Label != "Interface" && !n.IsTest {
			nameToNodeIDs[n.Name] = append(nameToNodeIDs[n.Name], nodeDBIDs[i])
		}
	}

	// Strategy 1.5 indexes: import-guided assertion resolution.
	// importIndex: test file path → imported name → list of target file paths
	importIndex := make(map[string]map[string][]string)
	for _, imp := range allImports {
		if imp.ImportedName == "" || imp.ImportedName == "*" {
			continue
		}
		byName, ok := importIndex[imp.File]
		if !ok {
			byName = make(map[string][]string)
			importIndex[imp.File] = byName
		}
		// Resolve module path to actual file(s) via fileMap
		if targetFiles, ok := fileMap[imp.ModulePath]; ok {
			byName[imp.ImportedName] = append(byName[imp.ImportedName], targetFiles...)
		}
	}
	// fileNodeIDs: file path → function name → list of node DB IDs
	fileNodeIDs := make(map[string]map[string][]int64)
	for i, n := range allNodePtrs {
		if i < len(nodeDBIDs) && n.Label != "Class" && n.Label != "Interface" && !n.IsTest {
			byName, ok := fileNodeIDs[n.FilePath]
			if !ok {
				byName = make(map[string][]int64)
				fileNodeIDs[n.FilePath] = byName
			}
			byName[n.Name] = append(byName[n.Name], nodeDBIDs[i])
		}
	}

	resolvedCount := 0
	for _, a := range allAssertions {
		if a.TestNodeIdx < 0 || a.TestNodeIdx >= len(nodeDBIDs) {
			continue
		}
		targetID := resolveAssertionTarget(a, allNodePtrs, nodeDBIDs, nameToNodeIDs, importIndex, fileNodeIDs)
		assertPtrs = append(assertPtrs, &store.Assertion{
			TestNodeID:   nodeDBIDs[a.TestNodeIdx],
			TargetNodeID: targetID,
			Kind:         a.Kind,
			Expression:   a.Expression,
			Expected:     a.Expected,
			Line:         a.Line,
		})
		if targetID > 0 {
			resolvedCount++
		}
	}
	if len(assertPtrs) > 0 {
		fmt.Fprintf(os.Stderr, "  Assertion targets resolved: %d/%d (%.0f%%)\n",
			resolvedCount, len(assertPtrs), 100.0*float64(resolvedCount)/float64(len(assertPtrs)))
	}
	if err := db.BatchInsertAssertions(assertPtrs); err != nil {
		log.Printf("WARNING: batch insert assertions: %v", err)
	}

	propElapsed := time.Since(propStart)
	fmt.Fprintf(os.Stderr, "  Inserted %d properties, %d assertions in %s\n",
		len(propPtrs), len(assertPtrs), propElapsed.Round(time.Millisecond))

	// ── Pass 4b: API EDGES — cross-service route matching ───────────────
	apiStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 4b: resolving API edges...\n")
	apiEdgeCount, apiErr := resolver.ResolveAPIEdges(db, files, *root)
	if apiErr != nil {
		log.Printf("WARNING: API edge resolution: %v", apiErr)
	}
	apiElapsed := time.Since(apiStart)
	fmt.Fprintf(os.Stderr, "  Resolved %d API edges in %s\n", apiEdgeCount, apiElapsed.Round(time.Millisecond))

	// ── Pass 4c: RELATIONSHIP EDGES — inheritance, interfaces, decorators, composition, re-exports
	relStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 4c: extracting relationships (inheritance, interfaces, composition, re-exports)...\n")
	relCount, relErr := resolver.ResolveRelationships(db, files, *root)
	if relErr != nil {
		log.Printf("WARNING: relationship extraction failed: %v", relErr)
	}
	relElapsed := time.Since(relStart)
	fmt.Fprintf(os.Stderr, "  Extracted %d relationship edges in %s\n", relCount, relElapsed.Round(time.Millisecond))

	// ── Pass 4d: SERIALIZATION PAIRS — detect serialize/deserialize partners ──
	serdeStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 4d: detecting serialization pairs...\n")
	serdeCount := detectSerdePairs(db, allNodePtrs, nodeDBIDs)
	serdeElapsed := time.Since(serdeStart)
	fmt.Fprintf(os.Stderr, "  Detected %d serialization pair properties in %s\n", serdeCount, serdeElapsed.Round(time.Millisecond))

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
	// FINAL_ARCH_V2 Track-A (B-1/B-5): schema_version is a contract between
	// the Go writer and Python readers. Readers MUST fail fast if this row
	// is missing (= old binary) or older than the version the reader expects.
	// Bump on every breaking edges/nodes schema change.
	db.SetMeta("schema_version", schemaVersion)
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

	// ── Pass 5b: FILE HASHES — populate file_hashes for incremental reindex ──
	fmt.Fprintf(os.Stderr, "Pass 5b: recording file hashes for %d files...\n", len(files))
	hashErrors := 0
	for _, sf := range files {
		content, err := os.ReadFile(sf.AbsPath)
		if err != nil {
			hashErrors++
			continue
		}
		sum := sha256.Sum256(content)
		h := hex.EncodeToString(sum[:])
		if err := db.InsertFileHash(sf.Path, h, sf.Language); err != nil {
			hashErrors++
		}
	}
	if hashErrors > 0 {
		fmt.Fprintf(os.Stderr, "  WARNING: %d file hash errors\n", hashErrors)
	}

	// Post-insert FK validation (non-fatal)
	db.ValidateForeignKeys()

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
			SourceID:           rc.SourceNodeID,
			TargetID:           rc.TargetNodeID,
			Type:               "CALLS",
			SourceLine:         rc.SourceLine,
			SourceFile:         rc.SourceFile,
			ResolutionMethod:   rc.Method,
			Confidence:         rc.Confidence,
			TrustTier:          rc.TrustTier,
			CandidateCount:     rc.CandidateCount,
			EvidenceType:       rc.EvidenceType,
			VerificationStatus: "unverified",
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
	// Build name→ID lookup for incremental assertion resolution
	incrNameToIDs := make(map[string][]int64)
	for i, n := range pr.Nodes {
		if i < len(newDBIDs) && n.Label != "Class" && n.Label != "Interface" && !n.IsTest {
			incrNameToIDs[n.Name] = append(incrNameToIDs[n.Name], newDBIDs[i])
		}
	}
	incrNodePtrs := make([]*store.Node, len(pr.Nodes))
	for i := range pr.Nodes {
		incrNodePtrs[i] = &pr.Nodes[i]
	}

	assertPtrs := make([]*store.Assertion, 0, len(pr.Assertions))
	for _, a := range pr.Assertions {
		if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(newDBIDs) {
			targetID := resolveAssertionTarget(a, incrNodePtrs, newDBIDs, incrNameToIDs, nil, nil)
			assertPtrs = append(assertPtrs, &store.Assertion{
				TestNodeID:   newDBIDs[a.TestNodeIdx],
				TargetNodeID: targetID,
				Kind:         a.Kind,
				Expression:   a.Expression,
				Expected:     a.Expected,
				Line:         a.Line,
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

// resolveAssertionTarget links an assertion to the production function it tests.
// Uses three strategies in priority order:
// 1. LCBA (Last-Call-Before-Assert): extract function name from assertion expression
// 2. Naming convention: test_foo → foo, TestFoo → Foo
// 3. Same-module fallback: unambiguous match within the tested module
var assertionCallPattern = regexp.MustCompile(`(\w+)\s*\(`)

// pickSamePackage selects the best candidate from multiple node IDs
// by preferring nodes in the same directory (package) as the test.
func pickSamePackage(ids []int64, allNodes []*store.Node, nodeDBIDs []int64, testDir string) int64 {
	if testDir == "" || len(ids) > 10 {
		return 0
	}
	// Normalize test directory variants
	testDirVariants := []string{testDir}
	for _, suffix := range []string{"/tests", "/test", "_test"} {
		trimmed := strings.TrimSuffix(testDir, suffix)
		if trimmed != testDir {
			testDirVariants = append(testDirVariants, trimmed)
		}
	}
	for _, prefix := range []string{"tests/", "test/"} {
		trimmed := strings.TrimPrefix(testDir, prefix)
		if trimmed != testDir {
			testDirVariants = append(testDirVariants, trimmed)
		}
	}
	// Also try parent directory (tests/unit/auth → auth)
	if parent := filepath.Base(testDir); parent != "." && parent != "/" {
		testDirVariants = append(testDirVariants, parent)
	}

	var matches []int64
	for _, id := range ids {
		for i, n := range allNodes {
			if i < len(nodeDBIDs) && nodeDBIDs[i] == id {
				nodeDir := filepath.Dir(n.FilePath)
				for _, variant := range testDirVariants {
					if nodeDir == variant || strings.HasSuffix(nodeDir, "/"+variant) ||
						filepath.Base(nodeDir) == filepath.Base(variant) {
						matches = append(matches, id)
						break
					}
				}
				break
			}
		}
	}
	if len(matches) == 1 {
		return matches[0]
	}
	return 0
}

func resolveAssertionTarget(
	a parser.AssertionRef,
	allNodes []*store.Node,
	nodeDBIDs []int64,
	nameToNodeIDs map[string][]int64,
	importIndex map[string]map[string][]string, // file → imported name → target files
	fileNodeIDs map[string]map[string][]int64, // file → func name → node DB IDs
) int64 {
	testDir := ""
	testFilePath := ""
	if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(allNodes) {
		testFilePath = allNodes[a.TestNodeIdx].FilePath
		testDir = filepath.Dir(testFilePath)
	}

	// Strategy 1.5: Import-guided — if test file imports a module that exports
	// a function matching a name in the assertion expression, resolve to that function.
	if a.Expression != "" && testFilePath != "" && importIndex != nil && fileNodeIDs != nil {
		if fileImports, ok := importIndex[testFilePath]; ok {
			candidates := extractCalledFunctions(a.Expression)
			for _, fname := range candidates {
				if targetFiles, ok := fileImports[fname]; ok {
					for _, targetFile := range targetFiles {
						if fnMap, ok := fileNodeIDs[targetFile]; ok {
							if ids, ok := fnMap[fname]; ok && len(ids) == 1 {
								return ids[0]
							}
						}
					}
				}
			}
		}
	}

	// Strategy 1: Extract called function from assertion expression
	// e.g. "assertEqual(get_user(99), None)" → "get_user"
	// e.g. "assert validate(token) == True" → "validate"
	if a.Expression != "" {
		candidates := extractCalledFunctions(a.Expression)
		for _, fname := range candidates {
			if ids, ok := nameToNodeIDs[fname]; ok {
				if len(ids) == 1 {
					return ids[0]
				}
				if best := pickSamePackage(ids, allNodes, nodeDBIDs, testDir); best > 0 {
					return best
				}
			}
		}
	}

	// Strategy 2: Naming convention from test function name
	// test_validate_user → validate_user
	// TestValidateUser → ValidateUser (Go convention)
	// test_X_something → X_something
	if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(allNodes) {
		testNode := allNodes[a.TestNodeIdx]
		targetName := deriveTargetFromTestName(testNode.Name)
		if targetName != "" {
			if ids, ok := nameToNodeIDs[targetName]; ok {
				if len(ids) == 1 {
					return ids[0]
				}
				if best := pickSamePackage(ids, allNodes, nodeDBIDs, testDir); best > 0 {
					return best
				}
			}
			// Try case-insensitive match for Go (TestFoo → foo)
			lower := strings.ToLower(targetName)
			for name, ids := range nameToNodeIDs {
				if strings.ToLower(name) == lower {
					if len(ids) == 1 {
						return ids[0]
					}
					if best := pickSamePackage(ids, allNodes, nodeDBIDs, testDir); best > 0 {
						return best
					}
				}
			}
		}
	}

	// Strategy 3: Same-module unambiguous match
	// If the test file imports exactly one module and that module has exactly one
	// function matching a name in the expression, use it
	if a.Expression != "" && a.TestNodeIdx >= 0 && a.TestNodeIdx < len(allNodes) {
		testNode := allNodes[a.TestNodeIdx]
		testDir := filepath.Dir(testNode.FilePath)
		candidates := extractCalledFunctions(a.Expression)
		for _, fname := range candidates {
			if ids, ok := nameToNodeIDs[fname]; ok {
				// Filter to same directory (same module heuristic)
				var sameDir []int64
				for _, id := range ids {
					for i, n := range allNodes {
						if i < len(nodeDBIDs) && nodeDBIDs[i] == id {
							if filepath.Dir(n.FilePath) == testDir ||
								filepath.Dir(n.FilePath) == strings.TrimSuffix(testDir, "_test") ||
								filepath.Dir(n.FilePath) == strings.TrimSuffix(testDir, "/tests") ||
								filepath.Dir(n.FilePath) == strings.TrimPrefix(testDir, "tests/") {
								sameDir = append(sameDir, id)
							}
							break
						}
					}
				}
				if len(sameDir) == 1 {
					return sameDir[0]
				}
			}
		}
	}

	return 0
}

func extractCalledFunctions(expr string) []string {
	// Extract function names from assertion expressions
	// Skip common assertion framework functions
	skip := map[string]bool{
		"assertEqual": true, "assertEquals": true, "assertNotEqual": true,
		"assertTrue": true, "assertFalse": true, "assertNone": true,
		"assertIsNone": true, "assertIsNotNone": true, "assertRaises": true,
		"assertIn": true, "assertNotIn": true, "assertIs": true,
		"assert_equal": true, "assert_raises": true, "assert_true": true,
		"expect": true, "assert": true, "require": true,
		"Equal": true, "NotEqual": true, "True": true, "False": true,
		"Nil": true, "NotNil": true, "Error": true, "NoError": true,
		"len": true, "str": true, "int": true, "list": true, "dict": true,
		"isinstance": true, "type": true, "print": true, "repr": true,
		"set": true, "tuple": true, "sorted": true, "range": true,
	}

	matches := assertionCallPattern.FindAllStringSubmatch(expr, -1)
	var result []string
	for _, m := range matches {
		name := m[1]
		if !skip[name] && len(name) > 1 && name[0] != '_' {
			result = append(result, name)
		}
	}
	return result
}

func deriveTargetFromTestName(testName string) string {
	// Python: test_validate_user → validate_user
	if strings.HasPrefix(testName, "test_") && len(testName) > 5 {
		return testName[5:]
	}
	// Go: TestValidateUser → ValidateUser
	if strings.HasPrefix(testName, "Test") && len(testName) > 4 {
		rest := testName[4:]
		if len(rest) > 0 && rest[0] >= 'A' && rest[0] <= 'Z' {
			return rest
		}
		// TestFoo → foo (lowercase first char)
		return strings.ToLower(rest[:1]) + rest[1:]
	}
	// Java: testValidateUser → validateUser
	if strings.HasPrefix(testName, "test") && len(testName) > 4 {
		rest := testName[4:]
		if len(rest) > 0 && rest[0] >= 'A' && rest[0] <= 'Z' {
			return strings.ToLower(rest[:1]) + rest[1:]
		}
	}
	return ""
}

// serdePairs defines common serialization/deserialization function name pairs.
// MSR community research: serialization pairs are a strong signal for behavioral
// contracts — modifying one side without the other is a common source of bugs.
var serdePairs = [][2]string{
	{"serialize", "deserialize"}, {"encode", "decode"}, {"marshal", "unmarshal"},
	{"to_json", "from_json"}, {"to_dict", "from_dict"}, {"dump", "load"},
	{"pack", "unpack"}, {"ToJSON", "FromJSON"}, {"ToMap", "FromMap"},
	{"String", "Parse"}, {"compress", "decompress"}, {"encrypt", "decrypt"},
}

// detectSerdePairs finds serialization/deserialization function pairs within
// the same file and class scope. When a pair is found, both functions get a
// "serialization_pair" property pointing to their partner.
func detectSerdePairs(db *store.DB, allNodes []*store.Node, nodeDBIDs []int64) int {
	// Group function nodes by (file_path, parent_id) — functions in the same
	// file and class/module scope are candidates for serde pairing.
	type nodeRef struct {
		name   string
		dbID   int64
		line   int
	}
	type groupKey struct {
		filePath string
		parentID int64
	}
	groups := make(map[groupKey][]nodeRef)
	for i, n := range allNodes {
		if i >= len(nodeDBIDs) {
			break
		}
		if n.Label == "Class" || n.Label == "Interface" || n.IsTest {
			continue
		}
		key := groupKey{filePath: n.FilePath, parentID: n.ParentID}
		groups[key] = append(groups[key], nodeRef{
			name: n.Name,
			dbID: nodeDBIDs[i],
			line: n.StartLine,
		})
	}

	var props []*store.Property
	for _, members := range groups {
		if len(members) < 2 {
			continue
		}
		for i := 0; i < len(members); i++ {
			for j := i + 1; j < len(members); j++ {
				a := members[i]
				b := members[j]
				if matchesSerdePair(a.name, b.name) {
					props = append(props, &store.Property{
						NodeID:     a.dbID,
						Kind:       "serialization_pair",
						Value:      fmt.Sprintf("partner:%s@file:%d", b.name, b.line),
						Line:       a.line,
						Confidence: 0.8,
					})
					props = append(props, &store.Property{
						NodeID:     b.dbID,
						Kind:       "serialization_pair",
						Value:      fmt.Sprintf("partner:%s@file:%d", a.name, a.line),
						Line:       b.line,
						Confidence: 0.8,
					})
				}
			}
		}
	}

	if len(props) > 0 {
		if err := db.BatchInsertProperties(props); err != nil {
			log.Printf("WARNING: serde pair properties: %v", err)
		}
	}
	return len(props)
}

// matchesSerdePair checks whether two function names form a serialization pair
// using case-insensitive substring matching against known serde patterns.
func matchesSerdePair(nameA, nameB string) bool {
	lowerA := strings.ToLower(nameA)
	lowerB := strings.ToLower(nameB)
	for _, pair := range serdePairs {
		pairLo0 := strings.ToLower(pair[0])
		pairLo1 := strings.ToLower(pair[1])
		if (strings.Contains(lowerA, pairLo0) && strings.Contains(lowerB, pairLo1)) ||
			(strings.Contains(lowerA, pairLo1) && strings.Contains(lowerB, pairLo0)) {
			return true
		}
	}
	return false
}

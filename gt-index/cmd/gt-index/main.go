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
	"io"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"

	// Import all language specs (their init() functions register them)
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

// fileParseResult holds the output of parsing a single file.
type fileParseResult struct {
	fileIdx int
	result  *parser.ParseResult
	err     error
}

// fileHash computes SHA-256 of a file's contents.
func fileHash(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func main() {
	root := flag.String("root", ".", "Project root directory")
	output := flag.String("output", "graph.db", "Output SQLite database path")
	maxFiles := flag.Int("max-files", 10000, "Maximum files to index")
	workers := flag.Int("workers", 0, "Parallel parse workers (0 = NumCPU)")
	incremental := flag.Bool("incremental", false, "Incremental mode: only re-index changed files")
	filesFlag := flag.String("files", "", "Comma-separated file paths to re-index (incremental mode)")
	flag.Parse()

	if *workers <= 0 {
		*workers = runtime.NumCPU()
	}

	// Incremental mode: re-index only specified files
	if *incremental && *filesFlag != "" {
		runIncremental(*root, *output, *filesFlag, *workers)
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
	db.SetMeta("build_time_ms", fmt.Sprintf("%d", elapsed.Milliseconds()))
	db.SetMeta("file_count", fmt.Sprintf("%d", len(files)))
	db.SetMeta("node_count", fmt.Sprintf("%d", len(allNodePtrs)))
	db.SetMeta("edge_count", fmt.Sprintf("%d", len(resolved)))
	db.SetMeta("import_count", fmt.Sprintf("%d", len(allImports)))
	db.SetMeta("property_count", fmt.Sprintf("%d", len(propPtrs)))
	db.SetMeta("assertion_count", fmt.Sprintf("%d", len(assertPtrs)))
	db.SetMeta("indexer_version", "v16-multilang")
	db.SetMeta("workers", fmt.Sprintf("%d", *workers))

	// Summary
	fmt.Fprintf(os.Stderr, "\nDone in %s\n", elapsed.Round(time.Millisecond))
	fmt.Fprintf(os.Stderr, "  Files:      %d\n", len(files))
	fmt.Fprintf(os.Stderr, "  Nodes:      %d\n", db.NodeCount())
	fmt.Fprintf(os.Stderr, "  Edges:      %d\n", db.EdgeCount())
	fmt.Fprintf(os.Stderr, "  Imports:    %d\n", len(allImports))
	fmt.Fprintf(os.Stderr, "  Properties: %d\n", db.PropertyCount())
	fmt.Fprintf(os.Stderr, "  Assertions: %d\n", db.AssertionCount())
	fmt.Fprintf(os.Stderr, "  Workers:    %d\n", *workers)
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

// runIncremental re-indexes only the specified files in an existing graph.db.
// For each file: compare hash → delete old data → re-parse → re-insert → re-resolve.
func runIncremental(root, output, filesCSV string, numWorkers int) {
	start := time.Now()
	absRoot, _ := filepath.Abs(root)

	// Check DB exists
	if _, err := os.Stat(output); os.IsNotExist(err) {
		log.Fatalf("incremental mode requires existing DB at %s — run full index first", output)
	}

	db, err := store.Open(output)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer db.Close()

	// Parse file list
	filePaths := strings.Split(filesCSV, ",")
	var changedFiles []string

	for _, fp := range filePaths {
		fp = strings.TrimSpace(fp)
		if fp == "" {
			continue
		}
		// Compute hash and compare
		absPath := filepath.Join(absRoot, filepath.FromSlash(fp))
		hash, err := fileHash(absPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "  skip %s (cannot hash: %v)\n", fp, err)
			continue
		}

		oldHash := db.GetFileHash(fp)
		if hash == oldHash {
			fmt.Fprintf(os.Stderr, "  skip %s (unchanged)\n", fp)
			continue
		}

		// Delete old nodes/edges/properties/assertions for this file
		deletedIDs, err := db.DeleteNodesByFile(fp)
		if err != nil {
			fmt.Fprintf(os.Stderr, "  WARNING: delete old data for %s: %v\n", fp, err)
		} else {
			fmt.Fprintf(os.Stderr, "  deleted %d old nodes for %s\n", len(deletedIDs), fp)
		}

		changedFiles = append(changedFiles, fp)

		// Update file hash
		ext := filepath.Ext(fp)
		lang := ""
		if spec := specs.ForExtension(ext); spec != nil {
			lang = spec.Name
		}
		db.InsertFileHash(fp, hash, lang)
	}

	if len(changedFiles) == 0 {
		elapsed := time.Since(start)
		fmt.Fprintf(os.Stderr, "No files changed (%s)\n", elapsed.Round(time.Millisecond))
		fmt.Printf(`{"incremental":true,"files":0,"changed":0,"time_ms":%d}`, elapsed.Milliseconds())
		fmt.Println()
		return
	}

	// Discover SourceFile structs for changed files only
	files, err := walker.WalkFiles(absRoot, changedFiles)
	if err != nil {
		log.Fatalf("walk files: %v", err)
	}

	fmt.Fprintf(os.Stderr, "Incremental: re-indexing %d changed file(s)...\n", len(files))

	// Parse changed files
	var allNodePtrs []*store.Node
	var allCalls []parser.CallRef
	var allImports []parser.ImportRef
	var allProps []parser.PropertyRef
	var allAssertions []parser.AssertionRef
	callerNodeIndexMap := make(map[int]int)

	globalNodeIdx := 0
	for _, sf := range files {
		isTest := walker.IsTestFile(sf.Path)
		result, err := parser.ParseFile(sf, isTest)
		if err != nil || result == nil {
			continue
		}
		fileNodeStartIdx := globalNodeIdx
		for i := range result.Nodes {
			node := &result.Nodes[i]
			if node.ParentID > 0 {
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

	// Fix parent IDs
	parentFixups := make(map[int]int64)
	for i, n := range allNodePtrs {
		if n.ParentID > 0 {
			parentFixups[i] = n.ParentID
			n.ParentID = 0
		}
	}

	// Batch insert new nodes
	nodeDBIDs, err := db.BatchInsertNodes(allNodePtrs)
	if err != nil {
		log.Fatalf("batch insert nodes: %v", err)
	}

	// Fix parent IDs
	for nodeIdx, parentGlobalIdx := range parentFixups {
		pidx := int(parentGlobalIdx) - 1
		if pidx >= 0 && pidx < len(nodeDBIDs) {
			parentDBID := nodeDBIDs[pidx]
			if parentDBID > 0 {
				db.UpdateParentID(nodeDBIDs[nodeIdx], parentDBID)
			}
		}
	}

	fmt.Fprintf(os.Stderr, "  Inserted %d new nodes\n", len(nodeDBIDs))

	// Load the FULL node universe from graph.db for cross-file resolution.
	// Without this, calls from changed file A to unchanged file B fail resolution.
	fullNodes, fullIDs, err := db.GetAllNodes()
	if err != nil {
		log.Fatalf("load full node universe: %v", err)
	}
	fmt.Fprintf(os.Stderr, "  Full node universe: %d nodes\n", len(fullNodes))

	// Build resolution indexes from the full database state
	nameIndex, fileIndex := resolver.BuildNameIndex(db, fullNodes, fullIDs)

	// Build file map from ALL file paths in the database, not just changed files
	allFilePaths, allFileLangs := db.GetAllFilePaths()
	fileMap := resolver.BuildFileMap(allFilePaths, allFileLangs)

	// Build caller ID list
	callerDBIDs := make([]int64, len(allCalls))
	for i := range allCalls {
		if globalIdx, ok := callerNodeIndexMap[i]; ok && globalIdx < len(nodeDBIDs) {
			callerDBIDs[i] = nodeDBIDs[globalIdx]
		}
	}

	resolved := resolver.Resolve(allCalls, nameIndex, fileIndex, callerDBIDs, allImports, fileMap)

	// Batch insert edges
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
		log.Printf("WARNING: batch insert edges: %v", err)
	}

	// Insert properties
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

	// Insert assertions
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

	elapsed := time.Since(start)
	fmt.Fprintf(os.Stderr, "Incremental done in %s — %d files, %d nodes, %d edges\n",
		elapsed.Round(time.Millisecond), len(changedFiles), len(nodeDBIDs), len(edgePtrs))

	fmt.Printf(`{"incremental":true,"files":%d,"changed":%d,"nodes":%d,"edges":%d,"time_ms":%d}`,
		len(filePaths), len(changedFiles), len(nodeDBIDs), len(edgePtrs), elapsed.Milliseconds())
	fmt.Println()
}

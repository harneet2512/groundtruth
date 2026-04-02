// gt-index: Multi-language code graph indexer using tree-sitter.
//
// Builds a SQLite graph database from source code. Supports Python, Go,
// JavaScript, TypeScript, Rust, Java via tree-sitter grammars.
//
// v13: Import-based edge resolution. Parses import statements and resolves
// cross-file calls through verified import paths (resolution_method='import').
//
// Usage:
//
//	gt-index --root=/path/to/repo --output=/tmp/gt_graph.db
package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"

	// Import all language specs (their init() functions register them)
	_ "github.com/harneet2512/groundtruth/gt-index/internal/specs"
)

func main() {
	root := flag.String("root", ".", "Project root directory")
	output := flag.String("output", "graph.db", "Output SQLite database path")
	maxFiles := flag.Int("max-files", 10000, "Maximum files to index")
	flag.Parse()

	start := time.Now()

	// Remove old DB if it exists
	os.Remove(*output)

	// Open database
	db, err := store.Open(*output)
	if err != nil {
		log.Fatalf("open db: %v", err)
	}
	defer db.Close()

	// Pass 1: STRUCTURE — discover files
	fmt.Fprintf(os.Stderr, "Pass 1: discovering files in %s...\n", *root)
	files, err := walker.Walk(*root, *maxFiles)
	if err != nil {
		log.Fatalf("walk: %v", err)
	}
	fmt.Fprintf(os.Stderr, "  Found %d source files\n", len(files))

	// Count by language
	langCount := make(map[string]int)
	for _, f := range files {
		langCount[f.Language]++
	}
	for lang, count := range langCount {
		fmt.Fprintf(os.Stderr, "  %s: %d files\n", lang, count)
	}

	// Pass 2: DEFINITIONS + IMPORTS — parse files, extract symbols and import statements
	fmt.Fprintf(os.Stderr, "Pass 2: extracting definitions + imports...\n")
	var allNodes []store.Node
	var allCalls []parser.CallRef
	var allImports []parser.ImportRef
	var nodeDBIDs []int64
	callerNodeIndexMap := make(map[int]int) // maps call index to global node index

	// Collect file paths and languages for BuildFileMap
	var filePaths []string
	var fileLangs []string
	for _, sf := range files {
		filePaths = append(filePaths, sf.Path)
		fileLangs = append(fileLangs, sf.Language)
	}

	globalNodeIdx := 0
	for _, sf := range files {
		isTest := walker.IsTestFile(sf.Path)
		result, err := parser.ParseFile(sf, isTest)
		if err != nil {
			continue // skip unparseable files
		}

		fileNodeStartIdx := globalNodeIdx
		for _, node := range result.Nodes {
			id, err := db.InsertNode(&node)
			if err != nil {
				continue
			}
			nodeDBIDs = append(nodeDBIDs, id)
			allNodes = append(allNodes, node)
			globalNodeIdx++
		}

		// Map call references to global node indices
		for _, call := range result.Calls {
			globalCallerIdx := fileNodeStartIdx + call.CallerNodeIdx
			allCalls = append(allCalls, call)
			callerNodeIndexMap[len(allCalls)-1] = globalCallerIdx
		}

		// Collect import references
		allImports = append(allImports, result.Imports...)
	}

	fmt.Fprintf(os.Stderr, "  Extracted %d definitions, %d imports\n", len(allNodes), len(allImports))

	// Pass 3: CALLS — resolve references (now with import-based resolution)
	fmt.Fprintf(os.Stderr, "Pass 3: resolving %d call references...\n", len(allCalls))
	nameIndex, fileIndex := resolver.BuildNameIndex(db, allNodes, nodeDBIDs)

	// Build file map for import resolution: module paths → file paths
	fileMap := resolver.BuildFileMap(filePaths, fileLangs)

	// Build caller ID list parallel to allCalls
	callerDBIDs := make([]int64, len(allCalls))
	for i := range allCalls {
		if globalIdx, ok := callerNodeIndexMap[i]; ok && globalIdx < len(nodeDBIDs) {
			callerDBIDs[i] = nodeDBIDs[globalIdx]
		}
	}

	resolved := resolver.Resolve(allCalls, nameIndex, fileIndex, callerDBIDs, allImports, fileMap)

	// Count by resolution method
	methodCounts := make(map[string]int)
	for _, rc := range resolved {
		methodCounts[rc.Method]++
	}
	fmt.Fprintf(os.Stderr, "  Resolved %d/%d calls", len(resolved), len(allCalls))
	for method, count := range methodCounts {
		fmt.Fprintf(os.Stderr, " [%s:%d]", method, count)
	}
	fmt.Fprintln(os.Stderr)

	// Insert edges
	for _, rc := range resolved {
		db.InsertEdge(&store.Edge{
			SourceID:         rc.SourceNodeID,
			TargetID:         rc.TargetNodeID,
			Type:             "CALLS",
			SourceLine:       rc.SourceLine,
			SourceFile:       rc.SourceFile,
			ResolutionMethod: rc.Method,
			Confidence:       rc.Confidence,
		})
	}

	// Pass 4: EXTRAS — store metadata
	fmt.Fprintf(os.Stderr, "Pass 4: storing metadata...\n")
	elapsed := time.Since(start)
	db.SetMeta("root", *root)
	db.SetMeta("build_time_ms", fmt.Sprintf("%d", elapsed.Milliseconds()))
	db.SetMeta("file_count", fmt.Sprintf("%d", len(files)))
	db.SetMeta("node_count", fmt.Sprintf("%d", len(allNodes)))
	db.SetMeta("edge_count", fmt.Sprintf("%d", len(resolved)))
	db.SetMeta("import_count", fmt.Sprintf("%d", len(allImports)))
	db.SetMeta("indexer_version", "v14-confidence")

	// Summary
	fmt.Fprintf(os.Stderr, "\nDone in %s\n", elapsed.Round(time.Millisecond))
	fmt.Fprintf(os.Stderr, "  Files:   %d\n", len(files))
	fmt.Fprintf(os.Stderr, "  Nodes:   %d\n", db.NodeCount())
	fmt.Fprintf(os.Stderr, "  Edges:   %d\n", db.EdgeCount())
	fmt.Fprintf(os.Stderr, "  Imports: %d\n", len(allImports))
	fmt.Fprintf(os.Stderr, "  Output:  %s\n", *output)

	// Print JSON summary to stdout for programmatic use
	importResolved := methodCounts["import"]
	sameFileResolved := methodCounts["same_file"]
	nameMatchResolved := methodCounts["name_match"]
	fmt.Printf(`{"files":%d,"nodes":%d,"edges":%d,"imports":%d,"edges_import":%d,"edges_same_file":%d,"edges_name_match":%d,"time_ms":%d}`,
		len(files), db.NodeCount(), db.EdgeCount(), len(allImports),
		importResolved, sameFileResolved, nameMatchResolved,
		elapsed.Milliseconds())
	fmt.Println()
}

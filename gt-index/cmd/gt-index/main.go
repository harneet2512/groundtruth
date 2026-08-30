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
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/harneet2512/groundtruth/gt-index/internal/closure"
	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
	// Note: specs is imported above (named); its init() functions register all language specs.
)

// RC-17 (F-003): build-stamp variables. Populated at link time via
//
//	go build -ldflags='-X main.commitSHA=... -X main.buildTimeUTC=... -X main.goToolchain=...'
//
// Defaults of "unknown" let `go run` and bare `go build` still produce a
// usable binary for development (the smoke-runner preflight refuses
// "unknown" so paid runs cannot ship with an unstamped binary).
//
// TODO(RC-17-build): rebuild on Linux host with the build script — this
// Windows worktree cannot regenerate bin/gt-index-linux.
var (
	commitSHA         = "unknown"
	buildTimeUTC      = "unknown"
	goToolchain       = "unknown"
	sourceFingerprint = "unknown"
	compiledBuildTags = "unknown"
)

func repoCommit(root string) string {
	cmd := exec.Command("git", "-C", root, "rev-parse", "HEAD")
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func repositoryIdentity(root string) string {
	out, err := exec.Command("git", "-C", root, "config", "--get", "remote.origin.url").Output()
	identity := strings.TrimSpace(string(out))
	if err != nil || identity == "" {
		identity = filepath.Base(filepath.Clean(root))
	}
	identity = strings.ToLower(strings.TrimSuffix(strings.ReplaceAll(identity, "\\", "/"), ".git"))
	sum := sha256.Sum256([]byte(identity))
	return hex.EncodeToString(sum[:])
}

func qualifiedCallChain(qualified string) []string {
	if qualified == "" {
		return []string{}
	}
	parts := strings.FieldsFunc(qualified, func(r rune) bool { return r == '.' || r == ':' })
	if len(parts) <= 1 {
		return []string{}
	}
	return parts[:len(parts)-1]
}

func setRequiredMetadata(db *store.DB, values map[string]string) error {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if err := db.SetMeta(key, values[key]); err != nil {
			return fmt.Errorf("set required metadata %s: %w", key, err)
		}
	}
	return nil
}

// FINAL_ARCH_V2 schema contract.
// Bump when edges/nodes columns change; Python readers gate on >= this.
const schemaVersion = "v15.2-trust-tier"

// fileParseResult holds the output of parsing a single file.
type fileParseResult struct {
	fileIdx int
	result  *parser.ParseResult
	err     error
}

func collectParseResults(files []walker.SourceFile, resultCh <-chan fileParseResult) ([]*parser.ParseResult, int, []string) {
	results := make([]*parser.ParseResult, len(files))
	parseFailures := 0
	var failSample []string
	for pr := range resultCh {
		if pr.err == nil && pr.result != nil && !pr.result.ParserIncomplete {
			results[pr.fileIdx] = pr.result
			continue
		}
		parseFailures++
		if pr.err == nil && pr.result != nil {
			// Keep recoverable partial AST facts, but account for the damaged
			// source and let ParserIncomplete suppress target authority.
			results[pr.fileIdx] = pr.result
		}
		if len(failSample) >= 10 || pr.fileIdx < 0 || pr.fileIdx >= len(files) {
			continue
		}
		if pr.err != nil {
			failSample = append(failSample, fmt.Sprintf("%s: %v", files[pr.fileIdx].Path, pr.err))
		} else if pr.result != nil && pr.result.ParserIncomplete {
			failSample = append(failSample, fmt.Sprintf("%s: parser-incomplete syntax tree", files[pr.fileIdx].Path))
		} else {
			failSample = append(failSample, fmt.Sprintf("%s: parser returned no result", files[pr.fileIdx].Path))
		}
	}
	return results, parseFailures, failSample
}

func main() {
	root := flag.String("root", ".", "Project root directory")
	output := flag.String("output", "graph.db", "Output SQLite database path")
	maxFiles := flag.Int("max-files", 10000, "Maximum files to index")
	workers := flag.Int("workers", 0, "Parallel parse workers (0 = NumCPU)")
	file := flag.String("file", "", "Incremental mode: re-index only this single file (relative to -root) into an existing -output graph.db")
	closureEnabled := flag.Bool("closure", true, "C7: compute the transitive-closure sidecar over VERIFIED CALLS edges (default on)")
	rebuildClosure := flag.Bool("rebuild-closure", false, "Recompute the closure sidecar on an existing -output graph.db over its CURRENT edges. Run AFTER the LSP resolve pass so the closure reflects LSP-promoted/re-pointed/deleted edges (it is built once at index time and goes stale otherwise). Clears the old closure first.")
	buildInfo := flag.Bool("build-info", false, "Print the gt-index.build.v1 binary identity JSON and exit")
	flag.Parse()
	if *buildInfo {
		if err := writeBuildInfo(os.Stdout); err != nil {
			log.Fatalf("build-info: %v", err)
		}
		return
	}

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

	// Closure-rebuild mode: recompute the transitive-closure sidecar over the
	// CURRENT edges of an existing graph.db, WITHOUT re-indexing. The closure is
	// built once at full-index time (Pass 4e), but the LSP resolve pass
	// (resolve.py) runs afterward and promotes/re-points/deletes edges — leaving
	// the closure stale: missing LSP-verified deep reach AND retaining reach
	// through edges LSP later disproved. This pass clears the stale closure and
	// recomputes it over the corrected edges (verified-only, depth-bounded —
	// the same RF-4 rules), so impact/trace/localization see LSP-accurate reach.
	if *rebuildClosure {
		db, err := store.Open(*output)
		if err != nil {
			log.Fatalf("rebuild-closure: open %s: %v", *output, err)
		}
		defer db.Close()
		before := db.ClosureCount()
		if err := db.ClearClosure(); err != nil {
			log.Fatalf("rebuild-closure: clear: %v", err)
		}
		n, cerr := closure.ComputeTransitiveClosure(db, "CALLS", closure.MaxDepth, closure.MinEdgeConfidence)
		if cerr != nil {
			log.Fatalf("rebuild-closure: compute: %v", cerr)
		}
		fmt.Fprintf(os.Stderr, "rebuild-closure: %d -> %d closure rows (recomputed over current/LSP-corrected edges)\n", before, n)
		// rebuild-closure is the LAST gt-index pass — it runs AFTER `groundtruth.resolve`
		// (which UPDATEs nodes.signature/return_type) and, in the DeepSWE flow, on a
		// graph.db that was `docker cp`'d between containers (which drops the -wal). Both
		// leave the external-content nodes_fts index STALE/desynced (COUNT reads `nodes` so
		// looks full, but MATCH returns 0). Rebuild the index from the CURRENT nodes here,
		// then checkpoint so graph.db is self-contained for the preflight + artifact copy.
		if err := db.PopulateFTS5(); err != nil {
			log.Printf("[WARN] rebuild-closure: FTS5 re-population failed: %v", err)
		}
		db.CheckpointWAL()
		return
	}

	start := time.Now()

	// Full builds are assembled beside the requested output and published only
	// after every pass, integrity check, and WAL checkpoint succeeds. The previous
	// complete graph therefore remains authoritative if parsing, indexing, or
	// finalization terminates early.
	requestedOutput := *output
	stagedOutput, err := createStagedOutput(requestedOutput)
	if err != nil {
		log.Fatalf("create staged output: %v", err)
	}
	published := false
	defer func() {
		if !published {
			cleanupStagedOutput(stagedOutput)
		}
	}()
	*output = stagedOutput

	// Open database
	db, err := store.Open(stagedOutput)
	if err != nil {
		abortStagedBuild(nil, stagedOutput, "open db: %v", err)
	}
	defer db.Close()

	// ── Pass 1: STRUCTURE — discover files ──────────────────────────────
	fmt.Fprintf(os.Stderr, "Pass 1: discovering files in %s...\n", *root)
	files, err := walker.Walk(*root, *maxFiles)
	if err != nil {
		abortStagedBuild(db, stagedOutput, "walk: %v", err)
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
				// Mark test AND non-source (benchmark/example/fixture/docs/vendored)
				// nodes is_test so their call edges stay OUT of the fact surface.
				isTest := walker.IsTestFile(sf.Path) || walker.IsNonSourceFile(sf.Path)
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

	// Collect results and preserve parser failures in project_meta.
	results, parseFailures, failSample := collectParseResults(files, resultCh)

	parseElapsed := time.Since(parseStart)
	parsedOK := len(files) - parseFailures
	failRate := 0.0
	if len(files) > 0 {
		failRate = float64(parseFailures) / float64(len(files))
	}
	fmt.Fprintf(os.Stderr, "  Parsed %d/%d files in %s (%d parse failures, %.1f%%)\n", parsedOK, len(files), parseElapsed.Round(time.Millisecond), parseFailures, failRate*100)
	if parseFailures > 0 {
		fmt.Fprintf(os.Stderr, "  [WARN] parse failures (first %d):\n", len(failSample))
		for _, sample := range failSample {
			fmt.Fprintf(os.Stderr, "    - %s\n", sample)
		}
	}
	if len(files) > 0 && parsedOK == 0 {
		abortStagedBuild(db, stagedOutput, "INDEX FAILED: 0/%d files parsed — graph would be empty (sample: %v)", len(files), failSample)
	}
	if requiredRate := os.Getenv("GT_REQUIRE_PARSE_RATE"); requiredRate != "" {
		var minimumRate float64
		if _, err := fmt.Sscanf(requiredRate, "%f", &minimumRate); err != nil {
			abortStagedBuild(db, stagedOutput, "GT_REQUIRE_PARSE_RATE=%q is not a valid number: %v", requiredRate, err)
		}
		if minimumRate > 0 && len(files) >= 20 && (1.0-failRate) < minimumRate {
			abortStagedBuild(db, stagedOutput, "GT_REQUIRE_PARSE_RATE=%.2f but only %.1f%% of %d files parsed — index too thin, failing closed (sample: %v)", minimumRate, (1.0-failRate)*100, len(files), failSample)
		}
	}

	// Collect all nodes for batch insert
	var allNodePtrs []*store.Node
	var allCalls []parser.CallRef
	var allImports []parser.ImportRef
	var allProps []parser.PropertyRef
	var allAssertions []parser.AssertionRef
	var allAssignments []parser.AssignmentRef
	var allModDecls []parser.ModDecl
	var allReExports []parser.ReExportRef
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
		allModDecls = append(allModDecls, result.ModDecls...)
		allReExports = append(allReExports, result.ReExports...)
		// PyCG Rule 1: collect variable assignments for type tracking
		for _, asgn := range result.Assignments {
			allAssignments = append(allAssignments, asgn)
		}
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
		abortStagedBuild(db, stagedOutput, "batch insert nodes: %v", err)
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

	// Populate FTS5 virtual table AFTER all nodes are inserted. The localizer
	// queries nodes_fts for BM25 retrieval — gives GT at least grep-grade recall
	// with structural ranking on top. Non-fatal: if FTS5 fails (e.g. SQLite
	// compiled without FTS5), the Python reader falls back to name-match seeding.
	if err := db.PopulateFTS5(); err != nil {
		log.Printf("WARNING: FTS5 population failed: %v", err)
	}
	// GT_REQUIRE_FTS5 preflight gate: on a paid benchmark we must NOT silently
	// degrade to the Python name-match fallback. If FTS5 isn't a real, populated
	// index, abort the index build so the run never starts. n<=0 means the binary
	// was built without `-tags sqlite_fts5` (FTS5 compiled out → nodes_fts absent).
	if os.Getenv("GT_REQUIRE_FTS5") == "1" {
		if n := db.FTS5RowCount(); n <= 0 {
			abortStagedBuild(db, stagedOutput, "GT_REQUIRE_FTS5=1 but nodes_fts has %d rows — FTS5 is not compiled in. "+
				"Rebuild gt-index with `-tags sqlite_fts5`. Aborting to avoid a degraded paid run.", n)
		} else {
			fmt.Fprintf(os.Stderr, "[GT preflight] FTS5 OK: nodes_fts populated (%d rows)\n", n)
		}
	}

	insertElapsed := time.Since(insertStart)
	fmt.Fprintf(os.Stderr, "  Inserted %d nodes in %s\n", len(nodeDBIDs), insertElapsed.Round(time.Millisecond))

	fmt.Fprintf(os.Stderr, "  Extracted %d definitions, %d imports\n", len(allNodePtrs), len(allImports))

	// ── Pass 3: CALLS — resolve references ──────────────────────────────
	resolveStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 3: resolving %d call references...\n", len(allCalls))

	// Build indexes from collected nodes (not from DB queries)
	// Restore ParentID from parentFixups (it was zeroed before batch insert)
	for nodeIdx, parentGlobalIdx := range parentFixups {
		pidx := int(parentGlobalIdx) - 1
		if pidx >= 0 && pidx < len(nodeDBIDs) && nodeIdx < len(allNodePtrs) {
			allNodePtrs[nodeIdx].ParentID = nodeDBIDs[pidx]
		}
	}
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
	resolver.RegisterJSPackagePaths(fileMap, *root)

	// Register Go package names as fileMap aliases + vendor paths
	resolver.RegisterGoPackageNames(fileMap, filePaths, fileLangs)
	resolver.RegisterGoVendorPaths(fileMap)

	// Register Rust crate names from Cargo.toml
	resolver.RegisterRustCratePaths(fileMap, *root)

	// Rust: build module tree from mod declarations (mod foo;) and register
	// module paths in fileMap. This bridges the gap between filesystem paths
	// and Rust's module tree, which is defined by explicit mod declarations.
	if len(allModDecls) > 0 {
		modTreeCount := resolver.BuildRustModuleTree(fileMap, allModDecls, filePaths, fileLangs, *root)
		if modTreeCount > 0 {
			fmt.Fprintf(os.Stderr, "  Rust module tree: %d module paths registered from %d mod declarations\n", modTreeCount, len(allModDecls))
		}
	}

	// Re-export chaining: register barrel/re-exporting files' fileMap keys as
	// also pointing to the source files. Covers TS/JS barrel files, Rust pub use,
	// and Python __init__.py re-exports.
	if len(allReExports) > 0 {
		chainCount := resolver.ChainReExports(fileMap, allReExports, filePaths, fileLangs)
		if chainCount > 0 {
			fmt.Fprintf(os.Stderr, "  Re-export chaining: %d aliases from %d re-exports\n", chainCount, len(allReExports))
		}
	}

	// Build caller ID list
	callerDBIDs := make([]int64, len(allCalls))
	for i := range allCalls {
		if globalIdx, ok := callerNodeIndexMap[i]; ok && globalIdx < len(nodeDBIDs) {
			callerDBIDs[i] = nodeDBIDs[globalIdx]
		}
	}

	nodeMeta := resolver.BuildNodeMeta(allNodes, nodeDBIDs)

	// T1 Step 1: build the declared-type (param/field) receiver index for Strategy
	// 1.94a. Reuses the `param` properties already extracted (no re-parse): caller
	// node DB id -> {paramName -> declared type}. allProps' NodeIdx is already global
	// (converted in Pass 2) and parallel to nodeDBIDs. Resolves REAL internal method
	// calls whose receiver is a typed parameter/field (e.g. `command.run()` where the
	// param is `command: Command`) — name_match -> type_flow, not exclusion.
	if len(allProps) > 0 {
		ptIdx := resolver.BuildParamTypeIndex(allProps, nodeDBIDs)
		resolver.SetParamTypeIndex(ptIdx)
		fmt.Fprintf(os.Stderr, "  Declared-type receivers: %d callers with typed params\n", len(ptIdx))

		// Strategy 2b: declared-FIELD-type receiver index from the SAME `class_field`
		// properties (no re-parse): class node DB id -> {fieldName -> declared type}.
		// Resolves `self.<field>.method()` whose field is annotation-only (not locally
		// assigned) — the fact promote.go reads then discards at the colon. allProps'
		// NodeIdx is global (Pass 2) and parallel to nodeDBIDs, same as the param index.
		ftIdx := resolver.BuildFieldTypeIndex(allProps, nodeDBIDs)
		resolver.SetFieldTypeIndex(ftIdx)
		fmt.Fprintf(os.Stderr, "  Declared-type fields: %d classes with typed fields\n", len(ftIdx))

		// GAP C: build the constructor-return-shape index for the Strategy 1.96/1.97
		// return-type FALLBACK. Reuses the `return_shape` properties (no re-parse): a
		// factory func whose body returns a bare constructor (`ClassName(...)` /
		// `&Struct{...}`) gets that class as its inferred return type so `x := factory();
		// x.M()` / `factory().M()` resolves even when the parser captured no declared
		// return annotation. classNames is the set of internal class-like node names —
		// a constructor not naming one is dropped (correct-or-quiet).
		classNames := make(map[string]bool)
		for _, m := range nodeMeta {
			if m.Label == "Class" || m.Label == "Struct" || m.Label == "Type" ||
				m.Label == "Enum" || m.Label == "Interface" {
				classNames[m.Name] = true
			}
		}
		rsIdx := resolver.BuildReturnShapeIndex(allProps, nodeDBIDs, classNames)
		resolver.SetReturnShapeIndex(rsIdx)
		fmt.Fprintf(os.Stderr, "  Return-shape receivers: %d factories with constructor returns\n", len(rsIdx))
	}

	// PyCG Step 1: build assignment index for Strategy 1.96
	if len(allAssignments) > 0 {
		asgnIdx := resolver.BuildAssignmentIndex(allAssignments)
		resolver.SetAssignmentIndex(asgnIdx)
		fmt.Fprintf(os.Stderr, "  Assignment tracking: %d assignments in %d files\n", len(allAssignments), len(asgnIdx))
	}

	// Build inheritance map for Strategy 1.75 (inherited method resolution)
	inhMap := buildInheritanceMap(files, *root, nameIndex, nodeMeta)
	if len(inhMap) > 0 {
		resolver.SetInheritanceMap(inhMap)
		fmt.Fprintf(os.Stderr, "  Inheritance chains: %d classes with parents\n", len(inhMap))
	}

	// Rust: expand crate:: → actual crate name in import paths. Each Rust file's
	// `crate::module` refers to the crate that owns that file (determined by which
	// Cargo.toml workspace member directory contains it). Without this substitution,
	// `crate::extract` in a file owned by `axum-core` doesn't match the fileMap key
	// `axum_core::extract`. This is the missing link that caused 1574 imports → 10
	// resolved edges on axum. Language-agnostic in the resolver; the expansion runs
	// once in the caller (main.go) before Resolve sees the imports.
	resolver.ExpandRustCrateImports(allImports, filePaths, fileLangs, *root)

	resolved, callsites := resolver.ResolveWithProvenance(allCalls, nameIndex, fileIndex, callerDBIDs, allImports, fileMap, nodeMeta)
	// CHA→RTA is an additive, score-free hierarchy pass. The default boundary
	// is open; callers may explicitly provide a closed repository boundary with
	// GT_HIERARCHY_CLOSED=1. In either case its coverage is published alongside
	// the existing resolver evidence, so an open hierarchy cannot be accepted
	// as a closed target set by downstream queries.
	hierarchyClosed := os.Getenv("GT_HIERARCHY_CLOSED") == "1"
	receiverTypes := make(map[int]string, len(callsites))
	resolverCandidates := make([][]int64, len(allCalls))
	for _, callsite := range callsites {
		receiverTypes[callsite.CallsiteOrdinal] = callsite.ReceiverType
		if callsite.CallsiteOrdinal >= 0 && callsite.CallsiteOrdinal < len(resolverCandidates) {
			resolverCandidates[callsite.CallsiteOrdinal] = append([]int64(nil), callsite.CandidateNodeIDs...)
		}
	}
	hierarchyResults := resolver.AnalyzeCHAThenRTAWithReachability(allCalls, nodeMeta, inhMap, allAssignments, receiverTypes, callerDBIDs, resolverCandidates, hierarchyClosed)
	hierarchyByOrdinal := make(map[int]resolver.CHAThenRTAResult, len(hierarchyResults))
	for _, result := range hierarchyResults {
		hierarchyByOrdinal[result.CallsiteOrdinal] = result
	}
	vtaResults := resolver.AnalyzeVTA(allCalls, nodeMeta, inhMap, allAssignments)
	vtaByOrdinal := make(map[int]resolver.VTAResult, len(vtaResults))
	for _, result := range vtaResults {
		vtaByOrdinal[result.CallsiteOrdinal] = result
	}
	mergeIDs := func(left, right []int64) []int64 {
		seen := make(map[int64]struct{}, len(left)+len(right))
		out := make([]int64, 0, len(left)+len(right))
		for _, id := range append(append([]int64(nil), left...), right...) {
			if _, ok := seen[id]; ok {
				continue
			}
			seen[id] = struct{}{}
			out = append(out, id)
		}
		sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
		return out
	}

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

	// Begin one publication transaction for legacy selected edges, attached
	// graph-native facts, compatibility rows, and completion metadata. Nodes were
	// inserted in the preceding structural phase; this transaction prevents any
	// resolution reader from observing only part of the resolution publication.
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
	publishTx, err := db.BeginTx()
	if err != nil {
		abortStagedBuild(db, stagedOutput, "begin resolution publication transaction: %v", err)
	}
	if err := store.BatchInsertEdgesTx(publishTx, edgePtrs); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "batch insert edges: %v", err)
	}
	// Additive producer-owned resolution contract. The legacy edges above remain
	// readable; these rows preserve stable identities and ambiguity explicitly.
	repositoryRevision := repoCommit(*root)
	if repositoryRevision == "" {
		repositoryRevision = "unversioned"
	}
	repositoryID := repositoryIdentity(*root)
	resolutionSymbols := make([]*store.ResolutionSymbol, 0, len(allNodePtrs))
	symbolByID := make(map[int64]store.ResolutionSymbol, len(allNodePtrs))
	for i, node := range allNodePtrs {
		if i >= len(nodeDBIDs) {
			continue
		}
		s := store.BuildResolutionSymbol(strconv.FormatInt(nodeDBIDs[i], 10), *node)
		resolutionSymbols = append(resolutionSymbols, &s)
		symbolByID[nodeDBIDs[i]] = s
	}
	if err := store.BatchInsertResolutionSymbolsTx(publishTx, resolutionSymbols); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "insert resolution symbols: %v", err)
	}
	callsiteRows := make([]*store.ResolutionCallsite, 0, len(callsites))
	candidateRows := make([]*store.ResolutionCandidate, 0)
	graphRows := make([]store.AttachedResolution, 0, len(callsites))
	nodeByID := make(map[int64]*store.Node, len(allNodes))
	fileNodeIDByPath := make(map[string]int64)
	fileIdentityByPath := make(map[string]string)
	for i := range allNodes {
		if i < len(nodeDBIDs) {
			n := allNodes[i]
			n.ID = nodeDBIDs[i]
			nodeByID[nodeDBIDs[i]] = &n
			if n.Label == "Module" && n.FilePath != "" {
				fileNodeIDByPath[n.FilePath] = n.ID
				if symbol, ok := symbolByID[n.ID]; ok {
					fileIdentityByPath[n.FilePath] = symbol.StableID
				}
			}
		}
	}
	for _, c := range callsites {
		source, ok := symbolByID[c.SourceNodeID]
		if !ok {
			_ = publishTx.Rollback()
			abortStagedBuild(db, stagedOutput, "callsite ordinal %d source node %d missing from graph", c.CallsiteOrdinal, c.SourceNodeID)
		}
		callsiteID := store.StableResolutionCallsiteIDWithOrdinal(repositoryRevision, source.StableID, c.SourceFile, c.SourceLine, c.SourceLine, c.CallsiteOrdinal, c.Callee)
		candidateNodeIDs := append([]int64(nil), c.CandidateNodeIDs...)
		selectedNodeID := c.SelectedTargetNodeID
		publishedDispatchState := string(c.DispatchState)
		publishedMechanism := c.Mechanism
		vtaUsed := false
		if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" && (c.DispatchForm == "interface" || c.DispatchForm == "virtual") {
			// VTA is candidate-only. Preserve CHA/RTA identities when flow is
			// incomplete, while adding every flow-supported identity to the
			// primary graph. A flow result with no viable method must not erase
			// conservative hierarchy candidates.
			if hierarchy, hierarchyOK := hierarchyByOrdinal[c.CallsiteOrdinal]; hierarchyOK {
				candidateNodeIDs = mergeIDs(vta.CandidateNodeIDs, hierarchy.CHACandidateNodeIDs)
			} else {
				candidateNodeIDs = append([]int64(nil), vta.CandidateNodeIDs...)
			}
			selectedNodeID = nil
			if len(vta.CandidateNodeIDs) > 0 {
				publishedMechanism = "vta"
				vtaUsed = true
			}
			if len(candidateNodeIDs) > 1 {
				publishedDispatchState = string(resolver.DispatchAmbiguous)
			} else {
				publishedDispatchState = string(resolver.DispatchCandidateOnly)
			}
		}
		if !vtaUsed {
			if hierarchy, ok := hierarchyByOrdinal[c.CallsiteOrdinal]; ok && (c.DispatchForm == "interface" || c.DispatchForm == "virtual") && (len(hierarchy.CHACandidateNodeIDs) > 0 || (hierarchy.ReceiverScoped && hierarchy.CHACompleteness == "closed")) {
				candidateNodeIDs = append([]int64(nil), hierarchy.CHACandidateNodeIDs...)
				selectedNodeID = nil
				if len(candidateNodeIDs) == 0 {
					publishedDispatchState = string(resolver.DispatchZero)
				} else if len(candidateNodeIDs) > 1 {
					publishedDispatchState = string(resolver.DispatchAmbiguous)
				} else {
					publishedDispatchState = string(resolver.DispatchCandidateOnly)
				}
			}
		}
		var selectedStable, selectedNative *string
		if selectedNodeID != nil {
			if target, exists := symbolByID[*selectedNodeID]; exists {
				stable, native := target.StableID, target.NativeID
				selectedStable, selectedNative = &stable, &native
			}
		}
		passCoverage := make([]store.ResolutionPassCoverage, 0, len(c.PassExecutions))
		for _, pass := range c.PassExecutions {
			passCoverage = append(passCoverage, store.ResolutionPassCoverage{PassKind: pass.PassKind, Version: "1", Status: pass.Status, Reason: pass.Reason})
		}
		if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" {
			vtaStatus := "partial"
			if len(vta.CandidateNodeIDs) == 0 {
				vtaStatus = "partial_no_match"
			}
			vtaStableIDs := make([]string, 0, len(vta.CandidateNodeIDs))
			for _, id := range vta.CandidateNodeIDs {
				if symbol, exists := symbolByID[id]; exists {
					vtaStableIDs = append(vtaStableIDs, symbol.StableID)
				}
			}
			flowStableIDs := make([]string, 0, len(vta.FlowTypeNodeIDs))
			for _, id := range vta.FlowTypeNodeIDs {
				if symbol, exists := symbolByID[id]; exists {
					flowStableIDs = append(flowStableIDs, symbol.StableID)
				}
			}
			flowReason := vta.AbstentionReason
			if len(flowStableIDs) > 0 {
				flowReason = "flow_type_stable_ids=" + strings.Join(flowStableIDs, ",") + "; " + flowReason
			}
			passCoverage = append(passCoverage, store.ResolutionPassCoverage{
				PassKind: "vta", Version: "1", Status: vtaStatus,
				Reason: flowReason, CandidateStableIDs: vtaStableIDs,
			})
		}
		if hierarchy, ok := hierarchyByOrdinal[c.CallsiteOrdinal]; ok && (c.DispatchForm == "interface" || c.DispatchForm == "virtual") {
			chaStatus := "partial"
			chaReason := "hierarchy_open"
			if hierarchy.CHACompleteness == "closed" {
				chaReason = ""
				if len(hierarchy.CHACandidateNodeIDs) == 0 {
					chaStatus = "completed_no_match"
				} else {
					chaStatus = "completed_match"
				}
			}
			rtaStatus := "partial"
			rtaReason := hierarchy.RTAAbstentionReason
			if hierarchy.RTACompleteness == "closed" {
				rtaReason = ""
				if len(hierarchy.RTACandidateNodeIDs) == 0 {
					rtaStatus = "completed_no_match"
				} else {
					rtaStatus = "completed_match"
				}
			}
			stableIDs := func(ids []int64) []string {
				out := make([]string, 0, len(ids))
				for _, id := range ids {
					if symbol, exists := symbolByID[id]; exists {
						out = append(out, symbol.StableID)
					}
				}
				return out
			}
			passCoverage = append(passCoverage,
				store.ResolutionPassCoverage{PassKind: "cha", Version: "1", Status: chaStatus, Reason: chaReason, CandidateStableIDs: stableIDs(hierarchy.CHACandidateNodeIDs)},
				store.ResolutionPassCoverage{PassKind: "rta", Version: "1", Status: rtaStatus, Reason: rtaReason, CandidateStableIDs: stableIDs(hierarchy.RTACandidateNodeIDs), AllocationTypeStableIDs: stableIDs(hierarchy.RTAAllocationTypeIDs), ReachableStableIDs: stableIDs(hierarchy.ReachableNodeIDs), RootStableIDs: stableIDs(hierarchy.RootNodeIDs), RootPolicy: hierarchy.RootPolicy, RootPolicyVersion: hierarchy.RootPolicyVersion, RootCompleteness: hierarchy.RootCompleteness, RootBoundary: hierarchy.RootBoundary},
			)
		}
		callsiteRows = append(callsiteRows, &store.ResolutionCallsite{CallsiteID: callsiteID, CallsiteOrdinal: c.CallsiteOrdinal, RepositoryRevision: repositoryRevision, SourceStableID: source.StableID, SourceNativeID: source.NativeID, SourceID: c.SourceNodeID, SourceLine: c.SourceLine, SourceFile: c.SourceFile, Callee: c.Callee, Language: source.Language, DispatchState: publishedDispatchState, CandidateCount: len(candidateNodeIDs), SelectedTargetStableID: selectedStable, SelectedTargetNativeID: selectedNative, Mechanism: publishedMechanism, VerificationStatus: c.VerificationStatus, PassCoverage: passCoverage})
		graphCandidates := make([]*store.ResolutionCandidate, 0, len(candidateNodeIDs))
		for ordinal, targetID := range candidateNodeIDs {
			target, exists := symbolByID[targetID]
			if !exists {
				continue
			}
			complete := c.ParserComplete
			receiverChain, _ := json.Marshal(qualifiedCallChain(c.CalleeQualified))
			importChain, _ := json.Marshal(c.CandidateImportChains[targetID])
			receiverType, receiverOrigin := c.ReceiverType, c.ReceiverOrigin
			if vta, ok := vtaByOrdinal[c.CallsiteOrdinal]; ok && vta.Completeness != "not_run" && len(vta.FlowTypeNodeIDs) > 0 {
				flowStableIDs := make([]string, 0, len(vta.FlowTypeNodeIDs))
				flowNames := make([]string, 0, len(vta.FlowTypeNodeIDs))
				for _, flowID := range vta.FlowTypeNodeIDs {
					if symbol, exists := symbolByID[flowID]; exists {
						flowStableIDs = append(flowStableIDs, symbol.StableID)
						flowNames = append(flowNames, symbol.QualifiedName)
					}
				}
				receiverType = strings.Join(flowNames, ",")
				receiverOrigin = "vta_flow_stable_ids=" + strings.Join(flowStableIDs, ",")
			}
			candidate := &store.ResolutionCandidate{CallsiteID: callsiteID, TargetID: targetID, TargetStableID: target.StableID, TargetNativeID: target.NativeID, Ordinal: ordinal, Mechanism: publishedMechanism, DeclaredScope: target.QualifiedName, ReceiverType: receiverType, ReceiverOrigin: receiverOrigin, ReceiverShape: c.CalleeQualified, ReceiverChain: string(receiverChain), ImportChain: string(importChain), DynamicDispatch: publishedDispatchState == string(resolver.DispatchDynamic), ExportStatus: target.ExportStatus, ParserComplete: &complete, VerificationStatus: c.VerificationStatus, Selected: selectedNodeID != nil && *selectedNodeID == targetID}
			candidateRows = append(candidateRows, candidate)
			graphCandidates = append(graphCandidates, candidate)
		}
		if len(graphCandidates) != len(candidateNodeIDs) {
			_ = publishTx.Rollback()
			abortStagedBuild(db, stagedOutput, "callsite %s candidate conservation failed: retained=%d expected=%d", callsiteID, len(graphCandidates), len(candidateNodeIDs))
		}
		if sourceNode := nodeByID[c.SourceNodeID]; sourceNode != nil {
			parseState := "complete"
			if !c.ParserComplete {
				parseState = "recovered"
			}
			graphCallsite := &store.ResolutionCallsite{
				CallsiteID: callsiteID, CallsiteOrdinal: c.CallsiteOrdinal, RepositoryRevision: repositoryRevision,
				SourceStableID: source.StableID, SourceNativeID: source.NativeID, SourceID: c.SourceNodeID,
				SourceLine: c.SourceLine, SourceFile: c.SourceFile, Callee: c.Callee, Language: source.Language,
				DispatchState: publishedDispatchState, CandidateCount: len(graphCandidates),
				SelectedTargetStableID: selectedStable, SelectedTargetNativeID: selectedNative,
				Mechanism: publishedMechanism, VerificationStatus: c.VerificationStatus,
				RepoID: repositoryID, FileNodeID: fileNodeIDByPath[c.SourceFile], FileIdentity: fileIdentityByPath[c.SourceFile], CallerSymbolID: source.StableID,
				ASTPath: c.ASTPath, ByteStart: c.ByteStart, ByteEnd: c.ByteEnd, ColumnStart: c.ColumnStart,
				DispatchForm: c.DispatchForm, ArgumentArity: c.ArgumentArity, ParseState: parseState,
				PassCoverage: passCoverage,
			}
			graphRows = append(graphRows, store.AttachedResolution{Callsite: graphCallsite, Source: sourceNode, Candidates: graphCandidates})
		}
	}
	if err := store.BatchInsertResolutionCallsitesTx(publishTx, callsiteRows); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "insert resolution callsites: %v", err)
	}
	if err := store.BatchInsertResolutionCandidatesTx(publishTx, candidateRows); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "insert resolution candidates: %v", err)
	}
	producerIdentity, err := currentBuildIdentity()
	if err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "compute producer build identity: %v", err)
	}
	graphIdentity := store.GraphCompletionIdentity{
		Schema: store.GraphCompletionSchema, RepositoryRevision: repositoryRevision,
		BuildInfoSchema: producerIdentity.Schema, BuildID: producerIdentity.BuildID,
		GitCommit: producerIdentity.GitCommit, BuildTimeUTC: producerIdentity.BuildTimeUTC,
		SourceFingerprint: producerIdentity.SourceFingerprint, ExecutableSHA256: producerIdentity.ExecutableSHA256,
		GoToolchain: producerIdentity.GoToolchain, BuildTags: producerIdentity.BuildTags,
		GraphSchemaVersion: producerIdentity.SchemaVersion, ResolutionContract: store.CallResolutionContractV2,
		ResolutionAuthoritySchema: resolver.ResolutionAuthoritySchema,
		Complete:                  producerIdentity.Complete,
	}
	if err := store.AttachResolutionGraphTx(publishTx, graphIdentity, graphRows); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "attach graph-native resolution evidence: %v", err)
	}
	if err := store.BindGraphCompletionTx(publishTx, graphIdentity); err != nil {
		_ = publishTx.Rollback()
		abortStagedBuild(db, stagedOutput, "bind graph completion to producer identity: %v", err)
	}
	metadata := map[string]string{
		"resolution_schema_version":      "2",
		"resolution_producer_contract":   store.CallResolutionContractV2,
		"resolution_repository_revision": repositoryRevision,
		"resolution_complete":            "1",
	}
	for key, value := range metadata {
		if _, err := publishTx.Exec(`INSERT INTO project_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value`, key, value); err != nil {
			_ = publishTx.Rollback()
			abortStagedBuild(db, stagedOutput, "set resolution metadata %s: %v", key, err)
		}
	}
	if err := publishTx.Commit(); err != nil {
		abortStagedBuild(db, stagedOutput, "commit resolution publication transaction: %v", err)
	}
	// Containment edges: parent_id → CONTAINS for class-structure queries
	// Use parentFixups since allNodePtrs had ParentID zeroed before batch insert.
	var containsPtrs []*store.Edge
	for nodeIdx, parentGlobalIdx := range parentFixups {
		pidx := int(parentGlobalIdx) - 1
		if pidx >= 0 && pidx < len(nodeDBIDs) && nodeIdx < len(nodeDBIDs) {
			parentDBID := nodeDBIDs[pidx]
			childDBID := nodeDBIDs[nodeIdx]
			if parentDBID > 0 && childDBID > 0 {
				filePath := ""
				if nodeIdx < len(allNodePtrs) {
					filePath = allNodePtrs[nodeIdx].FilePath
				}
				containsPtrs = append(containsPtrs, &store.Edge{
					SourceID:           parentDBID,
					TargetID:           childDBID,
					Type:               "CONTAINS",
					SourceFile:         filePath,
					ResolutionMethod:   "structural",
					Confidence:         1.0,
					TrustTier:          "CERTIFIED",
					EvidenceType:       "parent_id",
					VerificationStatus: "verified",
				})
			}
		}
	}
	if len(containsPtrs) > 0 {
		if err := db.BatchInsertEdges(containsPtrs); err != nil {
			log.Printf("WARNING: containment edges: %v", err)
		}
	}

	edgeElapsed := time.Since(edgeStart)
	fmt.Fprintf(os.Stderr, "  Inserted %d CALLS + %d CONTAINS edges in %s\n", len(edgePtrs), len(containsPtrs), edgeElapsed.Round(time.Millisecond))

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

	nodeIDToFilePath := make(map[int64]string, len(nodeDBIDs))
	for i, id := range nodeDBIDs {
		if i < len(allNodePtrs) {
			nodeIDToFilePath[id] = allNodePtrs[i].FilePath
		}
	}

	resolvedCount := 0
	for _, a := range allAssertions {
		if a.TestNodeIdx < 0 || a.TestNodeIdx >= len(nodeDBIDs) {
			continue
		}
		targetID, resScore := resolveAssertionTarget(a, allNodePtrs, nodeDBIDs, nameToNodeIDs, importIndex, fileNodeIDs, nodeIDToFilePath)
		assertPtrs = append(assertPtrs, &store.Assertion{
			TestNodeID:      nodeDBIDs[a.TestNodeIdx],
			TargetNodeID:    targetID,
			ResolutionScore: resScore,
			Kind:            a.Kind,
			Expression:      a.Expression,
			Expected:        a.Expected,
			Line:            a.Line,
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

	// ── Pass 4d: SERIALIZATION PAIRS + STRUCTURAL TWINS ───────────────────
	serdeStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 4d: detecting serialization pairs + structural twins...\n")
	serdeCount := detectSerdePairs(db, allNodePtrs, nodeDBIDs)
	twinCount := detectStructuralTwins(db, allNodePtrs, nodeDBIDs)
	serdeElapsed := time.Since(serdeStart)
	fmt.Fprintf(os.Stderr, "  Detected %d serialization pair properties, %d structural twin properties in %s\n", serdeCount, twinCount, serdeElapsed.Round(time.Millisecond))

	// ── Pass 4f: IMPORTS edges + PROMOTE property->edges ────────────────
	// Runs AFTER all node/edge/property passes (nodes are in the DB so the
	// file-anchor query returns real ids; serde/READS/WRITES/exception/data_flow
	// properties exist for promotion) and BEFORE Pass 4e (transitive closure) so the
	// closure reflects the IMPORTS + promoted edges. Both are language-agnostic and
	// purely additive — they only INSERT edges (and annotate CALLS metadata).
	importStart := time.Now()
	fmt.Fprintf(os.Stderr, "Pass 4f: materializing IMPORTS edges...\n")
	importEdgeCount, impErr := resolver.ResolveImports(db, allImports, fileMap, files)
	if impErr != nil {
		// Non-fatal: a failure here must not abort the index. The agent degrades to
		// the CALLS/closure graph without the file->module IMPORTS hops.
		log.Printf("WARNING: IMPORTS edge materialization failed: %v", impErr)
	}
	promotedCount, promErr := resolver.PromotePropertyEdges(db)
	if promErr != nil {
		log.Printf("WARNING: property->edge promotion failed: %v", promErr)
	}
	importElapsed := time.Since(importStart)
	fmt.Fprintf(os.Stderr, "  Pass 4f: %d IMPORTS edges, %d promoted relations in %s\n",
		importEdgeCount, promotedCount, importElapsed.Round(time.Millisecond))

	// ── Pass 4e: TRANSITIVE CLOSURE (C7 / RF-4) ─────────────────────────
	// Runs AFTER CALLS resolution + edge persistence (Pass 3) so it sees the
	// fully-resolved call graph. Computes depth-bounded transitive reach over
	// VERIFIED edges ONLY (confidence>=0.5 / deterministic+LSP resolution
	// methods) — name_match false positives are excluded so they cannot
	// propagate transitively. Default-on via -closure; the impact/trace
	// Python readers fall back to live BFS when the table is absent.
	closureCount := 0
	if *closureEnabled {
		closureStart := time.Now()
		fmt.Fprintf(os.Stderr, "Pass 4e: computing transitive closure (verified CALLS, depth<=%d)...\n", closure.MaxDepth)
		n, cerr := closure.ComputeTransitiveClosure(db, "CALLS", closure.MaxDepth, closure.MinEdgeConfidence)
		if cerr != nil {
			// Non-fatal: a closure failure must not abort the index. impact/trace
			// degrade gracefully to live BFS when the table is empty/absent.
			log.Printf("WARNING: transitive closure failed: %v", cerr)
		} else {
			closureCount = n
		}
		closureElapsed := time.Since(closureStart)
		fmt.Fprintf(os.Stderr, "  Computed %d closure rows in %s\n", closureCount, closureElapsed.Round(time.Millisecond))
	}

	// ── Pass 5: EXTRAS — store metadata ─────────────────────────────────
	fmt.Fprintf(os.Stderr, "Pass 5: storing metadata...\n")
	elapsed := time.Since(start)
	requiredMetadata := map[string]string{"root": *root}
	// RC-17 (F-004): build_time_ms removed from project_meta — it's wall-
	// clock dependent and breaks byte-equality across two builds of the
	// same commit. Diagnostic value only; emitted to stderr below instead.
	requiredMetadata["file_count"] = fmt.Sprintf("%d", len(files))
	requiredMetadata["parse_failures"] = fmt.Sprintf("%d", parseFailures)
	requiredMetadata["node_count"] = fmt.Sprintf("%d", len(allNodePtrs))
	requiredMetadata["edge_count"] = fmt.Sprintf("%d", len(resolved))
	requiredMetadata["import_count"] = fmt.Sprintf("%d", len(allImports))
	requiredMetadata["property_count"] = fmt.Sprintf("%d", len(propPtrs))
	requiredMetadata["assertion_count"] = fmt.Sprintf("%d", len(assertPtrs))
	requiredMetadata["indexer_version"] = "v16-multilang"
	// FINAL_ARCH_V2 Track-A (B-1/B-5): schema_version is a contract between
	// the Go writer and Python readers. Readers MUST fail fast if this row
	// is missing (= old binary) or older than the version the reader expects.
	// Bump on every breaking edges/nodes schema change.
	requiredMetadata["schema_version"] = schemaVersion
	// RC-17 (F-003): forensics-grade provenance. commitSHA / buildTimeUTC
	// / goToolchain are injected by the build script via -ldflags. With
	// "unknown" defaults, callers can still distinguish a stamped binary
	// from a bare `go build`.
	requiredMetadata["git_commit"] = commitSHA
	requiredMetadata["build_time_utc"] = buildTimeUTC
	requiredMetadata["go_toolchain"] = goToolchain
	requiredMetadata["workers"] = fmt.Sprintf("%d", *workers)

	// RC-04: per-repo MIN_CONFIDENCE — write the median (P50) of resolved edge
	// confidences so downstream readers can stop hardcoding 0.7. Writing to
	// project_meta (existing table, no schema change). Readers fall back to
	// 0.5 (brief-layer parity) when this key is missing.
	requiredMetadata["min_confidence"] = fmt.Sprintf("%.4f", computeMedianConfidence(resolved))

	// C7 (RF-4): closure row count. Diagnostic + lets readers detect a
	// closure-bearing db without a table probe. 0 means closure disabled or
	// no verified edges to close over — readers fall back to live BFS.
	requiredMetadata["closure_count"] = fmt.Sprintf("%d", closureCount)
	if err := setRequiredMetadata(db, requiredMetadata); err != nil {
		abortStagedBuild(db, stagedOutput, "%v", err)
	}

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

	// ── Pass 5c: CO-CHANGE MINING — git log analysis for file co-occurrence ──
	fmt.Fprintf(os.Stderr, "Pass 5c: mining co-change from git history...\n")
	cochangeCount := mineCochanges(db, *root)
	fmt.Fprintf(os.Stderr, "  Stored %d co-change pairs\n", cochangeCount)

	// Post-insert FK validation (non-fatal)
	if err := db.ValidateForeignKeys(); err != nil {
		abortStagedBuild(db, stagedOutput, "foreign-key validation failed: %v", err)
	}

	// Fold the WAL into graph.db so the file is SELF-CONTAINED before the process
	// exits. The DB is opened in WAL mode (sqlite.go: _journal_mode=WAL); the FTS5
	// index (PopulateFTS5, written near the end) plus the closure/cochange/meta
	// writes live in the -wal sidecar. `defer db.Close()` does NOT reliably
	// checkpoint with database/sql's connection pool, so the full-index path used
	// to leave them stranded — and the benchmark harness copies ONLY graph.db
	// (`docker cp .../graph.db`), not graph.db-wal, dropping the FTS5 inverted
	// index → `nodes_fts` COUNT looks full (external-content reads `nodes`) but a
	// real MATCH returns 0 / "database disk image is malformed". The incremental
	// path already checkpoints (RC-04); the full-index path must too.
	if err := db.CheckpointWALRequired(); err != nil {
		abortStagedBuild(db, stagedOutput, "checkpoint staged graph: %v", err)
	}

	// Capture summary values while the staged database is still open, then close
	// every SQLite handle before the platform-specific atomic replacement.
	nodeCount := db.NodeCount()
	edgeCount := db.EdgeCount()
	propertyCount := db.PropertyCount()
	assertionCount := db.AssertionCount()
	if err := db.Close(); err != nil {
		abortStagedBuild(nil, stagedOutput, "close staged graph: %v", err)
	}
	if err := publishStagedOutput(stagedOutput, requestedOutput); err != nil {
		abortStagedBuild(nil, stagedOutput, "publish staged graph: %v", err)
	}
	published = true
	*output = requestedOutput

	// Summary
	fmt.Fprintf(os.Stderr, "\nDone in %s\n", elapsed.Round(time.Millisecond))
	fmt.Fprintf(os.Stderr, "  Files:      %d\n", len(files))
	fmt.Fprintf(os.Stderr, "  Nodes:      %d\n", nodeCount)
	fmt.Fprintf(os.Stderr, "  Edges:      %d\n", edgeCount)
	fmt.Fprintf(os.Stderr, "  Imports:    %d\n", len(allImports))
	fmt.Fprintf(os.Stderr, "  Properties: %d\n", propertyCount)
	fmt.Fprintf(os.Stderr, "  Assertions: %d\n", assertionCount)
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
		len(files), nodeCount, edgeCount, len(allImports),
		propertyCount, assertionCount,
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
	// Mark test AND non-source (benchmark/example/fixture/docs/vendored) nodes
	// is_test so their call edges stay OUT of the fact surface (same as the bulk
	// parse path above) — the incremental -file reindex must classify identically.
	isTest := walker.IsTestFile(relSlash) || walker.IsNonSourceFile(relSlash)
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

	// Resolution provenance is revision-bound. A single-file edit changes node
	// identities and call candidates, so retain no old complete sidecar beside
	// the new graph. Full indexing will repopulate it; until then consumers must
	// fail closed on the explicit incomplete marker.
	for _, table := range []string{"resolution_candidates", "resolution_callsites", "resolution_symbols"} {
		if _, err := tx.Exec("DELETE FROM " + table); err != nil {
			return fmt.Errorf("invalidate %s: %w", table, err)
		}
	}
	if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES('resolution_complete','0') ON CONFLICT(key) DO UPDATE SET value='0'`); err != nil {
		return fmt.Errorf("invalidate resolution metadata: %w", err)
	}
	if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES('resolution_repository_revision','stale') ON CONFLICT(key) DO UPDATE SET value='stale'`); err != nil {
		return fmt.Errorf("bind stale resolution revision: %w", err)
	}
	if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES('graph_resolution_complete','0') ON CONFLICT(key) DO UPDATE SET value='0'`); err != nil {
		return fmt.Errorf("invalidate graph resolution metadata: %w", err)
	}
	if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES('graph_resolution_revision','stale') ON CONFLICT(key) DO UPDATE SET value='stale'`); err != nil {
		return fmt.Errorf("bind stale graph resolution revision: %w", err)
	}
	// A single-file refresh cannot prove repository-wide candidate parity because
	// callers in other files may depend on the changed identities. Remove the
	// entire attached resolution overlay in this same transaction. Metadata-only
	// invalidation is insufficient: direct SQL and generic graph readers do not
	// necessarily consult project_meta before traversing nodes and edges.
	if _, err := tx.Exec(`DELETE FROM edges WHERE type IN ('HAS_CALLSITE','CANDIDATE','CANDIDATE_TARGET','HAS_DERIVATION_FACT','HAS_COMPLETENESS_FACT','HAS_UNRESOLVED_FACT') OR (type='CALLS' AND callsite_stable_id IS NOT NULL)`); err != nil {
		return fmt.Errorf("remove stale attached resolution edges: %w", err)
	}
	if _, err := tx.Exec(`DELETE FROM nodes WHERE node_type IN ('callsite','derivation_fact','completeness_fact','unresolved_fact') OR label='Callsite'`); err != nil {
		return fmt.Errorf("remove stale attached resolution nodes: %w", err)
	}

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
		// Restore the parent DB id. n.ParentID was zeroed at the insert above (line 921)
		// and the DB row fixed up via UpdateParentIDTx — but THIS in-memory copy feeds
		// BuildNodeMeta, which the self.method()/inherited-method rungs (Strategy 1.75,
		// 1.94a, 2b) read as the caller's enclosing class. Left at 0, callerMeta.ParentID==0
		// → those rungs cannot identify self's class → every inherited/typed-receiver call
		// demotes to name_match on each `-file` reindex (the deeper half of the G09 gap:
		// the inheritance map is necessary but the caller-class link is also required).
		if plocal := parentLocal[i]; plocal > 0 {
			pidx := int(plocal) - 1
			if pidx >= 0 && pidx < len(newDBIDs) && newDBIDs[pidx] > 0 {
				nn.ParentID = newDBIDs[pidx]
			}
		}
		filteredNodes = append(filteredNodes, nn)
		filteredIDs = append(filteredIDs, newDBIDs[i])
	}
	nameIndex, fileIndex := resolver.BuildNameIndex(db, filteredNodes, filteredIDs)
	fileMap := resolver.BuildFileMap(allFiles, allLangs)
	if tsCfg := resolver.ParseTSConfig(root); tsCfg != nil {
		resolver.RegisterTSConfigPaths(fileMap, tsCfg)
	}
	resolver.RegisterJSPackagePaths(fileMap, root)

	callerDBIDs := make([]int64, len(pr.Calls))
	for i, call := range pr.Calls {
		if call.CallerNodeIdx >= 0 && call.CallerNodeIdx < len(newDBIDs) {
			callerDBIDs[i] = newDBIDs[call.CallerNodeIdx]
		}
	}

	// Build nodeMeta from the filtered nodes so strategies 1.75-1.98
	// (self.method, type_flow, inherited method resolution) work on
	// incremental reindex — not just full-index. Without this, the
	// Resolve() call was missing the variadic nodeMeta arg and those
	// strategies were dead on L6 reindex.
	nodeMeta := resolver.BuildNodeMeta(filteredNodes, filteredIDs)

	// -file degradation fix: the package-level assignmentIndex that Strategy 1.96
	// (x = ClassName(); x.method()) reads was never set on the incremental path (only in
	// full-index, main.go:380-389), so 1.96 ran DEAD on every `gt-index -file` reindex.
	// Wire it from the reparsed file's assignments (sufficient for that file's own calls).
	if len(pr.Assignments) > 0 {
		resolver.SetAssignmentIndex(resolver.BuildAssignmentIndex(pr.Assignments))
	}

	// G09 -file degradation fix: the class inheritanceMap (consumed by
	// lookupMethodWithInheritance for the CHA rungs 1.75 super/inherited,
	// 1.94a/2b inherited-method, 1.94 impl_method — gt_gt §2.3) was built ONLY on
	// the full-index path (main.go:426-428). On the incremental `-file` path it was
	// never set → nil → every inheritance-walking rung under-resolved a call whose
	// target is an inherited method / parent-class field in another file, demoting
	// it to name_match on each reindex (correct-or-quiet, but depth-completeness lost).
	// buildInheritanceMap needs a WHOLE-GRAPH SourceFile list; `allFiles`/`allLangs`
	// (the distinct file_path/language rows already loaded at the top of this fn) ARE
	// that cross-file list. nodeMeta (built from filteredNodes above) spans every
	// file's class nodes, and node file_path == SourceFile.Path (both relSlash), so
	// resolveClass matches same-file then global. AbsPath="" → buildInheritanceMap
	// reopens via filepath.Join(root, sf.Path), and the source tree is on disk here.
	// allFiles/allLangs are the PARALLEL distinct (file_path, language) rows
	// (db.GetDistinctFilesAndLanguages, zipped by BuildFileMap above); buildInheritanceMap
	// reads only sf.Language/Path/AbsPath, so reconstructing []walker.SourceFile from them
	// is sufficient (AbsPath="" -> reopen via filepath.Join(root, sf.Path)).
	inhFiles := make([]walker.SourceFile, 0, len(allFiles))
	for i, fp := range allFiles {
		lang := ""
		if i < len(allLangs) {
			lang = allLangs[i]
		}
		inhFiles = append(inhFiles, walker.SourceFile{Path: fp, Language: lang})
	}
	if inhMap := buildInheritanceMap(inhFiles, root, nameIndex, nodeMeta); len(inhMap) > 0 {
		resolver.SetInheritanceMap(inhMap)
	}

	// T1 on the incremental path: build the declared-type receiver index from the
	// reparsed file's `param` properties (NodeIdx -> pr.Nodes, parallel to newDBIDs),
	// so Strategy 1.94a resolves typed-receiver method calls on `gt-index -file` too.
	if len(pr.Properties) > 0 {
		resolver.SetParamTypeIndex(resolver.BuildParamTypeIndex(pr.Properties, newDBIDs))
		// Strategy 2b on the incremental path: declared-field-type index from the
		// reparsed file's `class_field` properties so self.<field>.method() resolves
		// on `gt-index -file` reindex too (parity with the param index above).
		resolver.SetFieldTypeIndex(resolver.BuildFieldTypeIndex(pr.Properties, newDBIDs))
		// GAP C on the incremental path: constructor-return-shape index for the
		// 1.96/1.97 return-type fallback, parity with the full-index path. classNames
		// is the FULL cross-file class-like name set (nodeMeta spans all files).
		classNames := make(map[string]bool)
		for _, m := range nodeMeta {
			if m.Label == "Class" || m.Label == "Struct" || m.Label == "Type" ||
				m.Label == "Enum" || m.Label == "Interface" {
				classNames[m.Name] = true
			}
		}
		resolver.SetReturnShapeIndex(resolver.BuildReturnShapeIndex(pr.Properties, newDBIDs, classNames))
	}

	resolved := resolver.Resolve(pr.Calls, nameIndex, fileIndex, callerDBIDs, pr.Imports, fileMap, nodeMeta)
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
	// This transaction only re-resolves the edited file. Other callsites may
	// depend on symbols whose identity changed, so the attached overlay remains
	// absent until a subsequent full build restores repository-wide authority.
	if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES('graph_resolution_complete','0') ON CONFLICT(key) DO UPDATE SET value='0'`); err != nil {
		return fmt.Errorf("mark incremental graph resolution incomplete: %w", err)
	}
	if _, err := tx.Exec(`INSERT INTO project_meta(key,value) VALUES('graph_resolution_revision','stale') ON CONFLICT(key) DO UPDATE SET value='stale'`); err != nil {
		return fmt.Errorf("mark incremental graph resolution stale: %w", err)
	}

	// IMPORTS edges for the reparsed file. Stale IMPORTS edges (source_file=relSlash)
	// were already deleted by DeleteFileEdgesAndNodesTx upstream, so re-emitting here
	// converges. Resolves pr.Imports against the in-memory post-reindex snapshot
	// (filteredNodes/filteredIDs = all DB nodes minus the stale file, plus fresh).
	if _, impErr := resolver.ResolveImportsTx(tx, pr.Imports, fileMap, filteredNodes, filteredIDs); impErr != nil {
		log.Printf("WARNING: incremental IMPORTS edges: %v", impErr)
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
	// Build cross-file indexes for assertion resolution using ALL nodes
	// (filteredNodes already contains all DB nodes minus stale file + fresh nodes)
	incrNameToIDs := make(map[string][]int64)
	for i, n := range filteredNodes {
		if n.Label != "Class" && n.Label != "Interface" && !n.IsTest {
			incrNameToIDs[n.Name] = append(incrNameToIDs[n.Name], filteredIDs[i])
		}
	}
	// pr.Nodes entries FIRST so a.TestNodeIdx (index into pr.Nodes) dereferences correctly
	incrNodePtrs := make([]*store.Node, len(pr.Nodes), len(pr.Nodes)+len(filteredNodes))
	for i := range pr.Nodes {
		incrNodePtrs[i] = &pr.Nodes[i]
	}
	for i := range filteredNodes {
		incrNodePtrs = append(incrNodePtrs, &filteredNodes[i])
	}

	// Import index for this file's imports
	incrImportIndex := make(map[string]map[string][]string)
	for _, imp := range pr.Imports {
		if imp.ImportedName == "" || imp.ImportedName == "*" {
			continue
		}
		byName, ok := incrImportIndex[imp.File]
		if !ok {
			byName = make(map[string][]string)
			incrImportIndex[imp.File] = byName
		}
		if targetFiles, ok := fileMap[imp.ModulePath]; ok {
			byName[imp.ImportedName] = append(byName[imp.ImportedName], targetFiles...)
		}
	}

	// File-scoped node IDs for import-guided resolution
	incrFileNodeIDs := make(map[string]map[string][]int64)
	for i, n := range filteredNodes {
		if n.Label != "Class" && n.Label != "Interface" && !n.IsTest {
			byName, ok := incrFileNodeIDs[n.FilePath]
			if !ok {
				byName = make(map[string][]int64)
				incrFileNodeIDs[n.FilePath] = byName
			}
			byName[n.Name] = append(byName[n.Name], filteredIDs[i])
		}
	}

	incrNodeIDToFilePath := make(map[int64]string, len(filteredIDs))
	for i, id := range filteredIDs {
		if i < len(filteredNodes) {
			incrNodeIDToFilePath[id] = filteredNodes[i].FilePath
		}
	}

	assertPtrs := make([]*store.Assertion, 0, len(pr.Assertions))
	for _, a := range pr.Assertions {
		if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(newDBIDs) {
			targetID, resScore := resolveAssertionTarget(a, incrNodePtrs, filteredIDs, incrNameToIDs, incrImportIndex, incrFileNodeIDs, incrNodeIDToFilePath)
			assertPtrs = append(assertPtrs, &store.Assertion{
				TestNodeID:      newDBIDs[a.TestNodeIdx],
				TargetNodeID:    targetID,
				ResolutionScore: resScore,
				Kind:            a.Kind,
				Expression:      a.Expression,
				Expected:        a.Expected,
				Line:            a.Line,
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

	// Step 10.5 — re-converge depth edges (inv-7). DeleteFileEdgesAndNodesTx stripped
	// this file's outbound promote_% edges and ResolveImportsTx re-emitted only IMPORTS
	// inside the tx, so the promoted READS/WRITES/RAISES/PRECEDES/CO_SERIALIZES/
	// DATA_FLOW-standalone edges would stay deleted until the next FULL rebuild. The
	// promote pass is idempotent (delete-before-rebuild) so re-running it whole-graph
	// here converges the reindexed file's depth edges. Non-fatal: a failure degrades to
	// the CALLS/IMPORTS graph, never aborts. (Perf residual: whole-graph re-promote per
	// -file reindex; a file-scoped tx variant is the future optimization.)
	if _, promErr := resolver.PromotePropertyEdges(db); promErr != nil {
		log.Printf("WARNING: incremental property->edge promotion: %v", promErr)
	}

	// Stamp schema_version + indexer provenance on every incremental run.
	// The full-index path (Pass 5) writes these in project_meta, but an older
	// DB built by a pre-FINAL_ARCH_V2 binary lacks the schema_version row.
	// Without it, the Python router_v2 reader raises SchemaMismatch and L3b
	// evidence is dead on every turn. INSERT OR REPLACE is idempotent so this
	// is safe on DBs that already have the row.
	if err := setRequiredMetadata(db, map[string]string{
		"schema_version": schemaVersion, "indexer_version": "v16-multilang",
		"git_commit": commitSHA, "build_time_utc": buildTimeUTC, "go_toolchain": goToolchain,
	}); err != nil {
		return err
	}

	// Refresh FTS5 index after incremental node changes so BM25 queries
	// stay current. Same call as the full-index path (idempotent).
	if err := db.PopulateFTS5(); err != nil {
		log.Printf("[WARN] FTS5 refresh after incremental reindex: %v", err)
	}
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

var assertionCallPattern = regexp.MustCompile(`(\w+)\s*\(`)
var dottedCallPattern = regexp.MustCompile(`(\w+)\.(\w+)\s*\(`)

// testDirVariants builds normalized directory variants for same-package matching.
// TCTracer (ICSE 2020): same-package is a strong disambiguator for test-to-code links.
func testDirVariants(testDir string) []string {
	if testDir == "" {
		return nil
	}
	variants := []string{testDir}
	for _, suffix := range []string{"/tests", "/test", "_test"} {
		if trimmed := strings.TrimSuffix(testDir, suffix); trimmed != testDir {
			variants = append(variants, trimmed)
		}
	}
	for _, prefix := range []string{"tests/", "test/"} {
		if trimmed := strings.TrimPrefix(testDir, prefix); trimmed != testDir {
			variants = append(variants, trimmed)
		}
	}
	if parent := filepath.Base(testDir); parent != "." && parent != "/" {
		variants = append(variants, parent)
	}
	return variants
}

// isSamePackage checks if a candidate file is in the same or related directory as the test.
func isSamePackage(candidateFilePath, testDir string) bool {
	if testDir == "" || candidateFilePath == "" {
		return false
	}
	nodeDir := filepath.Dir(candidateFilePath)
	for _, variant := range testDirVariants(testDir) {
		if nodeDir == variant || strings.HasSuffix(nodeDir, "/"+variant) ||
			filepath.Base(nodeDir) == filepath.Base(variant) {
			return true
		}
	}
	return false
}

// resolveAssertionTarget links an assertion to the production function it tests
// using multi-signal scoring (TCTracer, White et al., ICSE 2020 / EMSE 2022).
//
// Signals and weights:
//   - Import-guided:      4.0 (test imports module exporting the function)
//   - LCBA (expr call):   3.0 (function name extracted from assertion expression)
//   - Naming convention:  2.0 (test_foo → foo)
//   - Same-package:       2.0 (candidate in same/related directory)
//   - Non-test:           0.5 (candidate is not itself a test function)
//
// Minimum threshold: 3.5 (LCBA 3.0 + non-test 0.5 passes; naming 2.0 + same-pkg 2.0 passes)
func resolveAssertionTarget(
	a parser.AssertionRef,
	allNodes []*store.Node,
	nodeDBIDs []int64,
	nameToNodeIDs map[string][]int64,
	importIndex map[string]map[string][]string,
	fileNodeIDs map[string]map[string][]int64,
	nodeIDToFilePath map[int64]string,
) (int64, float64) {
	testDir := ""
	testFilePath := ""
	if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(allNodes) {
		testFilePath = allNodes[a.TestNodeIdx].FilePath
		testDir = filepath.Dir(testFilePath)
	}

	candidates := make(map[int64]float64)

	exprFuncs := extractCalledFunctions(a.Expression)

	// Signal 1: LCBA — function name in assertion expression (weight 3.0)
	for _, fname := range exprFuncs {
		if ids, ok := nameToNodeIDs[fname]; ok {
			for _, id := range ids {
				candidates[id] += 3.0
			}
		}
	}

	// Signal 2: Import-guided — test imports module containing candidate (weight 4.0)
	if testFilePath != "" && importIndex != nil && fileNodeIDs != nil {
		if fileImports, ok := importIndex[testFilePath]; ok {
			for _, fname := range exprFuncs {
				if targetFiles, ok := fileImports[fname]; ok {
					for _, targetFile := range targetFiles {
						if fnMap, ok := fileNodeIDs[targetFile]; ok {
							if ids, ok := fnMap[fname]; ok {
								for _, id := range ids {
									candidates[id] += 4.0
								}
							}
						}
					}
				}
			}
		}
	}

	// Signal 3: Naming convention — test_foo → foo (weight 2.0)
	if a.TestNodeIdx >= 0 && a.TestNodeIdx < len(allNodes) {
		testNode := allNodes[a.TestNodeIdx]
		if derivedName := deriveTargetFromTestName(testNode.Name); derivedName != "" {
			if ids, ok := nameToNodeIDs[derivedName]; ok {
				for _, id := range ids {
					candidates[id] += 2.0
				}
			}
			lower := strings.ToLower(derivedName)
			for name, ids := range nameToNodeIDs {
				if name != derivedName && strings.ToLower(name) == lower {
					for _, id := range ids {
						candidates[id] += 1.5
					}
				}
			}
		}
	}

	// Signal 4: Same-package bonus (weight 2.0)
	for id := range candidates {
		if fp, ok := nodeIDToFilePath[id]; ok && isSamePackage(fp, testDir) {
			candidates[id] += 2.0
		}
	}

	// Signal 5: Non-test bonus (weight 0.5) — check path components, not substrings
	for id := range candidates {
		if fp, ok := nodeIDToFilePath[id]; ok {
			isTestFile := false
			for _, part := range strings.Split(fp, "/") {
				if part == "test" || part == "tests" ||
					strings.HasSuffix(part, "_test") || strings.HasSuffix(part, "_test.go") ||
					strings.HasSuffix(part, "_test.py") || strings.HasPrefix(part, "test_") {
					isTestFile = true
					break
				}
			}
			if !isTestFile {
				candidates[id] += 0.5
			}
		}
	}

	// Pick winner: highest score, break ties by lowest nodeID for determinism
	var bestID int64
	var bestScore float64
	for id, score := range candidates {
		if score > bestScore || (score == bestScore && (bestID == 0 || id < bestID)) {
			bestScore = score
			bestID = id
		}
	}

	// Dynamic threshold: fewer candidates → lower bar (Cursor principle).
	threshold := 3.5
	if len(candidates) == 1 {
		threshold = 2.0
	} else if len(candidates) <= 3 {
		threshold = 3.0
	}

	if bestScore >= threshold {
		return bestID, bestScore
	}

	// File-stem rescue pass: when all 5 signals produce 0 candidates,
	// find production functions in files whose stem matches the test file stem.
	// TCTracer ICSE 2020: naming convention at file level, not function level.
	// This rescue uses a lower threshold (2.0) and only fires when the main
	// pass found nothing — no regression risk on existing links.
	if len(candidates) == 0 && testFilePath != "" {
		testBase := filepath.Base(testFilePath)
		testStem := strings.TrimSuffix(testBase, filepath.Ext(testBase))
		// test_qbittorrent → qbittorrent
		derivedStem := ""
		if strings.HasPrefix(testStem, "test_") && len(testStem) > 5 {
			derivedStem = testStem[5:]
		} else if strings.HasPrefix(testStem, "tests_") && len(testStem) > 6 {
			derivedStem = testStem[6:]
		} else if strings.HasSuffix(testStem, "_test") && len(testStem) > 5 {
			derivedStem = testStem[:len(testStem)-5]
		}
		if derivedStem != "" {
			rescueCandidates := make(map[int64]float64)
			for id, fp := range nodeIDToFilePath {
				fpBase := filepath.Base(fp)
				fpStem := strings.TrimSuffix(fpBase, filepath.Ext(fpBase))
				if fpStem == derivedStem || strings.HasPrefix(fpStem, derivedStem+"_") {
					rescueCandidates[id] = 1.5 // file-stem signal
				}
			}
			// Apply same-package and non-test bonuses to rescue candidates
			for id := range rescueCandidates {
				if fp, ok := nodeIDToFilePath[id]; ok && isSamePackage(fp, testDir) {
					rescueCandidates[id] += 2.0
				}
				if fp, ok := nodeIDToFilePath[id]; ok {
					isTestFile := false
					for _, part := range strings.Split(fp, "/") {
						if part == "test" || part == "tests" ||
							strings.HasSuffix(part, "_test") || strings.HasPrefix(part, "test_") {
							isTestFile = true
							break
						}
					}
					if !isTestFile {
						rescueCandidates[id] += 0.5
					}
				}
			}
			// Expression substring boost: if assertion expression mentions a
			// candidate function name, prefer it over siblings in the same file.
			exprLower := strings.ToLower(a.Expression)
			for id := range rescueCandidates {
				for i, n := range allNodes {
					if i < len(nodeDBIDs) && nodeDBIDs[i] == id {
						if strings.Contains(exprLower, strings.ToLower(n.Name)) {
							rescueCandidates[id] += 1.0
						}
						break
					}
				}
			}
			// Pick best rescue candidate, threshold 2.0
			var rescueBestID int64
			var rescueBestScore float64
			for id, score := range rescueCandidates {
				if score > rescueBestScore || (score == rescueBestScore && (rescueBestID == 0 || id < rescueBestID)) {
					rescueBestScore = score
					rescueBestID = id
				}
			}
			if rescueBestScore >= 2.0 {
				return rescueBestID, rescueBestScore
			}
		}
	}

	return 0, 0
}

func extractCalledFunctions(expr string) []string {
	skip := map[string]bool{
		"assertEqual": true, "assertEquals": true, "assertNotEqual": true,
		"assertTrue": true, "assertFalse": true, "assertNone": true,
		"assertIsNone": true, "assertIsNotNone": true, "assertRaises": true,
		"assertIn": true, "assertNotIn": true, "assertIs": true,
		"assertAlmostEqual": true, "assertGreater": true, "assertLess": true,
		"assertRegex": true, "assertCountEqual": true, "assertWarns": true,
		"assert_equal": true, "assert_raises": true, "assert_true": true,
		"assert_called_with": true, "assert_called_once_with": true,
		"expect": true, "assert": true, "require": true,
		"Equal": true, "NotEqual": true, "True": true, "False": true,
		"Nil": true, "NotNil": true, "Error": true, "NoError": true,
		"Contains": true, "HasPrefix": true, "HasSuffix": true, "DeepEqual": true,
		"toEqual": true, "toBe": true, "toThrow": true, "toHaveBeenCalled": true,
		"toContain": true, "toMatch": true, "toHaveLength": true,
		"is_ok": true, "is_err": true, "unwrap": true,
		"isinstance": true, "len": true, "hasattr": true, "getattr": true,
		"str": true, "int": true, "list": true, "dict": true,
		"type": true, "print": true, "repr": true,
		"set": true, "tuple": true, "sorted": true, "range": true,
	}
	receiverSkip := map[string]bool{
		"self": true, "this": true, "super": true, "t": true, "s": true,
		"fmt": true, "log": true, "os": true, "io": true, "json": true,
		"math": true, "strings": true, "bytes": true, "context": true,
		"http": true, "testing": true, "mock": true, "patch": true,
		"pytest": true, "np": true, "pd": true, "tf": true,
	}

	seen := map[string]bool{}
	var result []string

	dottedMatches := dottedCallPattern.FindAllStringSubmatch(expr, -1)
	for _, m := range dottedMatches {
		receiver, method := m[1], m[2]
		if !receiverSkip[receiver] && !skip[method] && len(method) > 1 && method[0] != '_' && !seen[method] {
			result = append(result, method)
			seen[method] = true
		}
	}

	matches := assertionCallPattern.FindAllStringSubmatch(expr, -1)
	for _, m := range matches {
		name := m[1]
		if !skip[name] && len(name) > 1 && name[0] != '_' && !seen[name] {
			result = append(result, name)
			seen[name] = true
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
		// Testfoo → foo (lowercase when rest starts lowercase — rare/invalid Go)
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
		name string
		dbID int64
		line int
		sig  string
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
			sig:  n.Signature,
		})
	}

	var props []*store.Property
	for _, members := range groups {
		if len(members) < 2 || len(members) > 200 {
			continue
		}
		for i := 0; i < len(members); i++ {
			for j := i + 1; j < len(members); j++ {
				a := members[i]
				b := members[j]
				if matchesSerdePair(a.name, b.name) {
					valA := fmt.Sprintf("partner:%s@file:%d", b.name, b.line)
					if b.sig != "" {
						sigB := b.sig
						if len(sigB) > 80 {
							sigB = sigB[:80]
						}
						valA += "|sig:" + sigB
					}
					valB := fmt.Sprintf("partner:%s@file:%d", a.name, a.line)
					if a.sig != "" {
						sigA := a.sig
						if len(sigA) > 80 {
							sigA = sigA[:80]
						}
						valB += "|sig:" + sigA
					}
					props = append(props, &store.Property{
						NodeID:     a.dbID,
						Kind:       "serialization_pair",
						Value:      valA,
						Line:       a.line,
						Confidence: 0.8,
					})
					props = append(props, &store.Property{
						NodeID:     b.dbID,
						Kind:       "serialization_pair",
						Value:      valB,
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

// twinPrefixes defines common structural twin verb pairs. Functions sharing a
// verb pair and the same remainder within the same scope are behavioral twins —
// modifying one without considering the other is a common source of bugs.
//
// The verbs are BARE (no trailing "_"). matchesTwinPair compares them against the
// first word of each name extracted by splitFirstWord, which handles snake_case,
// camelCase, and PascalCase uniformly. Bare verbs are required so the word-split
// matcher can demand an EXACT first-word match (e.g. "create"), not a substring
// (which would false-positive "created" against "create_").
var twinPrefixes = [][2]string{
	{"create", "update"}, {"create", "delete"},
	{"update", "delete"}, {"get", "set"},
	{"add", "remove"}, {"start", "stop"},
	{"open", "close"}, {"enable", "disable"},
	{"show", "hide"}, {"register", "unregister"},
	{"subscribe", "unsubscribe"}, {"lock", "unlock"},
	{"begin", "end"}, {"init", "cleanup"},
}

// detectStructuralTwins finds pairs of functions in the same scope whose names
// match a twin prefix pattern with the same suffix (e.g., create_user /
// delete_user). Each match produces a "structural_twin" property on both nodes.
func detectStructuralTwins(db *store.DB, allNodes []*store.Node, nodeDBIDs []int64) int {
	type nodeRef struct {
		name string
		dbID int64
		line int
		sig  string
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
			sig:  n.Signature,
		})
	}

	var props []*store.Property
	for _, members := range groups {
		if len(members) < 2 || len(members) > 200 {
			continue
		}
		for i := 0; i < len(members); i++ {
			for j := i + 1; j < len(members); j++ {
				a := members[i]
				b := members[j]
				if matched, pairType := matchesTwinPair(a.name, b.name); matched {
					props = append(props, &store.Property{
						NodeID:     a.dbID,
						Kind:       "structural_twin",
						Value:      fmt.Sprintf("twin: %s (%s pair)", b.name, pairType),
						Line:       a.line,
						Confidence: 0.7,
					})
					props = append(props, &store.Property{
						NodeID:     b.dbID,
						Kind:       "structural_twin",
						Value:      fmt.Sprintf("twin: %s (%s pair)", a.name, pairType),
						Line:       b.line,
						Confidence: 0.7,
					})
				}
			}
		}
	}

	if len(props) > 0 {
		if err := db.BatchInsertProperties(props); err != nil {
			log.Printf("WARNING: structural twin properties: %v", err)
		}
	}
	return len(props)
}

// splitFirstWord splits an identifier into its first word and the remainder,
// generalized across snake_case, camelCase, and PascalCase. Both returned parts
// are lower-cased so downstream comparison is case-insensitive.
//
//	create_user -> ("create", "user")   // snake_case: split at first '_' (idx>0)
//	createUser  -> ("create", "user")   // camelCase: split at lower->upper boundary
//	CreateUser  -> ("create", "user")   // PascalCase: same boundary (i>=1)
//	createuser  -> ("createuser", "")    // no boundary: whole name, empty remainder
//	get_value   -> ("get", "value")
func splitFirstWord(name string) (first, rest string) {
	// (a) snake_case: an underscore at index > 0 is the word boundary.
	if idx := strings.IndexByte(name, '_'); idx > 0 {
		return strings.ToLower(name[:idx]), strings.ToLower(name[idx+1:])
	}
	// (b) camelCase / PascalCase: first lower->upper boundary at i >= 1.
	for i := 1; i < len(name); i++ {
		c := name[i]
		if c >= 'A' && c <= 'Z' {
			return strings.ToLower(name[:i]), strings.ToLower(name[i:])
		}
	}
	// (c) no boundary: whole name is the first word, no remainder.
	return strings.ToLower(name), ""
}

// matchesTwinPair checks whether two function names form a structural twin pair.
// Each name is split into (firstWord, remainder) by splitFirstWord, so the matcher
// works uniformly across snake_case, camelCase, and PascalCase. A pair matches when
// the two first-words are the two halves of a twinPrefixes verb pair (in either
// order) AND the remainders are non-empty and identical.
//
// Requiring an EXACT first-word verb match (not a substring) plus an identical
// non-empty remainder prevents false positives: "created"/"deleted" split to
// ("created","")/("deleted","") whose first words are not the bare verbs and whose
// remainders are empty, so they do not match; "createUser"/"createPost" share a
// verb but differ in remainder, so they do not match.
func matchesTwinPair(nameA, nameB string) (bool, string) {
	firstA, restA := splitFirstWord(nameA)
	firstB, restB := splitFirstWord(nameB)
	if restA == "" || restA != restB {
		return false, ""
	}
	for _, pair := range twinPrefixes {
		p0 := pair[0]
		p1 := pair[1]
		if (firstA == p0 && firstB == p1) || (firstA == p1 && firstB == p0) {
			return true, p0 + "/" + p1
		}
	}
	return false, ""
}

// mineCochanges analyzes the last 500 git commits to find files that are
// frequently changed together. Pairs with >= 3 co-occurrences are stored
// in the cochanges table. Returns the number of pairs stored.
// Silently returns 0 if git is unavailable or the repo has no history.
func mineCochanges(db *store.DB, root string) int {
	// Two fixes vs the original: (1) "tformat:%x1e" is a VALID pretty-format —
	// bare "--format=COMMIT" is not a builtin format name, git rejects it
	// (exit 128), so this silently returned 0 on EVERY repo since b4761cc6
	// (2026-05-25). (2) the per-commit delimiter is now the ASCII record-
	// separator byte 0x1E, which cannot appear in a file path; the old literal
	// "COMMIT" delimiter corrupted co-change pairs whenever a tracked path
	// contained the substring "COMMIT".
	cmd := exec.Command("git", "log", "--name-only", "--format=tformat:%x1e", "-n", "500")
	cmd.Dir = root
	out, err := cmd.Output()
	if err != nil {
		return 0 // git unavailable, not a repo, or shallow clone with no history
	}

	cooccurrence := make(map[[2]string]int)
	commits := strings.Split(string(out), "\x1e")
	for _, commit := range commits {
		files := []string{}
		for _, line := range strings.Split(strings.TrimSpace(commit), "\n") {
			f := strings.TrimSpace(line)
			if f != "" {
				files = append(files, f)
			}
		}
		if len(files) > 50 {
			continue // skip mega-commits
		}
		for i := 0; i < len(files); i++ {
			for j := i + 1; j < len(files); j++ {
				a, b := files[i], files[j]
				if a > b {
					a, b = b, a // canonical order
				}
				cooccurrence[[2]string{a, b}]++
			}
		}
	}

	// Filter: min 3 co-occurrences
	filtered := make(map[[2]string]int)
	for pair, count := range cooccurrence {
		if count >= 3 {
			filtered[pair] = count
		}
	}

	if err := db.BatchInsertCochanges(filtered); err != nil {
		log.Printf("WARNING: co-change insert: %v", err)
	}
	return len(filtered)
}

var pyClassInhRe = regexp.MustCompile(`^\s*class\s+(\w+)\s*\(([^)]+)\)\s*:`)
var jsExtendsInhRe = regexp.MustCompile(`class\s+(\w+)(?:\s*<[^>]*>)?\s+extends\s+(\w+)`)

// Fix 3 (5-lang parity): Rust `impl Trait for Struct` → inheritance edge.
// Handles generics: `impl<T> Trait for Struct<T>`, `impl<T: Bound> Trait for Struct`.
// The regex captures the trait name (group 1) and struct name (group 2).
var rustImplForInhRe = regexp.MustCompile(`^\s*impl\s*(?:<[^>]*>\s*)?(\w+)(?:<[^>]*>)?\s+for\s+(\w+)`)

func buildInheritanceMap(files []walker.SourceFile, root string, nameIndex map[string][]int64, nodeMeta map[int64]resolver.NodeMeta) map[int64][]int64 {
	inhMap := make(map[int64][]int64)

	resolveClass := func(name string, filePath string) int64 {
		ids, ok := nameIndex[name]
		if !ok {
			return 0
		}
		for _, id := range ids {
			m, ok := nodeMeta[id]
			if ok && (m.Label == "Class" || m.Label == "Struct" || m.Label == "Interface") {
				if m.File == filePath {
					return id
				}
			}
		}
		for _, id := range ids {
			m, ok := nodeMeta[id]
			if ok && (m.Label == "Class" || m.Label == "Struct" || m.Label == "Interface") {
				return id
			}
		}
		return 0
	}

	for _, sf := range files {
		if sf.Language != "python" && sf.Language != "javascript" && sf.Language != "typescript" &&
			sf.Language != "java" && sf.Language != "kotlin" && sf.Language != "rust" {
			continue
		}
		absPath := sf.AbsPath
		if absPath == "" {
			absPath = filepath.Join(root, sf.Path)
		}
		f, err := os.Open(absPath)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 256*1024), 256*1024)
		for scanner.Scan() {
			line := scanner.Text()
			switch sf.Language {
			case "python":
				if m := pyClassInhRe.FindStringSubmatch(line); m != nil {
					childID := resolveClass(m[1], sf.Path)
					if childID == 0 {
						continue
					}
					for _, base := range strings.Split(m[2], ",") {
						base = strings.TrimSpace(base)
						if base == "" || base == "object" || base == "type" {
							continue
						}
						if idx := strings.Index(base, "["); idx > 0 {
							base = base[:idx]
						}
						if idx := strings.LastIndex(base, "."); idx > 0 {
							base = base[idx+1:]
						}
						parentID := resolveClass(base, "")
						if parentID != 0 && parentID != childID {
							inhMap[childID] = append(inhMap[childID], parentID)
						}
					}
				}
			case "javascript", "typescript", "java", "kotlin":
				if m := jsExtendsInhRe.FindStringSubmatch(line); m != nil {
					childID := resolveClass(m[1], sf.Path)
					parentID := resolveClass(m[2], "")
					if childID != 0 && parentID != 0 && childID != parentID {
						inhMap[childID] = append(inhMap[childID], parentID)
					}
				}
			case "rust":
				// Fix 3 (5-lang parity): `impl Trait for Struct` → inheritance.
				// Handles generics: `impl<T> Trait for Struct<T>`.
				// Group 1 = trait name, group 2 = struct name.
				if m := rustImplForInhRe.FindStringSubmatch(line); m != nil {
					traitName := m[1]
					structName := m[2]
					structID := resolveClass(structName, sf.Path)
					traitID := resolveClass(traitName, "")
					if structID != 0 && traitID != 0 && structID != traitID {
						inhMap[structID] = append(inhMap[structID], traitID)
					}
				}
			}
		}
		f.Close()
	}
	return inhMap
}

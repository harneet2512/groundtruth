package main

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// schemaVersionMultiRepo is the schema_version stamped ONLY on a multi-root graph.
// It intentionally differs from the single-root `schemaVersion` const so that an
// existing single-root-only reader (src/groundtruth/index/schema_version.py gates on
// EXACT equality with "v15.2-trust-tier") FAIL-CLOSES against a multi-repo graph
// rather than silently misreading it: once >1 repo is indexed, file_path alone no
// longer locates a node (identical relative paths collide across repos), so a reader
// that does not yet filter on repo_id MUST NOT consume it. The single-root path keeps
// the unchanged const, so every existing reader + the byte-identity golden are intact.
const schemaVersionMultiRepo = "v16-multirepo"

// repoIngest is the per-repo product of indexOneRepo the cross-repo pass consumes:
// the repo's partition id + root, its package COORDINATES, its parsed imports, and
// the file->first-node-id anchor map (repo-scoped, so colliding relative paths across
// repos never alias — the anchor is resolved inside the owning repo's own node set).
type repoIngest struct {
	repoID  int64
	root    string
	coord   resolver.RepoCoord
	imports []parser.ImportRef
	calls   []resolver.CrossRepoCall // QUALIFIED call sites (for Go-style pkg.Sym cross-repo CALLS)
	anchors map[string]int64         // repo-relative file path -> first (min) node DB id in THIS repo
}

// buildRootList combines the primary -root with the comma-separated -roots into an
// ordered, de-duplicated root list. -root is always first (repo_id 0), preserving its
// primacy. When -roots is empty the result is [root] (len 1) → the single-root path.
func buildRootList(root, roots string) []string {
	list := []string{root}
	seen := map[string]bool{root: true}
	for _, r := range strings.Split(roots, ",") {
		r = strings.TrimSpace(r)
		if r == "" || seen[r] {
			continue
		}
		seen[r] = true
		list = append(list, r)
	}
	return list
}

// runMultiRepo indexes >1 repository root into ONE graph.db with a repo_id partition
// key, then adds coordinate-verified cross-repo import edges. It is a SEPARATE path
// from the single-root main() body (which is left byte-identical): the single-root
// index never calls any of this. This is a lean FIRST slice — it lands the load-
// bearing surfaces (nodes, CALLS, CONTAINS, intra-repo IMPORTS, properties, repo_id
// partitioning, the repos table, and verified cross-repo edges) and logs the heavier
// optional passes as OWED for multi-repo (see the summary at the end).
func runMultiRepo(rootList []string, output string, maxFiles, workers int) error {
	start := time.Now()
	os.Remove(output)
	db, err := store.Open(output)
	if err != nil {
		return fmt.Errorf("open db: %w", err)
	}
	defer db.Close()

	fmt.Fprintf(os.Stderr, "MULTI-REPO index: %d roots -> %s\n", len(rootList), output)

	ingests := make([]repoIngest, 0, len(rootList))
	totalNodes, totalEdges := 0, 0
	for i, root := range rootList {
		repoID := int64(i)
		ing, n, e, err := indexOneRepo(db, root, repoID, maxFiles, workers)
		if err != nil {
			return fmt.Errorf("repo %d (%s): %w", repoID, root, err)
		}
		ingests = append(ingests, ing)
		totalNodes += n
		totalEdges += e
		if err := db.UpsertRepo(repoID, root, repoCommit(root)); err != nil {
			return fmt.Errorf("upsert repo %d: %w", repoID, err)
		}
		fmt.Fprintf(os.Stderr, "  repo %d: %s  (module=%q js=%v rust=%v)  nodes=%d edges=%d\n",
			repoID, root, ing.coord.GoModule, ing.coord.JSPackages, ing.coord.RustCrates, n, e)
	}

	// ── CROSS-REPO import edges (coordinate-verified, correct-or-quiet) ──────────
	crossEdges, tiers, err := resolveCrossRepo(db, ingests)
	if err != nil {
		return fmt.Errorf("cross-repo resolve: %w", err)
	}
	totalEdges += crossEdges
	fmt.Fprintf(os.Stderr, "  cross-repo edges: %d  (CERTIFIED=%d CANDIDATE=%d)\n",
		crossEdges, tiers["CERTIFIED"], tiers["CANDIDATE"])

	// Back-fill repo_id on every fact surface from its owning node (edges from source,
	// properties/content from node_id, etc.). Run AFTER all edges (intra + cross) exist.
	if err := db.StampDerivedRepoIDs(); err != nil {
		return fmt.Errorf("stamp derived repo_id: %w", err)
	}

	// FTS5 over the union node set (name surface; content-FTS is OWED for multi-repo).
	if err := db.PopulateFTS5(); err != nil {
		log.Printf("WARNING: multi-repo FTS5 population: %v", err)
	}

	// ── Metadata ────────────────────────────────────────────────────────────────
	elapsed := time.Since(start)
	db.SetMeta("roots", strings.Join(rootList, string(os.PathListSeparator)))
	db.SetMeta("repo_count", fmt.Sprintf("%d", len(rootList)))
	db.SetMeta("file_count", "0") // per-repo file counts are diagnostic-only in this slice
	db.SetMeta("node_count", fmt.Sprintf("%d", db.NodeCount()))
	db.SetMeta("edge_count", fmt.Sprintf("%d", db.EdgeCount()))
	db.SetMeta("indexer_version", "v16-multilang-multirepo")
	// Bumped schema_version — multi-repo ONLY (see schemaVersionMultiRepo doc).
	db.SetMeta("schema_version", schemaVersionMultiRepo)
	db.SetMeta("cross_repo_edges", fmt.Sprintf("%d", crossEdges))
	db.SetMeta("cross_repo_certified", fmt.Sprintf("%d", tiers["CERTIFIED"]))
	db.SetMeta("cross_repo_candidate", fmt.Sprintf("%d", tiers["CANDIDATE"]))

	if _, err := db.StampCompositeRevision(); err != nil {
		log.Printf("WARNING: multi-repo stamp composite revision: %v", err)
	}
	if err := db.ValidateForeignKeys(); err != nil {
		return fmt.Errorf("foreign-key validation failed: %w", err)
	}
	db.CheckpointWAL()

	fmt.Fprintf(os.Stderr, "\nMULTI-REPO done in %s\n", elapsed.Round(time.Millisecond))
	fmt.Fprintf(os.Stderr, "  Repos:  %d\n  Nodes:  %d\n  Edges:  %d (incl %d cross-repo)\n",
		db.RepoCount(), db.NodeCount(), db.EdgeCount(), crossEdges)
	fmt.Fprintf(os.Stderr, "  OWED (multi-repo slice): assertions target-resolution, transitive closure, "+
		"co-change mining, content_fts, API/relationship/serde edges, edge_metadata sub-table, "+
		"incremental -file repo selector, Python multi-repo consumer (repo_id-aware readers).\n")

	fmt.Printf(`{"repos":%d,"nodes":%d,"edges":%d,"cross_repo_edges":%d,"cross_repo_certified":%d,"cross_repo_candidate":%d,"time_ms":%d}`+"\n",
		db.RepoCount(), db.NodeCount(), db.EdgeCount(), crossEdges,
		tiers["CERTIFIED"], tiers["CANDIDATE"], elapsed.Milliseconds())
	return nil
}

// indexOneRepo runs the CORE per-repo passes (structure/definitions/calls/contains/
// intra-repo imports/properties) for one root, inserts with a repo_id stamp, and
// returns the data the cross-repo pass needs. Intra-repo resolution reuses the exact
// same resolver/store functions the single-root path uses, so per-repo edges match a
// standalone single-root index of that repo (up to global-id shift).
func indexOneRepo(db *store.DB, root string, repoID int64, maxFiles, workers int) (repoIngest, int, int, error) {
	ing := repoIngest{repoID: repoID, root: root, coord: resolver.ExtractRepoCoord(repoID, root), anchors: map[string]int64{}}

	files, err := walker.Walk(root, maxFiles)
	if err != nil {
		return ing, 0, 0, fmt.Errorf("walk: %w", err)
	}
	filePaths := make([]string, len(files))
	fileLangs := make([]string, len(files))
	for i, sf := range files {
		filePaths[i] = sf.Path
		fileLangs[i] = sf.Language
	}

	// Parse (sequential — the cross-repo slice favours simplicity over the parallel
	// worker pool; the single-root path keeps its parallelism untouched).
	results := make([]*parser.ParseResult, len(files))
	for i, sf := range files {
		isTest := walker.IsTestFile(sf.Path) || walker.IsNonSourceFile(sf.Path)
		pr, perr := parser.ParseFile(sf, isTest)
		if perr == nil {
			results[i] = pr
		}
	}

	var allNodePtrs []*store.Node
	var allCalls []parser.CallRef
	var allImports []parser.ImportRef
	var allProps []parser.PropertyRef
	callerNodeIndexMap := make(map[int]int)

	globalNodeIdx := 0
	for _, result := range results {
		if result == nil {
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
			if p.NodeIdx >= 0 && p.NodeIdx < len(allNodePtrs) && allNodePtrs[p.NodeIdx].IsTest {
				switch p.Kind {
				case "string_literals", "body_terms", "calls":
					continue
				}
			}
			allProps = append(allProps, p)
		}
		allImports = append(allImports, result.Imports...)
	}
	ing.imports = allImports

	// Parent fixups: zero ParentID for insert, restore after.
	parentFixups := make(map[int]int64)
	for i, n := range allNodePtrs {
		if n.ParentID > 0 {
			parentFixups[i] = n.ParentID
			n.ParentID = 0
		}
	}

	nodeDBIDs, err := db.BatchInsertNodes(allNodePtrs)
	if err != nil {
		return ing, 0, 0, fmt.Errorf("batch insert nodes: %w", err)
	}
	// Stamp THIS repo's node ids with its partition key.
	if err := db.StampNodeRepoIDs(nodeDBIDs, repoID); err != nil {
		return ing, 0, 0, fmt.Errorf("stamp node repo_id: %w", err)
	}
	// Fixup parent ids (sorted for determinism, mirroring main).
	fixupIdxs := make([]int, 0, len(parentFixups))
	for nodeIdx := range parentFixups {
		fixupIdxs = append(fixupIdxs, nodeIdx)
	}
	sort.Ints(fixupIdxs)
	for _, nodeIdx := range fixupIdxs {
		pidx := int(parentFixups[nodeIdx]) - 1
		if pidx >= 0 && pidx < len(nodeDBIDs) && nodeDBIDs[pidx] > 0 {
			db.UpdateParentID(nodeDBIDs[nodeIdx], nodeDBIDs[pidx])
		}
	}
	// Restore ParentID in-memory for resolution.
	for nodeIdx, parentGlobalIdx := range parentFixups {
		pidx := int(parentGlobalIdx) - 1
		if pidx >= 0 && pidx < len(nodeDBIDs) && nodeIdx < len(allNodePtrs) {
			allNodePtrs[nodeIdx].ParentID = nodeDBIDs[pidx]
		}
	}

	// Build the repo-scoped file->first-node anchor for cross-repo import sources.
	for i, np := range allNodePtrs {
		id := nodeDBIDs[i]
		if id == 0 || np.FilePath == "" {
			continue
		}
		if cur, ok := ing.anchors[np.FilePath]; !ok || id < cur {
			ing.anchors[np.FilePath] = id
		}
	}

	allNodes := make([]store.Node, len(allNodePtrs))
	for i, np := range allNodePtrs {
		allNodes[i] = *np
	}
	nameIndex, fileIndex := resolver.BuildNameIndex(db, allNodes, nodeDBIDs)
	fileMap := resolver.BuildFileMap(filePaths, fileLangs)

	// Per-root coordinate registration (same calls the single-root path makes).
	if goModPath := resolver.FindGoModulePath(root); goModPath != "" {
		resolver.RegisterGoModulePaths(fileMap, goModPath)
	}
	if tsCfg := resolver.ParseTSConfig(root); tsCfg != nil {
		resolver.RegisterTSConfigPaths(fileMap, tsCfg)
	}
	resolver.RegisterJSPackagePaths(fileMap, root)
	resolver.RegisterGoPackageNames(fileMap, filePaths, fileLangs)
	resolver.RegisterGoVendorPaths(fileMap)
	resolver.RegisterRustCratePaths(fileMap, root)
	resolver.SetReExportGraphIncomplete(false)

	callerDBIDs := make([]int64, len(allCalls))
	for i := range allCalls {
		if globalIdx, ok := callerNodeIndexMap[i]; ok && globalIdx < len(nodeDBIDs) {
			callerDBIDs[i] = nodeDBIDs[globalIdx]
		}
	}
	// Collect QUALIFIED call sites (pkg.Sym / crate::Sym) with their caller node id —
	// the cross-repo CALLS pass resolves those whose qualifier is a coordinate-bound
	// import alias into another repo. Only qualified callees can be cross-repo.
	for i, call := range allCalls {
		if i >= len(callerDBIDs) || callerDBIDs[i] == 0 || call.CalleeName == "" {
			continue
		}
		if !strings.Contains(call.CalleeQualified, ".") && !strings.Contains(call.CalleeQualified, "::") {
			continue
		}
		ing.calls = append(ing.calls, resolver.CrossRepoCall{
			RepoID:          repoID,
			CallerID:        callerDBIDs[i],
			CalleeName:      call.CalleeName,
			CalleeQualified: call.CalleeQualified,
			File:            call.File,
			Line:            call.Line,
		})
	}
	nodeMeta := resolver.BuildNodeMeta(allNodes, nodeDBIDs)
	if len(allProps) > 0 {
		resolver.SetParamTypeIndex(resolver.BuildParamTypeIndex(allProps, nodeDBIDs))
		resolver.SetFieldTypeIndex(resolver.BuildFieldTypeIndex(allProps, nodeDBIDs))
		classNames := make(map[string]bool)
		for _, m := range nodeMeta {
			switch m.Label {
			case "Class", "Struct", "Type", "Enum", "Interface":
				classNames[m.Name] = true
			}
		}
		resolver.SetReturnShapeIndex(resolver.BuildReturnShapeIndex(allProps, nodeDBIDs, classNames))
	}
	if inhMap := buildInheritanceMap(files, root, nameIndex, nodeMeta); len(inhMap) > 0 {
		resolver.SetInheritanceMap(inhMap)
	}
	resolver.ExpandRustCrateImports(allImports, filePaths, fileLangs, root)

	resolved := resolver.Resolve(allCalls, nameIndex, fileIndex, callerDBIDs, allImports, fileMap, nodeMeta)
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
			Metadata:           receiverEdgeMetadata(rc),
			TrustTier:          rc.TrustTier,
			CandidateCount:     rc.CandidateCount,
			EvidenceType:       rc.EvidenceType,
			VerificationStatus: "unverified",
		}
	}
	if err := db.BatchInsertEdges(edgePtrs); err != nil {
		return ing, 0, 0, fmt.Errorf("batch insert edges: %w", err)
	}
	edgeCount := len(edgePtrs)

	// CONTAINS edges (sorted, mirroring main).
	var containsPtrs []*store.Edge
	fixupNodeIdxs := make([]int, 0, len(parentFixups))
	for nodeIdx := range parentFixups {
		fixupNodeIdxs = append(fixupNodeIdxs, nodeIdx)
	}
	sort.Ints(fixupNodeIdxs)
	for _, nodeIdx := range fixupNodeIdxs {
		pidx := int(parentFixups[nodeIdx]) - 1
		if pidx >= 0 && pidx < len(nodeDBIDs) && nodeIdx < len(nodeDBIDs) {
			parentDBID, childDBID := nodeDBIDs[pidx], nodeDBIDs[nodeIdx]
			if parentDBID > 0 && childDBID > 0 {
				fp := ""
				if nodeIdx < len(allNodePtrs) {
					fp = allNodePtrs[nodeIdx].FilePath
				}
				containsPtrs = append(containsPtrs, &store.Edge{
					SourceID: parentDBID, TargetID: childDBID, Type: "CONTAINS",
					SourceFile: fp, ResolutionMethod: "structural", Confidence: 1.0,
					TrustTier: "CERTIFIED", EvidenceType: "parent_id", VerificationStatus: "verified",
				})
			}
		}
	}
	if len(containsPtrs) > 0 {
		if err := db.BatchInsertEdges(containsPtrs); err != nil {
			log.Printf("WARNING: repo %d containment edges: %v", repoID, err)
		} else {
			edgeCount += len(containsPtrs)
		}
	}

	// Intra-repo IMPORTS edges (reuses the single-root machinery, correct-or-quiet).
	if n, ierr := resolver.ResolveImports(db, allImports, fileMap, files); ierr != nil {
		log.Printf("WARNING: repo %d IMPORTS edges: %v", repoID, ierr)
	} else {
		edgeCount += n
	}

	// Properties.
	propPtrs := make([]*store.Property, 0, len(allProps))
	for _, p := range allProps {
		if p.NodeIdx >= 0 && p.NodeIdx < len(nodeDBIDs) {
			propPtrs = append(propPtrs, &store.Property{
				NodeID: nodeDBIDs[p.NodeIdx], Kind: p.Kind, Value: p.Value, Line: p.Line, Confidence: p.Confidence,
			})
		}
	}
	if err := db.BatchInsertProperties(propPtrs); err != nil {
		log.Printf("WARNING: repo %d properties: %v", repoID, err)
	}

	// Re-stamp: ResolveImports/promote may have inserted edges AFTER the node stamp,
	// but derived repo_id is back-filled globally at the end (StampDerivedRepoIDs), so
	// nothing to do here. Return node count = len(nodeDBIDs).
	return ing, len(nodeDBIDs), edgeCount, nil
}

// resolveCrossRepo builds the cross-repo import inputs from the DB's repo-stamped
// node set + each repo's parsed imports/anchors, runs the coordinate-verified
// resolver, inserts the resulting edges, and returns (count, per-tier counts).
func resolveCrossRepo(db *store.DB, ingests []repoIngest) (int, map[string]int, error) {
	tiers := map[string]int{}
	rows, err := db.CrossRepoNodeRows()
	if err != nil {
		return 0, tiers, err
	}
	nodes := make([]resolver.CrossRepoNode, 0, len(rows))
	for _, r := range rows {
		nodes = append(nodes, resolver.CrossRepoNode{
			ID: r.ID, RepoID: r.RepoID, Name: r.Name, FilePath: r.FilePath, IsExported: r.IsExported,
		})
	}

	var coords []resolver.RepoCoord
	var imports []resolver.CrossRepoImport
	var calls []resolver.CrossRepoCall
	for _, ing := range ingests {
		coords = append(coords, ing.coord)
		for _, imp := range ing.imports {
			imports = append(imports, resolver.CrossRepoImport{
				RepoID:         ing.repoID,
				File:           imp.File,
				ModulePath:     imp.ModulePath,
				ImportedName:   imp.ImportedName,
				Line:           imp.Line,
				SourceAnchorID: ing.anchors[imp.File],
			})
		}
		calls = append(calls, ing.calls...)
	}

	// Two coordinate-verified cross-repo passes: import→exported-symbol (IMPORTS edges;
	// named imports — JS/TS/Rust/Python) and qualified-call→exported-symbol (CALLS
	// edges; Go-style pkg.Sym where the import is package-level). Both correct-or-quiet.
	importEdges := resolver.ResolveCrossRepoImports(imports, nodes, coords)
	callEdges := resolver.ResolveCrossRepoCalls(calls, imports, nodes, coords)

	edgePtrs := make([]*store.Edge, 0, len(importEdges)+len(callEdges))
	mk := func(xe resolver.CrossRepoEdge, typ string) *store.Edge {
		tiers[xe.TrustTier]++
		return &store.Edge{
			SourceID:           xe.SourceID,
			TargetID:           xe.TargetID,
			Type:               typ,
			SourceLine:         xe.SourceLine,
			SourceFile:         xe.SourceFile,
			ResolutionMethod:   xe.Method,
			Confidence:         xe.Confidence,
			Metadata:           xe.Metadata,
			TrustTier:          xe.TrustTier,
			CandidateCount:     xe.CandidateCount,
			EvidenceType:       xe.EvidenceType,
			VerificationStatus: "verified",
		}
	}
	for _, xe := range importEdges {
		edgePtrs = append(edgePtrs, mk(xe, "IMPORTS"))
	}
	for _, xe := range callEdges {
		edgePtrs = append(edgePtrs, mk(xe, "CALLS"))
	}
	if len(edgePtrs) == 0 {
		return 0, tiers, nil
	}
	if err := db.BatchInsertEdges(edgePtrs); err != nil {
		return 0, tiers, fmt.Errorf("insert cross-repo edges: %w", err)
	}
	return len(edgePtrs), tiers, nil
}

// repoCommit returns the repo's VCS HEAD (best-effort; "" when not a git repo or git
// is unavailable). Stored in repos."commit" as forensic provenance.
func repoCommit(root string) string {
	out, err := exec.Command("git", "-C", root, "rev-parse", "HEAD").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

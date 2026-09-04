package resolver

import (
	"database/sql"
	"fmt"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ===========================================================================
// Pass 4f-sibling — IMPORTS edges (gt_gt.md §2.6, the depth/traversability bar).
//
// Materialize the IMPORTS relationship — importer file -> imported module/symbol
// — as a TRAVERSABLE `edges` row, so BFS over CALLS/IMPORTS (gt_gt §1) can hop
// from a file to the modules it depends on. The import data is ALREADY parsed
// (allImports, parser.ImportRef) and ALREADY resolved to real repo files by the
// exact same machinery the import-verified CALLS path uses (buildImportIndex /
// resolveModulePath, resolver.go:1440/1503). This pass FRESH-EXTRACTS the edge
// from that existing resolution — no re-parse, no AST re-walk.
//
// Research basis (import-as-navigable-edge precedent):
//   • SCIP — Sourcegraph Code Intelligence Protocol (2022): imports/defs/refs are
//     first-class language-agnostic relationships in one index.
//   • LSIF — Microsoft/Sourcegraph (2019): imports pre-computed as graph edges so
//     navigation is a traversal, not a re-parse.
//   • Kythe — Google (2015): source files connected to imported modules/symbols by
//     typed edges (childof/ref/imports).
//   • Stack Graphs — GitHub (2021) + Scope Graphs — van Antwerpen et al., ESOP 2015:
//     name/import resolution as reachability; an unresolved (external) name simply
//     has NO resolution path — grounds the correct-or-quiet rule below.
//
// CONTRACT (all enforced here):
//   • CORRECT-OR-QUIET / NON-INVENTION (gt_gt §2.6, the §2.5 stdlib-shadow bar):
//     mint an IMPORTS edge ONLY when the import resolves to an in-repo file/symbol
//     that maps to a real node id. An external/stdlib/unresolved import
//     (resolveModulePath returns empty) gets NO edge — never laundered.
//   • TRUST POLICY mirrors the import-verified CALLS path (resolver.go:783-803):
//     exactly one resolved target => conf 1.0 CERTIFIED; >1 candidate file with no
//     disambiguation => conf 0.6 CANDIDATE evidence='ast_import_ambiguous'.
//   • SourceFile = the importer file path (MANDATORY): the incremental -file delete
//     (DeleteFileEdgesAndNodesTx, incremental.go:284) is source_file-keyed, so a
//     stale IMPORTS edge is removed before re-emit only if SourceFile == imp.File.
//   • GENERALIZED: only tree-sitter import extraction is per-language (already done
//     by the 6 Tier-1 extractors feeding allImports). The resolution + emission are
//     ONE language-agnostic path. No task IDs, no gold labels, no per-repo logic.
//   • ADDITIVE: written alongside existing edges; no existing pass is modified.
// ===========================================================================

// ResolveImports emits one IMPORTS edge per import that resolves to a real in-repo
// node, REUSING the existing import resolution (buildImportIndex / resolveModulePath).
// Returns the number of IMPORTS edges inserted. Full-index path (uses db.BatchInsertEdges).
func ResolveImports(db *store.DB, imports []parser.ImportRef,
	fileMap map[string][]string, files []walker.SourceFile) (int, error) {
	if len(imports) == 0 {
		return 0, nil
	}

	// Source anchor: importer file -> its first node id (the RE_EXPORTS / API-edge
	// file-level anchor pattern, api_edges.go:308). Works for every file: a File-label
	// node for zero-symbol files, the first symbol otherwise.
	fileNodeMap := buildFileNodeMap(db, files)
	if len(fileNodeMap) == 0 {
		return 0, nil
	}

	// FILE->SYMBOL target index: targetFile -> importedName -> nodeID. Excludes
	// File-label nodes (those are the file anchors, targeted via fileNodeMap, never
	// by name lookup — mirrors BuildNameIndex #B3, resolver.go:2134).
	fileSymbolIndex, err := buildImportSymbolIndex(db)
	if err != nil {
		return 0, fmt.Errorf("imports: build symbol index: %w", err)
	}

	// REUSE the existing import resolution: callerFile -> importedName -> []targetFiles
	// of REAL repo files. resolveModulePath returns empty for external/stdlib, so an
	// external import never reaches the emit step (the non-invention guard, free).
	importIndex := buildImportIndex(imports, fileMap)

	edges := make([]*store.Edge, 0)
	seen := make(map[edgeKey]bool)

	for _, imp := range imports {
		if imp.ImportedName == "" {
			continue
		}
		byName, ok := importIndex[imp.File]
		if !ok {
			continue
		}
		targetFiles := byName[imp.ImportedName]
		if len(targetFiles) == 0 {
			continue // external / stdlib / unresolved -> NO edge (correct-or-quiet)
		}
		sourceID := fileNodeMap[imp.File]
		if sourceID == 0 {
			continue // importer file has no anchorable node
		}

		// Determine the target node + trust. Prefer FILE->SYMBOL (more precise):
		// a specific imported name that maps to a node in exactly one target file.
		targetID, candidateCount, conf, evidence := resolveImportTarget(
			imp.ImportedName, targetFiles, fileSymbolIndex, fileNodeMap)
		if targetID == 0 || targetID == sourceID {
			continue
		}

		key := edgeKey{sourceID: sourceID, targetID: targetID, typ: "IMPORTS"}
		if seen[key] {
			continue
		}
		seen[key] = true

		edges = append(edges, &store.Edge{
			SourceID:           sourceID,
			TargetID:           targetID,
			Type:               "IMPORTS",
			SourceLine:         imp.Line,
			SourceFile:         imp.File, // MANDATORY: incremental delete is source_file-keyed
			ResolutionMethod:   "import",
			Confidence:         conf,
			CandidateCount:     candidateCount,
			TrustTier:          tierFor(conf),
			EvidenceType:       evidence,
			VerificationStatus: "verified",
		})
	}

	if len(edges) == 0 {
		return 0, nil
	}
	if err := db.BatchInsertEdges(edges); err != nil {
		return 0, fmt.Errorf("imports: insert edges: %w", err)
	}
	return len(edges), nil
}

// resolveImportTarget picks the IMPORTS edge target node + trust, mirroring the
// import-verified CALLS policy (resolver.go:783-803). FILE->SYMBOL is preferred
// when the imported name (not '*') resolves to a symbol node; otherwise FILE->FILE
// anchors on the imported file's first node. Trust:
//   - exactly one resolved target  => conf 1.0 CERTIFIED, evidence 'ast_import'
//   - >1 distinct target files      => conf 0.6 CANDIDATE, evidence 'ast_import_ambiguous'
//
// candidateCount is the number of distinct resolved target files (the ambiguity the
// CALLS path counts), NOT the symbol count.
func resolveImportTarget(importedName string, targetFiles []string,
	fileSymbolIndex map[string]map[string]int64, fileNodeMap map[string]int64) (int64, int, float64, string) {
	// Distinct target files (buildImportIndex appends, so duplicates are possible).
	distinct := make([]string, 0, len(targetFiles))
	seenFile := make(map[string]bool)
	for _, tf := range targetFiles {
		if seenFile[tf] {
			continue
		}
		seenFile[tf] = true
		distinct = append(distinct, tf)
	}
	if len(distinct) == 0 {
		return 0, 0, 0, ""
	}

	candidateCount := len(distinct)
	conf := 1.0
	evidence := "ast_import"
	if candidateCount > 1 {
		conf = 0.6
		evidence = "ast_import_ambiguous"
	}

	// FILE->SYMBOL: a specific named import that resolves to a symbol node in the
	// FIRST target file that declares it. Strictly more precise than FILE->FILE.
	if importedName != "*" {
		for _, tf := range distinct {
			if byName, ok := fileSymbolIndex[tf]; ok {
				if nid := byName[importedName]; nid != 0 {
					return nid, candidateCount, conf, evidence
				}
			}
		}
	}

	// FILE->FILE: anchor on the (first / disambiguated) target file's first node.
	// For a single resolved file this is the canonical importer->imported-file edge;
	// for >1 it is the deterministic first-listed file at CANDIDATE trust.
	for _, tf := range distinct {
		if tid := fileNodeMap[tf]; tid != 0 {
			return tid, candidateCount, conf, evidence
		}
	}
	return 0, 0, 0, ""
}

// ResolveImportsTx is the incremental (-file) variant: it resolves the reindexed
// file's imports against the IN-MEMORY node snapshot (allNodes/allIDs, which the
// caller has already filtered to exclude the stale file and include the fresh
// nodes) and inserts the IMPORTS edges inside the caller's transaction. Same
// trust/correct-or-quiet policy as ResolveImports. Returns the count inserted.
//
// The stale IMPORTS edges for this file are removed before re-emit by
// DeleteFileEdgesAndNodesTx (incremental.go:284, source_file-keyed) — correct by
// construction because every IMPORTS edge sets SourceFile = imp.File.
func ResolveImportsTx(tx *sql.Tx, imports []parser.ImportRef, fileMap map[string][]string,
	allNodes []store.Node, allIDs []int64) (int, error) {
	if len(imports) == 0 {
		return 0, nil
	}

	// Source anchor (file -> first node id) + FILE->SYMBOL target index, built from
	// the in-memory snapshot (no DB query — the snapshot is the post-reindex truth).
	fileNodeMap := make(map[string]int64)
	fileSymbolIndex := make(map[string]map[string]int64)
	for i, n := range allNodes {
		if i >= len(allIDs) {
			break
		}
		id := allIDs[i]
		if id == 0 || n.FilePath == "" {
			continue
		}
		// First-id-wins file anchor (matches buildFileNodeMap's ORDER BY id LIMIT 1;
		// allNodes is appended filtered-then-fresh, fresh ids are larger, so an
		// existing target file keeps its smaller anchor id — deterministic).
		if cur, ok := fileNodeMap[n.FilePath]; !ok || id < cur {
			fileNodeMap[n.FilePath] = id
		}
		// FILE->SYMBOL: exclude File-label anchors from name targeting.
		if IsCodeSymbolLabel(n.Label) && n.Name != "" {
			byName, ok := fileSymbolIndex[n.FilePath]
			if !ok {
				byName = make(map[string]int64)
				fileSymbolIndex[n.FilePath] = byName
			}
			if _, exists := byName[n.Name]; !exists {
				byName[n.Name] = id
			}
		}
	}
	if len(fileNodeMap) == 0 {
		return 0, nil
	}

	importIndex := buildImportIndex(imports, fileMap)
	edges := make([]*store.Edge, 0)
	seen := make(map[edgeKey]bool)

	for _, imp := range imports {
		if imp.ImportedName == "" {
			continue
		}
		byName, ok := importIndex[imp.File]
		if !ok {
			continue
		}
		targetFiles := byName[imp.ImportedName]
		if len(targetFiles) == 0 {
			continue // external / stdlib / unresolved -> NO edge
		}
		sourceID := fileNodeMap[imp.File]
		if sourceID == 0 {
			continue
		}
		targetID, candidateCount, conf, evidence := resolveImportTarget(
			imp.ImportedName, targetFiles, fileSymbolIndex, fileNodeMap)
		if targetID == 0 || targetID == sourceID {
			continue
		}
		key := edgeKey{sourceID: sourceID, targetID: targetID, typ: "IMPORTS"}
		if seen[key] {
			continue
		}
		seen[key] = true
		edges = append(edges, &store.Edge{
			SourceID:           sourceID,
			TargetID:           targetID,
			Type:               "IMPORTS",
			SourceLine:         imp.Line,
			SourceFile:         imp.File,
			ResolutionMethod:   "import",
			Confidence:         conf,
			CandidateCount:     candidateCount,
			TrustTier:          tierFor(conf),
			EvidenceType:       evidence,
			VerificationStatus: "verified",
		})
	}

	if len(edges) == 0 {
		return 0, nil
	}
	if err := store.BatchInsertEdgesTx(tx, edges); err != nil {
		return 0, fmt.Errorf("imports(incremental): insert edges: %w", err)
	}
	return len(edges), nil
}

// buildImportSymbolIndex reads the nodes table ONCE and returns
// targetFile -> symbolName -> nodeID for FILE->SYMBOL import targeting. File-label
// nodes are excluded (they are file anchors, not import targets). First-id wins on a
// duplicate (file,name) pair for determinism. Read-only.
func buildImportSymbolIndex(db *store.DB) (map[string]map[string]int64, error) {
	tx, err := db.BeginTx()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	rows, err := tx.Query(`SELECT id, label, name, file_path FROM nodes`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make(map[string]map[string]int64)
	for rows.Next() {
		var id int64
		var label, name, file string
		if err := rows.Scan(&id, &label, &name, &file); err != nil {
			continue
		}
		if !IsCodeSymbolLabel(label) || name == "" || file == "" {
			continue
		}
		byName, ok := out[file]
		if !ok {
			byName = make(map[string]int64)
			out[file] = byName
		}
		if _, exists := byName[name]; !exists {
			byName[name] = id
		}
	}
	return out, nil
}

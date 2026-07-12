package resolver

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ===========================================================================
// SM-9a — CROSS-REPO import resolution (CONFIDENCE-TIERED, CORRECT-OR-QUIET).
//
// GT's multi-repo differentiator is NOT "more cross-repo edges" — it is
// cross-repo edges that carry an HONEST trust tier and STAY SILENT when GT is
// guessing. This resolver only ever emits an edge that is VERIFIED through
// package coordinates:
//
//	an import in repo A whose module path is repo B's registered package
//	coordinate (go.mod `module`, package.json `name`, or Cargo.toml crate) AND
//	binds to an EXPORTED symbol in repo B's corresponding package.
//
// That is a FACT: resolution_method `import_cross_repo`, CERTIFIED/CANDIDATE,
// with repo PROVENANCE on the edge (target repo id/root + how it resolved).
//
// A mere NAME match across repos is NEVER emitted. Two repos that happen to
// export the same symbol name but have NO coordinate import between them produce
// ZERO cross-repo edges (correct-or-quiet by construction — this function never
// does cross-repo name matching, mirroring the intra-repo `name_match` discipline
// that floors/drops unverified guesses rather than laundering them as facts).
//
// Research basis (import-as-verified-edge, same precedent as imports.go): SCIP /
// LSIF / Kythe / Stack Graphs — an import resolves to a def by name+scope, and an
// unresolved (externally-bound) name simply has no resolution path → no edge.
// ===========================================================================

// RepoCoord is one repository's PACKAGE COORDINATES — the identifiers other repos
// import it by. A cross-repo import is verified iff its module path matches one of
// these. RepoID is the repos.id partition key; Root is the on-disk root.
type RepoCoord struct {
	RepoID     int64
	Root       string
	GoModule   string   // go.mod `module <path>` (e.g. github.com/org/repob)
	JSPackages []string // package.json `name` (may be several in a monorepo)
	RustCrates []string // Cargo.toml `[package] name` / workspace member names
}

// CrossRepoNode is the resolver-local node projection (decoupled from store): a
// symbol's identity, the repo it lives in, its name, its repo-relative file path,
// and whether it is exported (only exported symbols are legal import targets).
type CrossRepoNode struct {
	ID         int64
	RepoID     int64
	Name       string
	FilePath   string
	IsExported bool
}

// CrossRepoImport is one import statement in a specific repo, pre-anchored to the
// importing file's source node (SourceAnchorID — the first node of the importer
// file, mirroring the imports.go file-anchor pattern).
type CrossRepoImport struct {
	RepoID         int64
	File           string
	ModulePath     string
	ImportedName   string
	Line           int
	SourceAnchorID int64
}

// CrossRepoEdge is a coordinate-verified cross-repo import edge, ready to become a
// store.Edge. TargetRepoID + Metadata carry the repo provenance a consumer needs to
// tell "verified cross-repo import" from a guess (there is no guess variant here).
type CrossRepoEdge struct {
	SourceID       int64
	TargetID       int64
	Method         string
	Confidence     float64
	TrustTier      string
	CandidateCount int
	EvidenceType   string
	Metadata       string
	SourceFile     string
	SourceLine     int
	TargetRepoID   int64
}

const (
	// crossRepoCertifiedConf: a coordinate-verified import binding to exactly one
	// exported symbol in the target package. Mirrors verified_unique (0.95 CERTIFIED):
	// the module identity is proven by go.mod/package.json/Cargo.toml and the symbol
	// is the unique exported target — a fact, but not a compiler-verified 1.0.
	crossRepoCertifiedConf = 0.95
	// crossRepoAmbiguousConf: the coordinate matched and an exported symbol of the
	// name exists, but >1 candidate does — CANDIDATE (still ≥ the 0.5 fact gate, never
	// CERTIFIED). Mirrors ast_import_ambiguous (0.6).
	crossRepoAmbiguousConf = 0.6
)

// ResolveCrossRepoImports produces coordinate-verified cross-repo import edges.
// Correct-or-quiet: an import that does not match another repo's package
// coordinate, or matches but binds to no EXPORTED symbol in the corresponding
// package, yields NO edge. Deterministic (inputs sorted; candidates content-sorted).
func ResolveCrossRepoImports(imports []CrossRepoImport, nodes []CrossRepoNode, coords []RepoCoord) []CrossRepoEdge {
	if len(imports) == 0 || len(nodes) == 0 || len(coords) == 0 {
		return nil
	}

	// Index coordinates by repo, and exported nodes by (repo, name). ONLY exported
	// symbols are indexed as targets — a non-exported def is not importable across a
	// package/repo boundary, so it can never be a cross-repo target (correct-or-quiet).
	coordByRepo := make(map[int64]RepoCoord, len(coords))
	for _, c := range coords {
		coordByRepo[c.RepoID] = c
	}
	type exp struct {
		id       int64
		filePath string
	}
	exportedByRepoName := make(map[int64]map[string][]exp)
	for _, n := range nodes {
		if !n.IsExported || n.Name == "" {
			continue
		}
		byName, ok := exportedByRepoName[n.RepoID]
		if !ok {
			byName = make(map[string][]exp)
			exportedByRepoName[n.RepoID] = byName
		}
		byName[n.Name] = append(byName[n.Name], exp{id: n.ID, filePath: n.FilePath})
	}

	// Deterministic iteration: sort the import list by a total key before resolving,
	// and sort each coord's repo scan order once.
	imps := make([]CrossRepoImport, len(imports))
	copy(imps, imports)
	sort.Slice(imps, func(i, j int) bool {
		a, b := imps[i], imps[j]
		if a.RepoID != b.RepoID {
			return a.RepoID < b.RepoID
		}
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		if a.ModulePath != b.ModulePath {
			return a.ModulePath < b.ModulePath
		}
		return a.ImportedName < b.ImportedName
	})
	repoIDs := make([]int64, 0, len(coords))
	for _, c := range coords {
		repoIDs = append(repoIDs, c.RepoID)
	}
	sort.Slice(repoIDs, func(i, j int) bool { return repoIDs[i] < repoIDs[j] })

	var edges []CrossRepoEdge
	seen := make(map[[2]int64]bool) // dedup (source,target)

	for _, imp := range imps {
		// A named symbol is required to bind a FACT (whole-module `*`/"" import →
		// owed, see file header). No anchorable source node → nothing to attach to.
		if imp.ImportedName == "" || imp.ImportedName == "*" || imp.SourceAnchorID == 0 {
			continue
		}
		// Find the target repo whose coordinate this import path matches (never the
		// importing repo itself). Deterministic: first match in sorted repo order.
		var (
			matched    bool
			targetRepo int64
			subpath    string
			resolvedBy string
			coordVal   string
		)
		for _, rid := range repoIDs {
			if rid == imp.RepoID {
				continue
			}
			c := coordByRepo[rid]
			if sp, by, cv, ok := matchModuleToCoord(imp.ModulePath, c); ok {
				matched, targetRepo, subpath, resolvedBy, coordVal = true, rid, sp, by, cv
				break
			}
		}
		if !matched {
			continue // NOT a coordinate-verified cross-repo import → correct-or-quiet
		}

		byName := exportedByRepoName[targetRepo]
		if byName == nil {
			continue
		}
		cands := byName[imp.ImportedName]
		if len(cands) == 0 {
			continue // module matched but the name is not EXPORTED there → quiet
		}

		// Package refinement: when the import names a specific sub-package (subpath),
		// keep only exported candidates whose file directory IS that package. Go/JS
		// sub-package imports are precise this way. A bare package-root import
		// (subpath == "") accepts any exported symbol of that name in the repo.
		var pkgCands []exp
		if subpath == "" {
			pkgCands = cands
		} else {
			for _, cd := range cands {
				if dirSlash(cd.filePath) == subpath {
					pkgCands = append(pkgCands, cd)
				}
			}
			if len(pkgCands) == 0 {
				// The import names a package that does not export this symbol.
				// Correct-or-quiet: DROP, never fall back to a repo-wide name guess.
				continue
			}
		}

		// Deterministic candidate order (file, id).
		sort.Slice(pkgCands, func(i, j int) bool {
			if pkgCands[i].filePath != pkgCands[j].filePath {
				return pkgCands[i].filePath < pkgCands[j].filePath
			}
			return pkgCands[i].id < pkgCands[j].id
		})

		conf := crossRepoCertifiedConf
		evidence := "import_cross_repo"
		if len(pkgCands) > 1 {
			conf = crossRepoAmbiguousConf
			evidence = "import_cross_repo_ambiguous"
		}
		target := pkgCands[0].id
		if target == imp.SourceAnchorID {
			continue
		}
		key := [2]int64{imp.SourceAnchorID, target}
		if seen[key] {
			continue
		}
		seen[key] = true

		edges = append(edges, CrossRepoEdge{
			SourceID:       imp.SourceAnchorID,
			TargetID:       target,
			Method:         "import_cross_repo",
			Confidence:     conf,
			TrustTier:      tierFor(conf),
			CandidateCount: len(pkgCands),
			EvidenceType:   evidence,
			Metadata: fmt.Sprintf(
				"target_repo_id=%d;target_repo_root=%s;resolved_by=%s;coord=%s",
				targetRepo, sanitizeMetaVal(coordByRepo[targetRepo].Root), resolvedBy, sanitizeMetaVal(coordVal),
			),
			SourceFile:   imp.File,
			SourceLine:   imp.Line,
			TargetRepoID: targetRepo,
		})
	}
	return edges
}

// CrossRepoCall is a QUALIFIED call site (`pkg.Sym(...)` / `crate::Sym(...)`) in a
// specific repo, pre-resolved to its caller node. Go binds imported symbols at call
// sites (the import is package-level), so a cross-repo Go usage surfaces HERE, not in
// the import pass. CallerID is the calling function's node id (a real function→function
// cross-repo CALLS edge, stronger than a file-anchor import edge).
type CrossRepoCall struct {
	RepoID          int64
	CallerID        int64
	CalleeName      string // last component (e.g. Add)
	CalleeQualified string // full qualified (e.g. mathlib.Add / crate_b::mod::Add)
	File            string
	Line            int
}

// ResolveCrossRepoCalls produces coordinate-verified cross-repo CALLS edges from
// qualified call sites. It is IMPORT-GUIDED: the call qualifier (`pkg` in `pkg.Sym`)
// must be an alias bound by an import that resolves — via package coordinates — to
// ANOTHER repo, and `Sym` must be an EXPORTED symbol in that repo's corresponding
// package. Same correct-or-quiet discipline as the import pass: no coordinate-bound
// alias, or no exported symbol in the package, → NO edge (never a bare name guess).
func ResolveCrossRepoCalls(calls []CrossRepoCall, imports []CrossRepoImport, nodes []CrossRepoNode, coords []RepoCoord) []CrossRepoEdge {
	if len(calls) == 0 || len(imports) == 0 || len(nodes) == 0 || len(coords) == 0 {
		return nil
	}
	coordByRepo := make(map[int64]RepoCoord, len(coords))
	for _, c := range coords {
		coordByRepo[c.RepoID] = c
	}
	repoIDs := make([]int64, 0, len(coords))
	for _, c := range coords {
		repoIDs = append(repoIDs, c.RepoID)
	}
	sort.Slice(repoIDs, func(i, j int) bool { return repoIDs[i] < repoIDs[j] })
	exportedByRepoName := buildExportedIndex(nodes)

	// alias binding: (sourceRepo, alias) -> the coordinate-matched target package.
	type bind struct {
		targetRepo int64
		subpath    string
		resolvedBy string
		coordVal   string
	}
	aliasMap := make(map[int64]map[string]bind)
	// Deterministic import order so a duplicated alias binds to a stable target.
	imps := append([]CrossRepoImport(nil), imports...)
	sort.Slice(imps, func(i, j int) bool {
		a, b := imps[i], imps[j]
		if a.RepoID != b.RepoID {
			return a.RepoID < b.RepoID
		}
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		return a.ModulePath < b.ModulePath
	})
	for _, imp := range imps {
		for _, rid := range repoIDs {
			if rid == imp.RepoID {
				continue
			}
			if sp, by, cv, ok := matchModuleToCoord(imp.ModulePath, coordByRepo[rid]); ok {
				if aliasMap[imp.RepoID] == nil {
					aliasMap[imp.RepoID] = make(map[string]bind)
				}
				b := bind{targetRepo: rid, subpath: sp, resolvedBy: by, coordVal: cv}
				// The package is referenced by its identifier at call sites: the import's
				// bound name (Go package ident) and/or the module path's last segment.
				for _, alias := range aliasCandidates(imp.ImportedName, imp.ModulePath) {
					if _, exists := aliasMap[imp.RepoID][alias]; !exists {
						aliasMap[imp.RepoID][alias] = b
					}
				}
				break
			}
		}
	}
	if len(aliasMap) == 0 {
		return nil
	}

	cs := append([]CrossRepoCall(nil), calls...)
	sort.Slice(cs, func(i, j int) bool {
		a, b := cs[i], cs[j]
		if a.RepoID != b.RepoID {
			return a.RepoID < b.RepoID
		}
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Line != b.Line {
			return a.Line < b.Line
		}
		if a.CalleeQualified != b.CalleeQualified {
			return a.CalleeQualified < b.CalleeQualified
		}
		return a.CallerID < b.CallerID
	})

	var edges []CrossRepoEdge
	seen := make(map[[2]int64]bool)
	for _, call := range cs {
		if call.CallerID == 0 || call.CalleeName == "" {
			continue
		}
		am := aliasMap[call.RepoID]
		if am == nil {
			continue
		}
		qualifier := callQualifier(call.CalleeQualified)
		if qualifier == "" {
			continue
		}
		b, ok := am[qualifier]
		if !ok {
			continue // qualifier is not a coordinate-bound cross-repo alias → quiet
		}
		byName := exportedByRepoName[b.targetRepo]
		if byName == nil {
			continue
		}
		pkgCands := refinePackageCandidates(byName[call.CalleeName], b.subpath)
		if len(pkgCands) == 0 {
			continue
		}
		conf := crossRepoCertifiedConf
		evidence := "call_cross_repo"
		if len(pkgCands) > 1 {
			conf = crossRepoAmbiguousConf
			evidence = "call_cross_repo_ambiguous"
		}
		target := pkgCands[0].id
		if target == call.CallerID {
			continue
		}
		key := [2]int64{call.CallerID, target}
		if seen[key] {
			continue
		}
		seen[key] = true
		edges = append(edges, CrossRepoEdge{
			SourceID:       call.CallerID,
			TargetID:       target,
			Method:         "call_cross_repo",
			Confidence:     conf,
			TrustTier:      tierFor(conf),
			CandidateCount: len(pkgCands),
			EvidenceType:   evidence,
			Metadata: fmt.Sprintf(
				"target_repo_id=%d;target_repo_root=%s;resolved_by=%s;coord=%s",
				b.targetRepo, sanitizeMetaVal(coordByRepo[b.targetRepo].Root), b.resolvedBy, sanitizeMetaVal(b.coordVal),
			),
			SourceFile:   call.File,
			SourceLine:   call.Line,
			TargetRepoID: b.targetRepo,
		})
	}
	return edges
}

// crExp is an exported-symbol candidate (id + repo-relative file path).
type crExp struct {
	id       int64
	filePath string
}

// buildExportedIndex indexes exported symbols by (repo, name). Only exported defs are
// legal cross-repo targets (a non-exported def is unreachable across a boundary).
func buildExportedIndex(nodes []CrossRepoNode) map[int64]map[string][]crExp {
	out := make(map[int64]map[string][]crExp)
	for _, n := range nodes {
		if !n.IsExported || n.Name == "" {
			continue
		}
		byName, ok := out[n.RepoID]
		if !ok {
			byName = make(map[string][]crExp)
			out[n.RepoID] = byName
		}
		byName[n.Name] = append(byName[n.Name], crExp{id: n.ID, filePath: n.FilePath})
	}
	return out
}

// refinePackageCandidates keeps only candidates in the named sub-package (Go/JS/Rust
// package == directory), then content-sorts them. subpath=="" (bare package-root
// import) accepts all. Empty result ⇒ the package does not export the symbol ⇒ the
// caller drops (correct-or-quiet).
func refinePackageCandidates(cands []crExp, subpath string) []crExp {
	var out []crExp
	if subpath == "" {
		out = append(out, cands...)
	} else {
		for _, cd := range cands {
			if dirSlash(cd.filePath) == subpath {
				out = append(out, cd)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].filePath != out[j].filePath {
			return out[i].filePath < out[j].filePath
		}
		return out[i].id < out[j].id
	})
	return out
}

// callQualifier returns the receiver/namespace part of a qualified callee — the text
// before the FIRST `.` or `::` (e.g. mathlib.Add → mathlib; crate_b::m::Add → crate_b).
// Returns "" when the callee is unqualified (a bare local call → never cross-repo here).
func callQualifier(qualified string) string {
	if qualified == "" {
		return ""
	}
	if i := strings.Index(qualified, "::"); i >= 0 {
		return qualified[:i]
	}
	if i := strings.Index(qualified, "."); i >= 0 {
		return qualified[:i]
	}
	return ""
}

// aliasCandidates returns the identifiers a package is referenced by at call sites:
// the import's bound name (when not a wildcard) and the module path's last segment.
func aliasCandidates(importedName, modulePath string) []string {
	var out []string
	seen := map[string]bool{}
	add := func(s string) {
		if s != "" && s != "*" && !seen[s] {
			seen[s] = true
			out = append(out, s)
		}
	}
	add(importedName)
	// last path segment across `/` or `::`
	seg := modulePath
	for _, sep := range []string{"/", "::"} {
		if i := strings.LastIndex(seg, sep); i >= 0 {
			seg = seg[i+len(sep):]
		}
	}
	add(seg)
	return out
}

// matchModuleToCoord reports whether an import module path is one of repo `c`'s
// package coordinates, returning the in-repo sub-package path, the coordinate KIND,
// and the matched coordinate value. Correct-or-quiet: returns ok=false when the
// path is not provably one of this repo's coordinates.
func matchModuleToCoord(modulePath string, c RepoCoord) (subpath, kind, coordVal string, ok bool) {
	if modulePath == "" {
		return "", "", "", false
	}
	// Go: `github.com/org/repob` or `github.com/org/repob/pkg/sub`.
	if c.GoModule != "" {
		if modulePath == c.GoModule {
			return "", "go_module", c.GoModule, true
		}
		if strings.HasPrefix(modulePath, c.GoModule+"/") {
			return strings.TrimPrefix(modulePath, c.GoModule+"/"), "go_module", c.GoModule, true
		}
	}
	// JS/TS: `@org/pkg` or `@org/pkg/sub`.
	for _, p := range c.JSPackages {
		if p == "" {
			continue
		}
		if modulePath == p {
			return "", "js_package", p, true
		}
		if strings.HasPrefix(modulePath, p+"/") {
			return strings.TrimPrefix(modulePath, p+"/"), "js_package", p, true
		}
	}
	// Rust: `crate_b` or `crate_b::module::Item` — the first `::` segment is the crate.
	for _, cr := range c.RustCrates {
		if cr == "" {
			continue
		}
		crate := modulePath
		rest := ""
		if i := strings.Index(modulePath, "::"); i >= 0 {
			crate = modulePath[:i]
			rest = modulePath[i+2:]
		}
		if crate == cr {
			// Map `a::b::Item` → sub-package `a/b` (drop the trailing item segment when
			// it is the imported name; keep module path as dir). Best-effort: join all
			// but the last `::` segment as the package dir.
			segs := splitNonEmpty(rest, "::")
			if len(segs) > 1 {
				return strings.Join(segs[:len(segs)-1], "/"), "rust_crate", cr, true
			}
			return "", "rust_crate", cr, true
		}
	}
	return "", "", "", false
}

// dirSlash returns the slash-form directory of a repo-relative path, "" for a
// repo-root file (filepath.Dir returns "." for a bare basename).
func dirSlash(p string) string {
	d := filepath.ToSlash(filepath.Dir(p))
	if d == "." {
		return ""
	}
	return d
}

func splitNonEmpty(s, sep string) []string {
	var out []string
	for _, p := range strings.Split(s, sep) {
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// sanitizeMetaVal strips the `;`/`=` separators from a value so it can be embedded
// in the semicolon/key=value edge-metadata string without corrupting the framing
// (ParseEdgeMetadata splits on those). Paths/coords rarely contain them.
func sanitizeMetaVal(v string) string {
	v = strings.ReplaceAll(v, ";", ",")
	v = strings.ReplaceAll(v, "=", "-")
	return v
}

// ExtractRepoCoord reads a repository root's package coordinates (go.mod module,
// package.json names, Cargo.toml crate names). Pure filesystem read; every field is
// best-effort — a repo with no manifest for a language simply contributes no
// coordinate for it (so it can never be a cross-repo target in that language).
func ExtractRepoCoord(repoID int64, root string) RepoCoord {
	return RepoCoord{
		RepoID:     repoID,
		Root:       root,
		GoModule:   FindGoModulePath(root),
		JSPackages: collectJSPackageNames(root),
		RustCrates: collectRustCrateNames(root),
	}
}

// collectJSPackageNames returns every package.json `name` under root (skipping
// node_modules/dist/build/.git), the identifier other repos import the package by.
func collectJSPackageNames(root string) []string {
	if root == "" {
		return nil
	}
	seen := make(map[string]bool)
	var names []string
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "node_modules", "dist", "build":
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() != "package.json" {
			return nil
		}
		data, rerr := os.ReadFile(path)
		if rerr != nil {
			return nil
		}
		var pkg struct {
			Name string `json:"name"`
		}
		if json.Unmarshal(data, &pkg) == nil && pkg.Name != "" && !seen[pkg.Name] {
			seen[pkg.Name] = true
			names = append(names, pkg.Name)
		}
		return nil
	})
	sort.Strings(names)
	return names
}

// collectRustCrateNames returns crate names declared in Cargo.toml files under root:
// the `[package] name = "..."` line. Best-effort line parser (no toml dependency);
// underscores/hyphens are normalised the way Rust import paths spell crate names
// (hyphens in a crate name become underscores in `use` paths).
func collectRustCrateNames(root string) []string {
	if root == "" {
		return nil
	}
	seen := make(map[string]bool)
	var names []string
	add := func(n string) {
		n = strings.TrimSpace(n)
		if n == "" {
			return
		}
		norm := strings.ReplaceAll(n, "-", "_")
		if !seen[norm] {
			seen[norm] = true
			names = append(names, norm)
		}
	}
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "target", "node_modules":
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() != "Cargo.toml" {
			return nil
		}
		f, oerr := os.Open(path)
		if oerr != nil {
			return nil
		}
		defer f.Close()
		inPackage := false
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			line := strings.TrimSpace(sc.Text())
			if strings.HasPrefix(line, "[") {
				inPackage = line == "[package]"
				continue
			}
			if inPackage && strings.HasPrefix(line, "name") {
				if i := strings.Index(line, "="); i >= 0 {
					val := strings.TrimSpace(line[i+1:])
					val = strings.Trim(val, `"'`)
					add(val)
				}
			}
		}
		return nil
	})
	sort.Strings(names)
	return names
}

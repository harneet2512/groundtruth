// Package resolver resolves call references to definition nodes.
package resolver

import (
	"bufio"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// TSConfig represents the relevant fields from tsconfig.json.
type TSConfig struct {
	BaseURL string
	Paths   map[string][]string
}

// ParseTSConfig reads tsconfig.json and extracts baseUrl and paths.
func ParseTSConfig(root string) *TSConfig {
	data, err := os.ReadFile(filepath.Join(root, "tsconfig.json"))
	if err != nil {
		return nil
	}
	var raw struct {
		CompilerOptions struct {
			BaseURL string              `json:"baseUrl"`
			Paths   map[string][]string `json:"paths"`
		} `json:"compilerOptions"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil
	}
	if raw.CompilerOptions.BaseURL == "" && len(raw.CompilerOptions.Paths) == 0 {
		return nil
	}
	return &TSConfig{
		BaseURL: raw.CompilerOptions.BaseURL,
		Paths:   raw.CompilerOptions.Paths,
	}
}

// ExpandTSConfigPath resolves a tsconfig path alias (e.g., "@/auth/login" → "src/auth/login").
func ExpandTSConfigPath(modulePath string, cfg *TSConfig) string {
	if cfg == nil || len(cfg.Paths) == 0 {
		return ""
	}
	for pattern, replacements := range cfg.Paths {
		if len(replacements) == 0 {
			continue
		}
		if strings.HasSuffix(pattern, "/*") {
			prefix := strings.TrimSuffix(pattern, "/*")
			if strings.HasPrefix(modulePath, prefix+"/") {
				rest := strings.TrimPrefix(modulePath, prefix+"/")
				replBase := strings.TrimSuffix(replacements[0], "/*")
				return replBase + "/" + rest
			}
		} else if pattern == modulePath {
			return replacements[0]
		}
	}
	return ""
}

// RegisterTSConfigPaths adds tsconfig path alias entries to the file map.
func RegisterTSConfigPaths(fm map[string][]string, cfg *TSConfig) {
	if cfg == nil || len(cfg.Paths) == 0 {
		return
	}
	for pattern, replacements := range cfg.Paths {
		if len(replacements) == 0 || !strings.HasSuffix(pattern, "/*") {
			continue
		}
		prefix := strings.TrimSuffix(pattern, "/*")
		replBase := strings.TrimSuffix(replacements[0], "/*")
		for key, files := range fm {
			if strings.HasPrefix(key, replBase+"/") {
				aliasKey := prefix + "/" + strings.TrimPrefix(key, replBase+"/")
				fm[aliasKey] = append(fm[aliasKey], files...)
			}
		}
	}
}

// FindGoModulePath parses go.mod in the given root directory and returns
// the module path (e.g., "example.com/project"). Returns "" if not found.
func FindGoModulePath(root string) string {
	f, err := os.Open(filepath.Join(root, "go.mod"))
	if err != nil {
		return ""
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "module ") {
			return strings.TrimSpace(strings.TrimPrefix(line, "module "))
		}
	}
	return ""
}

// RegisterGoModulePaths adds module-prefixed entries to the file map for Go files.
// This allows resolveModulePath to find local files when imports use the full
// module path (e.g., "example.com/project/auth" → files in "auth/").
func RegisterGoModulePaths(fm map[string][]string, goModulePath string) {
	if goModulePath == "" {
		return
	}
	// Collect existing Go directory keys and their files, then register module-prefixed versions.
	// Go directories are already registered as "pkg/auth", "auth", etc.
	// We need to also register "example.com/project/auth", "example.com/project/pkg/auth".
	additions := make(map[string][]string)
	for key, files := range fm {
		if strings.Contains(key, "::") || strings.Contains(key, `\`) {
			continue
		}
		if ext := filepath.Ext(key); ext != "" {
			continue
		}
		if strings.HasPrefix(key, goModulePath) {
			continue
		}
		if strings.Contains(key, ".") && !strings.Contains(key, "/") {
			continue
		}
		moduleKey := goModulePath + "/" + key
		additions[moduleKey] = files
	}
	for k, v := range additions {
		fm[k] = append(fm[k], v...)
	}
	// Versioned modules: github.com/org/repo/v2 → also register without version
	if parts := strings.Split(goModulePath, "/"); len(parts) > 0 {
		last := parts[len(parts)-1]
		if len(last) >= 2 && last[0] == 'v' && last[1] >= '0' && last[1] <= '9' {
			unversioned := strings.Join(parts[:len(parts)-1], "/")
			for key, files := range fm {
				if strings.Contains(key, ".") || strings.Contains(key, "::") || filepath.Ext(key) != "" {
					continue
				}
				additions[unversioned+"/"+key] = files
			}
			for k, v := range additions {
				fm[k] = append(fm[k], v...)
			}
		}
	}
}

// RegisterGoPackageNames scans Go files for `package X` declarations and
// registers the package name as an alias for the directory in the file map. (P4)
// In Go, the package name often differs from the directory name (e.g.,
// directory "pkg/reconciler/managed" has `package managed`, but imports use
// the full path including "managed").
func RegisterGoPackageNames(fm map[string][]string, files []string, languages []string) {
	dirPackages := make(map[string]string) // dir → package name
	for i, fp := range files {
		if i >= len(languages) || languages[i] != "go" {
			continue
		}
		dir := filepath.ToSlash(filepath.Dir(fp))
		if _, seen := dirPackages[dir]; seen {
			continue
		}
		f, err := os.Open(fp)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(f)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			if strings.HasPrefix(line, "package ") {
				pkgName := strings.TrimSpace(strings.TrimPrefix(line, "package "))
				if pkgName != "" && pkgName != "main" {
					dirPackages[dir] = pkgName
				}
				break
			}
			if line != "" && !strings.HasPrefix(line, "//") && !strings.HasPrefix(line, "/*") {
				break // past the package declaration
			}
		}
		f.Close()
	}
	// Register package-name aliases: if dir "internal/reconciler/managed" has package "managed",
	// register "managed" → files in that dir (if not already registered with higher priority)
	for dir, pkg := range dirPackages {
		dirFiles, ok := fm[dir]
		if !ok {
			continue
		}
		// Only register if the package name differs from the directory basename
		if filepath.Base(dir) != pkg {
			fm[pkg] = append(fm[pkg], dirFiles...)
		}
	}
}

// ResolvedCall is a call reference that has been resolved to a target node.
type ResolvedCall struct {
	SourceNodeID   int64
	TargetNodeID   int64
	SourceLine     int
	SourceFile     string
	Method         string  // "same_file", "import", "name_match"
	Confidence     float64 // 0.0–1.0
	CandidateCount int     // number of resolution candidates (1=unambiguous)
	TrustTier      string  // CERTIFIED, CANDIDATE, SPECULATIVE
	EvidenceType   string  // ast_call, ast_import, name_match
}

// edgeKey is used for deduplication.
type edgeKey struct {
	sourceID int64
	targetID int64
	typ      string
}

// NodeMeta carries class/interface membership data for method dispatch (P3/P4/P5).
type NodeMeta struct {
	Label    string // Function, Class, Method, Interface, Struct
	File     string
	ParentID int64
	Name     string
}

// BuildNodeMeta creates a metadata index from all parsed nodes.
func BuildNodeMeta(nodes []store.Node, nodeDBIDs []int64) map[int64]NodeMeta {
	meta := make(map[int64]NodeMeta, len(nodes))
	for i, n := range nodes {
		meta[nodeDBIDs[i]] = NodeMeta{
			Label:    n.Label,
			File:     n.FilePath,
			ParentID: n.ParentID,
			Name:     n.Name,
		}
	}
	return meta
}

// computeConfidence returns a confidence score based on resolution method and ambiguity.
func computeConfidence(method string, candidateCount int) float64 {
	switch method {
	case "same_file":
		return 1.0
	case "import":
		return 1.0
	case "name_match":
		if candidateCount <= 1 {
			return 0.9 // unique name — almost certainly correct
		} else if candidateCount == 2 {
			return 0.6
		} else if candidateCount <= 5 {
			return 0.4
		}
		return 0.2 // highly ambiguous
	}
	return 0.3
}

// Resolve takes all call refs and all defined nodes, and resolves calls to definitions.
// Resolution strategies (in priority order):
//  1. Same-file exact name match → "same_file" (conf=1.0)
//  2. Import-verified cross-file → "import" (conf=1.0)
//  3. Qualified call via import (Go pkg.Func, TS/Py Class.method) → "import" (conf=1.0)
//  4. Class-method dispatch via imported class → "import" (conf=0.95)
//  5. Interface/trait structural match → "import" (conf=0.85)
//  6. Cross-file name match → "name_match" (fallback, unreliable)
func Resolve(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64, // name → list of node IDs
	fileNodeIDs map[string]map[string][]int64, // file → name → list of node IDs
	callerNodeIDs []int64, // parallel to allCalls
	allImports []parser.ImportRef, // all parsed import statements
	fileMap map[string][]string, // module path → list of file paths
	nodeMeta map[int64]NodeMeta, // nodeID → metadata (nil = skip P3-P5)
) []ResolvedCall {
	// Build import index: file → imported name → list of candidate target files
	importIndex := buildImportIndex(allImports, fileMap)

	// P3/P4: Build class-member and interface-impl indexes if metadata available
	var classMethods map[int64][]int64         // parentID → []methodNodeIDs
	var classNameToIDs map[string][]int64       // className → []classNodeIDs
	var methodsByClass map[int64]map[string]int64 // classID → methodName → methodNodeID
	if nodeMeta != nil {
		classMethods = make(map[int64][]int64)
		classNameToIDs = make(map[string][]int64)
		methodsByClass = make(map[int64]map[string]int64)
		for id, m := range nodeMeta {
			if m.Label == "Class" || m.Label == "Interface" || m.Label == "Struct" {
				classNameToIDs[m.Name] = append(classNameToIDs[m.Name], id)
			}
			if m.ParentID != 0 && (m.Label == "Method" || m.Label == "Function") {
				classMethods[m.ParentID] = append(classMethods[m.ParentID], id)
				if methodsByClass[m.ParentID] == nil {
					methodsByClass[m.ParentID] = make(map[string]int64)
				}
				methodsByClass[m.ParentID][m.Name] = id
			}
		}
	}

	var resolved []ResolvedCall
	seen := make(map[edgeKey]bool)

	for i, call := range allCalls {
		callerID := callerNodeIDs[i]
		if callerID == 0 {
			continue
		}

		calleeName := call.CalleeName

		// -----------------------------------------------------------
		// Strategy 1: Same-file exact name match (conf=1.0)
		// -----------------------------------------------------------
		if fileNodes, ok := fileNodeIDs[call.File]; ok {
			if targetIDs, ok := fileNodes[calleeName]; ok && len(targetIDs) == 1 && targetIDs[0] != callerID {
				targetID := targetIDs[0]
				key := edgeKey{callerID, targetID, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   targetID,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         "same_file",
						Confidence:     1.0,
						CandidateCount: 1,
						TrustTier:      "CERTIFIED",
						EvidenceType:   "ast_call",
					})
				}
				continue
			}
		}

		// -----------------------------------------------------------
		// Strategy 2: Import-verified cross-file (conf=1.0)
		// -----------------------------------------------------------
		fileImports, hasImports := importIndex[call.File]
		if hasImports {
			var importCandidates []int64

			if candidateFiles, ok := fileImports[calleeName]; ok {
				for _, targetFile := range candidateFiles {
					if fileNodes, ok := fileNodeIDs[targetFile]; ok {
						if targetIDs, ok := fileNodes[calleeName]; ok {
							for _, tid := range targetIDs {
								if tid != callerID {
									importCandidates = append(importCandidates, tid)
								}
							}
						}
					}
				}
			}

			// Check wildcard imports
			if len(importCandidates) == 0 {
				if candidateFiles, ok := fileImports["*"]; ok {
					for _, targetFile := range candidateFiles {
						if fileNodes, ok := fileNodeIDs[targetFile]; ok {
							if targetIDs, ok := fileNodes[calleeName]; ok {
								for _, tid := range targetIDs {
									if tid != callerID {
										importCandidates = append(importCandidates, tid)
									}
								}
							}
						}
					}
				}
			}

			if len(importCandidates) > 0 {
				bestTarget := importCandidates[0]
				key := edgeKey{callerID, bestTarget, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   bestTarget,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         "import",
						Confidence:     1.0,
						CandidateCount: len(importCandidates),
						TrustTier:      "CERTIFIED",
						EvidenceType:   "ast_import",
					})
				}
				continue
			}
		}

		// -----------------------------------------------------------
		// Strategy 3 (P1): Qualified call via import (conf=1.0)
		// Handles Go pkg.Func(), TS/Python Class.staticMethod()
		// Runs as primary strategy, not fallback.
		// -----------------------------------------------------------
		if hasImports && call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				memberName := call.CalleeQualified[dotIdx+1:]
				if candidateFiles, ok := fileImports[qualifier]; ok {
					var qualCandidates []int64
					for _, targetFile := range candidateFiles {
						if fileNodes, ok := fileNodeIDs[targetFile]; ok {
							if targetIDs, ok := fileNodes[memberName]; ok {
								for _, tid := range targetIDs {
									if tid != callerID {
										qualCandidates = append(qualCandidates, tid)
									}
								}
							}
						}
					}
					if len(qualCandidates) > 0 {
						bestTarget := qualCandidates[0]
						key := edgeKey{callerID, bestTarget, "CALLS"}
						if !seen[key] {
							seen[key] = true
							resolved = append(resolved, ResolvedCall{
								SourceNodeID:   callerID,
								TargetNodeID:   bestTarget,
								SourceLine:     call.Line,
								SourceFile:     call.File,
								Method:         "import",
								Confidence:     1.0,
								CandidateCount: len(qualCandidates),
								TrustTier:      "CERTIFIED",
								EvidenceType:   "ast_import",
							})
						}
						continue
					}
				}
			}
		}

		// -----------------------------------------------------------
		// Strategy 4 (P3): Class-method dispatch via imported class (conf=0.95)
		// When file imports class C and calls method M, resolve to C.M
		// even when the call is obj.M() (not C.M()).
		// -----------------------------------------------------------
		if hasImports && nodeMeta != nil && classNameToIDs != nil {
			found := false
			// For qualified calls like "obj.method", try each imported class
			// that has a method with this name
			for importedName, candidateFiles := range fileImports {
				if importedName == "*" {
					continue
				}
				// Is this imported name a class/interface/struct?
				classIDs, isClass := classNameToIDs[importedName]
				if !isClass {
					continue
				}
				// Check if any of these classes has a method named calleeName
				for _, classID := range classIDs {
					if methods, ok := methodsByClass[classID]; ok {
						if methodID, ok := methods[calleeName]; ok {
							// Verify the class is in one of the candidate files
							classMeta := nodeMeta[classID]
							for _, cf := range candidateFiles {
								if classMeta.File == cf {
									key := edgeKey{callerID, methodID, "CALLS"}
									if !seen[key] {
										seen[key] = true
										resolved = append(resolved, ResolvedCall{
											SourceNodeID:   callerID,
											TargetNodeID:   methodID,
											SourceLine:     call.Line,
											SourceFile:     call.File,
											Method:         "import",
											Confidence:     0.95,
											CandidateCount: 1,
											TrustTier:      "CERTIFIED",
											EvidenceType:   "ast_import",
										})
									}
									found = true
									break
								}
							}
							if found {
								break
							}
						}
					}
				}
				if found {
					break
				}
			}
			if found {
				continue
			}
		}

		// -----------------------------------------------------------
		// Strategy 5 (P4): Interface/trait structural match (conf=0.85)
		// When calleeName matches a method on exactly one class/struct
		// in the same package as an imported interface, resolve it.
		// -----------------------------------------------------------
		if hasImports && nodeMeta != nil && methodsByClass != nil {
			// Collect all classes whose methods include calleeName
			var structCandidates []int64
			for classID, methods := range methodsByClass {
				if methodID, ok := methods[calleeName]; ok {
					cm := nodeMeta[classID]
					if cm.Label == "Struct" || cm.Label == "Class" {
						structCandidates = append(structCandidates, methodID)
					}
				}
			}
			if len(structCandidates) == 1 {
				methodID := structCandidates[0]
				key := edgeKey{callerID, methodID, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   methodID,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         "import",
						Confidence:     0.85,
						CandidateCount: 1,
						TrustTier:      "CERTIFIED",
						EvidenceType:   "ast_import",
					})
				}
				continue
			}
		}

		// -----------------------------------------------------------
		// Strategy 6: Cross-file name match (fallback)
		// -----------------------------------------------------------
		if targets, ok := nodeIDs[calleeName]; ok {
			candidateCount := 0
			var bestTarget int64

			for _, targetID := range targets {
				if targetID == callerID {
					continue
				}
				candidateCount++
				if bestTarget == 0 {
					bestTarget = targetID
				}
			}

			if bestTarget != 0 {
				conf := computeConfidence("name_match", candidateCount)
				tier := "SPECULATIVE"
				if candidateCount <= 1 {
					tier = "CERTIFIED"
				} else if candidateCount == 2 {
					tier = "CANDIDATE"
				}
				key := edgeKey{callerID, bestTarget, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   bestTarget,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         "name_match",
						Confidence:     conf,
						CandidateCount: candidateCount,
						TrustTier:      tier,
						EvidenceType:   "name_match",
					})
				}
			}
		}
	}

	return resolved
}

// buildImportIndex creates: callerFile → importedName → []targetFiles
// This tells us: "file X imports name Y, which could come from files [A, B, ...]"
func buildImportIndex(imports []parser.ImportRef, fileMap map[string][]string) map[string]map[string][]string {
	index := make(map[string]map[string][]string)

	// Cache: (modulePath, callerFile) → resolved files
	type cacheKey struct{ mod, caller string }
	moduleCache := make(map[cacheKey][]string)

	// P2: Build re-export index for barrel file following.
	// When file B has `export { X } from './C'`, B re-exports X from C.
	// We detect this by finding imports whose file is also an export source.
	reexportIndex := buildReexportIndex(imports, fileMap)

	for _, imp := range imports {
		if imp.ImportedName == "" {
			continue
		}

		fileEntry, ok := index[imp.File]
		if !ok {
			fileEntry = make(map[string][]string)
			index[imp.File] = fileEntry
		}

		// P0: Resolve with caller-relative path resolution
		ck := cacheKey{imp.ModulePath, imp.File}
		targetFiles, cached := moduleCache[ck]
		if !cached {
			targetFiles = resolveModulePathRelative(imp.ModulePath, imp.File, fileMap)
			moduleCache[ck] = targetFiles
		}

		// If module path didn't resolve, try combined forms
		if len(targetFiles) == 0 && imp.ImportedName != "*" && imp.ModulePath != "" {
			combined := imp.ModulePath + "." + imp.ImportedName
			ck2 := cacheKey{combined, imp.File}
			if cached, ok := moduleCache[ck2]; ok {
				targetFiles = cached
			} else {
				targetFiles = resolveModulePathRelative(combined, imp.File, fileMap)
				moduleCache[ck2] = targetFiles
			}
			if len(targetFiles) == 0 {
				combinedSlash := strings.ReplaceAll(imp.ModulePath, ".", "/") + "/" + imp.ImportedName
				ck3 := cacheKey{combinedSlash, imp.File}
				if cached, ok := moduleCache[ck3]; ok {
					targetFiles = cached
				} else {
					targetFiles = resolveModulePathRelative(combinedSlash, imp.File, fileMap)
					moduleCache[ck3] = targetFiles
				}
			}
		}

		// P2: Follow re-export chains (barrel files).
		// If target files re-export this name, follow to the original source.
		if len(targetFiles) > 0 {
			expanded := followReexports(targetFiles, imp.ImportedName, reexportIndex, fileMap, 3)
			if len(expanded) > 0 {
				targetFiles = append(targetFiles, expanded...)
			}
		}

		if len(targetFiles) > 0 {
			fileEntry[imp.ImportedName] = append(fileEntry[imp.ImportedName], targetFiles...)
		}
	}

	return index
}

// resolveModulePathRelative resolves a module path relative to the importing file. (P0)
// For relative paths (./foo, ../bar), resolves against the caller's directory
// and probes common extensions (.ts, .tsx, .js, /index.ts, etc.).
func resolveModulePathRelative(modulePath string, callerFile string, fileMap map[string][]string) []string {
	if modulePath == "" {
		return nil
	}

	// P0: Relative path resolution — resolve against caller's directory
	if strings.HasPrefix(modulePath, "./") || strings.HasPrefix(modulePath, "../") {
		callerDir := filepath.ToSlash(filepath.Dir(callerFile))
		resolved := filepath.ToSlash(filepath.Clean(filepath.Join(callerDir, modulePath)))

		// Probe: exact, then with extensions, then index files
		probes := []string{
			resolved,
			resolved + ".ts", resolved + ".tsx",
			resolved + ".js", resolved + ".jsx",
			resolved + ".py",
			resolved + ".rs",
			resolved + "/index.ts", resolved + "/index.tsx",
			resolved + "/index.js", resolved + "/index.jsx",
			resolved + "/mod.rs",
		}
		for _, probe := range probes {
			if files, ok := fileMap[probe]; ok {
				return files
			}
		}

		// Also try without leading directory components that BuildFileMap may have stripped
		parts := strings.Split(resolved, "/")
		for j := 1; j < len(parts); j++ {
			suffix := strings.Join(parts[j:], "/")
			if files, ok := fileMap[suffix]; ok {
				return files
			}
			for _, ext := range []string{".ts", ".tsx", ".js", ".jsx", ".py", ".rs"} {
				if files, ok := fileMap[suffix+ext]; ok {
					return files
				}
			}
			for _, idx := range []string{"/index.ts", "/index.js", "/index.tsx"} {
				if files, ok := fileMap[suffix+idx]; ok {
					return files
				}
			}
		}
	}

	// Rust use-path resolution: crate::foo::bar → foo/bar or src/foo/bar
	if strings.Contains(modulePath, "::") {
		slashPath := strings.ReplaceAll(modulePath, "::", "/")
		slashPath = strings.TrimPrefix(slashPath, "crate/")
		probes := []string{
			slashPath,
			"src/" + slashPath,
			slashPath + ".rs",
			"src/" + slashPath + ".rs",
			slashPath + "/mod.rs",
			"src/" + slashPath + "/mod.rs",
		}
		for _, probe := range probes {
			if files, ok := fileMap[probe]; ok {
				return files
			}
		}
	}

	// Fall back to global resolution
	return resolveModulePath(modulePath, fileMap)
}

// resolveModulePath maps a module path string to actual source file paths (global lookup).
func resolveModulePath(modulePath string, fileMap map[string][]string) []string {
	if modulePath == "" {
		return nil
	}

	if files, ok := fileMap[modulePath]; ok {
		return files
	}

	normalized := strings.ReplaceAll(modulePath, ".", "/")
	if files, ok := fileMap[normalized]; ok {
		return files
	}

	// JS/TS relative imports: strip leading ./ or ../
	cleaned := strings.TrimPrefix(modulePath, "./")
	cleaned = strings.TrimPrefix(cleaned, "../")
	if cleaned != modulePath {
		if files, ok := fileMap[cleaned]; ok {
			return files
		}
		for _, ext := range []string{".ts", ".tsx", ".js", ".jsx", ".py", ".rs"} {
			if files, ok := fileMap[cleaned+ext]; ok {
				return files
			}
		}
		for _, idx := range []string{"/index.ts", "/index.js", "/index.tsx"} {
			if files, ok := fileMap[cleaned+idx]; ok {
				return files
			}
		}
	}

	// Go module paths: github.com/org/repo/v2/pkg/auth → try suffix stripping.
	// BuildFileMap registers "pkg/auth", "auth" — we try each suffix until one hits.
	if strings.Contains(modulePath, "/") && strings.Contains(modulePath, ".") {
		parts := strings.Split(modulePath, "/")
		for j := len(parts) - 1; j >= 1; j-- {
			suffix := strings.Join(parts[j:], "/")
			if files, ok := fileMap[suffix]; ok {
				return files
			}
		}
	}

	// Rust module paths: crate::foo::bar → strip crate::, try ::form then /form
	if strings.Contains(modulePath, "::") {
		stripped := strings.TrimPrefix(modulePath, "crate::")
		if files, ok := fileMap[stripped]; ok {
			return files
		}
		slashForm := strings.ReplaceAll(stripped, "::", "/")
		if files, ok := fileMap[slashForm]; ok {
			return files
		}
		if files, ok := fileMap["src/"+slashForm]; ok {
			return files
		}
		colonParts := strings.Split(stripped, "::")
		for j := len(colonParts) - 1; j >= 1; j-- {
			suffix := strings.Join(colonParts[j:], "::")
			if files, ok := fileMap[suffix]; ok {
				return files
			}
		}
	}

	return nil
}

// reexportEntry tracks a re-export: file B re-exports name X from module M.
type reexportEntry struct {
	file       string // the file doing the re-export
	name       string // the symbol being re-exported
	fromModule string // the source module path
}

// buildReexportIndex detects re-export patterns (P2).
// A re-export is an import whose file also appears as a target in other imports
// (barrel file pattern: index.ts imports from ./foo and re-exports).
func buildReexportIndex(imports []parser.ImportRef, fileMap map[string][]string) map[string][]reexportEntry {
	// file → list of re-exports from that file
	index := make(map[string][]reexportEntry)
	for _, imp := range imports {
		if imp.ImportedName == "" || imp.ImportedName == "*" {
			continue
		}
		index[imp.File] = append(index[imp.File], reexportEntry{
			file:       imp.File,
			name:       imp.ImportedName,
			fromModule: imp.ModulePath,
		})
	}
	return index
}

// followReexports follows re-export chains up to maxDepth hops (P2).
// When targetFiles contain a barrel file that re-exports importedName from another module,
// this returns the transitive target files.
func followReexports(targetFiles []string, importedName string, reexportIdx map[string][]reexportEntry, fileMap map[string][]string, maxDepth int) []string {
	if maxDepth <= 0 {
		return nil
	}
	var extra []string
	seen := make(map[string]bool)
	for _, tf := range targetFiles {
		seen[tf] = true
	}

	for _, tf := range targetFiles {
		entries, ok := reexportIdx[tf]
		if !ok {
			continue
		}
		for _, entry := range entries {
			if entry.name != importedName {
				continue
			}
			// This file re-exports importedName from entry.fromModule — follow it
			resolved := resolveModulePathRelative(entry.fromModule, tf, fileMap)
			for _, rf := range resolved {
				if !seen[rf] {
					seen[rf] = true
					extra = append(extra, rf)
				}
			}
		}
	}

	// Recurse for deeper chains
	if len(extra) > 0 && maxDepth > 1 {
		deeper := followReexports(extra, importedName, reexportIdx, fileMap, maxDepth-1)
		extra = append(extra, deeper...)
	}
	return extra
}

// BuildNameIndex creates a map from symbol name to list of node IDs.
// fileIndex maps file → name → []nodeIDs to handle duplicate names
// (e.g., Java method overloading, Python nested classes with same-named methods).
func BuildNameIndex(db *store.DB, nodes []store.Node, nodeDBIDs []int64) (map[string][]int64, map[string]map[string][]int64) {
	nameIndex := make(map[string][]int64)
	fileIndex := make(map[string]map[string][]int64)

	for i, n := range nodes {
		dbID := nodeDBIDs[i]
		nameIndex[n.Name] = append(nameIndex[n.Name], dbID)

		if _, ok := fileIndex[n.FilePath]; !ok {
			fileIndex[n.FilePath] = make(map[string][]int64)
		}
		fileIndex[n.FilePath][n.Name] = append(fileIndex[n.FilePath][n.Name], dbID)
	}

	return nameIndex, fileIndex
}

// BuildFileMap creates a mapping from various module path representations to file paths.
// This allows resolveModulePath to find files for import strings like "os.path", "./utils", "fmt".
func BuildFileMap(files []string, languages []string) map[string][]string {
	fm := make(map[string][]string)

	register := func(key, filePath string) {
		if key != "" {
			fm[key] = append(fm[key], filePath)
		}
	}

	for i, filePath := range files {
		lang := ""
		if i < len(languages) {
			lang = languages[i]
		}

		// Raw file path (always register)
		register(filePath, filePath)

		dir := filepath.Dir(filePath)
		base := filepath.Base(filePath)
		ext := filepath.Ext(base)
		stem := strings.TrimSuffix(base, ext)

		switch lang {
		case "python":
			// Python: foo/bar/baz.py → "foo.bar.baz", "bar.baz", "baz"
			noExt := strings.TrimSuffix(filePath, ext)
			if stem == "__init__" {
				// Package init: foo/bar/__init__.py → "foo.bar", "bar"
				noExt = dir
			}
			dotted := strings.ReplaceAll(filepath.ToSlash(noExt), "/", ".")
			register(dotted, filePath)
			// Register progressively shorter suffixes
			parts := strings.Split(dotted, ".")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], ".")
				register(suffix, filePath)
			}
			// Also register the slash form
			register(filepath.ToSlash(noExt), filePath)

		case "javascript", "typescript":
			// JS/TS: src/utils/helpers.js → "src/utils/helpers", "utils/helpers", "helpers"
			// Also: index.js → register parent dir
			slashPath := filepath.ToSlash(filePath)
			noExt2 := strings.TrimSuffix(slashPath, ext)
			register(noExt2, filePath)
			// Register without src/ prefix
			for _, prefix := range []string{"src/", "lib/", "app/"} {
				if strings.HasPrefix(noExt2, prefix) {
					register(strings.TrimPrefix(noExt2, prefix), filePath)
				}
			}
			// Register just the stem
			register(stem, filePath)
			// For index.js/index.ts, register the parent directory
			if stem == "index" {
				slashDir := filepath.ToSlash(dir)
				register(slashDir, filePath)
				// Register directory suffix variants for barrel imports
				parts := strings.Split(slashDir, "/")
				for j := 1; j < len(parts); j++ {
					suffix := strings.Join(parts[j:], "/")
					register(suffix, filePath)
				}
			}
			// Register relative forms
			register("./"+noExt2, filePath)

		case "go":
			// Go: pkg/foo/bar.go → register the directory as the package path
			slashDir := filepath.ToSlash(dir)
			register(slashDir, filePath)
			// Also register shorter suffixes of the directory
			parts := strings.Split(slashDir, "/")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], "/")
				register(suffix, filePath)
			}

		case "java", "kotlin", "groovy", "scala":
			// JVM languages: [module/]src/main/java/com/foo/Bar.java → "com.foo.Bar", "com.foo"
			// Multi-module projects have a module prefix: extras/src/main/java/...
			slashPath := filepath.ToSlash(filePath)
			// Strip everything up to and including the JVM source root marker
			for _, root := range []string{
				"src/main/java/", "src/test/java/",
				"src/main/kotlin/", "src/test/kotlin/",
				"src/main/scala/", "src/test/scala/",
				"src/main/groovy/", "src/test/groovy/",
			} {
				if idx := strings.Index(slashPath, root); idx >= 0 {
					slashPath = slashPath[idx+len(root):]
					break
				}
			}
			// Fallback: strip src/ prefix if no standard marker found
			if strings.HasPrefix(slashPath, "src/") {
				slashPath = strings.TrimPrefix(slashPath, "src/")
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			dotted := strings.ReplaceAll(noExt2, "/", ".")
			register(dotted, filePath)
			// Register the package (dir only)
			pkgDotted := strings.ReplaceAll(filepath.ToSlash(filepath.Dir(slashPath)), "/", ".")
			register(pkgDotted, filePath)

		case "rust":
			// Rust: src/foo/bar.rs → "crate::foo::bar", "foo::bar", "bar"
			slashPath := filepath.ToSlash(filePath)
			// Strip workspace nesting: find last /src/ and use everything after
			if idx := strings.LastIndex(slashPath, "/src/"); idx >= 0 {
				slashPath = slashPath[idx+5:]
			} else {
				slashPath = strings.TrimPrefix(slashPath, "src/")
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			if stem == "mod" || stem == "lib" || stem == "main" {
				noExt2 = filepath.ToSlash(filepath.Dir(slashPath))
				if noExt2 == "." {
					noExt2 = ""
				}
			}
			if noExt2 == "" || noExt2 == "." {
				// Root lib.rs / main.rs — register just "crate"
				register("crate", filePath)
			} else {
				colonPath := strings.ReplaceAll(noExt2, "/", "::")
				register("crate::"+colonPath, filePath)
				register(colonPath, filePath)
				// Also register slash form for resolveModulePathRelative
				register(noExt2, filePath)
				register("src/"+noExt2, filePath)
				// Register short suffixes
				parts := strings.Split(colonPath, "::")
				for j := 1; j < len(parts); j++ {
					suffix := strings.Join(parts[j:], "::")
					register(suffix, filePath)
				}
			}

		case "csharp":
			// C#: Foo/Bar/Baz.cs → "Foo.Bar.Baz", "Bar.Baz", "Baz"
			slashPath := filepath.ToSlash(filePath)
			noExt2 := strings.TrimSuffix(slashPath, ext)
			dotted := strings.ReplaceAll(noExt2, "/", ".")
			register(dotted, filePath)
			parts := strings.Split(dotted, ".")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], ".")
				register(suffix, filePath)
			}

		case "php":
			// PHP PSR-4: src/App/Http/Controllers/FooController.php → "App\Http\Controllers\FooController"
			slashPath := filepath.ToSlash(filePath)
			for _, root := range []string{"src/", "app/", "lib/"} {
				if strings.HasPrefix(slashPath, root) {
					slashPath = strings.TrimPrefix(slashPath, root)
					break
				}
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			// Register backslash form (PHP namespace convention)
			bsPath := strings.ReplaceAll(noExt2, "/", `\`)
			register(bsPath, filePath)
			// Register slash form too for flexible matching
			register(noExt2, filePath)
			// Register just the class name
			register(stem, filePath)

		case "c", "cpp":
			// C/C++: include/foo/bar.h → "foo/bar.h", "foo/bar", "bar"
			slashPath := filepath.ToSlash(filePath)
			// Register the path as-is (matches #include "path")
			register(slashPath, filePath)
			// Strip include/ prefix
			for _, root := range []string{"include/", "inc/", "src/"} {
				if strings.HasPrefix(slashPath, root) {
					stripped := strings.TrimPrefix(slashPath, root)
					register(stripped, filePath)
				}
			}
			// Register without extension
			noExt2 := strings.TrimSuffix(slashPath, ext)
			register(noExt2, filePath)
			// Register just the stem
			register(stem, filePath)

		case "swift":
			// Swift: Sources/MyModule/Foo.swift → register directory as module
			slashDir := filepath.ToSlash(dir)
			register(slashDir, filePath)
			// Strip Sources/ prefix
			for _, root := range []string{"Sources/", "src/"} {
				if strings.HasPrefix(slashDir, root) {
					register(strings.TrimPrefix(slashDir, root), filePath)
				}
			}
			// Register shorter suffixes
			parts := strings.Split(slashDir, "/")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], "/")
				register(suffix, filePath)
			}

		case "ocaml":
			// OCaml: foo.ml → module name is capitalized stem: "Foo"
			moduleName := strings.ToUpper(stem[:1]) + stem[1:]
			register(moduleName, filePath)
			// Also register the raw stem
			register(stem, filePath)

		case "ruby":
			// Ruby: lib/foo/bar.rb → "foo/bar", "bar"
			slashPath := filepath.ToSlash(filePath)
			for _, root := range []string{"lib/", "app/", "src/"} {
				if strings.HasPrefix(slashPath, root) {
					slashPath = strings.TrimPrefix(slashPath, root)
					break
				}
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			register(noExt2, filePath)
			// Register shorter suffixes
			parts := strings.Split(noExt2, "/")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], "/")
				register(suffix, filePath)
			}
			// Also register just the stem
			register(stem, filePath)

		case "elixir":
			// Elixir: lib/my_app/user.ex → "MyApp.User" (camelized)
			slashPath := filepath.ToSlash(filePath)
			for _, root := range []string{"lib/", "src/"} {
				if strings.HasPrefix(slashPath, root) {
					slashPath = strings.TrimPrefix(slashPath, root)
					break
				}
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			// Register the slash form
			register(noExt2, filePath)
			// Register dotted form: my_app/user → MyApp.User
			parts := strings.Split(noExt2, "/")
			dottedParts := make([]string, len(parts))
			for k, p := range parts {
				// CamelCase: my_app → MyApp
				words := strings.Split(p, "_")
				for w := range words {
					if len(words[w]) > 0 {
						words[w] = strings.ToUpper(words[w][:1]) + words[w][1:]
					}
				}
				dottedParts[k] = strings.Join(words, "")
			}
			dotted := strings.Join(dottedParts, ".")
			register(dotted, filePath)
			// Register suffixes
			for j := 1; j < len(dottedParts); j++ {
				register(strings.Join(dottedParts[j:], "."), filePath)
			}

		case "lua":
			// Lua: lua/foo/bar.lua → "foo.bar", "bar"
			slashPath := filepath.ToSlash(filePath)
			for _, root := range []string{"lua/", "src/", "lib/"} {
				if strings.HasPrefix(slashPath, root) {
					slashPath = strings.TrimPrefix(slashPath, root)
					break
				}
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			// Lua uses dots: foo/bar → foo.bar
			dotted := strings.ReplaceAll(noExt2, "/", ".")
			register(dotted, filePath)
			// Register shorter suffixes
			parts := strings.Split(dotted, ".")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], ".")
				register(suffix, filePath)
			}
			register(stem, filePath)
		}
	}

	return fm
}

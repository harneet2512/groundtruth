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
// Go imports use full module paths (e.g., "github.com/org/repo/pkg/auth").
// BuildFileMap only registers directory paths ("pkg/auth", "auth").
// This function bridges the gap by registering "github.com/org/repo/pkg/auth" → same files.
func RegisterGoModulePaths(fm map[string][]string, goModulePath string) {
	if goModulePath == "" {
		return
	}
	additions := make(map[string][]string)
	for key, files := range fm {
		// Only process slash-separated directory paths (Go package dirs).
		// Skip: Rust (::), PHP (\), Python dotted (no slash), source files (.go etc)
		if strings.Contains(key, "::") || strings.Contains(key, `\`) {
			continue
		}
		if ext := filepath.Ext(key); ext != "" {
			continue
		}
		if strings.HasPrefix(key, goModulePath) {
			continue
		}
		// Skip Python dotted imports (e.g. "os.path") but NOT Go dirs with slashes
		if strings.Contains(key, ".") && !strings.Contains(key, "/") {
			continue
		}
		moduleKey := goModulePath + "/" + key
		additions[moduleKey] = files
	}
	for k, v := range additions {
		fm[k] = append(fm[k], v...)
	}
	// Also handle versioned modules: github.com/org/repo/v2/pkg → strip v2/ and try
	// Import "github.com/org/repo/v2/pkg" should match dir "pkg/"
	if parts := strings.Split(goModulePath, "/"); len(parts) > 0 {
		last := parts[len(parts)-1]
		if len(last) >= 2 && last[0] == 'v' && last[1] >= '0' && last[1] <= '9' {
			// Versioned module: github.com/org/repo/v2
			// Import "github.com/org/repo/v2/ast" → strip module prefix → "ast" → lookup
			// Already handled by suffix stripping in resolveModulePath.
			// But also register the full versioned path.
			unversioned := strings.Join(parts[:len(parts)-1], "/")
			for key, files := range fm {
				if strings.Contains(key, "::") || filepath.Ext(key) != "" {
					continue
				}
				if strings.Contains(key, ".") && !strings.Contains(key, "/") {
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

// RegisterGoVendorPaths strips vendor/ prefix from file map keys so that
// imports like "github.com/lib/pq" resolve to vendor/github.com/lib/pq/ files.
func RegisterGoVendorPaths(fm map[string][]string) {
	additions := make(map[string][]string)
	for key, files := range fm {
		if strings.HasPrefix(key, "vendor/") {
			stripped := strings.TrimPrefix(key, "vendor/")
			if _, exists := fm[stripped]; !exists {
				additions[stripped] = files
			}
		}
	}
	for k, v := range additions {
		fm[k] = append(fm[k], v...)
	}
}

// RegisterGoPackageNames scans Go files for `package X` declarations and
// registers the package name as an alias for the directory in the file map.
func RegisterGoPackageNames(fm map[string][]string, files []string, languages []string) {
	dirPackages := make(map[string]string)
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
				if idx := strings.IndexAny(pkgName, " \t/"); idx > 0 {
					pkgName = pkgName[:idx]
				}
				if pkgName != "" && pkgName != "main" {
					dirPackages[dir] = pkgName
				}
				break
			}
			if line != "" && !strings.HasPrefix(line, "//") && !strings.HasPrefix(line, "/*") {
				break
			}
		}
		f.Close()
	}
	for dir, pkg := range dirPackages {
		dirFiles, ok := fm[dir]
		if !ok {
			continue
		}
		if _, exists := fm[pkg]; exists {
			continue
		}
		fm[pkg] = dirFiles
	}
}

// BuildNodeMeta constructs the NodeMeta map from store nodes and their DB IDs.
func BuildNodeMeta(allNodes []store.Node, nodeDBIDs []int64) map[int64]NodeMeta {
	meta := make(map[int64]NodeMeta, len(nodeDBIDs))
	for i, n := range allNodes {
		if i < len(nodeDBIDs) {
			meta[nodeDBIDs[i]] = NodeMeta{
				Label:      n.Label,
				File:       n.FilePath,
				ParentID:   n.ParentID,
				Name:       n.Name,
				ReturnType: n.ReturnType,
			}
		}
	}
	return meta
}

// ResolvedCall is a call reference that has been resolved to a target node.
type ResolvedCall struct {
	SourceNodeID   int64
	TargetNodeID   int64
	SourceLine     int
	SourceFile     string
	Method         string  // "same_file", "import", "verified_unique", "type_flow", "name_match"
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

// stripTypeWrapper extracts the inner type from common wrapper types.
// Optional[User] → User, list[User] → User, List[User] → User, etc.
func stripTypeWrapper(t string) string {
	// Handle Optional[X], List[X], Set[X], Dict[K,V] → X or K
	idx := strings.Index(t, "[")
	if idx > 0 && strings.HasSuffix(t, "]") {
		inner := t[idx+1 : len(t)-1]
		// For Dict[K, V], take V (the value type)
		if comma := strings.LastIndex(inner, ","); comma > 0 {
			inner = strings.TrimSpace(inner[comma+1:])
		}
		return inner
	}
	// Handle Python pipe unions: User | None → User
	if pipe := strings.Index(t, " | "); pipe > 0 {
		left := strings.TrimSpace(t[:pipe])
		if left != "None" {
			return left
		}
		return strings.TrimSpace(t[pipe+3:])
	}
	// Handle pointer types: *User → User
	t = strings.TrimPrefix(t, "*")
	t = strings.TrimPrefix(t, "&")
	return t
}

// computeConfidence returns a confidence score based on resolution method and ambiguity.
func computeConfidence(method string, candidateCount int) float64 {
	switch method {
	case "same_file":
		return 1.0
	case "import":
		return 1.0
	case "verified_unique":
		return 0.95
	case "type_flow":
		return 0.9
	case "name_match":
		if candidateCount <= 1 {
			return 0.9
		} else if candidateCount == 2 {
			return 0.6
		} else if candidateCount <= 5 {
			return 0.4
		}
		return 0.2
	}
	return 0.3
}

// NodeMeta carries class/interface membership data for self.method resolution.
type NodeMeta struct {
	Label      string
	File       string
	ParentID   int64
	Name       string
	ReturnType string
}

// Resolve takes all call refs and all defined nodes, and resolves calls to definitions.
// Resolution strategies (in priority order):
//  1.    Same-file exact name match → "same_file" (conf=1.0)
//  1.25  Import-verified cross-file → "import" (conf=1.0)
//  1.75  self/this method via caller's class → "same_file" (conf=1.0)
//  1.9   Verified-unique: globally unique name → "verified_unique" (conf=0.95)
//  1.93  Import-scoped type_flow: import narrows class → "import_type" (conf=0.95)
//  1.95  Type-flow: qualified call on known class → "type_flow" (conf=0.9)
//  1.96  Assignment-flow: x = ClassName(); x.method() → "type_flow" (conf=0.9)
//        PyCG ICSE 2021: 99% precision from assignment tracking rules.
//  1.97  Return-type bridging: get_user().save() via return type → "return_type" (conf=0.85)
//  1.98  Unique-method-class: method name unique to one class → "unique_method" (conf=0.85)
//  2.    Cross-file name match → "name_match" (conf=0.2-0.6, fallback)
// assignmentIndex is set by the caller before Resolve() for Strategy 1.96.
var assignmentIndex map[string]*AssignmentMap

// inheritanceMap: child class DB ID → parent class DB IDs. Set before Resolve().
var inheritanceMap map[int64][]int64

// SetAssignmentIndex sets the global assignment index for Strategy 1.96.
func SetAssignmentIndex(idx map[string]*AssignmentMap) {
	assignmentIndex = idx
}

// SetInheritanceMap sets the class inheritance chain for method resolution.
func SetInheritanceMap(m map[int64][]int64) {
	inheritanceMap = m
}

// BuildAssignmentIndex builds a per-file variable→type map from parsed assignments.
// PyCG ICSE 2021: assignment tracking for x = ClassName() resolution.
func BuildAssignmentIndex(assignments []parser.AssignmentRef) map[string]*AssignmentMap {
	index := make(map[string]*AssignmentMap)
	for _, a := range assignments {
		if a.VarName == "" || a.TypeName == "" {
			continue
		}
		m, ok := index[a.File]
		if !ok {
			m = NewAssignmentMap()
			index[a.File] = m
		}
		m.Add(VarType{
			VarName:   a.VarName,
			TypeName:  a.TypeName,
			TypeFile:  "", // resolved later
			Scope:     a.Scope,
			Line:      a.Line,
			Confident: true,
		})
	}
	return index
}

func Resolve(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64, // name → list of node IDs
	fileNodeIDs map[string]map[string][]int64, // file → name → list of node IDs
	callerNodeIDs []int64, // parallel to allCalls
	allImports []parser.ImportRef, // all parsed import statements
	fileMap map[string][]string, // module path → list of file paths
	nodeMeta ...map[int64]NodeMeta, // optional: nodeID → metadata for self.method resolution
) []ResolvedCall {
	// Build import index: file → imported name → list of candidate target files
	importIndex := buildImportIndex(allImports, fileMap)

	// Build class-method index for self.method() resolution (Strategy 1.75)
	var methodsByClass map[int64]map[string]int64
	if len(nodeMeta) > 0 && nodeMeta[0] != nil {
		methodsByClass = make(map[int64]map[string]int64)
		for id, m := range nodeMeta[0] {
			if m.ParentID != 0 && (m.Label == "Method" || m.Label == "Function") {
				if methodsByClass[m.ParentID] == nil {
					methodsByClass[m.ParentID] = make(map[string]int64)
				}
				methodsByClass[m.ParentID][m.Name] = id
			}
		}
	}

	// lookupMethodWithInheritance walks the inheritance chain to find a method.
	// Returns (targetNodeID, found). Walks up to 10 levels to avoid cycles.
	lookupMethodWithInheritance := func(classID int64, methodName string) (int64, bool) {
		if methods, ok := methodsByClass[classID]; ok {
			if tid, ok := methods[methodName]; ok {
				return tid, true
			}
		}
		if inheritanceMap == nil {
			return 0, false
		}
		visited := map[int64]bool{classID: true}
		current := classID
		for depth := 0; depth < 10; depth++ {
			parents, ok := inheritanceMap[current]
			if !ok || len(parents) == 0 {
				return 0, false
			}
			for _, parentID := range parents {
				if visited[parentID] {
					continue
				}
				visited[parentID] = true
				if methods, ok := methodsByClass[parentID]; ok {
					if tid, ok := methods[methodName]; ok {
						return tid, true
					}
				}
			}
			current = parents[0]
		}
		return 0, false
	}

	// Build unique-method-class index: method names that belong to exactly one class.
	// "filter" exists only in QuerySet → self.queryset.filter() resolves to QuerySet.filter.
	methodClassCount := make(map[string]map[int64]bool)
	for classID, methods := range methodsByClass {
		for methodName := range methods {
			if methodClassCount[methodName] == nil {
				methodClassCount[methodName] = make(map[int64]bool)
			}
			methodClassCount[methodName][classID] = true
		}
	}
	uniqueMethodClass := make(map[string]int64)
	for methodName, classes := range methodClassCount {
		if len(classes) == 1 {
			for classID := range classes {
				uniqueMethodClass[methodName] = classID
			}
		}
	}

	var resolved []ResolvedCall
	seen := make(map[edgeKey]bool) // deduplication

	for i, call := range allCalls {
		callerID := callerNodeIDs[i]
		if callerID == 0 {
			continue
		}

		calleeName := call.CalleeName

		// Strategy 1: Same-file exact name match (only when unambiguous)
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
			// Multiple same-name definitions in this file: fall through to name_match
		}

		// Strategy 1.5: Import-verified cross-file resolution
		// H6 fix: collect all matching imported targets, pick best (prefer same dir)
		if fileImports, ok := importIndex[call.File]; ok {
			var importCandidates []int64

			// Check specific imports
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

			// Go package-qualified calls: "auth.Login" → look up "auth" in imports,
			// then find "Login" in the target files.
			if len(importCandidates) == 0 && call.CalleeQualified != "" && call.CalleeQualified != calleeName {
				if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
					pkgAlias := call.CalleeQualified[:dotIdx]
					funcName := call.CalleeQualified[dotIdx+1:]
					if candidateFiles, ok := fileImports[pkgAlias]; ok {
						for _, targetFile := range candidateFiles {
							if fileNodes, ok := fileNodeIDs[targetFile]; ok {
								if targetIDs, ok := fileNodes[funcName]; ok {
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
				// Pick best: first candidate (import order is meaningful)
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

		// Strategy 1.75: self/this method resolution via caller's class + inheritance (conf=1.0/0.95)
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				if qualifier == "self" || qualifier == "this" {
					callerMeta, hasMeta := nodeMeta[0][callerID]
					if hasMeta && callerMeta.ParentID != 0 {
						memberName := call.CalleeQualified[dotIdx+1:]
						if targetID, found := lookupMethodWithInheritance(callerMeta.ParentID, memberName); found && targetID != callerID {
							// Determine if same-class or inherited
							targetMeta := nodeMeta[0][targetID]
							method := "same_file"
							conf := 1.0
							evidence := "ast_call"
							if targetMeta.ParentID != callerMeta.ParentID {
								method = "inherited"
								conf = 0.95
								evidence = "inheritance_chain"
							}
							key := edgeKey{callerID, targetID, "CALLS"}
							if !seen[key] {
								seen[key] = true
								resolved = append(resolved, ResolvedCall{
									SourceNodeID:   callerID,
									TargetNodeID:   targetID,
									SourceLine:     call.Line,
									SourceFile:     call.File,
									Method:         method,
									Confidence:     conf,
									CandidateCount: 1,
									TrustTier:      "CERTIFIED",
									EvidenceType:   evidence,
								})
							}
							continue
						}
					}
				}
			}
		}

		// Strategy 1.9 (T1): Verified-unique cross-file resolution
		// ACG (ECOOP 2022): globally unique function names are 99%+ correct — but
		// that holds only for UNQUALIFIED calls. A qualified call X.attr(...) that
		// reached here did NOT resolve its qualifier via the import/type stages
		// above, so X is a stdlib/external/unknown receiver (e.g. `os.walk`). The
		// single-candidate cross-file match is the ONLY resolver stage that fires
		// for one candidate (Strategy 2 below needs 2+), so we must NOT drop it —
		// that would lose a real fallback edge. Instead DEMOTE it: emit name_match
		// (low trust) rather than verified_unique (deterministic), so a qualified
		// stdlib call never launders as a confident fact downstream while the agent
		// still gets the hint. [beancount-931 os.walk -> account.walk]
		qualifiedUnresolved := call.CalleeQualified != "" && call.CalleeQualified != calleeName
		if targets, ok := nodeIDs[calleeName]; ok {
			var candidates []int64
			for _, tid := range targets {
				if tid != callerID {
					candidates = append(candidates, tid)
				}
			}
			if len(candidates) == 1 {
				targetID := candidates[0]
				key := edgeKey{callerID, targetID, "CALLS"}
				if !seen[key] {
					seen[key] = true
					method, conf, tier, evidence := "verified_unique", 0.95, "CERTIFIED", "name_unique"
					if qualifiedUnresolved {
						method = "name_match"
						conf = computeConfidence("name_match", 1)
						tier = "SPECULATIVE"
						evidence = "name_match_qualified_unresolved"
					}
					resolved = append(resolved, ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   targetID,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         method,
						Confidence:     conf,
						CandidateCount: 1,
						TrustTier:      tier,
						EvidenceType:   evidence,
					})
				}
				continue
			}
		}

		// Strategy 1.93: Import-scoped type_flow
		// When caller imports ClassName from a specific file, scope class lookup to that file.
		// Fixes ambiguity when multiple classes share a name (e.g., "Client" in 5 files).
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+1:]
				if qualifier != "self" && qualifier != "this" {
						if fileImports, ok := importIndex[call.File]; ok {
						if candidateFiles, ok := fileImports[qualifier]; ok {
							for _, targetFile := range candidateFiles {
								if fileNodes, ok := fileNodeIDs[targetFile]; ok {
									if classNodeIDs, ok := fileNodes[qualifier]; ok {
										for _, classID := range classNodeIDs {
											cm, hasMeta := nodeMeta[0][classID]
											if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
												continue
											}
											if methods, ok := methodsByClass[classID]; ok {
												if targetID, ok := methods[methodName]; ok && targetID != callerID {
													key := edgeKey{callerID, targetID, "CALLS"}
													if !seen[key] {
														seen[key] = true
														resolved = append(resolved, ResolvedCall{
															SourceNodeID:   callerID,
															TargetNodeID:   targetID,
															SourceLine:     call.Line,
															SourceFile:     call.File,
															Method:         "import_type",
															Confidence:     0.95,
															CandidateCount: 1,
															TrustTier:      "CERTIFIED",
															EvidenceType:   "import_scoped_type",
														})
													}
													goto nextCall
												}
											}
										}
									}
								}
							}
						}
					}
				}
			}
		}

		// Strategy 1.95 (T2): Type-flow resolution for qualified calls
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && call.CalleeQualified != "" {
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+1:]
				if qualifier != "self" && qualifier != "this" {
					if classIDs, ok := nodeIDs[qualifier]; ok {
						for _, classID := range classIDs {
							cm, hasMeta := nodeMeta[0][classID]
							if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
								continue
							}
							if methods, ok := methodsByClass[classID]; ok {
								if targetID, ok := methods[methodName]; ok && targetID != callerID {
									key := edgeKey{callerID, targetID, "CALLS"}
									if !seen[key] {
										seen[key] = true
										resolved = append(resolved, ResolvedCall{
											SourceNodeID:   callerID,
											TargetNodeID:   targetID,
											SourceLine:     call.Line,
											SourceFile:     call.File,
											Method:         "type_flow",
											Confidence:     0.9,
											CandidateCount: 1,
											TrustTier:      "CERTIFIED",
											EvidenceType:   "type_qualified",
										})
									}
									goto nextCall
								}
							}
						}
					}
				}
			}
		}

		// Strategy 1.96: Assignment-flow resolution (PyCG ICSE 2021)
		// x = ClassName(); x.method() → resolve method via assignment tracking
		if assignmentIndex != nil && call.CalleeQualified != "" {
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+1:]
				// Handle self.x.method() → strip "self." to get "x"
				if strings.HasPrefix(qualifier, "self.") {
					qualifier = qualifier[5:]
				} else if strings.HasPrefix(qualifier, "this.") {
					qualifier = qualifier[5:]
				}
				if qualifier != "self" && qualifier != "this" && qualifier != "super" && qualifier != "" {
						if fileAssignments, ok := assignmentIndex[call.File]; ok {
						if className, _, found := fileAssignments.ResolveQualifiedCall(qualifier, methodName); found {
								// Look up the class in nodeIDs, then find the method
							if classIDs, ok := nodeIDs[className]; ok {
								for _, classID := range classIDs {
									if len(nodeMeta) > 0 && nodeMeta[0] != nil {
										cm, hasMeta := nodeMeta[0][classID]
										if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct") {
											continue
										}
										if methods, ok := methodsByClass[classID]; ok {
											if targetID, ok := methods[methodName]; ok && targetID != callerID {
												key := edgeKey{callerID, targetID, "CALLS"}
												if !seen[key] {
													seen[key] = true
													resolved = append(resolved, ResolvedCall{
														SourceNodeID:   callerID,
														TargetNodeID:   targetID,
														SourceLine:     call.Line,
														SourceFile:     call.File,
														Method:         "type_flow",
														Confidence:     0.9,
														CandidateCount: 1,
														TrustTier:      "CERTIFIED",
														EvidenceType:   "assignment_tracked",
													})
												}
												goto nextCall
											}
										}
									}
								}
							}
						}
					}
				}
			}
		}

		// Strategy 1.97: Return-type bridging
		// get_user().save() → look up get_user's return type → resolve save on that type.
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+1:]
				if qualifier != "self" && qualifier != "this" && qualifier != "super" {
					// Check if qualifier is a function call: look for a function with this name
					if funcIDs, ok := nodeIDs[qualifier]; ok {
						for _, funcID := range funcIDs {
							fm, hasMeta := nodeMeta[0][funcID]
							if !hasMeta || fm.ReturnType == "" {
								continue
							}
							if fm.Label == "Class" || fm.Label == "Struct" || fm.Label == "Interface" {
								continue
							}
							retType := fm.ReturnType
							// Strip common wrappers: Optional[X] → X, list[X] → X
							retType = stripTypeWrapper(retType)
							if retType == "" {
								continue
							}
							if classIDs, ok := nodeIDs[retType]; ok {
								for _, classID := range classIDs {
									cm, hasMeta := nodeMeta[0][classID]
									if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
										continue
									}
									if methods, ok := methodsByClass[classID]; ok {
										if targetID, ok := methods[methodName]; ok && targetID != callerID {
											key := edgeKey{callerID, targetID, "CALLS"}
											if !seen[key] {
												seen[key] = true
												resolved = append(resolved, ResolvedCall{
													SourceNodeID:   callerID,
													TargetNodeID:   targetID,
													SourceLine:     call.Line,
													SourceFile:     call.File,
													Method:         "return_type",
													Confidence:     0.85,
													CandidateCount: 1,
													TrustTier:      "CERTIFIED",
													EvidenceType:   "return_type_flow",
												})
											}
											goto nextCall
										}
									}
								}
							}
						}
					}
				}
			}
		}

		// Strategy 1.98: Unique-method-class resolution
		// If a method name belongs to exactly one class in the codebase, and this is a
		// qualified call (obj.method()), resolve to that class's method.
		// e.g., "filter" exists only in QuerySet → any x.filter() resolves to QuerySet.filter.
		if call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			if classID, ok := uniqueMethodClass[calleeName]; ok {
				if methods, ok := methodsByClass[classID]; ok {
					if targetID, ok := methods[calleeName]; ok && targetID != callerID {
						key := edgeKey{callerID, targetID, "CALLS"}
						if !seen[key] {
							seen[key] = true
							resolved = append(resolved, ResolvedCall{
								SourceNodeID:   callerID,
								TargetNodeID:   targetID,
								SourceLine:     call.Line,
								SourceFile:     call.File,
								Method:         "unique_method",
								Confidence:     0.85,
								CandidateCount: 1,
								TrustTier:      "CANDIDATE",
								EvidenceType:   "unique_method_class",
							})
						}
						continue
					}
				}
			}
		}

		// Strategy 2: Cross-file name match (fallback, 2+ candidates only)
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

			if bestTarget != 0 && candidateCount > 1 {
				conf := computeConfidence("name_match", candidateCount)
				tier := "SPECULATIVE"
				if candidateCount == 2 {
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
	nextCall:
	}

	return resolved
}

// buildImportIndex creates: callerFile → importedName → []targetFiles
// This tells us: "file X imports name Y, which could come from files [A, B, ...]"
func buildImportIndex(imports []parser.ImportRef, fileMap map[string][]string) map[string]map[string][]string {
	index := make(map[string]map[string][]string)

	// Cache resolveModulePath results — same module path resolved many times
	moduleCache := make(map[string][]string)

	for _, imp := range imports {
		if imp.ImportedName == "" {
			continue
		}

		fileEntry, ok := index[imp.File]
		if !ok {
			fileEntry = make(map[string][]string)
			index[imp.File] = fileEntry
		}

		// JS/TS relative imports: resolve ./foo or ../bar relative to caller dir
		effectivePath := imp.ModulePath
		if strings.HasPrefix(effectivePath, "./") || strings.HasPrefix(effectivePath, "../") {
			callerDir := filepath.ToSlash(filepath.Dir(imp.File))
			effectivePath = filepath.ToSlash(filepath.Join(callerDir, effectivePath))
			effectivePath = filepath.ToSlash(filepath.Clean(effectivePath))
		}

		// Resolve the module path to actual files (cached)
		cacheKey := effectivePath
		targetFiles, cached := moduleCache[cacheKey]
		if !cached {
			targetFiles = resolveModulePath(effectivePath, fileMap)
			moduleCache[cacheKey] = targetFiles
		}

		// If module path didn't resolve, try module_path + imported_name (cached)
		if len(targetFiles) == 0 && imp.ImportedName != "*" && effectivePath != "" {
			combined := effectivePath + "." + imp.ImportedName
			if cached, ok := moduleCache[combined]; ok {
				targetFiles = cached
			} else {
				targetFiles = resolveModulePath(combined, fileMap)
				moduleCache[combined] = targetFiles
			}
			if len(targetFiles) == 0 {
				combinedSlash := strings.ReplaceAll(effectivePath, ".", "/") + "/" + imp.ImportedName
				if cached, ok := moduleCache[combinedSlash]; ok {
					targetFiles = cached
				} else {
					targetFiles = resolveModulePath(combinedSlash, fileMap)
					moduleCache[combinedSlash] = targetFiles
				}
			}
		}

		if len(targetFiles) > 0 {
			fileEntry[imp.ImportedName] = append(fileEntry[imp.ImportedName], targetFiles...)
		}
	}

	return index
}

// resolveModulePath maps a module path string to actual source file paths.
// Returns all matching files. Uses only O(1) hash lookups (no linear scan).
func resolveModulePath(modulePath string, fileMap map[string][]string) []string {
	if modulePath == "" {
		return nil
	}

	if files, ok := fileMap[modulePath]; ok {
		return files
	}

	// Python dotted paths: foo.bar.baz → foo/bar/baz
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

	// Go module paths: github.com/org/repo/v2/pkg/auth → try progressively
	// shorter suffixes (auth, pkg/auth, v2/pkg/auth) until one matches.
	if strings.Contains(modulePath, "/") && strings.Contains(modulePath, ".") {
		parts := strings.Split(modulePath, "/")
		for j := len(parts) - 1; j >= 1; j-- {
			suffix := strings.Join(parts[j:], "/")
			if files, ok := fileMap[suffix]; ok {
				return files
			}
		}
	}

	// Rust module paths: crate::foo::bar → try foo::bar, then foo/bar
	// Also handles self:: (current module) and super:: (parent module)
	if strings.Contains(modulePath, "::") {
		// Strip path-relative prefixes: crate:: is the crate root,
		// self:: is the current module, super:: is the parent.
		// Without caller file context we can only strip them and
		// rely on suffix matching below to find the target.
		stripped := modulePath
		stripped = strings.TrimPrefix(stripped, "crate::")
		stripped = strings.TrimPrefix(stripped, "self::")
		stripped = strings.TrimPrefix(stripped, "super::")

		// Direct lookup (handles workspace crate keys like axum_core::extract)
		if files, ok := fileMap[stripped]; ok {
			return files
		}
		// Also try the full modulePath as-is (registerRustCrate may have
		// registered crate_name::module keys that match exactly)
		if stripped != modulePath {
			if files, ok := fileMap[modulePath]; ok {
				return files
			}
		}

		slashForm := strings.ReplaceAll(stripped, "::", "/")
		if files, ok := fileMap[slashForm]; ok {
			return files
		}
		// Try with src/ prefix
		if files, ok := fileMap["src/"+slashForm]; ok {
			return files
		}

		// For workspace crate paths (axum_core::extract), also try
		// crate:: prefix form since BuildFileMap registers crate::module
		if !strings.HasPrefix(modulePath, "crate::") {
			crateForm := "crate::" + stripped
			if files, ok := fileMap[crateForm]; ok {
				return files
			}
		}

		// Try suffix matching (progressively shorter colon-separated suffixes)
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

// RegisterRustCratePaths parses Cargo.toml to find workspace members and
// registers crate_name::module → files mappings in the file map.
// Handles [workspace] members and [package] name entries.
func RegisterRustCratePaths(fm map[string][]string, root string) {
	cargoPath := filepath.Join(root, "Cargo.toml")
	data, err := os.ReadFile(cargoPath)
	if err != nil {
		return
	}
	content := string(data)

	// Extract workspace members from [workspace] members = ["crate_a", "crate_b"]
	var memberDirs []string
	if idx := strings.Index(content, "members"); idx >= 0 {
		rest := content[idx:]
		if brk := strings.Index(rest, "["); brk >= 0 {
			rest = rest[brk:]
			if end := strings.Index(rest, "]"); end >= 0 {
				arr := rest[1:end]
				for _, item := range strings.Split(arr, ",") {
					dir := strings.TrimSpace(item)
					dir = strings.Trim(dir, `"' `)
					if dir != "" && !strings.Contains(dir, "*") {
						memberDirs = append(memberDirs, dir)
					}
				}
			}
		}
	}

	// For each workspace member, read its Cargo.toml to get the crate name
	for _, dir := range memberDirs {
		memberCargo := filepath.Join(root, dir, "Cargo.toml")
		mdata, err := os.ReadFile(memberCargo)
		if err != nil {
			// Default: use directory base name as crate name
			crateName := strings.ReplaceAll(filepath.Base(dir), "-", "_")
			registerRustCrate(fm, root, dir, crateName)
			continue
		}
		mcontent := string(mdata)
		crateName := ""
		if ni := strings.Index(mcontent, "name"); ni >= 0 {
			rest := mcontent[ni:]
			if eq := strings.Index(rest, "="); eq >= 0 {
				val := strings.TrimSpace(rest[eq+1:])
				if nl := strings.IndexByte(val, '\n'); nl >= 0 {
					val = val[:nl]
				}
				crateName = strings.Trim(strings.TrimSpace(val), `"' `)
			}
		}
		if crateName == "" {
			crateName = strings.ReplaceAll(filepath.Base(dir), "-", "_")
		}
		registerRustCrate(fm, root, dir, crateName)
	}

	// Also register the root crate if it has a [package] name
	if idx := strings.Index(content, "[package]"); idx >= 0 {
		rest := content[idx:]
		if ni := strings.Index(rest, "name"); ni >= 0 {
			nameRest := rest[ni:]
			if eq := strings.Index(nameRest, "="); eq >= 0 {
				val := strings.TrimSpace(nameRest[eq+1:])
				if nl := strings.IndexByte(val, '\n'); nl >= 0 {
					val = val[:nl]
				}
				crateName := strings.Trim(strings.TrimSpace(val), `"' `)
				if crateName != "" {
					registerRustCrate(fm, root, ".", crateName)
				}
			}
		}
	}
}

func registerRustCrate(fm map[string][]string, root, dir, crateName string) {
	crateName = strings.ReplaceAll(crateName, "-", "_")
	srcDir := filepath.ToSlash(filepath.Join(dir, "src"))

	// Collect keys to add (don't mutate map during iteration)
	type entry struct {
		key   string
		files []string
	}
	var toAdd []entry

	for key, files := range fm {
		if !strings.HasPrefix(key, srcDir+"/") && key != srcDir {
			continue
		}
		suffix := strings.TrimPrefix(key, srcDir)
		suffix = strings.TrimPrefix(suffix, "/")
		if suffix == "" {
			toAdd = append(toAdd, entry{crateName, files})
			continue
		}

		// Strip .rs extension — raw file paths have it, module keys don't
		if strings.HasSuffix(suffix, ".rs") {
			suffix = strings.TrimSuffix(suffix, ".rs")
		}

		// mod.rs / lib.rs / main.rs represent the parent module, not a child
		// e.g. axum-core/src/extract/mod.rs → crate module "extract", not "extract::mod"
		base := filepath.Base(suffix)
		if base == "mod" || base == "lib" || base == "main" {
			suffix = filepath.ToSlash(filepath.Dir(suffix))
			if suffix == "." {
				// src/lib.rs or src/mod.rs → represents the crate root
				toAdd = append(toAdd, entry{crateName, files})
				continue
			}
		}

		colonSuffix := strings.ReplaceAll(suffix, "/", "::")
		if colonSuffix != "" {
			toAdd = append(toAdd, entry{crateName + "::" + colonSuffix, files})
		} else {
			toAdd = append(toAdd, entry{crateName, files})
		}
	}

	for _, e := range toAdd {
		fm[e.key] = append(fm[e.key], e.files...)
	}
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
			// Strip multiple common prefixes for workspace crates
			for _, pfx := range []string{"src/", "crates/", "core/engine/src/", "core/src/"} {
				if strings.HasPrefix(slashPath, pfx) {
					slashPath = strings.TrimPrefix(slashPath, pfx)
					break
				}
			}
			// Also strip any path up to and including "/src/"
			if idx := strings.LastIndex(slashPath, "/src/"); idx >= 0 {
				slashPath = slashPath[idx+5:]
			}
			noExt2 := strings.TrimSuffix(slashPath, ext)
			if stem == "mod" || stem == "lib" || stem == "main" {
				noExt2 = filepath.ToSlash(filepath.Dir(slashPath))
				if noExt2 == "." {
					noExt2 = ""
				}
			}
			if noExt2 == "" {
				continue
			}
			colonPath := strings.ReplaceAll(noExt2, "/", "::")
			register("crate::"+colonPath, filePath)
			register(colonPath, filePath)
			// Register short suffixes
			parts := strings.Split(colonPath, "::")
			for j := 1; j < len(parts); j++ {
				suffix := strings.Join(parts[j:], "::")
				register(suffix, filePath)
			}
			// Register slash-form too (for resolveModulePathRelative)
			register(noExt2, filePath)
			register("src/"+noExt2, filePath)

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

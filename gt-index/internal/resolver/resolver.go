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

// tierFor maps a confidence score to a TrustTier via the single CLAUDE.md threshold
// table (CLAUDE.md:222) — the ONE source of truth so tier always follows confidence
// and a 0.85 edge can NEVER be stamped CERTIFIED. Used at every emit site instead of a
// hardcoded TrustTier literal, eliminating the conf↔tier mismatches where the same
// structural fact (e.g. 0.85 impl_method vs unique_method) carried different tiers.
//
//	conf >= 0.9  -> "CERTIFIED"
//	0.5 <= conf < 0.9 -> "CANDIDATE"
//	conf < 0.5   -> "SPECULATIVE"
func tierFor(conf float64) string {
	if conf >= 0.9 {
		return "CERTIFIED"
	}
	if conf >= 0.5 {
		return "CANDIDATE"
	}
	return "SPECULATIVE"
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

// pickBestImportCandidate implements the same-dir tie-break the Strategy-1.5 comment
// promises (finding #40). Among ≥1 candidate target node IDs it prefers a target whose
// file is in the caller's directory; otherwise it returns the candidate with the
// lexicographically-smallest (file, id) so the pick is DETERMINISTIC across runs (Go map
// order is randomized — `importCandidates[0]` was run-dependent). Returns the chosen
// target and whether a same-directory winner existed (the caller demotes below CERTIFIED
// when there are multiple candidates and NO same-dir winner — an ambiguous import is not a
// deterministic fact). When meta is absent it falls back to the smallest node ID (stable).
func pickBestImportCandidate(callerFile string, candidates []int64, meta map[int64]NodeMeta) (int64, bool) {
	if len(candidates) == 0 {
		return 0, false
	}
	callerDir := filepath.ToSlash(filepath.Dir(callerFile))
	best := int64(0)
	bestFile := ""
	sameDir := false
	for _, tid := range candidates {
		tf := ""
		if meta != nil {
			if m, ok := meta[tid]; ok {
				tf = filepath.ToSlash(m.File)
			}
		}
		inSameDir := tf != "" && filepath.ToSlash(filepath.Dir(tf)) == callerDir
		switch {
		case inSameDir && !sameDir:
			// First same-dir candidate always wins over any cross-dir pick.
			best, bestFile, sameDir = tid, tf, true
		case inSameDir == sameDir:
			// Same locality class: deterministic tie-break by (file, id).
			if best == 0 || tf < bestFile || (tf == bestFile && tid < best) {
				best, bestFile = tid, tf
			}
		}
	}
	if best == 0 {
		best = candidates[0]
	}
	return best, sameDir
}

// pickBestLocalTarget chooses the best same-file target among ≥1 candidate node IDs
// sharing a name (finding #39). It is DETERMINISTIC and language-agnostic: it prefers a
// callable-labelled node (Function/Method) over a same-named Class/other, then breaks ties
// by smallest node ID so the result is stable across runs (Go slice order is stable but the
// label preference + min-ID rule guarantees one answer). The caller itself is excluded.
// Returns 0 when no eligible candidate exists.
func pickBestLocalTarget(candidates []int64, callerID int64, meta map[int64]NodeMeta) int64 {
	best := int64(0)
	bestIsCallable := false
	for _, tid := range candidates {
		if tid == callerID {
			continue
		}
		callable := false
		if meta != nil {
			if m, ok := meta[tid]; ok {
				callable = m.Label == "Function" || m.Label == "Method"
			}
		}
		switch {
		case best == 0:
			best, bestIsCallable = tid, callable
		case callable && !bestIsCallable:
			// A callable target always beats a non-callable same-named node.
			best, bestIsCallable = tid, true
		case callable == bestIsCallable && tid < best:
			// Same callability class: deterministic min-ID tie-break.
			best = tid
		}
	}
	return best
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
//  1.75  self/this/Self method via caller's class → "same_file" (conf=1.0)
//  1.9   Verified-unique: globally unique name → "verified_unique" (conf=0.95)
//  1.93  Import-scoped type_flow: import narrows class → "import_type" (conf=0.95)
//  1.94  Single/few-implementor: method unique to 1-3 classes → "impl_method" (conf=0.4-0.85)
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

// paramTypeIndex: caller node DB ID → {param/field name → declared type name}. Set
// before Resolve() for Strategy 1.94b (T1 declared-type receiver resolution). Populated
// from the `param` properties the parser already extracts (declared annotations), so a
// typed receiver `command.run()` resolves to the param's class without re-parsing.
// Generalized across statically-typed languages (Go/Rust/Java/TS + annotated Python).
var paramTypeIndex map[int64]map[string]string

// builtinMethodNames: methods of language builtin/stdlib types (str/dict/list/set
// and equivalents). A QUALIFIED call obj.method() that reaches Strategy 2 did NOT
// resolve its receiver to an internal class above — so a builtin method name here
// means a builtin call (str.join, dict.get), NOT an internal graph edge. T2
// (application-centered — JARVIS 2023 / PyCG ICSE 2021): DROP it rather than emit a
// name_match guess to an arbitrary same-named internal method. Removes the dominant
// name_match garbage (conan-17123: join×1106, get×354, items×254, append×228, ...).
var builtinMethodNames = map[string]bool{
	"join": true, "split": true, "splitlines": true, "strip": true, "lstrip": true,
	"rstrip": true, "lower": true, "upper": true, "title": true, "startswith": true,
	"endswith": true, "encode": true, "decode": true, "format": true, "replace": true,
	"find": true, "rfind": true,
	"get": true, "keys": true, "values": true, "items": true, "setdefault": true,
	"update": true, "popitem": true,
	"append": true, "extend": true, "pop": true, "insert": true, "remove": true,
	"index": true, "count": true, "sort": true, "reverse": true, "add": true,
	"discard": true, "clear": true, "copy": true,
}

// strongBuiltinMethodNames: method names that are essentially ALWAYS builtin/stdlib
// (str / os.path / bytes) and almost never an internal method name. Unlike the broader
// builtinMethodNames set (applied only to multi-candidate name_match), these are safe to
// drop even on a SINGLE-candidate qualified-unresolved call (Strategy 1.9 demote), because
// a real internal method by these names is vanishingly rare. Catches os.path.join /
// str.split — the conan-17123 `join`×933 / `split`×122 single-candidate residual the
// multi-candidate Strategy-2 guard cannot see. Excludes ambiguous names (get/update/items)
// that legitimately occur as internal methods — those stay multi-candidate-only.
var strongBuiltinMethodNames = map[string]bool{
	"join": true, "split": true, "rsplit": true, "splitlines": true,
	"strip": true, "lstrip": true, "rstrip": true,
	"encode": true, "decode": true, "startswith": true, "endswith": true,
	"zfill": true, "casefold": true,
	// stdlib serialization (json/pickle/yaml/marshal) — qualified module calls
	// (json.loads), never internal method names. conan-17123: loads×188 = all json.loads.
	"loads": true, "dumps": true,
}

// SetAssignmentIndex sets the global assignment index for Strategy 1.96.
func SetAssignmentIndex(idx map[string]*AssignmentMap) {
	assignmentIndex = idx
}

// SetInheritanceMap sets the class inheritance chain for method resolution.
func SetInheritanceMap(m map[int64][]int64) {
	inheritanceMap = m
}

// SetParamTypeIndex sets the caller→param→type map for Strategy 1.94b (T1).
func SetParamTypeIndex(idx map[int64]map[string]string) {
	paramTypeIndex = idx
}

// BuildParamTypeIndex builds caller-node-DB-ID → {paramName → typeName} from the
// `param` properties (value form "name:type [flags]") the parser extracts. nodeDBIDs
// is parallel to the global node slice the properties' NodeIdx indexes into.
func BuildParamTypeIndex(props []parser.PropertyRef, nodeDBIDs []int64) map[int64]map[string]string {
	idx := make(map[int64]map[string]string)
	for _, p := range props {
		if p.Kind != "param" || p.NodeIdx < 0 || p.NodeIdx >= len(nodeDBIDs) {
			continue
		}
		dbid := nodeDBIDs[p.NodeIdx]
		if dbid <= 0 {
			continue
		}
		val := p.Value
		colon := strings.Index(val, ":")
		if colon <= 0 {
			continue
		}
		name := strings.TrimSpace(val[:colon])
		typ := strings.TrimSpace(val[colon+1:])
		if sp := strings.IndexAny(typ, " ["); sp > 0 { // strip " [required]" / "[..]" suffix flags
			typ = strings.TrimSpace(typ[:sp])
		}
		if name == "" || typ == "" {
			continue
		}
		if idx[dbid] == nil {
			idx[dbid] = make(map[string]string)
		}
		idx[dbid][name] = typ
	}
	return idx
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
			Confident: !a.ViaReturn, // direct constructor = confident; factory-return = tentative
			ViaReturn: a.ViaReturn,
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

	// metaMap: nodeID → NodeMeta, the single accessor for the optional variadic
	// nodeMeta[0] (nil when absent). Used by the Strategy-1.5 same-dir tie-break (#40).
	var metaMap map[int64]NodeMeta
	if len(nodeMeta) > 0 && nodeMeta[0] != nil {
		metaMap = nodeMeta[0]
	}

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
						TrustTier:      tierFor(1.0),
						EvidenceType:   "ast_call",
					})
				}
				continue
			}
			// #39: Multiple same-name definitions in this file. Previously this
			// abandoned same-file resolution entirely and fell through to a
			// cross-file name_match — discarding the strongest locality signal for a
			// speculative remote guess. Same-file locality dominates: for an
			// UNQUALIFIED call (no receiver type to infer; the type-flow strategies
			// below cannot apply) prefer the best LOCAL candidate at CANDIDATE tier
			// over any cross-file name_match. Qualified calls still fall through so
			// the receiver-typing strategies (1.75/1.94a/1.95/1.96) resolve precisely.
			isUnqualified := call.CalleeQualified == "" || call.CalleeQualified == calleeName
			if targetIDs, ok := fileNodes[calleeName]; ok && len(targetIDs) > 1 && isUnqualified {
				if best := pickBestLocalTarget(targetIDs, callerID, metaMap); best != 0 {
					key := edgeKey{callerID, best, "CALLS"}
					if !seen[key] {
						seen[key] = true
						// 0.6 = CANDIDATE: locality is strong, but WHICH same-named
						// local definition is the target is not certain → not CERTIFIED.
						resolved = append(resolved, ResolvedCall{
							SourceNodeID:   callerID,
							TargetNodeID:   best,
							SourceLine:     call.Line,
							SourceFile:     call.File,
							Method:         "same_file",
							Confidence:     0.6,
							CandidateCount: len(targetIDs),
							TrustTier:      tierFor(0.6),
							EvidenceType:   "same_file_ambiguous",
						})
					}
					continue
				}
			}
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
				// #40: implement the promised same-dir tie-break (prefer a target in the
				// caller's directory; else lexicographically-smallest path) so the pick is
				// DETERMINISTIC instead of map-order `importCandidates[0]`. When >1 candidate
				// files export the same name and NONE is same-dir, the pick is an ambiguous
				// guess — demote below CERTIFIED rather than stamping conf 1.0 on a coin-flip.
				bestTarget, sameDirWinner := pickBestImportCandidate(call.File, importCandidates, metaMap)
				conf := 1.0
				evidence := "ast_import"
				if len(importCandidates) > 1 && !sameDirWinner {
					conf = 0.6 // CANDIDATE: import is real, the among-files pick is not certain
					evidence = "ast_import_ambiguous"
				}
				key := edgeKey{callerID, bestTarget, "CALLS"}
				if !seen[key] {
					seen[key] = true
					resolved = append(resolved, ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   bestTarget,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         "import",
						Confidence:     conf,
						CandidateCount: len(importCandidates),
						TrustTier:      tierFor(conf),
						EvidenceType:   evidence,
					})
				}
				continue
			}
		}

		// Strategy 1.75: self/this/Self method resolution via caller's class + inheritance (conf=1.0/0.95)
		// Handles: self.method() (Python/Rust), this.method() (JS/TS/Java),
		//          Self::method() (Rust associated fn — Self is the impl's type)
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			// Try "." separator first (self.method, this.method), then "::" (Self::method)
			dotIdx175 := strings.LastIndex(call.CalleeQualified, ".")
			sep175 := 1
			if dotIdx175 <= 0 {
				dotIdx175 = strings.LastIndex(call.CalleeQualified, "::")
				sep175 = 2
			}
			if dotIdx175 > 0 {
				qualifier := call.CalleeQualified[:dotIdx175]
				if qualifier == "self" || qualifier == "this" || qualifier == "Self" {
					callerMeta, hasMeta := nodeMeta[0][callerID]
					if hasMeta && callerMeta.ParentID != 0 {
						memberName := call.CalleeQualified[dotIdx175+sep175:]
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
									TrustTier:      tierFor(conf),
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
		// T2 (builtins): a qualified call obj.method() whose receiver never resolved to an
		// internal class above + method is a builtin/stdlib name = a builtin call
		// (os.path.join, str.split, dict.get) — DROP it rather than emit a name_match guess
		// to an arbitrary same-named internal method (application-centered — JARVIS 2023 /
		// PyCG ICSE 2021). #6: apply the SAME builtin sets here on the SINGLE-candidate path
		// that Strategy 2 applies on the multi-candidate path, so a qualified `get`/`items`/
		// `append` with exactly one global definition can no longer skip the broad guard and
		// launder into a name_match_qualified_unresolved edge. strongBuiltinMethodNames is a
		// superset-by-intent for single-candidate (os.path.join), builtinMethodNames is the
		// broad set (get/items/append/...). One predicate, both candidate-count paths.
		if qualifiedUnresolved && (strongBuiltinMethodNames[calleeName] || builtinMethodNames[calleeName]) {
			continue
		}
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
					method, conf, evidence := "verified_unique", 0.95, "name_unique"
					if qualifiedUnresolved {
						// Qualified receiver never resolved internally => stdlib/external/
						// unknown. DEMOTE to a speculative name_match so it never launders as
						// a confident fact. Confidence must sit below the SPECULATIVE threshold
						// so tierFor agrees with the demote (a sub-0.5 conf, not the 0.9
						// single-candidate name_match score that tierFor would re-CERTIFY).
						method = "name_match"
						conf = 0.2
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
						TrustTier:      tierFor(conf),
						EvidenceType:   evidence,
					})
				}
				continue
			}
		}

		// Strategy 1.93: Import-scoped type_flow
		// When caller imports ClassName from a specific file, scope class lookup to that file.
		// Fixes ambiguity when multiple classes share a name (e.g., "Client" in 5 files).
		// Supports both "." (Python/JS/TS/Go) and "::" (Rust) qualified separators.
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			dotIdx := strings.LastIndex(call.CalleeQualified, ".")
			sep := "."
			if dotIdx <= 0 {
				dotIdx = strings.LastIndex(call.CalleeQualified, "::")
				sep = "::"
			}
			if dotIdx > 0 {
				// #41: qualifier is everything before the separator for BOTH "." and "::"
				// (the prior `if sep == "::"` re-assign was a no-op — identical slice).
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+len(sep):]
				// #41: exclude "Self" too, matching the four sibling strategies (1.75/1.94/
				// 1.94a/1.95) — a Rust Self::method() that slipped past 1.75 (no caller
				// ParentID) must not mis-scope to an imported class literally named "Self".
				if qualifier != "self" && qualifier != "this" && qualifier != "Self" {
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
															TrustTier:      tierFor(0.95),
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

		// Strategy 1.94a (T1): Declared-type receiver resolution.
		// For a qualified call qualifier.method() (or qualifier::method()) where the
		// caller function DECLARED `qualifier` as a typed parameter/field, resolve the
		// declared type -> the class node of that type -> the method via CHA. The type
		// is a FACT (the source annotated it), so this resolves REAL internal method
		// calls whose receiver T2 could not infer (e.g. `command.run()` where the param
		// is `command: Command`). XTA (Tip&Palsberg OOPSLA00, +88% vs RTA): the declared
		// type set is the propagated fact. Generalized across statically-typed langs
		// (Go/Rust/Java/TS) + annotated Python — the `param` property is language-uniform.
		// Correct-or-quiet: emit ONLY when the class node exists AND CHA resolves the
		// method; otherwise fall through to the next strategy.
		if paramTypeIndex != nil && len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil &&
			call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			if paramTypes, ok := paramTypeIndex[callerID]; ok && len(paramTypes) > 0 {
				dotIdx194a := strings.LastIndex(call.CalleeQualified, ".")
				sep194a := 1
				if dotIdx194a <= 0 {
					dotIdx194a = strings.LastIndex(call.CalleeQualified, "::")
					sep194a = 2
				}
				if dotIdx194a > 0 {
					qualifier194a := call.CalleeQualified[:dotIdx194a]
					methodName194a := call.CalleeQualified[dotIdx194a+sep194a:]
					if qualifier194a != "self" && qualifier194a != "this" && qualifier194a != "Self" {
						if declaredType, ok := paramTypes[qualifier194a]; ok && declaredType != "" {
							className194a := stripTypeWrapper(declaredType)
							if className194a != "" {
								if classIDs, ok := nodeIDs[className194a]; ok {
									for _, classID := range classIDs {
										cm, hasMeta := nodeMeta[0][classID]
										if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
											continue
										}
										if targetID, found := lookupMethodWithInheritance(classID, methodName194a); found && targetID != callerID {
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
													TrustTier:      tierFor(0.9),
													EvidenceType:   "param_type",
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

		// Strategy 1.94: Single/few-implementor method resolution
		// For a qualified call obj.method() or Type::method(), if method is defined
		// as a method in exactly 1-3 classes across the codebase (regardless of what
		// obj/Type is), resolve with graduated confidence. This is especially useful
		// for Rust trait methods where `impl Trait for Struct` means a method like
		// `next()` might exist in only a few structs. Fires before generic type_flow
		// (1.95) because it uses global method uniqueness as a disambiguation signal.
		// Skips self/this/Self (handled by 1.75) and common method names (>3 classes).
		// Skips calls where the qualifier is a known class name (1.95 handles those).
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil &&
			call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			resolved194 := false
			methodName194 := calleeName
			dotIdx194 := strings.LastIndex(call.CalleeQualified, ".")
			if dotIdx194 <= 0 {
				dotIdx194 = strings.LastIndex(call.CalleeQualified, "::")
			}
			if dotIdx194 > 0 {
				qualifier194 := call.CalleeQualified[:dotIdx194]
				// Skip self/this/Self (handled by 1.75)
				isSelfLike := qualifier194 == "self" || qualifier194 == "this" || qualifier194 == "Self"
				// Skip if qualifier is a known class name (1.95 will handle it better)
				qualifierIsClass := false
				if !isSelfLike {
					if qIDs, ok := nodeIDs[qualifier194]; ok {
						for _, qid := range qIDs {
							if qm, ok := nodeMeta[0][qid]; ok &&
								(qm.Label == "Class" || qm.Label == "Struct" || qm.Label == "Interface") {
								qualifierIsClass = true
								break
							}
						}
					}
				}
				if !isSelfLike && !qualifierIsClass {
					if classes194, ok := methodClassCount[methodName194]; ok && len(classes194) >= 1 && len(classes194) <= 3 {
						numClasses := len(classes194)
						// #5: impl_method resolves purely on GLOBAL METHOD-NAME UNIQUENESS
						// with ZERO check that the receiver `obj` is actually that class
						// (the qualifier was explicitly excluded from being a known class
						// above). Name-uniqueness != receiver-proof (RTA-without-the-receiver),
						// so the 1-class case must NEVER be CERTIFIED — cap it at CANDIDATE
						// (conf 0.6). CERTIFIED stays reserved for stages that PROVE the
						// receiver type (1.75 self, 1.93/1.94a import/declared-type, 1.95/1.96
						// type_flow). Graduated: 1 class=0.6, 2=0.5, 3=0.4; tier via tierFor.
						conf194 := 0.4
						if numClasses == 1 {
							conf194 = 0.6
						} else if numClasses == 2 {
							conf194 = 0.5
						}
						// Pick the best target: prefer same-file class, then first
						var bestTarget194 int64
						for classID := range classes194 {
							if methods, ok := methodsByClass[classID]; ok {
								if targetID, ok := methods[methodName194]; ok && targetID != callerID {
									cm := nodeMeta[0][classID]
									if cm.File == call.File {
										bestTarget194 = targetID
										break // same-file is best
									}
									if bestTarget194 == 0 {
										bestTarget194 = targetID
									}
								}
							}
						}
						if bestTarget194 != 0 {
							key := edgeKey{callerID, bestTarget194, "CALLS"}
							if !seen[key] {
								seen[key] = true
								resolved = append(resolved, ResolvedCall{
									SourceNodeID:   callerID,
									TargetNodeID:   bestTarget194,
									SourceLine:     call.Line,
									SourceFile:     call.File,
									Method:         "impl_method",
									Confidence:     conf194,
									CandidateCount: numClasses,
									TrustTier:      tierFor(conf194),
									EvidenceType:   "single_implementor",
								})
							}
							resolved194 = true
						}
					}
				}
			}
			if resolved194 {
				continue
			}
		}

		// Strategy 1.95 (T2): Type-flow resolution for qualified calls
		// Supports both "." and "::" separators (Rust: Router::new, Python: obj.method)
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && call.CalleeQualified != "" {
			dotIdx195 := strings.LastIndex(call.CalleeQualified, ".")
			sep195 := 1
			if dotIdx195 <= 0 {
				dotIdx195 = strings.LastIndex(call.CalleeQualified, "::")
				sep195 = 2
			}
			if dotIdx195 > 0 {
				qualifier := call.CalleeQualified[:dotIdx195]
				methodName := call.CalleeQualified[dotIdx195+sep195:]
				if qualifier != "self" && qualifier != "this" && qualifier != "Self" {
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
											TrustTier:      tierFor(0.9),
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

		// Strategy 1.96 (T3): Assignment-flow resolution (PyCG ICSE 2021 + JARVIS 2023)
		// x = ClassName(); x.method() → resolve method via assignment tracking.
		// Scope-aware (caller function name) + self-field-preferring + return-type
		// chaining (x = factory(); x.method() bridges through factory's return type).
		if assignmentIndex != nil && len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
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
						// Caller's enclosing function name = the Scope recorded for its
						// assignments (extractAssignments keys Scope on the function name).
						callerScope := ""
						if cm, ok := nodeMeta[0][callerID]; ok {
							callerScope = cm.Name
						}
						if typeName, _, viaReturn, found := fileAssignments.ResolveQualifiedCall(qualifier, methodName, callerScope); found {
							// Determine the receiver CLASS name. Direct constructor: typeName
							// is the class. Return-type chain (x = factory()): typeName is the
							// callee — bridge through its declared return type.
							className := ""
							if viaReturn {
								if funcIDs, ok := nodeIDs[typeName]; ok {
									for _, funcID := range funcIDs {
										fm, hasMeta := nodeMeta[0][funcID]
										if !hasMeta || fm.ReturnType == "" {
											continue
										}
										if fm.Label == "Class" || fm.Label == "Struct" || fm.Label == "Interface" {
											continue
										}
										if rt := stripTypeWrapper(fm.ReturnType); rt != "" {
											className = rt
											break
										}
									}
								}
							} else {
								className = stripTypeWrapper(typeName)
							}
							if className != "" {
								// Look up the class in nodeIDs, then find the method via CHA.
								if classIDs, ok := nodeIDs[className]; ok {
									for _, classID := range classIDs {
										cm, hasMeta := nodeMeta[0][classID]
										if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
											continue
										}
										if targetID, found := lookupMethodWithInheritance(classID, methodName); found && targetID != callerID {
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
													TrustTier:      tierFor(0.9),
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
													TrustTier:      tierFor(0.85),
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
								TrustTier:      tierFor(0.85),
								EvidenceType:   "unique_method_class",
							})
						}
						continue
					}
				}
			}
		}

		// Strategy 2: Cross-file name match (fallback, 2+ candidates only)
		// T2 builtin-exclude: a qualified call obj.method() whose receiver never resolved
		// to an internal class above + method is a builtin name = a builtin call
		// (str.join, dict.get), not an internal edge. Drop it instead of emitting a
		// name_match guess (application-centered — JARVIS 2023 / PyCG ICSE 2021).
		if call.CalleeQualified != "" && call.CalleeQualified != calleeName && builtinMethodNames[calleeName] {
			continue
		}
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
						TrustTier:      tierFor(conf),
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

		// Rust crate/src/module probe: for paths like "axum::routing::future",
		// the fileMap has raw filesystem keys like "axum/src/routing/future.rs"
		// but none of the above probes construct this form. Split on the first
		// "::" to get the crate name, convert the rest to slash form, and
		// insert "/src/" between them.
		if len(colonParts) >= 2 {
			cratePart := colonParts[0]
			moduleParts := colonParts[1:]
			moduleSlash := strings.Join(moduleParts, "/")
			base := cratePart + "/src/" + moduleSlash

			// Try without extension (in case registerRustCrate registered it)
			if files, ok := fileMap[base]; ok {
				return files
			}
			// Try with .rs extension (raw filesystem path)
			if files, ok := fileMap[base+".rs"]; ok {
				return files
			}
			// Try mod.rs for directory modules (e.g., axum/src/routing/mod.rs)
			if files, ok := fileMap[base+"/mod.rs"]; ok {
				return files
			}
		}
	}

	return nil
}

// ExpandRustCrateImports substitutes `crate::X` in Rust import ModulePaths with
// the actual crate name that owns the importing file. `crate::` in Rust is a
// self-reference to the current crate; the fileMap uses the crate's real name
// (e.g., `axum_core::extract`). Without this, the import index lookup for
// `crate::extract` fails — the root cause of 1574→10 import resolution on axum.
// Runs ONCE before Resolve(), modifying allImports in place. Only touches Rust
// files with `crate::` module paths; external crate paths are untouched.
func ExpandRustCrateImports(
	allImports []parser.ImportRef,
	filePaths []string,
	fileLangs []string,
	root string,
) {
	fileToCrate := buildFileToCrateMap(root)
	if len(fileToCrate) == 0 {
		return
	}
	for i := range allImports {
		imp := &allImports[i]
		if !strings.HasPrefix(imp.ModulePath, "crate::") {
			continue
		}
		crateName := ""
		dir := filepath.ToSlash(filepath.Dir(imp.File))
		for dir != "" && dir != "." {
			if cn, ok := fileToCrate[dir]; ok {
				crateName = cn
				break
			}
			dir = filepath.ToSlash(filepath.Dir(dir))
		}
		if crateName == "" {
			if cn, ok := fileToCrate["."]; ok {
				crateName = cn
			}
		}
		if crateName == "" {
			continue
		}
		suffix := strings.TrimPrefix(imp.ModulePath, "crate::")
		imp.ModulePath = crateName + "::" + suffix
	}
}

func buildFileToCrateMap(root string) map[string]string {
	cargoPath := filepath.Join(root, "Cargo.toml")
	data, err := os.ReadFile(cargoPath)
	if err != nil {
		return nil
	}
	content := string(data)
	result := make(map[string]string)
	var memberDirs []string
	if idx := strings.Index(content, "members"); idx >= 0 {
		rest := content[idx:]
		if brk := strings.Index(rest, "["); brk >= 0 {
			rest = rest[brk:]
			if end := strings.Index(rest, "]"); end >= 0 {
				for _, item := range strings.Split(rest[1:end], ",") {
					dir := strings.Trim(strings.TrimSpace(item), `"' `)
					if dir == "" {
						continue
					}
					if strings.Contains(dir, "*") {
						// Expand glob patterns against the filesystem (e.g., "axum-*" → axum-core, axum-extra, axum-macros)
						matches, err := filepath.Glob(filepath.Join(root, dir))
						if err == nil {
							for _, m := range matches {
								rel, _ := filepath.Rel(root, m)
								if rel != "" {
									memberDirs = append(memberDirs, filepath.ToSlash(rel))
								}
							}
						}
					} else {
						memberDirs = append(memberDirs, dir)
					}
				}
			}
		}
	}
	for _, dir := range memberDirs {
		crateName := strings.ReplaceAll(filepath.Base(dir), "-", "_")
		if mdata, err := os.ReadFile(filepath.Join(root, dir, "Cargo.toml")); err == nil {
			if ni := strings.Index(string(mdata), "name"); ni >= 0 {
				nameRest := string(mdata)[ni:]
				if eq := strings.Index(nameRest, "="); eq >= 0 {
					val := strings.TrimSpace(nameRest[eq+1:])
					if nl := strings.IndexByte(val, '\n'); nl >= 0 {
						val = val[:nl]
					}
					if parsed := strings.Trim(strings.TrimSpace(val), `"' `); parsed != "" {
						crateName = strings.ReplaceAll(parsed, "-", "_")
					}
				}
			}
		}
		dirSlash := filepath.ToSlash(dir)
		result[dirSlash] = crateName
		result[dirSlash+"/src"] = crateName
	}
	if idx := strings.Index(content, "[package]"); idx >= 0 {
		rest := content[idx:]
		if ni := strings.Index(rest, "name"); ni >= 0 {
			nameRest := rest[ni:]
			if eq := strings.Index(nameRest, "="); eq >= 0 {
				val := strings.TrimSpace(nameRest[eq+1:])
				if nl := strings.IndexByte(val, '\n'); nl >= 0 {
					val = val[:nl]
				}
				if parsed := strings.Trim(strings.TrimSpace(val), `"' `); parsed != "" {
					cn := strings.ReplaceAll(parsed, "-", "_")
					result["."] = cn
					result["src"] = cn
				}
			}
		}
	}
	return result
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

// BuildRustModuleTree walks Rust mod declarations starting from crate roots
// (lib.rs / main.rs) to build a map[filePath]modulePath. It then registers
// those module paths in the fileMap so that import resolution can match
// `use crate::routing::Router` to the file that defines `Router`.
//
// Rust's module tree is NOT the filesystem tree — it's built from explicit
// `mod foo;` declarations. A file only participates in a crate's module tree
// if a chain of `mod` declarations connects it from the crate root.
//
// Example: lib.rs has `mod routing;` → routing/mod.rs has `mod future;`
// → routing/future.rs gets module path `crate_name::routing::future`.
func BuildRustModuleTree(
	fm map[string][]string,
	modDecls []parser.ModDecl,
	filePaths []string,
	fileLangs []string,
	root string,
) int {
	if len(modDecls) == 0 {
		return 0
	}

	// Build a set of indexed Rust files for quick lookup
	rustFiles := make(map[string]bool)
	for i, fp := range filePaths {
		if i < len(fileLangs) && fileLangs[i] == "rust" {
			rustFiles[filepath.ToSlash(fp)] = true
		}
	}

	// Group mod declarations by declaring file
	declsByFile := make(map[string][]parser.ModDecl)
	for _, md := range modDecls {
		key := filepath.ToSlash(md.File)
		declsByFile[key] = append(declsByFile[key], md)
	}

	// Get the crate map to determine crate names from directories
	fileToCrate := buildFileToCrateMap(root)

	// Find crate roots: lib.rs, main.rs in known crate source directories
	type crateRoot struct {
		file      string // e.g., "axum/src/lib.rs"
		crateName string // e.g., "axum"
	}
	var roots []crateRoot

	for fp := range rustFiles {
		base := filepath.Base(fp)
		if base != "lib.rs" && base != "main.rs" {
			continue
		}
		// Determine crate name from fileToCrate map
		dir := filepath.ToSlash(filepath.Dir(fp))
		crateName := ""
		for d := dir; d != "" && d != "."; d = filepath.ToSlash(filepath.Dir(d)) {
			if cn, ok := fileToCrate[d]; ok {
				crateName = cn
				break
			}
		}
		if crateName == "" {
			if cn, ok := fileToCrate["."]; ok {
				crateName = cn
			}
		}
		if crateName == "" {
			// Fallback: derive from parent dir name
			crateName = strings.ReplaceAll(filepath.Base(filepath.Dir(fp)), "-", "_")
		}
		roots = append(roots, crateRoot{file: fp, crateName: crateName})
	}

	if len(roots) == 0 {
		return 0
	}

	registered := 0

	// BFS from each crate root, following mod declarations
	for _, cr := range roots {
		type walkEntry struct {
			file       string // file path declaring the mod
			modulePath string // accumulated module path (e.g., "axum::routing")
		}

		queue := []walkEntry{{file: cr.file, modulePath: cr.crateName}}

		for len(queue) > 0 {
			entry := queue[0]
			queue = queue[1:]

			decls, ok := declsByFile[entry.file]
			if !ok {
				continue
			}

			dir := filepath.ToSlash(filepath.Dir(entry.file))

			for _, md := range decls {
				childModulePath := entry.modulePath + "::" + md.Name

				// Resolve mod foo; → either dir/foo.rs or dir/foo/mod.rs
				candidates := []string{
					dir + "/" + md.Name + ".rs",
					dir + "/" + md.Name + "/mod.rs",
				}

				for _, candidate := range candidates {
					if !rustFiles[candidate] {
						continue
					}

					// Register this file under the computed module path
					fm[childModulePath] = appendUnique(fm[childModulePath], candidate)
					registered++

					// Also register short suffixes for flexible matching
					parts := strings.Split(childModulePath, "::")
					for j := 1; j < len(parts); j++ {
						suffix := strings.Join(parts[j:], "::")
						fm[suffix] = appendUnique(fm[suffix], candidate)
					}

					// Continue BFS into this file's mod declarations
					queue = append(queue, walkEntry{
						file:       candidate,
						modulePath: childModulePath,
					})
					break // found the file, don't check the other candidate
				}
			}
		}
	}

	return registered
}

// appendUnique appends val to slice only if not already present.
func appendUnique(slice []string, val string) []string {
	for _, s := range slice {
		if s == val {
			return slice
		}
	}
	return append(slice, val)
}

// ChainReExports processes re-export declarations to register aliases in the
// fileMap. When a barrel file (e.g., index.ts, __init__.py, lib.rs) re-exports
// a symbol from another module, the importing file should be able to resolve
// that symbol through the barrel.
//
// For each re-export {ExportedName: "Foo", SourceModule: "./Foo", File: "components/index.ts"}:
//   1. Find the source file in fileMap via SourceModule
//   2. Register the barrel file's fileMap keys as also pointing to the source file
//
// This way `import { Foo } from './components'` → barrel index.ts → source Foo.ts.
func ChainReExports(
	fm map[string][]string,
	reExports []parser.ReExportRef,
	filePaths []string,
	fileLangs []string,
) int {
	if len(reExports) == 0 {
		return 0
	}

	// Build reverse map: file path → all fileMap keys that point to it
	fileToKeys := make(map[string][]string)
	for key, files := range fm {
		for _, fp := range files {
			fileToKeys[fp] = append(fileToKeys[fp], key)
		}
	}

	chained := 0

	for _, re := range reExports {
		if re.ExportedName == "*" {
			// Star re-exports are too broad to chain precisely — the resolver
			// handles these via wildcard import fallback.
			continue
		}

		// Resolve the source module to file(s)
		sourceFiles := resolveModulePath(re.SourceModule, fm)
		if len(sourceFiles) == 0 {
			// Try relative resolution from the re-exporting file's directory
			dir := filepath.ToSlash(filepath.Dir(re.File))
			rel := re.SourceModule
			if strings.HasPrefix(rel, "./") {
				rel = rel[2:]
			} else if strings.HasPrefix(rel, "../") {
				if didx := strings.LastIndex(dir, "/"); didx >= 0 {
					dir = dir[:didx]
				} else {
					dir = ""
				}
				rel = rel[3:]
			} else if !strings.HasPrefix(rel, ".") {
				// Absolute module path — resolveModulePath already tried
				continue
			}
			var base string
			if dir != "" {
				base = dir + "/" + rel
			} else {
				base = rel
			}
			// Try common extensions
			for _, ext := range []string{"", ".ts", ".tsx", ".js", ".jsx", ".py", ".rs",
				"/index.ts", "/index.js", "/index.tsx", "/mod.rs"} {
				if files, ok := fm[base+ext]; ok {
					sourceFiles = files
					break
				}
			}
		}

		if len(sourceFiles) == 0 {
			continue
		}

		// The re-exporting file's directory acts as the barrel.
		// Register the source file under all keys that currently point to
		// the barrel file, so imports through the barrel resolve to the source.
		barrelFile := filepath.ToSlash(re.File)
		barrelKeys := fileToKeys[barrelFile]

		for _, sourceFile := range sourceFiles {
			for _, key := range barrelKeys {
				fm[key] = appendUnique(fm[key], sourceFile)
				chained++
			}
		}
	}

	return chained
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

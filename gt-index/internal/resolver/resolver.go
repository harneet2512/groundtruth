// Package resolver resolves call references to definition nodes.
package resolver

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// TSConfig represents the relevant fields from tsconfig.json.
type TSConfig struct {
	BaseURL string
	Paths   map[string][]string
}

// ParseTSConfig reads tsconfig.json or jsconfig.json and extracts baseUrl and paths.
func ParseTSConfig(root string) *TSConfig {
	for _, name := range []string{"tsconfig.json", "jsconfig.json"} {
		data, err := os.ReadFile(filepath.Join(root, name))
		if err != nil {
			continue
		}
		var raw struct {
			CompilerOptions struct {
				BaseURL string              `json:"baseUrl"`
				Paths   map[string][]string `json:"paths"`
			} `json:"compilerOptions"`
		}
		if err := json.Unmarshal(data, &raw); err != nil {
			continue
		}
		if raw.CompilerOptions.BaseURL == "" && len(raw.CompilerOptions.Paths) == 0 {
			continue
		}
		return &TSConfig{
			BaseURL: raw.CompilerOptions.BaseURL,
			Paths:   raw.CompilerOptions.Paths,
		}
	}
	return nil
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

// RegisterJSPackagePaths adds in-repo package.json aliases to the file map.
// It only registers targets that resolve to already-indexed files, so external
// packages and stale export targets never become name-match escape hatches.
func RegisterJSPackagePaths(fm map[string][]string, root string) {
	if root == "" {
		return
	}
	type packageJSON struct {
		Name    string          `json:"name"`
		Main    string          `json:"main"`
		Module  string          `json:"module"`
		Types   string          `json:"types"`
		Typings string          `json:"typings"`
		Exports json.RawMessage `json:"exports"`
	}
	addAlias := func(alias, target string) {
		alias = strings.Trim(alias, "/")
		target = strings.TrimPrefix(strings.TrimSpace(target), "./")
		if alias == "" || target == "" || strings.HasPrefix(target, ".") {
			return
		}
		if files := resolveModulePath(target, fm); len(files) > 0 {
			fm[alias] = appendUniqueMany(fm[alias], files)
		}
	}
	collectExportTargets := func(raw json.RawMessage) map[string][]string {
		out := make(map[string][]string)
		var walk func(key string, v any)
		walk = func(key string, v any) {
			switch x := v.(type) {
			case string:
				out[key] = append(out[key], x)
			case []any:
				for _, item := range x {
					walk(key, item)
				}
			case map[string]any:
				for k, item := range x {
					nextKey := key
					if strings.HasPrefix(k, ".") {
						nextKey = k
					}
					walk(nextKey, item)
				}
			}
		}
		if len(raw) == 0 {
			return out
		}
		var v any
		if json.Unmarshal(raw, &v) != nil {
			return out
		}
		walk(".", v)
		return out
	}
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			base := d.Name()
			if base == ".git" || base == "node_modules" || base == "dist" || base == "build" {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() != "package.json" {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var pkg packageJSON
		if json.Unmarshal(data, &pkg) != nil || pkg.Name == "" {
			return nil
		}
		dir, err := filepath.Rel(root, filepath.Dir(path))
		if err != nil {
			return nil
		}
		dir = filepath.ToSlash(dir)
		if dir == "." {
			dir = ""
		}
		joinTarget := func(spec string) string {
			spec = strings.TrimPrefix(strings.TrimSpace(spec), "./")
			if spec == "" {
				return ""
			}
			if dir == "" {
				return spec
			}
			return dir + "/" + spec
		}
		for _, target := range []string{pkg.Module, pkg.Main, pkg.Types, pkg.Typings} {
			addAlias(pkg.Name, joinTarget(target))
		}
		for exportKey, targets := range collectExportTargets(pkg.Exports) {
			alias := pkg.Name
			if exportKey != "." && exportKey != "" {
				alias = pkg.Name + "/" + strings.TrimPrefix(exportKey, "./")
			}
			for _, target := range targets {
				addAlias(alias, joinTarget(target))
			}
		}
		return nil
	})
}

func appendUniqueMany(slice []string, vals []string) []string {
	for _, v := range vals {
		slice = appendUnique(slice, v)
	}
	return slice
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
	// #B8b: collect ALL additions first and apply them to fm exactly ONCE at the
	// end. The previous code applied `additions` after the base loop AND re-applied
	// the SAME map again inside the versioned-module branch — every module-prefixed
	// key got its files appended TWICE, and the versioned loop (iterating the
	// already-mutated fm) minted garbage keys like
	// "github.com/org/repo/github.com/org/repo/v2/pkg".
	skip := func(key string) bool {
		// Only process slash-separated directory paths (Go package dirs).
		// Skip: Rust (::), PHP (\), Python dotted (no slash), source files (.go etc)
		if strings.Contains(key, "::") || strings.Contains(key, `\`) {
			return true
		}
		if ext := filepath.Ext(key); ext != "" {
			return true
		}
		if strings.HasPrefix(key, goModulePath) {
			return true
		}
		// Skip Python dotted imports (e.g. "os.path") but NOT Go dirs with slashes
		if strings.Contains(key, ".") && !strings.Contains(key, "/") {
			return true
		}
		return false
	}
	additions := make(map[string][]string)
	for key, files := range fm {
		if skip(key) {
			continue
		}
		additions[goModulePath+"/"+key] = files
	}
	// Also handle versioned modules: github.com/org/repo/v2/pkg → strip v2/ and try
	// Import "github.com/org/repo/v2/pkg" should match dir "pkg/"
	if parts := strings.Split(goModulePath, "/"); len(parts) > 0 {
		last := parts[len(parts)-1]
		if len(last) >= 2 && last[0] == 'v' && last[1] >= '0' && last[1] <= '9' {
			// Versioned module: github.com/org/repo/v2
			// Import "github.com/org/repo/v2/ast" → strip module prefix → "ast" → lookup
			// Already handled by suffix stripping in resolveModulePath.
			// But also register the unversioned-prefixed path.
			unversioned := strings.Join(parts[:len(parts)-1], "/")
			for key, files := range fm {
				if skip(key) || strings.HasPrefix(key, unversioned) {
					continue
				}
				vKey := unversioned + "/" + key
				if _, exists := additions[vKey]; !exists {
					additions[vKey] = files
				}
			}
		}
	}
	for k, v := range additions {
		fm[k] = append(fm[k], v...)
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
			// Go receiver var (the analogue of self/this) — derived structurally from the
			// method signature, only for Go method/function nodes. Empty everywhere else.
			recvName := ""
			if n.Language == "go" {
				recvName = parser.GoReceiverName(n.Signature)
			}
			meta[nodeDBIDs[i]] = NodeMeta{
				Label:        n.Label,
				File:         n.FilePath,
				ParentID:     n.ParentID,
				Name:         n.Name,
				ReturnType:   n.ReturnType,
				ReceiverName: recvName,
				StartLine:    n.StartLine,
			}
		}
	}
	return meta
}

// ResolvedCall is a call reference that has been resolved to a target node.
type ResolvedCall struct {
	CallsiteOrdinal  int
	Callee           string
	CalleeQualified  string
	SourceNodeID     int64
	TargetNodeID     int64
	SourceLine       int
	SourceFile       string
	Method           string  // "same_file", "import", "verified_unique", "type_flow", "name_match"
	Confidence       float64 // 0.0–1.0
	CandidateCount   int     // number of resolution candidates (1=unambiguous)
	TrustTier        string  // CERTIFIED, CANDIDATE, SPECULATIVE
	EvidenceType     string  // ast_call, ast_import, name_match
	CandidateNodeIDs []int64 // complete resolver-owned viable identities
	ReceiverType     string  // source-supported receiver type when a strategy proves it
	ReceiverOrigin   string  // import, field_annotation, param_annotation, assignment, return_type, or call_syntax
}

type DispatchState string

const (
	DispatchZero               DispatchState = "zero"
	DispatchUnique             DispatchState = "unique"
	DispatchAmbiguous          DispatchState = "ambiguous"
	DispatchCandidateOnly      DispatchState = "candidate_only"
	DispatchDynamic            DispatchState = "dynamic"
	DispatchExternalUnresolved DispatchState = "external_unresolved"
	DispatchParserIncomplete   DispatchState = "parser_incomplete"
)

const ResolutionAuthoritySchema = "gt-index.resolution-authority.v1"

var receiverOriginVocabulary = map[string]struct{}{
	"": {}, "import": {}, "field_annotation": {}, "param_annotation": {},
	"assignment": {}, "return_type": {}, "call_syntax": {}, "type_qualifier": {},
}

// sourceSupportedResolution is deliberately independent of Confidence.  It is
// the closed producer policy for evidence that can authorize a unique target.
// Heuristic/name-uniqueness mechanisms remain useful ranking candidates, but
// can never become source-supported merely by crossing a numeric threshold.
func sourceSupportedResolution(rc ResolvedCall) bool {
	allowed := map[string]map[string]struct{}{
		"same_file":   {"ast_call": {}, "inheritance_chain": {}},
		"inherited":   {"inheritance_chain": {}},
		"import":      {"ast_import": {}},
		"import_type": {"import_scoped_type": {}},
		"type_flow": {
			"field_type": {}, "param_type": {}, "type_qualified": {}, "assignment_tracked": {},
		},
		"return_type": {"return_type_flow": {}},
	}
	evidence, ok := allowed[rc.Method]
	if !ok {
		return false
	}
	_, ok = evidence[rc.EvidenceType]
	return ok && len(rc.CandidateNodeIDs) == 1
}

type ResolutionCallsite struct {
	CallsiteOrdinal       int
	SourceNodeID          int64
	SourceLine            int
	SourceFile            string
	Callee                string
	CalleeQualified       string
	DispatchState         DispatchState
	CandidateNodeIDs      []int64
	SelectedTargetNodeID  *int64
	Mechanism             string
	ReceiverType          string
	ReceiverOrigin        string
	ParserComplete        bool
	VerificationStatus    string
	EvidenceType          string
	ImportChain           []string
	CandidateImportChains map[int64][]string
	ASTPath               string
	ByteStart             uint64
	ByteEnd               uint64
	ColumnStart           uint32
	ArgumentArity         *uint16
	DispatchForm          string
	PassExecutions        []ResolutionPassExecution
}

// ResolutionPassExecution records a pass outcome emitted by the resolver
// control flow. It is not reconstructed from the final resolution mechanism.
type ResolutionPassExecution struct {
	PassKind string
	Status   string
	Reason   string
}

var resolutionPassOrder = []string{
	"lexical_binding", "import_binding", "declared_type", "implementation_set",
	"return_type", "global_name", "dynamic_framework",
}

type resolutionPassTracker struct {
	active  string
	entries map[string]ResolutionPassExecution
}

func newResolutionPassTracker(call parser.CallRef) *resolutionPassTracker {
	tracker := &resolutionPassTracker{entries: make(map[string]ResolutionPassExecution, len(resolutionPassOrder))}
	for _, pass := range resolutionPassOrder {
		entry := ResolutionPassExecution{PassKind: pass, Status: "not_run", Reason: "no_execution_event"}
		switch {
		case pass == "dynamic_framework" && call.DynamicDispatch:
			entry.Status, entry.Reason = "unavailable", "static_target_not_proven"
		case pass == "dynamic_framework":
			entry.Status, entry.Reason = "not_applicable", "no_dynamic_framework_construct"
		case call.ParserIncomplete:
			entry.Reason = "parser_incomplete"
		case call.DynamicDispatch:
			entry.Reason = "dynamic_dispatch_abstention"
		}
		tracker.entries[pass] = entry
	}
	return tracker
}

func (t *resolutionPassTracker) begin(pass string) {
	t.active = pass
	entry := t.entries[pass]
	if entry.Status == "not_run" {
		entry.Status = "completed_no_match"
		entry.Reason = ""
		t.entries[pass] = entry
	}
}

func (t *resolutionPassTracker) match() {
	if t.active == "" {
		return
	}
	entry := t.entries[t.active]
	entry.Status = "completed_match"
	entry.Reason = ""
	t.entries[t.active] = entry
}

func (t *resolutionPassTracker) snapshot() []ResolutionPassExecution {
	coverage := make([]ResolutionPassExecution, 0, len(resolutionPassOrder))
	for _, pass := range resolutionPassOrder {
		coverage = append(coverage, t.entries[pass])
	}
	return coverage
}

func (c ResolutionCallsite) Validate() error {
	if _, ok := receiverOriginVocabulary[c.ReceiverOrigin]; !ok {
		return fmt.Errorf("receiver origin %q is not in %s", c.ReceiverOrigin, ResolutionAuthoritySchema)
	}
	seen := make(map[int64]struct{}, len(c.CandidateNodeIDs))
	for _, id := range c.CandidateNodeIDs {
		if id == 0 {
			return fmt.Errorf("candidate target ID must be nonzero")
		}
		if _, ok := seen[id]; ok {
			return fmt.Errorf("candidate targets must be unique")
		}
		seen[id] = struct{}{}
	}
	if c.SelectedTargetNodeID != nil {
		if _, ok := seen[*c.SelectedTargetNodeID]; !ok {
			return fmt.Errorf("selected target must be a retained candidate")
		}
	}
	switch c.DispatchState {
	case DispatchUnique:
		if len(c.CandidateNodeIDs) != 1 || c.SelectedTargetNodeID == nil {
			return fmt.Errorf("unique callsite requires one selected candidate")
		}
	case DispatchAmbiguous:
		if len(c.CandidateNodeIDs) < 2 || c.SelectedTargetNodeID != nil {
			return fmt.Errorf("ambiguous callsite requires multiple candidates and no selection")
		}
	case DispatchCandidateOnly:
		if len(c.CandidateNodeIDs) < 1 || c.SelectedTargetNodeID != nil {
			return fmt.Errorf("candidate-only callsite requires retained candidates and no selection")
		}
	case DispatchDynamic:
		if len(c.CandidateNodeIDs) != 0 || c.SelectedTargetNodeID != nil {
			return fmt.Errorf("dynamic callsite must explicitly abstain with zero candidates")
		}
	case DispatchZero, DispatchExternalUnresolved, DispatchParserIncomplete:
		if len(c.CandidateNodeIDs) != 0 || c.SelectedTargetNodeID != nil {
			return fmt.Errorf("unresolved callsite cannot retain target authority")
		}
	default:
		return fmt.Errorf("unknown dispatch state %q", c.DispatchState)
	}
	return nil
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

// stripCallArgs reduces a direct-call qualifier `name(args)` to its bare callee `name`,
// so a factory chain `make_user(a, b).save()` (whose qualifier the parser records WITH the
// args) matches the factory's function node in the return-type bridge. It strips ONLY a
// single balanced trailing `(...)` that closes at the final character — a qualifier with a
// trailing `.field`, an unbalanced/partial paren, or no paren is returned unchanged (so a
// plain variable receiver `obj.method()` is untouched). Nested parens inside the args are
// balanced. Correct-or-quiet: callers still gate the result on a real function-node lookup.
func stripCallArgs(q string) string {
	q = strings.TrimSpace(q)
	if !strings.HasSuffix(q, ")") {
		return q
	}
	open := strings.IndexByte(q, '(')
	if open <= 0 {
		return q
	}
	depth := 0
	for i := open; i < len(q); i++ {
		switch q[i] {
		case '(':
			depth++
		case ')':
			depth--
			if depth == 0 {
				// The first balanced '(' must close at the LAST char for this to be a
				// bare `name(args)` call (no trailing `.x` / index after the close).
				if i != len(q)-1 {
					return q
				}
				name := strings.TrimSpace(q[:open])
				// The head must be a clean identifier (no remaining dots / operators) —
				// `a.b()` is a method chain, not a bare factory call; leave it for 1.96.
				if name != "" && identLikeRe.MatchString(name) {
					return name
				}
				return q
			}
		}
	}
	return q
}

// identLikeRe matches a clean bare identifier (no dots, parens, or operators) — the head
// of a direct factory call after its args are stripped.
var identLikeRe = regexp.MustCompile(`^[A-Za-z_]\w*$`)

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

// sameDirFile reports whether two files live in the same directory (same package,
// the strongest free provenance signal). Slash-normalized so "/" and "\\" agree.
func sameDirFile(a, b string) bool {
	return filepath.ToSlash(filepath.Dir(a)) == filepath.ToSlash(filepath.Dir(b))
}

// callerImportsFile reports whether callerFile has ANY import (by any imported
// name) that resolves to targetFile. This is the module-level provenance the
// verified_unique gate needs: Strategy 1.5 already ran a NAME-scoped import check
// (callee-name → file) and failed, but a globally-unique callee may still be
// genuinely reachable because the caller imports the target's MODULE (a re-export,
// a package import, or an aliased name). Slash-normalized comparison.
func callerImportsFile(callerFile, targetFile string, importIndex map[string]map[string][]string) bool {
	fileImports, ok := importIndex[callerFile]
	if !ok {
		return false
	}
	tgt := filepath.ToSlash(targetFile)
	for _, targetFiles := range fileImports {
		for _, tf := range targetFiles {
			if filepath.ToSlash(tf) == tgt {
				return true
			}
		}
	}
	return false
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
		// P2-9: cc==2 was tied at 0.6 with the cc<=1 case — two same-named candidates
		// is strictly MORE ambiguous than one, so it must score lower. cc==2 → 0.5
		// (still CANDIDATE, but tier-separated from the unique 0.6) so the ambiguity
		// gradient is monotone: 1→0.6, 2→0.5, 3-5→0.4, >5→0.2.
		if candidateCount <= 1 {
			return 0.6
		} else if candidateCount == 2 {
			return 0.5
		} else if candidateCount <= 5 {
			return 0.4
		}
		return 0.2
	case "name_match_alias":
		if candidateCount <= 1 {
			return 0.5
		} else if candidateCount == 2 {
			return 0.35
		} else if candidateCount <= 5 {
			return 0.25
		}
		return 0.15
	}
	return 0.3
}

func canonicalNameKey(name string) string {
	tokens := splitNameTokens(name)
	if len(tokens) == 0 {
		return ""
	}
	total := 0
	for _, tok := range tokens {
		total += len(tok)
	}
	if total < 4 {
		return ""
	}
	return strings.Join(tokens, "")
}

func splitNameTokens(name string) []string {
	var tokens []string
	var cur []rune
	flush := func() {
		if len(cur) == 0 {
			return
		}
		tokens = append(tokens, strings.ToLower(string(cur)))
		cur = cur[:0]
	}
	rs := []rune(strings.TrimSpace(name))
	for i, r := range rs {
		isLetter := (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z')
		isDigit := r >= '0' && r <= '9'
		if !isLetter && !isDigit {
			flush()
			continue
		}
		if len(cur) > 0 && r >= 'A' && r <= 'Z' {
			prev := rs[i-1]
			nextLower := i+1 < len(rs) && rs[i+1] >= 'a' && rs[i+1] <= 'z'
			prevLowerOrDigit := (prev >= 'a' && prev <= 'z') || (prev >= '0' && prev <= '9')
			prevUpper := prev >= 'A' && prev <= 'Z'
			if prevLowerOrDigit || (prevUpper && nextLower) {
				flush()
			}
		}
		cur = append(cur, r)
	}
	flush()
	return tokens
}

func buildNameAliasIndex(nodeIDs map[string][]int64) map[string][]int64 {
	alias := make(map[string][]int64)
	seen := make(map[string]map[int64]bool)
	for name, ids := range nodeIDs {
		key := canonicalNameKey(name)
		if key == "" {
			continue
		}
		if seen[key] == nil {
			seen[key] = make(map[int64]bool)
		}
		for _, id := range ids {
			if id <= 0 || seen[key][id] {
				continue
			}
			seen[key][id] = true
			alias[key] = append(alias[key], id)
		}
	}
	for key := range alias {
		sort.Slice(alias[key], func(i, j int) bool { return alias[key][i] < alias[key][j] })
	}
	return alias
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

func pickBestNameMatchTarget(candidates []int64, callerID int64, callerFile string, meta map[int64]NodeMeta) int64 {
	// CONTENT-deterministic candidate order (file_path, start_line, id) BEFORE the picks
	// below. Node IDs are assigned non-deterministically by the parallel parse, so an
	// ambiguous name_match (e.g. `query()`, defined in many classes) resolved to a
	// different LOGICAL target run-to-run — the first-match (callable/same-dir) branches
	// AND the final `tid < best` tiebreak both depended on node-id iteration order. Ground
	// truth: textual `promote_dataflow_callee` flipped run-to-run while the CALLS count
	// stayed constant (the canary). file_path+start_line is insertion-order-invariant, so
	// the same logical target wins every run. (id is a degenerate-case last resort only.)
	sort.Slice(candidates, func(a, b int) bool {
		ma, mb := meta[candidates[a]], meta[candidates[b]]
		if ma.File != mb.File {
			return ma.File < mb.File
		}
		if ma.StartLine != mb.StartLine {
			return ma.StartLine < mb.StartLine
		}
		return candidates[a] < candidates[b]
	})
	best := int64(0)
	bestFile := ""
	bestCallable := false
	bestSameDir := false
	callerDir := filepath.ToSlash(filepath.Dir(callerFile))
	for _, tid := range candidates {
		if tid == callerID {
			continue
		}
		tf := ""
		callable := false
		if meta != nil {
			if m, ok := meta[tid]; ok {
				tf = filepath.ToSlash(m.File)
				callable = m.Label == "Function" || m.Label == "Method"
			}
		}
		sameDir := tf != "" && filepath.ToSlash(filepath.Dir(tf)) == callerDir
		switch {
		case best == 0:
			best, bestFile, bestCallable, bestSameDir = tid, tf, callable, sameDir
		case callable && !bestCallable:
			best, bestFile, bestCallable, bestSameDir = tid, tf, true, sameDir
		case callable == bestCallable && sameDir && !bestSameDir:
			best, bestFile, bestSameDir = tid, tf, true
		case callable == bestCallable && sameDir == bestSameDir:
			if tf < bestFile || (tf == bestFile && tid < best) {
				best, bestFile = tid, tf
			}
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
	// ReceiverName is the Go method's receiver VARIABLE name (`func (r *T) M()` → "r"),
	// derived structurally from the signature. Empty for non-Go nodes, plain functions,
	// and anonymous receivers. Used by rung 2b to accept `<recv>.<field>.method()` as the
	// Go analogue of self./this. — abstains (stays empty) when the receiver is unnamed.
	ReceiverName string
	// StartLine is the node's definition line. Used ONLY as a CONTENT-based, insertion-
	// order-invariant tiebreak in name_match candidate selection (node IDs are assigned
	// non-deterministically by the parallel parse — see pickBestNameMatchTarget).
	StartLine int
}

// Resolve takes all call refs and all defined nodes, and resolves calls to definitions.
// Resolution strategies (in priority order):
//  1. Same-file exact name match → "same_file" (conf=1.0)
//     1.25  Import-verified cross-file → "import" (conf=1.0)
//     1.75  self/this/Self method via caller's class → "same_file" (conf=1.0)
//     1.9   Verified-unique: globally unique name → "verified_unique" (conf=0.95)
//     1.93  Import-scoped type_flow: import narrows class → "import_type" (conf=0.95)
//     2b    Declared-FIELD-type receiver: self.<field>.m() via the field's declared
//     annotation → "type_flow" (conf=0.9, evidence "field_type"). XTA over the
//     class_field-type set (Tip & Palsberg OOPSLA 2000), CHA-resolved over the
//     hierarchy. Fills the gap 1.94a/1.96/1.95 miss: an annotation-only typed
//     field (injected/inherited/declared, never locally assigned).
//     1.94  Single/few-implementor: method unique to 1-3 classes → "impl_method" (conf=0.4-0.85)
//     1.95  Type-flow: qualified call on known class → "type_flow" (conf=0.9)
//     1.96  Assignment-flow: x = ClassName(); x.method() → "type_flow" (conf=0.9)
//     PyCG ICSE 2021: 99% precision from assignment tracking rules.
//     1.97  Return-type bridging: get_user().save() via return type → "return_type" (conf=0.85)
//     1.98  Unique-method-class: method name unique to one class → "unique_method" (conf=0.85)
//  2. Cross-file name match → "name_match" (conf=0.2-0.6, fallback)
//
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

// returnShapeIndex: function node DB ID → the internal class-like type NAME the function
// constructs and returns, mined from the `return_shape` property ONLY when the body returns
// a BARE CONSTRUCTOR (`ClassName(...)` for Python/JS/TS, `&Struct{...}` / `Struct{...}` for
// Go/Rust composite literals). It is the FALLBACK declared-return-type for the return-type
// bridges (Strategy 1.96 viaReturn + 1.97) when the parser captured no `return_type`
// annotation (`x := factory(); x.M()` / `factory().M()` where factory has an inferred-but-
// undeclared return). A constructor return is a FACT (the function's runtime type IS that
// class), so it is conf-0.9 type_flow when consumed — UNLIKE data_flow, whose value is a
// forward-slice provenance string (not a var->type binding) and is NEVER used for receivers.
// CORRECT-OR-QUIET: populated ONLY for a clean single-constructor return whose name resolves
// to an internal class-like node; `collection|`, `tuple|`, `none`, multi-return, and any
// non-constructor `value|` expr (a var, a binary expr, a non-class call) record NOTHING.
var returnShapeIndex map[int64]string

// fieldTypeIndex: CLASS node DB ID → {field name → declared type name}. Set before
// Resolve() for Strategy 2b (declared-FIELD-type receiver resolution). Populated from
// the `class_field` properties the parser already extracts (`name: Type` annotations),
// so a typed `self.<field>.method()` whose field is annotation-only (NOT locally
// assigned — injected via __init__ param, declared on a base class, or annotation-only)
// resolves to the field's class without re-parsing. This is the fact promote.go reads
// then DISCARDS at the colon (promote.go:367-369 keeps only the field NAME). Keyed by
// the OWNING class node (class_field props attach to the class node) — NOT the caller —
// because the same typed field is visible to every method of the class. XTA over the
// declared field-type set (Tip & Palsberg, OOPSLA 2000). Generalized across
// statically-typed langs (Go/Rust/TS struct/class fields + annotated Python attrs) —
// `class_field` is language-uniform (parser.go:3817-3818).
var fieldTypeIndex map[int64]map[string]string

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

// SetFieldTypeIndex sets the class→field→type map for Strategy 2b.
func SetFieldTypeIndex(idx map[int64]map[string]string) {
	fieldTypeIndex = idx
}

// SetReturnShapeIndex sets the funcNodeID→constructed-class-name map used as the
// return-type FALLBACK in the Strategy 1.96 (viaReturn) + 1.97 return-type bridges.
func SetReturnShapeIndex(idx map[int64]string) {
	returnShapeIndex = idx
}

// returnShapeCtorRe matches a BARE CONSTRUCTOR return expression and captures the
// constructed type NAME, across the two language idioms:
//
//	Python/JS/TS  : ClassName(args)            -> ClassName
//	Go/Rust       : &Struct{fields} / Struct{} -> Struct   (composite literal)
//
// It is anchored (^) and requires the whole expr to BE the constructor (the closing
// `)`/`}` is asserted by the caller), so a method-chain (`Foo().bar()`), an arithmetic
// expr, or a non-constructor call cannot match. Generics/qualifiers are handled by the
// caller (drop-dotted, strip `[...]`), keeping this regex a clean leading-name extractor.
var returnShapeCtorRe = regexp.MustCompile(`^&?([A-Za-z_][A-Za-z0-9_]*)\s*[\({]`)

// ctorIsWholeExpr reports whether the constructor opened by the FIRST `open` bracket in
// expr closes (balanced) exactly at the final character, i.e. the constructor IS the whole
// return expression with nothing trailing it. This rejects method chains / field access on
// a constructor (`User(a).clone()`, `&S{}.Field`) which end in the right bracket but whose
// constructor close is INTERIOR. open is '(' or '{' (its match ')' or '}').
func ctorIsWholeExpr(expr string, open byte) bool {
	var close byte
	if open == '(' {
		close = ')'
	} else {
		close = '}'
	}
	depth := 0
	started := false
	for i := 0; i < len(expr); i++ {
		switch expr[i] {
		case open:
			depth++
			started = true
		case close:
			depth--
			if depth == 0 && started {
				// Constructor balanced here — it is the whole expr ONLY if this is the
				// last non-space character.
				rest := strings.TrimSpace(expr[i+1:])
				return rest == ""
			}
		}
	}
	return false
}

// BuildReturnShapeIndex builds funcNodeID → constructed-class-NAME from the `return_shape`
// properties the parser already extracts (no re-parse), keyed by the function node DB id.
// It mirrors BuildParamTypeIndex's plumbing (props' NodeIdx is global, parallel to
// nodeDBIDs). It records a type ONLY for a bare-constructor `value|<Ctor>(...)` /
// `value|&<Struct>{...}` shape — abstaining (recording nothing) on `none`, `collection|`,
// `tuple|`, dotted/qualified constructors, and non-constructor `value|` exprs — so the
// receiver type it later supplies is a CONSTRUCTOR FACT, never a guess (correct-or-quiet).
// classNames is the set of internal class-like node names; a constructor not naming an
// internal class is dropped (a stdlib/builtin constructor has no internal node to bridge to).
// When a function has MULTIPLE distinct constructor returns (rare: returns A in one branch,
// B in another), the type is AMBIGUOUS and the function is excluded — never pick one.
func BuildReturnShapeIndex(props []parser.PropertyRef, nodeDBIDs []int64, classNames map[string]bool) map[int64]string {
	type acc struct {
		typ   string
		ambig bool
	}
	tmp := make(map[int64]*acc)
	for _, p := range props {
		if p.Kind != "return_shape" || p.NodeIdx < 0 || p.NodeIdx >= len(nodeDBIDs) {
			continue
		}
		dbid := nodeDBIDs[p.NodeIdx]
		if dbid <= 0 {
			continue
		}
		val := strings.TrimSpace(p.Value)
		// Only the `value|<expr>` shape can be a constructor. `none`, `collection|...`
		// (`[...]`/`{...}` literal — NOT a struct constructor), `tuple|...` abstain.
		if !strings.HasPrefix(val, "value|") {
			continue
		}
		expr := strings.TrimSpace(val[len("value|"):])
		// A bare constructor's expr must END in the matching bracket of its leading form
		// (`)` for Name(...), `}` for &Struct{...}/Struct{...}). A trailing `.x`, operator,
		// or index means it is NOT a clean single constructor -> abstain.
		isCall := strings.HasSuffix(expr, ")")
		isLit := strings.HasSuffix(expr, "}")
		if !isCall && !isLit {
			continue
		}
		m := returnShapeCtorRe.FindStringSubmatch(expr)
		if m == nil {
			continue
		}
		// The opening bracket the regex matched must agree with the closing bracket:
		// `Name(` pairs with `)`, `&Struct{`/`Struct{` pairs with `}`. This rejects
		// `Name(...)` that actually ends in `}` (a slice/map of structs) and vice-versa.
		openBrace := strings.ContainsAny(m[0], "{")
		if openBrace != isLit {
			continue
		}
		// The constructor must be the WHOLE expression: the bracket the leading name
		// opens must close at the FINAL char (nothing trailing). A method chain
		// `User(a).clone()` ends in `)` and matches the leading-name regex, but the
		// constructor's own `)` is interior — `.clone()` trails it. We require the open
		// bracket (the last char of m[0]) to balance to depth 0 exactly at len(expr)-1,
		// so any trailing `.x` / index / operator abstains (correct-or-quiet).
		if !ctorIsWholeExpr(expr, m[0][len(m[0])-1]) {
			continue
		}
		name := m[1]
		if name == "" || !classNames[name] {
			continue // not an internal class-like type -> no bridge target, abstain
		}
		a := tmp[dbid]
		if a == nil {
			tmp[dbid] = &acc{typ: name}
		} else if a.typ != name {
			a.ambig = true // two different constructor returns -> ambiguous, drop
		}
	}
	idx := make(map[int64]string)
	for dbid, a := range tmp {
		if a.ambig || a.typ == "" {
			continue
		}
		idx[dbid] = a.typ
	}
	return idx
}

// BuildFieldTypeIndex builds CLASS-node-DB-ID → {fieldName → typeName} from the
// `class_field` properties the parser extracts. It is the exact mirror of
// BuildParamTypeIndex with two changes: (a) it keys on kind=="class_field" (not
// "param"); (b) the value is keyed by the OWNING CLASS node, because class_field
// props attach to the class node (parser extractClassFields nodeIdx=class).
//
// CORRECT-OR-QUIET PRECISION RULE (load-bearing): a type is extracted ONLY from the
// `name: Type` ANNOTATION shape. The parser emits class_field for BOTH `name: Type`
// (annotation — has a declared type) AND `name = expr` (assignment — NO declared
// type, e.g. `x = CharField(100)`). A type is recorded ONLY when a colon precedes
// any `=` (annotation), so an assignment-shape field produces NO entry and the call
// falls to rung 1.96 (assignment-graph, with its own scope-aware proof) or the tail.
// We NEVER invent a type from a constructor name — that is 1.96's job, not 2b's.
// tsFieldModifiers is the set of TS/JS class-field access/declaration modifiers that
// precede the field name. A LEADING run of them is stripped before the colon split so a
// `private readonly client: HttpClient` field indexes under "client", not "private client".
var tsFieldModifiers = map[string]bool{
	"private": true, "public": true, "protected": true, "readonly": true,
	"static": true, "declare": true, "abstract": true, "override": true,
}

// stripTSFieldModifiers removes a leading run of TS access-modifier keywords from a
// field-declaration string. It consumes ONLY a leading run of known modifiers; the first
// non-modifier token (the field name) ends the strip. Returns the input unchanged when no
// modifier leads (Python/Rust/Go fields). Correct-or-quiet: it never reorders or invents
// tokens, it only drops a recognized leading keyword.
func stripTSFieldModifiers(val string) string {
	for {
		sp := strings.IndexAny(val, " \t")
		if sp <= 0 {
			return val
		}
		if !tsFieldModifiers[val[:sp]] {
			return val
		}
		val = strings.TrimSpace(val[sp+1:])
	}
}

// goStructField parses a Go struct-field declaration string into (name, type) and a
// keep/abstain flag. It is the Go analogue of the `name: Type` colon split — a Go field
// is `Name [*][]Type [`tag`]` (whitespace-separated), so the FIRST token is the field
// name and the SECOND is the (wrapper-stripped) type. CORRECT-OR-QUIET (every guard
// returns ok=false so the field falls through unindexed → the call demotes to name_match
// rather than mis-resolving):
//   - exactly-≥2 tokens; a single token is an EMBEDDED field (type only, no name) → abstain
//   - a comma in the name token (`A, B string` multi-name group) → ambiguous → abstain
//   - the name must be a bare identifier (letters/digits/_, not starting with a digit) →
//     guards tags, comments, operators, and any malformed slice
//   - the type token, after stripTypeWrapper (`*T`/`[]T`/`&T` → T), must be non-empty
//
// It never invents a type and never widens beyond the declared one — pure propagation of
// the source fact the parser wrote.
func goStructField(val string) (name, typ string, ok bool) {
	fields := strings.Fields(val)
	if len(fields) < 2 {
		return "", "", false // embedded field (type only) or empty — no field name
	}
	name = fields[0]
	if name == "" || strings.ContainsAny(name, ",.[]()*&={}:`\"") {
		return "", "", false // multi-name group, tag/comment noise, or not a bare ident
	}
	if !isIdent(name) {
		return "", "", false
	}
	// Normalize the Go type token: strip leading slice/array/pointer markers
	// (`[]Type`, `[N]Type`, `*Type`, `&Type`, and combinations like `[]*Type`) down to the
	// element type, then run stripTypeWrapper for generic `Name[T]` / `Optional[T]` forms.
	// stripTypeWrapper alone does NOT remove a LEADING `[]` (its `[` index must be >0), so
	// the slice prefix is stripped here first.
	typ = stripGoTypePrefix(fields[1])
	typ = stripTypeWrapper(typ)
	if typ == "" || strings.ContainsAny(typ, "{}=`\"[]*&") {
		return "", "", false // unresolved wrapper / tag / map / func-type noise → abstain
	}
	if !isIdent(stripPkgQualifier(typ)) {
		return "", "", false // `map[..]..`, `func(..)`, anonymous struct, etc. → abstain
	}
	return name, typ, true
}

// stripGoTypePrefix removes a leading run of Go slice/array/pointer markers from a type
// string: `[]Type` / `[N]Type` / `*Type` / `&Type` (and combinations) → `Type`. It stops
// at the first non-marker char (the element type). Generic `Name[T]` forms are left for
// stripTypeWrapper (their `[` is not leading). Correct-or-quiet: pure prefix removal.
func stripGoTypePrefix(t string) string {
	for {
		switch {
		case strings.HasPrefix(t, "*"):
			t = t[1:]
		case strings.HasPrefix(t, "&"):
			t = t[1:]
		case strings.HasPrefix(t, "[]"):
			t = t[2:]
		case strings.HasPrefix(t, "[") && strings.Contains(t, "]"):
			// fixed-size array `[N]Type` — drop through the closing bracket
			t = t[strings.Index(t, "]")+1:]
		default:
			return t
		}
	}
}

// stripPkgQualifier returns the element name of a package-qualified type (`pkg.Type` →
// `Type`) so the ident check accepts a qualified type while still rejecting structural
// noise (`map[..]..`, `func(..)`). A qualified external type still produces a valid name;
// resolution then ABSTAINS later if no internal class node matches that name.
func stripPkgQualifier(t string) string {
	if dot := strings.LastIndex(t, "."); dot >= 0 {
		return t[dot+1:]
	}
	return t
}

// isIdent reports whether s is a bare identifier (first char a letter or '_',
// remaining chars letters/digits/'_'). Used to reject tag/comment/operator noise.
func isIdent(s string) bool {
	if s == "" {
		return false
	}
	for i, r := range s {
		isLetter := (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || r == '_'
		isDigit := r >= '0' && r <= '9'
		if i == 0 {
			if !isLetter {
				return false
			}
		} else if !isLetter && !isDigit {
			return false
		}
	}
	return true
}

func BuildFieldTypeIndex(props []parser.PropertyRef, nodeDBIDs []int64) map[int64]map[string]string {
	idx := make(map[int64]map[string]string)
	for _, p := range props {
		if p.Kind != "class_field" || p.NodeIdx < 0 || p.NodeIdx >= len(nodeDBIDs) {
			continue
		}
		classDBID := nodeDBIDs[p.NodeIdx]
		if classDBID <= 0 {
			continue
		}
		// TS access-modifier strip (language-agnostic, applied before the colon split):
		// a TS class field is recorded as `private client: HttpClient` /
		// `public readonly client: HttpClient`. Drop the leading modifier keyword(s) so
		// the field name (not "private client") becomes the index key. No-op on
		// Python/Rust/Go field strings (their first token is already the field name).
		val := stripTSFieldModifiers(strings.TrimSpace(p.Value))
		// Strip a trailing Go struct tag (backtick-delimited, e.g. `json:"x"`): the tag
		// can contain a `:` that would otherwise hijack the colon-annotation split below,
		// mis-routing a Go field (`Client *HttpClient `+"`json:\"x\"`"+`) into the colon
		// path and abstaining. Python/Rust/TS field strings never carry a backtick tag, so
		// this is a safe language-uniform no-op for them. Take everything before the FIRST
		// backtick — the field name + type always precede the tag.
		if bt := strings.IndexByte(val, '`'); bt >= 0 {
			val = strings.TrimSpace(val[:bt])
		}
		colon := strings.Index(val, ":")
		if colon <= 0 {
			// No colon-annotation: try the Go space-separated struct-field shape
			// (`Client *HttpClient` → name "Client", type "HttpClient"). This is the Go
			// analogue of `name: Type` — the struct field's declared type IS a source FACT,
			// identical epistemic status to a colon annotation. CORRECT-OR-QUIET: index ONLY
			// the unambiguous two-token shape (exactly a simple identifier name + a type);
			// ABSTAIN on embedded fields (single token, no name), multi-token tag/comment
			// noise, and any name that is not a bare identifier — those fall through unindexed
			// so the call demotes to name_match rather than mis-resolving.
			if name, typ, ok := goStructField(val); ok {
				if idx[classDBID] == nil {
					idx[classDBID] = make(map[string]string)
				}
				idx[classDBID][name] = typ
			}
			continue // bare name / no colon separator — Go shape handled above, else no type
		}
		// Annotation vs assignment discriminator: only treat as a typed annotation
		// when the colon comes BEFORE any `=` (so `name: Type` is kept; `d = {1: 2}`
		// or `x = f(a=1)` is rejected). A `name = Ctor()` field has no leading colon
		// for the field name → already excluded; this also guards `name: Type = default`
		// (still an annotation, colon precedes `=`) which is correctly KEPT.
		if eq := strings.Index(val, "="); eq >= 0 && eq < colon {
			continue
		}
		name := strings.TrimSpace(val[:colon])
		typ := strings.TrimSpace(val[colon+1:])
		// Strip an inline default (`name: Type = default`) and any trailing flags.
		if eq := strings.Index(typ, "="); eq > 0 {
			typ = strings.TrimSpace(typ[:eq])
		}
		if sp := strings.IndexAny(typ, " ["); sp > 0 { // strip " [required]" suffix; keep generics for stripTypeWrapper at resolve time
			// only strip a trailing flag introduced by a space, NOT a generic '[' that
			// stripTypeWrapper handles — so split on the FIRST space only.
			if strings.HasPrefix(typ[sp:], " ") {
				typ = strings.TrimSpace(typ[:sp])
			}
		}
		if name == "" || typ == "" {
			continue
		}
		// field name must be a simple identifier (no dots/brackets) — a malformed
		// annotation slice must not pollute the index.
		if strings.ContainsAny(name, ". []()=") {
			continue
		}
		if idx[classDBID] == nil {
			idx[classDBID] = make(map[string]string)
		}
		idx[classDBID][name] = typ
	}
	return idx
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
	nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64,
	callerNodeIDs []int64,
	allImports []parser.ImportRef,
	fileMap map[string][]string,
	nodeMeta ...map[int64]NodeMeta,
) []ResolvedCall {
	resolved, _ := resolveInternal(allCalls, nodeIDs, fileNodeIDs, callerNodeIDs, allImports, fileMap, true, nodeMeta...)
	return resolved
}

// resolveInternal resolves every parser callsite. When dedupeAcrossCallsites is
// true it preserves the legacy graph's endpoint deduplication. When false it
// emits one resolver result per callsite so provenance is captured before the
// legacy edge projection is deduplicated by ResolveWithProvenance.
func resolveInternal(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64, // name → list of node IDs
	fileNodeIDs map[string]map[string][]int64, // file → name → list of node IDs
	callerNodeIDs []int64, // parallel to allCalls
	allImports []parser.ImportRef, // all parsed import statements
	fileMap map[string][]string, // module path → list of file paths
	dedupeAcrossCallsites bool,
	nodeMeta ...map[int64]NodeMeta, // optional: nodeID → metadata for self.method resolution
) ([]ResolvedCall, [][]ResolutionPassExecution) {
	// Build import index: file → imported name → list of candidate target files
	importIndex := buildImportIndex(allImports, fileMap)
	nameAliasIndex := buildNameAliasIndex(nodeIDs)

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

	// lookupFieldTypeWithInheritance walks the inheritance chain to find a field's
	// declared type for Strategy 2b. The annotation may live on a BASE class (the field
	// declared/typed on the superclass), so a field absent on the subclass's own
	// fieldTypeIndex entry is searched up the hierarchy, mirroring
	// lookupMethodWithInheritance. Returns (typeName, found). Walks up to 10 levels.
	lookupFieldTypeWithInheritance := func(classID int64, fieldName string) (string, bool) {
		if fieldTypeIndex == nil {
			return "", false
		}
		if fields, ok := fieldTypeIndex[classID]; ok {
			if t, ok := fields[fieldName]; ok {
				return t, true
			}
		}
		if inheritanceMap == nil {
			return "", false
		}
		visited := map[int64]bool{classID: true}
		current := classID
		for depth := 0; depth < 10; depth++ {
			parents, ok := inheritanceMap[current]
			if !ok || len(parents) == 0 {
				return "", false
			}
			for _, parentID := range parents {
				if visited[parentID] {
					continue
				}
				visited[parentID] = true
				if fields, ok := fieldTypeIndex[parentID]; ok {
					if t, ok := fields[fieldName]; ok {
						return t, true
					}
				}
			}
			current = parents[0]
		}
		return "", false
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
	executionTraces := make([][]ResolutionPassExecution, len(allCalls))
	seen := make(map[edgeKey]bool) // deduplication
	currentCallsite := -1
	var currentPasses *resolutionPassTracker
	emit := func(rc ResolvedCall) {
		if currentPasses != nil {
			currentPasses.match()
		}
		if currentCallsite >= 0 && currentCallsite < len(allCalls) {
			rc.CallsiteOrdinal = currentCallsite
			rc.Callee = allCalls[currentCallsite].CalleeName
			rc.CalleeQualified = allCalls[currentCallsite].CalleeQualified
			executionTraces[currentCallsite] = currentPasses.snapshot()
		}
		rc.CandidateNodeIDs = uniqueIDs(rc.CandidateNodeIDs)
		rc.CandidateCount = len(rc.CandidateNodeIDs)
		resolved = append(resolved, rc)
	}

	for i, call := range allCalls {
		currentCallsite = i
		currentPasses = newResolutionPassTracker(call)
		if !dedupeAcrossCallsites {
			seen = make(map[edgeKey]bool)
		}
		if call.DynamicDispatch || call.ParserIncomplete {
			executionTraces[i] = currentPasses.snapshot()
			continue
		}
		callerID := callerNodeIDs[i]
		if callerID == 0 {
			continue
		}

		calleeName := call.CalleeName
		var targets []int64
		var ok bool
		matchMethod := "name_match"
		evidence := "name_match"

		// Strategy 1: Same-file exact name match (only when unambiguous)
		currentPasses.begin("lexical_binding")
		if fileNodes, ok := fileNodeIDs[call.File]; ok {
			if targetIDs, ok := fileNodes[calleeName]; ok && len(targetIDs) == 1 && targetIDs[0] != callerID {
				targetID := targetIDs[0]
				key := edgeKey{callerID, targetID, "CALLS"}
				if !seen[key] {
					seen[key] = true
					emit(ResolvedCall{
						SourceNodeID:     callerID,
						TargetNodeID:     targetID,
						SourceLine:       call.Line,
						SourceFile:       call.File,
						Method:           "same_file",
						Confidence:       1.0,
						CandidateCount:   1,
						CandidateNodeIDs: []int64{targetID},
						TrustTier:        tierFor(1.0),
						EvidenceType:     "ast_call",
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
						emit(ResolvedCall{
							SourceNodeID:     callerID,
							TargetNodeID:     best,
							SourceLine:       call.Line,
							SourceFile:       call.File,
							Method:           "same_file",
							Confidence:       0.6,
							CandidateCount:   len(targetIDs),
							CandidateNodeIDs: append([]int64(nil), targetIDs...),
							TrustTier:        tierFor(0.6),
							EvidenceType:     "same_file_ambiguous",
						})
					}
					continue
				}
			}
		}

		// Strategy 1.5: Import-verified cross-file resolution
		// H6 fix: collect all matching imported targets, pick best (prefer same dir)
		currentPasses.begin("import_binding")
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

			importCandidates = uniqueIDs(importCandidates)
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
					emit(ResolvedCall{
						SourceNodeID:     callerID,
						TargetNodeID:     bestTarget,
						SourceLine:       call.Line,
						SourceFile:       call.File,
						Method:           "import",
						Confidence:       conf,
						CandidateCount:   len(importCandidates),
						CandidateNodeIDs: append([]int64(nil), importCandidates...),
						TrustTier:        tierFor(conf),
						EvidenceType:     evidence,
					})
				}
				continue
			}
		}

		// Strategy 1.75: self/this/Self method resolution via caller's class + inheritance (conf=1.0/0.95)
		// Handles: self.method() (Python/Rust), this.method() (JS/TS/Java),
		//          Self::method() (Rust associated fn — Self is the impl's type)
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			currentPasses.begin("declared_type")
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
						receiverType := nodeMeta[0][callerMeta.ParentID].Name
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
								emit(ResolvedCall{
									SourceNodeID:     callerID,
									TargetNodeID:     targetID,
									SourceLine:       call.Line,
									SourceFile:       call.File,
									Method:           method,
									Confidence:       conf,
									CandidateCount:   1,
									CandidateNodeIDs: []int64{targetID},
									TrustTier:        tierFor(conf),
									EvidenceType:     evidence,
									ReceiverType:     receiverType,
									ReceiverOrigin:   "call_syntax",
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
		// that holds only for UNQUALIFIED calls. A qualified call X.attr(...) whose
		// receiver X never resolves to an internal class is stdlib/external/unknown
		// (e.g. `os.walk`) and must never launder as verified_unique; its demote
		// (name_match conf=0.2, evidence name_match_qualified_unresolved) now lives
		// in the last-chance block after 1.98. [beancount-931 os.walk -> account.walk]
		qualifiedUnresolved := call.CalleeQualified != "" && call.CalleeQualified != calleeName
		// T2 (builtins): a qualified call obj.method() whose receiver never resolves to an
		// internal class + builtin/stdlib method name = a builtin call (os.path.join,
		// str.split, dict.get) — DROP rather than guess (application-centered — JARVIS
		// 2023 / PyCG ICSE 2021). #B5 reorder: this predicate no longer short-circuits
		// HERE — the receiver-PROVING rungs (1.93/1.94a/1.95/1.96/1.97) get first
		// attempt; if one of them resolves the receiver to an internal class, the call
		// IS internal and must not be dropped. The drop/demote moved to the last-chance
		// block after 1.98 and still guards the receiver-UNPROVEN rungs (1.94/1.98).
		builtinQualified := qualifiedUnresolved && (strongBuiltinMethodNames[calleeName] || builtinMethodNames[calleeName])

		// Strategy 1.9 fires here ONLY for UNQUALIFIED calls (the ACG/ECOOP 2022
		// globally-unique-name property holds for bare names). #B5: a QUALIFIED call
		// previously got demoted to name_match conf=0.2 here, BEFORE the type-aware
		// rungs 1.93-1.98 ever ran — starving e.g. a declared-type receiver
		// (`command.run()` with `command: Command`) of its type_flow resolution. The
		// qualified-unresolved demote now runs as the true last chance after 1.98.
		if !qualifiedUnresolved {
			currentPasses.begin("global_name")
			if targets, ok := nodeIDs[calleeName]; ok {
				var candidates []int64
				for _, tid := range targets {
					if tid != callerID {
						candidates = append(candidates, tid)
					}
				}
				if len(candidates) == 1 {
					targetID := candidates[0]
					// PROVENANCE GATE (B fix): the ACG/ECOOP-2022 unique-name property
					// ("a globally-unique name is 99%+ correct") only holds when the unique
					// node is actually REACHABLE from the caller. A cross-package bare-name
					// collision — caller in benchmark/libs/dacite, the only `from_dict` node
					// in mashumaro/codecs, no import linking them — is a FALSE fact (10,444
					// such CALLS edges measured live). Keep verified_unique ONLY with
					// provenance: caller and target share a directory (same package) OR the
					// caller imports a module resolving to the target's file. Otherwise DEMOTE
					// to a speculative name_match (the agent still gets the hint, never as a
					// fact). The gate engages only when nodeMeta supplies the target's file —
					// the real index path always passes it; meta-less unit paths keep the
					// legacy behavior (correct-or-quiet: we cannot judge what we cannot see).
					provenanceOK := true
					if metaMap != nil {
						if targetFile := metaMap[targetID].File; targetFile != "" {
							provenanceOK = sameDirFile(call.File, targetFile) ||
								callerImportsFile(call.File, targetFile, importIndex)
						}
					}
					key := edgeKey{callerID, targetID, "CALLS"}
					if !provenanceOK {
						if !seen[key] {
							seen[key] = true
							// Demote shape mirrors the qualified-unresolved last-chance demote
							// (conf 0.2, sub-SPECULATIVE) so tierFor agrees and Strategy 2 does
							// not re-CERTIFY this single candidate at name_match conf 0.9.
							emit(ResolvedCall{
								SourceNodeID:     callerID,
								TargetNodeID:     targetID,
								SourceLine:       call.Line,
								SourceFile:       call.File,
								Method:           "name_match",
								Confidence:       0.2,
								CandidateCount:   1,
								CandidateNodeIDs: []int64{targetID},
								TrustTier:        tierFor(0.2),
								EvidenceType:     "name_match_verified_unique_no_provenance",
							})
						}
						continue
					}
					if !seen[key] {
						seen[key] = true
						emit(ResolvedCall{
							SourceNodeID:     callerID,
							TargetNodeID:     targetID,
							SourceLine:       call.Line,
							SourceFile:       call.File,
							Method:           "verified_unique",
							Confidence:       0.95,
							CandidateCount:   1,
							CandidateNodeIDs: []int64{targetID},
							TrustTier:        tierFor(0.95),
							EvidenceType:     "name_unique",
						})
					}
					continue
				}
			}
		}

		// Strategy 1.93: Import-scoped type_flow
		// When caller imports ClassName from a specific file, scope class lookup to that file.
		// Fixes ambiguity when multiple classes share a name (e.g., "Client" in 5 files).
		// Supports both "." (Python/JS/TS/Go) and "::" (Rust) qualified separators.
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && call.CalleeQualified != "" {
			currentPasses.begin("declared_type")
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
														emit(ResolvedCall{
															SourceNodeID:     callerID,
															TargetNodeID:     targetID,
															SourceLine:       call.Line,
															SourceFile:       call.File,
															Method:           "import_type",
															Confidence:       0.95,
															CandidateCount:   1,
															CandidateNodeIDs: []int64{targetID},
															TrustTier:        tierFor(0.95),
															EvidenceType:     "import_scoped_type",
															ReceiverType:     qualifier,
															ReceiverOrigin:   "import",
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

		// Strategy 2b: Declared-FIELD-type receiver resolution (XTA over the field-type set).
		// For a qualified call self.<field>.method() (or this.<field>.method()) where the
		// caller's ENCLOSING CLASS declared `<field>` with a typed annotation
		// (`client: HttpClient`), resolve the declared field type → the class node of that
		// type → the method via CHA (the SAME lookupMethodWithInheritance primitive rungs
		// 1.75/1.94a use, so an INHERITED method on a typed field still resolves over the
		// hierarchy). This fills the gap NONE of the other rungs cover: a declared-but-not-
		// locally-assigned typed field — injected via __init__ param, declared on a base
		// class, or annotation-only — produces NO AssignmentRef (so 1.96 misses it), is not
		// a `param` (so 1.94a misses it), and the whole qualifier "self.<field>" is not the
		// bare "self"/"this" 1.75 keys on. Without 2b the call falls to the demote tail and
		// is emitted as a name_match GUESS (ambiguous across N same-named methods).
		//
		// XTA (Tip & Palsberg, OOPSLA 2000, +88% precision over RTA): propagate the declared
		// type set the parser already wrote; the matcher does not re-derive types. The
		// declared field type is a source FACT (identical epistemic status to the declared
		// param type in 1.94a) → type_flow conf 0.9 → CERTIFIED via tierFor.
		//
		// CORRECT-OR-QUIET (five guards, all receiver-PROVING — it can only UPGRADE a call
		// that would otherwise demote, it can NEVER launder a guess): (1) the field must
		// have a declared TYPE in fieldTypeIndex (annotation-only — BuildFieldTypeIndex
		// already excluded the assignment shape); (2) that type must resolve to a real
		// internal Class/Struct/Interface node (an external/stdlib type like `dict`/library
		// HttpClient has no internal node → fall through, never mint — preserves the
		// stdlib-shadow bar); (3) CHA must FIND the method on that class or a superclass;
		// (4) ABSTAIN on ambiguity — if the type name resolves to >1 same-named internal
		// class with no winner, emit NOTHING rather than pick; (5) a broken/missing
		// inheritance chain returns (0,false) → fall through.
		//
		// SCOPE (honest, correct-or-quiet): resolves declared-field calls across four field
		// shapes / four receiver shapes, all reached structurally —
		//   FIELD shapes BuildFieldTypeIndex now indexes:
		//     · Python/Rust colon annotation     `field: Type`
		//     · Go space-separated struct field   `Field *Type`  (name=Field, type=Type)
		//     · TS access-modifier field          `private field: Type` (modifier stripped)
		//   RECEIVER prefixes the shape gate accepts:
		//     · `self.`/`this.` (Python/Rust/TS/JS)
		//     · the Go method's receiver VAR      `r.field.m()` (r = caller's receiver name)
		// It STILL ABSTAINS (falls through to name_match, never mis-resolves) when: the field
		// has no declared type, the type resolves to no internal class or to >1 ambiguously,
		// CHA can't find the method, or the receiver prefix is an unknown chain. Keys on the
		// language-uniform `class_field` property + the parser-emitted CalleeQualified +
		// (for Go) the signature-derived receiver name in NodeMeta.
		if fieldTypeIndex != nil && len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil &&
			call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			currentPasses.begin("declared_type")
			// Shape gate: require a receiver prefix + a SINGLE field segment, i.e.
			// "<recv>.<field>.<method>" — strip the receiver prefix, then confirm the
			// remaining qualifier (the field) has no further dots. The receiver is the
			// language-uniform instance handle: `self.`/`this.` (Python/Rust/TS/JS) OR the
			// Go method's receiver VARIABLE name (`func (r *T) M()` → `r.<field>.<method>`),
			// which is the Go analogue of self/this. ABSTAINS when no known receiver prefix
			// matches (the qualifier is then an unknown chain → never mis-resolves).
			dot2b := strings.LastIndex(call.CalleeQualified, ".")
			if callerMeta, okCM := nodeMeta[0][callerID]; okCM && callerMeta.ParentID != 0 && dot2b > 0 {
				qualifier2b := call.CalleeQualified[:dot2b]    // "self.client" / "r.client"
				methodName2b := call.CalleeQualified[dot2b+1:] // "get"
				var fieldName2b string
				switch {
				case strings.HasPrefix(qualifier2b, "self."):
					fieldName2b = qualifier2b[len("self."):]
				case strings.HasPrefix(qualifier2b, "this."):
					fieldName2b = qualifier2b[len("this."):]
				case callerMeta.ReceiverName != "" &&
					strings.HasPrefix(qualifier2b, callerMeta.ReceiverName+"."):
					// Go named receiver: `r.client.get()` where the caller method's receiver
					// var is `r`. The receiver name is a per-method FACT from the signature,
					// so this is exactly as sound as self/this and never widens scope.
					fieldName2b = qualifier2b[len(callerMeta.ReceiverName)+1:]
				}
				// fieldName2b must be a single non-empty segment (no further dots) and
				// not itself self/this (guards "self.self.x" style noise).
				if fieldName2b != "" && !strings.Contains(fieldName2b, ".") &&
					fieldName2b != "self" && fieldName2b != "this" {
					{
						classID2b := callerMeta.ParentID // enclosing class — KNOWN fact
						if fields, ok := fieldTypeIndex[classID2b]; ok {
							// Inheritance-aware field lookup: the annotation may live on a
							// base class (the field declared/typed on the superclass). Walk
							// the field over the inheritance chain, mirroring the method CHA.
							declaredType2b, hasType := fields[fieldName2b]
							if !hasType {
								declaredType2b, hasType = lookupFieldTypeWithInheritance(classID2b, fieldName2b)
							}
							if hasType && declaredType2b != "" {
								className2b := stripTypeWrapper(declaredType2b)
								if className2b != "" {
									// Resolve the type name → internal class node(s),
									// ABSTAIN on ambiguity (>1 same-named internal class with
									// no winner → emit nothing, do not guess).
									var fieldClassID2b int64
									ambiguous2b := false
									if classIDs, ok := nodeIDs[className2b]; ok {
										for _, cid := range classIDs {
											cm, hasMeta := nodeMeta[0][cid]
											if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
												continue
											}
											if fieldClassID2b == 0 {
												fieldClassID2b = cid
											} else if cid != fieldClassID2b {
												ambiguous2b = true // ≥2 distinct internal classes share the type name
											}
										}
									}
									if fieldClassID2b != 0 && !ambiguous2b {
										if targetID, found := lookupMethodWithInheritance(fieldClassID2b, methodName2b); found && targetID != callerID {
											key := edgeKey{callerID, targetID, "CALLS"}
											if !seen[key] {
												seen[key] = true
												emit(ResolvedCall{
													SourceNodeID:     callerID,
													TargetNodeID:     targetID,
													SourceLine:       call.Line,
													SourceFile:       call.File,
													Method:           "type_flow",
													Confidence:       0.9,
													CandidateCount:   1,
													CandidateNodeIDs: []int64{targetID},
													TrustTier:        tierFor(0.9),
													EvidenceType:     "field_type",
													ReceiverType:     className2b,
													ReceiverOrigin:   "field_annotation",
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
			currentPasses.begin("declared_type")
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
												emit(ResolvedCall{
													SourceNodeID:     callerID,
													TargetNodeID:     targetID,
													SourceLine:       call.Line,
													SourceFile:       call.File,
													Method:           "type_flow",
													Confidence:       0.9,
													CandidateCount:   1,
													CandidateNodeIDs: []int64{targetID},
													TrustTier:        tierFor(0.9),
													EvidenceType:     "param_type",
													ReceiverType:     className194a,
													ReceiverOrigin:   "param_annotation",
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
		// #B5: skips builtin-named qualified calls — 1.94 does NOT prove the receiver
		// (global name-uniqueness only), so letting it claim `obj.get()`/`x.update()`
		// would re-launder dict/str calls the builtin drop exists to remove.
		if len(nodeMeta) > 0 && nodeMeta[0] != nil && methodsByClass != nil && !builtinQualified &&
			call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			currentPasses.begin("implementation_set")
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
						// Pick the best target: prefer same-file class, then the smallest
						// class node ID. #B8a: the previous `range classes194` map
						// iteration made the cross-file pick RUN-DEPENDENT (Go map order
						// is randomized) — sort the class IDs so the pick is deterministic.
						classIDs194 := make([]int64, 0, len(classes194))
						for classID := range classes194 {
							classIDs194 = append(classIDs194, classID)
						}
						sort.Slice(classIDs194, func(a, b int) bool { return classIDs194[a] < classIDs194[b] })
						// Keep a deterministic preferred endpoint only for the backward-
						// compatible legacy CALLS projection. The attached callsite trace
						// below derives ambiguity from the complete candidate set and will
						// deliberately publish no unique selection when that set has more
						// than one viable implementor.
						var bestTarget194 int64
						var sameFileTarget194 int64
						var fallbackTarget194 int64
						var candidates194 []int64
						for _, classID := range classIDs194 {
							if methods, ok := methodsByClass[classID]; ok {
								if targetID, ok := methods[methodName194]; ok && targetID != callerID {
									candidates194 = append(candidates194, targetID)
									cm := nodeMeta[0][classID]
									if cm.File == call.File && sameFileTarget194 == 0 {
										sameFileTarget194 = targetID
									}
									if fallbackTarget194 == 0 {
										fallbackTarget194 = targetID
									}
								}
							}
						}
						bestTarget194 = sameFileTarget194
						if bestTarget194 == 0 {
							bestTarget194 = fallbackTarget194
						}
						if bestTarget194 != 0 {
							key := edgeKey{callerID, bestTarget194, "CALLS"}
							if !seen[key] {
								seen[key] = true
								emit(ResolvedCall{
									SourceNodeID: callerID,
									TargetNodeID: bestTarget194,
									SourceLine:   call.Line,
									SourceFile:   call.File,
									Method:       "impl_method",
									Confidence:   conf194,
									// CandidateNodeIDs is the authority for attached
									// provenance; TargetNodeID is retained only for the
									// legacy endpoint-deduplicated CALLS edge.
									CandidateCount:   len(candidates194),
									CandidateNodeIDs: append([]int64(nil), candidates194...),
									TrustTier:        tierFor(conf194),
									EvidenceType:     "single_implementor",
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
			currentPasses.begin("declared_type")
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
										emit(ResolvedCall{
											SourceNodeID:     callerID,
											TargetNodeID:     targetID,
											SourceLine:       call.Line,
											SourceFile:       call.File,
											Method:           "type_flow",
											Confidence:       0.9,
											CandidateCount:   1,
											CandidateNodeIDs: []int64{targetID},
											TrustTier:        tierFor(0.9),
											EvidenceType:     "type_qualified",
											ReceiverType:     qualifier,
											ReceiverOrigin:   "type_qualifier",
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
			currentPasses.begin("declared_type")
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+1:]
				// Handle self.x.method() / this.x.method() / super.x.method() → strip the
				// leading receiver keyword to get the field "x". super. was previously
				// NOT stripped (P2-8): `super.field.method()` left qualifier as
				// "super.field", which matched no assignment and mis-scoped the call
				// (or fell through to a name guess) instead of resolving field x's type.
				if strings.HasPrefix(qualifier, "self.") {
					qualifier = qualifier[len("self."):]
				} else if strings.HasPrefix(qualifier, "this.") {
					qualifier = qualifier[len("this."):]
				} else if strings.HasPrefix(qualifier, "super.") {
					qualifier = qualifier[len("super."):]
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
										if !hasMeta {
											continue
										}
										if fm.Label == "Class" || fm.Label == "Struct" || fm.Label == "Interface" {
											continue
										}
										// Prefer the parser's declared return TYPE; when it is
										// empty (no annotation captured), fall back to the
										// CONSTRUCTOR return shape (GAP C) — a factory whose
										// body returns `ClassName(...)`/`&Struct{...}` HAS that
										// runtime type even with no declared signature. The
										// fallback is constructor-only (a fact), never data_flow.
										rt := stripTypeWrapper(fm.ReturnType)
										if rt == "" && returnShapeIndex != nil {
											rt = returnShapeIndex[funcID]
										}
										if rt != "" {
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
												emit(ResolvedCall{
													SourceNodeID:     callerID,
													TargetNodeID:     targetID,
													SourceLine:       call.Line,
													SourceFile:       call.File,
													Method:           "type_flow",
													Confidence:       0.9,
													CandidateCount:   1,
													CandidateNodeIDs: []int64{targetID},
													TrustTier:        tierFor(0.9),
													EvidenceType:     "assignment_tracked",
													ReceiverType:     className,
													ReceiverOrigin:   "assignment",
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
			currentPasses.begin("return_type")
			if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
				qualifier := call.CalleeQualified[:dotIdx]
				methodName := call.CalleeQualified[dotIdx+1:]
				// A DIRECT factory chain `make_user(args).save()` records the qualifier
				// WITH its call args (`make_user(args)`), which never matches the bare
				// function node. Strip a trailing balanced `(...)` so `make_user(args)` ->
				// `make_user` resolves to the factory func (then bridge through its return
				// type / constructor-return shape). Correct-or-quiet: only the bare-name
				// lookup below admits it; a non-function qualifier still finds nothing.
				qualifier = stripCallArgs(qualifier)
				if qualifier != "self" && qualifier != "this" && qualifier != "super" {
					// Check if qualifier is a function call: look for a function with this name
					if funcIDs, ok := nodeIDs[qualifier]; ok {
						for _, funcID := range funcIDs {
							fm, hasMeta := nodeMeta[0][funcID]
							if !hasMeta {
								continue
							}
							if fm.Label == "Class" || fm.Label == "Struct" || fm.Label == "Interface" {
								continue
							}
							// Strip common wrappers: Optional[X] → X, list[X] → X, *X → X.
							retType := stripTypeWrapper(fm.ReturnType)
							// GAP C fallback: no declared return type -> use the CONSTRUCTOR
							// return shape (factory whose body returns `ClassName(...)` /
							// `&Struct{...}`). Constructor-only fact; never data_flow.
							if retType == "" && returnShapeIndex != nil {
								retType = returnShapeIndex[funcID]
							}
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
												emit(ResolvedCall{
													SourceNodeID:     callerID,
													TargetNodeID:     targetID,
													SourceLine:       call.Line,
													SourceFile:       call.File,
													Method:           "return_type",
													Confidence:       0.85,
													CandidateCount:   1,
													CandidateNodeIDs: []int64{targetID},
													TrustTier:        tierFor(0.85),
													EvidenceType:     "return_type_flow",
													ReceiverType:     retType,
													ReceiverOrigin:   "return_type",
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
		// #B5: builtin-named calls are excluded — 1.98 does not prove the receiver, and an
		// internal class happening to define `update`/`get` must not claim every dict call.
		// CONFIDENCE: 1.98 is the SAME receiver-unproven class as 1.94 (impl_method) —
		// global method-name uniqueness, ZERO check that `obj` is actually that class.
		// Name-uniqueness != receiver-proof, so it MUST be capped at CANDIDATE (0.6),
		// identical to 1.94's 1-class case — NEVER 0.85 (which read as a type-derived
		// fact, cleared the closure's 0.7 reach floor, and disagreed with the consumer
		// fact set that excludes unique_method). CERTIFIED/type-derived tiers stay
		// reserved for the rungs that PROVE the receiver (1.75 self, 1.93/1.94a
		// import/declared-type, 1.95/1.96 type_flow, 1.97 return_type).
		if !builtinQualified && call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			currentPasses.begin("global_name")
			if classID, ok := uniqueMethodClass[calleeName]; ok {
				if methods, ok := methodsByClass[classID]; ok {
					if targetID, ok := methods[calleeName]; ok && targetID != callerID {
						key := edgeKey{callerID, targetID, "CALLS"}
						if !seen[key] {
							seen[key] = true
							emit(ResolvedCall{
								SourceNodeID:     callerID,
								TargetNodeID:     targetID,
								SourceLine:       call.Line,
								SourceFile:       call.File,
								Method:           "unique_method",
								Confidence:       0.6,
								CandidateCount:   1,
								CandidateNodeIDs: []int64{targetID},
								TrustTier:        tierFor(0.6),
								EvidenceType:     "unique_method_class",
							})
						}
						continue
					}
				}
			}
		}

		// LAST CHANCE for qualified-unresolved calls (#B5 — the reordered tail of
		// Strategy 1.9): every receiver-typing rung above failed, so the receiver is
		// stdlib/external/unknown.
		//   - builtin method name → a builtin call (os.path.join, dict.get): DROP it
		//     rather than emit a name_match guess to an arbitrary same-named internal
		//     method (covers BOTH the single- and multi-candidate paths — #6).
		//   - single global candidate → DEMOTE to a speculative name_match
		//     (evidence name_match_qualified_unresolved) so a qualified stdlib call
		//     never launders as a confident fact while the agent still gets the hint.
		//     [beancount-931 os.walk -> account.walk]
		if qualifiedUnresolved {
			if builtinQualified {
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
						// Confidence must sit below the SPECULATIVE threshold so tierFor
						// agrees with the demote (a sub-0.5 conf, not the 0.9 single-
						// candidate name_match score that tierFor would re-CERTIFY).
						emit(ResolvedCall{
							SourceNodeID:     callerID,
							TargetNodeID:     targetID,
							SourceLine:       call.Line,
							SourceFile:       call.File,
							Method:           "name_match",
							Confidence:       0.2,
							CandidateCount:   1,
							CandidateNodeIDs: []int64{targetID},
							TrustTier:        tierFor(0.2),
							EvidenceType:     "name_match_qualified_unresolved",
						})
					}
					continue
				}
			}
		}

		currentPasses.begin("global_name")
		// Strategy 2: Cross-file name match (fallback). Exact spelling matches use
		// the raw name index; if none exists, the alias index bridges common naming
		// style variation (getUser/get_user/GetUser) at lower confidence.
		// Qualified builtin calls were already dropped by the last-chance block above.
		targets, ok = nodeIDs[calleeName]
		matchMethod = "name_match"
		evidence = "name_match"
		if !ok {
			if key := canonicalNameKey(calleeName); key != "" {
				targets, ok = nameAliasIndex[key]
				if ok {
					matchMethod = "name_match_alias"
					evidence = "name_match_alias"
				}
			}
		}
		if ok {
			candidateCount := 0
			var candidates []int64

			for _, targetID := range targets {
				if targetID == callerID {
					continue
				}
				candidateCount++
				candidates = append(candidates, targetID)
			}

			if candidateCount > 0 && (matchMethod == "name_match_alias" || candidateCount > 1) {
				bestTarget := pickBestNameMatchTarget(candidates, callerID, call.File, metaMap)
				if bestTarget == 0 {
					continue
				}
				conf := computeConfidence(matchMethod, candidateCount)
				key := edgeKey{callerID, bestTarget, "CALLS"}
				if !seen[key] {
					seen[key] = true
					emit(ResolvedCall{
						SourceNodeID:     callerID,
						TargetNodeID:     bestTarget,
						SourceLine:       call.Line,
						SourceFile:       call.File,
						Method:           "name_match",
						Confidence:       conf,
						CandidateCount:   candidateCount,
						CandidateNodeIDs: append([]int64(nil), candidates...),
						TrustTier:        tierFor(conf),
						EvidenceType:     evidence,
					})
				}
			}
		}
	nextCall:
		if len(executionTraces[i]) == 0 {
			executionTraces[i] = currentPasses.snapshot()
		}
	}

	return resolved, executionTraces
}

func uniqueIDs(ids []int64) []int64 {
	seen := make(map[int64]struct{}, len(ids))
	unique := make([]int64, 0, len(ids))
	for _, id := range ids {
		if id == 0 {
			continue
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		unique = append(unique, id)
	}
	return unique
}

// ResolveWithProvenance records resolver-owned candidate identities for every callsite.
func ResolveWithProvenance(allCalls []parser.CallRef, nodeIDs map[string][]int64, fileNodeIDs map[string]map[string][]int64, callerNodeIDs []int64, allImports []parser.ImportRef, fileMap map[string][]string, nodeMeta ...map[int64]NodeMeta) ([]ResolvedCall, []ResolutionCallsite) {
	// Resolve every callsite with only per-callsite deduplication. This keeps the
	// resolver's pre-dedupe result available for provenance, including repeated
	// calls that the legacy graph intentionally collapses to one endpoint edge.
	perCallsite, executionTraces := resolveInternal(allCalls, nodeIDs, fileNodeIDs, callerNodeIDs, allImports, fileMap, false, nodeMeta...)
	resolved := dedupeResolvedCalls(perCallsite)
	var provenanceMeta map[int64]NodeMeta
	if len(nodeMeta) > 0 {
		provenanceMeta = nodeMeta[0]
	}
	callsites := make([]ResolutionCallsite, len(allCalls))
	for i, call := range allCalls {
		var sourceID int64
		if i < len(callerNodeIDs) {
			sourceID = callerNodeIDs[i]
		}
		trace := ResolutionCallsite{CallsiteOrdinal: i, SourceNodeID: sourceID, SourceLine: call.Line, SourceFile: call.File, Callee: call.CalleeName, CalleeQualified: call.CalleeQualified, DispatchState: DispatchZero, ParserComplete: !call.ParserIncomplete, VerificationStatus: "unverified", ImportChain: callImportEvidence(call, allImports), CandidateImportChains: make(map[int64][]string), ASTPath: call.ASTPath, ByteStart: call.ByteStart, ByteEnd: call.ByteEnd, ColumnStart: call.ColumnStart, ArgumentArity: call.ArgumentArity, DispatchForm: call.DispatchForm, PassExecutions: append([]ResolutionPassExecution(nil), executionTraces[i]...)}
		if call.CalleeQualified != "" && call.CalleeQualified != call.CalleeName {
			trace.ReceiverOrigin = "call_syntax"
		}
		if call.ParserIncomplete {
			trace.DispatchState = DispatchParserIncomplete
			trace.VerificationStatus = "abstained_parser_incomplete"
			callsites[i] = trace
			continue
		}
		if call.DynamicDispatch {
			trace.DispatchState, trace.Mechanism = DispatchDynamic, "dynamic"
			trace.VerificationStatus = "abstained_dynamic"
			callsites[i] = trace
			continue
		}
		// The internal resolver stamped the parser ordinal at emission time, before
		// the separate legacy endpoint dedupe above. No source/line/name join is used.
		var rc *ResolvedCall
		for j := range perCallsite {
			if perCallsite[j].CallsiteOrdinal == i {
				rc = &perCallsite[j]
				break
			}
		}
		if rc != nil {
			trace.CandidateNodeIDs = append([]int64(nil), rc.CandidateNodeIDs...)
			trace.Mechanism, trace.EvidenceType = rc.Method, rc.EvidenceType
			trace.ReceiverType, trace.ReceiverOrigin = rc.ReceiverType, rc.ReceiverOrigin
			if sourceSupportedResolution(*rc) {
				trace.VerificationStatus = "source_supported"
			} else {
				trace.VerificationStatus = "candidate_only"
			}
			for _, targetID := range trace.CandidateNodeIDs {
				trace.CandidateImportChains[targetID] = candidateImportEvidence(call, targetID, allImports, fileMap, provenanceMeta)
			}
			switch len(trace.CandidateNodeIDs) {
			case 0:
				trace.DispatchState = DispatchZero
			case 1:
				if sourceSupportedResolution(*rc) {
					selected := trace.CandidateNodeIDs[0]
					trace.SelectedTargetNodeID = &selected
					trace.DispatchState = DispatchUnique
				} else {
					trace.DispatchState = DispatchCandidateOnly
				}
			default:
				trace.DispatchState = DispatchAmbiguous
			}
		} else if call.CalleeQualified != "" && call.CalleeQualified != call.CalleeName {
			trace.DispatchState, trace.Mechanism = DispatchExternalUnresolved, "external"
		} else {
			trace.Mechanism = "unknown_legacy"
		}
		if trace.PassExecutions == nil {
			trace.PassExecutions = newResolutionPassTracker(call).snapshot()
		}
		callsites[i] = trace
	}
	return resolved, callsites
}

func candidateImportEvidence(call parser.CallRef, targetID int64, imports []parser.ImportRef, fileMap map[string][]string, meta map[int64]NodeMeta) []string {
	target, ok := meta[targetID]
	if !ok || target.File == "" {
		return []string{}
	}
	qualifier := ""
	if idx := strings.IndexAny(call.CalleeQualified, ".:"); idx > 0 {
		qualifier = call.CalleeQualified[:idx]
	}
	evidence := make([]string, 0)
	seen := make(map[string]struct{})
	for _, imp := range imports {
		if imp.File != call.File || (imp.ImportedName != call.CalleeName && imp.ImportedName != qualifier && imp.ImportedName != "*") {
			continue
		}
		effectivePath := imp.ModulePath
		if strings.HasPrefix(effectivePath, "./") || strings.HasPrefix(effectivePath, "../") {
			effectivePath = filepath.ToSlash(filepath.Clean(filepath.Join(filepath.Dir(call.File), effectivePath)))
		}
		matched := false
		for _, candidateFile := range resolveModulePath(effectivePath, fileMap) {
			if filepath.ToSlash(candidateFile) == filepath.ToSlash(target.File) {
				matched = true
				break
			}
		}
		if !matched {
			continue
		}
		item := imp.ModulePath + ":" + imp.ImportedName
		if _, exists := seen[item]; exists {
			continue
		}
		seen[item] = struct{}{}
		evidence = append(evidence, item)
	}
	return evidence
}

func callImportEvidence(call parser.CallRef, imports []parser.ImportRef) []string {
	qualifier := ""
	if idx := strings.IndexAny(call.CalleeQualified, ".:"); idx > 0 {
		qualifier = call.CalleeQualified[:idx]
	}
	evidence := make([]string, 0)
	seen := make(map[string]struct{})
	for _, imp := range imports {
		if imp.File != call.File || (imp.ImportedName != call.CalleeName && imp.ImportedName != qualifier && imp.ImportedName != "*") {
			continue
		}
		item := imp.ModulePath + ":" + imp.ImportedName
		if _, ok := seen[item]; ok {
			continue
		}
		seen[item] = struct{}{}
		evidence = append(evidence, item)
	}
	return evidence
}

func dedupeResolvedCalls(calls []ResolvedCall) []ResolvedCall {
	seen := make(map[edgeKey]bool, len(calls))
	resolved := make([]ResolvedCall, 0, len(calls))
	for _, call := range calls {
		key := edgeKey{call.SourceNodeID, call.TargetNodeID, "CALLS"}
		if seen[key] {
			continue
		}
		seen[key] = true
		resolved = append(resolved, call)
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
		for _, ext := range []string{".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs"} {
			if files, ok := fileMap[cleaned+ext]; ok {
				return files
			}
		}
		for _, idx := range []string{"/index.ts", "/index.js", "/index.tsx", "/index.jsx", "/index.mjs", "/index.cjs"} {
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
//  1. Find the source file in fileMap via SourceModule
//  2. Register the barrel file's fileMap keys as also pointing to the source file
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
			for _, ext := range []string{"", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs",
				"/index.ts", "/index.js", "/index.tsx", "/index.jsx", "/index.mjs", "/index.cjs", "/mod.rs"} {
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
//
// #B3: synthetic File-anchor nodes (label "File", minted by the parser for
// barrel/re-export files with zero symbols) are EXCLUDED — they exist only to
// anchor file→file relationship edges (RE_EXPORTS/IMPORTS, looked up straight
// from the DB) and must never be call-resolution targets. Registering them let
// Strategy 1.9 stamp a phantom module-named node as a verified_unique callee.
func BuildNameIndex(db *store.DB, nodes []store.Node, nodeDBIDs []int64) (map[string][]int64, map[string]map[string][]int64) {
	nameIndex := make(map[string][]int64)
	fileIndex := make(map[string]map[string][]int64)

	for i, n := range nodes {
		if n.Label == "File" {
			continue // File anchors are never call targets (#B3)
		}
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

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
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
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
	// DETERMINISM (Fable RS2): sort the tsconfig patterns AND snapshot fm keys sorted
	// BEFORE mutating fm. Ranging cfg.Paths (a map) and ranging fm WHILE inserting into it
	// are both order-undefined — flipping which alias target the import resolver picks and
	// breaking graph.db byte-identity. Snapshotting also removes the range-and-mutate
	// footgun (Go may or may not visit an entry added during iteration): only the ORIGINAL
	// fm keys are expanded, never chain-expanding a freshly-added alias nondeterministically.
	patterns := make([]string, 0, len(cfg.Paths))
	for pattern := range cfg.Paths {
		patterns = append(patterns, pattern)
	}
	sort.Strings(patterns)
	fmKeys := make([]string, 0, len(fm))
	for key := range fm {
		fmKeys = append(fmKeys, key)
	}
	sort.Strings(fmKeys)
	for _, pattern := range patterns {
		replacements := cfg.Paths[pattern]
		if len(replacements) == 0 || !strings.HasSuffix(pattern, "/*") {
			continue
		}
		prefix := strings.TrimSuffix(pattern, "/*")
		replBase := strings.TrimSuffix(replacements[0], "/*")
		for _, key := range fmKeys {
			if strings.HasPrefix(key, replBase+"/") {
				aliasKey := prefix + "/" + strings.TrimPrefix(key, replBase+"/")
				fm[aliasKey] = append(fm[aliasKey], fm[key]...)
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
				IsExported:   n.IsExported,
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
	// ReceiverType is CALL-SITE PROVENANCE for a receiver-PROVEN method-call edge: the
	// class name the resolver proved the receiver to be when it resolved obj.method()
	// structurally (self/this/Self CHA, import_type, field_type, param_type, assignment
	// type_flow, return_type). Empty on every receiver-BLIND or name_match edge and on
	// the UNPROVEN cross-scope/unproven-qualifier fallbacks — a guess never carries a
	// fact-shaped receiver tag (correct-or-quiet). Serialized additively onto
	// edges.metadata as a `receiver_type=<T>` tag (cmd/gt-index/main.go), the same
	// `;`-separated key=value convention the promote pass uses for `dataflow=`. Purely
	// additive: zero-value "" leaves the edge's metadata byte-identical to before.
	ReceiverType string
	// CandidateNodeIDs retains the complete viable identity set considered by
	// the resolver for this call. TargetNodeID is only the selected edge; these
	// IDs preserve ambiguity for downstream consumers instead of collapsing it
	// into candidate_count alone.
	CandidateNodeIDs []int64
}

// DispatchState is the producer-owned resolution outcome for one parsed
// callsite. These values intentionally match the HAR-6 consumer contract.
type DispatchState string

const (
	DispatchZero               DispatchState = "zero"
	DispatchUnique             DispatchState = "unique"
	DispatchAmbiguous          DispatchState = "ambiguous"
	DispatchDynamic            DispatchState = "dynamic"
	DispatchExternalUnresolved DispatchState = "external_unresolved"
	DispatchParserIncomplete   DispatchState = "parser_incomplete"
)

// ResolutionCallsite retains resolver-time candidate identities before the
// legacy selected-edge deduplication step can discard callsite information.
type ResolutionCallsite struct {
	CallsiteOrdinal      int
	SourceNodeID         int64
	SourceLine           int
	SourceFile           string
	Callee               string
	CalleeQualified      string
	DispatchState        DispatchState
	CandidateNodeIDs     []int64
	SelectedTargetNodeID *int64
	Mechanism            string
	DeclaredScope        string
	ReceiverType         string
	ReceiverOrigin       string
	ReceiverShape        string
	ReceiverChain        []string
	ImportChain          []string
	DynamicDispatch      bool
	ParserComplete       bool
	VerificationStatus   string
}

// Validate enforces candidate conservation and selection membership at the
// producer boundary, before any row reaches SQLite.
func (c ResolutionCallsite) Validate() error {
	seen := make(map[int64]struct{}, len(c.CandidateNodeIDs))
	for _, id := range c.CandidateNodeIDs {
		if id == 0 {
			return fmt.Errorf("candidate target ID must be nonzero")
		}
		if _, exists := seen[id]; exists {
			return fmt.Errorf("candidate targets must be unique")
		}
		seen[id] = struct{}{}
	}
	if c.SelectedTargetNodeID != nil {
		if _, exists := seen[*c.SelectedTargetNodeID]; !exists {
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
	case DispatchZero, DispatchDynamic, DispatchExternalUnresolved, DispatchParserIncomplete:
		if len(c.CandidateNodeIDs) != 0 || c.SelectedTargetNodeID != nil {
			return fmt.Errorf("unresolved callsite cannot retain target authority")
		}
	default:
		return fmt.Errorf("unknown dispatch state %q", c.DispatchState)
	}
	return nil
}

func candidateIDs(ids []int64, callerID int64) []int64 {
	seen := make(map[int64]struct{}, len(ids))
	for _, id := range ids {
		if id != 0 && id != callerID {
			seen[id] = struct{}{}
		}
	}
	out := make([]int64, 0, len(seen))
	for id := range seen {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
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

// _containerHeads: type names whose RECEIVER is a builtin container — a call on one of these
// (`d.get()`, `xs.append()`) targets the language's builtin method, NOT an internal class. There is
// no internal node to resolve to, so receiver resolution must ABSTAIN. Language-uniform data table
// (Python typing + builtins, Rust std collections, TS/Java collections) — NOT a per-repo/task hack.
var _containerHeads = map[string]bool{
	// Python typing + builtins
	"list": true, "List": true, "dict": true, "Dict": true, "set": true, "Set": true,
	"frozenset": true, "FrozenSet": true, "tuple": true, "Tuple": true, "Sequence": true,
	"MutableSequence": true, "Mapping": true, "MutableMapping": true, "Iterable": true,
	"Iterator": true, "Collection": true, "Deque": true, "DefaultDict": true, "OrderedDict": true,
	"Counter": true, "ChainMap": true,
	// Rust std collections
	"Vec": true, "VecDeque": true, "HashMap": true, "HashSet": true, "BTreeMap": true,
	"BTreeSet": true, "BinaryHeap": true, "LinkedList": true,
	// TS/JS + Java collections
	"Array": true, "ReadonlyArray": true, "Map": true, "Record": true, "Promise": true,
}

// _identityWrappers: type constructors whose sole type argument IS the receiver — `Optional[User]`,
// `Option<User>`, `*User`, `Box<User>` all have receiver `User`. Unwrap to the inner type (recurse).
var _identityWrappers = map[string]bool{
	"Optional": true, "Option": true, "Box": true, "Rc": true, "Arc": true, "Ref": true,
	"RefCell": true, "Cell": true, "Weak": true, "Lazy": true, "Final": true, "ClassVar": true,
	"Awaitable": true, "Coroutine": true,
}

// receiverTypeName is the ONE receiver-position type normalizer (Fable P0-A, 2026-07-05). Unlike
// stripTypeWrapper — which unwraps `Dict[str,Entry]`→`Entry`, `Queue[Task]`→`Task`, `Foo|Bar`→`Foo`
// and lets the caller mint a CERTIFIED edge onto the WRONG class (the stdlib-shadow launder the P0
// suppression was built to kill) — this returns the type of the RECEIVER and honors the doc's 2b
// abstain contract:
//   - identity wrappers (Optional/Option/Box/*/&) → the inner type IS the receiver → unwrap;
//   - a builtin CONTAINER head (List/Dict/Vec/Map/…) → the receiver is builtin, no internal class
//     exists → ABSTAIN (name="", abstain=true) rather than resolve the element type;
//   - a CUSTOM generic (`Queue[Task]`, `MyBox<T>`) → the HEAD is the receiver class (`Queue`), never
//     the type argument;
//   - a union with ≥2 non-None arms → ambiguous receiver → ABSTAIN.
//
// Language-uniform: handles `[...]` (Python), `<...>` (Rust/TS/Java), Go `[]T`/`map[...]`, `*`/`&`,
// and `|` unions via the two data tables above. Non-receiver callers keep stripTypeWrapper.
func receiverTypeName(t string) (name string, abstain bool) {
	t = strings.TrimSpace(t)
	if t == "" {
		return "", true
	}
	t = strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(t, "*"), "&"))
	// Go builtin containers: []T slice, map[K]V → receiver is builtin → abstain.
	if strings.HasPrefix(t, "[]") || strings.HasPrefix(t, "map[") {
		return "", true
	}
	// Union: keep the single non-None arm; ≥2 non-None arms is an ambiguous receiver → abstain.
	if strings.Contains(t, "|") {
		var nonNone []string
		for _, p := range strings.Split(t, "|") {
			p = strings.TrimSpace(p)
			if p != "" && p != "None" && p != "nil" {
				nonNone = append(nonNone, p)
			}
		}
		if len(nonNone) == 1 {
			return receiverTypeName(nonNone[0])
		}
		return "", true
	}
	// Generic head: `Name[...]` or `Name<...>`.
	if i := strings.IndexAny(t, "[<"); i > 0 {
		head := strings.TrimSpace(t[:i])
		inner := ""
		if strings.HasSuffix(t, "]") || strings.HasSuffix(t, ">") {
			inner = strings.TrimSpace(t[i+1 : len(t)-1])
		}
		if _identityWrappers[head] {
			if comma := strings.Index(inner, ","); comma > 0 {
				inner = strings.TrimSpace(inner[:comma])
			}
			return receiverTypeName(inner)
		}
		if _containerHeads[head] {
			return "", true // builtin container receiver — no internal class to resolve
		}
		return head, false // custom generic — the HEAD is the receiver class
	}
	if _containerHeads[t] {
		return "", true // bare `list`/`dict`/`Vec` as a type
	}
	return t, false
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

// sortNodeIDsByContent returns ids ordered by (file, startLine, id) using meta — a
// CONTENT-deterministic order independent of the run-dependent node-ID assignment from the
// parallel parse. The type-flow rungs pick the FIRST same-named class/function that
// resolves; with ≥2 same-named candidates the raw nodeIDs[name] slice order is run-
// dependent, so the pick (and thus graph.db) becomes non-deterministic. This makes the
// pick content-stable. Mirrors resolveByName / pickBestNameMatchTarget (file,line,id).
// Fast-path: 0/1 candidates are already deterministic — return as-is (no allocation).
func sortNodeIDsByContent(ids []int64, meta map[int64]NodeMeta) []int64 {
	if len(ids) <= 1 {
		return ids
	}
	out := make([]int64, len(ids))
	copy(out, ids)
	sort.Slice(out, func(a, b int) bool {
		ma, mb := meta[out[a]], meta[out[b]]
		if ma.File != mb.File {
			return ma.File < mb.File
		}
		if ma.StartLine != mb.StartLine {
			return ma.StartLine < mb.StartLine
		}
		return out[a] < out[b]
	})
	return out
}

// resolveInternalClassByName maps a receiver TYPE NAME to a SINGLE internal class node,
// enforcing the SAME ambiguity contract Strategy 2b already applies so no rung guesses a
// cc=1 CERTIFIED edge onto an arbitrary same-named class. Returns:
//   - (id>0, false): exactly one internal class of this name, OR ≥2 but the caller's import
//     disambiguates to one (import-directed disambiguation, Strategy 1.93).
//   - (0, true):     ABSTAIN — ≥2 distinct internal classes share the name and imports do
//     not pick one. The caller must NOT mint a fact; fall through / demote.
//   - (0, false):    no internal class of this name — a plain miss (fall through).
//
// A3 (Fable 2026-07-05): the receiver rungs (1.94a param_type · 1.96 · fixpoint · return_type)
// each iterated sortNodeIDsByContent and picked the FIRST same-named class → a content-order-
// arbitrary cc=1 CERTIFIED launder. Centralizing 2b's guard makes every rung honest.
func resolveInternalClassByName(className, callerFile string,
	nodeIDs map[string][]int64, meta map[int64]NodeMeta,
	fileNodeIDs map[string]map[string][]int64,
	importIndex map[string]map[string][]string) (int64, bool) {

	isClass := func(id int64) bool {
		cm, ok := meta[id]
		return ok && (cm.Label == "Class" || cm.Label == "Struct" || cm.Label == "Interface")
	}
	var first int64
	ambiguous := false
	for _, cid := range sortNodeIDsByContent(nodeIDs[className], meta) {
		if !isClass(cid) {
			continue
		}
		if first == 0 {
			first = cid
		} else if cid != first {
			ambiguous = true
		}
	}
	if first == 0 {
		return 0, false // no internal class of this name
	}
	if !ambiguous {
		return first, false
	}
	// ≥2 distinct same-named internal classes: prefer the one the caller imports.
	if fileImports, ok := importIndex[callerFile]; ok {
		for _, tf := range fileImports[className] {
			if fn, ok := fileNodeIDs[tf]; ok {
				for _, cid := range sortNodeIDsByContent(fn[className], meta) {
					if isClass(cid) {
						return cid, false // import disambiguates
					}
				}
			}
		}
	}
	return 0, true // ambiguous, imports don't disambiguate → ABSTAIN
}

// a3SingleClass wraps resolveInternalClassByName as a 0-or-1-element slice so a receiver
// rung's `for _, classID := range …` loop body stays byte-identical while iterating ONLY
// the import-disambiguated class — empty on abstain/miss, never a content-first guess.
func a3SingleClass(className, callerFile string,
	nodeIDs map[string][]int64, meta map[int64]NodeMeta,
	fileNodeIDs map[string]map[string][]int64,
	importIndex map[string]map[string][]string) []int64 {
	if id, abstain := resolveInternalClassByName(className, callerFile, nodeIDs, meta, fileNodeIDs, importIndex); !abstain && id != 0 {
		return []int64{id}
	}
	return nil
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

// importShadowsButTargetAbsent reports whether `name` is import-BOUND in callerFile to a
// non-empty resolved file-set F* that contains NONE of the candidate targets' files —
// i.e. the import statement `import {name} from '...'` lexically says `name` lives in F*,
// yet no same-named node exists in F*, so any cross-file name_match minted onto a file
// ∉ F* would be a WRONG fact (arktype: `import {type} from "arktype"` resolves to
// ark/type/index.ts, whose flagship `type` export is a generic const the indexer cannot
// node-ify → 0 `type` nodes in F* → the only same-named nodes live in docs/attest/scratch,
// none of them the callee). This is B1's missing half: B1 drops when the import resolves
// to NO indexed file (external); this drops when it resolves to F* that holds no matching
// node. Returns false (do NOT drop, correct-or-quiet) when: name is not import-bound, F*
// is empty, ANY candidate IS in F* (legitimate — keep), or metaMap is nil (cannot judge).
// Keys ONLY on the NAME's import binding (positive structural evidence) → an implicit-this
// / framework-injected global (jest `expect`) is not import-bound by name → UNTOUCHED.
// filepath-normalized to match callerImportsFile.
func importShadowsButTargetAbsent(
	callerFile, name string,
	candidates []int64, callerID int64,
	importIndex map[string]map[string][]string,
	metaMap map[int64]NodeMeta,
) bool {
	if metaMap == nil {
		return false
	}
	fe, ok := importIndex[callerFile]
	if !ok {
		return false
	}
	boundFiles := fe[name]
	if len(boundFiles) == 0 {
		return false // name not import-bound in this file → B1's import-binding requirement
	}
	fset := make(map[string]bool, len(boundFiles))
	for _, f := range boundFiles {
		fset[filepath.ToSlash(f)] = true
	}
	for _, tid := range candidates {
		if tid == callerID {
			continue
		}
		if m, ok := metaMap[tid]; ok && m.File != "" {
			if fset[filepath.ToSlash(m.File)] {
				return false // a candidate lives in the imported file-set → legitimate, keep
			}
		}
	}
	return true // import shadows the name but no candidate is in F* → minting is provably wrong
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
	// IsExported mirrors the store node's is_exported flag (language-specific: Python =
	// no leading underscore, Go = capitalized, JS/TS = conservatively true). Consumed
	// ONLY by the B3 field-based candidate set (GT_FIELD_CANDIDATES): an unexported def
	// is not a legal cross-file receiver-method target. Unused (and thus behavior-inert)
	// on every path when GT_FIELD_CANDIDATES is unset.
	IsExported bool
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

// reExportGraphIncomplete: set true by the caller ONLY on the incremental (`-file`)
// reindex path, where the whole-repo re-export set is NOT re-parsed and so
// ChainReExports (which folds barrel/re-export SOURCES into the file map before
// Resolve — main.go full path) never ran. B1b (import-consistency negative evidence)
// keys its drop on "no candidate node lives in the imported file-set F*"; that is
// only SOUND when F* is complete, i.e. when re-exports have been folded. On the
// incremental path F* is a bare direct-module resolution (a barrel's re-export
// targets are absent from it), so a legitimate re-exported def would look "absent"
// and a DROP would delete a true edge. When this flag is true, B1b DEMOTES the
// shadowed candidates to sub-floor name_match (conf 0.2) instead of dropping — the
// suspicion stands, the drop is voided, and (critically) the call does not fall
// through to Strategy 1.9's verified_unique 0.95 CERTIFIED mint. Default false →
// full-index path + unit tests keep B1b's DROP active (F* complete there).
var reExportGraphIncomplete bool

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

// Per-language builtin/stdlib method drop-sets. The two sets above are the
// language-neutral (Python/stdlib-shaped) DEFAULT applied to every file (unchanged
// behavior). These add the method names builtin to a SPECIFIC language so a
// qualified-unresolved call to one is DROPPED rather than laundered into a
// name_match edge to an arbitrary same-named user function (e.g. JS `promise.then()`
// must not bind a user `then`). Keyed off the source file's language (derived from
// its extension — the ONE-surface dispatch convention). Per-language DATA, never a
// per-repo hardcode. Strictly ADDITIVE over the default sets: only ever drops MORE
// speculative name_match garbage, never emits an edge the default set suppressed.
// Correct-or-quiet: fire ONLY on qualifiedUnresolved calls (all receiver-typing
// rungs already failed) — a resolved internal `this.map()` never reaches here.
var (
	builtinMethodNamesByLang = map[string]map[string]bool{
		"javascript": jsBuiltinMethodNames,
		"typescript": jsBuiltinMethodNames,
		"rust":       rustBuiltinMethodNames,
		"go":         goBuiltinMethodNames,
	}

	// jsBuiltinMethodNames: JS/TS Array/Promise/String/Object/Map/Set prototype
	// methods NOT already in the neutral default set (join/split/replace/get/keys/
	// values/add/clear are covered there).
	jsBuiltinMethodNames = map[string]bool{
		"then": true, "catch": true, "finally": true,
		"map": true, "forEach": true, "filter": true, "reduce": true, "reduceRight": true,
		"find": true, "findIndex": true, "some": true, "every": true,
		"flat": true, "flatMap": true, "fill": true, "concat": true,
		"push": true, "shift": true, "unshift": true, "splice": true, "slice": true,
		"indexOf": true, "lastIndexOf": true, "includes": true,
		"entries": true, "has": true, "set": true, "delete": true,
		"bind": true, "call": true, "apply": true, "toString": true, "valueOf": true,
		"hasOwnProperty": true, "trim": true, "trimStart": true, "trimEnd": true,
		"padStart": true, "padEnd": true, "charAt": true, "charCodeAt": true,
		"substring": true, "substr": true, "toLowerCase": true, "toUpperCase": true,
		"match": true, "matchAll": true, "repeat": true,
	}

	// rustBuiltinMethodNames: ubiquitous Rust trait methods (Option/Result/Iterator/
	// From/Into/Deref/AsRef).
	rustBuiltinMethodNames = map[string]bool{
		"unwrap": true, "expect": true, "clone": true, "into": true, "from": true,
		"to_string": true, "to_owned": true, "as_str": true, "as_ref": true,
		"as_mut": true, "as_slice": true, "borrow": true, "borrow_mut": true,
		"iter": true, "iter_mut": true, "into_iter": true, "collect": true,
		"unwrap_or": true, "unwrap_or_else": true, "unwrap_or_default": true,
		"ok": true, "err": true, "is_some": true, "is_none": true,
		"is_ok": true, "is_err": true, "is_empty": true, "len": true,
		"contains": true, "get_mut": true, "next": true, "map_err": true,
		"and_then": true, "or_else": true,
	}

	// goBuiltinMethodNames: Go single-method interface methods (Stringer/error). An
	// unresolved receiver's String()/Error() is unresolvable garbage. Kept tiny — Go
	// method resolution is Tier-1, over-dropping would blind the graph.
	goBuiltinMethodNames = map[string]bool{
		"String": true, "Error": true,
	}
)

// isBuiltinMethodForLang reports whether calleeName is a builtin/stdlib method that
// should be dropped for a qualified-unresolved call in the given source language.
// The language-neutral default sets always apply (preserving the existing Python/
// stdlib behavior for every file); the per-language set adds names builtin to that
// specific language. lang is derived from the caller file extension.
func isBuiltinMethodForLang(lang, calleeName string) bool {
	if strongBuiltinMethodNames[calleeName] || builtinMethodNames[calleeName] {
		return true
	}
	if set, ok := builtinMethodNamesByLang[lang]; ok {
		return set[calleeName]
	}
	return false
}

// langFromFileExt maps a source file path to its language via extension — the same
// ONE-surface, extension-based dispatch used elsewhere (LSP dispatch, BuildFileMap).
// Returns "" for unknown extensions (the neutral default set still applies).
// Language-agnostic DATA, no per-repo logic.
func langFromFileExt(path string) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".py", ".pyi":
		return "python"
	case ".js", ".jsx", ".mjs", ".cjs":
		return "javascript"
	case ".ts", ".tsx", ".mts", ".cts":
		return "typescript"
	case ".go":
		return "go"
	case ".rs":
		return "rust"
	default:
		return ""
	}
}

// SetAssignmentIndex sets the global assignment index for Strategy 1.96.
func SetAssignmentIndex(idx map[string]*AssignmentMap) {
	assignmentIndex = idx
}

// SetReExportGraphIncomplete marks whether the whole-repo re-export graph was NOT
// folded into the file map before Resolve (true on the incremental `-file` path,
// where re-exports are not re-parsed). When true, B1b import-consistency abstains
// (see reExportGraphIncomplete). Callers set it explicitly per index run.
func SetReExportGraphIncomplete(v bool) {
	reExportGraphIncomplete = v
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
// constructed type NAME, across language idioms:
//
//	Python/JS/TS  : ClassName(args)            -> ClassName
//	JS/TS         : new ClassName(args)        -> ClassName
//	Go/Rust       : &Struct{fields} / Struct{} -> Struct   (composite literal)
//	Qualified     : Mod.ClassName(args)        -> ClassName (last dot-segment)
//	JS new qual   : new Mod.ClassName(args)    -> ClassName
//
// Anchored (^), closing bracket asserted by caller. The regex captures the LAST
// capitalized name before the opening bracket — this IS the type regardless of
// whether it's bare, prefixed with `new`, or qualified with a module path.
var returnShapeCtorRe = regexp.MustCompile(`^(?:new\s+)?(?:[A-Za-z_][A-Za-z0-9_]*\.)*&?([A-Z][A-Za-z0-9_]*)\s*[\({]`)

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
		// A5 (Fable 2026-07-05): mirror BuildFieldTypeIndex — strip ONLY a trailing
		// space-introduced flag (" [required]"), NEVER a generic '[' (Optional[User]/List[X]).
		// The 1.94a consumer normalizes via receiverTypeName→stripTypeWrapper at resolve time
		// (Optional[User]→User), so the generic MUST survive indexing. The old unconditional
		// strip-at-'[' truncated Optional[User]→Optional, leaving rung 1.94a dead for every
		// generic-typed param (receiverTypeName("Optional") = a nonexistent class → miss).
		if sp := strings.IndexAny(typ, " ["); sp > 0 {
			if strings.HasPrefix(typ[sp:], " ") {
				typ = strings.TrimSpace(typ[:sp])
			}
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
// assignmentTypeKnown reports whether `existing` already holds this (TypeName, ViaReturn)
// pair — the alias-fixpoint dedup so propagation converges instead of re-appending forever.
func assignmentTypeKnown(existing []VarType, typeName string, viaReturn bool) bool {
	for _, vt := range existing {
		if vt.TypeName == typeName && vt.ViaReturn == viaReturn {
			return true
		}
	}
	return false
}

func BuildAssignmentIndex(assignments []parser.AssignmentRef) map[string]*AssignmentMap {
	index := make(map[string]*AssignmentMap)
	// `b = a` alias edges (TypeName empty, AliasOf set) — collected here, resolved by the
	// fixpoint below. Keyed by file; each file is independent so the map-range is deterministic.
	type aliasEdge struct{ VarName, AliasOf, Scope string }
	aliasesByFile := make(map[string][]aliasEdge)
	for _, a := range assignments {
		if a.VarName == "" {
			continue
		}
		if a.TypeName == "" {
			if a.AliasOf != "" {
				aliasesByFile[a.File] = append(aliasesByFile[a.File], aliasEdge{a.VarName, a.AliasOf, a.Scope})
			}
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

	// PyCG assignment-graph alias fixpoint: propagate a's inferred type(s) onto b for every
	// `b = a` edge, iterating to convergence (cap 10) so `a=C(); b=a; c=b; c.m()` resolves.
	// Bounded + deterministic per file. Over-connection brakes: only propagates types that
	// ALREADY exist (never invents), respects function scope, and marks propagated copies
	// non-Confident so a direct constructor still wins last-write-wins downstream.
	for file, aliases := range aliasesByFile {
		m := index[file]
		if m == nil {
			continue // aliases but no direct types in this file → nothing to propagate onto
		}
		for iter := 0; iter < 10; iter++ {
			changed := false
			for _, e := range aliases {
				srcTypes := m.VarTypes[e.AliasOf]
				if len(srcTypes) == 0 {
					continue
				}
				for _, st := range srcTypes {
					// Local source-var write only aliases within the same function scope
					// (module-level / empty scope always eligible).
					if st.Scope != "" && e.Scope != "" && st.Scope != e.Scope {
						continue
					}
					if assignmentTypeKnown(m.VarTypes[e.VarName], st.TypeName, st.ViaReturn) {
						continue
					}
					m.VarTypes[e.VarName] = append(m.VarTypes[e.VarName], VarType{
						VarName:   e.VarName,
						TypeName:  st.TypeName,
						TypeFile:  st.TypeFile,
						Scope:     e.Scope,
						Line:      st.Line,
						Confident: false, // alias-propagated: one hop removed from the direct write
						ViaReturn: st.ViaReturn,
					})
					changed = true
				}
			}
			if !changed {
				break
			}
		}
	}
	return index
}

// ---------------------------------------------------------------------------
// B2 (GT_TYPEFLOW_FIXPOINT) + B3 (GT_FIELD_CANDIDATES) — DEPTH resolution rungs,
// each behind its own default-off env flag. OFF ⇒ resolver output byte-identical
// to today (both rungs' code paths are flag-gated). See the design headers at
// their fire sites inside Resolve.
// ---------------------------------------------------------------------------

// typeflowFixpointMaxIters bounds the B2 worklist so a pathological/cyclic alias
// chain can never loop forever (the worklist shrinks; each round resolves ≥1 new
// receiver type or the loop stops). It is ALSO the mutation lever: setting it to 1
// runs the propagation body ONCE (no re-propagation), which is exactly the neuter
// the mutation companion asserts against — a depth≥1 chain then fails to resolve.
// Production value 10 (mirrors the alias-fixpoint cap at BuildAssignmentIndex).
var typeflowFixpointMaxIters = 10

// fieldCandidatesReachabilityGate is the load-bearing import-reachability filter of
// the B3 field-based candidate set. Default true = production. The mutation companion
// flips it to false (neuters the filter) and asserts the set then wrongly admits an
// UNREACHABLE candidate — proving the filter is load-bearing.
var fieldCandidatesReachabilityGate = true

// negEvidenceRequireImportBinding is the load-bearing half of the B1 negative-evidence
// drop (F6): a bare call is dropped ONLY when its name is import-BOUND to a provably-
// external module (present in externalBoundNames). Default true = production. The B1
// mutation companion flips it to false, degrading the guard to "drop any name not resolved
// via importIndex" — which wrongly drops a NOT-imported cross-file name_match, reddening
// the mutation test and proving the import-binding requirement is load-bearing.
var negEvidenceRequireImportBinding = true

// scopeVarKey composes the per-file B2 fixpoint key. Function-local vars are scoped
// to their enclosing function (the assignment's Scope = the function name), so the
// same var name in two functions — or at module level — stays distinct.
func scopeVarKey(scope, varName string) string {
	return scope + "\x00" + varName
}

// splitReceiverMethod splits a qualified RHS/callee `recv.method` (or Rust `recv::method`)
// into a BARE single-segment receiver and the method name. Returns ok=false for a
// receiver that is itself qualified (`self.x`, `a.b`) or an empty half — the B2 fixpoint
// only chains through simple local-var receivers (correct-or-quiet; a compound receiver
// is left to the single-shot field/self rungs).
func splitReceiverMethod(q string) (recv, method string, ok bool) {
	dotIdx := strings.LastIndex(q, ".")
	colonIdx := strings.LastIndex(q, "::")
	var idx, sepLen int
	switch {
	case dotIdx > colonIdx:
		idx, sepLen = dotIdx, 1
	case colonIdx >= 0:
		idx, sepLen = colonIdx, 2
	default:
		return "", "", false
	}
	if idx <= 0 || idx+sepLen >= len(q) {
		return "", "", false
	}
	recv = q[:idx]
	method = q[idx+sepLen:]
	if recv == "" || method == "" || strings.ContainsAny(recv, ".:") {
		return "", "", false
	}
	return recv, method, true
}

// fieldImportReachable reports whether targetFile is IMPORT-reachable from callerFile:
// same file, a direct import (via the import index, which ChainReExports has already
// folded barrel re-exports into), or a whole-module/star import reaching the target's
// directory. Same-DIRECTORY (package) locality is deliberately NOT counted — B3's
// contract is "import-reachable ... via the existing import index + ChainReExports"
// (ACG field-based, Feldthaus/Sridharan/Tip ICSE 2013). When the gate var is neutered
// (mutation companion) it returns true unconditionally, exposing the unreachable member.
func fieldImportReachable(callerFile, targetFile string, importIndex map[string]map[string][]string) bool {
	if !fieldCandidatesReachabilityGate {
		return true // NEUTERED — mutation companion only; never in production (default true)
	}
	if targetFile == "" {
		return false
	}
	if targetFile == callerFile {
		return true
	}
	if callerImportsFile(callerFile, targetFile, importIndex) {
		return true
	}
	if fileImps, ok := importIndex[callerFile]; ok {
		if starFiles, ok := fileImps["*"]; ok {
			tgtDir := filepath.ToSlash(filepath.Dir(targetFile))
			for _, sf := range starFiles {
				if filepath.ToSlash(filepath.Dir(sf)) == tgtDir || sf == targetFile {
					return true
				}
			}
		}
	}
	return false
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
	resolved, _ := resolve(allCalls, nodeIDs, fileNodeIDs, callerNodeIDs, allImports, fileMap, false, nodeMeta)
	return resolved
}

// ResolveWithProvenance preserves the legacy selected-edge result while also
// returning exactly one conservative record for every input callsite.
func ResolveWithProvenance(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64,
	callerNodeIDs []int64,
	allImports []parser.ImportRef,
	fileMap map[string][]string,
	nodeMeta ...map[int64]NodeMeta,
) ([]ResolvedCall, []ResolutionCallsite) {
	return resolve(allCalls, nodeIDs, fileNodeIDs, callerNodeIDs, allImports, fileMap, true, nodeMeta)
}

func resolve(
	allCalls []parser.CallRef,
	nodeIDs map[string][]int64,
	fileNodeIDs map[string]map[string][]int64,
	callerNodeIDs []int64,
	allImports []parser.ImportRef,
	fileMap map[string][]string,
	withProvenance bool,
	nodeMeta []map[int64]NodeMeta,
) ([]ResolvedCall, []ResolutionCallsite) {
	// Build import index: file → imported name → list of candidate target files
	importIndex := buildImportIndex(allImports, fileMap)
	nameAliasIndex := buildNameAliasIndex(nodeIDs)

	// B1 negative evidence (GT_NEG_EVIDENCE, read ONCE at index time; default off).
	// When on, build the per-file set of imported BARE names whose module resolves to
	// NO indexed file (external packages). buildImportIndex records an (file,name) entry
	// ONLY when the name resolved to >=1 indexed file, so a name that is imported but
	// ABSENT from importIndex[file] is provably external-bound. "*"/"" (whole-module /
	// wildcard) are module-scope, not a bare binding — excluded. Built only under the
	// flag → nil + zero overhead when off (the Strategy-2 guard is also flag-gated), so
	// resolver output is byte-identical to today when GT_NEG_EVIDENCE is unset.
	negEvidence := os.Getenv("GT_NEG_EVIDENCE") == "1"
	var externalBoundNames map[string]map[string]bool
	if negEvidence {
		externalBoundNames = make(map[string]map[string]bool)
		// F9: project path segments (dirs/basenames/stems) — the workspace/monorepo
		// soundness set for moduleProvablyExternal. Built once, flag-gated (zero cost off).
		projectSegs := buildProjectPathSegments(fileMap)
		for _, imp := range allImports {
			if imp.ImportedName == "" || imp.ImportedName == "*" {
				continue
			}
			// Resolved to an indexed file? Then NOT external-bound (correct-or-quiet:
			// this is the "resolves to no indexed file" check the mutation companion
			// neuters — neutering it marks INTERNALLY-resolved names external and would
			// over-drop legitimate calls).
			if fe, ok := importIndex[imp.File]; ok {
				if _, resolved := fe[imp.ImportedName]; resolved {
					continue
				}
			}
			// F2 (B1 over-drop fix): an importIndex MISS is NOT proof the module is external —
			// resolveModulePath is known-incomplete, so a genuinely-internal import whose path
			// form we simply failed to map would otherwise be wrongly dropped. Require POSITIVE
			// external evidence: the module must be PROVABLY external (no indexed project file
			// could correspond to it). When externality is UNCERTAIN (relative import, or any
			// path segment names an indexed file/dir) we do NOT mark it external → do NOT drop
			// (correct-or-quiet). This keeps the genuine external-drop (lodash/extpkg) intact.
			if !moduleProvablyExternal(imp.ModulePath, fileMap, projectSegs) {
				continue
			}
			if externalBoundNames[imp.File] == nil {
				externalBoundNames[imp.File] = make(map[string]bool)
			}
			externalBoundNames[imp.File][imp.ImportedName] = true
		}
	}

	// B2/B3 flags, read ONCE at index time (default off). When unset, the fixpoint
	// pre-pass never runs and both new fire sites are skipped → resolver output is
	// byte-identical to today.
	typeflowFixpoint := os.Getenv("GT_TYPEFLOW_FIXPOINT") == "1"
	fieldCandidates := os.Getenv("GT_FIELD_CANDIDATES") == "1"

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
		// B2: build in a DETERMINISTIC order. Go map iteration is randomized, so the old
		// `for id, m := range nodeMeta[0]` last-writer-wins picked a run-dependent winner
		// when a class had >=2 members of the same name (conditional defs, overloads, cfg
		// twins) → the resolved CALLS target flipped between indexings (the ~0.5%
		// textual/wasmi drift). Sort ids by (file,line,id) and keep the FIRST (min) →
		// insertion-order-invariant, stable across runs.
		ids := make([]int64, 0, len(nodeMeta[0]))
		for id := range nodeMeta[0] {
			ids = append(ids, id)
		}
		sort.Slice(ids, func(i, j int) bool {
			a, b := nodeMeta[0][ids[i]], nodeMeta[0][ids[j]]
			if a.File != b.File {
				return a.File < b.File
			}
			if a.StartLine != b.StartLine {
				return a.StartLine < b.StartLine
			}
			return ids[i] < ids[j]
		})
		for _, id := range ids {
			m := nodeMeta[0][id]
			if m.ParentID != 0 && (m.Label == "Method" || m.Label == "Function") {
				if methodsByClass[m.ParentID] == nil {
					methodsByClass[m.ParentID] = make(map[string]int64)
				}
				if _, exists := methodsByClass[m.ParentID][m.Name]; !exists {
					methodsByClass[m.ParentID][m.Name] = id
				}
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

	// ── B2: GT_TYPEFLOW_FIXPOINT worklist pre-pass ──────────────────────────────
	// PROBLEM the single-shot ladder cannot solve: a CHAINED receiver flow
	//   a = make(); b = a.foo(); b.bar()
	// The parser records `b = a.foo()` as a ViaReturn assignment whose TypeName is the
	// QUALIFIED RHS "a.foo" — not a function node — so Strategy 1.96's return-type bridge
	// looks up nodeIDs["a.foo"] (nothing) and b's type is never inferred; b.bar() then
	// falls to name_match. The type of b only becomes knowable AFTER a's type (A) is
	// inferred and foo's return type (B) is resolved on class A — a second propagation
	// round. This pre-pass iterates the SAME facts the ladder already reads
	// (assignmentIndex + returnShapeIndex + methodsByClass/CHA) to a FIXPOINT, producing
	// file → scopeVarKey → concrete internal class name for the chained vars. The result
	// is consumed by a new rung after Strategy 1.96 (evidence "fixpoint", method
	// "type_flow", conf 0.9 — the SAME categorical FACT boundary as 1.96, never a
	// name_match promotion). Base-case var types (a) are SEEDED in round 1 and chained
	// receivers are read only from the PRIOR round's snapshot, so a depth-d chain
	// converges in d+1 rounds — which is why neutering re-propagation (cap=1) reverts a
	// depth-1 chain (the mutation companion). OFF ⇒ never computed, rung skipped ⇒
	// byte-identical. Termination: the resolved set only grows and is bounded by the var
	// count; the loop stops when a round adds nothing, with a defensive cap + loud log.
	var typeflowFixpointClass map[string]map[string]string
	// typeflowFixpointDepth[file][scopeVarKey] = the CHAIN DEPTH (0-indexed fixpoint round)
	// at which the var's concrete class was first inferred: 0 = a base seed (direct ctor /
	// bare factory), 1 = a depth-1 chain (b = a.foo(), a is a seed), 2 = a depth-2 chain,
	// etc. Carried to the consumer so a DEEPER chain's resolved call is demoted below the
	// depth-1/CERTIFIED tier (F4, non-increasing with depth). Populated alongside …Class.
	var typeflowFixpointDepth map[string]map[string]int
	if typeflowFixpoint && assignmentIndex != nil && metaMap != nil && methodsByClass != nil {
		isInternalClass := func(name string) bool {
			for _, id := range nodeIDs[name] {
				if m, ok := metaMap[id]; ok && (m.Label == "Class" || m.Label == "Struct" || m.Label == "Interface") {
					return true
				}
			}
			return false
		}
		// classReturnClass: the internal class NAME that `class.method` returns (declared
		// return type, else the constructor-return-shape fact), resolved via CHA. "" when
		// unknown or not an internal class. Deterministic (sortNodeIDsByContent picks).
		classReturnClass := func(className, method string) string {
			for _, classID := range sortNodeIDsByContent(nodeIDs[className], metaMap) {
				cm, ok := metaMap[classID]
				if !ok || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
					continue
				}
				targetID, found := lookupMethodWithInheritance(classID, method)
				if !found {
					continue
				}
				rt, rtAb := receiverTypeName(metaMap[targetID].ReturnType)
				if rtAb {
					rt = ""
				}
				if rt == "" && returnShapeIndex != nil {
					rt = returnShapeIndex[targetID]
				}
				if rt != "" && isInternalClass(rt) {
					return rt
				}
			}
			return ""
		}
		// factoryReturnClass: the internal class a bare factory callee returns (declared
		// return type, else constructor-return shape). Base case — no snapshot needed.
		factoryReturnClass := func(callee string) string {
			for _, funcID := range sortNodeIDsByContent(nodeIDs[callee], metaMap) {
				fm, ok := metaMap[funcID]
				if !ok || fm.Label == "Class" || fm.Label == "Struct" || fm.Label == "Interface" {
					continue
				}
				rt, rtAb := receiverTypeName(fm.ReturnType)
				if rtAb {
					rt = ""
				}
				if rt == "" && returnShapeIndex != nil {
					rt = returnShapeIndex[funcID]
				}
				if rt != "" && isInternalClass(rt) {
					return rt
				}
			}
			return ""
		}
		// pickVarType mirrors AssignmentMap.pick: latest binding, upgraded to the latest
		// CONFIDENT one — so a direct constructor wins over a tentative factory return.
		pickVarType := func(vts []VarType) (VarType, bool) {
			if len(vts) == 0 {
				return VarType{}, false
			}
			best := vts[len(vts)-1]
			if !best.Confident {
				for i := len(vts) - 1; i >= 0; i-- {
					if vts[i].Confident {
						best = vts[i]
						break
					}
				}
			}
			return best, true
		}
		// resolveForScope computes the concrete internal class of (scope,varName) using the
		// PRIOR round's snapshot. It scope-FILTERS am.VarTypes[varName] to `scope` FIRST
		// (F1: mirrors ResolveQualifiedCall's per-scope filter at assignments.go:118-127) so
		// the same var name reused in two functions resolves PER SCOPE — never one global
		// last-write pick keyed to a single scope. The chained-receiver lookup reads the SAME
		// scope's recv key, so a chain never crosses scopes.
		resolveForScope := func(am *AssignmentMap, scope, varName string, snap map[string]string) (string, bool) {
			var inScope []VarType
			for _, t := range am.VarTypes[varName] {
				if t.Scope == scope {
					inScope = append(inScope, t)
				}
			}
			vt, ok := pickVarType(inScope)
			if !ok {
				return "", false
			}
			if !vt.ViaReturn {
				// Direct constructor binding: TypeName IS the class.
				if c, ab := receiverTypeName(vt.TypeName); !ab && c != "" && isInternalClass(c) {
					return c, true
				}
				return "", false
			}
			if recv, method, isQual := splitReceiverMethod(vt.TypeName); isQual {
				// Chained receiver: its type must come from the PRIOR round, SAME scope.
				if rc, ok := snap[scopeVarKey(scope, recv)]; ok {
					if c := classReturnClass(rc, method); c != "" {
						return c, true
					}
				}
				return "", false
			}
			// Bare factory callee: bridge its return type (base case).
			if c := factoryReturnClass(vt.TypeName); c != "" {
				return c, true
			}
			return "", false
		}
		typeflowFixpointClass = make(map[string]map[string]string)
		typeflowFixpointDepth = make(map[string]map[string]int)
		for file, am := range assignmentIndex {
			if am == nil {
				continue
			}
			// Enumerate the DISTINCT (scope,varName) pairs deterministically (map iteration is
			// randomized): the fixpoint RESULT is order-independent, but a stable order keeps
			// the loop reproducible. Each pair is resolved independently → F1 per-scope keys.
			varNames := make([]string, 0, len(am.VarTypes))
			for v := range am.VarTypes {
				varNames = append(varNames, v)
			}
			sort.Strings(varNames)
			// F8 (same-name-method scope collapse — correct-or-quiet ABSTAIN). The parser
			// records ONLY the BARE enclosing-function name as an assignment's Scope
			// (parser.go:122; extractAssignments receives walkNode's `name`), and the
			// consumer keys its lookup by metaMap[callerID].Name — the same bare name.
			// Two same-named defs in one file (Foo.run / Bar.run, two __init__, two
			// handle) therefore COLLAPSE onto ONE scope key: a depth-1 chained receiver
			// typed in ONE method could mint a wrong 0.9 CERTIFIED type_flow/fixpoint
			// edge on the OTHER method's class (the F4 depth decay only reaches depth≥2).
			// Nothing finer than the bare name exists in the parsed facts, so the only
			// correct move is to ABSTAIN: skip every (scope,var) pair whose scope name
			// has ≥2 function/method definitions in this file. Module scope ("") is
			// structurally unique per file. Memoized per file; deterministic (a count
			// over fileNodeIDs, no ordering dependence).
			// See TestResolve_TypeflowFixpoint_SameNameScopeAbstains (red→green).
			scopeAmbiguity := map[string]bool{}
			scopeAmbiguous := func(scope string) bool {
				if scope == "" {
					return false
				}
				if amb, seen := scopeAmbiguity[scope]; seen {
					return amb
				}
				defs := 0
				for _, id := range fileNodeIDs[file][scope] {
					if m, ok := metaMap[id]; ok &&
						m.Label != "Class" && m.Label != "Struct" && m.Label != "Interface" {
						defs++
					}
				}
				amb := defs >= 2
				scopeAmbiguity[scope] = amb
				return amb
			}
			type scopeVar struct{ scope, name string }
			var pairs []scopeVar
			seenPair := map[string]bool{}
			for _, varName := range varNames {
				for _, t := range am.VarTypes[varName] {
					if scopeAmbiguous(t.Scope) {
						continue // F8: ambiguous scope name → abstain (never a guessed 0.9)
					}
					pk := scopeVarKey(t.Scope, varName)
					if !seenPair[pk] {
						seenPair[pk] = true
						pairs = append(pairs, scopeVar{t.Scope, varName})
					}
				}
			}
			sort.Slice(pairs, func(i, j int) bool {
				if pairs[i].scope != pairs[j].scope {
					return pairs[i].scope < pairs[j].scope
				}
				return pairs[i].name < pairs[j].name
			})
			snapshot := map[string]string{} // prior round's resolved var → class
			depth := map[string]int{}       // scopeVarKey → the round (0-indexed) it resolved
			converged := false
			for iter := 0; iter < typeflowFixpointMaxIters; iter++ {
				next := make(map[string]string, len(snapshot))
				for k, v := range snapshot {
					next[k] = v
				}
				changed := false
				for _, p := range pairs {
					key := scopeVarKey(p.scope, p.name)
					if _, done := snapshot[key]; done {
						continue // stable from a prior round
					}
					if cls, ok := resolveForScope(am, p.scope, p.name, snapshot); ok {
						if _, exists := next[key]; !exists {
							next[key] = cls
							depth[key] = iter
							changed = true
						}
					}
				}
				snapshot = next
				if !changed {
					converged = true
					break
				}
			}
			// F7: warn ONLY when the fixpoint is GENUINELY still resolving at the cap — a
			// further round WOULD infer a new receiver type. A chain whose depth equals the cap
			// resolves its last var ON the final permitted round (changed=true) yet is already
			// COMPLETE; the probe below proves nothing more would change, so no false "cyclic"
			// warning fires on a legitimate exactly-at-cap convergence. (The set only grows and
			// is bounded by the pair count, so a true cycle simply resolves nothing — it never
			// spins; the honest diagnosis is "chain deeper than the cap", not "cyclic".)
			if !converged {
				stillResolvable := false
				for _, p := range pairs {
					key := scopeVarKey(p.scope, p.name)
					if _, done := snapshot[key]; done {
						continue
					}
					if _, ok := resolveForScope(am, p.scope, p.name, snapshot); ok {
						stillResolvable = true
						break
					}
				}
				if stillResolvable {
					fmt.Fprintf(os.Stderr, "  [GT_TYPEFLOW_FIXPOINT] iteration cap %d reached for %s "+
						"(alias chain deeper than the cap) — stopping (partial result kept)\n", typeflowFixpointMaxIters, file)
				}
			}
			if len(snapshot) > 0 {
				typeflowFixpointClass[file] = snapshot
				typeflowFixpointDepth[file] = depth
			}
		}
	}

	var resolved []ResolvedCall
	var callsites []ResolutionCallsite
	if withProvenance {
		callsites = make([]ResolutionCallsite, len(allCalls))
		for i, call := range allCalls {
			sourceID := int64(0)
			if i < len(callerNodeIDs) {
				sourceID = callerNodeIDs[i]
			}
			callsites[i] = ResolutionCallsite{
				CallsiteOrdinal: i, SourceNodeID: sourceID, SourceLine: call.Line,
				SourceFile: call.File, Callee: call.CalleeName,
				CalleeQualified: call.CalleeQualified, DispatchState: DispatchZero,
				ParserComplete: !call.ParserIncomplete, VerificationStatus: "unverified",
			}
		}
	}
	currentCallIndex := -1
	// KEEP-BEST-CONFIDENCE dedup (replaces the old first-wins `seen` bool guard).
	// edgeSlot maps a (caller,target,"CALLS") key to its index in `resolved`. putEdge
	// records rc iff the pair is NEW or rc has STRICTLY higher confidence than the edge
	// already stored for that pair — so an early LOW-confidence resolution at one call
	// site (e.g. a cross-file name_match at line 10) never suppresses a LATER higher-
	// confidence proof of the SAME (caller,target) pair (e.g. a type_flow at line 20).
	// Ties keep the earlier (priority-ordered, therefore deterministic) resolution.
	// Mirrors the closure.go bestEdgeConf two-pass. Every CALLS emit in this loop goes
	// through putEdge, so there is exactly one (best) edge per (caller,target) pair.
	edgeSlot := make(map[edgeKey]int)
	putEdge := func(rc ResolvedCall) {
		rc.CandidateNodeIDs = candidateIDs(rc.CandidateNodeIDs, rc.SourceNodeID)
		if len(rc.CandidateNodeIDs) == 0 && rc.CandidateCount <= 1 {
			rc.CandidateNodeIDs = []int64{rc.TargetNodeID}
		}
		if withProvenance && currentCallIndex >= 0 {
			trace := &callsites[currentCallIndex]
			trace.CandidateNodeIDs = append([]int64(nil), rc.CandidateNodeIDs...)
			trace.Mechanism = rc.Method
			trace.ReceiverType = rc.ReceiverType
			trace.ReceiverOrigin = rc.EvidenceType
			trace.ParserComplete = true
			if len(trace.CandidateNodeIDs) > 1 {
				trace.DispatchState = DispatchAmbiguous
				trace.SelectedTargetNodeID = nil
				trace.VerificationStatus = "unverified"
			} else if len(trace.CandidateNodeIDs) == 1 {
				selected := rc.TargetNodeID
				trace.DispatchState = DispatchUnique
				trace.SelectedTargetNodeID = &selected
				if rc.TrustTier == "CERTIFIED" {
					trace.VerificationStatus = "verified"
				}
			} else {
				trace.DispatchState = DispatchParserIncomplete
				trace.SelectedTargetNodeID = nil
				trace.ParserComplete = false
			}
		}
		key := edgeKey{rc.SourceNodeID, rc.TargetNodeID, "CALLS"}
		if i, ok := edgeSlot[key]; ok {
			if rc.Confidence > resolved[i].Confidence {
				resolved[i] = rc
			}
			return
		}
		edgeSlot[key] = len(resolved)
		resolved = append(resolved, rc)
	}

	for i, call := range allCalls {
		currentCallIndex = i
		callerID := callerNodeIDs[i]
		if callerID == 0 {
			continue
		}

		calleeName := call.CalleeName
		var targets []int64
		var ok bool
		matchMethod := "name_match"
		evidence := "name_match"

		// Strategy 1: Same-file exact name match (only when unambiguous AND UNQUALIFIED).
		// B-2: a QUALIFIED call obj.method() must never bind a bare same-file name at
		// CERTIFIED 1.0 — the receiver-blind launder class B1 closed for imports (Strategy
		// 1.5), here for same_file. Qualified calls fall through to the receiver-typing rungs
		// (1.75 self/this via CHA lookupMethodWithInheritance, 2b field-type, 1.94a/1.95/1.96
		// typed receiver) which resolve them precisely; the multi-def branch below already
		// gates on isUnqualified for the same reason.
		if fileNodes, ok := fileNodeIDs[call.File]; ok {
			if targetIDs, ok := fileNodes[calleeName]; ok && len(targetIDs) == 1 && targetIDs[0] != callerID &&
				(call.CalleeQualified == "" || call.CalleeQualified == calleeName) {
				targetID := targetIDs[0]
				putEdge(ResolvedCall{
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
					// 0.6 = CANDIDATE: locality is strong, but WHICH same-named
					// local definition is the target is not certain → not CERTIFIED.
					putEdge(ResolvedCall{
						SourceNodeID:     callerID,
						TargetNodeID:     best,
						SourceLine:       call.Line,
						SourceFile:       call.File,
						Method:           "same_file",
						Confidence:       0.6,
						CandidateCount:   len(targetIDs),
						CandidateNodeIDs: targetIDs,
						TrustTier:        tierFor(0.6),
						EvidenceType:     "same_file_ambiguous",
					})
					continue
				}
			}
		}

		// B1: a QUALIFIED call obj.method() must never bind a BARE imported symbol
		// (`from lib.http import get`; cache.get() != lib.http.get). Compute this BEFORE
		// Strategy 1.5 so the receiver-blind bare-name lookups (Block A/C below) are
		// skipped for qualified calls — only the whole-module "*" (unqualified) case and
		// the package-alias branch are receiver-safe. Moved up from Strategy 1.9.
		qualifiedUnresolved := call.CalleeQualified != "" && call.CalleeQualified != calleeName

		// Strategy 1.5: Import-verified cross-file resolution
		// H6 fix: collect all matching imported targets, pick best (prefer same dir)
		if fileImports, ok := importIndex[call.File]; ok {
			var importCandidates []int64

			// Check specific imports first, then "*" wildcard (whole-module require).
			// Specific match wins: destructured `const {error} = require('./args')`
			// creates a specific entry for "error". Whole-module `const x = require('./args')`
			// creates a "*" entry so ANY function in args.js is import-reachable.
			// B1: a qualified receiver call cannot bind a bare imported name; skip the
			// bare-name lookups entirely and defer to the package-alias branch + the
			// receiver-typing rungs (correct-or-quiet). Unqualified calls are unchanged.
			bareNameLookups := []string{calleeName, "*"}
			if qualifiedUnresolved {
				bareNameLookups = nil
			}
			for _, lookupName := range bareNameLookups {
				if candidateFiles, ok := fileImports[lookupName]; ok {
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
				if len(importCandidates) > 0 {
					break
				}
			}

			// Go package-qualified calls: "auth.Login" → look up "auth" in imports,
			// then find "Login" in the target files.
			if len(importCandidates) == 0 && call.CalleeQualified != "" && call.CalleeQualified != calleeName {
				if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {
					pkgAlias := call.CalleeQualified[:dotIdx]
					funcName := call.CalleeQualified[dotIdx+1:]
					// A1 (Fable 2026-07-05): a LOCAL variable that shadows the imported module name
					// (`import config` + a caller-scope `config = load_config()`) means `config.get()`
					// targets the LOCAL, not the module — resolving via the import mints a WRONG
					// CERTIFIED edge. Skip the pkg-alias mint when pkgAlias is assigned in the caller's
					// scope (or module level) and defer to the receiver-typing rungs. PyCG scope rule.
					pkgAliasShadowed := false
					if fa, ok := assignmentIndex[call.File]; ok && fa != nil {
						callerScopeA1 := ""
						if cm, ok := nodeMeta[0][callerID]; ok {
							callerScopeA1 = cm.Name
						}
						for _, vt := range fa.VarTypes[pkgAlias] {
							if vt.Scope == callerScopeA1 || vt.Scope == "" {
								pkgAliasShadowed = true
								break
							}
						}
					}
					if candidateFiles, ok := fileImports[pkgAlias]; ok && !pkgAliasShadowed {
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

			// Check wildcard imports (UNQUALIFIED only — a qualified receiver call must
			// not bind a bare name via the whole-module wildcard; B1).
			if len(importCandidates) == 0 && !qualifiedUnresolved {
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
				putEdge(ResolvedCall{
					SourceNodeID:     callerID,
					TargetNodeID:     bestTarget,
					SourceLine:       call.Line,
					SourceFile:       call.File,
					Method:           "import",
					Confidence:       conf,
					CandidateCount:   len(importCandidates),
					CandidateNodeIDs: importCandidates,
					TrustTier:        tierFor(conf),
					EvidenceType:     evidence,
				})
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
				callerMeta, hasMeta := nodeMeta[0][callerID]
				// self/this/Self (Python/JS/TS/Java/Rust) OR the Go method's receiver
				// VARIABLE name (`func (r *T) M(){ r.helper() }` — r is the Go analogue
				// of self/this, supplied structurally in NodeMeta.ReceiverName by the
				// signature parser). A call whose qualifier IS the caller's own receiver
				// var resolves against the caller's type methods (Strategy 1.75) — a
				// receiver self-call is an unambiguous FACT, not a name_match guess.
				isReceiverSelf := qualifier == "self" || qualifier == "this" || qualifier == "Self" ||
					(hasMeta && callerMeta.ReceiverName != "" && qualifier == callerMeta.ReceiverName)
				if isReceiverSelf {
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
							// Provenance: the receiver's static type is the caller's ENCLOSING
							// class (self/this/Self, or the Go named receiver). For an inherited
							// method the target lives on a parent, but the receiver IS the child.
							putEdge(ResolvedCall{
								SourceNodeID:   callerID,
								TargetNodeID:   targetID,
								SourceLine:     call.Line,
								SourceFile:     call.File,
								Method:         method,
								Confidence:     conf,
								CandidateCount: 1,
								TrustTier:      tierFor(conf),
								EvidenceType:   evidence,
								ReceiverType:   nodeMeta[0][callerMeta.ParentID].Name,
							})
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
		// qualifiedUnresolved computed above (before Strategy 1.5) for the B1 receiver-blind guard.
		// T2 (builtins): a qualified call obj.method() whose receiver never resolves to an
		// internal class + builtin/stdlib method name = a builtin call (os.path.join,
		// str.split, dict.get) — DROP rather than guess (application-centered — JARVIS
		// 2023 / PyCG ICSE 2021). #B5 reorder: this predicate no longer short-circuits
		// HERE — the receiver-PROVING rungs (1.93/1.94a/1.95/1.96/1.97) get first
		// attempt; if one of them resolves the receiver to an internal class, the call
		// IS internal and must not be dropped. The drop/demote moved to the last-chance
		// block after 1.98 and still guards the receiver-UNPROVEN rungs (1.94/1.98).
		builtinQualified := qualifiedUnresolved && isBuiltinMethodForLang(langFromFileExt(call.File), calleeName)

		// B1 NEGATIVE EVIDENCE (GT_NEG_EVIDENCE, default off): an UNQUALIFIED bare call
		// whose name is lexically BOUND by an import in THIS file whose module resolves to
		// NO indexed project file (`import {merge} from 'lodash'; merge(a,b)`,
		// `from extpkg import y; y()`) is PROVABLY external — every remaining rung would
		// mint a name_match onto a same-named PROJECT symbol (Strategy 1.9 unique-name
		// demote OR Strategy 2 multi-candidate), a WRONG fact, not merely unproven. Drop it
		// (correct-or-quiet) rather than launder. Same move as the stdlib-shadow fix
		// (55ab30eb) extended from QUALIFIED to BARE names. Placed BEFORE Strategy 1.9 so it
		// covers BOTH the unique (1.9) and multi-candidate (Strategy 2) name_match paths.
		// Guards (never over-drop): unqualified only; the name must be external-BOUND in
		// THIS file (imported + unresolved) so a plain cross-file name_match that is NOT
		// imported is untouched; NEVER when the name also has a same-file LOCAL definition
		// (Strategy 1 already resolved those, but the guard is explicit). OFF → skipped →
		// byte-identical to today.
		if negEvidence && !qualifiedUnresolved {
			drop := false
			if ext, hasFile := externalBoundNames[call.File]; hasFile && ext[calleeName] {
				drop = true // import-BOUND to a provably-external module (production path)
			} else if !negEvidenceRequireImportBinding {
				// NEUTERED (F6 mutation companion ONLY; default true keeps this dead in
				// production): degrade the guard to "drop any name not resolved via
				// importIndex", dropping the load-bearing "must be import-bound" half. This
				// wrongly drops a NOT-imported cross-file name_match, proving that half matters.
				if fe, hasImports := importIndex[call.File]; !hasImports || len(fe[calleeName]) == 0 {
					drop = true
				}
			}
			if drop {
				if localDefs := fileNodeIDs[call.File][calleeName]; len(localDefs) == 0 {
					continue
				}
			}
		}

		// B1b IMPORT-CONSISTENCY (GT_NEG_EVIDENCE, default off): B1's missing half. When
		// the bare callee name is import-BOUND in THIS file to a resolved file-set F* but
		// NO same-named node lives in F* (a barrel / self-package re-export whose flagship
		// export is a generic const the indexer cannot node-ify — arktype
		// `import {type} from "arktype"`, whose real `type` produces no node), every
		// remaining rung mints a cross-file name_match onto a same-named node in a file the
		// import index just PROVED is not the callee's home. WRONG fact, not merely
		// unproven — drop (correct-or-quiet). Placed BEFORE Strategy 1.9 so it covers BOTH
		// the unique (1.9) and multi-candidate (Strategy 2) name_match paths. Guards (never
		// over-drop, all inside importShadowsButTargetAbsent): unqualified only; name must
		// be import-bound to a NON-EMPTY F*; ANY candidate in F* → keep; a framework global
		// / implicit-this call is not import-bound by name → untouched; a same-file LOCAL
		// def → untouched (checked here). OFF → skipped → byte-identical to today.
		//
		// SOUNDNESS PRECONDITION — F* must be COMPLETE for the DROP. B1b's drop is only correct
		// when the imported file-set F* already includes re-export SOURCES (ChainReExports folds
		// them into fileMap before Resolve on the full-index path). Where that fold did NOT run —
		// the incremental `-file` reindex (main.go: whole-repo re-exports are not re-parsed) —
		// F* is a bare direct-module resolution and a legitimately re-exported def looks
		// "absent", so a DROP would delete a true edge. reExportGraphIncomplete switches the
		// action from DROP to DEMOTE there (sub-floor name_match 0.2 — see below): the import
		// binding is still contradicting evidence (suspicion stands) but absence is no longer
		// proof (drop voided). NB a plain abstain would be WRONG — the call would fall to
		// Strategy 1.9 and re-mint at verified_unique 0.95 CERTIFIED whenever provenance holds
		// (same-dir coincidental name), laundering a fact on the agent's edited files (Fable
		// Finding 1). KNOWN residual (full path, rare):
		// a cross-package `export {x} from "@scope/pkg"` that ChainReExports could not map to
		// a local file leaves F* = {barrel} → x's real (folded-elsewhere) def is absent → a
		// legitimate call may still be dropped; bounded by ChainReExports' coverage (named /
		// `export *` / default / CJS / Python __init__ / Rust `pub use`, fixpoint depth 16).
		// NIT (defensible): the candidate set is nodeIDs[calleeName] (exact name); Strategy 2
		// has a nameAliasIndex fallback B1b does not consult, so a ≤0.5-confidence ALIAS edge
		// in F* could be suppressed unseen — dropping a sub-threshold fuzzy edge is quiet, not
		// a false fact, so it stays out of scope here.
		if negEvidence && !qualifiedUnresolved {
			if localDefs := fileNodeIDs[call.File][calleeName]; len(localDefs) == 0 {
				cands := nodeIDs[calleeName]
				if importShadowsButTargetAbsent(call.File, calleeName, cands, callerID, importIndex, metaMap) {
					if !reExportGraphIncomplete {
						// FULL-INDEX path: F* is complete (ChainReExports folded re-export
						// sources before Resolve), so target-absence is PROOF the import
						// disproves every same-named candidate — DROP (correct-or-quiet).
						continue
					}
					// INCREMENTAL (`-file`) path: F* is a bare direct-module resolution
					// (re-exports were NOT re-parsed / folded), so absence is NOT proof —
					// a legitimately re-exported def could be the "missing" target. DEMOTE
					// rather than DROP (which would delete a true edge) or ABSTAIN (which
					// would let the call fall to Strategy 1.9 and re-mint at verified_unique
					// 0.95 CERTIFIED whenever provenance happens to hold — e.g. a same-dir
					// coincidental same-name — laundering a fact the import binding
					// contradicts, on exactly the files the agent edits). Mint each candidate
					// at the sub-delivery-floor name_match 0.2 with an honest label: the
					// import binding is still contradicting evidence, so the SUSPICION stands
					// even though the DROP is voided. conf 0.2 < the 0.5 consumer floor → no
					// fact reaches the agent; the edge STRUCTURE is preserved so the next full
					// bake restores true trust (import 1.0) or re-drops it. Strictly better
					// than both the full-drop and the abstain. Deterministic: cands is a slice.
					nonSelf := 0
					for _, tid := range cands {
						if tid != callerID {
							nonSelf++
						}
					}
					minted := false
					for _, tid := range cands {
						if tid == callerID {
							continue
						}
						putEdge(ResolvedCall{
							SourceNodeID:     callerID,
							TargetNodeID:     tid,
							SourceLine:       call.Line,
							SourceFile:       call.File,
							Method:           "name_match",
							Confidence:       0.2,
							CandidateCount:   nonSelf,
							CandidateNodeIDs: cands,
							TrustTier:        tierFor(0.2),
							EvidenceType:     "name_match_import_shadow_unverified",
						})
						minted = true
					}
					if minted {
						continue
					}
					// No non-self candidate to demote → fall through (nothing to mint).
				}
			}
		}

		// Strategy 1.9 fires here ONLY for UNQUALIFIED calls (the ACG/ECOOP 2022
		// globally-unique-name property holds for bare names). Qualified calls go
		// through the receiver-typing strategies (1.75/1.93/1.94/1.95/1.96/1.97/1.98)
		// which prove the receiver type before promoting.
		if !qualifiedUnresolved {
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
					if !provenanceOK {
						// Demote shape mirrors the qualified-unresolved last-chance demote
						// (conf 0.2, sub-SPECULATIVE) so tierFor agrees and Strategy 2 does
						// not re-CERTIFY this single candidate at name_match conf 0.9.
						putEdge(ResolvedCall{
							SourceNodeID:   callerID,
							TargetNodeID:   targetID,
							SourceLine:     call.Line,
							SourceFile:     call.File,
							Method:         "name_match",
							Confidence:     0.2,
							CandidateCount: 1,
							TrustTier:      tierFor(0.2),
							EvidenceType:   "name_match_verified_unique_no_provenance",
						})
						continue
					}
					putEdge(ResolvedCall{
						SourceNodeID:   callerID,
						TargetNodeID:   targetID,
						SourceLine:     call.Line,
						SourceFile:     call.File,
						Method:         "verified_unique",
						Confidence:     0.95,
						CandidateCount: 1,
						TrustTier:      tierFor(0.95),
						EvidenceType:   "name_unique",
					})
					continue
				}
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
													putEdge(ResolvedCall{
														SourceNodeID:   callerID,
														TargetNodeID:   targetID,
														SourceLine:     call.Line,
														SourceFile:     call.File,
														Method:         "import_type",
														Confidence:     0.95,
														CandidateCount: 1,
														TrustTier:      tierFor(0.95),
														EvidenceType:   "import_scoped_type",
														ReceiverType:   cm.Name,
													})
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
								className2b, ab2b := receiverTypeName(declaredType2b)
								if !ab2b && className2b != "" {
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
											putEdge(ResolvedCall{
												SourceNodeID:   callerID,
												TargetNodeID:   targetID,
												SourceLine:     call.Line,
												SourceFile:     call.File,
												Method:         "type_flow",
												Confidence:     0.9,
												CandidateCount: 1,
												TrustTier:      tierFor(0.9),
												EvidenceType:   "field_type",
												ReceiverType:   className2b,
											})
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
							className194a, ab194a := receiverTypeName(declaredType)
							if !ab194a && className194a != "" {
								if singleID, abA3 := resolveInternalClassByName(className194a, call.File, nodeIDs, nodeMeta[0], fileNodeIDs, importIndex); !abA3 && singleID != 0 {
									// A3: iterate the ONE import-disambiguated class (or abstain) — never a
									// content-first pick among N same-named internal classes.
									for _, classID := range []int64{singleID} {
										cm, hasMeta := nodeMeta[0][classID]
										if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
											continue
										}
										if targetID, found := lookupMethodWithInheritance(classID, methodName194a); found && targetID != callerID {
											putEdge(ResolvedCall{
												SourceNodeID:   callerID,
												TargetNodeID:   targetID,
												SourceLine:     call.Line,
												SourceFile:     call.File,
												Method:         "type_flow",
												Confidence:     0.9,
												CandidateCount: 1,
												TrustTier:      tierFor(0.9),
												EvidenceType:   "param_type",
												ReceiverType:   className194a,
											})
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
				// Receiver-type guard (B1-#6): if the receiver's type is KNOWN via the
				// assignment index (`x = ClassName()`, or an alias chain b=a / c=b), do NOT
				// emit a receiver-BLIND name-uniqueness guess here — defer to Strategy 1.96,
				// which resolves the ACTUAL receiver class. 1.94's own contract is "no receiver
				// proof (name-uniqueness only)"; when proof exists downstream, it must yield,
				// else a 2-3-class method resolves to the wrong (first) class regardless of the
				// receiver's real type. Narrow blast radius: only fires on ambiguous methods
				// whose receiver is typed.
				receiverTypeKnown := false
				if assignmentIndex != nil {
					if fa, ok := assignmentIndex[call.File]; ok {
						callerScope194 := ""
						if cm, ok := nodeMeta[0][callerID]; ok {
							callerScope194 = cm.Name
						}
						if _, _, _, found, _ := fa.ResolveQualifiedCall(qualifier194, methodName194, callerScope194); found {
							receiverTypeKnown = true
						}
					}
				}
				if !isSelfLike && !qualifierIsClass && !receiverTypeKnown {
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
						var bestTarget194 int64
						var candidateTargets194 []int64
						for _, classID := range classIDs194 {
							if methods, ok := methodsByClass[classID]; ok {
								if targetID, ok := methods[methodName194]; ok && targetID != callerID {
									candidateTargets194 = append(candidateTargets194, targetID)
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
							putEdge(ResolvedCall{
								SourceNodeID:     callerID,
								TargetNodeID:     bestTarget194,
								SourceLine:       call.Line,
								SourceFile:       call.File,
								Method:           "impl_method",
								Confidence:       conf194,
								CandidateCount:   len(candidateTargets194),
								CandidateNodeIDs: candidateTargets194,
								TrustTier:        tierFor(conf194),
								EvidenceType:     "single_implementor",
							})
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
					if singleID, abA3 := resolveInternalClassByName(qualifier, call.File, nodeIDs, nodeMeta[0], fileNodeIDs, importIndex); !abA3 && singleID != 0 {
						// A3: iterate the ONE import-disambiguated class (or abstain) — never a
						// content-first pick among N same-named internal classes.
						for _, classID := range []int64{singleID} {
							cm, hasMeta := nodeMeta[0][classID]
							if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
								continue
							}
							if methods, ok := methodsByClass[classID]; ok {
								if targetID, ok := methods[methodName]; ok && targetID != callerID {
									// PROVENANCE GATE (Fable RS5): 1.95 matched `qualifier` to a class
									// NAME anywhere in the repo — it does NOT prove the receiver. For a
									// `.`-qualified call the qualifier is a VARIABLE that merely shares a
									// name with the class (a lowercase class, or an internal class
									// shadowing a stdlib module → json.load / session.get), so a flat 0.9
									// CERTIFIED is a laundered guess. `::` (Rust) IS syntactically
									// type-qualified. Require same-file OR caller-imports-the-class-file
									// (mirrors the Strategy-1.9 provenance gate) before CERTIFIED; a
									// `.`-qualified match without provenance stays CANDIDATE (0.6).
									conf195 := 0.9
									evid195 := "type_qualified"
									if sep195 == 1 && !(sameDirFile(call.File, cm.File) ||
										callerImportsFile(call.File, cm.File, importIndex)) {
										conf195 = 0.6
										evid195 = "type_qualified_unproven"
									}
									// Provenance ONLY on the receiver-PROVEN branch (same-file/imported,
									// 0.9). The unproven `.`-qualified match (0.6) is a name-shared guess,
									// not a proven receiver → no receiver tag (correct-or-quiet).
									recvType195 := ""
									if evid195 == "type_qualified" {
										recvType195 = cm.Name
									}
									putEdge(ResolvedCall{
										SourceNodeID:   callerID,
										TargetNodeID:   targetID,
										SourceLine:     call.Line,
										SourceFile:     call.File,
										Method:         "type_flow",
										Confidence:     conf195,
										CandidateCount: 1,
										TrustTier:      tierFor(conf195),
										EvidenceType:   evid195,
										ReceiverType:   recvType195,
									})
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
				// Handle self.x.method() / this.x.method() / super.x.method() → strip the
				// leading receiver keyword to get the field "x". super. was previously
				// NOT stripped (P2-8): `super.field.method()` left qualifier as
				// "super.field", which matched no assignment and mis-scoped the call
				// (or fell through to a name guess) instead of resolving field x's type.
				// A2 (Fable 2026-07-05): KEEP the self./this. prefix so ResolveQualifiedCall ->
				// Lookup targets the object-FIELD key ("self.x"), object-scoped across methods.
				// Stripping to bare "x" let Lookup match a fn-local shadow (`client = Mock()`) and
				// resolve self.client against the WRONG receiver. super. IS still stripped: its
				// field lives on the parent, there is no "super.x" assignment key (P2-8).
				if strings.HasPrefix(qualifier, "super.") {
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
						if typeName, _, viaReturn, found, scopeProven := fileAssignments.ResolveQualifiedCall(qualifier, methodName, callerScope); found {
							// Determine the receiver CLASS name. Direct constructor: typeName
							// is the class. Return-type chain (x = factory()): typeName is the
							// callee — bridge through its declared return type.
							className := ""
							if viaReturn {
								if funcIDs, ok := nodeIDs[typeName]; ok {
									for _, funcID := range sortNodeIDsByContent(funcIDs, nodeMeta[0]) {
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
										rt, rtAb := receiverTypeName(fm.ReturnType)
										if rtAb {
											rt = ""
										}
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
								if c, ab := receiverTypeName(typeName); !ab {
									className = c
								}
							}
							if className != "" {
								// Look up the class in nodeIDs, then find the method via CHA.
								if singleID, abA3 := resolveInternalClassByName(className, call.File, nodeIDs, nodeMeta[0], fileNodeIDs, importIndex); !abA3 && singleID != 0 {
									// A3: iterate the ONE import-disambiguated class (or abstain) — never a
									// content-first pick among N same-named internal classes.
									for _, classID := range []int64{singleID} {
										cm, hasMeta := nodeMeta[0][classID]
										if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
											continue
										}
										if targetID, found := lookupMethodWithInheritance(classID, methodName); found && targetID != callerID {
											// RS6: an assignment resolved OUT of the caller's scope (a
											// DIFFERENT function's write, or scope unknown) does NOT prove
											// the receiver type at THIS call — demote below the CERTIFIED
											// fact floor to CANDIDATE instead of minting a flat 0.9.
											conf196 := 0.9
											evid196 := "assignment_tracked"
											if !scopeProven {
												conf196 = 0.6
												evid196 = "assignment_tracked_crossscope"
											}
											// Provenance ONLY when the receiver type is proven in the
											// caller's OWN scope. A type borrowed from a DIFFERENT function's
											// write (crossscope, 0.6) is not proven here → no receiver tag.
											recvType196 := ""
											if scopeProven {
												recvType196 = className
											}
											putEdge(ResolvedCall{
												SourceNodeID:   callerID,
												TargetNodeID:   targetID,
												SourceLine:     call.Line,
												SourceFile:     call.File,
												Method:         "type_flow",
												Confidence:     conf196,
												CandidateCount: 1,
												TrustTier:      tierFor(conf196),
												EvidenceType:   evid196,
												ReceiverType:   recvType196,
											})
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

		// Strategy 1.96-fixpoint (B2, GT_TYPEFLOW_FIXPOINT): consume the worklist result.
		// Reached ONLY when every higher-priority rung (1.75/1.93/2b/1.94a/1.94/1.95/1.96)
		// failed to resolve this qualified call, i.e. the chained residual. If the caller
		// scope's receiver var has an inferred concrete class in typeflowFixpointClass,
		// resolve the method on it via the SAME CHA primitive — emit a type_flow fact with
		// evidence "fixpoint". A depth-1 chained hop carries conf 0.9/CERTIFIED (parity with
		// single-hop 1.96); a deeper chain (receiver type inferred at round >=2) is demoted to
		// CANDIDATE (F4). OFF ⇒ typeflowFixpointClass is nil ⇒ this whole block is skipped ⇒
		// byte-identical.
		if typeflowFixpoint && typeflowFixpointClass != nil && metaMap != nil && methodsByClass != nil &&
			call.CalleeQualified != "" && call.CalleeQualified != calleeName {
			if fileVars, ok := typeflowFixpointClass[call.File]; ok {
				// F5: split the receiver on `.` OR `::` via the SAME primitive the pre-pass
				// uses (splitReceiverMethod), so a Rust `::`-qualified chain the pre-pass
				// populated is actually consumed here (the old `.`-only split silently
				// dropped it). Rejects a compound receiver (`a.b`, `self.x`) — those are the
				// single-shot rungs' domain — exactly as the prior guard did.
				if qualifierFx, methodFx, okFx := splitReceiverMethod(call.CalleeQualified); okFx &&
					qualifierFx != "self" && qualifierFx != "this" {
					callerScopeFx := ""
					if cm, ok := metaMap[callerID]; ok {
						callerScopeFx = cm.Name
					}
					vkeyFx := scopeVarKey(callerScopeFx, qualifierFx)
					if className := fileVars[vkeyFx]; className != "" {
						// F4: chain-depth decay. A depth-1 chained hop keeps 0.9/CERTIFIED
						// (parity with single-hop 1.96); a receiver whose type was inferred at
						// a DEEPER round (>=2) is demoted to CANDIDATE (0.6) — strictly below
						// CERTIFIED, non-increasing with depth (no tuned per-depth float).
						fxConf := 0.9
						if dm := typeflowFixpointDepth[call.File]; dm != nil {
							if d, okD := dm[vkeyFx]; okD && d >= 2 {
								fxConf = 0.6
							}
						}
						// Provenance ONLY on the depth-1 chained hop (0.9, parity with single-hop
						// 1.96). A receiver whose type was inferred at a DEEPER round (0.6) is not
						// proven at this call site → no receiver tag (correct-or-quiet).
						recvTypeFx := ""
						if fxConf == 0.9 {
							recvTypeFx = className
						}
						for _, classID := range a3SingleClass(className, call.File, nodeIDs, metaMap, fileNodeIDs, importIndex) {
							cm, hasMeta := metaMap[classID]
							if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
								continue
							}
							if targetID, found := lookupMethodWithInheritance(classID, methodFx); found && targetID != callerID {
								putEdge(ResolvedCall{
									SourceNodeID:   callerID,
									TargetNodeID:   targetID,
									SourceLine:     call.Line,
									SourceFile:     call.File,
									Method:         "type_flow",
									Confidence:     fxConf,
									CandidateCount: 1,
									TrustTier:      tierFor(fxConf),
									EvidenceType:   "fixpoint",
									ReceiverType:   recvTypeFx,
								})
								goto nextCall
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
						for _, funcID := range sortNodeIDsByContent(funcIDs, nodeMeta[0]) {
							fm, hasMeta := nodeMeta[0][funcID]
							if !hasMeta {
								continue
							}
							if fm.Label == "Class" || fm.Label == "Struct" || fm.Label == "Interface" {
								continue
							}
							// Strip common wrappers: Optional[X] → X, list[X] → X, *X → X.
							retType, retAb := receiverTypeName(fm.ReturnType)
							if retAb {
								retType = ""
							}
							// GAP C fallback: no declared return type -> use the CONSTRUCTOR
							// return shape (factory whose body returns `ClassName(...)` /
							// `&Struct{...}`). Constructor-only fact; never data_flow.
							if retType == "" && returnShapeIndex != nil {
								retType = returnShapeIndex[funcID]
							}
							if retType == "" {
								continue
							}
							if singleID, abA3 := resolveInternalClassByName(retType, call.File, nodeIDs, nodeMeta[0], fileNodeIDs, importIndex); !abA3 && singleID != 0 {
								// A3: iterate the ONE import-disambiguated class (or abstain) — never a
								// content-first pick among N same-named internal classes.
								for _, classID := range []int64{singleID} {
									cm, hasMeta := nodeMeta[0][classID]
									if !hasMeta || (cm.Label != "Class" && cm.Label != "Struct" && cm.Label != "Interface") {
										continue
									}
									if methods, ok := methodsByClass[classID]; ok {
										if targetID, ok := methods[methodName]; ok && targetID != callerID {
											putEdge(ResolvedCall{
												SourceNodeID:   callerID,
												TargetNodeID:   targetID,
												SourceLine:     call.Line,
												SourceFile:     call.File,
												Method:         "return_type",
												Confidence:     0.85,
												CandidateCount: 1,
												TrustTier:      tierFor(0.85),
												EvidenceType:   "return_type_flow",
												ReceiverType:   cm.Name,
											})
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
			if classID, ok := uniqueMethodClass[calleeName]; ok {
				if methods, ok := methodsByClass[classID]; ok {
					if targetID, ok := methods[calleeName]; ok && targetID != callerID {
						putEdge(ResolvedCall{
							SourceNodeID:   callerID,
							TargetNodeID:   targetID,
							SourceLine:     call.Line,
							SourceFile:     call.File,
							Method:         "unique_method",
							Confidence:     0.6,
							CandidateCount: 1,
							TrustTier:      tierFor(0.6),
							EvidenceType:   "unique_method_class",
						})
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
					// Single candidate, qualified call, all receiver-typing strategies
					// exhausted. Two cases:
					//   (a) target is in the SAME file or imported by the caller file
					//       → internal, reachable, single candidate → CANDIDATE 0.6
					//       (receiver-type UNPROVEN → NOT CERTIFIED verified_unique)
					//   (b) target is in a file the caller doesn't import and isn't
					//       same-dir → likely stdlib/external → demote conf=0.2
					// This replaces the blanket demote that penalized internal single-
					// candidate calls (fastify: 1277 calls to the ONLY 'fastify' func).
					conf := 0.2
					method := "name_match"
					evidence := "name_match_qualified_unresolved"
					if metaMap != nil {
						tm := metaMap[targetID]
						if tm.File != "" {
							// The target is internal (has a file) and is the ONLY
							// candidate. Promote if caller imports that file OR
							// if caller imports ANY file from the same package.
							isImported := tm.File == call.File ||
								callerImportsFile(call.File, tm.File, importIndex)
							// Wildcard: if caller has a "*" import (whole-module
							// require) that resolved to ANY file in the target's
							// directory, the target is reachable.
							if !isImported {
								if fileImps, ok := importIndex[call.File]; ok {
									if starFiles, ok := fileImps["*"]; ok {
										tgtDir := filepath.ToSlash(filepath.Dir(tm.File))
										for _, sf := range starFiles {
											if filepath.ToSlash(filepath.Dir(sf)) == tgtDir || sf == tm.File {
												isImported = true
												break
											}
										}
									}
								}
							}
							if isImported {
								// Reachable + single candidate, but the receiver TYPE is
								// UNPROVEN (all typing rungs failed) → CANDIDATE (0.6), NOT
								// CERTIFIED verified_unique. Import/dir reachability is not
								// receiver-type proof; mirror the sibling receiver-unproven
								// rungs (1.94 impl_method / 1.98 unique_method = 0.6).
								conf = 0.6
								evidence = "name_match_qualified_imported"
							}
						}
					}
					putEdge(ResolvedCall{
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
					continue
				}
				// Multi-candidate qualified-unresolved: the receiver type was NOT
				// proven by any typing rung AND N internal methods share this name —
				// strictly MORE ambiguous than the single-candidate case above. Today
				// it falls through to Strategy 2 and mints a PLAIN name_match onto an
				// arbitrary same-named target, indistinguishable from a bare name match.
				// Correct-or-quiet by REACHABILITY (structural, NOT a builtin name-list,
				// so a genuine internal method is never blind-dropped): keep only the
				// candidates reachable from the caller (same-file / imported / same-dir /
				// star-import). NONE reachable → every target is an unrelated cross-repo
				// guess. PREFER a reachable target; else keep best-of-all (no drop — Fable #4). Mint
				// at a SPECULATIVE 0.2 floor (see the conf assignment below), with the honest
				// EvidenceType name_match_qualified_unresolved (mirrors the single-
				// candidate branch) so it never poses as a bare fact, the edge-truth
				// metric can tell it apart, and the demand-driven LSP pass can still
				// resolve it. Skipped when metaMap is absent (fall through, unchanged).
				if len(candidates) > 1 && metaMap != nil {
					// ── B3: field-based CANDIDATE SET (GT_FIELD_CANDIDATES) ──────────────
					// The receiver type is UNPROVEN (every typing rung failed) and N internal
					// methods share this name. Today the tail mints ONE arbitrary name_match.
					// ACG field-based (Feldthaus/Sridharan/Tip, ICSE 2013): the honest answer
					// is the CANDIDATE SET, not one guess. set = candidates ∩ import-reachable
					// (via the import index + ChainReExports) ∩ is_exported. Emitted as a NEW
					// categorical resolution_method "field_based" with candidate_count=K, tier
					// capped at CANDIDATE (never a single-target CERTIFIED fact, never a
					// name_match laundered as fact). Empty intersection → drop (correct-or-
					// quiet). K==1 with the 1.9 provenance gate → promote ONE notch (0.5→0.6),
					// still strictly below any compiler/type-verified tier. OFF ⇒ the whole
					// block is skipped and the single-name_match path below runs unchanged
					// (byte-identical). See TestResolve_FieldCandidates_*.
					if fieldCandidates {
						var set []int64
						for _, tid := range candidates { // candidates already excludes callerID
							tm := metaMap[tid]
							if tm.File == "" || !tm.IsExported {
								continue // non-exported def is not a legal cross-file receiver target
							}
							if fieldImportReachable(call.File, tm.File, importIndex) {
								set = append(set, tid)
							}
						}
						if len(set) == 0 {
							continue // empty intersection → drop, never fall back to a name_match guess
						}
						set = sortNodeIDsByContent(set, metaMap) // deterministic emit order
						// AMBIGUITY GRADIENT (Fable RS1): field_based minted a FLAT 0.5 for ANY K,
						// sitting EXACTLY at the closure/fact floor (_CLOSURE_MIN_CONFIDENCE=0.5) —
						// so a K=205 fan-out entered "verified reach"/blast-radius identically to a
						// K=2 one (measured: 74% of a real Rust graph's conf≥0.5 CALLS were flat-0.5
						// field_based). EVERY sibling surface decays a high-candidate name to
						// sub-floor (computeConfidence >5→0.2, dataFlowConfidence >5→0.0). Mirror the
						// documented name_match ladder: K≤2 stays at the 0.5 CANDIDATE floor,
						// K≤5→0.4, K>5→0.2 (below the traversal floor — the edge is still STORED so
						// the demand-driven LSP pass can resolve it, but a wide guess no longer poses
						// as reachable fact). K==1 keeps its 1.9 provenance promotion.
						conf := 0.5 // CANDIDATE floor: reachable+exported but K-way ambiguous
						evidence := "field_based"
						switch {
						case len(set) == 1:
							// Intersection collapsed to one reachable+exported candidate. The 1.9
							// provenance gate (same-dir OR caller-imports-file) promotes ONE notch,
							// still CANDIDATE (< the 0.9 CERTIFIED / compiler-verified tier).
							tm := metaMap[set[0]]
							if sameDirFile(call.File, tm.File) || callerImportsFile(call.File, tm.File, importIndex) {
								conf = 0.6
								evidence = "field_based_unique"
							}
						case len(set) <= 2:
							conf = 0.5
						case len(set) <= 5:
							conf = 0.4
						default:
							conf = 0.2
						}
						for _, tid := range set {
							putEdge(ResolvedCall{
								SourceNodeID:     callerID,
								TargetNodeID:     tid,
								SourceLine:       call.Line,
								SourceFile:       call.File,
								Method:           "field_based",
								Confidence:       conf,
								CandidateCount:   len(set),
								CandidateNodeIDs: set,
								TrustTier:        tierFor(conf),
								EvidenceType:     evidence,
							})
						}
						continue
					}
					callerDir := filepath.ToSlash(filepath.Dir(call.File))
					var reachable []int64
					for _, tid := range candidates {
						tm := metaMap[tid]
						if tm.File == "" {
							continue
						}
						if tm.File == call.File ||
							callerImportsFile(call.File, tm.File, importIndex) ||
							filepath.ToSlash(filepath.Dir(tm.File)) == callerDir {
							reachable = append(reachable, tid)
							continue
						}
						if fileImps, ok := importIndex[call.File]; ok {
							if starFiles, ok := fileImps["*"]; ok {
								tgtDir := filepath.ToSlash(filepath.Dir(tm.File))
								for _, sf := range starFiles {
									if filepath.ToSlash(filepath.Dir(sf)) == tgtDir || sf == tm.File {
										reachable = append(reachable, tid)
										break
									}
								}
							}
						}
					}
					// Prefer a caller-reachable target for the pick; if NONE is reachable,
					// keep the edge rather than DROP (Fable #4): Tier-2 languages have no
					// import extractor → reachability collapses to same-file/same-dir, so a
					// hard drop would thin graphs whose ONLY edges are name_match AND deny the
					// demand-driven LSP pass a call site to resolve. Pre-E these were kept at
					// 0.2-0.6; the bug was the false 0.6 CANDIDATE + plain label, already fixed
					// by the 0.2 floor + honest label below — so keeping at 0.2 de-certifies
					// the false CANDIDATE while preserving connectivity. 0.2 is under the 0.5
					// fact gate AND the BFS min_edge_conf, so it never poses as a fact or
					// misdirects path-decay either way.
					pickFrom := reachable
					if len(pickFrom) == 0 {
						pickFrom = candidates
					}
					best := pickBestNameMatchTarget(pickFrom, callerID, call.File, metaMap)
					if best == 0 {
						continue
					}
					// SPECULATIVE floor (0.2), NOT computeConfidence (LIPI BUG 5): a
					// multi-candidate untyped call is strictly MORE ambiguous than the
					// single-candidate case above, which reaches 0.6 only on IMPORT proof and
					// stays 0.2 on same-dir-only. computeConfidence gave a 2-candidate 0.5
					// (P2-9 lowered cc==2 from 0.6→0.5) — still out-ranking that 1-candidate
					// 0.2, an inversion. 0.2 keeps it below the 0.5 fact gate; the
					// demand-driven LSP pass can still upgrade it.
					conf := 0.2
					putEdge(ResolvedCall{
						SourceNodeID:     callerID,
						TargetNodeID:     best,
						SourceLine:       call.Line,
						SourceFile:       call.File,
						Method:           "name_match",
						Confidence:       conf,
						CandidateCount:   len(candidates),
						CandidateNodeIDs: candidates,
						TrustTier:        tierFor(conf),
						EvidenceType:     "name_match_qualified_unresolved",
					})
					continue
				}
			}
		}

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
				putEdge(ResolvedCall{
					SourceNodeID:     callerID,
					TargetNodeID:     bestTarget,
					SourceLine:       call.Line,
					SourceFile:       call.File,
					Method:           "name_match",
					Confidence:       conf,
					CandidateCount:   candidateCount,
					CandidateNodeIDs: candidates,
					TrustTier:        tierFor(conf),
					EvidenceType:     evidence,
				})
			}
		}
	nextCall:
	}

	if withProvenance {
		externalQualifiers := make(map[string]map[string]bool)
		for _, imp := range allImports {
			if !moduleProvablyExternal(imp.ModulePath, fileMap, buildProjectPathSegments(fileMap)) {
				continue
			}
			if externalQualifiers[imp.File] == nil {
				externalQualifiers[imp.File] = make(map[string]bool)
			}
			externalQualifiers[imp.File][imp.ImportedName] = true
		}
		for i, call := range allCalls {
			trace := &callsites[i]
			if call.ParserIncomplete {
				trace.DispatchState = DispatchParserIncomplete
				trace.ParserComplete = false
				trace.CandidateNodeIDs = nil
				trace.SelectedTargetNodeID = nil
				continue
			}
			if len(trace.CandidateNodeIDs) != 0 {
				if err := trace.Validate(); err != nil {
					trace.DispatchState = DispatchParserIncomplete
					trace.ParserComplete = false
					trace.CandidateNodeIDs = nil
					trace.SelectedTargetNodeID = nil
				}
				continue
			}
			qualifier := ""
			if dot := strings.IndexAny(call.CalleeQualified, ".:"); dot > 0 {
				qualifier = call.CalleeQualified[:dot]
			}
			if qualifier != "" && externalQualifiers[call.File][qualifier] {
				trace.DispatchState = DispatchExternalUnresolved
				trace.Mechanism = "external"
			} else if call.CalleeQualified != "" && call.CalleeQualified != call.CalleeName {
				trace.DispatchState = DispatchDynamic
				trace.Mechanism = "dynamic"
				trace.DynamicDispatch = true
			} else {
				trace.DispatchState = DispatchZero
			}
		}
	}

	return resolved, callsites
}

// moduleProvablyExternal reports whether a module path is PROVABLY external — i.e. no
// indexed project file could correspond to it. Used ONLY by the B1 negative-evidence guard
// (F2): a bare imported name is dropped as external-bound only on this POSITIVE evidence,
// never on a mere importIndex miss (resolveModulePath is known-incomplete). Conservative /
// correct-or-quiet — returns false (UNCERTAIN, do not drop) whenever a project file might be
// the target:
//   - empty module path (unknown provenance);
//   - a RELATIVE import (`./x`, `../x`, `/x`) — always meant to point at a project file, so a
//     resolve miss is a resolver gap, not externality;
//   - ANY `.`/`/`/`::`-separated segment names an indexed file/dir (a fileMap key), or the
//     whole path (or its slash form) is a fileMap key — a project module we merely failed to
//     resolve at the name level.
//
// Only a non-relative module whose every segment is unknown to the project is "external".
//
// projectSegs (F9, monorepo/workspace soundness): the set of PATH SEGMENTS of every
// indexed file (dirs + basenames + stems), built once per Resolve by
// buildProjectPathSegments. fileMap KEYS alone under-represent workspace layouts —
// a pnpm/yarn scoped package `@org/utils` maps to `packages/utils/…` whose "utils"
// directory is a path segment but NOT a fileMap key (JS registration only keys
// stems/index-dirs), so the old key-only check wrongly declared a genuinely-internal
// workspace import "provably external" and dropped its calls. Any module segment
// that names ANY path segment of ANY indexed file ⇒ UNCERTAIN ⇒ never dropped
// (conservative: this can only reduce drops, back toward today's behavior).
func moduleProvablyExternal(modulePath string, fileMap map[string][]string, projectSegs map[string]bool) bool {
	if modulePath == "" {
		return false
	}
	if strings.HasPrefix(modulePath, ".") || strings.HasPrefix(modulePath, "/") {
		return false
	}
	if _, ok := fileMap[modulePath]; ok {
		return false
	}
	if _, ok := fileMap[strings.ReplaceAll(modulePath, ".", "/")]; ok {
		return false
	}
	segs := strings.FieldsFunc(modulePath, func(r rune) bool {
		return r == '.' || r == '/' || r == ':'
	})
	for _, s := range segs {
		if s == "" {
			continue
		}
		if _, ok := fileMap[s]; ok {
			return false
		}
		if projectSegs[s] {
			return false // a project dir/file segment carries this name → uncertain
		}
	}
	return true
}

// buildProjectPathSegments returns every path segment (directory names, file
// basenames, and extension-stripped stems) of every indexed file in fileMap's
// values. Consulted by moduleProvablyExternal (F9): a module path touching any of
// these names is never "provably external". Built ONCE per Resolve (only under
// GT_NEG_EVIDENCE) — set-membership only, no ordering dependence (deterministic).
func buildProjectPathSegments(fileMap map[string][]string) map[string]bool {
	segs := make(map[string]bool)
	for _, files := range fileMap {
		for _, f := range files {
			for _, s := range strings.Split(filepath.ToSlash(f), "/") {
				if s == "" {
					continue
				}
				segs[s] = true
				if ext := filepath.Ext(s); ext != "" {
					if stem := strings.TrimSuffix(s, ext); stem != "" {
						segs[stem] = true
					}
				}
			}
		}
	}
	return segs
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
			// The bare-name "*" wildcard (Strategy 1.5 resolves an UNQUALIFIED call to ANY
			// file behind a "*" entry at import conf 1.0) is ONLY sound for a GENUINE whole-
			// module/star import that brings names into unqualified scope: Python `from m
			// import *`, ES `import * as x`, Go package / dot-import, Java `import p.*` — all
			// of which the parser already emits with ImportedName=="*", so they populate
			// fileEntry["*"] via the line above. It must NOT be seeded for a SPECIFIC named
			// import (`from m import foo`, `import {foo}`, `const {foo}=require`): that binds
			// only `foo`, so a bare same-named call to an UNRELATED symbol must not resolve
			// via "*" at 1.0 (correct-or-quiet). A whole-module require binding
			// (`const x=require('./m')`) is called QUALIFIED (`x.foo()`) and resolves via its
			// own `x` package-alias entry (the loop above + the Go-package-qualified path in
			// Strategy 1.5), so it needs no "*" seed either. Previously a "*" was seeded for
			// EVERY non-"*" import, letting any bare same-named call laundered onto the module.
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

	// DETERMINISM (Fable RS2): iterate fm in SORTED key order, never Go map-range order.
	// Two source keys (e.g. src/lib.rs + src/main.rs) fold into ONE module key; the append
	// order into fm[key] below then decides which target the "first target file" import
	// resolver (imports.go) picks — a randomized range flips CALLS/IMPORTS targets
	// run-to-run and makes graph.db non-byte-identical.
	fmKeys := make([]string, 0, len(fm))
	for key := range fm {
		fmKeys = append(fmKeys, key)
	}
	sort.Strings(fmKeys)
	for _, key := range fmKeys {
		files := fm[key]
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

	// Iterate to a FIXPOINT so transitive barrel chains resolve fully: A re-exports
	// from B which re-exports from C. A single pass registers only each barrel's
	// DIRECT source; the next pass lets a barrel's freshly-registered leaf files
	// become visible to the barrels that import IT, and so on down the chain. HARD
	// depth cap 16 — stop when a pass adds nothing (converged) OR the cap is hit
	// (cycle / pathological chain). Deterministic: fm keys are collected and SORTED
	// before the reverse-map build, so registration order never depends on Go map
	// iteration order.
	const maxReExportDepth = 16
	totalChained := 0
	for depth := 0; depth < maxReExportDepth; depth++ {
		// Rebuild the reverse map (file path → fileMap keys pointing to it) from the
		// CURRENT fm each pass — the previous pass mutated fm.
		fileToKeys := make(map[string][]string)
		keys := make([]string, 0, len(fm))
		for key := range fm {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			for _, fp := range fm[key] {
				fileToKeys[fp] = append(fileToKeys[fp], key)
			}
		}

		passChained := 0
		for _, re := range reExports {
			// Resolve the source module to file(s) via the shared 4-language resolver
			// (resolveReExportTargets) — the SAME path ResolveReExports uses to emit
			// RE_EXPORTS edges, so alias-chaining and edge-emission can never diverge.
			sourceFiles := resolveReExportTargets(re, fm)
			if len(sourceFiles) == 0 {
				continue
			}

			// The re-exporting file's directory acts as the barrel. Register each
			// source file under all keys that currently point to the barrel file, so
			// imports through the barrel resolve to the source.
			barrelFile := filepath.ToSlash(re.File)
			barrelKeys := fileToKeys[barrelFile]

			for _, sourceFile := range sourceFiles {
				for _, key := range barrelKeys {
					before := len(fm[key])
					fm[key] = appendUnique(fm[key], sourceFile)
					// Count only ACTUAL additions (appendUnique dedups) so a pass that
					// registers nothing new signals convergence and stops the loop.
					if len(fm[key]) != before {
						passChained++
					}
				}
			}
		}

		totalChained += passChained
		if passChained == 0 {
			break // fixpoint reached — no new alias this pass
		}
	}

	return totalChained
}

// resolveReExportTargets resolves a re-export's SourceModule to the indexed
// source file(s) it points at. Shared by ChainReExports (alias chaining) and
// ResolveReExports (RE_EXPORTS edge emission) so the two can never use different
// resolution and silently disagree — the exact bug that left RE_EXPORTS at 0
// edges (alias chaining worked off the AST ReExportRef while the edge used a
// weaker JS/TS-only line-regex). Language-agnostic: the extension/index/mod.rs
// probing covers TS/JS barrels, Python __init__.py, and Rust pub use alike.
func resolveReExportTargets(re parser.ReExportRef, fm map[string][]string) []string {
	sourceFiles := resolveModulePath(re.SourceModule, fm)
	if len(sourceFiles) > 0 {
		return sourceFiles
	}
	// Relative resolution from the re-exporting file's directory.
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
	} else if strings.HasPrefix(rel, ".") {
		// Python relative import (.mod / ..pkg.mod): leading dots are package
		// levels (one = current package dir, each extra = one parent up); the
		// remainder is a dotted submodule path (a.b -> a/b).
		dots := 0
		for dots < len(rel) && rel[dots] == '.' {
			dots++
		}
		for i := 1; i < dots; i++ {
			if didx := strings.LastIndex(dir, "/"); didx >= 0 {
				dir = dir[:didx]
			} else {
				dir = ""
			}
		}
		rel = strings.ReplaceAll(rel[dots:], ".", "/")
	} else {
		// Bare module name. Tree-sitter strips Python's leading dot from
		// module_name (parser.go:1641), so a same-package re-export arrives here
		// as a plain "mod"; resolve it relative to the re-exporting file's dir
		// (dotted submodule -> path). resolveModulePath already tried the absolute
		// form, so an external package simply finds no local file -> nil (quiet).
		rel = strings.ReplaceAll(rel, ".", "/")
	}
	var base string
	if dir != "" {
		base = dir + "/" + rel
	} else {
		base = rel
	}
	for _, ext := range []string{"", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs",
		"/index.ts", "/index.js", "/index.tsx", "/index.jsx", "/index.mjs", "/index.cjs", "/mod.rs"} {
		if files, ok := fm[base+ext]; ok {
			return files
		}
	}
	return nil
}

// ResolveReExports materializes RE_EXPORTS edges (re-exporting file -> source
// file) from the parser's ReExportRef AST extraction. This REPLACES the JS/TS-only
// line-regex that previously left RE_EXPORTS at 0 edges on every real repo: the
// parser already emits ReExportRef for TS/JS barrels, Python __init__.py, and Rust
// pub use, and resolveReExportTargets resolves all of them — so this is one
// language-agnostic pass with no per-language branch. Non-invention: an edge is
// emitted only when BOTH the barrel file and the resolved source file have real
// anchor nodes; an unresolved source is dropped (correct-or-quiet).
func ResolveReExports(db *store.DB, reExports []parser.ReExportRef, fm map[string][]string, files []walker.SourceFile) (int, error) {
	if len(reExports) == 0 {
		return 0, nil
	}
	fileNodeMap := buildFileNodeMap(db, files)
	if len(fileNodeMap) == 0 {
		return 0, nil
	}
	var edges []*store.Edge
	seen := make(map[edgeKey]bool)
	for _, re := range reExports {
		sourceFiles := resolveReExportTargets(re, fm)
		if len(sourceFiles) == 0 {
			continue
		}
		srcID := fileNodeMap[filepath.ToSlash(re.File)]
		if srcID == 0 {
			continue
		}
		for _, tf := range sourceFiles {
			tgtID := fileNodeMap[tf]
			if tgtID == 0 || tgtID == srcID {
				continue // non-invention / no self-edge
			}
			key := edgeKey{sourceID: srcID, targetID: tgtID, typ: "RE_EXPORTS"}
			if seen[key] {
				continue
			}
			seen[key] = true
			edges = append(edges, &store.Edge{
				SourceID:           srcID,
				TargetID:           tgtID,
				Type:               "RE_EXPORTS",
				SourceLine:         re.Line,
				SourceFile:         re.File,
				ResolutionMethod:   "re_export",
				Confidence:         1.0,
				TrustTier:          tierFor(1.0),
				CandidateCount:     1,
				EvidenceType:       "re_export",
				VerificationStatus: "unverified",
			})
		}
	}
	if len(edges) == 0 {
		return 0, nil
	}
	if err := db.BatchInsertEdges(edges); err != nil {
		return 0, err
	}
	return len(edges), nil
}

// ResolveComposesFromAssignments emits COMPOSES edges for INSTANCE-attribute
// composition — `self.x = ClassName()` / `self.x: ClassName` (Python __init__) and
// `this.x = new Foo()` (JS/TS constructor) — the dominant dynamic-language composition
// idiom that extractClassFields (class-body-only) cannot see, leaving COMPOSES at 0 on
// __init__-style repos (httpx) even though they compose heavily. The parser already
// records these as AssignmentRef (VarName="self.x", TypeName=ClassName, non-ViaReturn);
// this turns the receiver-attribute ones into the structural edge. Resolution is
// unambiguous-only (the bare type name maps to exactly ONE class) and deduped against
// any class-level COMPOSES already emitted — never a guess, never a double.
func ResolveComposesFromAssignments(db *store.DB, assignments []parser.AssignmentRef) (int, error) {
	if len(assignments) == 0 {
		return 0, nil
	}
	type classRange struct {
		id         int64
		start, end int
	}
	ranges := map[string][]classRange{}
	nameCount := map[string]int{}
	nameID := map[string]int64{}
	classLabelsSet := map[string]bool{"Class": true, "Struct": true, "Interface": true, "Enum": true, "Type": true}
	tx, err := db.BeginTx()
	if err != nil {
		return 0, err
	}
	rows, err := tx.Query(`SELECT id, name, file_path, COALESCE(start_line,0), COALESCE(end_line,0), label FROM nodes`)
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	for rows.Next() {
		var id int64
		var name, file, label string
		var start, end int
		if err := rows.Scan(&id, &name, &file, &start, &end, &label); err != nil {
			continue
		}
		if !classLabelsSet[label] {
			continue
		}
		ranges[file] = append(ranges[file], classRange{id, start, end})
		nameCount[name]++
		if _, ok := nameID[name]; !ok {
			nameID[name] = id
		}
	}
	rows.Close()

	// Dedup against COMPOSES already in the DB (class-level promoteComposes ran first).
	seen := map[edgeKey]bool{}
	if er, err := tx.Query(`SELECT source_id, target_id FROM edges WHERE type='COMPOSES'`); err == nil {
		for er.Next() {
			var s, t int64
			if er.Scan(&s, &t) == nil {
				seen[edgeKey{sourceID: s, targetID: t, typ: "COMPOSES"}] = true
			}
		}
		er.Close()
	}
	tx.Rollback() // read-only — release before BatchInsertEdges opens its own tx

	var edges []*store.Edge
	for _, a := range assignments {
		if a.ViaReturn {
			continue // factory call, not a constructor — TypeName is a callee, not a class
		}
		if !strings.HasPrefix(a.VarName, "self.") && !strings.HasPrefix(a.VarName, "this.") {
			continue // only receiver-attribute composition
		}
		typ := a.TypeName
		if i := strings.LastIndexAny(typ, ".:"); i >= 0 {
			typ = typ[i+1:]
		}
		typ = stripTypeGenerics(strings.TrimLeft(strings.TrimSpace(typ), "*&"))
		if typ == "" || nameCount[typ] != 1 {
			continue // unambiguous-only — never guess across same-named classes
		}
		targetID := nameID[typ]
		// Owner = the innermost class whose line range contains the assignment.
		var ownerID int64
		bestSize := 1 << 30
		for _, cr := range ranges[a.File] {
			if cr.start <= a.Line && a.Line <= cr.end && (cr.end-cr.start) < bestSize {
				ownerID, bestSize = cr.id, cr.end-cr.start
			}
		}
		if ownerID == 0 || ownerID == targetID {
			continue
		}
		key := edgeKey{sourceID: ownerID, targetID: targetID, typ: "COMPOSES"}
		if seen[key] {
			continue
		}
		seen[key] = true
		edges = append(edges, &store.Edge{
			SourceID:           ownerID,
			TargetID:           targetID,
			Type:               "COMPOSES",
			SourceLine:         a.Line,
			SourceFile:         a.File,
			ResolutionMethod:   "promote_composes_init",
			Confidence:         0.85,
			TrustTier:          tierFor(0.85),
			CandidateCount:     1,
			EvidenceType:       "instance_field",
			VerificationStatus: "unverified",
		})
	}
	if len(edges) == 0 {
		return 0, nil
	}
	if err := db.BatchInsertEdges(edges); err != nil {
		return 0, err
	}
	return len(edges), nil
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
			// Derive the crate source root GENERICALLY from the Cargo convention that
			// every crate's modules live under its own `src/` dir — NO per-repo path
			// literals. The "/src/" strip below removes any crate dir at ANY nesting
			// depth (a member crate under a workspace, at any path), so only the ROOT
			// crate's LEADING `src/` needs an explicit strip (it has no preceding
			// "/src/" for the general pass to catch).
			if strings.HasPrefix(slashPath, "src/") {
				slashPath = strings.TrimPrefix(slashPath, "src/")
			}
			// Strip any path up to and including the LAST "/src/" — the crate's src root
			// at any depth. This subsumes the removed hardcoded workspace prefixes.
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
